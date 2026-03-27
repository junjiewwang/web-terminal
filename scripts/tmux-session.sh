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

# ── tmux 配置文件 ─────────────────────────────
# 注意：tmux set-option -g 需要 tmux 服务器已运行，而首次创建会话时
# 服务器尚未启动，命令会静默失败。因此必须通过配置文件设置全局选项，
# tmux 在启动服务器（new-session）时会自动加载 ~/.tmux.conf。
TMUX_CONF="$HOME/.tmux.conf"
cat > "$TMUX_CONF" << 'EOF'
# ── wetty-mcp-terminal tmux 配置 ──

# window-size largest：始终使用最大客户端的窗口尺寸
# 解决多客户端（浏览器 + Agent PTY）attach 时尺寸不同导致的点号填充问题
set-option -g window-size largest

# default-terminal xterm-256color：覆盖 tmux 默认的 tmux-256color
# tmux 默认将 $TERM 设为 tmux-256color，通过 SSH 传播到目标主机后，
# 旧版 CentOS/RHEL 的 terminfo 库可能没有该条目，导致 top/vim 等报错：
#   'tmux-256color': unknown terminal type.
# xterm-256color 是几乎所有 Linux 发行版都预装的 terminfo，兼容性最好
set-option -g default-terminal "xterm-256color"

# history-limit 5000：tmux scrollback buffer 行数（默认 2000）
set-option -g history-limit 5000

# ── 鼠标支持（智能模式）──
# 开启 mouse，让鼠标滚轮可以翻看历史输出
set-option -g mouse on

# 智能滚轮：根据当前是否在 Alternate Screen（vim/top/less）区分行为
# - Normal Screen（命令行）：滚轮 → 自动进入 copy mode 翻看历史
# - Alternate Screen（vim/top）：滚轮 → 直接传递给应用程序（vim 正常滚动）
#
# 文本选择说明：
# - copy mode 内：用 tmux 内置选择（空格开始 → 方向键 → Enter 复制）
# - 退出 copy mode 后（按 q）：正常鼠标拖选即可
# - copy mode 内也可以 Shift+鼠标 做浏览器级别选择
bind-key -T root WheelUpPane \
  if-shell -Ft= "#{alternate_on}" \
    "send-keys -M" \
    "select-pane -t=; copy-mode -e; send-keys -M"
bind-key -T root WheelDownPane \
  if-shell -Ft= "#{alternate_on}" \
    "send-keys -M" \
    "select-pane -t=; send-keys -M"
EOF

# ── tmux 会话管理 ─────────────────────────────
# 使用 '=' 前缀精确匹配 session 名，避免子串匹配。
# 例如：'=wetty-tce-server' 不会误匹配 'wetty-tce-server--m12'。
if tmux has-session -t "=${SESSION_NAME}" 2>/dev/null; then
    # ✅ 会话已存在 → attach（多客户端共享同一个 PTY）
    # 使用 exec 替换当前进程，避免额外 shell 层
    exec tmux attach-session -t "=${SESSION_NAME}"
else
    # 🆕 会话不存在 → 创建新会话 + 在其中执行 SSH
    # tmux new-session 的命令参数：会话内执行 SSH 连接
    # 当 SSH 连接断开时，tmux 会话也随之结束（避免僵尸会话）
    exec tmux new-session -s "$SESSION_NAME" "$SSH_CMD"
fi
