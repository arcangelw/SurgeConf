"""仓库开源安全基线测试（fix round 2 加固版）。

- ``DEFAULT_GENERAL`` 不得包含静态控制器凭据键；
- 全仓库（tracked + untracked non-ignored）UTF-8 文本不得包含真实用户
  目录、私钥块、疑似凭据赋值、非白名单 RFC1918 地址（含 CIDR）或
  非白名单 MAC 地址；失败消息只报 ``(文件, 规则类别)``；
- UDP / 迁移 / README / 目标设备四篇文档必须处于扫描范围
  （``TestDocumentCoverage`` 防改名或 .gitignore 导致的静默缩水）；
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
import os
import re
import shlex
import shutil
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
# 规则 7：MAC 地址三格式（冒号/连字符/Cisco 点分 aabb" + ".ccdd.eeff），全部
# 检测、不按 locally-administered 整类排除；整体分组使左右边界作用于
# 全部分支；仅放行规范指定的同一合成 MAC 的三种等价表示
R_MAC = Rule(
    re.compile(
        r"(?<![0-9A-Fa-f:.-])"
        r"(?:"
        r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}"
        r"|[0-9A-Fa-f]{2}(?:-[0-9A-Fa-f]{2}){5}"
        r"|[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}"
        r")"
        r"(?![0-9A-Fa-f:.-])"),
    allow_values=frozenset({
        "02:00:00:00:00:01",
        "02-00-00-00-00-01",
        "0200.0000.0001",
    }),
)

FORBIDDEN = [R_USERS, R_PEM, R_SECRET_ASSIGN, R_CTRL_ENV_ASSIGN,
             R_CTRL_KEY_ASSIGN, R_RFC1918, R_MAC]
# 与 FORBIDDEN 索引对齐的规则类别名：失败消息只报 (文件, 类别)，
# 绝不回显命中内容
RULE_CATEGORIES = (
    "user-directory", "pem-private-key", "secret-assignment",
    "controller-env-assignment", "controller-key-assignment",
    "rfc1918-address", "mac-address",
)

# (相对路径, 规则号) -> 该文件当前 SHA-256。
# 仅豁免「指定规则」，文件内其他规则仍扫描；文件一旦变更，
# SHA 失配 → 豁免失效 → 测试失败，强制人工重新审查后更新摘要。
RULE_EXEMPTIONS: dict[tuple[str, int], str] = {
    # 合成 MITM 夹具：ca-p12 = <合成 base64>（规则 3）
    ("tests/fixtures/synthetic_profile.conf", 3): "6d0b5eeddc4c7670f03b486dd24836e33b155d17c8ea1e4d5952510cb4cfb103",
    # launchd 行为测试：合成凭据赋值断言行（规则 4）；Round 5 变更
    # （fake curl/readiness 提交门用例）复审无新增真实凭据赋值
    ("tests/test_launchd_scripts.py", 4): "8746d209c213cae9a30e595afa62470ec1580a40301d70ea7522511e6b23e01f",
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
    assert violations == [], "; ".join(
        f"{name} 触发安全规则类别 {RULE_CATEGORIES[rule_id - 1]}"
        for name, rule_id in violations)


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

    def test_all_mac_separators_detected(self):
        # 冒号 / 连字符 / Cisco 点分（aabb" + ".ccdd.eeff）：真实 MAC 三格式全部报告
        assert not self._ok(R_MAC, "a4:83" + ":e7:11:22:33")
        assert not self._ok(R_MAC, "a4-83" + "-e7-11-22-33")
        assert not self._ok(R_MAC, "a483" + ".e711.2233")
        # locally administered 与全局唯一同样报告（含 Cisco 点分形式）
        assert not self._ok(R_MAC, "02:11" + ":22:33:44:55")
        assert not self._ok(R_MAC, "00:1a" + ":2b:3c:4d:5e")
        assert not self._ok(R_MAC, "0211" + ".2233.4455")

    def test_designated_synthetic_mac_all_three_forms_allowed(self):
        # 同一合成 MAC 的三种等价表示均放行（UDP 计划文档的连字符形式不误报）
        assert self._ok(R_MAC, "rule MAC-ADDRESS,02:00:00:00:00:01,DIRECT")
        assert self._ok(R_MAC, "02-00-00-00-00-01")
        assert self._ok(R_MAC, "0200.0000.0001")

    def test_mac_boundaries_shared_across_all_branches(self):
        # 整体分组共享边界：合成值嵌入更长十六进制/分隔符串时不再产生
        # 可命中白名单的片段（旧版 hyphen 分支无边界时可被精确放行）
        matches = [m.group(0) for m in R_MAC.pattern.finditer("02-00-00-00-00-01-ff")]
        assert "02-00-00-00-00-01" not in matches
        assert [m.group(0) for m in R_MAC.pattern.finditer("ab" + "02:00:00:00:00:01")] == []
        # 真实 MAC 紧贴分隔符（被更长串吸收）同样不产生匹配片段
        assert [m.group(0) for m in R_MAC.pattern.finditer("-a4:83" + ":e7:11:22:33-")] == []
        # 独立上下文中的真实 MAC 仍完整匹配并报告
        found = [m.group(0) for m in R_MAC.pattern.finditer("peer a4:83" + ":e7:11:22:33 down")]
        assert found == ["a4:83" + ":e7:11:22:33"]

    def test_non_mac_unaffected(self):
        assert self._ok(R_MAC, "12:34:56 sha256 abcdef 2026-10-16 fe80::1")
        # 六组两位点分不是标准 MAC 格式，不构成匹配
        assert self._ok(R_MAC, "02.00.00.00.00.01")

    def test_real_user_directories_reported(self):
        # 绝对用户目录报告；分段构造避免测试文件自命中
        assert not self._ok(R_USERS, "备份在 " + "/Us" + "ers/alice/Documents/")
        assert not self._ok(R_USERS, "cat " + "/Us" + "ers/Bob/Library/foo")

    def test_tilde_home_paths_not_reported(self):
        # 规范的 ~ 展开写法与本文档占位路径不误判（非绝对 /Users/ 形式）
        assert self._ok(R_USERS, "~/Library/Application Support/SurgeConf")
        assert self._ok(R_USERS, "$HOME/Library/Application Support/SurgeConf")
        assert self._ok(R_USERS, "数据目录（默认 `~/Library/Application Support/SurgeConf`）")

    def test_secret_assignment_forms_reported(self):
        # ca-p12 / 旧 API token 键的真实值赋值（分段构造避免自命中）
        assert not self._ok(R_SECRET_ASSIGN, "ca-" + "p12 = MIIxyZ==")
        assert not self._ok(R_SECRET_ASSIGN, "SURGE_" + "API_TOKEN=live-token-value")

    def test_controller_env_and_key_assignments_reported(self):
        # 环境变量与 General 键的凭据赋值报告；example- 占位与空值不报告
        assert not self._ok(R_CTRL_ENV_ASSIGN, "SURGE_" + "HTTP_API=real-key@lan:6166")
        assert not self._ok(R_CTRL_KEY_ASSIGN, "http" + "-api = real-key@127.0.0.1:6166")
        assert self._ok(R_CTRL_KEY_ASSIGN, "http" + "-api = example-key@127.0.0.1:6166")

    def test_documentation_placeholders_not_reported(self):
        # UDP / 迁移文档使用的规范占位符全部不误判
        assert self._ok(R_MAC, "AND,((MAC-ADDRESS,02:00:00:00:00:01),(PROTOCOL,UDP)),DIRECT")
        assert self._ok(R_RFC1918, "server:192.0.2.10 peer 198.51.100.20 nat 203.0.113.9")
        assert self._ok(R_USERS, "ExampleVideo example.com ExampleNode-HK")


class TestDocumentCoverage:
    """UDP / 迁移 / README 文档必须处于仓库扫描范围内且内容干净。

    防两类静默失效：文档被重命名或加入 .gitignore 后脱离
    ``git ls-files`` 枚举（扫描范围缩水），或文档新增真实
    MAC / 私网地址 / 用户目录 / 凭据而主扫描未及发现。
    """

    REQUIRED_DOCS = (
        "README.md",
        "docs/UDP_COMPATIBILITY.md",
        "docs/MIGRATION_VERIFY.md",
        "docs/PROFILE_TARGETS.md",
        "docs/RUNTIME_OPERATIONS.md",
    )

    def test_required_docs_in_scan_scope(self, repo_text_files):
        missing = [d for d in self.REQUIRED_DOCS if d not in repo_text_files]
        assert missing == [], f"文档脱离扫描范围（改名/被忽略？）: {missing}"

    def test_required_docs_clean(self, repo_text_files):
        violations = scan_files(
            [d for d in self.REQUIRED_DOCS if d in repo_text_files], FORBIDDEN)
        assert violations == [], "; ".join(
            f"{name} 触发安全规则类别 {RULE_CATEGORIES[rule_id - 1]}"
            for name, rule_id in violations)



class TestHttpApiExampleSafety:
    """Surge HTTP API / curl 示例片段的结构性安全约束（Fix Round 5）。

    解析 markdown 的 shell 类 fenced 代码块（bash/sh/shell/zsh，info
    string 大小写归一），行尾反斜杠续行拼接为完整逻辑命令，再用
    ``shlex.shlex(..., punctuation_chars="|&;()<>")`` 分词。对逻辑
    命令中的**每一个** curl 命令段（含 ``/usr/bin/curl`` 等绝对路径
    形式、一行多个 curl）独立检测：

    - 命令边界只认真正控制运算符（``|`` / ``||`` / ``|&`` / ``&`` /
      ``&&`` / ``;`` / ``;;`` / ``(`` / ``)``）；``<`` / ``>`` 等
      重定向**不是**边界、不截断 curl 段；
    - 重定向 fail-closed：curl 段内任何含 ``<`` / ``>`` 的 token 一律
      报 ``curl-redirect``（不回显内容）——避免重定向位置截断段与
      复杂重定向语义（``>&`` / 进程替换等）绕过；合法示例无需
      curl 重定向；
    - TLS：``--insecure`` 与按 curl 语义展开的组合短选项中位于 H
      之前的 ``k``（``-sk`` / ``-fsSk``；``-sHk`` 的 k 属于 header
      值，不误报）；
    - X-Key argv：``-H`` / ``--header`` 的分离值、``--header=值``、
      以及组合短选项中 H 的附着值（``-sSHX-Key:secret``）或下一
      token 值（``-sSH 'X-Key: …'``）——头名大小写不敏感，一律
      拒绝；唯一放行 stdin 形式 ``@-``；任何 ``@文件`` 头来源
      （含 ``-sSH@secret``）也拒绝；
    - 6171：curl 任一 arg（含 ``--url`` 值与位置 URL）出现明文
      ``http://`` 即拒绝（scheme 大小写不敏感，``HTTP://`` 同样命中）。

    失败消息只含 ``(文件, 规则类别)``，绝不回显命令或 header 内容；
    检测结构行为，不绑定具体措辞。
    """

    SHELL_LANGS = frozenset({"bash", "sh", "shell", "zsh"})
    # 真正的 shell 控制运算符字符集：| || |& & && ; ;; ( )。
    # < > 属于重定向而非命令边界——绝不当边界截断 curl 段。
    _CONTROL = frozenset("|;&()")

    RULE_INSECURE = "insecure-curl"
    RULE_KEY_ARGV = "x-key-argv"
    RULE_HEADER_FILE = "header-from-file"
    RULE_PLAINTEXT_HTTP = "6171-plaintext-http"
    # fail-closed：curl 命令段内任何 shell 重定向（< > 及其组合）一律报告，
    # 避免重定向边界截断/复杂重定向语义绕过；合法示例不需要 curl 重定向
    RULE_CURL_REDIRECT = "curl-redirect"

    # ===== 注入矩阵：每类常见绕过写法必须命中对应规则（红） =====
    # (fenced 语言, 块内容, 期望命中的规则类别)；语言覆盖大小写与
    # bash/sh/shell/zsh 变体。
    INJECTIONS = [
        pytest.param(
            "bash",
            'printf \'X-Key: %s\\n\' "$KEY" | curl -sk '
            'https://127.0.0.1:6171/v1/profiles/reload|jq .',
            RULE_INSECURE,
            id="nospace-pipe-combined-short-sk"),
        pytest.param(
            "bash",
            'printf \'X-Key: %s\\n\' "$KEY" | curl -sS -X POST --header @- \\\n'
            '  --insecure \\\n'
            '  https://127.0.0.1:6171/v1/profiles/reload',
            RULE_INSECURE,
            id="insecure-on-continuation-line"),
        pytest.param(
            "zsh",
            '/usr/bin/curl -sS https://example.invalid/ && '
            'curl -k https://127.0.0.1:6171/v1/profiles/reload',
            RULE_INSECURE,
            id="second-curl-on-line-insecure"),
        pytest.param(
            "SH",
            'curl -sS -HX-Key: hunter2 https://127.0.0.1:6171/v1/profiles/reload',
            RULE_KEY_ARGV,
            id="attached-h-option"),
        pytest.param(
            "bash",
            "curl -sS --header='X-Key: hunter2' "
            "https://127.0.0.1:6171/v1/profiles/reload",
            RULE_KEY_ARGV,
            id="long-header-equals"),
        pytest.param(
            "bash",
            "curl -sS -H 'x-key: hunter2' "
            "https://127.0.0.1:6171/v1/profiles/reload",
            RULE_KEY_ARGV,
            id="lowercase-header-name"),
        pytest.param(
            "shell",
            'curl -sS --header @/tmp/secret-headers '
            'https://127.0.0.1:6171/v1/profiles/reload',
            RULE_HEADER_FILE,
            id="header-from-temp-file"),
        pytest.param(
            "sh",
            'curl -sS --url http://127.0.0.1:6171/v1/profiles/reload',
            RULE_PLAINTEXT_HTTP,
            id="plain-http-via-url-option"),
        pytest.param(
            "bash",
            'curl >/dev/null -k https://127.0.0.1:6171/v1/profiles/reload',
            RULE_CURL_REDIRECT,
            id="curl-redirect-inside-segment"),
        pytest.param(
            "bash",
            'curl >/dev/null -k https://127.0.0.1:6171/v1/profiles/reload',
            RULE_INSECURE,
            id="curl-redirect-does-not-truncate-insecure"),
        pytest.param(
            "bash",
            "curl -sSH 'X-Key: secret' "
            "https://127.0.0.1:6171/v1/profiles/reload",
            RULE_KEY_ARGV,
            id="combined-short-h-separated-value"),
        pytest.param(
            "bash",
            "curl -sSHX-Key:secret https://127.0.0.1:6171/v1/profiles/reload",
            RULE_KEY_ARGV,
            id="combined-short-h-attached-value"),
        pytest.param(
            "bash",
            "curl -sSH@secret https://127.0.0.1:6171/v1/profiles/reload",
            RULE_HEADER_FILE,
            id="combined-short-h-attached-file"),
        pytest.param(
            "bash",
            "curl -sS HTTP://127.0.0.1:6171/v1/profiles/reload",
            RULE_PLAINTEXT_HTTP,
            id="uppercase-scheme-positional-url"),
        pytest.param(
            "sh",
            "curl -sS --url HTTP://127.0.0.1:6171/v1/profiles/reload",
            RULE_PLAINTEXT_HTTP,
            id="uppercase-scheme-url-option"),
    ]

    @classmethod
    def _bash_logical_lines(cls, paths):
        """``paths`` 为 ``(展示名, Path)`` 序列；产出每个 shell 代码块内
        续行拼接后的逻辑命令。"""
        for display, path in paths:
            text = path.read_text(encoding="utf-8")
            in_block = False
            buffer: list[str] = []
            for raw in text.splitlines():
                stripped = raw.strip()
                if not in_block:
                    if stripped.startswith("```") and len(stripped) > 3:
                        info = stripped[3:].split()
                        if info and info[0].lower() in cls.SHELL_LANGS:
                            in_block = True
                            buffer = []
                    continue
                if stripped.startswith("```"):
                    in_block = False
                    for line in cls._joined(buffer):
                        yield display, line
                    buffer = []
                    continue
                buffer.append(raw)

    @staticmethod
    def _joined(lines):
        """把行尾反斜杠续行拼接为单条逻辑命令。"""
        current = ""
        for line in lines:
            candidate = f"{current} {line}".strip() if current else line
            if candidate.rstrip().endswith("\\"):
                current = candidate.rstrip()[:-1].rstrip()
            else:
                yield candidate
                current = ""
        if current:
            yield current

    @staticmethod
    def _tokens(logical_line):
        """带标点边界的分词：无空格 ``|``/``&&``/``;`` 也成独立 token。"""
        try:
            lex = shlex.shlex(logical_line, posix=True,
                              punctuation_chars="|&;()<>")
            lex.whitespace_split = True
            return list(lex)
        except ValueError:
            return logical_line.split()

    @classmethod
    def _is_curl(cls, token):
        return token == "curl" or token.endswith("/curl")

    @classmethod
    def _is_boundary(cls, token):
        """仅真正控制运算符（|/||/&/&&/;/;;/(/)）是命令边界。
        重定向 token（>/>>/</&>/>& 等）含 < 或 >，不满足全字符
        校验，既不当边界也不丢弃——留在段内由 fail-closed 规则报告。"""
        return bool(token) and all(c in cls._CONTROL for c in token)

    @classmethod
    def _curl_segments(cls, tokens):
        """逻辑命令中**每一个** curl 命令段的 argv（控制运算符边界
        截断；一行多个 curl 各自独立）。"""
        segments = []
        argv = None
        for tok in tokens:
            if argv is None:
                if cls._is_curl(tok):
                    argv = []
                continue
            if cls._is_boundary(tok):
                segments.append(argv)
                argv = None
            elif cls._is_curl(tok):
                segments.append(argv)
                argv = []
            else:
                argv.append(tok)
        if argv is not None:
            segments.append(argv)
        return segments

    @staticmethod
    def _is_short_cluster(token: str) -> bool:
        """curl 组合短选项簇：``-`` 后以字母开头（``-sk`` / ``-sSH值``）；
        长选项（``--`` 开头）、负数与其他附着形式不匹配。"""
        return (token.startswith("-") and not token.startswith("--")
                and len(token) > 1 and token[1].isalpha())

    @staticmethod
    def _expand_shorts(token: str):
        """按 curl 语义展开短选项簇，返回 ``(H 前的标志字母, H 附着值,
        是否含 H)``。curl 规则：短字母可连写；遇到带参选项 H 时，
        其后剩余字符整体是 H 的参数（``-sHk`` 即 header 值 ``k``），
        因此 H 之后的字母（含 k）属于 header 值，不是独立标志。"""
        body = token[1:]
        idx = body.find("H")
        if idx == -1:
            return body, "", False
        return body[:idx], body[idx + 1:], True

    @classmethod
    def _is_insecure_flag(cls, token: str) -> bool:
        if token == "--insecure":
            return True
        if not cls._is_short_cluster(token):
            return False
        letters, _attached, _has_h = cls._expand_shorts(token)
        return "k" in letters

    @classmethod
    def _header_values(cls, argv):
        """argv 中 -H/--header 的全部 header 值。

        覆盖四类形式：``--header 值`` 分离、``--header=值``、短选项簇
        中 H 的附着值（``-HX-Key:v`` / ``-sSHX-Key:v``）与 H 位于簇尾
        时的下一 token 值（``-H 值`` / ``-sSH 值``）。"""
        values = []
        i = 0
        while i < len(argv):
            tok = argv[i]
            if tok == "--header":
                if i + 1 < len(argv):
                    values.append(argv[i + 1])
                    i += 1
            elif tok.startswith("--header="):
                values.append(tok[len("--header="):])
            elif cls._is_short_cluster(tok):
                _letters, attached, has_h = cls._expand_shorts(tok)
                if has_h:
                    if attached:
                        values.append(attached)
                    elif i + 1 < len(argv):
                        values.append(argv[i + 1])
                        i += 1
            i += 1
        return values

    @classmethod
    def _segment_violations(cls, argv):
        """单 curl 段命中的规则类别集合。"""
        found = set()
        # fail-closed：段内任何含 < / > 的 token 视为 shell 重定向
        # （>、>>、>&、&>、<>、进程替换等一律覆盖）。
        if any("<" in t or ">" in t for t in argv):
            found.add(cls.RULE_CURL_REDIRECT)
        if any(cls._is_insecure_flag(t) for t in argv):
            found.add(cls.RULE_INSECURE)
        for value in cls._header_values(argv):
            if value.startswith("@"):
                if value != "@-":
                    found.add(cls.RULE_HEADER_FILE)
            elif value.split(":", 1)[0].strip().lower() == "x-key":
                found.add(cls.RULE_KEY_ARGV)
        joined = " ".join(argv).lower()
        if "6171" in joined and "http://" in joined:
            found.add(cls.RULE_PLAINTEXT_HTTP)
        return found

    @classmethod
    def _violations(cls, paths):
        """全部 ``(文件, 规则类别)``（按出现去重，不回显命令/header）。"""
        seen: list[tuple[str, str]] = []
        for display, line in cls._bash_logical_lines(paths):
            for argv in cls._curl_segments(cls._tokens(line)):
                for rule in cls._segment_violations(argv):
                    item = (display, rule)
                    if item not in seen:
                        seen.append(item)
        return seen

    @staticmethod
    def _repo_md_paths(files):
        return [(name, PROJECT_ROOT / name) for name in files
                if name.endswith(".md")]

    def _repo_rule_hits(self, repo_text_files, rule):
        return [v for v in self._violations(self._repo_md_paths(repo_text_files))
                if v[1] == rule]

    def test_no_insecure_curl_flags(self, repo_text_files):
        offenders = self._repo_rule_hits(repo_text_files, self.RULE_INSECURE)
        assert offenders == [], f"curl 跳过证书校验的示例: {offenders}"

    def test_no_key_in_curl_argv(self, repo_text_files):
        offenders = self._repo_rule_hits(repo_text_files, self.RULE_KEY_ARGV)
        assert offenders == [], f"X-Key 经 curl argv 注入: {offenders}"

    def test_no_header_values_from_files(self, repo_text_files):
        offenders = self._repo_rule_hits(repo_text_files, self.RULE_HEADER_FILE)
        assert offenders == [], f"curl 头值来自 @文件（临时 secret 风险）: {offenders}"

    def test_http_api_port_never_plaintext_http(self, repo_text_files):
        offenders = self._repo_rule_hits(repo_text_files,
                                         self.RULE_PLAINTEXT_HTTP)
        assert offenders == [], (
            f"默认 http-api-tls=true 的示例端口出现明文 http://: {offenders}")

    def test_no_curl_redirects_in_examples(self, repo_text_files):
        offenders = self._repo_rule_hits(repo_text_files, self.RULE_CURL_REDIRECT)
        assert offenders == [], (
            f"curl 命令段内出现 shell 重定向（fail-closed）: {offenders}")

    @pytest.mark.parametrize("lang,block,expected_rule", INJECTIONS)
    def test_injection_matrix_is_detected(self, tmp_path, lang, block,
                                          expected_rule):
        """矩阵里每类绕过写法都必须被扫描器命中（结构性红）。"""
        doc = tmp_path / "injected.md"
        doc.write_text(f"```{lang}\n{block}\n```\n", encoding="utf-8")
        hits = self._violations([(str(doc), doc)])
        assert (str(doc), expected_rule) in hits, (
            f"注入样例未被检出: {hits}")

    def test_documented_reload_blocks_stay_clean(self, tmp_path, reload_blocks):
        """从实际文档提取的 reload 块必须始终通过扫描器（结构性绿）。"""
        doc = tmp_path / "canonical.md"
        doc.write_text(
            "# canonical reload blocks\n"
            + "".join(f"\n```bash\n{block}\n```\n" for block in reload_blocks),
            encoding="utf-8")
        assert self._violations([(str(doc), doc)]) == []

    def test_benign_curl_forms_not_flagged(self, tmp_path):
        """合法形态不得误报：stdin header、普通头值、组合短选项中 H
        吞掉后续字母（curl 语义：-sHk 的 k 属于 header 值而非 -k）。"""
        benign = [
            "curl --fail --silent --show-error -X POST --header @- "
            "https://127.0.0.1:6171/v1/profiles/reload",
            "curl -H 'Accept: application/json' "
            "https://127.0.0.1:6171/v1/profiles/reload",
            "curl -sHk https://127.0.0.1:6171/v1/profiles/reload",
            "curl --header=@- -fsS https://127.0.0.1:6171/v1/profiles/reload",
        ]
        doc = tmp_path / "benign.md"
        doc.write_text("```bash\n" + "\n".join(benign) + "\n```\n",
                       encoding="utf-8")
        assert self._violations([(str(doc), doc)]) == []

# ===== 从实际文档提取 reload 块的行为验证（Fix Round 5） =====

RELOAD_ENDPOINT = "/v1/profiles/reload"


@pytest.fixture(scope="module")
def reload_blocks():
    """从实际 ``docs/RUNTIME_OPERATIONS.md`` 提取包含 reload 端点的
    shell fenced 块（原始行）。按「shell 块 + 端点路径」结构性定位，
    不复制文档全文常量：文档块更新后测试自动跟随真实内容。"""
    doc = PROJECT_ROOT / "docs" / "RUNTIME_OPERATIONS.md"
    blocks = []
    current = None
    for raw in doc.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if current is None:
            if stripped.startswith("```") and len(stripped) > 3:
                info = stripped[3:].split()
                if info and info[0].lower() in TestHttpApiExampleSafety.SHELL_LANGS:
                    current = []
            continue
        if stripped.startswith("```"):
            if any(RELOAD_ENDPOINT in line for line in current):
                blocks.append("\n".join(current))
            current = None
            continue
        current.append(raw)
    return blocks


class TestDocumentedReloadBlocks:
    """真实执行文档 reload 块（假 curl 经 PATH 注入）：key 经 header
    stdin 到达且 argv 无 secret、curl 退出码（含模拟 HTTP 失败码）传播、
    空 key / read EOF 不发起请求、备用 CA 有效/无效行为、块内 exit 不
    退出父 shell 且父变量不受影响。bash 必测；zsh 存在时同矩阵执行。"""

    SECRET = "sk-test-1234567890abcdef"
    SHELLS = ["bash"] + (["zsh"] if shutil.which("zsh") else [])

    @staticmethod
    def _needs_ca(block):
        return "CA_FILE" in block

    def _stdin_lines(self, block, key, ca_path=None):
        lines = [key]
        if self._needs_ca(block):
            lines.append(str(ca_path))
        return lines

    @staticmethod
    def _run(shell, block, tmp_path, stdin_lines, fake_rc="0", prefix="",
             suffix=""):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        argv_log = tmp_path / "curl-argv.log"
        stdin_log = tmp_path / "curl-stdin.log"
        fake = bin_dir / "curl"
        fake.write_text(
            "#!/bin/sh\n"
            'cat > "$FAKE_CURL_STDIN_LOG"\n'
            'printf \'%s\\n\' "$@" > "$FAKE_CURL_ARGV_LOG"\n'
            'exit "${FAKE_CURL_RC:-0}"\n', encoding="utf-8")
        fake.chmod(0o755)
        script = tmp_path / "block.sh"
        script.write_text(prefix + block + "\n" + suffix, encoding="utf-8")
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env["FAKE_CURL_ARGV_LOG"] = str(argv_log)
        env["FAKE_CURL_STDIN_LOG"] = str(stdin_log)
        env["FAKE_CURL_RC"] = fake_rc
        data = "\n".join(stdin_lines) + "\n" if stdin_lines else ""
        return subprocess.run([shell, str(script)], input=data.encode(),
                              capture_output=True, env=env, timeout=30)

    def _valid_ca(self, tmp_path):
        ca = tmp_path / "ca.pem"
        ca.write_text("synthetic-ca\n", encoding="utf-8")
        return ca

    def test_exactly_two_reload_blocks(self, reload_blocks):
        assert len(reload_blocks) == 2, (
            f"文档 reload 块应为 2 个（系统信任 / 备用 CA）: "
            f"{len(reload_blocks)}")

    @pytest.mark.parametrize("shell", SHELLS)
    @pytest.mark.parametrize("block_idx", [0, 1])
    def test_success_sends_key_via_stdin_not_argv(self, tmp_path,
                                                  reload_blocks, shell,
                                                  block_idx):
        block = reload_blocks[block_idx]
        proc = self._run(shell, block, tmp_path,
                         self._stdin_lines(block, self.SECRET,
                                           self._valid_ca(tmp_path)))
        assert proc.returncode == 0, proc.stderr
        argv = (tmp_path / "curl-argv.log").read_text(encoding="utf-8")
        stdin_data = (tmp_path / "curl-stdin.log").read_text(encoding="utf-8")
        assert f"X-Key: {self.SECRET}" in stdin_data
        assert self.SECRET not in argv
        argv_tokens = argv.split()
        assert "--fail" in argv_tokens
        assert "--header" in argv_tokens and "@-" in argv_tokens
        if self._needs_ca(block):
            assert "--cacert" in argv_tokens

    @pytest.mark.parametrize("fake_rc", ["7", "22"])
    @pytest.mark.parametrize("shell", SHELLS)
    @pytest.mark.parametrize("block_idx", [0, 1])
    def test_curl_exit_code_propagates(self, tmp_path, reload_blocks, shell,
                                       block_idx, fake_rc):
        """7 = 连接失败；22 = ``--fail`` 下 HTTP 4xx/5xx 的 curl 退出码。"""
        block = reload_blocks[block_idx]
        proc = self._run(shell, block, tmp_path,
                         self._stdin_lines(block, self.SECRET,
                                           self._valid_ca(tmp_path)),
                         fake_rc=fake_rc)
        assert proc.returncode == int(fake_rc)

    @pytest.mark.parametrize("shell", SHELLS)
    @pytest.mark.parametrize("block_idx", [0, 1])
    def test_empty_key_fails_without_invoking_curl(self, tmp_path,
                                                   reload_blocks, shell,
                                                   block_idx):
        proc = self._run(shell, reload_blocks[block_idx], tmp_path, [""])
        assert proc.returncode != 0
        assert not (tmp_path / "curl-argv.log").exists()

    @pytest.mark.parametrize("shell", SHELLS)
    @pytest.mark.parametrize("block_idx", [0, 1])
    def test_read_eof_fails_without_invoking_curl(self, tmp_path,
                                                  reload_blocks, shell,
                                                  block_idx):
        proc = self._run(shell, reload_blocks[block_idx], tmp_path, [])
        assert proc.returncode != 0
        assert not (tmp_path / "curl-argv.log").exists()

    @pytest.mark.parametrize("shell", SHELLS)
    @pytest.mark.parametrize("ca_input", ["eof", "empty"])
    def test_cacert_block_bad_ca_input_fails_without_curl(self, tmp_path,
                                                          reload_blocks,
                                                          shell, ca_input):
        block = next(b for b in reload_blocks if self._needs_ca(b))
        stdin = [self.SECRET] if ca_input == "eof" else [self.SECRET, ""]
        proc = self._run(shell, block, tmp_path, stdin)
        assert proc.returncode != 0
        assert not (tmp_path / "curl-argv.log").exists()

    @pytest.mark.parametrize("shell", SHELLS)
    @pytest.mark.parametrize("bad_ca_kind", ["missing", "directory"])
    def test_cacert_block_rejects_invalid_ca_path(self, tmp_path,
                                                  reload_blocks, shell,
                                                  bad_ca_kind):
        block = next(b for b in reload_blocks if self._needs_ca(b))
        bad = tmp_path / ("no-such-ca.pem" if bad_ca_kind == "missing"
                          else "ca-as-dir")
        if bad_ca_kind == "directory":
            bad.mkdir()
        proc = self._run(shell, block, tmp_path, [self.SECRET, str(bad)])
        assert proc.returncode != 0
        assert not (tmp_path / "curl-argv.log").exists()
        assert proc.stderr != b""

    @pytest.mark.parametrize("shell", SHELLS)
    @pytest.mark.parametrize("block_idx", [0, 1])
    def test_block_keeps_parent_shell_alive_and_vars_intact(self, tmp_path,
                                                            reload_blocks,
                                                            shell,
                                                            block_idx):
        block = reload_blocks[block_idx]
        prefix = "KEY=parent-key-value\nCA_FILE=parent-ca-value\n"
        suffix = ("echo BLOCK_COMPLETED_MARKER\n"
                  "printf 'K=%s C=%s\\n' \"$KEY\" \"$CA_FILE\"\n")
        proc = self._run(shell, block, tmp_path,
                         self._stdin_lines(block, self.SECRET,
                                           self._valid_ca(tmp_path)),
                         prefix=prefix, suffix=suffix)
        out = proc.stdout.decode("utf-8")
        assert "BLOCK_COMPLETED_MARKER" in out
        assert "K=parent-key-value C=parent-ca-value" in out

    @pytest.mark.parametrize("shell", SHELLS)
    @pytest.mark.parametrize("block_idx", [0, 1])
    def test_failure_path_keeps_parent_alive(self, tmp_path, reload_blocks,
                                             shell, block_idx):
        """失败分支的块内 exit 只退出子 shell：父脚本继续执行 marker。
        （脚本退出码由父脚本最后一条命令决定，存活即目的。）"""
        proc = self._run(shell, reload_blocks[block_idx], tmp_path, [""],
                         suffix="echo BLOCK_COMPLETED_MARKER\n")
        assert "BLOCK_COMPLETED_MARKER" in proc.stdout.decode("utf-8")
