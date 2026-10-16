"""订阅节点稳定身份：不含明文的连接规范化摘要

目的：同一订阅内跨同步识别"同一节点"，让 upsert/去重不依赖显示名。
取节点 config 首个 ``=`` 之后的连接表达做规范化：

- 忽略显示名（``=`` 之前的一切）；
- 协议名与主机名小写、端口按整数规范（``0443`` ≡ ``443``）；
- 含 ``=`` 的参数视为 key=value，按 (key, value) 排序参与（顺序无关）；
- 不含 ``=`` 的 positional 参数保持出现顺序参与；
- 引号包裹的值支持内嵌逗号（quoted comma），成对外层引号不参与摘要；
- 认证参数（密码等）与全部其余参数一样参与摘要，但中间明文绝不持久化。

安全不变量：config 缺失/类型错误、无连接表达、空 token、未闭合引号、
无效端口、CRLF/NUL 注入一律抛 :class:`NodeIdentityError`，异常消息是
静态文本，绝不回显 config 原文。

规范形态（冻结）：协议、主机、端口、positional（按序）、排序后的
``key=value``，以 ``\\n`` 连接后 UTF-8 编码取 SHA-256 十六进制摘要
（token 内不可能出现换行——CRLF 已被拒绝）。

``migrations/versions/0006_subscription_lifecycle.py`` 内嵌本算法的
冻结副本与 golden vector，二者必须保持字节级一致；此处演进不影响
历史迁移结果。
"""

import hashlib
from collections.abc import Mapping

__all__ = ["NodeIdentityError", "stable_node_key"]

_MAX_PORT = 65535
_FORBIDDEN_CHARS = ("\r", "\n", "\0")


class NodeIdentityError(ValueError):
    """节点连接配置无法规范化（消息为静态文本，不含 config 内容）"""


def _split_tokens(text: str) -> list[str]:
    """引号感知的逗号分割：引号内逗号属于 token 一部分"""
    tokens: list[str] = []
    buf: list[str] = []
    in_quotes = False
    for char in text:
        if char == '"':
            in_quotes = not in_quotes
            buf.append(char)
        elif char == "," and not in_quotes:
            tokens.append("".join(buf))
            buf = []
        else:
            buf.append(char)
    if in_quotes:
        raise NodeIdentityError("节点连接参数包含未闭合引号")
    tokens.append("".join(buf))
    return tokens


def _unquote(token: str) -> str:
    """去除包裹整个 token 的成对外层引号（内层引号保留原样）"""
    if len(token) >= 2 and token.startswith('"') and token.endswith('"'):
        return token[1:-1]
    return token


def _normalize_port(raw: str) -> str:
    if not raw:
        raise NodeIdentityError("节点连接缺少端口")
    try:
        port = int(raw, 10)
    except ValueError:
        raise NodeIdentityError("节点连接端口不是合法整数") from None
    if not 1 <= port <= _MAX_PORT:
        raise NodeIdentityError("节点连接端口超出有效范围")
    return str(port)


def _normalize_token(raw: str) -> str:
    token = _unquote(raw.strip())
    if not token:
        raise NodeIdentityError("节点连接包含空参数")
    return token


def _is_blank(value: object) -> bool:
    return not isinstance(value, str) or not value.strip()


def canonical_connection_bytes(node_data: Mapping[str, object]) -> bytes:
    """node_data（含 ``config`` 键）→ 规范化连接字节流（摘要输入，不落盘）"""
    if not isinstance(node_data, Mapping):
        raise NodeIdentityError("节点数据必须是映射类型")
    config = node_data.get("config")
    if _is_blank(config):
        raise NodeIdentityError("节点缺少连接配置")
    assert isinstance(config, str)  # _is_blank 已排除非 str
    if any(char in config for char in _FORBIDDEN_CHARS):
        raise NodeIdentityError("节点连接配置包含非法控制字符")

    if "=" not in config:
        raise NodeIdentityError("节点连接配置缺少连接表达")
    _, _, expression = config.partition("=")
    if not expression.strip():
        raise NodeIdentityError("节点连接配置缺少连接表达")

    tokens = [_normalize_token(t) for t in _split_tokens(expression)]
    if len(tokens) < 3:
        raise NodeIdentityError("节点连接参数不完整")

    protocol = tokens[0]
    if any(ch.isspace() for ch in protocol):
        raise NodeIdentityError("节点协议名格式非法")
    protocol = protocol.lower()

    host = tokens[1]
    if any(ch.isspace() for ch in host):
        raise NodeIdentityError("节点主机名格式非法")
    host = host.lower()

    port = _normalize_port(tokens[2])

    positional: list[str] = []
    key_values: list[tuple[str, str]] = []
    for token in tokens[3:]:
        if "=" in token:
            key, _, value = token.partition("=")
            key = key.strip().lower()
            if not key or any(ch.isspace() for ch in key):
                raise NodeIdentityError("节点参数名格式非法")
            key_values.append((key, _normalize_token(value)))
        else:
            positional.append(token)

    parts = [protocol, host, port, *positional,
             *(f"{key}={value}" for key, value in sorted(key_values))]
    return "\n".join(parts).encode("utf-8")


def stable_node_key(node_data: Mapping[str, object]) -> str:
    """节点连接的稳定身份：规范化字节流的 SHA-256 十六进制摘要（64 字符）"""
    return hashlib.sha256(canonical_connection_bytes(node_data)).hexdigest()
