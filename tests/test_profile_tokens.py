"""逐档案只读订阅 Token 与敏感响应脱敏测试（Task 5）。

- Profile Token：43 字符 urlsafe 明文仅签发响应出现一次，库中只存 SHA-256；
- ``GET /api/configs/profiles/{id}/subscribe?token=...`` 是唯一免管理会话路径，
  任何失败（缺 token/格式错/不匹配/未配置/档案不存在）统一 404；
- 轮换立即失效旧 URL；POST token 仍在管理边界（会话 + CSRF）内；
- 档案/节点/订阅普通列表最小披露；MITM 秘密仅 configured 状态 + 覆盖式写入。
"""

import hashlib
import json
import re

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import patch

TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")


@pytest_asyncio.fixture
async def secure_app(tmp_path, monkeypatch):
    """临时库 + 手动驱动 lifespan（与 test_api_endpoints 相同装配）"""
    from app.main import app as fastapi_app
    from app.database import DatabaseRuntime

    monkeypatch.setenv("SURGECONF_DATA_DIR", str(tmp_path / "runtime"))
    test_runtime = DatabaseRuntime.create(tmp_path / "runtime" / "surge_conf.db")

    with patch("app.database.database", test_runtime), \
         patch("app.bootstrap.database", test_runtime):
        async with fastapi_app.router.lifespan_context(fastapi_app):
            yield fastapi_app, test_runtime

    await test_runtime.dispose()


async def _make_client(secure_app, peer: str) -> AsyncClient:
    app, runtime = secure_app
    client = AsyncClient(transport=ASGITransport(app=app, client=(peer, 51234)),
                         base_url=f"http://{peer}")
    client._test_factory = runtime.session_factory
    return client


@pytest_asyncio.fixture
async def admin_client(secure_app):
    """回环管理员：GET / 获得隐式会话并从 meta 提取 CSRF"""
    client = await _make_client(secure_app, "127.0.0.1")
    page = await client.get("/")
    meta = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
    assert meta, "回环 GET / 应签发隐式会话并输出 CSRF meta"
    client.headers["X-CSRF-Token"] = meta.group(1)
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def anonymous_client(secure_app):
    """非回环匿名对端：无任何会话（local_only 下普通管理路径 403）"""
    client = await _make_client(secure_app, "203.0.113.50")
    yield client
    await client.aclose()


async def _create_second_profile(admin_client) -> int:
    resp = await admin_client.post("/api/configs/profiles", json={"name": "第二档案"})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _seed_subscription_and_node(admin_client) -> int:
    """直插数据库：含敏感 URL 的订阅 + 含密码配置行的节点"""
    from app.models import Subscription, ProxyNode

    async with admin_client._test_factory() as db:
        sub = Subscription(name="测试订阅", url="https://secret.example/clash?token=SECRET")
        db.add(sub)
        await db.commit()
        await db.refresh(sub)
        db.add(ProxyNode(
            name="HK-01", node_type="trojan", server="1.2.3.4", port=443,
            config="HK-01 = trojan, 1.2.3.4, 443, password=topsecret",
            subscription_id=sub.id, enabled=True,
        ))
        await db.commit()
        return sub.id


# ===== Token 签发 / 校验 / 轮换 =====


@pytest.mark.asyncio
async def test_profile_token_reads_only_its_profile(admin_client, anonymous_client):
    """有效 token 只能读取所属档案；跨档案与错误 token 统一 404"""
    pid2 = await _create_second_profile(admin_client)
    token = (await admin_client.post("/api/configs/profiles/1/token")).json()["token"]
    assert (await anonymous_client.get(f"/api/configs/profiles/1/subscribe?token={token}")).status_code == 200
    assert (await anonymous_client.get(f"/api/configs/profiles/{pid2}/subscribe?token={token}")).status_code == 404
    assert (await anonymous_client.get("/api/configs/profiles/1/subscribe?token=bad")).status_code == 404


@pytest.mark.asyncio
async def test_subscribe_failures_are_uniform_404(admin_client, anonymous_client):
    """缺 token/空 token/格式错/未配置/档案不存在：一律 404，不泄露差异"""
    await _create_second_profile(admin_client)  # id=2 未配置 token
    cases = [
        "/api/configs/profiles/1/subscribe",                    # 缺 token
        "/api/configs/profiles/1/subscribe?token=",             # 空 token
        "/api/configs/profiles/1/subscribe?token=short",        # 长度不符
        "/api/configs/profiles/1/subscribe?token=" + "!" * 43,  # 字符集外
        "/api/configs/profiles/2/subscribe?token=" + "A" * 43,  # 档案存在但未配置
        "/api/configs/profiles/9999/subscribe?token=" + "A" * 43,  # 档案不存在
    ]
    for url in cases:
        resp = await anonymous_client.get(url)
        assert resp.status_code == 404, f"{url} 应为 404，实际 {resp.status_code}"
        assert "不存在" not in resp.text  # 错误文案不区分原因


@pytest.mark.asyncio
async def test_token_format_and_hash_only_storage(admin_client):
    """token_urlsafe(32) 43 字符；数据库只保存 SHA-256 哈希"""
    from sqlalchemy import select
    from app.models import ConfigProfile

    resp = await admin_client.post("/api/configs/profiles/1/token")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"profile_id", "token"}
    token = body["token"]
    assert TOKEN_RE.fullmatch(token), f"token 应为 43 字符 urlsafe：{len(token)}"

    async with admin_client._test_factory() as db:
        profile = (await db.execute(
            select(ConfigProfile).where(ConfigProfile.id == 1))).scalar_one()
        assert profile.subscription_token_hash == hashlib.sha256(token.encode("ascii")).hexdigest()
    assert json.dumps(body) and "subscription_token_hash" not in body


@pytest.mark.asyncio
async def test_rotation_invalidates_old_url(admin_client, anonymous_client):
    """再次签发即轮换：旧 token 立即 404，新 token 可用"""
    t1 = (await admin_client.post("/api/configs/profiles/1/token")).json()["token"]
    assert (await anonymous_client.get(f"/api/configs/profiles/1/subscribe?token={t1}")).status_code == 200
    t2 = (await admin_client.post("/api/configs/profiles/1/token")).json()["token"]
    assert t1 != t2
    assert (await anonymous_client.get(f"/api/configs/profiles/1/subscribe?token={t1}")).status_code == 404
    assert (await anonymous_client.get(f"/api/configs/profiles/1/subscribe?token={t2}")).status_code == 200


@pytest.mark.asyncio
async def test_token_issue_stays_behind_admin_boundary(admin_client, anonymous_client):
    """POST token 必须管理会话 + CSRF；local_only 非回环 403"""
    assert (await anonymous_client.post("/api/configs/profiles/1/token")).status_code == 403
    wrong_csrf = await admin_client.post("/api/configs/profiles/1/token",
                                         headers={"X-CSRF-Token": "wrong"})
    assert wrong_csrf.status_code == 403
    assert (await admin_client.post("/api/configs/profiles/9999/token")).status_code == 404


@pytest.mark.asyncio
async def test_subscribe_exempt_is_get_only(admin_client, anonymous_client):
    """放行严格限定 GET：同路径 POST 仍走管理边界（local_only 非回环 403）"""
    assert (await anonymous_client.post("/api/configs/profiles/1/subscribe?token=x")).status_code == 403


@pytest.mark.asyncio
async def test_legacy_subscribe_endpoint_removed(admin_client):
    """旧未认证 /api/configs/subscribe 已移除"""
    assert (await admin_client.get("/api/configs/subscribe?profile_id=1")).status_code == 404


@pytest.mark.asyncio
async def test_subscribe_returns_generated_config(admin_client, anonymous_client):
    token = (await admin_client.post("/api/configs/profiles/1/token")).json()["token"]
    resp = await anonymous_client.get(f"/api/configs/profiles/1/subscribe?token={token}")
    assert resp.status_code == 200
    assert "[General]" in resp.text
    assert resp.headers["profile-update-interval"] == "24"
    assert resp.headers["subscription-userinfo"] == "managed-by=SurgeConf"


# ===== 列表最小披露 / 详情端点 =====


@pytest.mark.asyncio
async def test_list_responses_exclude_credentials(admin_client):
    await _seed_subscription_and_node(admin_client)
    profile = (await admin_client.get("/api/configs/profiles")).json()[0]
    assert "mitm" not in profile and "general" not in profile
    node = (await admin_client.get("/api/nodes")).json()[0]
    assert "config" not in node and "server" not in node
    subscription = (await admin_client.get("/api/subscriptions")).json()[0]
    assert "url" not in subscription


@pytest.mark.asyncio
async def test_profiles_list_is_minimal_and_no_legacy_token(admin_client):
    """字段白名单；迁移不给旧档案自动生成不可见 token"""
    data = (await admin_client.get("/api/configs/profiles")).json()
    assert data
    assert set(data[0]) == {"id", "name", "description", "is_default", "locale",
                            "target", "udp_security_mode", "token_configured"}
    assert data[0]["token_configured"] is False


@pytest.mark.asyncio
async def test_profile_detail_reports_token_configured(admin_client):
    token = (await admin_client.post("/api/configs/profiles/1/token")).json()["token"]
    detail = (await admin_client.get("/api/configs/profiles/1")).json()
    assert detail["token_configured"] is True
    assert token not in json.dumps(detail)
    assert detail["general"]  # 非敏感字段完整返回


@pytest.mark.asyncio
async def test_profile_detail_mitm_secrets_configured_only(admin_client):
    """MITM 详情只返回 configured 状态，不回显明文"""
    await admin_client.put("/api/configs/profiles/1", json={
        "mitm": {"ca-passphrase": "s3cret-pass", "ca-p12": "MIICgA==", "hostname": "a.com"},
    })
    detail = (await admin_client.get("/api/configs/profiles/1")).json()
    assert detail["mitm"]["ca-passphrase"] == {"configured": True}
    assert detail["mitm"]["ca-p12"] == {"configured": True}
    assert detail["mitm"]["hostname"] == "a.com"
    dumped = json.dumps(detail)
    assert "s3cret-pass" not in dumped and "MIICgA==" not in dumped


@pytest.mark.asyncio
async def test_subscription_detail_returns_url_in_admin_boundary(admin_client, anonymous_client):
    """已认证订阅详情返回 URL 供编辑；匿名对端 403"""
    sub_id = await _seed_subscription_and_node(admin_client)
    detail = (await admin_client.get(f"/api/subscriptions/{sub_id}")).json()
    assert detail["url"].startswith("https://secret.example")
    node = (await admin_client.get("/api/nodes")).json()[0]
    node_detail = (await admin_client.get(f"/api/nodes/{node['id']}")).json()
    assert node_detail["server"] == "1.2.3.4"
    assert node_detail["port"] == 443
    assert "password=topsecret" in node_detail["config"]
    assert (await anonymous_client.get(f"/api/subscriptions/{sub_id}")).status_code == 403


@pytest.mark.asyncio
async def test_node_lists_never_expose_server_or_config(admin_client):
    """/api/nodes 与 /api/nodes/by-region 均不泄露 server/port/config"""
    await _seed_subscription_and_node(admin_client)
    for url in ("/api/nodes", "/api/nodes/by-region"):
        dumped = json.dumps((await admin_client.get(url)).json())
        assert "1.2.3.4" not in dumped, url
        assert "topsecret" not in dumped, url


@pytest.mark.asyncio
async def test_subscribe_generation_race_matches_invalid_token_404(
        admin_client, anonymous_client, monkeypatch):
    """确定性竞态：token 验证通过后、生成阶段档案恰好被删（ProfileNotFound），
    响应与无效 token 的 404 完全一致（状态码 + detail），不泄露档案存在性差异"""
    from app.services.profile_resolution import ProfileNotFound

    token = (await admin_client.post("/api/configs/profiles/1/token")).json()["token"]
    baseline = await anonymous_client.get(
        f"/api/configs/profiles/1/subscribe?token={'A' * 43}")
    assert baseline.status_code == 404

    async def _racing_generate(db, profile_id=None, locale_override=None):
        # 模拟验证后档案被并发删除：唯一解析入口抛领域错误
        raise ProfileNotFound(profile_id if profile_id is not None else 1)

    monkeypatch.setattr("app.routers.configs.generate_surge_config", _racing_generate)
    raced = await anonymous_client.get(f"/api/configs/profiles/1/subscribe?token={token}")
    assert raced.status_code == 404
    assert raced.json() == baseline.json()
