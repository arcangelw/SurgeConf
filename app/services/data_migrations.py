"""有序业务数据迁移注册表。

每个迁移项与写入 ``DataMigrationRecord`` 使用同一事务：成功才记账，
失败整体回滚且不写记录、异常向上传播。已记录的 key 直接跳过，
因此用户之后删除默认规则不会被每次启动重新补回。
"""

import sys

from sqlalchemy import delete, select, update

from app.default_config import DEFAULT_RULE_SOURCES, SECURITY_PRE_RULES
from app.models import (ConfigProfile, CustomRule, DataMigrationRecord,
                        RuleSource, SystemConfig)
from app.services.config_validation import BUILTIN_ACTIONS
from app.services.generator import DEFAULT_NO_RESOLVE_KEYWORDS


async def has_data_migration(db, key: str) -> bool:
    """该业务迁移 key 是否已记账（只读，不提交）"""
    row = (await db.execute(
        select(DataMigrationRecord.key).where(DataMigrationRecord.key == key)
    )).scalar_one_or_none()
    return row is not None


async def record_data_migration(db, key: str) -> None:
    """记账（随外层事务提交）"""
    db.add(DataMigrationRecord(key=key))


async def _migrate_security_rules(db) -> int:
    """内置安全规则：按 rule_text 幂等插入，不复活用户删过的其他默认规则"""
    existing_rules = {row[0] for row in (await db.execute(select(CustomRule.rule_text))).all()}
    added = 0
    for r in SECURITY_PRE_RULES:
        if r["rule_text"] not in existing_rules:
            db.add(CustomRule(profile_id=None, **r))
            added += 1
    return added


async def _migrate_douyin_rule(db) -> int:
    """抖音与 TikTok 共用 snssdk.com 等字节系域名。TikTok-Unlock 规则源也包含该后缀，
    若它先匹配会把抖音评论/视频误送海外策略。补一条更靠前的抖音专用直连规则源；
    插入时整体后移后续规则，保持用户现有相对顺序。按名称或规范 URL 判重。"""
    douyin_default = next(rs for rs in DEFAULT_RULE_SOURCES if rs["name"] == "DouYin")
    existing_sources = (await db.execute(select(RuleSource))).scalars().all()
    douyin_exists = any(
        rs.name == douyin_default["name"] or rs.url == douyin_default["url"]
        for rs in existing_sources
    )
    if douyin_exists:
        return 0
    await db.execute(
        update(RuleSource)
        .where(RuleSource.sort_order >= douyin_default["sort_order"])
        .values(sort_order=RuleSource.sort_order + 1)
    )
    db.add(RuleSource(**douyin_default))
    return 1


async def _migrate_ipv6_vif(db) -> int:
    """存量档案 ipv6-vif 归一为官方规范值 disabled（auto/native 按既定策略强制关闭；
    off 是官方兼容旧值、disable 是历史拼写错误，统一归一不改变"关闭"语义）
    必须先复制再改后整体赋值：原地修改 + reassign 相等值会被 SQLAlchemy 判为无变化而跳过 UPDATE"""
    fixed = 0
    for profile in (await db.execute(select(ConfigProfile))).scalars().all():
        g = dict(profile.general) if isinstance(profile.general, dict) else {}
        if g.get("ipv6-vif") in ("auto", "native", "off", "disable"):
            g["ipv6-vif"] = "disabled"
            profile.general = g
            fixed += 1
    return fixed


async def _migrate_dns_defaults(db) -> int:
    """存量档案 General 旧默认值升级（基于 Surge 官方文档 + 本机实测的性能/隐私优化）：

    1) encrypted-dns-server 加入阿里 DoH3 主力（alt-svc 官宣 h3，QUIC 1-RTT 抗丢包），
       保留同域 TCP DoH 与腾讯 DoH 做跨协议/跨服务商并发兜底
    2) hijack-dns 从仅劫持 Google DNS 升级为 *:53 全劫持，防应用硬编码任意明文 DNS 绕过 fake-ip
    3) 删除死配置 geoip-maxmind-url：产物无 GEOIP 规则（国内判定走 domestic.ip/ChinaMax/ASN china 三集）
    4) 关闭 Surge 内置 Web 面板（常驻内存；SurgeConf 面板不依赖它，官方默认亦为 false）
    5) Surge 官方默认的按策略处理 QUIC：DIRECT 不封、代理按 UDP 能力自适应

    仅精确匹配旧默认值时升级，用户自定义值一律不动。"""
    _DNS_UPGRADES = {
        "encrypted-dns-server": (
            "https://doh.pub/dns-query, https://dns.alidns.com/dns-query",
            "h3://dns.alidns.com/dns-query, https://dns.alidns.com/dns-query, https://doh.pub/dns-query",
        ),
        "hijack-dns": ("8.8.8.8:53, 8.8.4.4:53", "*:53"),
    }
    fixed = 0
    for profile in (await db.execute(select(ConfigProfile))).scalars().all():
        g = dict(profile.general) if isinstance(profile.general, dict) else {}
        changed = False
        for key, (old, new) in _DNS_UPGRADES.items():
            if g.get(key) == old:
                g[key] = new
                changed = True
        if "geoip-maxmind-url" in g:
            del g["geoip-maxmind-url"]
            changed = True
        if g.get("http-api-web-dashboard") is True:
            g["http-api-web-dashboard"] = False
            changed = True
        # 仅在用户从未配置时补齐，明确设置 all/all-proxy/always-allow 的用户值不覆盖
        if "block-quic" not in g:
            g["block-quic"] = "per-policy"
            changed = True
        if not changed:
            continue
        profile.general = g
        fixed += 1
    return fixed


async def _migrate_remove_global_quic(db) -> int:
    """旧版曾全局阻断 UDP/443 以强制 QUIC 回退，但这也会误伤 DIRECT 的 HTTP/3，
    导致国内外图片、视频先失败再回退 TCP。删除该精确内置规则，改由 General 的
    block-quic=per-policy 让 DIRECT 保留 QUIC、代理按各自 UDP 能力自适应处理。"""
    obsolete_quic_rule = "AND,((PROTOCOL,UDP), (DEST-PORT,443)),REJECT"
    removed = (await db.execute(
        delete(CustomRule).where(
            CustomRule.profile_id.is_(None),
            CustomRule.position == "pre",
            CustomRule.rule_text == obsolete_quic_rule,
        )
    )).rowcount or 0
    return removed


async def _migrate_no_resolve_keywords(db) -> int:
    """系统键补齐（IP 类规则集 no-resolve 关键词，generator 有代码兜底，此处补齐供面板管理）"""
    existing_keys = {row[0] for row in (await db.execute(select(SystemConfig.key))).all()}
    if "no_resolve_keywords" in existing_keys:
        return 0
    db.add(SystemConfig(key="no_resolve_keywords", value=DEFAULT_NO_RESOLVE_KEYWORDS))
    return 1


async def _migrate_dangling_rule_source_actions(db) -> int:
    """历史默认 Stream EU/KR 四条规则源的悬空 action 修复 → Global。

    旧版 DEFAULT_RULE_SOURCES 曾把英/韩流媒体指向默认关闭的地区组（英国/
    韩国 auto/manual 均关，组不生成），旧产物实际引用了 Surge 无法解析的
    策略名；生成器引用校验上线后这类库将无法产出配置。

    严格限定修复范围，绝不波及用户数据：
    - 仅限四条历史内置记录，且须同时匹配稳定内置签名（name + source URL +
      rule_type）足以与用户自定义同名记录区分；
    - action 仍是旧值（英国/韩国）——用户改过 action 即视为有意，不动；
    - 用户禁用的规则源不动（enabled=False）；
    - 对应地区组当前确实不会生成（组不存在/被禁用/auto+manual 全关）才修；
      用户已显式开启英韩组则保留原选择。
    其余任何规则源（有效节点 action、自定义 action 等）一律不改写；
    CustomRule 的坏引用属用户手写内容，由生成时校验显式报错指引修复。
    """
    from app.models import RegionGroup, ServiceGroup

    # 历史内置悬空签名：(name, source URL, rule_type, legacy_action, group)
    legacy_stream = (
        ("Stream EU (non_ip)", "https://ruleset.skk.moe/List/non_ip/stream_eu.conf", "RULE-SET", "英国"),
        ("Stream EU (ip)", "https://ruleset.skk.moe/List/ip/stream_eu.conf", "RULE-SET", "英国"),
        ("Stream KR (non_ip)", "https://ruleset.skk.moe/List/non_ip/stream_kr.conf", "RULE-SET", "韩国"),
        ("Stream KR (ip)", "https://ruleset.skk.moe/List/ip/stream_kr.conf", "RULE-SET", "韩国"),
    )
    by_name = {item[0]: item for item in legacy_stream}

    regions = {rg.name: rg for rg in (await db.execute(select(RegionGroup))).scalars().all()}
    services = (await db.execute(select(ServiceGroup))).scalars().all()
    # 回退目标优先恒生成的 Global 服务组；用户删光服务组时退到内置 DIRECT
    fallback = "Global" if any(sg.name == "Global" and sg.enabled for sg in services) else "DIRECT"

    fixed = 0
    candidates = (await db.execute(
        select(RuleSource).where(RuleSource.name.in_(by_name))
    )).scalars().all()
    for rs in candidates:
        _, url, rule_type, legacy_action = by_name[rs.name]
        if not rs.enabled:
            continue  # 用户禁用的记录不动
        if rs.url != url or rs.rule_type != rule_type or rs.action != legacy_action:
            continue  # 签名不完全匹配（用户自定义同名记录 / 已改过 action）
        rg = regions.get(legacy_action)
        group_generated = rg is not None and rg.enabled and (rg.auto_enabled or rg.manual_enabled)
        if group_generated:
            continue  # 组会生成：引用有效，保留用户选择
        rs.action = fallback
        fixed += 1
    return fixed


# 有序注册表：新增业务迁移一律追加新 key，禁止修改已发布 key 的语义
DATA_MIGRATIONS: dict = {
    "security_rules_v1": _migrate_security_rules,
    "douyin_rule_v1": _migrate_douyin_rule,
    "ipv6_vif_v1": _migrate_ipv6_vif,
    "dns_defaults_v1": _migrate_dns_defaults,
    "remove_global_quic_v1": _migrate_remove_global_quic,
    "no_resolve_keywords_v1": _migrate_no_resolve_keywords,
    "dangling_rule_source_actions_v1": _migrate_dangling_rule_source_actions,
}


async def apply_data_migrations(session_factory) -> list:
    """按注册表顺序应用未记账的业务迁移，返回本次应用的 key 列表。

    每项独立事务：迁移写入 + 记账 + commit 同步完成；单项失败即回滚
    （不写记录）并向调用方抛出异常。
    """
    applied = []
    async with session_factory() as db:
        for key, migrate in DATA_MIGRATIONS.items():
            if await has_data_migration(db, key):
                continue
            try:
                changed = await migrate(db)
                await record_data_migration(db, key)
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            applied.append(key)
            if changed:
                print(f"[数据迁移] {key}: {changed} 项变更", file=sys.stderr)
    return applied
