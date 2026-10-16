"""档案目标设备与 UDP 安全模式的领域类型和组合规则。

未知枚举值由 Pydantic 在进入服务层前拦截为 422；本模块只负责
已知目标与模式之间的组合约束。
"""

from enum import StrEnum


class ProfileTarget(StrEnum):
    MAC_WORKSTATION = "mac_workstation"
    MAC_GATEWAY = "mac_gateway"
    IOS_MOBILE = "ios_mobile"


class UdpSecurityMode(StrEnum):
    STRICT = "strict"
    SCOPED = "scoped"
    COMPATIBLE = "compatible"


def default_udp_mode(target: ProfileTarget) -> UdpSecurityMode:
    """目标的默认 UDP 安全模式：iOS 走 compatible，其余走 scoped"""
    return UdpSecurityMode.COMPATIBLE if target is ProfileTarget.IOS_MOBILE else UdpSecurityMode.SCOPED


def validate_target_mode(target: ProfileTarget, mode: UdpSecurityMode) -> None:
    """组合校验：唯一被拒绝的是 ios_mobile + scoped"""
    target = ProfileTarget(target)
    mode = UdpSecurityMode(mode)
    if target is ProfileTarget.IOS_MOBILE and mode is UdpSecurityMode.SCOPED:
        raise ValueError(
            f"target ios_mobile 不允许 udp_security_mode scoped")
