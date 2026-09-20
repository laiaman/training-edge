#!/usr/bin/env bash
# 确定性验证：launchd、8420 监听者、运行路径、健康接口和计划 revision。
set -euo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd -P "$SCRIPT_DIR/.." && pwd)"
RUNTIME_DIR="${TRAININGEDGE_RUNTIME_DIR:-$HOME/Library/Application Support/TrainingEdge}"
LABEL="com.trainingedge.server"
GUI_DOMAIN="${TRAININGEDGE_LAUNCHD_DOMAIN:-gui/$(id -u)}"
PORT="${TRAININGEDGE_PORT:-8420}"
PLIST_DST="${TRAININGEDGE_PLIST_DST:-$HOME/Library/LaunchAgents/$LABEL.plist}"
PYTHON_BIN="$RUNTIME_DIR/venv/bin/python"

fail() { echo "✗ $*" >&2; exit 1; }

case "$PROJECT_DIR" in
    *"/Library/CloudStorage/"*|*"/OneDrive"*|*"/onedrive"*) fail "项目运行源位于云盘：$PROJECT_DIR" ;;
esac

[ -x "$PYTHON_BIN" ] || fail "runtime Python 不存在：$PYTHON_BIN"
[ -f "$PLIST_DST" ] || fail "launchd plist 不存在：$PLIST_DST"
plutil -lint "$PLIST_DST" >/dev/null || fail "launchd plist 语法错误"
grep -Fq "<string>$PROJECT_DIR</string>" "$PLIST_DST" || fail "plist WorkingDirectory 不是本地项目"
launchctl print "$GUI_DOMAIN/$LABEL" 2>/dev/null | grep -q 'state = running' || fail "launchd 服务未处于 running"

pids="$(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sort -u)"
[ -n "$pids" ] || fail "端口 $PORT 无监听者"
[ "$(echo "$pids" | wc -l | tr -d ' ')" = "1" ] || fail "端口 $PORT 存在多个监听者：$pids"
pid="$pids"
cmd="$(ps -p "$pid" -o command=)"
cwd="$(lsof -a -p "$pid" -d cwd -Fn | sed -n 's/^n//p' | head -1)"
[ "$cwd" = "$PROJECT_DIR" ] || fail "监听进程工作目录错误：$cwd"
case "$cmd" in *"scripts/cli.py serve"*) ;; *) fail "监听者不是 TrainingEdge CLI：$cmd" ;; esac

health="$(curl -fsS --max-time 3 "http://127.0.0.1:$PORT/api/health")"
echo "$health" | grep -q '"status":"ok"' || fail "健康接口异常：$health"

result="$(cd "$PROJECT_DIR" && "$PYTHON_BIN" - <<'PY'
import json
import sqlite3
import urllib.request
from engine import database, plan_store

document = plan_store.load_plan()
with sqlite3.connect(database.DB_PATH) as conn:
    row = conn.execute("SELECT value FROM settings WHERE key='api_key'").fetchone()
key = row[0] if row else ""
request = urllib.request.Request("http://127.0.0.1:8420/api/plan-document")
if key:
    request.add_header("X-API-Key", key)
with urllib.request.urlopen(request, timeout=3) as response:
    remote = json.load(response)["plan"]
if remote["revision"] != document["revision"]:
    raise SystemExit(f"API revision {remote['revision']} != YAML revision {document['revision']}")
print(f"revision={document['revision']} checksum={document['metadata']['checksum']}")
PY
)" || fail "计划 API 回读失败"

echo "✓ 本地 TrainingEdge E2E 通过"
echo "  PID=$pid cwd=$cwd"
echo "  $result"
