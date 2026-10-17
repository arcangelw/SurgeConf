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
                               assert_sqlite_database_safe,
                               harden_private_file,
                               refresh_runtime_permissions, reserve_private_file,
                               resolve_runtime_paths,
                               validate_reserved_empty_file)

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


def sqlite_backup(source: Path, target: Path, *, reserved: bool = False) -> None:
    """SQLite Backup API 复制（源以只读 URI 打开，正确处理 WAL 页级一致性）。

    源在打开前做全链校验（普通文件 + 父链 + sidecar）fail closed；目标
    两种交付形态：
    - ``reserved=False``（默认）：``O_CREAT|O_EXCL|O_NOFOLLOW`` 严格
      预占新文件（已存在即失败）；
    - ``reserved=True``：目标为调用方本次预占的 0 字节 0600 普通文件，
      经 ``validate_reserved_empty_file`` 复核后才交给 SQLite。
    """
    assert_sqlite_database_safe(source)
    if reserved:
        validate_reserved_empty_file(target)
    else:
        reserve_private_file(target)   # O_CREAT|O_EXCL|O_NOFOLLOW 0600 预占位
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
        harden_private_file(tmp)
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
    """列出全部备份（时间升序）；schema 版本只读解析，不含绝对路径。

    先安全重建目录并收敛权限（防启动后目录替换），再逐项 lstat：
    仅白名单命名且为非 symlink 普通文件的条目可进入列表/下载。
    """
    paths = resolve_runtime_paths()
    refresh_runtime_permissions(paths)
    if not paths.backups.is_dir():
        return []
    # 延迟导入：database_migration 顶层依赖本模块，避免循环导入
    from app.services.database_migration import alembic_version

    items: list[BackupInfo] = []
    for entry in sorted(paths.backups.iterdir(), key=lambda p: p.name):
        match = _BACKUP_NAME_RE.match(entry.name)
        if not match or entry.is_symlink() or not entry.is_file():
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
    assert_sqlite_database_safe(paths.database)
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
        harden_private_file(paths.database)
    if paths.database.exists():
        database.bind(paths.database)


async def restore_backup(name: str) -> None:
    """以选中备份替换 live：先强制备份当前数据，staging 完成全部
    Alembic/业务迁移/integrity 后原子替换并重绑。

    整个「安全备份 → dispose → staging → publish → bind/失败恢复」
    在维护屏障内执行：与 API 请求、自动同步及其他维护互斥；在途
    受控会话先归零，新会话阻塞到重绑完成。名字不在 ``BackupInfo.name``
    白名单内直接拒绝；任何失败都重新绑定原 live 库（缺失时从
    pre_restore 备份还原）并抛错。
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

    async with database.maintenance():
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
    """重置：维护屏障内 dispose → 强制备份（纳入保留策略）→ 全新
    staging 构建（显式 fresh 入口，绝不走 legacy 自动发现，也不先删
    live——原子替换由发布链完成）→ 重绑。

    保留的旧仓库库文件保持原样不动；reset 后 live 只含 fresh 默认
    数据。任何失败都先从备份副本还原 live 并重新绑定，再向上传播
    异常，避免运行时停留在"引擎存活但文件已删"或未绑定状态。
    """
    from app.database import database
    from app.services.database_migration import rebuild_from_source

    paths = resolve_runtime_paths()
    refresh_runtime_permissions(paths)
    async with database.maintenance():
        safety: Path | None = None
        try:
            await database.dispose()
            if paths.database.exists():
                safety = backup_database(paths.database, paths, "pre_reset")
            # source=None：全新构建；绝不引用旧仓库库（prepare_database
            # 的 legacy 自动发现会把保留的旧库重新导入，破坏 reset 语义）
            await rebuild_from_source(None, paths)
        except BaseException:
            _rebind_and_restore_live(paths, safety)
            raise
        database.bind(paths.database)
