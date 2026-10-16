"""SURGE_CONTROLLER_ACCESS / SURGE_HTTP_API 环境注入校验测试（fix round 1）。

非法值必须让生成明确失败（防配置注入）；合法值单行 ``access@host:port``，
host 支持 hostname / IPv4 / 方括号 IPv6，port 1..65535。
"""

import pytest

from app.services.generator import validated_access_env


def test_unset_returns_none(monkeypatch):
    monkeypatch.delenv("SURGE_CONTROLLER_ACCESS", raising=False)
    assert validated_access_env("SURGE_CONTROLLER_ACCESS") is None


def test_blank_is_treated_as_unset(monkeypatch):
    """仅空白 = 未设置（不注入、不失败）"""
    monkeypatch.setenv("SURGE_CONTROLLER_ACCESS", "   ")
    assert validated_access_env("SURGE_CONTROLLER_ACCESS") is None


def test_surrounding_whitespace_is_trimmed(monkeypatch):
    monkeypatch.setenv("SURGE_CONTROLLER_ACCESS", "  key@example.com:6160  ")
    assert validated_access_env("SURGE_CONTROLLER_ACCESS") == "key@example.com:6160"


@pytest.mark.parametrize("value", [
    "key@example.com:6160\r\nhttp" + "-api = false",  # CRLF 注入额外键
    "key@example.com:6160\n[Rule]",               # LF 注入 section
    "key@example.com:6160\x7f",                   # DEL
    "no-at-sign:6160",                            # 缺 access@host:port 结构
    # NUL 无法进入环境变量：OS putenv 直接拒绝，不属于生成器职责
    "key@exa\x01mple.com:6160",                   # 其他控制字符
    "key@example.com:0",                          # 端口 0
    "key@example.com:65536",                      # 端口越界
    "key@example.com:-1",                         # 负端口
    "key@example.com:abc",                        # 非数字端口
    "key@ex ample.com:6160",                      # host 含空白
    "key@example .com:6160",                      # host 含空白 2
    "k ey@example.com:6160",                      # access 含空白
    "key,extra@example.com:6160",                 # access 含配置分隔符
    "key=extra@example.com:6160",                 # access 含 = 
    "key@example.com:6160:6161",                  # 多余冒号（裸 IPv6 不允许）
    "key@fe80::1:6160",                           # IPv6 未加方括号
    "key@[fe80::1]:6160:1",                       # IPv6 括号后多余段
    "key@999.999.999.999:6160",                   # 非法 IPv4
])
def test_invalid_values_fail_generation(monkeypatch, value):
    monkeypatch.setenv("SURGE_CONTROLLER_ACCESS", value)
    with pytest.raises(ValueError, match="SURGE_CONTROLLER_ACCESS"):
        validated_access_env("SURGE_CONTROLLER_ACCESS")


@pytest.mark.parametrize("value,expected", [
    ("key@example.com:6160", "key@example.com:6160"),
    ("example-access-key@127.0.0.1:6160", "example-access-key@127.0.0.1:6160"),
    ("key@192.0.2.10:1", "key@192.0.2.10:1"),
    ("key@[2001:db8::1]:6166", "key@[2001:db8::1]:6166"),
    ("key@[::1]:65535", "key@[::1]:65535"),
    ("a-b.c-d@host-name.example:6160", "a-b.c-d@host-name.example:6160"),
])
def test_valid_values_pass(monkeypatch, value, expected):
    monkeypatch.setenv("SURGE_HTTP_API", value)
    assert validated_access_env("SURGE_HTTP_API") == expected
