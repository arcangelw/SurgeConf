"""运行时路径解析与私有权限策略。

所有持久化产物（数据库、备份、导出、会话密钥）统一由 ``RuntimePaths``
解析；``SURGECONF_DATA_DIR`` 环境变量可整体重定向数据目录。
"""

import os
import stat
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    database: Path
    backups: Path
    exports: Path
    session_secret: Path
    log_dir: Path
    log_file: Path


def resolve_runtime_paths(home: Path | None = None) -> RuntimePaths:
    home = home or Path.home()
    root = Path(os.environ.get(
        "SURGECONF_DATA_DIR",
        home / "Library" / "Application Support" / "SurgeConf",
    )).expanduser()
    log_dir = home / "Library" / "Logs" / "SurgeConf"
    return RuntimePaths(root, root / "surge_conf.db", root / "backups",
                        root / "exports", root / "session.key",
                        log_dir, log_dir / "surgeconf.log")


def _ensure_directory_safe(path: Path) -> None:
    """安全创建单个目录：先 lstat 校验（存在时拒绝 symlink/非目录），
    不存在则创建，最后再次 lstat 校验并收敛 0700 —— 全程不 follow symlink。"""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        path.mkdir(mode=0o700)
    else:
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise RuntimeError(f"runtime path is not a real directory: {path}")
    st = os.lstat(path)  # 创建后复核（防御 TOCTOU 之外的最小确认）
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f"runtime path is not a real directory: {path}")
    os.chmod(path, 0o700)


def ensure_runtime_directories(paths: RuntimePaths) -> None:
    """创建数据目录（root/backups/exports），始终以 lstat 安全方式收敛 0o700。

    已存在的目录必须通过 lstat 校验（symlink/非目录 fail closed），
    不允许先 follow 再收敛。日志目录 ``log_dir`` 由实际启用文件日志的
    任务负责创建，此处只管辖 SURGECONF_DATA_DIR 下的数据侧目录。
    """
    _ensure_directory_safe(paths.root)
    _ensure_directory_safe(paths.backups)
    _ensure_directory_safe(paths.exports)


def assert_regular_file(path: Path) -> None:
    """SQLite/敏感文件打开前校验：存在时必须是非 symlink 的普通文件。

    不存在时静默通过（由调用方决定创建语义）。
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise RuntimeError(f"runtime path is not a regular file: {path}")


def refresh_runtime_permissions(paths: RuntimePaths) -> None:
    """操作级安全入口：安全重建目录结构并收敛全部运行产物权限。

    备份/恢复/重置/导出在每次操作前调用，防止启动后目录被替换。
    """
    ensure_runtime_directories(paths)
    harden_runtime_permissions(paths)

def configure_private_umask() -> None:
    """在创建任何文件或数据库连接前收紧进程 umask，

    使 SQLite 后续创建的数据库与 WAL/SHM 伴生文件默认 0o600。
    """
    os.umask(0o077)


def _harden_directory(path: Path) -> None:
    """目录收敛：拒绝 symlink/非目录后 chmod 0700；不存在则忽略。"""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f"runtime path is not a real directory: {path}")
    os.chmod(path, 0o700)


def _harden_file(path: Path) -> None:
    """文件收敛：拒绝 symlink/非普通文件后 chmod 0600；不存在则忽略。"""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise RuntimeError(f"runtime path is not a regular file: {path}")
    os.chmod(path, 0o600)


def harden_runtime_permissions(paths: RuntimePaths) -> None:
    """运行产物权限统一收敛（幂等，bind/连接前调用）。

    - root/backups/exports/log_dir：lstat 拒绝 symlink/非目录，chmod 0700；
    - live DB 及 -wal/-shm、会话密钥、日志：拒绝 symlink/异常类型，chmod 0600；
    - backups/exports 下的普通文件 chmod 0600，子目录按目录规则收敛，
      其余类型（socket/fifo/设备）一律拒绝 —— fail closed。
    """
    for directory in (paths.root, paths.backups, paths.exports, paths.log_dir):
        _harden_directory(directory)
    for file in (paths.database,
                 Path(f"{paths.database}-wal"), Path(f"{paths.database}-shm"),
                 paths.session_secret, paths.log_file):
        _harden_file(file)
    for directory in (paths.backups, paths.exports):
        if not directory.exists():
            continue
        for child in directory.iterdir():
            st = os.lstat(child)
            if stat.S_ISDIR(st.st_mode):
                _harden_directory(child)
            elif stat.S_ISREG(st.st_mode):
                _harden_file(child)
            else:
                raise RuntimeError(f"unexpected entry type in runtime directory: {child}")
