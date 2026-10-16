"""仓库开源安全基线测试（fix round 2 加固版）。

- ``DEFAULT_GENERAL`` 不得包含静态控制器凭据键；
- 全仓库（tracked + untracked non-ignored）UTF-8 文本不得包含真实用户
  目录、私钥块、疑似凭据赋值、非白名单 RFC1918 地址（含 CIDR）或
  非白名单 MAC 地址；
- 命中只报告 ``(路径, 规则号)``，绝不回显疑似秘密内容；
- 豁免按 ``(文件, 规则号) -> 文件 SHA-256`` 精确锁定：被豁免文件发生
  任何改动都会使摘要失配而重新报警（可审计），不做整文件跳过；
- 规则带「值级白名单」：RFC1918 仅允许三个标准保留段 CIDR
  （10.0.0.0/8、172.16.0.0/12、192.168.0.0/16），其余 host/CIDR
  （含子段 CIDR 与任意 /32）一律报告；MAC 检测全部形式，
  仅允许规范指定的合成值；
- 扫描器自身以分段字符串构造规则；白名单字面量天然被值级白名单
  覆盖，因此不豁免自身。
"""

import hashlib
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.default_config import DEFAULT_GENERAL

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Rule:
    pattern: re.Pattern
    # 值级精确白名单：匹配值若在集合中则不算违规（如标准保留段 CIDR）
    allow_values: frozenset[str] = field(default_factory=frozenset)


# 规则 1：真实用户目录（分段构造避免自命中）
R_USERS = Rule(re.compile("/" + "Users/" + r"[^/\s]+/"))
# 规则 2：多类 PEM 私钥块（分段构造）
R_PEM = Rule(re.compile("BEGIN " + r"(?:RSA |EC |DSA |OPENSSH |ENCRYPTED |)PRIVATE KEY"))
# 规则 3：MITM 证书 / 已废除的静态 API Token 赋值（分段构造键名）
R_SECRET_ASSIGN = Rule(re.compile(
    r"(?:ca-p12|" + "SURGE_" + r"API_TOKEN)" + r"\s*[=:]\s*[^\s$<{]", re.I))
# 规则 4：控制器凭据环境变量赋值（分段构造键名）
R_CTRL_ENV_ASSIGN = Rule(re.compile(
    "(?:SURGE_" + "CONTROLLER_ACCESS" + r"|SURGE_" + "HTTP_API)"
    + r"\s*[=:]\s*[^\s$<{]"))
# 规则 5：General 控制器键的真实值赋值（example- 占位值除外；分段构造键名）
R_CTRL_KEY_ASSIGN = Rule(re.compile(
    "(?:external-controller" + r"-access|http" + r"-api)"
    + r"\s*[=:]\s*(?!example-)[^\s<{]"))
# 规则 6：RFC1918 host/CIDR（全部检测；仅三个标准保留段 CIDR 值级放行）
R_RFC1918 = Rule(
    re.compile(
        r"\b(?:10" + r"\.\d{1,3}\.\d{1,3}\.\d{1,3}"
        r"|192" + r"\.168\.\d{1,3}\.\d{1,3}"
        r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})"
        r"(?:/\d{1,2})?"),
    allow_values=frozenset({
        "10" + ".0.0.0/8",
        "172" + ".16.0.0/12",
        "192" + ".168.0.0/16",
    }),
)
# 规则 7：MAC 地址（全部检测，不按 locally-administered 整类排除；
# 仅允许规范指定的合成值）
R_MAC = Rule(
    re.compile(
        r"(?<![0-9A-Fa-f:])"
        r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}"
        r"(?![0-9A-Fa-f:])"),
    allow_values=frozenset({"02:00:00:00:00:01"}),
)

FORBIDDEN = [R_USERS, R_PEM, R_SECRET_ASSIGN, R_CTRL_ENV_ASSIGN,
             R_CTRL_KEY_ASSIGN, R_RFC1918, R_MAC]

# (相对路径, 规则号) -> 该文件当前 SHA-256。
# 仅豁免「指定规则」，文件内其他规则仍扫描；文件一旦变更，
# SHA 失配 → 豁免失效 → 测试失败，强制人工重新审查后更新摘要。
RULE_EXEMPTIONS: dict[tuple[str, int], str] = {
    # 合成 MITM 夹具：ca-p12 = <合成 base64>（规则 3）
    ("tests/fixtures/synthetic_profile.conf", 3): "6d0b5eeddc4c7670f03b486dd24836e33b155d17c8ea1e4d5952510cb4cfb103",
    # launchd 行为测试：合成凭据赋值断言行（规则 4）
    ("tests/test_launchd_scripts.py", 4): "4a62604102192ef7dbb2e87712dfe4797181041818f71d25241913c38fa2a4c7",
}


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="session")
def repo_text_files():
    """枚举 tracked + untracked（遵循 .gitignore）的可读 UTF-8 文本文件。

    输入必须来自 ``git ls-files --cached --others --exclude-standard -z``，
    因此天然排除 venv、__pycache__ 等被忽略目录；再过滤二进制文件。
    """
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=PROJECT_ROOT, check=True, capture_output=True,
    )
    files = []
    for name in out.stdout.decode("utf-8").split("\0"):
        if not name:
            continue
        path = PROJECT_ROOT / name
        try:
            if b"\x00" in path.read_bytes()[:8192]:
                continue  # 二进制
            path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        files.append(name)
    return files


def scan_files(files, rules, exemptions=None):
    """对每个文件应用规则，返回 ``(相对路径, 规则号)`` 列表，不回显内容。

    值级白名单：规则匹配值在 ``allow_values`` 中则跳过该匹配；
    文件中存在任一非白名单匹配才记违规。文件级豁免按
    ``(路径, 规则号)`` 精确匹配且要求文件 SHA-256 与登记值一致；
    整文件级 allow/exclude 不存在。
    """
    exemptions = exemptions if exemptions is not None else RULE_EXEMPTIONS
    violations = []
    for name in files:
        text = (PROJECT_ROOT / name).read_text(encoding="utf-8")
        for rule_id, rule in enumerate(rules, start=1):
            violated = False
            for match in rule.pattern.finditer(text):
                if match.group(0) not in rule.allow_values:
                    violated = True
                    break
            if not violated:
                continue
            expected_sha = exemptions.get((name, rule_id))
            if expected_sha is not None and _file_sha256(PROJECT_ROOT / name) == expected_sha:
                continue  # 精确豁免：仅此规则、且文件未被改动
            violations.append((name, rule_id))
    return violations


def test_default_general_has_no_static_controller_credentials():
    assert not {"external-controller-access", "http-api"} & DEFAULT_GENERAL.keys()


def test_repository_text_has_no_machine_or_secret_material(repo_text_files):
    violations = scan_files(repo_text_files, FORBIDDEN)
    assert violations == []


def test_exemptions_reference_existing_files_and_shas():
    """豁免表必须指向存在的文件且摘要格式合法（防登记笔误静默失效）"""
    for (name, rule_id), sha in RULE_EXEMPTIONS.items():
        assert (PROJECT_ROOT / name).is_file(), f"豁免文件不存在: {name}"
        assert re.fullmatch(r"[0-9a-f]{64}", sha), f"豁免摘要非法: {name}"
        assert 1 <= rule_id <= len(FORBIDDEN), f"豁免规则号越界: {name}"


# ===== 规则正反单测（不扫仓库，直接对规则断言） =====


class TestRuleSemantics:
    """RFC1918 / MAC 规则的白名单与报告行为"""

    @staticmethod
    def _ok(rule: Rule, text: str) -> bool:
        return all(m.group(0) in rule.allow_values
                   for m in rule.pattern.finditer(text))

    def test_standard_reserved_cidrs_allowed(self):
        assert self._ok(R_RFC1918, "10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16")

    def test_concrete_rfc1918_hosts_reported(self):
        assert not self._ok(R_RFC1918, "server:" + "10.0.1" + ".100")
        assert not self._ok(R_RFC1918, "peer " + "192.168.1" + ".50 denied")
        assert not self._ok(R_RFC1918, "db " + "172.20.0" + ".5")

    def test_nonstandard_cidrs_reported(self):
        assert not self._ok(R_RFC1918, "10.10" + ".0.0/16")        # 子段 CIDR
        assert not self._ok(R_RFC1918, "192.168.1" + ".0/24")      # 子网 CIDR
        assert not self._ok(R_RFC1918, "10.0.0" + ".5/32")         # /32
        assert not self._ok(R_RFC1918, "172.16.5" + ".0/24")

    def test_non_rfc1918_unaffected(self):
        assert self._ok(R_RFC1918, "198.51.100.0/24 203.0.113.9 192.0.2.10")
        assert self._ok(R_RFC1918, "127.0.0.1 0.0.0.0 100.64.0.0/10")

    def test_all_mac_forms_detected(self):
        assert not self._ok(R_MAC, "a4:83" + ":e7:11:22:33")       # 全局唯一（真实）
        assert not self._ok(R_MAC, "02:11" + ":22:33:44:55")       # locally administered 也报
        assert not self._ok(R_MAC, "00:1a" + ":2b:3c:4d:5e")

    def test_designated_synthetic_mac_allowed(self):
        assert self._ok(R_MAC, "rule MAC-ADDRESS,02:00:00:00:00:01,DIRECT")
        assert self._ok(R_MAC, "02:00:00:00:00:01")

    def test_non_mac_unaffected(self):
        assert self._ok(R_MAC, "12:34:56 sha256 abcdef 2026-10-16 fe80::1")
