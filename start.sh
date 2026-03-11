#!/bin/bash
# ContentPipe 服务启动脚本
# 用法: ./start.sh [start|stop|restart|status|logs|install-agent]

set -euo pipefail

SCRIPT_PATH="$(readlink -f "$0")"
PLUGIN_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
APP_NAME="contentpipe"
PORT="${CONTENTPIPE_PORT:-8765}"
HOST="${CONTENTPIPE_HOST:-0.0.0.0}"
LOG_FILE="/tmp/contentpipe.log"
CONTENTPIPE_AGENT_ID="${CONTENTPIPE_AGENT_ID:-contentpipe-blank}"
CONTENTPIPE_AGENT_WORKSPACE="${CONTENTPIPE_AGENT_WORKSPACE:-$HOME/.openclaw/workspace-${CONTENTPIPE_AGENT_ID}}"
CONTENTPIPE_AGENT_DIR="${CONTENTPIPE_AGENT_DIR:-$HOME/.openclaw/agents/${CONTENTPIPE_AGENT_ID}/agent}"
CONTENTPIPE_AGENT_MODEL="${CONTENTPIPE_AGENT_MODEL:-}"

service_start() {
    cd "$PLUGIN_DIR/scripts" || exit 1
    if pgrep -f "uvicorn web.app:app.*--port $PORT" > /dev/null 2>&1; then
        echo "⚠️  ContentPipe 已在运行 (port $PORT)"
        return 0
    fi

    echo "🚀 启动 ContentPipe (port $PORT)..."
    nohup python3 -m uvicorn web.app:app \
        --host "$HOST" --port "$PORT" \
        > "$LOG_FILE" 2>&1 &

    PID=$!
    sleep 2

    if kill -0 "$PID" 2>/dev/null; then
        echo "✅ ContentPipe 已启动 (PID: $PID, port: $PORT)"
        echo "   Web UI: http://localhost:$PORT"
        echo "   日志: $LOG_FILE"
    else
        echo "❌ 启动失败，查看日志: $LOG_FILE"
        tail -20 "$LOG_FILE"
        exit 1
    fi
}

service_stop() {
    echo "🛑 停止 ContentPipe..."
    pkill -f "uvicorn web.app:app.*--port $PORT" 2>/dev/null || true
    sleep 1
    echo "✅ 已停止"
}

service_status() {
    if pgrep -f "uvicorn web.app:app.*--port $PORT" > /dev/null 2>&1; then
        PID=$(pgrep -f "uvicorn web.app:app.*--port $PORT" | head -1)
        echo "✅ ContentPipe 运行中 (PID: $PID, port: $PORT)"
        HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$PORT/api/health" 2>/dev/null)
        echo "   健康检查: $HTTP_CODE"
    else
        echo "⭕ ContentPipe 未运行"
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

    if CONTENTPIPE_AGENT_ID="$CONTENTPIPE_AGENT_ID" python3 - <<'PY'
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

    AGENT_INDEX=$(CONTENTPIPE_AGENT_ID="$CONTENTPIPE_AGENT_ID" python3 - <<'PY'
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

    mkdir -p "$CONTENTPIPE_AGENT_DIR"
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
