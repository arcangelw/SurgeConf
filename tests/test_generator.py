"""配置生成器单元测试"""

import pytest
from app.services.generator import (
    generate_surge_config, _build_proxy_groups, _build_rules,
    _quote, _display_name,
)
from app.models import ProxyNode, RegionGroup, ServiceGroup, RuleSource, CustomRule
from app.default_config import locale_translate, CATEGORY_NAMES, COMMENT_NAMES
from app.services.config_validation import RuleActionRef
from app.services.node_identity import stable_node_key


# ────────────────────────────────────────────
# 辅助构造
# ────────────────────────────────────────────

def _make_region(name, regex=".*", icon=None, order=0, auto=True, manual=True):
    rg = RegionGroup(name=name, filter_regex=regex, sort_order=order, enabled=True,
                     auto_enabled=auto, manual_enabled=manual)
    rg.icon_url = icon
    return rg


def _make_service(name, policies, stype="select", icon=None, order=0):
    sg = ServiceGroup(name=name, group_type=stype, policies=policies, sort_order=order, enabled=True)
    sg.icon_url = icon
    return sg


def _make_node(name, region_manual=None, sub_id=None):
    n = ProxyNode(
        name=name, node_type="trojan", server="0.0.0.0", port=443,
        config=f"{name} = trojan, 0.0.0.0, 443, password=x",
        region_manual=region_manual, enabled=True,
        stable_key=stable_node_key({"config": f"{name} = trojan, 0.0.0.0, 443, password=x"})
        if sub_id is not None else None,
    )
    n.subscription_id = sub_id
    return n


def _make_custom_rule(rule_text, position="general", enabled=True, sort_order=0):
    r = CustomRule(rule_text=rule_text, position=position, sort_order=sort_order, enabled=enabled)
    return r


def _t_zh(name):
    return locale_translate(name, "zh")


def _t_en(name):
    return locale_translate(name, "en")


# ════════════════════════════════════════════
# 一、工具函数
# ════════════════════════════════════════════

class TestQuote:
    def test_empty(self):
        assert _quote("") == '""'

    def test_no_special_chars(self):
        assert _quote("Netflix") == "Netflix"

    def test_space(self):
        assert _quote("Hong Kong") == '"Hong Kong"'

    def test_brackets(self):
        assert _quote("[Hong]") == '"[Hong]"'

    def test_node_style(self):
        assert _quote("US-01") == "US-01"


class TestDisplayName:
    def test_no_subscription(self):
        node = ProxyNode(name="HK-01", node_type="trojan", server="0.0.0.0", port=443,
                         config="HK-01 = trojan, 0.0.0.0, 443, password=x")
        assert _display_name(node, {}) == "HK-01"

    def test_with_subscription(self):
        node = ProxyNode(name="HK-01", node_type="trojan", server="0.0.0.0", port=443,
                         config="HK-01 = trojan, 0.0.0.0, 443, password=x",
                         subscription_id=5,
                         stable_key=stable_node_key({"config": "HK-01 = trojan, 0.0.0.0, 443, password=x"}))
        assert _display_name(node, {5: "机场A"}) == "(机场A) HK-01"


class TestLocaleTranslate:
    def test_zh_passthrough(self):
        assert locale_translate("香港", "zh") == "香港"

    def test_en_translation(self):
        assert locale_translate("香港", "en") == "Hong Kong"

    def test_unknown_name_passthrough(self):
        assert locale_translate("自建组", "en") == "自建组"

    def test_manual_region_suffix(self):
        assert locale_translate("香港手动", "en") == "Hong Kong Manual"

    def test_none_locale_is_zh(self):
        assert locale_translate("香港", None) == "香港"


# ════════════════════════════════════════════
# 二、_build_proxy_groups
# ════════════════════════════════════════════

class TestProxyGroupBasicStructure:
    """基础结构：全部节点、自动组、手动组、其他"""

    def test_all_nodes_group_always_present(self):
        lines = _build_proxy_groups([], [], [], _t_zh, {})
        assert any("全部节点 = select," in l and "include-all-proxies=1" in l for l in lines)

    def test_auto_region_is_url_test(self):
        regions = [_make_region("香港", "港|HK")]
        lines = _build_proxy_groups([], regions, [], _t_zh, {})
        auto = [l for l in lines if l.startswith("香港 = url-test,")]
        assert len(auto) == 1
        assert "policy-regex-filter=港|HK" in auto[0]

    def test_manual_region_is_select(self):
        regions = [_make_region("香港", "港|HK")]
        lines = _build_proxy_groups([], regions, [], _t_zh, {})
        manual = [l for l in lines if l.startswith("香港手动 = select,")]
        assert len(manual) == 1
        assert "policy-regex-filter=港|HK" in manual[0]

    def test_other_group_generated_when_auto_regions(self):
        regions = [_make_region("香港", "港|HK")]
        lines = _build_proxy_groups([], regions, [], _t_zh, {})
        assert any(l.startswith("其他 = url-test,") for l in lines)

    def test_other_group_absent_when_no_auto_regions(self):
        regions = [_make_region("香港", "港|HK", auto=False, manual=True)]
        lines = _build_proxy_groups([], regions, [], _t_zh, {})
        assert not any("其他 = url-test," in l for l in lines)

    def test_other_manual_generated_when_enabled(self):
        regions = [_make_region("香港", "港|HK")]
        lines = _build_proxy_groups([], regions, [], _t_zh, {}, other_manual_enabled=True)
        assert any(l.startswith("其他手动 = select,") for l in lines)

    def test_other_manual_not_generated_when_disabled(self):
        regions = [_make_region("香港", "港|HK")]
        lines = _build_proxy_groups([], regions, [], _t_zh, {}, other_manual_enabled=False)
        assert not any("其他手动" in l for l in lines)

    def test_other_auto_disabled_still_has_manual(self):
        regions = [_make_region("香港", "港|HK")]
        lines = _build_proxy_groups([], regions, [], _t_zh, {},
                                    other_auto_enabled=False, other_manual_enabled=True)
        assert not any("其他 = url-test," in l for l in lines)
        assert any(l.startswith("其他手动 = select,") for l in lines)


class TestAutoManualToggle:
    """auto_enabled / manual_enabled 独立开关"""

    def test_auto_region_members(self):
        regions = [_make_region("香港", "港|HK"), _make_region("美国", "美|US")]
        lines = _build_proxy_groups([], regions, [], _t_zh, {})
        auto_select = [l for l in lines if l.startswith("自动选择 = select,")][0]
        assert "香港" in auto_select
        assert "美国" in auto_select

    def test_manual_group_uses_regex_filter(self):
        regions = [_make_region("香港", "港|HK")]
        nodes = [_make_node("HK-A"), _make_node("HK-B")]
        lines = _build_proxy_groups(nodes, regions, [], _t_zh, {})
        manual = [l for l in lines if l.startswith("香港手动 = select,")][0]
        assert "include-other-group=全部节点" in manual
        assert "HK-A" not in manual
        assert "HK-B" not in manual


class TestAutoManualRegionSwitches:
    def test_both_enabled(self):
        regions = [_make_region("香港", "港|HK", auto=True, manual=True)]
        lines = _build_proxy_groups([], regions, [], _t_zh, {})
        assert any("香港 = url-test," in l for l in lines)
        assert any("香港手动 = select," in l for l in lines)

    def test_auto_only(self):
        regions = [_make_region("香港", "港|HK", auto=True, manual=False)]
        lines = _build_proxy_groups([], regions, [], _t_zh, {})
        assert any("香港 = url-test," in l for l in lines)
        assert not any("香港手动 = select," in l for l in lines)

    def test_auto_on_manual_off(self):
        regions = [_make_region("英国", "英|UK", auto=True, manual=False)]
        lines = _build_proxy_groups([], regions, [], _t_zh, {})
        assert any("英国 = url-test," in l for l in lines)
        assert not any("英国手动 = select," in l for l in lines)

    def test_both_disabled_region_absent(self):
        regions = [_make_region("韩国", "韩|KR", auto=False, manual=False)]
        lines = _build_proxy_groups([], regions, [], _t_zh, {})
        assert not any(l.startswith("韩国 =") for l in lines)
        assert not any(l.startswith("韩国手动 =") for l in lines)


class TestCompositeGroups:
    """组合策略：自动选择、代理、手动选择"""

    def test_proxy_group_members(self):
        regions = [_make_region("香港", "港|HK"), _make_region("美国", "美|US")]
        lines = _build_proxy_groups([], regions, [], _t_zh, {})
        proxy = [l for l in lines if l.startswith("代理 = select,")][0]
        assert "自动选择" in proxy
        assert "香港" in proxy and "美国" in proxy

    def test_manual_select_members(self):
        regions = [_make_region("香港", "港|HK"), _make_region("美国", "美|US")]
        lines = _build_proxy_groups([], regions, [], _t_zh, {})
        manual_select = [l for l in lines if l.startswith("手动选择 = select,")][0]
        assert "大陆直连" in manual_select
        assert "自动选择" in manual_select
        assert "香港手动" in manual_select and "美国手动" in manual_select

    def test_no_regions_no_composite_groups(self):
        lines = _build_proxy_groups([], [], [], _t_zh, {})
        assert not any(l.startswith("自动选择 =") for l in lines)
        assert not any(l.startswith("代理 =") for l in lines)
        assert not any(l.startswith("手动选择 =") for l in lines)


class TestServiceGroups:
    def test_service_with_policies(self):
        services = [_make_service("AI服务", ["自动选择", "美国"])]
        lines = _build_proxy_groups([], [], services, _t_zh, {})
        ai = [l for l in lines if l.startswith("AI服务 = select,")][0]
        assert "自动选择" in ai and "美国" in ai

    def test_service_group_type(self):
        services = [_make_service("测速", ["全部节点"], stype="url-test")]
        lines = _build_proxy_groups([], [], services, _t_zh, {})
        assert any(l.startswith("测速 = url-test,") for l in lines)

    def test_pinned_nodes_before_policies(self):
        nodes = [_make_node("US-01")]
        nodes[0].pinned_services = ["AI服务"]
        services = [_make_service("AI服务", ["自动选择"])]
        lines = _build_proxy_groups(nodes, [], services, _t_zh, {})
        ai = [l for l in lines if l.startswith("AI服务 = select,")][0]
        idx_pinned = ai.index("US-01")
        idx_policy = ai.index("自动选择")
        assert idx_pinned < idx_policy


class TestProxyGroupEnglishLocale:
    def test_auto_region_quoted(self):
        regions = [_make_region("香港", "港|HK")]
        lines = _build_proxy_groups([], regions, [], _t_en, {})
        assert any(l.startswith('"Hong Kong" = url-test,') for l in lines)

    def test_manual_region_quoted(self):
        regions = [_make_region("英国", "英|UK")]
        lines = _build_proxy_groups([], regions, [], _t_en, {})
        assert any(l.startswith('"United Kingdom Manual" = select,') for l in lines)

    def test_disabled_region_not_generated_in_english(self):
        regions = [_make_region("英国", "英|UK", auto=False, manual=False)]
        lines = _build_proxy_groups([], regions, [], _t_en, {})
        assert not any("United Kingdom = url-test" in l for l in lines)
        assert not any("United Kingdom Manual" in l for l in lines)


class TestGroupNameSinks:
    """name_sinks 登记实际生成的组名（引用校验的可用策略来源）"""

    def test_sinks_match_generated_lines(self):
        from app.services.generator import GroupNameSinks

        regions = [_make_region("香港", "港|HK"), _make_region("英国", "英|UK", auto=False, manual=False)]
        services = [_make_service("AI服务", ["自动选择"])]
        sinks = GroupNameSinks()
        lines = _build_proxy_groups([], regions, services, _t_zh, {}, name_sinks=sinks)

        emitted = set()
        for line in lines:
            if " = " in line and line.strip():
                emitted.add(line.split(" = ", 1)[0].strip().strip('"'))
        registered = sinks.region | sinks.service | sinks.combined
        assert emitted <= registered
        # 未生成项不算存在：英国 auto/manual 全关 → 不登记
        assert "英国" not in sinks.region
        assert "香港" in sinks.region and "香港手动" in sinks.region
        assert "AI服务" in sinks.service
        assert "全部节点" in sinks.combined and "自动选择" in sinks.combined

    def test_member_refs_structured_per_group(self):
        """成员引用按组结构化登记：include-other-group、组合组成员、服务组成员"""
        from app.services.generator import GroupNameSinks

        regions = [_make_region("香港", "港|HK"), _make_region("美国", "美|US")]
        nodes = [_make_node("US-01")]
        nodes[0].pinned_services = ["AI服务"]
        services = [_make_service("AI服务", ["自动选择", "美国"])]
        sinks = GroupNameSinks()

        _build_proxy_groups(nodes, regions, services, _t_zh, {}, name_sinks=sinks)

        pairs = {(ref.group, ref.member) for ref in sinks.member_refs}
        # include-other-group 引用「全部节点」
        assert ("香港", "全部节点") in pairs and ("香港手动", "全部节点") in pairs
        # 组合组成员（输出名形态）
        assert ("自动选择", "香港") in pairs and ("自动选择", "美国") in pairs
        assert ("代理", "自动选择") in pairs and ("代理", "香港手动") in pairs
        assert ("手动选择", "大陆直连") in pairs and ("手动选择", "自动选择") in pairs
        # 服务组成员：pinned 节点显示名 + 策略显示名
        assert ("AI服务", "US-01") in pairs
        assert ("AI服务", "自动选择") in pairs and ("AI服务", "美国") in pairs
        # 组行选项（url/interval/regex-filter 等）不是成员，不得出现
        assert not any(ref.member.startswith(("http", "policy-regex", "include-all"))
                       for ref in sinks.member_refs)

    def test_member_refs_include_builtin_direct(self):
        from app.services.generator import GroupNameSinks

        services = [_make_service("大陆直连", ["DIRECT"])]
        sinks = GroupNameSinks()
        _build_proxy_groups([], [], services, _t_zh, {}, name_sinks=sinks)
        assert any(ref.group == "大陆直连" and ref.member == "DIRECT"
                   for ref in sinks.member_refs)

    def test_sinks_optional(self):
        # 不传 name_sinks（单测直调）行为不变
        lines = _build_proxy_groups([], [], [], _t_zh, {})
        assert any("全部节点 = select," in l for l in lines)


# ════════════════════════════════════════════
# 三、_build_rules
# ════════════════════════════════════════════

class TestBuildRules:
    def _rules(self, sources=None, pre_rules=None, general_rules=None, final_action="兜底"):
        if sources is None:
            sources = []
        if pre_rules is None:
            pre_rules = []
        if general_rules is None:
            general_rules = []
        return _build_rules(sources, pre_rules, general_rules, _t_zh,
                            lambda k: CATEGORY_NAMES.get(k, k),
                            lambda k: COMMENT_NAMES.get(k, (k, k))[0],
                            final_action=final_action)

    def test_final_uses_final_action(self):
        """FINAL 行直通 final_action，不再硬编码兜底组"""
        lines = self._rules(final_action="大陆直连")
        final = [l for l in lines if l.startswith("FINAL,")]
        assert len(final) == 1
        assert final[0] == "FINAL,大陆直连,dns-failed"

    def test_final_action_fallback_group_still_supported(self):
        lines = self._rules(final_action="兜底")
        assert "FINAL,兜底,dns-failed" in lines

    def test_lan_rule_present(self):
        lines = self._rules()
        assert any("RULE-SET,LAN,DIRECT,no-resolve" == l for l in lines)

    def test_ip_ruleset_gets_no_resolve(self):
        rs = RuleSource(name="Domestic (ip)", url="https://ruleset.skk.moe/List/ip/domestic.conf",
                        action="大陆直连", rule_type="RULE-SET", category="mainland",
                        sort_order=0, enabled=True, ip_rules=True)
        lines = self._rules(sources=[rs])
        line = next(l for l in lines if "domestic.conf" in l)
        assert line.endswith(",no-resolve")

    def test_ip_flag_not_duplicated(self):
        """ip_rules 追加 no-resolve 恰一次，绝不重复"""
        rs = RuleSource(name="Domestic (ip)", url="https://ruleset.skk.moe/List/ip/domestic.conf",
                        action="大陆直连", rule_type="RULE-SET", category="mainland",
                        sort_order=0, enabled=True, ip_rules=True)
        lines = self._rules(sources=[rs])
        line = next(l for l in lines if "domestic.conf" in l)
        assert ",no-resolve" in line
        assert ",no-resolve,no-resolve" not in line

    def test_asn_ruleset_gets_no_resolve(self):
        rs = RuleSource(name="ASN China", url="https://example.com/ASN.China.list",
                        action="DIRECT", rule_type="RULE-SET", category="mainland",
                        sort_order=0, enabled=True, ip_rules=True)
        lines = self._rules(sources=[rs])
        line = next(l for l in lines if "ASN.China.list" in l)
        assert line.endswith(",no-resolve")

    def test_url_keyword_alone_does_not_add_no_resolve(self):
        """结构化判定：URL 含 /ip/ 但未声明 ip_rules 时不加（不按 URL 猜）"""
        rs = RuleSource(name="Domestic (ip)", url="https://ruleset.skk.moe/List/ip/domestic.conf",
                        action="大陆直连", rule_type="RULE-SET", category="mainland",
                        sort_order=0, enabled=True)
        lines = self._rules(sources=[rs])
        line = next(l for l in lines if "domestic.conf" in l)
        assert "no-resolve" not in line

    def test_ip_flag_survives_en_locale(self):
        """locale 覆盖不影响 no-resolve 追加（判定与输出语言无关）"""
        rs = RuleSource(name="ASN China", url="https://example.com/ASN.China.list",
                        action="DIRECT", rule_type="RULE-SET", category="mainland",
                        sort_order=0, enabled=True, ip_rules=True)
        lines = _build_rules([rs], [], [], _t_en,
                             lambda k: k.replace("_", " ").title(),
                             lambda k: COMMENT_NAMES.get(k, (k, k))[1],
                             final_action="兜底")
        line = next(l for l in lines if "ASN.China.list" in l)
        assert line.endswith(",no-resolve")

    def test_domain_ruleset_no_no_resolve(self):
        rs = RuleSource(name="Domestic (non_ip)", url="https://ruleset.skk.moe/List/non_ip/domestic.conf",
                        action="大陆直连", rule_type="RULE-SET", category="mainland", sort_order=0, enabled=True)
        lines = self._rules(sources=[rs])
        line = next(l for l in lines if "non_ip/domestic.conf" in l)
        assert not line.endswith(",no-resolve")
        assert "no-resolve" not in line

    def test_synthetic_mixed_ruleset_flag_drives_no_resolve(self):
        """合成 mixed 源（URL 无任何 IP 特征）：判定完全由 ip_rules 声明驱动，
        与 URL 命名无关（默认源真实远端内容的审计结果固化在声明里，测试不触网）"""
        mixed = RuleSource(name="混合列表", url="https://example.com/my-mixed.list",
                           action="DIRECT", rule_type="RULE-SET", category="intl_service",
                           sort_order=0, enabled=True, ip_rules=True)
        same_url_unflagged = RuleSource(name="同库未声明", url="https://example.com/other.list",
                                        action="DIRECT", rule_type="RULE-SET",
                                        category="intl_service",
                                        sort_order=1, enabled=True, ip_rules=False)
        lines = self._rules(sources=[mixed, same_url_unflagged])
        flagged = next(l for l in lines if "my-mixed.list" in l)
        unflagged = next(l for l in lines if "other.list" in l)
        assert flagged.endswith(",no-resolve")
        assert not unflagged.endswith(",no-resolve")

    def test_rule_action_not_translated_zh(self):
        rs = RuleSource(name="Test", url="https://example.com", action="AI服务",
                        rule_type="RULE-SET", category="ai", sort_order=0, enabled=True)
        lines = self._rules(sources=[rs])
        action_lines = [l for l in lines if l.startswith("RULE-SET,https://example.com")]
        assert len(action_lines) == 1
        assert action_lines[0].endswith(",AI服务")

    def test_pre_rules_before_rule_sources(self):
        pre = [_make_custom_rule("DOMAIN,pre.example.com,DIRECT", "pre")]
        general = [_make_custom_rule("DOMAIN,general.example.com,Global", "general")]
        rs = RuleSource(name="Test", url="https://x.com", action="DIRECT",
                        rule_type="RULE-SET", category="adblock", sort_order=0, enabled=True)
        lines = self._rules(sources=[rs], pre_rules=pre, general_rules=general)
        pre_idx = next(i for i, l in enumerate(lines) if "pre.example.com" in l)
        src_idx = next(i for i, l in enumerate(lines) if "x.com" in l)
        general_idx = next(i for i, l in enumerate(lines) if "general.example.com" in l)
        assert pre_idx < src_idx < general_idx

    def test_general_rules_after_rule_sources(self):
        general = [_make_custom_rule("DOMAIN-SUFFIX,example.com,AI服务", "general")]
        lines = self._rules(general_rules=general)
        custom = [l for l in lines if "example.com" in l]
        assert len(custom) == 1
        assert custom[0] == "DOMAIN-SUFFIX,example.com,AI服务"

    def test_custom_rule_short_passthrough(self):
        """构建器宽松输出无法解析的规则原文；拦截由校验器负责（见 test_config_validation）"""
        general = [_make_custom_rule("SHORT-RULE", "general")]
        lines = self._rules(general_rules=general)
        assert "SHORT-RULE" in lines

    def test_custom_rule_no_resolve_policy_position(self):
        """带 no-resolve 的自定义规则：策略段正确定位，modifier 原样保留在尾部"""
        general = [_make_custom_rule("IP-CIDR,192.0.2.0/24,大陆直连,no-resolve", "general")]
        lines = self._rules(general_rules=general)
        assert "IP-CIDR,192.0.2.0/24,大陆直连,no-resolve" in lines

    def test_custom_rule_en_locale_no_resolve(self):
        general = [_make_custom_rule("IP-CIDR,192.0.2.0/24,大陆直连,no-resolve", "general")]
        lines = _build_rules([], [], general, _t_en,
                             lambda k: k.replace("_", " ").title(),
                             lambda k: COMMENT_NAMES.get(k, (k, k))[1],
                             final_action="兜底")
        custom = [l for l in lines if "192.0.2.0" in l]
        assert custom == ["IP-CIDR,192.0.2.0/24,Mainland,no-resolve"]

    def test_logic_rule_reassembled_verbatim(self):
        """逻辑规则（嵌套逗号）翻译后逐字重组，不破坏括号内结构"""
        rule = "AND,((PROTOCOL,UDP), (DEST-PORT,3478)),REJECT"
        general = [_make_custom_rule(rule, "general")]
        lines = self._rules(general_rules=general)
        assert rule in lines

    def test_record_collects_structured_actions(self):
        """record 登记结构化引用：规则源 / 自定义规则 / 无法解析全覆盖"""
        rs = RuleSource(name="T", url="https://x.com", action="AI服务",
                        rule_type="RULE-SET", category="ai", sort_order=0, enabled=True)
        general = [_make_custom_rule("DOMAIN,x.com,DIRECT", "general"),
                   _make_custom_rule("SHORT", "general")]
        record: list[RuleActionRef] = []
        _build_rules([rs], [], general, _t_zh,
                     lambda k: CATEGORY_NAMES.get(k, k),
                     lambda k: COMMENT_NAMES.get(k, (k, k))[0],
                     final_action="兜底", record=record)
        by_source = {ref.source: ref.action for ref in record}
        assert by_source["规则源「T」"] == "AI服务"
        assert by_source["通用自定义规则「DOMAIN,x.com,DIRECT」"] == "DIRECT"
        assert by_source["通用自定义规则「SHORT」"] is None
        # FINAL 不在 rule_actions 登记：由校验器经 context.final_action 单独校验
        assert not any("FINAL" in s for s in by_source)

    def test_category_comment_inserted(self):
        rs = RuleSource(name="Test", url="https://x.com", action="DIRECT",
                        rule_type="RULE-SET", category="adblock", sort_order=0, enabled=True)
        lines = self._rules(sources=[rs])
        assert any("广告拦截" in l for l in lines)

    def test_empty_still_has_lan_and_final(self):
        lines = self._rules()
        assert any("LAN" in l and "DIRECT" in l for l in lines)
        assert any(l.startswith("FINAL,") for l in lines)

    def test_custom_rule_en_locale(self):
        general = [_make_custom_rule("DOMAIN-SUFFIX,example.com,AI服务", "general")]
        lines = _build_rules([], [], general, _t_en,
                             lambda k: k.replace("_", " ").title(),
                             lambda k: COMMENT_NAMES.get(k, (k, k))[1],
                             final_action="兜底")
        custom = [l for l in lines if "example.com" in l]
        assert len(custom) == 1
        assert "AI" in custom[0]

    def test_no_pre_rules_no_pre_section(self):
        lines = self._rules()
        assert not any("前置" in l for l in lines)

    def test_pre_rules_has_comment(self):
        pre = [_make_custom_rule("DOMAIN,x.com,DIRECT", "pre")]
        lines = self._rules(pre_rules=pre)
        assert any("前置" in l for l in lines)
        assert any("x.com" in l for l in lines)

    def test_disabled_rule_excluded(self):
        """enabled 过滤由 generator 查询层完成，传入的规则全部输出"""
        general = [_make_custom_rule("DOMAIN,enabled.com,Global", "general", enabled=True),
                   _make_custom_rule("DOMAIN,disabled.com,Global", "general", enabled=False)]
        lines = self._rules(general_rules=general)
        assert any("enabled.com" in l for l in lines)
        assert any("disabled.com" in l for l in lines)


# ════════════════════════════════════════════
# 四、集成测试 generate_surge_config
# ════════════════════════════════════════════

@pytest.mark.asyncio
async def test_full_config_has_all_sections(db):
    config = await generate_surge_config(db)
    for section in ["[General]", "[Proxy]", "[Proxy Group]", "[Rule]",
                    "[Host]", "[URL Rewrite]", "[Header Rewrite]", "[MITM]", "[Script]"]:
        assert section in config, f"缺少 {section}"


@pytest.mark.asyncio
async def test_rules_ordered_by_sort_order_then_id(db):
    """生成器按 (sort_order, id) 稳定排序：同 sort_order 时 id 小者先输出"""
    db.add(RuleSource(name="同序-先", url="https://example.com/tie-earlier.list",
                      action="DIRECT", rule_type="RULE-SET", category="intl_service",
                      sort_order=50, enabled=True))
    db.add(RuleSource(name="同序-后", url="https://example.com/tie-later.list",
                      action="DIRECT", rule_type="RULE-SET", category="intl_service",
                      sort_order=50, enabled=True))
    await db.commit()

    config = await generate_surge_config(db)
    rule_section = config.split("[Rule]", 1)[1].split("[Host]", 1)[0]
    earlier = next(i for i, l in enumerate(rule_section.splitlines()) if "tie-earlier" in l)
    later = next(i for i, l in enumerate(rule_section.splitlines()) if "tie-later" in l)
    assert earlier < later


@pytest.mark.asyncio
async def test_full_config_nodes_in_proxy(db_with_nodes):
    config = await generate_surge_config(db_with_nodes)
    assert "HK-01 = trojan" in config
    assert "US-01 = ss" in config
    assert "JP-01 = vmess" in config


@pytest.mark.asyncio
async def test_full_config_service_with_pinned(db_with_nodes):
    config = await generate_surge_config(db_with_nodes)
    ai = [l for l in config.split("\n") if l.startswith("AI服务 = select,")]
    assert len(ai) == 1
    assert "US-01" in ai[0]


@pytest.mark.asyncio
async def test_full_config_manual_groups_use_regex_filter(db_with_nodes):
    """手动组通过 policy-regex-filter 筛选，不逐个列举节点名"""
    config = await generate_surge_config(db_with_nodes)
    hk_manual = [l for l in config.split("\n") if l.startswith("香港手动 = select,")]
    assert len(hk_manual) == 1
    assert "policy-regex-filter" in hk_manual[0]
    assert "include-other-group" in hk_manual[0]


@pytest.mark.asyncio
async def test_full_config_english_locale(db_with_nodes):
    config = await generate_surge_config(db_with_nodes, locale_override="en")
    assert '"Hong Kong"' in config
    assert '"United States"' in config
    assert "Proxy" in config
    assert "Automatic" in config


@pytest.mark.asyncio
async def test_full_config_subscription_prefix(db):
    """有 subscription 时节点名加订阅前缀"""
    from app.models import Subscription
    db.add(Subscription(name="测试机场", url="https://sub.example.com"))
    db.add(ProxyNode(name="HK-01", node_type="trojan", server="1.1.1.1", port=443,
                     config="HK-01 = trojan, 1.1.1.1, 443, password=x",
                     enabled=True, subscription_id=1,
                     stable_key=stable_node_key({"config": "HK-01 = trojan, 1.1.1.1, 443, password=x"})))
    await db.commit()

    config = await generate_surge_config(db)
    assert "(测试机场) HK-01" in config


# ════════════════════════════════════════════
# 五、General 段目标过滤（平台能力矩阵）
# ════════════════════════════════════════════

def _general_section(config: str) -> str:
    start = config.index("[General]")
    end = config.index("[Proxy]")
    return config[start:end]


async def _add_target_profile(db, name, target, udp_mode, general):
    from app.models import ConfigProfile
    profile = ConfigProfile(name=name, is_default=False, locale="zh",
                            final_action="手动选择", general=general,
                            target=target, udp_security_mode=udp_mode)
    db.add(profile)
    await db.commit()
    await db.refresh(profile)
    return profile


@pytest.mark.asyncio
async def test_general_workstation_keeps_mac_keys_drops_ios(db):
    """默认 workstation 档案：Mac 专属键保留，iOS/gateway 专属键被过滤"""
    general = _general_section(await generate_surge_config(db))
    assert "read-etc-hosts = true" in general
    assert "use-local-host-item-for-proxy" in general
    assert "compatibility-mode" not in general
    assert "auto-suspend" not in general
    assert "gateway-restricted-to-lan" not in general
    # skip-proxy 是生成期内部键（GENERATOR_ONLY）：输出保留
    assert any(l.startswith("skip-proxy = ") for l in general.splitlines())

@pytest.mark.asyncio
async def test_general_ios_profile_drops_mac_keys(db):
    """iOS 档案：Mac 专属键全部剔除，强制 compatibility-mode=1 与 auto-suspend=true"""
    profile = await _add_target_profile(
        db, "iPhone 档", "ios_mobile", "compatible",
        {"http-api": "synthetic-admin@127.0.0.1:6172",
         "read-etc-hosts": True, "wifi-assist": True})
    general = _general_section(await generate_surge_config(db, profile_id=profile.id))
    assert "http-api" not in general
    assert "read-etc-hosts" not in general
    assert "http-listen" not in general
    assert "compatibility-mode = 1" in general
    assert "auto-suspend = true" in general
    assert "wifi-assist = true" in general


@pytest.mark.asyncio
async def test_general_gateway_profile_forces_lan_restriction(db):
    """网关档案：保留 Mac 专属键并强制 gateway-restricted-to-lan=true"""
    profile = await _add_target_profile(
        db, "网关档", "mac_gateway", "scoped",
        {"gateway-restricted-to-lan": False, "read-etc-hosts": True})
    general = _general_section(await generate_surge_config(db, profile_id=profile.id))
    assert "gateway-restricted-to-lan = true" in general
    assert "read-etc-hosts = true" in general
    assert "compatibility-mode" not in general


@pytest.mark.asyncio
async def test_general_forces_safe_udp_values_all_targets(db):
    """危险 block-quic / UDP 行为值一律被强制为安全值"""
    profile = await _add_target_profile(
        db, "危险值档", "mac_workstation", "scoped",
        {"block-quic": "proxy", "udp-policy-not-supported-behaviour": "DROP"})
    general = _general_section(await generate_surge_config(db, profile_id=profile.id))
    assert "block-quic = per-policy" in general
    assert "udp-policy-not-supported-behaviour = REJECT" in general
    assert "block-quic = proxy" not in general


@pytest.mark.asyncio
async def test_general_skips_none_and_empty_values(db):
    """档案值中的 None 与空字符串不写入配置"""
    profile = await _add_target_profile(
        db, "空值守卫档", "mac_workstation", "scoped",
        {"http-api": None, "internet-test-url": "", "loglevel": "info"})
    general = _general_section(await generate_surge_config(db, profile_id=profile.id))
    assert not any(l.startswith("http-api ") for l in general.splitlines())
    assert not any(l.startswith("internet-test-url ") for l in general.splitlines())
    assert "loglevel = info" in general


# ════════════════════════════════════════════
# 六、档案解析与数据作用域（全局 + 当前档案）
# ════════════════════════════════════════════

import pytest_asyncio
from types import SimpleNamespace
from sqlalchemy import select, event
from app.models import ConfigProfile, HostMapping
from app.services.profile_resolution import (
    DefaultProfileMissing, ProfileNotFound, profile_scope, resolve_profile,
)


@pytest_asyncio.fixture
async def two_profiles(db):
    """默认档案(first) + 第二档案(second)，两者不共用任何定制数据"""
    first = (await db.execute(
        select(ConfigProfile).where(ConfigProfile.is_default == True)
    )).scalar_one()
    second = ConfigProfile(
        name="第二档案", target="mac_workstation", udp_security_mode="scoped",
        locale="zh",
    )
    db.add(second)
    await db.commit()
    return SimpleNamespace(first=first, second=second)


def _add_rule(db, profile_id, rule_text, position="general", sort_order=0):
    db.add(CustomRule(profile_id=profile_id, rule_text=rule_text,
                      position=position, sort_order=sort_order, enabled=True))


def _add_host(db, profile_id, domain, ip, skip_proxy=True, sort_order=0):
    db.add(HostMapping(profile_id=profile_id, domain=domain,
                       target=f"server:{ip}", skip_proxy=skip_proxy,
                       enabled=True, sort_order=sort_order))


@pytest.mark.asyncio
async def test_explicit_missing_profile_never_falls_back(db):
    """显式 id 不存在：抛 ProfileNotFound，绝不回退默认档案"""
    with pytest.raises(ProfileNotFound):
        await generate_surge_config(db, profile_id=99999)


@pytest.mark.asyncio
async def test_explicit_zero_profile_is_not_treated_as_omitted(db):
    """显式 0 是显式 id：按不存在处理，不落入默认档案分支"""
    with pytest.raises(ProfileNotFound):
        await generate_surge_config(db, profile_id=0)


@pytest.mark.asyncio
async def test_omitted_profile_without_default_raises(db):
    """省略 id 且无默认档案：DefaultProfileMissing，而不是静默空档案生成"""
    default = (await db.execute(
        select(ConfigProfile).where(ConfigProfile.is_default == True)
    )).scalar_one()
    await db.delete(default)
    await db.commit()
    with pytest.raises(DefaultProfileMissing):
        await generate_surge_config(db)


@pytest.mark.asyncio
async def test_rules_and_hosts_are_global_plus_current_only(db, two_profiles):
    """规则与 Host 作用域 = 全局(NULL) + 当前档案；其他档案一律不可见"""
    _add_rule(db, None, "DOMAIN,global.example,DIRECT")
    _add_rule(db, two_profiles.first.id, "DOMAIN,work.example,DIRECT")
    _add_rule(db, two_profiles.second.id, "DOMAIN,private.example,DIRECT")
    _add_host(db, None, "global-host.example", "203.0.113.10")
    _add_host(db, two_profiles.first.id, "work-host.example", "203.0.113.11")
    _add_host(db, two_profiles.second.id, "private-host.example", "203.0.113.12")
    await db.commit()

    config = await generate_surge_config(db, two_profiles.first.id)
    assert "global.example" in config and "work.example" in config
    assert "global-host.example" in config and "work-host.example" in config
    assert "private.example" not in config and "private-host.example" not in config

    # 省略 id 解析默认档案(=first)：作用域与显式 first 一致
    config_default = await generate_surge_config(db)
    assert "work.example" in config_default and "work-host.example" in config_default
    assert "private.example" not in config_default and "private-host.example" not in config_default

    # 换到 second：当前档案条目互换，全局条目始终在场
    config_second = await generate_surge_config(db, two_profiles.second.id)
    assert "private.example" in config_second and "private-host.example" in config_second
    assert "global.example" in config_second and "global-host.example" in config_second
    assert "work.example" not in config_second and "work-host.example" not in config_second


@pytest.mark.asyncio
async def test_rule_and_host_scope_applied_in_sql_not_python(db, two_profiles):
    """作用域必须下推到 SQL：custom_rules / host_mappings 语句带 profile 谓词"""
    bind = db.get_bind()
    engine = getattr(bind, "sync_engine", bind)  # AsyncEngine 取底层；sync Engine 直接用
    captured: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        captured.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        await generate_surge_config(db, two_profiles.first.id)
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    rule_stmts = [s for s in captured if "FROM custom_rules" in s]
    host_stmts = [s for s in captured if "FROM host_mappings" in s]
    assert rule_stmts, "生成器应从 custom_rules 表查询规则"
    assert host_stmts, "生成器应从 host_mappings 表查询 Host"
    for stmt in rule_stmts + host_stmts:
        assert "profile_id IS NULL" in stmt
        assert "profile_id =" in stmt


def test_profile_scope_predicate_shape():
    """profile_scope 产出 SQL 谓词：IS NULL OR = 当前档案"""
    from sqlalchemy.dialects import sqlite
    compiled = str(profile_scope(HostMapping, 7).compile(dialect=sqlite.dialect()))
    assert "host_mappings.profile_id IS NULL" in compiled
    assert "host_mappings.profile_id = ?" in compiled


@pytest.mark.asyncio
async def test_locale_target_and_general_never_cross_profiles(db, two_profiles):
    """locale / target / General / MITM 只来自本次解析的档案实例"""
    from app.default_config import DEFAULT_MITM
    two_profiles.second.locale = "en"
    two_profiles.second.target = "ios_mobile"
    two_profiles.second.udp_security_mode = "compatible"
    two_profiles.second.general = {"loglevel": "debug"}
    two_profiles.second.mitm = {"hostname": "second.example"}
    await db.commit()

    second_config = await generate_surge_config(db, two_profiles.second.id)
    second_general = _general_section(second_config)
    # second：英文分组名 + iOS 强制键 + 自己的 General/MITM 值
    assert "Automatic" in second_config and "自动选择" not in second_config
    assert "compatibility-mode = 1" in second_general
    assert "auto-suspend = true" in second_general
    assert "loglevel = debug" in second_general
    assert "hostname = second.example" in second_config

    first_config = await generate_surge_config(db, two_profiles.first.id)
    first_general = _general_section(first_config)
    # first：中文 + workstation（无 iOS 强制键）+ 默认值，不串 second 的任何定制
    assert "自动选择" in first_config and "Automatic" not in first_config
    assert "compatibility-mode" not in first_general
    assert "loglevel = debug" not in first_general
    assert "second.example" not in first_config
    assert DEFAULT_MITM["hostname"] in first_config


@pytest.mark.asyncio
async def test_compatible_keeps_user_stun_without_managed_block(db):
    """compatible 档案：不注入托管 STUN/UDP/443 阻断；用户自建 STUN 照常输出"""
    compatible = await _add_target_profile(db, "兼容档", "mac_workstation", "compatible", {})
    db.add(CustomRule(profile_id=compatible.id, position="pre", sort_order=50,
                      enabled=True, rule_text="PROTOCOL,STUN,REJECT", comment="user-owned"))
    await db.commit()

    config = await generate_surge_config(db, compatible.id)

    # 用户自建 STUN 规则照常输出（明确配置保留，不受模式影响）
    assert "PROTOCOL,STUN,REJECT" in config
    # compatible 不注入四条托管 STUN 与全局 UDP/443 阻断
    assert "DOMAIN-KEYWORD,stun,REJECT" not in config
    assert "DEST-PORT,3478" not in config
    assert "DEST-PORT,19302" not in config
    assert "DEST-PORT,443" not in config
    assert "AND,((PROTOCOL,UDP), (DEST-PORT,443)),REJECT" not in config


# ════════════════════════════════════════════
# 七、托管 UDP 安全规则注入（Task 4）
# ════════════════════════════════════════════

import os
import subprocess

from sqlalchemy import text

from app.default_config import MANAGED_STUN_RULES
from app.models import ConfigProfile, UdpException
from app.services.config_validation import InvalidConfiguration
from app.services.udp_rules import build_udp_rules, managed_udp_line_action

SURGE_CLI = "/Applications/Surge.app/Contents/Applications/surge-cli"


def _add_udp_exception(db, profile_id, match_type, match_value,
                       enabled=True, sort_order=0):
    db.add(UdpException(profile_id=profile_id, match_type=match_type,
                        match_value=match_value, enabled=enabled,
                        sort_order=sort_order))


def _rule_lines(config: str) -> list[str]:
    section = config.split("[Rule]", 1)[1].split("[Host]", 1)[0]
    return [l for l in section.splitlines() if l.strip() and not l.startswith("#")]


@pytest.mark.asyncio
async def test_scoped_workstation_injects_exceptions_then_blocks(db):
    """scoped workstation：例外 DIRECT 行 + 四条阻断，位于 pre 之后、规则源之前"""
    profile = await _add_target_profile(db, "注入档", "mac_workstation", "scoped", {})
    _add_rule(db, profile.id, "DOMAIN,pre.example.com,DIRECT", position="pre")
    _add_rule(db, profile.id, "DOMAIN-SUFFIX,general.example.com,Global", position="general")
    _add_udp_exception(db, profile.id, "process", "ExampleVideo", sort_order=10)
    await db.commit()

    config = await generate_surge_config(db, profile.id)
    lines = _rule_lines(config)

    pre_idx = next(i for i, l in enumerate(lines) if "pre.example.com" in l)
    exc_idx = next(i for i, l in enumerate(lines)
                   if l == "AND,((PROCESS-NAME,ExampleVideo),(PROTOCOL,UDP)),DIRECT")
    block_idx = next(i for i, l in enumerate(lines) if l == MANAGED_STUN_RULES[1])
    src_idx = next(i for i, l in enumerate(lines) if l.startswith("RULE-SET,"))
    general_idx = next(i for i, l in enumerate(lines) if "general.example.com" in l)
    final_idx = next(i for i, l in enumerate(lines) if l.startswith("FINAL,"))
    # 完整顺序不变量：pre < exceptions < managed blocks < rule sources < FINAL
    assert pre_idx < exc_idx < block_idx < src_idx < general_idx < final_idx
    # 例外行后紧跟四条阻断的精确集合
    assert lines[exc_idx + 1:exc_idx + 5] == list(MANAGED_STUN_RULES)
    assert len([l for l in lines if l.startswith("FINAL,")]) == 1


@pytest.mark.asyncio
async def test_scoped_gateway_injects_mac_exception(db):
    """scoped gateway：MAC 例外行按规范大写冒号输出 + 四条阻断"""
    profile = await _add_target_profile(db, "网关注入档", "mac_gateway", "scoped", {})
    _add_udp_exception(db, profile.id, "mac_address", "02:00:00:00:00:01")
    await db.commit()

    lines = _rule_lines(await generate_surge_config(db, profile.id))
    assert "AND,((MAC-ADDRESS,02:00:00:00:00:01),(PROTOCOL,UDP)),DIRECT" in lines
    assert all(l in lines for l in MANAGED_STUN_RULES)


@pytest.mark.asyncio
async def test_strict_ignores_exceptions_and_scoped_restores(db):
    """strict：enabled 例外保留在库但不输出；切回 scoped 后恢复（不删除）"""
    profile = await _add_target_profile(db, "切换档", "mac_workstation", "scoped", {})
    _add_udp_exception(db, profile.id, "process", "ExampleVideo")
    await db.commit()

    profile.udp_security_mode = "strict"
    await db.commit()
    strict_lines = _rule_lines(await generate_surge_config(db, profile.id))
    assert not any("ExampleVideo" in l for l in strict_lines)
    assert all(l in strict_lines for l in MANAGED_STUN_RULES)

    profile.udp_security_mode = "scoped"
    await db.commit()
    scoped_lines = _rule_lines(await generate_surge_config(db, profile.id))
    assert "AND,((PROCESS-NAME,ExampleVideo),(PROTOCOL,UDP)),DIRECT" in scoped_lines



@pytest.mark.asyncio
async def test_rule_section_comments_localized_never_literal_keys(db):
    """产物段注释全部本地化：任何 COMMENT_NAMES 键都不以 literal key 输出

    守卫键表完整性：缺键（如 general_rules/udp_managed 被误删）会让
    tcomment 回退输出键名本身，此测试即红。
    """
    profile = await _add_target_profile(db, "注释守卫档", "mac_workstation", "scoped", {})
    _add_rule(db, profile.id, "DOMAIN,pre.example.com,DIRECT", position="pre")
    _add_rule(db, profile.id, "DOMAIN-SUFFIX,general.example.com,Global", position="general")
    _add_udp_exception(db, profile.id, "process", "ExampleVideo")
    await db.commit()

    zh = await generate_surge_config(db, profile.id)
    assert "# >> 前置自定义规则" in zh
    assert "# >> 托管 UDP 安全规则" in zh
    assert "# >> 通用自定义规则" in zh
    assert "# >> 兜底规则" in zh
    for key in ("pre_rules", "udp_managed", "general_rules", "lan", "final_rule"):
        assert f"# >> {key}" not in zh

    en = await generate_surge_config(db, profile.id, locale_override="en")
    assert "# >> Pre Custom Rules" in en
    assert "# >> Managed UDP Security" in en
    assert "# >> General Custom Rules" in en

@pytest.mark.asyncio
async def test_disabled_exception_not_queried_into_output(db):
    """disabled 例外不进入产物（生成查询只取 enabled）"""
    profile = await _add_target_profile(db, "禁用档", "mac_workstation", "scoped", {})
    _add_udp_exception(db, profile.id, "process", "ExampleVideo", enabled=False)
    _add_udp_exception(db, profile.id, "process", "ActiveApp", enabled=True)
    await db.commit()

    lines = _rule_lines(await generate_surge_config(db, profile.id))
    assert not any("ExampleVideo" in l for l in lines)
    assert "AND,((PROCESS-NAME,ActiveApp),(PROTOCOL,UDP)),DIRECT" in lines


@pytest.mark.asyncio
async def test_udp_exceptions_scoped_to_current_profile(db, two_profiles):
    """UDP 例外只属于所属档案：跨档案互不可见（无全局作用域）"""
    first, second = two_profiles.first, two_profiles.second
    _add_udp_exception(db, first.id, "process", "FirstApp")
    _add_udp_exception(db, second.id, "process", "SecondApp")
    await db.commit()

    first_lines = _rule_lines(await generate_surge_config(db, first.id))
    assert any("FirstApp" in l for l in first_lines)
    assert not any("SecondApp" in l for l in first_lines)

    second_lines = _rule_lines(await generate_surge_config(db, second.id))
    assert any("SecondApp" in l for l in second_lines)
    assert not any("FirstApp" in l for l in second_lines)


@pytest.mark.asyncio
async def test_bypassed_illegal_exception_fails_generation_closed(db):
    """绕库直写非法例外值（无视写入端校验）：生成 fail closed 且错误不回显值"""
    profile = await _add_target_profile(db, "绕库档", "mac_workstation", "scoped", {})
    secret = "SECRET-INJECT,VALUE"
    await db.execute(text("PRAGMA ignore_check_constraints = ON"))
    _add_udp_exception(db, profile.id, "process", secret)
    await db.commit()
    await db.execute(text("PRAGMA ignore_check_constraints = OFF"))

    with pytest.raises(InvalidConfiguration) as excinfo:
        await generate_surge_config(db, profile.id)
    joined = "\n".join(excinfo.value.errors)
    assert "SECRET-INJECT" not in joined


@pytest.mark.asyncio
async def test_bypassed_ios_scoped_profile_fails_generation_closed(db):
    """绕库写入 ios_mobile+scoped（无视 CHECK）：生成时被模式矩阵拒绝"""
    await db.execute(text("PRAGMA ignore_check_constraints = ON"))
    db.add(ConfigProfile(name="绕库iOS档", is_default=False, locale="zh",
                         final_action="手动选择", target="ios_mobile",
                         udp_security_mode="scoped"))
    await db.commit()
    await db.execute(text("PRAGMA ignore_check_constraints = OFF"))
    bypassed = (await db.execute(
        select(ConfigProfile).where(ConfigProfile.name == "绕库iOS档"))).scalar_one()

    with pytest.raises(InvalidConfiguration):
        await generate_surge_config(db, bypassed.id)


@pytest.mark.asyncio
async def test_user_udp443_lookalike_preserved_in_scoped(db):
    """用户已存的 UDP/443 lookalike 自定义规则按 Task 3 语义保留，不做模糊删除"""
    profile = await _add_target_profile(db, "lookalike档", "mac_workstation", "scoped", {})
    lookalike = "AND,((PROTOCOL,UDP),(DEST-PORT,443)),REJECT"
    _add_rule(db, profile.id, lookalike, position="pre")
    await db.commit()

    lines = _rule_lines(await generate_surge_config(db, profile.id))
    assert lookalike in lines
    # 托管注入永不包含 UDP/443 阻断
    assert sum(1 for l in lines if "DEST-PORT,443" in l) == 1


@pytest.mark.asyncio
async def test_ios_strict_managed_rules_carry_no_process_or_mac(db):
    """iOS strict：托管行自身无 PROCESS/MAC 构造，[Rule] 段完整（唯一 FINAL）"""
    profile = await _add_target_profile(db, "iOS严格档", "ios_mobile", "strict", {})
    await db.commit()

    config = await generate_surge_config(db, profile.id)
    section = config.split("[Rule]", 1)[1].split("[Host]", 1)[0]
    lines = _rule_lines(config)
    assert "PROCESS-NAME" not in section and "PROCESS-PATH" not in section
    assert "MAC-ADDRESS" not in section
    assert all(l in lines for l in MANAGED_STUN_RULES)
    assert [l for l in lines if l.startswith("FINAL,")] == ["FINAL,手动选择,dns-failed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("target", "mode"), [
    ("mac_workstation", "strict"), ("mac_workstation", "scoped"),
    ("mac_gateway", "strict"), ("mac_gateway", "scoped"),
    ("ios_mobile", "strict"),
])
async def test_general_forces_safe_udp_values_strict_scoped(db, target, mode):
    """strict/scoped 全目标：General 仍强制 udp-policy-not-supported-behaviour=REJECT
    与 block-quic=per-policy（托管规则注入不改变 General 安全底线）"""
    profile = await _add_target_profile(
        db, f"安全值档-{target}-{mode}", target, mode,
        {"block-quic": "proxy", "udp-policy-not-supported-behaviour": "DROP"})
    general = _general_section(await generate_surge_config(db, profile.id))
    assert "block-quic = per-policy" in general
    assert "udp-policy-not-supported-behaviour = REJECT" in general


def test_managed_udp_lines_recorded_in_rule_actions():
    """托管行动作经 managed_udp_line_action 登记进 record：纳入引用完整性校验"""
    record: list[RuleActionRef] = []
    udp_lines = build_udp_rules("mac_workstation", "scoped", [
        UdpException(id=1, profile_id=1, match_type="process",
                     match_value="ExampleVideo", enabled=True, sort_order=0)])
    assert [managed_udp_line_action(l) for l in udp_lines] == \
        ["DIRECT", "REJECT", "REJECT", "REJECT", "REJECT"]
    _build_rules([], [], [], _t_zh, lambda k: CATEGORY_NAMES.get(k, k),
                 lambda k: COMMENT_NAMES.get(k, (k, k))[0],
                 final_action="兜底", udp_lines=udp_lines, record=record)
    managed = [r for r in record if r.source == "托管 UDP 安全规则"]
    assert [r.action for r in managed] == ["DIRECT", "REJECT", "REJECT", "REJECT", "REJECT"]
    assert len(managed) == len(udp_lines)


@pytest.mark.asyncio
@pytest.mark.skipif(not os.access(SURGE_CLI, os.X_OK), reason="本机无 surge-cli")
@pytest.mark.parametrize(("target", "match_type", "values"), [
    ("mac_workstation", "process", ["ExampleVideo", "Google Chrome"]),
    ("mac_gateway", "mac_address", ["02:00:00:00:00:01"]),
])
async def test_scoped_configs_pass_surge_cli_check(db_with_nodes, tmp_path,
                                                   target, match_type, values):
    """两个 Mac scoped 产物（含例外行与四条阻断）通过 Surge CLI --check 语法校验

    conftest 的 vmess/ss 桩（username=x / encrypt=x 非真实凭据形态）不被
    真实 CLI 接受，与本任务规则无关：CLI 冒烟只保留合法 trojan 桩节点。
    """
    from sqlalchemy import update
    await db_with_nodes.execute(
        update(ProxyNode).where(ProxyNode.node_type != "trojan").values(enabled=False))
    profile = await _add_target_profile(db_with_nodes, f"CLI档-{target}", target,
                                        "scoped", {})
    for i, value in enumerate(values):
        _add_udp_exception(db_with_nodes, profile.id, match_type, value, sort_order=i)
    await db_with_nodes.commit()

    config = await generate_surge_config(db_with_nodes, profile.id)
    artifact = tmp_path / f"{target}-scoped.conf"
    artifact.write_text(config, encoding="utf-8")
    result = subprocess.run([SURGE_CLI, "--check", str(artifact)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
