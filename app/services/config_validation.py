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

from app.default_config import locale_translate

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
