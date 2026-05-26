#!/bin/bash
# SurgeConf 服务管理脚本

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
SURGE_HOST="${SURGE_HOST:-127.0.0.1}"
SURGE_PORT="${SURGE_PORT:-61830}"
PLIST="$HOME/Library/LaunchAgents/com.surgeconf.plist"

case "${1:-help}" in
  start)
    launchctl load "$PLIST"
    echo "SurgeConf 已启动 (launchd 守护模式)"
    ;;
  stop)
    launchctl unload "$PLIST"
    echo "SurgeConf 已停止"
    ;;
  restart)
    launchctl unload "$PLIST" 2>/dev/null
    sleep 1
    launchctl load "$PLIST"
    echo "SurgeConf 已重启"
    ;;
  run)
    if [ ! -d "$PROJECT_DIR/venv" ]; then
      echo "首次运行，正在创建虚拟环境..."
      cd "$PROJECT_DIR" && python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
    fi
    echo "SurgeConf 独立启动中 (前台模式)..."
    echo "访问地址: http://${SURGE_HOST}:${SURGE_PORT}/"
    cd "$PROJECT_DIR" && exec "$PROJECT_DIR/venv/bin/python" -m uvicorn app.main:app \
      --host "${SURGE_HOST}" --port "${SURGE_PORT}"
    ;;
  enable)
    cat > "$PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.surgeconf</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PROJECT_DIR}/run_surgeconf.sh</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${PROJECT_DIR}/logs/surgeconf.log</string>
    <key>StandardErrorPath</key>
    <string>${PROJECT_DIR}/logs/surgeconf.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>SURGE_HOST</key>
        <string>0.0.0.0</string>
        <key>SURGE_PORT</key>
        <string>${SURGE_PORT}</string>
    </dict>
</dict>
</plist>
EOF
    launchctl load "$PLIST" 2>/dev/null
    echo "SurgeConf 已注册开机自启 ($PROJECT_DIR)"
    ;;
  disable)
    launchctl unload "$PLIST" 2>/dev/null
    rm -f "$PLIST"
    echo "SurgeConf 已取消开机自启"
    ;;
  status)
    if launchctl list | grep -q com.surgeconf; then
      PID=$(launchctl list | grep com.surgeconf | awk '{print $1}')
      if [ "$PID" != "-" ]; then
        echo "SurgeConf 运行中 (PID: $PID)"
        echo "访问地址: http://${SURGE_HOST}:${SURGE_PORT}/"
      else
        echo "SurgeConf 已注册但未运行"
      fi
    else
      echo "SurgeConf 未注册"
    fi
    ;;
  log)
    tail -f "$PROJECT_DIR/logs/surgeconf.log"
    ;;
  help|*)
    echo "用法: ./surgeconf.sh {start|stop|restart|run|enable|disable|status|log}"
    echo ""
    echo "  start    启动服务 (launchd 守护模式)"
    echo "  stop     停止服务"
    echo "  restart  重启服务"
    echo "  run      独立启动 (前台模式，不使用 launchd)"
    echo "  enable   注册开机自启"
    echo "  disable  取消开机自启"
    echo "  status   查看运行状态"
    echo "  log      实时查看日志"
    ;;
esac
