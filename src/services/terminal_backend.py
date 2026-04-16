"""终端会话后端抽象。

负责解析和管理终端会话承载方式：
- `tmux`：沿用现有 `tmux-session.sh` 方案
- `broker`：自研 PTY Broker 路径，直接在 PTY 中执行 SSH 命令
"""

from __future__ import annotations

import enum
import os
from typing import Final


class TerminalBackend(str, enum.Enum):
    TMUX = "tmux"
    BROKER = "broker"


DEFAULT_TERMINAL_BACKEND: Final[TerminalBackend] = TerminalBackend.BROKER
TERMINAL_BACKEND_ENV_VAR: Final[str] = "WETTY_SESSION_BACKEND"


def resolve_terminal_backend(
    value: str | TerminalBackend | None,
    *,
    fallback: TerminalBackend = DEFAULT_TERMINAL_BACKEND,
) -> TerminalBackend:
    """解析终端后端开关。"""
    if value is None:
        return fallback
    if isinstance(value, TerminalBackend):
        return value

    normalized = value.strip().lower()
    if not normalized:
        return fallback

    try:
        return TerminalBackend(normalized)
    except ValueError as e:
        raise ValueError(f"不支持的终端 backend: {value}，可选值: tmux | broker") from e


def read_default_terminal_backend() -> TerminalBackend:
    """从环境变量读取默认终端后端。"""
    raw = os.getenv(TERMINAL_BACKEND_ENV_VAR)
    return resolve_terminal_backend(raw, fallback=DEFAULT_TERMINAL_BACKEND)
