"""阶段2 新增事务端点集成测试（ASGI 全栈 + 临时 sqlite 文件库）

覆盖：级联删除档案 / set-default 唯一性 / batch config 幂等 / default-order / 批量 unpin
追加：显式导出端点只写 exports 目录、generate 无服务器写入与安全响应头
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from unittest.mock import patch

from app.default_config import DEFAULT_RULE_SOURCES
from app.models import ConfigProfile, CustomRule, HostMapping, ProxyNode
from app.runtime_paths import resolve_runtime_paths


@pytest.fixture
def runtime_paths(tmp_path, monkeypatch):
    """与 client fixture 同一 SURGECONF_DATA_DIR 的运行时路径（exports 断言用）"""
    monkeypatch.setenv("SURGECONF_DATA_DIR", str(tmp_path / "runtime"))
    return resolve_runtime_paths()


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """独立临时库 + 手动驱动 lifespan（ASGITransport 不触发 lifespan 事件）

    lifespan 会把 runtime 重新绑定到 SURGECONF_DATA_DIR 下的 surge_conf.db，
    因此 create 路径与其保持一致；回环对端经 GET / 获得隐式管理会话后，
    从页面 meta 提取 CSRF 供所有写请求使用。
    """
    import re
    from app.main import app as fastapi_app
    from app.database import DatabaseRuntime, get_db

    monkeypatch.setenv("SURGECONF_DATA_DIR", str(tmp_path / "runtime"))
    test_runtime = DatabaseRuntime.create(tmp_path / "runtime" / "surge_conf.db")

    async def override_get_db():
        async with test_runtime.session_factory() as session:
            yield session

    fastapi_app.dependency_overrides[get_db] = override_get_db
    with patch("app.database.database", test_runtime), \
         patch("app.bootstrap.database", test_runtime):
        async with fastapi_app.router.lifespan_context(fastapi_app):
            async with AsyncClient(transport=ASGITransport(app=fastapi_app), base_url="http://test") as c:
                page = await c.get("/")
                meta = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
                assert meta, "回环 GET / 应签发隐式会话并输出 CSRF meta"
                c.headers["X-CSRF-Token"] = meta.group(1)
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
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/runtime/surge_conf.db")
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


# ===== 配置生成端点：内存输出 + 唯一服务端导出路径 =====


@pytest.mark.asyncio
async def test_export_endpoint_writes_only_exports_dir(client, runtime_paths):
    """显式导出：confirm 门禁、只写 RuntimePaths.exports 且 0600；generate 不写文件"""
    denied = await client.post("/api/configs/1/exports", json={})
    assert denied.status_code == 400

    preview = await client.get("/api/configs/preview?profile_id=1")
    assert preview.status_code == 200
    inline = await client.get("/api/configs/generate?profile_id=1")
    assert inline.status_code == 200
    assert list(runtime_paths.exports.iterdir()) == []

    resp = await client.post("/api/configs/1/exports", json={"confirm": True})
    assert resp.status_code == 200
    exported = resp.json()
    assert exported["name"] and exported["size"] > 0

    files = list(runtime_paths.exports.iterdir())
    assert [f.name for f in files] == [exported["name"]]
    assert files[0].stat().st_mode & 0o777 == 0o600

    # 导出文件内容与内存生成一致（utf-8 配置文本）
    assert files[0].read_text(encoding="utf-8") == inline.text

    # 穿越文件名被消毒为安全的导出名
    resp = await client.post("/api/configs/1/exports",
                             json={"confirm": True, "filename": "../../etc/passwd"})
    assert resp.status_code == 200
    assert "/" not in resp.json()["name"] and ".." not in resp.json()["name"]
    assert all("/" not in f.name and ".." not in f.name
               for f in runtime_paths.exports.iterdir())


@pytest.mark.asyncio
async def test_generate_response_headers_are_safe(client):
    """generate：内存响应 + Content-Disposition 剥除路径并做 RFC 5987 编码"""
    resp = await client.get("/api/configs/generate?profile_id=1&filename=..%2F..%2Fpasswd.conf")
    assert resp.status_code == 200
    disposition = resp.headers["content-disposition"]
    assert "attachment" in disposition
    assert ".." not in disposition and "/" not in disposition.split("UTF-8''")[-1].split("%")[0]
    assert "filename*=UTF-8''" in disposition

    # 不带 filename：纯文本响应，无下载头
    resp = await client.get("/api/configs/generate?profile_id=1")
    assert resp.status_code == 200
    assert "content-disposition" not in resp.headers

    # 非 ASCII 文件名走 RFC 5987 编码
    resp = await client.get("/api/configs/generate?profile_id=1&filename=%E9%85%8D%E7%BD%AE.conf")
    disposition = resp.headers["content-disposition"]
    assert "filename*=UTF-8''%E9%85%8D%E7%BD%AE.conf" in disposition
    assert 'filename="Surge.conf"' in disposition


@pytest.mark.asyncio
async def test_profiles_list_and_detail_contract(client):
    """档案列表字段白名单（最小披露）；详情返回完整 MITM 视图但秘密仅 configured"""
    data = (await client.get("/api/configs/profiles")).json()
    assert data
    assert set(data[0]) == {"id", "name", "description", "is_default", "locale",
                            "target", "token_configured"}
    assert data[0]["token_configured"] is False  # 未生成过 token 的档案

    detail = (await client.get("/api/configs/profiles/1")).json()
    assert detail["general"] and "mitm" in detail
    assert detail["mitm"]["ca-passphrase"] == {"configured": False}
    assert detail["mitm"]["ca-p12"] == {"configured": False}


# ===== 目标感知档案：target / udp_security_mode CRUD 边界 =====


async def _detail(client, profile_id: int) -> dict:
    resp = await client.get(f"/api/configs/profiles/{profile_id}")
    assert resp.status_code == 200
    return resp.json()


async def _create(client, name: str, **extra) -> int:
    resp = await client.post("/api/configs/profiles", json={"name": name, **extra})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_create_profile_fills_mode_by_target_default(client):
    """创建未传模式：mac_workstation→scoped，ios_mobile→compatible"""
    mac = await _detail(client, await _create(client, "mac档"))
    assert mac["target"] == "mac_workstation"
    assert mac["udp_security_mode"] == "scoped"

    ios = await _detail(client, await _create(client, "iOS档", target="ios_mobile"))
    assert ios["target"] == "ios_mobile"
    assert ios["udp_security_mode"] == "compatible"


@pytest.mark.asyncio
async def test_create_profile_rejects_ios_scoped_without_commit(client):
    """ios_mobile+scoped 创建：422 且不产生任何新档案（事务未提交）"""
    before = (await client.get("/api/configs/profiles")).json()
    resp = await client.post("/api/configs/profiles", json={
        "name": "坏组合", "target": "ios_mobile", "udp_security_mode": "scoped"})
    assert resp.status_code == 422
    after = (await client.get("/api/configs/profiles")).json()
    assert [p["name"] for p in after] == [p["name"] for p in before]


@pytest.mark.asyncio
async def test_create_profile_unknown_target_is_422(client):
    """未知目标值由 Pydantic 枚举在进入服务前拒绝"""
    resp = await client.post("/api/configs/profiles",
                             json={"name": "烤面包机", "target": "linux_toaster"})
    assert resp.status_code == 422
    assert "linux_toaster" not in {p["name"] for p in
                                   (await client.get("/api/configs/profiles")).json()}


@pytest.mark.asyncio
async def test_update_target_without_mode_resets_default(client):
    """只换目标不传模式：模式随新目标重置（mac→ios 变 compatible，再回 mac 变 scoped）"""
    pid = await _create(client, "迁移档")
    resp = await client.put(f"/api/configs/profiles/{pid}", json={"target": "ios_mobile"})
    assert resp.status_code == 200
    detail = await _detail(client, pid)
    assert detail["target"] == "ios_mobile"
    assert detail["udp_security_mode"] == "compatible"

    resp = await client.put(f"/api/configs/profiles/{pid}", json={"target": "mac_gateway"})
    assert resp.status_code == 200
    detail = await _detail(client, pid)
    assert detail["target"] == "mac_gateway"
    assert detail["udp_security_mode"] == "scoped"


@pytest.mark.asyncio
async def test_update_target_and_mode_validated_together(client):
    """同时提供不兼容组合：422 且原值原样保留"""
    pid = await _create(client, "组合档")
    resp = await client.put(f"/api/configs/profiles/{pid}",
                            json={"target": "ios_mobile", "udp_security_mode": "scoped"})
    assert resp.status_code == 422
    detail = await _detail(client, pid)
    assert detail["target"] == "mac_workstation"
    assert detail["udp_security_mode"] == "scoped"


@pytest.mark.asyncio
async def test_update_mode_validated_against_current_target(client):
    """只换模式：与档案现存目标组合校验；合法值正常保存"""
    pid = await _create(client, "iOS严格档", target="ios_mobile")
    resp = await client.put(f"/api/configs/profiles/{pid}", json={"udp_security_mode": "scoped"})
    assert resp.status_code == 422
    assert (await _detail(client, pid))["udp_security_mode"] == "compatible"

    resp = await client.put(f"/api/configs/profiles/{pid}", json={"udp_security_mode": "strict"})
    assert resp.status_code == 200
    assert (await _detail(client, pid))["udp_security_mode"] == "strict"


@pytest.mark.asyncio
async def test_update_unrelated_field_keeps_target_fields(client):
    """未提供 target/mode 的部分更新（区分“未提供”与显式值）：目标字段不动"""
    pid = await _create(client, "改名档", target="mac_gateway",
                        udp_security_mode="strict")
    resp = await client.put(f"/api/configs/profiles/{pid}", json={"name": "改名档2"})
    assert resp.status_code == 200
    detail = await _detail(client, pid)
    assert detail["name"] == "改名档2"
    assert detail["target"] == "mac_gateway"
    assert detail["udp_security_mode"] == "strict"


@pytest.mark.asyncio
async def test_missing_profile_id_never_falls_back_to_default(client):
    """显式 id 不存在：404，默认档案不受任何影响（无隐式回退）"""
    default_before = await _detail(client, 1)
    resp = await client.put("/api/configs/profiles/9999", json={"name": "幽灵档案"})
    assert resp.status_code == 404
    assert (await client.get("/api/configs/profiles/9999")).status_code == 404
    assert await _detail(client, 1) == default_before


@pytest.mark.asyncio
async def test_export_missing_profile_is_404(client):
    """导出不存在的档案：命中真实 /api/configs/{id}/exports 路由的显式解析 404"""
    resp = await client.post("/api/configs/9999/exports", json={"confirm": True})
    assert resp.status_code == 404
    # 区别于路由不存在的默认 404（detail="Not Found"），证明命中 _require_profile
    assert resp.json()["detail"] == "配置方案不存在"


@pytest.mark.asyncio
async def test_create_profile_explicit_null_mode_rejected(client):
    """显式 null 的 udp_security_mode 是 422（不是“省略”），且不落库"""
    before = (await client.get("/api/configs/profiles")).json()
    resp = await client.post("/api/configs/profiles", json={
        "name": "空模式档", "target": "ios_mobile", "udp_security_mode": None})
    assert resp.status_code == 422
    after = (await client.get("/api/configs/profiles")).json()
    assert [p["name"] for p in after] == [p["name"] for p in before]
