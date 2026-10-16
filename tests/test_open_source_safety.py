"""仓库开源安全基线测试（fix round 1 加固版）。

- ``DEFAULT_GENERAL`` 不得包含静态控制器凭据键；
- 全仓库（tracked + untracked non-ignored）UTF-8 文本不得包含真实用户
  目录、私钥块、疑似凭据赋值、真实 RFC1918 地址或全局唯一 MAC；
- 命中只报告 ``(路径, 规则号)``，绝不回显疑似秘密内容；
- 豁免按 ``(文件, 规则号) -> 文件 SHA-256`` 精确锁定：被豁免文件发生
  任何改动都会使摘要失配而重新报警（可审计），不做整文件跳过；
- 扫描器自身以分段字符串构造规则，源码不含各规则的连续字面量，
  因此不豁免自身。
"""

import hashlib
import re
import subprocess
from pathlib import Path

import pytest

from app.default_config import DEFAULT_GENERAL

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 规则 1：真实用户目录（分段构造避免自命中）
R_USERS = re.compile("/" + "Users/" + r"[^/\s]+/")
# 规则 2：多类 PEM 私钥块（分段构造）
R_PEM = re.compile("BEGIN " + r"(?:RSA |EC |DSA |OPENSSH |ENCRYPTED |)PRIVATE KEY")
# 规则 3：MITM 证书 / 已废除的静态 API Token 赋值（分段构造键名）
R_SECRET_ASSIGN = re.compile(
    r"(?:ca-p12|" + "SURGE_" + r"API_TOKEN)" + r"\s*[=:]\s*[^\s$<{]", re.I)
# 规则 4：控制器凭据环境变量赋值（分段构造键名）
R_CTRL_ENV_ASSIGN = re.compile(
    "(?:SURGE_" + "CONTROLLER_ACCESS" + r"|SURGE_" + "HTTP_API)"
    + r"\s*[=:]\s*[^\s$<{]")
# 规则 5：General 控制器键的真实值赋值（example- 占位值除外；分段构造键名）
R_CTRL_KEY_ASSIGN = re.compile(
    "(?:external-controller" + r"-access|http" + r"-api)"
    + r"\s*[=:]\s*(?!example-)[^\s<{]")
# 规则 6：真实 RFC1918 具体地址（带 CIDR 掩码的保留段示例不算）
R_RFC1918 = re.compile(
    r"\b(?:(?:10|192" + r"\.168)"
    r"\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})"
    r"(?![\d/])")
# 规则 7：全局唯一（非 locally administered、非全零）MAC 地址
R_MAC = re.compile(
    r"(?<![0-9A-Fa-f:])"
    r"(?![0-9A-Fa-f][26AaEe]:)"        # U/L 位置位 = 本地管理（合成）
    r"(?!00(?::00){5})"                 # 全零
    r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}"
    r"(?![0-9A-Fa-f:])")

FORBIDDEN = [R_USERS, R_PEM, R_SECRET_ASSIGN, R_CTRL_ENV_ASSIGN,
             R_CTRL_KEY_ASSIGN, R_RFC1918, R_MAC]

# (相对路径, 规则号) -> 该文件当前 SHA-256。
# 仅豁免「指定规则」，文件内其他规则仍扫描；文件一旦变更，
# SHA 失配 → 豁免失效 → 测试失败，强制人工重新审查后更新摘要。
RULE_EXEMPTIONS: dict[tuple[str, int], str] = {
    # 合成 MITM 夹具：ca-p12 = <合成 base64>（规则 3）
    ("tests/fixtures/synthetic_profile.conf", 3): "6d0b5eeddc4c7670f03b486dd24836e33b155d17c8ea1e4d5952510cb4cfb103",
    # launchd 行为测试：合成凭据赋值断言行（规则 4）
    ("tests/test_launchd_scripts.py", 4): "8cebff203cee00bc210b51e99e67894f87f185facc78e77245aaf07c3795a517",
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

    豁免按 ``(路径, 规则号)`` 精确匹配且要求文件 SHA-256 与登记值一致；
    整文件级 allow/exclude 不存在。
    """
    exemptions = exemptions if exemptions is not None else RULE_EXEMPTIONS
    violations = []
    for name in files:
        text = (PROJECT_ROOT / name).read_text(encoding="utf-8")
        for rule_id, pattern in enumerate(rules, start=1):
            if not pattern.search(text):
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
