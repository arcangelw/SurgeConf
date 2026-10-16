#!/bin/bash
# launchd_lib.sh — SurgeConf launchd 安装与配置持久化共享函数库
#
# 被 surgeconf.sh（install/start/restart/enable）与 run_surgeconf.sh（安全读取）
# source 使用；测试直接 source 验证行为（不真实 launchctl）。
#
# 持久化语义（对 SURGE_HOST/SURGE_PORT/SURGECONF_DATA_DIR/
# SURGE_CONTROLLER_ACCESS/SURGE_HTTP_API 一致）：
#   - 调用时环境变量为非空值 → 写入 launchd.env 并在后续 restart 中保留；
#   - 未设置或显式空值 → 从 launchd.env 移除该键（回到安全默认）；
#   - 控制器凭据只存 data dir 下 0600 的 launchd.env，绝不写入 plist。

sc_resolve_data_dir() {
  printf '%s\n' "${SURGECONF_DATA_DIR:-$HOME/Library/Application Support/SurgeConf}"
}

# 预建日志文件：拒绝 symlink，收敛 0600（在 launchd/重定向前调用）
sc_ensure_log_file() {
  local f="$1"
  if [ -L "$f" ]; then
    echo "拒绝 symlink 日志路径: $f" >&2
    return 1
  fi
  touch "$f" && chmod 600 "$f"
}

# 将当前环境中的持久化键写入 launchd.env（原子替换：0600 临时文件 + mv）
sc_write_launchd_env() {
  local file="$1" tmp="${1}.tmp.$$"
  local dir
  dir="$(dirname "$file")"
  mkdir -p "$dir" && chmod 700 "$dir"
  : > "$tmp" && chmod 600 "$tmp"
  local key
  for key in SURGE_HOST SURGE_PORT SURGECONF_DATA_DIR \
             SURGE_CONTROLLER_ACCESS SURGE_HTTP_API; do
    local val="${!key}"
    if [ -n "$val" ]; then
      printf '%s=%s\n' "$key" "$val" >> "$tmp"
    fi
  done
  mv -f "$tmp" "$file"
  chmod 600 "$file"
}

# 安全读取 launchd.env：拒绝 symlink/非普通文件/未知键/CR；
# 值格式为单行 KEY=value，天然不含换行。逐键 export。
sc_read_launchd_env() {
  local file="$1"
  [ -f "$file" ] || return 0
  if [ -L "$file" ]; then
    echo "拒绝 symlink 的 launchd.env: $file" >&2
    return 1
  fi
  local line key val
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    key="${line%%=*}"
    val="${line#*=}"
    case "$key" in
      SURGE_HOST|SURGE_PORT|SURGECONF_DATA_DIR|SURGE_CONTROLLER_ACCESS|SURGE_HTTP_API) ;;
      *)
        echo "launchd.env 含未知键: $key" >&2
        return 1
        ;;
    esac
    case "$val" in *$'\r'*)
      echo "launchd.env 值含 CR: $key" >&2
      return 1
      ;;
    esac
    export "$key=$val"
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

# 值中不得出现任何控制字符（含 tab；plist/路径值均不应含）
sc_assert_no_control() {
  case "$1" in
    *[[:cntrl:]]*)
      echo "配置值含控制字符" >&2
      return 1
      ;;
  esac
}

# 生成 launchd plist（唯一安装物）：Umask 63(=077)、日志捕获到 ~/Library/Logs、
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
  mkdir -p "$(dirname "$plist")"
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
    <string>com.surgeconf</string>
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
#   1) 已加载则 launchctl unload（失败传播，不吞错）；
#   2) 持久化当前安全配置到 launchd.env；
#   3) 以持久化值（缺省回退安全默认 127.0.0.1）重新生成 plist —— 旧 plist
#      的 0.0.0.0/仓库 logs 等遗留字段必然被重写；
#   4) launchctl load 并确认成功（失败传播）。

# 以参数形式导出环境键（避免源码中出现 键名="字面" 形态）
sc_set_env() {
  export "$1=$2"
}

sc_install_launchd() {
  local plist="$1" project_dir="$2" data_dir="$3" env_file="$4" log_file="$5"

  if launchctl list 2>/dev/null | grep -q "com\.surgeconf"; then
    launchctl unload "$plist" || return $?
  fi

  # 键更新语义：调用环境「显式提供」的键（含空值）覆盖持久化现值
  # ——显式空 = 清除回默认；「未提供」（unset）= 保留持久化现值，
  # restart 不丢配置。先记录调用时显式值，再读旧持久化，最后回放。
  local in_host in_port in_data in_ctrl in_api
  local set_host=0 set_port=0 set_data=0 set_ctrl=0 set_api=0
  if [ "${SURGE_HOST+x}" = x ]; then set_host=1; in_host="$SURGE_HOST"; fi
  if [ "${SURGE_PORT+x}" = x ]; then set_port=1; in_port="$SURGE_PORT"; fi
  if [ "${SURGECONF_DATA_DIR+x}" = x ]; then set_data=1; in_data="$SURGECONF_DATA_DIR"; fi
  if [ "${SURGE_CONTROLLER_ACCESS+x}" = x ]; then set_ctrl=1; in_ctrl="$SURGE_CONTROLLER_ACCESS"; fi
  if [ "${SURGE_HTTP_API+x}" = x ]; then set_api=1; in_api="$SURGE_HTTP_API"; fi

  sc_read_launchd_env "$env_file" || return $?       # 旧持久化值填充
  if [ "$set_host" = 1 ]; then sc_set_env SURGE_HOST "$in_host"; fi
  if [ "$set_port" = 1 ]; then sc_set_env SURGE_PORT "$in_port"; fi
  if [ "$set_data" = 1 ]; then sc_set_env SURGECONF_DATA_DIR "$in_data"; fi
  if [ "$set_ctrl" = 1 ]; then sc_set_env SURGE_CONTROLLER_ACCESS "$in_ctrl"; fi
  if [ "$set_api" = 1 ]; then sc_set_env SURGE_HTTP_API "$in_api"; fi
  sc_write_launchd_env "$env_file" || return $?       # 合并结果落盘

  local host="${SURGE_HOST:-127.0.0.1}"
  local port="${SURGE_PORT:-61830}"
  local pdata="${SURGECONF_DATA_DIR:-$HOME/Library/Application Support/SurgeConf}"

  mkdir -p "$HOME/Library/Logs/SurgeConf"
  sc_ensure_log_file "$log_file" || return $?
  sc_generate_plist "$plist" "$project_dir" "$log_file" "$host" "$port" "$pdata" || return $?

  launchctl load "$plist" || return $?
  launchctl list 2>/dev/null | grep -q "com\.surgeconf" || {
    echo "launchctl load 后未见 com.surgeconf" >&2
    return 1
  }
}
