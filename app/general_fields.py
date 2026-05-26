"""General 字段元数据定义 — 驱动表单自动生成"""

from dataclasses import dataclass, field

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
                     options=[("auto", "自动", "Auto"), ("native", "原生", "Native"), ("off", "关闭", "Off")]),
            FieldDef("udp-priority", "UDP 优先", "UDP Priority", BOOL,
                     "优先处理 UDP 流量", "Prioritize UDP traffic"),
            FieldDef("all-hybrid", "全部混合代理", "All Hybrid Proxy", BOOL,
                     "所有代理使用混合模式", "Use hybrid mode for all proxies"),
            FieldDef("compatibility-mode", "兼容模式", "Compatibility Mode", INT,
                     "0=关闭, 1=兼容旧版", "0=Off, 1=Legacy compatible"),
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
                     "DoH/DoT 地址，逗号分隔", "DoH/DoT URLs, comma separated"),
            FieldDef("encrypted-dns-follow-outbound-mode", "加密 DNS 跟随出站模式", "Encrypted DNS Follow Outbound", BOOL),
            FieldDef("always-real-ip", "始终返回真实 IP", "Always Real IP", TAGS,
                     "这些域名绕过 fake-ip", "Domains that bypass fake-ip"),
            FieldDef("hijack-dns", "劫持 DNS", "Hijack DNS", TAGS,
                     "劫持指定端口的 DNS 请求", "Hijack DNS on specified ports"),
            FieldDef("geoip-maxmind-url", "GeoIP 数据库 URL", "GeoIP Database URL", TEXT),
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
            FieldDef("http-api-tls", "HTTP API TLS", "HTTP API TLS", BOOL),
            FieldDef("http-api-web-dashboard", "HTTP API Web 面板", "HTTP API Web Dashboard", BOOL),
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
            FieldDef("skip-proxy", "跳过代理", "Skip Proxy", TAGS,
                     "这些地址不走代理", "Addresses that bypass proxy"),
            FieldDef("exclude-simple-hostnames", "排除简单主机名", "Exclude Simple Hostnames", BOOL),
            FieldDef("read-etc-hosts", "读取 hosts 文件", "Read /etc/hosts", BOOL),
            FieldDef("use-local-host-item-for-proxy", "代理使用本地 Host", "Use Local Host for Proxy", BOOL),
            FieldDef("include-all-networks", "包含所有网络", "Include All Networks", BOOL,
                     "VPN 模式：禁止非 VPN 流量", "VPN mode: block non-VPN traffic"),
            FieldDef("include-local-networks", "包含本地网络", "Include Local Networks", BOOL,
                     "本地网络也走代理", "Route local network through proxy too"),
            FieldDef("udp-policy-not-supported-behaviour", "UDP 不支持策略行为", "UDP Unsupported Policy", SELECT,
                     options=[("REJECT", "拒绝", "Reject"), ("DROP", "丢弃", "Drop"), ("DIRECT", "直连", "Direct")]),
            FieldDef("proxy-restricted-to-lan", "代理限制到局域网", "Proxy Restricted to LAN", BOOL),
        ],
    },
]
