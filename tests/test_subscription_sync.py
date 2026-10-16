"""订阅同步事务化 upsert 与失败保留测试。

覆盖：rename 保 ID 与人工状态、等价 key 更新 provider 字段、新节点
默认值、失败/空结果保留最后可用节点、五类错误码分类与状态、调度
语义、去重计数、写事务原子回滚、now 规范、路由 404/502 脱敏、
sync-all 混合结果、日志无异常文字。
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (AsyncSession, async_sessionmaker,
                                    create_async_engine)

import app.services.subscription as subscription_service
from app.database import Base
from app.models import Subscription, ProxyNode
from app.routers.subscriptions import sync_one, sync_all
from app.services.node_identity import stable_node_key
from app.services.subscription import (SyncResult, safe_sync_error,
                                       sync_all_subscriptions,
                                       sync_subscription)

UTC_NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
UTC_NOW_NAIVE = UTC_NOW.replace(tzinfo=None)
URL = "https://sub.example.com/feed"
SAFE_KEYS = {"subscription_id", "success", "node_count", "inserted",
             "updated", "removed", "error_code", "public_message"}


def node_payload(name="New Name"):
    """与旧节点同连接身份的标准拉取结果"""
    config = f"{name} = trojan, 1.1.1.1, 443, password=x"
    return {"name": name, "node_type": "trojan", "server": "1.1.1.1",
            "port": 443, "config": config}


def equivalent_raw(name="Renamed"):
    """格式等价（大小写/端口前导零）但 raw 文本不同的同连接节点"""
    config = f"{name} = TROJAN, 1.1.1.1, 0443, password=x"
    return {"name": name, "node_type": "trojan", "server": "1.1.1.1",
            "port": 443, "config": config}


def other_payload(name="Other"):
    """不同连接身份（不同主机）"""
    config = f"{name} = trojan, 9.9.9.9, 443, password=y"
    return {"name": name, "node_type": "trojan", "server": "9.9.9.9",
            "port": 443, "config": config}


def fresh_payload(name="Fresh"):
    """第三个全新连接身份（既不匹配 Keep 也不匹配 Drop，触发 INSERT）"""
    config = f"{name} = trojan, 8.8.8.8, 443, password=z"
    return {"name": name, "node_type": "trojan", "server": "8.8.8.8",
            "port": 443, "config": config}


def timeout_exc(message=f"synthetic timeout {URL}"):
    return httpx.TimeoutException(message)


def status_exc(code=503):
    req = httpx.Request("GET", URL)
    resp = httpx.Response(code, request=req)
    return httpx.HTTPStatusError("Service Unavailable", request=req, response=resp)


@pytest_asyncio.fixture
async def subscription(db):
    sub = Subscription(name="测试订阅", url=URL, enabled=True,
                       auto_update=True, update_interval=3600)
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return sub


async def add_subscription_node(db, sub, name="Old", config=None,
                                region_manual=None, enabled=True,
                                pinned_services=None, region_auto=None):
    config = config or f"{name} = trojan, 1.1.1.1, 443, password=x"
    node = ProxyNode(
        name=name, subscription_id=sub.id, node_type="trojan",
        server="1.1.1.1", port=443, config=config,
        stable_key=stable_node_key({"name": name, "config": config}),
        region_auto=region_auto, region_manual=region_manual,
        enabled=enabled,
        pinned_services=pinned_services if pinned_services is not None else [],
    )
    db.add(node)
    await db.commit()
    await db.refresh(node)
    return node


async def node_snapshot(db, sub_id):
    """节点全字段快照（含 id 与人工状态），用于不变性断言"""
    rows = (await db.execute(
        select(ProxyNode).where(ProxyNode.subscription_id == sub_id)
        .order_by(ProxyNode.id)
    )).scalars().all()
    return [(n.id, n.name, n.node_type, n.server, n.port, n.config,
             n.region_auto, n.region_manual, n.enabled, n.pinned_services,
             n.stable_key) for n in rows]


async def sub_state(db, sub_id):
    sub = await db.get(Subscription, sub_id)
    return (sub.last_attempt_at, sub.last_success_at, sub.next_update_at,
            sub.last_error_code, sub.last_node_count)


# ===== upsert 保留语义 =====

@pytest.mark.asyncio
async def test_sync_upsert_preserves_manual_state_and_id(db, subscription, monkeypatch):
    """同 key rename：ID 不变，人工地区/禁用状态/钉选保留，provider 更新"""
    from unittest.mock import AsyncMock
    original = await add_subscription_node(
        db, subscription, name="Old Name", region_manual="US",
        enabled=False, pinned_services=["AI服务"], region_auto="香港",
    )
    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        AsyncMock(return_value=[node_payload("New Name")]))
    result = await sync_subscription(db, subscription.id, now=UTC_NOW)

    assert result.success is True
    assert (result.inserted, result.updated, result.removed) == (0, 1, 0)
    refreshed = await db.get(ProxyNode, original.id)
    assert refreshed is not None
    assert refreshed.id == original.id
    assert refreshed.name == "New Name"
    assert refreshed.config == "New Name = trojan, 1.1.1.1, 443, password=x"
    assert refreshed.region_manual == "US"
    assert refreshed.enabled is False
    assert refreshed.pinned_services == ["AI服务"]
    # region_auto 属 provider 字段：由本次拉取结果决定（解析层不产出 → 清空）
    assert refreshed.region_auto is None


@pytest.mark.asyncio
async def test_sync_equivalent_config_updates_raw_provider(db, subscription, monkeypatch):
    """格式等价（大小写/端口前导零）同 key：raw config 更新为本次拉取文本"""
    from unittest.mock import AsyncMock
    old = await add_subscription_node(
        db, subscription, name="Old Name",
        config="Old Name = trojan, 1.1.1.1, 443, password=x")
    assert stable_node_key(node_payload()) == stable_node_key(equivalent_raw())
    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        AsyncMock(return_value=[equivalent_raw()]))
    result = await sync_subscription(db, subscription.id, now=UTC_NOW)

    assert result.success is True
    assert result.updated == 1
    refreshed = await db.get(ProxyNode, old.id)
    assert refreshed.id == old.id
    assert refreshed.config == "Renamed = TROJAN, 1.1.1.1, 0443, password=x"
    assert refreshed.name == "Renamed"


@pytest.mark.asyncio
async def test_sync_new_nodes_get_defaults(db, subscription, monkeypatch):
    """新 stable_key：enabled=True、人工地区为空、钉选为空列表"""
    from unittest.mock import AsyncMock
    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        AsyncMock(return_value=[node_payload("A"), other_payload("B")]))
    result = await sync_subscription(db, subscription.id, now=UTC_NOW)

    assert result.success is True
    assert (result.inserted, result.updated, result.removed) == (2, 0, 0)
    assert result.node_count == 2
    nodes = (await db.execute(
        select(ProxyNode).where(ProxyNode.subscription_id == subscription.id)
    )).scalars().all()
    assert len(nodes) == 2
    for node in nodes:
        assert node.enabled is True
        assert node.region_manual is None
        assert node.pinned_services == []
        assert node.stable_key


@pytest.mark.asyncio
async def test_sync_removes_missing_nodes_only_after_success(db, subscription, monkeypatch):
    """缺失旧节点仅在成功路径删除；失败/空结果同步零删改"""
    from unittest.mock import AsyncMock
    keep = await add_subscription_node(db, subscription, name="Keep")
    await add_subscription_node(db, subscription, name="Drop",
                                config="Drop = trojan, 9.9.9.9, 443, password=y")
    # 成功：只保留 keep
    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        AsyncMock(return_value=[node_payload("Keep")]))
    result = await sync_subscription(db, subscription.id, now=UTC_NOW)
    assert result.success is True
    assert (result.inserted, result.updated, result.removed) == (0, 1, 1)
    remaining = (await db.execute(
        select(ProxyNode).where(ProxyNode.subscription_id == subscription.id)
    )).scalars().all()
    assert [n.id for n in remaining] == [keep.id]

    # 失败：快照不变
    before = await node_snapshot(db, subscription.id)
    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        AsyncMock(side_effect=timeout_exc()))
    result = await sync_subscription(db, subscription.id, now=UTC_NOW)
    assert result.success is False
    assert await node_snapshot(db, subscription.id) == before
    # 空结果：快照不变
    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        AsyncMock(return_value=[]))
    result = await sync_subscription(db, subscription.id, now=UTC_NOW)
    assert result.success is False
    assert result.error_code == "invalid_content"
    assert await node_snapshot(db, subscription.id) == before


# ===== 错误分类与失败状态 =====

@pytest.mark.asyncio
@pytest.mark.parametrize("exc,expected_code", [
    (httpx.TimeoutException(f"synthetic timeout {URL}"), "timeout"),
    (status_exc(503), "http_status"),
    (httpx.ConnectError(f"refused {URL}"), "network_error"),
    (RuntimeError("unexpected boom /etc/passwd"), "internal_error"),
])
async def test_sync_failure_classes_keep_nodes_and_state(
        db, subscription, monkeypatch, exc, expected_code):
    from unittest.mock import AsyncMock
    await add_subscription_node(db, subscription, name="Keep",
                                region_manual="US", pinned_services=["AI服务"])
    before = await node_snapshot(db, subscription.id)
    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        AsyncMock(side_effect=exc))
    result = await sync_subscription(db, subscription.id, now=UTC_NOW)

    assert result.success is False
    assert result.error_code == expected_code
    assert (result.inserted, result.updated, result.removed) == (0, 0, 0)
    assert await node_snapshot(db, subscription.id) == before
    # 失败状态准确：attempt/next 更新，success/count 不动
    attempt, success, nxt, err, count = await sub_state(db, subscription.id)
    assert attempt == UTC_NOW_NAIVE
    assert success is None
    assert nxt == UTC_NOW_NAIVE + timedelta(seconds=900)
    assert err == expected_code
    assert count is None


@pytest.mark.asyncio
async def test_sync_node_identity_error_maps_invalid_content(db, subscription, monkeypatch):
    from unittest.mock import AsyncMock
    bad = {"name": "x", "node_type": "ss", "server": "h", "port": 1,
           "config": "y = ss, h, 1"}  # name 与 config 行首不匹配
    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        AsyncMock(return_value=[bad]))
    result = await sync_subscription(db, subscription.id, now=UTC_NOW)
    assert result.success is False
    assert result.error_code == "invalid_content"


def test_safe_sync_error_timeout_beats_request_error():
    """Timeout 是 RequestError 子类：必须归 timeout 而非 network_error"""
    assert isinstance(timeout_exc(), httpx.RequestError)
    code, message = safe_sync_error(timeout_exc())
    assert code == "timeout"
    assert message == "订阅拉取超时，已保留上次可用节点"


def test_safe_sync_error_messages_are_static():
    """公开消息固定文本集合，不含异常原文/URL/主机/配置/状态码"""
    allowed = {
        "订阅拉取超时，已保留上次可用节点",
        "订阅源响应异常，已保留上次可用节点",
        "订阅内容无法解析，已保留上次可用节点",
        "订阅源网络错误，已保留上次可用节点",
        "同步发生内部错误，已保留上次可用节点",
    }
    cases = [timeout_exc(), status_exc(500), httpx.ConnectError("x"),
             ValueError("不允许的地址: 内网地址"), RuntimeError("secret")]
    for exc in cases:
        code, message = safe_sync_error(exc)
        assert message in allowed
        assert "example.com" not in message and "secret" not in message


@pytest.mark.asyncio
async def test_sync_failed_result_keeps_last_node_count(db, subscription, monkeypatch):
    from unittest.mock import AsyncMock
    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        AsyncMock(return_value=[node_payload("A"), other_payload("B")]))
    ok = await sync_subscription(db, subscription.id, now=UTC_NOW)
    assert ok.success is True and ok.node_count == 2

    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        AsyncMock(side_effect=timeout_exc()))
    failed = await sync_subscription(db, subscription.id,
                                     now=UTC_NOW + timedelta(hours=1))
    assert failed.success is False
    assert failed.node_count == 2  # 保持上次成功值
    _, _, _, _, count = await sub_state(db, subscription.id)
    assert count == 2
    _, success, _, _, _ = await sub_state(db, subscription.id)
    assert success == UTC_NOW_NAIVE  # 成功时间不被失败覆盖


@pytest_asyncio.fixture
async def file_db(tmp_path):
    """文件库 + 主会话与会话工厂：race/原子回滚测试用全新会话验证真实落库状态"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/race.sqlite3")

    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "connect")
    def _busy_timeout(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s1:
        yield s1, factory
    await engine.dispose()


async def make_subscription(session, **kw):
    sub = Subscription(name=kw.get("name", "S"), url=kw.get("url", URL),
                       enabled=kw.get("enabled", True),
                       auto_update=kw.get("auto_update", True),
                       update_interval=kw.get("update_interval", 3600))
    session.add(sub)
    await session.commit()
    await session.refresh(sub)
    return sub


@pytest.mark.asyncio
async def test_sync_write_failure_rolls_back_atomically(file_db, monkeypatch):
    """写事务 DML 三类齐备（matched 更新 + INSERT + DELETE）真实 flush 后
    commit 故障：rollback 原子撤销；全新会话验证 Fresh 不存在、Drop 仍
    存在且原字段、Keep provider/人工状态回滚；失败状态由独立短事务提交；
    同会话可继续下一次同步。"""
    from unittest.mock import AsyncMock
    s1, factory = file_db
    sub = await make_subscription(s1)
    sub_id = sub.id  # rollback/过期后不再访问 s1 实例属性
    await add_subscription_node(
        s1, sub, name="Keep", region_manual="US", enabled=False,
        pinned_services=["AI服务"])
    await add_subscription_node(s1, sub, name="Drop",
                                config="Drop = trojan, 9.9.9.9, 443, password=y")
    async with factory() as s2:
        before_nodes = await node_snapshot(s2, sub_id)
    assert [n[1] for n in before_nodes] == ["Keep", "Drop"]

    # 守卫（防测试退化）：incoming 恰好构成 matched 更新（Renamed ≡ Keep）
    # + 全新 INSERT（Fresh，不匹配任何既有 key）；既有 Drop 不在 incoming
    # （触发 DELETE）；三个连接身份两两不同。
    keep_payload = node_payload("Renamed")
    fresh = fresh_payload("Fresh")
    drop_like = {"name": "Drop", "config": "Drop = trojan, 9.9.9.9, 443, password=y"}
    keep_key, fresh_key, drop_key = (stable_node_key(p)
                                     for p in (keep_payload, fresh, drop_like))
    assert len({keep_key, fresh_key, drop_key}) == 3
    assert keep_key == before_nodes[0][10]  # Renamed 与既有 Keep 同身份
    assert drop_key == before_nodes[1][10]   # Drop 与既有 Drop 同身份
    assert fresh_key not in {before_nodes[0][10], before_nodes[1][10]}

    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        AsyncMock(return_value=[keep_payload, fresh]))

    calls = {"n": 0}
    flushed = {}
    real_commit = s1.commit

    async def flaky_commit():
        calls["n"] += 1
        if calls["n"] == 2:
            # 写事务 commit：先把三类 DML 真实送入事务再故障（非 mock echo）；
            # 同事务内查询确认 UPDATE/INSERT/DELETE 均已生效（防退化守卫）
            await s1.flush()
            rows = (await s1.execute(
                select(ProxyNode).where(ProxyNode.subscription_id == sub_id)
                .order_by(ProxyNode.id))).scalars().all()
            flushed["names"] = [n.name for n in rows]
            raise RuntimeError("disk i/o boom")
        await real_commit()

    monkeypatch.setattr(s1, "commit", flaky_commit)
    result = await sync_subscription(s1, sub_id, now=UTC_NOW)
    assert result.success is False
    assert result.error_code == "internal_error"
    assert "boom" not in result.public_message
    assert calls["n"] >= 3  # 失败状态短事务已真实提交
    # 守卫：flush 时事务内已见三种 DML（Keep→Renamed 更新、Fresh 插入、Drop 删除）
    assert flushed["names"] == ["Renamed", "Fresh"]
    # 全新会话验证：事务内 DML 全部回滚——Fresh 不存在、Drop 仍存在且
    # 原字段、Keep provider/人工状态原样
    async with factory() as s2:
        assert await node_snapshot(s2, sub_id) == before_nodes
        attempt, success, nxt, err, count = await sub_state(s2, sub_id)
        assert attempt == UTC_NOW_NAIVE
        assert success is None
        assert count is None
        assert err == "internal_error"
        assert nxt == UTC_NOW_NAIVE + timedelta(seconds=900)
    # 同会话可继续：下一次同步成功（commit 替身已恢复正常），
    # 且三类 DML 真实落库：1 更新 + 1 插入 + 1 删除
    ok = await sync_subscription(s1, sub_id, now=UTC_NOW + timedelta(hours=1))
    assert ok.success is True
    assert (ok.inserted, ok.updated, ok.removed) == (1, 1, 1)
    async with factory() as s2:
        after = await node_snapshot(s2, sub_id)
    assert [n[1] for n in after] == ["Renamed", "Fresh"]


# ===== 调度语义 =====

@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,auto,interval,expected_next", [
    (True, True, 3600, UTC_NOW_NAIVE + timedelta(seconds=3600)),
    (True, False, 3600, None),
    (False, True, 3600, None),
    (True, True, 100, UTC_NOW_NAIVE + timedelta(seconds=86400)),  # 范围外归一
])
async def test_sync_success_schedule(db, monkeypatch, enabled, auto,
                                     interval, expected_next):
    from unittest.mock import AsyncMock
    sub = Subscription(name="S", url=URL, enabled=enabled, auto_update=auto,
                       update_interval=interval)
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        AsyncMock(return_value=[node_payload()]))
    result = await sync_subscription(db, sub.id, now=UTC_NOW)
    assert result.success is True
    _, _, nxt, err, _ = await sub_state(db, sub.id)
    assert nxt == expected_next
    assert err is None


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,auto,interval,expected_next", [
    (True, True, 3600, UTC_NOW_NAIVE + timedelta(seconds=900)),
    (True, True, 100000, UTC_NOW_NAIVE + timedelta(seconds=900)),  # min(86400,900)
    (True, False, 3600, None),
    (False, True, 3600, None),
])
async def test_sync_failure_schedule(db, monkeypatch, enabled, auto,
                                     interval, expected_next):
    from unittest.mock import AsyncMock
    sub = Subscription(name="S", url=URL, enabled=enabled, auto_update=auto,
                       update_interval=interval)
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        AsyncMock(side_effect=timeout_exc()))
    result = await sync_subscription(db, sub.id, now=UTC_NOW)
    assert result.success is False
    _, _, nxt, err, _ = await sub_state(db, sub.id)
    assert nxt == expected_next
    assert err == "timeout"


# ===== 去重与计数 =====

@pytest.mark.asyncio
async def test_sync_dedupes_same_connection_counts_once(db, subscription, monkeypatch):
    from unittest.mock import AsyncMock
    # 两条同连接身份（仅显示名不同）：保留首个，计数基于去重后集合
    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        AsyncMock(return_value=[node_payload("First"), node_payload("Second")]))
    result = await sync_subscription(db, subscription.id, now=UTC_NOW)
    assert result.success is True
    assert (result.inserted, result.updated, result.removed) == (1, 0, 0)
    assert result.node_count == 1
    nodes = (await db.execute(
        select(ProxyNode).where(ProxyNode.subscription_id == subscription.id)
    )).scalars().all()
    assert len(nodes) == 1
    assert nodes[0].name == "First"
    _, _, _, _, count = await sub_state(db, subscription.id)
    assert count == 1


# ===== now 规范 =====

@pytest.mark.asyncio
async def test_sync_now_normalization(db, subscription, monkeypatch):
    from unittest.mock import AsyncMock
    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        AsyncMock(return_value=[node_payload()]))
    # aware UTC → 库内 naive UTC
    result = await sync_subscription(db, subscription.id, now=UTC_NOW)
    assert result.success is True
    attempt, success, _, _, _ = await sub_state(db, subscription.id)
    assert attempt == UTC_NOW_NAIVE and success == UTC_NOW_NAIVE

    # naive 视为 UTC，原样写库
    naive = datetime(2026, 10, 18, 8, 30, 0)
    result = await sync_subscription(db, subscription.id, now=naive)
    assert result.success is True
    attempt, _, _, _, _ = await sub_state(db, subscription.id)
    assert attempt == naive

    # aware 非 UTC：拒绝（不进入同步流程）
    with pytest.raises(ValueError):
        await sync_subscription(db, subscription.id,
                                now=UTC_NOW.astimezone(timezone(timedelta(hours=8))))


# ===== 订阅不存在 / 并发删除 =====

@pytest.mark.asyncio
async def test_sync_missing_subscription_returns_none(db):
    assert await sync_subscription(db, 9999, now=UTC_NOW) is None


@pytest.mark.asyncio
async def test_sync_concurrent_delete_during_fetch_returns_none(db, subscription, monkeypatch):
    """拉取期间订阅被删除：写事务重查为空 → None，不泄漏、不写状态"""
    async def fetch_and_delete(url):
        await db.execute(
            select(Subscription).where(Subscription.id == subscription.id))
        from sqlalchemy import delete as sa_delete
        await db.execute(sa_delete(ProxyNode).where(
            ProxyNode.subscription_id == subscription.id))
        await db.execute(sa_delete(Subscription).where(
            Subscription.id == subscription.id))
        await db.commit()
        return [node_payload()]

    monkeypatch.setattr(subscription_service, "fetch_subscription", fetch_and_delete)
    assert await sync_subscription(db, subscription.id, now=UTC_NOW) is None


# ===== 路由：单项 200 / 502 / 404 =====

@pytest.mark.asyncio
async def test_sync_one_success_returns_safe_dto(db, subscription, monkeypatch):
    from unittest.mock import AsyncMock
    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        AsyncMock(return_value=[node_payload()]))
    body = await sync_one(subscription.id, db)
    assert set(body) == SAFE_KEYS
    assert body["success"] is True
    assert body["error_code"] is None
    assert body["node_count"] == 1


@pytest.mark.asyncio
async def test_sync_one_failure_502_sanitized(db, subscription, monkeypatch):
    from unittest.mock import AsyncMock
    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        AsyncMock(side_effect=timeout_exc()))
    resp = await sync_one(subscription.id, db)
    assert resp.status_code == 502
    body = json.loads(resp.body)
    assert set(body) == SAFE_KEYS
    assert body["error_code"] == "timeout"
    dumped = json.dumps(body, ensure_ascii=False)
    assert "sub.example.com" not in dumped
    assert "synthetic" not in dumped
    assert "detail" not in body


@pytest.mark.asyncio
async def test_sync_one_missing_subscription_404_static(db):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        await sync_one(9999, db)
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "订阅不存在"


@pytest.mark.asyncio
async def test_sync_one_manual_sync_ignores_enabled_and_auto(db, monkeypatch):
    """手动单项同步不受 enabled/auto 限制（禁用订阅也可手动同步）"""
    from unittest.mock import AsyncMock
    sub = Subscription(name="S", url=URL, enabled=False, auto_update=False)
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        AsyncMock(return_value=[node_payload()]))
    body = await sync_one(sub.id, db)
    assert body["success"] is True


# ===== sync-all =====

@pytest.mark.asyncio
async def test_sync_all_mixed_results_continue(db, monkeypatch):
    sub_a = Subscription(name="A", url=URL, enabled=True)
    sub_b = Subscription(name="B", url="https://other.example.com/feed",
                         enabled=True)
    sub_off = Subscription(name="off", url="https://off.example.com/f",
                           enabled=False)
    db.add_all([sub_a, sub_b, sub_off])
    await db.commit()
    for s in (sub_a, sub_b, sub_off):
        await db.refresh(s)

    async def fetch_by_url(url):
        if url == URL:
            return [node_payload("A1")]
        raise timeout_exc()

    monkeypatch.setattr(subscription_service, "fetch_subscription", fetch_by_url)
    results = await sync_all_subscriptions(db)
    # 仅启用的两个订阅；禁用项不在结果中
    assert sorted(r.subscription_id for r in results) == sorted([sub_a.id, sub_b.id])
    by_id = {r.subscription_id: r for r in results}
    assert by_id[sub_a.id].success is True
    assert by_id[sub_a.id].node_count == 1
    assert by_id[sub_b.id].success is False
    assert by_id[sub_b.id].error_code == "timeout"
    # 单项失败不中断：A 的节点已落库
    nodes = (await db.execute(
        select(ProxyNode).where(ProxyNode.subscription_id == sub_a.id)
    )).scalars().all()
    assert len(nodes) == 1

    # 路由层逐项安全 DTO
    body = await sync_all(db)
    assert set(body) == {"results"}
    assert all(set(r) == SAFE_KEYS for r in body["results"])
    dumped = json.dumps(body, ensure_ascii=False)
    assert "sub.example.com" not in dumped and "synthetic" not in dumped


# ===== 日志脱敏 =====

@pytest.mark.asyncio
async def test_sync_failure_logs_no_exception_text(db, subscription, monkeypatch, caplog):
    from unittest.mock import AsyncMock
    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        AsyncMock(side_effect=RuntimeError(
                            "secret boom https://sub.example.com/feed password=x")))
    with caplog.at_level(logging.ERROR, logger="app.services.subscription"):
        result = await sync_subscription(db, subscription.id, now=UTC_NOW)
    assert result.error_code == "internal_error"
    assert "sub.example.com" not in caplog.text
    assert "boom" not in caplog.text
    assert "password=x" not in caplog.text
    assert "internal_error" in caplog.text  # 只记录错误码


# ===== DTO 序列化 =====

def test_sync_result_to_dict_exact_fields():
    result = SyncResult(subscription_id=1, success=False, node_count=3,
                        inserted=0, updated=0, removed=0,
                        error_code="timeout", public_message="订阅拉取超时，已保留上次可用节点")
    assert result.to_dict() == {
        "subscription_id": 1,
        "success": False,
        "node_count": 3,
        "inserted": 0,
        "updated": 0,
        "removed": 0,
        "error_code": "timeout",
        "public_message": "订阅拉取超时，已保留上次可用节点",
    }


# ===== Fix Round 1：快照事务边界 / 并发删除 / URL 竞态 / 取消传播 =====

@pytest.mark.asyncio
async def test_sync_snapshot_commit_failure_recovers_same_session(db, subscription, monkeypatch):
    """阶段 1 只读 commit 故障：安全失败 DTO + 失败状态；同会话可继续同步"""
    from unittest.mock import AsyncMock
    await add_subscription_node(db, subscription, name="Keep")
    calls = {"n": 0}
    real_commit = db.commit

    async def flaky_commit():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("snapshot boom /etc/secret")
        await real_commit()

    monkeypatch.setattr(db, "commit", flaky_commit)
    result = await sync_subscription(db, subscription.id, now=UTC_NOW)
    assert result.success is False
    assert result.error_code == "internal_error"
    assert "boom" not in result.public_message
    assert "/etc/secret" not in result.public_message
    # 失败状态短事务已提交（第 2 次 commit 恢复正常）
    _, _, nxt, err, _ = await sub_state(db, subscription.id)
    assert err == "internal_error"
    assert nxt == UTC_NOW_NAIVE + timedelta(seconds=900)
    # 同一 session 继续下一次同步成功
    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        AsyncMock(return_value=[node_payload("Keep")]))
    ok = await sync_subscription(db, subscription.id,
                                 now=UTC_NOW + timedelta(hours=1))
    assert ok.success is True


@pytest.mark.asyncio
async def test_sync_failure_concurrent_delete_returns_none(file_db, monkeypatch):
    """拉取失败期间订阅被并发删除：失败状态事务发现不存在 → 返回 None"""
    from sqlalchemy import delete as sa_delete
    s1, factory = file_db
    sub = await make_subscription(s1)
    sub_id = sub.id  # rollback/过期后不再访问 s1 实例属性
    await add_subscription_node(s1, sub, name="Keep")

    async def fetch_fail_after_delete(url):
        async with factory() as s2:
            await s2.execute(sa_delete(ProxyNode).where(
                ProxyNode.subscription_id == sub_id))
            await s2.execute(sa_delete(Subscription).where(
                Subscription.id == sub_id))
            await s2.commit()
        raise timeout_exc()

    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        fetch_fail_after_delete)
    assert await sync_subscription(s1, sub_id, now=UTC_NOW) is None


@pytest.mark.asyncio
async def test_sync_failure_count_uses_latest_from_record_txn(file_db, monkeypatch):
    """失败 DTO node_count 用失败状态事务读到的最新值（并发成功已更新）"""
    s1, factory = file_db
    sub = await make_subscription(s1)
    sub_id = sub.id  # rollback/过期后不再访问 s1 实例属性

    async def fetch_fail_after_concurrent_success(url):
        async with factory() as s2:
            s2_sub = await s2.get(Subscription, sub_id)
            s2_sub.last_node_count = 5
            s2_sub.last_success_at = UTC_NOW_NAIVE
            await s2.commit()
        raise timeout_exc()

    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        fetch_fail_after_concurrent_success)
    result = await sync_subscription(s1, sub_id, now=UTC_NOW)
    assert result.success is False
    assert result.error_code == "timeout"
    assert result.node_count == 5
    async with factory() as s2:
        s2_sub = await s2.get(Subscription, sub_id)
        assert s2_sub.last_node_count == 5  # 失败路径不改写成功状态


@pytest.mark.asyncio
async def test_sync_stale_url_snapshot_rejects_write(file_db, monkeypatch, caplog):
    """fetch 间隙 URL 被并发修改：写事务拒绝旧结果，安全失败且不泄漏 URL"""
    s1, factory = file_db
    sub = await make_subscription(s1)
    sub_id = sub.id  # rollback/过期后不再访问 s1 实例属性
    await add_subscription_node(s1, sub, name="Keep", region_manual="US")
    async with factory() as s2:
        before_nodes = await node_snapshot(s2, sub_id)

    new_url = "https://changed.example.com/v2"

    async def fetch_with_url_race(url):
        async with factory() as s2:
            s2_sub = await s2.get(Subscription, sub_id)
            s2_sub.url = new_url
            await s2.commit()
        return [node_payload("Stale Rename")]

    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        fetch_with_url_race)
    with caplog.at_level(logging.ERROR, logger="app.services.subscription"):
        result = await sync_subscription(s1, sub_id, now=UTC_NOW)
    assert result.success is False
    assert result.error_code == "internal_error"
    assert new_url not in result.public_message
    assert new_url not in caplog.text and URL not in caplog.text
    # 旧拉取结果绝不落库：节点与成功状态原样，失败状态按 retry 语义记录
    async with factory() as s2:
        assert await node_snapshot(s2, sub_id) == before_nodes
        attempt, success, nxt, err, _ = await sub_state(s2, sub_id)
        assert attempt == UTC_NOW_NAIVE
        assert success is None
        assert err == "internal_error"
        assert nxt == UTC_NOW_NAIVE + timedelta(seconds=900)


@pytest.mark.asyncio
async def test_sync_cancelled_error_propagates_from_fetch(db, subscription, monkeypatch):
    """取消异常不被吞、不记失败状态（CancelledError 非 Exception 子类）"""
    from unittest.mock import AsyncMock
    await add_subscription_node(db, subscription, name="Keep")
    before = await node_snapshot(db, subscription.id)
    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await sync_subscription(db, subscription.id, now=UTC_NOW)
    assert await node_snapshot(db, subscription.id) == before
    attempt, success, _, err, _ = await sub_state(db, subscription.id)
    assert attempt is None and success is None and err is None


@pytest.mark.asyncio
async def test_sync_cancelled_error_propagates_from_write(file_db, monkeypatch):
    """写事务中被取消：先尽力回滚再传播；全新会话验证无 DML 残留、无失败状态"""
    from unittest.mock import AsyncMock
    s1, factory = file_db
    sub = await make_subscription(s1)
    sub_id = sub.id  # rollback/过期后不再访问 s1 实例属性
    await add_subscription_node(s1, sub, name="Keep", region_manual="US")
    async with factory() as s2:
        before_nodes = await node_snapshot(s2, sub_id)

    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        AsyncMock(return_value=[node_payload("Renamed")]))
    calls = {"n": 0}
    real_commit = s1.commit

    async def cancel_on_write_commit():
        calls["n"] += 1
        if calls["n"] == 2:
            await s1.flush()  # 真实 DML 入事务后取消
            raise asyncio.CancelledError()
        await real_commit()

    monkeypatch.setattr(s1, "commit", cancel_on_write_commit)
    with pytest.raises(asyncio.CancelledError):
        await sync_subscription(s1, sub_id, now=UTC_NOW)
    async with factory() as s2:
        assert await node_snapshot(s2, sub_id) == before_nodes
        attempt, success, nxt, err, _ = await sub_state(s2, sub_id)
        assert attempt is None and success is None
        assert err is None and nxt is None
