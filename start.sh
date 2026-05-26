#!/bin/bash
# SurgeConf 启动脚本

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
mkdir -p logs

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

if [ "$1" = "--daemon" ]; then
  nohup uvicorn app.main:app --host "$HOST" --port "$PORT" > logs/surgeconf.log 2>&1 &
  echo "PID: $!"
elif [ "$1" = "--production" ]; then
  uvicorn app.main:app --host "$HOST" --port "$PORT"
else
  uvicorn app.main:app --host "$HOST" --port "$PORT" --reload
fi
