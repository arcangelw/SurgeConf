"""General 字段元数据定义 — 驱动表单自动生成

FieldDef.targets 在模块加载时由 app.profile_targets 的能力矩阵统一派生
（见文件尾部），不允许手写第二份平台 allowlist。
"""

from dataclasses import dataclass, field

from app.profile_targets import ProfileTarget, general_targets_for

# 字段类型
BOOL = "bool"
TEXT = "text"
INT = "int"
SELECT = "select"
TAGS = "tags"  # 逗号/换行分隔的列表


@dataclass
class FieldDef:
    key: str
    label_zh: str
    label_en: str
    type: str
    hint_zh: str = ""
    hint_en: str = ""
    options: list[tuple[str, str, str]] = field(default_factory=list)  # (value, zh_label, en_label)
    # 支持的目标平台；由能力矩阵派生，加载后统一填充（见文件尾部）
    targets: frozenset = frozenset()


# ── 分组定义 ──

GENERAL_GROUPS = [
    {
        "key": "network",
        "label_zh": "网络与代理",
        "label_en": "Network & Proxy",
        "fields": [
            FieldDef("wifi-assist", "Wi-Fi 辅助", "Wi-Fi Assist", BOOL,
                     "Wi-Fi 和蜂窝同时使用", "Use Wi-Fi and cellular simultaneously"),
            FieldDef("ipv6", "IPv6", "IPv6", BOOL,
                     "启用 IPv6 支持", "Enable IPv6 support"),
            FieldDef("ipv6-vif", "IPv6 VIF 模式", "IPv6 VIF Mode", SELECT,
                     hint_zh="IPv6 虚拟网卡模式", hint_en="IPv6 virtual interface mode",
                     options=[("disabled", "关闭", "Disabled"), ("auto", "自动", "Auto"), ("always", "始终", "Always")]),
            FieldDef("udp-priority", "UDP 优先", "UDP Priority", BOOL,
                     "优先处理 UDP 流量", "Prioritize UDP traffic"),
            FieldDef("all-hybrid", "全部混合代理", "All Hybrid Proxy", BOOL,
                     "所有代理使用混合模式", "Use hybrid mode for all proxies"),
            FieldDef("compatibility-mode", "兼容模式", "Compatibility Mode", INT,
                     "仅 Surge iOS 生效：1=代理+VIF 接管（性能最好），3=仅 VIF 接管（默认），0=自动",
                     "Surge iOS only: 1=Proxy+VIF (best perf), 3=VIF only (default), 0=auto"),
            FieldDef("auto-suspend", "自动挂起", "Auto Suspend", BOOL,
                     "仅 Surge iOS 生效：后台自动挂起以省电",
                     "Surge iOS only: auto suspend in background to save battery"),
            FieldDef("internet-test-url", "网络检测 URL", "Internet Test URL", TEXT),
            FieldDef("proxy-test-url", "代理检测 URL", "Proxy Test URL", TEXT),
            FieldDef("test-timeout", "测试超时（秒）", "Test Timeout (sec)", INT),
        ],
    },
    {
        "key": "dns",
        "label_zh": "DNS",
        "label_en": "DNS",
        "fields": [
            FieldDef("dns-server", "DNS 服务器", "DNS Servers", TAGS,
                     "传统 DNS，逗号分隔", "Plain DNS, comma separated"),
            FieldDef("encrypted-dns-server", "加密 DNS", "Encrypted DNS", TAGS,
                     "h3://quic://tls://https:// 逗号分隔，并发先答先用", "h3://quic://tls://https:// comma separated, first answer wins"),
            FieldDef("encrypted-dns-follow-outbound-mode", "加密 DNS 跟随出站模式", "Encrypted DNS Follow Outbound", BOOL,
                     "关闭=恒直连，避免解析依赖代理", "Off=always DIRECT, avoids resolve-via-proxy loop"),
            FieldDef("always-real-ip", "始终返回真实 IP", "Always Real IP", TAGS,
                     "这些域名绕过 fake-ip", "Domains that bypass fake-ip"),
            FieldDef("hijack-dns", "劫持 DNS", "Hijack DNS", TAGS,
                     "劫持明文 DNS 请求，*:53 全劫持", "Hijack plain DNS, *:53 for all"),
        ],
    },
    {
        "key": "controller",
        "label_zh": "控制器与 API",
        "label_en": "Controller & API",
        "fields": [
            FieldDef("external-controller-access", "远程控制器", "External Controller", TEXT,
                     "格式: user@host:port", "Format: user@host:port"),
            FieldDef("http-api", "HTTP API", "HTTP API", TEXT,
                     "格式: user@host:port", "Format: user@host:port"),
            FieldDef("http-api-tls", "HTTP API TLS", "HTTP API TLS", BOOL,
                     "HTTP API 强制 TLS 连接", "Require TLS for HTTP API"),
            FieldDef("http-api-web-dashboard", "HTTP API Web 面板", "HTTP API Web Dashboard", BOOL,
                     "Surge 内置 Web 面板，常驻内存；SurgeConf 不依赖，默认关", "Built-in web dashboard, memory resident; SurgeConf doesn't need it"),
            FieldDef("http-listen", "HTTP 代理监听地址", "HTTP Proxy Listen", TEXT),
            FieldDef("socks5-listen", "SOCKS5 代理监听地址", "SOCKS5 Proxy Listen", TEXT),
            FieldDef("allow-wifi-access", "允许 Wi-Fi 访问", "Allow Wi-Fi Access", BOOL),
            FieldDef("wifi-access-http-port", "Wi-Fi HTTP 端口", "Wi-Fi HTTP Port", INT),
            FieldDef("wifi-access-socks5-port", "Wi-Fi SOCKS5 端口", "Wi-Fi SOCKS5 Port", INT),
            FieldDef("allow-hotspot-access", "允许热点访问", "Allow Hotspot Access", BOOL),
        ],
    },
    {
        "key": "advanced",
        "label_zh": "高级",
        "label_en": "Advanced",
        "fields": [
            FieldDef("loglevel", "日志级别", "Log Level", SELECT,
                     options=[("verbose", "详细", "Verbose"), ("info", "信息", "Info"),
                              ("notify", "通知", "Notify"), ("warning", "警告", "Warning"),
                              ("error", "错误", "Error")]),
            FieldDef("show-error-page-for-reject", "拒绝时显示错误页", "Show Error Page for Reject", BOOL),
            FieldDef("exclude-simple-hostnames", "排除简单主机名", "Exclude Simple Hostnames", BOOL),
            FieldDef("read-etc-hosts", "读取 hosts 文件", "Read /etc/hosts", BOOL),
            FieldDef("use-local-host-item-for-proxy", "代理使用本地 Host", "Use Local Host for Proxy", BOOL),
            FieldDef("include-all-networks", "包含所有网络", "Include All Networks", BOOL,
                     "VPN 模式：禁止非 VPN 流量", "VPN mode: block non-VPN traffic"),
            FieldDef("include-local-networks", "包含本地网络", "Include Local Networks", BOOL,
                     "本地网络也走代理", "Route local network through proxy too"),
            FieldDef("block-quic", "QUIC 处理策略", "Block QUIC", SELECT,
                     hint_zh="强制安全值 per-policy：DIRECT 直连保留 QUIC，代理按策略处理",
                     hint_en="Forced safe value per-policy: QUIC direct stays, proxied per policy",
                     options=[("per-policy", "按策略", "Per Policy"), ("direct", "始终直连", "Always Direct"),
                              ("proxy", "始终代理", "Always Proxy")]),
            FieldDef("udp-policy-not-supported-behaviour", "UDP 不支持策略行为", "UDP Unsupported Policy", SELECT,
                     options=[("REJECT", "拒绝", "Reject"), ("DROP", "丢弃", "Drop"), ("DIRECT", "直连", "Direct")]),
            FieldDef("proxy-restricted-to-lan", "代理限制到局域网", "Proxy Restricted to LAN", BOOL),
            FieldDef("gateway-restricted-to-lan", "网关限制到局域网", "Gateway Restricted to LAN", BOOL,
                     "仅 Surge Mac 网关模式：限制配置生效范围到局域网",
                     "Surge Mac gateway only: restrict config to LAN"),
        ],
    },
]

# ── 能力派生：targets 由唯一真源（profile_targets 能力矩阵）统一填充 ──

for _group in GENERAL_GROUPS:
    for _f in _group["fields"]:
        _f.targets = general_targets_for(_f.key)


def _serialize_field(f: FieldDef) -> dict:
    return {
        "key": f.key,
        "label_zh": f.label_zh,
        "label_en": f.label_en,
        "type": f.type,
        "hint_zh": f.hint_zh,
        "hint_en": f.hint_en,
        "options": [{"value": o[0], "label_zh": o[1], "label_en": o[2]} for o in f.options],
    }


def general_groups_for_target(target: ProfileTarget) -> list[dict]:
    """按目标过滤的分组字段元数据（空组跳过）"""
    target = ProfileTarget(target)
    groups = []
    for g in GENERAL_GROUPS:
        fields = [_serialize_field(f) for f in g["fields"] if target in f.targets]
        if fields:
            groups.append({
                "key": g["key"],
                "label_zh": g["label_zh"],
                "label_en": g["label_en"],
                "fields": fields,
            })
    return groups


def general_fields_for_target(target: ProfileTarget) -> list[dict]:
    """按目标过滤的扁平字段元数据列表（与 general_groups_for_target 同源）"""
    return [f for g in general_groups_for_target(target) for f in g["fields"]]
