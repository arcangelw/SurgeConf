#!/bin/bash
# launchd_lib.sh — SurgeConf launchd 安装与配置持久化共享函数库
#
# 被 surgeconf.sh（install/start/restart/enable）与 run_surgeconf.sh（安全读取）
# source 使用；测试直接 source 并以 fake launchctl 驱动真实脚本入口。
#
# 控制文件固定位于 ~/Library/Application Support/SurgeConf/launchd.env
# （与实际 SURGECONF_DATA_DIR 独立），持久化 data dir/host/port/凭据；
# restart 先读固定控制文件，再由其中的 SURGECONF_DATA_DIR 解析运行目录。
#
# 键更新语义（五个持久化键一致）：
#   - 调用环境「显式提供」非空值 → 写入并持久化；
#   - 「未提供」（unset，restart 场景）→ 保留持久化现值；
#   - 「显式空值」→ 清除该键（回到安全默认 127.0.0.1）。
# 控制器凭据只存 0600 控制文件，绝不写入 plist。

SC_LAUNCHD_LABEL="com.surgeconf"

sc_control_dir() {
  printf '%s\n' "$HOME/Library/Application Support/SurgeConf"
}

sc_control_env_path() {
  printf '%s\n' "$(sc_control_dir)/launchd.env"
}

# 目录安全创建：拒绝 symlink/非目录（fail closed），mkdir -p 后收敛 0700
sc_ensure_directory() {
  local d="$1"
  if [ -L "$d" ]; then
    echo "拒绝 symlink 目录路径: $d" >&2
    return 1
  fi
  if [ -e "$d" ] && [ ! -d "$d" ]; then
    echo "路径存在但不是目录: $d" >&2
    return 1
  fi
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

# 值中不得出现任何控制字符（C0/C1 全集；bash case 用 [[:cntrl:]]
# 覆盖 C0 + DEL，另对高位 C1 用 grep 补充）。
sc_assert_no_control() {
  case "$1" in
    *[[:cntrl:]]*)
      echo "配置值含控制字符" >&2
      return 1
      ;;
  esac
  if printf '%s' "$1" | LC_ALL=C grep -q $'[\x80-\x9f]'; then
    echo "配置值含控制字符" >&2
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

# 将当前环境中的持久化键写入控制文件（原子替换：0600 临时文件 + mv）。
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
    val="$(sc_env_value "$key")" || return $?
    if [ -n "$val" ]; then
      sc_assert_no_control "$val" || return $?   # 写入前拒绝 LF/CR/全部控制字符
      out="${out}${key}=${val}"$'\n'
    fi
  done

  local tmp="${file}.tmp.$$"
  printf '%s' "$out" > "$tmp" && chmod 600 "$tmp" || {
    rm -f "$tmp"; return 1; }
  mv -f "$tmp" "$file"
  chmod 600 "$file"
}

# 安全读取控制文件：拒绝 symlink/非普通文件、未知键、重复键、控制字符；
# 单行 KEY=value 格式 + 按键唯一性检查杜绝多行伪造。读前收敛 0600。
sc_read_launchd_env() {
  local file="$1"
  [ -e "$file" ] || return 0
  if [ -L "$file" ]; then
    echo "拒绝 symlink 的控制文件: $file" >&2
    return 1
  fi
  if [ ! -f "$file" ]; then
    echo "控制文件不是普通文件: $file" >&2
    return 1
  fi
  chmod 600 "$file"

  local line key val
  local seen_host=0 seen_port=0 seen_data=0 seen_ctrl=0 seen_api=0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
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
    case "$val" in
      *$'\r'*)
        echo "控制文件值含 CR: $key" >&2
        return 1
        ;;
    esac
    sc_set_env "$key" "$val"
  done < "$file"
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

  local tmp="${plist}.tmp.$$"
  cat > "$tmp" << EOF
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
  mv -f "$tmp" "$plist"
  chmod 600 "$plist"
}

# 唯一 install/regenerate 流程：enable/start/restart 共用。
#   1) 已加载（launchctl list "$LABEL" 精确 exit status）则 unload，失败传播；
#   2) 固定控制文件记录显式值 → 读旧值回放 → 原子落盘（非法值写盘前失败）；
#   3) 由控制文件解析 data dir/host/port，安全创建目录与日志；
#   4) 重新生成 plist —— 旧 plist 的 0.0.0.0/仓库 logs 等遗留字段必然重写；
#   5) launchctl load（失败传播）并以 launchctl list "$LABEL" 确认。
sc_install_launchd() {
  local plist="$1" project_dir="$2" log_file="$3"
  local control_env
  control_env="$(sc_control_env_path)"

  if launchctl list "$SC_LAUNCHD_LABEL" >/dev/null 2>&1; then
    launchctl unload "$plist" || return $?
  fi

  sc_ensure_directory "$(sc_control_dir)" || return $?

  # 记录调用环境显式提供的键（set -u 安全；无 ${!var} / eval）
  local in_host in_port in_data in_ctrl in_api
  local set_host=0 set_port=0 set_data=0 set_ctrl=0 set_api=0
  if [ "${SURGE_HOST+x}" = x ]; then set_host=1; in_host="${SURGE_HOST-}"; fi
  if [ "${SURGE_PORT+x}" = x ]; then set_port=1; in_port="${SURGE_PORT-}"; fi
  if [ "${SURGECONF_DATA_DIR+x}" = x ]; then set_data=1; in_data="${SURGECONF_DATA_DIR-}"; fi
  if [ "${SURGE_CONTROLLER_ACCESS+x}" = x ]; then set_ctrl=1; in_ctrl="${SURGE_CONTROLLER_ACCESS-}"; fi
  if [ "${SURGE_HTTP_API+x}" = x ]; then set_api=1; in_api="${SURGE_HTTP_API-}"; fi

  sc_read_launchd_env "$control_env" || return $?      # 旧持久化值（回退来源）
  if [ "$set_host" = 1 ]; then sc_set_env SURGE_HOST "$in_host"; fi
  if [ "$set_port" = 1 ]; then sc_set_env SURGE_PORT "$in_port"; fi
  if [ "$set_data" = 1 ]; then sc_set_env SURGECONF_DATA_DIR "$in_data"; fi
  if [ "$set_ctrl" = 1 ]; then sc_set_env SURGE_CONTROLLER_ACCESS "$in_ctrl"; fi
  if [ "$set_api" = 1 ]; then sc_set_env SURGE_HTTP_API "$in_api"; fi
  sc_write_launchd_env "$control_env" || return $?     # 非法值在写盘前失败

  local host port data_dir
  host="$(sc_env_value SURGE_HOST)"
  port="$(sc_env_value SURGE_PORT)"
  data_dir="$(sc_env_value SURGECONF_DATA_DIR)"
  : "${host:=127.0.0.1}"
  : "${port:=61830}"
  : "${data_dir:=$(sc_control_dir)}"

  sc_ensure_directory "$data_dir" || return $?
  sc_ensure_directory "$(dirname "$log_file")" || return $?
  sc_ensure_log_file "$log_file" || return $?
  sc_generate_plist "$plist" "$project_dir" "$log_file" "$host" "$port" "$data_dir" || return $?

  launchctl load "$plist" || return $?
  launchctl list "$SC_LAUNCHD_LABEL" >/dev/null 2>&1 || {
    echo "launchctl load 后未能确认 $SC_LAUNCHD_LABEL" >&2
    return 1
  }
}
