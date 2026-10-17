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
    "0004_rule_source_ip_rules": {("rule_sources", "ip_rules")},
    "0005_udp_exceptions": {("udp_exceptions", "id"),
                            ("udp_exceptions", "profile_id"),
                            ("udp_exceptions", "match_type"),
                            ("udp_exceptions", "match_value"),
                            ("udp_exceptions", "display_name"),
                            ("udp_exceptions", "enabled"),
                            ("udp_exceptions", "sort_order")},
    "0006_subscription_lifecycle": {
        ("subscriptions", "last_attempt_at"),
        ("subscriptions", "last_success_at"),
        ("subscriptions", "next_update_at"),
        ("subscriptions", "last_error_code"),
        ("subscriptions", "last_node_count"),
        ("proxy_nodes", "stable_key")},
}

# 各 revision 相对前一 revision 删除的 (表, 列)。指纹推导时先应用新增再
# 应用删除；**带删列的 revision 在此登记**（0006 删除 subscriptions.last_update）。
SCHEMA_REVISION_REMOVED_COLUMNS: dict[str, set[tuple[str, str]]] = {
    "0006_subscription_lifecycle": {("subscriptions", "last_update")},
}

# 各 revision 必需的命名 CHECK 约束（表 → 约束名 → 冻结的规范化表达式）。
# **新增带约束的 revision 时在此登记**；无版本库识别缺任一必需约束、或
# 同名约束的实际表达式与冻结语义文本不一致（如被篡改为恒真）都 fail-closed
#（列指纹一致也不 stamp）。约束名与表达式由 ORM metadata（models.py
# __table_args__）与对应迁移声明，三处必须同名同表达式；测试强制
# Alembic 产物的 inspector sqltext 规范化后与冻结值一致。
SCHEMA_REVISION_CHECKS: dict[str, dict[str, dict[str, str]]] = {
    "0003_profile_targets": {
        "config_profiles": {
            "ck_config_profiles_target":
                "target IN ('mac_workstation', 'mac_gateway', 'ios_mobile')",
            "ck_config_profiles_udp_security_mode":
                "udp_security_mode IN ('strict', 'scoped', 'compatible')",
            "ck_config_profiles_target_udp_mode":
                "NOT (target = 'ios_mobile' AND udp_security_mode = 'scoped')",
        },
    },
    "0005_udp_exceptions": {
        "udp_exceptions": {
            "ck_udp_exceptions_match_type":
                "match_type IN ('process', 'mac_address')",
        },
    },
    "0006_subscription_lifecycle": {
        "proxy_nodes": {
            "ck_proxy_nodes_subscription_stable_key":
                "subscription_id IS NULL OR stable_key IS NOT NULL",
        },
    },
}

# 各 revision 相对更早 revision 新增/收紧的列可空性要求：(表, 列) → 期望 nullable。
# **新增带列属性要求的 revision 时在此登记**；识别时按 revision 顺序累积
#（后续 revision 继承更早要求），更早 revision 不被后续要求回溯误伤。
# True = 可空，False = NOT NULL。
SCHEMA_REVISION_COLUMN_NULLABILITY: dict[str, dict[tuple[str, str], bool]] = {
    "0004_rule_source_ip_rules": {("rule_sources", "sort_order"): False},
    "0005_udp_exceptions": {("udp_exceptions", "profile_id"): False,
                            ("udp_exceptions", "match_type"): False,
                            ("udp_exceptions", "match_value"): False,
                            ("udp_exceptions", "enabled"): False,
                            ("udp_exceptions", "sort_order"): False,
                            # display_name 登记为可空：伪结构（被建成 NOT NULL）
                            # 同样拒绝， nullable 要求不单向收紧
                            ("udp_exceptions", "display_name"): True},
    "0006_subscription_lifecycle": {
        # 新列全部显式登记 nullable=True：被建成 NOT NULL 的伪结构同样拒绝
        ("subscriptions", "last_attempt_at"): True,
        ("subscriptions", "last_success_at"): True,
        ("subscriptions", "next_update_at"): True,
        ("subscriptions", "last_error_code"): True,
        ("subscriptions", "last_node_count"): True,
        ("proxy_nodes", "stable_key"): True,
    },
}

# 各 revision 必需的结构化外键（revision → 表 → 约束名 → 冻结定义）。
# **新增带 FK 的 revision 时在此登记**；与 checks/nullability 同一累积语义：
# 后续 revision 继承更早要求，旧 revision 不被未来要求回溯误伤。
# ondelete 比较前规范化大写（SQLite inspector 将其放在 options 子字典）。
SCHEMA_REVISION_FOREIGN_KEYS: dict[str, dict[str, dict[str, dict[str, object]]]] = {
    "0005_udp_exceptions": {
        "udp_exceptions": {
            "fk_udp_exceptions_profile_id": {
                "local_columns": ("profile_id",),
                "referred_table": "config_profiles",
                "referred_columns": ("id",),
                "ondelete": "CASCADE",
            },
        },
    },
}

# 各 revision 必需的命名唯一约束（revision → 表 → 约束名 → 列顺序元组）
SCHEMA_REVISION_UNIQUE_CONSTRAINTS: dict[str, dict[str, dict[str, tuple[str, ...]]]] = {
    "0005_udp_exceptions": {
        "udp_exceptions": {
            "uq_udp_exceptions_profile_match": ("profile_id", "match_type", "match_value"),
        },
    },
}

# 各 revision 必需的命名索引（revision → 表 → 索引名 → 冻结定义；
# unique=False 表示普通索引，唯一索引须显式声明 unique=True）
SCHEMA_REVISION_INDEXES: dict[str, dict[str, dict[str, dict[str, object]]]] = {
    "0005_udp_exceptions": {
        "udp_exceptions": {
            "ix_udp_exceptions_profile_enabled_sort": {
                "column_names": ("profile_id", "enabled", "sort_order"),
                "unique": False,
            },
        },
    },
    "0006_subscription_lifecycle": {
        "proxy_nodes": {
            # 部分唯一索引：sqlite_where 冻结 WHERE 表达式（规范化后比较），
            # 缺 WHERE 或 WHERE 不符同样 fail closed
            "uq_proxy_nodes_subscription_stable_key": {
                "column_names": ("subscription_id", "stable_key"),
                "unique": True,
                "sqlite_where": "stable_key IS NOT NULL",
            },
        },
    },
}

# 识别时忽略的表：alembic_version 属版本簿记；data_migration_records 由 _sync_schema 补建
_IDENTIFY_IGNORED_TABLES = {"alembic_version", "data_migration_records"}


def schema_fingerprints() -> dict[str, dict[str, frozenset[str]]]:
    """revision → 表→列指纹：0001 基线 + 逐 revision 冻结增量（新增/删除）推导"""
    fingerprints: dict[str, dict[str, frozenset[str]]] = {}
    tables = {table: set(columns) for table, columns in _BASELINE_TABLE_COLUMNS.items()}
    fingerprints[BASELINE_REVISION] = {t: frozenset(c) for t, c in tables.items()}
    for revision, additions in SCHEMA_REVISION_DELTAS.items():
        for table, column in additions:
            tables.setdefault(table, set()).add(column)
        for table, column in SCHEMA_REVISION_REMOVED_COLUMNS.get(revision, set()):
            tables.get(table, set()).discard(column)
        fingerprints[revision] = {t: frozenset(c) for t, c in tables.items()}
    return fingerprints


def _normalize_actual_fk(fk: dict) -> dict:
    """inspector FK 原始 dict → 规范化形态（列序元组 + ondelete 大写；
    SQLAlchemy 2.x 的 SQLite 方言把 ondelete 放在 options 子字典）"""
    ondelete = (fk.get("options") or {}).get("ondelete")
    return {
        "constrained_columns": tuple(fk.get("constrained_columns") or ()),
        "referred_table": fk.get("referred_table"),
        "referred_columns": tuple(fk.get("referred_columns") or ()),
        "ondelete": ondelete.strip().upper() if isinstance(ondelete, str) else None,
    }


def _actual_schema(database_path: Path) -> tuple[set[str], dict[str, set[str]],
                                                  dict[str, dict[str, str]],
                                                  dict[str, dict[str, bool]],
                                                  dict[str, dict[str, dict]],
                                                  dict[str, dict[str, tuple]],
                                                  dict[str, dict[str, dict]]]:
    assert_sqlite_database_safe(database_path)
    """实际库的表集合、表→列集合、表→命名 CHECK 约束集合、表→列 nullable、
    表→命名 FK（规范化）、表→命名唯一约束（列序元组）、表→命名索引
    （列序 + unique 标志）（均排除簿记表）。

    约束与列属性经 SQLAlchemy inspector 读取，不解析 sqlite_master DDL 文本
    （避免依赖空白/顺序的脆弱正则）。
    """
    engine = sqlalchemy.create_engine(f"sqlite:///{database_path}")
    try:
        inspector = sqlalchemy.inspect(engine)
        tables = {t for t in inspector.get_table_names()
                  if t not in _IDENTIFY_IGNORED_TABLES}
        columns = {t: {c["name"] for c in inspector.get_columns(t)} for t in tables}
        nullability = {t: {c["name"]: bool(c["nullable"]) for c in inspector.get_columns(t)}
                       for t in tables}
        checks = {t: {cc["name"]: cc["sqltext"] for cc in
                      inspector.get_check_constraints(t)}
                  for t in tables}
        foreign_keys = {t: {fk["name"]: _normalize_actual_fk(fk) for fk in
                            inspector.get_foreign_keys(t) if fk.get("name")}
                        for t in tables}
        uniques = {t: {u["name"]: tuple(u.get("column_names") or ())
                       for u in inspector.get_unique_constraints(t) if u.get("name")}
                   for t in tables}
        indexes = {t: {i["name"]: {"column_names": tuple(i.get("column_names") or ()),
                                    "unique": bool(i.get("unique")),
                                    "sqlite_where": _normalize_check_expression(
                                        str((i.get("dialect_options") or {})
                                            .get("sqlite_where")))
                                    if (i.get("dialect_options") or {})
                                            .get("sqlite_where") is not None
                                    else None}
                       for i in inspector.get_indexes(t) if i.get("name")}
                   for t in tables}
    finally:
        engine.dispose()
    return (tables, columns, checks, nullability,
            foreign_keys, uniques, indexes)


def _normalize_check_expression(sqltext: str) -> str:
    """仅消除确定性的表示差异，绝不改变布尔语义。

    处理三类：SQLite/Alembic/inspector 之间的空白差异（含换行折叠）、
    包裹整个表达式的外层成对括号（字符串字面量感知，含 '' 转义）、
    标识符引用符统一（`x`/[x] → "x"；单引号字符串字面量原样保留）。
    不做 token 排序、大小写折叠或删引号——那些可能把不同布尔表达式判同。
    """
    text = sqltext.strip().replace("`", '"').replace("[", '"').replace("]", '"')
    while len(text) >= 2 and text[0] == "(" and text[-1] == ")":
        depth = 0
        in_string = False
        wrapped = True
        index = 0
        while index < len(text):
            char = text[index]
            if in_string:
                if char == "'":
                    if index + 1 < len(text) and text[index + 1] == "'":
                        index += 2  # '' 转义的单引号
                        continue
                    in_string = False
            elif char == "'":
                in_string = True
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and index != len(text) - 1:
                    wrapped = False  # 外层括号在末尾之前已闭合 → 非整体包裹
                    break
            index += 1
        if not wrapped or in_string or depth != 0:
            break
        text = text[1:-1].strip()
    return " ".join(text.split())


def _accumulated_required_checks(revision: str) -> dict[str, dict[str, str]]:
    """截至该 revision（含）累积的全部必需命名 CHECK 约束。

    约束随结构继承：后续 revision 不引入新约束时，识别该 revision 仍须
    校验更早 revision 的全部约束（否则篡改旧约束的库会被无约束的新
    revision 误 stamp）。baseline（0001）无约束。
    """
    revisions = list(SCHEMA_REVISION_DELTAS)
    if revision not in revisions:
        return {}
    accumulated: dict[str, dict[str, str]] = {}
    for known in revisions:
        for table, checks in SCHEMA_REVISION_CHECKS.get(known, {}).items():
            accumulated.setdefault(table, {}).update(checks)
        if known == revision:
            break
    return accumulated


def _accumulated_column_nullability(revision: str) -> dict[tuple[str, str], bool]:
    """截至该 revision（含）累积的全部列可空性要求。

    列属性随结构继承：识别 revision 须满足自身及更早 revision 收紧的全部
    nullable 要求；baseline / 未登记的 revision 返回空（不施加额外要求）。
    """
    revisions = list(SCHEMA_REVISION_DELTAS)
    if revision not in revisions:
        return {}
    accumulated: dict[tuple[str, str], bool] = {}
    for known in revisions:
        accumulated.update(SCHEMA_REVISION_COLUMN_NULLABILITY.get(known, {}))
        if known == revision:
            break
    return accumulated


def _accumulated_required_foreign_keys(revision: str) -> dict[str, dict[str, dict]]:
    """截至该 revision（含）累积的全部必需命名 FK（同 checks 的继承语义）"""
    revisions = list(SCHEMA_REVISION_DELTAS)
    if revision not in revisions:
        return {}
    accumulated: dict[str, dict[str, dict]] = {}
    for known in revisions:
        for table, fks in SCHEMA_REVISION_FOREIGN_KEYS.get(known, {}).items():
            accumulated.setdefault(table, {}).update(fks)
        if known == revision:
            break
    return accumulated


def _accumulated_required_uniques(revision: str) -> dict[str, dict[str, tuple]]:
    """截至该 revision（含）累积的全部必需命名唯一约束（同继承语义）"""
    revisions = list(SCHEMA_REVISION_DELTAS)
    if revision not in revisions:
        return {}
    accumulated: dict[str, dict[str, tuple]] = {}
    for known in revisions:
        for table, uniques in SCHEMA_REVISION_UNIQUE_CONSTRAINTS.get(known, {}).items():
            accumulated.setdefault(table, {}).update(uniques)
        if known == revision:
            break
    return accumulated


def _accumulated_required_indexes(revision: str) -> dict[str, dict[str, dict]]:
    """截至该 revision（含）累积的全部必需命名索引（同继承语义）"""
    revisions = list(SCHEMA_REVISION_DELTAS)
    if revision not in revisions:
        return {}
    accumulated: dict[str, dict[str, dict]] = {}
    for known in revisions:
        for table, indexes in SCHEMA_REVISION_INDEXES.get(known, {}).items():
            accumulated.setdefault(table, {}).update(indexes)
        if known == revision:
            break
    return accumulated


def _fk_requirement_satisfied(actual: dict | None, required: dict) -> bool:
    """实际 FK 是否与冻结定义一致（本地/引用列序、引用表、ondelete）"""
    if not actual:
        return False
    return (actual["constrained_columns"] == tuple(required["local_columns"])
            and actual["referred_table"] == required["referred_table"]
            and actual["referred_columns"] == tuple(required["referred_columns"])
            and actual["ondelete"] == required["ondelete"])


def _structure_requirements_satisfied(revision: str,
                                      actual_foreign_keys: dict[str, dict[str, dict]],
                                      actual_uniques: dict[str, dict[str, tuple]],
                                      actual_indexes: dict[str, dict[str, dict]]) -> bool:
    """revision 累积的 FK / 唯一约束 / 索引要求是否全部满足
    （缺任一或任一字段不符 → False，识别 fail closed）"""
    for table, required in _accumulated_required_foreign_keys(revision).items():
        actual = actual_foreign_keys.get(table, {})
        if not all(_fk_requirement_satisfied(actual.get(name), req)
                   for name, req in required.items()):
            return False
    for table, required in _accumulated_required_uniques(revision).items():
        actual = actual_uniques.get(table, {})
        if any(actual.get(name) != tuple(columns) for name, columns in required.items()):
            return False
    for table, required in _accumulated_required_indexes(revision).items():
        actual = actual_indexes.get(table, {})
        for name, req in required.items():
            entry = actual.get(name)
            if (not entry
                    or entry["column_names"] != tuple(req["column_names"])
                    or entry["unique"] != bool(req["unique"])
                    or entry.get("sqlite_where") != (
                        _normalize_check_expression(req["sqlite_where"])
                        if req.get("sqlite_where") is not None else None)):
                return False
    return True


def _required_check_problems(revision: str,
                             actual_checks: dict[str, dict[str, str]],
                             table: str | None = None
                             ) -> tuple[set[str], set[str]]:
    """相对 revision 必需约束（累积）返回（缺失的约束名, 表达式不一致的约束名）。

    table=None 汇总全部表；指定表时只检查该表（诊断按真实表归属报告，
    不把 A 表缺失谎报到 B 表头上）。
    """
    missing: set[str] = set()
    mismatched: set[str] = set()
    for required_table, required in _accumulated_required_checks(revision).items():
        if table is not None and required_table != table:
            continue
        actual = actual_checks.get(required_table, {})
        for name, expected in required.items():
            if name not in actual:
                missing.add(name)
            elif _normalize_check_expression(actual[name]) != \
                    _normalize_check_expression(expected):
                mismatched.add(name)
    return missing, mismatched


def identify_unversioned_schema(database_path: Path) -> str | None:
    """无版本库精确识别：表/列与某个 revision 指纹完全一致、且该 revision
    必需的命名 CHECK / FK / 唯一约束 / 索引与列可空性要求全部满足，
    才返回该 revision。

    额外列、缺失列、混合多个 revision 的列、缺任一必需约束或结构字段
    不符都返回 None（绝不猜测性 stamp）。
    """
    (actual_tables, actual_columns, actual_checks, actual_nullability,
     actual_foreign_keys, actual_uniques, actual_indexes) = _actual_schema(database_path)
    for revision, fingerprint in schema_fingerprints().items():
        if actual_tables != set(fingerprint):
            continue
        if not all(actual_columns[table] == set(columns)
                   for table, columns in fingerprint.items()):
            continue
        missing, mismatched = _required_check_problems(revision, actual_checks)
        if missing or mismatched:
            continue
        # 列可空性（累积要求）：不满足同样 fail closed，绝不猜测性 stamp
        if any(actual_nullability.get(table, {}).get(column) != expected
               for (table, column), expected
               in _accumulated_column_nullability(revision).items()):
            continue
        # 结构化约束（FK / 唯一约束 / 索引，累积要求）：缺任一或字段不符
        # 同样 fail closed（如缺 CASCADE、唯一约束列序错、索引被建成唯一）
        if not _structure_requirements_satisfied(revision, actual_foreign_keys,
                                                 actual_uniques, actual_indexes):
            continue
        return revision
    return None


def _schema_mismatch_details(database_path: Path) -> list[str]:
    """与最近 revision 指纹的差异描述：只含表/列/约束名，绝不含数据值"""
    head = _head_revision()
    fingerprint = schema_fingerprints()[head]
    (actual_tables, actual_columns, actual_checks, actual_nullability,
     actual_foreign_keys, actual_uniques, actual_indexes) = _actual_schema(database_path)
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
    # 约束差异按真实表归属报告：逐表调用，缺 udp_exceptions 的约束绝不
    # 谎报到 config_profiles 头上
    constrained_tables = sorted({t for t in _accumulated_required_checks(head)}
                                | set(_accumulated_required_foreign_keys(head))
                                | set(_accumulated_required_uniques(head))
                                | set(_accumulated_required_indexes(head)))
    for table in constrained_tables:
        missing, mismatched = _required_check_problems(head, actual_checks, table=table)
        if missing:
            problems.append(f"{table} 缺 CHECK 约束: {', '.join(sorted(missing))}")
        if mismatched:
            problems.append(
                f"{table} CHECK 约束表达式不一致: {', '.join(sorted(mismatched))}")
        for name, req in _accumulated_required_foreign_keys(head).get(table, {}).items():
            if not _fk_requirement_satisfied(
                    actual_foreign_keys.get(table, {}).get(name), req):
                problems.append(f"{table} 外键缺失或定义不符: {name}")
        for name, columns in _accumulated_required_uniques(head).get(table, {}).items():
            if actual_uniques.get(table, {}).get(name) != tuple(columns):
                problems.append(f"{table} 唯一约束缺失或列不符: {name}")
        for name, req in _accumulated_required_indexes(head).get(table, {}).items():
            entry = actual_indexes.get(table, {}).get(name)
            if (not entry
                    or entry["column_names"] != tuple(req["column_names"])
                    or entry["unique"] != bool(req["unique"])
                    or entry.get("sqlite_where") != (
                        _normalize_check_expression(req["sqlite_where"])
                        if req.get("sqlite_where") is not None else None)):
                problems.append(f"{table} 索引缺失或定义不符: {name}")
    for (table, column), expected in sorted(_accumulated_column_nullability(head).items()):
        actual = actual_nullability.get(table, {}).get(column)
        if actual != expected:
            describe = "缺失" if actual is None else ("NOT NULL" if not actual else "nullable")
            wanted = "NOT NULL" if not expected else "nullable"
            problems.append(f"{table}.{column} 列可空性不符: 期望 {wanted}，实际 {describe}")
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
    """关闭引擎后 checkpoint WAL 并删除 -wal/-shm，确保单文件原子替换。

    TRUNCATE 必须完整完成：返回 busy（并发 reader 持 WAL 快照）时
    fail closed 抛错且不删 sidecar——live 保持「main + 可恢复 WAL」
    的逻辑完整状态，绝不允许「未 checkpoint 就删 WAL」。
    """
    conn = sqlite3.connect(database_path)
    try:
        busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
        if busy:
            raise RuntimeError(
                "WAL checkpoint 未完成（存在并发读取），拒绝清理 sidecar")
        conn.commit()
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        sidecar = database_path.with_name(database_path.name + suffix)
        if sidecar.exists():
            sidecar.unlink()


def verify_staging(staging: Path) -> None:
    """staging 侧全部可能失败的准备：integrity 校验、伴生文件收敛、
    权限 0600。完成前绝不触碰 live。"""
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


def _prepare_live_for_replacement(live: Path) -> None:
    """无连接维护边界内处理旧 live：WAL 已提交页安全并入 main 后才
    清理 sidecars，供原子替换。

    - checkpoint 失败（busy/IO）：抛错且不动任何 sidecar，live 保持
      「main + 可恢复 WAL」逻辑完整；
    - checkpoint 成功后 sidecar 删除/replace 失败：WAL 已并入 main，
      live 逻辑完整（字节因合法 checkpoint 变化），异常向上传播；
    - live 缺失：清理可能残留的孤儿 sidecar（残留会污染新库）。
    """
    if live.exists():
        _checkpoint_and_remove_sidecars(live)
    else:
        _remove_live_sidecars(live)


def verify_and_publish(staging: Path, target: Path) -> None:
    """安全发布：staging 全部可能失败的准备（verify_staging）先完成，
    live 只在 WAL 安全并入 main 后才被动，最后原子替换。"""
    verify_staging(staging)
    _prepare_live_for_replacement(target)
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

    prepare_database 与备份恢复/重置共用此链。发布顺序保证任何失败
    （staging 升级/integrity/权限/checkpoint/sidecar/replace）都让
    live 保持逻辑完整：staging 侧全部可能失败的准备（verify_staging）
    先完成，live 只在「WAL 已提交页安全并入 main」之后才被动——
    绝不出现「旧 main + 已删未 checkpoint WAL」；checkpoint 之后 live
    字节可能因合法 checkpoint 变化，但已提交数据不丢。失败丢弃
    staging。要求调用方处于无连接维护边界（live 已 dispose）。
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
        # 存在待执行项：时间戳备份 + staging 上完成后原子替换；失败删除
        # staging，live 保持逻辑完整（checkpoint 后字节可能合法变化）
        backup = backup_database(live, paths, current or "pre_alembic")
        applied = await rebuild_from_source(live, paths)
        return MigrationResult("live", applied, backup)

    # 路径 1：新旧库均不存在 —— staging 上从零构建后原子发布
    applied = await rebuild_from_source(None, paths)
    return MigrationResult("fresh", applied, None)
