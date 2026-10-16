"""Alembic 迁移环境。

目标数据库只来自命令行 ``-x database_path=<path>``，由
``app/services/database_migration.py`` 程序化传入；本模块禁止
硬编码任何仓库路径或生产路径。基线 schema 手写于迁移脚本，
不做 autogenerate（``target_metadata`` 保持为 None）。
"""

import sqlalchemy
from alembic import context


def _database_url() -> str:
    raw_args = getattr(context.config.cmd_opts, "x", None) or []
    overrides = {}
    for item in raw_args:
        key, _, value = item.partition("=")
        overrides[key.strip()] = value.strip()
    if "database_path" not in overrides:
        raise SystemExit("必须通过 -x database_path=<path> 指定目标数据库")
    return f"sqlite:///{overrides['database_path']}"


def run_migrations_online() -> None:
    connectable = sqlalchemy.create_engine(_database_url())
    try:
        with connectable.connect() as connection:
            context.configure(connection=connection, render_as_batch=True)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


run_migrations_online()
