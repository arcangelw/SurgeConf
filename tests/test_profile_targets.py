"""目标感知档案：目标/UDP 模式领域矩阵、迁移确定性与档案解析唯一语义

迁移用例全部基于 tmp_path 合成的旧 schema 副本（alembic 部分升级到
0002 + 存量数据行），绝不读取运行库。
"""

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import update as sa_update

from app.models import ConfigProfile
from app.profile_targets import (ProfileTarget, UdpSecurityMode,
                                 default_udp_mode, validate_target_mode)
from app.services.database_migration import _alembic_config
from app.services.profile_resolution import (DefaultProfileMissing,
                                             ProfileNotFound, resolve_profile)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PREV_REVISION = "0002_profile_subscription_tokens"


# ===== 领域矩阵（兼容组合唯一约束源） =====

@pytest.mark.parametrize(("target", "mode", "valid"), [
    ("mac_workstation", "strict", True),
    ("mac_workstation", "scoped", True),
    ("mac_workstation", "compatible", True),
    ("mac_gateway", "strict", True),
    ("mac_gateway", "scoped", True),
    ("mac_gateway", "compatible", True),
    ("ios_mobile", "strict", True),
    ("ios_mobile", "scoped", False),
    ("ios_mobile", "compatible", True),
])
def test_target_udp_mode_matrix(target, mode, valid):
    if valid:
        validate_target_mode(ProfileTarget(target), UdpSecurityMode(mode))
    else:
        with pytest.raises(ValueError, match="ios_mobile.*scoped"):
            validate_target_mode(ProfileTarget(target), UdpSecurityMode(mode))


def test_default_udp_mode_by_target():
    assert default_udp_mode(ProfileTarget.MAC_WORKSTATION) is UdpSecurityMode.SCOPED
    assert default_udp_mode(ProfileTarget.MAC_GATEWAY) is UdpSecurityMode.SCOPED
    assert default_udp_mode(ProfileTarget.IOS_MOBILE) is UdpSecurityMode.COMPATIBLE


# ===== fresh seed：新建默认档案显式写 target/mode（与旧档迁移分离） =====

@pytest.mark.asyncio
async def test_fresh_seed_default_profile_carries_role_target():
    """全新空库 seed_defaults：默认档案按 DEFAULT_INSTALLATION_ROLE + 中央映射
    显式写 target/mode（work_computer → mac_workstation/scoped），与列默认无关"""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from app.bootstrap import seed_defaults
    from app.security import DEFAULT_INSTALLATION_ROLE
    from app.profile_targets import default_target_for_role
    from app.database import Base

    engine = create_async_engine("sqlite+aiosqlite://")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, class_=AsyncSession,
                                             expire_on_commit=False)
        await seed_defaults(session_factory)
        async with session_factory() as db:
            rows = (await db.execute(
                select(ConfigProfile.target, ConfigProfile.udp_security_mode)
            )).fetchall()
    finally:
        await engine.dispose()

    expected_target = default_target_for_role(DEFAULT_INSTALLATION_ROLE)
    assert rows == [(expected_target.value, default_udp_mode(expected_target).value)]
    # 防漂移锁：默认角色 work_computer → mac_workstation/scoped（设计/README
    # 默认工作电脑；0003 旧档迁移 server_default 同值但路径独立，见上方迁移用例）
    assert (DEFAULT_INSTALLATION_ROLE, rows[0]) == ("work_computer", ("mac_workstation", "scoped"))


# ===== 迁移：合成旧 schema 副本 =====

def sqlite_rows(path: Path, sql: str):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


@pytest.fixture
def legacy_copy_db(tmp_path):
    """合成 0002 旧库副本并写入两条存量档案行"""
    path = tmp_path / "legacy_copy.db"
    command.upgrade(_alembic_config(path), PREV_REVISION)
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("INSERT INTO config_profiles (name, is_default) VALUES ('旧档A', 1)")
        conn.execute("INSERT INTO config_profiles (name, is_default) VALUES ('旧档B', 0)")
    conn.close()
    return path

@pytest.fixture
def upgraded_legacy_db(legacy_copy_db):
    command.upgrade(_alembic_config(legacy_copy_db), "head")
    return legacy_copy_db


@pytest.mark.asyncio
async def test_existing_profiles_migrate_to_workstation(upgraded_legacy_db):
    rows = sqlite_rows(upgraded_legacy_db, "SELECT target, udp_security_mode FROM config_profiles")
    assert rows and set(rows) == {("mac_workstation", "scoped")}


def test_check_constraints_reject_invalid_values(upgraded_legacy_db):
    """目标集合 / 模式集合 / ios_mobile+scoped 三类 CHECK 在 DB 层生效"""
    bad_rows = [
        ("坏目标", 0, "linux", "scoped"),
        ("坏模式", 0, "mac_workstation", "loose"),
        ("坏组合", 0, "ios_mobile", "scoped"),
    ]
    for name, is_default, target, mode in bad_rows:
        conn = sqlite3.connect(upgraded_legacy_db)
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute(
                    "INSERT INTO config_profiles (name, is_default, target, udp_security_mode) "
                    "VALUES (?, ?, ?, ?)", (name, is_default, target, mode))
        conn.close()


def test_downgrade_is_reversible(upgraded_legacy_db):
    """downgrade 删除两列与约束，存量行（含升级后新建的行）全部保留"""
    conn = sqlite3.connect(upgraded_legacy_db)
    with conn:
        conn.execute(
            "INSERT INTO config_profiles (name, is_default, target, udp_security_mode) "
            "VALUES ('新档', 0, 'ios_mobile', 'compatible')")
    conn.close()

    command.downgrade(_alembic_config(upgraded_legacy_db), PREV_REVISION)

    cols = {row[1] for row in sqlite_rows(upgraded_legacy_db, "PRAGMA table_info(config_profiles)")}
    assert "target" not in cols
    assert "udp_security_mode" not in cols
    names = sorted(row[0] for row in sqlite_rows(upgraded_legacy_db, "SELECT name FROM config_profiles"))
    assert names == sorted(["旧档A", "旧档B", "新档"])


# ===== resolve_profile 唯一解析语义 =====

@pytest.mark.asyncio
async def test_resolve_profile_explicit_id_never_falls_back(db):
    profile = await resolve_profile(db, 1)
    assert profile.id == 1
    # 显式 id 不存在：直接领域错误，绝不静默回退默认档案
    with pytest.raises(ProfileNotFound):
        await resolve_profile(db, 99999)


@pytest.mark.asyncio
async def test_resolve_profile_default_semantics(db):
    profile = await resolve_profile(db, None)
    assert profile.is_default is True

    await db.execute(sa_update(ConfigProfile).values(is_default=False))
    await db.commit()
    # 省略 id 且默认档案不存在：独立的领域错误
    with pytest.raises(DefaultProfileMissing):
        await resolve_profile(db, None)
