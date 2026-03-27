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
