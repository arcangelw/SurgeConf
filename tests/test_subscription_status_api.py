"""订阅同步状态 API 测试（Task 4）：安全 DTO、错误码收敛、状态聚合。

覆盖：
- 列表响应严格 12 字段（无 url/created_at/server/config），时间为
  YYYY-MM-DDTHH:MM:SSZ（naive 当 UTC、aware 转 UTC、去微秒）
- last_error_code 只允许 Task2 固定五码，脏/未知码收敛 internal_error
- 详情 = summary + url（url 仅供编辑回填）
- /status-summary 聚合：exact keys、禁用不计数、due 维度可重叠、
  边界（过去/未来/auto off/next NULL）、响应不泄漏 URL
- 颜色：create/update 拒绝非 #RRGGBB；存量脏数据序列化 fallback
- ASGI 层固定路由不被 /{sub_id} 吞掉
"""

import re
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from pydantic import ValidationError
from unittest.mock import patch

from app.models import Subscription
from app.routers.subscriptions import (
    SubscriptionCreate,
    SubscriptionUpdate,
    _palette_color,
    _iso_utc,
    _subscription_summary,
    get_subscription,
    list_subscriptions,
    subscription_status_summary,
)
from app.services.subscription import SYNC_ERROR_CODES

# brief 定义的安全摘要字段面（严格 12 项）
SUMMARY_FIELDS = {
    "id", "name", "tag", "color", "enabled", "auto_update", "update_interval",
    "last_attempt_at", "last_success_at", "next_update_at",
    "last_error_code", "last_node_count",
}

SECRET_URL = "https://secret-provider.example.com/path?token=t0k3n"


def _mk_sub(**overrides) -> Subscription:
    fields = dict(
        name="测试订阅",
        url=SECRET_URL,
        tag="主力",
        color="#00b894",
        enabled=True,
        auto_update=True,
        update_interval=3600,
    )
    fields.update(overrides)
    return Subscription(**fields)


# ===== 时间序列化 =====

def test_iso_utc_naive_aware_and_microseconds():
    """naive 当 UTC；aware 转 UTC；微秒截断；None 透传"""
    assert _iso_utc(None) is None
    assert _iso_utc(datetime(2026, 10, 17, 8, 30, 5, 123456)) == "2026-10-17T08:30:05Z"
    aware = datetime(2026, 10, 17, 16, 30, 5, 999999, tzinfo=timezone(timedelta(hours=8)))
    assert _iso_utc(aware) == "2026-10-17T08:30:05Z"


# ===== 安全 DTO =====

@pytest.mark.asyncio
async def test_list_summary_exact_fields_and_z_time(db):
    """列表项严格 12 字段：无 url/created_at/server/config；时间带 Z 无微秒"""
    db.add(_mk_sub(
        last_attempt_at=datetime(2026, 10, 17, 1, 2, 3, 456789),
        last_success_at=datetime(2026, 10, 16, 23, 59, 59),
        next_update_at=datetime(2026, 10, 18, 0, 0, 0),
        last_node_count=42,
    ))
    await db.commit()
    (sub,) = (await db.execute(
        __import__("sqlalchemy").select(Subscription))).scalars().all()

    items = await list_subscriptions(db=db)
    assert len(items) == 1
    item = items[0]
    assert set(item) == SUMMARY_FIELDS
    assert item["last_attempt_at"] == "2026-10-17T01:02:03Z"
    assert item["last_success_at"] == "2026-10-16T23:59:59Z"
    assert item["last_success_at"].endswith("Z")
    assert item["next_update_at"] == "2026-10-18T00:00:00Z"
    assert item["last_node_count"] == 42
    # 时间格式整体校验（无微秒、无偏移后缀）
    iso_re = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    for key in ("last_attempt_at", "last_success_at", "next_update_at"):
        assert iso_re.match(item[key]), item[key]


@pytest.mark.asyncio
async def test_error_code_allowlist_converges_unknown(db):
    """五码原样返回；脏/未知码收敛 internal_error；None 保持 None"""
    codes = {
        1: None,
        2: "timeout",
        3: "weird-db-value <script>",
        4: "internal_error",
    }
    for sub_id, code in codes.items():
        db.add(_mk_sub(name=f"sub{sub_id}", last_error_code=code))
    await db.commit()

    items = {i["name"]: i for i in await list_subscriptions(db=db)}
    assert items["sub1"]["last_error_code"] is None
    assert items["sub2"]["last_error_code"] == "timeout"
    assert items["sub3"]["last_error_code"] == "internal_error"
    assert items["sub4"]["last_error_code"] == "internal_error"
    # 污染值绝不原样出网
    assert all("<script>" not in (i["last_error_code"] or "")
               for i in items.values())


def test_sync_error_codes_are_the_fixed_five():
    """router/service 共享同一 allowlist（防漂移）"""
    assert SYNC_ERROR_CODES == {
        "timeout", "http_status", "invalid_content",
        "network_error", "internal_error",
    }


@pytest.mark.asyncio
async def test_detail_is_summary_plus_url(db):
    """详情 = summary + url；url 值正确（仅编辑用途）"""
    db.add(_mk_sub())
    await db.commit()
    sub = (await db.execute(
        __import__("sqlalchemy").select(Subscription))).scalar_one()

    detail = await get_subscription(sub_id=sub.id, db=db)
    assert set(detail) == SUMMARY_FIELDS | {"url"}
    assert detail["url"] == SECRET_URL

    # 404 语义保留
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await get_subscription(sub_id=9999, db=db)
    assert exc.value.status_code == 404


# ===== 聚合 =====

@pytest.mark.asyncio
async def test_status_summary_matrix(db):
    """聚合谓词矩阵：due 重叠、failed+never 同时成立、禁用排除、边界"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    past = now - timedelta(seconds=10)
    future = now + timedelta(hours=1)

    cases = [
        # (期望 due/healthy/failed/never, 构造参数)
        # 1. due + healthy：有成功、无错误、到期（due 维度与 healthy 重叠）
        ((1, 1, 0, 0), dict(enabled=True, auto_update=True, next_update_at=past,
                            last_success_at=past, last_error_code=None)),
        # 2. healthy 未到期
        ((0, 1, 0, 0), dict(enabled=True, auto_update=True, next_update_at=future,
                            last_success_at=past, last_error_code=None)),
        # 3. failed（有历史成功，仍保留 success 供 UI 显示）
        ((0, 0, 1, 0), dict(enabled=True, auto_update=True, next_update_at=future,
                            last_success_at=past, last_error_code="timeout")),
        # 4. failed + never 重叠（从未成功且失败）
        ((0, 0, 1, 1), dict(enabled=True, auto_update=True, next_update_at=future,
                            last_success_at=None, last_error_code="network_error")),
        # 5. never（从未尝试，auto off）
        ((0, 0, 0, 1), dict(enabled=True, auto_update=False, next_update_at=None,
                            last_success_at=None, last_error_code=None)),
        # 6. due + never：从未成功但已到期
        ((1, 0, 0, 1), dict(enabled=True, auto_update=True, next_update_at=past,
                            last_success_at=None, last_error_code=None)),
        # 7. enabled 但 auto off + next 过期 → 不计 due
        ((0, 1, 0, 0), dict(enabled=True, auto_update=False, next_update_at=past,
                            last_success_at=past, last_error_code=None)),
        # 8. 禁用订阅：即使错误+到期+从未成功，四项全不计
        ((0, 0, 0, 0), dict(enabled=False, auto_update=True, next_update_at=past,
                            last_success_at=None, last_error_code="http_status")),
        # 9. due 边界：next 过去 60 秒（<= 路由内部 now，避免时钟竞态）
        ((1, 1, 0, 0), dict(enabled=True, auto_update=True,
                            next_update_at=now - timedelta(seconds=60),
                            last_success_at=past, last_error_code=None)),
    ]

    for i, (_, fields) in enumerate(cases):
        db.add(_mk_sub(name=f"case{i}", **fields))
    await db.commit()

    summary = await subscription_status_summary(db=db)
    due = sum(exp[0] for exp, _ in cases)
    healthy = sum(exp[1] for exp, _ in cases)
    failed = sum(exp[2] for exp, _ in cases)
    never = sum(exp[3] for exp, _ in cases)
    assert summary == {"due": due, "healthy": healthy,
                       "failed": failed, "never_synced": never}
    assert (due, healthy, failed, never) == (3, 4, 2, 3)


@pytest.mark.asyncio
async def test_status_summary_error_code_normalization(db):
    """聚合与 DTO/UI 同口径：先 _safe_error_code 规范化再判定。

    None 才无错误；空字符串/未知码收敛 internal_error 并计 failed
    （尤其空字符串不得被当作"无错误"计入 healthy）。
    """
    from sqlalchemy import select as _select

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    past = now - timedelta(hours=2)
    cases = [
        (None, "healthy"),          # 无错误 + 曾成功
        ("", "failed"),             # 空字符串 → internal_error → failed（非 healthy）
        ("garbage-code", "failed"), # 未知码 → internal_error → failed
        ("timeout", "failed"),      # 合法五码原样计 failed
        ("http_status", "failed"),
        ("invalid_content", "failed"),
        ("network_error", "failed"),
        ("internal_error", "failed"),
    ]
    for i, (code, _) in enumerate(cases):
        db.add(_mk_sub(name=f"ec{i}", enabled=True, auto_update=False,
                       next_update_at=None, last_success_at=past,
                       last_error_code=code))
    await db.commit()

    summary = await subscription_status_summary(db=db)
    n = len(cases)
    assert summary == {"due": 0, "healthy": 1, "failed": n - 1,
                       "never_synced": 0}
    # 空字符串订阅绝不在 healthy：failed 计数覆盖除 None 外全部
    assert summary["failed"] == 7 and summary["healthy"] == 1

    # DTO 同口径：空字符串/未知码序列化输出 internal_error
    subs = {s.name: s for s in (await db.execute(_select(Subscription))).scalars().all()}
    assert _subscription_summary(subs["ec1"])["last_error_code"] == "internal_error"
    assert _subscription_summary(subs["ec3"])["last_error_code"] == "timeout"


@pytest.mark.asyncio
async def test_status_summary_exact_keys_and_no_url_leak(db):
    """聚合响应 exact keys；不读/不返回 URL（响应文本无订阅 URL 字样）"""
    import json

    # 空库全零（先于任何数据）
    empty = await subscription_status_summary(db=db)
    assert empty == {"due": 0, "healthy": 0, "failed": 0, "never_synced": 0}

    db.add(_mk_sub())
    await db.commit()

    summary = await subscription_status_summary(db=db)
    assert set(summary) == {"due", "healthy", "failed", "never_synced"}
    assert SECRET_URL not in json.dumps(summary)
    assert all(isinstance(v, int) for v in summary.values())


@pytest.mark.asyncio
async def test_status_summary_route_order_not_swallowed(client):
    """ASGI 层：GET /api/subscriptions/status-summary 是聚合而非 /{sub_id} 422"""
    resp = await client.get("/api/subscriptions/status-summary")
    assert resp.status_code == 200
    assert set(resp.json()) == {"due", "healthy", "failed", "never_synced"}

    # 422 不回显输入（颜色非法值绝不出现在响应文本）
    bad = await client.post("/api/subscriptions", json={
        "name": "n", "url": "https://example.com/s", "color": "#123456\n"})
    assert bad.status_code == 422
    assert "#123456" not in bad.text
    assert set(bad.json()) == {"detail"}
    for err in bad.json()["detail"]:
        assert set(err) == {"loc", "type", "msg"}

    # 列表端点同样返回安全 DTO（无 URL 泄漏）
    listing = await client.get("/api/subscriptions")
    assert listing.status_code == 200
    for item in listing.json():
        assert set(item) == SUMMARY_FIELDS


# ===== 颜色 =====

def test_color_rejected_on_create_and_update():
    """API create/update 拒绝非 #RRGGBB；合法值收敛小写"""
    base = dict(name="n", url="https://example.com/s")
    for bad in ("red", "#fff", "#GGGGGG", "#12345", "javascript:alert(1)", "#1234567",
                "#123456\n", " #123456", "#123456 ", "#123456\nx", "\n#123456"):
        with pytest.raises(ValidationError):
            SubscriptionCreate(**base, color=bad)
        with pytest.raises(ValidationError):
            SubscriptionUpdate(color=bad)
    assert SubscriptionCreate(**base, color="#AbCdEf").color == "#abcdef"
    assert SubscriptionUpdate(color=None).color is None  # null = 清除


@pytest.mark.asyncio
async def test_serializer_color_fallback_for_legacy_rows(db):
    """存量脏颜色（绕过 API 直写）序列化收敛到调色板 fallback"""
    db.add(_mk_sub(name="dirty", color="javascript:alert(1)"))
    db.add(_mk_sub(name="newline", color="#123456\n"))
    db.add(_mk_sub(name="ok", color="#E17055"))
    db.add(_mk_sub(name="nullcolor", color=None))
    await db.commit()
    subs = {s.name: s for s in (await db.execute(
        __import__("sqlalchemy").select(Subscription))).scalars().all()}

    assert _subscription_summary(subs["dirty"])["color"].startswith("#")
    assert re.match(r"^#[0-9a-f]{6}$", _subscription_summary(subs["dirty"])["color"])
    assert _subscription_summary(subs["ok"])["color"] == "#e17055"
    assert _subscription_summary(subs["newline"])["color"] == _palette_color(subs["newline"].id)
    assert re.match(r"^#[0-9a-f]{6}$", _subscription_summary(subs["nullcolor"])["color"])


# ===== ASGI client 复用（与 test_api_endpoints 同一装配） =====

@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """独立临时库 + 手动驱动 lifespan（ASGITransport 不触发 lifespan 事件）"""
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
            async with AsyncClient(transport=ASGITransport(app=fastapi_app),
                                   base_url="http://test") as c:
                import re as _re
                page = await c.get("/")
                meta = _re.search(r'name="csrf-token" content="([^"]+)"', page.text)
                assert meta, "GET / 应签发隐式会话并输出 CSRF meta"
                c.headers["X-CSRF-Token"] = meta.group(1)
                yield c

    fastapi_app.dependency_overrides.pop(get_db, None)
    await test_runtime.dispose()
