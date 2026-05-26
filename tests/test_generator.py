"""配置生成器单元测试"""

import pytest
from app.services.generator import (
    generate_surge_config, _build_proxy_groups, _build_rules,
    _quote, _display_name,
)
from app.models import ProxyNode, RegionGroup, ServiceGroup, RuleSource, CustomRule
from app.default_config import locale_translate, CATEGORY_NAMES, COMMENT_NAMES


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

    def test_left_bracket(self):
        assert _quote("[ark]") == '"[ark]"'

    def test_right_bracket(self):
        assert _quote("test]") == '"test]"'

    def test_normal_parentheses(self):
        assert _quote("(Sub) Node") == '"(Sub) Node"'

    def test_plain_name(self):
        assert _quote("US-01") == "US-01"


class TestDisplayName:
    def test_no_subscription(self):
        node = _make_node("HK-01")
        assert _display_name(node, {}) == "HK-01"

    def test_with_subscription(self):
        node = _make_node("HK-01", sub_id=5)
        assert _display_name(node, {5: "机场A"}) == "(机场A) HK-01"

    def test_subscription_id_not_in_map(self):
        node = _make_node("HK-01", sub_id=99)
        assert _display_name(node, {5: "机场A"}) == "HK-01"


class TestLocaleTranslate:
    def test_zh_passthrough(self):
        assert locale_translate("香港", "zh") == "香港"

    def test_en_known_region(self):
        assert locale_translate("香港", "en") == "Hong Kong"
        assert locale_translate("美国", "en") == "United States"

    def test_en_known_service(self):
        assert locale_translate("AI服务", "en") == "AI"
        assert locale_translate("兜底", "en") == "Fallback"

    def test_en_unknown_passthrough(self):
        assert locale_translate("Netflix", "en") == "Netflix"

    def test_manual_group_zh(self):
        assert locale_translate("香港手动", "zh") == "香港手动"

    def test_manual_group_en_known_region(self):
        assert locale_translate("香港手动", "en") == "Hong Kong Manual"
        assert locale_translate("美国手动", "en") == "United States Manual"

    def test_manual_group_en_unknown_region(self):
        assert locale_translate("法国手动", "en") == "法国 Manual"

    def test_none_locale(self):
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
        assert "include-other-group=全部节点" in auto[0]

    def test_manual_region_is_select(self):
        regions = [_make_region("香港", "港|HK")]
        lines = _build_proxy_groups([], regions, [], _t_zh, {})
        manual = [l for l in lines if l.startswith("香港手动 = select,")]
        assert len(manual) == 1
        assert "policy-regex-filter=港|HK" in manual[0]

    def test_other_group_present_when_auto_regions_exist(self):
        regions = [_make_region("香港", "港|HK")]
        lines = _build_proxy_groups([], regions, [], _t_zh, {})
        assert any(l.startswith("其他 = url-test,") for l in lines)

    def test_other_group_absent_when_no_auto_regions(self):
        regions = [_make_region("香港", "港|HK", auto=False, manual=True)]
        lines = _build_proxy_groups([], regions, [], _t_zh, {})
        assert not any("其他 = url-test," in l for l in lines)

    def test_manual_group_uses_include_other_group(self):
        """手动组通过 include-other-group 引用全部节点，不逐个列举节点名"""
        regions = [_make_region("香港", "港|HK")]
        nodes = [_make_node("HK-A"), _make_node("HK-B")]
        lines = _build_proxy_groups(nodes, regions, [], _t_zh, {})
        manual = [l for l in lines if l.startswith("香港手动 = select,")][0]
        assert "include-other-group=全部节点" in manual
        assert "HK-A" not in manual
        assert "HK-B" not in manual


class TestAutoManualToggle:
    """auto_enabled / manual_enabled 独立开关"""

    def test_both_enabled_generates_both(self):
        regions = [_make_region("香港", "港|HK", auto=True, manual=True)]
        lines = _build_proxy_groups([], regions, [], _t_zh, {})
        assert any("香港 = url-test," in l for l in lines)
        assert any("香港手动 = select," in l for l in lines)

    def test_auto_off_manual_on(self):
        regions = [_make_region("香港", "港|HK", auto=False, manual=True)]
        lines = _build_proxy_groups([], regions, [], _t_zh, {})
        assert not any("香港 = url-test," in l for l in lines)
        assert any("香港手动 = select," in l for l in lines)

    def test_auto_on_manual_off(self):
        regions = [_make_region("英国", "英|UK", auto=True, manual=False)]
        lines = _build_proxy_groups([], regions, [], _t_zh, {})
        assert any("英国 = url-test," in l for l in lines)
        assert not any("英国手动 = select," in l for l in lines)

    def test_both_disabled_region_absent(self):
        regions = [_make_region("韩国", "韩|KR", auto=False, manual=False)]
        lines = _build_proxy_groups([], regions, [], _t_zh, {})
        assert not any("韩国" in l for l in lines)
        assert not any("韩国手动" in l for l in lines)

    def test_both_disabled_no_other_filter_for_it(self):
        """其他组的过滤只包含活跃地区的正则"""
        regions = [
            _make_region("香港", "港|HK", auto=True, manual=True),
            _make_region("英国", "英|UK", auto=False, manual=False),
        ]
        lines = _build_proxy_groups([], regions, [], _t_zh, {})
        other = [l for l in lines if l.startswith("其他 = url-test,")][0]
        assert "英|UK" not in other


class TestCompositeGroups:
    """组合策略：自动选择、代理、手动选择"""

    def test_auto_select_has_only_auto_regions(self):
        regions = [
            _make_region("香港", "港|HK"),
            _make_region("美国", "美|US"),
        ]
        lines = _build_proxy_groups([], regions, [], _t_zh, {})
        auto_select = [l for l in lines if l.startswith("自动选择 = select,")][0]
        assert "香港" in auto_select
        assert "美国" in auto_select
        assert "其他" in auto_select
        assert "手动" not in auto_select

    def test_proxy_has_auto_and_manual(self):
        regions = [
            _make_region("香港", "港|HK"),
            _make_region("美国", "美|US"),
        ]
        lines = _build_proxy_groups([], regions, [], _t_zh, {})
        proxy = [l for l in lines if l.startswith("代理 = select,")][0]
        assert "自动选择" in proxy
        assert "香港" in proxy
        assert "香港手动" in proxy
        assert "美国" in proxy
        assert "美国手动" in proxy

    def test_manual_select_has_manual_regions(self):
        regions = [
            _make_region("香港", "港|HK"),
            _make_region("美国", "美|US"),
        ]
        lines = _build_proxy_groups([], regions, [], _t_zh, {})
        ms = [l for l in lines if l.startswith("手动选择 = select,")][0]
        assert "大陆直连" in ms
        assert "自动选择" in ms
        assert "香港手动" in ms
        assert "美国手动" in ms

    def test_manual_off_excluded_from_composite(self):
        """manual_enabled=False 的地区不出现在手动选择中"""
        regions = [
            _make_region("香港", "港|HK", auto=True, manual=True),
            _make_region("英国", "英|UK", auto=True, manual=False),
        ]
        lines = _build_proxy_groups([], regions, [], _t_zh, {})
        proxy = [l for l in lines if l.startswith("代理 = select,")][0]
        ms = [l for l in lines if l.startswith("手动选择 = select,")][0]
        assert "香港手动" in proxy
        assert "英国手动" not in proxy
        assert "香港手动" in ms
        assert "英国手动" not in ms

    def test_auto_off_excluded_from_auto_select(self):
        """auto_enabled=False 的地区不出现在自动选择中"""
        regions = [
            _make_region("香港", "港|HK", auto=True, manual=True),
            _make_region("英国", "英|UK", auto=False, manual=True),
        ]
        lines = _build_proxy_groups([], regions, [], _t_zh, {})
        auto_select = [l for l in lines if l.startswith("自动选择 = select,")][0]
        assert "香港" in auto_select
        assert "英国" not in auto_select

    def test_no_regions_no_composite_groups(self):
        """没有活跃地区时不生成组合策略"""
        lines = _build_proxy_groups([], [], [], _t_zh, {})
        assert not any(l.startswith("自动选择 =") for l in lines)
        assert not any(l.startswith("代理 =") for l in lines)
        assert not any(l.startswith("手动选择 =") for l in lines)


class TestServiceGroups:
    def test_service_with_policies(self):
        services = [_make_service("AI服务", ["自动选择", "美国"])]
        lines = _build_proxy_groups([], [], services, _t_zh, {})
        ai = [l for l in lines if l.startswith("AI服务 = select,")][0]
        assert "自动选择" in ai
        assert "美国" in ai

    def test_service_with_pinned_node(self):
        services = [_make_service("AI服务", ["自动选择"])]
        nodes = [_make_node("US-Pinned")]
        nodes[0].pinned_services = ["AI服务"]
        lines = _build_proxy_groups(nodes, [], services, _t_zh, {})
        ai = [l for l in lines if l.startswith("AI服务 = select,")][0]
        assert "US-Pinned" in ai
        assert "自动选择" in ai

    def test_pinned_node_with_subscription_prefix(self):
        services = [_make_service("AI服务", ["自动选择"])]
        nodes = [_make_node("US-01", sub_id=5)]
        nodes[0].pinned_services = ["AI服务"]
        sub_map = {5: "机场A"}
        lines = _build_proxy_groups(nodes, [], services, _t_zh, sub_map)
        ai = [l for l in lines if l.startswith("AI服务 = select,")][0]
        assert "(机场A) US-01" in ai

    def test_pinned_node_before_policies(self):
        """钉选节点排在 policies 前面"""
        services = [_make_service("AI服务", ["自动选择"])]
        nodes = [_make_node("Pinned-Node")]
        nodes[0].pinned_services = ["AI服务"]
        lines = _build_proxy_groups(nodes, [], services, _t_zh, {})
        ai = [l for l in lines if l.startswith("AI服务 = select,")][0]
        idx_pinned = ai.index("Pinned-Node")
        idx_policy = ai.index("自动选择")
        assert idx_pinned < idx_policy


class TestProxyGroupEnglishLocale:
    def test_auto_region_quoted(self):
        regions = [_make_region("香港", "港|HK")]
        lines = _build_proxy_groups([], regions, [], _t_en, {})
        assert any('"Hong Kong" = url-test,' in l for l in lines)

    def test_manual_region_quoted(self):
        regions = [_make_region("香港", "港|HK"), _make_region("美国", "美|US")]
        lines = _build_proxy_groups([], regions, [], _t_en, {})
        manual = [l for l in lines if 'Manual" = select,' in l]
        assert len(manual) == 2
        assert any('"Hong Kong Manual"' in l for l in manual)
        assert any('"United States Manual"' in l for l in manual)

    def test_composite_groups_translated(self):
        regions = [_make_region("香港", "港|HK")]
        lines = _build_proxy_groups([], regions, [], _t_en, {})
        assert any(l.startswith("Automatic = select,") for l in lines)
        assert any(l.startswith("Proxy = select,") for l in lines)
        assert any(l.startswith("NoAuto = select,") for l in lines)

    def test_service_policy_translated(self):
        services = [_make_service("AI服务", ["美国手动"])]
        lines = _build_proxy_groups([], [], services, _t_en, {})
        ai = [l for l in lines if l.startswith("AI = select,")][0]
        assert "United States Manual" in ai

    def test_manual_off_not_translated(self):
        regions = [_make_region("英国", "英|UK", auto=True, manual=False)]
        lines = _build_proxy_groups([], regions, [], _t_en, {})
        assert not any("United Kingdom Manual" in l for l in lines)


# ════════════════════════════════════════════
# 三、_build_rules
# ════════════════════════════════════════════

class TestBuildRules:
    def _rules(self, sources=None, pre_rules=None, general_rules=None):
        if sources is None:
            sources = []
        if pre_rules is None:
            pre_rules = []
        if general_rules is None:
            general_rules = []
        return _build_rules(sources, pre_rules, general_rules, _t_zh,
                            lambda k: CATEGORY_NAMES.get(k, k),
                            lambda k: COMMENT_NAMES.get(k, (k, k))[0])

    def test_final_always_fallback(self):
        lines = self._rules()
        final = [l for l in lines if l.startswith("FINAL,")]
        assert len(final) == 1
        assert "兜底" in final[0]
        assert "dns-failed" in final[0]

    def test_lan_rule_present(self):
        lines = self._rules()
        assert any("RULE-SET,LAN,DIRECT" == l for l in lines)

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
        general = [_make_custom_rule("SHORT-RULE", "general")]
        lines = self._rules(general_rules=general)
        assert "SHORT-RULE" in lines

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
                             lambda k: COMMENT_NAMES.get(k, (k, k))[1])
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
                     enabled=True, subscription_id=1))
    await db.commit()

    config = await generate_surge_config(db)
    assert "(测试机场) HK-01" in config
