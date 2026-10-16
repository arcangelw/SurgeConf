"""显式 SQLite 运行时：单引擎 + 会话工厂 + 每连接安全 PRAGMA。

调用方一律通过全局 ``database`` 获取引擎与会话工厂；
``get_db()`` 是 FastAPI 依赖入口。旧模块级 ``DB_DIR``/``DB_PATH``/
``engine``/``async_session`` 导出已删除，不保留兼容别名。
"""

from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (AsyncEngine, AsyncSession,
                                    async_sessionmaker, create_async_engine)
from sqlalchemy.orm import DeclarativeBase

from app.runtime_paths import ensure_runtime_directories, resolve_runtime_paths


class Base(DeclarativeBase):
    pass


class DatabaseRuntime:
    """绑定了固定数据库文件路径的引擎/会话工厂运行时。"""

    def __init__(self, engine: AsyncEngine, path: Path):
        self.engine = engine
        self.path = path
        self.session_factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False)

    @classmethod
    def create(cls, path: Path | str) -> "DatabaseRuntime":
        path = Path(path)
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}", echo=False)

        @event.listens_for(engine.sync_engine, "connect")
        def _apply_pragma(dbapi_conn, _connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return cls(engine, path)

    async def dispose(self) -> None:
        await self.engine.dispose()


database = DatabaseRuntime.create(resolve_runtime_paths().database)


async def init_db():
    ensure_runtime_directories(resolve_runtime_paths())
    async with database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # 迁移：为已有 region_groups 表添加新列
        for col, default in [("auto_enabled", 1), ("manual_enabled", 1)]:
            try:
                await conn.execute(text(
                    f"ALTER TABLE region_groups ADD COLUMN {col} BOOLEAN NOT NULL DEFAULT {default}"
                ))
            except Exception:
                pass  # 列已存在

        # 迁移：为已有 host_mappings 表添加 skip_proxy 列
        try:
            await conn.execute(text(
                "ALTER TABLE host_mappings ADD COLUMN skip_proxy BOOLEAN NOT NULL DEFAULT 1"
            ))
        except Exception:
            pass  # 列已存在


async def get_db():
    async with database.session_factory() as session:
        yield session
