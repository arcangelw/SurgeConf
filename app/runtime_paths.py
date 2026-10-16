"""运行时路径解析与私有权限策略。

所有持久化产物（数据库、备份、导出、会话密钥）统一由 ``RuntimePaths``
解析；``SURGECONF_DATA_DIR`` 环境变量可整体重定向数据目录。
"""

import os
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


def ensure_runtime_directories(paths: RuntimePaths) -> None:
    """创建数据目录（root/backups/exports），始终收敛到 0o700。

    日志目录 ``log_dir`` 由实际启用文件日志的任务负责创建，
    此处只管辖 SURGECONF_DATA_DIR 下的数据侧目录。
    """
    for directory in (paths.root, paths.backups, paths.exports):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)


def configure_private_umask() -> None:
    """在创建任何文件或数据库连接前收紧进程 umask，

    使 SQLite 后续创建的数据库与 WAL/SHM 伴生文件默认 0o600。
    """
    os.umask(0o077)
