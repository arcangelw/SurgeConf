"""launchd 安装/持久化脚本行为测试（fix round 2）。

直接 source ``launchd_lib.sh`` 验证函数行为；入口级测试执行真实
``surgeconf.sh start/restart``。launchctl 一律用 PATH 注入的 fake
可执行文件替代（支持 ``launchctl list <label>`` 精确 exit status），
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
    if [ -n "$2" ]; then
      # 精确 label 查询：输出 PID\tStatus\tLabel，exit status 表达加载状态；
      # state 文件内容为 "running" 时第一列为数字 PID，其余（含空）为 "-"
      if [ -f "${FAKE_LAUNCHCTL_STATE:-/nonexistent}" ]; then
        if [ "$(cat "${FAKE_LAUNCHCTL_STATE}")" = "running" ]; then
          printf -- '12345\t0\t%s\n' "$2"
        else
          printf -- '-\t0\t%s\n' "$2"
        fi
        exit 0
      fi
      exit 1
    fi
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


def run_script(args: list[str], tmp_path: Path, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """执行真实 surgeconf.sh 入口（fake launchctl PATH 注入）"""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
    }
    env.update(extra_env or {})
    return subprocess.run(["bash", str(PROJECT_ROOT / "surgeconf.sh"), *args],
                          capture_output=True, text=True, env=env, cwd=str(tmp_path))


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
    control_dir = home / "Library" / "Application Support" / "SurgeConf"
    control_env = control_dir / "launchd.env"
    plist = home / "Library" / "LaunchAgents" / "com.surgeconf.plist"
    log_file = home / "Library" / "Logs" / "SurgeConf" / "surgeconf.log"
    return home, control_dir, control_env, plist, log_file


def _prepare(tmp_path: Path):
    home, control_dir, control_env, plist, log_file = _paths(tmp_path)
    plist.parent.mkdir(parents=True, exist_ok=True)
    control_dir.mkdir(parents=True, exist_ok=True)
    return home, control_dir, control_env, plist, log_file


# ===== 控制文件写入/读取语义 =====


def test_env_write_persists_nonempty_and_clears_empty(tmp_path):
    home, *_ = _paths(tmp_path)
    r = run_bash(r"""
        source "$LIB"
        export SURGE_HOST=0.0.0.0 SURGE_PORT=61900 SURGECONF_DATA_DIR="$HOME/custom-data"
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
        f"SURGECONF_DATA_DIR={home}/custom-data",
        "SURGE_CONTROLLER_ACCESS=example-access-key@127.0.0.1:6160",
    }  # SURGE_HTTP_API 未设置 → 不出现

    r2 = run_bash(r"""
        source "$LIB"
        export SURGE_HOST= SURGE_PORT=61900
        unset SURGE_CONTROLLER_ACCESS SURGECONF_DATA_DIR
        sc_write_launchd_env "$HOME/env"
        cat "$HOME/env"
    """, tmp_path)
    assert r2.returncode == 0, r2.stderr
    assert r2.stdout.strip().splitlines() == ["SURGE_PORT=61900"]


def test_env_write_rejects_control_chars_before_writing(tmp_path):
    """值含 LF/CR/TAB/DEL/C1：写入前失败，不生成文件（printf 构造真实控制字符）"""
    cases = [
        "k@h:6160\\nSURGE_HOST=0.0.0.0",   # LF 注入第二个键
        "k@h:6160\\r\\nx",               # CRLF
        "k@h:61\\t60",                     # TAB
        "k@h:6160\\x7f",                   # DEL
        "k@h:61\\x9660",                   # C1
    ]
    for bad in cases:
        r = run_bash(f"""
            source "$LIB"
            export SURGE_CONTROLLER_ACCESS="$(printf '{bad}')"
            sc_write_launchd_env "$HOME/env"
            echo "wrote: $?"
            ls "$HOME/env" 2>/dev/null && echo FILE_EXISTS || echo NO_FILE
        """, tmp_path)
        assert r.returncode == 0, r.stderr
        out = r.stdout
        assert "wrote: 1" in out, f"{bad} 应写前失败: {out}"
        assert "NO_FILE" in out, f"{bad} 不应生成文件"


def test_env_write_controller_injection_cannot_add_surge_host(tmp_path):
    """controller 值注入第二个 SURGE_HOST 键：必须写前失败，不改监听"""
    home, control_dir, control_env, plist, _ = _prepare(tmp_path)
    # 先持久化一个安全监听
    r0 = run_bash(r"""
        source "$LIB"
        export SURGE_HOST=127.0.0.1 SURGE_PORT=61830
        sc_write_launchd_env "$HOME/env0"
    """, tmp_path)
    assert r0.returncode == 0, r0.stderr
    (home / "env0").rename(control_env)

    r = run_bash(r"""
        source "$LIB"
        export SURGE_CONTROLLER_ACCESS='k@h:6160
SURGE_HOST=0.0.0.0'
        sc_write_launchd_env "$HOME/env"
        echo "rc=$?"
    """, tmp_path)
    assert "rc=1" in r.stdout or r.returncode != 0, r.stdout + r.stderr
    # 原控制文件未被触碰：监听仍为安全值
    content = control_env.read_text()
    assert "SURGE_HOST=127.0.0.1" in content
    assert "0.0.0.0" not in content
    # 注入文件未生成
    assert not (home / "env").exists()


def test_env_file_permissions_are_0600(tmp_path):
    r = run_bash(r"""
        source "$LIB"
        export SURGE_PORT=61830
        sc_write_launchd_env "$HOME/env"
        stat -f '%Lp' "$HOME/env"
    """, tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "600"


def test_env_read_rejects_symlink_and_non_regular(tmp_path):
    r = run_bash(r"""
        source "$LIB"
        echo evil > "$HOME/real"
        ln -s "$HOME/real" "$HOME/env"
        sc_read_launchd_env "$HOME/env"
    """, tmp_path)
    assert r.returncode != 0
    assert "symlink" in r.stderr

    r2 = run_bash(r"""
        source "$LIB"
        mkfifo "$HOME/pipe" 2>/dev/null || python3 -c "import os; os.mkfifo('$HOME/pipe')"
        sc_read_launchd_env "$HOME/pipe"
    """, tmp_path)
    assert r2.returncode != 0
    assert "普通文件" in r2.stderr


def test_env_read_rejects_unknown_duplicate_and_control(tmp_path):
    r = run_bash(r"""
        source "$LIB"
        printf 'SURGE_HOST=127.0.0.1\nPATH=/tmp\n' > "$HOME/env"
        sc_read_launchd_env "$HOME/env"
    """, tmp_path)
    assert r.returncode != 0
    assert "未知键" in r.stderr

    r2 = run_bash(r"""
        source "$LIB"
        printf 'SURGE_HOST=127.0.0.1\nSURGE_HOST=0.0.0.0\n' > "$HOME/env"
        sc_read_launchd_env "$HOME/env"
    """, tmp_path)
    assert r2.returncode != 0
    assert "重复键" in r2.stderr

    r3 = run_bash(r"""
        source "$LIB"
        printf 'SURGE_HOST=127.0.0.1\r\n' > "$HOME/env"
        sc_read_launchd_env "$HOME/env"
    """, tmp_path)
    assert r3.returncode != 0


def test_env_read_exports_values_and_converges_0600(tmp_path):
    r = run_bash(r"""
        source "$LIB"
        printf 'SURGE_HOST=127.0.0.1\nSURGE_CONTROLLER_ACCESS=k@example.com:6160\n' > "$HOME/env"
        chmod 644 "$HOME/env"
        sc_read_launchd_env "$HOME/env"
        printf 'host=%s cred=%s\n' "$SURGE_HOST" "$SURGE_CONTROLLER_ACCESS"
        stat -f '%Lp' "$HOME/env"
    """, tmp_path)
    assert r.returncode == 0, r.stderr
    out = r.stdout.strip().splitlines()
    assert out[0] == "host=127.0.0.1 cred=k@example.com:6160"
    assert out[1] == "600"  # 读前收敛


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
    legacy = pdir / "com.surgeconf.plist"
    legacy.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
  <key>Label</key><string>com.surgeconf</string>
  <key>StandardOutPath</key><string>/repo/logs/surgeconf.log</string>
  <key>EnvironmentVariables</key><dict>
    <key>SURGE_HOST</key><string>0.0.0.0</string>
  </dict></dict></plist>
""")
    weird = home / "a&b<c>"
    weird.mkdir()
    r = run_bash(f"""
        source "$LIB"
        sc_generate_plist "{legacy}" "{weird}" "$HOME/Library/Logs/SurgeConf/surgeconf.log" \\
            127.0.0.1 61830 "$HOME/Library/Application Support/SurgeConf"
    """, tmp_path)
    assert r.returncode == 0, r.stderr
    data = plistlib.loads(legacy.read_bytes())
    assert data["EnvironmentVariables"]["SURGE_HOST"] == "127.0.0.1"
    assert "SurgeConf/surgeconf.log" in data["StandardOutPath"]
    assert data["ProgramArguments"] == [f"{weird}/run_surgeconf.sh"]


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


def test_log_and_directory_guards(tmp_path):
    r = run_bash(r"""
        source "$LIB"
        sc_ensure_log_file "$HOME/surgeconf.log"
        stat -f '%Lp' "$HOME/surgeconf.log"
        echo decoy > "$HOME/real.log"
        ln -s "$HOME/real.log" "$HOME/link.log"
        sc_ensure_log_file "$HOME/link.log" && echo BAD || echo REJECTED
        mkfifo "$HOME/pipe" 2>/dev/null || python3 -c "import os; os.mkfifo('$HOME/pipe')"
        sc_ensure_log_file "$HOME/pipe" && echo BAD2 || echo REJECTED2
        ln -s /tmp "$HOME/dirlink"
        sc_ensure_directory "$HOME/dirlink" && echo BAD3 || echo REJECTED3
        sc_ensure_directory "$HOME/newdir"
        stat -f '%Lp' "$HOME/newdir"
    """, tmp_path)
    assert r.returncode == 0, r.stderr
    out = r.stdout.strip().splitlines()
    assert out[0] == "600"
    assert "REJECTED" in out and "BAD" not in out
    assert "REJECTED2" in out and "BAD2" not in out
    assert "REJECTED3" in out and "BAD3" not in out
    assert out[-1] == "700"


# ===== 函数级 install 流程 =====


def _install_script():
    return r"""
        source "$LIB"
        sc_install_launchd "$HOME/Library/LaunchAgents/com.surgeconf.plist" "/proj" \
            "$HOME/Library/Logs/SurgeConf/surgeconf.log"
    """


def test_install_unset_keeps_and_explicit_empty_clears(tmp_path, fake_launchctl):
    """unset（未提供）保留持久化值；显式空值清除回默认 —— 更新/清除语义"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)

    r = run_bash(_install_script(), tmp_path,
                 extra_env={**env, "SURGE_HOST": "192.0.2.10"})
    assert r.returncode == 0, r.stderr
    assert "SURGE_HOST=192.0.2.10" in control_env.read_text()

    r2 = run_bash(_install_script(), tmp_path, extra_env=env)
    assert r2.returncode == 0, r2.stderr
    assert "SURGE_HOST=192.0.2.10" in control_env.read_text()
    assert plistlib.loads(plist.read_bytes())["EnvironmentVariables"]["SURGE_HOST"] == "192.0.2.10"

    r3 = run_bash(_install_script(), tmp_path, extra_env={**env, "SURGE_HOST": ""})
    assert r3.returncode == 0, r3.stderr
    assert "SURGE_HOST=" not in control_env.read_text()
    assert plistlib.loads(plist.read_bytes())["EnvironmentVariables"]["SURGE_HOST"] == "127.0.0.1"


def test_install_propagates_unload_and_load_failures(tmp_path, fake_launchctl):
    log, env, state = fake_launchctl
    home, *_ = _prepare(tmp_path)
    env["HOME"] = str(home)
    state.write_text("loaded")

    env["FAKE_LAUNCHCTL_UNLOAD_RC"] = "1"
    r = run_bash(_install_script(), tmp_path, extra_env=env)
    assert r.returncode != 0

    env["FAKE_LAUNCHCTL_UNLOAD_RC"] = "0"
    env["FAKE_LAUNCHCTL_LOAD_RC"] = "1"
    r2 = run_bash(_install_script(), tmp_path, extra_env=env)
    assert r2.returncode != 0


def test_install_keeps_credentials_out_of_plist(tmp_path, fake_launchctl):
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)
    r = run_bash(_install_script(), tmp_path, extra_env={
        **env,
        "SURGE_CONTROLLER_ACCESS": "secret-key@192.0.2.10:6160",
        "SURGE_HTTP_API": "secret-key@192.0.2.10:6166",
    })
    assert r.returncode == 0, r.stderr
    plist_text = plist.read_text()
    assert "secret-key" not in plist_text
    assert "SURGE_CONTROLLER_ACCESS" not in plist_text
    env_content = control_env.read_text()
    assert "SURGE_CONTROLLER_ACCESS=secret-key@192.0.2.10:6160" in env_content
    assert control_env.stat().st_mode & 0o777 == 0o600


def test_install_custom_data_dir_survives_unset_restart(tmp_path, fake_launchctl):
    """custom data dir 安装 → 全 unset restart → 仍指向同一 DB 路径"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)
    custom = home / "custom-data"

    r = run_bash(_install_script(), tmp_path,
                 extra_env={**env, "SURGECONF_DATA_DIR": str(custom)})
    assert r.returncode == 0, r.stderr
    data = plistlib.loads(plist.read_bytes())
    assert data["EnvironmentVariables"]["SURGECONF_DATA_DIR"] == str(custom)
    assert (custom / "surge_conf.db") is not None  # 目录已安全创建
    assert custom.is_dir()

    # 全 unset restart：控制文件持久化的 data dir 不丢
    r2 = run_bash(_install_script(), tmp_path, extra_env=env)
    assert r2.returncode == 0, r2.stderr
    data2 = plistlib.loads(plist.read_bytes())
    assert data2["EnvironmentVariables"]["SURGECONF_DATA_DIR"] == str(custom)
    assert f"SURGECONF_DATA_DIR={custom}" in control_env.read_text()

    # 切换 data dir 时未显式提供的 host/port 保留
    r3 = run_bash(_install_script(), tmp_path,
                  extra_env={**env, "SURGE_HOST": "192.0.2.10",
                             "SURGE_PORT": "61900",
                             "SURGECONF_DATA_DIR": str(custom)})
    assert r3.returncode == 0, r3.stderr
    r4 = run_bash(_install_script(), tmp_path,
                  extra_env={**env, "SURGECONF_DATA_DIR": str(home / "other-data")})
    assert r4.returncode == 0, r4.stderr
    content = control_env.read_text()
    assert f"SURGECONF_DATA_DIR={home}/other-data" in content
    assert "SURGE_HOST=192.0.2.10" in content     # 未显式 → 保留
    assert "SURGE_PORT=61900" in content


# ===== 真实脚本入口（surgeconf.sh + fake launchctl） =====


def test_real_start_with_all_unset_replaces_legacy_job(tmp_path, fake_launchctl):
    """真实 surgeconf.sh start：五变量全 unset + 旧 job 已加载 →
    unload 旧 job → 重写安全 plist → load 成功"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)
    state.write_text("loaded")  # 旧 job 已加载
    plist.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
  <key>Label</key><string>com.surgeconf</string>
  <key>StandardOutPath</key><string>/repo/logs/surgeconf.log</string>
  <key>EnvironmentVariables</key><dict>
    <key>SURGE_HOST</key><string>0.0.0.0</string>
  </dict></dict></plist>
""")

    r = run_script(["start"], tmp_path, extra_env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = [c for c in log.read_text().strip().splitlines()]
    unload_idx = calls.index(f"unload {plist}")
    load_idx = calls.index(f"load {plist}")
    assert unload_idx < load_idx
    assert f"list com.surgeconf" in calls       # 精确 label 查询
    data = plistlib.loads(plist.read_bytes())
    assert data["EnvironmentVariables"]["SURGE_HOST"] == "127.0.0.1"   # 首次无持久配置→安全默认
    assert data["Umask"] == 63
    assert "SurgeConf/surgeconf.log" in data["StandardOutPath"]
    assert state.exists()                        # load 成功后仍加载
    assert control_env.exists()                  # 控制文件创建于固定路径
    assert control_env.stat().st_mode & 0o777 == 0o600


def test_real_restart_presists_and_reloads(tmp_path, fake_launchctl):
    """真实 surgeconf.sh enable → restart：配置保留、job 重载"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)

    r = run_script(["enable"], tmp_path,
                   extra_env={**env, "SURGE_HOST": "192.0.2.10", "SURGE_PORT": "61900"})
    assert r.returncode == 0, r.stdout + r.stderr
    data = plistlib.loads(plist.read_bytes())
    assert data["EnvironmentVariables"]["SURGE_HOST"] == "192.0.2.10"

    log.write_text("")
    r2 = run_script(["restart"], tmp_path, extra_env=env)   # 全 unset
    assert r2.returncode == 0, r2.stdout + r2.stderr
    data2 = plistlib.loads(plist.read_bytes())
    assert data2["EnvironmentVariables"]["SURGE_HOST"] == "192.0.2.10"    # 不丢
    assert data2["EnvironmentVariables"]["SURGE_PORT"] == "61900"
    calls = log.read_text().strip().splitlines()
    assert f"unload {plist}" in calls            # restart 也走卸载→重载
    assert f"load {plist}" in calls


# ===== Round 3：C1/UTF-8、迁移、status 精确判定 =====


def test_control_char_check_utf8_safe(tmp_path):
    """Unicode code point 语义：中文路径/非 ASCII 值不误报；真实 C1 拒绝"""
    r = run_bash(r"""
        source "$LIB"
        sc_assert_no_control "$HOME/项目数据目录" && echo CN_DIR_OK || echo CN_DIR_BAD
        sc_assert_no_control "密钥@主机.example.com:6160" && echo CN_VAL_OK || echo CN_VAL_BAD
        sc_assert_no_control "02-00-00-00-00-01" && echo ASCII_OK || echo ASCII_BAD
        NEL="$(printf '\xc2\x85')"
        sc_assert_no_control "k@h${NEL}:6160" && echo NEL_BAD || echo NEL_OK
        C1="$(printf '\xc2\x9f')"
        sc_assert_no_control "k${C1}@h:6160" && echo C1_BAD || echo C1_OK
        LF="$(printf 'a\nb')"
        sc_assert_no_control "$LF" && echo LF_BAD || echo LF_OK
    """, tmp_path)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "CN_DIR_OK" in out and "CN_DIR_BAD" not in out
    assert "CN_VAL_OK" in out and "CN_VAL_BAD" not in out
    assert "ASCII_OK" in out and "ASCII_BAD" not in out
    assert "NEL_OK" in out and "NEL_BAD" not in out      # U+0085 (NEL) 属 C1
    assert "C1_OK" in out and "C1_BAD" not in out        # U+009F (C1 末位)
    assert "LF_OK" in out and "LF_BAD" not in out


def test_chinese_custom_data_dir_install_roundtrip(tmp_path, fake_launchctl):
    """中文 custom data dir 全链：install → unset restart 保持同一目录"""
    log, env, state = fake_launchctl
    home, *_ = _prepare(tmp_path)
    env["HOME"] = str(home)
    custom = home / "数据目录甲"

    r = run_bash(_install_script(), tmp_path,
                 extra_env={**env, "SURGECONF_DATA_DIR": str(custom)})
    assert r.returncode == 0, r.stderr
    assert custom.is_dir()

    r2 = run_bash(_install_script(), tmp_path, extra_env=env)   # 全 unset
    assert r2.returncode == 0, r2.stderr
    data = plistlib.loads((home / "Library/LaunchAgents/com.surgeconf.plist").read_bytes())
    assert data["EnvironmentVariables"]["SURGECONF_DATA_DIR"] == str(custom)


def test_migrate_legacy_custom_data_control(tmp_path, fake_launchctl):
    """旧 plist + custom dir 内旧架构 launchd.env + 全变量 unset →
    迁移到固定控制文件并保持原 DB 路径与旧配置"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)
    custom = home / "legacy-custom-data"
    custom.mkdir(parents=True, exist_ok=True)

    # 旧架构：控制文件在 custom data dir 内；plist 指向 custom + 旧监听 0.0.0.0
    (custom / "launchd.env").write_text(
        f"SURGE_HOST=192.0.2.10\nSURGE_PORT=61900\n"
        f"SURGE_CONTROLLER_ACCESS=legacy-key@192.0.2.10:6160\n")
    plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
  <key>Label</key><string>com.surgeconf</string>
  <key>EnvironmentVariables</key><dict>
    <key>SURGE_HOST</key><string>0.0.0.0</string>
    <key>SURGECONF_DATA_DIR</key><string>{custom}</string>
  </dict></dict></plist>
""")

    r = run_script(["restart"], tmp_path, extra_env=env)   # 全 unset 真实入口
    assert r.returncode == 0, r.stdout + r.stderr

    # 固定控制文件已生成且包含迁移值
    content = control_env.read_text()
    assert f"SURGECONF_DATA_DIR={custom}" in content       # 原 DB 路径保持
    assert "SURGE_HOST=192.0.2.10" in content              # 来自旧架构 env
    assert "SURGE_PORT=61900" in content
    assert "SURGE_CONTROLLER_ACCESS=legacy-key@192.0.2.10:6160" in content
    # plist 重写：监听来自迁移值（不再是 0.0.0.0），data dir 保持 custom
    data = plistlib.loads(plist.read_bytes())
    assert data["EnvironmentVariables"]["SURGE_HOST"] == "192.0.2.10"
    assert data["EnvironmentVariables"]["SURGECONF_DATA_DIR"] == str(custom)


def test_status_uses_exact_label_query(tmp_path, fake_launchctl):
    """status 精确 label 判定：运行中 / 已注册未运行 / 未注册"""
    log, env, state = fake_launchctl
    home, _, _, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)

    r0 = run_script(["status"], tmp_path, extra_env=env)
    assert r0.returncode == 0, r0.stderr
    assert "未注册" in r0.stdout

    plist.write_text("<plist/>")
    r1 = run_script(["status"], tmp_path, extra_env=env)
    assert "已注册但未运行" in r1.stdout
    calls = [c for c in log.read_text().strip().splitlines()]
    assert "list com.surgeconf" in calls                  # 精确 label 查询
    assert not any("|" in c or "grep" in c for c in calls)  # 无子串 grep 管道

    state.write_text("loaded")                            # 已加载但无 PID
    r2 = run_script(["status"], tmp_path, extra_env=env)
    assert "已加载但未运行" in r2.stdout
    assert "运行中" not in r2.stdout                       # 不得误报运行中

    state.write_text("running")                            # 数字 PID
    r3 = run_script(["status"], tmp_path, extra_env=env)
    assert "运行中" in r3.stdout


def test_run_branch_refuses_corrupt_control_file(tmp_path, fake_launchctl):
    """run 分支控制文件损坏（未知键）→ 经 sc_read_launchd_env 拒绝启动"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)
    control_env.write_text("EVIL_KEY=x\n")

    r = run_script(["run"], tmp_path, extra_env=env)
    assert r.returncode != 0
    assert "未知键" in r.stderr or "未知键" in r.stdout



# ===== Round 4：reader 完整性、显式值校验、空 data dir、.. 拒绝 =====


def test_reader_rejects_line_without_equals(tmp_path):
    """每行必须含 =：单独键名行拒绝，不被解释成键值"""
    r = run_bash(r"""
        source "$LIB"
        printf '%s\n' 'SURGE_HOST' > "$HOME/env"
        if sc_read_launchd_env "$HOME/env" 2>"$HOME/err"; then
          echo ACCEPTED
        else
          echo REJECTED
        fi
        cat "$HOME/err"
    """, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "REJECTED" in r.stdout
    assert "缺少 = 分隔" in r.stdout


def test_reader_rejects_dangling_symlink_control_file(tmp_path):
    """dangling symlink 控制文件：symlink 检查先于存在性，不当作缺失"""
    r = run_bash(r"""
        source "$LIB"
        ln -s "$HOME/no-such-target" "$HOME/env"
        if sc_read_launchd_env "$HOME/env" 2>"$HOME/err"; then
          echo ACCEPTED
        else
          echo REJECTED
        fi
    """, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "REJECTED" in r.stdout
    assert "symlink" in (tmp_path / "home" / "err").read_text()


def test_env_write_rejects_trailing_newline_value(tmp_path):
    """显式凭据含尾随 LF：不经命令替换吞掉，写盘前必须失败且不生成文件"""
    r = run_bash(r"""
        source "$LIB"
        printf -v val 'example-access-key@127.0.0.1:6160\n'
        export SURGE_CONTROLLER_ACCESS="$val"
        if sc_write_launchd_env "$HOME/env" 2>/dev/null; then
          echo WROTE
        else
          echo REJECTED
        fi
        [ -e "$HOME/env" ] && echo FILE_EXISTS || echo NO_FILE
    """, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "REJECTED" in r.stdout
    assert "NO_FILE" in r.stdout


def test_resolve_unsets_empty_data_dir(tmp_path):
    """显式空 SURGECONF_DATA_DIR：resolve 后按 unset 处理（不传空值给 Python）"""
    r = run_bash(r"""
        source "$LIB"
        export SURGECONF_DATA_DIR=""
        sc_resolve_runtime_config
        if [ "${SURGECONF_DATA_DIR+x}" = x ]; then
          echo STILL_SET
        else
          echo UNSET
        fi
    """, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "UNSET" in r.stdout
    assert "STILL_SET" not in r.stdout


def test_resolve_rejects_control_char_in_explicit_data_dir(tmp_path):
    """显式 data dir 含控制字符：resolve 阶段（写/导出前）失败"""
    r = run_bash(r"""
        source "$LIB"
        printf -v bad '%s\n/data' "$HOME"
        export SURGECONF_DATA_DIR="$bad"
        if sc_resolve_runtime_config 2>/dev/null; then
          echo ACCEPTED
        else
          echo REJECTED
        fi
    """, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "REJECTED" in r.stdout


def test_ensure_directory_rejects_dotdot_escape(tmp_path):
    """含 .. 的路径词法拒绝：即使词法上位于锚点内也不创建"""
    r = run_bash(r"""
        source "$LIB"
        if sc_ensure_directory "$HOME/../escape" 2>/dev/null; then
          echo ACCEPTED
        else
          echo REJECTED
        fi
    """, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "REJECTED" in r.stdout
    assert not (tmp_path / "escape").exists()
