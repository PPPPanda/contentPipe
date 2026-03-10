#!/bin/bash
# ContentPipe 服务启动脚本
# 用法: ./start.sh [start|stop|restart|status|logs]

PLUGIN_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="contentpipe"
PORT="${CONTENTPIPE_PORT:-8765}"
HOST="${CONTENTPIPE_HOST:-0.0.0.0}"
LOG_FILE="/tmp/contentpipe.log"

cd "$PLUGIN_DIR/scripts" || exit 1

case "${1:-start}" in
    start)
        # 检查是否已运行
        if pgrep -f "uvicorn web.app:app.*--port $PORT" > /dev/null 2>&1; then
            echo "⚠️  ContentPipe 已在运行 (port $PORT)"
            exit 0
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
        ;;
    
    stop)
        echo "🛑 停止 ContentPipe..."
        pkill -f "uvicorn web.app:app.*--port $PORT" 2>/dev/null
        sleep 1
        echo "✅ 已停止"
        ;;
    
    restart)
        "$0" stop
        sleep 1
        "$0" start
        ;;
    
    status)
        if pgrep -f "uvicorn web.app:app.*--port $PORT" > /dev/null 2>&1; then
            PID=$(pgrep -f "uvicorn web.app:app.*--port $PORT" | head -1)
            echo "✅ ContentPipe 运行中 (PID: $PID, port: $PORT)"
            # 健康检查
            HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$PORT/api/health" 2>/dev/null)
            echo "   健康检查: $HTTP_CODE"
        else
            echo "⭕ ContentPipe 未运行"
        fi
        ;;
    
    logs)
        tail -f "$LOG_FILE"
        ;;
    
    *)
        echo "用法: $0 {start|stop|restart|status|logs}"
        exit 1
        ;;
esac
