#!/bin/bash
# ══════════════════════════════════════════════════════════════════
# tmux-session.sh — WeTTY 调用的 tmux 会话入口脚本
#
# 功能：
#   - 首次连接（浏览器/Agent 谁先到都行）→ tmux new-session + SSH
#   - 后续连接 → tmux attach-session（共享同一个终端）
#
# 调用方式（由 wetty_manager.py 构造）：
#   tmux-session.sh <session_name> <ssh_host> <ssh_port> <ssh_user> [password] [key_path]
#
# tmux 会话命名规则：wetty-{host_name}
# ══════════════════════════════════════════════════════════════════

set -euo pipefail

# ── 参数解析 ──────────────────────────────────
SESSION_NAME="${1:?Usage: tmux-session.sh <session_name> <ssh_host> <ssh_port> <ssh_user> [password] [key_path]}"
SSH_HOST="${2:?Missing ssh_host}"
SSH_PORT="${3:?Missing ssh_port}"
SSH_USER="${4:?Missing ssh_user}"
SSH_PASS="${5:-}"
SSH_KEY="${6:-}"

# ── 构建 SSH 命令 ─────────────────────────────
# 公共 SSH 选项：禁用 host key 检查（容器内无 known_hosts）
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"

build_ssh_command() {
    if [ -n "$SSH_KEY" ]; then
        # 密钥认证
        echo "ssh ${SSH_OPTS} -i ${SSH_KEY} -p ${SSH_PORT} ${SSH_USER}@${SSH_HOST}"
    elif [ -n "$SSH_PASS" ]; then
        # 密码认证（通过 sshpass）
        echo "sshpass -p '${SSH_PASS}' ssh ${SSH_OPTS} -p ${SSH_PORT} ${SSH_USER}@${SSH_HOST}"
    else
        # 无认证信息，交互式（WeTTY 终端会提示输入密码）
        echo "ssh ${SSH_OPTS} -p ${SSH_PORT} ${SSH_USER}@${SSH_HOST}"
    fi
}

SSH_CMD=$(build_ssh_command)

# ── tmux 全局配置 ─────────────────────────────
# window-size largest：tmux 始终使用最大客户端的窗口尺寸
# 解决多客户端（浏览器 + Agent PTY）attach 时尺寸不同导致的点号填充问题
# Agent PTY 尺寸通常较小（80x30），浏览器终端较大（~76x72），
# 使用 largest 确保浏览器（主界面）的显示不受 Agent 影响
tmux set-option -g window-size largest 2>/dev/null || true

# ── tmux 会话管理 ─────────────────────────────
if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    # ✅ 会话已存在 → attach（多客户端共享同一个 PTY）
    # 使用 exec 替换当前进程，避免额外 shell 层
    exec tmux attach-session -t "$SESSION_NAME"
else
    # 🆕 会话不存在 → 创建新会话 + 在其中执行 SSH
    # tmux new-session 的命令参数：会话内执行 SSH 连接
    # 当 SSH 连接断开时，tmux 会话也随之结束（避免僵尸会话）
    exec tmux new-session -s "$SESSION_NAME" "$SSH_CMD"
fi
