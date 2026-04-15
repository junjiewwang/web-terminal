"""终端会话管理服务（自研 Python PTY 层）

替代 WeTTY(Node.js) 中间层，使用 Python 原生 PTY + asyncio 实现：
- 每个终端会话 = 一个 PTY 子进程
  - TMUX 模式：exec tmux-session.sh → tmux → SSH
  - Broker 模式：exec bash -lc "ssh ..." → 原生 PTY 直连 SSH（无 tmux 依赖）
- 浏览器通过 FastAPI WebSocket 直连 PTY
- Agent 通过进程内共享缓冲区直接读写 PTY
- 多客户端（浏览器 + Agent）共享同一个 PTY fd，输出广播给所有订阅者

架构（TMUX 模式）：
  浏览器 xterm.js → WebSocket → TerminalSession → PTY fd → tmux → SSH → 远端主机

架构（Broker 模式）：
  浏览器 xterm.js → WebSocket → TerminalSession → PTY fd → bash → SSH → 远端主机
  （平台自控 resize / scrollback / 渲染 / 录制，不依赖 tmux）
"""

from __future__ import annotations

import asyncio
import enum
import fcntl
import logging
import os
import pty
import re
import signal
import struct
import termios
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from fastapi import WebSocket

from src.models.host import Host
from src.services.terminal_backend import (
    TerminalBackend,
    read_default_terminal_backend,
    resolve_terminal_backend,
)
from src.utils.ssh_command import build_ssh_command_for_host

# 延迟导入 VirtualTerminal（仅 Broker 模式需要 pyte 依赖）
_VirtualTerminal = None


def _get_virtual_terminal_class():
    """懒加载 VirtualTerminal 类，避免 pyte 未安装时影响 TMUX 模式。"""
    global _VirtualTerminal
    if _VirtualTerminal is None:
        from src.services.virtual_terminal import VirtualTerminal
        _VirtualTerminal = VirtualTerminal
    return _VirtualTerminal

logger = logging.getLogger(__name__)

# tmux 会话脚本路径
_TMUX_SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "tmux-session.sh"

# tmux 会话名前缀
_TMUX_SESSION_PREFIX = "wetty"

# Scrollback 缓冲区默认容量（字节）
_DEFAULT_SCROLLBACK_CAPACITY = 256 * 1024  # 256 KB

# 终端查询序列正则（scrollback 回放前过滤，防止 xterm.js 响应这些查询导致乱码）
# 覆盖：DA1 (\x1b[c / \x1b[0c)、DA2 (\x1b[>c / \x1b[>0c)、DA3 (\x1b[=c / \x1b[=0c)、
#       DSR/CPR (\x1b[6n)、DECRQM (\x1b[?Nn$p)、XTVERSION (\x1b[>0q)
_TERMINAL_QUERY_RE = re.compile(
    r"\x1b\["           # CSI 前缀
    r"(?:"
    r"[0-9]*c"          # DA1: \x1b[c / \x1b[0c
    r"|>[0-9]*c"        # DA2: \x1b[>c / \x1b[>0c
    r"|=[0-9]*c"        # DA3: \x1b[=c / \x1b[=0c
    r"|6n"              # DSR/CPR: \x1b[6n
    r"|\?[0-9]+\$p"    # DECRQM: \x1b[?Nn$p
    r"|>[0-9]*q"        # XTVERSION: \x1b[>0q
    r")"
)

# 鼠标事件序列正则（Broker 模式下，远端未启用鼠标追踪时过滤前端发来的鼠标事件）
# SGR 鼠标（Mode 1006）：\x1b[<数字;数字;数字M 或 \x1b[<数字;数字;数字m
# 普通鼠标（Mode 1000）：\x1b[M + 3 字节
_MOUSE_EVENT_RE = re.compile(
    r"\x1b\["
    r"(?:"
    r"<\d+;\d+;\d+[Mm]"   # SGR 鼠标: \x1b[<btn;col;row{M|m}
    r"|M..."               # 普通鼠标: \x1b[M + 3 bytes (btn, col, row)
    r")"
)


class SessionExitReason(str, enum.Enum):
    """会话退出原因枚举。

    用于在会话结束时提供明确的退出原因分类，
    支持日志、诊断和前端状态展示。
    """

    NORMAL = "normal"               # 正常退出（exit/logout）
    SSH_FAILED = "ssh_failed"       # SSH 连接失败
    PTY_CLOSED = "pty_closed"       # PTY fd 被关闭（EOF/error）
    CHILD_CRASHED = "child_crashed" # 子进程异常退出（非零 exit code）
    STOPPED = "stopped"             # 被主动 stop() 调用停止
    UNKNOWN = "unknown"             # 无法归类的退出原因


# 会话退出回调类型：(session_id, exit_reason, exit_code)
OnExitCallback = Callable[[str, SessionExitReason, int | None], None]


@dataclass
class ClientInfo:
    """WebSocket 客户端元数据。

    记录每个 WebSocket 客户端的连接信息和终端尺寸，
    用于 Broker 模式下的 min-size resize 策略。
    """

    ws: WebSocket
    client_id: str
    cols: int = 80
    rows: int = 24
    connected_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.connected_at is None:
            self.connected_at = datetime.now()


@dataclass
class TerminalInfo:
    """终端会话信息（供 API 返回）"""

    session_id: str
    instance_name: str
    backend: str
    pid: int | None
    running: bool
    created_at: str
    ws_clients: int
    exit_reason: str | None = None
    exit_code: int | None = None


class TerminalSession:
    """单个终端会话

    管理一个 PTY 子进程，提供：
    - asyncio 异步 fd 读写
    - 多 WebSocket 客户端广播（浏览器实时回显）
    - Agent 共享缓冲区（send_input / wait_for / read_screen）
    - 终端 resize
    - Scrollback 缓冲区（新客户端连接时回放历史）
    - 统一的会话退出处理与回调

    后端模式：
    - TMUX：PTY → tmux-session.sh → tmux → SSH
    - Broker：PTY → bash -lc "ssh ..." → 原生 PTY 直连 SSH
    """

    MAX_BUFFER_LINES = 500

    def __init__(
        self,
        session_id: str,
        instance_name: str,
        backend: TerminalBackend = TerminalBackend.TMUX,
        scrollback_capacity: int = _DEFAULT_SCROLLBACK_CAPACITY,
    ) -> None:
        self.session_id = session_id
        self.instance_name = instance_name
        self.backend = backend
        self.tmux_session_name = f"{_TMUX_SESSION_PREFIX}-{instance_name}"

        # PTY 进程
        self._pid: int | None = None
        self._fd: int | None = None
        self._running = False
        self._created_at = datetime.now()

        # 会话退出状态
        self._exit_reason: SessionExitReason | None = None
        self._exit_code: int | None = None

        # 退出回调列表
        self._on_exit_callbacks: list[OnExitCallback] = []

        # 多客户端管理：client_id → ClientInfo
        self._ws_clients: dict[str, ClientInfo] = {}

        # 虚拟终端（仅 Broker 模式启用，TMUX 模式为 None）
        self._vterm = None
        if self.backend == TerminalBackend.BROKER:
            try:
                VTClass = _get_virtual_terminal_class()
                self._vterm = VTClass(cols=80, rows=24)
                logger.debug("VirtualTerminal 已初始化: session=%s", self.session_id[:8])
            except ImportError:
                logger.warning(
                    "pyte 未安装，Broker 模式将使用直通渲染（无差分优化和全屏快照）: session=%s",
                    self.session_id[:8],
                )

        # Scrollback 缓冲区（字节级，保存 PTY 原始输出用于新客户端历史回放）
        self._scrollback = bytearray()
        self._scrollback_capacity = scrollback_capacity

        # Agent 共享缓冲区（与旧 PTYSession 兼容）
        self._raw_buffer: deque[str] = deque(maxlen=self.MAX_BUFFER_LINES)
        self._output_event = asyncio.Event()

        # 输出回调（可选，用于 SSE 事件等）
        self._on_output_callbacks: list[Callable[[str], None]] = []

    @property
    def running(self) -> bool:
        return self._running

    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def exit_reason(self) -> SessionExitReason | None:
        return self._exit_reason

    @property
    def exit_code(self) -> int | None:
        return self._exit_code

    @property
    def info(self) -> TerminalInfo:
        return TerminalInfo(
            session_id=self.session_id,
            instance_name=self.instance_name,
            backend=self.backend.value,
            pid=self._pid,
            running=self._running,
            created_at=self._created_at.isoformat(),
            ws_clients=len(self._ws_clients),
            exit_reason=self._exit_reason.value if self._exit_reason else None,
            exit_code=self._exit_code,
        )

    # ── 退出回调管理 ──────────────────────────────

    def add_on_exit(self, callback: OnExitCallback) -> None:
        """注册会话退出回调。"""
        self._on_exit_callbacks.append(callback)

    def _fire_on_exit(self) -> None:
        """触发所有退出回调（同步调用，不阻塞）。"""
        for cb in self._on_exit_callbacks:
            try:
                cb(self.session_id, self._exit_reason or SessionExitReason.UNKNOWN, self._exit_code)
            except Exception:
                logger.exception("on_exit 回调异常: session=%s", self.session_id[:8])

    # ── 生命周期 ──────────────────────────────────

    async def start(self, host: Host, decrypted_password: str | None = None) -> None:
        """启动 PTY 子进程。"""
        if self._running:
            logger.warning("终端会话已在运行: %s", self.session_id[:8])
            return

        argv = self._build_backend_argv(host, decrypted_password)
        if self.backend == TerminalBackend.TMUX:
            await self._cleanup_tmux_session()

        logger.info(
            "启动终端子进程: session=%s, backend=%s, host=%s:%d, user=%s",
            self.session_id[:8],
            self.backend.value,
            host.hostname,
            host.port,
            host.username,
        )

        pid, fd = pty.fork()

        if pid == 0:
            try:
                os.execvp(argv[0], argv)
            except Exception:
                os._exit(1)
        else:
            self._pid = pid
            self._fd = fd
            self._running = True
            # 重置退出状态（重新启动时清除旧状态）
            self._exit_reason = None
            self._exit_code = None

            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

            loop = asyncio.get_event_loop()
            loop.add_reader(fd, self._on_pty_readable)

            logger.info(
                "终端会话已启动: %s (pid=%d, backend=%s, tmux=%s)",
                self.session_id[:8],
                pid,
                self.backend.value,
                self.tmux_session_name if self.backend == TerminalBackend.TMUX else "-",
            )

            asyncio.create_task(self._monitor_child())

    async def stop(self) -> None:
        """主动停止终端会话。"""
        if not self._running:
            return

        self._exit_reason = SessionExitReason.STOPPED
        self._exit_code = None

        await self._cleanup_resources()
        self._fire_on_exit()

        logger.info(
            "终端会话已停止: %s (reason=%s)",
            self.session_id[:8],
            self._exit_reason.value,
        )

    def _handle_child_exit(self, reason: SessionExitReason, exit_code: int | None = None) -> None:
        """统一的子进程退出处理。

        所有退出路径（_monitor_child 检测到退出、_on_pty_readable 遇到 EOF/error）
        都汇聚到此方法，确保状态一致性。

        注意：此方法在 event loop 线程中同步调用，资源清理通过 create_task 异步执行。
        """
        if not self._running:
            return  # 避免重复处理

        self._running = False
        self._exit_reason = reason
        self._exit_code = exit_code
        self._output_event.set()

        logger.info(
            "终端子进程退出: session=%s, pid=%s, reason=%s, exit_code=%s",
            self.session_id[:8],
            self._pid,
            reason.value,
            exit_code,
        )

        # 异步清理资源（fd、WebSocket、tmux session）
        asyncio.create_task(self._async_exit_cleanup())

    async def _async_exit_cleanup(self) -> None:
        """子进程退出后的异步资源清理。"""
        # 移除 fd reader
        if self._fd is not None:
            try:
                loop = asyncio.get_event_loop()
                loop.remove_reader(self._fd)
            except Exception:
                pass

        # 关闭 fd
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

        # 回收子进程（避免僵尸）
        if self._pid is not None:
            try:
                os.waitpid(self._pid, os.WNOHANG)
            except ChildProcessError:
                pass

        # 通知所有 WebSocket 客户端会话已结束
        reason_msg = self._exit_reason.value if self._exit_reason else "unknown"
        for client in list(self._ws_clients.values()):
            try:
                await client.ws.send_json({
                    "type": "session_exit",
                    "reason": reason_msg,
                    "exit_code": self._exit_code,
                })
            except Exception:
                pass

        # 触发退出回调
        self._fire_on_exit()

    async def _cleanup_resources(self) -> None:
        """停止会话的完整资源清理（由 stop() 主动调用）。"""
        self._running = False

        # 移除 fd reader
        if self._fd is not None:
            try:
                loop = asyncio.get_event_loop()
                loop.remove_reader(self._fd)
            except Exception:
                pass

        # tmux backend 需要额外清理残留 session
        if self.backend == TerminalBackend.TMUX:
            await self._cleanup_tmux_session()

        # 终止子进程
        if self._pid is not None:
            try:
                os.kill(self._pid, signal.SIGTERM)
                # 等待子进程退出（避免僵尸进程）
                for _ in range(50):  # 5 秒超时
                    try:
                        pid, _ = os.waitpid(self._pid, os.WNOHANG)
                        if pid != 0:
                            break
                    except ChildProcessError:
                        break
                    await asyncio.sleep(0.1)
                else:
                    # 超时，强制 kill
                    try:
                        os.kill(self._pid, signal.SIGKILL)
                        os.waitpid(self._pid, 0)
                    except (ProcessLookupError, ChildProcessError):
                        pass
            except ProcessLookupError:
                pass

        # 关闭 fd
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

        # 关闭所有 WebSocket
        for client in list(self._ws_clients.values()):
            try:
                await client.ws.close(code=1001, reason="终端已关闭")
            except Exception:
                pass
        self._ws_clients.clear()

        # 唤醒所有等待者
        self._output_event.set()

    # ── PTY I/O ──────────────────────────────────

    def write(self, data: str) -> None:
        """向 PTY 写入数据（用户输入）。

        Broker 模式下智能过滤鼠标事件序列：
        - 远端启用了鼠标追踪（如 vim/less/htop）→ 放行鼠标事件，远端处理
        - 远端未启用鼠标追踪（普通 shell）→ 过滤鼠标事件，由 xterm.js 本地处理（如滚动 scrollback）
        """
        if self._fd is not None and self._running:
            # Broker 模式：根据远端鼠标追踪状态决定是否过滤鼠标事件
            if self._vterm and not self._vterm.mouse_tracking_enabled:
                data = _MOUSE_EVENT_RE.sub("", data)
                if not data:
                    return
            try:
                os.write(self._fd, data.encode())
            except OSError as e:
                logger.warning("PTY 写入失败: %s - %s", self.session_id[:8], e)

    def resize(self, cols: int, rows: int, client_id: str | None = None) -> None:
        """调整 PTY 终端尺寸。

        Broker 模式 + client_id 提供时：
        1. 更新该 client 的尺寸记录
        2. 计算所有 client 的 min(cols) × min(rows)
        3. 用最小尺寸设置 PTY
        4. 通知所有客户端当前有效尺寸（resize_hint）

        TMUX 模式或无 client_id 时：直写 PTY（tmux 自身处理多客户端渲染）。
        """
        if self._fd is None or not self._running:
            return

        if self.backend == TerminalBackend.BROKER and client_id:
            # Broker 模式：min-size 策略
            client = self._ws_clients.get(client_id)
            if client:
                client.cols = cols
                client.rows = rows

            effective_cols, effective_rows = self._compute_min_size()
            self._set_pty_size(effective_cols, effective_rows)
            # 同步更新虚拟终端尺寸
            if self._vterm:
                self._vterm.resize(effective_cols, effective_rows)
            self._broadcast_resize_hint(effective_cols, effective_rows)
        else:
            # TMUX 模式或 Agent 调用：直写 PTY
            self._set_pty_size(cols, rows)

    def _set_pty_size(self, cols: int, rows: int) -> None:
        """底层 PTY ioctl 调用。"""
        if self._fd is not None:
            try:
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(self._fd, termios.TIOCSWINSZ, winsize)
            except OSError as e:
                logger.debug("PTY resize 失败: %s - %s", self.session_id[:8], e)

    def _compute_min_size(self) -> tuple[int, int]:
        """计算所有活跃客户端的 min(cols) × min(rows)。

        设下限 10×3 避免异常小窗口导致程序渲染出错。
        无客户端时返回默认 80×24。
        """
        if not self._ws_clients:
            return (80, 24)
        cols = min(c.cols for c in self._ws_clients.values())
        rows = min(c.rows for c in self._ws_clients.values())
        return (max(cols, 10), max(rows, 3))

    def _broadcast_resize_hint(self, effective_cols: int, effective_rows: int) -> None:
        """通知所有客户端当前有效终端尺寸。

        当 min-size 策略生效时，某些客户端的尺寸可能大于有效尺寸。
        前端可据此在 StatusBar 显示提示信息。
        """
        msg = {
            "type": "resize_hint",
            "effective_cols": effective_cols,
            "effective_rows": effective_rows,
        }
        for client in self._ws_clients.values():
            try:
                asyncio.create_task(self._safe_ws_send_json(client.ws, msg))
            except Exception:
                pass

    def get_scrollback(self) -> bytes:
        """获取 scrollback 缓冲区内容（用于新客户端历史回放）。"""
        return bytes(self._scrollback)

    def _append_scrollback(self, data: bytes) -> None:
        """追加数据到 scrollback 缓冲区，超出容量时裁剪头部。"""
        self._scrollback.extend(data)
        overflow = len(self._scrollback) - self._scrollback_capacity
        if overflow > 0:
            del self._scrollback[:overflow]

    def _on_pty_readable(self) -> None:
        """PTY fd 可读回调（由 asyncio event loop 调用）"""
        if self._fd is None:
            return

        try:
            data = os.read(self._fd, 65536)
        except OSError:
            # fd 已关闭或错误 → 统一退出处理
            self._handle_child_exit(SessionExitReason.PTY_CLOSED)
            return

        if not data:
            # EOF → 统一退出处理
            self._handle_child_exit(SessionExitReason.PTY_CLOSED)
            return

        # 追加到 scrollback 缓冲区
        self._append_scrollback(data)

        text = data.decode(errors="replace")

        # 追加到 Agent 缓冲区
        for line in text.split("\n"):
            if line:
                self._raw_buffer.append(line)
        self._output_event.set()

        # 广播给所有 WebSocket 客户端
        if self._vterm:
            # Broker 虚拟终端模式：双模式输出
            #
            # Normal Screen（普通 shell 交互）：
            #   直通原始 ANSI → xterm.js 自然滚动（scrollback 正常工作）
            #   pyte 仅做旁路解析（保持内部状态同步：mouse_tracking / alternate_screen / dump）
            #
            # Alternate Screen（vim/top/less 等全屏程序）：
            #   pyte 差分渲染 → 全屏程序无需 scrollback
            #   多客户端共享时差分渲染能保证渲染一致性
            was_alt = self._vterm.alternate_screen_active
            if was_alt:
                # Alternate Screen → 先 feed 让 pyte 状态同步，再决定广播策略
                rendered = self._vterm.feed_and_render(text)
                now_alt = self._vterm.alternate_screen_active

                if not now_alt:
                    # ── Alternate → Normal 切换帧 ──
                    # vim/less 退出时，原始 text 中包含完整的 mode reset 序列：
                    #   \x1b[?1049l  — 退出 Alternate Screen，xterm.js 恢复 normal screen 内容
                    #   \x1b[?1000l  — 关闭鼠标追踪
                    #   + shell prompt 等后续输出
                    #
                    # 直通原始 text 给 xterm.js，让它自行处理所有 mode 切换和画面恢复。
                    # 不能用 full_screen_dump()：它会 \x1b[2J 清屏，破坏 xterm.js 通过
                    # \x1b[?1049l 恢复出来的正确 normal screen 画面和 scrollback。
                    self._broadcast_output(text)
                elif rendered:
                    self._broadcast_output(rendered)
            else:
                # Normal Screen → 直通原始 ANSI + 旁路 feed pyte
                self._vterm.feed_only(text)
                self._broadcast_output(text)
        else:
            # TMUX 直通模式：原始文本广播
            self._broadcast_output(text)

    def _broadcast_output(self, text: str) -> None:
        """广播 PTY 输出给所有 WebSocket 客户端"""
        if not self._ws_clients:
            return

        dead_client_ids: list[str] = []
        for client_id, client in self._ws_clients.items():
            try:
                asyncio.create_task(self._safe_ws_send(client.ws, text))
            except Exception:
                dead_client_ids.append(client_id)

        # 清理已断开的客户端
        for cid in dead_client_ids:
            self._ws_clients.pop(cid, None)

    @staticmethod
    async def _safe_ws_send(ws: WebSocket, text: str) -> None:
        """安全发送 WebSocket output 消息（忽略发送错误）"""
        try:
            await ws.send_json({"type": "output", "data": text})
        except Exception:
            pass

    @staticmethod
    async def _safe_ws_send_history(ws: WebSocket, text: str) -> None:
        """安全发送 WebSocket history 消息（scrollback 回放专用）。

        使用 "history" 消息类型区分于实时 "output"，前端收到后会
        临时屏蔽 onData 回调，防止 xterm.js 对回放中的终端查询序列
        生成响应并发送到 PTY 导致乱码。
        """
        try:
            await ws.send_json({"type": "history", "data": text})
        except Exception:
            pass

    @staticmethod
    async def _safe_ws_send_json(ws: WebSocket, msg: dict) -> None:
        """安全发送任意 JSON 消息（忽略发送错误）"""
        try:
            await ws.send_json(msg)
        except Exception:
            pass

    # ── WebSocket 客户端管理 ──────────────────────

    def add_ws_client(self, ws: WebSocket, cols: int = 80, rows: int = 24) -> str:
        """添加 WebSocket 客户端，返回分配的 client_id。

        新客户端连接时：
        1. 分配唯一 client_id
        2. 记录客户端尺寸（用于 Broker min-size 策略）
        3. 发送 scrollback 历史回放
        4. Broker 模式下重新计算 min-size 并通知所有客户端
        """
        client_id = str(uuid.uuid4())
        client = ClientInfo(ws=ws, client_id=client_id, cols=cols, rows=rows)
        self._ws_clients[client_id] = client

        # 发送历史恢复（仅 Broker 模式需要，TMUX 模式由 tmux 自身处理屏幕恢复）
        if self._vterm:
            if self._vterm.alternate_screen_active:
                # Alternate Screen（全屏程序）→ 使用 pyte 全屏快照精确恢复
                snapshot = self._vterm.full_screen_dump()
                if snapshot:
                    asyncio.create_task(self._safe_ws_send_history(ws, snapshot))
            else:
                # Normal Screen（普通 shell）→ 使用 scrollback 缓冲区回放
                # 直通模式下 scrollback 包含原始 ANSI 流，需过滤终端查询序列防止乱码
                raw = self.get_scrollback()
                if raw:
                    text = raw.decode(errors="replace")
                    filtered = _TERMINAL_QUERY_RE.sub("", text)
                    if filtered:
                        asyncio.create_task(self._safe_ws_send_history(ws, filtered))

        # Broker 模式下重新计算 min-size
        if self.backend == TerminalBackend.BROKER and self._fd is not None and self._running:
            effective_cols, effective_rows = self._compute_min_size()
            self._set_pty_size(effective_cols, effective_rows)
            if self._vterm:
                self._vterm.resize(effective_cols, effective_rows)
            self._broadcast_resize_hint(effective_cols, effective_rows)

        logger.info(
            "WebSocket 客户端已连接: %s (client=%s, 总数: %d, backend=%s)",
            self.session_id[:8], client_id[:8], len(self._ws_clients), self.backend.value,
        )
        return client_id

    def remove_ws_client(self, client_id: str) -> None:
        """按 client_id 移除 WebSocket 客户端。

        客户端断开后：
        - Broker 模式下重新计算 min-size（剩余客户端可能获得更大的有效尺寸）
        """
        removed = self._ws_clients.pop(client_id, None)
        if removed:
            logger.info(
                "WebSocket 客户端已断开: %s (client=%s, 总数: %d)",
                self.session_id[:8], client_id[:8], len(self._ws_clients),
            )

            # Broker 模式下重新计算 min-size
            if (
                self.backend == TerminalBackend.BROKER
                and self._ws_clients
                and self._fd is not None
                and self._running
            ):
                effective_cols, effective_rows = self._compute_min_size()
                self._set_pty_size(effective_cols, effective_rows)
                if self._vterm:
                    self._vterm.resize(effective_cols, effective_rows)
                self._broadcast_resize_hint(effective_cols, effective_rows)

    def remove_ws_client_by_ws(self, ws: WebSocket) -> None:
        """按 WebSocket 实例移除客户端（兼容旧调用方式）。"""
        for client_id, client in list(self._ws_clients.items()):
            if client.ws is ws:
                self.remove_ws_client(client_id)
                return

    async def send_to_clients(self, msg: dict[str, object]) -> None:
        """向所有 WebSocket 客户端发送自定义 JSON 消息

        用于 tmux copy-mode → 浏览器剪贴板联动等场景。
        """
        if not self._ws_clients:
            return

        dead_client_ids: list[str] = []
        for client_id, client in self._ws_clients.items():
            try:
                await client.ws.send_json(msg)
            except Exception:
                dead_client_ids.append(client_id)

        for cid in dead_client_ids:
            self._ws_clients.pop(cid, None)

    # ── Agent 共享接口（兼容旧 PTYSession 的 send_input/wait_for/read_screen）──

    async def send_input(self, text: str) -> None:
        """向 PTY 发送输入（Agent 使用）"""
        if not self._running:
            raise ConnectionError("终端会话未运行")
        self.write(text)

    async def wait_for(
        self,
        pattern: str,
        timeout: float = 30.0,
        _start_pos: int | None = None,
    ) -> str:
        """等待 PTY 输出中出现指定模式（Agent 使用，expect 风格）"""
        import re
        from src.services.pty_session import is_tmux_status_line, strip_ansi

        if not self._running:
            raise ConnectionError("终端会话未运行")

        try:
            regex = re.compile(pattern, re.MULTILINE)
        except re.error:
            regex = re.compile(re.escape(pattern), re.MULTILINE)

        start_time = asyncio.get_event_loop().time()
        collected_lines: list[str] = []
        start_pos = _start_pos if _start_pos is not None else len(self._raw_buffer)

        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed >= timeout:
                collected = strip_ansi("\n".join(collected_lines))
                raise TimeoutError(
                    f"等待模式 '{pattern}' 超时（{timeout}s）。"
                    f"最近输出:\n{collected[-500:]}"
                )

            current_len = len(self._raw_buffer)
            if current_len > start_pos:
                new_lines = list(self._raw_buffer)[start_pos:current_len]
                start_pos = current_len

                for line in new_lines:
                    clean_line = strip_ansi(line)
                    # 过滤 tmux 状态栏行，避免干扰提示符匹配
                    if is_tmux_status_line(clean_line):
                        continue
                    collected_lines.append(clean_line)

                full_text = "\n".join(collected_lines)
                if regex.search(full_text):
                    return full_text

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
        """发送命令并等待完成（Agent 使用）"""
        pre_pos = len(self._raw_buffer)

        if not command.endswith("\n") and not command.endswith("\r"):
            command += "\r"
        await self.send_input(command)

        await asyncio.sleep(0.3)

        return await self.wait_for(wait_pattern, timeout=timeout, _start_pos=pre_pos)

    def read_screen(self, lines: int = 50) -> str:
        """读取终端屏幕缓冲区（Agent 使用）"""
        from src.services.pty_session import strip_ansi, strip_tmux_status

        buf = list(self._raw_buffer)
        recent = buf[-lines:] if len(buf) > lines else buf
        raw_text = strip_ansi("\n".join(recent))
        return strip_tmux_status(raw_text)

    # ── 内部方法 ──────────────────────────────────

    def _build_backend_argv(self, host: Host, password: str | None) -> list[str]:
        """根据 backend 构建子进程启动命令。"""
        if self.backend == TerminalBackend.TMUX:
            script_path = str(_TMUX_SCRIPT_PATH)
            if not _TMUX_SCRIPT_PATH.exists():
                raise FileNotFoundError(f"tmux 脚本不存在: {script_path}")
            return [
                "bash",
                script_path,
                self.tmux_session_name,
                host.hostname,
                str(host.port),
                host.username,
                password or "",
                host.private_key_path or "",
            ]

        ssh_command = build_ssh_command_for_host(host, decrypted_password=password)
        return ["bash", "-lc", ssh_command]

    async def _monitor_child(self) -> None:
        """监控子进程退出，归类退出原因。"""
        if self._pid is None:
            return

        while self._running:
            try:
                pid, raw_status = os.waitpid(self._pid, os.WNOHANG)
                if pid != 0:
                    # 子进程已退出，分析退出原因
                    reason, code = self._classify_exit(raw_status)
                    self._handle_child_exit(reason, code)
                    return
            except ChildProcessError:
                self._handle_child_exit(SessionExitReason.UNKNOWN)
                return
            await asyncio.sleep(1.0)

    @staticmethod
    def _classify_exit(raw_status: int) -> tuple[SessionExitReason, int | None]:
        """根据 waitpid 返回的 raw_status 分类退出原因。

        Returns:
            (reason, exit_code) 元组
        """
        if os.WIFEXITED(raw_status):
            exit_code = os.WEXITSTATUS(raw_status)
            if exit_code == 0:
                return SessionExitReason.NORMAL, 0
            # SSH 失败通常返回 exit code 255
            if exit_code == 255:
                return SessionExitReason.SSH_FAILED, 255
            return SessionExitReason.CHILD_CRASHED, exit_code

        if os.WIFSIGNALED(raw_status):
            sig = os.WTERMSIG(raw_status)
            return SessionExitReason.CHILD_CRASHED, -sig

        return SessionExitReason.UNKNOWN, None

    async def _cleanup_tmux_session(self) -> None:
        """清理 tmux session（精确匹配 + 验证）"""
        session_name = self.tmux_session_name
        exact_target = f"={session_name}"

        try:
            proc = await asyncio.create_subprocess_exec(
                "tmux", "has-session", "-t", exact_target,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if proc.returncode != 0:
                return

            proc = await asyncio.create_subprocess_exec(
                "tmux", "kill-session", "-t", exact_target,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if proc.returncode == 0:
                logger.info("tmux session 已清理: %s", session_name)
        except Exception as e:
            logger.warning("清理 tmux session 异常: %s - %s", session_name, e)


class TerminalManager:
    """终端会话管理器

    管理所有终端会话的生命周期，替代 WeTTYManager。
    """

    def __init__(self, default_backend: TerminalBackend | None = None) -> None:
        self._sessions: dict[str, TerminalSession] = {}
        self._lock = asyncio.Lock()
        self._default_backend = default_backend or read_default_terminal_backend()

    @property
    def default_backend(self) -> TerminalBackend:
        return self._default_backend

    @default_backend.setter
    def default_backend(self, value: TerminalBackend) -> None:
        self._default_backend = value

    async def switch_backend(self, new_backend: TerminalBackend) -> list[str]:
        """全局切换 backend：更新默认值 + 停止所有现有会话。

        前端收到响应后会逐个 Tab 重新 startTerminal，
        后端自动用新 default_backend 创建会话。

        Returns:
            被停止的 instance_name 列表（前端据此知道哪些 Tab 需要重连）。
        """
        old_backend = self._default_backend
        self._default_backend = new_backend
        logger.info("全局 backend 切换: %s -> %s", old_backend.value, new_backend.value)

        # 停止所有现有会话
        async with self._lock:
            sessions = list(self._sessions.values())
            stopped_names = list(self._sessions.keys())
            self._sessions.clear()

        for s in sessions:
            await s.stop()

        if stopped_names:
            logger.info(
                "全局切换已停止 %d 个会话: %s",
                len(stopped_names),
                ", ".join(stopped_names),
            )

        return stopped_names

    async def create_session(
        self,
        instance_name: str,
        host: Host,
        decrypted_password: str | None = None,
        backend: str | TerminalBackend | None = None,
    ) -> tuple[TerminalSession, bool]:
        """创建并启动终端会话。

        Returns:
            (session, is_new) 元组。is_new=True 表示新创建了会话，
            is_new=False 表示复用了已有会话（instance_name + backend 完全相同）。
        """
        selected_backend = resolve_terminal_backend(backend, fallback=self._default_backend)
        previous_session: TerminalSession | None = None

        async with self._lock:
            existing = self._sessions.get(instance_name)
            if existing and existing.running and existing.backend == selected_backend:
                return existing, False
            if existing is not None:
                previous_session = self._sessions.pop(instance_name, None)

            session_id = str(uuid.uuid4())
            session = TerminalSession(
                session_id=session_id,
                instance_name=instance_name,
                backend=selected_backend,
            )
            self._sessions[instance_name] = session

        if previous_session is not None:
            await previous_session.stop()

        try:
            await session.start(host, decrypted_password)
        except Exception:
            async with self._lock:
                current = self._sessions.get(instance_name)
                if current is session:
                    self._sessions.pop(instance_name, None)
            raise

        logger.info(
            "终端会话已创建: %s -> %s (backend=%s)",
            session_id[:8],
            instance_name,
            selected_backend.value,
        )
        return session, True

    def get_session(self, instance_name: str) -> TerminalSession | None:
        """根据实例名获取会话"""
        session = self._sessions.get(instance_name)
        if session and session.running:
            return session
        return None

    def get_session_by_id(self, session_id: str) -> TerminalSession | None:
        """根据 session_id 获取会话"""
        for session in self._sessions.values():
            if session.session_id == session_id:
                return session
        return None

    def has_running_session(self, instance_name: str) -> bool:
        """检测是否有运行中的会话"""
        session = self._sessions.get(instance_name)
        return session is not None and session.running

    async def stop_session(self, instance_name: str) -> bool:
        """停止指定会话"""
        async with self._lock:
            session = self._sessions.pop(instance_name, None)

        if not session:
            return False

        await session.stop()
        logger.info("终端会话已停止: %s", instance_name)
        return True

    async def stop_all(self) -> None:
        """停止所有会话"""
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()

        for s in sessions:
            await s.stop()

        logger.info("所有终端会话已停止")

    def list_sessions(self) -> list[TerminalInfo]:
        """列出所有会话"""
        return [s.info for s in self._sessions.values()]

    async def cleanup_zombie_sessions(self) -> int:
        """清理 zombie tmux session。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "tmux", "ls", "-F", "#{session_name}:#{session_attached}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            logger.debug("tmux 未安装，跳过 zombie session 清理")
            return 0

        stdout, _ = await proc.communicate()

        if proc.returncode != 0 or not stdout:
            return 0

        active_sessions: set[str] = set()
        async with self._lock:
            for session in self._sessions.values():
                if session.running and session.backend == TerminalBackend.TMUX:
                    active_sessions.add(session.tmux_session_name)

        cleaned = 0
        for line in stdout.decode().strip().split("\n"):
            if not line.strip():
                continue
            parts = line.strip().split(":")
            if len(parts) < 2:
                continue
            session_name = parts[0]
            attached = int(parts[1]) if parts[1].isdigit() else 0

            if (
                session_name.startswith(_TMUX_SESSION_PREFIX + "-")
                and attached == 0
                and session_name not in active_sessions
            ):
                kill_proc = await asyncio.create_subprocess_exec(
                    "tmux", "kill-session", "-t", f"={session_name}",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await kill_proc.wait()
                if kill_proc.returncode == 0:
                    logger.info("已清理 zombie tmux session: %s", session_name)
                    cleaned += 1

        return cleaned
