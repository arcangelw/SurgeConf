#!/bin/bash
# SurgeConf launchd 包装脚本
# 由 com.surgeconf.plist 调用，用于动态解析项目路径
# 日志由 launchd 的 StandardOutPath/StandardErrorPath 捕获到
# ~/Library/Logs/SurgeConf/surgeconf.log（surgeconf.sh enable 生成 plist 时设置）

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

# 收紧新建文件权限：数据库/WAL/会话密钥默认仅属主可读写
umask 077

# 运行目录（默认 Application Support；SURGECONF_DATA_DIR 可整体重定向）
DATA_DIR="${SURGECONF_DATA_DIR:-$HOME/Library/Application Support/SurgeConf}"
LOG_DIR="$HOME/Library/Logs/SurgeConf"
mkdir -p "$DATA_DIR" "$LOG_DIR"
chmod 700 "$DATA_DIR" "$LOG_DIR"

# 默认仅回环监听；LAN 管理需显式 SURGE_HOST（由 plist 环境变量传递）
exec "$PROJECT_DIR/venv/bin/python" -m uvicorn app.main:app \
  --host "${SURGE_HOST:-127.0.0.1}" \
  --port "${SURGE_PORT:-61830}" \
  --no-access-log
