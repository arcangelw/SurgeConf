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

# 一次性信号注入：向父进程（被测 bash）发信号并落标记；仅发一次
_signal_once() {
  [ "${FAKE_LAUNCHCTL_SIGNAL:-}" = "$1" ] || return 1
  [ -f "${FAKE_LAUNCHCTL_SIGNAL_MARK:-/nonexistent}" ] && return 1
  : > "${FAKE_LAUNCHCTL_SIGNAL_MARK:-/dev/null}" 2>/dev/null
  kill -"${FAKE_LAUNCHCTL_SIGNAL_KIND:-TERM}" "$PPID" 2>/dev/null
  return 0
}

case "$1" in
  list)
    if [ -n "$2" ]; then
      # 精确 label 查询：输出 PID\tStatus\tLabel，exit status 表达加载状态；
      # state 文件内容为 "running" 时第一列为数字 PID，其余（含空）为 "-"
      # FAKE_LAUNCHCTL_LIST_FAIL_AT=N：第 N 次精确查询 exit 1（计数文件持久）
      if [ -n "${FAKE_LAUNCHCTL_LIST_FAIL_AT:-}" ]; then
        n=$(($(cat "${FAKE_LAUNCHCTL_LIST_COUNT:-/dev/null}" 2>/dev/null || echo 0) + 1))
        echo "$n" > "${FAKE_LAUNCHCTL_LIST_COUNT:-/dev/null}" 2>/dev/null
        [ "$n" = "${FAKE_LAUNCHCTL_LIST_FAIL_AT}" ] && exit 1
      fi
      if [ -f "${FAKE_LAUNCHCTL_STATE:-/nonexistent}" ]; then
        pid_col="-"
        if [ "$(cat "${FAKE_LAUNCHCTL_STATE}")" = "running" ]; then
          pid_col="12345"
        fi
        # FAKE_LAUNCHCTL_PID_FROM / PID_UNTIL：按精确查询计数控制第一列
        # （延迟启动 / 进程退出模拟；计数包含 prepare 探测与回滚确认）
        if [ -n "${FAKE_LAUNCHCTL_PID_FROM:-}" ] || [ -n "${FAKE_LAUNCHCTL_PID_UNTIL:-}" ]; then
          pn=$(($(cat "${FAKE_LAUNCHCTL_PID_COUNT:-/dev/null}" 2>/dev/null || echo 0) + 1))
          echo "$pn" > "${FAKE_LAUNCHCTL_PID_COUNT:-/dev/null}" 2>/dev/null
          pid_col="-"
          if [ "$pn" -ge "${FAKE_LAUNCHCTL_PID_FROM:-1}" ]; then
            pid_col="12345"
          fi
          if [ -n "${FAKE_LAUNCHCTL_PID_UNTIL:-}" ] && \
             [ "$pn" -gt "${FAKE_LAUNCHCTL_PID_UNTIL}" ]; then
            pid_col="-"
          fi
        fi
        printf -- '%s\t0\t%s\n' "$pid_col" "$2"
        # after-confirm 注入点：候选 load 已发生且 readiness 已通过
        # （curl-ok 标记存在）后的第一次精确查询——即 switch 提交
        # 临界区内的最终确认；轮询阶段的查询不触发（ok 标记未写）
        if [ "${FAKE_LAUNCHCTL_SIGNAL:-}" = "after-confirm" ] && \
           [ -f "${FAKE_LAUNCHCTL_LOAD_MARK:-/nonexistent}" ] && \
           [ -f "${FAKE_CURL_OK_MARK:-/nonexistent}" ] && \
           [ ! -f "${FAKE_LAUNCHCTL_SIGNAL_MARK:-/nonexistent}" ]; then
          : > "${FAKE_LAUNCHCTL_SIGNAL_MARK:-/dev/null}" 2>/dev/null
          kill -"${FAKE_LAUNCHCTL_SIGNAL_KIND:-TERM}" "$PPID" 2>/dev/null
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
    # before-load 注入点：文件已切换、load 未执行——发信号后以 0 退出
    if _signal_once before-load; then
      exit 0
    fi
    # FAKE_LAUNCHCTL_FAIL_ONCE=load：第一次 load exit 1（标记文件），之后恢复
    if [ "${FAKE_LAUNCHCTL_FAIL_ONCE:-}" = "load" ] && \
       [ ! -f "${FAKE_LAUNCHCTL_ONCE_MARK:-/nonexistent}" ]; then
      : > "${FAKE_LAUNCHCTL_ONCE_MARK:-/nonexistent}"
      exit 1
    fi
    # 加固：plist 必须存在、非 symlink、普通文件且可读，否则拒绝
    # （不得在旧 plist 缺失/不可读时假成功）；成功时 state 记为进程
    # 已启动（"running"→数字 PID）；FAKE_LAUNCHCTL_NORUN=1 时记
    # "norun"（loaded 但 PID 列为 "-"：崩溃/退避/启动失败模拟）
    plist_arg="$2"
    if [ -z "$plist_arg" ] || [ ! -e "$plist_arg" ] || [ -L "$plist_arg" ] || \
       [ ! -f "$plist_arg" ] || [ ! -r "$plist_arg" ]; then
      exit 1
    fi
    rc="${FAKE_LAUNCHCTL_LOAD_RC:-0}"
    if [ "$rc" = "0" ]; then
      if [ "${FAKE_LAUNCHCTL_NORUN:-}" = "1" ]; then
        printf '%s' "norun" > "${FAKE_LAUNCHCTL_STATE:-/dev/null}"
      else
        printf '%s' "running" > "${FAKE_LAUNCHCTL_STATE:-/dev/null}"
      fi
      # load 标记：供 after-confirm 注入点区分「确认」与「探测」
      : > "${FAKE_LAUNCHCTL_LOAD_MARK:-/dev/null}" 2>/dev/null
      # after-load 注入点：候选已加载——发信号后以 0 退出
      if _signal_once after-load; then
        exit 0
      fi
    fi
    exit "$rc"
    ;;
  unload)
    # FAKE_LAUNCHCTL_UNLOAD_FAIL_AT=N：第 N 次 unload exit 1（一次性计数）
    if [ -n "${FAKE_LAUNCHCTL_UNLOAD_FAIL_AT:-}" ]; then
      n=$(($(cat "${FAKE_LAUNCHCTL_UNLOAD_COUNT:-/dev/null}" 2>/dev/null || echo 0) + 1))
      echo "$n" > "${FAKE_LAUNCHCTL_UNLOAD_COUNT:-/dev/null}" 2>/dev/null
      [ "$n" = "${FAKE_LAUNCHCTL_UNLOAD_FAIL_AT}" ] && exit 1
    fi
    if [ "${FAKE_LAUNCHCTL_FAIL_ONCE:-}" = "unload" ] && \
       [ ! -f "${FAKE_LAUNCHCTL_ONCE_MARK:-/nonexistent}" ]; then
      : > "${FAKE_LAUNCHCTL_ONCE_MARK:-/nonexistent}"
      exit 1
    fi
    rc="${FAKE_LAUNCHCTL_UNLOAD_RC:-0}"
    if [ "$rc" = "0" ]; then
      rm -f "${FAKE_LAUNCHCTL_STATE:-/nonexistent}"
      # after-unload 注入点：旧 job 已卸载、文件未切换——发信号后以 0 退出
      if _signal_once after-unload; then
        exit 0
      fi
    fi
    exit "$rc"
    ;;
esac
exit 0
"""

FAKE_MV = r"""#!/bin/sh
# 透传真 mv；FAKE_MV_SIGNAL 精确选择四个切换窗口之一（基于源/目标
# basename 与 candidate/backup 模式判定）：
#   control-backup  旧 launchd.env → .launchd.env.backup.*
#   control-final   .launchd.env.candidate.* → launchd.env
#   plist-backup    旧 com.surgeconf.plist → .com.surgeconf.plist.backup.*
#   plist-final     .com.surgeconf.plist.candidate.* → com.surgeconf.plist
# 命中时在「mv 已完成、返回前」向父进程（入口 PID）一次性发信号，
# 并把命中标签写入 marker 文件供测试断言注入点确实命中。
/bin/mv "$@"
rc=$?
src=""; dst=""
for arg in "$@"; do
  case "$arg" in -*) continue ;; esac
  if [ -z "$src" ]; then src="$arg"
  elif [ -z "$dst" ]; then dst="$arg"
  else src="$dst"; dst="$arg"; fi
done
label=""
if [ -n "$src" ] && [ -n "$dst" ]; then
  case "${FAKE_MV_SIGNAL:-}" in
    control-backup)
      case "$src" in */launchd.env)
        case "$dst" in *.launchd.env.backup.*) label=control-backup ;; esac ;;
      esac ;;
    control-final)
      case "$src" in *.launchd.env.candidate.*)
        case "$dst" in */launchd.env) label=control-final ;; esac ;;
      esac ;;
    plist-backup)
      case "$src" in */com.surgeconf.plist)
        case "$dst" in *.com.surgeconf.plist.backup.*) label=plist-backup ;; esac ;;
      esac ;;
    plist-final)
      case "$src" in *.com.surgeconf.plist.candidate.*)
        case "$dst" in */com.surgeconf.plist) label=plist-final ;; esac ;;
      esac ;;
  esac
fi
if [ -n "$label" ] && [ ! -f "${FAKE_MV_SIGNAL_MARK:-/nonexistent}" ]; then
  printf '%s' "$label" > "${FAKE_MV_SIGNAL_MARK:-/dev/null}" 2>/dev/null
  kill -"${FAKE_MV_SIGNAL_KIND:-TERM}" "$PPID" 2>/dev/null
fi
exit "$rc"
"""


FAKE_CURL = r"""#!/bin/sh
# fake curl：绝不访问网络（纯 shell，无任何网络系统调用）。
# 每次调用把完整 argv（空格 join，含 URL）追加到 FAKE_CURL_LOG。
# FAKE_CURL_FAIL_TIMES=N：前 N 次调用 exit 1（延迟成功模拟）；
# FAKE_CURL_RC：持续失败退出码（默认 0 = 成功）。
# 成功时写 FAKE_CURL_OK_MARK（供 after-confirm 注入点区分
# 「readiness 已通过后的最终确认」与「轮询期查询」）。
printf '%s\n' "$*" >> "${FAKE_CURL_LOG:-/dev/null}"
if [ -n "${FAKE_CURL_FAIL_TIMES:-}" ]; then
  n=$(($(cat "${FAKE_CURL_COUNT:-/dev/null}" 2>/dev/null || echo 0) + 1))
  echo "$n" > "${FAKE_CURL_COUNT:-/dev/null}" 2>/dev/null
  [ "$n" -le "${FAKE_CURL_FAIL_TIMES}" ] && exit 1
fi
rc="${FAKE_CURL_RC:-0}"
if [ "$rc" = "0" ]; then
  : > "${FAKE_CURL_OK_MARK:-/dev/null}" 2>/dev/null
fi
exit "$rc"
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
    """PATH 前置 fake launchctl / fake mv / fake curl；返回 (调用日志,
    env 片段, state 文件)

    env 片段额外携带各类故障注入辅助路径：FAKE_LAUNCHCTL_ONCE_MARK
    （fail-once 标记）、FAKE_LAUNCHCTL_LIST_COUNT（精确查询计数）、
    FAKE_LAUNCHCTL_UNLOAD_COUNT（unload 计数）、FAKE_LAUNCHCTL_SIGNAL_MARK
    （一次性信号注入标记）、FAKE_LAUNCHCTL_LOAD_MARK（load 已发生标记）、
    FAKE_MV_SIGNAL_MARK（mv 窗口信号标记）、FAKE_LAUNCHCTL_PID_COUNT
    （PID_FROM/PID_UNTIL 精确查询计数）、FAKE_CURL_LOG（curl argv 日志）、
    FAKE_CURL_COUNT（curl 失败计数）、FAKE_CURL_OK_MARK（readiness
    已通过标记）。curl 默认成功（launchd 事务仅在 readiness 探测
    通过后提交）。"""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "launchctl").write_text(FAKE_LAUNCHCTL)
    (bindir / "launchctl").chmod(0o755)
    (bindir / "mv").write_text(FAKE_MV)
    (bindir / "mv").chmod(0o755)
    (bindir / "curl").write_text(FAKE_CURL)
    (bindir / "curl").chmod(0o755)
    log = tmp_path / "launchctl.log"
    log.touch()   # 预创建：零调用的场景（如 prepare 早期拒绝）也能安全读取
    curl_log = tmp_path / "curl.log"
    curl_log.touch()
    state = tmp_path / "loaded.marker"
    once_mark = tmp_path / "fail-once.mark"
    list_count = tmp_path / "list-count"
    unload_count = tmp_path / "unload-count"
    signal_mark = tmp_path / "signal.mark"
    load_mark = tmp_path / "load-happened.mark"
    mv_signal_mark = tmp_path / "mv-signal.mark"
    pid_count = tmp_path / "pid-count"
    curl_count = tmp_path / "curl-count"
    curl_ok_mark = tmp_path / "curl-ok.mark"
    env = {"PATH": f"{bindir}:/usr/bin:/bin",
           "FAKE_LAUNCHCTL_LOG": str(log),
           "FAKE_LAUNCHCTL_STATE": str(state),
           "FAKE_LAUNCHCTL_ONCE_MARK": str(once_mark),
           "FAKE_LAUNCHCTL_LIST_COUNT": str(list_count),
           "FAKE_LAUNCHCTL_UNLOAD_COUNT": str(unload_count),
           "FAKE_LAUNCHCTL_SIGNAL_MARK": str(signal_mark),
           "FAKE_LAUNCHCTL_LOAD_MARK": str(load_mark),
           "FAKE_MV_SIGNAL_MARK": str(mv_signal_mark),
           "FAKE_LAUNCHCTL_PID_COUNT": str(pid_count),
           "FAKE_CURL_LOG": str(curl_log),
           "FAKE_CURL_COUNT": str(curl_count),
           "FAKE_CURL_OK_MARK": str(curl_ok_mark)}
    return log, env, state


def _paths(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    control_dir = home / "Library" / "Application Support" / "SurgeConf"
    control_env = control_dir / "launchd.env"
    plist = home / "Library" / "LaunchAgents" / "com.surgeconf.plist"
    # launchd 的 StandardOut/ErrorPath 指向 console 回退日志；
    # 结构化事件日志 surgeconf.log 由应用内私有轮转 handler 独占
    log_file = home / "Library" / "Logs" / "SurgeConf" / "surgeconf-console.log"
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
            "$HOME/Library/Logs/SurgeConf/surgeconf-console.log" 127.0.0.1 61830 \\
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
    assert data["StandardOutPath"] == str(home / "Library/Logs/SurgeConf/surgeconf-console.log")
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
        sc_generate_plist "{legacy}" "{weird}" "$HOME/Library/Logs/SurgeConf/surgeconf-console.log" \\
            127.0.0.1 61830 "$HOME/Library/Application Support/SurgeConf"
    """, tmp_path)
    assert r.returncode == 0, r.stderr
    data = plistlib.loads(legacy.read_bytes())
    assert data["EnvironmentVariables"]["SURGE_HOST"] == "127.0.0.1"
    assert "SurgeConf/surgeconf-console.log" in data["StandardOutPath"]
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
            "$HOME/Library/Logs/SurgeConf/surgeconf-console.log"
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
    assert "SurgeConf/surgeconf-console.log" in data["StandardOutPath"]
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


# ===== console 回退日志与结构化事件日志的分离（Task 5）=====


def test_console_and_structured_log_paths_are_distinct(tmp_path):
    """stdout/stderr 只指向 surgeconf-console.log；surgeconf.log 由应用内
    私有轮转 handler 独占，任何 shell 重定向/预建都不得指向它。"""
    start = (PROJECT_ROOT / "start.sh").read_text()
    run_wrapper = (PROJECT_ROOT / "run_surgeconf.sh").read_text()
    manager = (PROJECT_ROOT / "surgeconf.sh").read_text()
    for text in (start, run_wrapper, manager):
        assert "surgeconf-console.log" in text
    # daemon 重定向与 launchd/包装脚本预建目标一律是 console 文件
    assert '>> "$LOG_DIR/surgeconf.log"' not in start
    assert 'sc_ensure_log_file "$LOG_DIR/surgeconf.log"' not in start
    assert 'sc_ensure_log_file "$LOG_DIR/surgeconf.log"' not in run_wrapper
    # 管理脚本：launchd 安装传 console；log 子命令跟踪结构化事件日志
    assert 'sc_install_launchd "$PLIST" "$PROJECT_DIR" "$CONSOLE_LOG_FILE"' in manager
    assert 'tail -F "${EVENT_LOG_FILE}"' in manager
    assert 'EVENT_LOG_FILE="$HOME/Library/Logs/SurgeConf/surgeconf.log"' in manager

    # plist 的 StandardOut/ErrPath 指向 console 且不是结构化事件日志
    home, *_ = _paths(tmp_path)
    r = run_bash(r"""
        source "$LIB"
        sc_generate_plist "$HOME/p/com.surgeconf.plist" "/proj" \
            "$HOME/Library/Logs/SurgeConf/surgeconf-console.log" \
            127.0.0.1 61830 "$HOME/Library/Application Support/SurgeConf"
    """, tmp_path)
    assert r.returncode == 0, r.stderr
    data = plistlib.loads((home / "p" / "com.surgeconf.plist").read_bytes())
    assert data["StandardOutPath"].endswith("surgeconf-console.log")
    assert data["StandardErrorPath"] == data["StandardOutPath"]
    assert not data["StandardOutPath"].endswith("/surgeconf.log")

# ===== Slice 4：事务性 restart（prepare → switch，失败不卸载/可回滚） =====


def _seed_running_service(tmp_path):
    """手写旧 control/plist 字节（返回原始字节对，供不变性断言）"""
    home, control_dir, control_env, plist, _ = _prepare(tmp_path)
    control_env.write_text("SURGE_HOST=192.0.2.10\nSURGE_PORT=61900\n")
    plist.write_bytes(b"OLD-PLIST-BYTES-9f2c")
    return (home, control_dir, control_env, plist,
            control_env.read_bytes(), plist.read_bytes())


def test_restart_invalid_port_never_unloads_or_touches_files(tmp_path, fake_launchctl):
    """运行中服务 + SURGE_PORT=abc：候选校验在 unload 前失败——
    绝不调用 unload，旧 control/plist 字节与 loaded 状态全部不变"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, old_control, old_plist = \
        _seed_running_service(tmp_path)
    env["HOME"] = str(home)
    state.write_text("running")

    r = run_script(["restart"], tmp_path, extra_env={**env, "SURGE_PORT": "abc"})
    assert r.returncode != 0
    calls = log.read_text().strip().splitlines()
    assert calls == ["list com.surgeconf"]          # 仅状态探测，绝不 unload
    assert control_env.read_bytes() == old_control
    assert plist.read_bytes() == old_plist
    assert state.exists()                            # loaded 状态不变


def test_restart_control_char_value_never_unloads(tmp_path, fake_launchctl):
    """显式 host 含控制字符（注入第二个键）：候选写盘前失败，
    不 unload、旧 control/plist 字节与 loaded 状态不变"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, old_control, old_plist = \
        _seed_running_service(tmp_path)
    env["HOME"] = str(home)
    state.write_text("running")

    r = run_script(["restart"], tmp_path,
                   extra_env={**env, "SURGE_HOST": "127.0.0.1\nSURGE_HOST=0.0.0.0"})
    assert r.returncode != 0
    calls = log.read_text().strip().splitlines()
    assert not any(c.startswith("unload") for c in calls)
    assert control_env.read_bytes() == old_control
    assert plist.read_bytes() == old_plist
    assert state.exists()


def test_restart_uncreatable_data_dir_never_unloads(tmp_path, fake_launchctl):
    """data dir 路径被普通文件阻挡：目录创建失败发生在 unload 之前，
    正式文件字节不变且无候选残留"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, old_control, old_plist = \
        _seed_running_service(tmp_path)
    env["HOME"] = str(home)
    state.write_text("running")
    blocker = home / "blocker"
    blocker.write_text("not a dir")

    r = run_script(["restart"], tmp_path,
                   extra_env={**env, "SURGECONF_DATA_DIR": f"{blocker}/sub"})
    assert r.returncode != 0
    calls = log.read_text().strip().splitlines()
    assert not any(c.startswith("unload") for c in calls)
    assert control_env.read_bytes() == old_control
    assert plist.read_bytes() == old_plist
    assert state.exists()
    assert [p.name for p in control_dir.iterdir()] == ["launchd.env"]


def test_restart_symlink_plist_rejected_before_unload(tmp_path, fake_launchctl):
    """旧 plist 是 symlink：prepare 阶段拒绝——不 unload、不跟随、不替换"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, old_control, _ = \
        _seed_running_service(tmp_path)
    env["HOME"] = str(home)
    state.write_text("running")
    victim = home / "victim.plist"
    victim.write_bytes(b"VICTIM")
    plist.unlink()
    plist.symlink_to(victim)

    r = run_script(["restart"], tmp_path, extra_env=env)
    assert r.returncode != 0
    calls = log.read_text().strip().splitlines()
    assert not any(c.startswith("unload") for c in calls)
    assert plist.is_symlink()
    assert victim.read_bytes() == b"VICTIM"
    assert state.exists()


def test_restart_load_failure_rolls_back_and_reloads_old_job(tmp_path, fake_launchctl):
    """候选 load 失败（fail-once）：回滚恢复旧 control/plist 字节、
    旧 job 重新加载并确认、无候选/备份残留、整体明确非零"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)

    r = run_script(["restart"], tmp_path, extra_env={**env, "SURGE_PORT": "61900"})
    assert r.returncode == 0, r.stdout + r.stderr
    old_control, old_plist = control_env.read_bytes(), plist.read_bytes()
    log.write_text("")

    r2 = run_script(["restart"], tmp_path,
                    extra_env={**env, "FAKE_LAUNCHCTL_FAIL_ONCE": "load"})
    assert r2.returncode != 0
    assert control_env.read_bytes() == old_control
    assert plist.read_bytes() == old_plist
    assert state.exists()                        # 回滚后旧 job 重新加载
    calls = log.read_text().strip().splitlines()
    assert sum(1 for c in calls if c.startswith("load ")) == 2   # 候选失败 + 回滚成功
    assert "回滚" in (r2.stdout + r2.stderr)
    assert [p.name for p in plist.parent.iterdir()] == ["com.surgeconf.plist"]
    assert [p.name for p in control_dir.iterdir()] == ["launchd.env"]


def test_restart_confirm_failure_rolls_back(tmp_path, fake_launchctl):
    """load 成功但精确 list 确认失败（第 2 次查询注入失败）：
    停止候选 job、恢复旧文件并重新加载，整体非零"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)

    r = run_script(["restart"], tmp_path, extra_env={**env, "SURGE_PORT": "61900"})
    assert r.returncode == 0, r.stdout + r.stderr
    old_control, old_plist = control_env.read_bytes(), plist.read_bytes()
    log.write_text("")

    r2 = run_script(["restart"], tmp_path,
                    extra_env={**env, "FAKE_LAUNCHCTL_LIST_FAIL_AT": "2"})
    assert r2.returncode != 0
    assert control_env.read_bytes() == old_control
    assert plist.read_bytes() == old_plist
    assert state.exists()
    calls = log.read_text().strip().splitlines()
    assert sum(1 for c in calls if c.startswith("load ")) == 2
    assert "回滚" in (r2.stdout + r2.stderr)
    assert [p.name for p in plist.parent.iterdir()] == ["com.surgeconf.plist"]
    assert [p.name for p in control_dir.iterdir()] == ["launchd.env"]


def test_restart_unload_failure_keeps_files_and_job(tmp_path, fake_launchctl):
    """unload 失败：正式文件未被切换（字节不变）、候选/备份清理、job 保持 loaded"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, old_control, old_plist = \
        _seed_running_service(tmp_path)
    env["HOME"] = str(home)
    state.write_text("running")
    env["FAKE_LAUNCHCTL_UNLOAD_RC"] = "1"

    r = run_script(["restart"], tmp_path, extra_env=env)
    assert r.returncode != 0
    assert control_env.read_bytes() == old_control
    assert plist.read_bytes() == old_plist
    assert state.exists()                        # fake unload 失败不删 state
    assert [p.name for p in plist.parent.iterdir()] == ["com.surgeconf.plist"]
    assert [p.name for p in control_dir.iterdir()] == ["launchd.env"]


def test_restart_not_loaded_skips_unload_and_switches(tmp_path, fake_launchctl):
    """旧 job 未加载（如 stop 后）：不调用 unload，直接切换并加载成功"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)
    control_env.write_text("SURGE_PORT=61900\n")
    plist.write_bytes(b"STALE")

    r = run_script(["restart"], tmp_path, extra_env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = log.read_text().strip().splitlines()
    assert not any(c.startswith("unload") for c in calls)
    assert plist.read_bytes() != b"STALE"        # plist 已重生成
    assert state.exists()
    assert [p.name for p in plist.parent.iterdir()] == ["com.surgeconf.plist"]
    assert control_env.stat().st_mode & 0o777 == 0o600


def test_restart_rollback_reload_failure_keeps_old_files(tmp_path, fake_launchctl):
    """回滚后的重新 load 也失败（恒败）：明确非零、旧文件已恢复并保留、
    输出人工恢复指引，不伪装成功"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)

    r = run_script(["restart"], tmp_path, extra_env={**env, "SURGE_PORT": "61900"})
    assert r.returncode == 0, r.stdout + r.stderr
    old_control, old_plist = control_env.read_bytes(), plist.read_bytes()
    log.write_text("")

    r2 = run_script(["restart"], tmp_path,
                    extra_env={**env, "FAKE_LAUNCHCTL_LOAD_RC": "1"})
    assert r2.returncode != 0
    assert control_env.read_bytes() == old_control   # 旧文件已恢复且保留
    assert plist.read_bytes() == old_plist
    assert "人工" in (r2.stdout + r2.stderr)
    assert not state.exists()                        # 回滚 load 失败：job 未加载
    calls = log.read_text().strip().splitlines()
    assert sum(1 for c in calls if c.startswith("load ")) == 2   # 两次 load 均失败


def test_enable_first_time_load_failure_leaves_no_artifacts(tmp_path, fake_launchctl):
    """首次 enable 且 load 失败：不留半发布 control/plist/job"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)
    env["FAKE_LAUNCHCTL_LOAD_RC"] = "1"

    r = run_script(["enable"], tmp_path, extra_env=env)
    assert r.returncode != 0
    assert not control_env.exists()
    assert not plist.exists()
    assert not state.exists()
    assert [p.name for p in plist.parent.iterdir()] == []


def _valid_plist_dict():
    return {
        "Label": "com.surgeconf",
        "ProgramArguments": ["/proj/run_surgeconf.sh"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "Umask": 63,
        "StandardOutPath": "/x/logs/surgeconf-console.log",
        "StandardErrorPath": "/x/logs/surgeconf-console.log",
        "EnvironmentVariables": {
            "SURGE_HOST": "127.0.0.1",
            "SURGE_PORT": "61830",
            "SURGECONF_DATA_DIR": "/x/data",
        },
    }


def test_verify_plist_candidate_contract(tmp_path):
    """候选 plist 结构验证：合法产物通过；未知顶层键/错 Label/凭据键/
    缺环境键/C1 控制字符/坏 XML 一律拒绝（纵深防御，防生成器回归）"""
    home, *_ = _paths(tmp_path)
    r = run_bash(r"""
        source "$LIB"
        sc_generate_plist "$HOME/good.plist" "/proj" \
          "$HOME/Library/Logs/SurgeConf/surgeconf-console.log" \
          "127.0.0.1" "61830" "$HOME/data" \
        && sc_verify_plist_candidate "$HOME/good.plist" \
        && echo VERIFY_OK
    """, tmp_path)
    assert "VERIFY_OK" in r.stdout, r.stderr

    unknown_top = _valid_plist_dict()
    unknown_top["Mystery"] = True
    wrong_label = _valid_plist_dict()
    wrong_label["Label"] = "other.label"
    credential_env = _valid_plist_dict()
    credential_env["EnvironmentVariables"]["SURGE_CONTROLLER_ACCESS"] = "k@h:1"
    missing_env = _valid_plist_dict()
    del missing_env["EnvironmentVariables"]["SURGE_PORT"]
    control_char = _valid_plist_dict()
    control_char["EnvironmentVariables"]["SURGE_HOST"] = "127.0.0.1\x85"
    bad = {
        "bad-unknown-top": plistlib.dumps(unknown_top),
        "bad-wrong-label": plistlib.dumps(wrong_label),
        "bad-credential-env": plistlib.dumps(credential_env),
        "bad-missing-env": plistlib.dumps(missing_env),
        "bad-control-char": plistlib.dumps(control_char),
        "bad-garbage": b"not-a-plist-at-all",
    }
    for name, blob in bad.items():
        (home / f"{name}.plist").write_bytes(blob)

    names = " ".join(f"$HOME/{n}.plist" for n in bad)
    r2 = run_bash(
        'source "$LIB"\n'
        f'for f in {names}; do\n'
        '  if sc_verify_plist_candidate "$f"; then echo "BAD_ACCEPTED:$f"; fi\n'
        'done', tmp_path)
    assert "BAD_ACCEPTED" not in r2.stdout

# ===== Slice 4 Fix Round 1：回滚恢复条件/候选存活/信号保护 =====


def test_control_candidate_mv_failure_restores_moved_backup(tmp_path, fake_launchctl):
    """control 候选 mv 失败（候选意外丢失）：旧文件已挪入备份（MOVED）——
    回滚必须以 MOVED 为准恢复备份字节，而非仅看 SWITCHED"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)
    state.write_text("running")
    control_env.write_text("SURGE_PORT=61900\n")
    plist.write_bytes(b"OLD-PLIST")
    before_control = control_env.read_bytes()
    before_plist = plist.read_bytes()

    r = run_bash(r"""
        source "$LIB"
        sc_prepare_launchd "$HOME/Library/LaunchAgents/com.surgeconf.plist" "/proj" \
            "$HOME/Library/Logs/SurgeConf/surgeconf-console.log" || exit 9
        rm -f "$SC_CAND_CONTROL"
        sc_switch_launchd "$HOME/Library/LaunchAgents/com.surgeconf.plist"
        echo "SWITCH_RC=$?"
    """, tmp_path, extra_env=env)
    assert "SWITCH_RC=1" in r.stdout, r.stdout + r.stderr
    assert control_env.read_bytes() == before_control   # 备份字节恢复
    assert plist.read_bytes() == before_plist
    assert state.exists()                                # 旧 job 重新加载
    assert [p.name for p in control_dir.iterdir()] == ["launchd.env"]
    assert [p.name for p in plist.parent.iterdir()] == ["com.surgeconf.plist"]


def test_plist_candidate_mv_failure_restores_moved_backup(tmp_path, fake_launchctl):
    """plist 候选 mv 失败：control 已切换（备份消耗）+ plist 备份已挪出——
    两个文件都必须按 MOVED 精确恢复旧字节"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)
    state.write_text("running")
    control_env.write_text("SURGE_PORT=61900\n")
    plist.write_bytes(b"OLD-PLIST")
    before_control = control_env.read_bytes()
    before_plist = plist.read_bytes()

    r = run_bash(r"""
        source "$LIB"
        sc_prepare_launchd "$HOME/Library/LaunchAgents/com.surgeconf.plist" "/proj" \
            "$HOME/Library/Logs/SurgeConf/surgeconf-console.log" || exit 9
        rm -f "$SC_CAND_PLIST"
        sc_switch_launchd "$HOME/Library/LaunchAgents/com.surgeconf.plist"
        echo "SWITCH_RC=$?"
    """, tmp_path, extra_env=env)
    assert "SWITCH_RC=1" in r.stdout, r.stdout + r.stderr
    assert control_env.read_bytes() == before_control
    assert plist.read_bytes() == before_plist
    assert state.exists()
    assert [p.name for p in control_dir.iterdir()] == ["launchd.env"]
    assert [p.name for p in plist.parent.iterdir()] == ["com.surgeconf.plist"]


def test_rollback_restore_mv_failure_preserves_0600_backup(tmp_path, fake_launchctl):
    """备份恢复 mv 失败（目录只读）：保留 0600 备份并输出不含敏感值的
    人工恢复指令，非零退出"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)
    control_env.write_text("SURGE_PORT=61900\n")
    bak = control_dir / ".launchd.env.backup.test01"
    bak.write_bytes(control_env.read_bytes())
    bak.chmod(0o600)
    control_env.unlink()   # 模拟旧文件已挪走、正式位置空缺

    r = run_bash(rf"""
        source "$LIB"
        sc_switch_state_reset
        SC_SWITCH_TARGET_CONTROL="$HOME/Library/Application Support/SurgeConf/launchd.env"
        SC_SWITCH_TARGET_PLIST="$HOME/Library/LaunchAgents/com.surgeconf.plist"
        SC_OLD_CONTROL_EXISTED=1
        SC_CONTROL_MOVED=1
        SC_CONTROL_SWITCHED=1
        SC_BAK_CONTROL="{bak}"
        chmod 500 "$HOME/Library/Application Support/SurgeConf"
        sc_rollback_launchd
        echo "ROLLBACK_RC=$?"
        chmod 700 "$HOME/Library/Application Support/SurgeConf"
        [ -f "{bak}" ] && echo BAK_KEPT
        stat -f "%Lp" "{bak}"
    """, tmp_path, extra_env=env)
    out = r.stdout + r.stderr
    assert "ROLLBACK_RC=1" in r.stdout
    assert "BAK_KEPT" in r.stdout
    assert bak.read_bytes() == b"SURGE_PORT=61900\n"
    assert bak.stat().st_mode & 0o777 == 0o600
    assert 'mv "' in out                      # 人工恢复指令（仅路径，无敏感值）


def test_confirm_failure_with_rollback_unload_failure_is_incomplete(tmp_path, fake_launchctl):
    """确认失败 + 回滚 unload 失败：磁盘恢复旧文件，但不得伪称 job 已恢复——
    输出人工 unload+load 指引、明确回滚不完整、非零"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)
    r = run_script(["restart"], tmp_path, extra_env={**env, "SURGE_PORT": "61900"})
    assert r.returncode == 0, r.stdout + r.stderr
    old_control, old_plist = control_env.read_bytes(), plist.read_bytes()
    log.write_text("")

    r2 = run_script(["restart"], tmp_path, extra_env={
        **env,
        "FAKE_LAUNCHCTL_LIST_FAIL_AT": "2",      # 第 2 次精确查询（确认）失败
        "FAKE_LAUNCHCTL_UNLOAD_FAIL_AT": "2",    # 第 2 次 unload（回滚）失败
    })
    assert r2.returncode != 0
    assert control_env.read_bytes() == old_control    # 磁盘已恢复
    assert plist.read_bytes() == old_plist
    assert state.exists()                             # 候选 job 可能仍 loaded
    out = r2.stdout + r2.stderr
    assert "launchctl unload" in out                  # 人工指引
    assert "launchctl load" in out
    assert "不完整" in out


def test_enable_rollback_unload_failure_warns_candidate_alive(tmp_path, fake_launchctl):
    """未加载场景 enable 候选 load 成功但确认失败 + 回滚 unload 失败：
    磁盘恢复（删除新文件），且明确候选 job 可能仍处于加载状态
    （人工 unload 指引），非零"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)

    r = run_script(["enable"], tmp_path, extra_env={
        **env,
        "FAKE_LAUNCHCTL_LIST_FAIL_AT": "2",      # 第 2 次精确查询（确认）失败
        "FAKE_LAUNCHCTL_UNLOAD_FAIL_AT": "1",    # 第 1 次 unload（回滚）失败
    })
    assert r.returncode != 0
    assert not control_env.exists()                   # 磁盘恢复（原先不存在）
    assert not plist.exists()
    out = r.stdout + r.stderr
    assert "launchctl unload" in out
    assert "仍" in out and "加载" in out


def test_signal_after_unload_rolls_back_and_keeps_exit_code(tmp_path, fake_launchctl):
    """TERM 于 unload 后、文件切换前到达：trap 保护自动回滚（此处无需
    文件操作）、旧 job 重新加载，退出码保留 143"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)
    state.write_text("running")
    control_env.write_text("SURGE_PORT=61900\n")
    plist.write_bytes(b"OLD-PLIST")
    before_control = control_env.read_bytes()
    before_plist = plist.read_bytes()

    r = run_script(["restart"], tmp_path, extra_env={
        **env,
        "FAKE_LAUNCHCTL_SIGNAL": "after-unload",
        "FAKE_LAUNCHCTL_SIGNAL_KIND": "TERM",
    })
    assert r.returncode == 143
    assert control_env.read_bytes() == before_control
    assert plist.read_bytes() == before_plist
    assert state.exists()                             # 旧 job 重新加载
    assert [p.name for p in control_dir.iterdir()] == ["launchd.env"]
    assert [p.name for p in plist.parent.iterdir()] == ["com.surgeconf.plist"]


def test_signal_before_load_rolls_back_moved_files(tmp_path, fake_launchctl):
    """TERM 于旧文件 moved/候选就位后、load 前：trap 回滚按 MOVED 恢复
    旧字节并重新加载，退出码 143"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)
    state.write_text("running")
    control_env.write_text("SURGE_PORT=61900\n")
    plist.write_bytes(b"OLD-PLIST")
    before_control = control_env.read_bytes()
    before_plist = plist.read_bytes()

    r = run_script(["restart"], tmp_path, extra_env={
        **env,
        "FAKE_LAUNCHCTL_SIGNAL": "before-load",
    })
    assert r.returncode == 143
    assert control_env.read_bytes() == before_control
    assert plist.read_bytes() == before_plist
    assert state.exists()
    assert [p.name for p in control_dir.iterdir()] == ["launchd.env"]
    assert [p.name for p in plist.parent.iterdir()] == ["com.surgeconf.plist"]


def test_signal_after_load_rolls_back_and_reloads(tmp_path, fake_launchctl):
    """INT 于候选 load 后、确认前：trap 回滚停候选 job、恢复旧文件、
    重新加载旧 job，退出码 130"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)
    state.write_text("running")
    control_env.write_text("SURGE_PORT=61900\n")
    plist.write_bytes(b"OLD-PLIST")
    before_control = control_env.read_bytes()
    before_plist = plist.read_bytes()

    r = run_script(["restart"], tmp_path, extra_env={
        **env,
        "FAKE_LAUNCHCTL_SIGNAL": "after-load",
        "FAKE_LAUNCHCTL_SIGNAL_KIND": "INT",
    })
    assert r.returncode == 130
    assert control_env.read_bytes() == before_control
    assert plist.read_bytes() == before_plist
    assert state.exists()
    assert [p.name for p in control_dir.iterdir()] == ["launchd.env"]
    assert [p.name for p in plist.parent.iterdir()] == ["com.surgeconf.plist"]


def test_fake_launchctl_load_requires_real_plist(tmp_path, fake_launchctl):
    """fake launchctl 加固契约：load 缺失 plist 必须拒绝，
    不得假成功（保证回滚重新 load 的可信度）"""
    log, env, state = fake_launchctl
    env["HOME"] = str(tmp_path / "home")
    r = run_bash('launchctl load "$HOME/missing.plist"\necho "RC=$?"',
                 tmp_path, extra_env=env)
    assert "RC=1" in r.stdout
    assert not state.exists()

# ===== Slice 4 Fix Round 2：入口 PID trap / mv 窗口原子性 / 提交顺序 =====


@pytest.mark.parametrize("window,kind,expected_rc", [
    ("control-backup", "TERM", 143),
    ("plist-backup", "TERM", 143),
])
def test_mv_signal_backup_window_restores_moved(tmp_path, fake_launchctl,
                                                 window, kind, expected_rc):
    """备份切换窗口（旧文件→备份，control 与 plist 各一）：旧文件与
    loaded 状态预建，信号注入于「mv 已完成、MOVED 赋值前」——临界区
    挂起信号、状态先更新再回滚，按 MOVED 精确恢复旧字节。断言注入点
    命中（marker 标签）、退出非零、唯一备份不被删、旧 control/plist
    字节与 loaded 状态恢复、无临时残留。final 两窗口的首次 enable
    场景见 test_mv_signal_final_window_first_enable_rollback。"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)
    state.write_text("running")
    control_env.write_text("SURGE_PORT=61900\n")
    plist.write_bytes(b"OLD-PLIST")
    before_control = control_env.read_bytes()
    before_plist = plist.read_bytes()
    mv_signal_mark = tmp_path / "mv-signal.mark"

    r = run_script(["restart"], tmp_path, extra_env={
        **env,
        "FAKE_MV_SIGNAL": window,
        "FAKE_MV_SIGNAL_KIND": kind,
    })
    assert r.returncode == expected_rc
    assert mv_signal_mark.read_text() == window      # 注入点确实命中且仅命中
    assert control_env.read_bytes() == before_control   # 备份恢复，未被删除
    assert plist.read_bytes() == before_plist
    assert state.exists()                                # 旧 job 重新加载
    assert [p.name for p in control_dir.iterdir()] == ["launchd.env"]
    assert [p.name for p in plist.parent.iterdir()] == ["com.surgeconf.plist"]


@pytest.mark.parametrize("window,kind,expected_rc", [
    ("control-final", "INT", 130),
    ("plist-final", "INT", 130),
])
def test_mv_signal_final_window_first_enable_rollback(window, kind, expected_rc,
                                                       tmp_path, fake_launchctl):
    """正式就位窗口（候选→正式，control 与 plist 各一）：首次 enable
    场景——正式 control/plist 原先都不存在、旧 job 未加载（无 MOVED
    备份可依赖）。信号注入于「mv 已完成、SWITCHED 赋值前」的真实入口
    PID：回滚必须依赖对应 SWITCHED 删除刚就位的文件。断言非零退出、
    注入点精确命中（marker 标签，同时证明其他窗口未消耗注入）、
    control/plist 最终仍不存在、job 未加载、候选/备份无残留。
    若对应 rename 的临界区保护被移除，信号将在 SWITCHED 赋值前触发
    回滚，官方文件残留导致本测试变红。"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)
    mv_signal_mark = tmp_path / "mv-signal.mark"

    r = run_script(["enable"], tmp_path, extra_env={
        **env,
        "FAKE_MV_SIGNAL": window,
        "FAKE_MV_SIGNAL_KIND": kind,
    })
    assert r.returncode == expected_rc
    assert mv_signal_mark.read_text() == window      # 注入点确实命中且仅命中
    assert not control_env.exists()                  # 刚就位文件已按 SWITCHED 删除
    assert not plist.exists()
    assert not state.exists()                        # job 从未加载（首次 enable）
    assert list(control_dir.iterdir()) == []         # 候选/备份无残留
    assert list(plist.parent.iterdir()) == []


def test_signal_after_confirm_respects_commit(tmp_path, fake_launchctl):
    """TERM 于确认成功瞬间（committed 置位窗口）：不回滚、不卸载已发布
    新 job，退出 143，无残留（信号尊重提交标记）"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)
    state.write_text("running")
    control_env.write_text("SURGE_PORT=61900\n")
    plist.write_bytes(b"OLD-PLIST")

    r = run_script(["restart"], tmp_path, extra_env={
        **env,
        "FAKE_LAUNCHCTL_SIGNAL": "after-confirm",
        "FAKE_LAUNCHCTL_SIGNAL_KIND": "TERM",
    })
    assert r.returncode == 143
    assert b"OLD-PLIST" != plist.read_bytes()          # 新配置已发布
    data = plistlib.loads(plist.read_bytes())
    assert data["EnvironmentVariables"]["SURGE_PORT"] == "61900"
    assert "SURGE_PORT=61900" in control_env.read_text()
    assert state.exists()                               # 新 job 未被卸载
    assert [p.name for p in plist.parent.iterdir()] == ["com.surgeconf.plist"]
    assert [p.name for p in control_dir.iterdir()] == ["launchd.env"]


# ===== Slice 4 Fix Round 5：readiness 提交门 =====


def test_readiness_host_mapping_variants(tmp_path):
    """readiness 探测 host 归一：127/0.0.0.0→127.0.0.1；::→[::1]；
    其他 IPv6 字面量加括号；LAN 地址原样直连"""
    r = run_bash(r"""
        source "$LIB"
        for h in "127" "0.0.0.0" "::" "fe80::1" "192.0.2.10"; do
          printf '%s=>%s\n' "$h" "$(sc_readiness_host "$h")"
        done
    """, tmp_path)
    lines = dict(l.split("=>", 1) for l in r.stdout.strip().splitlines())
    assert lines == {
        "127": "127.0.0.1",
        "0.0.0.0": "127.0.0.1",
        "::": "[::1]",
        "fe80::1": "[fe80::1]",
        "192.0.2.10": "192.0.2.10",
    }


def test_readiness_curl_contract_on_success(tmp_path, fake_launchctl):
    """成功提交路径的 curl argv 契约：--fail --silent --show-error
    --noproxy '*' --max-time 引用值；SURGE_HOST=0.0.0.0 时 URL host
    归一为 127.0.0.1；URL 指向 /api/health/ready，无任何凭据"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)

    r = run_script(["enable"], tmp_path, extra_env={
        **env, "SURGE_HOST": "0.0.0.0", "SURGE_PORT": "61900"})
    assert r.returncode == 0, r.stdout + r.stderr
    curl_calls = (tmp_path / "curl.log").read_text().strip().splitlines()
    assert len(curl_calls) == 1
    line = curl_calls[0]
    assert line.startswith(
        "--fail --silent --show-error --noproxy * --max-time ")
    assert line.endswith("http://127.0.0.1:61900/api/health/ready")
    assert "@" not in line                          # URL 不含凭据


def test_switch_loaded_without_running_pid_rolls_back(tmp_path, fake_launchctl):
    """load 成功但进程从未运行（PID 列恒 "-"，如数据目录网络卷/WAL
    守卫让应用反复启动失败）：loaded 不等于可服务——超时后回滚旧
    字节并恢复旧 loaded；readiness curl 从未被调用"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, old_control, old_plist = \
        _seed_running_service(tmp_path)
    env["HOME"] = str(home)
    state.write_text("running")

    r = run_script(["restart"], tmp_path, extra_env={
        **env,
        "FAKE_LAUNCHCTL_NORUN": "1",
        "SC_READINESS_TIMEOUT": "2",
        "SC_READINESS_INTERVAL": "0.3",
    })
    assert r.returncode != 0
    assert "回滚" in (r.stdout + r.stderr)
    assert control_env.read_bytes() == old_control
    assert plist.read_bytes() == old_plist
    assert state.exists()                            # 旧 job 重新加载
    assert (tmp_path / "curl.log").read_text().strip() == ""   # 无 PID 时不探测
    assert [p.name for p in plist.parent.iterdir()] == ["com.surgeconf.plist"]
    assert [p.name for p in control_dir.iterdir()] == ["launchd.env"]


def test_switch_numeric_pid_readiness_fail_rolls_back(tmp_path, fake_launchctl):
    """数字 PID 但 readiness 持续失败（应用进程活着但服务不可用，
    模拟启动失败/数据库 WAL 拒绝后的僵死窗口）：回滚旧字节+旧 loaded、
    非零退出；curl 按轮询多次探测"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, old_control, old_plist = \
        _seed_running_service(tmp_path)
    env["HOME"] = str(home)
    state.write_text("running")

    r = run_script(["restart"], tmp_path, extra_env={
        **env,
        "FAKE_CURL_RC": "7",
        "SC_READINESS_TIMEOUT": "2",
        "SC_READINESS_INTERVAL": "0.3",
    })
    assert r.returncode != 0
    assert "回滚" in (r.stdout + r.stderr)
    assert control_env.read_bytes() == old_control
    assert plist.read_bytes() == old_plist
    assert state.exists()
    curl_calls = (tmp_path / "curl.log").read_text().strip().splitlines()
    assert len(curl_calls) >= 2                      # 有界轮询，非单次放弃
    assert all(c.endswith("/api/health/ready") for c in curl_calls)


def test_switch_delayed_pid_then_delayed_ready_commits(tmp_path, fake_launchctl):
    """延迟启动（前两次精确查询 PID 为 "-"，首次迁移场景）+ 首次
    readiness 失败后成功：轮询在时限内收敛——最终提交新配置、
    job loaded、无候选残留、curl 恰好两次（一败一成）"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)

    r = run_script(["enable"], tmp_path, extra_env={
        **env,
        "SURGE_PORT": "61900",
        "FAKE_LAUNCHCTL_PID_FROM": "3",      # prepare 探测(1) + 轮询首轮(2) 为 "-"
        "FAKE_CURL_FAIL_TIMES": "1",
        "SC_READINESS_TIMEOUT": "5",
        "SC_READINESS_INTERVAL": "0.2",
    })
    assert r.returncode == 0, r.stdout + r.stderr
    data = plistlib.loads(plist.read_bytes())
    assert data["EnvironmentVariables"]["SURGE_PORT"] == "61900"
    assert state.exists()
    assert [p.name for p in plist.parent.iterdir()] == ["com.surgeconf.plist"]
    assert [p.name for p in control_dir.iterdir()] == ["launchd.env"]
    curl_calls = (tmp_path / "curl.log").read_text().strip().splitlines()
    assert len(curl_calls) == 2
    assert curl_calls[-1].endswith("http://127.0.0.1:61900/api/health/ready")


def test_switch_process_exit_after_pid_rolls_back(tmp_path, fake_launchctl):
    """PID 出现后进程退出（数字→"-"）：视同候选确认失败立即回滚，
    不等满超时（总时限 8s，退出判定应在第 2 轮轮询内发生）"""
    import time
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, old_control, old_plist = \
        _seed_running_service(tmp_path)
    env["HOME"] = str(home)
    state.write_text("running")

    started = time.monotonic()
    r = run_script(["restart"], tmp_path, extra_env={
        **env,
        "FAKE_LAUNCHCTL_PID_FROM": "2",      # 轮询首轮（总第 2 次查询）数字
        "FAKE_LAUNCHCTL_PID_UNTIL": "2",     # 第 3 次起回 "-"（进程退出）
        "SC_READINESS_TIMEOUT": "8",
        "SC_READINESS_INTERVAL": "0.3",
    })
    elapsed = time.monotonic() - started
    assert r.returncode != 0
    assert "回滚" in (r.stdout + r.stderr)
    assert elapsed < 6, f"进程退出应立即失败而非等满超时（耗时 {elapsed:.1f}s）"
    assert control_env.read_bytes() == old_control
    assert plist.read_bytes() == old_plist
    assert state.exists()


@pytest.mark.parametrize("bad_port", ["0", "65536"])
def test_restart_port_out_of_range_rejected_before_unload(tmp_path,
                                                          fake_launchctl,
                                                          bad_port):
    """端口越界（0/65536）在 unload 之前拒绝：旧 job 不停机、旧
    control/plist 字节与 loaded 状态零改动——避免已知必失败配置停服"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, old_control, old_plist = \
        _seed_running_service(tmp_path)
    env["HOME"] = str(home)
    state.write_text("running")

    r = run_script(["restart"], tmp_path,
                   extra_env={**env, "SURGE_PORT": bad_port})
    assert r.returncode != 0
    calls = log.read_text().strip().splitlines()
    assert calls == ["list com.surgeconf"]          # 仅状态探测，绝不 unload
    assert control_env.read_bytes() == old_control
    assert plist.read_bytes() == old_plist
    assert state.exists()


def test_enable_readiness_failure_leaves_no_artifacts(tmp_path, fake_launchctl):
    """首次 enable 且 readiness 失败：不留半发布 control/plist/job，
    目录零残留"""
    log, env, state = fake_launchctl
    home, control_dir, control_env, plist, _ = _prepare(tmp_path)
    env["HOME"] = str(home)

    r = run_script(["enable"], tmp_path, extra_env={
        **env,
        "FAKE_CURL_RC": "7",
        "SC_READINESS_TIMEOUT": "2",
        "SC_READINESS_INTERVAL": "0.3",
    })
    assert r.returncode != 0
    assert not control_env.exists()
    assert not plist.exists()
    assert not state.exists()
    assert [p.name for p in plist.parent.iterdir()] == []
    assert [p.name for p in control_dir.iterdir()] == []
