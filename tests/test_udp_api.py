"""UDP 例外管理 API 测试（Task 2）：脱敏响应、父档案约束与访问边界。

- 成功与错误响应都绝不含完整 ``match_value``，仅 ``match_value_masked``；
- 子项查询同时约束 exception.id + profile_id：跨档案统一 404（不可枚举）；
- 更新省略 match_value 保留原值；任一改变用最终有效二元组重新规范化；
- 非空字段显式 null 422；唯一冲突稳定 409；管理边界沿用阶段 A 中间件。
"""

import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# 列表/详情响应字段白名单（锁定：无 hash/secret/完整值）
LIST_FIELDS = {"id", "profile_id", "match_type", "match_value_masked",
               "display_name", "enabled", "sort_order"}


@pytest_asyncio.fixture
async def secure_app(tmp_path, monkeypatch):
    """临时库 + 手动驱动 lifespan（与 test_profile_tokens 相同装配）"""
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
    """非回环匿名对端（local_only 下管理路径 403）"""
    client = await _make_client(secure_app, "203.0.113.50")
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def csrf_less_client(secure_app):
    """回环客户端：持有隐式会话 Cookie 但不携带 X-CSRF-Token（写方法 403）"""
    client = await _make_client(secure_app, "127.0.0.1")
    await client.get("/")
    yield client
    await client.aclose()


async def _create_profile(admin_client, name: str, target: str) -> SimpleNamespace:
    resp = await admin_client.post("/api/configs/profiles",
                                   json={"name": name, "target": target})
    assert resp.status_code == 200, resp.text
    return SimpleNamespace(id=resp.json()["id"], target=target)


@pytest_asyncio.fixture
async def gateway_profile(admin_client):
    return await _create_profile(admin_client, "网关档案", "mac_gateway")


@pytest_asyncio.fixture
async def workstation_profiles(admin_client):
    return [await _create_profile(admin_client, f"工作站档案{i}", "mac_workstation")
            for i in (1, 2)]


def _base(profile) -> str:
    return f"/api/configs/profiles/{profile.id}/udp-exceptions"


async def create_process_exception(admin_client, profile_id: int,
                                   value: str = "SurgeTauEngine") -> int:
    """创建 process 例外并返回 id；成功响应不得含完整匹配值"""
    resp = await admin_client.post(
        f"/api/configs/profiles/{profile_id}/udp-exceptions",
        json={"match_type": "process", "match_value": value})
    assert resp.status_code == 201, resp.text
    assert "match_value" not in resp.json()
    return resp.json()["id"]


# ===== 脱敏（brief Step 1 原始用例 + 边界形态） =====

@pytest.mark.asyncio
async def test_udp_exception_list_masks_match_value(admin_client, gateway_profile):
    base = f"/api/configs/profiles/{gateway_profile.id}/udp-exceptions"
    created = await admin_client.post(base, json={
        "match_type": "mac_address",
        "match_value": "02:00:00:00:00:01",
        "display_name": "Example display",
        "enabled": True,
        "sort_order": 10,
    })
    assert created.status_code == 201
    item = (await admin_client.get(base)).json()[0]
    assert item["match_value_masked"] == "02:00:**:**:**:01"
    assert "match_value" not in item


@pytest.mark.asyncio
async def test_create_normalizes_and_masks_mac(admin_client, gateway_profile):
    """小写横线 MAC 入库前规范化为大写冒号；响应只见掩码"""
    from app.models import UdpException

    resp = await admin_client.post(_base(gateway_profile), json={
        "match_type": "mac_address", "match_value": "aa-bb-cc" + "-dd-ee-ff"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["match_value_masked"] == "AA:BB:**:**:**:FF"
    assert "match_value" not in body
    async with admin_client._test_factory() as db:
        row = await db.get(UdpException, body["id"])
        assert row.match_value == "AA:BB:CC" + ":DD:EE:FF"


@pytest.mark.asyncio
async def test_process_mask_long_and_short(admin_client, workstation_profiles):
    profile, _ = workstation_profiles
    resp = await admin_client.post(_base(profile), json={
        "match_type": "process", "match_value": "GoogleChromeHelper"})
    assert resp.status_code == 201
    assert resp.json()["match_value_masked"] == "Go**er"
    assert "GoogleChromeHelper" not in resp.text
    # 长度不足（首尾各 2 字符将覆盖完整值）统一 ***
    for short in ("ab", "abc", "abcd"):
        masked = (await admin_client.post(_base(profile), json={
            "match_type": "process", "match_value": short})).json()["match_value_masked"]
        assert masked == "***", short


# ===== 目标矩阵 / Pydantic 边界（错误不回显输入值） =====

@pytest.mark.parametrize("target,value_type", [
    ("mac_gateway", "process"),
    ("mac_workstation", "mac_address"),
    ("ios_mobile", "process"),
    ("ios_mobile", "mac_address"),
])
@pytest.mark.asyncio
async def test_incompatible_combination_rejected_without_echo(
        admin_client, target, value_type):
    profile = await _create_profile(
        admin_client, f"组合-{target}-{value_type}", target)
    value = "mDNSResponder" if value_type == "process" else "02:00:00:00:00:01"
    resp = await admin_client.post(_base(profile), json={
        "match_type": value_type, "match_value": value})
    assert resp.status_code == 422
    assert value not in resp.text


@pytest.mark.asyncio
async def test_schema_boundaries_reject_bad_input(admin_client, workstation_profiles):
    profile, _ = workstation_profiles
    resp = await admin_client.post(_base(profile), json={
        "match_type": "device_id", "match_value": "x"})
    assert resp.status_code == 422
    for bad in ("", "a" * 256):
        resp = await admin_client.post(_base(profile), json={
            "match_type": "process", "match_value": bad})
        assert resp.status_code == 422, repr(bad[:10])
    assert "a" * 256 not in resp.text
    resp = await admin_client.post(_base(profile), json={
        "match_type": "process", "match_value": "ok", "display_name": "x" * 101})
    assert resp.status_code == 422
    assert "x" * 101 not in resp.text


@pytest.mark.asyncio
async def test_bad_mac_format_static_error(admin_client, gateway_profile):
    resp = await admin_client.post(_base(gateway_profile), json={
        "match_type": "mac_address", "match_value": "02:00:00:00:0G:01"})
    assert resp.status_code == 422
    assert "02:00:00:00:0G:01" not in resp.text


# ===== 唯一冲突（稳定 409，不含输入/存储值） =====

@pytest.mark.asyncio
async def test_duplicate_returns_stable_conflict(admin_client, gateway_profile):
    payload = {"match_type": "mac_address", "match_value": "02:00:00:00:00:01"}
    assert (await admin_client.post(_base(gateway_profile), json=payload)).status_code == 201
    second = await admin_client.post(_base(gateway_profile), json=payload)
    assert second.status_code == 409
    assert "02:00:00:00:00:01" not in second.text
    assert len((await admin_client.get(_base(gateway_profile))).json()) == 1


@pytest.mark.asyncio
async def test_update_to_duplicate_value_conflicts(admin_client, workstation_profiles):
    profile, _ = workstation_profiles
    keep = await create_process_exception(admin_client, profile_id=profile.id)
    other = await create_process_exception(
        admin_client, profile_id=profile.id, value="GoogleChromeHelper")
    resp = await admin_client.put(f"{_base(profile)}/{keep}",
                                  json={"match_value": "GoogleChromeHelper"})
    assert resp.status_code == 409
    assert "GoogleChromeHelper" not in resp.text
    # 原值不受影响
    listed = {i["id"]: i for i in (await admin_client.get(_base(profile))).json()}
    assert listed[other]["match_value_masked"] == "Go**er"


# ===== 更新语义：省略保留 / 最终二元组重校验 / 显式 null =====

@pytest.mark.asyncio
async def test_update_omitted_match_value_keeps_original(
        admin_client, workstation_profiles):
    profile, _ = workstation_profiles
    item_id = await create_process_exception(admin_client, profile_id=profile.id)
    before = (await admin_client.get(_base(profile))).json()[0]
    resp = await admin_client.put(f"{_base(profile)}/{item_id}",
                                  json={"enabled": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["match_value_masked"] == before["match_value_masked"] == "Su**ne"
    assert "match_value" not in body


@pytest.mark.asyncio
async def test_update_new_value_revalidates_effective_pair(
        admin_client, workstation_profiles):
    profile, _ = workstation_profiles
    item_id = await create_process_exception(admin_client, profile_id=profile.id)
    resp = await admin_client.put(f"{_base(profile)}/{item_id}",
                                  json={"match_value": "GoogleChromeHelper"})
    assert resp.status_code == 200
    assert resp.json()["match_value_masked"] == "Go**er"
    assert "GoogleChromeHelper" not in resp.text
    # 只换类型为档案不允许的组合 → 422，原二元组保持不变
    bad = await admin_client.put(f"{_base(profile)}/{item_id}",
                                 json={"match_type": "mac_address"})
    assert bad.status_code == 422
    row = (await admin_client.get(_base(profile))).json()[0]
    assert row["match_type"] == "process"
    assert row["match_value_masked"] == "Go**er"


@pytest.mark.asyncio
async def test_update_mac_value_normalized(admin_client, gateway_profile):
    from app.models import UdpException

    created = await admin_client.post(_base(gateway_profile), json={
        "match_type": "mac_address", "match_value": "AA:BB:CC" + ":DD:EE:FF"})
    item_id = created.json()["id"]
    resp = await admin_client.put(f"{_base(gateway_profile)}/{item_id}",
                                  json={"match_value": "11-22-33" + "-44-55-66"})
    assert resp.status_code == 200
    assert resp.json()["match_value_masked"] == "11:22:**:**:**:66"
    async with admin_client._test_factory() as db:
        row = await db.get(UdpException, item_id)
        assert row.match_value == "11:22:33" + ":44:55:66"


@pytest.mark.asyncio
async def test_update_explicit_null_rejected_for_required_fields(
        admin_client, workstation_profiles):
    profile, _ = workstation_profiles
    item_id = await create_process_exception(
        admin_client, profile_id=profile.id, value="NamedProcess")
    await admin_client.put(f"{_base(profile)}/{item_id}",
                           json={"display_name": "备注"})
    for field in ("match_type", "match_value", "enabled", "sort_order"):
        resp = await admin_client.put(f"{_base(profile)}/{item_id}",
                                      json={field: None})
        assert resp.status_code == 422, field
        assert "NamedProcess" not in resp.text
    # display_name 可空：显式 null = 清空（区别于省略）
    resp = await admin_client.put(f"{_base(profile)}/{item_id}",
                                  json={"display_name": None})
    assert resp.status_code == 200
    assert resp.json()["display_name"] is None


# ===== 删除 / 列表排序与白名单 =====

@pytest.mark.asyncio
async def test_delete_no_echo_and_gone(admin_client, workstation_profiles):
    profile, _ = workstation_profiles
    item_id = await create_process_exception(admin_client, profile_id=profile.id)
    resp = await admin_client.delete(f"{_base(profile)}/{item_id}")
    assert resp.status_code == 204
    assert resp.text == ""
    assert (await admin_client.get(_base(profile))).json() == []
    again = await admin_client.delete(f"{_base(profile)}/{item_id}")
    assert again.status_code == 404
    assert "SurgeTauEngine" not in again.text


@pytest.mark.asyncio
async def test_list_ordering_whitelist_and_disabled_visible(
        admin_client, workstation_profiles):
    profile, _ = workstation_profiles
    a = await admin_client.post(_base(profile), json={
        "match_type": "process", "match_value": "SurgeTauEngine",
        "display_name": "中继", "sort_order": 20})
    b = await admin_client.post(_base(profile), json={
        "match_type": "process", "match_value": "GoogleChromeHelper",
        "sort_order": 10, "enabled": False})
    c = await admin_client.post(_base(profile), json={
        "match_type": "process", "match_value": "mDNSResponderWorker",
        "sort_order": 10})
    listed = (await admin_client.get(_base(profile))).json()
    # (sort_order, id)：b/c 同序按 id，a 序号最大排最后
    assert [i["id"] for i in listed] == [b.json()["id"], c.json()["id"], a.json()["id"]]
    assert listed[0]["enabled"] is False
    assert listed[2]["display_name"] == "中继"
    for item in listed:
        assert set(item) == LIST_FIELDS
        assert "match_value" not in item


# ===== 父档案约束：缺失 404 / 跨档案统一 404 =====

@pytest.mark.asyncio
async def test_udp_exception_cannot_cross_profile(
        admin_client, workstation_profiles,
):
    first, second = workstation_profiles
    item_id = await create_process_exception(admin_client, profile_id=first.id)
    assert (await admin_client.put(
        f"/api/configs/profiles/{second.id}/udp-exceptions/{item_id}",
        json={"enabled": False},
    )).status_code == 404
    # DELETE 与列表同样不跨档案
    assert (await admin_client.delete(
        f"/api/configs/profiles/{second.id}/udp-exceptions/{item_id}",
    )).status_code == 404
    assert (await admin_client.get(_base(second))).json() == []
    listed = (await admin_client.get(_base(first))).json()
    assert [i["id"] for i in listed] == [item_id]


@pytest.mark.asyncio
async def test_missing_profile_is_404_for_all_verbs(admin_client):
    base = "/api/configs/profiles/999999/udp-exceptions"
    assert (await admin_client.get(base)).status_code == 404
    assert (await admin_client.post(base, json={
        "match_type": "process", "match_value": "x"})).status_code == 404
    assert (await admin_client.put(base + "/1",
                                   json={"enabled": False})).status_code == 404
    assert (await admin_client.delete(base + "/1")).status_code == 404


# ===== 访问边界：local_only / lan_authenticated / CSRF（沿用阶段 A 中间件） =====

@pytest.mark.asyncio
async def test_local_only_non_loopback_denied(anonymous_client, gateway_profile):
    assert (await anonymous_client.get(_base(gateway_profile))).status_code == 403
    assert (await anonymous_client.post(_base(gateway_profile), json={
        "match_type": "mac_address",
        "match_value": "02:00:00:00:00:01"})).status_code == 403


@pytest.mark.asyncio
async def test_lan_unauthenticated_cannot_access(secure_app, admin_client,
                                                 gateway_profile):
    client = await _make_client(secure_app, "203.0.113.51")
    try:
        resp = await admin_client.put("/api/settings/access", json={
            "installation_role": "home_gateway",
            "admin_access": "lan_authenticated",
            "current_password": None,
            "new_password": "correct horse battery staple",
        })
        assert resp.status_code == 200, resp.text
        assert (await client.get(_base(gateway_profile))).status_code == 401
        assert (await client.post(_base(gateway_profile), json={
            "match_type": "mac_address",
            "match_value": "02:00:00:00:00:01"})).status_code == 401
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_writes_require_csrf(csrf_less_client, workstation_profiles):
    profile, _ = workstation_profiles
    assert (await csrf_less_client.post(_base(profile), json={
        "match_type": "process", "match_value": "x"})).status_code == 403
    assert (await csrf_less_client.put(
        f"{_base(profile)}/1", json={"enabled": False})).status_code == 403
    assert (await csrf_less_client.delete(f"{_base(profile)}/1")).status_code == 403
    # 安全方法凭隐式会话可读
    assert (await csrf_less_client.get(_base(profile))).status_code == 200


# ===== 全响应文本扫描：完整合成值绝不出现（含跨档案与错误路径） =====

@pytest.mark.asyncio
async def test_full_value_never_serialized_in_responses(
        admin_client, workstation_profiles):
    first, second = workstation_profiles
    secret = "VerySecretProcessName"
    item_id = await create_process_exception(
        admin_client, profile_id=first.id, value=secret)
    responses = [
        await admin_client.get(_base(first)),
        await admin_client.put(f"{_base(first)}/{item_id}",
                               json={"match_value": secret}),          # 409
        await admin_client.put(f"{_base(first)}/{item_id}",
                               json={"match_value": ""}),              # 422
        await admin_client.put(f"{_base(second)}/{item_id}",
                               json={"enabled": False}),               # 404 跨档案
        await admin_client.post(_base(first), json={
            "match_type": "mac_address",
            "match_value": "02:00:00:00:00:01"}),                      # 422 组合
        await admin_client.delete(f"{_base(second)}/{item_id}"),       # 404 跨档案
    ]
    for resp in responses:
        assert secret not in resp.text, (resp.status_code, resp.text)
    assert "02:00:00:00:00:01" not in responses[4].text


@pytest.mark.asyncio
async def test_validation_errors_never_echo_request_input(
        admin_client, workstation_profiles):
    """FastAPI 默认 422 结构携带原始 input；全局处理器已剔除，锁定该行为"""
    profile, _ = workstation_profiles
    resp = await admin_client.post(_base(profile), json={
        "match_type": "process", "match_value": "a" * 256})
    assert resp.status_code == 422
    assert "a" * 256 not in resp.text
    assert '"input"' not in resp.text


# ===== Fix round 1：掩码碰撞 / 严格 MAC 契约 / 静态校验消息 / DB 异常 =====

@pytest.mark.asyncio
async def test_process_mask_collision_falls_back_on_all_paths(
        admin_client, workstation_profiles):
    """值本身形如掩码候选（ab**cd）时 create/list/update 全文不含原值"""
    profile, _ = workstation_profiles
    secret = "ab**cd"
    created = await admin_client.post(_base(profile), json={
        "match_type": "process", "match_value": secret})
    assert created.status_code == 201
    assert secret not in created.text
    assert created.json()["match_value_masked"] == "***"
    item_id = created.json()["id"]

    listed = await admin_client.get(_base(profile))
    assert secret not in listed.text
    assert listed.json()[0]["match_value_masked"] == "***"

    updated = await admin_client.put(f"{_base(profile)}/{item_id}",
                                     json={"match_value": "xy**zw"})
    assert updated.status_code == 200
    assert "xy**zw" not in updated.text
    assert updated.json()["match_value_masked"] == "***"


@pytest.mark.asyncio
async def test_noncanonical_mac_stock_masked(admin_client, gateway_profile):
    """绕库直插的 MAC 存量：非法形态 fallback；首行小写候选与掩码形
    存量 raw 碰撞（AA:BB:**:**:**:FF）也 fallback，完整 raw 全不回显"""
    from app.models import UdpException

    rows = [
        "aa:bb:cc" + ":dd:ee:ff",   # 存量小写：候选为大写掩码形态
        "0G:00:00:00:00:01",        # 非 hex
        "aa-bb-cc" + "-dd-ee-ff",   # 错误分隔（未规范化的横线存量）
        "AA:BB:**:**:**:FF",        # 六组但含 **（掩码形字面量存量）
    ]
    async with admin_client._test_factory() as db:
        for value in rows:
            db.add(UdpException(profile_id=gateway_profile.id,
                                match_type="mac_address", match_value=value))
        await db.commit()
    resp = await admin_client.get(_base(gateway_profile))
    masked = [item["match_value_masked"] for item in resp.json()]
    # 首行候选 AA:BB:**:**:**:FF 恰等于第四行 raw：跨记录碰撞 → 全部
    # fallback（***）；非法形态本就 fallback
    assert masked.count("***") == len(rows)
    # 掩码形 raw 与小写/非hex/横线原值一律不在响应文本
    assert "AA:BB:**:**:**:FF" not in resp.text
    for value in ("aa:bb:cc" + ":dd:ee:ff", "0G:00:00:00:00:01",
                  "aa-bb-cc" + "-dd-ee-ff"):
        assert value not in resp.text


@pytest.mark.asyncio
async def test_lowercase_mac_masked_uppercase_when_no_collision(
        admin_client, gateway_profile):
    """无碰撞时存量小写 MAC 按大写契约掩码（02:00:**:**:**:01 形态）"""
    from app.models import UdpException

    async with admin_client._test_factory() as db:
        db.add(UdpException(profile_id=gateway_profile.id,
                            match_type="mac_address",
                            match_value="aa:bb:cc" + ":dd:ee:ff"))
        await db.commit()
    listed = await admin_client.get(_base(gateway_profile))
    assert listed.json()[0]["match_value_masked"] == "AA:BB:**:**:**:FF"
    assert "aa:bb:cc" + ":dd:ee:ff" not in listed.text


@pytest.mark.asyncio
async def test_validation_errors_use_static_messages(
        admin_client, workstation_profiles):
    """422 仅含 loc/type/静态中文 msg；不透传动态 msg/input/ctx"""
    profile, _ = workstation_profiles
    checks = [
        ({"match_type": "process"}, "missing", "缺少必填字段"),
        ({"match_type": "device", "match_value": "x"},
         "enum", "字段值不在允许的取值范围内"),
        ({"match_type": "process", "match_value": "a" * 300},
         "string_too_long", "字符串长度不符合要求"),
        ({"match_type": "process", "match_value": "ok", "enabled": "banana"},
         "bool_parsing", "字段类型不正确"),
    ]
    for payload, want_type, want_msg in checks:
        resp = await admin_client.post(_base(profile), json=payload)
        assert resp.status_code == 422, (payload.keys(), resp.text)
        detail = resp.json()["detail"]
        assert all(set(e) == {"loc", "type", "msg"} for e in detail)
        assert any(e["type"] == want_type and e["msg"] == want_msg
                   for e in detail), (want_type, detail)
        assert "Input should" not in resp.text
        assert '"input"' not in resp.text


@pytest.mark.asyncio
async def test_display_name_control_chars_rejected_statically(
        admin_client, workstation_profiles):
    """自定义 validator（value_error 路径）走静态映射，不回显输入"""
    profile, _ = workstation_profiles
    resp = await admin_client.post(_base(profile), json={
        "match_type": "process", "match_value": "ok",
        "display_name": "bad\x01name"})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert any(e["type"] == "value_error" and e["msg"] == "字段值不合法"
               for e in detail)
    assert "bad\x01name" not in resp.text


@pytest.mark.asyncio
async def test_validation_handler_never_passes_dynamic_msg():
    """直接调用生产处理器：msg 含秘密时输出只有静态映射，无 input/ctx"""
    import json as jsonlib

    from fastapi.exceptions import RequestValidationError
    from app.main import validation_error_handler

    secret = "SuperSecretToken123"
    exc = RequestValidationError([{
        "type": "value_error",
        "loc": ("body", "match_value"),
        "msg": f"Value error, invalid {secret}",
        "input": secret,
        "ctx": {"error": ValueError(f"{secret} boom")},
    }])
    resp = await validation_error_handler(None, exc)
    assert resp.status_code == 422
    body = jsonlib.loads(resp.body)
    assert secret not in resp.body.decode()
    assert body["detail"] == [{
        "loc": ["body", "match_value"], "type": "value_error",
        "msg": "字段值不合法"}]


@pytest.mark.asyncio
async def test_db_operational_error_static_503_and_connection_reusable(
        admin_client, gateway_profile, monkeypatch, caplog):
    """OperationalError 含秘密参数：503 静态文案，日志只有异常类型名，
    rollback 后同一工厂连接继续可用"""
    import logging as logginglib

    from sqlalchemy.exc import OperationalError
    from sqlalchemy.ext.asyncio import AsyncSession

    secret = "TOPSECRET-mac-value"
    orig_commit = AsyncSession.commit
    calls = {"n": 0}

    async def flaky(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError(
                "INSERT INTO udp_exceptions", {"match_value": secret},
                RuntimeError(f"disk error near {secret}"))
        return await orig_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", flaky)
    with caplog.at_level(logginglib.ERROR, logger="app.routers.udp"):
        resp = await admin_client.post(_base(gateway_profile), json={
            "match_type": "mac_address", "match_value": "02:00:00:00:00:01"})
    assert resp.status_code == 503
    assert "服务暂时不可用" in resp.text
    assert secret not in resp.text
    assert secret not in caplog.text
    assert "OperationalError" in caplog.text  # 只记类型名
    # rollback 生效：工厂健康，同值重试成功且库里只有一条
    retry = await admin_client.post(_base(gateway_profile), json={
        "match_type": "mac_address", "match_value": "02:00:00:00:00:01"})
    assert retry.status_code == 201
    assert len((await admin_client.get(_base(gateway_profile))).json()) == 1


@pytest.mark.asyncio
async def test_db_statement_error_update_rolls_back(
        admin_client, workstation_profiles, monkeypatch):
    """StatementError 含秘密参数：update 503；rollback 使属性修改未持久化"""
    from sqlalchemy.exc import StatementError
    from sqlalchemy.ext.asyncio import AsyncSession

    profile, _ = workstation_profiles
    item_id = await create_process_exception(admin_client, profile_id=profile.id)
    secret = "TOPSECRET-stmt"
    orig_commit = AsyncSession.commit

    async def flaky(self):
        raise StatementError(
            f"update failed with {secret}",
            "UPDATE udp_exceptions", {"match_value": secret},
            RuntimeError("boom"))

    monkeypatch.setattr(AsyncSession, "commit", flaky)
    resp = await admin_client.put(f"{_base(profile)}/{item_id}",
                                  json={"enabled": False})
    monkeypatch.undo()
    assert resp.status_code == 503
    assert secret not in resp.text
    row = (await admin_client.get(_base(profile))).json()[0]
    assert row["enabled"] is True  # rollback：修改未落库


def test_runtime_engine_hides_bound_parameters(tmp_path):
    """运行时引擎（含 rebind 路径）一律 hide_parameters：异常不带绑定值"""
    from app.database import DatabaseRuntime

    runtime = DatabaseRuntime.create(tmp_path / "hide.db")
    try:
        assert runtime.engine.sync_engine.hide_parameters is True
        runtime.bind(tmp_path / "hide2.db")  # rebind 同一创建点
        assert runtime.engine.sync_engine.hide_parameters is True
    finally:
        import asyncio
        asyncio.run(runtime.dispose())


# ===== Fix round 2：跨记录掩码碰撞（对全库敏感值集合 fail closed） =====

@pytest.mark.asyncio
async def test_process_mask_collides_with_other_record_raw(
        admin_client, workstation_profiles):
    """一条记录的掩码候选恰等于另一条完整值（abcdef→ab**ef vs raw=ab**ef）：
    create/update/list 逐条与整体响应都不回显任一完整 raw"""
    first, second = workstation_profiles
    r2 = await admin_client.post(_base(second), json={
        "match_type": "process", "match_value": "ab**ef"})
    assert r2.status_code == 201
    assert r2.json()["match_value_masked"] == "***"  # 自碰撞 fallback
    assert "ab**ef" not in r2.text

    r1 = await admin_client.post(_base(first), json={
        "match_type": "process", "match_value": "abcdef"})
    assert r1.status_code == 201
    assert r1.json()["match_value_masked"] == "***"  # 候选命中 rec2 raw
    assert "ab**ef" not in r1.text and "abcdef" not in r1.text

    for profile in (first, second):
        listed = await admin_client.get(_base(profile))
        assert "ab**ef" not in listed.text
        assert "abcdef" not in listed.text

    upd = await admin_client.put(f"{_base(first)}/{r1.json()['id']}",
                                 json={"enabled": False})
    assert upd.status_code == 200
    assert "ab**ef" not in upd.text and "abcdef" not in upd.text


@pytest.mark.asyncio
async def test_mac_candidate_collides_with_stored_raw_variants(
        admin_client, gateway_profile):
    """合法 MAC 候选与存量掩码形 raw 碰撞（含小写语义等价）：不回显"""
    from app.models import UdpException

    async with admin_client._test_factory() as db:
        db.add(UdpException(profile_id=gateway_profile.id,
                            match_type="mac_address",
                            match_value="AA:BB:**:**:**:FF"))
        db.add(UdpException(profile_id=gateway_profile.id,
                            match_type="mac_address",
                            match_value="aa:bb:cc" + ":dd:ee:ff"))
        await db.commit()
    created = await admin_client.post(_base(gateway_profile), json={
        "match_type": "mac_address", "match_value": "AA:BB:CC" + ":DD:EE:FF"})
    assert created.status_code == 201
    assert created.json()["match_value_masked"] == "***"
    listed = await admin_client.get(_base(gateway_profile))
    assert "AA:BB:**:**:**:FF" not in listed.text
    assert "aa:bb:cc" + ":dd:ee:ff" not in listed.text
    assert "AA:BB:CC" + ":DD:EE:FF" not in listed.text


@pytest.mark.asyncio
async def test_mask_collision_across_profiles(admin_client, workstation_profiles):
    """碰撞检查覆盖全库：另一档案的 raw 也阻止本档案掩码回显"""
    first, second = workstation_profiles
    await create_process_exception(admin_client, profile_id=second.id,
                                   value="ab**ef")
    resp = await admin_client.post(_base(first), json={
        "match_type": "process", "match_value": "abcdef"})
    assert resp.status_code == 201
    assert resp.json()["match_value_masked"] == "***"
    assert "ab**ef" not in resp.text
    listed = await admin_client.get(_base(first))  # 仅 first 的列表
    assert "ab**ef" not in listed.text
    assert "abcdef" not in listed.text


@pytest.mark.asyncio
async def test_fallback_placeholder_itself_stored(admin_client, workstation_profiles):
    """占位 *** 与 [redacted] 都被存为 raw 时，碰撞记录递增到 <redacted:1>；
    任何 fallback 都不含原值片段"""
    from app.models import UdpException

    profile, _ = workstation_profiles
    async with admin_client._test_factory() as db:
        db.add(UdpException(profile_id=profile.id, match_type="process",
                            match_value="***"))
        db.add(UdpException(profile_id=profile.id, match_type="process",
                            match_value="[redacted]"))
        await db.commit()
    # 短值本应 ***，但 *** 已是存储 raw；[redacted] 也是 → <redacted:1>
    created = await admin_client.post(_base(profile), json={
        "match_type": "process", "match_value": "ab"})
    assert created.status_code == 201
    assert created.json()["match_value_masked"] == "<redacted:1>"
    listed = await admin_client.get(_base(profile))
    assert "[redacted]" not in listed.text  # 完整 raw 占位不回显
    for item in listed.json():
        assert item["match_value_masked"] not in ("***", "[redacted]")
        assert item["match_value_masked"] != "ab"


# ===== Fix round 3：掩码形 MAC 存量的等价规范（MAC-like 规范器） =====

@pytest.mark.parametrize("stock", [
    "aa:bb:**:**:**:ff",   # 小写冒号掩码形
    "AA-BB-**-**-**-FF",   # 大写横线掩码形
    "aa:bb-**:**-**:ff",   # 混合分隔（保守纳入）
])
@pytest.mark.asyncio
async def test_mask_like_mac_stock_collides_with_candidate(
        admin_client, gateway_profile, stock):
    """掩码形 MAC 存量（含分隔/大小写变体）经规范 key 与合法 MAC 候选
    碰撞：list/create/update 均 fallback，不回显候选与存量原文"""
    from app.models import UdpException

    async with admin_client._test_factory() as db:
        db.add(UdpException(profile_id=gateway_profile.id,
                            match_type="mac_address", match_value=stock))
        await db.commit()
    created = await admin_client.post(_base(gateway_profile), json={
        "match_type": "mac_address", "match_value": "AA:BB:CC" + ":DD:EE:FF"})
    assert created.status_code == 201
    assert created.json()["match_value_masked"] == "***"  # key 碰撞 fallback
    assert "AA:BB:**:**:**:FF" not in created.text
    listed = await admin_client.get(_base(gateway_profile))
    assert "AA:BB:**:**:**:FF" not in listed.text
    assert stock not in listed.text
    upd = await admin_client.put(
        f"{_base(gateway_profile)}/{created.json()['id']}",
        json={"enabled": False})
    assert upd.status_code == 200
    assert "AA:BB:**:**:**:FF" not in upd.text


@pytest.mark.asyncio
async def test_non_mac_values_not_over_normalized(admin_client,
                                                  workstation_profiles):
    """非 MAC 形值只按原文碰撞：含冒号的 process 值正常掩码，不误
    fallback；MAC 档案照常输出掩码（互不干扰）"""
    profile, _ = workstation_profiles
    resp = await admin_client.post(_base(profile), json={
        "match_type": "process", "match_value": "not:a:mac:value!"})
    assert resp.status_code == 201
    assert resp.json()["match_value_masked"] == "no**e!"
    gw = await _create_profile(admin_client, "隔离档案", "mac_gateway")
    mac = await admin_client.post(_base(gw), json={
        "match_type": "mac_address", "match_value": "02:00:00:00:00:01"})
    assert mac.status_code == 201
    assert mac.json()["match_value_masked"] == "02:00:**:**:**:01"
