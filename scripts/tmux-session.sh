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
bind-key -T root WheelUpPane \
  if-shell -Ft= "#{alternate_on}" \
    "send-keys -M" \
    "select-pane -t=; copy-mode -e; send-keys -M"
bind-key -T root WheelDownPane \
  if-shell -Ft= "#{alternate_on}" \
    "send-keys -M" \
    "select-pane -t=; send-keys -M"

# 鼠标拖选：始终进入 copy-mode
# - Normal Screen（命令行）：拖选 → 进入 copy-mode
# - Alternate Screen（vim/top）：拖选 → 也进入 copy-mode（统一行为）
#
# 注意：如果你希望在 vim 中使用 vim 自己的鼠标功能，
# 请在 vim 中设置 :set mouse= ，然后使用 Shift+鼠标拖选进入 tmux copy-mode
bind-key -T root MouseDown1Pane select-pane
bind-key -T root MouseDrag1Pane select-pane \; copy-mode \; send-keys -X begin-selection

# ── 文本选择行为优化 ──
# 鼠标拖选文本的行为：
# 1. 鼠标拖选 → 进入 copy-mode 并开始选择
# 2. 松开鼠标后，保持选择状态（不自动复制）
# 3. 用户按 Ctrl+C 确认复制，或按 ESC/q 取消
#
# 关键修改：MouseDragEnd1Pane 不绑定任何操作
# 这样拖选后选择区域保持高亮，用户可以决定下一步操作
unbind-key -T copy-mode MouseDragEnd1Pane

# 禁用双击/三击选择（太容易误触）
# 默认：双击选择单词，三击选择整行
unbind-key -T root DoubleClick1Pane
unbind-key -T root TripleClick1Pane
unbind-key -T copy-mode DoubleClick1Pane
unbind-key -T copy-mode TripleClick1Pane

# ── tmux copy-mode → 浏览器剪贴板联动 ──
# 方案：tmux copy-mode 里的选区通过 OSC 52 转义序列推送到 xterm.js，
# xterm.js 监听后写入浏览器剪贴板。
#
# 关键配置：
# 1. set-clipboard on: tmux 自动把选区内容通过 OSC 52 发送给终端
# 2. 重绑定 Ctrl+C: 默认是 cancel，改为 copy-selection-and-cancel
#
# 用户操作流程：
#   鼠标拖选 → 进入 copy-mode → 按 Enter 或 Ctrl+C → 选区内容 → 浏览器剪贴板
#
# 注意：xterm.js 需要配置 allowProposedApi: true 并监听 onClipboardPaste
# 事件来接收 OSC 52 内容。但由于浏览器安全限制，xterm.js 无法直接写入剪贴板，
# 需要通过我们自定义的消息通道（WebSocket）推送。

# 启用 tmux 的 OSC 52 剪贴板支持（选区自动发送给终端）
set-option -g set-clipboard on

# ── copy-mode 键绑定重定义 ──
# 用户友好的复制交互：
# 1. 鼠标拖选 → 进入 copy-mode 并开始选择
# 2. 松开鼠标 → 保持选择状态（不自动复制）
# 3. 按 Ctrl+C → 复制 + 清除选择 + **保持在 copy-mode**
# 4. 按 ESC → 只清除选择，不退出 copy-mode
# 5. 按 q 或 Enter → 退出 copy-mode（不复制）
#
# ESC 键：只清除选择，不退出 copy-mode
# 使用 clear-selection 命令清除选择高亮
bind-key -T copy-mode Escape send-keys -X clear-selection

# Ctrl+C：复制但不退出 copy-mode
# 使用 copy-selection-no-clear 复制（不退出），然后 clear-selection 清除选择
bind-key -T copy-mode C-c send-keys -X copy-selection-no-clear \; send-keys -X clear-selection

# Enter：退出但不复制
bind-key -T copy-mode Enter send-keys -X cancel

# q：取消并退出
bind-key -T copy-mode q send-keys -X cancel

# Enter 绑定为 cancel（退出但不复制）
bind-key -T copy-mode Enter send-keys -X cancel

# q 保持默认的 cancel（取消并退出）

# 注意：Mac 上的 Cmd+C 无法直接绑定到 tmux（会被浏览器拦截）
# 但 tmux 的 C-c 在 Mac 上也能工作（Ctrl+C）

# after-copy-mode hook：copy-mode 退出后推送 buffer 到前端
# 注意：只有 copy-selection-and-cancel 会往 buffer 写内容，cancel 不会
# 使用 \$(...) 防止 tmux 在加载配置时展开变量，保留到运行时才展开
set-hook -g after-copy-mode \
  "run-shell 'TMUX_BUF=\$(tmux save-buffer - 2>/dev/null); if [ -n \"\$TMUX_BUF\" ]; then echo \"\$TMUX_BUF\" > /tmp/tmux-copy-#{session_name}; curl -sf -X POST -H \"Content-Type: application/json\" -d \"{\\\"session_name\\\":\\\"#{session_name}\\\"}\" http://127.0.0.1:8001/api/tmux/copy-buffer > /dev/null 2>&1 & fi; rm -f /tmp/tmux-copy-#{session_name}'"
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
