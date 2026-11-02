"""PTY 输出保护：注入/传输期间临时关闭输出后处理（stty -opost）。

问题背景（Bugfix #24）：
  浏览器窗口过窄时，PTY 列宽（cols）被同步成很小的值。TTY 驱动的输出
  后处理（opost/onlcr）会按列宽在输出流中真实插入 \\r\\n，把
  __SNIPPET_INJECTED__ / __PROBE_YES__ / __FT_ACK__ 等协议标记折行切碎，
  导致 wait_for 匹配失败 —— 表现为「注入超时」「探测不可用」等假阴性
  （功能实际成功，但被误判失败，用户看到 503）。

解决方案：
  在 snippet 注入 / 文件传输期间临时 ``stty -opost``（关闭输出后处理，
  输出流原样透传，绝不插入折行），结束后 ``stty opost`` 恢复。
  期间 WebSocket 已静默（set_ws_muted），用户在浏览器端无感知。

  通过会话级引用计数支持安全嵌套（如分片上传递归调用 upload）：
  仅最外层真正执行 stty，最外层退出时才恢复。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.services.terminal_manager import TerminalSession

logger = logging.getLogger(__name__)

# 挂在 session 上的引用计数属性名
_DEPTH_ATTR = "_raw_output_guard_depth"


async def _stty(session: "TerminalSession", args: str) -> None:
    """执行 stty 命令，任何异常均静默（保护失败仅回退到原有行为）。"""
    try:
        await session.send_command(command=f"stty {args}", timeout=8.0)
    except Exception as e:  # noqa: BLE001
        # wait 超时时命令大概率已被 shell 执行（stty 已生效），静默忽略
        logger.debug("stty %s 执行/等待异常（忽略）: %s", args, e)


async def acquire_raw_output(session: "TerminalSession") -> None:
    """进入输出保护：计数 +1，最外层（0→1）时执行 stty -opost。"""
    depth = getattr(session, _DEPTH_ATTR, 0)
    if depth == 0:
        await _stty(session, "-opost")
        logger.debug("已关闭 PTY 输出后处理（-opost），防标记折行")
    setattr(session, _DEPTH_ATTR, depth + 1)


async def release_raw_output(session: "TerminalSession") -> None:
    """退出输出保护：计数 -1，最外层（1→0）时恢复 stty opost。"""
    depth = max(0, getattr(session, _DEPTH_ATTR, 1) - 1)
    setattr(session, _DEPTH_ATTR, depth)
    if depth == 0:
        await _stty(session, "opost")
        logger.debug("已恢复 PTY 输出后处理（opost）")


class raw_output_terminal:
    """异步上下文管理器：进入时 acquire，退出时 release（支持嵌套）。"""

    def __init__(self, session: "TerminalSession") -> None:
        self._session = session

    async def __aenter__(self) -> "raw_output_terminal":
        await acquire_raw_output(self._session)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        await release_raw_output(self._session)
        return False  # 不吞业务异常
