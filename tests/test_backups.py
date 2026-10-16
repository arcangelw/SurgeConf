"""备份/恢复/重置生命周期测试。

覆盖：备份一致性 + 0600 + 原子发布 + 最近 10 份保留、白名单下载、
恢复/重置 confirm 门禁、staging 恢复与失败重绑原 live、
生成端点无服务器写入副作用、显式导出只写 exports 目录。
全部使用临时目录与合成数据，不接触真实运行库。
"""

import sqlite3

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.database import DatabaseRuntime
from app.models import SystemConfig
from app.services import backups

# client / runtime_paths fixture 复用 API 测试模块（同一 tmp_path/runtime 目录）
from tests.test_api_endpoints import client, runtime_paths  # noqa: F401


def sqlite_scalar(path, sql: str):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


async def _system_config(runtime, key: str):
    async with runtime.session_factory() as db:
        return (await db.execute(
            select(SystemConfig).where(SystemConfig.key == key)
        )).scalar_one_or_none()


@pytest_asyncio.fixture
async def runtime_db(runtime_paths, monkeypatch):
    """独立临时运行时：完整迁移建库 + 绑定 + marker 数据。

    backups 服务在函数内解析 ``app.database.database``（security.py 先例），
    patch 全局引用即定向到本运行时。
    """
    from app.services.database_migration import prepare_database

    await prepare_database(runtime_paths, runtime_paths.root / "missing.db")
    runtime = DatabaseRuntime.create(runtime_paths.database)
    monkeypatch.setattr("app.database.database", runtime)
    async with runtime.session_factory() as db:
        db.add(SystemConfig(key="marker", value="original"))
        await db.commit()
    yield runtime
    await runtime.dispose()


# ===== 简报规定用例 =====


@pytest.mark.asyncio
async def test_backup_is_integrity_checked_and_pruned(runtime_db, runtime_paths):
    for _ in range(12):
        await backups.create_backup("manual")
    items = backups.list_backups()
    assert len(items) == 10
    assert all(sqlite_scalar(item.path, "PRAGMA integrity_check") == "ok" for item in items)
    assert all(item.path.stat().st_mode & 0o777 == 0o600 for item in items)


@pytest.mark.asyncio
async def test_generate_does_not_write_server_file(client, runtime_paths):
    response = await client.get("/api/configs/generate?profile_id=1&filename=Example.conf")
    assert response.status_code == 200
    assert list(runtime_paths.exports.iterdir()) == []


# ===== 备份元数据与白名单 =====


@pytest.mark.asyncio
async def test_backup_metadata_has_no_absolute_paths(runtime_db):
    info = await backups.create_backup("manual")
    assert info.name.startswith("surge_conf_") and info.name.endswith("_manual.db")
    payload = info.as_dict()
    assert set(payload) == {"name", "created_at", "reason", "size", "schema_version"}
    assert payload["reason"] == "manual"
    assert payload["size"] > 0
    assert payload["schema_version"]  # head revision
    assert "/" not in payload["name"] and ".." not in payload["name"]
    assert [item.name for item in backups.list_backups()] == [info.name]


@pytest.mark.asyncio
async def test_restore_rejects_names_outside_whitelist(runtime_db):
    before = {item.name for item in backups.list_backups()}
    with pytest.raises(LookupError):
        await backups.restore_backup("../surge_conf.db")
    with pytest.raises(LookupError):
        await backups.restore_backup("nope.db")
    assert {item.name for item in backups.list_backups()} == before
    assert await _system_config(runtime_db, "marker") is not None


# ===== 恢复：staging 迁移后原子替换，失败重绑原 live =====


@pytest.mark.asyncio
async def test_restore_replaces_live_and_rebinds_runtime(runtime_db, runtime_paths):
    info = await backups.create_backup("manual")
    async with runtime_db.session_factory() as db:
        await db.execute(delete(SystemConfig).where(SystemConfig.key == "marker"))
        db.add(SystemConfig(key="added_after_backup", value="1"))
        await db.commit()

    await backups.restore_backup(info.name)

    assert runtime_db.path == runtime_paths.database
    assert await _system_config(runtime_db, "marker") is not None
    assert await _system_config(runtime_db, "added_after_backup") is None
    assert sqlite_scalar(runtime_paths.database, "PRAGMA integrity_check") == "ok"
    assert any(item.reason == "pre_restore" for item in backups.list_backups())


@pytest.mark.asyncio
async def test_restore_oldest_backup_survives_pre_restore_prune(runtime_db):
    """满额（10 份）时恢复最旧备份：pre_restore 发布触发 prune 不得删除
    点名备份；恢复成功、目标数据生效，最终总数仍 <= 10。"""
    oldest = await backups.create_backup("manual")
    for _ in range(9):
        await backups.create_backup("manual")
    assert len(backups.list_backups()) == 10

    async with runtime_db.session_factory() as db:
        await db.execute(delete(SystemConfig).where(SystemConfig.key == "marker"))
        db.add(SystemConfig(key="mutated_after_oldest", value="1"))
        await db.commit()

    await backups.restore_backup(oldest.name)

    # 恢复成功即点名备份全程未被删除；目标数据（最旧备份时刻）生效
    assert await _system_config(runtime_db, "marker") is not None
    assert await _system_config(runtime_db, "mutated_after_oldest") is None
    items = backups.list_backups()
    names = [item.name for item in items]
    assert oldest.name in names, "点名备份不得在 pre_restore prune 中丢失"
    assert any(item.reason == "pre_restore" for item in items)
    assert len(items) <= 10

@pytest.mark.asyncio
async def test_restore_failure_rebinds_original_live(runtime_db, runtime_paths, monkeypatch):
    info = await backups.create_backup("manual")

    async def broken_upgrade(staging):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.services.database_migration.upgrade_staging", broken_upgrade)
    with pytest.raises(RuntimeError, match="boom"):
        await backups.restore_backup(info.name)

    # live 未被半成品替换：原数据仍在且运行时重新绑定可用
    assert runtime_db.path == runtime_paths.database
    assert await _system_config(runtime_db, "marker") is not None
    assert sqlite_scalar(runtime_paths.database, "PRAGMA integrity_check") == "ok"


# ===== 重置：归并 Task 2 流程 + 消除备份无限累积 =====


@pytest.mark.asyncio
async def test_reset_database_rebuilds_and_prunes_backup_history(runtime_db, runtime_paths):
    for _ in range(11):
        await backups.reset_database()

    items = backups.list_backups()
    assert len(items) == 10
    assert all(item.reason == "pre_reset" for item in items)
    assert await _system_config(runtime_db, "marker") is None
    async with runtime_db.session_factory() as db:
        total = len((await db.execute(select(SystemConfig))).scalars().all())
    assert total > 0, "重置后必须绑定含种子数据的新库"
    assert sqlite_scalar(runtime_paths.database, "PRAGMA integrity_check") == "ok"


@pytest.mark.asyncio
async def test_reset_disposes_before_deleting_and_rebinds_runtime(
        runtime_db, runtime_paths, monkeypatch):
    from app.services import database_migration

    order = []
    original_dispose = runtime_db.dispose

    async def traced_dispose():
        order.append("dispose")
        assert runtime_paths.database.exists(), "dispose 必须发生在任何数据库文件删除之前"
        await original_dispose()

    monkeypatch.setattr(runtime_db, "dispose", traced_dispose)
    original_prepare = database_migration.prepare_database

    async def traced_prepare(p, legacy):
        order.append("prepare")
        assert not runtime_paths.database.exists(), "prepare 前旧库文件必须已被删除"
        return await original_prepare(p, legacy)

    monkeypatch.setattr("app.services.database_migration.prepare_database", traced_prepare)

    await backups.reset_database()

    assert order == ["dispose", "prepare"]
    assert runtime_db.path == runtime_paths.database
    assert await _system_config(runtime_db, "marker") is None


@pytest.mark.asyncio
async def test_reset_failure_restores_previous_live_database(runtime_db, runtime_paths, monkeypatch):
    async def broken_prepare(p, legacy):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.services.database_migration.prepare_database", broken_prepare)
    with pytest.raises(RuntimeError, match="boom"):
        await backups.reset_database()

    assert runtime_paths.database.exists()
    assert await _system_config(runtime_db, "marker") is not None


# ===== 管理端点：confirm 门禁、白名单下载、响应不含绝对路径 =====


@pytest.mark.asyncio
async def test_backup_endpoints_lifecycle(client, runtime_paths):
    resp = await client.post("/api/settings/backups")
    assert resp.status_code == 200
    created = resp.json()
    assert created["reason"] == "manual"

    listing = (await client.get("/api/settings/backups")).json()
    assert any(item["name"] == created["name"] for item in listing)
    assert str(runtime_paths.root) not in str(listing), "响应不得暴露绝对路径"

    download = await client.get(f"/api/settings/backups/{created['name']}")
    assert download.status_code == 200
    assert download.content.startswith(b"SQLite format 3")

    # 白名单之外（不存在 / 编码穿越 / 任意路径）一律 404
    for bad in ("nope.db", "..%2F..%2Fetc%2Fpasswd", "surge_conf.db"):
        assert (await client.get(f"/api/settings/backups/{bad}")).status_code == 404


@pytest.mark.asyncio
async def test_restore_and_reset_endpoints_require_confirm(client):
    resp = await client.post("/api/settings/reset", json={})
    assert resp.status_code == 400

    created = (await client.post("/api/settings/backups")).json()
    resp = await client.post(
        f"/api/settings/backups/{created['name']}/restore", json={"confirm": False})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_reset_endpoint_rebuilds_database(client, runtime_paths):
    resp = await client.post("/api/settings/reset", json={"confirm": True})
    assert resp.status_code == 200
    assert resp.json() == {"reset": True}
    assert sqlite_scalar(runtime_paths.database, "PRAGMA integrity_check") == "ok"
