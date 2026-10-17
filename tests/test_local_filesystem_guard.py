"""Fix Slice 5 RED→GREEN：非本地挂载拒绝 + WAL 生效验证。

- 数据目录（``SURGECONF_DATA_DIR``）必须位于本地文件系统：Darwin 用
  statfs64 的 ``MNT_LOCAL`` 权威挂载标志判定，非 Darwin 用 /proc/mounts
  已知网络 fs 类型表（产品目标 macOS，见 docs/RUNTIME_OPERATIONS.md）。
  目录不存在时对最近现存父目录探针，创建后复核；探针错误 fail closed。
- SQLite 连接请求 ``journal_mode=WAL`` 后必须验证实际返回值为 wal
  （大小写/空白归一），否则关闭 cursor 抛 RuntimeError，后续 PRAGMA
  绝不执行；raw 备份落盘请求 DELETE 模式同样验证。
- 拒绝必须发生在任何迁移/备份/staging/live 文件产生之前；startup
  传播失败且 runtime 不宣告 ready。全部用 tmp + monkeypatch 模拟，
  绝不操作真实挂载。
"""

import sqlite3
from pathlib import Path

import pytest

from app import runtime_paths
from app.runtime_paths import (MountInfo, assert_local_filesystem,
                               assert_sqlite_database_safe,
                               ensure_runtime_directories,
                               refresh_runtime_permissions,
                               resolve_runtime_paths)

_FOLLOWUP_PRAGMAS = ("PRAGMA synchronous=NORMAL", "PRAGMA busy_timeout=5000",
                     "PRAGMA foreign_keys=ON")


def _paths(tmp_path, monkeypatch):
    monkeypatch.setenv("SURGECONF_DATA_DIR", str(tmp_path / "runtime"))
    return resolve_runtime_paths(home=tmp_path / "home")


# ===== A. 挂载守卫：Darwin local flag 缺失 / 探针错误 / 最近父 / 创建后复核 =====


def test_reject_non_local_mount_before_any_file_exists(tmp_path, monkeypatch):
    """模拟 Darwin MNT_LOCAL 未置位：目录创建前拒绝；错误不含绝对路径；零产物"""
    paths = _paths(tmp_path, monkeypatch)
    calls = []

    def fake_probe(directory):
        calls.append(Path(directory))
        return MountInfo(local=False, fs_type="nfs")

    monkeypatch.setattr(runtime_paths, "_probe_mount", fake_probe)
    with pytest.raises(RuntimeError) as exc:
        ensure_runtime_directories(paths)
    assert "non-local" in str(exc.value)
    assert str(tmp_path) not in str(exc.value), "错误文本不得泄露绝对路径"
    assert not paths.root.exists()
    assert list(tmp_path.iterdir()) == [], "拒绝必须发生在任何文件产生前"


def test_probe_failure_fails_closed(tmp_path, monkeypatch):
    """模拟探针自身错误（statfs 返回码异常/结构异常）：fail closed 传播"""
    paths = _paths(tmp_path, monkeypatch)

    def broken_probe(directory):
        raise RuntimeError("statfs64 probe failed with errno 5")

    monkeypatch.setattr(runtime_paths, "_probe_mount", broken_probe)
    with pytest.raises(RuntimeError, match="probe"):
        ensure_runtime_directories(paths)
    assert not paths.root.exists()


def test_missing_directory_probes_nearest_existing_parent(tmp_path, monkeypatch):
    """目标目录不存在：对最近现存父目录探针；创建成功后对 root 自身复核"""
    paths = _paths(tmp_path, monkeypatch)
    calls = []

    def fake_probe(directory):
        calls.append(Path(directory))
        return MountInfo(local=True, fs_type="apfs")

    monkeypatch.setattr(runtime_paths, "_probe_mount", fake_probe)
    ensure_runtime_directories(paths)
    assert calls[0] == tmp_path, "创建前应对最近现存父目录（tmp_path）探针"
    assert paths.root.is_dir()
    assert calls[1] == paths.root, "创建后应复核 root 自身"


def test_recheck_after_creation_catches_reclassification(tmp_path, monkeypatch):
    """创建后探针转为非本地（挂载属性翻转/替换）：复核拒绝 fail closed"""
    paths = _paths(tmp_path, monkeypatch)
    calls = []
    outcomes = [MountInfo(local=True, fs_type="apfs"),
                MountInfo(local=False, fs_type="smbfs")]

    def fake_probe(directory):
        calls.append(Path(directory))
        return outcomes[len(calls) - 1]

    monkeypatch.setattr(runtime_paths, "_probe_mount", fake_probe)
    with pytest.raises(RuntimeError, match="non-local"):
        ensure_runtime_directories(paths)
    assert paths.root.is_dir(), "复核发生在创建之后"
    assert calls == [tmp_path, paths.root]


def test_assert_local_filesystem_requires_absolute_path(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_paths, "_probe_mount",
                        lambda d: MountInfo(local=True, fs_type="apfs"))
    with pytest.raises(RuntimeError, match="absolute"):
        assert_local_filesystem(Path("relative/dir"))


def test_refresh_rejects_post_startup_network_redirection(tmp_path, monkeypatch):
    """启动后数据目录被重定向到网络卷：操作级安全入口（备份/恢复/重置）复核拒绝"""
    paths = _paths(tmp_path, monkeypatch)
    ensure_runtime_directories(paths)
    monkeypatch.setattr(runtime_paths, "_probe_mount",
                        lambda d: MountInfo(local=False, fs_type="nfs"))
    with pytest.raises(RuntimeError, match="non-local"):
        refresh_runtime_permissions(paths)


def test_assert_sqlite_database_safe_requires_local_parent(tmp_path, monkeypatch):
    """raw/staging 打开前的最终验证：父目录非本地即拒绝（含主文件不存在的全新路径）"""
    monkeypatch.setattr(runtime_paths, "_probe_mount",
                        lambda d: MountInfo(local=False, fs_type="cifs"))
    with pytest.raises(RuntimeError, match="non-local"):
        assert_sqlite_database_safe(tmp_path / "brand-new.db")
    monkeypatch.setattr(runtime_paths, "_probe_mount",
                        lambda d: MountInfo(local=True, fs_type="apfs"))
    assert_sqlite_database_safe(tmp_path / "brand-new.db")


@pytest.mark.asyncio
async def test_prepare_database_rejects_before_any_artifact(tmp_path, monkeypatch):
    """非本地挂载：prepare_database（迁移/staging/备份链入口）在产生任何文件前拒绝"""
    from app.services.database_migration import prepare_database

    paths = _paths(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime_paths, "_probe_mount",
                        lambda d: MountInfo(local=False, fs_type="nfs"))
    with pytest.raises(RuntimeError, match="non-local"):
        await prepare_database(paths, tmp_path / "legacy.db")
    assert not paths.root.exists(), "数据库/备份/staging 均未产生"


def test_backup_database_rejects_before_tmp_artifact(tmp_path, monkeypatch):
    """非本地挂载：备份操作在 tmp/target 任何文件产生前拒绝"""
    from app.services.backups import backup_database

    paths = _paths(tmp_path, monkeypatch)
    ensure_runtime_directories(paths)
    source = tmp_path / "src.db"
    conn = sqlite3.connect(source)
    conn.execute("create table t(x)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(runtime_paths, "_probe_mount",
                        lambda d: MountInfo(local=False, fs_type="nfs"))
    with pytest.raises(RuntimeError, match="non-local"):
        backup_database(source, paths, "test")
    assert list(paths.backups.iterdir()) == [], "不得残留任何 tmp/半成品"


# ===== B. Darwin statfs 探针：结构/返回码校验（真实 API 冒烟 + 故障注入） =====


def test_darwin_probe_reports_local_for_test_tmp(tmp_path):
    """真实 statfs64 探针：pytest tmp 位于本地卷，local=True 且类型名非空"""
    info = runtime_paths._darwin_statfs_info(tmp_path)
    assert isinstance(info.local, bool)
    assert info.fs_type, "fstypename 结构校验应得到非空类型名"
    assert info.local is True


def test_darwin_probe_fails_closed_on_error_return_code(tmp_path, monkeypatch):
    """statfs64 返回码非 0：RuntimeError fail closed"""
    monkeypatch.setattr(runtime_paths, "_darwin_statfs64",
                        lambda path, buf: -1)
    with pytest.raises(RuntimeError, match="statfs64"):
        runtime_paths._darwin_statfs_info(tmp_path)


def test_darwin_probe_fails_closed_on_malformed_buffer(tmp_path, monkeypatch):
    """返回码 0 但缓冲区全零（结构布局不符/字段异常）：fail closed"""
    monkeypatch.setattr(runtime_paths, "_darwin_statfs64",
                        lambda path, buf: 0)
    with pytest.raises(RuntimeError, match="statfs64"):
        runtime_paths._darwin_statfs_info(tmp_path)


# ===== C. 非 Darwin /proc/mounts 策略：已知网络 fs 不当本地 =====


def test_proc_mounts_longest_prefix_and_network_classification(tmp_path, monkeypatch):
    """最长前缀匹配：nfs 挂载下非本地；更深的本地 bind mount 覆盖为本地"""
    mounts = tmp_path / "mounts"
    mounts.write_text(
        "/dev/disk1s1 / apfs rw 0 0\n"
        "nas:/export /mnt/nas nfs rw 0 0\n"
        "//fileserver/share /mnt/smb cifs rw 0 0\n"
        "srv:/afp /mnt/afp afpfs rw 0 0\n"
        "/dev/disk2s1 /mnt/nas/deeper ext4 rw 0 0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_paths, "_PROC_MOUNTS", str(mounts))
    assert runtime_paths._proc_mounts_info(Path("/mnt/nas/other")) == \
        MountInfo(local=False, fs_type="nfs")
    assert runtime_paths._proc_mounts_info(Path("/mnt/smb/sub")) == \
        MountInfo(local=False, fs_type="cifs")
    assert runtime_paths._proc_mounts_info(Path("/mnt/afp")) == \
        MountInfo(local=False, fs_type="afpfs")
    deeper = runtime_paths._proc_mounts_info(Path("/mnt/nas/deeper/data"))
    assert deeper == MountInfo(local=True, fs_type="ext4")
    assert runtime_paths._proc_mounts_info(Path("/home/u/runtime")).local is True


def test_proc_mounts_unreadable_or_uncovered_fails_closed(tmp_path, monkeypatch):
    """无法读取/无覆盖条目：明确 fail closed（宁拒不误放）"""
    monkeypatch.setattr(runtime_paths, "_PROC_MOUNTS", str(tmp_path / "absent"))
    with pytest.raises(RuntimeError):
        runtime_paths._proc_mounts_info(tmp_path)
    empty = tmp_path / "empty-mounts"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setattr(runtime_paths, "_PROC_MOUNTS", str(empty))
    with pytest.raises(RuntimeError):
        runtime_paths._proc_mounts_info(tmp_path)


# ===== D. WAL 生效验证：模拟 SQLite 返回降级值一律拒绝 =====


class _FakeCursor:
    def __init__(self, row):
        self.row = row
        self.executed = []
        self.closed = False

    def execute(self, sql):
        self.executed.append(sql)

    def fetchone(self):
        return self.row

    def close(self):
        self.closed = True


class _FakeConn:
    def __init__(self, row):
        self._row = row
        self.cursor_obj = None

    def cursor(self):
        self.cursor_obj = _FakeCursor(self._row)
        return self.cursor_obj


@pytest.mark.parametrize("row", [
    ("delete",), ("memory",), ("truncate",), ("persist",), ("off",),
    (None,), (), None,
])
def test_wal_downgrade_rejected_before_followup_pragmas(row):
    """返回 delete/memory/None 等降级值：关闭 cursor、抛 RuntimeError、
    后续 PRAGMA（synchronous 等）绝不执行"""
    from app.database import apply_connection_pragmas

    conn = _FakeConn(row)
    with pytest.raises(RuntimeError, match="journal_mode"):
        apply_connection_pragmas(conn)
    assert conn.cursor_obj.executed == ["PRAGMA journal_mode=WAL"]
    assert conn.cursor_obj.closed is True


@pytest.mark.parametrize("row", [("wal",), ("WAL",), (" wal ",)])
def test_wal_reported_mode_normalized_before_acceptance(row):
    """严格大小写/空白归一后等于 wal 才放行，随后完整执行后续 PRAGMA"""
    from app.database import apply_connection_pragmas

    conn = _FakeConn(row)
    apply_connection_pragmas(conn)
    assert conn.cursor_obj.executed == \
        ["PRAGMA journal_mode=WAL", *_FOLLOWUP_PRAGMAS]
    assert conn.cursor_obj.closed is True


@pytest.mark.asyncio
async def test_real_connection_confirms_wal_end_to_end(tmp_path):
    """真实连接（本地 tmp 卷）：hook 请求后实际模式确为 wal"""
    from app.database import DatabaseRuntime

    runtime = DatabaseRuntime.create(tmp_path / "runtime.db")
    try:
        from sqlalchemy import text
        async with runtime.engine.connect() as conn:
            assert (await conn.scalar(text("PRAGMA journal_mode"))).lower() == "wal"
    finally:
        await runtime.dispose()
    raw = sqlite3.connect(tmp_path / "runtime.db")
    try:
        assert raw.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        raw.close()


# ===== E. raw 备份落盘：DELETE 模式同样验证返回 =====


def test_backup_finalize_verifies_delete_journal_mode(tmp_path, monkeypatch):
    """备份落盘请求 journal_mode=DELETE：返回非 delete 拒绝；正常路径
    确实收敛为 delete 模式"""
    from app.services import backups

    db = tmp_path / "b.db"
    conn = sqlite3.connect(db)
    conn.execute("create table t(x)")
    conn.commit()
    conn.close()
    backups._finalize_backup_file(db)
    check = sqlite3.connect(db)
    try:
        assert check.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        check.close()

    class _BadConn:
        def __init__(self):
            self.queries = 0

        def execute(self, sql):
            self.queries += 1
            return self

        def fetchone(self):
            return ("wal",) if self.queries == 1 else ("ok",)

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(backups.sqlite3, "connect", lambda p: _BadConn())
    with pytest.raises(RuntimeError, match="journal_mode"):
        backups._finalize_backup_file(tmp_path / "x.db")


# ===== F. startup：传播失败且 runtime 不宣告 ready =====


@pytest.mark.asyncio
async def test_startup_propagates_non_local_data_dir(tmp_path, monkeypatch):
    """lifespan 对非本地数据目录 fail closed：异常传播、零产物、runtime 未绑定"""
    from app.main import app as fastapi_app
    from app.database import database as global_database

    monkeypatch.setenv("SURGECONF_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(runtime_paths, "_probe_mount",
                        lambda d: MountInfo(local=False, fs_type="nfs"))
    with pytest.raises(RuntimeError, match="non-local"):
        async with fastapi_app.router.lifespan_context(fastapi_app):
            pass
    assert global_database.engine is None, "失败后 runtime 不得宣告 ready"
    assert not (tmp_path / "runtime").exists()


@pytest.mark.asyncio
async def test_startup_propagates_wal_rejection(tmp_path, monkeypatch):
    """WAL 请求未生效（模拟连接返回降级）：startup 传播失败、live 未产生、
    runtime 不宣告 ready"""
    from app.main import app as fastapi_app
    from app.database import database as global_database
    import app.database as database_mod

    monkeypatch.setenv("SURGECONF_DATA_DIR", str(tmp_path / "runtime"))

    def simulated_downgrade(dbapi_conn):
        raise RuntimeError("simulated journal_mode downgrade")

    monkeypatch.setattr(database_mod, "apply_connection_pragmas", simulated_downgrade)
    with pytest.raises(RuntimeError, match="simulated journal_mode downgrade"):
        async with fastapi_app.router.lifespan_context(fastapi_app):
            pass
    assert global_database.engine is None, "失败后 runtime 不得宣告 ready"
    root = tmp_path / "runtime"
    assert not (root / "surge_conf.db").exists(), "live 未产生"
    assert not (root / "surge_conf.db.staging").exists(), "staging 已被失败清理"
