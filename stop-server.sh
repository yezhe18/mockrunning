#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/server.pid"
if [[ ! -f "$PID_FILE" ]]; then
    echo "未找到此项目启动的服务记录。"
    exit 0
fi
read -r SERVER_PID < "$PID_FILE"
if [[ ! "$SERVER_PID" =~ ^[0-9]+$ ]]; then
    echo "错误: 无效的服务 PID 记录。" >&2
    exit 1
fi
if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "服务已停止。"
    exit 0
fi

# Verify ownership before signaling; a port number is not a process identity.
if [[ "$(readlink "/proc/$SERVER_PID/cwd")" != "$SCRIPT_DIR" ]] ||
   ! "$SCRIPT_DIR/.venv/bin/python" - "$SERVER_PID" "$SCRIPT_DIR/.venv/bin/python" <<'PY'
import sys
from pathlib import Path
args = Path('/proc', sys.argv[1], 'cmdline').read_bytes().split(b'\0')
expected = [sys.argv[2].encode(), b'-m', b'ios_location_controller.web']
raise SystemExit(0 if args[:3] == expected else 1)
PY
then
    echo "错误: PID 不属于此项目的 Route Studio 服务，请检查 server.pid。" >&2
    exit 1
fi
kill -TERM "$SERVER_PID"
for _ in {1..60}; do
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        rm -f "$PID_FILE"
        echo "服务已停止并完成退出清理。"
        exit 0
    fi
    sleep 0.3
done
echo "错误: 服务仍在退出清理中，请检查日志后再试。" >&2
exit 1
