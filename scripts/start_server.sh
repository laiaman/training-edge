#!/usr/bin/env bash
# TrainingEdge 服务启动脚本（开发用）
#
# 生产/长期运行请优先使用 launchd 守护：
#   bash scripts/install_service.sh
#
# 用法:
#   bash scripts/start_server.sh             # 前台启动（开发，带 --reload）
#   bash scripts/start_server.sh --daemon    # 后台启动（开发）
#   bash scripts/start_server.sh --stop      # 停止开发进程
#   bash scripts/start_server.sh --status    # 检查状态
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
RUNTIME_DIR="${HOME}/Library/Application Support/TrainingEdge"
PORT="${TRAININGEDGE_PORT:-8420}"
PIDFILE="${RUNTIME_DIR}/.dev_server.pid"
LOGFILE="${RUNTIME_DIR}/logs/dev_server.log"
LABEL="com.trainingedge.server"
GUI_DOMAIN="gui/$(id -u)"

cd "$PROJECT_DIR"

_pick_python() {
    if [ -x "${RUNTIME_DIR}/venv/bin/python" ]; then
        echo "${RUNTIME_DIR}/venv/bin/python"
    elif [ -f ".venv/bin/activate" ]; then
        # shellcheck disable=SC1091
        source .venv/bin/activate
        command -v python
    else
        command -v python3
    fi
}

PYTHON_BIN="$(_pick_python)"

_is_running() {
    if [ -f "$PIDFILE" ]; then
        local pid
        pid=$(cat "$PIDFILE")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
        rm -f "$PIDFILE"
    fi
    return 1
}

_launchd_running() {
    launchctl print "${GUI_DOMAIN}/${LABEL}" 2>/dev/null | grep -q "state = running"
}

_kill_port() {
    local pids
    pids=$(lsof -ti:"$PORT" 2>/dev/null || true)
    if [ -n "$pids" ]; then
        for pid in $pids; do
            local cmd
            cmd=$(ps -p "$pid" -o command= 2>/dev/null || true)
            if echo "$cmd" | grep -qiE "python|uvicorn|training"; then
                echo "清理端口 $PORT 上的 TrainingEdge 残留进程: $pid"
                kill -9 "$pid" 2>/dev/null || true
            else
                echo "跳过端口 $PORT 上的非 TrainingEdge 进程: $pid"
            fi
        done
        sleep 1
    fi
}

cmd_stop() {
    if _is_running; then
        local pid
        pid=$(cat "$PIDFILE")
        echo "停止开发服务 (PID $pid)..."
        kill "$pid" 2>/dev/null || true
        sleep 2
        kill -9 "$pid" 2>/dev/null || true
        rm -f "$PIDFILE"
        echo "已停止"
    else
        echo "开发服务未在运行"
        _kill_port
    fi
}

cmd_status() {
    if _launchd_running; then
        echo "✓ launchd 守护运行中 ($LABEL, 端口 $PORT)"
    elif _is_running; then
        echo "✓ 开发服务运行中 (PID $(cat "$PIDFILE"), 端口 $PORT)"
    else
        echo "✗ TrainingEdge 未运行"
        echo "  启动: bash scripts/install_service.sh  （推荐，开机自启）"
        echo "  或:   bash scripts/start_server.sh      （开发调试）"
        return 1
    fi
    "$PYTHON_BIN" -c "
import urllib.request
try:
    r = urllib.request.urlopen('http://127.0.0.1:${PORT}/api/health', timeout=3)
    print('  健康检查: OK')
except Exception as e:
    print(f'  健康检查: FAIL - {e}')
" 2>/dev/null || true
}

cmd_start() {
    local daemon="${1:-}"

    if _launchd_running; then
        echo "launchd 守护已在运行，无需重复启动"
        echo "  重启: bash scripts/install_service.sh --restart"
        cmd_status
        return 0
    fi

    if _is_running; then
        echo "开发服务已在运行 (PID $(cat "$PIDFILE"))"
        cmd_status
        return 0
    fi

    _kill_port
    mkdir -p "$(dirname "$PIDFILE")" "$(dirname "$LOGFILE")"

    export TRAININGEDGE_SYNC_INTERVAL_HOURS="${TRAININGEDGE_SYNC_INTERVAL_HOURS:-0}"

    if [ "$daemon" = "--daemon" ]; then
        echo "后台启动开发服务 (端口 $PORT)..."
        nohup "$PYTHON_BIN" scripts/cli.py serve --reload --port "$PORT" \
            >> "$LOGFILE" 2>&1 &
        local pid=$!
        echo "$pid" > "$PIDFILE"
        sleep 2
        if _is_running; then
            echo "✓ 启动成功 (PID $pid)"
            echo "  日志: tail -f $LOGFILE"
        else
            echo "✗ 启动失败，查看日志: $LOGFILE"
            tail -20 "$LOGFILE" 2>/dev/null || true
            return 1
        fi
    else
        echo "前台启动开发服务 (端口 $PORT, Ctrl+C 停止)..."
        "$PYTHON_BIN" scripts/cli.py serve --reload --port "$PORT"
    fi
}

case "${1:-}" in
    --stop)   cmd_stop ;;
    --status) cmd_status ;;
    --daemon) cmd_start --daemon ;;
    *)        cmd_start ;;
esac
