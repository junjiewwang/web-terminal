"""终端会话管理服务（自研 Python PTY 层）

替代 WeTTY(Node.js) 中间层，使用 Python 原生 PTY + asyncio 实现：
- 每个终端会话 = 一个 PTY 子进程（exec tmux-session.sh → tmux → SSH）
- 浏览器通过 FastAPI WebSocket 直连 PTY（不再经过 WeTTY + socket.io）
- Agent 通过进程内共享缓冲区直接读写 PTY（不再通过 socket.io client）
- 多客户端（浏览器 + Agent）共享同一个 PTY fd，输出广播给所有订阅者

架构：
  浏览器 xterm.js → WebSocket → TerminalSession → PTY fd → tmux → SSH → 远端主机
  Agent PTY       →           → TerminalSession → PTY fd（共享）
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import pty
import signal
import struct
import termios
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from fastapi import WebSocket

from src.models.host import AuthType, Host
from src.utils.ssh_command import build_ssh_command

logger = logging.getLogger(__name__)

# tmux 会话脚本路径
_TMUX_SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "tmux-session.sh"

# tmux 会话名前缀
_TMUX_SESSION_PREFIX = "wetty"


@dataclass
class TerminalInfo:
    """终端会话信息（供 API 返回）"""
    session_id: str
    instance_name: str
    pid: int | None
    running: bool
    created_at: str
    ws_clients: int


class TerminalSession:
    """单个终端会话

    管理一个 PTY 子进程（tmux-session.sh），提供：
    - asyncio 异步 fd 读写
    - 多 WebSocket 客户端广播（浏览器实时回显）
    - Agent 共享缓冲区（send_input / wait_for / read_screen）
    - 终端 resize
    """

    MAX_BUFFER_LINES = 500

    def __init__(self, session_id: str, instance_name: str) -> None:
        self.session_id = session_id
        self.instance_name = instance_name
        self.tmux_session_name = f"{_TMUX_SESSION_PREFIX}-{instance_name}"

        # PTY 进程
        self._pid: int | None = None
        self._fd: int | None = None
        self._running = False
        self._created_at = datetime.now()

        # 多客户端广播
        self._ws_clients: list[WebSocket] = []

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
    def info(self) -> TerminalInfo:
        return TerminalInfo(
            session_id=self.session_id,
            instance_name=self.instance_name,
            pid=self._pid,
            running=self._running,
            created_at=self._created_at.isoformat(),
            ws_clients=len(self._ws_clients),
        )

    # ── 生命周期 ──────────────────────────────────

    async def start(self, host: Host, decrypted_password: str | None = None) -> None:
        """启动 PTY 子进程（exec tmux-session.sh）

        Args:
            host: 主机 ORM 对象（用于 SSH 连接参数）
            decrypted_password: 已解密的密码
        """
        if self._running:
            logger.warning("终端会话已在运行: %s", self.session_id[:8])
            return

        # 启动前清理残留 tmux session
        await self._cleanup_tmux_session()

        # 构建 tmux-session.sh 参数
        script_path = str(_TMUX_SCRIPT_PATH)
        if not _TMUX_SCRIPT_PATH.exists():
            raise FileNotFoundError(f"tmux 脚本不存在: {script_path}")

        args = self._build_script_args(host, decrypted_password)

        # fork PTY
        pid, fd = pty.fork()

        if pid == 0:
            # 子进程：exec tmux-session.sh
            try:
                os.execvp("bash", ["bash", script_path] + args)
            except Exception:
                os._exit(1)
        else:
            # 父进程：保存 fd，注册 asyncio reader
            self._pid = pid
            self._fd = fd
            self._running = True

            # 设置 fd 为非阻塞
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

            # 注册 asyncio fd reader
            loop = asyncio.get_event_loop()
            loop.add_reader(fd, self._on_pty_readable)

            logger.info(
                "终端会话已启动: %s (pid=%d, tmux=%s)",
                self.session_id[:8], pid, self.tmux_session_name,
            )

            # 启动子进程监控任务
            asyncio.create_task(self._monitor_child())

    async def stop(self) -> None:
        """停止终端会话"""
        if not self._running:
            return

        self._running = False

        # 移除 fd reader
        if self._fd is not None:
            try:
                loop = asyncio.get_event_loop()
                loop.remove_reader(self._fd)
            except Exception:
                pass

        # 先清理 tmux session
        await self._cleanup_tmux_session()

        # 再终止子进程
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
        for ws in list(self._ws_clients):
            try:
                await ws.close(code=1001, reason="终端已关闭")
            except Exception:
                pass
        self._ws_clients.clear()

        # 唤醒所有等待者
        self._output_event.set()

        logger.info("终端会话已停止: %s", self.session_id[:8])

    # ── PTY I/O ──────────────────────────────────

    def write(self, data: str) -> None:
        """向 PTY 写入数据（用户输入）"""
        if self._fd is not None and self._running:
            try:
                os.write(self._fd, data.encode())
            except OSError as e:
                logger.warning("PTY 写入失败: %s - %s", self.session_id[:8], e)

    def resize(self, cols: int, rows: int) -> None:
        """调整 PTY 终端尺寸"""
        if self._fd is not None and self._running:
            try:
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(self._fd, termios.TIOCSWINSZ, winsize)
            except OSError as e:
                logger.debug("PTY resize 失败: %s - %s", self.session_id[:8], e)

    def _on_pty_readable(self) -> None:
        """PTY fd 可读回调（由 asyncio event loop 调用）"""
        if self._fd is None:
            return

        try:
            data = os.read(self._fd, 65536)
        except OSError:
            # fd 已关闭或错误
            if self._running:
                self._running = False
                self._output_event.set()
            return

        if not data:
            # EOF
            if self._running:
                self._running = False
                self._output_event.set()
            return

        text = data.decode(errors="replace")

        # 追加到 Agent 缓冲区
        for line in text.split("\n"):
            if line:
                self._raw_buffer.append(line)
        self._output_event.set()

        # 广播给所有 WebSocket 客户端
        self._broadcast_output(text)

    def _broadcast_output(self, text: str) -> None:
        """广播 PTY 输出给所有 WebSocket 客户端"""
        if not self._ws_clients:
            return

        dead_clients: list[WebSocket] = []
        for ws in self._ws_clients:
            try:
                asyncio.create_task(self._safe_ws_send(ws, text))
            except Exception:
                dead_clients.append(ws)

        # 清理已断开的客户端
        for ws in dead_clients:
            self._ws_clients.remove(ws)

    @staticmethod
    async def _safe_ws_send(ws: WebSocket, text: str) -> None:
        """安全发送 WebSocket 消息（忽略发送错误）"""
        try:
            await ws.send_json({"type": "output", "data": text})
        except Exception:
            pass

    # ── WebSocket 客户端管理 ──────────────────────

    def add_ws_client(self, ws: WebSocket) -> None:
        """添加 WebSocket 客户端"""
        self._ws_clients.append(ws)
        logger.info(
            "WebSocket 客户端已连接: %s (总数: %d)",
            self.session_id[:8], len(self._ws_clients),
        )

    def remove_ws_client(self, ws: WebSocket) -> None:
        """移除 WebSocket 客户端"""
        if ws in self._ws_clients:
            self._ws_clients.remove(ws)
            logger.info(
                "WebSocket 客户端已断开: %s (总数: %d)",
                self.session_id[:8], len(self._ws_clients),
            )

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
        from src.services.pty_session import strip_ansi

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
        from src.services.pty_session import strip_ansi

        buf = list(self._raw_buffer)
        recent = buf[-lines:] if len(buf) > lines else buf
        return strip_ansi("\n".join(recent))

    # ── 内部方法 ──────────────────────────────────

    def _build_script_args(self, host: Host, password: str | None) -> list[str]:
        """构建 tmux-session.sh 参数列表"""
        args = [
            self.tmux_session_name,
            host.hostname,
            str(host.port),
            host.username,
            password or "",
            host.private_key_path or "",
        ]
        return args

    async def _monitor_child(self) -> None:
        """监控子进程退出"""
        if self._pid is None:
            return

        while self._running:
            try:
                pid, status = os.waitpid(self._pid, os.WNOHANG)
                if pid != 0:
                    logger.info(
                        "终端子进程已退出: %s (pid=%d, status=%d)",
                        self.session_id[:8], pid, status,
                    )
                    self._running = False
                    self._output_event.set()
                    return
            except ChildProcessError:
                self._running = False
                self._output_event.set()
                return
            await asyncio.sleep(1.0)

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

    def __init__(self) -> None:
        self._sessions: dict[str, TerminalSession] = {}
        self._lock = asyncio.Lock()

    async def create_session(
        self,
        instance_name: str,
        host: Host,
        decrypted_password: str | None = None,
    ) -> TerminalSession:
        """创建并启动终端会话

        Args:
            instance_name: 实例名（如 "tce-server" 或 "tce-server--m12"）
            host: 主机 ORM 对象
            decrypted_password: 已解密的密码

        Returns:
            已启动的 TerminalSession
        """
        async with self._lock:
            # 复用已有会话
            if instance_name in self._sessions:
                existing = self._sessions[instance_name]
                if existing.running:
                    return existing
                # 已停止，清理后重建
                del self._sessions[instance_name]

            session_id = str(uuid.uuid4())
            session = TerminalSession(session_id=session_id, instance_name=instance_name)
            self._sessions[instance_name] = session

        await session.start(host, decrypted_password)
        logger.info("终端会话已创建: %s -> %s", session_id[:8], instance_name)
        return session

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
        """清理 zombie tmux session"""
        proc = await asyncio.create_subprocess_exec(
            "tmux", "ls", "-F", "#{session_name}:#{session_attached}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()

        if proc.returncode != 0 or not stdout:
            return 0

        active_sessions = set()
        async with self._lock:
            for session in self._sessions.values():
                if session.running:
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
