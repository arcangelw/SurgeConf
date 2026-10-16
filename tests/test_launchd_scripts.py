"""launchd 安装/持久化脚本行为测试（fix round 1）。

直接 source ``launchd_lib.sh`` 验证函数行为；launchctl 用 PATH 注入的
fake 可执行文件替代（带状态 marker，load 后 list 反映已加载），
绝不真实 load/unload，不污染用户现有 job。
"""

import plistlib
import subprocess
import textwrap
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LIB = PROJECT_ROOT / "launchd_lib.sh"

FAKE_LAUNCHCTL = r"""#!/bin/sh
echo "$@" >> "${FAKE_LAUNCHCTL_LOG:-/dev/null}"
case "$1" in
  list)
    if [ -f "${FAKE_LAUNCHCTL_STATE:-/nonexistent}" ]; then
      printf -- '-\t0\tcom.surgeconf\n'
    fi
    exit 0
    ;;
  load)
    rc="${FAKE_LAUNCHCTL_LOAD_RC:-0}"
    [ "$rc" = "0" ] && : > "${FAKE_LAUNCHCTL_STATE:-/dev/null}"
    exit "$rc"
    ;;
  unload)
    rc="${FAKE_LAUNCHCTL_UNLOAD_RC:-0}"
    [ "$rc" = "0" ] && rm -f "${FAKE_LAUNCHCTL_STATE:-/nonexistent}"
    exit "$rc"
    ;;
esac
exit 0
"""


def run_bash(script: str, tmp_path: Path, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "LIB": str(LIB),
    }
    env.update(extra_env or {})
    return subprocess.run(["bash", "-c", textwrap.dedent(script)],
                          capture_output=True, text=True, env=env)


@pytest.fixture
def fake_launchctl(tmp_path):
    """PATH 前置 fake launchctl；返回 (调用日志, env 片段, state 文件)"""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "launchctl").write_text(FAKE_LAUNCHCTL)
    (bindir / "launchctl").chmod(0o755)
    log = tmp_path / "launchctl.log"
    state = tmp_path / "loaded.marker"
    env = {"PATH": f"{bindir}:/usr/bin:/bin",
           "FAKE_LAUNCHCTL_LOG": str(log),
           "FAKE_LAUNCHCTL_STATE": str(state)}
    return log, env, state


def _paths(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    data_dir = home / "Library" / "Application Support" / "SurgeConf"
    env_file = data_dir / "launchd.env"
    plist = home / "Library" / "LaunchAgents" / "com.surgeconf.plist"
    log_file = home / "Library" / "Logs" / "SurgeConf" / "surgeconf.log"
    return home, data_dir, env_file, plist, log_file


def _prepare(tmp_path: Path):
    """预建 LaunchAgents + Application Support，返回 _paths 五元组"""
    home, data_dir, env_file, plist, log_file = _paths(tmp_path)
    plist.parent.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    return home, data_dir, env_file, plist, log_file


# ===== launchd.env 写入/读取语义 =====


def test_env_write_persists_nonempty_and_clears_empty(tmp_path):
    home, *_ = _paths(tmp_path)
    r = run_bash(r"""
        source "$LIB"
        export SURGE_HOST=0.0.0.0 SURGE_PORT=61900 SURGECONF_DATA_DIR="$HOME/custom"
        export SURGE_CONTROLLER_ACCESS='example-access-key@127.0.0.1:6160'
        unset SURGE_HTTP_API
        sc_write_launchd_env "$HOME/env"
        cat "$HOME/env"
    """, tmp_path)
    assert r.returncode == 0, r.stderr
    lines = set(r.stdout.strip().splitlines())
    assert lines == {
        "SURGE_HOST=0.0.0.0",
        "SURGE_PORT=61900",
        f"SURGECONF_DATA_DIR={home}/custom",
        "SURGE_CONTROLLER_ACCESS=example-access-key@127.0.0.1:6160",
    }  # SURGE_HTTP_API 未设置 → 不出现

    # 显式空值 → 键被移除（等效清除）
    r2 = run_bash(r"""
        source "$LIB"
        export SURGE_HOST= SURGE_PORT=61900
        unset SURGE_CONTROLLER_ACCESS SURGECONF_DATA_DIR
        sc_write_launchd_env "$HOME/env"
        cat "$HOME/env"
    """, tmp_path)
    assert r2.returncode == 0, r2.stderr
    assert r2.stdout.strip().splitlines() == ["SURGE_PORT=61900"]


def test_env_file_permissions_are_0600(tmp_path):
    r = run_bash(r"""
        source "$LIB"
        export SURGE_PORT=61830
        sc_write_launchd_env "$HOME/env"
        stat -f '%Lp' "$HOME/env"
    """, tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "600"


def test_env_read_rejects_symlink(tmp_path):
    r = run_bash(r"""
        source "$LIB"
        echo evil > "$HOME/real"
        ln -s "$HOME/real" "$HOME/env"
        sc_read_launchd_env "$HOME/env"
    """, tmp_path)
    assert r.returncode != 0
    assert "symlink" in r.stderr


def test_env_read_rejects_unknown_key_and_cr(tmp_path):
    r = run_bash(r"""
        source "$LIB"
        printf 'SURGE_HOST=0.0.0.0\nPATH=/tmp\n' > "$HOME/env"
        sc_read_launchd_env "$HOME/env"
    """, tmp_path)
    assert r.returncode != 0
    assert "未知键" in r.stderr

    r2 = run_bash(r"""
        source "$LIB"
        printf 'SURGE_HOST=0.0.0.0\r\n' > "$HOME/env"
        sc_read_launchd_env "$HOME/env"
    """, tmp_path)
    assert r2.returncode != 0
    assert "CR" in r2.stderr


def test_env_read_exports_values(tmp_path):
    r = run_bash(r"""
        source "$LIB"
        printf 'SURGE_HOST=127.0.0.1\nSURGE_CONTROLLER_ACCESS=k@example.com:6160\n' > "$HOME/env"
        sc_read_launchd_env "$HOME/env"
        printf 'host=%s cred=%s\n' "$SURGE_HOST" "$SURGE_CONTROLLER_ACCESS"
    """, tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "host=127.0.0.1 cred=k@example.com:6160"


# ===== plist 生成 =====


def test_plist_structure_and_defaults(tmp_path):
    home, *_ = _paths(tmp_path)
    r = run_bash(f"""
        source "$LIB"
        sc_generate_plist "$HOME/p/com.surgeconf.plist" "/proj&dir" \\
            "$HOME/Library/Logs/SurgeConf/surgeconf.log" 127.0.0.1 61830 \\
            "$HOME/Library/Application Support/SurgeConf"
        stat -f '%Lp' "$HOME/p/com.surgeconf.plist"
    """, tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "600"
    data = plistlib.loads((home / "p" / "com.surgeconf.plist").read_bytes())
    assert data["Label"] == "com.surgeconf"
    assert data["Umask"] == 63
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] is True
    assert data["ProgramArguments"] == ["/proj&dir/run_surgeconf.sh"]
    assert data["StandardOutPath"] == str(home / "Library/Logs/SurgeConf/surgeconf.log")
    assert data["StandardErrorPath"] == data["StandardOutPath"]
    assert data["EnvironmentVariables"] == {
        "SURGE_HOST": "127.0.0.1",
        "SURGE_PORT": "61830",
        "SURGECONF_DATA_DIR": str(home / "Library/Application Support/SurgeConf"),
    }


def test_plist_escapes_xml_and_rewrites_legacy_fields(tmp_path):
    home, *_ = _paths(tmp_path)
    pdir = home / "p"
    pdir.mkdir(parents=True, exist_ok=True)
    # 旧 plist：0.0.0.0 + 仓库 logs 路径（升级遗留形态）
    legacy = pdir / "com.surgeconf.plist"
    legacy.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
  <key>Label</key><string>com.surgeconf</string>
  <key>StandardOutPath</key><string>/repo/logs/surgeconf.log</string>
  <key>EnvironmentVariables</key><dict>
    <key>SURGE_HOST</key><string>0.0.0.0</string>
  </dict></dict></plist>
""")
    # 目录名含 XML 特殊字符，验证转义
    weird = home / "a&b<c>"
    weird.mkdir()
    r = run_bash(f"""
        source "$LIB"
        sc_generate_plist "{legacy}" "{weird}" "$HOME/Library/Logs/SurgeConf/surgeconf.log" \\
            127.0.0.1 61830 "$HOME/Library/Application Support/SurgeConf"
    """, tmp_path)
    assert r.returncode == 0, r.stderr
    data = plistlib.loads(legacy.read_bytes())
    assert data["EnvironmentVariables"]["SURGE_HOST"] == "127.0.0.1"       # 0.0.0.0 被重写
    assert "SurgeConf/surgeconf.log" in data["StandardOutPath"]            # repo logs 被重写
    assert data["ProgramArguments"] == [f"{weird}/run_surgeconf.sh"]       # & < > 转义成功


def test_plist_rejects_control_characters_and_bad_port(tmp_path):
    r = run_bash(r"""
        source "$LIB"
        HOST="$(printf 'a\tb')"
        sc_generate_plist "$HOME/p/x.plist" "/proj" "$HOME/log" "$HOST" 61830 "$HOME/d"
    """, tmp_path)
    assert r.returncode != 0
    assert "控制字符" in r.stderr

    r2 = run_bash(r"""
        source "$LIB"
        sc_generate_plist "$HOME/p/x.plist" "/proj" "$HOME/log" 127.0.0.1 61a30 "$HOME/d"
    """, tmp_path)
    assert r2.returncode != 0


def test_log_file_is_0600_and_symlink_rejected(tmp_path):
    r = run_bash(r"""
        source "$LIB"
        sc_ensure_log_file "$HOME/surgeconf.log"
        stat -f '%Lp' "$HOME/surgeconf.log"
        echo decoy > "$HOME/real.log"
        ln -s "$HOME/real.log" "$HOME/link.log"
        sc_ensure_log_file "$HOME/link.log" && echo BAD || echo REJECTED
    """, tmp_path)
    assert r.returncode == 0, r.stderr
    out = r.stdout.strip().splitlines()
    assert out[0] == "600"
    assert out[-1] == "REJECTED"


# ===== 统一 install 流程（fake launchctl） =====


def _install_script():
    return r"""
        source "$LIB"
        sc_install_launchd "$HOME/Library/LaunchAgents/com.surgeconf.plist" "/proj" \
            "$HOME/Library/Application Support/SurgeConf" \
            "$HOME/Library/Application Support/SurgeConf/launchd.env" \
            "$HOME/Library/Logs/SurgeConf/surgeconf.log"
    """


def test_install_regenerates_legacy_plist_to_safe_defaults(tmp_path, fake_launchctl):
    log, env, state = fake_launchctl
    home, _, _, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)
    state.write_text("loaded")                          # 模拟已加载
    plist.write_text("<plist><dict><key>Label</key><string>com.surgeconf</string>"
                     "<key>StandardOutPath</key><string>/repo/logs/surgeconf.log</string>"
                     "<key>EnvironmentVariables</key><dict>"
                     "<key>SURGE_HOST</key><string>0.0.0.0</string>"
                     "</dict></dict></plist>")           # 旧 plist 遗留字段

    r = run_bash(_install_script(), tmp_path, extra_env={**env, "SURGE_HOST": ""})
    assert r.returncode == 0, r.stderr
    calls = [c for c in log.read_text().strip().splitlines() if c != "list"]
    assert calls[0] == f"unload {plist}"                # 已加载 → 先卸载
    assert calls[-1] == f"load {plist}"                 # 重新 load
    data = plistlib.loads(plist.read_bytes())
    assert data["EnvironmentVariables"]["SURGE_HOST"] == "127.0.0.1"  # 显式空→安全默认
    assert data["Umask"] == 63
    assert state.exists()                               # load 成功后仍处于已加载态


def test_install_unset_keeps_and_explicit_empty_clears(tmp_path, fake_launchctl):
    """unset（未提供）保留持久化值；显式空值清除回默认 —— 更新/清除语义"""
    log, env, state = fake_launchctl
    home, _, env_file, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)

    # 持久化一个显式值
    r = run_bash(_install_script(), tmp_path,
                 extra_env={**env, "SURGE_HOST": "192.0.2.10"})
    assert r.returncode == 0, r.stderr
    assert "SURGE_HOST=192.0.2.10" in env_file.read_text()

    # unset（未提供）→ 保留（restart 场景）
    r2 = run_bash(_install_script(), tmp_path, extra_env=env)
    assert r2.returncode == 0, r2.stderr
    assert "SURGE_HOST=192.0.2.10" in env_file.read_text()
    assert plistlib.loads(plist.read_bytes())["EnvironmentVariables"]["SURGE_HOST"] == "192.0.2.10"

    # 显式空（SURGE_HOST=）→ 清除，plist 回默认 127.0.0.1
    r3 = run_bash(_install_script(), tmp_path, extra_env={**env, "SURGE_HOST": ""})
    assert r3.returncode == 0, r3.stderr
    assert "SURGE_HOST=" not in env_file.read_text()
    assert plistlib.loads(plist.read_bytes())["EnvironmentVariables"]["SURGE_HOST"] == "127.0.0.1"


def test_install_propagates_unload_and_load_failures(tmp_path, fake_launchctl):
    log, env, state = fake_launchctl
    home, _, _, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)
    state.write_text("loaded")

    env["FAKE_LAUNCHCTL_UNLOAD_RC"] = "1"
    r = run_bash(_install_script(), tmp_path, extra_env=env)
    assert r.returncode != 0                            # unload 失败传播，不谎报成功

    env["FAKE_LAUNCHCTL_UNLOAD_RC"] = "0"
    env["FAKE_LAUNCHCTL_LOAD_RC"] = "1"
    r2 = run_bash(_install_script(), tmp_path, extra_env=env)
    assert r2.returncode != 0                           # load 失败传播


def test_install_skips_unload_when_not_loaded(tmp_path, fake_launchctl):
    log, env, state = fake_launchctl
    home, _, _, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)
    r = run_bash(_install_script(), tmp_path, extra_env=env)
    assert r.returncode == 0, r.stderr
    calls = [c for c in log.read_text().strip().splitlines() if c != "list"]
    assert calls == [f"load {plist}"]                   # 无 unload 调用


def test_install_keeps_credentials_out_of_plist(tmp_path, fake_launchctl):
    log, env, state = fake_launchctl
    home, _, env_file, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)
    r = run_bash(_install_script(), tmp_path, extra_env={
        **env,
        "SURGE_CONTROLLER_ACCESS": "secret-key@192.0.2.10:6160",
        "SURGE_HTTP_API": "secret-key@192.0.2.10:6166",
    })
    assert r.returncode == 0, r.stderr
    plist_text = plist.read_text()
    assert "secret-key" not in plist_text               # 凭据绝不进 plist
    assert "SURGE_CONTROLLER_ACCESS" not in plist_text
    env_content = env_file.read_text()
    assert "SURGE_CONTROLLER_ACCESS=secret-key@192.0.2.10:6160" in env_content  # 只在 env 文件
    assert env_file.stat().st_mode & 0o777 == 0o600
