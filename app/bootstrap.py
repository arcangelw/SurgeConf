"""应用启动引导：种子数据、启动维护与 lifespan。

schema 与业务数据迁移交给 ``app/services/database_migration.py`` 的
``prepare_database``（staging 原子准备），本模块不再执行任何建表或
schema 变更。
"""

import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from app.database import database
from app.runtime_paths import (configure_private_umask,
                               ensure_runtime_directories,
                               resolve_runtime_paths)
from app.models import RegionGroup, ServiceGroup, RuleSource, ConfigProfile, CustomRule, HostMapping, SystemConfig
from app.default_config import (
    DEFAULT_REGION_GROUPS, DEFAULT_SERVICE_GROUPS,
    DEFAULT_RULE_SOURCES, DEFAULT_GENERAL,
    DEFAULT_URL_REWRITES, DEFAULT_HEADER_REWRITES,
    DEFAULT_MITM, DEFAULT_PRE_RULES, DEFAULT_GENERAL_RULES,
    DEFAULT_HOST_MAPPINGS,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


async def seed_defaults(session_factory) -> None:
    """首次启动时填充默认数据（在 staging 上执行，幂等）"""
    from sqlalchemy import select

    async with session_factory() as db:
        result = await db.execute(select(RegionGroup).limit(1))
        if result.scalar_one_or_none():
            # 已有数据，仅确保系统配置存在
            pass
        else:
            for rg in DEFAULT_REGION_GROUPS:
                db.add(RegionGroup(**rg))

            for sg in DEFAULT_SERVICE_GROUPS:
                db.add(ServiceGroup(**sg))

            for rs in DEFAULT_RULE_SOURCES:
                db.add(RuleSource(**rs))

            db.add(ConfigProfile(
                name="默认配置",
                description="基于原始 Surge.conf 的默认配置",
                is_default=True,
                locale="zh",
                final_action="兜底",
                general=dict(DEFAULT_GENERAL),
                url_rewrites=list(DEFAULT_URL_REWRITES),
                header_rewrites=list(DEFAULT_HEADER_REWRITES),
                mitm=dict(DEFAULT_MITM),
            ))

            for r in DEFAULT_PRE_RULES:
                db.add(CustomRule(profile_id=None, **r))
            for r in DEFAULT_GENERAL_RULES:
                db.add(CustomRule(profile_id=None, **r))
            for h in DEFAULT_HOST_MAPPINGS:
                db.add(HostMapping(profile_id=None, **h))

            await db.commit()

    # 确保系统级配置存在（首次启动或数据库已有旧数据时）
    # 注：no_resolve_keywords 键由业务迁移 no_resolve_keywords_v1 幂等补齐，此处不重复
    sys_config_keys = {"exclusion_keywords", "other_auto_enabled", "other_manual_enabled", "skip_proxy_custom"}
    existing_configs = set()
    async with session_factory() as db:
        result = await db.execute(select(SystemConfig))
        for row in result.scalars().all():
            existing_configs.add(row.key)
        missing = sys_config_keys - existing_configs
        if "exclusion_keywords" in missing:
            db.add(SystemConfig(key="exclusion_keywords",
                                value="Remain|Expired|Expire|应急|官网|如需|套餐|去除|剩余|距离|Reset|重置|流量|Traffic|Used|已用|Today|今日|GB|Download|Upload|Speed"))
        if "other_auto_enabled" in missing:
            db.add(SystemConfig(key="other_auto_enabled", value="true"))
        if "other_manual_enabled" in missing:
            db.add(SystemConfig(key="other_manual_enabled", value="true"))
        if "skip_proxy_custom" in missing:
            db.add(SystemConfig(key="skip_proxy_custom", value="[]"))
        if missing:
            await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    banner = """
╔══════════════════════════════════════════════════════════╗
║                     SurgeConf                           ║
║                                                         ║
║  免责声明:                                               ║
║  本工具仅用于学习和研究 Surge 配置管理技术。               ║
║  使用前请阅读完整的免责声明:                              ║
║  github.com/arcangelw/SurgeConf#免责声明                ║
║                                                         ║
║  继续使用即表示您接受免责声明的一切条款。                  ║
╚═════════════════════════════════════════════════════════╝
"""
    print(banner, file=sys.stderr)
    configure_private_umask()
    paths = resolve_runtime_paths()
    ensure_runtime_directories(paths)
    # 延迟导入：database_migration 顶层依赖本模块的 seed_defaults
    from app.services.database_migration import prepare_database
    result = await prepare_database(paths, PROJECT_ROOT / "data" / "surge_conf.db")
    database.bind(paths.database)
    print(f"[启动] 数据库就绪（来源 {result.source}，"
          f"已应用业务迁移 {len(result.applied_migrations)} 项）", file=sys.stderr)

    # 数据维护：清理孤儿节点（修复旧版本删除订阅时未级联删除节点导致的残留）
    from app.services.subscription import cleanup_orphan_nodes
    async with database.session_factory() as db:
        removed = await cleanup_orphan_nodes(db)
    if removed:
        print(f"[数据维护] 已清理 {removed} 个孤儿节点（归属订阅已删除的残留节点）", file=sys.stderr)
    yield
