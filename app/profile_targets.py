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


# 实例角色 → 新档案默认目标（单向映射）。键集合与 app.security 的
# VALID_INSTALLATION_ROLES 一致，由测试锁定防漂移；角色变化只影响
# 此后未显式传 target 的创建请求，绝不回写已有档案。
DEFAULT_TARGET_BY_ROLE: dict[str, ProfileTarget] = {
    "work_computer": ProfileTarget.MAC_WORKSTATION,
    "home_gateway": ProfileTarget.MAC_GATEWAY,
}


def default_target_for_role(role: str) -> ProfileTarget:
    """实例角色对应的新档案默认目标；未知角色 fail-closed 回落默认角色映射。

    上游 load_access_settings 已把库中非法角色收敛为默认角色，此处的
    回落仅作防御（不该被触达）。
    """
    return DEFAULT_TARGET_BY_ROLE.get(role, ProfileTarget.MAC_WORKSTATION)


def validate_target_mode(target: ProfileTarget, mode: UdpSecurityMode) -> None:
    """组合校验：唯一被拒绝的是 ios_mobile + scoped"""
    target = ProfileTarget(target)
    mode = UdpSecurityMode(mode)
    if target is ProfileTarget.IOS_MOBILE and mode is UdpSecurityMode.SCOPED:
        raise ValueError(
            f"target ios_mobile 不允许 udp_security_mode scoped")


# ── General 能力矩阵（唯一真源，完整显式 allowlist）──
# COMMON/MAC_ONLY/IOS_ONLY/GATEWAY_ONLY + GENERATOR_ONLY 登记全部合法键；
# 表外未知键不属于任何目标（PUT 拒绝、生成/读取丢弃）。
# 字段元数据（FieldDef.targets）与值过滤（filter_general_for_target）
# 都从这张表派生，禁止另建 allowlist。
COMMON_GENERAL_KEYS = frozenset({
    "wifi-assist", "ipv6", "ipv6-vif", "udp-priority", "all-hybrid",
    "internet-test-url", "proxy-test-url", "test-timeout",
    "dns-server", "encrypted-dns-server", "encrypted-dns-follow-outbound-mode",
    "always-real-ip", "hijack-dns",
    "loglevel", "show-error-page-for-reject", "exclude-simple-hostnames",
    "include-all-networks", "include-local-networks",
    "block-quic", "udp-policy-not-supported-behaviour",
})
IOS_ONLY = frozenset({"compatibility-mode", "auto-suspend"})
MAC_ONLY = frozenset({
    "external-controller-access", "http-api", "http-api-tls",
    "http-api-web-dashboard", "http-listen", "socks5-listen",
    "allow-wifi-access", "wifi-access-http-port", "wifi-access-socks5-port",
    "allow-hotspot-access", "read-etc-hosts",
    "use-local-host-item-for-proxy", "proxy-restricted-to-lan",
})
GATEWAY_ONLY = frozenset({"gateway-restricted-to-lan"})
# 内部生成键：仅由生成器在输出时写入（如 skip-proxy 每次重算），
# 不属于任何目标的用户可配置集合
GENERATOR_ONLY_KEYS = frozenset({"skip-proxy"})

# 全部合法键（含生成期内部键）
KNOWN_GENERAL_KEYS = (COMMON_GENERAL_KEYS | IOS_ONLY | MAC_ONLY
                      | GATEWAY_ONLY | GENERATOR_ONLY_KEYS)
# 用户可配置键（UI 表单与 PUT 的 allowlist）
CONFIGURABLE_GENERAL_KEYS = KNOWN_GENERAL_KEYS - GENERATOR_ONLY_KEYS

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
    """单个 General 键允许出现在输出中的目标集合（能力表唯一派生入口）。

    生成期内部键（skip-proxy 等）三平台输出均允许；表外未知键
    不属于任何目标——PUT 拒绝、生成/读取丢弃。
    """
    if key in IOS_ONLY:
        return frozenset({ProfileTarget.IOS_MOBILE})
    if key in MAC_ONLY:
        return frozenset({ProfileTarget.MAC_WORKSTATION, ProfileTarget.MAC_GATEWAY})
    if key in GATEWAY_ONLY:
        return frozenset({ProfileTarget.MAC_GATEWAY})
    if key in COMMON_GENERAL_KEYS or key in GENERATOR_ONLY_KEYS:
        return frozenset(ProfileTarget)
    return frozenset()


def configurable_general_keys(target: ProfileTarget) -> frozenset[str]:
    """目标可由用户配置的键（UI 元数据与 PUT 的同一 allowlist）"""
    target = ProfileTarget(target)
    return frozenset(
        k for k in CONFIGURABLE_GENERAL_KEYS if target in general_targets_for(k))


def forced_general_values(target: ProfileTarget) -> dict[str, object]:
    """目标必须强制生效的安全值（全目标公共项 + 平台项）"""
    return {**_FORCED_COMMON, **_FORCED_BY_TARGET.get(target, {})}


def filter_general_for_target(target: ProfileTarget, values: dict[str, object]) -> dict[str, object]:
    """按目标过滤 General 值并强制安全值。

    先丢弃目标不支持与表外未知的键（含旧目标残留），再写入强制
    安全值；危险输入一律被覆盖而非透传。
    """
    target = ProfileTarget(target)
    output = {k: v for k, v in values.items() if target in general_targets_for(k)}
    output.update(forced_general_values(target))
    return output
