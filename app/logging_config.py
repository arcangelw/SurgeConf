"""结构化事件日志：专用 logger、私有轮转文件与字段级 allowlist。

安全契约：
- 事件日志只经专用 logger ``surgeconf.events`` 落盘，由
  ``PrivateRotatingFileHandler``（默认 5 MB × 3 备份，UTF-8）写入
  ``RuntimePaths.log_file``；日志目录 0700、文件与每次轮转后的
  新主文件 0600。``RuntimePaths.log_file`` 是该文件唯一 writer，
  launchd/nohup 的 stdout/stderr 重定向必须指向独立的
  ``surgeconf-console.log``（见 docs/RUNTIME_OPERATIONS.md），
  避免轮转 rename 后旧 FD 继续写已轮换 inode。
- ``log_event`` 的键与值全部强校验：URL、节点 config、凭据、设备名、
  异常文本等自由字符串没有任何合法入口——unknown 键/事件/值立即
  ValueError（fail closed，开发期暴露）。
- 行格式确定性：``ts=<UTC ISO8601> event=<name> k=v ...
  schema_version=1``，键按固定顺序、counts 键按字典序，无 exc_info、
  无多行值、无异常消息或类型名。
"""

import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.profile_targets import ProfileTarget
from app.runtime_paths import (_converge_managed_directory,
                               _ensure_directory_safe, assert_regular_file)

EVENT_LOGGER_NAME = "surgeconf.events"
LOG_SCHEMA_VERSION = 1

DEFAULT_MAX_BYTES = 5_000_000
DEFAULT_BACKUP_COUNT = 3

# 事件名 allowlist（固定集合；新增事件必须同步 tests/test_logging_safety.py 锁定）
EVENT_NAMES = frozenset({
    "subscription_sync",       # 一次 SyncResult 完成（成功/失败各一条）
    "scheduler_active",        # 本实例获得调度锁，运行自动更新循环
    "scheduler_standby",       # 锁竞争失败，本实例只服务不循环
    "scheduler_cycle_error",   # 调度循环单轮意外异常（仅 internal_error）
    "scheduler_item_error",    # 单个订阅的调度同步意外异常
    "scheduler_stop_error",    # 停止循环时循环任务以异常退出
    "database_ready",          # 迁移/绑定完成，数据库可用
    "orphan_cleanup",          # 启动维护：孤儿节点清理计数
    "startup_failed",          # 启动阶段（迁移/绑定/维护/调度器）失败
    "service_shutdown",        # lifespan 正常收敛完成
})

# 业务日志字段 allowlist：与计划接口一致，只有这 6 个键可出现在事件里
ALLOWED_FIELDS = frozenset({
    "subscription_id", "success", "counts", "error_code",
    "schema_version", "profile_id", "target",
})

# counts 值键 allowlist（展开为独立 ``<key>=<n>`` 输出）
COUNT_KEYS = frozenset({
    "node_count", "inserted", "updated", "removed", "cleaned",
    "applied_migrations", "total", "failed",
})

# 错误码 allowlist：与 app.services.subscription.SYNC_ERROR_CODES 同口径，
# 测试锁定两集合相等防漂移
ERROR_CODES = frozenset({
    "timeout", "http_status", "invalid_content",
    "network_error", "internal_error",
})

# 合法 target：ProfileTarget 的值集合（枚举值或字符串均可传入）
_TARGET_VALUES = frozenset(t.value for t in ProfileTarget)

# 行内字段固定输出顺序（counts 键另按字典序追加在 error_code 之后）
_FIELD_ORDER = ("subscription_id", "profile_id", "success", "target",
                "error_code")


def _reject(field: str) -> None:
    """值校验失败统一抛静态消息 ValueError（不回显值本身）。"""
    raise ValueError(f"log_event field rejected: {field}")


def _validate_id(value: object, field: str) -> None:
    if type(value) is not int or value <= 0:
        _reject(field)


def _validate_bool(value: object, field: str) -> None:
    if type(value) is not bool:
        _reject(field)


def _validate_error_code(value: object, field: str) -> None:
    if value is not None and value not in ERROR_CODES:
        _reject(field)


def _validate_target(value: object, field: str) -> None:
    if value is not None and value not in _TARGET_VALUES:
        _reject(field)


def _validate_counts(value: object, field: str) -> None:
    if type(value) is not dict:
        _reject(field)
    for key, count in value.items():
        if key not in COUNT_KEYS:
            _reject(field)
        if type(count) is not int or count < 0:
            _reject(f"{field}.{key}")


_FIELD_VALIDATORS = {
    "subscription_id": _validate_id,
    "profile_id": _validate_id,
    "success": _validate_bool,
    "counts": _validate_counts,
    "error_code": _validate_error_code,
    "target": _validate_target,
    # schema_version 由渲染层固定写入，不接受调用方覆盖
}


def _validate_fields(event: str, fields: dict) -> None:
    if event not in EVENT_NAMES:
        raise ValueError("log_event event name rejected")
    unknown = set(fields) - ALLOWED_FIELDS
    if unknown or "schema_version" in fields:
        raise ValueError("log_event received non-allowlisted field(s)")
    for key, value in fields.items():
        _FIELD_VALIDATORS[key](value, key)


def _render_line(event: str, fields: dict) -> str:
    """确定性 key=value 行（单行、无异常文本）。"""
    parts = [
        "ts=" + datetime.now(timezone.utc).isoformat(
            timespec="milliseconds").replace("+00:00", "Z"),
        f"event={event}",
    ]
    for key in _FIELD_ORDER:
        if key not in fields:
            continue
        value = fields[key]
        if value is None:
            continue
        if key == "success":
            parts.append(f"{key}={'true' if value else 'false'}")
        else:
            parts.append(f"{key}={value}")
    counts = fields.get("counts")
    if counts:
        for count_key in sorted(counts):
            parts.append(f"{count_key}={counts[count_key]}")
    parts.append(f"schema_version={LOG_SCHEMA_VERSION}")
    return " ".join(parts)


def log_event(event: str, **fields) -> None:
    """记录一条结构化事件（唯一业务入口）。

    event 必须在 ``EVENT_NAMES``；字段必须全部来自
    ``ALLOWED_FIELDS`` 且通过各自的强校验（正整数 ID、bool、
    非负 int counts、allowlist 错误码/target），否则立即
    ValueError——URL、节点 config、凭据等没有任何合法写入口。
    """
    _validate_fields(event, fields)
    logger = logging.getLogger(EVENT_LOGGER_NAME)
    if logger.level == logging.NOTSET:
        # 未 configure（测试/工具上下文）也保证 INFO 可用；
        # propagate 保持默认 True，caplog/root 层可捕获安全字段
        logger.setLevel(logging.INFO)
    logger.info(_render_line(event, fields))


class PrivateRotatingFileHandler(RotatingFileHandler):
    """0600 私有轮转文件 handler。

    打开前对已存在目标 ``lstat`` 校验（symlink/FIFO/socket/目录一律
    fail closed）；每次 ``_open``（含 rollover 后重开主文件）对真实
    fd ``fchmod 0600``。轮转通过 rename 保备份文件的原 inode 权限，
    备份文件因此维持首次创建时的 0600。
    """

    def __init__(self, filename, *, maxBytes: int = DEFAULT_MAX_BYTES,
                 backupCount: int = DEFAULT_BACKUP_COUNT):
        path = Path(filename)
        assert_regular_file(path)  # 存在时：非 symlink 的普通文件
        super().__init__(path, mode="a", maxBytes=maxBytes,
                         backupCount=backupCount, encoding="utf-8",
                         delay=False)

    def _open(self):
        stream = super()._open()
        try:
            os.fchmod(stream.fileno(), 0o600)
        except BaseException:
            stream.close()
            raise
        return stream


def configure_event_logging(log_file, *,
                            max_bytes: int = DEFAULT_MAX_BYTES,
                            backup_count: int = DEFAULT_BACKUP_COUNT) -> None:
    """配置事件日志（幂等）。

    安全创建/收敛日志目录（逐组件 no-follow，managed 目录 0700）后
    建立唯一 ``PrivateRotatingFileHandler``。重复以同一路径调用为
    no-op（绝不叠加 handler）；以不同路径重复调用抛 RuntimeError
    （避免双文件双写）。
    """
    log_file = Path(log_file)
    logger = logging.getLogger(EVENT_LOGGER_NAME)
    absolute = os.path.abspath(log_file)
    for handler in logger.handlers:
        if isinstance(handler, PrivateRotatingFileHandler):
            if os.path.abspath(handler.baseFilename) == absolute:
                return  # 幂等：同路径不叠 handler
            raise RuntimeError(
                "event logging already configured for a different file")
    _ensure_directory_safe(log_file.parent)
    _converge_managed_directory(log_file.parent)
    handler = PrivateRotatingFileHandler(log_file, maxBytes=max_bytes,
                                         backupCount=backup_count)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def shutdown_event_logging() -> None:
    """关闭并移除全部事件 handler（幂等；启动失败路径同样安全）。"""
    logger = logging.getLogger(EVENT_LOGGER_NAME)
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
