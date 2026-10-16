#!/bin/bash
# SurgeConf 启动脚本

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# shellcheck source=launchd_lib.sh
source "$SCRIPT_DIR/launchd_lib.sh"

# 收紧新建文件权限：数据库/WAL/会话密钥默认仅属主可读写
umask 077

# 运行目录（默认 Application Support；SURGECONF_DATA_DIR 可整体重定向）
DATA_DIR="${SURGECONF_DATA_DIR:-$HOME/Library/Application Support/SurgeConf}"
LOG_DIR="$HOME/Library/Logs/SurgeConf"
sc_ensure_directory "$DATA_DIR"
sc_ensure_directory "$LOG_DIR"

# 检查虚拟环境
if [ ! -d "venv" ]; then
  echo "首次运行，正在创建虚拟环境..."
  python3 -m venv venv
  source venv/bin/activate
  pip install -r requirements.txt
else
  source venv/bin/activate
fi

HOST="${SURGE_HOST:-127.0.0.1}"
PORT="${SURGE_PORT:-61830}"

echo "SurgeConf 启动中..."
echo "访问地址: http://${HOST}:${PORT}"

if [ "${1:-}" = "--daemon" ]; then
  # console 回退日志：nohup 的 stdout/stderr 只写这里。结构化事件日志
  # surgeconf.log 由应用内私有轮转 handler 独占写入——任何 shell 重定向
  # 都不得指向它（轮转 rename 后旧 FD 会写错 inode 的双 writer 问题）
  sc_ensure_log_file "$LOG_DIR/surgeconf-console.log"
  nohup uvicorn app.main:app --host "$HOST" --port "$PORT" --no-access-log \
    >> "$LOG_DIR/surgeconf-console.log" 2>&1 &
  echo "PID: $!"
  echo "日志: $LOG_DIR/surgeconf-console.log（结构化事件日志: $LOG_DIR/surgeconf.log）"
elif [ "${1:-}" = "--production" ]; then
  uvicorn app.main:app --host "$HOST" --port "$PORT" --no-access-log
else
  uvicorn app.main:app --host "$HOST" --port "$PORT" --no-access-log --reload
fi
