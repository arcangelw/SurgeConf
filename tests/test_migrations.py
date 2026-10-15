"""数据迁移 apply_data_migrations 行为测试（内置安全规则 / ipv6-vif 归一 / DNS 默认值升级 / 系统键）

持久化验证要点：断言一律通过独立新 session（db.bind 新开）查询，
防止 autoflush 后未 commit 的事务内可见性造成假阳性。
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bootstrap import apply_data_migrations
from app.models import CustomRule, ConfigProfile, SystemConfig, RuleSource
from app.default_config import SECURITY_PRE_RULES


async def _fresh_view(db) -> AsyncSession:
    """基于同一 engine 开新 session，验证数据已持久化（非事务内可见）"""
    new_session = AsyncSession(bind=db.bind, expire_on_commit=False)
    return new_session


@pytest.mark.asyncio
async def test_security_rules_backfilled_on_legacy_db(db):
    """老库（无内置安全规则）迁移后补齐 STUN 阻断三条，且已持久化"""
    before = (await db.execute(select(CustomRule.rule_text))).scalars().all()
    assert not any("stun" in r.lower() for r in before), "前置条件：老库无 STUN 规则"

    await apply_data_migrations(db)

    async with await _fresh_view(db) as fresh:
        texts = set((await fresh.execute(select(CustomRule.rule_text))).scalars().all())
    for r in SECURITY_PRE_RULES:
        assert r["rule_text"] in texts


@pytest.mark.asyncio
async def test_migration_idempotent(db):
    """重复执行不产生重复规则（跨 session 验证）"""
    await apply_data_migrations(db)
    await apply_data_migrations(db)

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

    await apply_data_migrations(db)
    await apply_data_migrations(db)

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

    await apply_data_migrations(db)

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

    await apply_data_migrations(db)

    async with await _fresh_view(db) as fresh:
        g = (await fresh.execute(select(ConfigProfile.general))).scalars().first()
    assert g["ipv6-vif"] == "disabled"

    # 已是 disabled 时再迁移无变化（幂等）
    await apply_data_migrations(db)
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

    await apply_data_migrations(db)

    async with await _fresh_view(db) as fresh:
        g = (await fresh.execute(select(ConfigProfile.general))).scalars().first()
    assert g["encrypted-dns-server"] == \
        "h3://dns.alidns.com/dns-query, https://dns.alidns.com/dns-query, https://doh.pub/dns-query"
    assert g["hijack-dns"] == "*:53"

    # 幂等：再迁移无变化
    await apply_data_migrations(db)
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

    await apply_data_migrations(db)

    async with await _fresh_view(db) as fresh:
        g = (await fresh.execute(select(ConfigProfile.general))).scalars().first()
    assert g["encrypted-dns-server"] == custom_dns
    assert g["hijack-dns"] == custom_hijack


@pytest.mark.asyncio
async def test_no_resolve_keywords_backfilled_and_persisted(db):
    """缺 no_resolve_keywords 系统键时补默认值并持久化；已有自定义值不覆盖"""
    keys = (await db.execute(select(SystemConfig.key))).scalars().all()
    assert "no_resolve_keywords" not in keys, "前置条件：老库无该键"

    await apply_data_migrations(db)

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

    await apply_data_migrations(db)
    async with await _fresh_view(db) as fresh:
        v = (await fresh.execute(
            select(SystemConfig.value).where(SystemConfig.key == "no_resolve_keywords"))).scalar_one()
    assert v == "custom"


@pytest.mark.asyncio
async def test_no_change_run_leaves_no_pending(db):
    """无任何变更时迁移仍安全（无条件 commit 空事务，不产生副作用）"""
    await apply_data_migrations(db)  # 先补齐
    await apply_data_migrations(db)  # 再跑一次：无变更路径
    # 不抛异常即通过；数据完整性由其他测试的跨 session 断言保证
