"""数据库备份生命周期：原子备份、保留策略、恢复与重置的唯一入口。

备份/还原原语（``sqlite_backup`` / ``backup_database``）供
``app.services.database_migration`` 复用，不另立第二套复制逻辑；
恢复与重置经 staging 完成全部 Alembic 与业务迁移后原子替换 live，
任何失败都重新绑定原 live 库并向上传播异常（不返回半成功）。
"""

import asyncio
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from secrets import token_hex

from app.runtime_paths import (RuntimePaths, assert_regular_file,
                               refresh_runtime_permissions,
                               resolve_runtime_paths)

# 自动/手动备份统一保留最近 10 份（迁移前备份同样纳入该策略，消除无限累积）
BACKUP_KEEP = 10

# surge_conf_<UTC 时间戳>_<原因>[__序号].db；文件名字典序即时间序
_BACKUP_NAME_RE = re.compile(r"^surge_conf_(\d{8}T\d{6}\d{3}Z)_([a-z0-9_-]+?)(?:__(\d+))?\.db$")


@dataclass(frozen=True)
class BackupInfo:
    """备份元数据；``path`` 仅服务端使用，绝不进入 API 响应。"""

    name: str
    created_at: str  # ISO 8601（UTC）
    reason: str
    size: int
    schema_version: str  # 备份时的 alembic 版本；无版本表记 pre_alembic
    path: Path

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "created_at": self.created_at,
            "reason": self.reason,
            "size": self.size,
            "schema_version": self.schema_version,
        }


# ===== 备份原语（database_migration 复用） =====


def sqlite_backup(source: Path, target: Path) -> None:
    """SQLite Backup API 复制（源以只读 URI 打开，正确处理 WAL 页级一致性）。

    打开源之前先做 lstat 校验：symlink/非普通文件 fail closed。
    """
    assert_regular_file(source)
    source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    target_conn = sqlite3.connect(target)
    try:
        with target_conn:
            source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()


def _sanitize_tag(reason: str) -> str:
    tag = re.sub(r"[^a-z0-9_-]+", "_", reason.strip().lower()).strip("_-")
    return (tag or "manual")[:40]


def _utc_timestamp() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond // 1000:03d}Z"


def _parse_created_at(timestamp: str) -> str:
    moment = datetime.strptime(timestamp, "%Y%m%dT%H%M%S%fZ")
    return moment.replace(tzinfo=timezone.utc).isoformat()


def unique_path(directory: Path, base: str, suffix: str) -> Path:
    """目录内唯一的发布路径；同毫秒冲突时追加 __序号（时间序保持稳定）。"""
    candidate = directory / f"{base}{suffix}"
    seq = 2
    while candidate.exists():
        candidate = directory / f"{base}__{seq}{suffix}"
        seq += 1
    return candidate


def _finalize_backup_file(tmp: Path) -> None:
    """落盘为普通（非 WAL）模式并做完整性校验，保证备份文件独立可读。"""
    conn = sqlite3.connect(tmp)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.commit()
    finally:
        conn.close()
    if result[0] != "ok":
        raise RuntimeError(f"备份完整性检查失败: {result[0]}")


def backup_database(source: Path, paths: RuntimePaths, schema_tag: str,
                    protected: set[str] | frozenset[str] = frozenset()) -> Path:
    """一致性备份并原子发布到 ``paths.backups``，随后执行保留策略。

    ``protected`` 为本次 prune 不可删除的文件名集合（恢复流程保护
    点名备份）。临时文件 → integrity_check → 0600 → ``os.replace``；
    备份前先安全重建目录并收敛既有产物权限（启动后目录可能被替换）。
    失败不产生半成品，也不影响既有备份。
    """
    refresh_runtime_permissions(paths)
    target = unique_path(paths.backups,
                         f"surge_conf_{_utc_timestamp()}_{_sanitize_tag(schema_tag)}", ".db")
    tmp = paths.backups / f".{target.name}.tmp-{os.getpid()}-{token_hex(4)}"
    try:
        sqlite_backup(source, tmp)
        _finalize_backup_file(tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    prune_backups(paths, protected=protected)
    return target


def prune_backups(paths: RuntimePaths, keep: int = BACKUP_KEEP,
                  protected: set[str] | frozenset[str] = frozenset()) -> None:
    """从最旧的未受保护项开始删除，直到总数 <= ``keep``。

    受保护项（如恢复中点名的备份）跳过不删；若可删项不足则允许
    暂时超量——恢复流程的正确性优先于硬上限。普通备份不传
    ``protected``，保留策略不变。
    """
    if not paths.backups.is_dir():
        return
    names = sorted(entry.name for entry in paths.backups.iterdir()
                   if entry.is_file() and _BACKUP_NAME_RE.match(entry.name))
    excess = len(names) - keep
    for name in names:
        if excess <= 0:
            break
        if name in protected:
            continue
        (paths.backups / name).unlink(missing_ok=True)
        excess -= 1


# ===== 备份生命周期入口（管理端点消费） =====


def list_backups() -> list[BackupInfo]:
    """列出全部备份（时间升序）；schema 版本只读解析，不含绝对路径。"""
    paths = resolve_runtime_paths()
    if not paths.backups.is_dir():
        return []
    # 延迟导入：database_migration 顶层依赖本模块，避免循环导入
    from app.services.database_migration import alembic_version

    items: list[BackupInfo] = []
    for entry in sorted(paths.backups.iterdir(), key=lambda p: p.name):
        match = _BACKUP_NAME_RE.match(entry.name)
        if not match or not entry.is_file():
            continue
        items.append(BackupInfo(
            name=entry.name,
            created_at=_parse_created_at(match.group(1)),
            reason=match.group(2),
            size=entry.stat().st_size,
            schema_version=alembic_version(entry) or "pre_alembic",
            path=entry,
        ))
    return items


async def create_backup(reason: str) -> BackupInfo:
    """对 live 库做一致性备份（阻塞的 SQLite 复制卸载到线程池）。"""
    paths = resolve_runtime_paths()
    assert_regular_file(paths.database)
    if not paths.database.exists():
        raise FileNotFoundError("live 数据库不存在，无法备份")
    published = await asyncio.to_thread(backup_database, paths.database, paths, reason)
    for item in list_backups():
        if item.path == published:
            return item
    raise RuntimeError("备份已发布但未出现在备份列表中")


def _rebind_and_restore_live(paths: RuntimePaths, safety: Path | None) -> None:
    """失败路径：live 缺失时从安全备份还原，随后重新绑定 live 运行时。"""
    # 函数内导入保证测试 patch app.database.database 生效（security.py 先例）
    from app.database import database

    if not paths.database.exists() and safety is not None and safety.exists():
        sqlite_backup(safety, paths.database)
        os.chmod(paths.database, 0o600)
    if paths.database.exists():
        database.bind(paths.database)


async def restore_backup(name: str) -> None:
    """以选中备份替换 live：先强制备份当前数据，staging 完成全部
    Alembic/业务迁移/integrity 后原子替换并重绑。

    名字不在 ``BackupInfo.name`` 白名单内直接拒绝；任何失败都
    重新绑定原 live 库（缺失时从 pre_restore 备份还原）并抛错。
    """
    from app.database import database
    from app.services.database_migration import (discard_staging,
                                                 rebuild_from_source,
                                                 staging_path)

    paths = resolve_runtime_paths()
    refresh_runtime_permissions(paths)
    selected = next((item for item in list_backups() if item.name == name), None)
    if selected is None:
        raise LookupError(f"备份不存在: {name}")

    # pre_restore 发布触发 prune：保护点名备份，否则满额时最旧的
    # selected 恰好会被删除，恢复随即失败且点名备份永久丢失
    safety = backup_database(paths.database, paths, "pre_restore",
                             protected={selected.name})
    staging = staging_path(paths)
    try:
        # 必须先 await dispose：同步 dispose 无法可靠关闭 aiosqlite 线程连接
        await database.dispose()
        await rebuild_from_source(selected.path, paths)
    except BaseException:
        discard_staging(staging)
        _rebind_and_restore_live(paths, safety)
        raise
    database.bind(paths.database)


async def reset_database() -> None:
    """重置：dispose → 强制备份（纳入保留策略）→ 删除 live → Alembic
    在 staging 全新构建后原子发布 → 重绑。

    任何失败都先从备份副本还原 live 并重新绑定，再向上传播异常，
    避免运行时停留在"引擎存活但文件已删"或未绑定状态。
    """
    from app.database import database
    from app.services.database_migration import PROJECT_ROOT, prepare_database

    paths = resolve_runtime_paths()
    refresh_runtime_permissions(paths)
    safety: Path | None = None
    try:
        await database.dispose()
        if paths.database.exists():
            safety = backup_database(paths.database, paths, "pre_reset")
        # WAL 引入后需连同伴生文件一并删除，否则残留 -wal/-shm 会污染重建的新库
        for suffix in ("", "-wal", "-shm"):
            candidate = paths.database.with_name(paths.database.name + suffix)
            if candidate.exists():
                candidate.unlink()
        await prepare_database(paths, PROJECT_ROOT / "data" / "surge_conf.db")
    except BaseException:
        _rebind_and_restore_live(paths, safety)
        raise
    database.bind(paths.database)
