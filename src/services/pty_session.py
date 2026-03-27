"""PTY 工具函数

保留 strip_ansi（被 terminal_manager.py、jump_orchestrator.py 引用）。
旧的 PTYSession/PTYSessionManager（socket.io client）已被 TerminalSession 替代。
"""

from __future__ import annotations

import re

# ── ANSI 转义序列清洗 ──────────────────────────

_ANSI_ESCAPE_RE = re.compile(
    r"""
    \x1b          # ESC
    (?:
        \[[0-?]*[ -/]*[@-~]   # CSI 序列
        | \].*?(?:\x07|\x1b\\)  # OSC 序列
        | [()][AB012]           # 字符集选择
        | [>=]                  # 键盘模式
        | [\x20-\x2f][\x30-\x7e]  # 两字符序列
        | [78DEHM]              # 单字符序列
    )
    | [\x00-\x08\x0e-\x1a\x7f]   # 控制字符（保留 \t \n \r）
    """,
    re.VERBOSE,
)


def strip_ansi(text: str) -> str:
    """清除 ANSI 转义序列和控制字符，保留可读文本"""
    return _ANSI_ESCAPE_RE.sub("", text)


# ── tmux 状态栏过滤 ──────────────────────────

# tmux 状态栏行特征：以 [ 开头，包含 session_name:window_name，末尾是时间戳
# 示例: [wetty-tce0:sshpass*   "root@host:" 09:15 27-Mar-26
_TMUX_STATUS_RE = re.compile(
    r"^\[[\w-]+:.*\d{2}:\d{2}\s+\d{2}-\w{3}-\d{2}\s*$"
)


def is_tmux_status_line(line: str) -> bool:
    """判断一行文本是否为 tmux 状态栏输出"""
    return bool(_TMUX_STATUS_RE.search(line.strip()))


def strip_tmux_status(text: str) -> str:
    """从多行文本中过滤掉 tmux 状态栏行

    tmux window-size=largest 模式下，状态栏每分钟刷新一次，
    刷新内容会被 PTY 捕获并混入 Agent 缓冲区，干扰 shell 提示符匹配。
    """
    lines = text.split("\n")
    filtered = [line for line in lines if not is_tmux_status_line(strip_ansi(line))]
    return "\n".join(filtered)
