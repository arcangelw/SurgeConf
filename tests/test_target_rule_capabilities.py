"""规则目标能力：iOS 产物过滤 Mac 进程规则 + FINAL 生成器专属（fail closed）

覆盖：
- 结构化检测（括号感知）：PROCESS-* 前缀族与 MAC-ADDRESS 在顶层、AND/OR/NOT
  嵌套内全部命中；payload 子串（如域名 process-name.example.com）不误伤
- iOS 生成四维：{顶层, 嵌套} × {全局规则, 当前档案规则} 的 Mac 构造一律
  不输出；合法 iOS 规则（含合法逻辑规则）保留；Mac 目标不受影响
- 被过滤的规则不进入 rule_actions：其 policy 引用不存在的策略时 iOS 生成
  仍成功（Mac 生成则 422）
- FINAL 为生成器专属：任何 pre/general 自定义 FINAL（含绕过保存端直接
  写库的存量）令生成 fail closed（InvalidConfiguration → 路由 422）；
  最终 [Rule] 段恰一条 FINAL，且策略来自 ConfigProfile.final_action
"""

import pytest
from sqlalchemy import select

from app.models import ConfigProfile, CustomRule
from app.services.config_validation import (
    InvalidConfiguration,
    is_final_rule,
    rule_excluded_for_target,
    rule_uses_mac_only_constructs,
)
from app.services.generator import generate_surge_config


def _add_profile(db, name, target, mode):
    profile = ConfigProfile(name=name, is_default=False, locale="zh",
                            final_action="手动选择",
                            target=target, udp_security_mode=mode)
    db.add(profile)
    return profile


def _add_rule(db, profile_id, rule_text, position="general", sort_order=0):
    db.add(CustomRule(profile_id=profile_id, rule_text=rule_text,
                      position=position, sort_order=sort_order, enabled=True))


def _rule_section(config: str) -> str:
    start = config.index("[Rule]")
    end = config.index("[Host]")
    return config[start:end]


# ===== 结构化检测单元（括号感知，substring 不误伤 payload） =====

@pytest.mark.parametrize("rule_text", [
    "PROCESS-NAME,Telegram,DIRECT",
    "PROCESS-PATH,/usr/bin/ssh,Proxy",
    "PROCESS-NAME,git,REJECT",
    "MAC-ADDRESS,02:00:00:00:00:01,DIRECT",
    # 逻辑规则顶层与嵌套
    "AND,((PROCESS-NAME,Telegram),(DOMAIN-SUFFIX,t.me)),DIRECT",
    "OR,((PROCESS-PATH,/usr/bin/curl),(IP-CIDR,10.0.0.0/8,no-resolve)),Proxy",
    "NOT,((MAC-ADDRESS,02:00:00:00:00:01)),REJECT",
    # 嵌套深度 > 1
    "AND,((NOT,((PROCESS-NAME,x))),(DOMAIN-SUFFIX,y.com)),DIRECT",
])
def test_mac_only_constructs_detected(rule_text):
    assert rule_uses_mac_only_constructs(rule_text) is True


@pytest.mark.parametrize("rule_text", [
    "DOMAIN-SUFFIX,process-name.example.com,DIRECT",       # payload 子串
    "DOMAIN,mac-address.io,DIRECT",                         # payload 含关键字
    "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve",
    # 合法逻辑规则：子规则均无 Mac 构造
    "AND,((DOMAIN-SUFFIX,a.com),(IP-CIDR,192.168.0.0/16,no-resolve)),DIRECT",
    "NOT,((GEOIP,CN)),DIRECT",
    "OR,((DOMAIN-SUFFIX,process.com),(DOMAIN-KEYWORD,github)),Proxy",
])
def test_non_mac_rules_not_flagged(rule_text):
    assert rule_uses_mac_only_constructs(rule_text) is False


def test_exclusion_matrix_by_target():
    mac_rule = "PROCESS-NAME,Telegram,DIRECT"
    ios_rule = "DOMAIN-SUFFIX,example.com,DIRECT"
    assert rule_excluded_for_target(mac_rule, "ios_mobile") is True
    assert rule_excluded_for_target(mac_rule, "mac_workstation") is False
    assert rule_excluded_for_target(mac_rule, "mac_gateway") is False
    assert rule_excluded_for_target(ios_rule, "ios_mobile") is False


# ===== FINAL 判定单元 =====

@pytest.mark.parametrize("rule_text", [
    "FINAL,DIRECT",
    "FINAL,手动选择",
    "FINAL,自动选择,dns-failed",
])
def test_final_rule_detected(rule_text):
    assert is_final_rule(rule_text) is True


@pytest.mark.parametrize("rule_text", [
    "DOMAIN-SUFFIX,final.com,DIRECT",
    "AND,((DOMAIN,final.io)),DIRECT",
    "PROCESS-NAME,FINAL,DIRECT",   # FINAL 只是 payload
])
def test_final_rule_not_flagged(rule_text):
    assert is_final_rule(rule_text) is False


# ===== iOS 生成四维：{顶层, 嵌套} × {全局, 当前档案} =====

@pytest.mark.asyncio
@pytest.mark.parametrize("mac_rule", [
    "PROCESS-NAME,Telegram,DIRECT",                                    # 顶层
    "AND,((PROCESS-NAME,Telegram),(DOMAIN-SUFFIX,t.me)),DIRECT",       # 嵌套
    "MAC-ADDRESS,02:00:00:00:00:01,DIRECT",                            # 顶层
    "NOT,((PROCESS-PATH,/usr/bin/ssh)),Proxy",                         # 嵌套
])
@pytest.mark.parametrize("scope", ["global", "profile"])
async def test_ios_generation_drops_mac_rules_all_dimensions(db, mac_rule, scope):
    """iOS 产物不输出任何 Mac 进程/MAC 规则：顶层与嵌套、全局与档案作用域全覆盖"""
    ios = _add_profile(db, "iOS四维档", "ios_mobile", "compatible")
    pid = None if scope == "global" else ios.id
    _add_rule(db, pid, mac_rule)
    _add_rule(db, pid, "DOMAIN-SUFFIX,keep.example,DIRECT")  # 合法 iOS 规则
    await db.commit()

    config = await generate_surge_config(db, ios.id)
    rules = _rule_section(config)
    assert "PROCESS-NAME" not in rules and "PROCESS-PATH" not in rules
    assert "MAC-ADDRESS" not in rules
    assert "/usr/bin/ssh" not in rules and "02:00:00:00:00:01" not in rules
    assert "keep.example" in rules, "合法 iOS 规则必须保留"
    assert rules.count("FINAL,") == 1 and "FINAL," in rules


@pytest.mark.asyncio
async def test_mac_targets_keep_process_rules(db):
    """Mac 两目标不受 iOS 过滤影响：进程规则原样输出"""
    for target, mode in (("mac_workstation", "scoped"), ("mac_gateway", "scoped")):
        profile = _add_profile(db, f"Mac保留档-{target}", target, mode)
        _add_rule(db, profile.id, "PROCESS-NAME,Telegram,DIRECT")
        _add_rule(db, None, "AND,((MAC-ADDRESS,02:00:00:00:00:01),(DOMAIN,x.io)),REJECT")
        await db.commit()
        config = await generate_surge_config(db, profile.id)
        rules = _rule_section(config)
        assert "PROCESS-NAME,Telegram,DIRECT" in rules
        assert "MAC-ADDRESS,02:00:00:00:00:01" in rules


@pytest.mark.asyncio
async def test_filtered_rules_never_enter_rule_actions(db):
    """被 iOS 过滤的规则不进入引用校验：其 policy 引用不存在策略也不报错

    （同一规则在 Mac 目标下会因无效引用 422——对照组证明规则本身确实非法）
    """
    ios = _add_profile(db, "iOS引用档", "ios_mobile", "compatible")
    _add_rule(db, ios.id, "PROCESS-NAME,Telegram,不存在的策略组")
    await db.commit()

    config = await generate_surge_config(db, ios.id)   # iOS：过滤在先，不校验引用
    assert "PROCESS-NAME" not in _rule_section(config)

    mac = _add_profile(db, "Mac对照档", "mac_workstation", "scoped")
    _add_rule(db, mac.id, "PROCESS-NAME,Telegram,不存在的策略组")
    await db.commit()
    with pytest.raises(InvalidConfiguration) as exc_info:
        await generate_surge_config(db, mac.id)
    assert any("不存在的策略组" in e for e in exc_info.value.errors)


@pytest.mark.asyncio
@pytest.mark.parametrize("final_rule", [
    "FINAL,DIRECT",            # 内置 action
    "FINAL,手动选择",           # policy action
])
@pytest.mark.parametrize("position", ["pre", "general"])
async def test_custom_final_fails_generation_closed(db, final_rule, position):
    """绕过保存端直接写库的自定义 FINAL：生成 fail closed（四组合全覆盖）"""
    profile = _add_profile(db, f"FINAL档-{position}-{final_rule}", "mac_workstation", "scoped")
    _add_rule(db, profile.id, final_rule, position=position)
    await db.commit()
    with pytest.raises(InvalidConfiguration) as exc_info:
        await generate_surge_config(db, profile.id)
    assert any("FINAL" in e for e in exc_info.value.errors)


@pytest.mark.asyncio
async def test_final_line_comes_from_profile_final_action_exactly_once(db):
    """最终 [Rule] 恰一条 FINAL；策略直通档案 final_action，不被自定义规则覆盖"""
    profile = _add_profile(db, "唯一FINAL档", "ios_mobile", "compatible")
    profile.final_action = "自动选择"
    # 干扰项：与 FINAL 策略同名也无法借道（类型段非 FINAL 的规则不受影响）
    _add_rule(db, profile.id, "DOMAIN-SUFFIX,final.example,DIRECT")
    await db.commit()

    config = await generate_surge_config(db, profile.id)
    rules = _rule_section(config)
    final_lines = [l for l in rules.splitlines() if l.startswith("FINAL,")]
    assert final_lines == ["FINAL,自动选择,dns-failed"]
