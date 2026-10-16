"""数据迁移 apply_data_migrations 行为测试（内置安全规则 / ipv6-vif 归一 / DNS 默认值升级 / 系统键 / 悬空规则源引用修复）

持久化验证要点：断言一律通过独立新 session（db.bind 新开）查询，
防止 autoflush 后未 commit 的事务内可见性造成假阳性。
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.data_migrations import apply_data_migrations
from app.models import CustomRule, ConfigProfile, ProxyNode, SystemConfig, RuleSource
from app.default_config import SECURITY_PRE_RULES


async def _fresh_view(db) -> AsyncSession:
    """基于同一 engine 开新 session，验证数据已持久化（非事务内可见）"""
    new_session = AsyncSession(bind=db.bind, expire_on_commit=False)
    return new_session



def _factory(db):
    """apply_data_migrations 以 session_factory 为参数：基于同一 engine 构造"""
    return async_sessionmaker(db.bind, class_=AsyncSession, expire_on_commit=False)

@pytest.mark.asyncio
async def test_security_rules_backfilled_on_legacy_db(db):
    """老库（无内置安全规则）迁移后补齐 STUN 阻断三条，且已持久化"""
    before = (await db.execute(select(CustomRule.rule_text))).scalars().all()
    assert not any("stun" in r.lower() for r in before), "前置条件：老库无 STUN 规则"

    await apply_data_migrations(_factory(db))

    async with await _fresh_view(db) as fresh:
        texts = set((await fresh.execute(select(CustomRule.rule_text))).scalars().all())
    for r in SECURITY_PRE_RULES:
        assert r["rule_text"] in texts


@pytest.mark.asyncio
async def test_migration_idempotent(db):
    """重复执行不产生重复规则（跨 session 验证）"""
    await apply_data_migrations(_factory(db))
    await apply_data_migrations(_factory(db))

    async with await _fresh_view(db) as fresh:
        texts = list((await fresh.execute(select(CustomRule.rule_text))).scalars().all())
    for r in SECURITY_PRE_RULES:
        assert texts.count(r["rule_text"]) == 1, f"{r['rule_text']} 重复插入"


@pytest.mark.asyncio
async def test_douyin_direct_source_backfilled_before_tiktok_and_idempotent(db):
    """抖音规则源优先直连，避免共享 snssdk.com 被 TikTok 海外策略抢先匹配。"""
    legacy_tiktok = RuleSource(
        name="TikTok",
        url="https://raw.githubusercontent.com/Semporia/TikTok-Unlock/master/Surge/TikTok.list",
        action="TikTok",
        rule_type="RULE-SET",
        category="streaming",
        sort_order=26,
        enabled=True,
    )
    db.add(legacy_tiktok)
    await db.commit()

    await apply_data_migrations(_factory(db))
    await apply_data_migrations(_factory(db))

    async with await _fresh_view(db) as fresh:
        sources = (await fresh.execute(select(RuleSource))).scalars().all()
    douyin = [rs for rs in sources if rs.name == "DouYin"]
    tiktok = next(rs for rs in sources if rs.name == "TikTok")
    assert len(douyin) == 1
    assert douyin[0].action == "大陆直连"
    assert douyin[0].enabled is True
    assert douyin[0].sort_order < tiktok.sort_order


@pytest.mark.asyncio
async def test_user_deleted_default_rules_not_revived(db):
    """用户删除过的非安全默认规则不被复活（迁移只插 SECURITY_PRE_RULES）"""
    first = (await db.execute(select(CustomRule).limit(1))).scalar_one()
    deleted_text = first.rule_text
    await db.delete(first)
    await db.commit()

    await apply_data_migrations(_factory(db))

    async with await _fresh_view(db) as fresh:
        texts = (await fresh.execute(select(CustomRule.rule_text))).scalars().all()
    assert deleted_text not in texts


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", ["auto", "native", "off", "disable"])
async def test_ipv6_vif_normalized_to_disabled(db, legacy):
    """存量档案 ipv6-vif 的 auto/native/off/disable 全部归一为官方规范值 disabled 并持久化；幂等"""
    p = (await db.execute(select(ConfigProfile))).scalars().first()
    p.general = dict(p.general, **{"ipv6-vif": legacy})
    await db.commit()

    await apply_data_migrations(_factory(db))

    async with await _fresh_view(db) as fresh:
        g = (await fresh.execute(select(ConfigProfile.general))).scalars().first()
    assert g["ipv6-vif"] == "disabled"

    # 已是 disabled 时再迁移无变化（幂等）
    await apply_data_migrations(_factory(db))
    async with await _fresh_view(db) as fresh:
        g2 = (await fresh.execute(select(ConfigProfile.general))).scalars().first()
    assert g2["ipv6-vif"] == "disabled"


@pytest.mark.asyncio
async def test_dns_defaults_upgraded_from_legacy(db):
    """DNS 旧默认值升级：DoH 双端点→DoH3 三端点、hijack-dns→*:53；幂等"""
    p = (await db.execute(select(ConfigProfile))).scalars().first()
    p.general = dict(p.general, **{
        "encrypted-dns-server": "https://doh.pub/dns-query, https://dns.alidns.com/dns-query",
        "hijack-dns": "8.8.8.8:53, 8.8.4.4:53",
    })
    await db.commit()

    await apply_data_migrations(_factory(db))

    async with await _fresh_view(db) as fresh:
        g = (await fresh.execute(select(ConfigProfile.general))).scalars().first()
    assert g["encrypted-dns-server"] == \
        "h3://dns.alidns.com/dns-query, https://dns.alidns.com/dns-query, https://doh.pub/dns-query"
    assert g["hijack-dns"] == "*:53"

    # 幂等：再迁移无变化
    await apply_data_migrations(_factory(db))
    async with await _fresh_view(db) as fresh:
        g2 = (await fresh.execute(select(ConfigProfile.general))).scalars().first()
    assert g2["hijack-dns"] == "*:53"


@pytest.mark.asyncio
async def test_dns_custom_values_not_touched(db):
    """用户自定义 DNS 值不被迁移覆盖"""
    custom_dns = "https://1.1.1.1/dns-query"
    custom_hijack = "1.2.3.4:53"
    p = (await db.execute(select(ConfigProfile))).scalars().first()
    p.general = dict(p.general, **{
        "encrypted-dns-server": custom_dns,
        "hijack-dns": custom_hijack,
    })
    await db.commit()

    await apply_data_migrations(_factory(db))

    async with await _fresh_view(db) as fresh:
        g = (await fresh.execute(select(ConfigProfile.general))).scalars().first()
    assert g["encrypted-dns-server"] == custom_dns
    assert g["hijack-dns"] == custom_hijack


@pytest.mark.asyncio
async def test_global_quic_block_removed_and_per_policy_default_added(db):
    """删除误伤媒体性能的全局 UDP/443 阻断，改为 Surge 官方按策略处理 QUIC。"""
    obsolete = "AND,((PROTOCOL,UDP), (DEST-PORT,443)),REJECT"
    db.add(CustomRule(
        profile_id=None,
        rule_text=obsolete,
        position="pre",
        sort_order=93,
        enabled=True,
        comment="legacy global QUIC block",
    ))
    profile = (await db.execute(select(ConfigProfile))).scalars().first()
    general = dict(profile.general)
    general.pop("block-quic", None)
    profile.general = general
    await db.commit()

    await apply_data_migrations(_factory(db))

    async with await _fresh_view(db) as fresh:
        rules = (await fresh.execute(select(CustomRule.rule_text))).scalars().all()
        migrated_general = (await fresh.execute(select(ConfigProfile.general))).scalars().first()
    assert obsolete not in rules
    assert migrated_general["block-quic"] == "per-policy"


@pytest.mark.asyncio
async def test_custom_block_quic_value_not_touched(db):
    """用户显式选择的 QUIC 策略不被迁移覆盖。"""
    profile = (await db.execute(select(ConfigProfile))).scalars().first()
    profile.general = dict(profile.general, **{"block-quic": "always-allow"})
    await db.commit()

    await apply_data_migrations(_factory(db))

    async with await _fresh_view(db) as fresh:
        general = (await fresh.execute(select(ConfigProfile.general))).scalars().first()
    assert general["block-quic"] == "always-allow"


@pytest.mark.asyncio
async def test_no_resolve_keywords_backfilled_and_persisted(db):
    """缺 no_resolve_keywords 系统键时补默认值并持久化；已有自定义值不覆盖"""
    keys = (await db.execute(select(SystemConfig.key))).scalars().all()
    assert "no_resolve_keywords" not in keys, "前置条件：老库无该键"

    await apply_data_migrations(_factory(db))

    # 跨 session 验证持久化（回归：autoflush 后跳过 commit 导致回滚丢失）
    async with await _fresh_view(db) as fresh:
        row = (await fresh.execute(
            select(SystemConfig).where(SystemConfig.key == "no_resolve_keywords"))).scalar_one()
        assert row.value == "(ip),ASN,/ip/,Lan"

    # 用户自定义值不被覆盖
    async with await _fresh_view(db) as fresh:
        obj = (await fresh.execute(
            select(SystemConfig).where(SystemConfig.key == "no_resolve_keywords"))).scalar_one()
        obj.value = "custom"
        await fresh.commit()

    await apply_data_migrations(_factory(db))
    async with await _fresh_view(db) as fresh:
        v = (await fresh.execute(
            select(SystemConfig.value).where(SystemConfig.key == "no_resolve_keywords"))).scalar_one()
    assert v == "custom"


@pytest.mark.asyncio
async def test_dangling_rule_source_actions_fixed_to_global(db):
    """历史默认 Stream EU/KR 悬空 action 迁移为 Global；范围外记录一律不动；幂等"""
    from app.models import RegionGroup

    # 前置条件：conftest 种子中英国/韩国地区组 auto/manual 均关 → 不生成
    uk = (await db.execute(select(RegionGroup).where(RegionGroup.name == "英国"))).scalar_one()
    assert uk.auto_enabled is False and uk.manual_enabled is False

    db.add(RuleSource(name="Stream EU (non_ip)", url="https://ruleset.skk.moe/List/non_ip/stream_eu.conf",
                      action="英国", rule_type="RULE-SET", category="region_stream",
                      sort_order=32, enabled=True))
    # 范围外：未知引用的普通规则源、被用户禁用的同名内置记录
    db.add(RuleSource(name="幽灵源", url="https://example.com/ghost.list",
                      action="不存在的组", rule_type="RULE-SET", category="global",
                      sort_order=80, enabled=True))
    db.add(RuleSource(name="Stream KR (ip)", url="https://ruleset.skk.moe/List/ip/stream_kr.conf",
                      action="韩国", rule_type="RULE-SET", category="region_stream",
                      sort_order=37, enabled=False))
    await db.commit()

    await apply_data_migrations(_factory(db))
    await apply_data_migrations(_factory(db))  # 幂等

    async with await _fresh_view(db) as fresh:
        sources = {rs.name: rs.action for rs in
                   (await fresh.execute(select(RuleSource))).scalars().all()}
    assert sources["Stream EU (non_ip)"] == "Global"
    # 严格限定历史签名：范围外悬空引用与用户禁用的记录一律保留原值
    assert sources["幽灵源"] == "不存在的组"
    assert sources["Stream KR (ip)"] == "韩国"


@pytest.mark.asyncio
async def test_dangling_migration_scoped_to_legacy_signature(db):
    """签名不完全匹配（用户自定义同名不同 URL）与有效节点 action 均不改写"""
    db.add(ProxyNode(name="US-01", node_type="ss", server="203.0.113.1", port=443,
                     config="US-01 = ss, 203.0.113.1, 443, password=x", enabled=True))
    # 同名但 URL 不同：用户自定义记录，不是历史内置
    db.add(RuleSource(name="Stream EU (non_ip)", url="https://user.example/my-stream.list",
                      action="英国", rule_type="RULE-SET", category="region_stream",
                      sort_order=32, enabled=True))
    # action 指向真实节点：有效引用，不属于历史悬空签名
    db.add(RuleSource(name="节点源", url="https://example.com/node.list",
                      action="US-01", rule_type="RULE-SET", category="global",
                      sort_order=81, enabled=True))
    await db.commit()

    await apply_data_migrations(_factory(db))

    async with await _fresh_view(db) as fresh:
        sources = {rs.name: rs.action for rs in
                   (await fresh.execute(select(RuleSource))).scalars().all()}
    assert sources["Stream EU (non_ip)"] == "英国"  # 签名不匹配：不动
    assert sources["节点源"] == "US-01"              # 有效节点 action：不动


@pytest.mark.asyncio
async def test_dangling_migration_keeps_explicitly_enabled_groups(db):
    """用户已显式开启的地区组（组会生成）：引用保留，不被迁移改写"""
    from app.models import RegionGroup
    from sqlalchemy import update as sa_update

    await db.execute(sa_update(RegionGroup).where(RegionGroup.name == "英国")
                     .values(auto_enabled=True, manual_enabled=True))
    db.add(RuleSource(name="Stream EU (non_ip)", url="https://ruleset.skk.moe/List/non_ip/stream_eu.conf",
                      action="英国", rule_type="RULE-SET", category="region_stream",
                      sort_order=32, enabled=True))
    await db.commit()

    await apply_data_migrations(_factory(db))

    async with await _fresh_view(db) as fresh:
        action = (await fresh.execute(
            select(RuleSource.action).where(RuleSource.name == "Stream EU (non_ip)"))).scalar_one()
    assert action == "英国"



@pytest.mark.asyncio
async def test_dangling_migration_blocked_by_same_name_enabled_node(db):
    """enabled 同名节点（canonical 节点优先，旧引用实际有效）：不改写"""
    db.add(ProxyNode(name="英国", node_type="trojan", server="203.0.113.2", port=443,
                     config="英国 = trojan, 203.0.113.2, 443, password=x", enabled=True))
    db.add(RuleSource(name="Stream EU (non_ip)", url="https://ruleset.skk.moe/List/non_ip/stream_eu.conf",
                      action="英国", rule_type="RULE-SET", category="region_stream",
                      sort_order=32, enabled=True))
    await db.commit()

    await apply_data_migrations(_factory(db))

    async with await _fresh_view(db) as fresh:
        action = (await fresh.execute(
            select(RuleSource.action).where(RuleSource.name == "Stream EU (non_ip)"))).scalar_one()
    assert action == "英国"  # 同名 enabled 节点使旧引用可能仍有效：保守保留


@pytest.mark.asyncio
async def test_dangling_migration_blocked_by_same_name_enabled_service_group(db):
    """enabled 同名服务组（恒生成，旧引用实际有效）：不改写"""
    from app.models import ServiceGroup

    db.add(ServiceGroup(name="韩国", group_type="select", policies=["自动选择"],
                        sort_order=90, enabled=True))
    db.add(RuleSource(name="Stream KR (non_ip)", url="https://ruleset.skk.moe/List/non_ip/stream_kr.conf",
                      action="韩国", rule_type="RULE-SET", category="region_stream",
                      sort_order=36, enabled=True))
    await db.commit()

    await apply_data_migrations(_factory(db))

    async with await _fresh_view(db) as fresh:
        action = (await fresh.execute(
            select(RuleSource.action).where(RuleSource.name == "Stream KR (non_ip)"))).scalar_one()
    assert action == "韩国"


@pytest.mark.asyncio
async def test_dangling_migration_disabled_same_name_candidates_do_not_block(db):
    """disabled 同名节点/服务组不会进入产物：不阻止确定迁移"""
    from app.models import ServiceGroup

    db.add(ProxyNode(name="英国", node_type="trojan", server="203.0.113.3", port=443,
                     config="英国 = trojan, 203.0.113.3, 443, password=x", enabled=False))
    db.add(ServiceGroup(name="英国", group_type="select", policies=["DIRECT"],
                        sort_order=91, enabled=False))
    db.add(RuleSource(name="Stream EU (non_ip)", url="https://ruleset.skk.moe/List/non_ip/stream_eu.conf",
                      action="英国", rule_type="RULE-SET", category="region_stream",
                      sort_order=32, enabled=True))
    await db.commit()

    await apply_data_migrations(_factory(db))
    await apply_data_migrations(_factory(db))  # 幂等

    async with await _fresh_view(db) as fresh:
        action = (await fresh.execute(
            select(RuleSource.action).where(RuleSource.name == "Stream EU (non_ip)"))).scalar_one()
    assert action == "Global"


@pytest.mark.asyncio
async def test_dangling_migration_keeps_builtin_and_valid_actions(db):
    """内置动作与有效组引用（服务组/活跃地区组）一律不动"""
    from app.models import RegionGroup

    hk = (await db.execute(select(RegionGroup).where(RegionGroup.name == "香港"))).scalar_one()
    assert hk.auto_enabled or hk.manual_enabled  # 前置：香港活跃

    db.add(RuleSource(name="内置动作源", url="https://example.com/a.list",
                      action="REJECT-DROP", rule_type="RULE-SET", category="adblock",
                      sort_order=81, enabled=True))
    db.add(RuleSource(name="服务组源", url="https://example.com/b.list",
                      action="AI服务", rule_type="RULE-SET", category="ai",
                      sort_order=82, enabled=True))
    db.add(RuleSource(name="活跃地区源", url="https://example.com/c.list",
                      action="香港", rule_type="RULE-SET", category="region_stream",
                      sort_order=83, enabled=True))
    await db.commit()

    await apply_data_migrations(_factory(db))

    async with await _fresh_view(db) as fresh:
        sources = {rs.name: rs.action for rs in
                   (await fresh.execute(select(RuleSource))).scalars().all()}
    assert sources["内置动作源"] == "REJECT-DROP"
    assert sources["服务组源"] == "AI服务"
    assert sources["活跃地区源"] == "香港"


@pytest.mark.asyncio
async def test_no_change_run_leaves_no_pending(db):
    """无任何变更时迁移仍安全（无条件 commit 空事务，不产生副作用）"""
    await apply_data_migrations(_factory(db))  # 先补齐
    await apply_data_migrations(_factory(db))  # 再跑一次：无变更路径
    # 不抛异常即通过；数据完整性由其他测试的跨 session 断言保证
