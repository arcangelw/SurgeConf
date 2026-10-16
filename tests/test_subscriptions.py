"""订阅管理测试 - 删除级联 & 孤儿节点清理（升级数据修复）"""

import pytest
from sqlalchemy import select

from app.models import Subscription, ProxyNode
from app.routers.subscriptions import delete_subscription
from app.services.node_identity import stable_node_key
from app.services.subscription import cleanup_orphan_nodes


def _subscribed(config: str, **overrides) -> dict:
    """订阅节点公共字段：stable_key 由连接配置计算（CHECK 要求非 NULL）"""
    fields = {"config": config, "stable_key": stable_node_key({"config": config})}
    fields.update(overrides)
    return fields


@pytest.mark.asyncio
async def test_delete_subscription_cascades_nodes(db):
    """删除订阅时应级联删除其下所有节点，其他订阅的节点保留"""
    sub = Subscription(name="测试订阅A", url="https://example.com/a", enabled=True)
    db.add(sub)
    await db.commit()
    await db.refresh(sub)

    db.add(ProxyNode(name="A1", node_type="ss", server="1.1.1.1", port=1,
                     subscription_id=sub.id, enabled=True,
                     **_subscribed("A1 = ss, 1.1.1.1, 1")))
    db.add(ProxyNode(name="A2", node_type="ss", server="1.1.1.2", port=1,
                     subscription_id=sub.id, enabled=True,
                     **_subscribed("A2 = ss, 1.1.1.2, 1")))
    # 另一个订阅的节点，不应被删除
    db.add(ProxyNode(name="B1", node_type="ss", server="2.2.2.1", port=1,
                     subscription_id=sub.id + 1000, enabled=True,
                     **_subscribed("B1 = ss, 2.2.2.1, 1")))
    await db.commit()

    res = await delete_subscription(sub.id, db)
    assert res == {"deleted": sub.id}

    # 订阅已删除
    assert (await db.execute(
        select(Subscription).where(Subscription.id == sub.id)
    )).scalar_one_or_none() is None

    # A 订阅下的节点已被级联删除
    remaining = (await db.execute(
        select(ProxyNode).where(ProxyNode.subscription_id == sub.id)
    )).scalars().all()
    assert remaining == []

    # 其他订阅的节点保留
    assert (await db.execute(
        select(ProxyNode).where(ProxyNode.name == "B1")
    )).scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_cleanup_orphan_nodes_removes_residuals(db):
    """升级清理：删除归属订阅已不存在的残留节点，保留手动节点（subscription_id=NULL）"""
    sub = Subscription(name="真实订阅", url="https://example.com/r", enabled=True)
    db.add(sub)
    await db.commit()
    await db.refresh(sub)

    # 归属真实订阅的有效节点，保留
    db.add(ProxyNode(name="real", node_type="ss", server="1.1.1.1", port=1,
                     subscription_id=sub.id, enabled=True,
                     **_subscribed("real = ss, 1.1.1.1, 1")))
    # 孤儿节点（订阅已被删除的残留）
    db.add(ProxyNode(name="orphan1", node_type="ss", server="9.9.9.1", port=1,
                     subscription_id=999, enabled=True,
                     **_subscribed("orphan1 = ss, 9.9.9.1, 1")))
    db.add(ProxyNode(name="orphan2", node_type="ss", server="9.9.9.2", port=1,
                     subscription_id=998, enabled=True,
                     **_subscribed("orphan2 = ss, 9.9.9.2, 1")))
    # 手动节点（无订阅，stable_key 允许 NULL），不应被清理
    db.add(ProxyNode(name="manual", node_type="ss", server="8.8.8.8", port=1,
                     config="manual = ss, 8.8.8.8, 1", subscription_id=None, enabled=True))
    await db.commit()

    removed = await cleanup_orphan_nodes(db)
    assert removed == 2

    names = {n.name for n in (await db.execute(select(ProxyNode))).scalars().all()}
    assert names == {"real", "manual"}


@pytest.mark.asyncio
async def test_cleanup_orphan_nodes_idempotent(db):
    """清理操作幂等：没有孤儿时不删任何节点，重复执行结果一致"""
    sub = Subscription(name="订阅X", url="https://example.com/x", enabled=True)
    db.add(sub)
    await db.commit()
    await db.refresh(sub)

    db.add(ProxyNode(name="x1", node_type="ss", server="1.1.1.1", port=1,
                     subscription_id=sub.id, enabled=True,
                     **_subscribed("x1 = ss, 1.1.1.1, 1")))
    await db.commit()

    assert await cleanup_orphan_nodes(db) == 0
    assert await cleanup_orphan_nodes(db) == 0
    assert (await db.execute(select(ProxyNode))).scalars().all().__len__() == 1
