#!/bin/sh
# ══════════════════════════════════════════════
# 容器入口脚本
#
# 启动 nginx（前端反代 :8000）和 uvicorn（后端 :8001）
# nginx 解决浏览器 HTTP/1.1 SSE 长连接阻塞 POST 请求的问题
# ══════════════════════════════════════════════

set -e

echo "Starting nginx (port 8000)..."
nginx

echo "Starting uvicorn (port 8001)..."
exec uvicorn src.main:app --host 127.0.0.1 --port 8001
