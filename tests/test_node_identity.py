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
