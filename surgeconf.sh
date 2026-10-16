#!/bin/bash
# SurgeConf 服务管理脚本
# launchd 安装统一走 launchd_lib.sh::sc_install_launchd（唯一 install/regenerate 流程）
# 控制文件消费一律经 sc_read_launchd_env / sc_resolve_runtime_config（无 grep/head/cut 旁路）

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=launchd_lib.sh
source "$PROJECT_DIR/launchd_lib.sh"

PLIST="$HOME/Library/LaunchAgents/com.surgeconf.plist"
# launchd 的 StandardOut/ErrorPath 只指向 console 回退日志；结构化事件
# 日志 surgeconf.log 由应用内私有轮转 handler 独占写入，禁止外部重定向
CONSOLE_LOG_FILE="$HOME/Library/Logs/SurgeConf/surgeconf-console.log"
EVENT_LOG_FILE="$HOME/Library/Logs/SurgeConf/surgeconf.log"

# 收紧新建文件权限：数据库/WAL/会话密钥默认仅属主可读写
umask 077

case "${1:-help}" in
  start)
    sc_install_launchd "$PLIST" "$PROJECT_DIR" "$CONSOLE_LOG_FILE"
    echo "SurgeConf 已启动 (launchd 守护模式)"
    ;;
  stop)
    launchctl unload "$PLIST" || true
    echo "SurgeConf 已停止"
    ;;
  restart)
    sc_install_launchd "$PLIST" "$PROJECT_DIR" "$CONSOLE_LOG_FILE"
    echo "SurgeConf 已重启"
    ;;
  run)
    if [ ! -d "$PROJECT_DIR/venv" ]; then
      echo "首次运行，正在创建虚拟环境..."
      cd "$PROJECT_DIR" && python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
    fi
    # 配置合并：控制文件值 + 显式环境值（统一经安全 reader）
    sc_resolve_runtime_config || {
      echo "控制文件校验失败，拒绝启动" >&2
      exit 1
    }
    HOST="${SURGE_HOST-}"
    PORT="${SURGE_PORT-}"
    : "${HOST:=127.0.0.1}"
    : "${PORT:=61830}"
    # 空值（含显式空）已由 sc_resolve_runtime_config unset，回默认控制目录
    DATA_DIR="${SURGECONF_DATA_DIR-}"
    : "${DATA_DIR:=$(sc_control_dir)}"
    echo "SurgeConf 独立启动中 (前台模式)..."
    echo "访问地址: http://${HOST}:${PORT}/"
    sc_ensure_directory "$DATA_DIR"
    sc_ensure_directory "$(dirname "$CONSOLE_LOG_FILE")"
    sc_ensure_log_file "$CONSOLE_LOG_FILE"
    cd "$PROJECT_DIR" && exec "$PROJECT_DIR/venv/bin/python" -m uvicorn app.main:app \
      --host "$HOST" --port "$PORT" --no-access-log
    ;;
  enable)
    # 统一 install 流程：显式 SURGE_HOST/SURGE_PORT 会被持久化；
    # 均未设置时按安全默认 127.0.0.1 安装。需要局域网管理：
    #   SURGE_HOST=0.0.0.0 ./surgeconf.sh enable
    # （并先在设置页切换 lan_authenticated 访问模式）
    sc_install_launchd "$PLIST" "$PROJECT_DIR" "$CONSOLE_LOG_FILE"
    echo "SurgeConf 已注册开机自启 ($PROJECT_DIR)"
    echo "日志: ${CONSOLE_LOG_FILE}（结构化事件日志: ${EVENT_LOG_FILE}）"
    ;;
  disable)
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "SurgeConf 已取消开机自启"
    ;;
  status)
    # 精确 label 查询（exit status + PID 列）：不解析 label、不做子串 grep。
    # launchctl list <label> 成功仅表示已加载；第一列数字 PID 才是运行中，
    # "-"/空 表示已加载但当前无进程（崩溃/退避状态）。
    if listing="$(launchctl list "$SC_LAUNCHD_LABEL" 2>/dev/null)"; then
      pid="${listing%%$'\t'*}"
      case "$pid" in
        ''|*[!0-9]*) echo "SurgeConf 已加载但未运行" ;;
        *) echo "SurgeConf 运行中" ;;
      esac
    elif [ -f "$PLIST" ]; then
      echo "SurgeConf 已注册但未运行"
    else
      echo "SurgeConf 未注册"
    fi
    ;;
  log)
    tail -F "${EVENT_LOG_FILE}"
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
    echo "  status   查看运行状态 (精确 label 判定)"
    echo "  log      实时查看结构化事件日志 (~/Library/Logs/SurgeConf/surgeconf.log)"
    echo "           (console 回退日志: ~/Library/Logs/SurgeConf/surgeconf-console.log)"
    echo ""
    echo "控制文件 (~/Library/Application Support/SurgeConf/launchd.env, 0600):"
    echo "持久化 SURGE_HOST/SURGE_PORT/SURGECONF_DATA_DIR/SURGE_CONTROLLER_ACCESS/"
    echo "SURGE_HTTP_API；未提供的键保留现值，显式空值清除（回默认 127.0.0.1）。"
    ;;
esac
