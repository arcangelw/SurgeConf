from pathlib import Path

import pytest
from sqlalchemy import text

from app.runtime_paths import (resolve_runtime_paths, ensure_runtime_directories,
                               harden_runtime_permissions)
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
    with pytest.raises(RuntimeError, match="not a real directory"):
        harden_runtime_permissions(paths)


def test_harden_rejects_unexpected_entry_type(tmp_path, monkeypatch):
    """backups 下的 fifo/socket 等异常类型被拒绝"""
    import os
    paths = _make_paths(tmp_path, monkeypatch)
    os.mkfifo(paths.backups / "pipe")
    with pytest.raises(RuntimeError, match="unexpected entry type"):
        harden_runtime_permissions(paths)
