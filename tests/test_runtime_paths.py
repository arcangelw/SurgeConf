from pathlib import Path

import pytest
from sqlalchemy import text

from app.runtime_paths import (resolve_runtime_paths, ensure_runtime_directories,
                               assert_regular_file, assert_sqlite_database_safe,
                               harden_runtime_permissions, refresh_runtime_permissions,
                               reserve_private_file)
from app.database import DatabaseRuntime


def test_data_dir_override_is_private(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    monkeypatch.setenv("SURGECONF_DATA_DIR", str(root))
    paths = resolve_runtime_paths(home=tmp_path / "home")
    ensure_runtime_directories(paths)
    assert paths.database == root / "surge_conf.db"
    assert paths.backups == root / "backups"
    assert paths.exports == root / "exports"
    assert paths.root.stat().st_mode & 0o777 == 0o700


@pytest.mark.asyncio
async def test_sqlite_connection_pragmas(tmp_path):
    runtime = DatabaseRuntime.create(tmp_path / "runtime.db")
    async with runtime.engine.connect() as conn:
        assert (await conn.scalar(text("PRAGMA journal_mode"))).lower() == "wal"
        assert await conn.scalar(text("PRAGMA synchronous")) == 1
        assert await conn.scalar(text("PRAGMA busy_timeout")) == 5000
        assert await conn.scalar(text("PRAGMA foreign_keys")) == 1
    await runtime.dispose()


def _make_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("SURGECONF_DATA_DIR", str(tmp_path / "runtime"))
    paths = resolve_runtime_paths(home=tmp_path / "home")
    ensure_runtime_directories(paths)
    return paths


def test_harden_converges_loose_permissions(tmp_path, monkeypatch):
    """0644 目录/文件、backups 内文件全部收敛到 0700/0600（幂等）"""
    import os
    paths = _make_paths(tmp_path, monkeypatch)
    paths.database.write_bytes(b"db")
    Path(f"{paths.database}-wal").write_bytes(b"wal")
    (paths.backups / "20260101T000000Z-0001.db").write_bytes(b"bk")
    (paths.exports / "conf.txt").write_text("x")
    os.chmod(paths.root, 0o755)
    os.chmod(paths.database, 0o644)
    os.chmod(f"{paths.database}-wal", 0o666)
    os.chmod(paths.backups / "20260101T000000Z-0001.db", 0o644)
    os.chmod(paths.exports / "conf.txt", 0o644)

    harden_runtime_permissions(paths)
    harden_runtime_permissions(paths)  # 幂等

    assert paths.root.stat().st_mode & 0o777 == 0o700
    assert paths.database.stat().st_mode & 0o777 == 0o600
    assert Path(f"{paths.database}-wal").stat().st_mode & 0o777 == 0o600
    assert (paths.backups / "20260101T000000Z-0001.db").stat().st_mode & 0o777 == 0o600
    assert (paths.exports / "conf.txt").stat().st_mode & 0o777 == 0o600


def test_harden_fails_closed_on_symlink(tmp_path, monkeypatch):
    """root/db/backups/log 任一为 symlink 时 fail closed"""
    import os
    paths = _make_paths(tmp_path, monkeypatch)
    target = tmp_path / "outside.txt"
    target.write_text("p")
    os.symlink(target, paths.exports / "linked.txt")
    with pytest.raises(RuntimeError, match="unexpected entry type"):
        harden_runtime_permissions(paths)

    # 顶层敏感文件（会话密钥）为 symlink 同样 fail closed
    paths2 = _make_paths(tmp_path, monkeypatch)
    os.symlink(target, paths2.session_secret)
    with pytest.raises(RuntimeError, match="not a regular file"):
        harden_runtime_permissions(paths2)


def test_harden_fails_closed_on_symlinked_directory(tmp_path, monkeypatch):
    """数据目录本身被符号链接替换时拒绝收敛"""
    import os
    real = tmp_path / "real-runtime"
    real.mkdir()
    monkeypatch.setenv("SURGECONF_DATA_DIR", str(tmp_path / "link-runtime"))
    os.symlink(real, tmp_path / "link-runtime")
    paths = resolve_runtime_paths(home=tmp_path / "home")
    with pytest.raises(RuntimeError, match="directory"):
        harden_runtime_permissions(paths)


def test_harden_rejects_unexpected_entry_type(tmp_path, monkeypatch):
    """backups 下的 fifo/socket 等异常类型被拒绝"""
    import os
    paths = _make_paths(tmp_path, monkeypatch)
    os.mkfifo(paths.backups / "pipe")
    with pytest.raises(RuntimeError, match="unexpected entry type"):
        harden_runtime_permissions(paths)


def test_ensure_directories_fails_closed_on_symlink(tmp_path, monkeypatch):
    """ensure_runtime_directories 自身（不是事后 harden）拒绝 symlink root"""
    import os
    real = tmp_path / "real-data"
    real.mkdir()
    monkeypatch.setenv("SURGECONF_DATA_DIR", str(tmp_path / "link-data"))
    os.symlink(real, tmp_path / "link-data")
    paths = resolve_runtime_paths(home=tmp_path / "home")
    with pytest.raises(RuntimeError, match="directory"):
        ensure_runtime_directories(paths)


def test_assert_regular_file_semantics(tmp_path):
    """打开前校验：普通文件/不存在通过；symlink/FIFO 拒绝"""
    import os
    f = tmp_path / "db.sqlite"
    f.write_bytes(b"x")
    assert_regular_file(f)                    # 普通文件 OK
    assert_regular_file(tmp_path / "missing")  # 不存在 OK（由调用方定创建语义）

    target = tmp_path / "target"
    target.write_bytes(b"y")
    link = tmp_path / "link.sqlite"
    os.symlink(target, link)
    with pytest.raises(RuntimeError, match="not a regular file"):
        assert_regular_file(link)

    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(RuntimeError, match="not a regular file"):
        assert_regular_file(fifo)


# ===== Round 3：no-follow 收口 =====


def test_reserve_private_file_semantics(tmp_path):
    """O_CREAT|O_EXCL|O_NOFOLLOW：新建 0600 普通文件、幂等复用、symlink 拒绝"""
    import os
    target = tmp_path / "staging.db"
    reserve_private_file(target)
    assert target.is_file()
    assert target.stat().st_mode & 0o777 == 0o600
    reserve_private_file(target)          # 已存在普通文件 → 幂等复用
    assert target.stat().st_mode & 0o777 == 0o600

    os.remove(target)
    os.symlink(tmp_path / "elsewhere", target)
    with pytest.raises(RuntimeError):
        reserve_private_file(target)


def test_assert_sqlite_database_safe_sidecars_and_parent(tmp_path):
    """sidecar symlink 拒绝；父链 symlink 拒绝；主文件缺失放行"""
    import os
    db = tmp_path / "live.db"
    db.write_bytes(b"db")
    assert_sqlite_database_safe(db)       # 正常通过

    wal = tmp_path / "live.db-wal"
    os.symlink(tmp_path / "evil", wal)
    with pytest.raises(RuntimeError, match="regular file"):
        assert_sqlite_database_safe(db)
    os.remove(wal)

    # 父链 symlink
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    db2 = real_parent / "live.db"
    db2.write_bytes(b"db")
    link_parent = tmp_path / "link-parent"
    os.symlink(real_parent, link_parent)
    with pytest.raises(RuntimeError, match="parent chain"):
        assert_sqlite_database_safe(link_parent / "live.db")

    # 主文件缺失放行（新建路径由 ensure/占位负责）
    assert_sqlite_database_safe(tmp_path / "missing.db")


def test_refresh_fails_closed_after_directory_replacement(tmp_path, monkeypatch):
    """启动后 backups/exports 被整体替换为 symlink → 操作前 refresh 拒绝"""
    import os
    paths = _make_paths(tmp_path, monkeypatch)
    refresh_runtime_permissions(paths)    # 正常收敛一次

    # 启动后运行目录整体被替换为 symlink → 下一次操作前 refresh 拒绝
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "surge_conf.db").write_bytes(b"stolen")
    os.symlink(outside, tmp_path / "replaced-runtime")
    monkeypatch.setenv("SURGECONF_DATA_DIR", str(tmp_path / "replaced-runtime"))
    paths2 = resolve_runtime_paths(home=tmp_path / "home")
    with pytest.raises(RuntimeError):
        refresh_runtime_permissions(paths2)
