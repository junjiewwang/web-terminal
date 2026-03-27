"""tmux 多窗口管理服务

管理堡垒机 tmux session 中的多窗口（每个二级主机一个窗口），
提供窗口的创建、切换、列出和关闭操作。

架构关系：
  WeTTYManager → 启动 WeTTY 进程（使用 tmux-session.sh 创建 tmux session）
  TmuxWindowManager → 在已有 tmux session 中管理窗口（不启动新进程）

tmux 命名规则：
  - session: wetty-{host_name}（由 tmux-session.sh 创建）
  - window:  {jump_host_name}（如 m12, m15）
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class TmuxWindow:
    """tmux 窗口信息"""

    session_name: str
    window_name: str
    window_index: int
    active: bool


@dataclass
class TmuxClient:
    """tmux 客户端信息"""
    tty: str          # 客户端 TTY（如 /dev/pts/3）
    window: str       # 当前窗口名
    session: str      # 会话名


class TmuxWindowManager:
    """tmux 多窗口管理器

    在堡垒机的 tmux session 中创建/切换/列出窗口。
    所有操作通过 asyncio.subprocess 执行 tmux 命令。
    """

    # tmux 会话名前缀（与 wetty_manager.py 中的 _WeTTYProcess.TMUX_SESSION_PREFIX 一致）
    SESSION_PREFIX = "wetty"

    @classmethod
    def session_name_for(cls, host_name: str) -> str:
        """根据主机名生成 tmux 会话名"""
        return f"{cls.SESSION_PREFIX}-{host_name}"

    async def session_exists(self, session_name: str) -> bool:
        """检查 tmux 会话是否存在（精确匹配）"""
        proc = await asyncio.create_subprocess_exec(
            "tmux", "has-session", "-t", f"={session_name}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return proc.returncode == 0

    async def is_session_logged_in(self, session_name: str) -> bool:
        """检测 tmux session 是否已有活跃的 SSH/sshpass 连接

        用于跳板编排防重入：如果 session 中的 pane 已经在执行 SSH 相关命令，
        说明上次的跳板编排已成功，不需要再次执行。

        Returns:
            True 如果 session 存在且 pane 的 current_command 包含 ssh/sshpass
        """
        if not await self.session_exists(session_name):
            return False

        proc = await asyncio.create_subprocess_exec(
            "tmux", "list-panes", "-t", f"={session_name}",
            "-F", "#{pane_current_command}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()

        if proc.returncode != 0:
            return False

        # 检查所有 pane 的命令是否包含 ssh/sshpass
        for line in stdout.decode().strip().split("\n"):
            cmd = line.strip().lower()
            if cmd and cmd in ("ssh", "sshpass", "bash", "zsh", "sh"):
                return True

        return False

    async def create_window(
        self,
        session_name: str,
        window_name: str,
        command: Optional[str] = None,
    ) -> bool:
        """在指定 tmux session 中创建新窗口

        如果窗口已存在则直接返回 True（幂等操作）。

        Args:
            session_name: tmux 会话名
            window_name: 窗口名
            command: 可选，窗口创建后自动执行的 shell 命令。
                     用于 jump_host 场景：新窗口启动时自动 SSH 到堡垒机，
                     避免创建空 shell 窗口导致跳板编排无法匹配 ready_pattern。
                     命令通过 tmux new-window 的命令参数传入，窗口的生命周期
                     与命令进程绑定（命令退出 → 窗口关闭）。

        Returns:
            是否成功
        """
        # 检查窗口是否已存在
        existing = await self.list_windows(session_name)
        if any(w.window_name == window_name for w in existing):
            logger.info("tmux 窗口已存在: %s:%s", session_name, window_name)
            return True

        # 构造 tmux new-window 命令（-d: detached，不切换全局 active window）
        # 关键：不加 -d 会导致 tmux new-window 将新窗口设为 session 全局活跃窗口，
        # 所有 attached client（包括浏览器终端）的视图都会被切换到新窗口。
        # 加 -d 后新窗口在后台创建，不影响任何现有 client 的视图。
        cmd: list[str] = [
            "tmux", "new-window", "-d",
            "-t", session_name, "-n", window_name,
        ]
        if command:
            cmd.append(command)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error(
                "创建 tmux 窗口失败: %s:%s - %s",
                session_name, window_name, stderr.decode().strip(),
            )
            return False

        if command:
            logger.info("tmux 窗口已创建: %s:%s (command=%s)", session_name, window_name, command[:60])
        else:
            logger.info("tmux 窗口已创建: %s:%s", session_name, window_name)
        return True

    async def select_window(self, session_name: str, window_name: str) -> bool:
        """切换到指定窗口

        Args:
            session_name: tmux 会话名
            window_name: 窗口名

        Returns:
            是否成功
        """
        proc = await asyncio.create_subprocess_exec(
            "tmux", "select-window", "-t", f"{session_name}:{window_name}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error(
                "切换 tmux 窗口失败: %s:%s - %s",
                session_name, window_name, stderr.decode().strip(),
            )
            return False

        logger.info("tmux 窗口已切换: %s:%s", session_name, window_name)
        return True

    async def list_windows(self, session_name: str) -> list[TmuxWindow]:
        """列出指定 session 的所有窗口

        Args:
            session_name: tmux 会话名

        Returns:
            窗口列表
        """
        proc = await asyncio.create_subprocess_exec(
            "tmux", "list-windows", "-t", session_name,
            "-F", "#{window_index}:#{window_name}:#{window_active}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.warning(
                "列出 tmux 窗口失败: %s - %s",
                session_name, stderr.decode().strip(),
            )
            return []

        windows: list[TmuxWindow] = []
        for line in stdout.decode().strip().split("\n"):
            if not line.strip():
                continue
            parts = line.strip().split(":")
            if len(parts) >= 3:
                windows.append(TmuxWindow(
                    session_name=session_name,
                    window_name=parts[1],
                    window_index=int(parts[0]),
                    active=parts[2] == "1",
                ))

        return windows

    async def close_window(self, session_name: str, window_name: str) -> bool:
        """关闭指定窗口

        Args:
            session_name: tmux 会话名
            window_name: 窗口名

        Returns:
            是否成功
        """
        proc = await asyncio.create_subprocess_exec(
            "tmux", "kill-window", "-t", f"{session_name}:{window_name}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.warning(
                "关闭 tmux 窗口失败: %s:%s - %s",
                session_name, window_name, stderr.decode().strip(),
            )
            return False

        logger.info("tmux 窗口已关闭: %s:%s", session_name, window_name)
        return True

    async def get_active_window(self, session_name: str) -> Optional[str]:
        """获取当前活跃窗口名

        Args:
            session_name: tmux 会话名

        Returns:
            活跃窗口名，无则返回 None
        """
        windows = await self.list_windows(session_name)
        for w in windows:
            if w.active:
                return w.window_name
        return None

    async def list_clients(self, session_name: str) -> list[TmuxClient]:
        """列出指定 tmux 会话的所有客户端信息

        用于 per-client 窗口切换（tmux switch-client -c）。
        返回每个客户端的 TTY、当前窗口和会话信息。

        Args:
            session_name: tmux 会话名

        Returns:
            客户端信息列表，会话不存在返回空列表
        """
        proc = await asyncio.create_subprocess_exec(
            "tmux", "list-clients", "-t", session_name,
            "-F", "#{client_tty}:#{client_window_name}:#{client_session}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.warning(
                "列出 tmux 客户端失败: %s - %s",
                session_name, stderr.decode().strip(),
            )
            return []

        clients: list[TmuxClient] = []
        for line in stdout.decode().strip().split("\n"):
            if not line.strip():
                continue
            parts = line.strip().split(":")
            if len(parts) >= 3:
                clients.append(TmuxClient(
                    tty=parts[0],
                    window=parts[1],
                    session=parts[2],
                ))

        return clients

    async def switch_client(
        self,
        client_tty: str,
        session_name: str,
        window_name: str,
    ) -> bool:
        """切换指定 tmux 客户端的视图到目标窗口（不影响其他客户端）

        与 select_window 的区别：
          - select_window: 全局切换，所有 attached client 都会切换到目标窗口
          - switch_client: 只切换指定的 client，其他 client 视图不受影响

        适用场景：
          - 多 Tab 终端：每个 Tab 保持独立的 tmux 窗口视图
          - 后台编排：PTY client 切换到目标窗口，不影响浏览器 client

        Args:
            client_tty: tmux 客户端的 TTY（如 '/dev/pts/3'）
            session_name: tmux 会话名
            window_name: 目标窗口名

        Returns:
            是否成功
        """
        proc = await asyncio.create_subprocess_exec(
            "tmux", "switch-client",
            "-c", client_tty,
            "-t", f"{session_name}:{window_name}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error(
                "tmux switch-client 失败: client=%s, %s:%s - %s",
                client_tty, session_name, window_name, stderr.decode().strip(),
            )
            return False

        logger.info(
            "tmux switch-client: %s → %s:%s (client=%s)",
            session_name, session_name, window_name, client_tty,
        )
        return True

    async def send_keys(
        self,
        session_name: str,
        window_name: str,
        keys: str,
    ) -> bool:
        """向指定窗口发送按键

        通过 tmux send-keys 命令向窗口发送输入。
        适用于需要精确控制发送目标窗口的场景。

        Args:
            session_name: tmux 会话名
            window_name: 窗口名
            keys: 要发送的按键内容

        Returns:
            是否成功
        """
        proc = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", f"{session_name}:{window_name}", keys,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error(
                "tmux send-keys 失败: %s:%s - %s",
                session_name, window_name, stderr.decode().strip(),
            )
            return False

        return True
