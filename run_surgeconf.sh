#!/bin/bash
# SurgeConf launchd 包装脚本
# 由 com.surgeconf.plist 调用，用于动态解析项目路径

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR" || exit 1
mkdir -p logs

exec "$PROJECT_DIR/venv/bin/python" -m uvicorn app.main:app \
  --host "${SURGE_HOST:-127.0.0.1}" \
  --port "${SURGE_PORT:-61830}"
