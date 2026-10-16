"""Task 4：final_action 生效与生成引用完整性校验。

覆盖：
- 规则文本策略段提取（含 no-resolve / 逻辑规则嵌套逗号 / FINAL 位置差异 / fail closed）
- final_action 直通 FINAL 行（zh/en 翻译、空值/纯空白与未知值拒绝、trim 恰一次共享）
- 规则源 / 前置与通用自定义规则引用校验（disabled 服务组不可引用）
- 策略组成员引用校验（disabled 依赖、pinned 节点被信息关键词过滤、
  合法 DIRECT 与组行选项不误判）
- canonical 对照：内置动作/节点名原样（不翻译）、组逻辑名翻译、
  同名节点与组的冲突取节点优先
- 多错误聚合一次抛出；生成路由 422 且不产出部分配置 / 部分文件
- 订阅边界：无效 token 永远 404，配置错误只在 token 验证后 422
"""

import re

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from unittest.mock import patch

from app.default_config import locale_translate
from app.models import ConfigProfile, CustomRule, ProxyNode, RuleSource, ServiceGroup
from app.services.config_validation import (
    BUILTIN_ACTIONS,
    GenerationContext,
    GroupMemberRef,
    InvalidConfiguration,
    RuleActionRef,
    extract_rule_action,
    translate_policy_name,
    translate_rule_action,
    validate_generated_references,
)
from app.services.generator import generate_surge_config

# ────────────────────────────────────────────
# 辅助
# ────────────────────────────────────────────


@pytest_asyncio.fixture
async def default_profile(db):
    result = await db.execute(select(ConfigProfile).where(ConfigProfile.is_default == True))
    return result.scalar_one()


async def _set_final_action(db, profile, value):
    profile.final_action = value
    await db.commit()


# ════════════════════════════════════════════
# 一、规则策略段提取（结构化提取，fail closed）
# ════════════════════════════════════════════


class TestExtractRuleAction:
    def test_domain_rule(self):
        assert extract_rule_action("DOMAIN,example.com,AI服务") == "AI服务"

    def test_action_with_no_resolve(self):
        assert extract_rule_action("IP-CIDR,192.0.2.0/24,大陆直连,no-resolve") == "大陆直连"

    def test_logic_rule_nested_commas(self):
        rule = "AND,((PROTOCOL,UDP), (DOMAIN-SUFFIX,googlevideo.com)),REJECT-NO-DROP"
        assert extract_rule_action(rule) == "REJECT-NO-DROP"

    def test_logic_rule_with_no_resolve(self):
        rule = "OR,((DEST-PORT,3478), (PROTOCOL,STUN)),兜底,no-resolve"
        assert extract_rule_action(rule) == "兜底"

    def test_final_rule_policy_position(self):
        assert extract_rule_action("FINAL,兜底,dns-failed") == "兜底"
        assert extract_rule_action("FINAL,兜底") == "兜底"

    def test_builtin_action_passthrough(self):
        assert extract_rule_action("DOMAIN-KEYWORD,96110,REJECT") == "REJECT"
        assert extract_rule_action("PROTOCOL,STUN,REJECT") == "REJECT"

    def test_url_regex_payload_with_parentheses(self):
        assert extract_rule_action("URL-REGEX,(Subject|HELO|SMTP),DIRECT") == "DIRECT"

    def test_unparseable_short_rule_fails_closed(self):
        assert extract_rule_action("SHORT-RULE") is None
        assert extract_rule_action("") is None
        assert extract_rule_action("   ") is None

    def test_too_many_top_level_segments_fails_closed(self):
        # 顶层 5 段无法唯一定位策略段：拒绝而不是猜最后一段
        assert extract_rule_action("DOMAIN,a,b,c,DIRECT") is None

    def test_empty_policy_fails_closed(self):
        assert extract_rule_action("DOMAIN,example.com,") is None


class TestTranslateRuleAction:
    def test_translates_policy_only(self):
        line = translate_rule_action("DOMAIN-SUFFIX,example.com,AI服务", locale_translate)
        assert line == "DOMAIN-SUFFIX,example.com,AI服务"

    def test_keeps_no_resolve_suffix(self):
        # 旧行为会把 no-resolve 当策略翻译；修复后策略段正确定位且修饰符保留
        line = translate_rule_action(
            "IP-CIDR,192.0.2.0/24,大陆直连,no-resolve",
            lambda n: locale_translate(n, "zh"))
        assert line == "IP-CIDR,192.0.2.0/24,大陆直连,no-resolve"

    def test_english_locale_translates_policy(self):
        line = translate_rule_action("DOMAIN-SUFFIX,example.com,AI服务",
                                     lambda n: locale_translate(n, "en"))
        assert line == "DOMAIN-SUFFIX,example.com,AI"

    def test_resolve_translator_keeps_node_name_verbatim(self):
        # 输出流与校验同源：t 传 resolve 版翻译时，精确命中节点名的策略原样
        nodes = frozenset({"香港"})
        resolver = lambda n: n if n in BUILTIN_ACTIONS or n in nodes else locale_translate(n, "en")
        line = translate_rule_action("DOMAIN,hk-node.example,香港", resolver)
        assert line == "DOMAIN,hk-node.example,香港"

    def test_logic_rule_translated_and_reassembled(self):
        rule = "AND,((PROTOCOL,UDP), (DEST-PORT,3478)),REJECT"
        assert translate_rule_action(rule, lambda n: locale_translate(n, "zh")) == rule

    def test_unparseable_returns_none(self):
        assert translate_rule_action("SHORT-RULE", locale_translate) is None


# ════════════════════════════════════════════
# 二、GenerationContext 与聚合校验
# ════════════════════════════════════════════


def _ctx(locale="zh", nodes=(), regions=(), services=(), combined=(),
         actions=(), members=(), final_action="手动选择", target="mac_workstation"):
    # 名字集合语义 = 本次实际输出名（节点显示名 / t() 翻译后的组名）
    return GenerationContext(
        locale=locale,
        target=target,
        node_names=frozenset(nodes),
        region_group_names=frozenset(regions),
        service_group_names=frozenset(services),
        combined_group_names=frozenset(combined),
        rule_actions=tuple(actions),
        group_members=tuple(members),
        final_action=final_action,
    )


class TestValidateGeneratedReferences:
    def test_valid_context_passes(self):
        ctx = _ctx(
            services=("AI服务", "兜底", "大陆直连"),
            regions=("美国",),
            combined=("手动选择", "自动选择"),
            actions=(RuleActionRef("规则源「OpenAI」", "AI服务"),
                     RuleActionRef("前置规则", "REJECT-NO-DROP"),
                     RuleActionRef("通用规则", "美国")),
            members=(GroupMemberRef("兜底", "大陆直连"),
                     GroupMemberRef("自动选择", "美国"),
                     GroupMemberRef("手动选择", "大陆直连")),
            final_action="兜底",
        )
        validate_generated_references(ctx)  # 不抛即通过

    def test_builtin_actions_always_available(self):
        for action in BUILTIN_ACTIONS:
            ctx = _ctx(actions=(RuleActionRef("r", action),),
                       combined=("手动选择",), members=(GroupMemberRef("g", action),),
                       final_action="手动选择")
            validate_generated_references(ctx)

    def test_english_locale_compares_output_names(self):
        # 集合是 en 输出名（AI / NoAuto）；逻辑引用经同一解析流翻译后命中
        ctx = _ctx(locale="en", services=("AI",), combined=("NoAuto",),
                   actions=(RuleActionRef("规则源「OpenAI」", "AI服务"),),
                   final_action="手动选择")
        validate_generated_references(ctx)

    def test_no_fuzzy_fallback_for_localized_name_in_zh(self):
        # zh 档案里直接写英文名「AI」：不做反向映射回「AI服务」，判无效
        ctx = _ctx(locale="zh", services=("AI服务",), combined=("手动选择",),
                   actions=(RuleActionRef("规则源「X」", "AI"),))
        with pytest.raises(InvalidConfiguration) as ei:
            validate_generated_references(ctx)
        assert any("AI" in e for e in ei.value.errors)

    def test_node_reference_not_translated_in_english(self):
        # 节点名「香港」在 en 档案不翻译：引用原样命中实际节点输出名
        ctx = _ctx(locale="en", nodes=("香港",), regions=("Hong Kong",),
                   combined=("NoAuto",),
                   actions=(RuleActionRef("自定义规则", "香港"),),
                   final_action="手动选择")
        validate_generated_references(ctx)

    def test_node_name_wins_over_same_named_group(self):
        # 同名冲突取节点优先（既有产品预期）：节点「香港」与地区组「香港」
        # 并存时，规则引用解析为节点名而不是翻译后的组名
        from app.services.config_validation import resolve_policy_reference
        resolved = resolve_policy_reference("香港", "en", frozenset({"香港"}))
        assert resolved == "香港"

    def test_missing_group_reported_with_source(self):
        ctx = _ctx(combined=("手动选择",),
                   actions=(RuleActionRef("规则源「Stream EU」", "英国"),))
        with pytest.raises(InvalidConfiguration) as ei:
            validate_generated_references(ctx)
        assert any("Stream EU" in e and "英国" in e for e in ei.value.errors)

    def test_unparseable_action_fails_closed(self):
        ctx = _ctx(combined=("手动选择",),
                   actions=(RuleActionRef("前置规则「SHORT」", None),))
        with pytest.raises(InvalidConfiguration) as ei:
            validate_generated_references(ctx)
        assert any("SHORT" in e for e in ei.value.errors)

    def test_empty_final_action_rejected(self):
        for value in (None, "", "   "):
            ctx = _ctx(final_action=value)
            with pytest.raises(InvalidConfiguration) as ei:
                validate_generated_references(ctx)
            assert any("final_action" in e for e in ei.value.errors)

    def test_unknown_final_action_rejected(self):
        ctx = _ctx(final_action="幽灵组")
        with pytest.raises(InvalidConfiguration) as ei:
            validate_generated_references(ctx)
        assert any("幽灵组" in e and "final_action" in e for e in ei.value.errors)

    def test_disabled_group_name_not_in_available(self):
        # 被禁用的服务组：context 不含该名字 → 引用即错
        ctx = _ctx(services=(), combined=("手动选择",),
                   actions=(RuleActionRef("规则源「X」", "AI服务"),))
        with pytest.raises(InvalidConfiguration):
            validate_generated_references(ctx)

    def test_node_names_are_referenceable(self):
        ctx = _ctx(nodes=("HK-01",), combined=("手动选择",),
                   actions=(RuleActionRef("自定义规则", "HK-01"),))
        validate_generated_references(ctx)

    def test_all_errors_aggregated_in_one_raise(self):
        ctx = _ctx(
            combined=("手动选择",),
            actions=(RuleActionRef("规则源「A」", "幽灵组A"),
                     RuleActionRef("前置规则「B」", "幽灵组B"),
                     RuleActionRef("通用规则「C」", None)),
            members=(GroupMemberRef("苹果服务", "香港"),),
            final_action="幽灵组F",
        )
        with pytest.raises(InvalidConfiguration) as ei:
            validate_generated_references(ctx)
        errors = ei.value.errors
        assert len(errors) == 5
        assert any("幽灵组A" in e for e in errors)
        assert any("幽灵组B" in e for e in errors)
        assert any("规则「C」" in e or "C" in e for e in errors)
        assert any("苹果服务" in e and "香港" in e for e in errors)
        assert any("final_action" in e for e in errors)


class TestGroupMemberValidation:
    def test_dangling_member_reported_with_group(self):
        ctx = _ctx(combined=("手动选择",),
                   members=(GroupMemberRef("苹果服务", "香港"),))
        with pytest.raises(InvalidConfiguration) as ei:
            validate_generated_references(ctx)
        assert any("苹果服务" in e and "香港" in e for e in ei.value.errors)

    def test_direct_member_is_valid(self):
        ctx = _ctx(services=("大陆直连",), combined=("手动选择",),
                   members=(GroupMemberRef("大陆直连", "DIRECT"),),
                   final_action="手动选择")
        validate_generated_references(ctx)

    def test_node_member_matches_output_name(self):
        ctx = _ctx(nodes=("(机场A) HK-01",), combined=("手动选择",),
                   members=(GroupMemberRef("AI服务", "(机场A) HK-01"),),
                   final_action="手动选择")
        validate_generated_references(ctx)

    def test_english_group_output_name_member(self):
        # 成员登记的是输出名（en 翻译后），直接对照不再翻译
        ctx = _ctx(locale="en", regions=("United States Manual",), combined=("NoAuto",),
                   members=(GroupMemberRef("兜底", "United States Manual"),
                            GroupMemberRef("手动选择", "NoAuto")),
                   final_action="手动选择")
        validate_generated_references(ctx)


class TestTranslatePolicyName:
    def test_matches_locale_translate(self):
        assert translate_policy_name("手动选择", "en") == "NoAuto"
        assert translate_policy_name("手动选择", "zh") == "手动选择"
        assert translate_policy_name("DIRECT", "en") == "DIRECT"


# ════════════════════════════════════════════
# 三、集成：generate_surge_config 端到端
# ════════════════════════════════════════════


class TestFinalActionIntegration:
    @pytest.mark.asyncio
    async def test_final_action_controls_final_rule(self, db, default_profile):
        await _set_final_action(db, default_profile, "大陆直连")
        config = await generate_surge_config(db, default_profile.id)
        assert "FINAL,大陆直连,dns-failed" in config
        assert "FINAL,兜底,dns-failed" not in config

    @pytest.mark.asyncio
    async def test_final_action_english_locale(self, db, default_profile):
        await _set_final_action(db, default_profile, "手动选择")
        config = await generate_surge_config(db, default_profile.id, locale_override="en")
        assert "FINAL,NoAuto,dns-failed" in config

    @pytest.mark.asyncio
    async def test_final_action_whitespace_trimmed_once_and_shared(self, db, default_profile):
        """前后空白只 trim 一次，校验与 FINAL 输出共享同一值（逐字一致）"""
        await _set_final_action(db, default_profile, "  手动选择  ")
        config = await generate_surge_config(db, default_profile.id)
        assert "FINAL,手动选择,dns-failed" in config
        # FINAL 行与校验名逐字一致：策略段不得残留前后空白
        final_lines = [l for l in config.splitlines() if l.startswith("FINAL,")]
        assert final_lines == ["FINAL,手动选择,dns-failed"]

    @pytest.mark.asyncio
    async def test_final_action_whitespace_only_aborts(self, db, default_profile):
        await _set_final_action(db, default_profile, "   ")
        with pytest.raises(InvalidConfiguration) as ei:
            await generate_surge_config(db, default_profile.id)
        assert any("final_action" in e for e in ei.value.errors)

    @pytest.mark.asyncio
    async def test_empty_final_action_aborts(self, db, default_profile):
        await _set_final_action(db, default_profile, "")
        with pytest.raises(InvalidConfiguration) as ei:
            await generate_surge_config(db, default_profile.id)
        assert any("final_action" in e for e in ei.value.errors)

    @pytest.mark.asyncio
    async def test_unknown_final_action_aborts(self, db, default_profile):
        await _set_final_action(db, default_profile, "幽灵组")
        with pytest.raises(InvalidConfiguration) as ei:
            await generate_surge_config(db, default_profile.id)
        assert any("幽灵组" in e for e in ei.value.errors)


class TestReferenceValidationIntegration:
    @pytest.mark.asyncio
    async def test_missing_ai_service_group_aborts_generation(self, db):
        # 引用 AI服务 的规则存在，但服务组被禁用 → 聚合报错，不生成部分配置
        db.add(CustomRule(rule_text="DOMAIN-SUFFIX,apple-relay.example.com,AI服务",
                          position="pre", sort_order=50, enabled=True))
        await db.commit()
        await db.execute(
            update(ServiceGroup).where(ServiceGroup.name == "AI服务").values(enabled=False))
        await db.commit()
        with pytest.raises(InvalidConfiguration, match="AI服务"):
            await generate_surge_config(db)

    @pytest.mark.asyncio
    async def test_rule_source_dangling_action_aborts(self, db):
        db.add(RuleSource(name="幽灵规则源", url="https://example.com/ghost.list",
                          action="幽灵组", rule_type="RULE-SET", category="global",
                          sort_order=80, enabled=True))
        await db.commit()
        with pytest.raises(InvalidConfiguration) as ei:
            await generate_surge_config(db)
        assert any("幽灵规则源" in e and "幽灵组" in e for e in ei.value.errors)

    @pytest.mark.asyncio
    async def test_rule_source_valid_action_passes(self, db):
        db.add(RuleSource(name="有效规则源", url="https://example.com/ok.list",
                          action="AI服务", rule_type="RULE-SET", category="ai",
                          sort_order=80, enabled=True))
        await db.commit()
        config = await generate_surge_config(db)
        assert "RULE-SET,https://example.com/ok.list,AI服务" in config

    @pytest.mark.asyncio
    async def test_custom_rule_no_resolve_action_position(self, db):
        # no-resolve 在尾部：策略段是倒数第二段，提取不得把 no-resolve 当策略
        db.add(CustomRule(rule_text="IP-CIDR,192.0.2.0/24,幽灵组,no-resolve",
                          position="general", sort_order=50, enabled=True))
        await db.commit()
        with pytest.raises(InvalidConfiguration) as ei:
            await generate_surge_config(db)
        assert any("幽灵组" in e for e in ei.value.errors)
        assert not any("no-resolve" in e and "幽灵组" not in e for e in ei.value.errors)

    @pytest.mark.asyncio
    async def test_custom_rule_valid_no_resolve_kept(self, db):
        db.add(CustomRule(rule_text="IP-CIDR,192.0.2.0/24,大陆直连,no-resolve",
                          position="general", sort_order=50, enabled=True))
        await db.commit()
        config = await generate_surge_config(db)
        assert "IP-CIDR,192.0.2.0/24,大陆直连,no-resolve" in config

    @pytest.mark.asyncio
    async def test_custom_logic_rule_nested_commas_validated(self, db):
        db.add(CustomRule(
            rule_text="AND,((PROTOCOL,UDP), (DOMAIN-SUFFIX,example.com)),幽灵组",
            position="pre", sort_order=50, enabled=True))
        await db.commit()
        with pytest.raises(InvalidConfiguration) as ei:
            await generate_surge_config(db)
        assert any("幽灵组" in e for e in ei.value.errors)

    @pytest.mark.asyncio
    async def test_custom_logic_rule_valid_passes(self, db):
        db.add(CustomRule(
            rule_text="AND,((PROTOCOL,UDP), (DOMAIN-SUFFIX,example.com)),REJECT-NO-DROP",
            position="pre", sort_order=50, enabled=True))
        await db.commit()
        config = await generate_surge_config(db)
        assert "AND,((PROTOCOL,UDP), (DOMAIN-SUFFIX,example.com)),REJECT-NO-DROP" in config

    @pytest.mark.asyncio
    async def test_unparseable_custom_rule_aborts(self, db):
        db.add(CustomRule(rule_text="SHORT-RULE", position="general",
                          sort_order=50, enabled=True))
        await db.commit()
        with pytest.raises(InvalidConfiguration) as ei:
            await generate_surge_config(db)
        assert any("SHORT-RULE" in e for e in ei.value.errors)

    @pytest.mark.asyncio
    async def test_rule_reference_to_node_name_english_locale(self, db):
        """规则 action 引用节点名：en 档案下原样保留（节点名不翻译）"""
        db.add(ProxyNode(name="HK-Premium", node_type="trojan", server="203.0.113.20",
                         port=443, config="HK-Premium = trojan, 203.0.113.20, 443, password=x",
                         enabled=True))
        db.add(CustomRule(rule_text="DOMAIN,node-route.example,HK-Premium",
                          position="general", sort_order=50, enabled=True))
        await db.commit()
        config = await generate_surge_config(db, locale_override="en")
        assert "DOMAIN,node-route.example,HK-Premium" in config

    @pytest.mark.asyncio
    async def test_node_named_as_region_resolves_to_node(self, db):
        """冲突锁定：节点名与地区组同名（香港）时，规则引用解析为节点名。

        en 档案下输出保留「香港」（[Proxy] 段的实际节点名），不误翻为
        组显示名 Hong Kong；地区组行本身仍正常翻译。
        """
        db.add(ProxyNode(name="香港", node_type="trojan", server="203.0.113.99",
                         port=443, config="香港 = trojan, 203.0.113.99, 443, password=x",
                         enabled=True))
        db.add(CustomRule(rule_text="DOMAIN,hk-node.example,香港",
                          position="general", sort_order=60, enabled=True))
        await db.commit()
        config = await generate_surge_config(db, locale_override="en")
        assert "DOMAIN,hk-node.example,香港" in config
        assert '"Hong Kong" = url-test,' in config  # 地区组照常翻译

    @pytest.mark.asyncio
    async def test_multiple_errors_reported_together(self, db, default_profile):
        await _set_final_action(db, default_profile, "幽灵组F")
        db.add(RuleSource(name="坏源一", url="https://example.com/a.list",
                          action="幽灵组A", rule_type="RULE-SET", category="global",
                          sort_order=80, enabled=True))
        db.add(CustomRule(rule_text="DOMAIN,ghost.example.com,幽灵组B",
                          position="general", sort_order=50, enabled=True))
        await db.commit()
        with pytest.raises(InvalidConfiguration) as ei:
            await generate_surge_config(db)
        assert len(ei.value.errors) == 3
        assert any("幽灵组A" in e for e in ei.value.errors)
        assert any("幽灵组B" in e for e in ei.value.errors)
        assert any("final_action" in e for e in ei.value.errors)

    @pytest.mark.asyncio
    async def test_all_valid_references_three_targets(self, db, default_profile):
        """有效引用三目标回归：mac_workstation / mac_gateway / ios_mobile 全部可生成"""
        for target, udp in (("mac_workstation", "scoped"),
                            ("mac_gateway", "compatible"),
                            ("ios_mobile", "compatible")):
            profile = ConfigProfile(
                name=f"目标-{target}", is_default=False, locale="zh",
                final_action="手动选择", target=target, udp_security_mode=udp)
            db.add(profile)
        await db.commit()
        result = await db.execute(select(ConfigProfile).where(ConfigProfile.is_default == False))
        for profile in result.scalars().all():
            config = await generate_surge_config(db, profile.id)
            assert "FINAL,手动选择,dns-failed" in config


class TestGroupGraphValidationIntegration:
    @pytest.mark.asyncio
    async def test_disabled_region_referenced_by_service_group_aborts(self, db):
        """禁用的地区组仍被服务组 policies 引用 → 组图悬空，生成中止"""
        from app.models import RegionGroup
        await db.execute(update(RegionGroup).where(RegionGroup.name == "香港")
                         .values(enabled=False))
        await db.commit()
        with pytest.raises(InvalidConfiguration) as ei:
            await generate_surge_config(db)
        errors = "\n".join(ei.value.errors)
        assert "苹果服务" in errors and "香港" in errors

    @pytest.mark.asyncio
    async def test_disabled_service_group_breaks_dependent_groups_aborts(self, db):
        """禁用「大陆直连」服务组：引用它的组合组（手动选择）与服务组悬空"""
        await db.execute(
            update(ServiceGroup).where(ServiceGroup.name == "大陆直连").values(enabled=False))
        await db.commit()
        with pytest.raises(InvalidConfiguration) as ei:
            await generate_surge_config(db)
        errors = "\n".join(ei.value.errors)
        assert "手动选择" in errors  # 组合组成员悬空
        assert "大陆直连" in errors

    @pytest.mark.asyncio
    async def test_pinned_node_filtered_by_info_keywords_aborts(self, db):
        """pinned 节点名命中信息排除关键词：[Proxy] 段无此节点，服务组引用悬空"""
        db.add(ProxyNode(name="剩余流量提示", node_type="trojan", server="203.0.113.7",
                         port=443, config="剩余流量提示 = trojan, 203.0.113.7, 443, password=x",
                         enabled=True, pinned_services=["AI服务"]))
        await db.commit()
        with pytest.raises(InvalidConfiguration) as ei:
            await generate_surge_config(db)
        errors = "\n".join(ei.value.errors)
        assert "AI服务" in errors and "剩余流量提示" in errors
        assert "剩余流量提示 = trojan" not in errors  # 被过滤的节点本身不输出

    @pytest.mark.asyncio
    async def test_default_graph_passes_and_options_not_mistaken_as_members(self, db_with_nodes):
        """默认组图全通过：组行选项（url/interval/regex-filter）不是成员，不误报"""
        config = await generate_surge_config(db_with_nodes)
        assert "手动选择 = select, 大陆直连" in config
        assert "大陆直连 = select, DIRECT" in config  # 合法内置动作成员
        assert "RULE-SET,LAN,DIRECT,no-resolve" in config

    @pytest.mark.asyncio
    async def test_every_proxy_group_line_is_referenceable(self, db_with_nodes):
        """防漂移不变量：[Proxy Group] 实际输出的每个组名都在校验可用集合内。

        若 _build_proxy_groups 新增组行但未登记到 GroupNameSinks，
        该组名将无法被规则引用，此测试守卫登记完整性。
        """
        from app.services.generator import GroupNameSinks, _build_proxy_groups
        from sqlalchemy import select as _select
        from app.models import RegionGroup, ProxyNode

        db = db_with_nodes
        regions = (await db.execute(_select(RegionGroup).where(RegionGroup.enabled == True)
                                    .order_by(RegionGroup.sort_order))).scalars().all()
        services = (await db.execute(_select(ServiceGroup).where(ServiceGroup.enabled == True)
                                     .order_by(ServiceGroup.sort_order))).scalars().all()
        nodes = (await db.execute(_select(ProxyNode).where(ProxyNode.enabled == True)
                                  .order_by(ProxyNode.name))).scalars().all()
        sinks = GroupNameSinks()
        lines = _build_proxy_groups(nodes, regions, services,
                                    lambda n: locale_translate(n, "zh"), {},
                                    name_sinks=sinks)
        emitted = set()
        for line in lines:
            if " = " in line and line.strip():
                emitted.add(line.split(" = ", 1)[0].strip().strip('"'))
        registered = sinks.region | sinks.service | sinks.combined | {"全部节点"}
        assert emitted <= registered, emitted - registered
        # 成员登记完备：每条成员引用的 group 本身也是被登记的组
        assert sinks.member_refs
        member_groups = {ref.group for ref in sinks.member_refs}
        assert member_groups <= registered | sinks.region | sinks.service | sinks.combined


# ════════════════════════════════════════════
# 四、路由映射：422 + 完整错误列表 + 无部分输出
# ════════════════════════════════════════════


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """独立临时库 + 手动驱动 lifespan（与 test_api_endpoints 相同装配）"""
    from app.main import app as fastapi_app
    from app.database import DatabaseRuntime, get_db

    monkeypatch.setenv("SURGECONF_DATA_DIR", str(tmp_path / "runtime"))
    test_runtime = DatabaseRuntime.create(tmp_path / "runtime" / "surge_conf.db")

    async def override_get_db():
        async with test_runtime.session_factory() as session:
            yield session

    fastapi_app.dependency_overrides[get_db] = override_get_db
    with patch("app.database.database", test_runtime), \
         patch("app.bootstrap.database", test_runtime):
        async with fastapi_app.router.lifespan_context(fastapi_app):
            async with AsyncClient(transport=ASGITransport(app=fastapi_app),
                                   base_url="http://test") as c:
                page = await c.get("/")
                meta = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
                assert meta, "回环 GET / 应签发隐式会话并输出 CSRF"
                c.headers["X-CSRF-Token"] = meta.group(1)
                c._test_factory = test_runtime.session_factory
                c._runtime = test_runtime
                yield c

    fastapi_app.dependency_overrides.pop(get_db, None)
    await test_runtime.dispose()


async def _break_default_config(client, value="幽灵组"):
    """直插数据库：把默认档案 final_action 改成无效策略"""
    async with client._test_factory() as db:
        await db.execute(update(ConfigProfile)
                         .where(ConfigProfile.is_default == True)
                         .values(final_action=value))
        await db.commit()


class TestRouteMapping:
    @pytest.mark.asyncio
    async def test_generate_route_returns_422_with_errors(self, client):
        await _break_default_config(client)
        resp = await client.get("/api/configs/generate?profile_id=1")
        assert resp.status_code == 422
        errors = resp.json()["detail"]["errors"]
        assert any("幽灵组" in e for e in errors)

    @pytest.mark.asyncio
    async def test_preview_route_returns_422_with_errors(self, client):
        await _break_default_config(client)
        resp = await client.get("/api/configs/preview?profile_id=1")
        assert resp.status_code == 422
        assert any("幽灵组" in e for e in resp.json()["detail"]["errors"])

    @pytest.mark.asyncio
    async def test_export_route_422_writes_no_partial_file(self, client, tmp_path):
        from app.runtime_paths import resolve_runtime_paths

        await _break_default_config(client)
        resp = await client.post("/api/configs/1/exports", json={"confirm": True})
        assert resp.status_code == 422
        assert any("幽灵组" in e for e in resp.json()["detail"]["errors"])

        paths = resolve_runtime_paths()
        assert not paths.exports.exists() or list(paths.exports.iterdir()) == [], \
            "无效配置不得产出任何部分导出文件"

    @pytest.mark.asyncio
    async def test_whitespace_final_action_export_no_partial_file(self, client):
        """final_action 纯空白：export 422 且零文件（trim 归一的无效路径同样闭环）"""
        from app.runtime_paths import resolve_runtime_paths

        await _break_default_config(client, value="   ")
        resp = await client.post("/api/configs/1/exports", json={"confirm": True})
        assert resp.status_code == 422
        assert any("final_action" in e for e in resp.json()["detail"]["errors"])
        paths = resolve_runtime_paths()
        assert not paths.exports.exists() or list(paths.exports.iterdir()) == []

    @pytest.mark.asyncio
    async def test_normal_domain_errors_still_propagate(self, client, monkeypatch):
        """非 InvalidConfiguration 的领域错误（如凭据 env 非法）继续原样传播"""
        from app.routers import configs as configs_router

        async def _boom(db, profile_id=None, locale_override=None):
            raise ValueError("boom: 非领域错误")

        # patch 路由模块的导入点：configs.py 以 from-import 绑定名字
        monkeypatch.setattr(configs_router, "generate_surge_config", _boom)
        # 非领域异常不被路由吞掉或改写：直接向外传播
        # （ASGITransport 默认 raise_app_exceptions=True，原样抛给调用方）
        with pytest.raises(ValueError, match="boom"):
            await client.get("/api/configs/generate?profile_id=1")

    @pytest.mark.asyncio
    async def test_subscribe_invalid_token_is_404_not_422(self, client):
        """无效 token 永远 404：配置错误信息不泄露给未验证对端"""
        await _break_default_config(client)
        resp = await client.get("/api/configs/profiles/1/subscribe?token=bad-token")
        assert resp.status_code == 404
        assert "幽灵组" not in resp.text

    @pytest.mark.asyncio
    async def test_subscribe_valid_token_invalid_config_is_422(self, client):
        """token 验证通过后，配置本身无效可如实 422（含完整错误列表）"""
        await _break_default_config(client)
        token = (await client.post("/api/configs/profiles/1/token")).json()["token"]
        resp = await client.get(f"/api/configs/profiles/1/subscribe?token={token}")
        assert resp.status_code == 422
        assert any("幽灵组" in e for e in resp.json()["detail"]["errors"])

# ════════════════════════════════════════════
# 五、规则顺序不变量（稳定身份 + validate_rule_order）
# ════════════════════════════════════════════


from types import SimpleNamespace

from app.default_config import DEFAULT_RULE_SOURCES
from app.services.config_validation import rule_order_violations, validate_rule_order


def _builtin_stub(name: str, sort_order: int, source_id: int, **override):
    """按内置默认身份构造校验候选（真实 name/url/rule_type，不臆造）"""
    default = next(rs for rs in DEFAULT_RULE_SOURCES if rs["name"] == name)
    fields = {**default, "sort_order": sort_order, "id": source_id,
              "enabled": True, **override}
    return SimpleNamespace(**fields)


@pytest.mark.parametrize(("earlier", "later", "earlier_label", "later_label"), [
    ("AI 综合", "AppStore", "AI", "Apple"),
    ("AI 综合", "Google", "AI", "Google"),
    ("AI 兜底", "Global", "AI", "Global"),
    ("DouYin", "TikTok", "抖音", "TikTok"),
    ("Domestic (ip)", "Global", "国内直连", "Global"),
])
def test_each_invariant_pair_violation_detected(earlier, later, earlier_label, later_label):
    """同一不变量两侧均在且顺序颠倒时必须报错，错误含双方源名"""
    ordered = [
        _builtin_stub(later, sort_order=1, source_id=1),
        _builtin_stub(earlier, sort_order=2, source_id=2),
    ]
    with pytest.raises(InvalidConfiguration) as ei:
        validate_rule_order(ordered)
    message = "\n".join(ei.value.errors)
    assert earlier in message and later in message


@pytest.mark.parametrize(("earlier", "later"), [
    ("AI 综合", "AppStore"),
    ("AI 综合", "Google"),
    ("AI 兜底", "Global"),
    ("DouYin", "TikTok"),
    ("Domestic (ip)", "Global"),
])
def test_compliant_order_passes(earlier, later):
    ordered = [
        _builtin_stub(earlier, sort_order=1, source_id=1),
        _builtin_stub(later, sort_order=2, source_id=2),
    ]
    assert rule_order_violations(ordered) == []


@pytest.mark.parametrize(("earlier", "later"), [
    ("AI 综合", "AppStore"),
    ("AI 综合", "Google"),
    ("AI 兜底", "Global"),
    ("DouYin", "TikTok"),
    ("Domestic (ip)", "Global"),
])
def test_missing_either_side_not_reported(earlier, later):
    """可选源缺失（一侧不存在）不得误报"""
    assert rule_order_violations([_builtin_stub(earlier, 5, 1)]) == []
    assert rule_order_violations([_builtin_stub(later, 5, 1)]) == []


def test_custom_source_with_builtin_category_not_checked():
    """用户自定义源即使借用内置 category（url 不同）也不参与不变量"""
    custom_ai = _builtin_stub("AI 综合", 10, 1,
                              url="https://example.com/my-own-ai.list")
    builtin_apple = _builtin_stub("AppStore", 1, 2)
    assert rule_order_violations([builtin_apple, custom_ai]) == []


def test_identity_tampered_builtin_not_checked():
    """内置记录被用户改名后不再视为内置：不参与不变量（保守不动，运行时另由显式报错路径覆盖）"""
    renamed = _builtin_stub("AI 综合", 10, 1)
    renamed.name = "我的综合 AI"  # 身份字段被改：三元组不再匹配内置
    builtin_apple = _builtin_stub("AppStore", 1, 2)
    assert rule_order_violations([builtin_apple, renamed]) == []


class TestSortOrderTieBreak:
    def test_same_sort_order_ai_with_smaller_id_first_passes(self):
        """同 sort_order 时按 id 升序 tie-break：AI(id=1) 先于 Apple(id=2) 合规"""
        ordered = [
            _builtin_stub("AI 综合", 50, 1),
            _builtin_stub("AppStore", 50, 2),
        ]
        assert rule_order_violations(ordered) == []

    def test_same_sort_order_ai_with_larger_id_later_violates(self):
        """同 sort_order：Apple(id=1) 先于 AI(id=2) 违例"""
        ordered = [
            _builtin_stub("AppStore", 50, 1),
            _builtin_stub("AI 综合", 50, 2),
        ]
        errors = rule_order_violations(ordered)
        assert len(errors) == 1
        assert "AI 综合" in errors[0] and "AppStore" in errors[0]


def test_multiple_invariant_violations_aggregated():
    """AI/Apple 与 DouYin/TikTok 同时违例：一次抛出全部错误"""
    ordered = [
        _builtin_stub("AppStore", 1, 1),
        _builtin_stub("TikTok", 2, 2),
        _builtin_stub("AI 综合", 3, 3),
        _builtin_stub("DouYin", 4, 4),
    ]
    with pytest.raises(InvalidConfiguration) as ei:
        validate_rule_order(ordered)
    assert len(ei.value.errors) == 2
    joined = "\n".join(ei.value.errors)
    assert "AI 综合" in joined and "AppStore" in joined
    assert "DouYin" in joined and "TikTok" in joined


@pytest_asyncio.fixture
async def full_default_sources(db):
    """补齐 conftest 未种的默认规则源，得到完整内置集合（含新默认排序）"""
    existing = {rs.name for rs in (await db.execute(select(RuleSource))).scalars().all()}
    for rs in DEFAULT_RULE_SOURCES:
        if rs["name"] not in existing:
            db.add(RuleSource(**rs))
    await db.commit()
    return db


def _rule_section_lines(config: str) -> list[str]:
    section = config.split("[Rule]", 1)[1].split("[Host]", 1)[0]
    return [line for line in section.splitlines() if line.strip() and not line.startswith("#")]


@pytest.mark.asyncio
@pytest.mark.parametrize(("earlier_url", "later_url"), [
    ("OpenAI/OpenAI.list", "AppStore/AppStore.list"),
    ("Rule/OpenAI.list", "Apple/Apple.list"),
    ("non_ip/ai.conf", "Google/Google.list"),
    ("non_ip/ai.conf", "non_ip/global.conf"),
    ("DouYin/DouYin.list", "TikTok.list"),
    ("ip/domestic.conf", "non_ip/global.conf"),
    ("WeChat/WeChat.list", "non_ip/global.conf"),
])
async def test_required_rule_order_in_generated_output(full_default_sources, earlier_url, later_url):
    """完整默认源生成产物中，每个不变量对（按规则源 URL 定位行）的顺序符合要求"""
    config = await generate_surge_config(full_default_sources)
    lines = _rule_section_lines(config)
    earlier_idx = next(i for i, l in enumerate(lines) if earlier_url in l)
    later_idx = next(i for i, l in enumerate(lines) if later_url in l)
    assert earlier_idx < later_idx


@pytest.mark.asyncio
async def test_ip_rule_sources_always_use_no_resolve(full_default_sources):
    """以默认数据的显式 ip_rules 声明为唯一真源：每条默认源的产物行
    恰好匹配声明（True → 以 ,no-resolve 结尾且不重复；False → 不带）"""
    config = await generate_surge_config(full_default_sources)
    lines = _rule_section_lines(config)
    flagged = unflagged = 0
    for rs in DEFAULT_RULE_SOURCES:
        line = next(l for l in lines if rs["url"] in l)
        if rs.get("ip_rules"):
            flagged += 1
            assert line.endswith(",no-resolve"), rs["name"]
            assert ",no-resolve,no-resolve" not in line, rs["name"]
        else:
            unflagged += 1
            assert not line.endswith(",no-resolve"), rs["name"]
    assert flagged, "默认源应包含声明的 IP 类规则集"
    assert unflagged, "默认源应包含未声明的非 IP 规则集"


@pytest.mark.asyncio
async def test_ip_no_resolve_survives_locale_override(full_default_sources):
    """locale 覆盖（en）不影响 no-resolve 追加（判定不依赖输出语言）"""
    config = await generate_surge_config(full_default_sources, locale_override="en")
    ip_lines = [l for l in _rule_section_lines(config) if "/ip/" in l or "ASN" in l]
    assert ip_lines and all(l.endswith(",no-resolve") for l in ip_lines)


@pytest.mark.asyncio
async def test_legacy_ai_after_apple_aborts_generation(db):
    """老库顺序（AI 18-23 在 Apple 12-16 之后）：生成前被顺序校验拒绝"""
    legacy_order = {"AppStore": 12, "AppleNews": 13, "Apple CDN": 14, "AppleTV": 15,
                    "Apple": 16, "GitHub": 17, "OpenAI": 18, "Claude": 19, "Gemini": 20,
                    "Bing": 21, "AI 综合": 22, "AI 兜底": 23}
    for name, sort_order in legacy_order.items():
        default = next(rs for rs in DEFAULT_RULE_SOURCES if rs["name"] == name)
        db.add(RuleSource(**{**default, "sort_order": sort_order}))
    await db.commit()
    with pytest.raises(InvalidConfiguration) as ei:
        await generate_surge_config(db)
    joined = "\n".join(ei.value.errors)
    assert "AI 兜底" in joined and "AppStore" in joined


@pytest.mark.asyncio
async def test_full_defaults_generate_for_three_targets(full_default_sources):
    """完整默认源在三目标下均可生成（顺序与引用不变量共同通过）"""
    from app.models import ConfigProfile as Profile
    for target, udp in (("mac_workstation", "scoped"),
                        ("mac_gateway", "compatible"),
                        ("ios_mobile", "compatible")):
        profile = Profile(name=f"顺序-{target}", is_default=False, locale="zh",
                          final_action="手动选择", target=target, udp_security_mode=udp)
        full_default_sources.add(profile)
    await full_default_sources.commit()
    result = await full_default_sources.execute(
        select(Profile).where(Profile.is_default == False))
    for profile in result.scalars().all():
        config = await generate_surge_config(full_default_sources, profile.id)
        assert "FINAL,手动选择,dns-failed" in config


class TestRuleOrderRouteGuard:
    """reorder / update 端点：提交前构造完整 enabled 候选序校验，违例 422 且整事务回滚"""

    @staticmethod
    async def _rule_ids(client, *names) -> dict:
        async with client._test_factory() as db:
            rows = (await db.execute(select(RuleSource))).scalars().all()
        by_name = {r.name: r for r in rows}
        return {name: by_name[name].id for name in names}

    @pytest.mark.asyncio
    async def test_reorder_breaking_invariant_422_and_rolls_back(self, client):
        ids = await self._rule_ids(client, "OpenAI", "AppStore")
        resp = await client.put("/api/configs/rules/reorder",
                                json=[{"id": ids["OpenAI"], "sort_order": 99}])
        assert resp.status_code == 422
        errors = resp.json()["detail"]["errors"]
        assert any("OpenAI" in e for e in errors)
        # 整事务回滚：数据库顺序未被破坏
        async with client._test_factory() as db:
            openai = (await db.execute(
                select(RuleSource).where(RuleSource.name == "OpenAI"))).scalar_one()
        assert openai.sort_order == 12  # 新默认：AI 块 12-17

    @pytest.mark.asyncio
    async def test_reorder_unknown_id_keeps_legacy_semantics(self, client):
        """既有语义不放宽：未知 id 静默忽略，仍按请求数量计数返回 200"""
        resp = await client.put("/api/configs/rules/reorder",
                                json=[{"id": 999999, "sort_order": 0}])
        assert resp.status_code == 200
        assert resp.json() == {"reordered": 1}

    @pytest.mark.asyncio
    async def test_reorder_compliant_change_commits(self, client):
        """不破坏不变量的 reorder 正常提交（如 GitHub 移到 40）"""
        ids = await self._rule_ids(client, "GitHub")
        resp = await client.put("/api/configs/rules/reorder",
                                json=[{"id": ids["GitHub"], "sort_order": 40}])
        assert resp.status_code == 200
        async with client._test_factory() as db:
            github = (await db.execute(
                select(RuleSource).where(RuleSource.name == "GitHub"))).scalar_one()
        assert github.sort_order == 40

    @pytest.mark.asyncio
    async def test_update_sort_order_breaking_invariant_422_and_rolls_back(self, client):
        ids = await self._rule_ids(client, "AppStore")
        resp = await client.put(f"/api/configs/rules/{ids['AppStore']}",
                                json={"sort_order": 0})
        assert resp.status_code == 422
        assert any("AppStore" in e for e in resp.json()["detail"]["errors"])
        async with client._test_factory() as db:
            appstore = (await db.execute(
                select(RuleSource).where(RuleSource.name == "AppStore"))).scalar_one()
        assert appstore.sort_order == 18  # 新默认：Apple 块 18-22

    @pytest.mark.asyncio
    async def test_update_disabling_one_side_is_allowed(self, client):
        """禁用不变量一侧（Apple 源缺席）不构成违例：正常提交"""
        ids = await self._rule_ids(client, "AppStore")
        resp = await client.put(f"/api/configs/rules/{ids['AppStore']}",
                                json={"enabled": False})
        assert resp.status_code == 200
        async with client._test_factory() as db:
            appstore = (await db.execute(
                select(RuleSource).where(RuleSource.name == "AppStore"))).scalar_one()
        assert appstore.enabled is False

    @pytest.mark.asyncio
    async def test_generate_route_422_on_bad_order(self, client):
        """顺序违例沿用 Task4 生成路由 422 通道（错误列表完整返回）"""
        ids = await self._rule_ids(client, "OpenAI")
        async with client._test_factory() as db:
            await db.execute(update(RuleSource)
                             .where(RuleSource.id == ids["OpenAI"])
                             .values(sort_order=99))
            await db.commit()
        resp = await client.get("/api/configs/generate?profile_id=1")
        assert resp.status_code == 422
        assert any("OpenAI" in e for e in resp.json()["detail"]["errors"])

    @pytest.mark.asyncio
    async def test_reorder_null_sort_order_rejected_422(self, client):
        """reorder 强类型条目：sort_order 显式 null / 非整数直接 422（Pydantic）"""
        ids = await self._rule_ids(client, "GitHub")
        resp = await client.put("/api/configs/rules/reorder",
                                json=[{"id": ids["GitHub"], "sort_order": None}])
        assert resp.status_code == 422
        resp = await client.put("/api/configs/rules/reorder",
                                json=[{"id": ids["GitHub"], "sort_order": "abc"}])
        assert resp.status_code == 422
        # 未提交：顺序保持默认
        async with client._test_factory() as db:
            github = (await db.execute(
                select(RuleSource).where(RuleSource.name == "GitHub"))).scalar_one()
        assert github.sort_order == 23

    @pytest.mark.asyncio
    async def test_update_explicit_null_non_nullable_field_422_no_commit(self, client):
        """update 区分未提供与显式 null：非空列显式 null 422 且不提交"""
        ids = await self._rule_ids(client, "GitHub")
        resp = await client.put(f"/api/configs/rules/{ids['GitHub']}",
                                json={"sort_order": None})
        assert resp.status_code == 422
        assert "sort_order" in resp.json()["detail"]
        resp = await client.put(f"/api/configs/rules/{ids['GitHub']}",
                                json={"name": None})
        assert resp.status_code == 422
        async with client._test_factory() as db:
            github = (await db.execute(
                select(RuleSource).where(RuleSource.name == "GitHub"))).scalar_one()
        assert github.sort_order == 23 and github.name == "GitHub"

    @pytest.mark.asyncio
    async def test_update_category_null_keeps_optional_semantics(self, client):
        """category 为可选列：显式 null 保留清空语义，正常提交"""
        ids = await self._rule_ids(client, "GitHub")
        resp = await client.put(f"/api/configs/rules/{ids['GitHub']}",
                                json={"category": None})
        assert resp.status_code == 200
        async with client._test_factory() as db:
            github = (await db.execute(
                select(RuleSource).where(RuleSource.name == "GitHub"))).scalar_one()
        assert github.category is None


def test_none_sort_order_fails_closed_as_invalid_configuration():
    """绕过 DB 约束构造的 None sort_order：fail closed 为 InvalidConfiguration，
    绝不让 (sort_order, id) 排序比较退化成 TypeError（生成路由 422 而非 500）"""
    bad = _builtin_stub("AI 综合", 0, 1)
    bad.sort_order = None
    with pytest.raises(InvalidConfiguration) as ei:
        validate_rule_order([bad, _builtin_stub("AppStore", 5, 2)])
    assert any("sort_order" in e for e in ei.value.errors)
