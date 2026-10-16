"""Surge 配置文件生成器 - 支持中英文分组名"""

import ipaddress
import os
import re

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models import (
    ProxyNode, RegionGroup, ServiceGroup,
    RuleSource, Subscription,
    CustomRule, HostMapping, SystemConfig,
)
from app.default_config import DEFAULT_GENERAL, DEFAULT_SKIP_PROXY, DEFAULT_URL_REWRITES, DEFAULT_HEADER_REWRITES, DEFAULT_MITM, CATEGORY_NAMES, COMMENT_NAMES
from app.profile_targets import ProfileTarget, filter_general_for_target
from app.services.config_validation import (
    GenerationContext,
    GroupMemberRef,
    RuleActionRef,
    extract_rule_action,
    resolve_policy_reference,
    translate_policy_name,
    translate_rule_action,
    validate_generated_references,
)
from app.services.profile_resolution import profile_scope, resolve_profile

# 默认排除关键词（优先使用系统配置）
DEFAULT_EXCLUSION_KEYWORDS = r"Remain|Expired|Expire|应急|官网|如需|套餐|去除|剩余|距离|Reset|重置|流量|Traffic|Used|已用|Today|今日|GB|Download|Upload|Speed"

# IP 类规则集关键词：命中则生成 no-resolve，避免域名连接为匹配 IP 规则触发本地 DNS 解析（DNS 泄漏）
DEFAULT_NO_RESOLVE_KEYWORDS = "(ip),ASN,/ip/,Lan"

# 控制器凭据 env 值必须匹配的单行 access@host:port 格式（host 支持
# hostname / IPv4 / 方括号 IPv6）；任何控制字符或格式偏离都让生成失败，
# 防止 CR/LF 等注入额外键或 section。
_HOSTNAME_RE = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$")
_ACCESS_FORBIDDEN = set(" \t\r\n\v\f,=#@")


def validated_access_env(name: str) -> str | None:
    """读取并校验控制器凭据环境变量；未设置/仅空白返回 None。

    合法值：单行 ``access@host:port``，access 不含空白与配置分隔符，
    host 为 hostname、IPv4 或 ``[IPv6]``，port 为 1..65535。
    非法值抛 ValueError —— 明确生成失败，绝不带病输出。
    """
    raw = os.environ.get(name)
    if raw is None:
        return None
    # 任何 trim 之前先拒绝全部 C0/C1 控制字符（含首尾 CR/LF/TAB/DEL）
    if any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in raw):
        raise ValueError(f"{name}: 控制字符不被允许")
    # 仅普通空格可 trim（不吞 TAB/CR/LF 等——它们已在上面被拒绝）
    value = raw.strip(" ")
    if not value:
        return None
    access, sep, host_port = value.partition("@")
    if not sep or not access or any(ch in _ACCESS_FORBIDDEN for ch in access):
        raise ValueError(f"{name}: 需要 access@host:port 格式且 access 不含空白/分隔符")
    host, port_sep, port = host_port.rpartition(":")
    if not port_sep or not host:
        raise ValueError(f"{name}: 缺少 host:port")
    if host.startswith("[") and host.endswith("]") and host.count("[") == 1:
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError as exc:
            raise ValueError(f"{name}: 非法 IPv6 字面量") from exc
    else:
        if ":" in host:
            raise ValueError(f"{name}: IPv6 必须使用方括号包裹")
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            # 形似 IPv4（纯数字与点）的 host 不允许回退为 hostname
            if host.replace(".", "").isdigit() or not _HOSTNAME_RE.match(host):
                raise ValueError(f"{name}: 非法 host（需 hostname/IPv4/[IPv6]）")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError(f"{name}: 端口需为 1..65535")
    return value

async def _get_sys_config(db: AsyncSession) -> dict:
    """读取系统级配置"""
    from app.models import SystemConfig
    result = await db.execute(select(SystemConfig))
    configs = {row.key: row.value for row in result.scalars().all()}
    return configs


def _quote(name: str) -> str:
    if not name:
        return '""'
    return f'"{name}"' if (" " in name or "]" in name or "[" in name) else name


def _display_name(node: ProxyNode, sub_map: dict) -> str:
    sub_name = sub_map.get(node.subscription_id, "")
    if sub_name:
        return f"({sub_name}) {node.name}"
    return node.name


class GroupNameSinks:
    """结构化登记本次实际生成的组名与组成员引用（供引用校验）。

    组名取 t() 翻译后的输出名（未加引号）；成员引用在每条组行拼接处
    同步登记（输出名形态，直接对照可用策略集合）。disabled、未生成、
    被过滤的组/节点不进入登记。防漂移不变量由测试守卫：[Proxy Group]
    输出的每个组名都被登记到对应桶。
    """

    def __init__(self):
        self.region: set[str] = set()
        self.service: set[str] = set()
        self.combined: set[str] = set()
        self.member_refs: list[GroupMemberRef] = []


async def generate_surge_config(db: AsyncSession, profile_id: int | None = None, locale_override: str | None = None) -> str:
    # 唯一档案解析：显式 id（含 0/负数）不存在即 ProfileNotFound，仅省略时取默认
    profile = await resolve_profile(db, profile_id)

    locale = locale_override or (profile.locale or "zh")

    def t(name: str) -> str:
        # canonical 翻译：输出与引用校验共用 translate_policy_name，杜绝两套映射
        return translate_policy_name(name, locale)

    def tc(key: str) -> str:
        """翻译分类名：zh 用中文名，en 保留英文原名"""
        if locale == "zh" or not locale:
            return CATEGORY_NAMES.get(key, key)
        upper_keys = {"ai", "lan"}
        if key in upper_keys:
            return key.upper()
        return key.replace("_", " ").title()

    def tcomment(key: str) -> str:
        """翻译固定注释"""
        zh, en = COMMENT_NAMES.get(key, (key, key))
        return zh if locale == "zh" or not locale else en

    # final_action 规范化恰好一次（trim）：校验与 FINAL 输出共享同一值，
    # 杜绝「校验 strip 后的名字、输出带空白的原值」
    final_action = (profile.final_action or "").strip()

    nodes_result = await db.execute(
        select(ProxyNode).where(ProxyNode.enabled == True).order_by(ProxyNode.name)
    )
    all_nodes = nodes_result.scalars().all()

    regions_result = await db.execute(
        select(RegionGroup).where(RegionGroup.enabled == True).order_by(RegionGroup.sort_order)
    )
    region_groups = regions_result.scalars().all()

    services_result = await db.execute(
        select(ServiceGroup).where(ServiceGroup.enabled == True).order_by(ServiceGroup.sort_order)
    )
    service_groups = services_result.scalars().all()

    rules_result = await db.execute(
        select(RuleSource).where(RuleSource.enabled == True).order_by(RuleSource.sort_order)
    )
    rule_sources = rules_result.scalars().all()

    subs_result = await db.execute(select(Subscription))
    sub_map = {s.id: s.name for s in subs_result.scalars().all()}

    # 系统级配置
    sys_config = await _get_sys_config(db)
    exclusion_keywords = sys_config.get("exclusion_keywords", DEFAULT_EXCLUSION_KEYWORDS)
    other_auto_enabled = sys_config.get("other_auto_enabled", "true") == "true"
    other_manual_enabled = sys_config.get("other_manual_enabled", "true") == "true"

    # Host 映射（提前查询：[Host] 段与 skip-proxy 合并共用）
    host_result = await db.execute(
        select(HostMapping)
        .where(HostMapping.enabled == True, profile_scope(HostMapping, profile.id))
        .order_by(HostMapping.sort_order)
    )
    host_mappings = host_result.scalars().all()

    # General
    general = dict(DEFAULT_GENERAL)
    # 控制器凭据：无静态默认，仅在对应环境变量为合法非空值时注入为默认值
    # （配置方案 General 表单的显式值仍可覆盖；非法值在此明确失败）
    env_controller = validated_access_env("SURGE_CONTROLLER_ACCESS")
    if env_controller:
        general["external-controller-access"] = env_controller
    env_http_api = validated_access_env("SURGE_HTTP_API")
    if env_http_api:
        general["http-api"] = env_http_api
    if profile.general:
        general.update(profile.general)
    # skip-proxy：默认条目 + 旧残留 + 系统级自定义 + host 勾选域名，合并去重
    general["skip-proxy"] = ", ".join(_build_skip_proxy(profile, sys_config, host_mappings))
    # 目标能力过滤：跳过 None/空字符串后，丢弃平台不支持键并强制安全值
    profile_target = ProfileTarget(profile.target)
    general = filter_general_for_target(
        profile_target, {k: v for k, v in general.items() if v is not None and v != ""})

    sections = [
        "# ============================================================",
        "# SurgeConf - Surge Configuration Manager",
        "# This file was generated by SurgeConf, a local configuration",
        "# management tool. Use at your own risk. Ensure your usage",
        "# complies with all applicable laws and regulations.",
        "# ============================================================",
        "",
    ]
    sections.append("[General]")
    for key, value in general.items():
        if isinstance(value, bool):
            sections.append(f"{key} = {'true' if value else 'false'}")
        else:
            sections.append(f"{key} = {value}")
    sections.append("")

    # Proxy（排除订阅信息节点，如 Expire/Traffic，与分组过滤使用同一关键词）
    import re as _re
    info_pattern = _re.compile(exclusion_keywords)
    # 实际输出节点名：引用校验的可用策略来源之一（被过滤的节点不算存在）
    proxy_names: set[str] = set()
    sections.append("[Proxy]")
    if all_nodes:
        for node in all_nodes:
            if info_pattern.search(node.name):
                continue
            display = _display_name(node, sub_map)
            config_line = node.config.replace(node.name, display, 1)
            proxy_names.add(display)
            sections.append(config_line)
    sections.append("")

    # 规则策略引用的输出翻译（与校验 resolve_policy_reference 同一条流）：
    # 内置动作与精确命中节点名的引用原样（节点名不翻译），生成组按逻辑名翻译
    proxy_name_set = frozenset(proxy_names)

    def tr(name: str) -> str:
        return resolve_policy_reference(name, locale, proxy_name_set)

    # Proxy Group
    group_names = GroupNameSinks()
    sections.append("[Proxy Group]")
    sections.extend(_build_proxy_groups(all_nodes, region_groups, service_groups, t, sub_map,
                                        exclusion_keywords, other_auto_enabled, other_manual_enabled,
                                        name_sinks=group_names))
    sections.append("")

    # Rule
    sections.append("[Rule]")

    # 从新表读取自定义规则
    pre_rules_result = await db.execute(
        select(CustomRule)
        .where(CustomRule.enabled == True, CustomRule.position == "pre",
               profile_scope(CustomRule, profile.id))
        .order_by(CustomRule.sort_order)
    )
    pre_rules = pre_rules_result.scalars().all()

    general_rules_result = await db.execute(
        select(CustomRule)
        .where(CustomRule.enabled == True, CustomRule.position == "general",
               profile_scope(CustomRule, profile.id))
        .order_by(CustomRule.sort_order)
    )
    general_rules = general_rules_result.scalars().all()

    no_resolve_keywords = sys_config.get("no_resolve_keywords", DEFAULT_NO_RESOLVE_KEYWORDS)
    # 先构建 + 记录结构化引用，再统一校验：任何无效引用聚合抛出，
    # 绝不把部分 [Rule] 段拼进产物（sections 只在校验通过后扩展）
    rule_refs: list[RuleActionRef] = []
    rule_lines = _build_rules(rule_sources, pre_rules, general_rules, tr, tc, tcomment,
                              no_resolve_keywords,
                              final_action=final_action, record=rule_refs)
    validate_generated_references(GenerationContext(
        locale=locale,
        target=profile.target,
        node_names=proxy_name_set,
        region_group_names=frozenset(group_names.region),
        service_group_names=frozenset(group_names.service),
        combined_group_names=frozenset(group_names.combined),
        rule_actions=tuple(rule_refs),
        group_members=tuple(group_names.member_refs),
        final_action=final_action,
    ))
    sections.extend(rule_lines)
    sections.append("")

    # Host
    sections.append("[Host]")
    for m in host_mappings:
        sections.append(f"{m.domain} = {m.target}")
    sections.append("")

    # URL Rewrite
    sections.append("[URL Rewrite]")
    rewrites = profile.url_rewrites if profile.url_rewrites else DEFAULT_URL_REWRITES
    for rule in rewrites:
        sections.append(rule)
    sections.append("")

    # Header Rewrite
    sections.append("[Header Rewrite]")
    header_rewrites = profile.header_rewrites if profile.header_rewrites else DEFAULT_HEADER_REWRITES
    for rule in header_rewrites:
        sections.append(rule)
    sections.append("")

    # Script
    sections.append("[Script]")
    sections.append("")

    # MITM
    sections.append("[MITM]")
    mitm = {**DEFAULT_MITM, **(profile.mitm or {})}
    for key, value in mitm.items():
        if isinstance(value, bool):
            sections.append(f"{key} = {'true' if value else 'false'}")
        else:
            sections.append(f"{key} = {value}")
    sections.append("")

    return "\n".join(sections)


def _build_proxy_groups(
    all_nodes: list[ProxyNode],
    region_groups: list[RegionGroup],
    service_groups: list[ServiceGroup],
    t,
    sub_map: dict,
    exclusion_keywords: str = DEFAULT_EXCLUSION_KEYWORDS,
    other_auto_enabled: bool = True,
    other_manual_enabled: bool = True,
    name_sinks: GroupNameSinks | None = None,
) -> list[str]:
    lines = []

    def _sink(bucket: set[str], name: str) -> None:
        if name_sinks is not None:
            bucket.add(name)

    def _members(group: str, member_names: list[str]) -> None:
        """结构化登记一个组的成员引用（输出名形态；组行的非策略选项不算成员）"""
        if name_sinks is not None:
            for member in member_names:
                name_sinks.member_refs.append(GroupMemberRef(group=group, member=member))

    # 全部节点组
    sub_filter = f"^((?!{exclusion_keywords}).)+$"
    _sink(name_sinks.combined if name_sinks else set(), t("全部节点"))
    lines.append(
        f'{_quote(t("全部节点"))} = select, '
        f'policy-regex-filter={sub_filter}, '
        f'include-all-proxies=1, '
        f'icon-url=https://raw.githubusercontent.com/lige47/QuanX-icon-rule/main/icon/05icon/rocket(1).png'
    )
    lines.append("")

    # 筛选活跃地区：至少一个子开关启用
    active_regions = [rg for rg in region_groups if rg.auto_enabled or rg.manual_enabled]
    auto_regions = [rg for rg in active_regions if rg.auto_enabled]
    manual_regions = [rg for rg in active_regions if rg.manual_enabled]

    # ── 自动地区测速组 ──
    for rg in auto_regions:
        icon = f', icon-url={rg.icon_url}' if rg.icon_url else ""
        _sink(name_sinks.region if name_sinks else set(), t(rg.name))
        # include-other-group 引用「全部节点」组
        _members(t(rg.name), [t("全部节点")])
        lines.append(
            f'{_quote(t(rg.name))} = url-test, '
            f'policy-regex-filter={rg.filter_regex}, '
            f'include-other-group={_quote(t("全部节点"))}, '
            f'url=http://cp.cloudflare.com/generate_204, '
            f'interval=300, tolerance=100, timeout=3'
            f'{icon}'
        )

    has_other = False
    other_region_quoted = _quote(t("其他"))
    other_manual_quoted = _quote(t("其他手动"))
    # 计算其他组过滤正则（排除所有活跃地区的匹配）
    other_filter = ""
    if auto_regions:
        all_filters = "|".join(rg.filter_regex for rg in active_regions)
        other_filter = f"^((?!{all_filters}|{exclusion_keywords}).)+$"

    # 其他地区（自动）— 仅在有自动地区时生成
    if auto_regions and other_auto_enabled:
        has_other = True
        _sink(name_sinks.region if name_sinks else set(), t("其他"))
        _members(t("其他"), [t("全部节点")])
        lines.append(
            f'{other_region_quoted} = url-test, '
            f'policy-regex-filter={other_filter}, '
            f'include-other-group={_quote(t("全部节点"))}, '
            f'url=http://cp.cloudflare.com/generate_204, '
            f'interval=300, tolerance=100, timeout=3, '
            f'icon-url=https://raw.githubusercontent.com/lige47/QuanX-icon-rule/main/icon/01Country/EuropeanUnion.png'
        )

    lines.append("")

    # ── 手动地区分组 ──
    manual_region_names = []
    manual_region_display = []
    for rg in manual_regions:
        manual_name = f"{rg.name}手动"
        icon = f', icon-url={rg.icon_url}' if rg.icon_url else ""
        _sink(name_sinks.region if name_sinks else set(), t(manual_name))
        _members(t(manual_name), [t("全部节点")])
        lines.append(
            f'{_quote(t(manual_name))} = select, '
            f'policy-regex-filter={rg.filter_regex}, '
            f'include-other-group={_quote(t("全部节点"))}'
            f'{icon}'
        )
        manual_region_names.append(_quote(t(manual_name)))
        manual_region_display.append(t(manual_name))
    # 其他手动选择 — 独立开关（放在手动地区组之后）
    other_manual_names = []
    other_manual_display = []
    if auto_regions and other_manual_enabled:
        has_other = True
        _sink(name_sinks.region if name_sinks else set(), t("其他手动"))
        _members(t("其他手动"), [t("全部节点")])
        lines.append(
            f'{other_manual_quoted} = select, '
            f'policy-regex-filter={other_filter}, '
            f'include-other-group={_quote(t("全部节点"))}, '
            f'icon-url=https://raw.githubusercontent.com/lige47/QuanX-icon-rule/main/icon/01Country/EuropeanUnion.png'
        )
        other_manual_names.append(other_manual_quoted)
        other_manual_display.append(t("其他手动"))
    lines.append("")

    # ── 组合策略 ──
    auto_region_names = [_quote(t(rg.name)) for rg in auto_regions]
    all_region_names = auto_region_names + manual_region_names + other_manual_names
    # 全部地区组显示名（代理组成员顺序与 all_region_names 一致）
    all_region_display = ([t(rg.name) for rg in auto_regions]
                          + manual_region_display + other_manual_display)

    # 自动选择：仅包含明确分区的自动测速组（排除「其他」兜底组）
    auto_select_members = list(auto_region_names)
    if auto_select_members:
        _sink(name_sinks.combined if name_sinks else set(), t("自动选择"))
        _members(t("自动选择"), [t(rg.name) for rg in auto_regions])
        lines.append(
            f'{_quote(t("自动选择"))} = select, {", ".join(auto_select_members)}, '
            f'url=http://cp.cloudflare.com/generate_204, interval=300, tolerance=100, timeout=3, '
            f'icon-url=https://raw.githubusercontent.com/lige47/QuanX-icon-rule/main/icon/05icon/lightning(5).png'
        )
    # 代理：自动选择 + 所有地区（自动+手动）
    proxy_members = []
    proxy_member_display = []
    if auto_select_members:
        proxy_members.append(_quote(t("自动选择")))
        proxy_member_display.append(t("自动选择"))
    proxy_members.extend(all_region_names)
    proxy_member_display.extend(all_region_display)
    if proxy_members:
        _sink(name_sinks.combined if name_sinks else set(), t("代理"))
        _members(t("代理"), proxy_member_display)
        lines.append(
            f'{_quote(t("代理"))} = select, {", ".join(proxy_members)}, '
            f'icon-url=https://raw.githubusercontent.com/lige47/QuanX-icon-rule/main/icon/05icon/rocket(1).png'
        )
    # 手动选择：大陆直连 + 自动选择 + 所有手动地区组
    manual_select_members = [_quote(t("大陆直连"))]
    manual_select_display = [t("大陆直连")]
    if auto_select_members:
        manual_select_members.append(_quote(t("自动选择")))
        manual_select_display.append(t("自动选择"))
    manual_select_members.extend(manual_region_names + other_manual_names)
    manual_select_display.extend(manual_region_display + other_manual_display)
    if manual_region_names or other_manual_names or auto_select_members:
        _sink(name_sinks.combined if name_sinks else set(), t("手动选择"))
        _members(t("手动选择"), manual_select_display)
        lines.append(
            f'{_quote(t("手动选择"))} = select, {", ".join(manual_select_members)}, '
            f'icon-url=https://raw.githubusercontent.com/lige47/QuanX-icon-rule/main/icon/05icon/lightning.png'
        )
    lines.append("")

    # ── 服务分组 ──
    for sg in service_groups:
        pinned = [n for n in all_nodes if sg.name in (n.pinned_services or [])]
        pinned_names = [_display_name(n, sub_map) for n in pinned]
        policy_names = [t(p) for p in sg.policies]

        all_entries = pinned_names + policy_names
        entries_str = ", ".join(
            _quote(e) if " " in e else e for e in all_entries
        )
        icon = f', icon-url={sg.icon_url}' if sg.icon_url else ""
        _sink(name_sinks.service if name_sinks else set(), t(sg.name))
        _members(t(sg.name), pinned_names + policy_names)
        lines.append(f'{_quote(t(sg.name))} = {sg.group_type}, {entries_str}{icon}')

    return lines


def _emit_custom_rule(rule_text: str, label: str, t, record: list[RuleActionRef] | None) -> str:
    """翻译自定义规则的策略段并登记引用。

    无法定位策略段时原样输出并登记 action=None —— 构建器保持宽松，
    fail closed 由校验器裁决（generate_surge_config 先校验后拼接产物）。
    """
    action = extract_rule_action(rule_text)
    if record is not None:
        record.append(RuleActionRef(label, action))
    translated = translate_rule_action(rule_text, t)
    return translated if translated is not None else rule_text


def _build_rules(rule_sources: list[RuleSource], pre_rules: list[CustomRule], general_rules: list[CustomRule], t, tc, tcomment, no_resolve_keywords: str | None = None, *, final_action: str, record: list[RuleActionRef] | None = None) -> list[str]:
    lines = []

    # 命中关键词的规则集视为 IP 类，追加 no-resolve 防止触发本地 DNS 解析
    ip_keywords = [k.strip() for k in (no_resolve_keywords or DEFAULT_NO_RESOLVE_KEYWORDS).split(",") if k.strip()]

    # 前置自定义规则
    if pre_rules:
        lines.append(f"# >> {tcomment('pre_rules')}")
        for r in pre_rules:
            lines.append(_emit_custom_rule(
                r.rule_text, f"前置自定义规则「{r.rule_text}」", t, record))
        lines.append("")

    # 规则集
    current_category = None
    for rs in rule_sources:
        if rs.category != current_category:
            current_category = rs.category
            lines.append(f"# >> {tc(current_category)}")

        translated_action = t(rs.action)
        if record is not None:
            record.append(RuleActionRef(f"规则源「{rs.name}」", rs.action))
        line = f"{rs.rule_type},{rs.url},{_quote(translated_action)}"
        if any(kw in rs.name or kw in rs.url for kw in ip_keywords):
            line += ",no-resolve"
        lines.append(line)

    # 通用自定义规则
    if general_rules:
        lines.append("")
        lines.append(f"# >> {tcomment('general_rules')}")
        for r in general_rules:
            lines.append(_emit_custom_rule(
                r.rule_text, f"通用自定义规则「{r.rule_text}」", t, record))

    lines.append("")
    lines.append("RULE-SET,LAN,DIRECT,no-resolve")

    lines.append("")
    lines.append(f"# >> {tcomment('final_rule')}")
    # final_action 直通（调用方已 trim 并与校验共享同一值）：空值由校验器
    # 拒绝，此处绝不静默回退到默认策略；其引用由 validate_generated_references
    # 经 context.final_action 单独校验，不在 rule_actions 重复登记
    lines.append(f"FINAL,{_quote(t(final_action))},dns-failed")

    return lines


def _build_skip_proxy(profile, sys_config: dict, host_mappings) -> list[str]:
    """构建 skip-proxy 列表：默认条目 + 旧 profile 残留 + 系统级自定义 + host 勾选域名，去重保序。"""
    import json

    items = list(DEFAULT_SKIP_PROXY)

    # 旧版 profile.general 中残留的 skip-proxy（升级兼容）
    if profile.general:
        legacy = profile.general.get("skip-proxy")
        if isinstance(legacy, str):
            items.extend(x.strip() for x in legacy.split(",") if x.strip())

    # 系统级自定义条目
    try:
        custom = json.loads(sys_config.get("skip_proxy_custom", "[]"))
        if isinstance(custom, list):
            items.extend(str(x).strip() for x in custom if str(x).strip())
    except (ValueError, TypeError):
        pass

    # host_mappings 已在 SQL 层按「全局 + 当前档案」作用域过滤；
    # 勾选 skip_proxy 的映射全部并入，其他档案不会出现在产物中
    for m in host_mappings:
        if m.skip_proxy:
            domain = (m.domain or "").strip()
            if domain:
                items.append(domain)

    # 去重保序
    seen = set()
    result = []
    for x in items:
        if x not in seen:
            seen.add(x)
            result.append(x)
    return result
