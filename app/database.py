from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
import os

DB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "surge_conf.db")

DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH}"

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # 迁移：为已有 region_groups 表添加新列
        from sqlalchemy import text
        for col, default in [("auto_enabled", 1), ("manual_enabled", 1)]:
            try:
                await conn.execute(text(
                    f"ALTER TABLE region_groups ADD COLUMN {col} BOOLEAN NOT NULL DEFAULT {default}"
                ))
            except Exception:
                pass  # 列已存在


async def get_db():
    async with async_session() as session:
        yield session
