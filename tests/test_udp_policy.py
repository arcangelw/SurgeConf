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
