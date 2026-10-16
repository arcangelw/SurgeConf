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
import re
import shlex
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
    # launchd 行为测试：合成凭据赋值断言行（规则 4）
    ("tests/test_launchd_scripts.py", 4): "e1c11b2a7d10843bab30f4c093b10a7e5d13d66619e927f50af4e09e3a95b293",
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
    """Surge HTTP API / curl 示例片段的结构性安全约束（Fix Round 3）。

    解析 markdown 的 bash 代码块，先把反斜杠续行拼成完整逻辑命令，
    再 ``shlex`` 分词检测 curl argv：``--insecure``、独立 ``-k``、含
    k 的组合短选项（如 ``-sk`` / ``-fsSk``）、X-Key 头值进 argv；
    另按逻辑命令（含续行拼接结果）检查示例端口 6171 不得出现明文
    ``http://``。检测的是结构行为，不绑定具体措辞。
    """

    @staticmethod
    def _bash_logical_lines(files):
        """全部 md 文档 ```bash 代码块内续行拼接后的逻辑命令。"""
        for name in files:
            if not (name.endswith(".md") or name == "README.md"):
                continue
            text = (PROJECT_ROOT / name).read_text(encoding="utf-8")
            in_block = False
            buffer: list[str] = []
            for raw in text.splitlines():
                stripped = raw.strip()
                if not in_block:
                    if stripped.startswith("```bash"):
                        in_block = True
                        buffer = []
                    continue
                if stripped.startswith("```"):
                    in_block = False
                    for line in TestHttpApiExampleSafety._joined(buffer):
                        yield name, line
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
    def _curl_argv(logical_line):
        """逻辑命令中 curl 的 argv 段（到下一个管道边界）；无 curl 为 None。"""
        try:
            tokens = shlex.split(logical_line)
        except ValueError:
            tokens = logical_line.split()
        if "curl" not in tokens:
            return None
        argv = []
        for token in tokens[tokens.index("curl") + 1:]:
            if token == "|":
                break
            argv.append(token)
        return argv

    @staticmethod
    def _is_insecure_flag(token: str) -> bool:
        if token in ("--insecure", "-k"):
            return True
        # 组合短选项：单 - 后纯字母且含 k（-sk / -fsSk 等）；
        # --header/--cacert 等长选项以 -- 开头，天然不匹配
        return bool(re.fullmatch(r"-[a-zA-Z]*k[a-zA-Z]*", token))

    def test_no_insecure_curl_flags(self, repo_text_files):
        offenders = []
        for name, line in self._bash_logical_lines(repo_text_files):
            argv = self._curl_argv(line)
            if argv and any(self._is_insecure_flag(t) for t in argv):
                offenders.append(f"{name}: {line[:90]}")
        assert offenders == [], f"curl 跳过证书校验的示例: {offenders}"

    def test_no_key_in_curl_argv(self, repo_text_files):
        offenders = []
        for name, line in self._bash_logical_lines(repo_text_files):
            argv = self._curl_argv(line)
            if argv and any(t.startswith("X-Key") for t in argv):
                offenders.append(f"{name}: {line[:90]}")
        assert offenders == [], f"X-Key 经 curl argv 注入: {offenders}"

    def test_http_api_port_never_plaintext_http(self, repo_text_files):
        offenders = [
            f"{name}: {line[:90]}"
            for name, line in self._bash_logical_lines(repo_text_files)
            if "6171" in line and "http://" in line]
        assert offenders == [], (
            f"默认 http-api-tls=true 的示例端口出现明文 http://: {offenders}")
