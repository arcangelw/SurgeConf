"""共享测试 fixtures — 内存 SQLite + 最小种子数据"""

import asyncio
import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.database import Base
from app.models import (ProxyNode, RegionGroup, ServiceGroup, ConfigProfile,
                        RuleSource, CustomRule, HostMapping, SystemConfig)
from app.default_config import (DEFAULT_REGION_GROUPS, DEFAULT_SERVICE_GROUPS,
                                DEFAULT_RULE_SOURCES, DEFAULT_GENERAL,
                                DEFAULT_PRE_RULES, DEFAULT_GENERAL_RULES,
                                DEFAULT_HOST_MAPPINGS)
from app.services.generator import DEFAULT_EXCLUSION_KEYWORDS


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def db():
    """每个测试独立的内存 DB，表结构 + 种子数据"""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        for rg in DEFAULT_REGION_GROUPS:
            session.add(RegionGroup(**rg))
        for sg in DEFAULT_SERVICE_GROUPS:
            session.add(ServiceGroup(**sg))
        for rs in DEFAULT_RULE_SOURCES[:3]:
            session.add(RuleSource(**rs))
        session.add(ConfigProfile(
            name="默认配置", is_default=True, locale="zh",
            final_action="兜底", general=dict(DEFAULT_GENERAL),
            target="mac_workstation", udp_security_mode="scoped",
        ))
        for r in DEFAULT_PRE_RULES[:3]:
            session.add(CustomRule(**r))
        for r in DEFAULT_GENERAL_RULES[:3]:
            session.add(CustomRule(**r))
        for h in DEFAULT_HOST_MAPPINGS[:3]:
            session.add(HostMapping(**h))
        # 系统级配置
        session.add(SystemConfig(key="exclusion_keywords", value=DEFAULT_EXCLUSION_KEYWORDS))
        session.add(SystemConfig(key="other_auto_enabled", value="true"))
        session.add(SystemConfig(key="other_manual_enabled", value="true"))
        await session.commit()
        yield session

    await engine.dispose()


@pytest_asyncio.fixture
async def db_fk_on():
    """每连接 PRAGMA foreign_keys=ON 的空内存库（与运行时 DatabaseRuntime 同口径）：
    专用于验证数据库级 ON DELETE CASCADE 的真实行为，不依赖路由手工删除"""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_conn, _connection_record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def db_with_nodes(db):
    """在 db fixture 基础上添加模拟节点"""
    nodes = [
        ProxyNode(name="HK-01", node_type="trojan", server="1.1.1.1", port=443,
                  config="HK-01 = trojan, 1.1.1.1, 443, password=x", region_auto="香港",
                  region_manual="香港", enabled=True, subscription_id=10),
        ProxyNode(name="HK-02", node_type="trojan", server="1.1.1.2", port=443,
                  config="HK-02 = trojan, 1.1.1.2, 443, password=x", region_auto="香港",
                  enabled=True),
        ProxyNode(name="US-01", node_type="ss", server="2.2.2.1", port=443,
                  config="US-01 = ss, 2.2.2.1, 443, encrypt=x, password=x", region_auto="美国",
                  region_manual="美国", enabled=True),
        ProxyNode(name="JP-01", node_type="vmess", server="3.3.3.1", port=443,
                  config="JP-01 = vmess, 3.3.3.1, 443, username=x", region_auto="日本",
                  enabled=True),
    ]
    nodes[2].pinned_services = ["AI服务"]
    for n in nodes:
        db.add(n)
    await db.commit()
    yield db
