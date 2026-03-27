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
from src.utils.ssh_command import build_ssh_command

logger = logging.getLogger(__name__)

# WeTTY 端口分配起始值
WETTY_BASE_PORT = 3000


async def wait_for_port(port: int, timeout: float = 10.0, interval: float = 0.2) -> None:
    """等待端口可连接（WeTTY 进程就绪探测）

    循环尝试 TCP 连接，直到端口可达或超时。
    替代固定 sleep(2.0)，WeTTY 就绪多快就返回多快。

    Args:
        port: 目标端口
        timeout: 最大等待秒数
        interval: 重试间隔秒数

    Raises:
        TimeoutError: 超时未就绪
    """
    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < timeout:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port),
                timeout=1.0,
            )
            writer.close()
            await writer.wait_closed()
            elapsed = asyncio.get_event_loop().time() - start
            logger.info("WeTTY 端口就绪: port %d (%.1fs)", port, elapsed)
            return
        except (ConnectionRefusedError, OSError, asyncio.TimeoutError):
            await asyncio.sleep(interval)

    raise TimeoutError(f"WeTTY 端口 {port} 在 {timeout}s 内未就绪")


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
        self._recycled_ports: list[int] = []  # 回收的端口（优先复用）
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
        """停止指定主机的 WeTTY 实例并回收端口"""
        async with self._lock:
            process = self._instances.pop(host_name, None)

        if not process:
            return False

        port = process.port
        await process.stop()
        # 回收端口供后续复用
        async with self._lock:
            self._recycled_ports.append(port)
        logger.info("WeTTY 实例已停止: %s (端口 %d 已回收)", host_name, port)
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

    async def cleanup_zombie_sessions(self) -> int:
        """清理 zombie tmux session（没有对应 WeTTY 实例的残留 session）

        扫描所有 wetty- 前缀的 tmux session，与当前活跃的 WeTTY 实例对比，
        清理没有对应活跃实例的 session。

        Returns:
            清理的 session 数量
        """
        # 获取所有 tmux session
        proc = await asyncio.create_subprocess_exec(
            "tmux", "ls", "-F", "#{session_name}:#{session_attached}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()

        if proc.returncode != 0 or not stdout:
            return 0

        # 当前活跃实例的 tmux session 名集合
        active_sessions = set()
        async with self._lock:
            for process in self._instances.values():
                if process.is_running:
                    active_sessions.add(process.tmux_session_name)

        cleaned = 0
        for line in stdout.decode().strip().split("\n"):
            if not line.strip():
                continue
            parts = line.strip().split(":")
            if len(parts) < 2:
                continue
            session_name = parts[0]
            attached = int(parts[1]) if parts[1].isdigit() else 0

            # 只清理 wetty- 前缀且无 client attached 且无对应活跃实例的 session
            if (
                session_name.startswith(_WeTTYProcess.TMUX_SESSION_PREFIX + "-")
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

        if cleaned:
            logger.info("zombie session 清理完成: 清理了 %d 个", cleaned)
        return cleaned

    def get_ssh_command(self, host_name: str) -> Optional[str]:
        """获取指定主机实例的 SSH 连接命令

        用于 jump_host 后台编排：在新 tmux 窗口中需要先 SSH 到堡垒机，
        此方法返回堡垒机的完整 SSH 命令字符串（含认证参数），
        可直接传给 tmux new-window 的 command 参数。

        Args:
            host_name: 主机名（堡垒机名称）

        Returns:
            SSH 命令字符串，实例不存在或未运行时返回 None
        """
        process = self._instances.get(host_name)
        if process and process.is_running:
            return process.ssh_command
        return None

    def get_instance(self, host_name: str) -> Optional[WeTTYInstance]:
        """获取指定主机的 WeTTY 实例信息

        Args:
            host_name: 主机名

        Returns:
            WeTTYInstance 实例信息，不存在时返回 None
        """
        process = self._instances.get(host_name)
        if process and process.is_running:
            return process.info
        return None

    async def start_instance_for_jump_host(
        self,
        instance_name: str,
        bastion: Host,
    ) -> WeTTYInstance:
        """为 jump_host 创建独立的 WeTTY 实例

        使用堡垒机的连接信息，但实例名使用复合名称（如 tce-server--m12）。
        这样每个 jump_host Tab 有独立的 WeTTY 实例，输入完全隔离。

        Args:
            instance_name: 实例名（复合名称，如 tce-server--m12）
            bastion: 父堡垒机 ORM 对象

        Returns:
            WeTTYInstance 实例信息
        """
        async with self._lock:
            # 复用已有实例
            if instance_name in self._instances:
                existing = self._instances[instance_name]
                if existing.is_running:
                    return existing.info
                # 已停止，清理后重建
                del self._instances[instance_name]

            port = self._allocate_port()
            process = _WeTTYProcess(
                host=bastion,  # 使用堡垒机连接信息
                port=port,
                instance_name=instance_name,  # 使用复合名称
            )
            await process.start()
            self._instances[instance_name] = process

        logger.info("WeTTY 实例已启动 (jump_host): %s -> port %d", instance_name, port)
        return process.info

    def _allocate_port(self) -> int:
        """分配下一个可用端口（优先复用回收的端口）"""
        if self._recycled_ports:
            return self._recycled_ports.pop(0)
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

    def __init__(self, host: Host, port: int, instance_name: Optional[str] = None) -> None:
        self.host = host
        self.port = port
        self._process: Optional[asyncio.subprocess.Process] = None
        # 使用传入的 instance_name（如复合名称）或默认使用 host.name
        self._effective_name = instance_name or host.name
        self._base_path = self.BASE_PATH_TEMPLATE.format(host_name=self._effective_name)
        self._tmux_session_name = f"{self.TMUX_SESSION_PREFIX}-{self._effective_name}"

    @property
    def tmux_session_name(self) -> str:
        """tmux 会话名称，供外部查询"""
        return self._tmux_session_name

    @property
    def ssh_command(self) -> str:
        """该实例对应主机的 SSH 连接命令

        复用公共 build_ssh_command()，保持与 tmux-session.sh 的 SSH 命令格式一致。
        用于 jump_host 场景：在新 tmux 窗口中需要先 SSH 到堡垒机。

        Returns:
            完整的 SSH 命令字符串（可直接在 shell 中执行）
        """
        decrypted_password = self._decrypt_host_password()
        return build_ssh_command(
            hostname=self.host.hostname,
            port=self.host.port,
            username=self.host.username,
            password=decrypted_password,
            key_path=self.host.private_key_path,
        )

    async def start(self) -> None:
        """启动 WeTTY 进程

        通过 --command 参数启动 tmux 会话脚本，而非直接 SSH。
        每个新 socket.io 连接都会触发一次脚本执行：
          - 首次连接 → tmux new-session + SSH
          - 后续连接 → tmux attach-session（共享同一个 PTY）

        关键：启动前先清理可能残留的同名 tmux session，
        确保第一个连接一定走 new-session 创建全新的 SSH，
        避免 attach 到上次的残留 session（数据泄漏/同步 Bug）。
        """
        # 验证 tmux 脚本存在
        script_path = str(self.TMUX_SCRIPT_PATH)
        if not self.TMUX_SCRIPT_PATH.exists():
            raise FileNotFoundError(
                f"tmux 会话脚本不存在: {script_path}\n"
                "请确保 scripts/tmux-session.sh 已部署。"
            )

        # 🔑 启动前清理残留 tmux session（防止 tmux-session.sh attach 到旧 session）
        await self._cleanup_tmux_session()

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
        """停止 WeTTY 进程并清理 tmux session

        关键顺序：先清理 tmux session，再停止 WeTTY 进程。
        如果先停 WeTTY，tmux client 断开但 session 可能残留，
        导致下次启动时 tmux-session.sh attach 到旧 session（数据泄漏/同步 Bug）。
        """
        # Step 1: 先清理 tmux session（确保 SSH 连接彻底断开）
        await self._cleanup_tmux_session()

        # Step 2: 再停止 WeTTY 进程
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
            self._process = None

    async def _cleanup_tmux_session(self) -> None:
        """清理 WeTTY 对应的 tmux session（异步 + 精确匹配 + 结果验证）

        使用 '=session_name' 精确匹配语法，避免 tmux 将 session_name 作为前缀子串匹配。
        例如：-t '=wetty-tce-server' 不会误匹配 'wetty-tce-server--m12'。
        """
        session_name = self._tmux_session_name
        # tmux 精确匹配语法：'=' 前缀
        exact_target = f"={session_name}"

        try:
            # 检查 session 是否存在（异步执行）
            proc = await asyncio.create_subprocess_exec(
                "tmux", "has-session", "-t", exact_target,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()

            if proc.returncode != 0:
                logger.debug("tmux session 不存在，跳过清理: %s", session_name)
                return

            # Session 存在，强制终止
            proc = await asyncio.create_subprocess_exec(
                "tmux", "kill-session", "-t", exact_target,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

            if proc.returncode != 0:
                logger.warning(
                    "tmux kill-session 失败: %s - %s",
                    session_name, stderr.decode().strip(),
                )
                return

            # 验证清理结果（二次确认）
            verify = await asyncio.create_subprocess_exec(
                "tmux", "has-session", "-t", exact_target,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await verify.wait()

            if verify.returncode == 0:
                logger.error("tmux session 清理验证失败（仍存在）: %s", session_name)
            else:
                logger.info("tmux session 已清理（已验证）: %s", session_name)

        except Exception as e:
            logger.warning("清理 tmux session 异常: %s - %s", session_name, e)

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def info(self) -> WeTTYInstance:
        return WeTTYInstance(
            host_name=self._effective_name,  # 使用 effective_name（支持复合名称）
            port=self.port,
            pid=self._process.pid if self._process else None,
            url=f"{self._base_path}/",
            running=self.is_running,
        )

    def _decrypt_host_password(self) -> Optional[str]:
        """解密主机密码，失败时记录日志并返回 None"""
        if not self.host.password_encrypted:
            return None
        try:
            from src.utils.security import decrypt_password
            return decrypt_password(self.host.password_encrypted)
        except (ValueError, Exception) as e:
            logger.warning(
                "WeTTY 密码解密失败 (%s), 将回退到交互式认证: %s",
                self.host.name, e,
            )
            return None
