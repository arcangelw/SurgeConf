"""显式 SQLite 运行时：单引擎 + 会话工厂 + 每连接安全 PRAGMA。

调用方一律通过全局 ``database`` 获取引擎与会话工厂；
``get_db()`` 是 FastAPI 依赖入口。旧模块级 ``DB_DIR``/``DB_PATH``/
``engine``/``async_session`` 导出已删除，不保留兼容别名。

全局 ``database`` 初始处于未绑定状态：``lifespan`` 必须先完成
``prepare_database``（staging 原子准备），再调用 ``bind()`` 挂载
live 数据库，之后才允许创建任何连接。
"""

from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (AsyncEngine, AsyncSession,
                                    async_sessionmaker, create_async_engine)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class DatabaseRuntime:
    """绑定到固定数据库文件路径的引擎/会话工厂运行时。

    未绑定时 ``engine``/``path``/``session_factory`` 为 None；
    ``bind()`` 可重复调用（先处置旧引擎），供启动与数据重置复用。
    """

    def __init__(self) -> None:
        self.engine: AsyncEngine | None = None
        self.path: Path | None = None
        self.session_factory: async_sessionmaker | None = None

    def bind(self, path: Path | str) -> None:
        """绑定（或重绑）到指定数据库文件，替换现有引擎"""
        if self.engine is not None:
            self.engine.sync_engine.dispose()
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

        self.engine = engine
        self.path = path
        self.session_factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False)

    @classmethod
    def create(cls, path: Path | str) -> "DatabaseRuntime":
        runtime = cls()
        runtime.bind(path)
        return runtime

    async def dispose(self) -> None:
        if self.engine is not None:
            await self.engine.dispose()
            self.engine = None
            self.session_factory = None


database = DatabaseRuntime()


async def get_db():
    async with database.session_factory() as session:
        yield session
