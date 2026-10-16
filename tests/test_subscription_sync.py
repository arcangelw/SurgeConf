"""订阅同步事务化 upsert 与失败保留测试。

覆盖：rename 保 ID 与人工状态、等价 key 更新 provider 字段、新节点
默认值、失败/空结果保留最后可用节点、五类错误码分类与状态、调度
语义、去重计数、写事务原子回滚、now 规范、路由 404/502 脱敏、
sync-all 混合结果、日志无异常文字。
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

import app.services.subscription as subscription_service
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


@pytest.mark.asyncio
async def test_sync_write_failure_rolls_back_atomically(db, subscription, monkeypatch):
    from unittest.mock import AsyncMock
    await add_subscription_node(db, subscription, name="Keep",
                                region_manual="US", pinned_services=["AI服务"])
    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        AsyncMock(return_value=[node_payload("Renamed")]))
    ok = await sync_subscription(db, subscription.id, now=UTC_NOW)
    assert ok.success is True
    before = await node_snapshot(db, subscription.id)

    # 第 1 次 commit 是阶段 1 只读快照事务（正常）；第 2 次是写事务
    # upsert commit（抛内部错误）；第 3 次起失败状态短事务正常提交
    calls = {"n": 0}
    real_commit = db.commit

    async def flaky_commit():
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk i/o boom")
        await real_commit()

    monkeypatch.setattr(db, "commit", flaky_commit)
    result = await sync_subscription(db, subscription.id,
                                     now=UTC_NOW + timedelta(hours=1))
    assert result.success is False
    assert result.error_code == "internal_error"
    assert "boom" not in result.public_message
    assert calls["n"] >= 2  # 失败状态事务已提交
    assert await node_snapshot(db, subscription.id) == before
    _, success, _, err, count = await sub_state(db, subscription.id)
    assert success == UTC_NOW_NAIVE
    assert err == "internal_error"
    assert count == 1


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
