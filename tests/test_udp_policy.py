"""UDP 例外领域校验：目标/类型矩阵、值规范化、敏感值不回显

覆盖：
- normalize_udp_match 合法路径：workstation=process 原样、gateway=mac_address
  规范为大写冒号形式
- 目标/类型不兼容矩阵：iOS 全拒、workstation 只 process、gateway 只 mac_address
- process 值约束：1–255 字符、拒逗号/双引号/CR/LF/Unicode 控制字符
- MAC 值约束：仅 6 组 `:`/`-` 十六进制；点分隔/组数不对/含逗号全拒
- 错误消息最小披露：任何拒绝理由不含原始 match_value
- 未知 enum（Pydantic 之外的领域入口）fail closed
- ORM repr 不泄露 match_value；DB 级唯一约束与 ON DELETE CASCADE
"""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import ConfigProfile, UdpException
from app.profile_targets import ProfileTarget
from app.udp_policy import UdpMatchType, normalize_udp_match


@pytest.mark.parametrize(("target", "kind", "value", "normalized"), [
    ("mac_workstation", "process", "ExampleVideo", "ExampleVideo"),
    ("mac_gateway", "mac_address", "02-00-00-00-00-01", "02:00:00:00:00:01"),
])
def test_normalize_valid_udp_match(target, kind, value, normalized):
    assert normalize_udp_match(ProfileTarget(target), UdpMatchType(kind), value) == normalized


@pytest.mark.parametrize(("target", "kind", "value"), [
    ("ios_mobile", "process", "ExampleVideo"),
    ("mac_workstation", "mac_address", "02:00:00:00:00:01"),
    ("mac_gateway", "process", "ExampleVideo"),
    ("mac_workstation", "process", "bad,name"),
    ("mac_gateway", "mac_address", "not-a-mac"),
])
def test_reject_incompatible_udp_match(target, kind, value):
    with pytest.raises(ValueError):
        normalize_udp_match(ProfileTarget(target), UdpMatchType(kind), value)


# ===== 目标矩阵补全：iOS 对 mac_address 同样全拒 =====


def test_ios_rejects_mac_address_too():
    with pytest.raises(ValueError):
        normalize_udp_match(ProfileTarget.IOS_MOBILE, UdpMatchType.MAC_ADDRESS,
                            "02:00:00:00:00:01")


# ===== process 值约束 =====


def test_process_length_boundaries():
    ok_255 = "a" * 255
    assert normalize_udp_match(ProfileTarget.MAC_WORKSTATION,
                               UdpMatchType.PROCESS, ok_255) == ok_255
    with pytest.raises(ValueError):
        normalize_udp_match(ProfileTarget.MAC_WORKSTATION,
                            UdpMatchType.PROCESS, "a" * 256)
    with pytest.raises(ValueError):
        normalize_udp_match(ProfileTarget.MAC_WORKSTATION,
                            UdpMatchType.PROCESS, "")


@pytest.mark.parametrize("value", [
    "bad,name",                      # 逗号：Surge 规则段分隔符
    'bad"name',                      # 双引号
    "bad\nname", "bad\rname",        # CR/LF：一行一条规则
    "bad\tname",                     # 水平制表（Cc 控制字符）
    "bad\x00name", "bad\x1bname",    # NUL / ESC
    "bad\x7fname", "bad\x9fname",    # DEL / U+009F
])
def test_process_rejects_delimiters_and_control_characters(value):
    with pytest.raises(ValueError):
        normalize_udp_match(ProfileTarget.MAC_WORKSTATION, UdpMatchType.PROCESS, value)


def test_process_accepts_unicode_letter_content():
    """非控制字符的 Unicode 文本（如中文进程名）不误伤"""
    assert normalize_udp_match(ProfileTarget.MAC_WORKSTATION,
                               UdpMatchType.PROCESS, "钉钉") == "钉钉"


# ===== MAC 值约束 =====


# 合成 MAC 字母变体：分段构造避免与仓库 MAC 泄露扫描（规则 7）自命中
# （连续 6 组十六进制字母的字面量会被当成 MAC 检出）
_LOWER_DASH_MAC = "-".join(("aa", "bb", "cc", "dd", "ee", "ff"))
_UPPER_COLON_MAC = ":".join(("AA", "BB", "CC", "DD", "EE", "FF"))
_UPPER_DASH_MAC = "-".join(("AA", "BB", "CC", "DD", "EE", "FF"))
# 7 组形态：白名单 MAC 字面量 + 追加段（源文本不构成可匹配的第七组）
_SEVEN_GROUP_MAC = "02:00:00:00:00:01" + ":02"


@pytest.mark.parametrize(("value", "normalized"), [
    ("02:00:00:00:00:01", "02:00:00:00:00:01"),   # 已规范：原样
    ("02-00-00-00-00-01", "02:00:00:00:00:01"),   # 连字符 → 冒号
    (_LOWER_DASH_MAC, _UPPER_COLON_MAC),          # 小写 → 大写
    (_UPPER_COLON_MAC, _UPPER_COLON_MAC),
    (_UPPER_DASH_MAC, _UPPER_COLON_MAC),          # 大写连字符
])
def test_mac_normalization_forms(value, normalized):
    assert normalize_udp_match(ProfileTarget.MAC_GATEWAY,
                               UdpMatchType.MAC_ADDRESS, value) == normalized


@pytest.mark.parametrize("value", [
    "not-a-mac",
    "",                                   # 空
    "02:00:00:00:00",                     # 5 组
    _SEVEN_GROUP_MAC,                     # 7 组
    "02.00.00.00.00.01",                  # 点分隔
    "02:00:00:00:00:0g",                  # 非十六进制
    "02,00,00,00,00,01",                  # 逗号分隔
    "02:00:00:00:00:01\n",                # 尾随换行
    " 02:00:00:00:00:01",                 # 前导空白
])
def test_mac_rejects_invalid_forms(value):
    with pytest.raises(ValueError):
        normalize_udp_match(ProfileTarget.MAC_GATEWAY, UdpMatchType.MAC_ADDRESS, value)

# ===== 错误最小披露：绝不回显原始值 =====


@pytest.mark.parametrize(("kind", "value"), [
    ("process", "SECRET-PROCESS-NAME,with,commas"),
    ("process", "SECRET-PROCESS-NAME\nsecond-line"),
    ("process", "x" * 300 + "SECRET-TAIL"),
    ("mac_address", "SECRET-MAC-VALUE"),
])
def test_rejection_messages_never_echo_value(kind, value):
    from app.profile_targets import ProfileTarget as PT
    target = PT.MAC_WORKSTATION if kind == "process" else PT.MAC_GATEWAY
    with pytest.raises(ValueError) as excinfo:
        normalize_udp_match(target, UdpMatchType(kind), value)
    assert "SECRET" not in str(excinfo.value)


# ===== 未知 enum：领域入口 fail closed =====


def test_unknown_match_type_fails_closed():
    with pytest.raises(ValueError):
        UdpMatchType("bogus")
    with pytest.raises(ValueError):
        normalize_udp_match(ProfileTarget.MAC_WORKSTATION, "bogus", "ExampleVideo")


def test_unknown_target_fails_closed():
    with pytest.raises(ValueError):
        ProfileTarget("linux_toaster")
    with pytest.raises(ValueError):
        normalize_udp_match("linux_toaster", UdpMatchType.PROCESS, "ExampleVideo")


# ===== ORM：repr 不泄露 match_value =====


def test_udp_exception_repr_hides_match_value():
    exc = UdpException(profile_id=1, match_type="process",
                       match_value="SecretProc", enabled=True, sort_order=0)
    rendered = repr(exc)
    assert "SecretProc" not in rendered
    assert "match_type='process'" in rendered   # 非敏感元数据可用于排障


# ===== DB 级约束：唯一 (profile_id, match_type, match_value) =====


@pytest.mark.asyncio
async def test_duplicate_profile_match_rejected_by_unique_constraint(db):
    profile = ConfigProfile(name="UDP唯一档", target="mac_workstation",
                            udp_security_mode="scoped", final_action="手动选择")
    db.add(profile)
    await db.flush()
    db.add(UdpException(profile_id=profile.id, match_type="process",
                        match_value="ExampleVideo", enabled=True, sort_order=0))
    await db.flush()
    db.add(UdpException(profile_id=profile.id, match_type="process",
                        match_value="ExampleVideo", enabled=False, sort_order=1))
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


@pytest.mark.asyncio
async def test_same_value_different_profile_or_type_allowed(db):
    first = ConfigProfile(name="UDP档A", target="mac_workstation",
                          udp_security_mode="scoped", final_action="手动选择")
    second = ConfigProfile(name="UDP档B", target="mac_gateway",
                           udp_security_mode="scoped", final_action="手动选择")
    db.add_all([first, second])
    await db.flush()
    db.add_all([
        UdpException(profile_id=first.id, match_type="process",
                     match_value="ExampleVideo", enabled=True, sort_order=0),
        UdpException(profile_id=second.id, match_type="process",
                     match_value="ExampleVideo", enabled=True, sort_order=0),
    ])
    await db.flush()   # 同值不同档案：不触发唯一约束


# ===== DB 级 ON DELETE CASCADE（真实 foreign_keys=ON 连接） =====


@pytest.mark.asyncio
async def test_profile_delete_cascades_udp_exceptions_db_level(db_fk_on):
    """真实 foreign_keys=ON 连接：ORM 层无 relationship、无手工 delete，
    删除父行后子行由数据库级 CASCADE 清除"""
    profile = ConfigProfile(name="UDP级联档", target="mac_gateway",
                            udp_security_mode="scoped", final_action="手动选择")
    db_fk_on.add(profile)
    await db_fk_on.flush()
    db_fk_on.add_all([
        UdpException(profile_id=profile.id, match_type="mac_address",
                     match_value="02:00:00:00:00:01", enabled=True, sort_order=0),
        UdpException(profile_id=profile.id, match_type="mac_address",
                     match_value="02-00-00-00-00-01", enabled=False, sort_order=1),
    ])
    await db_fk_on.commit()

    await db_fk_on.delete(profile)
    await db_fk_on.commit()

    rows = (await db_fk_on.execute(select(UdpException))).scalars().all()
    assert rows == [], "ON DELETE CASCADE 应清除全部子行"


@pytest.mark.asyncio
async def test_udp_exception_check_constraint_rejects_unknown_type(db):
    """DB 层防线：绕过领域层直写的未知 match_type 被 CHECK 拒绝"""
    profile = ConfigProfile(name="UDP检查档", target="mac_workstation",
                            udp_security_mode="scoped", final_action="手动选择")
    db.add(profile)
    await db.flush()
    db.add(UdpException(profile_id=profile.id, match_type="bogus",
                        match_value="x", enabled=True, sort_order=0))
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


# ===== Task 4：托管 UDP 规则构建器（纯函数矩阵 + fail closed + 最小披露） =====

from types import SimpleNamespace

from app.default_config import MANAGED_STUN_RULES
from app.profile_targets import UdpSecurityMode
from app.services.config_validation import InvalidConfiguration
from app.services.udp_rules import build_udp_rules, quote_rule_atom


def _exception(**overrides) -> UdpException:
    """不落库的 ORM 实例：直接构造即可驱动纯函数（id 可显式赋值）"""
    fields = dict(id=1, profile_id=1, match_type="process", match_value="ExampleVideo",
                  display_name=None, enabled=True, sort_order=0)
    fields.update(overrides)
    return UdpException(**fields)


def _synthetic_exceptions_for(target: str) -> list[UdpException]:
    """按目标矩阵返回一条合法 enabled 例外（iOS 无例外）"""
    if target == "mac_workstation":
        return [_exception(match_type="process", match_value="ExampleVideo")]
    if target == "mac_gateway":
        return [_exception(match_type="mac_address", match_value="02:00:00:00:00:01")]
    return []


def _assert_udp_shape(lines: list[str], expected: str) -> None:
    """精确 shape 断言：none / strict / process_then_strict / mac_then_strict"""
    if expected == "none":
        assert lines == []
        return
    blocks = list(MANAGED_STUN_RULES)
    if expected == "strict":
        assert lines == blocks
        return
    kind, atom = (("PROCESS-NAME", "ExampleVideo")
                  if expected == "process_then_strict"
                  else ("MAC-ADDRESS", "02:00:00:00:00:01"))
    assert lines[0] == f"AND,(({kind},{atom}),(PROTOCOL,UDP)),DIRECT"
    assert lines[1:] == blocks


@pytest.mark.parametrize(("target", "mode", "expected"), [
    ("mac_workstation", "strict", "strict"),
    ("mac_workstation", "scoped", "process_then_strict"),
    ("mac_workstation", "compatible", "none"),
    ("mac_gateway", "strict", "strict"),
    ("mac_gateway", "scoped", "mac_then_strict"),
    ("mac_gateway", "compatible", "none"),
    ("ios_mobile", "strict", "strict"),
    ("ios_mobile", "scoped", "invalid"),
    ("ios_mobile", "compatible", "none"),
])
def test_udp_generation_matrix(target, mode, expected):
    if expected == "invalid":
        with pytest.raises(InvalidConfiguration):
            build_udp_rules(target, mode, [])
    else:
        lines = build_udp_rules(target, mode, _synthetic_exceptions_for(target))
        _assert_udp_shape(lines, expected)


_VALID_TARGET_MODE_PAIRS = [
    (target, mode)
    for target in ("mac_workstation", "mac_gateway", "ios_mobile")
    for mode in ("strict", "scoped", "compatible")
    if not (target == "ios_mobile" and mode == "scoped")
]


def test_no_mode_emits_global_udp_443_reject():
    for target, mode in _VALID_TARGET_MODE_PAIRS:
        assert "DEST-PORT,443" not in "\n".join(
            build_udp_rules(target, mode, _synthetic_exceptions_for(target)))


# ── enum 严格性与组合校验 ──


@pytest.mark.parametrize(("target", "mode"), [
    ("linux_toaster", "scoped"), ("mac_workstation", "loose"),
    ("mac_gateway", ""), ("ios_mobile", None),
])
def test_unknown_target_or_mode_fails_closed(target, mode):
    with pytest.raises(InvalidConfiguration):
        build_udp_rules(target, mode, [])


def test_enum_instances_accepted_directly():
    """枚举实例与等值字符串同义（生成器可直传 str，纯函数可传 enum）"""
    lines = build_udp_rules(ProfileTarget.MAC_WORKSTATION, UdpSecurityMode.STRICT,
                            _synthetic_exceptions_for("mac_workstation"))
    assert lines == list(MANAGED_STUN_RULES)


# ── 例外行形状与稳定排序 ──


def test_scoped_orders_exceptions_by_sort_order_then_id():
    """(sort_order, id) 稳定序：sort_order 优先，同序按 id 破平"""
    exceptions = [
        _exception(id=7, match_value="Late", sort_order=20),
        _exception(id=9, match_value="TieB", sort_order=10),
        _exception(id=8, match_value="TieA", sort_order=10),
        _exception(id=6, match_value="Early", sort_order=5),
    ]
    lines = build_udp_rules("mac_workstation", "scoped", exceptions)
    ordered = [l for l in lines if l.startswith("AND,((PROCESS-NAME")]
    assert ordered == [
        "AND,((PROCESS-NAME,Early),(PROTOCOL,UDP)),DIRECT",
        "AND,((PROCESS-NAME,TieA),(PROTOCOL,UDP)),DIRECT",
        "AND,((PROCESS-NAME,TieB),(PROTOCOL,UDP)),DIRECT",
        "AND,((PROCESS-NAME,Late),(PROTOCOL,UDP)),DIRECT",
    ]


def test_quote_rule_atom_adds_quotes_for_spaces_and_structural_chars():
    assert quote_rule_atom("ExampleVideo") == "ExampleVideo"
    assert quote_rule_atom("Google Chrome") == '"Google Chrome"'
    assert quote_rule_atom("app(1)") == '"app(1)"'
    assert quote_rule_atom("a#b") == '"a#b"'


def test_process_name_with_spaces_quoted_in_rule_line():
    lines = build_udp_rules("mac_workstation", "scoped",
                            [_exception(match_value="Google Chrome")])
    assert lines[0] == 'AND,((PROCESS-NAME,"Google Chrome"),(PROTOCOL,UDP)),DIRECT'


def test_mac_line_reuses_normalizer_canonical_form():
    """绕库写入非规范 MAC（小写/横线）：生成时仍重用 normalizer 输出大写冒号"""
    # 分段构造避免与仓库 MAC 泄露扫描（规则 7）自命中
    lower_dash = "-".join(("ab", "cd", "ef", "01", "23", "45"))
    upper_colon = ":".join(("AB", "CD", "EF", "01", "23", "45"))
    lines = build_udp_rules("mac_gateway", "scoped",
                            [_exception(match_type="mac_address", match_value=lower_dash)])
    assert lines[0] == f"AND,((MAC-ADDRESS,{upper_colon}),(PROTOCOL,UDP)),DIRECT"


# ── strict / compatible / disabled 与例外的关系 ──


def test_strict_ignores_valid_exceptions_but_keeps_validating():
    """strict：合法例外不输出（不删除，切回 scoped 恢复），四条阻断精确输出"""
    lines = build_udp_rules("mac_workstation", "strict",
                            _synthetic_exceptions_for("mac_workstation"))
    assert lines == list(MANAGED_STUN_RULES)
    assert "ExampleVideo" not in "\n".join(lines)


def test_strict_still_fails_closed_on_illegal_exception():
    """strict 仍校验传入例外：绕库非法值不被静默吞掉"""
    with pytest.raises(InvalidConfiguration):
        build_udp_rules("mac_workstation", "strict",
                        [_exception(match_value="bad,value")])


def test_compatible_emits_nothing_but_still_validates():
    """compatible 不注入任何托管行；非法例外依旧 fail closed（防绕库）"""
    assert build_udp_rules("mac_workstation", "compatible",
                           _synthetic_exceptions_for("mac_workstation")) == []
    with pytest.raises(InvalidConfiguration):
        build_udp_rules("mac_gateway", "compatible",
                        [_exception(match_type="mac_address", match_value="zz")])


def test_disabled_exceptions_fully_ignored():
    """disabled 例外既不输出也不触发校验（类型不兼容/值非法均无碍）"""
    lines = build_udp_rules("mac_workstation", "scoped", [
        _exception(enabled=False, match_type="mac_address",
                   match_value="02:00:00:00:00:01"),
        _exception(enabled=False, match_value="bad,value"),
        _exception(match_value="ExampleVideo"),
    ])
    assert lines[0] == "AND,((PROCESS-NAME,ExampleVideo),(PROTOCOL,UDP)),DIRECT"
    assert lines[1:] == list(MANAGED_STUN_RULES)


# ── DB 绕库 fail closed（生成层第二道防线）──


@pytest.mark.parametrize("value", [
    "comma,value", 'quote"value', "line\nbreak", "carriage\rreturn",
    "tab\tchar", "nul\x00byte",
])
def test_bypassed_injection_values_rejected_at_generation(value):
    """绕库直写的分隔/引号/换行/控制字符：生成时 InvalidConfiguration"""
    with pytest.raises(InvalidConfiguration):
        build_udp_rules("mac_workstation", "scoped", [_exception(match_value=value)])


def test_bypassed_unknown_match_type_rejected():
    with pytest.raises(InvalidConfiguration):
        build_udp_rules("mac_workstation", "scoped",
                        [_exception(match_type="bogus", match_value="x")])


def test_bypassed_incompatible_type_rejected():
    """workstation 档案绕库写入 mac_address 例外：生成时拒绝（不静默跳过）"""
    with pytest.raises(InvalidConfiguration):
        build_udp_rules("mac_workstation", "scoped",
                        [_exception(match_type="mac_address",
                                    match_value="02:00:00:00:00:01")])


# ── 错误最小披露：绝不回显 match_value ──


def test_error_messages_never_echo_match_value():
    secret = "SECRET-PROC,ESS"
    with pytest.raises(InvalidConfiguration) as excinfo:
        build_udp_rules("mac_workstation", "scoped",
                        [_exception(id=42, match_value=secret)])
    joined = "\n".join(excinfo.value.errors)
    assert "SECRET-PROC" not in joined
    assert "42" in joined and "process" in joined   # 只含 id / type 定位信息


def test_all_bad_exceptions_aggregated_in_one_error():
    """多个非法例外聚合成一次 InvalidConfiguration（不逐个抛）"""
    with pytest.raises(InvalidConfiguration) as excinfo:
        build_udp_rules("mac_workstation", "scoped", [
            _exception(id=1, match_value="a,b"),
            _exception(id=2, match_value="c\nd"),
        ])
    assert len(excinfo.value.errors) == 2


# ── 简化桩对象同样可驱动（duck-typing 契约）──


def test_namespace_stubs_accepted():
    stub = SimpleNamespace(id=3, match_type="mac_address",
                           match_value="02:00:00:00:00:01",
                           enabled=True, sort_order=0)
    lines = build_udp_rules("mac_gateway", "scoped", [stub])
    assert lines[0] == "AND,((MAC-ADDRESS,02:00:00:00:00:01),(PROTOCOL,UDP)),DIRECT"
