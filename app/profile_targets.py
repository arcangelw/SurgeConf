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


# ── General 能力矩阵（唯一真源）──
# 通用键默认三平台可用；以下集合定义平台专属键，字段元数据
# （app/general_fields.py 的 FieldDef.targets）与值过滤
# （filter_general_for_target）都从这张表派生，禁止另建 allowlist。
IOS_ONLY = frozenset({"compatibility-mode", "auto-suspend"})
MAC_ONLY = frozenset({
    "external-controller-access", "http-api", "http-api-tls",
    "http-api-web-dashboard", "http-listen", "socks5-listen",
    "allow-wifi-access", "wifi-access-http-port", "wifi-access-socks5-port",
    "allow-hotspot-access", "read-etc-hosts",
    "use-local-host-item-for-proxy", "proxy-restricted-to-lan",
})
GATEWAY_ONLY = frozenset({"gateway-restricted-to-lan"})

# 全目标强制安全值：保存 API 拒绝危险覆盖，生成/读取一律以此为准
_FORCED_COMMON = {
    "block-quic": "per-policy",
    "udp-policy-not-supported-behaviour": "REJECT",
}
# 平台强制安全值
_FORCED_BY_TARGET = {
    ProfileTarget.MAC_GATEWAY: {"gateway-restricted-to-lan": True},
    ProfileTarget.IOS_MOBILE: {"compatibility-mode": 1, "auto-suspend": True},
}


def general_targets_for(key: str) -> frozenset[ProfileTarget]:
    """单个 General 键支持的目标集合（能力表唯一派生入口）。

    表外未知键视为通用键（自定义 JSON 入口可用），三平台均支持。
    """
    if key in IOS_ONLY:
        return frozenset({ProfileTarget.IOS_MOBILE})
    if key in MAC_ONLY:
        return frozenset({ProfileTarget.MAC_WORKSTATION, ProfileTarget.MAC_GATEWAY})
    if key in GATEWAY_ONLY:
        return frozenset({ProfileTarget.MAC_GATEWAY})
    return frozenset(ProfileTarget)


def forced_general_values(target: ProfileTarget) -> dict[str, object]:
    """目标必须强制生效的安全值（全目标公共项 + 平台项）"""
    return {**_FORCED_COMMON, **_FORCED_BY_TARGET.get(target, {})}


def filter_general_for_target(target: ProfileTarget, values: dict[str, object]) -> dict[str, object]:
    """按目标过滤 General 值并强制安全值。

    先丢弃目标不支持的键（含旧目标残留），再写入强制安全值；
    危险输入一律被覆盖而非透传。
    """
    target = ProfileTarget(target)
    output = {k: v for k, v in values.items() if target in general_targets_for(k)}
    output.update(forced_general_values(target))
    return output
