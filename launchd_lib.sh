#!/bin/bash
# launchd_lib.sh — SurgeConf launchd 安装与配置持久化共享函数库
#
# 被 surgeconf.sh（install/start/restart/enable）与 run_surgeconf.sh（安全读取）
# source 使用；测试直接 source 并以 fake launchctl 驱动真实脚本入口。
#
# 控制文件固定位于 ~/Library/Application Support/SurgeConf/launchd.env
# （与实际 SURGECONF_DATA_DIR 独立），持久化 data dir/host/port/凭据；
# restart 先读固定控制文件，再由其中的 SURGECONF_DATA_DIR 解析运行目录。
# 固定控制文件缺失时，从现有 plist 安全迁移旧 custom-data 配置（见
# sc_migrate_legacy_control）。
#
# 键更新语义（五个持久化键一致）：
#   - 调用环境「显式提供」非空值 → 写入并持久化；
#   - 「未提供」（unset，restart 场景）→ 保留持久化现值；
#   - 「显式空值」→ 清除该键（回到安全默认 127.0.0.1）。
# 控制器凭据只存 0600 控制文件，绝不写入 plist。

SC_LAUNCHD_LABEL="com.surgeconf"

# ===== 事务性切换状态（sc_install_launchd 内部使用）=====
# 候选/备份均为目标目录内的 mktemp 随机名 0600 文件：与正式路径同目录
# （同文件系统），切换/回滚用 rename(2) 原子完成；随机名杜绝可预测路径
# 抢占，rm/mv 均只作用于链接本身（不跟随 symlink）。
SC_CAND_CONTROL=""
SC_CAND_PLIST=""
SC_BAK_CONTROL=""
SC_BAK_PLIST=""
SC_OLD_CONTROL_EXISTED=0
SC_OLD_PLIST_EXISTED=0
SC_CONTROL_MOVED=0
SC_CONTROL_SWITCHED=0
SC_PLIST_MOVED=0
SC_PLIST_SWITCHED=0
SC_WAS_LOADED=0
SC_SWITCH_TARGET_PLIST=""
SC_SWITCH_TARGET_CONTROL=""

# 复位事务状态（每次 sc_install_launchd 开始时调用）
sc_switch_state_reset() {
  SC_CAND_CONTROL=""
  SC_CAND_PLIST=""
  SC_BAK_CONTROL=""
  SC_BAK_PLIST=""
  SC_OLD_CONTROL_EXISTED=0
  SC_OLD_PLIST_EXISTED=0
  SC_CONTROL_MOVED=0
  SC_CONTROL_SWITCHED=0
  SC_PLIST_MOVED=0
  SC_PLIST_SWITCHED=0
  SC_WAS_LOADED=0
  SC_SWITCH_TARGET_PLIST=""
  SC_SWITCH_TARGET_CONTROL=""
}

# 清理候选/备份临时文件（成功路径与回滚完成路径调用；恒返回 0，
# 不掩盖主操作的失败语义）
sc_discard_candidates() {
  if [ -n "$SC_CAND_CONTROL" ]; then rm -f "$SC_CAND_CONTROL" || true; fi
  if [ -n "$SC_CAND_PLIST" ]; then rm -f "$SC_CAND_PLIST" || true; fi
  if [ -n "$SC_BAK_CONTROL" ]; then rm -f "$SC_BAK_CONTROL" || true; fi
  if [ -n "$SC_BAK_PLIST" ]; then rm -f "$SC_BAK_PLIST" || true; fi
  return 0
}

SC_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# C0/C1 判定用 venv Python（Unicode code point 语义，不误伤 UTF-8 中文路径）
SC_PYTHON="$SC_LIB_DIR/venv/bin/python"
if [ ! -x "$SC_PYTHON" ]; then
  SC_PYTHON="$(command -v python3 || echo python3)"
fi

sc_control_dir() {
  printf '%s\n' "$HOME/Library/Application Support/SurgeConf"
}

sc_control_env_path() {
  printf '%s\n' "$(sc_control_dir)/launchd.env"
}

# 目录安全创建：锚点（默认 $HOME）以下逐组件拒绝 symlink/非目录，
# mkdir -p 后收敛 0700。不在锚点下的路径从根起全链校验（系统 symlink
# 如 /tmp 将被拒绝 —— 调用方应使用物理路径）。
sc_ensure_directory() {
  local d="$1"
  local anchor="${2:-$HOME}"
  # 词法拒绝 .. 组件：含 .. 的路径可在词法上匹配锚点前缀、解析后逃逸
  case "/$d/" in
    *"/../"*)
      echo "拒绝包含 .. 的路径: $d" >&2
      return 1
      ;;
  esac
  case "$d" in
    "$anchor"|"$anchor"/*) ;;
    *) anchor="/" ;;
  esac
  # 锚点自身必须存在且为非 symlink 目录（根锚点除外）
  if [ "$anchor" != "/" ]; then
    if [ -L "$anchor" ] || [ ! -d "$anchor" ]; then
      echo "锚点目录无效: $anchor" >&2
      return 1
    fi
  fi
  # 从锚点以下逐组件校验
  local rel="${d#"$anchor"}"
  rel="${rel#/}"
  local p="$anchor"
  local comp
  while [ -n "$rel" ]; do
    comp="${rel%%/*}"
    if [ "$comp" = "$rel" ]; then
      rel=""
    else
      rel="${rel#*/}"
    fi
    [ -z "$comp" ] && continue
    p="${p%/}/$comp"
    if [ -L "$p" ]; then
      echo "拒绝 symlink 路径组件: $p" >&2
      return 1
    fi
    if [ -e "$p" ] && [ ! -d "$p" ]; then
      echo "路径组件不是目录: $p" >&2
      return 1
    fi
  done
  mkdir -p "$d" || return $?
  chmod 700 "$d"
}

# 日志文件安全预建：拒绝 symlink/FIFO/socket/非普通文件，touch 后 0600
sc_ensure_log_file() {
  local f="$1"
  if [ -L "$f" ]; then
    echo "拒绝 symlink 日志路径: $f" >&2
    return 1
  fi
  if [ -e "$f" ] && [ ! -f "$f" ]; then
    echo "日志路径不是普通文件（FIFO/socket/目录等）: $f" >&2
    return 1
  fi
  touch "$f" && chmod 600 "$f"
}

# C0/C1 控制字符与非法 UTF-8 判定：由 venv Python 以 Unicode code point
# 语义执行（0x00-0x1F、0x7F-0x9F），中文路径/非 ASCII 合法值不误报。
sc_assert_no_control() {
  if ! printf '%s' "$1" | "$SC_PYTHON" -c 'import sys
raw = sys.stdin.buffer.read()
try:
    text = raw.decode("utf-8")
except UnicodeDecodeError:
    sys.exit(1)
for ch in text:
    o = ord(ch)
    if o < 0x20 or 0x7F <= o <= 0x9F:
        sys.exit(1)
sys.exit(0)'; then
    echo "配置值含控制字符或非法 UTF-8" >&2
    return 1
  fi
}

# 以参数形式导出环境键（避免源码出现 键名="字面" 形态；不 eval）
sc_set_env() {
  export "$1=$2"
}

# 读取一个持久化键的当前显式环境值（set -u 安全；未设置返回空串）
sc_env_value() {
  case "$1" in
    SURGE_HOST)               printf '%s' "${SURGE_HOST-}" ;;
    SURGE_PORT)               printf '%s' "${SURGE_PORT-}" ;;
    SURGECONF_DATA_DIR)       printf '%s' "${SURGECONF_DATA_DIR-}" ;;
    SURGE_CONTROLLER_ACCESS)  printf '%s' "${SURGE_CONTROLLER_ACCESS-}" ;;
    SURGE_HTTP_API)           printf '%s' "${SURGE_HTTP_API-}" ;;
    *) return 1 ;;
  esac
}

# mktemp 独占创建（O_EXCL、随机名、0600）安全临时文件；禁止可预测名重定向。
sc_mktemp_private() {
  local dir="$1" prefix="$2"
  mktemp "${dir}/${prefix}.XXXXXX"
}

# 将当前环境中的持久化键写入控制文件（mktemp 独占 + 原子 mv）。
# 所有值先做控制字符/换行校验 —— 任一非法即在写盘前失败，不生成文件。
sc_write_launchd_env() {
  local file="$1"
  local dir
  dir="$(dirname "$file")"
  sc_ensure_directory "$dir" || return $?

  local out=""
  local key val
  for key in SURGE_HOST SURGE_PORT SURGECONF_DATA_DIR \
             SURGE_CONTROLLER_ACCESS SURGE_HTTP_API; do
    # 显式 case 直接取全局变量：不经命令替换（命令替换吞尾随换行，
    # 尾随 LF 会在校验前被静默规范化而漏过拒绝）
    case "$key" in
      SURGE_HOST)              val="${SURGE_HOST-}" ;;
      SURGE_PORT)              val="${SURGE_PORT-}" ;;
      SURGECONF_DATA_DIR)      val="${SURGECONF_DATA_DIR-}" ;;
      SURGE_CONTROLLER_ACCESS) val="${SURGE_CONTROLLER_ACCESS-}" ;;
      SURGE_HTTP_API)          val="${SURGE_HTTP_API-}" ;;
    esac
    if [ -n "$val" ]; then
      sc_assert_no_control "$val" || return $?   # 原始值（含尾随 LF）在校验中失败
      out="${out}${key}=${val}"$'\n'
    fi
  done

  local tmp
  tmp="$(sc_mktemp_private "$dir" ".launchd.env")" || return $?
  chmod 600 "$tmp"
  if ! printf '%s' "$out" > "$tmp"; then
    rm -f "$tmp"
    return 1
  fi
  mv -f "$tmp" "$file"
  chmod 600 "$file"
}

# 安全读取控制文件：拒绝 symlink/非普通文件、未知键、重复键、控制字符；
# 单行 KEY=value 格式 + 按键唯一性检查杜绝多行伪造。读前收敛 0600。
sc_read_launchd_env() {
  local file="$1"
  # symlink 检查先于存在性：dangling symlink 不得被当作"文件不存在"
  if [ -L "$file" ]; then
    echo "拒绝 symlink 的控制文件: $file" >&2
    return 1
  fi
  [ -e "$file" ] || return 0
  if [ ! -f "$file" ]; then
    echo "控制文件不是普通文件: $file" >&2
    return 1
  fi
  # 收敛 0600 前后比对 device/inode：chmod 作用对象在窗口内被替换则 fail closed
  local identity_before identity_after
  identity_before="$(stat -f '%d %i' "$file")" || return $?
  chmod 600 "$file" || return $?
  identity_after="$(stat -f '%d %i' "$file")" || return $?
  if [ "$identity_before" != "$identity_after" ]; then
    echo "控制文件在权限收敛期间被替换: $file" >&2
    return 1
  fi

  local line key val
  local seen_host=0 seen_port=0 seen_data=0 seen_ctrl=0 seen_api=0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    case "$line" in
      *=*) ;;
      *)
        echo "控制文件行缺少 = 分隔: ${line%%$'\t'*}" >&2
        return 1
        ;;
    esac
    key="${line%%=*}"
    val="${line#*=}"
    case "$key" in
      SURGE_HOST)
        [ "$seen_host" = 0 ] || { echo "控制文件含重复键: $key" >&2; return 1; }
        seen_host=1 ;;
      SURGE_PORT)
        [ "$seen_port" = 0 ] || { echo "控制文件含重复键: $key" >&2; return 1; }
        seen_port=1 ;;
      SURGECONF_DATA_DIR)
        [ "$seen_data" = 0 ] || { echo "控制文件含重复键: $key" >&2; return 1; }
        seen_data=1 ;;
      SURGE_CONTROLLER_ACCESS)
        [ "$seen_ctrl" = 0 ] || { echo "控制文件含重复键: $key" >&2; return 1; }
        seen_ctrl=1 ;;
      SURGE_HTTP_API)
        [ "$seen_api" = 0 ] || { echo "控制文件含重复键: $key" >&2; return 1; }
        seen_api=1 ;;
      *)
        echo "控制文件含未知键: $key" >&2
        return 1
        ;;
    esac
    sc_assert_no_control "$val" || return $?
    sc_set_env "$key" "$val"
  done < "$file"
}

# 统一配置消费：显式环境值优先于控制文件值，结果 export 到当前 shell。
# 所有 control file 读取一律经 sc_read_launchd_env（本函数是唯一入口旁的
# 合并层），禁止 grep/head/cut 等旁路解析。
sc_resolve_runtime_config() {
  local in_host in_port in_data in_ctrl in_api
  local set_host=0 set_port=0 set_data=0 set_ctrl=0 set_api=0
  if [ "${SURGE_HOST+x}" = x ]; then set_host=1; in_host="${SURGE_HOST-}"; fi
  if [ "${SURGE_PORT+x}" = x ]; then set_port=1; in_port="${SURGE_PORT-}"; fi
  if [ "${SURGECONF_DATA_DIR+x}" = x ]; then set_data=1; in_data="${SURGECONF_DATA_DIR-}"; fi
  if [ "${SURGE_CONTROLLER_ACCESS+x}" = x ]; then set_ctrl=1; in_ctrl="${SURGE_CONTROLLER_ACCESS-}"; fi
  if [ "${SURGE_HTTP_API+x}" = x ]; then set_api=1; in_api="${SURGE_HTTP_API-}"; fi

  # 显式非空值在写/导出前拒绝控制字符：原始值直传校验，不经命令替换
  if [ "$set_host" = 1 ] && [ -n "$in_host" ]; then
    sc_assert_no_control "$in_host" || return $?
  fi
  if [ "$set_port" = 1 ] && [ -n "$in_port" ]; then
    sc_assert_no_control "$in_port" || return $?
  fi
  if [ "$set_data" = 1 ] && [ -n "$in_data" ]; then
    sc_assert_no_control "$in_data" || return $?
  fi
  if [ "$set_ctrl" = 1 ] && [ -n "$in_ctrl" ]; then
    sc_assert_no_control "$in_ctrl" || return $?
  fi
  if [ "$set_api" = 1 ] && [ -n "$in_api" ]; then
    sc_assert_no_control "$in_api" || return $?
  fi

  sc_read_launchd_env "$(sc_control_env_path)" || return $?

  if [ "$set_host" = 1 ]; then sc_set_env SURGE_HOST "$in_host"; fi
  if [ "$set_port" = 1 ]; then sc_set_env SURGE_PORT "$in_port"; fi
  if [ "$set_data" = 1 ]; then sc_set_env SURGECONF_DATA_DIR "$in_data"; fi
  if [ "$set_ctrl" = 1 ]; then sc_set_env SURGE_CONTROLLER_ACCESS "$in_ctrl"; fi
  if [ "$set_api" = 1 ]; then sc_set_env SURGE_HTTP_API "$in_api"; fi

  # 显式空 SURGECONF_DATA_DIR 视为 unset：回默认目录且绝不把空值传给
  # 子进程（Python 端 Path("") 会解析为当前目录，运行数据落回源码树）
  if [ "${SURGECONF_DATA_DIR+x}" = x ] && [ -z "${SURGECONF_DATA_DIR-}" ]; then
    unset SURGECONF_DATA_DIR || true
  fi
}

# 旧架构迁移：固定控制文件缺失时，从现有 plist 安全解析
# SURGECONF_DATA_DIR（plistlib，禁止 shell source/grep XML），若其目录
# 存在旧 launchd.env 则经统一安全 reader 读取；迁移值作为持久化现值。
# 必须在「显式值记录」之后调用，调用方随后正常回放显式值。
sc_migrate_legacy_control() {
  local plist="$1"
  local control_env
  control_env="$(sc_control_env_path)"
  # symlink 先于存在性：dangling symlink 不得被当作"控制文件缺失"而触发迁移覆盖
  if [ -L "$control_env" ]; then
    echo "拒绝 symlink 的控制文件: $control_env" >&2
    return 1
  fi
  [ -e "$control_env" ] && return 0
  [ -f "$plist" ] || return 0
  if [ -L "$plist" ]; then
    echo "拒绝 symlink plist: $plist" >&2
    return 1
  fi

  local data_dir
  data_dir="$("$SC_PYTHON" -c '
import plistlib, sys
with open(sys.argv[1], "rb") as handle:
    data = plistlib.load(handle)
env = data.get("EnvironmentVariables") or {}
sys.stdout.write(env.get("SURGECONF_DATA_DIR", ""))
' "$plist" 2>/dev/null)" || data_dir=""
  [ -n "$data_dir" ] || return 0

  local legacy_env="$data_dir/launchd.env"
  if [ -e "$legacy_env" ]; then
    if [ -L "$legacy_env" ]; then
      echo "拒绝 symlink 的旧控制文件: $legacy_env" >&2
      return 1
    fi
    if [ -f "$legacy_env" ]; then
      sc_read_launchd_env "$legacy_env" || return $?   # 统一安全 reader
    fi
  fi
  # data dir 迁移值（无论是否存在旧 env，plist 的 data dir 本身要保留）
  sc_set_env SURGECONF_DATA_DIR "$data_dir"
}

sc_xml_escape() {
  local s="$1"
  s="${s//&/&amp;}"
  s="${s//</&lt;}"
  s="${s//>/&gt;}"
  s="${s//\"/&quot;}"
  s="${s//\'/&apos;}"
  printf '%s' "$s"
}

# 生成 launchd plist（唯一安装物）：Umask 63(=077)、日志捕获到用户日志目录、
# 环境只传定位运行目录所需三键（凭据不进 plist）；XML 转义 + 控制字符拒绝。
sc_generate_plist() {
  local plist="$1" project_dir="$2" log_file="$3" host="$4" port="$5" data_dir="$6"
  local arg
  for arg in "$project_dir" "$log_file" "$host" "$data_dir"; do
    sc_assert_no_control "$arg" || return 1
  done
  case "$port" in
    ''|*[!0-9]*) echo "端口必须为数字: $port" >&2; return 1 ;;
  esac

  sc_ensure_directory "$(dirname "$plist")" || return $?

  local x_project x_log x_host x_data
  x_project="$(sc_xml_escape "$project_dir")"
  x_log="$(sc_xml_escape "$log_file")"
  x_host="$(sc_xml_escape "$host")"
  x_data="$(sc_xml_escape "$data_dir")"

  # mktemp 独占创建（随机名 + 0600），禁止可预测名重定向
  local tmp
  tmp="$(sc_mktemp_private "$(dirname "$plist")" "$(basename "$plist")")" || return $?
  chmod 600 "$tmp"
  if ! cat > "$tmp" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${SC_LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${x_project}/run_surgeconf.sh</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>Umask</key>
    <integer>63</integer>
    <key>StandardOutPath</key>
    <string>${x_log}</string>
    <key>StandardErrorPath</key>
    <string>${x_log}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>SURGE_HOST</key>
        <string>${x_host}</string>
        <key>SURGE_PORT</key>
        <string>${port}</string>
        <key>SURGECONF_DATA_DIR</key>
        <string>${x_data}</string>
    </dict>
</dict>
</plist>
EOF
  then
    rm -f "$tmp"
    return 1
  fi
  mv -f "$tmp" "$plist"
  chmod 600 "$plist"
}

# 候选 plist 结构验证：plistlib 解析 + 顶层键白名单 + Label 精确匹配 +
# EnvironmentVariables 键集合（凭据键必被拒绝）+ 值控制字符 + Umask=63。
# argv 只传候选路径与 label（不含任何配置值/凭据）；失败输出不含值本身。
sc_verify_plist_candidate() {
  local plist="$1"
  "$SC_PYTHON" - "$plist" "$SC_LAUNCHD_LABEL" <<'PYEOF'
import plistlib
import sys

path, label = sys.argv[1], sys.argv[2]
try:
    with open(path, "rb") as handle:
        data = plistlib.load(handle)
except Exception:
    sys.stderr.write("候选 plist 无法解析\n")
    sys.exit(1)
if not isinstance(data, dict):
    sys.stderr.write("候选 plist 顶层不是 dict\n")
    sys.exit(1)
allowed_top = {"Label", "ProgramArguments", "RunAtLoad", "KeepAlive",
               "Umask", "StandardOutPath", "StandardErrorPath",
               "EnvironmentVariables"}
if set(data) - allowed_top:
    sys.stderr.write("候选 plist 含未知顶层键\n")
    sys.exit(1)
if data.get("Label") != label:
    sys.stderr.write("候选 plist Label 不匹配\n")
    sys.exit(1)
args = data.get("ProgramArguments")
if (not isinstance(args, list) or len(args) != 1
        or not isinstance(args[0], str)
        or not args[0].endswith("/run_surgeconf.sh")):
    sys.stderr.write("候选 plist ProgramArguments 非法\n")
    sys.exit(1)
env = data.get("EnvironmentVariables")
if not isinstance(env, dict):
    sys.stderr.write("候选 plist 缺少 EnvironmentVariables\n")
    sys.exit(1)
allowed_env = {"SURGE_HOST", "SURGE_PORT", "SURGECONF_DATA_DIR"}
if set(env) - allowed_env or allowed_env - set(env):
    sys.stderr.write("候选 plist 环境键集合非法（凭据键禁止入 plist）\n")
    sys.exit(1)
if data.get("Umask") != 63:
    sys.stderr.write("候选 plist Umask 非 077\n")
    sys.exit(1)


def has_control(value):
    return any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in value)


for key in ("SURGE_HOST", "SURGE_PORT", "SURGECONF_DATA_DIR"):
    if has_control(env[key]):
        sys.stderr.write("候选 plist 环境值含控制字符\n")
        sys.exit(1)
for key in ("StandardOutPath", "StandardErrorPath", "Label"):
    if has_control(data[key]):
        sys.stderr.write("候选 plist 路径/Label 含控制字符\n")
        sys.exit(1)
if has_control(args[0]):
    sys.stderr.write("候选 plist Program 路径含控制字符\n")
    sys.exit(1)
sys.exit(0)
PYEOF
}

# 阶段一 prepare：所有可能因配置/生成失败的步骤都在停机之前完成。
# 顺序：记录旧状态 → 显式值捕获 → 迁移/读取旧控制文件 → 回放合并 →
# 端口预检 → 写候选 control（目标目录随机名，值校验在写盘前）→
# 创建 data dir/日志目录/日志文件 → 生成候选 plist → plistlib 解析验证。
# 任一步失败：正式 control/plist 字节与 job 加载状态零改动，候选由调用方清理。
sc_prepare_launchd() {
  local plist="$1" project_dir="$2" log_file="$3"
  local control_env
  control_env="$(sc_control_env_path)"
  sc_switch_state_reset
  SC_SWITCH_TARGET_PLIST="$plist"
  SC_SWITCH_TARGET_CONTROL="$control_env"

  # 旧状态记录（symlink 检查先于存在性：dangling 不得当作缺失）
  if [ -L "$control_env" ]; then
    echo "拒绝 symlink 的控制文件: $control_env" >&2
    return 1
  fi
  if [ -e "$control_env" ]; then SC_OLD_CONTROL_EXISTED=1; fi
  if [ -L "$plist" ]; then
    echo "拒绝 symlink plist: $plist" >&2
    return 1
  fi
  if [ -e "$plist" ]; then SC_OLD_PLIST_EXISTED=1; fi
  if launchctl list "$SC_LAUNCHD_LABEL" >/dev/null 2>&1; then
    SC_WAS_LOADED=1
  fi

  # 记录调用环境显式提供的键（set -u 安全；无 ${!var} / eval）
  local in_host in_port in_data in_ctrl in_api
  local set_host=0 set_port=0 set_data=0 set_ctrl=0 set_api=0
  if [ "${SURGE_HOST+x}" = x ]; then set_host=1; in_host="${SURGE_HOST-}"; fi
  if [ "${SURGE_PORT+x}" = x ]; then set_port=1; in_port="${SURGE_PORT-}"; fi
  if [ "${SURGECONF_DATA_DIR+x}" = x ]; then set_data=1; in_data="${SURGECONF_DATA_DIR-}"; fi
  if [ "${SURGE_CONTROLLER_ACCESS+x}" = x ]; then set_ctrl=1; in_ctrl="${SURGE_CONTROLLER_ACCESS-}"; fi
  if [ "${SURGE_HTTP_API+x}" = x ]; then set_api=1; in_api="${SURGE_HTTP_API-}"; fi

  # 固定控制文件缺失 → 从旧 plist 安全迁移（显式值随后回放覆盖）
  sc_migrate_legacy_control "$plist" || return $?

  sc_read_launchd_env "$control_env" || return $?      # 旧持久化值（回退来源）
  if [ "$set_host" = 1 ]; then sc_set_env SURGE_HOST "$in_host"; fi
  if [ "$set_port" = 1 ]; then sc_set_env SURGE_PORT "$in_port"; fi
  if [ "$set_data" = 1 ]; then sc_set_env SURGECONF_DATA_DIR "$in_data"; fi
  if [ "$set_ctrl" = 1 ]; then sc_set_env SURGE_CONTROLLER_ACCESS "$in_ctrl"; fi
  if [ "$set_api" = 1 ]; then sc_set_env SURGE_HTTP_API "$in_api"; fi

  local host port data_dir
  host="$(sc_env_value SURGE_HOST)"
  port="$(sc_env_value SURGE_PORT)"
  data_dir="$(sc_env_value SURGECONF_DATA_DIR)"
  : "${host:=127.0.0.1}"
  : "${port:=61830}"
  : "${data_dir:=$(sc_control_dir)}"

  # 端口预检：非法值（如 SURGE_PORT=abc）在生成任何候选、停机之前失败
  case "$port" in
    ''|*[!0-9]*) echo "端口必须为数字: $port" >&2; return 1 ;;
  esac

  # 候选路径：目标目录内 mktemp 独占随机名（0600），与正式切换同文件系统
  local control_dir plist_dir
  control_dir="$(dirname "$control_env")"
  plist_dir="$(dirname "$plist")"
  sc_ensure_directory "$control_dir" || return $?
  sc_ensure_directory "$plist_dir" || return $?
  SC_CAND_CONTROL="$(sc_mktemp_private "$control_dir" ".launchd.env.candidate")" || return $?
  SC_CAND_PLIST="$(sc_mktemp_private "$plist_dir" ".com.surgeconf.plist.candidate")" || return $?

  # 候选 control：所有值控制字符校验在写盘前（sc_write_launchd_env 内置）
  sc_write_launchd_env "$SC_CAND_CONTROL" || return $?

  # 运行目录与日志：新配置目录预建失败同样发生在停机之前
  sc_ensure_directory "$data_dir" || return $?
  sc_ensure_directory "$(dirname "$log_file")" || return $?
  sc_ensure_log_file "$log_file" || return $?

  # 候选 plist：XML 转义 + 控制字符/端口校验（写盘前）+ 结构解析验证
  sc_generate_plist "$SC_CAND_PLIST" "$project_dir" "$log_file" "$host" "$port" "$data_dir" || return $?
  sc_verify_plist_candidate "$SC_CAND_PLIST" || return $?
}

# 回滚：停止可能已加载的候选 job → 按切换状态机精确恢复旧文件
# （未动过的不碰、挪走的 rename 回、原先不存在的删除新文件）→
# 旧 job 原已加载则重新 load 并以精确 list 确认 → 清理候选/备份残留。
# 回滚 load 也失败：明确返回非零并保留已恢复的旧文件供人工恢复。
sc_rollback_launchd() {
  local plist="$SC_SWITCH_TARGET_PLIST"
  local control_env="$SC_SWITCH_TARGET_CONTROL"
  local restore_failed=0

  # 停止可能已加载的候选 job（尽力而为，不掩盖恢复阶段错误）
  launchctl unload "$plist" >/dev/null 2>&1 || true

  if [ "$SC_CONTROL_SWITCHED" = 1 ]; then
    if [ "$SC_CONTROL_MOVED" = 1 ]; then
      mv -f "$SC_BAK_CONTROL" "$control_env" || restore_failed=1
    else
      rm -f "$control_env" || restore_failed=1
    fi
  fi
  if [ "$SC_PLIST_SWITCHED" = 1 ]; then
    if [ "$SC_PLIST_MOVED" = 1 ]; then
      mv -f "$SC_BAK_PLIST" "$plist" || restore_failed=1
    else
      rm -f "$plist" || restore_failed=1
    fi
  fi
  if [ "$restore_failed" = 1 ]; then
    # 备份保留供人工恢复；未被切换的候选清理（已切换的候选即当前正式文件）
    if [ "$SC_CONTROL_SWITCHED" != 1 ] && [ -n "$SC_CAND_CONTROL" ]; then
      rm -f "$SC_CAND_CONTROL" || true
    fi
    if [ "$SC_PLIST_SWITCHED" != 1 ] && [ -n "$SC_CAND_PLIST" ]; then
      rm -f "$SC_CAND_PLIST" || true
    fi
    echo "回滚失败：备份保留于 ${SC_BAK_CONTROL:-（无）} 与 ${SC_BAK_PLIST:-（无）}，请人工恢复" >&2
    return 1
  fi

  if [ "$SC_WAS_LOADED" = 1 ]; then
    local reload_rc=0
    launchctl load "$plist" >/dev/null 2>&1 || reload_rc=$?
    if [ "$reload_rc" = 0 ]; then
      launchctl list "$SC_LAUNCHD_LABEL" >/dev/null 2>&1 || reload_rc=1
    fi
    if [ "$reload_rc" != 0 ]; then
      echo "回滚后重新加载失败：旧配置文件已恢复，请人工执行 launchctl load \"$plist\" 并确认 ${SC_LAUNCHD_LABEL}" >&2
      sc_discard_candidates
      return 1
    fi
  fi
  sc_discard_candidates
  echo "已回滚到切换前配置" >&2
  return 0
}

# 切换失败收尾：回滚 + 汇总消息（回滚完整与否均明确非零由调用方返回）
sc_switch_failed() {
  echo "launchd 切换/加载失败，尝试回滚" >&2
  if sc_rollback_launchd; then
    echo "已回滚：旧 control/plist 与加载状态恢复（本次操作未完成）" >&2
  else
    echo "回滚不完整：请按上方提示人工恢复" >&2
  fi
}

# 阶段二 switch：unload 旧 job → 同目录原子切换文件 → load 候选 → 精确确认。
# unload 失败：正式文件未动，仅清理候选。load/确认失败：完整回滚。
sc_switch_launchd() {
  local plist="$1"
  local control_env
  control_env="$(sc_control_env_path)"

  if [ "$SC_WAS_LOADED" = 1 ]; then
    local unload_rc=0
    launchctl unload "$plist" || unload_rc=$?
    if [ "$unload_rc" != 0 ]; then
      sc_discard_candidates
      return "$unload_rc"
    fi
  fi

  # 原子切换（同目录 rename）：旧文件先挪入随机名备份，候选就位
  if [ "$SC_OLD_CONTROL_EXISTED" = 1 ]; then
    SC_BAK_CONTROL="$(sc_mktemp_private "$(dirname "$control_env")" ".launchd.env.backup")" || {
      sc_switch_failed
      return 1
    }
    if mv -f "$control_env" "$SC_BAK_CONTROL"; then
      SC_CONTROL_MOVED=1
    else
      sc_switch_failed
      return 1
    fi
  fi
  if mv -f "$SC_CAND_CONTROL" "$control_env"; then
    SC_CONTROL_SWITCHED=1
  else
    sc_switch_failed
    return 1
  fi
  if [ "$SC_OLD_PLIST_EXISTED" = 1 ]; then
    SC_BAK_PLIST="$(sc_mktemp_private "$(dirname "$plist")" ".com.surgeconf.plist.backup")" || {
      sc_switch_failed
      return 1
    }
    if mv -f "$plist" "$SC_BAK_PLIST"; then
      SC_PLIST_MOVED=1
    else
      sc_switch_failed
      return 1
    fi
  fi
  if mv -f "$SC_CAND_PLIST" "$plist"; then
    SC_PLIST_SWITCHED=1
  else
    sc_switch_failed
    return 1
  fi

  local load_rc=0
  launchctl load "$plist" || load_rc=$?
  if [ "$load_rc" = 0 ]; then
    launchctl list "$SC_LAUNCHD_LABEL" >/dev/null 2>&1 || load_rc=1
  fi
  if [ "$load_rc" != 0 ]; then
    echo "launchctl load 后未能确认 $SC_LAUNCHD_LABEL" >&2
    sc_switch_failed
    return 1
  fi
  sc_discard_candidates   # 成功：清理切换中消耗剩余的备份/候选残留
}

# 唯一 install/regenerate 流程：enable/start/restart 共用，事务性两阶段。
#   阶段一 prepare（不停机）：记录旧 loaded 状态与旧文件存在性 →
#     迁移/读取旧控制文件 → 合并显式值并预检（端口/控制字符/目录）→
#     写候选 control（随机名 0600，非法值写盘前失败）→ 预建运行目录/日志 →
#     生成候选 plist 并以 plistlib 解析验证（未知键/凭据键/控制字符拒绝）。
#     任一失败：不 unload、正式文件字节与加载状态零改动、候选清理。
#   阶段二 switch：unload 旧 job（失败则不切文件）→ 同目录 rename 原子
#     切换 control/plist → load 候选并以精确 list 确认。load/确认失败：
#     停止候选 job、按状态机原子恢复旧文件（含原先不存在情形）、
#     旧先前 loaded 则重新 load 并确认、清理候选/备份；回滚 load 也失败
#     时明确非零并保留旧文件供人工恢复，绝不伪装成功。
sc_install_launchd() {
  local plist="$1" project_dir="$2" log_file="$3"
  if ! sc_prepare_launchd "$plist" "$project_dir" "$log_file"; then
    sc_discard_candidates
    return 1
  fi
  sc_switch_launchd "$plist" || return $?
}
