"""托管 UDP 安全规则的纯函数构建器（Task 4）。

build_udp_rules(target, mode, exceptions) 按档案 UDP 安全模式生成
项目管理的规则行（绝不持久化为 CustomRule）：

- compatible：不注入任何托管行；用户自建 CustomRule 照常保留
- strict：仅注入四条 MANAGED_STUN_RULES；合法例外被忽略但不删除
  （切回 scoped 后恢复输出）
- scoped：enabled 例外按 (sort_order, id) 稳定序生成 DIRECT 行，
  随后注入四条 STUN 阻断

任何模式都重新校验传入的 enabled 例外（类型与目标兼容、值可安全输出）：
绕库直写的非法/不兼容值在此 fail closed（InvalidConfiguration）。
错误只含 id / match_type 定位信息，绝不回显 match_value（敏感值）。
"""

from app.default_config import MANAGED_STUN_RULES
from app.profile_targets import ProfileTarget, UdpSecurityMode, validate_target_mode
from app.services.config_validation import InvalidConfiguration
from app.udp_policy import UdpMatchType, normalize_udp_match

# 需要双引号包裹的原子字符：空白与 Surge 行结构字符。逗号/双引号/控制
# 字符不走引号转义——Surge 原子内无法转义它们，由值校验直接拒绝。
_ATOM_QUOTE_CHARS = frozenset(" \t,()[]#;=")


def quote_rule_atom(value: str) -> str:
    """含空格 / Surge 结构字符的合法原子加双引号，其余原样输出。

    输入必须是已通过 normalize_udp_match 的合法值（无逗号/引号/换行）。
    """
    if any(ch in _ATOM_QUOTE_CHARS for ch in value):
        return f'"{value}"'
    return value


def managed_udp_line_action(line: str) -> str:
    """托管行的固定动作（行由本模块拼接，动作恒为末段 DIRECT/REJECT）。

    供生成器把托管动作登记进 GenerationContext.rule_actions 做完整性
    校验；这不是对用户输入的解析。
    """
    return line.rsplit(",", 1)[1]


def _direct_line(match_type: UdpMatchType, normalized_value: str) -> str:
    if match_type is UdpMatchType.PROCESS:
        atom = f"PROCESS-NAME,{quote_rule_atom(normalized_value)}"
    else:
        # MAC 生成复用 normalizer：只输出规范大写冒号形式
        atom = f"MAC-ADDRESS,{normalized_value}"
    return f"AND,(({atom}),(PROTOCOL,UDP)),DIRECT"


def _exception_label(item) -> str:
    item_id = getattr(item, "id", None)
    return f"UDP 例外 #{item_id if item_id is not None else '?'}（match_type={item.match_type}）"


def build_udp_rules(target, mode, exceptions) -> list[str]:
    """按目标 × 模式矩阵构建托管 UDP 规则行（纯函数，无 IO）。

    - target / mode 接受 str 或枚举；未知值与 ios_mobile+scoped 组合
      包装为 InvalidConfiguration fail closed
    - exceptions 为任意带 id/match_type/match_value/enabled/sort_order
      属性的对象序列（ORM 实例或等价桩）；disabled 项完全忽略
    - 所有模式都校验 enabled 例外：类型与目标不兼容、值含分隔/引号/
      换行/控制字符（DB 绕过）一律 InvalidConfiguration，错误不回显值
    """
    try:
        target = ProfileTarget(target)
        mode = UdpSecurityMode(mode)
        validate_target_mode(target, mode)
    except ValueError as exc:
        raise InvalidConfiguration([f"UDP 安全规则: {exc}"]) from exc

    errors: list[str] = []
    normalized: list[tuple[tuple[int, int], UdpMatchType, str]] = []
    for item in exceptions:
        if not item.enabled:
            continue
        label = _exception_label(item)
        try:
            match_type = UdpMatchType(item.match_type)
        except ValueError:
            errors.append(f"{label}: 未知匹配类型，拒绝生成")
            continue
        try:
            value = normalize_udp_match(target, match_type, item.match_value)
        except ValueError as exc:
            errors.append(f"{label}: {exc}")
            continue
        normalized.append(((item.sort_order, item.id), match_type, value))

    if errors:
        raise InvalidConfiguration(errors)

    if mode is UdpSecurityMode.COMPATIBLE:
        return []
    if mode is UdpSecurityMode.SCOPED:
        # (sort_order, id) 稳定序：查询层已排序，纯函数不信任传入顺序
        lines = [_direct_line(mt, value)
                 for _, mt, value in sorted(normalized, key=lambda pair: pair[0])]
    else:  # strict：合法例外忽略不输出（保留在库，切回 scoped 恢复）
        lines = []
    lines.extend(MANAGED_STUN_RULES)
    return lines
