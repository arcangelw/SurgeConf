#!/bin/bash
# SurgeConf launchd 包装脚本
# 由 com.surgeconf.plist 调用（plist 由统一 install 流程生成）。
# 日志由 launchd StandardOut/ErrorPath 捕获到 ~/Library/Logs/SurgeConf/surgeconf.log。

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

# shellcheck source=launchd_lib.sh
source "$PROJECT_DIR/launchd_lib.sh"

# 收紧新建文件权限：数据库/WAL/会话密钥默认仅属主可读写
# （plist 亦设置 Umask 63，双保险）
umask 077

# 先读固定控制文件（~/Library/Application Support/SurgeConf/launchd.env，
# 与实际数据目录独立）：获得持久化的 host/port/data dir/控制器凭据。
# 消费统一经 sc_read_launchd_env（0600、普通文件、无 symlink、
# 重复/未知/控制字符拒绝）。
sc_read_launchd_env "$(sc_control_env_path)"

# 运行目录：控制文件持久化值或默认；空值视为 unset——绝不把空
# SURGECONF_DATA_DIR 传给子进程（Python 端 Path("") 会解析为当前目录）
if [ -z "${SURGECONF_DATA_DIR-}" ]; then
  unset SURGECONF_DATA_DIR || true
  DATA_DIR="$(sc_control_dir)"
else
  DATA_DIR="$SURGECONF_DATA_DIR"
fi
LOG_DIR="$HOME/Library/Logs/SurgeConf"
sc_ensure_directory "$DATA_DIR"
sc_ensure_directory "$LOG_DIR"

# launchd 捕获日志前预建 0600（拒绝 symlink/FIFO/socket）
sc_ensure_log_file "$LOG_DIR/surgeconf.log"

exec "$PROJECT_DIR/venv/bin/python" -m uvicorn app.main:app \
  --host "${SURGE_HOST:-127.0.0.1}" \
  --port "${SURGE_PORT:-61830}" \
  --no-access-log
