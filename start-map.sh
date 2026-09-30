#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON="$SCRIPT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    echo "错误: 未找到虚拟环境 Python: $PYTHON" >&2
    echo "请先在项目根目录运行: uv venv --python 3.13 .venv && uv pip install -e . pytest" >&2
    exit 1
fi

URL="http://127.0.0.1:8765"

# 检查服务健康状态
check_health() {
    curl -s --max-time 1 "$URL/api/health" 2>/dev/null | grep -q '"app": *"ios-location-controller"'
}

if check_health; then
    echo "Route Studio 服务已在运行: $URL"
else
    # 检查端口是否被其他进程占用
    if ss -tuln | grep -q ':8765 '; then
        echo "错误: 端口 8765 已被其他进程占用，请排查后再试。" >&2
        exit 1
    fi

    echo "正在启动 Route Studio 服务..."
    nohup "$PYTHON" -m ios_location_controller.web > "$SCRIPT_DIR/server.stdout.log" 2> "$SCRIPT_DIR/server.stderr.log" &
    SERVER_PID=$!
    echo "$SERVER_PID" > "$SCRIPT_DIR/server.pid"

    READY=0
    for _ in {1..30}; do
        sleep 0.3
        if check_health; then
            READY=1
            break
        fi
        if ! kill -0 "$SERVER_PID" 2>/dev/null; then
            break
        fi
    done

    if [[ $READY -ne 1 ]]; then
        if ! kill -0 "$SERVER_PID" 2>/dev/null; then
            rm -f "$SCRIPT_DIR/server.pid"
        fi
        echo "错误: 服务启动超时，请检查日志: $SCRIPT_DIR/server.stderr.log" >&2
        exit 1
    fi
fi

echo "=========================================="
echo "Route Studio 已就绪: $URL"
echo "=========================================="

# 尝试用系统默认浏览器打开
if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL/" >/dev/null 2>&1 &
fi
