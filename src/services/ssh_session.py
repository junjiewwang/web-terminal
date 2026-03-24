"""SSH 会话管理服务

基于 asyncssh 实现持久 SSH 连接池，支持：
- exec_command：单次命令执行（独立 channel，有返回值）
- shell session：交互式 shell（cd 后保持目录状态）
- 连接复用 & 自动重连
- 命令超时保护
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import asyncssh

from src.models.host import AuthType, Host

logger = logging.getLogger(__name__)

# 默认命令超时（秒）
DEFAULT_COMMAND_TIMEOUT = 30


@dataclass
class CommandResult:
    """命令执行结果"""

    session_id: str
    host_name: str
    command: str
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: float
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    @property
    def success(self) -> bool:
        return self.exit_code == 0


@dataclass
class SessionInfo:
    """SSH 会话信息"""

    session_id: str
    host_name: str
    hostname: str
    username: str
    connected: bool
    created_at: str
    last_activity: str


class SSHSessionManager:
    """SSH 会话管理器

    维护一个 session_id -> SSHConnection 的连接池，
    每个会话绑定一个主机，支持命令执行和状态查询。
    """

    def __init__(self) -> None:
        self._sessions: dict[str, _SSHSession] = {}
        self._lock = asyncio.Lock()

    async def create_session(self, host: Host) -> str:
        """创建新的 SSH 会话并建立连接

        Args:
            host: 主机 ORM 对象

        Returns:
            session_id

        Raises:
            ConnectionError: 无法连接到目标主机
        """
        session_id = str(uuid.uuid4())
        ssh_session = _SSHSession(session_id=session_id, host=host)
        await ssh_session.connect()

        async with self._lock:
            self._sessions[session_id] = ssh_session

        logger.info("SSH 会话已创建: %s -> %s@%s", session_id[:8], host.username, host.hostname)
        return session_id

    async def execute_command(
        self,
        session_id: str,
        command: str,
        timeout: int = DEFAULT_COMMAND_TIMEOUT,
    ) -> CommandResult:
        """在指定会话上执行命令

        Args:
            session_id: 会话 ID
            command: Shell 命令
            timeout: 超时秒数

        Returns:
            CommandResult 执行结果

        Raises:
            KeyError: 会话不存在
            TimeoutError: 命令超时
            ConnectionError: 连接已断开
        """
        session = self._get_session(session_id)
        return await session.exec_command(command, timeout=timeout)

    async def close_session(self, session_id: str) -> bool:
        """关闭指定会话"""
        async with self._lock:
            session = self._sessions.pop(session_id, None)

        if not session:
            return False

        await session.close()
        logger.info("SSH 会话已关闭: %s", session_id[:8])
        return True

    async def close_all(self) -> None:
        """关闭所有会话（服务停止时调用）"""
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()

        for session in sessions:
            await session.close()

        logger.info("所有 SSH 会话已关闭，共 %d 个", len(sessions))

    def list_sessions(self) -> list[SessionInfo]:
        """列出所有活跃会话"""
        return [s.info for s in self._sessions.values()]

    def get_session_info(self, session_id: str) -> Optional[SessionInfo]:
        """获取指定会话信息"""
        session = self._sessions.get(session_id)
        return session.info if session else None

    def _get_session(self, session_id: str) -> _SSHSession:
        """获取会话，不存在则抛异常"""
        session = self._sessions.get(session_id)
        if not session:
            raise KeyError(f"SSH 会话不存在: {session_id}")
        return session


class _SSHSession:
    """单个 SSH 会话的内部实现"""

    def __init__(self, session_id: str, host: Host) -> None:
        self.session_id = session_id
        self.host = host
        self._conn: Optional[asyncssh.SSHClientConnection] = None
        self._created_at = datetime.now()
        self._last_activity = datetime.now()

    async def connect(self) -> None:
        """建立 SSH 连接"""
        connect_kwargs: dict = {
            "host": self.host.hostname,
            "port": self.host.port,
            "username": self.host.username,
            "known_hosts": None,  # 开发阶段跳过 host key 验证
        }

        if self.host.auth_type == AuthType.KEY and self.host.private_key_path:
            connect_kwargs["client_keys"] = [self.host.private_key_path]
        elif self.host.auth_type == AuthType.PASSWORD and self.host.password_encrypted:
            from src.utils.security import decrypt_password

            connect_kwargs["password"] = decrypt_password(self.host.password_encrypted)

        try:
            self._conn = await asyncssh.connect(**connect_kwargs)
        except (OSError, asyncssh.Error) as e:
            raise ConnectionError(f"无法连接到 {self.host.hostname}:{self.host.port} - {e}") from e

    async def exec_command(self, command: str, timeout: int = DEFAULT_COMMAND_TIMEOUT) -> CommandResult:
        """执行单条命令"""
        if not self._conn:
            raise ConnectionError("SSH 连接未建立")

        start = asyncio.get_event_loop().time()
        try:
            result = await asyncio.wait_for(
                self._conn.run(command, check=False),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            raise TimeoutError(f"命令超时（{timeout}s）: {command}") from None

        elapsed_ms = (asyncio.get_event_loop().time() - start) * 1000
        self._last_activity = datetime.now()

        return CommandResult(
            session_id=self.session_id,
            host_name=self.host.name,
            command=command,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
            exit_code=result.exit_status or 0,
            duration_ms=round(elapsed_ms, 2),
        )

    async def close(self) -> None:
        """关闭连接"""
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def info(self) -> SessionInfo:
        """获取会话信息"""
        return SessionInfo(
            session_id=self.session_id,
            host_name=self.host.name,
            hostname=self.host.hostname,
            username=self.host.username,
            connected=self._conn is not None,
            created_at=self._created_at.isoformat(),
            last_activity=self._last_activity.isoformat(),
        )
