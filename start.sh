#!/bin/bash
# ContentPipe 服务启动脚本
# 用法: ./start.sh [start|stop|restart|status|logs|install-agent]

set -euo pipefail

SCRIPT_PATH="$(readlink -f "$0")"
PLUGIN_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"

# 加载本地环境变量（如果存在）
if [ -f "$PLUGIN_DIR/.env.local" ]; then
    set -a
    source "$PLUGIN_DIR/.env.local"
    set +a
    echo "✅ 已加载 .env.local"
fi
APP_NAME="contentpipe"
PORT="${CONTENTPIPE_PORT:-8765}"
HOST="${CONTENTPIPE_HOST:-0.0.0.0}"
LOG_FILE="/tmp/contentpipe.log"
CONTENTPIPE_AGENT_ID="${CONTENTPIPE_AGENT_ID:-contentpipe-blank}"
CONTENTPIPE_AGENT_WORKSPACE="${CONTENTPIPE_AGENT_WORKSPACE:-$HOME/.openclaw/workspace-${CONTENTPIPE_AGENT_ID}}"
CONTENTPIPE_AGENT_DIR="${CONTENTPIPE_AGENT_DIR:-$HOME/.openclaw/agents/${CONTENTPIPE_AGENT_ID}/agent}"
CONTENTPIPE_AGENT_MODEL="${CONTENTPIPE_AGENT_MODEL:-}"
CONTENTPIPE_SKILLS_DIR="${CONTENTPIPE_SKILLS_DIR:-$PLUGIN_DIR/skills}"
CONTENTPIPE_AGENT_SKILLS_JSON="${CONTENTPIPE_AGENT_SKILLS_JSON:-[\"contentpipe-wechat-reader\",\"contentpipe-url-reader\",\"contentpipe-web-research\",\"contentpipe-social-research\",\"contentpipe-style-reference\",\"contentpipe-wechat-draft-publisher\",\"multi-search-engine\",\"baidu-web-search\",\"agent-reach\"]}"

resolve_python() {
    if [ -n "${CONTENTPIPE_PYTHON:-}" ] && [ -x "${CONTENTPIPE_PYTHON}" ]; then
        echo "$CONTENTPIPE_PYTHON"
        return 0
    fi
    if [ -x "$PLUGIN_DIR/.venv/bin/python" ]; then
        echo "$PLUGIN_DIR/.venv/bin/python"
        return 0
    fi
    if [ -x "$PLUGIN_DIR/venv/bin/python" ]; then
        echo "$PLUGIN_DIR/venv/bin/python"
        return 0
    fi
    command -v python3
}

PYTHON_BIN="$(resolve_python)"

check_runtime_python() {
    if ! "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import importlib.util, sys
mods = ['uvicorn']
missing = [m for m in mods if importlib.util.find_spec(m) is None]
raise SystemExit(1 if missing else 0)
PY
    then
        echo "❌ 当前 Python 不可用：$PYTHON_BIN"
        echo "   缺少模块: uvicorn"
        echo "   请优先使用项目虚拟环境，例如："
        echo "   python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
        echo "   或显式指定 CONTENTPIPE_PYTHON=/path/to/venv/bin/python"
        exit 1
    fi
}

WATCHDOG_PID_FILE="/tmp/contentpipe-watchdog.pid"
RESTART_DELAY="${CONTENTPIPE_RESTART_DELAY:-3}"
MAX_RAPID_RESTARTS=5
RAPID_WINDOW=60

# 守护进程：自动重启 uvicorn，崩溃后等待 $RESTART_DELAY 秒再拉起
_watchdog_loop() {
    cd "$PLUGIN_DIR/scripts" || exit 1
    local rapid_count=0
    local window_start
    window_start=$(date +%s)

    while true; do
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🚀 启动 uvicorn (port $PORT)..." >> "$LOG_FILE"
        "$PYTHON_BIN" -m uvicorn web.app:app \
            --host "$HOST" --port "$PORT" \
            >> "$LOG_FILE" 2>&1
        EXIT_CODE=$?

        # 检测快速连续崩溃，防止无限重启风暴
        local now
        now=$(date +%s)
        if (( now - window_start < RAPID_WINDOW )); then
            rapid_count=$((rapid_count + 1))
        else
            rapid_count=1
            window_start=$now
        fi

        if (( rapid_count >= MAX_RAPID_RESTARTS )); then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] ❌ ${RAPID_WINDOW}s 内连续崩溃 ${MAX_RAPID_RESTARTS} 次，停止自动重启" >> "$LOG_FILE"
            rm -f "$WATCHDOG_PID_FILE"
            exit 1
        fi

        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⚠️  uvicorn 退出 (code=$EXIT_CODE)，${RESTART_DELAY}s 后重启..." >> "$LOG_FILE"
        sleep "$RESTART_DELAY"
    done
}

service_start() {
    cd "$PLUGIN_DIR/scripts" || exit 1

    # 检查守护进程是否已在运行
    if [ -f "$WATCHDOG_PID_FILE" ]; then
        local OLD_PID
        OLD_PID=$(cat "$WATCHDOG_PID_FILE" 2>/dev/null)
        if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
            echo "⚠️  ContentPipe 已在运行 (watchdog PID: $OLD_PID, port $PORT)"
            return 0
        fi
        rm -f "$WATCHDOG_PID_FILE"
    fi

    check_runtime_python

    echo "🚀 启动 ContentPipe (port $PORT, 自动重启已启用)..."
    echo "   Python: $PYTHON_BIN"
    nohup bash -c "$(declare -f _watchdog_loop); PLUGIN_DIR='$PLUGIN_DIR' PYTHON_BIN='$PYTHON_BIN' HOST='$HOST' PORT='$PORT' LOG_FILE='$LOG_FILE' RESTART_DELAY='$RESTART_DELAY' MAX_RAPID_RESTARTS='$MAX_RAPID_RESTARTS' RAPID_WINDOW='$RAPID_WINDOW' WATCHDOG_PID_FILE='$WATCHDOG_PID_FILE' _watchdog_loop" \
        > /dev/null 2>&1 &

    WATCHDOG_PID=$!
    echo "$WATCHDOG_PID" > "$WATCHDOG_PID_FILE"
    sleep 2

    if pgrep -f "uvicorn web.app:app.*--port $PORT" > /dev/null 2>&1; then
        echo "✅ ContentPipe 已启动 (watchdog PID: $WATCHDOG_PID, port: $PORT)"
        echo "   Web UI: http://localhost:$PORT"
        echo "   日志: $LOG_FILE"
        echo "   崩溃后自动重启（${RESTART_DELAY}s 间隔，${RAPID_WINDOW}s 内最多 ${MAX_RAPID_RESTARTS} 次）"
    else
        echo "❌ 启动失败，查看日志: $LOG_FILE"
        tail -20 "$LOG_FILE"
        rm -f "$WATCHDOG_PID_FILE"
        exit 1
    fi
}

service_stop() {
    echo "🛑 停止 ContentPipe..."
    # 先杀守护进程，防止它把 uvicorn 拉起来
    if [ -f "$WATCHDOG_PID_FILE" ]; then
        local WD_PID
        WD_PID=$(cat "$WATCHDOG_PID_FILE" 2>/dev/null)
        if [ -n "$WD_PID" ] && kill -0 "$WD_PID" 2>/dev/null; then
            kill "$WD_PID" 2>/dev/null || true
        fi
        rm -f "$WATCHDOG_PID_FILE"
    fi
    pkill -f "uvicorn web.app:app.*--port $PORT" 2>/dev/null || true
    sleep 1
    echo "✅ 已停止"
}

service_status() {
    # 守护进程状态
    local wd_status="未运行"
    if [ -f "$WATCHDOG_PID_FILE" ]; then
        local WD_PID
        WD_PID=$(cat "$WATCHDOG_PID_FILE" 2>/dev/null)
        if [ -n "$WD_PID" ] && kill -0 "$WD_PID" 2>/dev/null; then
            wd_status="运行中 (PID: $WD_PID)"
        fi
    fi

    if pgrep -f "uvicorn web.app:app.*--port $PORT" > /dev/null 2>&1; then
        PID=$(pgrep -f "uvicorn web.app:app.*--port $PORT" | head -1)
        echo "✅ ContentPipe 运行中 (PID: $PID, port: $PORT)"
        echo "   守护进程: $wd_status"
        HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$PORT/api/health" 2>/dev/null)
        echo "   健康检查: $HTTP_CODE"
    else
        echo "⭕ ContentPipe 未运行"
        echo "   守护进程: $wd_status"
    fi
}

install_agent() {
    if ! command -v openclaw >/dev/null 2>&1; then
        echo "❌ 未找到 openclaw CLI"
        exit 1
    fi

    echo "🔧 安装/修正 blank agent: $CONTENTPIPE_AGENT_ID"
    echo "   workspace: $CONTENTPIPE_AGENT_WORKSPACE"
    echo "   agentDir:  $CONTENTPIPE_AGENT_DIR"

    if CONTENTPIPE_AGENT_ID="$CONTENTPIPE_AGENT_ID" "$PYTHON_BIN" - <<'PY'
import json, os, subprocess, sys
agent_id = os.environ['CONTENTPIPE_AGENT_ID']
items=json.loads(subprocess.check_output(['openclaw','config','get','agents.list','--json']).decode())
sys.exit(0 if any(item.get('id') == agent_id for item in items) else 1)
PY
    then
        echo "ℹ️  agent 已存在，跳过 agents add"
    else
        CMD=(openclaw agents add "$CONTENTPIPE_AGENT_ID" --workspace "$CONTENTPIPE_AGENT_WORKSPACE" --agent-dir "$CONTENTPIPE_AGENT_DIR" --non-interactive)
        if [ -n "$CONTENTPIPE_AGENT_MODEL" ]; then
            CMD+=(--model "$CONTENTPIPE_AGENT_MODEL")
        fi
        "${CMD[@]}"
    fi

    AGENT_INDEX=$(CONTENTPIPE_AGENT_ID="$CONTENTPIPE_AGENT_ID" "$PYTHON_BIN" - <<'PY'
import json, os, subprocess
agent_id = os.environ['CONTENTPIPE_AGENT_ID']
items=json.loads(subprocess.check_output(['openclaw','config','get','agents.list','--json']).decode())
for i,item in enumerate(items):
    if item.get('id') == agent_id:
        print(i)
        break
else:
    raise SystemExit('contentpipe agent not found after add')
PY
)

    openclaw config set "agents.list[$AGENT_INDEX].name" '"ContentPipe Blank"' --strict-json
    openclaw config set "agents.list[$AGENT_INDEX].workspace" "\"$CONTENTPIPE_AGENT_WORKSPACE\"" --strict-json
    openclaw config set "agents.list[$AGENT_INDEX].agentDir" "\"$CONTENTPIPE_AGENT_DIR\"" --strict-json
    if [ -n "$CONTENTPIPE_AGENT_MODEL" ]; then
        openclaw config set "agents.list[$AGENT_INDEX].model" "\"$CONTENTPIPE_AGENT_MODEL\"" --strict-json
    fi
    openclaw config set "agents.list[$AGENT_INDEX].tools.allow" '[]' --strict-json
    openclaw config set "agents.list[$AGENT_INDEX].tools.deny" '[]' --strict-json
    openclaw config set "agents.list[$AGENT_INDEX].skills" "$CONTENTPIPE_AGENT_SKILLS_JSON" --strict-json

    MERGED_SKILL_DIRS=$(CONTENTPIPE_SKILLS_DIR="$CONTENTPIPE_SKILLS_DIR" "$PYTHON_BIN" - <<'PY'
import json, os, subprocess
skills_dir = os.environ['CONTENTPIPE_SKILLS_DIR']
try:
    existing = json.loads(subprocess.check_output(['openclaw', 'config', 'get', 'skills.load.extraDirs', '--json']).decode())
    if not isinstance(existing, list):
        existing = []
except Exception:
    existing = []
merged = []
for item in [*existing, skills_dir]:
    if item and item not in merged:
        merged.append(item)
print(json.dumps(merged, ensure_ascii=False))
PY
)
    openclaw config set "skills.load.extraDirs" "$MERGED_SKILL_DIRS" --strict-json

    mkdir -p "$CONTENTPIPE_AGENT_DIR"
    mkdir -p "$CONTENTPIPE_AGENT_WORKSPACE"
    if [ -f "$HOME/.openclaw/agents/main/agent/auth-profiles.json" ] && [ ! -f "$CONTENTPIPE_AGENT_DIR/auth-profiles.json" ]; then
        cp "$HOME/.openclaw/agents/main/agent/auth-profiles.json" "$CONTENTPIPE_AGENT_DIR/auth-profiles.json"
        chmod 600 "$CONTENTPIPE_AGENT_DIR/auth-profiles.json" || true
        echo "✅ 已复制 main agent 的 auth-profiles.json 到 $CONTENTPIPE_AGENT_DIR"
    else
        echo "ℹ️  未复制 auth-profiles.json（目标已存在，或 main agent 尚无 auth-profiles.json）"
    fi

    cat <<EOF
✅ blank agent 已配置完成

下一步：
1. 重启 Gateway 使 agent 配置生效：
   openclaw gateway restart
2. 如需验证：
   openclaw agents list

约定：
- agent id: $CONTENTPIPE_AGENT_ID
- workspace: $CONTENTPIPE_AGENT_WORKSPACE
- agentDir: $CONTENTPIPE_AGENT_DIR
- skill source: $CONTENTPIPE_SKILLS_DIR
- agent skills: $CONTENTPIPE_AGENT_SKILLS_JSON
EOF
}

case "${1:-start}" in
    start)
        service_start
        ;;

    stop)
        service_stop
        ;;

    restart)
        "$SCRIPT_PATH" stop
        sleep 1
        "$SCRIPT_PATH" start
        ;;

    status)
        service_status
        ;;

    logs)
        tail -f "$LOG_FILE"
        ;;

    install-agent)
        install_agent
        ;;

    *)
        echo "用法: $0 {start|stop|restart|status|logs|install-agent}"
        exit 1
        ;;
esac
