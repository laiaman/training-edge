#!/usr/bin/env bash
# 安装/卸载 TrainingEdge launchd 守护服务（macOS）。
# 本脚本只允许从本地项目副本安装；OneDrive 仅用于计划白名单备份。
set -euo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd -P "$SCRIPT_DIR/.." && pwd)"
RUNTIME_DIR="${TRAININGEDGE_RUNTIME_DIR:-$HOME/Library/Application Support/TrainingEdge}"
LABEL="com.trainingedge.server"
PLIST_DST="${TRAININGEDGE_PLIST_DST:-$HOME/Library/LaunchAgents/$LABEL.plist}"
GUI_DOMAIN="${TRAININGEDGE_LAUNCHD_DOMAIN:-gui/$(id -u)}"
PORT="${TRAININGEDGE_PORT:-8420}"
PYTHON_BIN="$RUNTIME_DIR/venv/bin/python"
BACKUP_DIR="$RUNTIME_DIR/backups/launchd"
RECOVERY_PIDFILE="$RUNTIME_DIR/.recovery_server.pid"
PLIST_TEMPLATE="$SCRIPT_DIR/$LABEL.plist"
GENERATED_PLIST=""

cleanup() {
    if [ -n "$GENERATED_PLIST" ] && [ -f "$GENERATED_PLIST" ]; then
        rm -f -- "$GENERATED_PLIST"
    fi
}
trap cleanup EXIT INT TERM

die() { echo "✗ $*" >&2; exit 1; }

is_cloud_path() {
    case "$1" in
        *"/Library/CloudStorage/"*|*"/OneDrive"*|*"/onedrive"*) return 0 ;;
        *) return 1 ;;
    esac
}

ensure_local_project() {
    is_cloud_path "$PROJECT_DIR" && die "拒绝从云盘副本安装：$PROJECT_DIR"
    [ -f "$PROJECT_DIR/scripts/cli.py" ] || die "缺少 scripts/cli.py：$PROJECT_DIR"
    [ -f "$PROJECT_DIR/api/app.py" ] || die "缺少 api/app.py：$PROJECT_DIR"
    [ -d "$PROJECT_DIR/web/static" ] || die "缺少静态资源目录：$PROJECT_DIR/web/static"
}

ensure_runtime_python() {
    mkdir -p "$RUNTIME_DIR/logs" "$BACKUP_DIR" "$(dirname "$PLIST_DST")"
    if [ ! -x "$PYTHON_BIN" ]; then
        echo "运行时 venv 不存在，正在创建：$RUNTIME_DIR/venv"
        python3 -m venv "$RUNTIME_DIR/venv"
        "$PYTHON_BIN" -m pip install -q --upgrade pip
        "$PYTHON_BIN" -m pip install -q -e "$PROJECT_DIR"
    else
        # 修正迁移后 editable install 仍指向旧云盘副本的问题。
        "$PYTHON_BIN" -m pip install -q --no-deps -e "$PROJECT_DIR"
    fi
}

render_plist() {
    GENERATED_PLIST="$(mktemp /private/tmp/trainingedge-launchd.XXXXXX)"
    sed -e "s|__RUNTIME__|$RUNTIME_DIR|g" \
        -e "s|__PROJECT__|$PROJECT_DIR|g" \
        "$PLIST_TEMPLATE" > "$GENERATED_PLIST"
    plutil -lint "$GENERATED_PLIST" >/dev/null || die "生成的 plist 校验失败"
    grep -Fq "$PROJECT_DIR" "$GENERATED_PLIST" || die "生成的 plist 未指向本地项目"
    local working_dir
    working_dir="$(sed -n '/<key>WorkingDirectory<\/key>/{n;s/.*<string>\(.*\)<\/string>.*/\1/p;}' "$GENERATED_PLIST")"
    is_cloud_path "$working_dir" && die "生成的 plist 仍指向云盘"
    return 0
}

preflight_application() {
    local result
    if ! result="$(cd "$PROJECT_DIR" && \
        TRAININGEDGE_RUNTIME_DIR="$RUNTIME_DIR" \
        TRAININGEDGE_SYNC_INTERVAL_HOURS=0 PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" - <<'PY'
import os
from pathlib import Path

import api.app
from engine import database, plan_store

runtime = Path(os.environ["TRAININGEDGE_RUNTIME_DIR"]).expanduser().resolve()

def resolved(value: str) -> Path:
    return Path(value).expanduser().resolve()

paths = {
    "db": database.DB_PATH.resolve(),
    "fit": resolved(os.environ.get("TRAININGEDGE_FIT_DIR", str(runtime / "fit_files"))),
    "log": resolved(os.environ.get("TRAININGEDGE_LOG_FILE", str(runtime / "logs/training_edge.log"))),
    "tokens": resolved(os.environ.get("GARMINTOKENS", str(runtime / "tokens"))),
}
for name, path in paths.items():
    try:
        path.relative_to(runtime)
    except ValueError as exc:
        raise SystemExit(f"{name} 路径不在 runtime 内: {path}") from exc
    if "CloudStorage" in path.parts or "OneDrive" in str(path):
        raise SystemExit(f"{name} 路径落在云盘: {path}")

document = plan_store.load_plan()
checksum = document.get("metadata", {}).get("checksum")
if not checksum:
    raise SystemExit("计划缺少 metadata.checksum")
print(f"revision={document['revision']} checksum={checksum} db={paths['db']}")
PY
    )"; then
        die "应用预检失败：${result:-未知错误}"
    fi
    echo "✓ 应用预检通过：$result"
}

cmd_preflight() {
    ensure_local_project
    ensure_runtime_python
    render_plist
    preflight_application
    echo "✓ launchd 安装预检完成（未停止或启动任何服务）"
}

listener_pids() {
    lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sort -u || true
}

listener_is_trainingedge() {
    local pid="$1" cmd cwd
    cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)"
    case "$cmd" in *"scripts/cli.py serve"*) ;; *) return 1 ;; esac
    case "$cwd" in */training-edge) return 0 ;; *) return 1 ;; esac
}

validate_listeners() {
    local pids pid
    pids="$(listener_pids)"
    [ -z "$pids" ] && return 0
    for pid in $pids; do
        listener_is_trainingedge "$pid" || die "端口 $PORT 被非 TrainingEdge 或来源不明的进程占用（PID ${pid}），未处理"
    done
}

stop_verified_listeners() {
    local pids pid
    pids="$(listener_pids)"
    [ -z "$pids" ] && return 0
    validate_listeners
    for pid in $pids; do
        echo "正在停止已核验的 TrainingEdge 监听者（PID ${pid}）"
        kill -TERM "$pid"
    done
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        [ -z "$(listener_pids)" ] && return 0
        sleep 1
    done
    die "已核验进程未在 10 秒内退出；未强制终止"
}

wait_for_health() {
    local expected_state="${1:-any}" body state
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
        body="$(curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/health" 2>/dev/null || true)"
        if echo "$body" | grep -q '"status":"ok"'; then
            if [ "$expected_state" = "launchd" ]; then
                state="$(launchctl print "$GUI_DOMAIN/$LABEL" 2>/dev/null | sed -n 's/^[[:space:]]*state = //p' | head -1)"
                [ "$state" = "running" ] || { sleep 1; continue; }
            fi
            return 0
        fi
        sleep 1
    done
    return 1
}

start_local_recovery() {
    echo "launchd 加载失败，恢复本地非 reload 服务……" >&2
    cd "$PROJECT_DIR"
    TRAININGEDGE_SYNC_INTERVAL_HOURS="${TRAININGEDGE_SYNC_INTERVAL_HOURS:-0}" \
        nohup "$PYTHON_BIN" scripts/cli.py serve --port "$PORT" \
        >> "$RUNTIME_DIR/logs/recovery-server.log" 2>&1 &
    local pid=$!
    echo "$pid" > "$RECOVERY_PIDFILE"
    if wait_for_health any; then
        echo "✓ 已恢复本地替代服务（PID ${pid}）；未回退到 OneDrive" >&2
        return 0
    fi
    echo "✗ 本地恢复服务也未通过健康检查，日志：$RUNTIME_DIR/logs/recovery-server.log" >&2
    return 1
}

backup_existing_plist() {
    if [ -f "$PLIST_DST" ]; then
        local backup="$BACKUP_DIR/$LABEL.$(date '+%Y%m%d-%H%M%S').plist"
        cp -p "$PLIST_DST" "$backup"
        echo "✓ 已备份原 plist：$backup"
    fi
}

cmd_install() {
    cmd_preflight
    backup_existing_plist

    # 只有全部预检通过后才改变服务状态。
    validate_listeners
    launchctl bootout "$GUI_DOMAIN/$LABEL" 2>/dev/null || true
    stop_verified_listeners

    local plist_new="$PLIST_DST.new.$$"
    cp -p "$GENERATED_PLIST" "$plist_new"
    mv -f "$plist_new" "$PLIST_DST"

    if launchctl bootstrap "$GUI_DOMAIN" "$PLIST_DST" && \
       launchctl kickstart -k "$GUI_DOMAIN/$LABEL" && \
       wait_for_health launchd; then
        rm -f -- "$RECOVERY_PIDFILE"
        echo "✓ $LABEL 已从本地项目安装并运行"
        echo "  项目: $PROJECT_DIR"
        echo "  runtime: $RUNTIME_DIR"
        echo "  日志: $RUNTIME_DIR/logs/server.log"
        return 0
    fi

    launchctl bootout "$GUI_DOMAIN/$LABEL" 2>/dev/null || true
    if start_local_recovery; then
        die "launchd 未注册（可在正常登录终端重试本脚本）；本地替代服务保持可用"
    fi
    die "launchd 安装及本地恢复均失败"
}

cmd_uninstall() {
    launchctl bootout "$GUI_DOMAIN/$LABEL" 2>/dev/null || true
    rm -f -- "$PLIST_DST"
    echo "已卸载 ${LABEL}（runtime 数据未删除）"
}

cmd_restart() {
    cmd_preflight
    launchctl print "$GUI_DOMAIN/$LABEL" >/dev/null 2>&1 || die "服务尚未注册，请先运行安装"
    launchctl kickstart -k "$GUI_DOMAIN/$LABEL"
    wait_for_health launchd || die "重启后健康检查失败"
    echo "✓ $LABEL 已重启并通过健康检查"
}

case "${1:-}" in
    --preflight) cmd_preflight ;;
    --uninstall) cmd_uninstall ;;
    --restart) cmd_restart ;;
    --help|-h) echo "用法: bash scripts/install_service.sh [--preflight|--restart|--uninstall]" ;;
    "") cmd_install ;;
    *) die "未知参数：$1" ;;
esac
