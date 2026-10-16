"""运行时路径解析与私有权限策略。

所有持久化产物（数据库、备份、导出、会话密钥）统一由 ``RuntimePaths``
解析；``SURGECONF_DATA_DIR`` 环境变量可整体重定向数据目录。

no-follow 原则：目录创建/验证逐层 ``O_DIRECTORY|O_NOFOLLOW``；文件收敛
与预占位经 fd 的 ``fstat``/``fchmod``；SQLite 目标文件以
``O_CREAT|O_EXCL|O_NOFOLLOW`` 原子保留为普通文件后再交给 SQLite。
"""

import os
import stat as stat_mod
from dataclasses import dataclass
from pathlib import Path


# console 回退日志文件名（launchd/nohup 的 stdout/stderr 重定向目标）：
# 与结构化事件日志 surgeconf.log 同目录但不同文件，杜绝轮转 rename 后
# 旧 FD 继续写已轮换 inode 的双 writer 问题
CONSOLE_LOG_FILENAME = "surgeconf-console.log"


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
    # 防御性：空 SURGECONF_DATA_DIR（set 但为空串）与 unset 同样回默认，
    # 避免 Path("") 解析为当前目录、把运行数据落进源码树
    env_dir = os.environ.get("SURGECONF_DATA_DIR", "")
    root = Path(env_dir if env_dir
                else home / "Library" / "Application Support" / "SurgeConf").expanduser()
    log_dir = home / "Library" / "Logs" / "SurgeConf"
    return RuntimePaths(root, root / "surge_conf.db", root / "backups",
                        root / "exports", root / "session.key",
                        log_dir, log_dir / "surgeconf.log")


# ===== no-follow 原语 =====


def _open_directory_nofollow(path: Path) -> int:
    """以 O_DIRECTORY|O_NOFOLLOW 打开目录并返回 fd（拒绝 symlink/非目录）。"""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise RuntimeError(f"cannot safely open directory {path}: {exc}") from exc
    st = os.fstat(fd)
    if not stat_mod.S_ISDIR(st.st_mode):
        os.close(fd)
        raise RuntimeError(f"runtime path is not a real directory: {path}")
    return fd


def _ensure_directory_safe(path: Path) -> None:
    """逐组件 dirfd walk 安全创建目录（支持多层缺失的嵌套路径）。

    从根目录开始以 ``O_DIRECTORY|O_NOFOLLOW`` 逐组件打开：已存在组件
    只做验证（绝不 chmod 属于他人的共享祖先）；缺失组件以
    ``mkdir(0o700, dir_fd=...)`` 创建后立即打开验证并 ``fchmod 0700``。
    全程不按完整路径打开，任何组件为 symlink/非目录时 fail closed。
    """
    path = Path(path)
    if not path.is_absolute():
        raise RuntimeError(f"runtime directory must be absolute: {path}")
    fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for comp in path.parts[1:]:
            if comp == os.curdir:
                continue
            try:
                child = os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=fd)
            except FileNotFoundError:
                try:
                    os.mkdir(comp, 0o700, dir_fd=fd)
                except OSError as exc:
                    raise RuntimeError(
                        f"cannot create runtime directory component {comp!r} "
                        f"of {path}: {exc}") from exc
                child = os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=fd)
                os.fchmod(child, 0o700)
            except NotADirectoryError as exc:
                raise RuntimeError(
                    f"runtime path component is not a directory: {path}") from exc
            except OSError as exc:
                raise RuntimeError(
                    f"cannot safely open runtime directory {path}: {exc}") from exc
            os.close(fd)
            fd = child
    finally:
        os.close(fd)


def _converge_managed_directory(path: Path) -> None:
    """SurgeConf 管理目录收敛 0700：fd 打开（no-follow）验证后 fchmod。"""
    fd = _open_directory_nofollow(path)
    try:
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def ensure_runtime_directories(paths: RuntimePaths) -> None:
    """创建数据目录（root/backups/exports），逐组件 dirfd walk。

    中间祖先（不属于 SurgeConf 的共享目录，如 ``~/Library`` 或自定义
    data dir 的上层）只验证、绝不修改权限；本次新建的中间组件以 0700
    创建；最终 managed root/backups/exports 收敛 0700（它们是 SurgeConf
    管理目录）。嵌套的新 ``SURGECONF_DATA_DIR`` 全程不 follow symlink。
    日志目录 ``log_dir`` 由实际启用文件日志的任务负责创建。
    """
    for directory in (paths.root, paths.backups, paths.exports):
        _ensure_directory_safe(directory)
        _converge_managed_directory(directory)


def assert_regular_file(path: Path) -> None:
    """敏感文件打开前校验：存在时必须是非 symlink 的普通文件。

    不存在时静默通过（由调用方决定创建语义）。
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat_mod.S_ISLNK(st.st_mode) or not stat_mod.S_ISREG(st.st_mode):
        raise RuntimeError(f"runtime path is not a regular file: {path}")


def assert_parent_chain(path: Path) -> None:
    """校验父目录链上每一层均为真实目录（拒绝任何 symlink 组件）。"""
    current = Path(path).parent
    while True:
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            raise RuntimeError(f"parent directory missing: {current}")
        if stat_mod.S_ISLNK(st.st_mode) or not stat_mod.S_ISDIR(st.st_mode):
            raise RuntimeError(f"parent chain contains non-directory/symlink: {current}")
        if current.parent == current:
            return
        current = current.parent


def assert_sqlite_database_safe(path: Path) -> None:
    """SQLite 数据库打开前校验：最终普通文件、父链无 symlink、
    现存 -wal/-shm 伴生文件同样为普通文件。

    缺失的 sidecar 可由 SQLite 在已验证的安全 parent 下自行创建；
    主文件本身缺失时放行（新建路径由调用方的 ensure/占位流程负责）。
    """
    import os as _os
    if not _os.path.lexists(path):
        return
    assert_regular_file(path)
    assert_parent_chain(path)
    for suffix in ("-wal", "-shm"):
        assert_regular_file(Path(f"{path}{suffix}"))


def reserve_private_file(path: Path) -> None:
    """为 SQLite 新目标原子保留普通文件：O_CREAT|O_EXCL|O_NOFOLLOW 0600。

    严格独占创建：目标已存在（无论何种类型/内容）即失败，绝不复用
    既有文件。已预占目标交给 SQLite 前的复核由
    ``validate_reserved_empty_file`` 负责。
    """
    path = Path(path)
    assert_parent_chain(path)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(
            f"cannot reserve private file, target already exists: {path}") from exc
    except OSError as exc:
        raise RuntimeError(f"cannot reserve private file {path}: {exc}") from exc
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def validate_reserved_empty_file(path: Path) -> None:
    """复核目标为本次流程预占的 0 字节 0600 普通文件后交付 SQLite。

    fd 打开（``O_NOFOLLOW|O_NONBLOCK``，后者防止 FIFO 阻塞打开）+
    ``fstat``：非 symlink、普通文件、权限 0600、size == 0；任一不
    满足即 fail closed（预占后目标被替换或篡改）。
    """
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise RuntimeError(
            f"reserved file is missing or unsafe: {path} ({exc})") from exc
    try:
        st = os.fstat(fd)
        if not stat_mod.S_ISREG(st.st_mode):
            raise RuntimeError(f"reserved file is not a regular file: {path}")
        if stat_mod.S_IMODE(st.st_mode) & 0o777 != 0o600:
            raise RuntimeError(f"reserved file is not mode 0600: {path}")
        if st.st_size != 0:
            raise RuntimeError(f"reserved file is not empty: {path}")
    finally:
        os.close(fd)


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
    """目录收敛：fd 打开（no-follow）验证后 fchmod 0700；不存在则忽略
    （创建是 ensure 的职责，收敛只处理已存在产物）。"""
    if not os.path.lexists(path):
        return
    fd = _open_directory_nofollow(path)
    try:
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def harden_private_file(path: Path) -> None:
    """文件收敛：fd 打开（no-follow）+ fstat 验证普通文件后 fchmod 0600。"""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError(f"runtime path is not a regular file: {path} ({exc})") from exc
    try:
        st = os.fstat(fd)
        if not stat_mod.S_ISREG(st.st_mode):
            raise RuntimeError(f"runtime path is not a regular file: {path}")
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def harden_runtime_permissions(paths: RuntimePaths) -> None:
    """运行产物权限统一收敛（幂等，bind/连接前调用）。

    - root/backups/exports/log_dir：fd no-follow 验证后 fchmod 0700；
    - live DB 及 -wal/-shm、会话密钥、事件日志与 console 回退日志：
      fd + fstat 验证后 fchmod 0600；
    - backups/exports 下的普通文件 fchmod 0600，子目录按目录规则收敛，
      其余类型（socket/fifo/设备/symlink）一律拒绝 —— fail closed。
    """
    for directory in (paths.root, paths.backups, paths.exports, paths.log_dir):
        _harden_directory(directory)
    for file in (paths.database,
                 Path(f"{paths.database}-wal"), Path(f"{paths.database}-shm"),
                 paths.session_secret, paths.log_file,
                 paths.log_dir / CONSOLE_LOG_FILENAME):
        harden_private_file(file)
    for directory in (paths.backups, paths.exports):
        if not os.path.lexists(directory):
            continue
        for child in sorted(directory.iterdir()):
            st = os.lstat(child)
            if stat_mod.S_ISDIR(st.st_mode):
                _harden_directory(child)
            elif stat_mod.S_ISREG(st.st_mode):
                harden_private_file(child)
            else:
                raise RuntimeError(f"unexpected entry type in runtime directory: {child}")
