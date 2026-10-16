#!/bin/bash
# SurgeConf 服务管理脚本
# launchd 安装统一走 launchd_lib.sh::sc_install_launchd（唯一 install/regenerate 流程）

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=launchd_lib.sh
source "$PROJECT_DIR/launchd_lib.sh"

PLIST="$HOME/Library/LaunchAgents/com.surgeconf.plist"
LOG_FILE="$HOME/Library/Logs/SurgeConf/surgeconf.log"
CONTROL_ENV="$(sc_control_env_path)"

# 收紧新建文件权限：数据库/WAL/会话密钥默认仅属主可读写
umask 077

# 展示用监听参数：显式 env 优先，否则控制文件持久化值，最后安全默认
HOST="${SURGE_HOST:-}"
PORT="${SURGE_PORT:-}"
if [ -f "$CONTROL_ENV" ] && [ ! -L "$CONTROL_ENV" ]; then
  if [ -z "$HOST" ]; then
    HOST="$(grep '^SURGE_HOST=' "$CONTROL_ENV" | head -1 | cut -d= -f2- || true)"
  fi
  if [ -z "$PORT" ]; then
    PORT="$(grep '^SURGE_PORT=' "$CONTROL_ENV" | head -1 | cut -d= -f2- || true)"
  fi
fi
: "${HOST:=127.0.0.1}"
: "${PORT:=61830}"

case "${1:-help}" in
  start)
    sc_install_launchd "$PLIST" "$PROJECT_DIR" "$LOG_FILE"
    echo "SurgeConf 已启动 (launchd 守护模式)"
    ;;
  stop)
    launchctl unload "$PLIST" || true
    echo "SurgeConf 已停止"
    ;;
  restart)
    sc_install_launchd "$PLIST" "$PROJECT_DIR" "$LOG_FILE"
    echo "SurgeConf 已重启"
    ;;
  run)
    if [ ! -d "$PROJECT_DIR/venv" ]; then
      echo "首次运行，正在创建虚拟环境..."
      cd "$PROJECT_DIR" && python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
    fi
    echo "SurgeConf 独立启动中 (前台模式)..."
    echo "访问地址: http://${HOST}:${PORT}/"
    sc_ensure_directory "$(dirname "$LOG_FILE")"
    sc_ensure_log_file "$LOG_FILE"
    cd "$PROJECT_DIR" && exec "$PROJECT_DIR/venv/bin/python" -m uvicorn app.main:app \
      --host "${SURGE_HOST:-$HOST}" --port "${SURGE_PORT:-$PORT}" --no-access-log
    ;;
  enable)
    # 统一 install 流程：显式 SURGE_HOST/SURGE_PORT 会被持久化；
    # 均未设置时按安全默认 127.0.0.1 安装。需要局域网管理：
    #   SURGE_HOST=0.0.0.0 ./surgeconf.sh enable
    # （并先在设置页切换 lan_authenticated 访问模式）
    sc_install_launchd "$PLIST" "$PROJECT_DIR" "$LOG_FILE"
    echo "SurgeConf 已注册开机自启 ($PROJECT_DIR)"
    echo "监听: ${HOST}:${PORT}（控制文件持久化；未提供/显式空的键按语义保留或清除）"
    echo "日志: ${LOG_FILE}"
    ;;
  disable)
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "SurgeConf 已取消开机自启"
    ;;
  status)
    if launchctl list "$SC_LAUNCHD_LABEL" >/dev/null 2>&1; then
      PID=$(launchctl list | grep com.surgeconf | awk '{print $1}')
      if [ "$PID" != "-" ] && [ -n "$PID" ]; then
        echo "SurgeConf 运行中 (PID: $PID)"
        echo "访问地址: http://${HOST}:${PORT}/"
      else
        echo "SurgeConf 已注册但未运行"
      fi
    else
      echo "SurgeConf 未注册"
    fi
    ;;
  log)
    tail -f "${LOG_FILE}"
    ;;
  help|*)
    echo "用法: ./surgeconf.sh {start|stop|restart|run|enable|disable|status|log}"
    echo ""
    echo "  start    启动服务 (launchd；按控制文件/安全默认配置安装)"
    echo "  stop     停止服务"
    echo "  restart  重启服务 (重新生成 plist，保留控制文件配置)"
    echo "  run      独立启动 (前台模式，不使用 launchd)"
    echo "  enable   注册开机自启 (SURGE_HOST=0.0.0.0 开启局域网并持久化)"
    echo "  disable  取消开机自启"
    echo "  status   查看运行状态"
    echo "  log      实时查看日志 (~/Library/Logs/SurgeConf/surgeconf.log)"
    echo ""
    echo "控制文件 (~/Library/Application Support/SurgeConf/launchd.env, 0600):"
    echo "持久化 SURGE_HOST/SURGE_PORT/SURGECONF_DATA_DIR/SURGE_CONTROLLER_ACCESS/"
    echo "SURGE_HTTP_API；未提供的键保留现值，显式空值清除（回默认 127.0.0.1）。"
    ;;
esac
