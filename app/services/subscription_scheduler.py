"""单实例订阅自动更新调度器。

进程级互斥：锁文件（默认 ``RuntimePaths.root / "scheduler.lock"``）以
``O_CREAT|O_RDWR|O_CLOEXEC|O_NOFOLLOW`` 0600 打开，``fstat`` 校验普通
文件后 ``fchmod`` 收敛权限，再 ``flock LOCK_EX|LOCK_NB`` 非阻塞加锁。
竞争方立即关闭描述符并返回 standby（False），不创建任何任务；其余
安全/IO 错误让启动失败（fail closed）。锁文件内容不写 PID/路径——
存活语义只由内核 flock 持有状态表达。

调度循环：拿到锁后立即执行一轮到期同步，之后每 ``interval`` 秒
（默认 30）一轮；``stop`` 通过 Event 唤醒后等待任务退出并释放锁。
单轮意外异常只记录异常类型名（不携带订阅/URL/配置内容）并继续下一
轮；CancelledError 原样传播，由 ``stop`` 统一收尾。
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import logging
import os
import stat as stat_mod
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import select

from app.models import Subscription
from app.services.subscription import (SCHEDULE_SKIPPED, SyncResult,
                                       _utc_naive, sync_subscription)

logger = logging.getLogger(__name__)

LOOP_INTERVAL_SECONDS = 30.0
LOCK_FILE_MODE = 0o600


def utc_now() -> datetime:
    """默认时钟：aware UTC（比较前经 ``_utc_naive`` 规范为 naive UTC）"""
    return datetime.now(timezone.utc)


class SubscriptionScheduler:
    """每实例至多一条自动更新循环；leader 持锁执行，standby 只服务不循环。"""

    def __init__(self, database, lock_path: str | os.PathLike,
                 clock: Callable[[], datetime] | None = None,
                 interval: float = LOOP_INTERVAL_SECONDS):
        self._database = database
        self._lock_path = Path(lock_path)
        self._clock = clock or utc_now
        self._interval = interval
        self._lock_fd: int | None = None
        self._task: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None

    @property
    def active(self) -> bool:
        """是否持有锁并运行循环（standby 恒为 False）"""
        return self._lock_fd is not None

    async def start(self) -> bool:
        """尝试成为 leader。已 active 时幂等返回 True；竞争返回 False。"""
        if self._lock_fd is not None:
            return True
        fd = self._acquire_lock_fd()
        if fd is None:
            return False
        self._lock_fd = fd
        try:
            self._wake = asyncio.Event()
            self._task = asyncio.create_task(self._loop())
        except BaseException:
            self._task = None
            self._release_lock()
            raise
        return True

    async def stop(self) -> None:
        """停止循环并释放锁。对 active/standby/重复调用均安全。"""
        task, wake = self._task, self._wake
        self._task = None
        self._wake = None
        try:
            if task is not None:
                if wake is not None and not task.done():
                    wake.set()
                try:
                    await task
                except asyncio.CancelledError:
                    if not task.cancelled():
                        task.cancel()
                    # 循环任务被取消（或 stop 自身被取消）：锁必须释放
                except Exception as exc:
                    logger.error("scheduler loop stopped with error: %s",
                                 type(exc).__name__)
        finally:
            self._release_lock()

    async def run_due_once(self, now: datetime | None = None) -> list[SyncResult]:
        """同步全部到期订阅（enabled+auto 且 next<=now）。

        选择查询使用独立短会话并立即结束事务，并保留每项的
        next_update_at 快照；随后逐个订阅开独立会话调用同步服务
        （传入排期守卫：拉取前/写事务内排期被并发改动即跳过，不
        拉取、不记失败、不覆盖新排期）——网络拉取不持有选择查询的
        事务。单项异常不阻断后续（记录类型名，不泄内容）；订阅不
        存在（None）与守卫跳过（SCHEDULE_SKIPPED）均不出现在结果中。
        返回实际执行的同步结果列表。
        """
        now_naive = _utc_naive(now if now is not None else self._clock())
        session_factory = self._database.session_factory

        async with session_factory() as db:
            due = list((await db.execute(
                select(Subscription.id, Subscription.next_update_at)
                .where(Subscription.enabled == True,  # noqa: E712
                       Subscription.auto_update == True,  # noqa: E712
                       Subscription.next_update_at.is_not(None),
                       Subscription.next_update_at <= now_naive)
                .order_by(Subscription.next_update_at.asc(),
                          Subscription.id.asc())
            )).all())
            await db.commit()

        results: list[SyncResult] = []
        for sub_id, expected_next in due:
            try:
                async with session_factory() as db:
                    result = await sync_subscription(db, sub_id, now=now_naive,
                                                     scheduled_next=expected_next)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("scheduled sync failed: subscription_id=%s error=%s",
                             sub_id, type(exc).__name__)
                continue
            if result is None or result is SCHEDULE_SKIPPED:
                continue
            results.append(result)
        return results

    # ===== 内部实现 =====

    async def _loop(self) -> None:
        assert self._wake is not None
        while True:
            try:
                await self.run_due_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("scheduler cycle failed: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(self._wake.wait(),
                                       timeout=self._interval)
            except asyncio.TimeoutError:
                continue
            return  # 被唤醒（stop）→ 退出

    def _acquire_lock_fd(self) -> int | None:
        """打开并独占锁文件；竞争返回 None（fd 已关闭），其余错误抛出。"""
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self._lock_path, flags, LOCK_FILE_MODE)
        try:
            if not stat_mod.S_ISREG(os.fstat(fd).st_mode):
                raise RuntimeError("scheduler lock is not a regular file")
            os.fchmod(fd, LOCK_FILE_MODE)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                try:
                    os.close(fd)
                except OSError:
                    pass
                if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                    return None
                raise
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        return fd

    def _release_lock(self) -> None:
        fd, self._lock_fd = self._lock_fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError as exc:
            logger.error("scheduler lock release failed: %s",
                         type(exc).__name__)
        try:
            os.close(fd)
        except OSError as exc:
            logger.error("scheduler lock close failed: %s",
                         type(exc).__name__)
