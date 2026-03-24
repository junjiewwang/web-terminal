#!/bin/sh
# ══════════════════════════════════════════════
# 容器入口脚本
#
# 启动 nginx（前端反代 :8000）和 uvicorn（后端 :8001）
# nginx 解决浏览器 HTTP/1.1 SSE 长连接阻塞 POST 请求的问题
#
# tmux 依赖：WeTTY 通过 --command 参数调用 tmux-session.sh，
# tmux 负责会话复用（浏览器 + MCP Agent 共享 SSH PTY）
# ══════════════════════════════════════════════

set -e

# ── tmux 环境检查 ─────────────────────────────
# 确保 tmux 已安装（apt-get install tmux）
if ! command -v tmux >/dev/null 2>&1; then
    echo "ERROR: tmux not found. Please install tmux." >&2
    exit 1
fi
echo "tmux ready: $(tmux -V)"

# 确保 TERM 环境变量已设置（tmux 和 SSH 依赖它）
export TERM="${TERM:-xterm-256color}"

echo "Starting nginx (port 8000)..."
nginx

echo "Starting uvicorn (port 8001)..."
exec uvicorn src.main:app --host 127.0.0.1 --port 8001
