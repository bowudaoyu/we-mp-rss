#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 停止服务
bash "$SCRIPT_DIR/stop.sh" || true

sleep 1

# 后台启动服务
cd "$SCRIPT_DIR"
nohup python3 main.py -job True -init True > logs/app.log 2>&1 &

echo "服务已在后台启动，PID: $!"
echo "日志: logs/app.log"
