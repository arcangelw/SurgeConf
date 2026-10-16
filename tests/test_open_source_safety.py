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


# 与 docs/RUNTIME_OPERATIONS.md 第 3 节两个 reload 代码块逐字同步的标准
# 形态（结构性对照样例：这类「子 shell 内 unset→隐藏 read→stdin header
# curl→捕获 rc→unset→exit rc」写法必须始终通过下方扫描器，防止规则
# 收紧时误伤合法示例；文档块更新时同步此处）。
_TRUSTED_KEYCHAIN_RELOAD_BLOCK = """\\
  (                                # 子 shell：块内 exit 不退出当前交互 shell
    unset KEY                      # 先清防继承已 export 的旧值
    read -sr KEY                   # 手动输入 key：不回显、不进 shell 历史
    printf 'X-Key: %s\\n' "$KEY" | curl -sS -X POST --header @- \\
      https://127.0.0.1:6171/v1/profiles/reload
    rc=$?                          # 捕获 curl 退出码后再清理
    unset KEY                      # 用完即清
    exit "$rc"                     # curl 结果带回当前 shell 的 $?
  )"""

_CACERT_FALLBACK_RELOAD_BLOCK = """\\
  (                                # 自包含：不依赖上一块，可单独复制执行
    unset KEY CA_FILE              # 先清防继承已 export 的旧值
    read -sr KEY                   # 手动输入 key：不回显、不进 shell 历史
    read -r CA_FILE                # 输入本机真实的 CA 文件路径
    [ -f "$CA_FILE" ] || { echo "CA 文件不存在或不是普通文件" >&2; \\
      unset KEY CA_FILE; exit 1; } # 验证失败同样清理 KEY/CA_FILE 再退出
    printf 'X-Key: %s\\n' "$KEY" | curl -sS -X POST --header @- \\
      --cacert "$CA_FILE" \\
      https://127.0.0.1:6171/v1/profiles/reload
    rc=$?                          # 捕获 curl 退出码后再清理
    unset KEY CA_FILE              # 用完即清
    exit "$rc"                     # curl 结果带回当前 shell 的 $?
  )"""


class TestHttpApiExampleSafety:
    """Surge HTTP API / curl 示例片段的结构性安全约束（Fix Round 4）。

    解析 markdown 的 shell 类 fenced 代码块（bash/sh/shell/zsh，info
    string 大小写归一），行尾反斜杠续行拼接为完整逻辑命令，再用
    ``shlex.shlex(..., punctuation_chars="|&;()<>")`` 分词——无空格的
    ``|`` / ``&&`` / ``;`` 同样成为独立命令边界。对逻辑命令中的
    **每一个** curl 命令段（含 ``/usr/bin/curl`` 等绝对路径形式、
    一行多个 curl）独立检测：

    - TLS：``--insecure``、独立 ``-k``、含 k 的组合短选项（``-sk`` /
      ``-fsSk``）；``--header`` / ``--cacert`` 等长选项与 ``-H<值>``
      附着形式（含 ``:`` 或空格）天然不匹配短选项模式；
    - X-Key argv：``-H 值`` / ``--header 值`` / ``-H值`` /
      ``--header=值`` 四种形式携带 X-Key 头（头名大小写不敏感）一律
      拒绝；唯一放行 stdin 形式 ``@-``；任何 ``@文件`` 头来源（防
      临时 secret 文件）也拒绝；
    - 6171：curl 任一 arg（含 ``--url`` 值与位置 URL）出现明文
      ``http://`` 即拒绝。

    失败消息只含 ``(文件, 规则类别)``，绝不回显命令或 header 内容；
    检测结构行为，不绑定具体措辞。
    """

    SHELL_LANGS = frozenset({"bash", "sh", "shell", "zsh"})
    _PUNCT = frozenset("|&;()<>")

    RULE_INSECURE = "insecure-curl"
    RULE_KEY_ARGV = "x-key-argv"
    RULE_HEADER_FILE = "header-from-file"
    RULE_PLAINTEXT_HTTP = "6171-plaintext-http"

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
        return bool(token) and all(c in cls._PUNCT for c in token)

    @classmethod
    def _curl_segments(cls, tokens):
        """逻辑命令中**每一个** curl 命令段的 argv（管道/&&/; 等边界
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
    def _is_insecure_flag(token: str) -> bool:
        if token in ("--insecure", "-k"):
            return True
        # 组合短选项：单 - 后纯字母且含 k（-sk / -fsSk 等）；
        # --header/--cacert 等长选项与 -H<值> 附着（含 : 或空格）不匹配
        return bool(re.fullmatch(r"-[a-zA-Z]*k[a-zA-Z]*", token))

    @staticmethod
    def _header_values(argv):
        """argv 中 -H/--header 的全部值（分离 / 附着 / = 三类形式）。"""
        values = []
        i = 0
        while i < len(argv):
            tok = argv[i]
            if tok in ("-H", "--header"):
                if i + 1 < len(argv):
                    values.append(argv[i + 1])
                    i += 1
            elif tok.startswith("--header="):
                values.append(tok[len("--header="):])
            elif tok.startswith("-H") and len(tok) > 2:
                values.append(tok[2:])
            i += 1
        return values

    @classmethod
    def _segment_violations(cls, argv):
        """单 curl 段命中的规则类别集合。"""
        found = set()
        if any(cls._is_insecure_flag(t) for t in argv):
            found.add(cls.RULE_INSECURE)
        for value in cls._header_values(argv):
            if value.startswith("@"):
                if value != "@-":
                    found.add(cls.RULE_HEADER_FILE)
            elif value.split(":", 1)[0].strip().lower() == "x-key":
                found.add(cls.RULE_KEY_ARGV)
        joined = " ".join(argv)
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

    @pytest.mark.parametrize("lang,block,expected_rule", INJECTIONS)
    def test_injection_matrix_is_detected(self, tmp_path, lang, block,
                                          expected_rule):
        """矩阵里每类绕过写法都必须被扫描器命中（结构性红）。"""
        doc = tmp_path / "injected.md"
        doc.write_text(f"```{lang}\n{block}\n```\n", encoding="utf-8")
        hits = self._violations([(str(doc), doc)])
        assert (str(doc), expected_rule) in hits, (
            f"注入样例未被检出: {hits}")

    def test_documented_reload_blocks_stay_clean(self, tmp_path):
        """文档标准两块（子 shell 自包含形态）必须始终通过扫描器。"""
        doc = tmp_path / "canonical.md"
        doc.write_text(
            "# canonical reload blocks\n\n```bash\n"
            + _TRUSTED_KEYCHAIN_RELOAD_BLOCK + "\n```\n\n```bash\n"
            + _CACERT_FALLBACK_RELOAD_BLOCK + "\n```\n",
            encoding="utf-8")
        assert self._violations([(str(doc), doc)]) == []
