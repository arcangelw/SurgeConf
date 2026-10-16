#!/bin/bash
# SurgeConf launchd 包装脚本
# 由 com.surgeconf.plist 调用（plist 由 surgeconf.sh 的统一 install 流程生成）。
# 日志由 launchd StandardOut/ErrorPath 捕获到 ~/Library/Logs/SurgeConf/surgeconf.log。

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

# shellcheck source=launchd_lib.sh
source "$PROJECT_DIR/launchd_lib.sh"

# 收紧新建文件权限：数据库/WAL/会话密钥默认仅属主可读写
# （plist 亦设置 Umask 63，双保险）
umask 077

# 运行目录：plist 传入 SURGECONF_DATA_DIR（或默认 Application Support）
DATA_DIR="$(sc_resolve_data_dir)"
LOG_DIR="$HOME/Library/Logs/SurgeConf"
mkdir -p "$DATA_DIR" "$LOG_DIR"
chmod 700 "$DATA_DIR" "$LOG_DIR"

# 安全读取持久化配置（含控制器凭据；凭据只存 launchd.env 0600，不进 plist）
sc_read_launchd_env "$DATA_DIR/launchd.env"

# launchd 捕获日志前预建 0600（拒绝 symlink）
sc_ensure_log_file "$LOG_DIR/surgeconf.log"

exec "$PROJECT_DIR/venv/bin/python" -m uvicorn app.main:app \
  --host "${SURGE_HOST:-127.0.0.1}" \
  --port "${SURGE_PORT:-61830}" \
  --no-access-log
