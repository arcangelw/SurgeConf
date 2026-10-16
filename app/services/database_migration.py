import os
import sqlalchemy
import sqlite3
from argparse import Namespace
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.bootstrap import seed_defaults
from app.database import Base, DatabaseRuntime
from app.runtime_paths import (RuntimePaths, assert_regular_file, assert_sqlite_database_safe,
                               ensure_runtime_directories, harden_private_file,
                               reserve_private_file)
from app.services.backups import backup_database, sqlite_backup
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



def _head_revision() -> str:
    return ScriptDirectory(str(PROJECT_ROOT / "migrations")).get_current_head()


# ===== 只读检查 =====

def alembic_version(database_path: Path):
    assert_sqlite_database_safe(database_path)
    """读取 alembic_version；无表/无行返回 None（调用方确保文件存在）"""
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as conn:
        try:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        except sqlite3.OperationalError:
            return None
        return row[0] if row else None



def pending_data_migration_keys(database_path: Path) -> list:
    assert_sqlite_database_safe(database_path)
    """只读列出尚未记账的业务迁移 key（data_migration_records 缺表视为全部待执行）"""
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as conn:
        try:
            recorded = {row[0] for row in conn.execute("SELECT key FROM data_migration_records")}
        except sqlite3.OperationalError:
            return list(DATA_MIGRATIONS)
        return [key for key in DATA_MIGRATIONS if key not in recorded]


# ===== 无版本库识别：冻结的 revision → 表/列指纹 =====

# 0001 基线快照（表 → 列）。冻结声明，不随 Base.metadata 演进；
# data_migration_records 与 alembic_version 不参与识别（前者缺失时由 _sync_schema 补建）。
_BASELINE_TABLE_COLUMNS: dict[str, set[str]] = {
    "subscriptions": {"id", "name", "url", "tag", "color", "enabled", "auto_update",
                      "update_interval", "last_update", "created_at", "updated_at"},
    "proxy_nodes": {"id", "name", "subscription_id", "node_type", "server", "port",
                    "config", "region_auto", "region_manual", "pinned_services",
                    "enabled", "created_at"},
    "region_groups": {"id", "name", "display_name", "icon_url", "filter_regex",
                      "sort_order", "enabled", "auto_enabled", "manual_enabled",
                      "created_at"},
    "service_groups": {"id", "name", "display_name", "icon_url", "group_type",
                       "policies", "sort_order", "enabled", "created_at"},
    "config_profiles": {"id", "name", "description", "is_default", "locale",
                        "final_action", "general", "dns", "url_rewrites",
                        "header_rewrites", "mitm", "created_at", "updated_at"},
    "custom_rules": {"id", "profile_id", "rule_text", "position", "sort_order",
                     "enabled", "comment"},
    "host_mappings": {"id", "profile_id", "domain", "target", "sort_order",
                      "enabled", "skip_proxy"},
    "rule_sources": {"id", "name", "url", "action", "rule_type", "category",
                     "enabled", "sort_order", "created_at"},
    "system_config": {"id", "key", "value"},
}

# 各 revision 相对前一 revision 新增的 (表, 列)。
# **新增 revision 时唯一需要更新的地方**；测试强制 head 指纹与 Base.metadata 一致。
SCHEMA_REVISION_DELTAS: dict[str, set[tuple[str, str]]] = {
    "0002_profile_subscription_tokens": {("config_profiles", "subscription_token_hash")},
    "0003_profile_targets": {("config_profiles", "target"),
                             ("config_profiles", "udp_security_mode")},
}

# 各 revision 必需的命名 CHECK 约束（表 → 约束名集合）。
# **新增带约束的 revision 时在此登记**；无版本库识别缺任一必需约束即 fail-closed
#（列指纹一致也不 stamp）。约束名同时由 ORM metadata（models.py __table_args__）
# 与对应迁移声明，三处必须同名。
SCHEMA_REVISION_CHECKS: dict[str, dict[str, frozenset[str]]] = {
    "0003_profile_targets": {
        "config_profiles": frozenset({
            "ck_config_profiles_target",
            "ck_config_profiles_udp_security_mode",
            "ck_config_profiles_target_udp_mode",
        }),
    },
}

# 识别时忽略的表：alembic_version 属版本簿记；data_migration_records 由 _sync_schema 补建
_IDENTIFY_IGNORED_TABLES = {"alembic_version", "data_migration_records"}


def schema_fingerprints() -> dict[str, dict[str, frozenset[str]]]:
    """revision → 表→列指纹：0001 基线 + 逐 revision 冻结增量推导"""
    fingerprints: dict[str, dict[str, frozenset[str]]] = {}
    tables = {table: set(columns) for table, columns in _BASELINE_TABLE_COLUMNS.items()}
    fingerprints[BASELINE_REVISION] = {t: frozenset(c) for t, c in tables.items()}
    for revision, additions in SCHEMA_REVISION_DELTAS.items():
        for table, column in additions:
            tables.setdefault(table, set()).add(column)
        fingerprints[revision] = {t: frozenset(c) for t, c in tables.items()}
    return fingerprints


def _actual_schema(database_path: Path) -> tuple[set[str], dict[str, set[str]], dict[str, set[str]]]:
    assert_sqlite_database_safe(database_path)
    """实际库的表集合、表→列集合、表→命名 CHECK 约束集合（排除簿记表）。

    约束经 SQLAlchemy inspector 读取，不解析 sqlite_master DDL 文本
    （避免依赖空白/顺序的脆弱正则）。
    """
    engine = sqlalchemy.create_engine(f"sqlite:///{database_path}")
    try:
        inspector = sqlalchemy.inspect(engine)
        tables = {t for t in inspector.get_table_names()
                  if t not in _IDENTIFY_IGNORED_TABLES}
        columns = {t: {c["name"] for c in inspector.get_columns(t)} for t in tables}
        checks = {t: {cc["name"] for cc in inspector.get_check_constraints(t)}
                  for t in tables}
    finally:
        engine.dispose()
    return tables, columns, checks


def _missing_required_checks(revision: str, actual_checks: dict[str, set[str]]) -> set[str]:
    """revision 必需但库中缺失的 CHECK 约束名（跨表打平）"""
    missing: set[str] = set()
    for table, required in SCHEMA_REVISION_CHECKS.get(revision, {}).items():
        missing |= required - actual_checks.get(table, set())
    return missing


def identify_unversioned_schema(database_path: Path) -> str | None:
    """无版本库精确识别：表/列与某个 revision 指纹完全一致、且该 revision
    必需的命名 CHECK 约束齐全，才返回该 revision。

    额外列、缺失列、混合多个 revision 的列或缺任一必需约束都返回 None
    （绝不猜测性 stamp）。
    """
    actual_tables, actual_columns, actual_checks = _actual_schema(database_path)
    for revision, fingerprint in schema_fingerprints().items():
        if actual_tables != set(fingerprint):
            continue
        if not all(actual_columns[table] == set(columns)
                   for table, columns in fingerprint.items()):
            continue
        if _missing_required_checks(revision, actual_checks):
            continue
        return revision
    return None


def _schema_mismatch_details(database_path: Path) -> list[str]:
    """与最近 revision 指纹的差异描述：只含表/列/约束名，绝不含数据值"""
    head = _head_revision()
    fingerprint = schema_fingerprints()[head]
    actual_tables, actual_columns, actual_checks = _actual_schema(database_path)
    problems: list[str] = []
    for table in sorted(set(fingerprint) - actual_tables):
        problems.append(f"缺表 {table}")
    for table in sorted(actual_tables - set(fingerprint)):
        problems.append(f"额外表 {table}")
    for table, expected in fingerprint.items():
        if table not in actual_columns:
            continue
        missing = expected - actual_columns[table]
        extra = actual_columns[table] - expected
        if missing:
            problems.append(f"{table} 缺列: {', '.join(sorted(missing))}")
        if extra:
            problems.append(f"{table} 额外列: {', '.join(sorted(extra))}")
    for table, required in SCHEMA_REVISION_CHECKS.get(head, {}).items():
        missing = required - actual_checks.get(table, set())
        if missing:
            problems.append(f"{table} 缺 CHECK 约束: {', '.join(sorted(missing))}")
    if not problems:
        problems.append("schema 与已知 revision 指纹均不一致（混合列）")
    return problems


def _validate_current_schema(database_path: Path) -> str:
    """无版本库 stamp 前的硬校验：精确匹配已知 revision，否则中止并给表/列名"""
    revision = identify_unversioned_schema(database_path)
    if revision is None:
        raise RuntimeError(
            "数据库 schema 不精确匹配任何已知 revision，中止迁移（不做猜测性修补）: "
            + "; ".join(_schema_mismatch_details(database_path))
        )
    return revision


# ===== staging 与原子发布 =====

def staging_path(paths: RuntimePaths) -> Path:
    return paths.database.with_name(paths.database.name + STAGING_SUFFIX)


def discard_staging(staging: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        candidate = staging.with_name(staging.name + suffix)
        if os.path.lexists(candidate):
            # dangling/普通 symlink 一律直接 unlink（不跟随）；其余异常类型 fail closed
            if candidate.is_symlink():
                candidate.unlink()
            elif candidate.exists():
                candidate.unlink()
            else:
                raise RuntimeError(f"staging 路径为异常类型: {candidate}")


def _checkpoint_and_remove_sidecars(database_path: Path) -> None:
    assert_sqlite_database_safe(database_path)
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


def verify_and_publish(staging: Path, target: Path) -> None:
    assert_sqlite_database_safe(staging)
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
    harden_private_file(staging)
    os.replace(staging, target)


# ===== schema 推进与 staging 升级 =====

def _sync_schema(database_path: Path) -> None:
    """把 staging 库推进到 Alembic head（不存在或 0 字节占位文件由
    Alembic 全新建表；预占位保留的普通文件不触发 schema 识别分支）"""
    if not database_path.exists() or database_path.stat().st_size == 0:
        run_alembic_upgrade(database_path)
        return
    if alembic_version(database_path) is None:
        # 无版本库：精确识别 schema 所属 revision 后 stamp 该 revision 再升级，
        # 额外/缺失/混合列在识别阶段明确拒绝（绝不确定性地统一 stamp 基线）
        revision = _validate_current_schema(database_path)
        with sqlite3.connect(database_path) as conn:
            exists = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='data_migration_records'"
            ).fetchone()
            if not exists:
                conn.execute(_DATA_MIGRATION_RECORDS_DDL)
        command.stamp(_alembic_config(database_path), revision)
    run_alembic_upgrade(database_path)


async def upgrade_staging(staging: Path) -> tuple:
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


def _remove_live_sidecars(live: Path) -> None:
    """原子替换前清理 live 残留 -wal/-shm，防止旧伴生文件污染新库"""
    for suffix in ("-wal", "-shm"):
        sidecar = live.with_name(live.name + suffix)
        if sidecar.exists():
            sidecar.unlink()


async def rebuild_from_source(source: Path | None, paths: RuntimePaths) -> tuple:
    """从源库（None 为全新）经 staging 完成 Alembic/种子/业务迁移后原子替换 live。

    prepare_database 与备份恢复/重置共用此链；失败丢弃 staging，
    live 字节不变（原子性不变量）。
    """
    live = paths.database
    staging = staging_path(paths)
    discard_staging(staging)
    try:
        # 在已验证 0700 parent 下原子保留 staging 为普通文件（0600），
        # SQLite 随后打开的是我们已验证的文件；全新路径同样先占位
        reserve_private_file(staging)
        if source is not None:
            sqlite_backup(source, staging, reserved=True)
        applied = await upgrade_staging(staging)
        _remove_live_sidecars(live)
        verify_and_publish(staging, live)
    except BaseException:
        discard_staging(staging)
        raise
    return applied


async def prepare_database(paths: RuntimePaths, legacy_path: Path) -> MigrationResult:
    """三路径准备数据库，全部经由 staging 原子发布（见模块 docstring 的不变量）"""
    ensure_runtime_directories(paths)
    live = paths.database
    legacy = Path(legacy_path)
    # 任何 SQLite 打开（含 legacy/backup API 只读源）前：最终普通文件 +
    # 父链无 symlink + 现存 -wal/-shm 伴生无异常（缺失 sidecar 由 SQLite 自建）
    assert_sqlite_database_safe(live)
    assert_sqlite_database_safe(legacy)

    if not live.exists() and legacy.exists():
        # 路径 2：仅旧仓库库存在 —— 备份未升级副本，staging 上完成后原子发布，旧库保持原样
        backup = backup_database(legacy, paths, "pre_alembic")
        applied = await rebuild_from_source(legacy, paths)
        return MigrationResult("legacy", applied, backup)

    if live.exists():
        # 路径 3：live 已存在 —— 先只读检查，全部完成则直接复用，不动任何文件
        head = _head_revision()
        current = alembic_version(live)
        schema_pending = current != head
        data_pending = pending_data_migration_keys(live)
        if not schema_pending and not data_pending:
            return MigrationResult("live", (), None)
        # 存在待执行项：时间戳备份 + staging 上完成后原子替换；失败删除 staging，live 字节不变
        backup = backup_database(live, paths, current or "pre_alembic")
        applied = await rebuild_from_source(live, paths)
        return MigrationResult("live", applied, backup)

    # 路径 1：新旧库均不存在 —— staging 上从零构建后原子发布
    applied = await rebuild_from_source(None, paths)
    return MigrationResult("fresh", applied, None)
