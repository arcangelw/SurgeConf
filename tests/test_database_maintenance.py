"""数据库维护生命周期回归测试（Fix Slice 3）。

复现并锁死三条失败路径：
A. reset 走 legacy auto-discovery 把保留的旧仓库库重新导入；
B. staging 发布链在 integrity/权限/checkpoint/sidecar/replace 故障时
   先删 live -wal/-shm，丢失仅存在于 WAL 的已提交数据；
C. dispose 不关 checkout 连接且维护无互斥：在途/新会话与恢复/重置
   并发写旧 inode，维护期间新请求遇到未绑定 500。

全部使用 tmp sqlite；不接触真实运行库。
"""

import asyncio
import hashlib
import sqlite3

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.database import DatabaseRuntime
from app.models import Subscription, SystemConfig
from app.services import backups, database_migration
from tests.test_api_endpoints import runtime_paths  # noqa: F401
from tests.test_database_migration import create_current_schema_database


def sqlite_scalar(path, sql: str):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


async def _system_config_value(runtime, key: str):
    async with _tracked_session(runtime) as db:
        row = (await db.execute(
            select(SystemConfig.value).where(SystemConfig.key == key)
        )).scalar_one_or_none()
    return row


def _tracked_session(runtime):
    """生产受控入口（新代码）；无该入口的旧代码回退工厂直开（等价于
    旧版 API 请求路径），两种形态都能复现/验证「在途会话与维护互斥」。"""
    if hasattr(runtime, "session"):
        return runtime.session()
    return runtime.session_factory()


@pytest_asyncio.fixture
async def runtime_db(runtime_paths, monkeypatch):
    """独立临时运行时：完整迁移建库 + 绑定 + marker 数据。

    backups/security 服务在函数内解析 ``app.database.database``，
    patch 全局引用即定向到本运行时。
    """
    await database_migration.prepare_database(
        runtime_paths, runtime_paths.root / "missing.db")
    runtime = await DatabaseRuntime.create(runtime_paths.database)
    monkeypatch.setattr("app.database.database", runtime)
    async with runtime.session_factory() as db:
        db.add(SystemConfig(key="marker", value="original"))
        await db.commit()
    yield runtime
    await runtime.dispose()


def _write_wal_only_record(live) -> sqlite3.Connection:
    """写入仅存在于 WAL 的已提交记录；保持连接打开（不触发关闭时
    自动 checkpoint），返回连接供测试结束时关闭。"""
    conn = sqlite3.connect(live)
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute(
        "INSERT INTO system_config(key, value) VALUES('wal_only', '1')")
    conn.commit()
    return conn


def _assert_record_only_in_wal(live) -> None:
    """前置证明：main 文件副本不含该记录（副本无 -wal 伴随）。"""
    copy = live.with_name(live.name + ".main-copy")
    copy.write_bytes(live.read_bytes())
    try:
        assert sqlite_scalar(copy, (
            "SELECT count(*) FROM system_config WHERE key='wal_only'")) == 0
        assert sqlite_scalar(live, (
            "SELECT count(*) FROM system_config WHERE key='wal_only'")) == 1
    finally:
        copy.unlink()


def _pause_upgrade(monkeypatch, entered: asyncio.Event, release: asyncio.Event):
    """把 rebuild 链暂停在 staging 升级处（维护屏障内、live 未被动）。"""
    real = database_migration.upgrade_staging

    async def paused(staging):
        entered.set()
        await release.wait()
        return await real(staging)

    monkeypatch.setattr(database_migration, "upgrade_staging", paused)


# ===== A：重置不得回导旧仓库库 =====


@pytest.mark.asyncio
async def test_reset_does_not_reimport_legacy(runtime_db, runtime_paths,
                                              monkeypatch, tmp_path):
    """reset 必须使用显式 fresh staging 入口：绝不走 prepare_database
    的三路径自动发现（旧实现删 live 后，PROJECT_ROOT 下存在旧仓库库
    时会把它重新导入 live，破坏重置语义）。"""
    from pathlib import Path as _Path

    legacy_root = tmp_path / "repo"
    legacy = create_current_schema_database(
        legacy_root / "data" / "surge_conf.db")
    conn = sqlite3.connect(legacy)
    with conn:
        conn.execute(
            "INSERT INTO system_config(key, value) VALUES('legacy_marker', 'x')")
    conn.close()
    before = hashlib.sha256(legacy.read_bytes()).hexdigest()

    prepare_calls = []
    real_prepare = database_migration.prepare_database

    async def spying_prepare(paths, legacy_path):
        prepare_calls.append(_Path(legacy_path))
        return await real_prepare(paths, legacy_path)

    monkeypatch.setattr(database_migration, "prepare_database",
                        spying_prepare)

    await backups.reset_database()

    assert prepare_calls == [], \
        "reset 绝不能走 prepare_database（含 legacy 自动发现回导）"
    assert await _system_config_value(runtime_db, "legacy_marker") is None
    assert await _system_config_value(runtime_db, "marker") is None, \
        "reset 后 live 不得残留本实例旧数据"
    async with runtime_db.session_factory() as db:
        total = len((await db.execute(select(SystemConfig))).scalars().all())
    assert total > 0, "reset 后 live 必须含 fresh 种子数据"
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == before, \
        "reset 不得改动旧仓库库文件"


# ===== B：发布链故障不得丢失 live WAL 已提交数据 =====


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["integrity", "permission",
                                     "checkpoint", "sidecar", "replace"])
async def test_publish_failure_preserves_wal_only_record(
        runtime_db, runtime_paths, monkeypatch, failure):
    """live 含仅存在于 WAL 的已提交记录：staging integrity/权限、
    checkpoint、sidecar 清理、os.replace 任一故障后，live 仍逻辑完整
    且该记录可查（绝不能出现「旧 main + 已删未 checkpoint WAL」）。

    经 restore 暴露：restore 失败时 live main 未被替换（不同于 reset
    的 safety 还原兜底），任何「先删 live WAL 再失败」的发布顺序都会
    在此处丢已提交数据。"""
    live = runtime_paths.database
    info = await backups.create_backup("manual")
    conn = _write_wal_only_record(live)
    try:
        _assert_record_only_in_wal(live)
        staging = database_migration.staging_path(runtime_paths)

        if failure == "integrity":
            def broken(staging_path_arg):
                raise RuntimeError("integrity boom")
            monkeypatch.setattr(database_migration, "verify_staging", broken)
        elif failure == "permission":
            real_harden = database_migration.harden_private_file

            def fail_on_staging(path):
                if path == staging:
                    raise RuntimeError("harden boom")
                return real_harden(path)
            monkeypatch.setattr(database_migration, "harden_private_file",
                                fail_on_staging)
        elif failure == "checkpoint":
            real_checkpoint = database_migration._checkpoint_and_remove_sidecars

            def fail_on_live(path):
                if path == live:
                    raise RuntimeError("checkpoint boom")
                return real_checkpoint(path)
            monkeypatch.setattr(database_migration,
                                "_checkpoint_and_remove_sidecars", fail_on_live)
        elif failure == "sidecar":
            import os as os_mod
            real_unlink = os_mod.unlink

            def fail_live_wal(path, **kwargs):
                if str(path) == str(live) + "-wal":
                    raise OSError("unlink boom")
                return real_unlink(path, **kwargs)
            monkeypatch.setattr(database_migration.os, "unlink", fail_live_wal)
        else:  # replace
            def broken_replace(src, dst):
                raise OSError("replace boom")
            monkeypatch.setattr(database_migration.os, "replace",
                                broken_replace)

        with pytest.raises((RuntimeError, OSError)):
            await backups.restore_backup(info.name)

        # 故障后 live 逻辑完整且 WAL 已提交记录仍可查
        assert sqlite_scalar(live, "PRAGMA integrity_check") == "ok"
        assert sqlite_scalar(live, (
            "SELECT count(*) FROM system_config WHERE key='wal_only'")) == 1, \
            f"{failure} 故障不得丢失仅存在于 WAL 的已提交记录"
    finally:
        conn.close()


# ===== C：维护屏障（会话互斥、串行化、可靠放行） =====


@pytest.mark.asyncio
async def test_reset_waits_for_inflight_session_then_completes(
        runtime_db, runtime_paths):
    """持有受控在途会话（API/调度器同入口）时 reset 不得越过；会话
    释放后 reset 完成并读到 fresh 库。"""
    async with _tracked_session(runtime_db) as db:
        db.add(SystemConfig(key="inflight", value="1"))
        await db.commit()
        reset_task = asyncio.create_task(backups.reset_database())
        await asyncio.sleep(0.1)
        assert not reset_task.done(), "在途会话未归零前 reset 不得进入维护区"
        assert sqlite_scalar(runtime_paths.database, (
            "SELECT count(*) FROM system_config WHERE key='inflight'")) == 1

    await asyncio.wait_for(reset_task, timeout=10)
    assert await _system_config_value(runtime_db, "inflight") is None
    assert await _system_config_value(runtime_db, "marker") is None


@pytest.mark.asyncio
async def test_new_session_blocks_during_maintenance_then_reads_new_db(
        runtime_db, monkeypatch):
    """维护开始后的新会话阻塞到重绑完成，随后读到新库（而非旧 inode
    或未绑定错误）。"""
    entered, release = asyncio.Event(), asyncio.Event()
    _pause_upgrade(monkeypatch, entered, release)
    reset_task = asyncio.create_task(backups.reset_database())
    await asyncio.wait_for(entered.wait(), timeout=10)

    async def probe():
        return await _system_config_value(runtime_db, "marker")

    probe_task = asyncio.create_task(probe())
    await asyncio.sleep(0.1)
    assert not probe_task.done(), "维护期间新会话必须阻塞"

    release.set()
    await asyncio.wait_for(reset_task, timeout=10)
    assert await asyncio.wait_for(probe_task, timeout=10) is None, \
        "重绑完成后新会话必须读到 fresh 库"


@pytest.mark.asyncio
async def test_scheduler_session_waits_and_next_round_blocks(
        runtime_db, runtime_paths, monkeypatch):
    """在途调度式会话（run_due_once 同步单项）被等待；维护期间下一轮
    调度的新会话阻塞到重绑。"""
    import app.services.subscription as subscription_service
    from app.services.subscription_scheduler import SubscriptionScheduler

    async with _tracked_session(runtime_db) as db:
        db.add(Subscription(name="S", url="https://sub.example.com/f",
                            enabled=True, auto_update=True,
                            update_interval=3600, next_update_at=None))
        await db.commit()
    # 直写 SQL 排期为已到期（绕开 ORM 层排期计算）
    conn = sqlite3.connect(runtime_db.path)
    with conn:
        conn.execute(
            "UPDATE subscriptions SET next_update_at = '2000-01-01 00:00:00'")
    conn.close()

    fetch_entered, fetch_release = asyncio.Event(), asyncio.Event()

    async def pausing_fetch(url):
        fetch_entered.set()
        await fetch_release.wait()
        return [{"name": "Node", "node_type": "trojan", "server": "1.1.1.1",
                 "port": 443, "config": "Node = trojan, 1.1.1.1, 443, password=x"}]

    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        pausing_fetch)
    scheduler = SubscriptionScheduler(runtime_db,
                                      runtime_paths.root / "sched.lock")
    sync_task = asyncio.create_task(scheduler.run_due_once())
    await asyncio.wait_for(fetch_entered.wait(), timeout=10)

    entered, release = asyncio.Event(), asyncio.Event()
    _pause_upgrade(monkeypatch, entered, release)
    reset_task = asyncio.create_task(backups.reset_database())
    await asyncio.sleep(0.1)
    assert not reset_task.done(), "在途调度式会话未结束前 reset 不得越过"

    next_round = asyncio.create_task(scheduler.run_due_once())
    await asyncio.sleep(0.1)

    fetch_release.set()
    await asyncio.wait_for(sync_task, timeout=10)
    assert not next_round.done(), "维护期间下一轮调度必须阻塞"
    release.set()
    await asyncio.wait_for(reset_task, timeout=10)
    results = await asyncio.wait_for(next_round, timeout=10)
    assert results == [], "重绑后的新库无订阅，下一轮调度空转"


@pytest.mark.asyncio
async def test_restore_and_reset_are_mutually_exclusive(
        runtime_db, monkeypatch):
    """restore 与 reset 整个维护流程互斥：一个在维护区内时另一个
    阻塞在屏障外（不产生任何文件副作用）。"""
    info = await backups.create_backup("manual")
    entered, release = asyncio.Event(), asyncio.Event()
    _pause_upgrade(monkeypatch, entered, release)
    reset_task = asyncio.create_task(backups.reset_database())
    await asyncio.wait_for(entered.wait(), timeout=10)

    restore_task = asyncio.create_task(backups.restore_backup(info.name))
    await asyncio.sleep(0.1)
    assert not restore_task.done(), "reset 持有维护屏障时 restore 必须等待"
    assert not any(item.reason == "pre_restore"
                   for item in backups.list_backups()), \
        "等待中的 restore 不得产生任何文件副作用"

    release.set()
    await asyncio.wait_for(reset_task, timeout=10)
    await asyncio.wait_for(restore_task, timeout=10)
    assert any(item.reason == "pre_restore" for item in backups.list_backups())


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["failure", "cancel"])
async def test_maintenance_gate_released_on_failure_or_cancel(
        runtime_db, monkeypatch, mode):
    """维护失败/取消后屏障必须放行：新会话立即可用并读到原 live。"""
    if mode == "failure":
        async def broken(staging):
            raise RuntimeError("boom")
        monkeypatch.setattr(database_migration, "upgrade_staging", broken)
        task = asyncio.create_task(backups.reset_database())
        with pytest.raises(RuntimeError, match="boom"):
            await asyncio.wait_for(task, timeout=10)
    else:
        entered, release = asyncio.Event(), asyncio.Event()
        _pause_upgrade(monkeypatch, entered, release)
        task = asyncio.create_task(backups.reset_database())
        await asyncio.wait_for(entered.wait(), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)

    assert await _system_config_value(runtime_db, "marker") is not None, \
        "屏障释放后新会话必须立即可用并读到原 live"


@pytest.mark.asyncio
async def test_no_live_session_survives_replacement(
        runtime_db, runtime_paths):
    """replace 时刻不存在任何受控在途会话：持受控会话时 restore 在
    屏障外等待（live inode 不变）；会话归零（连接随后被 dispose 关闭）
    后 replace 才发生。

    绕过受控入口直连工厂的遗留连接在 replace 后提交会经路径 -wal
    污染新库（实验证实），因此生产会话必须全部走受控入口——由
    ``test_production_sessions_all_gated`` 静态守卫。"""
    live = runtime_paths.database
    info = await backups.create_backup("manual")
    inode_before = live.stat().st_ino

    async with _tracked_session(runtime_db) as db:
        db.add(SystemConfig(key="inflight2", value="1"))
        await db.commit()
        restore_task = asyncio.create_task(backups.restore_backup(info.name))
        await asyncio.sleep(0.1)
        assert not restore_task.done()
        assert live.stat().st_ino == inode_before, \
            "受控会话在途时绝不能发生 live 替换"

    await asyncio.wait_for(restore_task, timeout=10)
    assert live.stat().st_ino != inode_before, "会话归零后 replace 才完成"
    assert await _system_config_value(runtime_db, "inflight2") is None


def test_production_sessions_all_gated():
    """静态守卫：生产模块不得在 runtime 对象上直接取 ``.session_factory``
    开全局会话（唯一豁免：database_migration 在 staging 私有 runtime 上），
    全局会话一律走受控 ``session()`` 入口。

    只匹配属性访问（``xxx.session_factory``）；``seed_defaults`` /
    ``apply_data_migrations`` 的同名形参是 staging 私有 runtime 的
    工厂传递，不属于全局 runtime 直连。"""
    from pathlib import Path
    import app

    offenders = []
    for py in Path(app.__file__).parent.rglob("*.py"):
        if py.name in ("database.py", "database_migration.py"):
            continue
        for lineno, line in enumerate(py.read_text().splitlines(), 1):
            if ".session_factory" in line:
                offenders.append(
                    f"{py.relative_to(py.parents[2])}:{lineno}: {line.strip()}")
    assert offenders == [], f"生产代码绕过受控会话入口: {offenders}"


@pytest.mark.asyncio
async def test_get_db_blocks_instead_of_unbound_500(
        runtime_db, runtime_paths, monkeypatch):
    """维护期间 FastAPI 依赖入口 get_db 阻塞等待而非读到未绑定
    runtime 抛错/500；重绑完成后正常取得会话。"""
    from app.database import get_db

    entered, release = asyncio.Event(), asyncio.Event()
    _pause_upgrade(monkeypatch, entered, release)
    reset_task = asyncio.create_task(backups.reset_database())
    await asyncio.wait_for(entered.wait(), timeout=10)

    gen = get_db()
    anext_task = asyncio.create_task(gen.__anext__())
    await asyncio.sleep(0.1)
    assert not anext_task.done(), "维护期间 get_db 必须阻塞而非失败"

    release.set()
    await asyncio.wait_for(reset_task, timeout=10)
    session = await asyncio.wait_for(anext_task, timeout=10)
    try:
        value = (await session.execute(
            select(SystemConfig.value).where(SystemConfig.key == "marker"))
        ).scalar_one_or_none()
        assert value is None, "重绑后 get_db 会话必须读到 fresh 库"
    finally:
        await gen.aclose()


# ===== Fix Round 1：嵌套受控会话、bind 失败恢复、真实 reader busy =====


@pytest.mark.asyncio
async def test_load_access_settings_reuses_caller_session_under_gate(
        runtime_db, monkeypatch):
    """update_access 场景：外层受控会话在途、维护已关闸时，传入 db
    的 load_access_settings 直接用外层会话完成——不再入闸开第二个
    受控会话（否则与维护屏障循环等待死锁）。"""
    from app.security import load_access_settings

    entered, release = asyncio.Event(), asyncio.Event()
    _pause_upgrade(monkeypatch, entered, release)
    # 端点真实形态：get_db 会话先在途，维护随后关闸等待其归零
    async with runtime_db.session() as req_db:
        reset_task = asyncio.create_task(backups.reset_database())
        for _ in range(1000):
            if runtime_db._maintenance:
                break
            await asyncio.sleep(0)
        assert runtime_db._maintenance, "reset 应已关闸等待在途会话归零"

        settings = await asyncio.wait_for(
            load_access_settings(req_db), timeout=3)
        assert settings.installation_role in ("work_computer", "home_gateway")
        assert settings.password_configured is False

        release.set()

    await asyncio.wait_for(reset_task, timeout=10)


def _flaky_bind_after_publish(monkeypatch, runtime, fail_times=1):
    """注入 bind 前 fail_times 次失败；返回计数器。"""
    real_bind = runtime.bind
    counter = {"n": 0}

    def flaky_bind(path):
        counter["n"] += 1
        if counter["n"] <= fail_times:
            raise RuntimeError("bind boom")
        return real_bind(path)

    monkeypatch.setattr(runtime, "bind", flaky_bind)
    return counter


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["restore", "reset"])
async def test_publish_then_bind_failure_recovers_previous_live(
        runtime_db, runtime_paths, monkeypatch, operation):
    """publish 成功但 bind 失败：从 safety 经独立 reserved staging +
    integrity + 权限 + 原子 replace 恢复旧 live 一致内容并重绑（含仅
    WAL 已提交记录）；异常仍传播（不伪装成功），屏障随后放行。"""
    live = runtime_paths.database
    info = await backups.create_backup("manual") if operation == "restore" else None
    conn = _write_wal_only_record(live)
    try:
        _assert_record_only_in_wal(live)
        _flaky_bind_after_publish(monkeypatch, runtime_db, fail_times=1)

        if operation == "restore":
            with pytest.raises(RuntimeError, match="bind boom"):
                await backups.restore_backup(info.name)
        else:
            with pytest.raises(RuntimeError, match="bind boom"):
                await backups.reset_database()

        # 旧 live 一致内容已恢复：marker 与仅 WAL 已提交记录均可读
        assert await _system_config_value(runtime_db, "marker") is not None
        assert sqlite_scalar(live, (
            "SELECT count(*) FROM system_config WHERE key='wal_only'")) == 1
        assert sqlite_scalar(live, "PRAGMA integrity_check") == "ok"
        # runtime 已重绑、屏障已放行
        assert runtime_db.engine is not None
        assert runtime_db._maintenance is False
    finally:
        conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["restore", "reset"])
async def test_bind_always_failing_keeps_failure_visible(
        runtime_db, runtime_paths, monkeypatch, operation):
    """bind 恒败（恢复/二次 bind 也失败）：原异常保留传播，runtime 保持
    未绑定——绝不伪装成功。"""
    info = await backups.create_backup("manual") if operation == "restore" else None
    _flaky_bind_after_publish(monkeypatch, runtime_db, fail_times=10**6)

    if operation == "restore":
        with pytest.raises(RuntimeError, match="bind boom"):
            await backups.restore_backup(info.name)
    else:
        with pytest.raises(RuntimeError, match="bind boom"):
            await backups.reset_database()

    assert runtime_db.engine is None, "恢复失败后不得伪装已绑定"
    assert runtime_db._maintenance is False, "屏障必须放行（等待无意义）"
    with pytest.raises(RuntimeError, match="not bound"):
        async with runtime_db.session() as db:
            await db.execute(select(SystemConfig).limit(1))


@pytest.mark.asyncio
async def test_real_reader_blocks_checkpoint_fail_closed(
        runtime_db, runtime_paths):
    """真实 sqlite3 reader 持读快照（BEGIN + SELECT 未结束）：restore 的
    TRUNCATE checkpoint 等待 busy 后 fail closed——拒绝清理 sidecar、
    restore 失败、仅 WAL 已提交记录仍可查（真实并发语义，非 mock）。"""
    live = runtime_paths.database
    info = await backups.create_backup("manual")
    conn = _write_wal_only_record(live)
    try:
        _assert_record_only_in_wal(live)
        reader = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
        try:
            reader.execute("BEGIN")
            reader.execute("SELECT count(*) FROM system_config").fetchone()

            with pytest.raises(RuntimeError, match="checkpoint"):
                await backups.restore_backup(info.name)

            # sidecar 未被清理，WAL 已提交记录从 WAL 恢复可读
            assert (live.parent / (live.name + "-wal")).exists()
            assert sqlite_scalar(live, (
                "SELECT count(*) FROM system_config WHERE key='wal_only'")) == 1
            assert sqlite_scalar(live, "PRAGMA integrity_check") == "ok"
            # runtime 已重绑原 live（发布链失败路径）
            assert await _system_config_value(runtime_db, "marker") is not None
        finally:
            reader.close()
    finally:
        conn.close()
