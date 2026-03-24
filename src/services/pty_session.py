"""PTY 交互式会话服务

通过 Python socket.io 客户端连接 WeTTY 实例，实现 Agent 对交互式终端的控制。
与浏览器共享同一个 WeTTY SSH PTY，Agent 的所有操作在浏览器端实时可见。

架构：
  Agent (MCP) → PTYSession (socket.io client) → WeTTY (Node.js) → SSH PTY → 堡垒机
  浏览器      → socket.io client (xterm.js)  → 同一个 WeTTY 实例 ↗

核心能力：
  - send_input: 向 PTY 发送任意输入（键盘字符、命令、菜单选择等）
  - wait_for: 等待终端输出中出现指定模式（类似 expect）
  - read_screen: 读取当前终端屏幕缓冲区（最近 N 行）
  - send_command: send_input + wait_for 的组合快捷方式
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import socketio

logger = logging.getLogger(__name__)

# ── ANSI 转义序列清洗 ──────────────────────────

# 匹配所有 ANSI 转义序列（CSI、OSC、颜色、光标移动等）
_ANSI_ESCAPE_RE = re.compile(
    r"""
    \x1b          # ESC
    (?:
        \[[0-?]*[ -/]*[@-~]   # CSI 序列: ESC [ ... 终止字符
        | \].*?(?:\x07|\x1b\\)  # OSC 序列: ESC ] ... (BEL 或 ST 终止)
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
    """清除 ANSI 转义序列和控制字符，保留可读文本

    保留: 普通文本、空格、制表符(\t)、换行(\n)、回车(\r)
    """
    return _ANSI_ESCAPE_RE.sub("", text)


# ── 数据模型 ──────────────────────────────────


@dataclass
class PTYSessionInfo:
    """PTY 会话信息"""

    session_id: str
    host_name: str
    connected: bool
    wetty_port: int
    created_at: str
    last_activity: str
    screen_lines: int


# ── PTY 会话实现 ──────────────────────────────


class PTYSession:
    """单个 PTY 交互式会话

    通过 socket.io 客户端连接到 WeTTY 实例，
    维护一个终端输出缓冲区，支持 expect 风格的模式匹配。
    """

    # 终端屏幕缓冲区最大行数
    MAX_SCREEN_LINES = 500

    def __init__(
        self,
        session_id: str,
        host_name: str,
        wetty_port: int,
        wetty_base_path: str,
    ) -> None:
        self.session_id = session_id
        self.host_name = host_name
        self.wetty_port = wetty_port
        self.wetty_base_path = wetty_base_path

        # socket.io 客户端
        self._sio = socketio.AsyncClient(
            reconnection=True,
            reconnection_attempts=3,
            reconnection_delay=1,
            logger=False,
            engineio_logger=False,
        )

        # 终端输出缓冲区（原始数据，含 ANSI 用于回显保真）
        self._raw_buffer: deque[str] = deque(maxlen=self.MAX_SCREEN_LINES)
        # 新增输出事件（用于 wait_for 唤醒）
        self._output_event = asyncio.Event()
        # 连接状态
        self._connected = False
        self._created_at = datetime.now()
        self._last_activity = datetime.now()

        # 注册 socket.io 事件处理
        self._sio.on("data", self._on_data)
        self._sio.on("connect", self._on_connect)
        self._sio.on("disconnect", self._on_disconnect)

    async def connect(self, timeout: float = 10.0) -> None:
        """连接到 WeTTY 实例

        Args:
            timeout: 连接超时秒数

        Raises:
            ConnectionError: 连接失败或超时
        """
        url = f"http://127.0.0.1:{self.wetty_port}"
        path = f"{self.wetty_base_path}/socket.io"

        logger.info(
            "PTY 会话连接中: %s -> %s (path=%s)",
            self.session_id[:8], url, path,
        )

        try:
            await asyncio.wait_for(
                self._sio.connect(
                    url,
                    socketio_path=path,
                    transports=["websocket", "polling"],
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            raise ConnectionError(
                f"PTY 连接超时（{timeout}s）: {self.host_name}"
            ) from None
        except Exception as e:
            raise ConnectionError(
                f"PTY 连接失败: {self.host_name} - {e}"
            ) from e

        # 等待 connect 事件确认
        for _ in range(int(timeout * 10)):
            if self._connected:
                break
            await asyncio.sleep(0.1)

        if not self._connected:
            raise ConnectionError(
                f"PTY 连接后未收到 connect 确认: {self.host_name}"
            )

        logger.info("PTY 会话已连接: %s -> %s", self.session_id[:8], self.host_name)

    async def send_input(self, text: str) -> None:
        """向终端发送输入

        Args:
            text: 要发送的文本（可包含 \\n 表示回车）

        发送后浏览器端的 xterm.js 会实时显示输入内容和命令输出。
        """
        if not self._connected:
            raise ConnectionError("PTY 会话未连接")

        await self._sio.emit("input", text)
        self._last_activity = datetime.now()
        logger.debug("PTY 输入: %s -> %r", self.session_id[:8], text[:50])

    async def wait_for(
        self,
        pattern: str,
        timeout: float = 30.0,
        consume_since: bool = True,
        _start_pos: int | None = None,
    ) -> str:
        """等待终端输出中出现指定模式

        类似 expect 工具，扫描终端输出缓冲区直到匹配成功或超时。

        Args:
            pattern: 正则表达式或普通文本（自动做 re.escape 如果非正则）
            timeout: 超时秒数
            consume_since: 是否返回从等待开始到匹配位置的所有输出
            _start_pos: 内部参数 — 从指定的缓冲区位置开始扫描（send_command 使用）

        Returns:
            匹配到的输出文本（已清除 ANSI 转义序列）

        Raises:
            TimeoutError: 超时未匹配到模式
        """
        if not self._connected:
            raise ConnectionError("PTY 会话未连接")

        # 编译匹配模式（MULTILINE 使 $ 匹配每行末尾）
        try:
            regex = re.compile(pattern, re.MULTILINE)
        except re.error:
            # 非正则，当作普通文本匹配
            regex = re.compile(re.escape(pattern), re.MULTILINE)

        start_time = asyncio.get_event_loop().time()
        collected_lines: list[str] = []
        # 记录等待开始时的缓冲区位置（或使用外部传入的起始位置）
        start_pos = _start_pos if _start_pos is not None else len(self._raw_buffer)

        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed >= timeout:
                # 超时：返回已收集的内容用于调试
                collected = strip_ansi("\n".join(collected_lines))
                raise TimeoutError(
                    f"等待模式 '{pattern}' 超时（{timeout}s）。"
                    f"最近输出:\n{collected[-500:]}"
                )

            # 扫描缓冲区中新增的内容
            current_len = len(self._raw_buffer)
            if current_len > start_pos:
                new_lines = list(self._raw_buffer)[start_pos:current_len]
                start_pos = current_len

                for line in new_lines:
                    clean_line = strip_ansi(line)
                    collected_lines.append(clean_line)

                # 检查全部已收集内容是否匹配
                full_text = "\n".join(collected_lines)
                if regex.search(full_text):
                    self._last_activity = datetime.now()
                    return full_text

            # 等待新输出或超时
            self._output_event.clear()
            remaining = timeout - elapsed
            try:
                await asyncio.wait_for(
                    self._output_event.wait(),
                    timeout=min(remaining, 0.5),
                )
            except asyncio.TimeoutError:
                continue

    async def send_command(
        self,
        command: str,
        wait_pattern: str = r"[\$#>]\s*$",
        timeout: float = 30.0,
    ) -> str:
        """发送命令并等待命令执行完成

        组合了 send_input + wait_for 的快捷方式。

        Args:
            command: 要执行的命令（自动添加回车）
            wait_pattern: 等待的命令完成标志（默认匹配 $、#、> 提示符）
            timeout: 超时秒数

        Returns:
            命令输出文本（已清除 ANSI）
        """
        # 在发送前记录缓冲区位置，确保不会跳过快速返回的输出
        pre_pos = len(self._raw_buffer)

        # 发送命令（自动加回车）
        if not command.endswith("\n") and not command.endswith("\r"):
            command += "\r"
        await self.send_input(command)

        # 短暂等待确保命令开始执行
        await asyncio.sleep(0.3)

        # 等待提示符出现（从发送前的位置开始扫描）
        output = await self.wait_for(wait_pattern, timeout=timeout, _start_pos=pre_pos)
        return output

    def read_screen(self, lines: int = 50) -> str:
        """读取终端屏幕缓冲区

        Args:
            lines: 读取最近 N 行，默认 50

        Returns:
            终端内容（已清除 ANSI 转义序列）
        """
        buf = list(self._raw_buffer)
        recent = buf[-lines:] if len(buf) > lines else buf
        return strip_ansi("\n".join(recent))

    async def close(self) -> None:
        """关闭 PTY 会话"""
        if self._sio.connected:
            await self._sio.disconnect()
        self._connected = False
        logger.info("PTY 会话已关闭: %s", self.session_id[:8])

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def info(self) -> PTYSessionInfo:
        return PTYSessionInfo(
            session_id=self.session_id,
            host_name=self.host_name,
            connected=self._connected,
            wetty_port=self.wetty_port,
            created_at=self._created_at.isoformat(),
            last_activity=self._last_activity.isoformat(),
            screen_lines=len(self._raw_buffer),
        )

    # ── socket.io 事件处理 ──────────────────────

    async def _on_data(self, data: str) -> None:
        """处理终端输出数据"""
        # 按行分割存入缓冲区
        lines = data.split("\n")
        for line in lines:
            if line:  # 跳过空行
                self._raw_buffer.append(line)

        # 通知 wait_for 有新数据
        self._output_event.set()

    async def _on_connect(self) -> None:
        """socket.io 连接成功"""
        self._connected = True
        logger.debug("PTY socket.io 已连接: %s", self.session_id[:8])

    async def _on_disconnect(self) -> None:
        """socket.io 断开"""
        self._connected = False
        logger.warning("PTY socket.io 已断开: %s", self.session_id[:8])
        # 唤醒所有等待者
        self._output_event.set()


# ── PTY 会话管理器 ──────────────────────────────


class PTYSessionManager:
    """PTY 会话管理器

    管理所有 PTY 交互式会话的生命周期。
    与 WeTTYManager 协作：WeTTYManager 管理进程，PTYSessionManager 管理 socket.io 连接。
    """

    def __init__(self) -> None:
        self._sessions: dict[str, PTYSession] = {}
        self._lock = asyncio.Lock()

    async def create_session(
        self,
        host_name: str,
        wetty_port: int,
        wetty_base_path: str,
        connect_timeout: float = 15.0,
    ) -> PTYSession:
        """创建并连接 PTY 会话

        Args:
            host_name: 主机名
            wetty_port: WeTTY 实例端口
            wetty_base_path: WeTTY base path（如 /wetty/t/tce-server）
            connect_timeout: 连接超时

        Returns:
            已连接的 PTYSession

        Raises:
            ConnectionError: 连接失败
        """
        session_id = str(uuid.uuid4())
        session = PTYSession(
            session_id=session_id,
            host_name=host_name,
            wetty_port=wetty_port,
            wetty_base_path=wetty_base_path,
        )

        await session.connect(timeout=connect_timeout)

        async with self._lock:
            self._sessions[session_id] = session

        logger.info(
            "PTY 会话已创建: %s -> %s (port %d)",
            session_id[:8], host_name, wetty_port,
        )
        return session

    def get_session(self, session_id: str) -> Optional[PTYSession]:
        """获取 PTY 会话"""
        return self._sessions.get(session_id)

    async def close_session(self, session_id: str) -> bool:
        """关闭指定 PTY 会话"""
        async with self._lock:
            session = self._sessions.pop(session_id, None)

        if not session:
            return False

        await session.close()
        return True

    async def close_all(self) -> None:
        """关闭所有 PTY 会话"""
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()

        for session in sessions:
            await session.close()

        logger.info("所有 PTY 会话已关闭，共 %d 个", len(sessions))

    def list_sessions(self) -> list[PTYSessionInfo]:
        """列出所有 PTY 会话"""
        return [s.info for s in self._sessions.values()]
