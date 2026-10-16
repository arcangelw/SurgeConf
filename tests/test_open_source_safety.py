"""仓库开源安全基线测试（Task 6）。

- ``DEFAULT_GENERAL`` 不得再包含静态控制器凭据键；
- 全仓库（tracked + untracked non-ignored）UTF-8 文本不得包含
  真实用户目录、私钥块或疑似凭据赋值；
- 命中只报告 ``(路径, 规则号)``，绝不回显疑似秘密内容。
"""

import re
import subprocess
from pathlib import Path

import pytest

from app.default_config import DEFAULT_GENERAL

PROJECT_ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN = [
    re.compile("/" + "Users/" + r"[^/\s]+/"),
    re.compile("BEGIN " + r"(?:RSA |EC )?PRIVATE KEY"),
    re.compile(r"(?:ca-p12|SURGE_API_TOKEN)\s*[=:]\s*[^\s$<{]", re.I),
]

EXCLUDED_PATHS = {"tests/test_open_source_safety.py"}
ALLOWED_PATHS = {"tests/fixtures/synthetic_profile.conf"}


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


def scan_files(files, rules, *, excluded=frozenset(), allowlist=frozenset()):
    """对每个文件应用规则，返回 ``(相对路径, 规则号)`` 列表，不回显内容。"""
    violations = []
    for name in files:
        if name in excluded or name in allowlist:
            continue
        text = (PROJECT_ROOT / name).read_text(encoding="utf-8")
        for rule_id, pattern in enumerate(rules, start=1):
            if pattern.search(text):
                violations.append((name, rule_id))
    return violations


def test_default_general_has_no_static_controller_credentials():
    assert not {"external-controller-access", "http-api"} & DEFAULT_GENERAL.keys()


def test_repository_text_has_no_machine_or_secret_material(repo_text_files):
    violations = scan_files(
        repo_text_files,
        FORBIDDEN,
        excluded=EXCLUDED_PATHS,
        allowlist=ALLOWED_PATHS,
    )
    assert violations == []
