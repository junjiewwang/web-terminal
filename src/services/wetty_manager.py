"""WeTTY 实例管理服务

管理多个 WeTTY 进程实例，实现多主机的 Web Terminal 动态切换。
每个主机对应一个独立的 WeTTY 实例，监听不同端口。

路由架构：
  - 每个 WeTTY 实例启动时通过 --base 参数指定唯一前缀路径
  - WeTTY 内部资源(HTML/CSS/JS/socket.io) 全部挂载在该前缀下
  - FastAPI 反代 /wetty/t/{host_name}/ → 127.0.0.1:{port}/wetty/t/{host_name}/
  - 前端 iframe src 直接使用反代后的 URL

tmux 会话共享：
  - WeTTY 使用 --command 参数调用 tmux-session.sh 脚本
  - 首个连接创建 tmux new-session + SSH，后续连接 tmux attach
  - 浏览器和 MCP Agent 共享同一个 SSH PTY，实时同步可见
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.models.host import AuthType, Host

logger = logging.getLogger(__name__)

# WeTTY 端口分配起始值
WETTY_BASE_PORT = 3000


@dataclass
class WeTTYInstance:
    """WeTTY 实例信息"""

    host_name: str
    port: int
    pid: Optional[int]
    url: str
    running: bool


class WeTTYManager:
    """WeTTY 实例管理器

    为每个主机启动独立的 WeTTY 进程，
    通过 --base 参数设置唯一路径前缀，
    由 FastAPI 反代实现多主机 Web Terminal。
    """

    def __init__(self, base_port: int = WETTY_BASE_PORT) -> None:
        self._base_port = base_port
        self._instances: dict[str, _WeTTYProcess] = {}
        self._port_counter = base_port
        self._lock = asyncio.Lock()

    def get_instance_port(self, host_name: str) -> Optional[int]:
        """根据主机名获取 WeTTY 实例的内部端口

        用于反代路由查询目标端口。

        Returns:
            端口号，如果实例不存在或未运行则返回 None
        """
        process = self._instances.get(host_name)
        if process and process.is_running:
            return process.port
        return None

    def has_running_instance(self, host_name: str) -> bool:
        """检测指定主机是否有运行中的 WeTTY 实例

        用于判断 MCP connect_host 时应该是 "new" 还是 "attach" 模式：
          - False → 首次连接，start_instance 后需要等待 SSH 建立
          - True  → 已有实例（浏览器已连接），直接 attach 即可

        Args:
            host_name: 主机名

        Returns:
            True 如果有运行中的实例
        """
        process = self._instances.get(host_name)
        return process is not None and process.is_running

    async def start_instance(self, host: Host) -> WeTTYInstance:
        """为指定主机启动 WeTTY 实例

        如果该主机已有运行中的实例，直接返回。

        Args:
            host: 主机 ORM 对象

        Returns:
            WeTTYInstance 实例信息
        """
        async with self._lock:
            # 复用已有实例
            if host.name in self._instances:
                existing = self._instances[host.name]
                if existing.is_running:
                    return existing.info
                # 已停止，清理后重建
                del self._instances[host.name]

            port = self._allocate_port()
            process = _WeTTYProcess(host=host, port=port)
            await process.start()
            self._instances[host.name] = process

        logger.info("WeTTY 实例已启动: %s -> port %d", host.name, port)
        return process.info

    async def stop_instance(self, host_name: str) -> bool:
        """停止指定主机的 WeTTY 实例"""
        async with self._lock:
            process = self._instances.pop(host_name, None)

        if not process:
            return False

        await process.stop()
        logger.info("WeTTY 实例已停止: %s", host_name)
        return True

    async def stop_all(self) -> None:
        """停止所有 WeTTY 实例"""
        async with self._lock:
            processes = list(self._instances.values())
            self._instances.clear()

        for p in processes:
            await p.stop()

        logger.info("所有 WeTTY 实例已停止")

    def list_instances(self) -> list[WeTTYInstance]:
        """列出所有 WeTTY 实例"""
        return [p.info for p in self._instances.values()]

    def _allocate_port(self) -> int:
        """分配下一个可用端口"""
        port = self._port_counter
        self._port_counter += 1
        return port


class _WeTTYProcess:
    """单个 WeTTY 进程封装

    通过 WeTTY --command 参数调用 tmux-session.sh 脚本，
    实现多客户端（浏览器 + MCP Agent）共享同一个 SSH PTY。
    """

    # WeTTY 实例的 base path 前缀模板
    BASE_PATH_TEMPLATE = "/wetty/t/{host_name}"

    # tmux 会话脚本路径（相对于项目根目录）
    # Docker 容器内为 /app/scripts/tmux-session.sh
    TMUX_SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "tmux-session.sh"

    # tmux 会话名前缀
    TMUX_SESSION_PREFIX = "wetty"

    def __init__(self, host: Host, port: int) -> None:
        self.host = host
        self.port = port
        self._process: Optional[asyncio.subprocess.Process] = None
        self._base_path = self.BASE_PATH_TEMPLATE.format(host_name=host.name)
        self._tmux_session_name = f"{self.TMUX_SESSION_PREFIX}-{host.name}"

    @property
    def tmux_session_name(self) -> str:
        """tmux 会话名称，供外部查询"""
        return self._tmux_session_name

    async def start(self) -> None:
        """启动 WeTTY 进程

        通过 --command 参数启动 tmux 会话脚本，而非直接 SSH。
        每个新 socket.io 连接都会触发一次脚本执行：
          - 首次连接 → tmux new-session + SSH
          - 后续连接 → tmux attach-session（共享同一个 PTY）

        认证信息通过脚本参数传递给 tmux-session.sh，
        由脚本内部构造 sshpass/ssh 命令。
        """
        # 验证 tmux 脚本存在
        script_path = str(self.TMUX_SCRIPT_PATH)
        if not self.TMUX_SCRIPT_PATH.exists():
            raise FileNotFoundError(
                f"tmux 会话脚本不存在: {script_path}\n"
                "请确保 scripts/tmux-session.sh 已部署。"
            )

        # 构建 tmux-session.sh 的参数列表
        script_args = self._build_tmux_script_args()

        # 构建 --command 参数值
        # WeTTY 会对每个新 socket.io 连接执行此命令
        command_value = f"{script_path} {script_args}"

        cmd = [
            "wetty",
            "--host", "0.0.0.0",
            "--port", str(self.port),
            "--base", self._base_path,
            "--allow-iframe",
            "--command", command_value,
        ]

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            logger.info(
                "WeTTY 进程启动 (tmux 模式): PID=%s, port=%d, base=%s, session=%s",
                self._process.pid, self.port, self._base_path, self._tmux_session_name,
            )
        except FileNotFoundError:
            logger.error("wetty 命令未找到，请先安装: npm install -g wetty")
            raise
        except OSError as e:
            logger.error("WeTTY 启动失败: %s", e)
            raise

    def _build_tmux_script_args(self) -> str:
        """构建 tmux-session.sh 的参数字符串

        参数顺序: <session_name> <ssh_host> <ssh_port> <ssh_user> [password] [key_path]

        Returns:
            用空格拼接的参数字符串（密码和密钥路径可选）
        """
        args = [
            self._tmux_session_name,
            self.host.hostname,
            str(self.host.port),
            self.host.username,
        ]

        # 密码参数（第5个位置参数）
        password = ""
        if self.host.auth_type == AuthType.PASSWORD and self.host.password_encrypted:
            decrypted = self._decrypt_host_password()
            if decrypted:
                password = decrypted

        # 密钥路径参数（第6个位置参数）
        key_path = ""
        if self.host.private_key_path:
            key_path = self.host.private_key_path

        args.append(password)
        args.append(key_path)

        return " ".join(args)

    async def stop(self) -> None:
        """停止 WeTTY 进程"""
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
            self._process = None

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def info(self) -> WeTTYInstance:
        return WeTTYInstance(
            host_name=self.host.name,
            port=self.port,
            pid=self._process.pid if self._process else None,
            url=f"{self._base_path}/",
            running=self.is_running,
        )

    def _decrypt_host_password(self) -> Optional[str]:
        """解密主机密码，失败时记录日志并返回 None"""
        try:
            from src.utils.security import decrypt_password
            return decrypt_password(self.host.password_encrypted)
        except (ValueError, Exception) as e:
            logger.warning(
                "WeTTY 密码解密失败 (%s), 将回退到交互式认证: %s",
                self.host.name, e,
            )
            return None
