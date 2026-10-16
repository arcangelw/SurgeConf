"""订阅自动更新调度器测试。

覆盖：到期选择矩阵与排序边界、逐项独立会话同步与失败隔离、选择
查询事务不跨网络拉取、时钟 UTC 规范、flock 进程互斥与释放接管、
锁文件权限/普通文件/symlink 拒绝、start/stop 幂等、循环立即执行
与可注入短间隔、轮次异常继续、取消传播、无 pending task。
"""

import asyncio
import logging
import os
import stat as stat_mod
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete as sa_delete, select as sa_select, update as sa_update

import app.services.subscription as subscription_service
from app.database import Base, DatabaseRuntime
from app.models import Subscription
from app.services.subscription_scheduler import (LOOP_INTERVAL_SECONDS,
                                                 SubscriptionScheduler)

UTC_NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
UTC_NOW_NAIVE = UTC_NOW.replace(tzinfo=None)
URL = "https://sub.example.com/feed"

NODE_PAYLOAD = {"name": "Node", "node_type": "trojan", "server": "1.1.1.1",
                "port": 443, "config": "Node = trojan, 1.1.1.1, 443, password=x"}


def make_scheduler(database, lock_path, **kw):
    return SubscriptionScheduler(database, lock_path, **kw)


async def add_subscription(runtime, name="S", url=URL, enabled=True,
                           auto_update=True, update_interval=3600,
                           next_update_at=None):
    async with runtime.session_factory() as db:
        sub = Subscription(name=name, url=url, enabled=enabled,
                           auto_update=auto_update,
                           update_interval=update_interval,
                           next_update_at=next_update_at)
        db.add(sub)
        await db.commit()
        await db.refresh(sub)
        return sub.id


class _TrackingSessionFactory:
    """包装 session 工厂：记录并发打开的会话数（证明选择查询会话
    在进入网络拉取前已关闭）"""

    def __init__(self, inner):
        self._inner = inner
        self.open_sessions = 0
        self.fetch_time_open = None

    def __call__(self):
        return _TrackingSessionContext(self._inner(), self)


class _TrackingSessionContext:
    def __init__(self, cm, factory):
        self._cm = cm
        self._factory = factory

    async def __aenter__(self):
        session = await self._cm.__aenter__()
        self._factory.open_sessions += 1
        return session

    async def __aexit__(self, exc_type, exc, tb):
        try:
            return await self._cm.__aexit__(exc_type, exc, tb)
        finally:
            self._factory.open_sessions -= 1


class _TrackingDatabase:
    """duck-typed DatabaseRuntime：会话工厂带计数"""

    def __init__(self, inner):
        self._inner = inner
        self.tracking = _TrackingSessionFactory(inner.session_factory)

    @property
    def session_factory(self):
        return self.tracking


@pytest_asyncio.fixture
async def sched_db(tmp_path):
    runtime = DatabaseRuntime.create(tmp_path / "sched.sqlite3")
    async with runtime.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield runtime
    finally:
        await runtime.dispose()


# ===== 到期选择矩阵 / 边界 / 排序 =====

@pytest.mark.asyncio
async def test_due_selection_only_auto_enabled_due(sched_db, tmp_path, monkeypatch):
    """仅 enabled+auto+next<=now 参与；边界 next==now 选中、next=now+1 不选"""
    due_past = await add_subscription(sched_db, name="past", url=f"{URL}?p",
                                      next_update_at=UTC_NOW_NAIVE - timedelta(seconds=1))
    due_now = await add_subscription(sched_db, name="now", url=f"{URL}?n",
                                     next_update_at=UTC_NOW_NAIVE)
    await add_subscription(sched_db, name="future", url=f"{URL}?f",
                           next_update_at=UTC_NOW_NAIVE + timedelta(seconds=1))
    await add_subscription(sched_db, name="null", url=f"{URL}?z",
                           next_update_at=None)
    await add_subscription(sched_db, name="disabled", url=f"{URL}?d",
                           enabled=False,
                           next_update_at=UTC_NOW_NAIVE - timedelta(seconds=1))
    await add_subscription(sched_db, name="auto-off", url=f"{URL}?a",
                           auto_update=False,
                           next_update_at=UTC_NOW_NAIVE - timedelta(seconds=1))

    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        _fake_fetch_ok())
    scheduler = make_scheduler(sched_db, tmp_path / "l.lock",
                               clock=lambda: UTC_NOW)
    results = await scheduler.run_due_once()
    assert [r.subscription_id for r in results] == [due_past, due_now]
    assert all(r.success for r in results)


@pytest.mark.asyncio
async def test_due_selection_orders_by_next_then_id(sched_db, tmp_path, monkeypatch):
    """排序：next 升序，next 相同按 id 升序（fetch 调用顺序即证）"""
    ids = {}
    ids["late"] = await add_subscription(sched_db, name="late", url=f"{URL}?late",
                                         next_update_at=UTC_NOW_NAIVE + timedelta(hours=1))
    ids["early"] = await add_subscription(sched_db, name="early", url=f"{URL}?early",
                                          next_update_at=UTC_NOW_NAIVE)
    ids["mid"] = await add_subscription(sched_db, name="mid", url=f"{URL}?mid",
                                        next_update_at=UTC_NOW_NAIVE + timedelta(minutes=30))
    ids["t2"] = await add_subscription(sched_db, name="tie2", url=f"{URL}?tie2",
                                       next_update_at=UTC_NOW_NAIVE + timedelta(minutes=30))
    order = [ids["early"], ids["mid"], ids["t2"], ids["late"]]
    calls = []

    async def fake_fetch(url):
        calls.append(url)
        return [NODE_PAYLOAD]

    monkeypatch.setattr(subscription_service, "fetch_subscription", fake_fetch)
    scheduler = make_scheduler(sched_db, tmp_path / "l.lock",
                               clock=lambda: UTC_NOW + timedelta(hours=2))
    results = await scheduler.run_due_once()
    assert [r.subscription_id for r in results] == order
    assert calls == [f"{URL}?early", f"{URL}?mid", f"{URL}?tie2", f"{URL}?late"]


@pytest.mark.asyncio
async def test_due_sync_failure_does_not_block_rest(sched_db, tmp_path, monkeypatch):
    """第一项拉取超时：返回安全失败结果，后续项继续同步"""
    good = await add_subscription(sched_db, name="bad", url=f"{URL}?bad",
                                  next_update_at=UTC_NOW_NAIVE - timedelta(seconds=2))
    other = await add_subscription(sched_db, name="good", url=f"{URL}?good",
                                   next_update_at=UTC_NOW_NAIVE - timedelta(seconds=1))

    async def fake_fetch(url):
        if url.endswith("?bad"):
            raise httpx.TimeoutException(f"synthetic timeout {URL}?bad")
        return [NODE_PAYLOAD]

    monkeypatch.setattr(subscription_service, "fetch_subscription", fake_fetch)
    scheduler = make_scheduler(sched_db, tmp_path / "l.lock",
                               clock=lambda: UTC_NOW)
    results = await scheduler.run_due_once()
    assert [(r.subscription_id, r.success) for r in results] == [
        (good, False), (other, True)]
    assert results[0].error_code == "timeout"


@pytest.mark.asyncio
async def test_due_skips_concurrently_deleted(sched_db, tmp_path, monkeypatch):
    """拉取期间订阅被并发删除：sync 返回 None，跳过不出现在结果中"""
    sub_id = await add_subscription(sched_db, next_update_at=UTC_NOW_NAIVE)

    async def deleting_fetch(url):
        async with sched_db.session_factory() as db:
            await db.execute(sa_delete(Subscription).where(Subscription.id == sub_id))
            await db.commit()
        return [NODE_PAYLOAD]

    monkeypatch.setattr(subscription_service, "fetch_subscription", deleting_fetch)
    scheduler = make_scheduler(sched_db, tmp_path / "l.lock",
                               clock=lambda: UTC_NOW)
    assert await scheduler.run_due_once() == []


@pytest.mark.asyncio
async def test_due_query_session_closed_before_fetch(sched_db, tmp_path, monkeypatch):
    """选择查询的会话在进入网络拉取前已关闭（不跨 fetch 持事务）"""
    await add_subscription(sched_db, next_update_at=UTC_NOW_NAIVE)
    tracking_db = _TrackingDatabase(sched_db)

    async def probing_fetch(url):
        tracking_db.tracking.fetch_time_open = tracking_db.tracking.open_sessions
        return [NODE_PAYLOAD]

    monkeypatch.setattr(subscription_service, "fetch_subscription", probing_fetch)
    scheduler = make_scheduler(tracking_db, tmp_path / "l.lock",
                               clock=lambda: UTC_NOW)
    results = await scheduler.run_due_once()
    assert len(results) == 1
    # fetch 期间只开着当前同步会话（选择查询会话已退出）
    assert tracking_db.tracking.fetch_time_open == 1
    assert tracking_db.tracking.open_sessions == 0  # 全部会话已归还


# ===== 时钟规范 =====

@pytest.mark.asyncio
async def test_clock_naive_utc_accepted(sched_db, tmp_path, monkeypatch):
    """naive UTC 时钟按库内口径接受（同一到期集合）"""
    sub_id = await add_subscription(sched_db, next_update_at=UTC_NOW_NAIVE)
    await add_subscription(sched_db, name="later", url=f"{URL}?later",
                           next_update_at=UTC_NOW_NAIVE + timedelta(seconds=1))
    monkeypatch.setattr(subscription_service, "fetch_subscription", _fake_fetch_ok())
    scheduler = make_scheduler(sched_db, tmp_path / "l.lock",
                               clock=lambda: UTC_NOW_NAIVE)
    assert [r.subscription_id for r in await scheduler.run_due_once()] == [sub_id]


@pytest.mark.asyncio
async def test_clock_non_utc_timezone_rejected(sched_db, tmp_path):
    """非 UTC 时区时钟在进入调度前拒绝（禁止本地时区模糊）"""
    scheduler = make_scheduler(
        sched_db, tmp_path / "l.lock",
        clock=lambda: UTC_NOW.astimezone(timezone(timedelta(hours=8))))
    with pytest.raises(ValueError):
        await scheduler.run_due_once()


# ===== flock 互斥与锁文件安全 =====

@pytest.mark.asyncio
async def test_only_one_scheduler_holds_process_lock(sched_db, tmp_path):
    """实际 flock：两实例互斥；释放后第二实例可接管（brief 原文场景）"""
    lock = tmp_path / "scheduler.lock"
    first = SubscriptionScheduler(sched_db, lock)
    second = SubscriptionScheduler(sched_db, lock)
    assert await first.start() is True
    assert await second.start() is False
    await first.stop()
    assert await second.start() is True
    await second.stop()


@pytest.mark.asyncio
async def test_lock_file_is_regular_mode_0600(sched_db, tmp_path):
    lock = tmp_path / "scheduler.lock"
    scheduler = make_scheduler(sched_db, lock)
    assert await scheduler.start() is True
    st = lock.lstat()
    assert stat_mod.S_ISREG(st.st_mode) and not stat_mod.S_ISLNK(st.st_mode)
    assert st.st_mode & 0o777 == 0o600
    # 锁文件不写 PID/路径等内容
    assert lock.stat().st_size == 0
    await scheduler.stop()


@pytest.mark.asyncio
async def test_lock_rejects_symlink(sched_db, tmp_path):
    """锁路径为 symlink：打开即失败（O_NOFOLLOW），不进入 standby"""
    target = tmp_path / "real.lock"
    target.write_text("")
    link = tmp_path / "link.lock"
    link.symlink_to(target)
    scheduler = make_scheduler(sched_db, link)
    with pytest.raises(OSError):
        await scheduler.start()
    assert scheduler.active is False


@pytest.mark.asyncio
async def test_standby_creates_no_task_and_stop_is_safe(sched_db, tmp_path):
    """standby 不建 task/fd；stop 安全；leader 释放后可接管"""
    lock = tmp_path / "scheduler.lock"
    leader = make_scheduler(sched_db, lock)
    standby = make_scheduler(sched_db, lock)
    assert await leader.start() is True
    assert await standby.start() is False
    assert standby._task is None and standby._lock_fd is None
    await standby.stop()  # no-op，不影响 leader
    assert leader.active is True
    await leader.stop()
    assert await standby.start() is True
    await standby.stop()


# ===== start/stop 幂等与生命周期 =====

@pytest.mark.asyncio
async def test_start_idempotent_while_active(sched_db, tmp_path):
    scheduler = make_scheduler(sched_db, tmp_path / "l.lock")
    assert await scheduler.start() is True
    task, fd = scheduler._task, scheduler._lock_fd
    assert await scheduler.start() is True
    assert scheduler._task is task and scheduler._lock_fd == fd
    await scheduler.stop()
    await scheduler.stop()  # 重复 stop 安全
    assert scheduler._task is None and scheduler._lock_fd is None
    assert scheduler.active is False


@pytest.mark.asyncio
async def test_stop_on_never_started_is_safe(sched_db, tmp_path):
    scheduler = make_scheduler(sched_db, tmp_path / "l.lock")
    await scheduler.stop()
    assert scheduler.active is False


@pytest.mark.asyncio
async def test_start_releases_lock_when_task_creation_fails(sched_db, tmp_path, monkeypatch):
    """建 task 失败：锁立即释放，另一实例可成为 leader"""

    def broken_create_task(coro):
        coro.close()
        raise RuntimeError("no task")

    monkeypatch.setattr(asyncio, "create_task", broken_create_task)
    scheduler = make_scheduler(sched_db, tmp_path / "l.lock")
    with pytest.raises(RuntimeError):
        await scheduler.start()
    monkeypatch.undo()
    other = make_scheduler(sched_db, tmp_path / "l.lock")
    assert await other.start() is True
    await other.stop()


@pytest.mark.asyncio
async def test_stop_wakes_sleeping_loop_quickly(sched_db, tmp_path):
    """间隔等待中的循环被 stop 立即唤醒（不等满 30 秒）"""
    scheduler = make_scheduler(sched_db, tmp_path / "l.lock", interval=3600)
    assert await scheduler.start() is True
    await asyncio.sleep(0.05)  # 让首轮 run（空库）完成并进入间隔等待
    start = time.monotonic()
    await asyncio.wait_for(scheduler.stop(), timeout=5)
    assert time.monotonic() - start < 4


@pytest.mark.asyncio
async def test_stop_leaves_no_pending_tasks(sched_db, tmp_path):
    before = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}
    scheduler = make_scheduler(sched_db, tmp_path / "l.lock", interval=0.05)
    assert await scheduler.start() is True
    await asyncio.sleep(0.12)
    await scheduler.stop()
    after = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}
    assert after == before


# ===== 循环行为 =====

@pytest.mark.asyncio
async def test_loop_runs_immediately_and_repeats(sched_db, tmp_path, monkeypatch):
    """循环启动即执行一轮；注入短间隔后周期执行；stop 后不再执行"""
    await add_subscription(sched_db, next_update_at=UTC_NOW_NAIVE)
    ticks = 0

    def advancing_clock():
        nonlocal ticks
        ticks += 1
        return UTC_NOW + timedelta(seconds=7200 * ticks)

    ran_first = asyncio.Event()
    count = 0

    async def fake_fetch(url):
        nonlocal count
        count += 1
        ran_first.set()
        return [NODE_PAYLOAD]

    monkeypatch.setattr(subscription_service, "fetch_subscription", fake_fetch)
    scheduler = make_scheduler(sched_db, tmp_path / "l.lock",
                               clock=advancing_clock, interval=0.05)
    assert await scheduler.start() is True
    await asyncio.wait_for(ran_first.wait(), timeout=2)  # 立即首轮
    await asyncio.sleep(0.25)  # 短间隔下多轮
    assert count >= 2
    await scheduler.stop()
    stopped_at = count
    await asyncio.sleep(0.15)
    assert count == stopped_at


@pytest.mark.asyncio
async def test_loop_survives_cycle_exception_without_leaking(sched_db, tmp_path,
                                                             monkeypatch, caplog):
    """单轮意外异常：仅记类型名（不含异常消息/URL），循环继续下一轮"""
    calls = 0
    failed = asyncio.Event()
    recovered = asyncio.Event()

    async def flaky_run_due_once(now=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            failed.set()
            raise RuntimeError("boom https://secret.example/feed")
        recovered.set()
        return []

    scheduler = make_scheduler(sched_db, tmp_path / "l.lock", interval=0.05)
    monkeypatch.setattr(scheduler, "run_due_once", flaky_run_due_once)
    with caplog.at_level(logging.ERROR,
                         logger="app.services.subscription_scheduler"):
        assert await scheduler.start() is True
        await asyncio.wait_for(failed.wait(), timeout=2)
        await asyncio.wait_for(recovered.wait(), timeout=2)
        await scheduler.stop()
    assert "RuntimeError" in caplog.text
    assert "secret" not in caplog.text
    assert "boom" not in caplog.text


@pytest.mark.asyncio
async def test_loop_cancelled_error_propagates_and_stop_releases(sched_db, tmp_path):
    """取消循环任务：CancelledError 传播；stop 收尾后锁可被接管"""
    scheduler = make_scheduler(sched_db, tmp_path / "l.lock", interval=3600)
    assert await scheduler.start() is True
    task = scheduler._task
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(task), timeout=2)
    await scheduler.stop()
    taker = make_scheduler(sched_db, tmp_path / "l.lock")
    assert await taker.start() is True
    await taker.stop()


# ===== 调度守卫：选择后排期竞态 =====


@pytest.mark.asyncio
async def test_due_skips_items_mutated_after_selection(sched_db, tmp_path, monkeypatch):
    """前项慢拉取期间后项被禁用/auto-off/推迟：后项拉取前守卫拒绝，
    不 fetch、不记任何状态、不覆盖新排期"""
    from sqlalchemy import update as sa_update

    first = await add_subscription(sched_db, name="slow", url=f"{URL}?slow",
                                   next_update_at=UTC_NOW_NAIVE - timedelta(seconds=3))
    off_id = await add_subscription(sched_db, name="off", url=f"{URL}?off",
                                    next_update_at=UTC_NOW_NAIVE - timedelta(seconds=2))
    autooff_id = await add_subscription(sched_db, name="autooff", url=f"{URL}?autooff",
                                        next_update_at=UTC_NOW_NAIVE - timedelta(seconds=2))
    future_id = await add_subscription(sched_db, name="future", url=f"{URL}?future",
                                       next_update_at=UTC_NOW_NAIVE - timedelta(seconds=2))
    far = UTC_NOW_NAIVE + timedelta(days=30)

    async def mutating_fetch(url):
        if url.endswith("?slow"):
            # 前项网络往返窗口：后项被并发修改（禁用/auto-off/推迟）
            async with sched_db.session_factory() as db:
                await db.execute(sa_update(Subscription)
                                  .where(Subscription.id == off_id)
                                  .values(enabled=False, next_update_at=None))
                await db.execute(sa_update(Subscription)
                                  .where(Subscription.id == autooff_id)
                                  .values(auto_update=False, next_update_at=None))
                await db.execute(sa_update(Subscription)
                                  .where(Subscription.id == future_id)
                                  .values(next_update_at=far))
                await db.commit()
            await asyncio.sleep(0.05)
            return [NODE_PAYLOAD]
        raise AssertionError(f"排期已变化的订阅不得拉取: {url}")

    monkeypatch.setattr(subscription_service, "fetch_subscription", mutating_fetch)
    scheduler = make_scheduler(sched_db, tmp_path / "l.lock",
                               clock=lambda: UTC_NOW)
    results = await scheduler.run_due_once()
    assert [r.subscription_id for r in results] == [first]

    async with sched_db.session_factory() as db:
        subs = {s.id: s for s in (await db.execute(
            sa_select(Subscription))).scalars()}
    for sid in (off_id, autooff_id, future_id):
        sub = subs[sid]
        assert sub.last_attempt_at is None and sub.last_error_code is None
    assert subs[off_id].next_update_at is None
    assert subs[autooff_id].next_update_at is None
    assert subs[future_id].next_update_at == far  # 新排期未被覆盖


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["reschedule", "disable", "auto_off"])
async def test_scheduled_final_guard_rejects_write_after_mutation(
        sched_db, tmp_path, monkeypatch, mutation):
    """当前项拉取期间自身排期/开关被改：写事务守卫拒写（不落节点、
    不记失败、新排期保留）；manual 同步无守卫仍可正常执行"""
    from sqlalchemy import update as sa_update

    from app.models import ProxyNode
    from app.services.subscription import sync_subscription

    sub_id = await add_subscription(sched_db, next_update_at=UTC_NOW_NAIVE)
    rescheduled = UTC_NOW_NAIVE + timedelta(seconds=7200)

    async def mutating_fetch(url):
        # 本项拉取窗口内并发修改（模拟用户 update / 另一成功同步）
        async with sched_db.session_factory() as db:
            if mutation == "reschedule":  # interval 改变 → now+新 interval
                values = {"update_interval": 7200, "next_update_at": rescheduled}
            elif mutation == "disable":  # 禁用 → NULL（update 路由语义）
                values = {"enabled": False, "next_update_at": None}
            else:  # auto off → NULL
                values = {"auto_update": False, "next_update_at": None}
            await db.execute(sa_update(Subscription)
                              .where(Subscription.id == sub_id).values(**values))
            await db.commit()
        return [{"name": "Fresh", "node_type": "trojan", "server": "8.8.8.8",
                 "port": 443,
                 "config": "Fresh = trojan, 8.8.8.8, 443, password=z"}]

    monkeypatch.setattr(subscription_service, "fetch_subscription", mutating_fetch)
    scheduler = make_scheduler(sched_db, tmp_path / "l.lock",
                               clock=lambda: UTC_NOW)
    assert await scheduler.run_due_once() == []  # 守卫跳过不出现在结果

    async with sched_db.session_factory() as db:
        nodes = (await db.execute(sa_select(ProxyNode))).scalars().all()
        sub = await db.get(Subscription, sub_id)
    assert nodes == [], "守卫拒绝后不得写入任何节点"
    assert sub.last_attempt_at is None and sub.last_error_code is None
    assert sub.last_success_at is None and sub.last_node_count is None
    assert sub.next_update_at == (None if mutation != "reschedule" else rescheduled)

    # manual 同步（无守卫）：仍可正常拉取写入
    async def plain_fetch(url):
        return [{"name": "Manual", "node_type": "trojan", "server": "9.9.9.9",
                 "port": 443,
                 "config": "Manual = trojan, 9.9.9.9, 443, password=m"}]

    monkeypatch.setattr(subscription_service, "fetch_subscription", plain_fetch)
    async with sched_db.session_factory() as db:
        result = await sync_subscription(db, sub_id, now=UTC_NOW_NAIVE)
    assert result.success is True and result.node_count == 1


# ===== ORM interval 赋值校验 =====

def test_model_rejects_out_of_range_interval():
    with pytest.raises(ValueError):
        Subscription(name="S", url=URL, update_interval=100)
    with pytest.raises(ValueError):
        Subscription(name="S", url=URL, update_interval=604801)
    sub = Subscription(name="S", url=URL, update_interval=300)
    with pytest.raises(ValueError):
        sub.update_interval = 604800 * 2
    with pytest.raises(ValueError):
        sub.update_interval = None
    sub.update_interval = 604800
    assert sub.update_interval == 604800


def _fake_fetch_ok():
    async def fake_fetch(url):
        return [NODE_PAYLOAD]
    return fake_fetch


async def _add_keep_node(runtime, sub_id, name="Keep"):
    from app.models import ProxyNode
    from app.services.node_identity import stable_node_key
    config = f"{name} = trojan, 1.1.1.1, 443, password=x"
    async with runtime.session_factory() as db:
        db.add(ProxyNode(name=name, subscription_id=sub_id, node_type="trojan",
                         server="1.1.1.1", port=443, config=config,
                         stable_key=stable_node_key({"name": name, "config": config}),
                         enabled=True, pinned_services=[]))
        await db.commit()


FRESH_PAYLOAD = {"name": "Fresh", "node_type": "trojan", "server": "8.8.8.8",
                 "port": 443,
                 "config": "Fresh = trojan, 8.8.8.8, 443, password=z"}


@pytest.mark.asyncio
async def test_scheduled_skip_wins_over_stale_url_when_both_mutated(
        sched_db, tmp_path, monkeypatch):
    """双 session 交错：URL+interval 同时更新且旧拉取成功——写事务守卫
    先于 URL 比较，判 SCHEDULE_SKIPPED；新 URL/新排期/节点与既有
    成功状态全部保留，无失败记录"""
    from sqlalchemy import update as sa_update

    from app.models import ProxyNode

    sub_id = await add_subscription(sched_db, url=f"{URL}?old",
                                   next_update_at=UTC_NOW_NAIVE)
    await _add_keep_node(sched_db, sub_id)
    baseline_attempt = UTC_NOW_NAIVE - timedelta(hours=2)
    baseline_success = UTC_NOW_NAIVE - timedelta(hours=2)
    async with sched_db.session_factory() as db:
        await db.execute(sa_update(Subscription).where(Subscription.id == sub_id)
                         .values(last_attempt_at=baseline_attempt,
                                 last_success_at=baseline_success,
                                 last_node_count=1))
        await db.commit()
    new_next = UTC_NOW_NAIVE + timedelta(seconds=7200)

    async def mutating_fetch(url):
        async with sched_db.session_factory() as db:
            await db.execute(sa_update(Subscription)
                             .where(Subscription.id == sub_id)
                             .values(url=f"{URL}?new", update_interval=7200,
                                     next_update_at=new_next))
            await db.commit()
        return [FRESH_PAYLOAD]

    monkeypatch.setattr(subscription_service, "fetch_subscription", mutating_fetch)
    scheduler = make_scheduler(sched_db, tmp_path / "l.lock",
                               clock=lambda: UTC_NOW)
    assert await scheduler.run_due_once() == []

    async with sched_db.session_factory() as db:
        sub = await db.get(Subscription, sub_id)
        nodes = (await db.execute(sa_select(ProxyNode)
                                  .where(ProxyNode.subscription_id == sub_id))
                 ).scalars().all()
    assert sub.url == f"{URL}?new"
    assert sub.next_update_at == new_next
    assert [n.name for n in nodes] == ["Keep"], "Fresh 不得落库"
    assert sub.last_attempt_at == baseline_attempt, "基线 attempt 不得覆盖"
    assert sub.last_success_at == baseline_success
    assert sub.last_error_code is None and sub.last_node_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "empty"])
async def test_scheduled_failure_record_skipped_after_reschedule(
        sched_db, tmp_path, monkeypatch, failure):
    """双 session 交错：next 重排后旧拉取 timeout/空结果——失败短事务
    守卫拒绝，不记 attempt/error/next；manual timeout 无守卫仍正常记状态"""
    from sqlalchemy import update as sa_update

    import httpx as httpx_mod
    from app.services.subscription import sync_subscription

    sub_id = await add_subscription(sched_db, next_update_at=UTC_NOW_NAIVE)
    far = UTC_NOW_NAIVE + timedelta(days=7)

    async def failing_fetch(url):
        async with sched_db.session_factory() as db:
            await db.execute(sa_update(Subscription)
                             .where(Subscription.id == sub_id)
                             .values(next_update_at=far))
            await db.commit()
        if failure == "timeout":
            raise httpx_mod.TimeoutException("synthetic timeout")
        return []

    monkeypatch.setattr(subscription_service, "fetch_subscription", failing_fetch)
    scheduler = make_scheduler(sched_db, tmp_path / "l.lock",
                               clock=lambda: UTC_NOW)
    assert await scheduler.run_due_once() == []

    async with sched_db.session_factory() as db:
        sub = await db.get(Subscription, sub_id)
    assert sub.last_attempt_at is None, "守卫跳过不得记录尝试"
    assert sub.last_error_code is None
    assert sub.next_update_at == far, "重排后的新排期不得覆盖"

    # manual timeout（无守卫）：仍正常记录失败状态
    async def manual_timeout_fetch(url):
        raise httpx_mod.TimeoutException("synthetic manual timeout")

    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        manual_timeout_fetch)
    async with sched_db.session_factory() as db:
        result = await sync_subscription(db, sub_id, now=UTC_NOW_NAIVE)
    assert result.success is False and result.error_code == "timeout"
    async with sched_db.session_factory() as db:
        sub = await db.get(Subscription, sub_id)
    assert sub.last_attempt_at == UTC_NOW_NAIVE
    assert sub.last_error_code == "timeout"
    assert sub.next_update_at == UTC_NOW_NAIVE + timedelta(seconds=900)


@pytest.mark.asyncio
async def test_scheduled_url_only_change_keeps_stale_url_failure(
        sched_db, tmp_path, monkeypatch):
    """双 session 交错：仅 URL 改、next 仍为 expected——守卫通过，走原
    stale URL 失败语义（internal_error + attempt/error/next=+900），旧结果
    拒写、新 URL 保留"""
    from sqlalchemy import update as sa_update

    from app.models import ProxyNode

    sub_id = await add_subscription(sched_db, url=f"{URL}?old",
                                   next_update_at=UTC_NOW_NAIVE)
    await _add_keep_node(sched_db, sub_id)

    async def mutating_fetch(url):
        async with sched_db.session_factory() as db:
            await db.execute(sa_update(Subscription)
                             .where(Subscription.id == sub_id)
                             .values(url=f"{URL}?new"))
            await db.commit()
        return [FRESH_PAYLOAD]

    monkeypatch.setattr(subscription_service, "fetch_subscription", mutating_fetch)
    scheduler = make_scheduler(sched_db, tmp_path / "l.lock",
                               clock=lambda: UTC_NOW)
    results = await scheduler.run_due_once()
    assert [(r.subscription_id, r.success, r.error_code) for r in results] == [
        (sub_id, False, "internal_error")]

    async with sched_db.session_factory() as db:
        sub = await db.get(Subscription, sub_id)
        nodes = (await db.execute(sa_select(ProxyNode)
                                  .where(ProxyNode.subscription_id == sub_id))
                 ).scalars().all()
    assert sub.url == f"{URL}?new"
    assert sub.last_attempt_at == UTC_NOW_NAIVE
    assert sub.last_error_code == "internal_error"
    assert sub.next_update_at == UTC_NOW_NAIVE + timedelta(seconds=900)
    assert [n.name for n in nodes] == ["Keep"], "旧拉取结果不得写入"


# ===== 写锁串行化：guard 与 DML 之间无排期穿透（Fix Round 3） =====


@pytest.mark.asyncio
async def test_failure_record_waits_for_held_write_lock_then_skips(
        sched_db, tmp_path, monkeypatch):
    """失败记录窗口：另一 session 持写锁更新排期未提交——sync 的失败
    状态事务 BEGIN IMMEDIATE 排队等待（pending 断言，未并行穿透）；
    用户提交后失败记录获锁、守卫看到新排期 → skip，绝不写状态"""
    import httpx as httpx_mod
    from sqlalchemy import text as sa_text

    sub_id = await add_subscription(sched_db, next_update_at=UTC_NOW_NAIVE)
    far = UTC_NOW_NAIVE + timedelta(days=7)

    async def timeout_fetch(url):
        raise httpx_mod.TimeoutException("synthetic timeout")

    monkeypatch.setattr(subscription_service, "fetch_subscription", timeout_fetch)
    scheduler = make_scheduler(sched_db, tmp_path / "l.lock",
                               clock=lambda: UTC_NOW)

    # 用户事务：先持 SQLite 写锁、更新排期但不提交
    async with sched_db.session_factory() as holder:
        await holder.execute(sa_text("BEGIN IMMEDIATE"))
        await holder.execute(sa_update(Subscription)
                             .where(Subscription.id == sub_id)
                             .values(next_update_at=far))
        sync_task = asyncio.create_task(scheduler.run_due_once())
        await asyncio.sleep(0.3)
        assert not sync_task.done(), "失败状态事务应被写锁挡住（排队等待）"
        await holder.commit()  # 用户先提交（新排期生效）

    results = await asyncio.wait_for(sync_task, 5)
    assert results == [], "用户已提交新排期，守卫应跳过"
    async with sched_db.session_factory() as db:
        sub = await db.get(Subscription, sub_id)
    assert sub.last_attempt_at is None, "排队获锁后守卫跳过：不写 attempt"
    assert sub.last_error_code is None
    assert sub.next_update_at == far


@pytest.mark.asyncio
async def test_success_phase3_write_lock_blocks_concurrent_reschedule(
        sched_db, tmp_path, monkeypatch):
    """成功 phase3 窗口：sync BEGIN IMMEDIATE 持锁后发起的并发排期更新
    被写锁挡住（不穿透、guard 不误判），sync 正常提交 500 节点；随后
    用户更新获锁提交，最终排期为用户值（用户更新最终胜出）"""
    from sqlalchemy import event as sa_event, text as sa_text

    from app.models import ProxyNode
    sub_id = await add_subscription(sched_db, next_update_at=UTC_NOW_NAIVE)
    user_value = UTC_NOW_NAIVE + timedelta(seconds=7200)
    user_done = asyncio.Event()
    fired = {"lock_seen": False}

    # 500 个全新连接身份：拉长 phase3 持锁窗口（500 次 INSERT 往返）
    bulk = [{"name": f"Bulk{i}", "node_type": "trojan", "server": f"10.0.{i // 250}.{i % 250}",
             "port": 443,
             "config": f"Bulk{i} = trojan, 10.0.{i // 250}.{i % 250}, 443, password=p"}
            for i in range(500)]

    async def user_update():
        async with sched_db.session_factory() as db:
            await db.execute(sa_update(Subscription)
                             .where(Subscription.id == sub_id)
                             .values(update_interval=7200, next_update_at=user_value))
            await db.commit()
        user_done.set()

    def on_before_cursor(conn, cursor, statement, params, context, executemany):
        # sync 的写事务首条语句：此刻发起用户更新——它只能在本语句
        # （已在 db 线程队列头）获锁之后到达 SQLite，必然排队等待
        if (not fired["lock_seen"]
                and statement.strip().upper().startswith("BEGIN IMMEDIATE")):
            fired["lock_seen"] = True
            asyncio.get_running_loop().create_task(user_update())

    sa_event.listen(sched_db.engine.sync_engine, "before_cursor_execute",
                    on_before_cursor)

    async def bulk_fetch(url):
        return bulk

    try:
        monkeypatch.setattr(subscription_service, "fetch_subscription", bulk_fetch)
        scheduler = make_scheduler(sched_db, tmp_path / "l.lock",
                                   clock=lambda: UTC_NOW)
        results = await asyncio.wait_for(scheduler.run_due_once(), timeout=30)
    finally:
        sa_event.remove(sched_db.engine.sync_engine, "before_cursor_execute",
                        on_before_cursor)

    assert fired["lock_seen"], "测试未观察到写锁获取点"
    # sync 持锁完成全部 upsert：若用户更新曾穿透提交，guard 会转 skip
    assert [(r.success, r.node_count) for r in results] == [(True, 500)]
    await asyncio.wait_for(user_done.wait(), timeout=10)
    async with sched_db.session_factory() as db:
        sub = await db.get(Subscription, sub_id)
        count = (await db.execute(
            sa_select(ProxyNode).where(ProxyNode.subscription_id == sub_id))
        ).scalars().all()
    assert len(count) == 500
    assert sub.next_update_at == user_value, "用户更新最终胜出（未被 sync 覆盖）"
