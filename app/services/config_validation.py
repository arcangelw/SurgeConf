"""生成配置引用完整性校验。

生成器输出任何字节前，校验全部规则 action 与 ``final_action`` 引用的策略
确实存在于本次输出（节点 / 地区组 / 服务组 / 组合组 / Surge 内置动作）。
任何无效引用聚合为 :class:`InvalidConfiguration` 一次抛出，绝不产出部分配置。

canonical 命名流：数据库存逻辑名（中文），输出与校验共用
:func:`translate_policy_name` 翻译到目标 locale 后比较——校验的名字
就是输出的名字，不做任何模糊或反向映射回退。
"""

from dataclasses import dataclass

from app.default_config import locale_translate

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
    """一条已输出规则的终结策略引用。

    action 为 None 表示无法从规则文本解析出策略段（fail closed：
    校验器将其按无效引用处理，而不是猜测或跳过）。
    """

    source: str  # 人类可读来源标签，用于错误定位
    action: str | None


@dataclass
class GenerationContext:
    """一次生成的完整引用视图。

    名字集合全部来自本次实际输出：节点显示名（未被信息关键词过滤）、
    实际生成的地区/服务/组合组名。disabled、被过滤、未生成的项不算存在。
    """

    locale: str
    target: str = ""
    node_names: frozenset[str] = frozenset()
    region_group_names: frozenset[str] = frozenset()
    service_group_names: frozenset[str] = frozenset()
    combined_group_names: frozenset[str] = frozenset()
    rule_actions: tuple[RuleActionRef, ...] = ()
    final_action: str | None = None

    def available_policies(self) -> frozenset[str]:
        """规则可引用的完整策略集合（已翻译到目标 locale）+ 内置动作"""
        names = set(BUILTIN_ACTIONS)
        for group in (self.node_names, self.region_group_names,
                      self.service_group_names, self.combined_group_names):
            names.update(translate_policy_name(n, self.locale) for n in group)
        return frozenset(names)


def validate_generated_references(context: GenerationContext) -> None:
    """聚合校验全部规则引用；任一无效抛 InvalidConfiguration（错误一次全给）"""
    errors: list[str] = []
    available = context.available_policies()

    for ref in context.rule_actions:
        if ref.action is None:
            errors.append(
                f"{ref.source}: 无法解析出策略段（仅支持 TYPE,payload,policy[,modifier] "
                f"与 FINAL,policy[,modifier] 形态），拒绝生成")
            continue
        if translate_policy_name(ref.action, context.locale) not in available:
            errors.append(
                f"{ref.source}: 策略「{ref.action}」未被本次生成（不存在或已禁用），不能被引用")

    final = (context.final_action or "").strip() or None
    if final is None:
        errors.append("final_action: 档案未设置最终策略（FINAL），拒绝生成")
    elif translate_policy_name(final, context.locale) not in available:
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

    返回 None 表示无法定位策略段（与 :func:`extract_rule_action` 同一判定）。
    """
    text = rule_text.strip()
    segments = [s.strip() for s in _split_top_level(text)]
    idx = _locate_policy_index(segments)
    if idx is None or idx >= len(segments) or not segments[idx]:
        return None
    segments[idx] = t(segments[idx])
    return ",".join(segments)
