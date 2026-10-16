"""结构化事件日志安全测试（Task 5）。

覆盖：
- 秘密永不落盘：emit_sync_log 兼容 fixture 签名的 url/node_config/
  subscription_id 参数被立即丢弃，caplog 与轮转文件两侧都无
  URL/凭据/节点配置；
- log_event 强校验：unknown event/field、各字段类型与值域
  （正整数 ID、bool、counts 非负 int、错误码/target allowlist、
  拒绝任意对象与含控制字符的值）一律 ValueError；
- 成功/失败行的精确字段（确定性 key=value、UTC ts、schema_version）；
- configure 幂等（不叠 handler）、symlink/FIFO fail closed；
- 小 maxBytes 多轮 rotation 只保留 3 个备份、全部 0600、目录 0700、
  主文件继续可写；
- lifespan 启动失败仍按 scheduler stop → DB dispose → logging
  shutdown 收敛（专用 logger 无残留 handler，FD 已关闭）；
- 错误码 allowlist 与订阅服务 SYNC_ERROR_CODES 同口径防漂移。
"""

import logging
import os
import stat as stat_mod

import pytest

from app.logging_config import (ERROR_CODES, EVENT_LOGGER_NAME,
                                EVENT_NAMES, LOG_SCHEMA_VERSION,
                                configure_event_logging, log_event,
                                shutdown_event_logging)
from app.services.subscription import SYNC_ERROR_CODES, SyncResult, emit_sync_log


@pytest.fixture
def event_log(tmp_path):
    root = tmp_path.resolve()  # 避开 macOS /var symlink 的 no-follow 拒绝
    path = root / "logs" / "surgeconf.log"
    configure_event_logging(path)
    try:
        yield path
    finally:
        shutdown_event_logging()


def _failed_result(code="timeout"):
    return SyncResult(7, False, 3, 0, 0, 0, code, "静态失败文案")


def _ok_result():
    return SyncResult(7, True, 4, 2, 1, 1, None, "同步成功")


# ===== 秘密过滤 =====


class TestSecretsNeverLogged:
    def test_sync_log_does_not_contain_subscription_or_node_secrets(
            self, event_log, caplog):
        caplog.set_level(logging.INFO, logger=EVENT_LOGGER_NAME)
        emit_sync_log(
            subscription_id=7,
            url="https://synthetic-user:synthetic-secret@example.com/sub?token=synthetic",
            node_config="Example = trojan, edge.example.com, 443, password=synthetic",
            result=_failed_result("timeout"),
        )
        text = caplog.text
        assert "subscription_id=7" in text and "error_code=timeout" in text
        assert "secret" not in text and "password" not in text and "token=" not in text
        # 文件侧同样只有安全字段
        content = event_log.read_text()
        assert "synthetic" not in content
        assert "example.com" not in content
        assert "trojan" not in content
        assert "subscription_id=7" in content and "error_code=timeout" in content

    def test_success_emit_also_leaks_nothing(self, event_log):
        emit_sync_log(
            url="https://synthetic-user:synthetic-secret@example.com/sub",
            node_config="Example = trojan, edge.example.com, 443, password=synthetic",
            result=_ok_result(),
        )
        content = event_log.read_text()
        assert "synthetic" not in content and "trojan" not in content
        assert "success=true" in content


# ===== log_event 强校验 =====


class TestLogEventValidation:
    def test_unknown_event_rejected(self, event_log):
        with pytest.raises(ValueError):
            log_event("not_an_event")

    def test_unknown_field_rejected(self, event_log):
        with pytest.raises(ValueError):
            log_event("subscription_sync", url="https://example.com/sub")

    def test_url_and_node_config_directly_rejected(self, event_log):
        with pytest.raises(ValueError):
            log_event("subscription_sync",
                      url="https://synthetic-secret@example.com/sub",
                      node_config="Example = trojan, edge.example.com, 443, password=synthetic")

    @pytest.mark.parametrize("bad_id", [0, -1, True, "7", 3.5, None
                                        if False else object()])
    def test_non_positive_or_non_int_id_rejected(self, event_log, bad_id):
        with pytest.raises(ValueError):
            log_event("subscription_sync", subscription_id=bad_id)

    @pytest.mark.parametrize("bad", ["true", 1, 0, None if False else object()])
    def test_non_bool_success_rejected(self, event_log, bad):
        with pytest.raises(ValueError):
            log_event("subscription_sync", success=bad)

    @pytest.mark.parametrize("bad_counts", [
        {"node_count": -1},
        {"downloads": 1},
        {"node_count": True},
        {"node_count": 1.5},
        {"node_count": object()},
        {"node_count": "3"},
        ["node_count"],
        "node_count=3",
    ])
    def test_bad_counts_rejected(self, event_log, bad_counts):
        with pytest.raises(ValueError):
            log_event("subscription_sync", counts=bad_counts)

    @pytest.mark.parametrize("bad_code", [
        "arbitrary\ninjection", "timeout ", "TimeoutError",
        "errno 60", object(), 500,
    ])
    def test_bad_error_code_rejected(self, event_log, bad_code):
        with pytest.raises(ValueError):
            log_event("subscription_sync", error_code=bad_code)

    @pytest.mark.parametrize("bad_target", [
        "mac_workstation\n", "workstation", "MAC_WORKSTATION", 7, object(),
    ])
    def test_bad_target_rejected(self, event_log, bad_target):
        with pytest.raises(ValueError):
            log_event("subscription_sync", target=bad_target)

    def test_allowlists_locked_against_drift(self):
        # 与订阅服务对外错误码同口径，防两套 allowlist 漂移
        assert ERROR_CODES == SYNC_ERROR_CODES
        assert ERROR_CODES == frozenset({
            "timeout", "http_status", "invalid_content",
            "network_error", "internal_error"})
        assert EVENT_NAMES == frozenset({
            "subscription_sync", "scheduler_active", "scheduler_standby",
            "scheduler_cycle_error", "scheduler_item_error",
            "scheduler_stop_error", "database_ready", "orphan_cleanup",
            "startup_failed", "service_shutdown"})
        assert LOG_SCHEMA_VERSION == 1


# ===== 行格式（确定性 key=value）=====


class TestLineFormat:
    def _last_line(self, path):
        lines = path.read_text().strip().splitlines()
        assert lines, "事件日志为空"
        return lines[-1]

    def test_success_line_exact_fields(self, event_log):
        emit_sync_log(_ok_result())
        line = self._last_line(event_log)
        assert "\n" not in line and "\r" not in line
        tokens = dict(token.split("=", 1) for token in line.split())
        assert set(tokens) == {"ts", "event", "subscription_id", "success",
                               "node_count", "inserted", "updated", "removed",
                               "schema_version"}
        assert tokens["event"] == "subscription_sync"
        assert tokens["subscription_id"] == "7"
        assert tokens["success"] == "true"
        assert tokens["node_count"] == "4"
        assert tokens["inserted"] == "2"
        assert tokens["updated"] == "1"
        assert tokens["removed"] == "1"
        assert tokens["schema_version"] == "1"
        # UTC ISO8601 毫秒 + Z
        assert tokens["ts"].endswith("Z") and "T" in tokens["ts"]

    def test_failure_line_exact_fields(self, event_log):
        emit_sync_log(_failed_result("http_status"))
        line = self._last_line(event_log)
        tokens = dict(token.split("=", 1) for token in line.split())
        assert tokens["success"] == "false"
        assert tokens["error_code"] == "http_status"
        assert tokens["inserted"] == "0" and tokens["updated"] == "0"
        assert tokens["removed"] == "0" and tokens["node_count"] == "3"

    def test_node_count_none_omitted(self, event_log):
        result = SyncResult(9, False, None, 0, 0, 0, "network_error", "静态")
        emit_sync_log(result)
        tokens = dict(token.split("=", 1)
                      for token in self._last_line(event_log).split())
        assert "node_count" not in tokens
        assert tokens["subscription_id"] == "9"

    def test_scheduler_and_lifecycle_events_render(self, event_log):
        log_event("database_ready", counts={"applied_migrations": 2})
        log_event("orphan_cleanup", counts={"removed": 0})
        log_event("scheduler_active")
        log_event("startup_failed", error_code="internal_error")
        content = event_log.read_text()
        assert "event=database_ready" in content
        assert "applied_migrations=2" in content
        assert "event=orphan_cleanup" in content and "removed=0" in content
        assert "event=scheduler_active" in content
        assert "event=startup_failed" in content
        assert "error_code=internal_error" in content


# ===== configure/rotation/收敛 =====


class TestConfigureAndRotation:
    def test_configure_idempotent_no_duplicate_handlers(self, event_log):
        configure_event_logging(event_log)
        configure_event_logging(event_log)
        logger = logging.getLogger(EVENT_LOGGER_NAME)
        assert len(logger.handlers) == 1

    def test_directory_0700_and_file_0600(self, event_log):
        log_event("database_ready", counts={"applied_migrations": 0})
        assert stat_mod.S_IMODE(
            os.stat(event_log.parent).st_mode) == 0o700
        assert stat_mod.S_IMODE(os.stat(event_log).st_mode) == 0o600
        # 跨进程轮转锁文件同样 0600 普通文件
        lock = event_log.parent / "surgeconf.log.lock"
        assert stat_mod.S_IMODE(os.stat(lock).st_mode) == 0o600

    def test_symlink_log_file_rejected(self, tmp_path):
        root = tmp_path.resolve()
        logs = root / "logs"
        logs.mkdir(parents=True)
        target = logs / "real.log"
        target.write_text("decoy")
        link = logs / "surgeconf.log"
        link.symlink_to(target)
        with pytest.raises(RuntimeError):
            configure_event_logging(link)
        assert logging.getLogger(EVENT_LOGGER_NAME).handlers == []
        shutdown_event_logging()  # 幂等清理

    def test_fifo_log_file_rejected(self, tmp_path):
        root = tmp_path.resolve()
        logs = root / "logs"
        logs.mkdir(parents=True)
        fifo = logs / "surgeconf.log"
        os.mkfifo(fifo)
        with pytest.raises(RuntimeError):
            configure_event_logging(fifo)
        assert logging.getLogger(EVENT_LOGGER_NAME).handlers == []
        shutdown_event_logging()  # 幂等清理

    def test_rotation_keeps_three_backups_all_0600(self, tmp_path):
        root = tmp_path.resolve()
        path = root / "logs" / "surgeconf.log"
        configure_event_logging(path, max_bytes=400, backup_count=3)
        try:
            for i in range(60):  # 触发远多于 3 轮 rotation
                log_event("subscription_sync", subscription_id=i + 1,
                          success=True,
                          counts={"node_count": 5, "inserted": 1})
            backups = sorted(
                p for p in path.parent.glob("surgeconf.log.*")
                if p.name.rsplit(".", 1)[-1].isdigit())
            assert [b.name for b in backups] == [
                "surgeconf.log.1", "surgeconf.log.2", "surgeconf.log.3"]
            for f in [path, *backups]:
                assert stat_mod.S_IMODE(os.stat(f).st_mode) == 0o600
            # 主文件在多轮 rotation 后继续可写
            log_event("scheduler_standby")
            assert "event=scheduler_standby" in path.read_text()
        finally:
            shutdown_event_logging()

    def test_shutdown_and_fd_hygiene(self, event_log):
        """新模型无跨 emit 常驻 stream：每次写入的 fd 即开即关，
        shutdown/close 后不再写。用 /dev/fd 计数证明无 FD 泄漏。"""
        logger = logging.getLogger(EVENT_LOGGER_NAME)
        handler = logger.handlers[0]
        log_event("database_ready", counts={"applied_migrations": 0})

        def _fd_count() -> int:
            return len(os.listdir("/dev/fd"))

        before = _fd_count()
        for i in range(5):
            log_event("subscription_sync", subscription_id=i + 1,
                      success=True, counts={"inserted": 1})
        assert _fd_count() == before  # 无 FD 泄漏（每轮 lock/base fd 已关）

        shutdown_event_logging()
        assert logger.handlers == []
        assert handler._closed is True
        size = event_log.stat().st_size
        log_event("scheduler_active")  # handler 已摘除，不落盘
        assert event_log.stat().st_size == size
        shutdown_event_logging()  # 幂等

    def test_base_replaced_by_symlink_between_emits_fails_closed(
            self, event_log, capsys):
        """emit 之间 base 被替换为 symlink：后续写入 fail closed，
        绝不跟随 symlink 写入目标文件。"""
        log_event("database_ready", counts={"applied_migrations": 0})
        event_log.unlink()
        decoy = event_log.parent / "decoy.log"
        decoy.write_text("sentinel")
        event_log.symlink_to(decoy)
        log_event("scheduler_active")  # O_NOFOLLOW 拒绝 → 吞进 handleError
        assert decoy.read_text() == "sentinel"  # 未写入 symlink 目标
        assert not event_log.exists() or event_log.is_symlink()


# ===== lifespan 收敛 =====


@pytest.mark.asyncio
async def test_lifespan_startup_failure_shuts_down_logging(
        tmp_path, monkeypatch):
    root = tmp_path.resolve()
    monkeypatch.setenv("SURGECONF_DATA_DIR", str(root / "data"))
    monkeypatch.setenv("HOME", str(root / "home"))

    from app import main as main_module

    async def _boom(*args, **kwargs):
        raise RuntimeError("迁移失败（静态测试文本）")

    monkeypatch.setattr(
        "app.services.database_migration.prepare_database", _boom)
    with pytest.raises(RuntimeError):
        async with main_module.app.router.lifespan_context(
                main_module.app):
            pass
    # 收敛顺序的最后一步：logging shutdown（无残留 handler、FD 已关）
    assert logging.getLogger(EVENT_LOGGER_NAME).handlers == []
    # 启动失败事件已落盘且不含异常文本
    log_path = (root / "home" / "Library" / "Logs" / "SurgeConf"
                / "surgeconf.log")
    content = log_path.read_text()
    assert "event=startup_failed" in content
    assert "error_code=internal_error" in content
    assert "迁移失败" not in content


# ===== 双进程真实轮转竞争（Fix Round 1）=====

_WRITER_SOURCE = '''
import sys
sys.path.insert(0, {repo!r})
from app.logging_config import configure_event_logging, log_event
log_file, max_bytes, count, base_id = (
    sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]))
configure_event_logging(log_file, max_bytes=max_bytes, backup_count=3)
for i in range(count):
    log_event("subscription_sync", subscription_id=base_id + i,
              success=True, counts={{"node_count": 5}})
'''


def test_multiprocess_rotation_no_loss_no_duplication(tmp_path):
    """两个真实进程并发写同一事件日志并跨阈值多轮轮转。

    总数据量 ≤ base + 3 备份容量：全部唯一事件应恰出现一次，
    无丢失/重复/半行；备份 ≤ 3；全部文件 0600。"""
    import subprocess
    import sys as _sys
    from pathlib import Path as _Path
    repo = str(_Path(__file__).resolve().parent.parent)
    root = tmp_path.resolve()
    path = root / "logs" / "surgeconf.log"
    # 预建日志目录（消除两子进程并发首建目录的竞争；轮转竞争仍真实）
    configure_event_logging(path, max_bytes=2500, backup_count=3)
    shutdown_event_logging()
    writer = root / "writer.py"
    writer.write_text(_WRITER_SOURCE.format(repo=repo))
    py = str(_Path(repo) / "venv/bin/python")
    max_bytes, count = 2500, 40  # 80 行 ≈ 8.6KB ≤ 4×2500 容量
    procs = [
        subprocess.Popen([py, str(writer), str(path),
                          str(max_bytes), str(count), "1"]),
        subprocess.Popen([py, str(writer), str(path),
                          str(max_bytes), str(count), "1001"]),
    ]
    for proc in procs:
        assert proc.wait(timeout=90) == 0
    backups = sorted(p for p in path.parent.glob("surgeconf.log.*")
                     if p.name.rsplit(".", 1)[-1].isdigit())
    assert 1 <= len(backups) <= 3
    files = [path, *backups]
    lines = []
    for f in files:
        content = f.read_text()
        assert content.endswith("\n")  # 无截断半行
        lines.extend(ln for ln in content.splitlines() if ln.strip())
    ids = sorted(int(ln.split("subscription_id=", 1)[1].split(" ", 1)[0])
                 for ln in lines if "subscription_id=" in ln)
    assert ids == list(range(1, 41)) + list(range(1001, 1041))  # 恰一次
    assert all(ln.startswith("ts=") and ln.endswith("schema_version=1")
               for ln in lines)
    for f in files + [path.parent / "surgeconf.log.lock"]:
        assert stat_mod.S_IMODE(os.stat(f).st_mode) == 0o600


def _lifespan_env(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    monkeypatch.setenv("SURGECONF_DATA_DIR", str(root / "data"))
    monkeypatch.setenv("HOME", str(root / "home"))
    return root / "home" / "Library" / "Logs" / "SurgeConf" / "surgeconf.log"


@pytest.mark.asyncio
async def test_lifespan_harden_failure_full_cleanup(tmp_path, monkeypatch):
    """首次 harden 在边界内失败：startup_failed 落盘、原始异常传播、
    dispose 与 logging shutdown 仍执行（handler 无残留）。"""
    log_path = _lifespan_env(tmp_path, monkeypatch)

    def _boom(*args, **kwargs):
        raise RuntimeError("harden 失败（静态测试文本）")

    monkeypatch.setattr("app.runtime_paths.harden_runtime_permissions", _boom)
    from app import main as main_module
    with pytest.raises(RuntimeError):
        async with main_module.app.router.lifespan_context(main_module.app):
            pass
    assert logging.getLogger(EVENT_LOGGER_NAME).handlers == []
    content = log_path.read_text()
    assert "event=startup_failed" in content
    assert "harden 失败" not in content


@pytest.mark.asyncio
async def test_lifespan_stop_failure_propagates_first_and_shutdown_runs(
        tmp_path, monkeypatch):
    """正常服务后 stop 抛错：无原始异常时传播首个清理异常，
    logging shutdown 仍执行。"""
    log_path = _lifespan_env(tmp_path, monkeypatch)

    async def _boom_stop(self):
        raise RuntimeError("stop 失败（静态测试文本）")

    monkeypatch.setattr(
        "app.services.subscription_scheduler.SubscriptionScheduler.stop",
        _boom_stop)
    from app import main as main_module
    with pytest.raises(RuntimeError, match="stop 失败"):
        async with main_module.app.router.lifespan_context(main_module.app):
            pass
    assert logging.getLogger(EVENT_LOGGER_NAME).handlers == []
    assert "event=service_shutdown" in log_path.read_text()


@pytest.mark.asyncio
async def test_lifespan_dispose_failure_still_shuts_down_logging(
        tmp_path, monkeypatch):
    log_path = _lifespan_env(tmp_path, monkeypatch)

    async def _boom_dispose():
        raise RuntimeError("dispose 失败（静态测试文本）")

    from app.database import database as database_singleton
    monkeypatch.setattr(database_singleton, "dispose", _boom_dispose)
    from app import main as main_module
    with pytest.raises(RuntimeError, match="dispose 失败"):
        async with main_module.app.router.lifespan_context(main_module.app):
            pass
    assert logging.getLogger(EVENT_LOGGER_NAME).handlers == []
    assert "event=service_shutdown" in log_path.read_text()


@pytest.mark.asyncio
async def test_lifespan_combined_cleanup_failures_first_wins(
        tmp_path, monkeypatch):
    """stop 与 dispose 双失败：无原始异常时传播首个（stop），
    logging shutdown 仍执行。"""
    log_path = _lifespan_env(tmp_path, monkeypatch)

    async def _boom_stop(self):
        raise RuntimeError("stop 失败（静态测试文本）")

    async def _boom_dispose():
        raise RuntimeError("dispose 失败（静态测试文本）")

    monkeypatch.setattr(
        "app.services.subscription_scheduler.SubscriptionScheduler.stop",
        _boom_stop)
    from app.database import database as database_singleton
    monkeypatch.setattr(database_singleton, "dispose", _boom_dispose)
    from app import main as main_module
    with pytest.raises(RuntimeError, match="stop 失败"):
        async with main_module.app.router.lifespan_context(main_module.app):
            pass
    assert logging.getLogger(EVENT_LOGGER_NAME).handlers == []
    assert "event=service_shutdown" in log_path.read_text()


@pytest.mark.asyncio
async def test_lifespan_request_error_not_masked_by_cleanup_failure(
        tmp_path, monkeypatch):
    """yield 期原始异常 + stop 失败：传播的是原始请求异常（不遮蔽）。"""
    _lifespan_env(tmp_path, monkeypatch)

    async def _boom_stop(self):
        raise RuntimeError("stop 失败（静态测试文本）")

    monkeypatch.setattr(
        "app.services.subscription_scheduler.SubscriptionScheduler.stop",
        _boom_stop)
    from app import main as main_module
    with pytest.raises(ValueError, match="请求期错误"):
        async with main_module.app.router.lifespan_context(main_module.app):
            raise ValueError("请求期错误（静态测试文本）")
    assert logging.getLogger(EVENT_LOGGER_NAME).handlers == []
