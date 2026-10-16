"""阶段2 新增事务端点集成测试（ASGI 全栈 + 临时 sqlite 文件库）

覆盖：级联删除档案 / set-default 唯一性 / batch config 幂等 / default-order / 批量 unpin
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from unittest.mock import patch

from app.default_config import DEFAULT_RULE_SOURCES
from app.models import ConfigProfile, CustomRule, HostMapping, ProxyNode


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """独立临时库 + 手动驱动 lifespan（ASGITransport 不触发 lifespan 事件）"""
    from app.main import app as fastapi_app
    from app.database import DatabaseRuntime, get_db

    monkeypatch.setenv("SURGECONF_DATA_DIR", str(tmp_path / "runtime"))
    test_runtime = DatabaseRuntime.create(tmp_path / "runtime" / "api_test.db")

    async def override_get_db():
        async with test_runtime.session_factory() as session:
            yield session

    fastapi_app.dependency_overrides[get_db] = override_get_db
    with patch("app.database.database", test_runtime), \
         patch("app.bootstrap.database", test_runtime):
        async with fastapi_app.router.lifespan_context(fastapi_app):
            async with AsyncClient(transport=ASGITransport(app=fastapi_app), base_url="http://test") as c:
                c._test_factory = test_runtime.session_factory
                yield c

    fastapi_app.dependency_overrides.pop(get_db, None)
    await test_runtime.dispose()


@pytest.mark.asyncio
async def test_delete_profile_cascades(client, tmp_path):
    """删除档案：定制规则与 Host 映射同事务消失，档案列表少一档"""
    # 造第二个档案并挂上定制数据
    created = await client.post("/api/configs/profiles", json={"name": "临时档案"})
    assert created.status_code == 200
    pid = created.json()["id"]

    await client.post("/api/configs/custom-rules", json={
        "profile_id": pid, "position": "pre",
        "rule_text": "DOMAIN-SUFFIX,example.com,DIRECT", "enabled": True,
    })
    before = await client.get("/api/configs/profiles")
    n_before = len(before.json())

    resp = await client.delete(f"/api/configs/profiles/{pid}")
    assert resp.status_code == 200

    after = await client.get("/api/configs/profiles")
    assert len(after.json()) == n_before - 1
    assert all(p["id"] != pid for p in after.json())

    # 通过独立会话直查数据库：关联行已清零且已持久化
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/runtime/api_test.db")
    async with async_sessionmaker(engine, class_=AsyncSession)() as s:
        rules = (await s.execute(select(CustomRule).where(CustomRule.profile_id == pid))).scalars().all()
        hosts = (await s.execute(select(HostMapping).where(HostMapping.profile_id == pid))).scalars().all()
        assert rules == [] and hosts == []
    await engine.dispose()

    # 不存在的档案 404
    resp = await client.delete(f"/api/configs/profiles/{pid}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_set_default_exactly_one(client):
    """set-default 后全局恰一条 is_default=1 且为目标档案；404 语义正常"""
    created = await client.post("/api/configs/profiles", json={"name": "新默认"})
    pid = created.json()["id"]

    resp = await client.put(f"/api/configs/profiles/{pid}/set-default")
    assert resp.status_code == 200 and resp.json() == {"ok": True}

    profiles = (await client.get("/api/configs/profiles")).json()
    defaults = [p for p in profiles if p["is_default"]]
    assert len(defaults) == 1 and defaults[0]["id"] == pid

    assert (await client.put(f"/api/configs/profiles/9999/set-default")).status_code == 404


@pytest.mark.asyncio
async def test_batch_config_upsert_idempotent(client):
    """batch upsert 后 GET 返回新值；重复提交值不变"""
    resp = await client.put("/api/settings/config/batch", json={
        "configs": {"exclusion_keywords": "test|kw", "brand_new_key": "1"},
    })
    assert resp.status_code == 200 and resp.json()["updated"] == 2

    cfg = (await client.get("/api/settings/config")).json()
    assert cfg["exclusion_keywords"] == "test|kw"
    assert cfg["brand_new_key"] == "1"

    # 幂等：再跑一次值不变
    await client.put("/api/settings/config/batch", json={
        "configs": {"exclusion_keywords": "test|kw", "brand_new_key": "1"},
    })
    cfg2 = (await client.get("/api/settings/config")).json()
    assert cfg2["exclusion_keywords"] == "test|kw" and cfg2["brand_new_key"] == "1"


@pytest.mark.asyncio
async def test_rules_default_order(client):
    """default-order 返回非空且与内置 DEFAULT_RULE_SOURCES 一一对应"""
    resp = await client.get("/api/configs/rules/default-order")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == len(DEFAULT_RULE_SOURCES) > 0
    assert data[0]["name"] == DEFAULT_RULE_SOURCES[0]["name"]
    assert all("name" in d and "sort_order" in d for d in data)


@pytest.mark.asyncio
async def test_batch_unpin_removes_service(client):
    """pinned=false 批量请求后，节点 pinned_services 不再含该服务"""
    async with client._test_factory() as s:
        nodes = [
            ProxyNode(name="HK-T1", node_type="trojan", server="9.9.9.9", port=443,
                      config="HK-T1 = trojan, 9.9.9.9, 443, password=x", region_auto="香港", enabled=True),
            ProxyNode(name="HK-T2", node_type="trojan", server="9.9.9.8", port=443,
                      config="HK-T2 = trojan, 9.9.9.8, 443, password=x", region_auto="香港", enabled=True),
        ]
        s.add_all(nodes)
        await s.commit()
        ids = [n.id for n in nodes]

    await client.put("/api/nodes/batch/pin", json={
        "node_ids": ids, "service_name": "AI服务", "pinned": True,
    })
    resp = await client.put("/api/nodes/batch/pin", json={
        "node_ids": ids, "service_name": "AI服务", "pinned": False,
    })
    assert resp.status_code == 200

    for nid in ids:
        node = (await client.get(f"/api/nodes/{nid}")).json()
        assert "AI服务" not in (node.get("pinned_services") or [])


@pytest.mark.asyncio
async def test_update_node_null_clears_region(client):
    """显式传 null 清空 region_manual；未提供的字段保持不变"""
    async with client._test_factory() as s:
        node = ProxyNode(name="HK-C1", node_type="trojan", server="9.9.9.7", port=443,
                         config="HK-C1 = trojan, 9.9.9.7, 443, password=x",
                         region_auto="香港", region_manual="香港", enabled=True)
        s.add(node)
        await s.commit()
        nid = node.id

    # 显式 null：清空手动地区（回落 auto）
    resp = await client.put(f"/api/nodes/{nid}", json={"region_manual": None})
    assert resp.status_code == 200
    assert resp.json()["region_manual"] is None

    # 空请求体：所有字段不变
    resp = await client.put(f"/api/nodes/{nid}", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["region_manual"] is None and body["enabled"] is True

    detail = (await client.get(f"/api/nodes/{nid}")).json()
    assert detail["region_auto"] == "香港"
