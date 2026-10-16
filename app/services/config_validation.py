"""生成配置引用完整性校验。

生成器输出任何字节前，校验全部规则 action、策略组成员引用与
``final_action`` 引用的策略确实存在于本次输出（节点 / 地区组 / 服务组 /
组合组 / Surge 内置动作）。任何无效引用聚合为
:class:`InvalidConfiguration` 一次抛出，绝不产出部分配置。

canonical 命名流：数据库存逻辑名（中文）。GenerationContext 的名字集合
全部是**本次实际输出名**（节点显示名、t() 翻译后的组名），校验时不再
二次翻译；规则里的逻辑引用经 :func:`resolve_policy_reference` 解析为
输出名后对照——内置动作与精确命中节点名的引用原样保留（节点名不翻译，
避免名为「香港」的节点在 en 档案被误翻为 Hong Kong），生成组按逻辑名
翻译。输出与校验共用同一条解析路径，不做任何模糊或反向映射回退。
"""

from dataclasses import dataclass

from app.default_config import DEFAULT_RULE_SOURCES, locale_translate

# Surge 内置终结动作：无需在 [Proxy] / [Proxy Group] 中定义即可被规则引用
BUILTIN_ACTIONS = frozenset({"DIRECT", "REJECT", "REJECT-NO-DROP", "REJECT-DROP", "DNS"})

# 规则尾部的修饰符：不属于策略段，定位策略时需跳过
_RULE_MODIFIERS = frozenset({"no-resolve", "dns-failed"})


class InvalidConfiguration(Exception):
    """生成配置存在无效引用；errors 聚合全部问题（中文，面向用户）"""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = list(errors)


def translate_policy_name(name: str, locale: str = "zh") -> str:
    """策略名的唯一 canonical 翻译：输出与校验共用同一实现，杜绝两套映射漂移"""
    return locale_translate(name, locale)


@dataclass
class RuleActionRef:
    """一条已输出规则的终结策略引用（逻辑形态，校验时再解析）。

    action 为 None 表示无法从规则文本解析出策略段（fail closed：
    校验器将其按无效引用处理，而不是猜测或跳过）。
    """

    source: str  # 人类可读来源标签，用于错误定位
    action: str | None


@dataclass
class GroupMemberRef:
    """一条策略组成员引用（构建组行时结构化登记，不从最终行字符串解析）。

    group/member 均为本次输出的实际显示名（已翻译、未加引号）；
    member 直接对照 available_policies()，无需再翻译。组行的非策略
    选项（url-test/select 类型、policy-regex-filter、url、interval、
    icon-url 等）不是成员引用，不进入登记。
    """

    group: str
    member: str


@dataclass
class GenerationContext:
    """一次生成的完整引用视图。

    名字集合全部来自本次实际输出：节点显示名（未被信息关键词过滤）、
    实际生成的地区/服务/组合组名（t() 翻译后的输出名）。disabled、
    被过滤、未生成的项不算存在。集合已是输出名，校验不再二次翻译。
    """

    locale: str
    target: str = ""
    node_names: frozenset[str] = frozenset()
    region_group_names: frozenset[str] = frozenset()
    service_group_names: frozenset[str] = frozenset()
    combined_group_names: frozenset[str] = frozenset()
    rule_actions: tuple[RuleActionRef, ...] = ()
    group_members: tuple[GroupMemberRef, ...] = ()
    final_action: str | None = None

    def available_policies(self) -> frozenset[str]:
        """规则与组成员可引用的完整策略集合（实际输出名 + 内置动作）"""
        names = set(BUILTIN_ACTIONS)
        names.update(self.node_names)
        names.update(self.region_group_names)
        names.update(self.service_group_names)
        names.update(self.combined_group_names)
        return frozenset(names)


def resolve_policy_reference(name: str, locale: str, node_names: frozenset[str]) -> str:
    """把规则里的策略引用（数据库逻辑形态）解析为实际输出名。

    与生成器输出流完全一致：
    1. 内置动作原样；
    2. 精确命中实际节点名 → 原样（节点名不翻译，防止名为「香港」的
       节点在 en 档案被误翻为 Hong Kong 而与 [Proxy] 段脱节）；
    3. 其余按 canonical 逻辑名翻译到目标 locale（生成组显示名）。

    同一名字既是节点又是组时节点优先（更精确的既有产品预期，
    有冲突测试锁定）。
    """
    if name in BUILTIN_ACTIONS or name in node_names:
        return name
    return translate_policy_name(name, locale)


# 受支持的目标 locale（生成器 tc/t 分支与中央翻译共同覆盖的集合）
SUPPORTED_LOCALES = ("zh", "en")


def localized_output_candidates(name: str) -> frozenset[str]:
    """逻辑名在所有受支持 locale 下的输出名候选（无副作用，纯函数）。

    迁移等无完整生成上下文的调用方用它做保守判定：请求 locale 未知时，
    任一 locale 的输出可能有效就不得改写。内部复用中央
    translate_policy_name，不复制第二份翻译表（zh 候选即原名直通）。
    """
    return frozenset(translate_policy_name(name, locale) for locale in SUPPORTED_LOCALES)


def validate_generated_references(context: GenerationContext) -> None:
    """聚合校验规则 action、策略组成员与 final_action；一次抛出全部错误"""
    errors: list[str] = []
    available = context.available_policies()

    for ref in context.rule_actions:
        if ref.action is None:
            errors.append(
                f"{ref.source}: 无法解析出策略段（仅支持 TYPE,payload,policy[,modifier] "
                f"与 FINAL,policy[,modifier] 形态），拒绝生成")
            continue
        resolved = resolve_policy_reference(ref.action, context.locale, context.node_names)
        if resolved not in available:
            errors.append(
                f"{ref.source}: 策略「{ref.action}」未被本次生成（不存在或已禁用），不能被引用")

    for ref in context.group_members:
        if ref.member not in available:
            errors.append(
                f"策略组「{ref.group}」的成员「{ref.member}」未被本次生成"
                f"（不存在或已禁用），不能被引用")

    final = (context.final_action or "").strip() or None
    if final is None:
        errors.append("final_action: 档案未设置最终策略（FINAL），拒绝生成")
    else:
        resolved = resolve_policy_reference(final, context.locale, context.node_names)
        if resolved not in available:
            errors.append(
                f"final_action: 最终策略「{final}」未被本次生成（不存在或已禁用），不能被 FINAL 引用")

    if errors:
        raise InvalidConfiguration(errors)


def _split_top_level(rule_text: str) -> list[str]:
    """按括号深度为 0 的逗号切分；括号内的逗号属于 payload（逻辑规则嵌套）"""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in rule_text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _locate_policy_index(segments: list[str]) -> int | None:
    """定位策略段下标（纯函数，不修改 segments；modifier 通过下标回退跳过）。

    只支持本仓库生成/可编辑的形态：
    ``TYPE,payload,policy[,modifier]``、逻辑规则 ``AND/OR/NOT,(...),policy[,modifier]``
    （顶层恰 3 段）、``FINAL,policy[,modifier]``（策略固定第 2 段）。
    其他形态返回 None —— 调用方必须按无效引用处理（fail closed）。
    """
    if not segments:
        return None
    if segments[0] == "FINAL":
        return 1 if len(segments) >= 2 else None
    end = len(segments)
    while end > 0 and segments[end - 1] in _RULE_MODIFIERS:
        end -= 1
    if end != 3:
        return None
    return end - 1


def extract_rule_action(rule_text: str) -> str | None:
    """从 Surge 规则文本提取终结策略段；无法唯一定位时返回 None"""
    segments = [s.strip() for s in _split_top_level(rule_text.strip())]
    idx = _locate_policy_index(segments)
    if idx is None or idx >= len(segments):
        return None
    return segments[idx] or None


def translate_rule_action(rule_text: str, t) -> str | None:
    """把规则文本中的策略段替换为 ``t(policy)``，其余段（含 modifier）原样保留。

    ``t`` 必须与校验同一条解析流（生成器传入 resolve 版翻译：内置动作与
    节点名原样、组名翻译），保证输出的策略名就是被校验的名字。
    返回 None 表示无法定位策略段（与 :func:`extract_rule_action` 同一判定）。
    """
    text = rule_text.strip()
    segments = [s.strip() for s in _split_top_level(text)]
    idx = _locate_policy_index(segments)
    if idx is None or idx >= len(segments) or not segments[idx]:
        return None
    segments[idx] = t(segments[idx])
    return ",".join(segments)


# ===== 规则顺序不变量（稳定身份判定） =====

# 内置规则源的稳定身份：(name, url, rule_type) 三元组。任何身份字段被用户
# 修改过的记录都不再视为内置源，不参与顺序不变量（自定义源永不误伤）；
# 宽泛 substring 匹配（如按名称包含 "AI"）会把用户自定义源误判为内置。
RuleSourceIdentity = tuple[str, str, str]

BUILTIN_RULE_SOURCE_IDENTITIES: frozenset[RuleSourceIdentity] = frozenset(
    (rs["name"], rs["url"], rs["rule_type"]) for rs in DEFAULT_RULE_SOURCES
)


def _order_category_identities(*, category: str | None = None,
                               categories: tuple[str, ...] = (),
                               name: str | None = None) -> frozenset[RuleSourceIdentity]:
    """按 category / 精确名称从内置默认声明挑选身份集合（结构字段驱动）"""
    picked: set[RuleSourceIdentity] = set()
    for rs in DEFAULT_RULE_SOURCES:
        if category is not None and rs["category"] != category:
            continue
        if categories and rs["category"] not in categories:
            continue
        if name is not None and rs["name"] != name:
            continue
        picked.add((rs["name"], rs["url"], rs["rule_type"]))
    return frozenset(picked)


# 不变量类别 → 内置身份集合。用户借用同名/同 category 的自定义记录因 url
# 不同不会命中任何集合。
_ORDER_CATEGORY_IDENTITIES: dict[str, frozenset[RuleSourceIdentity]] = {
    "ai": _order_category_identities(category="ai"),
    "apple": _order_category_identities(category="apple"),
    "google": _order_category_identities(category="google"),
    "global": _order_category_identities(category="global"),
    "douyin": _order_category_identities(name="DouYin"),
    "tiktok": _order_category_identities(name="TikTok"),
    "domestic": _order_category_identities(categories=("mainland", "mainland_app")),
}

_ORDER_CATEGORY_LABELS = {
    "ai": "AI 服务", "apple": "Apple", "google": "Google", "global": "Global 兜底",
    "douyin": "抖音直连", "tiktok": "TikTok", "domestic": "国内直连",
}

# 顺序不变量：(earlier 类别, later 类别, 冲突原因说明)。
# 生成、迁移与 reorder/update 端点共同执行的同一条规则。
RULE_ORDER_INVARIANTS: tuple[tuple[str, str, str], ...] = (
    ("ai", "apple", "AI 服务域名与 Apple 规则存在重叠，AI 规则须先行匹配"),
    ("ai", "google", "AI 服务域名与 Google 规则存在重叠（如 Gemini），AI 规则须先行匹配"),
    ("ai", "global", "AI 服务域名与 Global 兜底规则存在重叠，AI 规则须先行匹配"),
    ("douyin", "tiktok", "抖音与 TikTok 共用 snssdk.com 等字节系域名，抖音直连须先行匹配"),
    ("domestic", "global", "国内直连域名与 Global 兜底规则存在重叠，国内规则须先行匹配"),
)


def rule_source_category_positions(ordered) -> dict[str, list[tuple[int, object]]]:
    """按 (sort_order, id) 稳定序为每个不变量类别登记 (位置, 记录)。

    只登记与内置稳定身份完全匹配的记录；身份字段被改过或用户自定义的
    源一律不参与。sort_order 为 None（绕过 DB 约束构造的对象）时
    fail closed 抛 InvalidConfiguration，绝不让排序比较退化成 TypeError。
    """
    for rs in ordered:
        if rs.sort_order is None:
            raise InvalidConfiguration(
                [f"规则源「{rs.name}」缺少 sort_order（NULL），无法确定稳定顺序，拒绝生成"])
    sequence = sorted(ordered, key=lambda rs: (rs.sort_order, rs.id))
    positions: dict[str, list[tuple[int, object]]] = {}
    for index, rs in enumerate(sequence):
        identity = (rs.name, rs.url, rs.rule_type)
        if identity not in BUILTIN_RULE_SOURCE_IDENTITIES:
            continue
        for category, identities in _ORDER_CATEGORY_IDENTITIES.items():
            if identity in identities:
                positions.setdefault(category, []).append((index, rs))
    return positions


def rule_order_violations(ordered) -> list[str]:
    """检查完整 enabled 候选序列（按 (sort_order, id) 稳定序）的顺序不变量。

    返回全部违例描述（中文、含双方源名与 sort_order）；同一不变量两侧
    任一缺失时不报告——可选源缺席不构成违例。
    """
    positions = rule_source_category_positions(ordered)
    errors: list[str] = []
    for earlier, later, reason in RULE_ORDER_INVARIANTS:
        earlier_entries = positions.get(earlier)
        later_entries = positions.get(later)
        if not earlier_entries or not later_entries:
            continue
        earlier_last_index, earlier_last = max(earlier_entries, key=lambda item: item[0])
        later_first_index, later_first = min(later_entries, key=lambda item: item[0])
        if earlier_last_index >= later_first_index:
            errors.append(
                f"规则顺序违反不变量：{_ORDER_CATEGORY_LABELS[earlier]}规则源"
                f"「{earlier_last.name}」(sort_order={earlier_last.sort_order}) 须先于 "
                f"{_ORDER_CATEGORY_LABELS[later]}规则源「{later_first.name}」"
                f"(sort_order={later_first.sort_order})；{reason}")
    return errors


def validate_rule_order(ordered) -> None:
    """聚合校验规则源顺序不变量；一次抛出全部错误（InvalidConfiguration）。"""
    errors = rule_order_violations(ordered)
    if errors:
        raise InvalidConfiguration(errors)


# ===== 规则目标能力（结构化/括号感知检测） =====

# Mac 专属规则类型：PROCESS-* 前缀族（进程名/路径等）与 MAC-ADDRESS。
# iOS Surge 不支持按 Mac 进程或网卡地址匹配；检测只看类型段，
# payload 子串（如域名 process-name.example.com）不受影响。
MAC_ONLY_RULE_TYPE_PREFIXES = ("PROCESS-",)
MAC_ONLY_RULE_TYPES = frozenset({"MAC-ADDRESS"})
LOGICAL_RULE_TYPES = frozenset({"AND", "OR", "NOT"})


def _is_mac_only_rule_type(rule_type: str) -> bool:
    return (rule_type in MAC_ONLY_RULE_TYPES
            or any(rule_type.startswith(p) for p in MAC_ONLY_RULE_TYPE_PREFIXES))


def _is_wrapped(text: str) -> bool:
    """首尾括号是否配对在同一层（包裹整段），避免误剥 ``(a),(b)`` 形态"""
    if not (text.startswith("(") and text.endswith(")")):
        return False
    depth = 0
    for i, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and i != len(text) - 1:
                return False
    return depth == 0


def _contains_mac_only_rule(fragment: str) -> bool:
    """在规则片段（顶层规则或逻辑 payload）中递归检测 Mac 专属构造。

    结构化解析：剥掉包裹整段的平衡括号后按深度 0 逗号切分，检查类型段；
    逻辑规则（AND/OR/NOT）的 payload 段继续递归（支持多层嵌套）。
    """
    text = fragment.strip()
    if not text:
        return False
    if _is_wrapped(text):
        inner = text[1:-1].strip()
        # 剥层后是多段（逻辑 payload 的子规则列表）：每段递归
        return any(_contains_mac_only_rule(s) for s in _split_top_level(inner) if s.strip())
    segments = [s.strip() for s in _split_top_level(text)]
    if not segments:
        return False
    head = segments[0]
    if _is_mac_only_rule_type(head):
        return True
    if head in LOGICAL_RULE_TYPES and len(segments) >= 2:
        return _contains_mac_only_rule(segments[1])
    return False


def rule_uses_mac_only_constructs(rule_text: str) -> bool:
    """规则是否使用 Mac 专属构造（PROCESS-* / MAC-ADDRESS，含逻辑嵌套）"""
    return _contains_mac_only_rule(rule_text)


def rule_excluded_for_target(rule_text: str, target: str) -> bool:
    """规则是否因目标能力被排除出产物（当前仅 ios_mobile 排除 Mac 专属构造）"""
    if target == "ios_mobile":
        return rule_uses_mac_only_constructs(rule_text)
    return False


# ===== FINAL 生成器专属判定 =====

def is_final_rule(rule_text: str) -> bool:
    """规则文本是否为 FINAL 规则（顶层类型段恰为 FINAL，结构化判定）。

    FINAL 是生成器按 ConfigProfile.final_action 追加的终结规则，任何
    pre/general 自定义 FINAL 都在保存端与生成校验端被拒绝。
    """
    segments = [s.strip() for s in _split_top_level(rule_text.strip())]
    return bool(segments) and segments[0] == "FINAL"
