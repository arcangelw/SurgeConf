"""管理认证、签名会话与 CSRF 访问边界测试。

访问矩阵（简报 Task 3）：
- local_only：仅回环可管理（隐式签名会话），非回环一律 403；
- lan_authenticated：未登录 API 401 / 页面 302 /login，登录后写操作须带 CSRF；
- 公开路径（/static、/login、/api/auth/*）不暴露业务数据。
"""

import re

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import patch

from app.security import (COOKIE_NAME, hash_admin_password,
                          load_or_create_session_secret,
                          verify_admin_password)

PASSWORD = "correct horse battery staple"


pytestmark = pytest.mark.asyncio

@pytest_asyncio.fixture
async def auth_app(tmp_path, monkeypatch):
    """与 test_api_endpoints 相同的装配：临时库 + 手动驱动 lifespan"""
    from app.main import app as fastapi_app
    from app.database import DatabaseRuntime

    monkeypatch.setenv("SURGECONF_DATA_DIR", str(tmp_path / "runtime"))
    test_runtime = DatabaseRuntime.create(tmp_path / "runtime" / "surge_conf.db")

    with patch("app.database.database", test_runtime), \
         patch("app.bootstrap.database", test_runtime):
        async with fastapi_app.router.lifespan_context(fastapi_app):
            yield fastapi_app

    await test_runtime.dispose()


@pytest_asyncio.fixture
async def client_loopback(auth_app):
    """对端 127.0.0.1（ASGITransport 直接给出 TCP 对端）"""
    transport = ASGITransport(app=auth_app, client=("127.0.0.1", 51234))
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        yield c


@pytest_asyncio.fixture
async def client_from_lan(auth_app):
    """对端 192.168.1.50：非回环的局域网客户端"""
    transport = ASGITransport(app=auth_app, client=("192.168.1.50", 51234))
    async with AsyncClient(transport=transport, base_url="http://192.168.1.50") as c:
        yield c


def _csrf_from_html(html: str) -> str:
    match = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert match, "页面应输出 csrf meta"
    return match.group(1)


async def _enable_lan_mode(client_loopback, password=PASSWORD):
    """回环隐式会话启用局域网管理并设置密码（首次启用路径）"""
    await client_loopback.get("/")  # 获得隐式会话 + CSRF
    csrf = client_loopback.headers.get("X-CSRF-Token") or _csrf_from_html(
        (await client_loopback.get("/")).text)
    resp = await client_loopback.put(
        "/api/settings/access",
        headers={"X-CSRF-Token": csrf},
        json={"installation_role": "home_gateway",
              "admin_access": "lan_authenticated",
              "current_password": None,
              "new_password": password},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ===== 密码编码 =====

def test_password_hash_format_and_verify():
    encoded = hash_admin_password(PASSWORD)
    scheme, n, r, p, salt, digest = encoded.split("$")
    assert scheme == "scrypt" and n == "16384" and r == "8" and p == "1"
    assert len(salt) > 0 and len(digest) > 0  # base64 段
    assert verify_admin_password(PASSWORD, encoded)
    assert not verify_admin_password("wrong password", encoded)
    assert not verify_admin_password(PASSWORD, "garbage")
    assert not verify_admin_password(PASSWORD, "")


def test_password_salt_is_random():
    assert hash_admin_password(PASSWORD) != hash_admin_password(PASSWORD)


def test_session_secret_32_bytes_and_stable(tmp_path):
    secret_path = tmp_path / "session.key"
    first = load_or_create_session_secret(secret_path)
    assert len(first) == 32
    assert load_or_create_session_secret(secret_path) == first
    assert (secret_path.stat().st_mode & 0o777) == 0o600


# ===== local_only：回环隐式管理，非回环一律拒绝 =====

@pytest.mark.parametrize("path", ["/", "/api/subscriptions", "/api/configs/profiles"])
async def test_local_only_rejects_non_loopback(client_from_lan, path):
    assert (await client_from_lan.get(path)).status_code == 403


async def test_local_only_rejects_non_loopback_writes(client_from_lan):
    resp = await client_from_lan.post("/api/subscriptions", json={"name": "x", "url": "http://x"})
    assert resp.status_code == 403


async def test_xff_cannot_spoof_loopback(client_from_lan):
    resp = await client_from_lan.get(
        "/api/configs/profiles",
        headers={"X-Forwarded-For": "127.0.0.1, 10.0.0.1"})
    assert resp.status_code == 403


async def test_loopback_gets_implicit_session_and_manages(client_loopback):
    resp = await client_loopback.get("/")
    assert resp.status_code == 200
    cookie = resp.headers.get("set-cookie", "")
    assert COOKIE_NAME in cookie
    assert "httponly" in cookie.lower()
    assert "samesite=strict" in cookie.lower()
    assert "path=/" in cookie.lower()
    assert "secure" not in cookie.lower()  # http 请求不加 Secure

    csrf = _csrf_from_html(resp.text)
    # 写操作带 CSRF 即可管理；缺失则 403
    no_csrf = await client_loopback.put("/api/settings/config/batch", json={"configs": {}})
    assert no_csrf.status_code == 403
    ok = await client_loopback.put("/api/settings/config/batch",
                                   headers={"X-CSRF-Token": csrf},
                                   json={"configs": {}})
    assert ok.status_code == 200


async def test_implicit_session_cookie_attributes_https(auth_app):
    transport = ASGITransport(app=auth_app, client=("127.0.0.1", 51234))
    async with AsyncClient(transport=transport, base_url="https://127.0.0.1") as c:
        resp = await c.get("/")
        assert "secure" in resp.headers.get("set-cookie", "").lower()


# ===== lan_authenticated：登录 + CSRF =====

async def test_lan_admin_requires_login_and_csrf(client_from_lan, client_loopback):
    await _enable_lan_mode(client_loopback)
    assert (await client_from_lan.get("/api/configs/profiles")).status_code == 401
    login = await client_from_lan.post("/api/auth/login", json={"password": PASSWORD})
    assert login.status_code == 200
    assert (await client_from_lan.put("/api/settings/config/batch", json={"configs": {}})).status_code == 403
    csrf = login.json()["csrf_token"]
    assert (await client_from_lan.put("/api/settings/config/batch",
                                      headers={"X-CSRF-Token": csrf},
                                      json={"configs": {}})).status_code == 200


async def test_lan_unauthenticated_page_redirects_to_login(client_from_lan, client_loopback):
    await _enable_lan_mode(client_loopback)
    resp = await client_from_lan.get("/")
    assert resp.status_code == 307
    assert resp.headers["location"].endswith("/login")


async def test_lan_loopback_also_requires_login(client_loopback):
    await _enable_lan_mode(client_loopback)
    # lan 模式下隐式会话不再签发：回环未登录同样被拒
    client_loopback.cookies.clear()
    resp = await client_loopback.get("/api/configs/profiles")
    assert resp.status_code == 401


async def test_wrong_password_rejected(client_from_lan, client_loopback):
    await _enable_lan_mode(client_loopback)
    resp = await client_from_lan.post("/api/auth/login", json={"password": "nope"})
    assert resp.status_code == 401
    assert (await client_from_lan.get("/api/configs/profiles")).status_code == 401


async def test_logout_clears_session(client_from_lan, client_loopback):
    await _enable_lan_mode(client_loopback)
    login = await client_from_lan.post("/api/auth/login", json={"password": PASSWORD})
    assert login.status_code == 200
    logout = await client_from_lan.post("/api/auth/logout")
    assert logout.status_code == 200
    assert (await client_from_lan.get("/api/configs/profiles")).status_code == 401


async def test_implicit_session_unusable_in_lan_mode(client_loopback, client_from_lan):
    # local_only 下回环获得隐式会话 Cookie
    resp = await client_loopback.get("/")
    assert resp.status_code == 200
    implicit_cookie = resp.cookies.get(COOKIE_NAME)
    assert implicit_cookie

    await _enable_lan_mode(client_loopback)
    # 非回环带着隐式会话 Cookie：salt 不同、无法通过显式会话校验
    client_from_lan.cookies.set(COOKIE_NAME, implicit_cookie)
    assert (await client_from_lan.get("/api/configs/profiles")).status_code == 401


async def test_csrf_bound_to_own_session(auth_app, client_from_lan, client_loopback):
    await _enable_lan_mode(client_loopback)
    other = AsyncClient(transport=ASGITransport(app=auth_app,
                                                client=("192.168.1.51", 51234)),
                        base_url="http://192.168.1.51")
    login_other = await other.post("/api/auth/login", json={"password": PASSWORD})
    assert login_other.status_code == 200
    stolen_csrf = login_other.json()["csrf_token"]

    login = await client_from_lan.post("/api/auth/login", json={"password": PASSWORD})
    assert login.status_code == 200
    # 用别人的 CSRF 值提交：会话不匹配，拒绝
    resp = await client_from_lan.put("/api/settings/config/batch",
                                     headers={"X-CSRF-Token": stolen_csrf},
                                     json={"configs": {}})
    assert resp.status_code == 403
    await other.aclose()


async def test_tampered_cookie_treated_as_anonymous(client_from_lan, client_loopback):
    await _enable_lan_mode(client_loopback)
    login = await client_from_lan.post("/api/auth/login", json={"password": PASSWORD})
    assert login.status_code == 200
    token = client_from_lan.cookies.get(COOKIE_NAME)
    client_from_lan.cookies.set(COOKIE_NAME, token[:-4] + "AAAA")
    assert (await client_from_lan.get("/api/configs/profiles")).status_code == 401


# ===== 公开路径不暴露业务数据 =====

async def test_public_paths_stay_open_without_data(client_from_lan, client_loopback):
    await _enable_lan_mode(client_loopback)
    assert (await client_from_lan.get("/login")).status_code == 200
    assert (await client_from_lan.get("/static/js/app.js")).status_code == 200
    session = await client_from_lan.get("/api/auth/session")
    assert session.status_code == 200
    assert session.json() == {"authenticated": False}


async def test_login_page_redirects_when_authenticated(client_from_lan, client_loopback):
    await _enable_lan_mode(client_loopback)
    login = await client_from_lan.post("/api/auth/login", json={"password": PASSWORD})
    assert login.status_code == 200
    resp = await client_from_lan.get("/login")
    assert resp.status_code == 307


# ===== 访问模式切换端点 =====

async def test_access_first_enable_requires_password(client_loopback):
    await client_loopback.get("/")
    csrf = _csrf_from_html((await client_loopback.get("/")).text)
    resp = await client_loopback.put(
        "/api/settings/access",
        headers={"X-CSRF-Token": csrf},
        json={"installation_role": "home_gateway",
              "admin_access": "lan_authenticated",
              "current_password": None,
              "new_password": None},
    )
    assert resp.status_code == 400


async def test_access_change_requires_current_password(client_from_lan, client_loopback):
    await _enable_lan_mode(client_loopback)
    login = await client_from_lan.post("/api/auth/login", json={"password": PASSWORD})
    csrf = login.json()["csrf_token"]

    wrong = await client_from_lan.put(
        "/api/settings/access",
        headers={"X-CSRF-Token": csrf},
        json={"installation_role": "home_gateway",
              "admin_access": "lan_authenticated",
              "current_password": "bad password",
              "new_password": "new password 123"},
    )
    assert wrong.status_code == 403

    missing = await client_from_lan.put(
        "/api/settings/access",
        headers={"X-CSRF-Token": csrf},
        json={"installation_role": "home_gateway",
              "admin_access": "lan_authenticated",
              "current_password": None,
              "new_password": "new password 123"},
    )
    assert missing.status_code == 403

    ok = await client_from_lan.put(
        "/api/settings/access",
        headers={"X-CSRF-Token": csrf},
        json={"installation_role": "home_gateway",
              "admin_access": "lan_authenticated",
              "current_password": PASSWORD,
              "new_password": "new password 123"},
    )
    assert ok.status_code == 200

    # 旧密码失效、新密码可登录
    assert (await client_from_lan.post("/api/auth/login", json={"password": PASSWORD})).status_code == 401
    relogin = await client_from_lan.post("/api/auth/login", json={"password": "new password 123"})
    assert relogin.status_code == 200


async def test_access_response_and_config_api_never_leak_hash(client_loopback, client_from_lan):
    resp = await _enable_lan_mode(client_loopback)
    assert set(resp.keys()) == {"installation_role", "admin_access", "password_configured"}
    assert resp["password_configured"] is True

    login = await client_from_lan.post("/api/auth/login", json={"password": PASSWORD})
    csrf = login.json()["csrf_token"]

    configs = await client_from_lan.get("/api/settings/config")
    assert "admin_password_hash" not in configs.json()

    hijack = await client_from_lan.put(
        "/api/settings/config/batch",
        headers={"X-CSRF-Token": csrf},
        json={"configs": {"admin_password_hash": "scrypt$1$1$1$AAAA$BBBB"}},
    )
    assert hijack.status_code == 400
