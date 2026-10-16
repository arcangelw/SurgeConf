"""管理认证核心：scrypt 密码编码、签名会话、访问矩阵判定与 CSRF 校验。

访问矩阵（唯一认证入口，无静态 Bearer 双轨）：
- 公开路径（静态资源、登录端点）放行，但不得返回业务数据；
- ``local_only``：仅回环对端可管理，回环 GET 自动签发隐式签名会话
  （专用 salt，非回环请求永远无法利用）；
- ``lan_authenticated``：有效显式会话放行；未登录 API 401、页面 302 /login；
- 全部状态变更方法（POST/PUT/DELETE/PATCH）必须携带与会话负载一致的
  ``X-CSRF-Token``（``hmac.compare_digest`` 恒时比较）。
"""

import base64
from collections import defaultdict, deque
import hashlib
import hmac
import ipaddress
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from itsdangerous import URLSafeTimedSerializer
from starlette.datastructures import Address
from starlette.requests import Request

COOKIE_NAME = "surgeconf_session"
SESSION_MAX_AGE = 7 * 86400

DEFAULT_INSTALLATION_ROLE = "home_gateway"
DEFAULT_ADMIN_ACCESS = "local_only"
VALID_INSTALLATION_ROLES = {"home_gateway"}
VALID_ADMIN_ACCESS = {"local_only", "lan_authenticated"}

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# 公开路径：静态资源、登录页/登录端点与未认证 session 探测；
# logout 不公开（需有效会话 + CSRF）。它们不读取任何管理数据。
PUBLIC_PATHS = {"/login", "/api/auth/login", "/api/auth/session"}

# 登录限流：每直接对端 60 秒窗口内最多 10 次失败，超限阻断 60 秒
LOGIN_FAILURE_WINDOW = 60.0
LOGIN_FAILURE_LIMIT = 10
LOGIN_BLOCK_SECONDS = 60.0

_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_SALT_BYTES = 16
_SCRYPT_DKLEN = 32
_SECRET_BYTES = 32
# verify 侧只接受编码规范值（n=16384,r=8,p=1），不接受任何参数偏离

_SIGNER_CACHE: dict[str, "SessionSigner"] = {}


# ===== 密码编码：scrypt$n$r$p$salt_b64$digest_b64 =====

def hash_admin_password(password: str) -> str:
    salt = os.urandom(_SCRYPT_SALT_BYTES)
    digest = hashlib.scrypt(password.encode("utf-8"), n=_SCRYPT_N, r=_SCRYPT_R,
                            p=_SCRYPT_P, salt=salt, dklen=_SCRYPT_DKLEN)
    return (f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}"
            f"${base64.b64encode(salt).decode()}"
            f"${base64.b64encode(digest).decode()}")


def verify_admin_password(password: str, encoded: str) -> bool:
    """只接受 n=16384,r=8,p=1、16 字节盐、32 字节摘要；一切检查在 KDF 前"""
    try:
        parts = encoded.split("$")
        if len(parts) != 6:
            return False
        scheme, n_s, r_s, p_s, salt_b64, digest_b64 = parts
        if (scheme != "scrypt" or n_s != str(_SCRYPT_N)
                or r_s != str(_SCRYPT_R) or p_s != str(_SCRYPT_P)):
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(digest_b64, validate=True)
        if len(salt) != _SCRYPT_SALT_BYTES or len(expected) != _SCRYPT_DKLEN:
            return False
        actual = hashlib.scrypt(password.encode("utf-8"), n=_SCRYPT_N, r=_SCRYPT_R,
                                p=_SCRYPT_P, salt=salt, dklen=_SCRYPT_DKLEN)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


# ===== 会话签名密钥 =====

def _read_secret_file(path: Path) -> bytes | None:
    """O_NOFOLLOW 只读打开并校验为 32 字节普通文件；任何异常视作不可用"""
    import stat as stat_module
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        if not stat_module.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks = []
        remaining = _SECRET_BYTES + 1  # 多读 1 字节用于拒绝超长文件
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        return data if len(data) == _SECRET_BYTES else None
    finally:
        os.close(fd)


def load_or_create_session_secret(path: Path) -> bytes:
    """加载（或首次原子创建）32 字节会话签名密钥。

    - 创建用 ``O_CREAT|O_EXCL|O_WRONLY|O_NOFOLLOW`` 一次原子完成（0o600），
      并发竞争失败方重读最终文件，所有调用方拿到同一密钥；
    - 已有合法文件（含 0644 等宽松权限）加载时收敛到 0o600；
    - 符号链接 / 非普通文件 / 长度异常一律拒绝（fail closed），
      密钥丢失仅导致既有会话失效，不涉及业务数据。
    """
    path = Path(path)
    data = _read_secret_file(path)
    if data is not None:
        os.chmod(path, 0o600)
        return data

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    secret = secrets.token_bytes(_SECRET_BYTES)
    try:
        fd = os.open(path,
                     os.O_CREAT | os.O_EXCL | os.O_WRONLY
                     | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        # 另一个调用方赢了竞态，或路径被符号链接/异常文件占据
        data = _read_secret_file(path)
        if data is None:
            raise
        os.chmod(path, 0o600)
        return data
    except OSError:
        # 符号链接（ELOOP）等：拒绝在不安全路径上创建
        raise
    try:
        os.write(fd, secret)
    finally:
        os.close(fd)
    return secret


class SessionSigner:
    """签名会话签发与解析。

    会话负载只含随机 session id、签发时间与 CSRF 随机值；显式（密码登录）
    会话与回环隐式会话使用不同 salt，隐式会话仅在请求对端为回环时才被解析，
    因此非回环请求永远无法利用隐式会话。
    """

    def __init__(self, secret_key: bytes) -> None:
        self._secret = secret_key
        self._serializer = URLSafeTimedSerializer(secret_key)

    def issue(self, *, loopback_implicit: bool) -> tuple[str, str]:
        """签发会话，返回 (token, csrf_token)"""
        payload = {
            "sid": secrets.token_urlsafe(24),
            "iat": int(time.time()),
            "csrf": secrets.token_urlsafe(24),
        }
        token = self._serializer.dumps(payload, salt=self._salt(loopback_implicit))
        return token, payload["csrf"]

    def parse(self, token: str, *, loopback_implicit: bool) -> dict | None:
        """解析并校验签名与时效；任何异常视作无会话"""
        try:
            payload = self._serializer.loads(
                token, salt=self._salt(loopback_implicit), max_age=SESSION_MAX_AGE)
        except Exception:
            return None
        if (not isinstance(payload, dict)
                or not isinstance(payload.get("sid"), str)
                or not isinstance(payload.get("csrf"), str)):
            return None
        return payload

    @staticmethod
    def _salt(loopback_implicit: bool) -> str:
        return ("surgeconf.session.loopback" if loopback_implicit
                else "surgeconf.session")


def get_session_signer() -> SessionSigner:
    """按当前运行时密钥路径取签名器（缓存实例，密钥只读一次）"""
    from app.runtime_paths import resolve_runtime_paths
    secret_path = resolve_runtime_paths().session_secret
    key = str(secret_path)
    signer = _SIGNER_CACHE.get(key)
    if signer is None:
        signer = SessionSigner(load_or_create_session_secret(secret_path))
        _SIGNER_CACHE[key] = signer
    return signer


# ===== 访问设置（数据库唯一事实来源） =====

@dataclass(frozen=True)
class AccessSettings:
    installation_role: str
    admin_access: str
    password_hash: str | None

    @property
    def password_configured(self) -> bool:
        return bool(self.password_hash)


async def load_access_settings() -> AccessSettings:
    """从 system_config 读取访问设置；缺失/非法键收敛到 fail-closed 默认值。

    函数内导入以保证测试对 ``app.database.database`` 的 patch 生效。
    """
    from sqlalchemy import select
    from app.database import database
    from app.models import SystemConfig

    keys = ("installation_role", "admin_access", "admin_password_hash")
    async with database.session_factory() as db:
        result = await db.execute(select(SystemConfig).where(SystemConfig.key.in_(keys)))
        values = {row.key: (row.value or "") for row in result.scalars().all()}

    role = values.get("installation_role") or DEFAULT_INSTALLATION_ROLE
    access = values.get("admin_access") or DEFAULT_ADMIN_ACCESS
    return AccessSettings(
        installation_role=role if role in VALID_INSTALLATION_ROLES else DEFAULT_INSTALLATION_ROLE,
        admin_access=access if access in VALID_ADMIN_ACCESS else DEFAULT_ADMIN_ACCESS,
        password_hash=values.get("admin_password_hash") or None,
    )


# ===== 请求判定 =====

class Decision:
    """中间件判定结果常量"""
    PUBLIC = "public"
    ALLOW = "allow"
    REDIRECT_LOGIN = "redirect_login"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    CSRF_REJECTED = "csrf_rejected"


def is_public_path(path: str) -> bool:
    return path.startswith("/static/") or path in PUBLIC_PATHS


def request_peer_is_loopback(request: Request) -> bool:
    """直接 TCP 对端是否回环；绝不信任 X-Forwarded-For，对端缺失 fail closed"""
    peer: Address | None = request.client
    if peer is None or not peer.host:
        return False
    try:
        return ipaddress.ip_address(peer.host).is_loopback
    except ValueError:
        return False


def resolve_session(request: Request, *, allow_implicit: bool = True) -> dict | None:
    """解析请求携带的有效会话。

    显式会话对任意对端有效；隐式（loopback salt）会话仅在显式允许
    （``local_only`` 模式）且对端为回环时才解析——切换到
    ``lan_authenticated`` 后隐式会话立即失效，LAN 模式只接受显式会话。
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    signer = get_session_signer()
    explicit = signer.parse(token, loopback_implicit=False)
    if explicit is not None:
        return explicit
    if allow_implicit and request_peer_is_loopback(request):
        return signer.parse(token, loopback_implicit=True)
    return None


def resolve_session_for(request: Request, settings: AccessSettings) -> dict | None:
    """按当前访问模式解析会话：仅 local_only 接受回环隐式会话"""
    return resolve_session(request,
                           allow_implicit=settings.admin_access == "local_only")


def require_csrf(request: Request) -> bool:
    """状态变更请求的 CSRF 校验：与会话负载中的随机值恒时比较"""
    session = getattr(request.state, "session", None)
    if not isinstance(session, dict):
        return False
    expected = str(session.get("csrf") or "")
    supplied = request.headers.get("X-CSRF-Token", "")
    return bool(expected) and bool(supplied) and hmac.compare_digest(supplied, expected)


async def require_admin_request(request: Request, settings: AccessSettings) -> str:
    """访问矩阵唯一判定入口。

    返回 :class:`Decision` 常量；放行时有效会话负载挂在
    ``request.state.session``，需要为回环新签发隐式会话时
    ``request.state.issue_implicit_session`` 为 True（由装配层负责 Set-Cookie）。
    """
    loopback = request_peer_is_loopback(request)
    if is_public_path(request.url.path):
        # local_only 下公开路径也仅限本机：非回环连登录页/登录端点都不可见
        if settings.admin_access == "local_only" and not loopback:
            return Decision.FORBIDDEN
        return Decision.PUBLIC

    session = resolve_session_for(request, settings)
    needs_csrf = request.method not in SAFE_METHODS

    if settings.admin_access == "local_only":
        if not loopback:
            return Decision.FORBIDDEN
        if session is None:
            if not needs_csrf:
                # 回环安全方法：签发隐式管理会话（装配层 Set-Cookie），
                # 当前请求即可用该会话渲染 CSRF meta
                _, csrf = _mark_implicit_issue(request)
                request.state.session = {"sid": "", "csrf": csrf}
                return Decision.ALLOW
            return Decision.CSRF_REJECTED
        request.state.session = session
        if needs_csrf and not require_csrf(request):
            return Decision.CSRF_REJECTED
        return Decision.ALLOW

    # lan_authenticated：只接受显式会话（resolve_session_for 已排除隐式）
    if session is None:
        if request.url.path.startswith("/api/"):
            return Decision.UNAUTHORIZED
        return Decision.REDIRECT_LOGIN
    request.state.session = session
    if needs_csrf and not require_csrf(request):
        return Decision.CSRF_REJECTED
    return Decision.ALLOW


def _mark_implicit_issue(request: Request) -> tuple[str, str]:
    token, csrf = get_session_signer().issue(loopback_implicit=True)
    request.state.issue_implicit_session = token
    return token, csrf


# ===== 登录限流（按直接 TCP 对端，不信任任何转发头） =====


_login_failures: dict[str, deque] = defaultdict(deque)
_login_blocked_until: dict[str, float] = {}


def _peer_key(request: Request) -> str:
    peer: Address | None = request.client
    return peer.host if peer and peer.host else "unknown-peer"


def login_throttle_wait(request: Request) -> int | None:
    """登录前检查：被阻断时返回建议等待秒数（>=1），否则 None。"""
    key = _peer_key(request)
    now = time.monotonic()
    until = _login_blocked_until.get(key, 0.0)
    if now < until:
        return int(until - now) + 1
    failures = _login_failures.get(key)
    if failures is not None:
        while failures and now - failures[0] >= LOGIN_FAILURE_WINDOW:
            failures.popleft()
    return None


def record_login_failure(request: Request) -> None:
    key = _peer_key(request)
    now = time.monotonic()
    failures = _login_failures[key]
    failures.append(now)
    while failures and now - failures[0] >= LOGIN_FAILURE_WINDOW:
        failures.popleft()
    if len(failures) >= LOGIN_FAILURE_LIMIT:
        _login_blocked_until[key] = now + LOGIN_BLOCK_SECONDS
        failures.clear()


def record_login_success(request: Request) -> None:
    _login_failures.pop(_peer_key(request), None)
    _login_blocked_until.pop(_peer_key(request), None)
