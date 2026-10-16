"""UDP 例外匹配的领域类型与规范化（目标感知矩阵 + 最小披露错误）。

目标矩阵（唯一真源）：
- mac_workstation：仅 process（按进程名放行 UDP）
- mac_gateway：仅 mac_address（按内网设备 MAC 放行 UDP）
- ios_mobile：无任何 UDP 例外（iOS Surge 不支持进程/MAC 匹配）

所有拒绝消息只描述格式/组合规则，绝不回显原始 match_value（敏感值）。
"""

import re
import unicodedata
from enum import StrEnum

from app.profile_targets import ProfileTarget


class UdpMatchType(StrEnum):
    PROCESS = "process"
    MAC_ADDRESS = "mac_address"


# 目标 → 允许的例外类型（完整显式矩阵；iOS 一律为空）
ALLOWED_UDP_MATCH_TYPES: dict[ProfileTarget, frozenset[UdpMatchType]] = {
    ProfileTarget.MAC_WORKSTATION: frozenset({UdpMatchType.PROCESS}),
    ProfileTarget.MAC_GATEWAY: frozenset({UdpMatchType.MAC_ADDRESS}),
    ProfileTarget.IOS_MOBILE: frozenset(),
}


def udp_match_types_allowed_for(target: ProfileTarget) -> frozenset[UdpMatchType]:
    """目标允许的 UDP 例外类型集合（iOS 为空集 = 全拒）"""
    return ALLOWED_UDP_MATCH_TYPES[ProfileTarget(target)]


# process 值长度上限（与 udp_exceptions.match_value 列宽一致）
PROCESS_VALUE_MAX_LENGTH = 255

# MAC 地址：6 组 2 位十六进制，`:` 或 `-` 分隔。
# fullmatch 且不写 `$` 锚点：`$` 会放过尾随单个换行
_MAC_ADDRESS_PATTERN = re.compile(r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}")


def _contains_control_character(value: str) -> bool:
    """是否含 Unicode Cc 控制字符（含 CR/LF/TAB/NUL/DEL 及 C1 区）"""
    return any(unicodedata.category(ch) == "Cc" for ch in value)


def normalize_udp_match(target: ProfileTarget | str,
                        match_type: UdpMatchType | str,
                        value: str) -> str:
    """按目标矩阵校验并规范化 UDP 例外匹配值，返回持久化形式。

    - 目标/类型不兼容、未知枚举值：ValueError（fail closed）
    - process：1–255 字符，拒逗号、双引号与全部 Unicode 控制字符；原样返回
    - mac_address：仅接受 6 组 `:`/`-` 分隔的十六进制；返回大写冒号形式

    任何错误消息只含静态说明，不含原始值。
    """
    target = ProfileTarget(target)
    match_type = UdpMatchType(match_type)
    if match_type not in ALLOWED_UDP_MATCH_TYPES[target]:
        raise ValueError(
            f"目标 {target.value} 不支持 {match_type.value} 类型的 UDP 例外")

    if match_type is UdpMatchType.PROCESS:
        if not value or len(value) > PROCESS_VALUE_MAX_LENGTH:
            raise ValueError(
                f"process 匹配值长度必须是 1-{PROCESS_VALUE_MAX_LENGTH} 个字符")
        if "," in value or '"' in value:
            raise ValueError("process 匹配值不得包含逗号或双引号")
        if _contains_control_character(value):
            raise ValueError("process 匹配值不得包含换行或控制字符")
        return value

    # UdpMatchType.MAC_ADDRESS
    if not _MAC_ADDRESS_PATTERN.fullmatch(value):
        raise ValueError("MAC 地址必须是 6 组十六进制，以 : 或 - 分隔")
    return value.upper().replace("-", ":")
