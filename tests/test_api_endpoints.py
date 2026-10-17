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
    from app.runtime_paths import resolve_runtime_paths
    from app.services.database_migration import prepare_database

    monkeypatch.setenv("SURGECONF_DATA_DIR", str(tmp_path / "runtime"))
    paths = resolve_runtime_paths()
    await prepare_database(paths, tmp_path / "missing-legacy.db")
    test_runtime = await DatabaseRuntime.create(paths.database)

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
                            "target", "udp_security_mode", "token_configured"}
    assert data[0]["udp_security_mode"] in {"strict", "scoped", "compatible"}
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
    """创建显式 target 未传模式：mac_workstation→scoped，ios_mobile→compatible
    （省略 target 的角色默认行为见下方实例角色用例）"""
    mac = await _detail(client, await _create(client, "mac档", target="mac_workstation"))
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
    pid = await _create(client, "组合档", target="mac_workstation")
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


# ===== 生成入口档案错误语义：显式 404 / 省略无默认 422 =====


@pytest.mark.asyncio
async def test_generate_and_preview_explicit_missing_profile_404(client):
    """显式 id 不存在：/generate 与 /preview 均 404，不回退默认档案"""
    assert (await client.get("/api/configs/generate?profile_id=99999")).status_code == 404
    assert (await client.get("/api/configs/preview?profile_id=99999")).status_code == 404
    # 显式 0 是显式 id：按不存在处理，而非省略
    assert (await client.get("/api/configs/generate?profile_id=0")).status_code == 404
    assert (await client.get("/api/configs/preview?profile_id=0")).status_code == 404


@pytest.mark.asyncio
async def test_generate_preview_and_general_without_default_422(client):
    """省略 id 且无默认档案：生成与 General 读取入口统一 422"""
    profiles = (await client.get("/api/configs/profiles")).json()
    for p in profiles:
        assert (await client.delete(f"/api/configs/profiles/{p['id']}")).status_code == 200

    assert (await client.get("/api/configs/generate")).status_code == 422
    assert (await client.get("/api/configs/preview")).status_code == 422
    assert (await client.get("/api/general/fields")).status_code == 422
    assert (await client.get("/api/general/values")).status_code == 422


@pytest.mark.asyncio
async def test_general_endpoints_explicit_missing_profile_404(client):
    """General 字段/值读取：显式 id 不存在 404（与生成入口一致）"""
    assert (await client.get("/api/general/fields?profile_id=99999")).status_code == 404
    assert (await client.get("/api/general/values?profile_id=99999")).status_code == 404


@pytest.mark.asyncio
async def test_export_resolves_profile_exactly_once(client, runtime_paths, monkeypatch):
    """导出由生成入口唯一解析档案：不存在前置 _require_profile 预查（行为验证）"""
    import app.routers.configs as configs_router

    async def _no_prefetch(db, profile_id):
        raise AssertionError("导出不应再做前置档案预查（唯一解析在生成入口内）")

    monkeypatch.setattr(configs_router, "_require_profile", _no_prefetch)
    resp = await client.post("/api/configs/1/exports", json={"confirm": True})
    assert resp.status_code == 200, resp.text
    assert (runtime_paths.exports / resp.json()["name"]).exists()


@pytest.mark.asyncio
async def test_generate_unexpected_error_is_not_swallowed(client, monkeypatch):
    """_render_profile_config 只映射两类档案领域错误：其他生成异常原样传播"""

    async def _boom(db, profile_id=None, locale_override=None):
        raise ValueError("generator exploded")

    monkeypatch.setattr("app.routers.configs.generate_surge_config", _boom)
    with pytest.raises(ValueError):
        await client.get("/api/configs/generate?profile_id=1")


# ===== 实例角色 → 新档案默认目标 =====


async def _set_instance_role(client, role: str) -> dict:
    """local_only 无密码下直接改角色：真实提交 + 断言回显"""
    resp = await client.put("/api/settings/access", json={
        "installation_role": role, "admin_access": "local_only",
        "current_password": None, "new_password": None})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["installation_role"] == role
    return body


@pytest.mark.asyncio
async def test_new_profile_defaults_follow_instance_role(client):
    """省略 target 的创建按实例角色取默认目标：work→workstation，home→gateway；
    mode 省略按最终目标默认（mac_gateway → scoped）"""
    await _set_instance_role(client, "work_computer")
    created = await client.post("/api/configs/profiles", json={"name": "Work"})
    assert created.status_code == 200, created.text
    work = await _detail(client, created.json()["id"])
    assert work["target"] == "mac_workstation"
    assert work["udp_security_mode"] == "scoped"

    await _set_instance_role(client, "home_gateway")
    created = await client.post("/api/configs/profiles", json={"name": "Gateway"})
    assert created.status_code == 200, created.text
    gateway = await _detail(client, created.json()["id"])
    assert gateway["target"] == "mac_gateway"
    assert gateway["udp_security_mode"] == "scoped"


@pytest.mark.asyncio
async def test_new_profile_omitted_target_uses_default_role(client):
    """角色从未设置（缺 SystemConfig 行）时收敛到默认角色 work_computer → mac_workstation"""
    created = await client.post("/api/configs/profiles", json={"name": "未设置角色"})
    assert created.status_code == 200, created.text
    profile = await _detail(client, created.json()["id"])
    assert profile["target"] == "mac_workstation"
    assert profile["udp_security_mode"] == "scoped"


@pytest.mark.asyncio
async def test_session_reports_default_role_when_missing(client):
    """设置读取路径（load_access_settings）缺失 installation_role 行：
    /api/auth/session 回显收敛后的默认角色 work_computer（README 默认工作电脑）"""
    resp = await client.get("/api/auth/session")
    assert resp.status_code == 200, resp.text
    assert resp.json()["installation_role"] == "work_computer"


@pytest.mark.asyncio
async def test_session_reports_default_role_when_db_value_invalid(client):
    """设置读取路径（load_access_settings）库内非法角色行（绕过 API 写入）：
    /api/auth/session 回显回落默认角色 work_computer，绝不透传非法值"""
    from app.models import SystemConfig

    async with client._test_factory() as db:
        db.add(SystemConfig(key="installation_role", value="linux_toaster"))
        await db.commit()

    resp = await client.get("/api/auth/session")
    assert resp.status_code == 200, resp.text
    assert resp.json()["installation_role"] == "work_computer"


@pytest.mark.asyncio
async def test_explicit_target_overrides_instance_role(client):
    """显式 target 优先于角色默认：home_gateway 实例仍可显式创建 iOS 档案"""
    await _set_instance_role(client, "home_gateway")
    pid = await _create(client, "显式iOS", target="ios_mobile")
    profile = await _detail(client, pid)
    assert profile["target"] == "ios_mobile"
    assert profile["udp_security_mode"] == "compatible"


@pytest.mark.asyncio
async def test_role_change_never_touches_existing_profiles(client):
    """角色变化只影响此后创建；已有档案逐条保持原目标与模式"""
    await _set_instance_role(client, "work_computer")
    old = await _detail(client, await _create(client, "旧档案"))
    assert old["target"] == "mac_workstation"

    await _set_instance_role(client, "home_gateway")
    # 已有档案不被改写
    assert (await _detail(client, old["id"]))["target"] == "mac_workstation"
    # 此后省略 target 的创建用新角色默认
    fresh = await _detail(client, await _create(client, "新档案"))
    assert fresh["target"] == "mac_gateway"


@pytest.mark.asyncio
async def test_access_update_rejects_unknown_role(client):
    """非法 installation_role 由请求模型拒绝（422），合法角色真实落库并在会话回显"""
    bad = await client.put("/api/settings/access", json={
        "installation_role": "linux_toaster", "admin_access": "local_only"})
    assert bad.status_code == 422

    ok = await _set_instance_role(client, "work_computer")
    session = (await client.get("/api/auth/session")).json()
    assert session["installation_role"] == "work_computer"
    assert ok["installation_role"] == "work_computer"


@pytest.mark.asyncio
async def test_create_profile_explicit_null_target_rejected(client):
    """显式 null target 与显式 null mode 同语义：422（省略才使用默认值）"""
    resp = await client.post("/api/configs/profiles",
                             json={"name": "空目标", "target": None})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_role_target_mapping_covers_all_valid_roles(client):
    """角色→目标映射与安全层合法角色集合保持一致（防两处登记漂移）"""
    from app.profile_targets import DEFAULT_TARGET_BY_ROLE
    from app.security import VALID_INSTALLATION_ROLES

    assert set(DEFAULT_TARGET_BY_ROLE) == VALID_INSTALLATION_ROLES
    assert DEFAULT_TARGET_BY_ROLE["work_computer"].value == "mac_workstation"
    assert DEFAULT_TARGET_BY_ROLE["home_gateway"].value == "mac_gateway"


@pytest.mark.asyncio
async def test_create_role_resolution_reuses_request_session(client, monkeypatch):
    """省略 target 的角色解析必须复用请求注入的同一 session：
    spy 捕获 create 端点收到的 db，断言与 Depends(get_db) 注入实例同一对象，
    而非另开 session 的全局 factory 产物。"""
    import app.routers.configs as configs_router
    from app.database import get_db
    from app.main import app as fastapi_app

    captured = {"role_db": None, "request_db": None}
    orig_resolve = configs_router.resolve_installation_role

    async def _spy(db):
        captured["role_db"] = db
        return await orig_resolve(db)

    monkeypatch.setattr(configs_router, "resolve_installation_role", _spy)

    orig_dep = fastapi_app.dependency_overrides[get_db]

    async def _tracking_get_db():
        async for session in orig_dep():
            if captured["request_db"] is None:
                captured["request_db"] = session
            yield session

    fastapi_app.dependency_overrides[get_db] = _tracking_get_db
    try:
        resp = await client.post("/api/configs/profiles", json={"name": "同session档"})
        assert resp.status_code == 200, resp.text
    finally:
        fastapi_app.dependency_overrides[get_db] = orig_dep

    assert captured["role_db"] is not None, "create 必须经 resolve_installation_role 取角色"
    assert captured["role_db"] is captured["request_db"], \
        "角色解析必须复用请求同一 session，不得另开连接"


@pytest.mark.asyncio
async def test_create_role_falls_back_when_db_value_invalid(client):
    """库内非法角色行（绕过 API 写入）按 DEFAULT_INSTALLATION_ROLE 回落：
    linux_toaster → work_computer → mac_workstation"""
    from app.models import SystemConfig

    async with client._test_factory() as db:
        db.add(SystemConfig(key="installation_role", value="linux_toaster"))
        await db.commit()

    created = await client.post("/api/configs/profiles", json={"name": "非法角色档"})
    assert created.status_code == 200, created.text
    profile = await _detail(client, created.json()["id"])
    assert profile["target"] == "mac_workstation"


@pytest.mark.asyncio
async def test_guide_route_admin_boundary(client):
    """/guide/{doc} 与管理页同一访问边界：回环管理会话 200；
    切 lan_authenticated 后旧隐式会话失效 → 302 /login；未知指南 404。"""
    ok = await client.get("/guide/profile-targets")
    assert ok.status_code == 200
    assert "档案目标设备" in ok.text
    assert "<pre" in ok.text  # 以文档原文渲染，未转义丢失内容

    await client.put("/api/settings/access", json={
        "installation_role": "home_gateway", "admin_access": "lan_authenticated",
        "new_password": "guide-test-pass"})
    redirect = await client.get("/guide/profile-targets")
    assert redirect.status_code == 307
    assert redirect.headers["location"] == "/login"

    login = await client.post("/api/auth/login", json={"password": "guide-test-pass"})
    assert login.status_code == 200
    assert (await client.get("/guide/profile-targets")).status_code == 200
    assert (await client.get("/guide/not-exist")).status_code == 404

# ===== 修复3：自定义规则 FINAL 为生成器专属（保存端 fail closed） =====

@pytest.mark.asyncio
@pytest.mark.parametrize("final_text", ["FINAL,DIRECT", "FINAL,手动选择"])
async def test_create_custom_rule_final_rejected(client, final_text):
    """pre/general 保存端拒绝任何自定义 FINAL（内置 action 与 policy action），
    数据库不落新行"""
    before = await client.get("/api/configs/custom-rules")
    n_before = len(before.json())
    for position in ("pre", "general"):
        resp = await client.post("/api/configs/custom-rules", json={
            "rule_text": final_text, "position": position})
        assert resp.status_code == 422, resp.text
        assert "FINAL" in resp.json()["detail"]
    after = await client.get("/api/configs/custom-rules")
    assert len(after.json()) == n_before, "被拒绝的 FINAL 规则不得落库"


@pytest.mark.asyncio
async def test_update_custom_rule_to_final_rejected(client):
    """更新端把 rule_text 改成 FINAL：422 且原文本保持不变；不改 rule_text 的
    更新（如排序/启停）不受影响"""
    created = await client.post("/api/configs/custom-rules", json={
        "rule_text": "DOMAIN-SUFFIX,normal.example,DIRECT"})
    rid = created.json()["id"]

    bad = await client.put(f"/api/configs/custom-rules/{rid}",
                           json={"rule_text": "FINAL,自动选择"})
    assert bad.status_code == 422
    assert "FINAL" in bad.json()["detail"]

    row = [r for r in (await client.get("/api/configs/custom-rules")).json()
           if r["id"] == rid][0]
    assert row["rule_text"] == "DOMAIN-SUFFIX,normal.example,DIRECT"

    ok = await client.put(f"/api/configs/custom-rules/{rid}",
                          json={"sort_order": 5, "enabled": False})
    assert ok.status_code == 200


# ===== 修复4：POST /profiles 持久化请求 final_action =====

@pytest.mark.asyncio
async def test_create_profile_persists_requested_final_action(client):
    """请求 final_action 必须落库并直通生成 FINAL；省略默认使用兜底组"""
    pid = await _create(client, "自定义兜底档", final_action="自动选择")
    assert (await _detail(client, pid))["final_action"] == "自动选择"
    content = (await client.get(f"/api/configs/generate?profile_id={pid}")).text
    assert "FINAL,自动选择,dns-failed" in content

    pid2 = await _create(client, "默认兜底档")
    assert (await _detail(client, pid2))["final_action"] == "兜底"
    content2 = (await client.get(f"/api/configs/generate?profile_id={pid2}")).text
    assert "FINAL,兜底,dns-failed" in content2


@pytest.mark.asyncio
async def test_create_profile_explicit_null_final_action_rejected(client):
    """final_action 显式 null：422（省略才使用模型默认）"""
    resp = await client.post("/api/configs/profiles",
                             json={"name": "空兜底", "final_action": None})
    assert resp.status_code == 422


# ===== 修复6：POST /rules 创建后提交前校验顺序不变量 =====

@pytest.mark.asyncio
async def test_create_rule_breaking_order_rolls_back(client):
    """创建内置稳定身份副本（同 name/url/rule_type）且排在 Apple 之后：
    422 + 数据库行数不变（flush 后 rollback）"""
    from sqlalchemy import select as sa_select
    from app.models import RuleSource as RuleSourceModel
    builtin_ai = next(rs for rs in DEFAULT_RULE_SOURCES if rs["category"] == "ai")
    before = len((await client.get("/api/configs/rules")).json())

    resp = await client.post("/api/configs/rules", json={
        "name": builtin_ai["name"], "url": builtin_ai["url"],
        "action": "DIRECT", "rule_type": builtin_ai["rule_type"],
        "category": builtin_ai["category"], "sort_order": 9999, "enabled": True})
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "invalid_configuration"

    after = len((await client.get("/api/configs/rules")).json())
    assert after == before, "顺序违例的创建不得提交（数据库不变）"

    async with client._test_factory() as db:
        copies = (await db.execute(
            sa_select(RuleSourceModel).where(
                RuleSourceModel.sort_order == 9999))).scalars().all()
        assert copies == []


@pytest.mark.asyncio
async def test_create_rule_custom_source_still_works(client):
    """合法自定义规则源（非内置身份）创建照常成功"""
    before = len((await client.get("/api/configs/rules")).json())
    resp = await client.post("/api/configs/rules", json={
        "name": "我的私有规则", "url": "https://example.com/my.list",
        "action": "DIRECT", "rule_type": "RULE-SET", "sort_order": 200})
    assert resp.status_code == 200, resp.text
    assert len((await client.get("/api/configs/rules")).json()) == before + 1


# ===== 多行 rule_text：保存端 422 / 生成端 fail closed =====


@pytest.mark.asyncio
@pytest.mark.parametrize("rule_text", [
    "# 注释\nPROCESS-NAME,ExampleVideo,DIRECT",   # 第二行携带 Mac 专属规则
    "# 注释\nFINAL,DIRECT",                       # 第二行携带 FINAL
    "DOMAIN-SUFFIX,example.com,DIRECT\r\nDOMAIN,key.com,DIRECT",  # CRLF
])
async def test_create_custom_rule_multiline_rejected(client, rule_text):
    """保存端拒绝任何含 CR/LF 的 rule_text：一行一条规则，数据库不落新行"""
    before = len((await client.get("/api/configs/custom-rules")).json())
    resp = await client.post("/api/configs/custom-rules", json={
        "profile_id": None, "position": "general", "rule_text": rule_text})
    assert resp.status_code == 422
    assert "单行" in resp.json()["detail"]
    assert len((await client.get("/api/configs/custom-rules")).json()) == before


@pytest.mark.asyncio
async def test_update_custom_rule_multiline_rejected_keeps_original(client):
    """更新端把 rule_text 改成多行：422 且原文本保持不变"""
    created = await client.post("/api/configs/custom-rules", json={
        "profile_id": None, "position": "general",
        "rule_text": "DOMAIN-SUFFIX,keep.example,DIRECT"})
    rid = created.json()["id"]
    resp = await client.put(f"/api/configs/custom-rules/{rid}", json={
        "rule_text": "# 注释\nFINAL,DIRECT"})
    assert resp.status_code == 422
    rules = {r["id"]: r for r in (await client.get("/api/configs/custom-rules")).json()}
    assert rules[rid]["rule_text"] == "DOMAIN-SUFFIX,keep.example,DIRECT"


@pytest.mark.asyncio
async def test_single_line_comment_and_rule_not_harmed(client):
    """合法单行注释与单行规则不误伤：保存端照常受理（多行拦截只针对
    CR/LF）；合法规则正常进入产物。注释行在生成端因无法解析策略段被
    既有校验拒绝（与多行拦截无关），故 preview 用纯规则档案验证"""
    ios = await _create(client, "单行iOS档", target="ios_mobile")
    note = await client.post("/api/configs/custom-rules", json={
        "profile_id": ios, "position": "general", "rule_text": "# 单行注释：放行示例"})
    assert note.status_code == 200, note.text     # 单行注释：保存端不误伤
    # 删掉注释行使档案回到纯规则形态（生成端对注释行的既有拒绝不属本任务）
    await client.delete(f"/api/configs/custom-rules/{note.json()['id']}")
    rule = await client.post("/api/configs/custom-rules", json={
        "profile_id": ios, "position": "general",
        "rule_text": "DOMAIN-SUFFIX,keep.example,DIRECT"})
    assert rule.status_code == 200, rule.text
    preview = await client.get(f"/api/configs/preview?profile_id={ios}")
    assert preview.status_code == 200, preview.text
    assert "keep.example" in preview.json()["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("target", "rule_text"), [
    ("ios_mobile", "# 注释\nPROCESS-NAME,ExampleVideo,DIRECT"),
    ("mac_workstation", "# 注释\nFINAL,DIRECT"),
])
async def test_generate_multiline_rule_fails_closed(client, target, rule_text):
    """绕库直写的多行 row（保存端拦截的存量）：生成 fail closed（422 +
    invalid_configuration），绝不把第二物理行输出；错误不回显规则内容"""
    from app.models import CustomRule as Rule
    pid = await _create(client, f"多行档-{target}", target=target)
    async with client._test_factory() as db:
        db.add(Rule(profile_id=pid, rule_text=rule_text, position="general",
                    sort_order=0, enabled=True))
        await db.commit()

    preview = await client.get(f"/api/configs/preview?profile_id={pid}")
    assert preview.status_code == 422
    body = preview.json()["detail"]
    assert body["code"] == "invalid_configuration"
    joined = " ".join(body["errors"])
    assert "包含换行" in joined
    # 最小披露：错误不回显规则内容（注释/第二物理行均不出现）
    assert "PROCESS-NAME" not in joined and "FINAL,DIRECT" not in joined
    assert "ExampleVideo" not in joined


# ===== 档案 target 更新 × UDP 例外兼容性 =====


async def _add_udp_exception(client, profile_id: int, match_type: str,
                             match_value: str, display_name: str = "客厅设备"):
    """直插 UDP 例外（CRUD 路由属后续任务）：绕 API 用底层会话"""
    from app.models import UdpException
    async with client._test_factory() as db:
        db.add(UdpException(profile_id=profile_id, match_type=match_type,
                            match_value=match_value, display_name=display_name,
                            enabled=True, sort_order=0))
        await db.commit()


@pytest.mark.asyncio
async def test_update_target_udp_exception_incompatible_422(client):
    """workstation(process 例外) → gateway：422 且 detail 只给数量；
    事务不提交（target 不变）；target 不变/未提供不额外阻止"""
    pid = await _create(client, "UDP兼容档", target="mac_workstation")
    await _add_udp_exception(client, pid, "process", "ExampleVideo")
    await _add_udp_exception(client, pid, "process", "SecondProc")

    resp = await client.put(f"/api/configs/profiles/{pid}", json={"target": "mac_gateway"})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "2 条例外" in detail
    # 最小披露：不含 type/value/display_name
    for secret in ("process", "ExampleVideo", "SecondProc", "客厅设备"):
        assert secret not in detail

    # 事务未提交：target 保持 workstation，模式组合校验仍生效
    assert (await _detail(client, pid))["target"] == "mac_workstation"
    ios_scoped = await client.put(f"/api/configs/profiles/{pid}",
                                  json={"target": "ios_mobile",
                                        "udp_security_mode": "scoped"})
    assert ios_scoped.status_code == 422
    assert (await _detail(client, pid))["target"] == "mac_workstation"

    # target 不变（同值 PUT）与不带 target 的更新不额外阻止
    same = await client.put(f"/api/configs/profiles/{pid}",
                            json={"target": "mac_workstation"})
    assert same.status_code == 200, same.text
    rename = await client.put(f"/api/configs/profiles/{pid}",
                              json={"description": "只改描述"})
    assert rename.status_code == 200, rename.text


@pytest.mark.asyncio
async def test_update_target_compatible_exceptions_do_not_block(client):
    """无例外或例外类型兼容（gateway→workstation 清掉 mac_address 后）不阻止切换"""
    pid = await _create(client, "网关档", target="mac_gateway")
    resp = await client.put(f"/api/configs/profiles/{pid}", json={"target": "mac_workstation"})
    assert resp.status_code == 200, resp.text
    assert (await _detail(client, pid))["target"] == "mac_workstation"

    await _create(client, "iOS档", target="ios_mobile")
    ios_profiles = [p["id"] for p in (await client.get("/api/configs/profiles")).json()]
    ios_pid = ios_profiles[-1]
    to_mac = await client.put(f"/api/configs/profiles/{ios_pid}",
                              json={"target": "mac_workstation"})
    assert to_mac.status_code == 200, to_mac.text


# ===== 档案删除 × UDP 例外 DB 级 cascade（client runtime 每连接 FK ON） =====


@pytest.mark.asyncio
async def test_delete_profile_db_cascades_udp_exceptions(client):
    """删除档案：UDP 例外由数据库级 ON DELETE CASCADE 清除（路由不手工删），
    无孤儿行；其他档案的例外不受影响"""
    from sqlalchemy import select as sa_select
    from app.models import UdpException

    pid = await _create(client, "UDP删除档", target="mac_gateway")
    other = await _create(client, "UDP保留档", target="mac_gateway")
    await _add_udp_exception(client, pid, "mac_address", "02:00:00:00:00:01")
    await _add_udp_exception(client, other, "mac_address", "02-00-00-00-00-01")

    resp = await client.delete(f"/api/configs/profiles/{pid}")
    assert resp.status_code == 200

    async with client._test_factory() as db:
        remaining = (await db.execute(sa_select(UdpException))).scalars().all()
        assert [(e.profile_id, e.match_value) for e in remaining] == \
            [(other, "02-00-00-00-00-01")]


# ===== 订阅调度：interval 校验 / create-update 排期矩阵 / lifespan =====


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", [299, 604801, 0, -300])
async def test_subscription_create_interval_out_of_range_422(client, interval):
    """越界间隔：创建与更新均 422，静态错误不回显输入值"""
    resp = await client.post("/api/subscriptions", json={
        "name": "越界", "url": "https://sub.example.com/x",
        "update_interval": interval})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert all(set(e) == {"loc", "type", "msg"} for e in detail)
    assert str(interval) not in resp.text

    sub_id = (await client.post("/api/subscriptions", json={
        "name": "基准", "url": "https://sub.example.com/base"})).json()["id"]
    resp = await client.put(f"/api/subscriptions/{sub_id}",
                            json={"update_interval": interval})
    assert resp.status_code == 422
    assert str(interval) not in resp.text


@pytest.mark.asyncio
async def test_subscription_update_explicit_null_interval_422(client):
    """显式 null 间隔是非法输入（省略才表示不修改）"""
    sub_id = (await client.post("/api/subscriptions", json={
        "name": "空值", "url": "https://sub.example.com/n"})).json()["id"]
    resp = await client.put(f"/api/subscriptions/{sub_id}",
                            json={"update_interval": None})
    assert resp.status_code == 422
    rows = (await client.get("/api/subscriptions")).json()
    assert next(s for s in rows if s["id"] == sub_id)["update_interval"] == 86400


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", [300, 3600, 604800])
async def test_subscription_interval_boundaries_accepted(client, interval):
    """边界值 300 / 604800 及常规值创建成功且原样落库"""
    resp = await client.post("/api/subscriptions", json={
        "name": f"边界{interval}", "url": "https://sub.example.com/b",
        "update_interval": interval})
    assert resp.status_code == 200
    sub_id = resp.json()["id"]
    rows = (await client.get("/api/subscriptions")).json()
    assert next(s for s in rows if s["id"] == sub_id)["update_interval"] == interval


def _parse_api_utc(value):
    """API UTC 时间（YYYY-MM-DDTHH:MM:SSZ）→ naive UTC datetime"""
    from datetime import datetime
    return datetime.fromisoformat(value.rstrip("Z"))


async def _next_of(client, sub_id):
    rows = (await client.get("/api/subscriptions")).json()
    return next(s for s in rows if s["id"] == sub_id)["next_update_at"]


@pytest.mark.asyncio
async def test_subscription_create_scheduling_matrix(client):
    """创建：enabled+auto 立即排期（next 非空）；否则 NULL 不参与调度"""
    a = (await client.post("/api/subscriptions", json={
        "name": "排期", "url": "https://sub.example.com/a"})).json()["id"]
    b = (await client.post("/api/subscriptions", json={
        "name": "手动", "url": "https://sub.example.com/b",
        "auto_update": False})).json()["id"]
    c = (await client.post("/api/subscriptions", json={
        "name": "停用", "url": "https://sub.example.com/c",
        "enabled": False})).json()["id"]
    assert await _next_of(client, a) is not None
    assert await _next_of(client, b) is None
    assert await _next_of(client, c) is None


@pytest.mark.asyncio
async def test_subscription_update_scheduling_matrix(client):
    """更新矩阵：仅在调度相关值实际变化时重算 next（同值全提交不推迟）"""
    from datetime import datetime, timedelta, timezone

    sub_id = (await client.post("/api/subscriptions", json={
        "name": "矩阵", "url": "https://sub.example.com/m",
        "update_interval": 3600})).json()["id"]
    created_next = await _next_of(client, sub_id)
    assert created_next is not None

    # 1) 仅改 name：next 不动
    resp = await client.put(f"/api/subscriptions/{sub_id}", json={"name": "矩阵2"})
    assert resp.status_code == 200
    assert await _next_of(client, sub_id) == created_next

    # 2) 全字段提交但值未变：next 不动（UI 全量提交场景）
    detail = (await client.get(f"/api/subscriptions/{sub_id}")).json()
    resp = await client.put(f"/api/subscriptions/{sub_id}", json={
        "name": detail["name"], "url": detail["url"], "tag": detail["tag"],
        "color": detail["color"], "enabled": detail["enabled"],
        "auto_update": detail["auto_update"],
        "update_interval": detail["update_interval"]})
    assert resp.status_code == 200
    assert await _next_of(client, sub_id) == created_next

    # 3) enabled+auto 下 interval 改变 → now + 新 interval
    # API 时间序列化按规范去微秒，t0 同步截断以对齐精度
    t0 = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    resp = await client.put(f"/api/subscriptions/{sub_id}",
                            json={"update_interval": 7200})
    assert resp.status_code == 200
    nxt = _parse_api_utc(await _next_of(client, sub_id))
    t1 = datetime.now(timezone.utc).replace(tzinfo=None)
    assert t0 + timedelta(seconds=7200) <= nxt <= t1 + timedelta(seconds=7200)

    # 4) auto off → NULL
    await client.put(f"/api/subscriptions/{sub_id}", json={"auto_update": False})
    assert await _next_of(client, sub_id) is None

    # 5) 从 auto-off 开启 → now（立即到期）
    # API 时间序列化按规范去微秒，t0 同步截断以对齐精度
    t0 = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    await client.put(f"/api/subscriptions/{sub_id}", json={"auto_update": True})
    nxt = _parse_api_utc(await _next_of(client, sub_id))
    t1 = datetime.now(timezone.utc).replace(tzinfo=None)
    assert t0 <= nxt <= t1

    # 6) 禁用 → NULL
    await client.put(f"/api/subscriptions/{sub_id}", json={"enabled": False})
    assert await _next_of(client, sub_id) is None


@pytest.mark.asyncio
async def test_lifespan_starts_scheduler_active(client):
    """单实例：lifespan 启动调度器为 active，锁文件为 root 下 0600 普通文件"""
    import stat as stat_mod
    from app.main import app as fastapi_app
    from app.runtime_paths import resolve_runtime_paths
    from app.services.subscription_scheduler import SubscriptionScheduler

    scheduler = fastapi_app.state.subscription_scheduler
    assert isinstance(scheduler, SubscriptionScheduler)
    assert fastapi_app.state.subscription_scheduler_active is True
    assert scheduler.active is True

    lock = resolve_runtime_paths().root / "scheduler.lock"
    st = lock.lstat()
    assert stat_mod.S_ISREG(st.st_mode) and not stat_mod.S_ISLNK(st.st_mode)
    assert st.st_mode & 0o777 == 0o600
    assert lock.stat().st_size == 0  # 不写 PID/路径


@pytest.mark.asyncio
async def test_lifespan_standby_when_lock_held(tmp_path, monkeypatch, capsys):
    """锁被占：lifespan 进入 standby（正常服务、无 task），退出不抢锁"""
    from unittest.mock import patch
    from app.main import app as fastapi_app
    from app.database import DatabaseRuntime, get_db
    from app.models import Subscription
    from app.runtime_paths import resolve_runtime_paths
    from app.services.subscription_scheduler import SubscriptionScheduler

    monkeypatch.setenv("SURGECONF_DATA_DIR", str(tmp_path / "runtime"))
    paths = resolve_runtime_paths()
    from app.services.database_migration import prepare_database
    await prepare_database(paths, tmp_path / "missing-legacy.db")
    test_runtime = await DatabaseRuntime.create(paths.database)

    async def override_get_db():
        async with test_runtime.session_factory() as session:
            yield session

    fastapi_app.dependency_overrides[get_db] = override_get_db
    holder = SubscriptionScheduler(test_runtime, paths.root / "scheduler.lock")
    assert await holder.start() is True
    try:
        with patch("app.database.database", test_runtime), \
             patch("app.bootstrap.database", test_runtime):
            async with fastapi_app.router.lifespan_context(fastapi_app):
                assert fastapi_app.state.subscription_scheduler_active is False
                sched = fastapi_app.state.subscription_scheduler
                assert sched._task is None and sched._lock_fd is None
                # standby 实例数据库仍可正常服务
                async with test_runtime.session_factory() as db:
                    db.add(Subscription(name="S", url="https://sub.example.com/s",
                                        enabled=True, auto_update=True,
                                        update_interval=3600))
                    await db.commit()
            out = capsys.readouterr().err
            assert "standby" in out
            # holder 全程持锁未被抢占
            assert holder.active is True
    finally:
        await holder.stop()
        fastapi_app.dependency_overrides.pop(get_db, None)
        await test_runtime.dispose()


@pytest.mark.asyncio
async def test_lifespan_stops_scheduler_before_db_dispose(tmp_path, monkeypatch):
    """清理顺序：调度器先停（task 收敛），之后才 dispose 数据库；锁释放可接管"""
    from unittest.mock import patch
    from app.main import app as fastapi_app
    from app.database import DatabaseRuntime, get_db
    from app.runtime_paths import resolve_runtime_paths
    from app.services.subscription_scheduler import SubscriptionScheduler

    monkeypatch.setenv("SURGECONF_DATA_DIR", str(tmp_path / "runtime"))
    paths = resolve_runtime_paths()
    from app.services.database_migration import prepare_database
    await prepare_database(paths, tmp_path / "missing-legacy.db")

    class _OrderRecordingRuntime(DatabaseRuntime):
        dispose_seen_scheduler_stopped: bool | None = None

        async def dispose(self):
            sched = getattr(fastapi_app.state, "subscription_scheduler", None)
            self.dispose_seen_scheduler_stopped = bool(
                sched is not None and sched._task is None)
            await super().dispose()

    test_runtime = await _OrderRecordingRuntime.create(paths.database)

    async def override_get_db():
        async with test_runtime.session_factory() as session:
            yield session

    fastapi_app.dependency_overrides[get_db] = override_get_db
    try:
        with patch("app.database.database", test_runtime), \
             patch("app.bootstrap.database", test_runtime):
            async with fastapi_app.router.lifespan_context(fastapi_app):
                assert fastapi_app.state.subscription_scheduler_active is True
                assert fastapi_app.state.subscription_scheduler.active is True
            # 退出后：dispose 执行时调度器 task 已收敛（stop 先于 dispose）
            assert test_runtime.dispose_seen_scheduler_stopped is True
            assert fastapi_app.state.subscription_scheduler._task is None
            # 锁已释放：新实例可立即接管
            taker = SubscriptionScheduler(test_runtime, paths.root / "scheduler.lock")
            assert await taker.start() is True
            await taker.stop()
    finally:
        fastapi_app.dependency_overrides.pop(get_db, None)
        await test_runtime.dispose()


# ===== lifespan 启动失败清理：bind 后任一步骤失败都停调度器并释放 DB =====
from app.database import DatabaseRuntime


class _DisposalRecordingRuntime(DatabaseRuntime):
    """记录 dispose 是否执行（lifespan finally 无条件释放）"""

    disposed = False

    async def dispose(self):
        self.disposed = True
        await super().dispose()


async def _drive_lifespan_expecting_failure(tmp_path, monkeypatch, sabotager):
    """公共脚手架：占位 sabotage → lifespan 应传播异常且 finally 清理"""
    from app.main import app as fastapi_app
    from app.database import get_db
    from app.runtime_paths import resolve_runtime_paths

    monkeypatch.setenv("SURGECONF_DATA_DIR", str(tmp_path / "runtime"))
    paths = resolve_runtime_paths()
    from app.services.database_migration import prepare_database
    await prepare_database(paths, tmp_path / "missing-legacy.db")
    test_runtime = await _DisposalRecordingRuntime.create(paths.database)

    async def override_get_db():
        async with test_runtime.session_factory() as session:
            yield session

    fastapi_app.dependency_overrides[get_db] = override_get_db
    sabotager(paths)
    try:
        from unittest.mock import patch
        with pytest.raises(Exception), \
                patch("app.database.database", test_runtime), \
                patch("app.bootstrap.database", test_runtime):
            async with fastapi_app.router.lifespan_context(fastapi_app):
                pass
        scheduler = getattr(fastapi_app.state, "subscription_scheduler", None)
        if scheduler is not None:
            assert scheduler._task is None, "失败后不得残留循环任务"
            assert scheduler._lock_fd is None, "失败后不得残留锁描述符"
        assert test_runtime.disposed is True, "finally 必须无条件 dispose 数据库"
    finally:
        fastapi_app.dependency_overrides.pop(get_db, None)
        await test_runtime.dispose()


@pytest.mark.asyncio
async def test_lifespan_cleanup_when_scheduler_start_fails(tmp_path, monkeypatch):
    """scheduler.start 安全错误（锁路径为 symlink → OSError）：无 fd/task 且 DB disposed"""
    def sabotage(paths):
        target = paths.root / "real.lock"
        target.write_text("")
        (paths.root / "scheduler.lock").symlink_to(target)

    await _drive_lifespan_expecting_failure(tmp_path, monkeypatch, sabotage)


@pytest.mark.asyncio
async def test_lifespan_cleanup_when_create_task_fails(tmp_path, monkeypatch):
    """scheduler 已拿锁但 create_task 失败：start 内部已释放 fd，finally stop 幂等 + DB disposed"""
    import asyncio as asyncio_mod

    def sabotage(paths):
        # 只拦截调度循环协程：prepare_database 等更早阶段（清理边界之外）
        # 的 create_task 不受影响，确保失败点落在 scheduler.start 内部
        real_create_task = asyncio_mod.create_task

        def broken_create_task(coro, **kw):
            if getattr(coro, "__name__", "") == "_loop":
                coro.close()
                raise RuntimeError("no task")
            return real_create_task(coro, **kw)

        monkeypatch.setattr(asyncio_mod, "create_task", broken_create_task)

    await _drive_lifespan_expecting_failure(tmp_path, monkeypatch, sabotage)


@pytest.mark.asyncio
async def test_lifespan_cleanup_when_maintenance_fails(tmp_path, monkeypatch):
    """孤儿节点维护异常（scheduler 未构造）：finally 无 dispose 前置即释放 DB"""
    async def broken_cleanup(db):
        raise RuntimeError("maintenance boom")

    def sabotage(paths):
        import app.services.subscription as subscription_service
        monkeypatch.setattr(subscription_service, "cleanup_orphan_nodes",
                            broken_cleanup)

    await _drive_lifespan_expecting_failure(tmp_path, monkeypatch, sabotage)


@pytest.mark.asyncio
async def test_lifespan_cleanup_when_startup_log_fails(tmp_path, monkeypatch):
    """启动日志输出异常（数据库就绪 print 抛错）：同一清理边界内 DB disposed"""
    import builtins

    real_print = print

    def selective_print(*args, **kw):
        if any("数据库就绪" in str(a) for a in args):
            raise OSError("log device gone")
        real_print(*args, **kw)

    def sabotage(paths):
        monkeypatch.setattr(builtins, "print", selective_print)

    await _drive_lifespan_expecting_failure(tmp_path, monkeypatch, sabotage)
