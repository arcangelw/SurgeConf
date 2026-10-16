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
    "dangling_rule_source_actions_v1",
    "rule_source_ip_flags_v1", "rule_source_order_invariants_v1",
    "retire_no_resolve_keywords_v1",
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
    """强制器：ORM metadata、SCHEMA_REVISION_CHECKS、Alembic 产物三处的
    约束名与规范化表达式完全一致"""
    from sqlalchemy.schema import CheckConstraint

    from app.models import ConfigProfile

    head = database_migration._head_revision()
    registered = database_migration._accumulated_required_checks(head)["config_profiles"]
    metadata_checks = {
        c.name: database_migration._normalize_check_expression(c.sqltext.text)
        for c in ConfigProfile.__table__.constraints
        if isinstance(c, CheckConstraint)
    }
    assert set(metadata_checks) == set(registered) == {
        "ck_config_profiles_target",
        "ck_config_profiles_udp_security_mode",
        "ck_config_profiles_target_udp_mode",
    }
    assert metadata_checks == {
        name: database_migration._normalize_check_expression(expr)
        for name, expr in registered.items()
    }

    path = tmp_path / "head.db"
    command.upgrade(database_migration._alembic_config(path), "head")
    engine = sqlalchemy.create_engine(f"sqlite:///{path}")
    try:
        alembic_checks = {
            cc["name"]: database_migration._normalize_check_expression(cc["sqltext"])
            for cc in sqlalchemy.inspect(engine).get_check_constraints("config_profiles")
        }
    finally:
        engine.dispose()
    assert alembic_checks == metadata_checks


@pytest.mark.asyncio
async def test_unversioned_alembic_head_identified(tmp_path):
    """Alembic 产物的无版本副本（仅剔除簿记表）仍精确识别为 head"""
    legacy = tmp_path / "repo" / "data" / "surge_conf.db"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(database_migration._alembic_config(legacy), "head")
    conn = sqlite3.connect(legacy)
    with conn:
        conn.execute("DROP TABLE alembic_version")
        conn.execute("DROP TABLE data_migration_records")
    conn.close()
    assert database_migration.identify_unversioned_schema(legacy) == \
        database_migration._head_revision()


@pytest.mark.parametrize("tampered", [
    # 任一表达式恒真（CHECK(1)）：名对语义错
    {"ck_config_profiles_target": "1",
     "ck_config_profiles_udp_security_mode": "1",
     "ck_config_profiles_target_udp_mode": "1"},
    # 组合约束被改成错误语义（禁止 mac_workstation+scoped，放行 iOS+scoped）
    {"ck_config_profiles_target": "target IN ('mac_workstation', 'mac_gateway', 'ios_mobile')",
     "ck_config_profiles_udp_security_mode": "udp_security_mode IN ('strict', 'scoped', 'compatible')",
     "ck_config_profiles_target_udp_mode":
         "NOT (target = 'mac_workstation' AND udp_security_mode = 'scoped')"},
])
def test_unversioned_head_tampered_check_expression_rejected(tmp_path, tampered):
    """列与三个约束名都正确、但任一表达式为恒真/错误组合：fail closed，
    绝不识别（更不 stamp）为 0003"""
    path = tmp_path / "tampered.db"
    engine = sqlalchemy.create_engine(f"sqlite:///{path}")
    tables = [t for t in Base.metadata.sorted_tables
              if t.name not in ("data_migration_records", "config_profiles")]
    Base.metadata.create_all(engine, tables=tables)
    engine.dispose()

    ddl = """
    CREATE TABLE config_profiles (
        id INTEGER NOT NULL PRIMARY KEY,
        name VARCHAR(100) NOT NULL,
        description TEXT,
        is_default BOOLEAN,
        target VARCHAR(20) NOT NULL DEFAULT 'mac_workstation',
        udp_security_mode VARCHAR(20) NOT NULL DEFAULT 'scoped',
        locale VARCHAR(10),
        final_action VARCHAR(100),
        general JSON,
        dns JSON,
        url_rewrites JSON,
        header_rewrites JSON,
        mitm JSON,
        subscription_token_hash VARCHAR(64),
        created_at DATETIME,
        updated_at DATETIME,
        CONSTRAINT ck_config_profiles_target CHECK (%s),
        CONSTRAINT ck_config_profiles_udp_security_mode CHECK (%s),
        CONSTRAINT ck_config_profiles_target_udp_mode CHECK (%s)
    )""" % (tampered["ck_config_profiles_target"],
            tampered["ck_config_profiles_udp_security_mode"],
            tampered["ck_config_profiles_target_udp_mode"])
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(ddl)
    conn.close()

    assert database_migration.identify_unversioned_schema(path) is None
    details = "; ".join(database_migration._schema_mismatch_details(path))
    assert "CHECK 约束表达式不一致" in details
    assert "ck_config_profiles" in details


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


# ===== 0004：sort_order 收敛非空 + ip_rules 列 downgrade→upgrade 往返 =====


def test_upgrade_0004_normalizes_null_sort_order(tmp_path):
    """0003 库存量 sort_order NULL → upgrade head：收敛为 0（NULL 本就排最前，
    语义不变）、既有值保留、列变 NOT NULL（再写 NULL 被 DB 拒绝）"""
    db_path = tmp_path / "null-sort.db"
    command.upgrade(database_migration._alembic_config(db_path), "0003_profile_targets")
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO rule_sources (name, url, action, rule_type, enabled, sort_order) "
            "VALUES ('NULL 源', 'https://example.com/null.list', 'DIRECT', 'RULE-SET', 1, NULL)")
        conn.execute(
            "INSERT INTO rule_sources (name, url, action, rule_type, enabled, sort_order) "
            "VALUES ('有值源', 'https://example.com/ok.list', 'DIRECT', 'RULE-SET', 1, 7)")
    conn.close()

    command.upgrade(database_migration._alembic_config(db_path), "head")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = dict(conn.execute("SELECT name, sort_order FROM rule_sources").fetchall())
        assert rows == {"NULL 源": 0, "有值源": 7}
    finally:
        conn.close()
    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute("UPDATE rule_sources SET sort_order = NULL WHERE name = '有值源'")
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_rule_source_ip_flags_roundtrip(tmp_path):
    """upgrade → 业务迁移回填记账 → downgrade → upgrade → 业务迁移：
    ip_rules 列删除后重建（值归 0），ip_flags 记账随 downgrade 删除使回填
    可重放；order / retire 迁移记账不随列删除（语义独立）"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.default_config import DEFAULT_RULE_SOURCES
    from app.models import RuleSource
    from app.services.data_migrations import apply_data_migrations

    db_path = tmp_path / "roundtrip.db"
    cfg = database_migration._alembic_config(db_path)
    command.upgrade(cfg, "head")

    # 种默认源并强制 ip_rules=0，模拟 0004 刚加列后的存量库
    sync_engine = sqlalchemy.create_engine(f"sqlite:///{db_path}")
    Session = sessionmaker(sync_engine)
    with Session() as s:
        for rs in DEFAULT_RULE_SOURCES:
            s.add(RuleSource(**{**rs, "ip_rules": False}))
        s.commit()
    sync_engine.dispose()

    async def _run_migrations():
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            return await apply_data_migrations(factory)
        finally:
            await engine.dispose()

    # 第一次业务迁移：回填 ip_rules 并记账
    applied1 = await _run_migrations()
    assert "rule_source_ip_flags_v1" in applied1
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        flagged = conn.execute(
            "SELECT name FROM rule_sources WHERE ip_rules = 1").fetchall()
    finally:
        conn.close()
    assert {row[0] for row in flagged} == {
        rs["name"] for rs in DEFAULT_RULE_SOURCES if rs.get("ip_rules")}

    # downgrade：ip_rules 列删除 + ip_flags 记账删除（order/retire 保留）
    command.downgrade(cfg, "0003_profile_targets")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(rule_sources)").fetchall()}
        records = {row[0] for row in conn.execute(
            "SELECT key FROM data_migration_records").fetchall()}
    finally:
        conn.close()
    assert "ip_rules" not in columns
    assert "rule_source_ip_flags_v1" not in records
    assert "rule_source_order_invariants_v1" in records
    assert "retire_no_resolve_keywords_v1" in records

    # upgrade：列重建（server_default 归 0）→ 第二次业务迁移重新回填
    command.upgrade(cfg, "head")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        zeroed = conn.execute("SELECT COUNT(*) FROM rule_sources WHERE ip_rules = 1").fetchone()[0]
    finally:
        conn.close()
    assert zeroed == 0

    applied2 = await _run_migrations()
    assert applied2 == ["rule_source_ip_flags_v1"], "仅被 downgrade 撤销记账的迁移重放"
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        restored = conn.execute(
            "SELECT COUNT(*) FROM rule_sources WHERE ip_rules = 1").fetchone()[0]
    finally:
        conn.close()
    assert restored == sum(1 for rs in DEFAULT_RULE_SOURCES if rs.get("ip_rules"))


# ===== Round 2：0004 downgrade 无记账表 / nullable 指纹 =====


def test_0004_downgrade_without_records_table_succeeds(tmp_path):
    """无 data_migration_records 表（外部工具建库 / 尚未跑业务迁移）时，
    0004 downgrade 不因 DELETE 解析缺失表报错；ip_rules 列照常删除、数据行保留"""
    db_path = tmp_path / "no-records.db"
    command.upgrade(database_migration._alembic_config(db_path), "head")
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("DROP TABLE data_migration_records")
        conn.execute(
            "INSERT INTO rule_sources (name, url, action, rule_type, enabled, sort_order, ip_rules) "
            "VALUES ('X', 'https://x', 'DIRECT', 'RULE-SET', 1, 5, 1)")
    conn.close()

    # 不抛 no such table：Python 层先查 sqlite_master 再 DELETE
    command.downgrade(database_migration._alembic_config(db_path), "0003_profile_targets")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(rule_sources)").fetchall()}
        rows = conn.execute("SELECT name, sort_order FROM rule_sources").fetchall()
    finally:
        conn.close()
    assert "ip_rules" not in columns
    assert rows == [("X", 5)]


def test_unversioned_0003_nullable_sort_order_still_identified(tmp_path):
    """旧 0003 库（sort_order 本就可空）仍精确识别为 0003：
    0004 的 nullable 收紧要求不回溯误伤更早 revision"""
    db_path = tmp_path / "old-0003.db"
    command.upgrade(database_migration._alembic_config(db_path), "0003_profile_targets")
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("DROP TABLE alembic_version")
    conn.close()
    assert database_migration.identify_unversioned_schema(db_path) == "0003_profile_targets"


@pytest.mark.asyncio
async def test_unversioned_nullable_sort_order_not_stamped_as_0004(tmp_path):
    """表/列/CHECK 与 0004 完全一致但 sort_order 可空（外部工具加列）：
    fail closed 拒绝识别与 stamp，诊断含列名，live 不受影响"""
    legacy = tmp_path / "repo" / "data" / "surge_conf.db"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(database_migration._alembic_config(legacy), "0003_profile_targets")
    conn = sqlite3.connect(legacy)
    with conn:
        conn.execute("ALTER TABLE rule_sources "
                     "ADD COLUMN ip_rules BOOLEAN NOT NULL DEFAULT 0")
        conn.execute("DROP TABLE alembic_version")
    conn.close()

    assert database_migration.identify_unversioned_schema(legacy) is None

    paths = runtime_paths(tmp_path / "app-support")
    before = paths.database.read_bytes() if paths.database.exists() else None
    with pytest.raises(RuntimeError, match="sort_order"):
        await prepare_database(paths, legacy)
    # 诊断内容已由 match 校验（sort_order 列名进入错误消息）
    assert (paths.database.read_bytes() if paths.database.exists() else None) == before


# ===== 0005：udp_exceptions 表（FK CASCADE / unique / index / CHECK） =====


def _udp_exceptions_constraints(path: Path) -> dict:
    """读取迁移产物的 udp_exceptions 结构化约束：FK / unique / index / CHECK"""
    engine = sqlalchemy.create_engine(f"sqlite:///{path}")
    try:
        inspector = sqlalchemy.inspect(engine)
        fks = {fk["name"]: fk for fk in inspector.get_foreign_keys("udp_exceptions")}
        uniques = {u["name"]: u for u in inspector.get_unique_constraints("udp_exceptions")}
        indexes = {i["name"]: i for i in inspector.get_indexes("udp_exceptions")}
        checks = {cc["name"]: cc["sqltext"]
                  for cc in inspector.get_check_constraints("udp_exceptions")}
    finally:
        engine.dispose()
    return {"fk": fks, "unique": uniques, "index": indexes, "check": checks}


@pytest.mark.asyncio
async def test_legacy_0004_upgrades_to_0005_udp_exceptions(tmp_path):
    """0004 旧库经 staging 升级 head：udp_exceptions 表齐全且 stamp 到 0005，
    FK ON DELETE CASCADE、三元唯一、(profile_id, enabled, sort_order) 索引、
    match_type CHECK 全部落盘"""
    legacy = tmp_path / "repo" / "data" / "surge_conf.db"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(database_migration._alembic_config(legacy),
                    "0004_rule_source_ip_rules")

    paths = runtime_paths(tmp_path / "app-support")
    result = await prepare_database(paths, legacy)
    assert result.source == "legacy"
    assert sqlite_scalar(paths.database, "SELECT version_num FROM alembic_version") \
        == "0005_udp_exceptions"

    c = _udp_exceptions_constraints(paths.database)
    fk = c["fk"]["fk_udp_exceptions_profile_id"]
    assert fk["referred_table"] == "config_profiles"
    assert fk["options"]["ondelete"] == "CASCADE"
    assert c["unique"]["uq_udp_exceptions_profile_match"]["column_names"] == \
        ["profile_id", "match_type", "match_value"]
    assert c["index"]["ix_udp_exceptions_profile_enabled_sort"]["column_names"] == \
        ["profile_id", "enabled", "sort_order"]
    assert database_migration._normalize_check_expression(
        c["check"]["ck_udp_exceptions_match_type"]) == \
        "match_type IN ('process', 'mac_address')"


def test_0005_downgrade_drops_udp_exceptions(tmp_path):
    """head → 0004 downgrade：udp_exceptions 表与索引一并消失，可逆"""
    db_path = tmp_path / "downgrade.db"
    command.upgrade(database_migration._alembic_config(db_path), "head")
    command.downgrade(database_migration._alembic_config(db_path),
                      "0004_rule_source_ip_rules")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()
    assert "udp_exceptions" not in tables


@pytest.mark.asyncio
async def test_unversioned_0005_complete_identified(tmp_path):
    """无版本 0005 完整库（列+CHECK+nullable 全符合）仍精确识别为 head"""
    legacy = tmp_path / "repo" / "data" / "surge_conf.db"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(database_migration._alembic_config(legacy), "head")
    conn = sqlite3.connect(legacy)
    with conn:
        conn.execute("DROP TABLE alembic_version")
        conn.execute("DROP TABLE data_migration_records")
    conn.close()
    assert database_migration.identify_unversioned_schema(legacy) == \
        "0005_udp_exceptions"


def _make_unversioned_0005_sans_constraints(db_path: Path, ddl: str) -> None:
    """0004 库 + 外部工具手建 udp_exceptions（列名齐全但按给定 DDL 缺约束）"""
    command.upgrade(database_migration._alembic_config(db_path),
                    "0004_rule_source_ip_rules")
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(ddl)
        conn.execute("DROP TABLE alembic_version")
    conn.close()


_UNCONSTRAINED_UDP_DDL = (
    "CREATE TABLE udp_exceptions ("
    "id INTEGER PRIMARY KEY, profile_id INTEGER NOT NULL, "
    "match_type VARCHAR(20) NOT NULL, match_value VARCHAR(255) NOT NULL, "
    "display_name VARCHAR(100), enabled BOOLEAN NOT NULL DEFAULT 1, "
    "sort_order INTEGER NOT NULL DEFAULT 0)"
)

_NULLABLE_UDP_DDL = (
    "CREATE TABLE udp_exceptions ("
    "id INTEGER PRIMARY KEY, profile_id INTEGER, match_type VARCHAR(20), "
    "match_value VARCHAR(255), display_name VARCHAR(100), enabled BOOLEAN, "
    "sort_order INTEGER)"
)


@pytest.mark.parametrize(("ddl", "problem"), [
    (_UNCONSTRAINED_UDP_DDL, "ck_udp_exceptions_match_type"),
    (_NULLABLE_UDP_DDL, "match_value"),
])
def test_unversioned_0005_missing_constraints_fail_closed(tmp_path, ddl, problem):
    """无版本库列指纹与 0005 一致但缺 CHECK / 列可空性不符（外部工具建表）：
    识别 fail closed，绝不猜测性 stamp"""
    db_path = tmp_path / "sans.db"
    _make_unversioned_0005_sans_constraints(db_path, ddl)
    assert database_migration.identify_unversioned_schema(db_path) is None
    details = "; ".join(database_migration._schema_mismatch_details(db_path))
    assert problem in details


def test_unversioned_0004_without_udp_exceptions_still_identified(tmp_path):
    """0004 形状（无 udp_exceptions 表）仍识别为 0004：新 revision 不回溯误伤"""
    db_path = tmp_path / "legacy0004.db"
    command.upgrade(database_migration._alembic_config(db_path),
                    "0004_rule_source_ip_rules")
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("DROP TABLE alembic_version")
    conn.close()
    assert database_migration.identify_unversioned_schema(db_path) == \
        "0004_rule_source_ip_rules"


def test_udp_exceptions_check_constraints_match_metadata(tmp_path):
    """强制器：ORM metadata、SCHEMA_REVISION_CHECKS、Alembic 产物的
    udp_exceptions CHECK 三处同名同表达式"""
    from sqlalchemy.schema import CheckConstraint

    from app.models import UdpException

    head = database_migration._head_revision()
    registered = database_migration._accumulated_required_checks(head)["udp_exceptions"]
    metadata_checks = {
        c.name: database_migration._normalize_check_expression(c.sqltext.text)
        for c in UdpException.__table__.constraints
        if isinstance(c, CheckConstraint)
    }
    assert set(metadata_checks) == set(registered) == {"ck_udp_exceptions_match_type"}
    assert metadata_checks == {
        name: database_migration._normalize_check_expression(expr)
        for name, expr in registered.items()
    }

    path = tmp_path / "head.db"
    command.upgrade(database_migration._alembic_config(path), "head")
    c = _udp_exceptions_constraints(path)
    assert database_migration._normalize_check_expression(
        c["check"]["ck_udp_exceptions_match_type"]) == \
        metadata_checks["ck_udp_exceptions_match_type"]


def test_0005_head_fingerprint_covers_udp_exception_columns():
    """head 指纹的 udp_exceptions 列集合与 ORM metadata 一致（防登记漂移）"""
    from app.models import UdpException

    head = database_migration._head_revision()
    fingerprint = database_migration.schema_fingerprints()[head]
    assert fingerprint["udp_exceptions"] == \
        {c.name for c in UdpException.__table__.columns}


# ===== fix round 1：0005 新表结构（FK / unique / index）指纹验证 =====


def _synth_udp_exceptions(db_path: Path, *,
                          fk: str | None = "CASCADE",
                          uq: bool = True,
                          uq_cols: str = "profile_id, match_type, match_value",
                          ix: bool = True,
                          ix_cols: str = "profile_id, enabled, sort_order",
                          ix_unique: bool = False,
                          display_name_nullable: bool = True) -> None:
    """0004 库 + 手工合成 udp_exceptions（默认与 0005 迁移产物 inspector 视图
    等价；关掉/改写单个结构维度即可构造伪结构）"""
    command.upgrade(database_migration._alembic_config(db_path),
                    "0004_rule_source_ip_rules")
    fk_clause = "" if fk is None else (
        ", CONSTRAINT fk_udp_exceptions_profile_id "
        f"FOREIGN KEY (profile_id) REFERENCES config_profiles (id) ON DELETE {fk}")
    uq_clause = "" if not uq else (
        f", CONSTRAINT uq_udp_exceptions_profile_match UNIQUE ({uq_cols})")
    display_name = "display_name VARCHAR(100)" if display_name_nullable \
        else "display_name VARCHAR(100) NOT NULL"
    conn = sqlite3.connect(db_path)
    with conn:
        # 多行 DDL（逗号+换行分隔约束）：SQLite 方言对单行 DDL 的约束名
        # 解析会把相邻约束粘连读错；Alembic 产物即多行格式
        conn.execute(
            "CREATE TABLE udp_exceptions (\n"
            "\tid INTEGER PRIMARY KEY,\n"
            "\tprofile_id INTEGER NOT NULL,\n"
            "\tmatch_type VARCHAR(20) NOT NULL,\n"
            "\tmatch_value VARCHAR(255) NOT NULL,\n"
            f"\t{display_name},\n"
            "\tenabled BOOLEAN NOT NULL DEFAULT 1,\n"
            "\tsort_order INTEGER NOT NULL DEFAULT 0"
            f"{uq_clause}{fk_clause},\n"
            "\tCONSTRAINT ck_udp_exceptions_match_type "
            "CHECK (match_type IN ('process', 'mac_address'))\n)")
        if ix:
            conn.execute(
                f"CREATE {'UNIQUE ' if ix_unique else ''}INDEX "
                f"ix_udp_exceptions_profile_enabled_sort ON udp_exceptions ({ix_cols})")
        conn.execute("DROP TABLE alembic_version")
    conn.close()


def test_unversioned_0005_synthetic_complete_identified(tmp_path):
    """基准自证：手工合成的完整 0005（列/CHECK/FK/unique/index/nullable 全对）
    精确识别为 0005（合成 DDL 与迁移产物在 inspector 视图上等价）"""
    db_path = tmp_path / "synth_complete.db"
    _synth_udp_exceptions(db_path)
    assert database_migration.identify_unversioned_schema(db_path) == \
        "0005_udp_exceptions"


def test_unversioned_0005_missing_fk_rejected(tmp_path):
    """缺 FK（无外键的伪结构）：fail closed，诊断含外键名"""
    db_path = tmp_path / "no_fk.db"
    _synth_udp_exceptions(db_path, fk=None)
    assert database_migration.identify_unversioned_schema(db_path) is None
    details = "; ".join(database_migration._schema_mismatch_details(db_path))
    assert "fk_udp_exceptions_profile_id" in details
    assert "外键缺失或定义不符" in details


def test_unversioned_0005_wrong_ondelete_rejected(tmp_path):
    """FK 存在但 ondelete 不是 CASCADE（RESTRICT）：fail closed"""
    db_path = tmp_path / "wrong_ondelete.db"
    _synth_udp_exceptions(db_path, fk="RESTRICT")
    assert database_migration.identify_unversioned_schema(db_path) is None
    details = "; ".join(database_migration._schema_mismatch_details(db_path))
    assert "fk_udp_exceptions_profile_id" in details


def test_unversioned_0005_missing_unique_rejected(tmp_path):
    """缺唯一约束：fail closed，诊断含约束名"""
    db_path = tmp_path / "no_uq.db"
    _synth_udp_exceptions(db_path, uq=False)
    assert database_migration.identify_unversioned_schema(db_path) is None
    details = "; ".join(database_migration._schema_mismatch_details(db_path))
    assert "uq_udp_exceptions_profile_match" in details
    assert "唯一约束缺失或列不符" in details


def test_unversioned_0005_wrong_unique_column_order_rejected(tmp_path):
    """唯一约束列序不符（match_type 提前）：fail closed"""
    db_path = tmp_path / "wrong_uq_order.db"
    _synth_udp_exceptions(db_path, uq_cols="match_type, profile_id, match_value")
    assert database_migration.identify_unversioned_schema(db_path) is None
    details = "; ".join(database_migration._schema_mismatch_details(db_path))
    assert "uq_udp_exceptions_profile_match" in details


@pytest.mark.parametrize(("ix", "ix_cols", "ix_unique"), [
    (False, "profile_id, enabled, sort_order", False),   # 缺索引
    (True, "sort_order, enabled, profile_id", False),    # 列序错
    (True, "profile_id, enabled, sort_order", True),     # 被建成唯一索引
])
def test_unversioned_0005_index_defects_rejected(tmp_path, ix, ix_cols, ix_unique):
    """索引缺失/列序错/唯一标志不符：全部 fail closed，诊断含索引名"""
    db_path = tmp_path / "index_defect.db"
    _synth_udp_exceptions(db_path, ix=ix, ix_cols=ix_cols, ix_unique=ix_unique)
    assert database_migration.identify_unversioned_schema(db_path) is None
    details = "; ".join(database_migration._schema_mismatch_details(db_path))
    assert "ix_udp_exceptions_profile_enabled_sort" in details
    assert "索引缺失或定义不符" in details


def test_unversioned_0005_display_name_not_null_rejected(tmp_path):
    """display_name 被建成 NOT NULL（伪结构，其余全对）：违反登记的
    nullable=True 要求，fail closed；诊断含列名"""
    db_path = tmp_path / "dn_notnull.db"
    _synth_udp_exceptions(db_path, display_name_nullable=False)
    assert database_migration.identify_unversioned_schema(db_path) is None
    details = "; ".join(database_migration._schema_mismatch_details(db_path))
    assert "display_name" in details
    assert "期望 nullable" in details


def test_missing_udp_check_diagnosis_attributed_to_udp_table_only(tmp_path):
    """CHECK 诊断按真实表归属：缺 udp_exceptions 的约束只报告该表，
    绝不谎称 config_profiles 缺约束"""
    db_path = tmp_path / "check_attribution.db"
    _make_unversioned_0005_sans_constraints(db_path, _UNCONSTRAINED_UDP_DDL)
    assert database_migration.identify_unversioned_schema(db_path) is None
    details = database_migration._schema_mismatch_details(db_path)
    joined = "; ".join(details)
    assert "udp_exceptions 缺 CHECK 约束: ck_udp_exceptions_match_type" in joined
    assert "config_profiles 缺 CHECK 约束" not in joined
    assert "config_profiles CHECK 约束表达式不一致" not in joined


def test_udp_exception_structures_match_metadata(tmp_path):
    """三方强制器：冻结映射、ORM metadata、Alembic head 产物的 FK /
    唯一约束 / 索引名称、列序与动作（ondelete / unique 标志）完全一致"""
    from sqlalchemy.schema import ForeignKeyConstraint, UniqueConstraint

    from app.models import UdpException

    head = database_migration._head_revision()
    frozen_fks = database_migration._accumulated_required_foreign_keys(head)["udp_exceptions"]
    frozen_uqs = database_migration._accumulated_required_uniques(head)["udp_exceptions"]
    frozen_ixs = database_migration._accumulated_required_indexes(head)["udp_exceptions"]

    orm_fks = {
        c.name: {
            "local_columns": tuple(col.name for col in c.columns),
            "referred_table": tuple(c.elements)[0].column.table.name,
            "referred_columns": tuple(e.column.name for e in c.elements),
            "ondelete": (tuple(c.elements)[0].ondelete or "").strip().upper() or None,
        }
        for c in UdpException.__table__.constraints
        if isinstance(c, ForeignKeyConstraint)
    }
    assert set(orm_fks) == set(frozen_fks) == {"fk_udp_exceptions_profile_id"}
    assert orm_fks["fk_udp_exceptions_profile_id"] == dict(frozen_fks["fk_udp_exceptions_profile_id"])

    orm_uqs = {
        c.name: tuple(col.name for col in c.columns)
        for c in UdpException.__table__.constraints
        if isinstance(c, UniqueConstraint)
    }
    assert orm_uqs == {name: tuple(cols) for name, cols in frozen_uqs.items()}

    orm_ixs = {
        ix.name: {"column_names": tuple(col.name for col in ix.columns),
                  "unique": bool(ix.unique)}
        for ix in UdpException.__table__.indexes
    }
    assert set(orm_ixs) == set(frozen_ixs) == {"ix_udp_exceptions_profile_enabled_sort"}
    assert orm_ixs["ix_udp_exceptions_profile_enabled_sort"] == {
        "column_names": tuple(frozen_ixs["ix_udp_exceptions_profile_enabled_sort"]["column_names"]),
        "unique": False,
    }

    path = tmp_path / "head.db"
    command.upgrade(database_migration._alembic_config(path), "head")
    c = _udp_exceptions_constraints(path)
    fk = c["fk"]["fk_udp_exceptions_profile_id"]
    assert tuple(fk["constrained_columns"]) == ("profile_id",)
    assert fk["referred_table"] == "config_profiles"
    assert tuple(fk["referred_columns"]) == ("id",)
    assert (fk.get("options") or {}).get("ondelete", "").upper() == "CASCADE"
    assert c["unique"]["uq_udp_exceptions_profile_match"]["column_names"] == \
        ["profile_id", "match_type", "match_value"]
    ix = c["index"]["ix_udp_exceptions_profile_enabled_sort"]
    assert ix["column_names"] == ["profile_id", "enabled", "sort_order"]
    assert bool(ix["unique"]) is False
