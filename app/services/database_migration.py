import os
import sqlite3
from argparse import Namespace
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.bootstrap import seed_defaults
from app.database import Base, DatabaseRuntime
from app.runtime_paths import RuntimePaths, ensure_runtime_directories
from app.services.data_migrations import DATA_MIGRATIONS, apply_data_migrations

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BASELINE_REVISION = "0001_current_schema"
STAGING_SUFFIX = ".staging"

# 与 migrations/versions/0001_current_schema.py 中 data_migration_records 定义保持一致
_DATA_MIGRATION_RECORDS_DDL = (
    "CREATE TABLE data_migration_records ("
    "key VARCHAR(100) NOT NULL, "
    "applied_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL, "
    "PRIMARY KEY (key))"
)


@dataclass(frozen=True)
class MigrationResult:
    """prepare_database 结果：来源、本次应用的业务迁移 key、备份文件（如有）"""

    source: str  # fresh（全新库）/ legacy（旧仓库库搬迁）/ live（原地补齐）
    applied_migrations: tuple
    backup: Path | None = None


# ===== Alembic 程序化调用 =====

def _alembic_config(database_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    cfg.cmd_opts = Namespace(x=[f"database_path={database_path}"])
    return cfg


def run_alembic_upgrade(database_path: Path) -> None:
    """对传入的数据库文件执行 alembic upgrade head（只操作该文件）"""
    command.upgrade(_alembic_config(database_path), "head")


def _stamp_baseline(database_path: Path) -> None:
    command.stamp(_alembic_config(database_path), BASELINE_REVISION)


def _head_revision() -> str:
    return ScriptDirectory(str(PROJECT_ROOT / "migrations")).get_current_head()


# ===== 只读检查 =====

def _alembic_version(database_path: Path):
    """读取 alembic_version；无表/无行返回 None（调用方确保文件存在）"""
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as conn:
        try:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        except sqlite3.OperationalError:
            return None
        return row[0] if row else None


def pending_data_migration_keys(database_path: Path) -> list:
    """只读列出尚未记账的业务迁移 key（data_migration_records 缺表视为全部待执行）"""
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as conn:
        try:
            recorded = {row[0] for row in conn.execute("SELECT key FROM data_migration_records")}
        except sqlite3.OperationalError:
            return list(DATA_MIGRATIONS)
        return [key for key in DATA_MIGRATIONS if key not in recorded]


def _expected_columns() -> dict:
    """当前基线期望的表→列（data_migration_records 属 0001 新表，缺失时补建）"""
    return {
        table.name: {column.name for column in table.columns}
        for table in Base.metadata.sorted_tables
        if table.name != "data_migration_records"
    }


def _validate_current_schema(database_path: Path) -> None:
    """无版本库 stamp 前的硬校验：未知或不完整 schema 中止并给出表/列名"""
    expected = _expected_columns()
    problems = []
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as conn:
        actual_tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table, columns in expected.items():
            if table not in actual_tables:
                problems.append(f"缺表 {table}")
                continue
            actual_columns = {
                row[1] for row in conn.execute(f"PRAGMA table_info({table})")
            }
            missing = columns - actual_columns
            if missing:
                problems.append(f"{table} 缺列: {', '.join(sorted(missing))}")
        unknown = actual_tables - set(expected) - {"alembic_version", "data_migration_records"}
        if unknown:
            problems.append(f"未知表: {', '.join(sorted(unknown))}")
    if problems:
        raise RuntimeError(
            "数据库 schema 不在已知基线，中止迁移（不做猜测性修补）: "
            + "; ".join(problems)
        )


# ===== SQLite Backup API 与发布 =====

def sqlite_backup(source: Path, target: Path) -> None:
    """SQLite Backup API 复制（正确处理 WAL 与页级一致性），禁止 shutil.copyfile"""
    source_conn = sqlite3.connect(source)
    target_conn = sqlite3.connect(target)
    try:
        with target_conn:
            source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()


def backup_database(source: Path, paths: RuntimePaths, schema_tag: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = paths.backups / f"surge_conf_{timestamp}_{schema_tag}.db"
    sqlite_backup(source, target)
    os.chmod(target, 0o600)
    return target


def _staging_path(paths: RuntimePaths) -> Path:
    return paths.database.with_name(paths.database.name + STAGING_SUFFIX)


def _discard_staging(staging: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        candidate = staging.with_name(staging.name + suffix)
        if candidate.exists():
            candidate.unlink()


def _checkpoint_and_remove_sidecars(database_path: Path) -> None:
    """关闭引擎后 checkpoint WAL 并删除 -wal/-shm，确保单文件原子替换"""
    conn = sqlite3.connect(database_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        sidecar = database_path.with_name(database_path.name + suffix)
        if sidecar.exists():
            sidecar.unlink()


def _verify_and_publish(staging: Path, target: Path) -> None:
    # 读写连接：ro URI 打开 WAL 库会重建 -shm/-wal 伴生文件，无法保证单文件替换
    conn = sqlite3.connect(staging)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
    finally:
        conn.close()
    if result[0] != "ok":
        raise RuntimeError(f"staging 完整性检查失败: {result[0]}")
    for suffix in ("-wal", "-shm"):
        sidecar = staging.with_name(staging.name + suffix)
        if sidecar.exists():
            sidecar.unlink()
    os.chmod(staging, 0o600)
    os.replace(staging, target)


# ===== schema 推进与 staging 升级 =====

def _sync_schema(database_path: Path) -> None:
    """把 staging 库推进到 Alembic head（不存在的文件由 Alembic 全新建表）"""
    if not database_path.exists():
        run_alembic_upgrade(database_path)
        return
    if _alembic_version(database_path) is None:
        # 无版本库：先验证是已知基线（业务表/列齐全、无未知表），补建 0001 新表后 stamp
        _validate_current_schema(database_path)
        with sqlite3.connect(database_path) as conn:
            exists = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='data_migration_records'"
            ).fetchone()
            if not exists:
                conn.execute(_DATA_MIGRATION_RECORDS_DDL)
        _stamp_baseline(database_path)
    run_alembic_upgrade(database_path)


async def _upgrade_staging(staging: Path) -> tuple:
    """staging 上依次完成 schema 推进、种子、业务数据迁移；返回应用的迁移 key"""
    _sync_schema(staging)
    runtime = DatabaseRuntime.create(staging)
    try:
        await seed_defaults(runtime.session_factory)
        applied = await apply_data_migrations(runtime.session_factory)
    finally:
        await runtime.dispose()
    _checkpoint_and_remove_sidecars(staging)
    return tuple(applied)


async def prepare_database(paths: RuntimePaths, legacy_path: Path) -> MigrationResult:
    """三路径准备数据库，全部经由 staging 原子发布（见模块 docstring 的不变量）"""
    ensure_runtime_directories(paths)
    live = paths.database
    staging = _staging_path(paths)
    legacy = Path(legacy_path)

    if not live.exists() and legacy.exists():
        # 路径 2：仅旧仓库库存在 —— 备份未升级副本，staging 上完成后原子发布，旧库保持原样
        backup = backup_database(legacy, paths, "pre_alembic")
        sqlite_backup(legacy, staging)
        try:
            applied = await _upgrade_staging(staging)
            _verify_and_publish(staging, live)
        except BaseException:
            _discard_staging(staging)
            raise
        return MigrationResult("legacy", applied, backup)

    if live.exists():
        # 路径 3：live 已存在 —— 先只读检查，全部完成则直接复用，不动任何文件
        head = _head_revision()
        current = _alembic_version(live)
        schema_pending = current != head
        data_pending = pending_data_migration_keys(live)
        if not schema_pending and not data_pending:
            return MigrationResult("live", (), None)
        # 存在待执行项：时间戳备份 + staging 上完成后原子替换；失败删除 staging，live 字节不变
        backup = backup_database(live, paths, current or "pre_alembic")
        sqlite_backup(live, staging)
        try:
            applied = await _upgrade_staging(staging)
            _verify_and_publish(staging, live)
        except BaseException:
            _discard_staging(staging)
            raise
        return MigrationResult("live", applied, backup)

    # 路径 1：新旧库均不存在 —— staging 上从零构建后原子发布
    try:
        applied = await _upgrade_staging(staging)
        _verify_and_publish(staging, live)
    except BaseException:
        _discard_staging(staging)
        raise
    return MigrationResult("fresh", applied, None)
