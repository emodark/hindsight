#!/bin/bash
# ==========================================================
# Hindsight 启动脚本
# 用法: bash start-hindsight.sh [--foreground]
#
# 默认: 后台运行，输出到 ~/.hindsight/hindsight.log
# --foreground: 前台运行，Ctrl+C 停止
# ==========================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HAPI_BIN="/home/john/.local/bin/hindsight-api"
ENV_FILE="${SCRIPT_DIR}/.env"
LOG_FILE="${HOME}/.hindsight/hindsight.log"
PID_FILE="${HOME}/.hindsight/hindsight.pid"

# 加载环境变量
if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

# 确保日志目录存在
mkdir -p "$(dirname "$LOG_FILE")"

# 清理旧进程
cleanup() {
    if [ -f "$PID_FILE" ]; then
        OLD_PID=$(cat "$PID_FILE")
        if kill -0 "$OLD_PID" 2>/dev/null; then
            echo "⏹  停止旧进程 (PID=$OLD_PID)..."
            kill "$OLD_PID" 2>/dev/null || true
            sleep 2
            kill -0 "$OLD_PID" 2>/dev/null && kill -9 "$OLD_PID" 2>/dev/null || true
        fi
        rm -f "$PID_FILE"
    fi
    # 也清孤立进程
    pkill -f "hindsight-api.*9177" 2>/dev/null || true
    sleep 1
}

start_foreground() {
    echo "🚀 前台启动 hindsight-api (127.0.0.1:9177)..."
    echo "   日志: $LOG_FILE"
    exec "$HAPI_BIN" \
        --host 127.0.0.1 \
        --port 9177 \
        --log-level info \
        --idle-timeout 0 \
        2>&1 | tee -a "$LOG_FILE"
}

start_background() {
    echo "🚀 后台启动 hindsight-api..."
    echo "   PID 文件: $PID_FILE"
    echo "   日志文件: $LOG_FILE"

    "$HAPI_BIN" \
        --host 127.0.0.1 \
        --port 9177 \
        --log-level info \
        --idle-timeout 0 \
        >> "$LOG_FILE" 2>&1 &
    PID=$!
    echo $PID > "$PID_FILE"
    echo "   PID: $PID"

    # 等待健康检查（最多60秒）
    echo -n "   等待启动"
    for i in $(seq 1 60); do
        if curl -s http://127.0.0.1:9177/health > /dev/null 2>&1; then
            echo ""
            echo "✅ 启动成功！"
            echo "   API: http://127.0.0.1:9177"
            echo "   健康: curl -s http://127.0.0.1:9177/health"
            echo "   日志: tail -f $LOG_FILE"
            exit 0
        fi
        echo -n "."
        sleep 1
    done

    echo ""
    echo "❌ 启动超时（60秒），检查日志:"
    tail -20 "$LOG_FILE"
    exit 1
}

# ── 主流程 ──
case "${1:-}" in
    --foreground)
        cleanup
        start_foreground
        ;;
    --help|-h)
        echo "用法: bash start-hindsight.sh [--foreground]"
        echo ""
        echo "  默认:    后台运行，等待健康检查"
        echo "  --foreground: 前台运行 (Ctrl+C 停止)"
        exit 0
        ;;
    *)
        cleanup
        start_background
        ;;
esac
