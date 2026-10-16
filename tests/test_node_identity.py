"""stable_node_key 节点稳定身份测试

不变量：忽略显示名；协议/主机大小写与端口整数形态归一；key=value 参数
与顺序无关（排序后参与）；positional 参数保持出现顺序；认证参数参与
摘要；非法/不完整连接配置静态失败且异常不回显原文。
"""

import pytest

from app.services.node_identity import NodeIdentityError, stable_node_key


def parsed_node(name: str, config: str) -> dict:
    """与运行时 _parse_surge_line 相同形态的 node_data（含 name/config）"""
    return {"name": name, "config": config}


# ===== 等价性 =====

def test_stable_key_ignores_display_name():
    before = parsed_node(name="US A", config="US A = trojan, edge.example.com, 443, password=synthetic")
    after = parsed_node(name="US Renamed", config="US Renamed = trojan, edge.example.com, 443, password=synthetic")
    assert stable_node_key(before) == stable_node_key(after)


def test_stable_key_normalizes_whitespace_and_case():
    tight = parsed_node(name="A", config="A=trojan,edge.example.com,443,password=x")
    spaced = parsed_node(name="B", config="B = TROJAN ,  EDGE.Example.COM , 443 , Password = x ")
    assert stable_node_key(tight) == stable_node_key(spaced)


def test_stable_key_normalizes_port_integer_forms():
    padded = parsed_node(name="A", config="A = trojan, edge.example.com, 0443, password=x")
    plain = parsed_node(name="B", config="B = trojan, edge.example.com, 443, password=x")
    assert stable_node_key(padded) == stable_node_key(plain)


def test_stable_key_parameter_order_is_irrelevant():
    first = parsed_node(name="A", config="A = trojan, edge.example.com, 443, password=x, sni=example.com")
    second = parsed_node(name="B", config="B = trojan, edge.example.com, 443, sni=example.com, password=x")
    assert stable_node_key(first) == stable_node_key(second)


def test_stable_key_equivalent_for_quoted_comma_in_any_position():
    first = parsed_node(name="A", config='A = trojan, edge.example.com, 443, password="x,y", sni=a.example')
    second = parsed_node(name="B", config='B = trojan, edge.example.com, 443, sni=a.example, password="x,y"')
    assert stable_node_key(first) == stable_node_key(second)


def test_stable_key_quotes_do_not_change_value_identity():
    quoted = parsed_node(name="A", config='A = trojan, edge.example.com, 443, password="secret"')
    bare = parsed_node(name="B", config="B = trojan, edge.example.com, 443, password=secret")
    assert stable_node_key(quoted) == stable_node_key(bare)


# ===== 区分性 =====

def test_stable_key_distinguishes_credentials():
    first = parsed_node(name="A", config="A = trojan, edge.example.com, 443, password=synthetic-a")
    second = parsed_node(name="B", config="B = trojan, edge.example.com, 443, password=synthetic-b")
    assert stable_node_key(first) != stable_node_key(second)


def test_stable_key_distinguishes_host():
    first = parsed_node(name="A", config="A = trojan, edge.example.com, 443, password=x")
    second = parsed_node(name="B", config="B = trojan, edge.example.net, 443, password=x")
    assert stable_node_key(first) != stable_node_key(second)


def test_stable_key_distinguishes_port():
    first = parsed_node(name="A", config="A = trojan, edge.example.com, 443, password=x")
    second = parsed_node(name="B", config="B = trojan, edge.example.com, 8443, password=x")
    assert stable_node_key(first) != stable_node_key(second)


def test_stable_key_distinguishes_protocol():
    first = parsed_node(name="A", config="A = trojan, edge.example.com, 443, password=x")
    second = parsed_node(name="B", config="B = hysteria2, edge.example.com, 443, password=x")
    assert stable_node_key(first) != stable_node_key(second)


def test_stable_key_distinguishes_positional_order():
    first = parsed_node(name="A", config="A = ss, edge.example.com, 443, aes-128-gcm, rc4-md5")
    second = parsed_node(name="B", config="B = ss, edge.example.com, 443, rc4-md5, aes-128-gcm")
    assert stable_node_key(first) != stable_node_key(second)


def test_stable_key_ignores_relative_position_of_keyed_and_positional_params():
    first = parsed_node(name="A", config="A = ss, edge.example.com, 443, aes-256-gcm, password=x")
    second = parsed_node(name="B", config="B = ss, edge.example.com, 443, password=x, aes-256-gcm")
    assert stable_node_key(first) == stable_node_key(second)


def test_stable_key_distinguishes_subscription_scopes_same_config_text():
    """同一连接文本在不同订阅下 key 相同：身份属于连接本身（用于订阅内去重）"""
    first = {"name": "A", "config": "A = trojan, edge.example.com, 443, password=x"}
    assert stable_node_key(first) == stable_node_key(dict(first))


# ===== 输出形态 =====

def test_stable_key_is_64_char_lowercase_hex():
    key = stable_node_key(parsed_node(name="A", config="A = trojan, edge.example.com, 443, password=x"))
    assert len(key) == 64
    assert all(c in "0123456789abcdef" for c in key)


def test_stable_key_is_deterministic():
    node = parsed_node(name="A", config="A = trojan, edge.example.com, 443, password=x")
    assert stable_node_key(node) == stable_node_key(dict(node))


# ===== fail closed：非法输入静态失败且不回显原文 =====

@pytest.mark.parametrize("config", [
    "no connection expression",            # 无 =
    "A = ",                                 # 空连接表达
    "A = trojan",                           # 缺 host/port
    "A = trojan, edge.example.com",         # 缺 port
    "A = trojan, edge.example.com, port",   # 端口非数字
    "A = trojan, edge.example.com, 0",      # 端口越界（低）
    "A = trojan, edge.example.com, 65536",  # 端口越界（高）
    "A = trojan, edge.example.com, -443",   # 端口为负
    "A = trojan, , 443",                    # 空 host
    "A = , edge.example.com, 443",          # 空协议
    "A = trojan, edge.example.com, 443, , password=x",   # 空 token
    'A = trojan, edge.example.com, 443, password="x',    # 未闭合引号
])
def test_invalid_config_fails_closed(config):
    with pytest.raises(NodeIdentityError):
        stable_node_key({"name": "A", "config": config})


@pytest.mark.parametrize("config", [
    "A = trojan, edge.example.com, 443, password=x\r\nB = ss, 1.1.1.1, 1",
    "A = trojan, edge.example.com, 443, password=x\n",
    "A = trojan, edge.example.com, 443, password=x\0",
])
def test_crlf_and_nul_injection_fails_closed(config):
    with pytest.raises(NodeIdentityError):
        stable_node_key({"name": "A", "config": config})


@pytest.mark.parametrize("node_data", [
    {},
    {"name": "A"},
    {"name": "A", "config": None},
    {"name": "A", "config": 123},
])
def test_missing_or_non_string_config_fails_closed(node_data):
    with pytest.raises(NodeIdentityError):
        stable_node_key(node_data)


def test_failure_messages_never_echo_config():
    secret = "s3cret-credential-value"
    config = f"A = trojan, edge.example.com, notaport, password={secret}"
    with pytest.raises(NodeIdentityError) as excinfo:
        stable_node_key({"name": "A", "config": config})
    assert secret not in str(excinfo.value)
    assert "edge.example.com" not in str(excinfo.value)


def test_failure_messages_never_echo_config_on_crlf():
    config = "A = trojan, edge.example.com, 443, password=x\r\ninjected"
    with pytest.raises(NodeIdentityError) as excinfo:
        stable_node_key({"name": "A", "config": config})
    assert "injected" not in str(excinfo.value)


# ===== Fix Round 1：重复参数名 / name 含 = / 转义与注释 / 主机规范 =====

import pytest

from app.services.node_identity import canonical_connection_bytes


# --- 重复（大小写归一后的）参数名：fail closed ---

@pytest.mark.parametrize("config", [
    "n = trojan, h.example.com, 443, sni=x, sni=y",
    "n = trojan, h.example.com, 443, sni=x, SNI=y",
    "n = trojan, h.example.com, 443, SNI=x, sni=y",
    'n = trojan, h.example.com, 443, sni="x", sni=y',
])
def test_duplicate_parameter_key_fails_closed(config):
    with pytest.raises(NodeIdentityError):
        stable_node_key({"name": "n", "config": config})


def test_duplicate_parameter_key_failure_does_not_echo_values():
    with pytest.raises(NodeIdentityError) as excinfo:
        stable_node_key({"name": "n",
                         "config": "n = trojan, h.example.com, 443, secret-a=1, SECRET-B=2, secret-a=3"})
    assert "secret-a" not in str(excinfo.value)


def test_distinct_parameter_keys_still_allowed():
    node = parsed_node(name="n", config="n = trojan, h.example.com, 443, sni=x, skip-cert-verify=true")
    assert stable_node_key(node)


# --- 显示名含 =（URI fragment 解码产物）：按 name 精确定位分隔 ---

def test_stable_key_supports_name_containing_equals():
    first = parsed_node(name="edge=a=b", config="edge=a=b = trojan, edge.example.com, 443, password=x")
    second = parsed_node(name="renamed", config="renamed = TROJAN, EDGE.example.com, 0443, password=x")
    assert stable_node_key(first) == stable_node_key(second)


def test_name_mismatch_fails_closed():
    with pytest.raises(NodeIdentityError):
        stable_node_key({"name": "wrong", "config": "n = trojan, h.example.com, 443, password=x"})


def test_missing_separator_after_name_fails_closed():
    with pytest.raises(NodeIdentityError):
        stable_node_key({"name": "n", "config": "n trojan, h.example.com, 443"})


def test_uri_fragment_encoded_name_flows_to_identity():
    """URI fragment %3D → parser 产出 name 含 = → identity 按 name 提取连接"""
    from app.services.subscription import _parse_uri

    node = _parse_uri("trojan://pw@edge.example.com:443?sni=edge.example.com#edge%3Da%3Db")
    assert node is not None
    assert node["name"] == "edge=a=b"
    key = stable_node_key(node)
    manual = parsed_node(name="x", config="x = trojan, edge.example.com, 443, password=pw, sni=edge.example.com")
    assert key == stable_node_key(manual)


def test_failure_never_echoes_name_or_host():
    secret_host = "s3cret-host.example.internal"
    with pytest.raises(NodeIdentityError) as excinfo:
        stable_node_key({"name": "n",
                         "config": f"n = trojan, {secret_host}, 443, sni=x, sni=y"})
    assert secret_host not in str(excinfo.value)


# --- 转义与注释 ---

def test_quoted_escape_sequences_decode():
    node = {"name": "x", "config": 'x = trojan, h.example.com, 443, password="a\\"b"'}
    assert canonical_connection_bytes(node) == b'trojan\nh.example.com\n443\npassword=a"b'
    node2 = {"name": "x", "config": 'x = trojan, h.example.com, 443, password="a\\\\b"'}
    assert canonical_connection_bytes(node2) == b"trojan\nh.example.com\n443\npassword=a\\b"


@pytest.mark.parametrize("config", [
    'n = trojan, h, 443, password="a\\qb"',   # 未知转义
    'n = trojan, h, 443, password="ab\\',    # 尾随转义
], ids=["unknown-escape", "trailing-escape"])
def test_invalid_escape_fails_closed(config):
    with pytest.raises(NodeIdentityError):
        stable_node_key({"name": "n", "config": config})


def test_inline_comment_after_space_is_stripped():
    with_comment = parsed_node(name="v6", config="v6 = trojan, h.example.com, 443, password=p # backup node")
    without = parsed_node(name="z", config="z = trojan, H.example.com, 443, password=p")
    assert stable_node_key(with_comment) == stable_node_key(without)

    semicolon = parsed_node(name="s", config="s = trojan, h.example.com, 443, password=p ; note")
    slashes = parsed_node(name="d", config="d = trojan, h.example.com, 443, password=p // note")
    assert stable_node_key(semicolon) == stable_node_key(without)
    assert stable_node_key(slashes) == stable_node_key(without)


def test_comment_markers_without_leading_space_are_literal():
    """无前导空格的 # 与 // 不是注释（如 URL scheme 里的 ://）"""
    url_node = parsed_node(name="u", config="u = http, h.example.com, 443, url=https://a.example/p, password=p")
    assert canonical_connection_bytes(url_node) == \
        b"http\nh.example.com\n443\npassword=p\nurl=https://a.example/p"
    hash_node = parsed_node(name="m", config="m = trojan, h.example.com, 443, path=a#b")
    assert b"path=a#b" in canonical_connection_bytes(hash_node)


def test_comment_markers_inside_quotes_are_literal():
    node = {"name": "q", "config": 'q = trojan, h.example.com, 443, password="a#b;c//d"'}
    assert canonical_connection_bytes(node) == b"trojan\nh.example.com\n443\npassword=a#b;c//d"


# --- 主机规范化 ---

def test_ipv6_literal_compressed_and_bracket_stripped():
    first = parsed_node(name="a", config="a = trojan, [2001:0DB8:0000::0001], 443, password=x")
    second = parsed_node(name="b", config="b = trojan, 2001:db8::1, 443, password=x")
    assert stable_node_key(first) == stable_node_key(second)
    assert canonical_connection_bytes({"name": "a", "config": "a = trojan, [::FFFF:0102:0304], 443, p=k"}) \
        .split(b"\n")[1] == b"::ffff:1.2.3.4"


def test_invalid_ipv6_text_fails_closed():
    with pytest.raises(NodeIdentityError):
        stable_node_key({"name": "n", "config": "n = trojan, [2001:zz::1], 443, password=x"})


def test_unicode_host_nfc_idna_equivalence():
    precomposed = parsed_node(name="a", config="a = ss, BÜCHER.example.COM, 443, password=x")
    decomposed_name = "BU\u0308CHER.example.com"
    decomposed = parsed_node(name="b", config=f"b = ss, {decomposed_name}, 443, password=x")
    punycode = parsed_node(name="c", config="c = ss, xn--bcher-kva.example.com, 443, password=x")
    keys = {stable_node_key(precomposed), stable_node_key(decomposed), stable_node_key(punycode)}
    assert len(keys) == 1


def test_trailing_dot_host_equivalence():
    first = parsed_node(name="a", config="a = trojan, example.com., 443, password=x")
    second = parsed_node(name="b", config="b = trojan, EXAMPLE.com, 443, password=x")
    assert stable_node_key(first) == stable_node_key(second)


def test_distinct_hosts_remain_distinct():
    first = parsed_node(name="a", config="a = ss, xn--bcher-kva.example.com, 443, password=x")
    second = parsed_node(name="b", config="b = ss, bucher.example.com, 443, password=x")
    assert stable_node_key(first) != stable_node_key(second)


def test_host_with_whitespace_fails_closed():
    with pytest.raises(NodeIdentityError):
        stable_node_key({"name": "n", "config": "n = ss, bad host.example, 443, password=x"})
    with pytest.raises(NodeIdentityError):
        stable_node_key({"name": "n", "config": "n = ss, ☃.example\tcom, 443, password=x"})
