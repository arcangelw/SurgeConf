"""应用启动引导：数据库初始化、默认数据填充、幂等迁移与 lifespan。"""

import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database import engine, init_db
from app.models import RegionGroup, ServiceGroup, RuleSource, ConfigProfile, CustomRule, HostMapping, SystemConfig
from app.default_config import (
    DEFAULT_REGION_GROUPS, DEFAULT_SERVICE_GROUPS,
    DEFAULT_RULE_SOURCES, DEFAULT_GENERAL,
    DEFAULT_URL_REWRITES, DEFAULT_HEADER_REWRITES,
    DEFAULT_MITM, DEFAULT_PRE_RULES, DEFAULT_GENERAL_RULES,
    DEFAULT_HOST_MAPPINGS,
)


async def seed_defaults():
    """首次启动时填充默认数据"""
    from sqlalchemy import select

    async_session = async_sessionmaker(engine, expire_on_commit=False)
    async with async_session() as db:
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
    # 注：no_resolve_keywords 键由下方 apply_data_migrations 幂等补齐，此处不重复
    sys_config_keys = {"exclusion_keywords", "other_auto_enabled", "other_manual_enabled", "skip_proxy_custom"}
    existing_configs = set()
    async with async_session() as db:
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

    # 数据迁移：内置安全规则补齐 + 存量 ipv6-vif 修正（多设备部署，修复随代码分发）
    async with async_session() as db:
        await apply_data_migrations(db)

    # 数据维护：清理孤儿节点（修复旧版本删除订阅时未级联删除节点导致的残留）
    from app.services.subscription import cleanup_orphan_nodes
    async with async_session() as db:
        removed = await cleanup_orphan_nodes(db)
        if removed:
            print(f"[数据维护] 已清理 {removed} 个孤儿节点（归属订阅已删除的残留节点）", file=sys.stderr)


async def apply_data_migrations(db) -> None:
    """幂等数据迁移：内置安全规则补齐 + 存量 ipv6-vif 归一 + DNS 默认值升级。可安全重复执行。"""
    from sqlalchemy import select
    from app.default_config import SECURITY_PRE_RULES
    from app.services.generator import DEFAULT_NO_RESOLVE_KEYWORDS
    from app.models import CustomRule, ConfigProfile, SystemConfig

    # 系统键补齐（no_resolve_keywords：IP 类规则集 no-resolve 关键词，generator 有代码兜底，此处补齐供面板管理）
    existing_keys = {row[0] for row in (await db.execute(select(SystemConfig.key))).all()}
    if "no_resolve_keywords" not in existing_keys:
        db.add(SystemConfig(key="no_resolve_keywords", value=DEFAULT_NO_RESOLVE_KEYWORDS))

    # 内置安全规则：按 rule_text 幂等插入，不复活用户删过的其他默认规则
    existing_rules = {row[0] for row in (await db.execute(select(CustomRule.rule_text))).all()}
    added = 0
    for r in SECURITY_PRE_RULES:
        if r["rule_text"] not in existing_rules:
            db.add(CustomRule(profile_id=None, **r))
            added += 1

    # 存量档案 ipv6-vif 归一为官方规范值 disabled（auto/native 按既定策略强制关闭；
    # off 是官方兼容旧值、disable 是历史拼写错误，统一归一不改变"关闭"语义）
    # 必须先复制再改后整体赋值：原地修改 + reassign 相等值会被 SQLAlchemy 判为无变化而跳过 UPDATE
    fixed_v6 = 0
    for profile in (await db.execute(select(ConfigProfile))).scalars().all():
        g = dict(profile.general) if isinstance(profile.general, dict) else {}
        if g.get("ipv6-vif") in ("auto", "native", "off", "disable"):
            g["ipv6-vif"] = "disabled"
            profile.general = g
            fixed_v6 += 1

    # 存量档案 DNS 旧默认值升级（基于 Surge 官方文档 + 本机实测的性能/隐私优化）：
    # 1) encrypted-dns-server 加入阿里 DoH3 主力（alt-svc 官宣 h3，QUIC 1-RTT 抗丢包），
    #    保留同域 TCP DoH 与腾讯 DoH 做跨协议/跨服务商并发兜底
    # 2) hijack-dns 从仅劫持 Google DNS 升级为 *:53 全劫持，防应用硬编码任意明文 DNS 绕过 fake-ip
    # 仅精确匹配旧默认值时升级，用户自定义值一律不动
    _DNS_UPGRADES = {
        "encrypted-dns-server": (
            "https://doh.pub/dns-query, https://dns.alidns.com/dns-query",
            "h3://dns.alidns.com/dns-query, https://dns.alidns.com/dns-query, https://doh.pub/dns-query",
        ),
        "hijack-dns": ("8.8.8.8:53, 8.8.4.4:53", "*:53"),
    }
    fixed_dns = 0
    for profile in (await db.execute(select(ConfigProfile))).scalars().all():
        g = dict(profile.general) if isinstance(profile.general, dict) else {}
        changed = False
        for key, (old, new) in _DNS_UPGRADES.items():
            if g.get(key) == old:
                g[key] = new
                changed = True
        if changed:
            profile.general = g
            fixed_dns += 1

    # 无条件 commit：上面的 select 会触发 autoflush（flush ≠ commit），若按 db.new/db.dirty 判断
    # 会因已 flush 而误判"无变更"跳过 commit，session 关闭时事务回滚、变更丢失
    key_added = "no_resolve_keywords" not in existing_keys
    await db.commit()
    if added or fixed_v6 or fixed_dns or key_added:
        print(f"[数据迁移] 补齐内置安全规则 {added} 条，修正存量 ipv6-vif {fixed_v6} 个档案，"
              f"升级 DNS 配置 {fixed_dns} 个档案，补系统键 {int(key_added)} 项", file=sys.stderr)


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
║  继续使用即表示您接受免责声明的全部条款。                  ║
╚══════════════════════════════════════════════════════════╝
"""
    print(banner, file=sys.stderr)
    await init_db()
    await seed_defaults()
    yield
