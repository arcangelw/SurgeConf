"""订阅节点稳定身份：不含明文的连接规范化摘要

目的：同一订阅内跨同步识别"同一节点"，让 upsert/去重不依赖显示名。

解析模型（与 Surge Profile Format 对齐）：
- 分隔定位：以 ``node_data["name"]`` 精确匹配行首显示名，其后只允许
  空白，随后必须是 ``=``；其右为连接表达。显示名可包含 ``=``
  （URI fragment 解码产物），因此绝不使用"首个 ``=``"切分。
- 词法：引号感知。引号内 ``\\"`` 与 ``\\\\`` 为转义（其他转义序列与
  尾随 ``\\`` 一律失败）；引号内逗号属于 token（quoted comma）。
  引号外且前一个字符为 ASCII 空格的 ``#``、``;``、``//`` 开始
  inline comment（其后全部忽略）；``https://`` 等无前导空格内容不是
  注释；引号内的注释字符是普通字符。
- 规范化：协议名与主机名小写；端口按整数规范（``0443`` ≡ ``443``）；
  含 ``=`` 的参数视为 key=value，按 (key, value) 排序参与（顺序无关）；
  不含 ``=`` 的参数保持出现顺序参与；重复（大小写归一后的）参数名
  fail closed；成对外层引号剥除；认证参数参与摘要但明文不持久化。
- 主机规范：IPv6/IPv4 文本经 :mod:`ipaddress` 标准压缩（去 ``[...]``
  方括号）；非 ASCII 域名先 NFC 再 IDNA punycode 再小写；单一尾点
  视为 DNS 绝对名标记，去除后等价。

安全不变量：config/name 缺失或类型错误、名称不匹配、无连接表达、
空 token、空参数名、重复参数名、未闭合引号、未知/尾随转义、无效
端口、非法控制字符一律抛 :class:`NodeIdentityError`，异常消息为静态
文本，绝不回显 name/config/host/value。

规范形态（冻结）：协议、主机、端口、positional（按序）、排序后的
``key=value``，以 ``\\n`` 连接后 UTF-8 编码取 SHA-256 十六进制摘要。

``migrations/versions/0006_subscription_lifecycle.py`` 内嵌本算法的
冻结副本与 golden vector，二者必须保持行为一致；此处演进不影响
历史迁移结果（迁移携带自身冻结版）。
"""

import hashlib
import ipaddress
import unicodedata
from collections.abc import Mapping

__all__ = ["NodeIdentityError", "stable_node_key"]

_MAX_PORT = 65535
_FORBIDDEN_CHARS = ("\r", "\n", "\0")


class NodeIdentityError(ValueError):
    """节点连接配置无法规范化（消息为静态文本，不含 name/config 内容）"""


def _split_tokens(text: str) -> list[str]:
    """转义与注释感知的逗号分割。

    引号内逗号属于 token；引号内 ``\\"``/``\\\\`` 解码；引号外且前一
    字符为 ASCII 空格的 ``#``/``;``/``//`` 开始 inline comment（丢弃其
    后全部内容）。解码后的字符参与 token（外层引号对由
    :func:`_unquote` 剥除）。
    """
    tokens: list[str] = []
    buf: list[str] = []
    in_quotes = False
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if in_quotes:
            if char == "\\":
                if index + 1 >= length:
                    raise NodeIdentityError("节点连接参数包含尾随转义符")
                nxt = text[index + 1]
                if nxt == '"':
                    buf.append('"')
                elif nxt == "\\":
                    buf.append("\\")
                else:
                    raise NodeIdentityError("节点连接参数包含未知转义序列")
                index += 2
                continue
            if char == '"':
                in_quotes = False
            buf.append(char)
        else:
            if char == '"':
                in_quotes = True
                buf.append(char)
            elif char == ",":
                tokens.append("".join(buf))
                buf = []
            elif char in "#;" and index > 0 and text[index - 1] == " ":
                break  # inline comment：丢弃其余内容
            elif (char == "/" and index + 1 < length and text[index + 1] == "/"
                    and index > 0 and text[index - 1] == " "):
                break
            else:
                buf.append(char)
        index += 1
    if in_quotes:
        raise NodeIdentityError("节点连接参数包含未闭合引号")
    tokens.append("".join(buf))
    return tokens


def _unquote(token: str) -> str:
    """去除包裹整个（已解码）token 的成对外层引号（内层引号保留原样）"""
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


def _strip_fqdn_dot(host: str) -> str:
    """移除单一 FQDN 尾点；空域与多重尾点 fail closed"""
    if not host:
        raise NodeIdentityError("节点主机名格式非法")
    if not host.endswith("."):
        return host
    stripped = host[:-1]
    if not stripped or stripped.endswith("."):
        raise NodeIdentityError("节点主机名格式非法")
    return stripped


def _normalize_host(raw: str) -> str:
    """主机规范化：IP 文本标准压缩；非 ASCII 域名 NFC→IDNA→小写；
    单一尾点（含 IDNA 归一出的 U+3002/U+FF0E/U+FF61 根分隔符）去除"""
    host = raw
    if any(ch.isspace() for ch in host):
        raise NodeIdentityError("节点主机名格式非法")
    if host.startswith("[") and host.endswith("]") and len(host) > 2:
        host = host[1:-1]  # IPv6 字面量方括号
    if ":" in host:
        try:
            return str(ipaddress.ip_address(host))
        except ValueError:
            raise NodeIdentityError("节点主机名格式非法") from None
    try:
        return str(ipaddress.IPv4Address(host))
    except ValueError:
        pass  # 非点分十进制 IPv4，按域名处理
    if host.isascii():
        return _strip_fqdn_dot(host).lower()
    normalized = unicodedata.normalize("NFC", host)
    try:
        # IDNA 把 U+3002/U+FF0E/U+FF61 统一为 ASCII "."，尾点剥除必须
        # 在 ASCII 结果上进行，否则 Unicode 尾点与 ASCII 尾点身份分裂
        return _strip_fqdn_dot(normalized.encode("idna").decode("ascii").lower())
    except (UnicodeError, ValueError):
        raise NodeIdentityError("节点主机名格式非法") from None


def _is_blank(value: object) -> bool:
    return not isinstance(value, str) or not value.strip()


def _extract_expression(config: str, name: str | None) -> str:
    """以显示名精确定位 ``name 空白* =`` 分隔，返回其右的连接表达。

    显示名可包含 ``=``（URI fragment 解码产物），因此不能按"首个 ``=``"
    切分；名称不匹配或缺少分隔符均失败。
    """
    if _is_blank(name):
        raise NodeIdentityError("节点缺少显示名")
    assert isinstance(name, str)  # _is_blank 已排除非 str
    if not config.startswith(name):
        raise NodeIdentityError("节点连接配置与显示名不匹配")
    rest = config[len(name):]
    index = 0
    while index < len(rest) and rest[index] in (" ", "\t"):
        index += 1
    if index >= len(rest) or rest[index] != "=":
        raise NodeIdentityError("节点连接配置缺少连接表达")
    expression = rest[index + 1:]
    if not expression.strip():
        raise NodeIdentityError("节点连接配置缺少连接表达")
    return expression


def canonical_connection_bytes(node_data: Mapping[str, object]) -> bytes:
    """node_data（含 ``name`` 与 ``config`` 键）→ 规范化连接字节流（不落盘）"""
    if not isinstance(node_data, Mapping):
        raise NodeIdentityError("节点数据必须是映射类型")
    config = node_data.get("config")
    if _is_blank(config):
        raise NodeIdentityError("节点缺少连接配置")
    assert isinstance(config, str)  # _is_blank 已排除非 str
    if any(char in config for char in _FORBIDDEN_CHARS):
        raise NodeIdentityError("节点连接配置包含非法控制字符")

    expression = _extract_expression(config, node_data.get("name"))

    tokens = [_normalize_token(t) for t in _split_tokens(expression)]
    if len(tokens) < 3:
        raise NodeIdentityError("节点连接参数不完整")

    protocol = tokens[0]
    if any(ch.isspace() for ch in protocol):
        raise NodeIdentityError("节点协议名格式非法")
    protocol = protocol.lower()

    host = _normalize_host(tokens[1])

    port = _normalize_port(tokens[2])

    positional: list[str] = []
    key_values: list[tuple[str, str]] = []
    seen_keys: set[str] = set()
    for token in tokens[3:]:
        if "=" in token:
            key, _, value = token.partition("=")
            key = key.strip().lower()
            if not key or any(ch.isspace() for ch in key):
                raise NodeIdentityError("节点参数名格式非法")
            if key in seen_keys:
                raise NodeIdentityError("节点连接参数存在重复参数名")
            seen_keys.add(key)
            key_values.append((key, _normalize_token(value)))
        else:
            positional.append(token)

    parts = [protocol, host, port, *positional,
             *(f"{key}={value}" for key, value in sorted(key_values))]
    return "\n".join(parts).encode("utf-8")


def stable_node_key(node_data: Mapping[str, object]) -> str:
    """节点连接的稳定身份：规范化字节流的 SHA-256 十六进制摘要（64 字符）"""
    return hashlib.sha256(canonical_connection_bytes(node_data)).hexdigest()
