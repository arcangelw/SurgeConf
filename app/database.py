"""显式 SQLite 运行时：单引擎 + 会话工厂 + 每连接安全 PRAGMA。

调用方一律通过全局 ``database`` 获取引擎与会话；``get_db()`` 是
FastAPI 依赖入口。旧模块级 ``DB_DIR``/``DB_PATH``/``engine``/
``async_session`` 导出已删除，不保留兼容别名。

全局 ``database`` 初始处于未绑定状态：``lifespan`` 必须先完成
``prepare_database``（staging 原子准备），再调用 ``bind()`` 挂载
live 数据库，之后才允许创建任何连接。

会话与维护互斥：生产代码一律经 ``database.session()`` 受控入口取
会话（FastAPI 依赖、security、调度器、启动维护）；恢复/重置在
``database.maintenance()`` 屏障内串行执行——先阻止新会话、再等待
在途会话归零，维护期间新请求阻塞等待而非读到未绑定 runtime，
也不在发布窗口写旧 inode。staging 等私有 runtime 无并发，直接用
``session_factory``。
"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (AsyncEngine, AsyncSession,
                                    async_sessionmaker, create_async_engine)
from sqlalchemy.orm import DeclarativeBase


def apply_connection_pragmas(dbapi_conn) -> None:
    """每连接安全 PRAGMA：先请求 WAL 并验证实际生效，再执行其余。

    ``PRAGMA journal_mode=WAL`` 在网络文件系统/受限卷上会被 SQLite
    静默降级（返回 delete/memory 等）——必须读取返回值并归一（去
    空白、转小写）等于 ``wal`` 才放行；否则抛固定 RuntimeError
    （消息不含路径与参数），后续 PRAGMA（synchronous/busy_timeout/
    foreign_keys）绝不带着错误日志模式执行。cursor 在任何异常路径
    都被关闭（try/finally）。
    """
    cursor = dbapi_conn.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        row = cursor.fetchone()
        reported = row[0] if row else None
        mode = str(reported).strip().lower() if reported is not None else ""
        if mode != "wal":
            raise RuntimeError(
                "sqlite journal_mode=WAL was not honored "
                f"(reported: {reported!r}); refusing this connection")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


class Base(DeclarativeBase):
    pass


class DatabaseRuntime:
    """绑定到固定数据库文件路径的引擎/会话工厂运行时。

    未绑定时 ``engine``/``path``/``session_factory`` 为 None；
    ``bind()`` 可重复调用（先处置旧引擎），供启动与数据重置复用。

    ``bind`` 是 async 验证绑定（verified bind）：局部创建 engine 并
    注册 PRAGMA hook 后**实际建立一次连接**——hook（WAL 生效验证）
    全部成功才把 engine/path/session_factory 发布到 runtime；失败
    dispose 局部 engine 并抛错，runtime 保持未绑定或维持旧绑定，
    绝不暴露未验证的 engine/会话工厂，也不在未验证前宣告成功。

    维护屏障（同一事件循环内）：
    - ``session()``：受控会话入口。非维护期立即取得会话并登记在途
      计数；维护期阻塞到维护结束——绝不读到 ``session_factory=None``
      抛错，也绝不在旧文件被替换后打开其旧 inode。
    - ``maintenance()``：维护屏障，排他串行（restore/reset 互斥）。
      进入维护区前先阻止新会话、再等待在途会话全部结束；退出
      （含异常/取消）可靠放行。维护体内只允许 bind/dispose 与
      staging 私有 runtime，不得再开受控会话（会死锁）。
    """

    def __init__(self) -> None:
        self.engine: AsyncEngine | None = None
        self.path: Path | None = None
        self.session_factory: async_sessionmaker | None = None
        self._state_lock = asyncio.Lock()
        self._maintenance_lock = asyncio.Lock()
        self._maintenance = False
        self._active_sessions = 0
        self._drained = asyncio.Event()
        self._sessions_allowed = asyncio.Event()
        self._drained.set()
        self._sessions_allowed.set()

    async def bind(self, path: Path | str) -> None:
        """验证绑定（verified bind）：WAL hook 实际成功前不发布任何引用。

        局部创建 engine（唯一创建点，hide_parameters：异常 str/repr
        不携带绑定参数）并注册每连接 PRAGMA hook，随后从池实际
        checkout 一次连接——建立新 DBAPI 连接时 hook 同步执行，
        WAL 验证失败（RuntimeError）在替换任何 runtime 引用之前
        抛出；失败路径 dispose 局部 engine（清理 aiosqlite 线程），
        runtime 保持未绑定或维持旧绑定。成功才原子发布三元组。
        """
        path = Path(path)
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}",
                                     echo=False, hide_parameters=True)

        @event.listens_for(engine.sync_engine, "connect")
        def _apply_pragma(dbapi_conn, _connection_record):
            apply_connection_pragmas(dbapi_conn)

        try:
            # 实际连接：checkout 触发 hook（真实 SQLAlchemy/aiosqlite
            # 路径，非 raw sqlite 旁路）；连接立即归还池
            async with engine.connect():
                pass
        except BaseException:
            await engine.dispose()
            raise
        if self.engine is not None:
            self.engine.sync_engine.dispose()
        self.engine = engine
        self.path = path
        self.session_factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False)

    @classmethod
    async def create(cls, path: Path | str) -> "DatabaseRuntime":
        """验证绑定的工厂入口（语义与 ``bind`` 一致）"""
        runtime = cls()
        await runtime.bind(path)
        return runtime

    async def dispose(self) -> None:
        """处置引擎（仅在维护屏障或进程关闭边界内调用）。

        只关闭池内连接；被 checkout 的遗留连接（绕过受控入口的旧
        代码路径）不阻塞本调用——维护屏障已保证受控在途会话归零。
        """
        if self.engine is not None:
            await self.engine.dispose()
            self.engine = None
            self.session_factory = None

    @asynccontextmanager
    async def session(self):
        """受控会话入口：登记在途计数；维护期阻塞到维护结束。"""
        factory = await self._acquire_session_slot()
        try:
            async with factory() as session:
                yield session
        finally:
            await self._release_session_slot()

    async def _acquire_session_slot(self) -> async_sessionmaker:
        while True:
            async with self._state_lock:
                if not self._maintenance:
                    factory = self.session_factory
                    if factory is None:
                        raise RuntimeError(
                            "database runtime is not bound")
                    self._active_sessions += 1
                    if self._active_sessions == 1:
                        self._drained.clear()
                    return factory
            await self._sessions_allowed.wait()

    async def _release_session_slot(self) -> None:
        async with self._state_lock:
            self._active_sessions -= 1
            if self._active_sessions == 0:
                self._drained.set()

    @asynccontextmanager
    async def maintenance(self):
        """维护屏障：排他串行；阻止新会话 → 等待在途归零 → 维护区。

        维护体内（yield 与 finally 之间）由调用方保证失败恢复与
        重绑完成；本屏障在退出（含异常/取消）时无条件放行新会话。
        """
        async with self._maintenance_lock:
            async with self._state_lock:
                self._maintenance = True
                self._sessions_allowed.clear()
            try:
                await self._drained.wait()
                yield
            finally:
                async with self._state_lock:
                    self._maintenance = False
                    self._sessions_allowed.set()


database = DatabaseRuntime()


async def get_db():
    async with database.session() as session:
        yield session
