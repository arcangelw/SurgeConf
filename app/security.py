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

# 公开路径：静态资源与登录端点；它们不读取任何管理数据
PUBLIC_PATHS = {"/login", "/api/auth/login", "/api/auth/logout", "/api/auth/session"}

_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_SALT_BYTES = 16
_SCRYPT_DKLEN = 32
# 解码时防止库内被篡改参数造成 DoS
_SCRYPT_PARAM_LIMITS = (2 ** 20, 64, 32)

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
    try:
        scheme, n_s, r_s, p_s, salt_b64, digest_b64 = encoded.split("$")
        n, r, p = int(n_s), int(r_s), int(p_s)
        if scheme != "scrypt":
            return False
        if not all(0 < v <= lim for v, lim in zip((n, r, p), _SCRYPT_PARAM_LIMITS)):
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(digest_b64, validate=True)
        if not salt or not expected:
            return False
        actual = hashlib.scrypt(password.encode("utf-8"), n=n, r=r, p=p,
                                salt=salt, dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


# ===== 会话签名密钥 =====

def load_or_create_session_secret(path: Path) -> bytes:
    """加载（或首次生成）32 字节会话签名密钥；密钥文件收敛到 0o600。

    密钥丢失仅导致既有会话失效，不涉及业务数据，损坏/长度异常时重新生成。
    """
    path = Path(path)
    if path.exists():
        data = path.read_bytes()
        if len(data) == 32:
            return data
    secret = secrets.token_bytes(32)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(secret)
    path.chmod(0o600)
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


def resolve_session(request: Request) -> dict | None:
    """解析请求携带的有效会话。

    显式会话对任意对端有效；隐式（loopback salt）会话仅在回环对端时解析，
    其余情况一律视作无会话。
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    signer = get_session_signer()
    explicit = signer.parse(token, loopback_implicit=False)
    if explicit is not None:
        return explicit
    if request_peer_is_loopback(request):
        return signer.parse(token, loopback_implicit=True)
    return None


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
    if is_public_path(request.url.path):
        return Decision.PUBLIC

    loopback = request_peer_is_loopback(request)
    session = resolve_session(request)
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

    # lan_authenticated：隐式会话 salt 不同，天然不通过 resolve_session
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
