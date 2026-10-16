"""prepare_database staging 原子迁移测试（fresh / legacy / live pending / 失败回滚 / 幂等）

只使用 tmp_path 下的临时合成数据库，不接触任何真实运行库。
"""

import sqlite3
from pathlib import Path

import pytest
import sqlalchemy
from alembic import command

from app.database import Base
from app.runtime_paths import RuntimePaths
from app.services import database_migration
from app.services.database_migration import prepare_database

DATA_MIGRATION_KEYS = {
    "security_rules_v1", "douyin_rule_v1", "ipv6_vif_v1",
    "dns_defaults_v1", "remove_global_quic_v1", "no_resolve_keywords_v1",
}


def runtime_paths(root: Path) -> RuntimePaths:
    root = Path(root)
    log_dir = root / "logs"
    return RuntimePaths(
        root=root, database=root / "surge_conf.db", backups=root / "backups",
        exports=root / "exports", session_secret=root / "session.key",
        log_dir=log_dir, log_file=log_dir / "surgeconf.log",
    )


def create_current_schema_database(path: Path) -> Path:
    """合成 0001 基线指纹、无 alembic_version / 无 data_migration_records 的旧式库。

    经 Alembic 建到 0001 再剔除簿记表：ORM metadata 声明了 0003 的命名
    CHECK 约束（引用 target/udp_security_mode 列），直接 create_all 后
    DROP COLUMN 会被 SQLite 拒绝，且基线库本就不该带 0003 约束。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(database_migration._alembic_config(path),
                    database_migration.BASELINE_REVISION)
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("DROP TABLE alembic_version")
        conn.execute("DROP TABLE data_migration_records")
    conn.close()
    return path


def sqlite_scalar(path: Path, sql: str):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def sqlite_rows(path: Path, sql: str):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _staging_path(paths: RuntimePaths) -> Path:
    return paths.database.with_name(paths.database.name + ".staging")


# ===== 简报规定的三个用例 =====

@pytest.mark.asyncio
async def test_legacy_database_moves_through_staging(tmp_path):
    legacy = create_current_schema_database(tmp_path / "repo" / "data" / "surge_conf.db")
    paths = runtime_paths(tmp_path / "app-support")
    result = await prepare_database(paths, legacy)
    assert result.source == "legacy"
    assert paths.database.exists()
    assert legacy.exists()
    assert sqlite_scalar(paths.database, "PRAGMA integrity_check") == "ok"
    assert paths.database.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_failed_upgrade_keeps_live_database_unchanged(tmp_path, monkeypatch):
    paths = runtime_paths(tmp_path / "app-support")
    create_current_schema_database(paths.database)
    before = paths.database.read_bytes()
    monkeypatch.setattr(database_migration, "run_alembic_upgrade",
                        lambda _: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        await prepare_database(paths, tmp_path / "missing.db")
    assert paths.database.read_bytes() == before


@pytest.mark.asyncio
async def test_prepare_database_is_idempotent(tmp_path):
    paths = runtime_paths(tmp_path / "app-support")
    await prepare_database(paths, tmp_path / "missing.db")
    first = sqlite_scalar(paths.database, "SELECT version_num FROM alembic_version")
    backups_before = {item.name for item in paths.backups.iterdir()}
    await prepare_database(paths, tmp_path / "missing.db")
    assert sqlite_scalar(paths.database, "SELECT version_num FROM alembic_version") == first
    assert {item.name for item in paths.backups.iterdir()} == backups_before


# ===== 验收补充用例 =====

@pytest.mark.asyncio
async def test_fresh_database_gets_schema_seed_and_migration_records(tmp_path):
    """全新环境：staging 上完成 schema + 种子 + 业务迁移记账后原子发布"""
    paths = runtime_paths(tmp_path / "app-support")
    result = await prepare_database(paths, tmp_path / "missing.db")

    assert result.source == "fresh"
    assert result.backup is None
    assert sqlite_scalar(paths.database, "SELECT count(*) FROM alembic_version") == 1
    assert sqlite_scalar(paths.database, "SELECT count(*) FROM region_groups") > 0
    keys = {row[0] for row in sqlite_rows(paths.database, "SELECT key FROM data_migration_records")}
    assert keys == DATA_MIGRATION_KEYS
    assert sqlite_scalar(paths.database, "PRAGMA integrity_check") == "ok"
    assert paths.database.stat().st_mode & 0o777 == 0o600
    assert not _staging_path(paths).exists()
    assert not _staging_path(paths).with_name(_staging_path(paths).name + "-wal").exists()


# ===== 无版本库精确识别（fix round 1） =====


def create_unversioned_complete_head_database(path: Path) -> Path:
    """合成"无 alembic_version、业务表与 head 指纹完全一致"的库"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = sqlalchemy.create_engine(f"sqlite:///{path}")
    tables = [t for t in Base.metadata.sorted_tables if t.name != "data_migration_records"]
    Base.metadata.create_all(engine, tables=tables)
    engine.dispose()
    return path


@pytest.mark.asyncio
async def test_unversioned_complete_head_stamps_head_not_baseline(tmp_path):
    """无版本但列+约束与完整 head 指纹一致：stamp 当前 head（no-op 升级），
    绝不统一 stamp 0001"""
    legacy = create_unversioned_complete_head_database(
        tmp_path / "repo" / "data" / "surge_conf.db")
    paths = runtime_paths(tmp_path / "app-support")
    result = await prepare_database(paths, legacy)
    assert result.source == "legacy"
    assert sqlite_scalar(paths.database, "SELECT version_num FROM alembic_version") \
        == database_migration._head_revision()
    cols = {row[1] for row in sqlite_rows(paths.database, "PRAGMA table_info(config_profiles)")}
    assert "subscription_token_hash" in cols
    assert sqlite_scalar(paths.database, "PRAGMA integrity_check") == "ok"


@pytest.mark.asyncio
async def test_unversioned_head_columns_without_checks_rejected(tmp_path):
    """列指纹与 head 一致但缺 0003 CHECK 约束：fail closed，绝不 stamp head。

    模拟外部工具直接 ALTER TABLE ADD COLUMN 加列（无约束）的真实 SQLite 库。
    """
    legacy = tmp_path / "repo" / "data" / "surge_conf.db"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(database_migration._alembic_config(legacy),
                    "0002_profile_subscription_tokens")
    conn = sqlite3.connect(legacy)
    with conn:
        conn.execute("DROP TABLE alembic_version")
        conn.execute("ALTER TABLE config_profiles ADD COLUMN target VARCHAR(20) "
                     "NOT NULL DEFAULT 'mac_workstation'")
        conn.execute("ALTER TABLE config_profiles ADD COLUMN udp_security_mode VARCHAR(20) "
                     "NOT NULL DEFAULT 'scoped'")
    conn.close()

    paths = runtime_paths(tmp_path / "app-support")
    before = paths.database.read_bytes() if paths.database.exists() else None
    with pytest.raises(RuntimeError, match="ck_config_profiles") as excinfo:
        await prepare_database(paths, legacy)
    details = str(excinfo.value)
    for name in ("ck_config_profiles_target", "ck_config_profiles_udp_security_mode",
                 "ck_config_profiles_target_udp_mode"):
        assert name in details
    assert (paths.database.read_bytes() if paths.database.exists() else None) == before


@pytest.mark.asyncio
async def test_prepared_head_rejects_invalid_target_and_mode_rows(tmp_path):
    """prepare 之后的 head 库在 DB 层拒绝三类非法写入：目标集合 / 模式集合 / iOS+scoped"""
    paths = runtime_paths(tmp_path / "app-support")
    await prepare_database(paths, tmp_path / "missing.db")
    bad_rows = [
        ("坏目标", 0, "linux", "scoped"),
        ("坏模式", 0, "mac_workstation", "loose"),
        ("坏组合", 0, "ios_mobile", "scoped"),
    ]
    for name, is_default, target, mode in bad_rows:
        conn = sqlite3.connect(paths.database)
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute(
                    "INSERT INTO config_profiles (name, is_default, target, udp_security_mode) "
                    "VALUES (?, ?, ?, ?)", (name, is_default, target, mode))
        conn.close()


def test_alembic_head_check_constraints_match_metadata(tmp_path):
    """强制器：迁移产物、ORM metadata、SCHEMA_REVISION_CHECKS 三处约束名一致"""
    from sqlalchemy.schema import CheckConstraint

    from app.models import ConfigProfile

    expected = {c.name for c in ConfigProfile.__table__.constraints
                if isinstance(c, CheckConstraint)}
    registered = set(database_migration.SCHEMA_REVISION_CHECKS[
        database_migration._head_revision()]["config_profiles"])
    assert expected == registered == {
        "ck_config_profiles_target",
        "ck_config_profiles_udp_security_mode",
        "ck_config_profiles_target_udp_mode",
    }

    path = tmp_path / "head.db"
    command.upgrade(database_migration._alembic_config(path), "head")
    engine = sqlalchemy.create_engine(f"sqlite:///{path}")
    try:
        actual = {cc["name"] for cc in
                  sqlalchemy.inspect(engine).get_check_constraints("config_profiles")}
    finally:
        engine.dispose()
    assert actual == expected


@pytest.mark.asyncio
async def test_unversioned_extra_column_rejected_without_data_values(tmp_path):
    """额外列：明确拒绝；错误消息只含表/列名，不含任何数据值"""
    paths = runtime_paths(tmp_path / "app-support")
    legacy = create_current_schema_database(tmp_path / "repo" / "data" / "surge_conf.db")
    secret_value = "SECRET-VALUE-must-not-leak"
    conn = sqlite3.connect(legacy)
    with conn:
        conn.execute("ALTER TABLE config_profiles ADD COLUMN rogue_column TEXT")
        conn.execute("INSERT INTO config_profiles (name, rogue_column) VALUES (?, ?)",
                     ("档", secret_value))
    conn.close()

    before = paths.database.read_bytes() if paths.database.exists() else None
    with pytest.raises(RuntimeError, match="rogue_column") as excinfo:
        await prepare_database(paths, legacy)
    assert secret_value not in str(excinfo.value)
    assert (paths.database.read_bytes() if paths.database.exists() else None) == before


@pytest.mark.asyncio
async def test_unversioned_mixed_revision_columns_rejected(tmp_path):
    """混合列（0001 + 0002 之外再加列）：精确识别失败，明确拒绝"""
    paths = runtime_paths(tmp_path / "app-support")
    legacy = create_unversioned_complete_head_database(
        tmp_path / "repo" / "data" / "surge_conf.db")
    conn = sqlite3.connect(legacy)
    with conn:
        conn.execute("ALTER TABLE subscriptions ADD COLUMN stray_column TEXT")
    conn.close()

    with pytest.raises(RuntimeError, match="stray_column"):
        await prepare_database(paths, legacy)


@pytest.mark.asyncio
async def test_unversioned_missing_column_rejected(tmp_path):
    """缺失列（真实 0001 也不匹配）：拒绝并列出列名"""
    paths = runtime_paths(tmp_path / "app-support")
    legacy = create_current_schema_database(tmp_path / "repo" / "data" / "surge_conf.db")
    conn = sqlite3.connect(legacy)
    with conn:
        conn.execute("ALTER TABLE subscriptions DROP COLUMN tag")
    conn.close()

    with pytest.raises(RuntimeError, match="tag"):
        await prepare_database(paths, legacy)


def test_head_fingerprint_matches_current_metadata():
    """指纹单一更新点强制器：head 指纹必须与当前 Base.metadata 一致，
    模型新增列时必须同步更新 SCHEMA_REVISION_DELTAS"""
    fingerprints = database_migration.schema_fingerprints()
    head = database_migration._head_revision()
    assert head in fingerprints
    expected = {
        table.name: {column.name for column in table.columns}
        for table in Base.metadata.sorted_tables
        if table.name != "data_migration_records"
    }
    actual = {table: set(columns) for table, columns in fingerprints[head].items()}
    assert actual == expected


def test_baseline_fingerprint_is_frozen_0001():
    """0001 指纹不含 0002 新列（冻结快照，不受模型演进影响）"""
    baseline = database_migration.schema_fingerprints()["0001_current_schema"]
    assert "subscription_token_hash" not in baseline["config_profiles"]
    assert set(baseline) == {
        "subscriptions", "proxy_nodes", "region_groups", "service_groups",
        "config_profiles", "custom_rules", "host_mappings", "rule_sources",
        "system_config",
    }


@pytest.mark.asyncio
async def test_live_pending_data_migration_applies_and_backs_up(tmp_path):
    """live 已在 head 但业务迁移有缺口：时间戳备份 + staging 补齐后原子替换"""
    paths = runtime_paths(tmp_path / "app-support")
    await prepare_database(paths, tmp_path / "missing.db")

    conn = sqlite3.connect(paths.database)
    with conn:
        conn.execute("DELETE FROM data_migration_records")
    conn.close()

    result = await prepare_database(paths, tmp_path / "missing.db")
    assert result.source == "live"
    assert set(result.applied_migrations) == DATA_MIGRATION_KEYS
    backups = list(paths.backups.iterdir())
    assert len(backups) == 1
    assert not any("hostname" in b.name or "user" in b.name for b in backups)
    keys = {row[0] for row in sqlite_rows(paths.database, "SELECT key FROM data_migration_records")}
    assert keys == DATA_MIGRATION_KEYS
    assert sqlite_scalar(paths.database, "PRAGMA integrity_check") == "ok"


@pytest.mark.asyncio
async def test_unknown_schema_aborts_with_table_and_column_names(tmp_path):
    """未知/不完整 schema 中止并给出表/列名，不做猜测性修补，live 不变"""
    paths = runtime_paths(tmp_path / "app-support")
    create_current_schema_database(paths.database)
    conn = sqlite3.connect(paths.database)
    with conn:
        conn.execute("ALTER TABLE region_groups DROP COLUMN auto_enabled")
    conn.close()
    before = paths.database.read_bytes()

    with pytest.raises(RuntimeError, match="auto_enabled"):
        await prepare_database(paths, tmp_path / "missing.db")
    assert paths.database.read_bytes() == before
    assert not _staging_path(paths).exists()


@pytest.mark.asyncio
async def test_failed_upgrade_removes_staging_and_sidecars(tmp_path, monkeypatch):
    """失败路径清理 staging 及其 -wal/-shm，不留下半成品"""
    paths = runtime_paths(tmp_path / "app-support")
    create_current_schema_database(paths.database)
    monkeypatch.setattr(database_migration, "run_alembic_upgrade",
                        lambda _: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        await prepare_database(paths, tmp_path / "missing.db")
    staging = _staging_path(paths)
    assert not staging.exists()
    assert not staging.with_name(staging.name + "-wal").exists()
    assert not staging.with_name(staging.name + "-shm").exists()


@pytest.mark.asyncio
async def test_lifespan_prepares_database_before_binding_live_runtime(tmp_path, monkeypatch):
    """startup 在 bind live runtime 前完成 prepare：bind 时库已存在且已 stamp 到 head"""
    from app.main import app as fastapi_app
    from app.database import database as global_database

    monkeypatch.setenv("SURGECONF_DATA_DIR", str(tmp_path / "app-support"))
    original_bind = global_database.bind
    bound = {}

    def checked_bind(path):
        assert Path(path).exists(), "bind 前必须完成 prepare_database"
        assert sqlite_scalar(Path(path), "SELECT version_num FROM alembic_version") \
            == database_migration._head_revision()
        original_bind(path)
        bound["path"] = Path(path)

    monkeypatch.setattr(global_database, "bind", checked_bind)
    async with fastapi_app.router.lifespan_context(fastapi_app):
        assert bound["path"].exists()



# ===== Round 3：dangling staging symlink =====


def test_discard_staging_removes_dangling_symlink(tmp_path):
    """staging 为 dangling symlink：直接 unlink（不跟随、不保留）"""
    import os
    from app.services.database_migration import discard_staging
    staging = tmp_path / "surge_conf.db.staging"
    os.symlink(tmp_path / "does-not-exist", staging)
    discard_staging(staging)
    assert not os.path.lexists(staging)   # 不保留 dangling symlink
