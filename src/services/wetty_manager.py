"""WeTTY 实例管理服务

管理多个 WeTTY 进程实例，实现多主机的 Web Terminal 动态切换。
每个主机对应一个独立的 WeTTY 实例，监听不同端口。

路由架构：
  - 每个 WeTTY 实例启动时通过 --base 参数指定唯一前缀路径
  - WeTTY 内部资源(HTML/CSS/JS/socket.io) 全部挂载在该前缀下
  - FastAPI 反代 /wetty/t/{host_name}/ → 127.0.0.1:{port}/wetty/t/{host_name}/
  - 前端 iframe src 直接使用反代后的 URL
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
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
    """单个 WeTTY 进程封装"""

    # WeTTY 实例的 base path 前缀模板
    BASE_PATH_TEMPLATE = "/wetty/t/{host_name}"

    def __init__(self, host: Host, port: int) -> None:
        self.host = host
        self.port = port
        self._process: Optional[asyncio.subprocess.Process] = None
        self._base_path = self.BASE_PATH_TEMPLATE.format(host_name=host.name)

    async def start(self) -> None:
        """启动 WeTTY 进程

        通过 --base 参数为每个主机设置唯一的路径前缀，
        使 WeTTY 内部资源全部挂载在该前缀下。

        认证策略：
          - auth_type=key   → --ssh-key + --ssh-auth publickey
          - auth_type=password → --ssh-pass + --ssh-auth password
        """
        cmd = [
            "wetty",
            "--host", "0.0.0.0",
            "--port", str(self.port),
            "--base", self._base_path,
            "--allow-iframe",
            "--ssh-host", self.host.hostname,
            "--ssh-port", str(self.host.port),
            "--ssh-user", self.host.username,
        ]

        # 根据认证方式设置参数
        if self.host.auth_type == AuthType.PASSWORD and self.host.password_encrypted:
            password = self._decrypt_host_password()
            if password:
                cmd.extend([
                    "--ssh-auth", "password",
                    "--ssh-pass", password,
                ])
        elif self.host.private_key_path:
            cmd.extend([
                "--ssh-auth", "publickey",
                "--ssh-key", self.host.private_key_path,
            ])

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            logger.info(
                "WeTTY 进程启动: PID=%s, port=%d, base=%s",
                self._process.pid, self.port, self._base_path,
            )
        except FileNotFoundError:
            logger.error("wetty 命令未找到，请先安装: npm install -g wetty")
            raise
        except OSError as e:
            logger.error("WeTTY 启动失败: %s", e)
            raise

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
