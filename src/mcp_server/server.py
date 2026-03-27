"""MCP Server - AI Agent 工具接口

通过 Python PTY 直连终端，支持堡垒机交互，浏览器通过 WebSocket 实时回显。

PTY 模式工具：
  - connect_host: 连接到指定主机（自动启动 WeTTY + PTY 会话）
  - run_command: 在会话中执行命令（通过 PTY 发送 + 等待提示符）
  - send_input: 向终端发送任意输入（菜单选择、交互式命令等）
  - wait_for_output: 等待终端输出中出现指定文本
  - read_terminal: 读取当前终端屏幕内容
  - get_session_status: 查询会话状态
  - disconnect: 断开连接

  - list_hosts: 列出可用主机
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Optional

from mcp.server.fastmcp import FastMCP

from src.models.database import async_session_factory
from src.models.host import Host, HostType
from src.services.event_service import AgentEvent, EventType, event_bus
from src.services.host_manager import HostManager
from src.services.jump_orchestrator import JumpOrchestrator
from src.services.terminal_manager import TerminalManager, TerminalSession
from src.services.tmux_manager import TmuxWindowManager

logger = logging.getLogger(__name__)

# ── 全局引用（通过 init_mcp_server 注入）──
_terminal_manager: TerminalManager | None = None
_tmux_manager: TmuxWindowManager | None = None

# 创建 MCP Server 实例
mcp = FastMCP(
    name="wetty-mcp-terminal",
    # streamable_http_path="/" 避免与 FastAPI app.mount("/mcp", ...) 路径双重前缀
    streamable_http_path="/",
    instructions=(
        "你是一个 SSH 终端管理助手。你可以连接到预配置的远程主机，"
        "通过交互式终端执行命令。你的所有操作会在用户的浏览器终端中实时显示。\n\n"
        "使用流程：\n"
        "1. list_hosts 查看可用主机\n"
        "2. connect_host 建立连接（自动启动终端）\n"
        "3. 等待终端就绪后，使用 run_command 执行命令\n"
        "4. 如果是堡垒机场景，先用 send_input 输入主机IP + wait_for_output 等待跳转\n"
        "5. 完成后用 disconnect 断开"
    ),
)


def init_mcp_server(terminal_manager: TerminalManager, tmux_manager: TmuxWindowManager | None = None) -> None:
    """初始化 MCP Server 的依赖（由 main.py 在启动时调用）"""
    global _terminal_manager, _tmux_manager
    _terminal_manager = terminal_manager
    _tmux_manager = tmux_manager or TmuxWindowManager()
    logger.info("MCP Server 依赖注入完成（Python PTY 直连模式）")


def get_pty_manager() -> None:
    """兼容旧接口（不再需要独立的 PTY Manager）"""
    return None


# ── 命令安全过滤 ──────────────────────────────

# 危险命令黑名单（正则匹配命令开头）
_BLOCKED_COMMANDS: list[re.Pattern] = [
    re.compile(r"^\s*rm\s+(-[rfR]+\s+)?/\s*$"),           # rm -rf /
    re.compile(r"^\s*mkfs\b"),                               # 格式化磁盘
    re.compile(r"^\s*dd\s+.*of=/dev/"),                      # 覆写磁盘设备
    re.compile(r"^\s*:?\(\)\s*\{\s*:\|\:&\s*\}\s*;?\s*:"),  # fork bomb
    re.compile(r">\s*/dev/sd[a-z]"),                         # 重定向到磁盘设备
    re.compile(r"^\s*shutdown\b"),                            # 关机
    re.compile(r"^\s*reboot\b"),                              # 重启
    re.compile(r"^\s*init\s+0\b"),                            # 关机
    re.compile(r"^\s*halt\b"),                                # 关机
]


def _validate_command(command: str) -> str | None:
    """验证命令安全性，返回 None 表示安全，返回字符串表示拒绝原因"""
    for pattern in _BLOCKED_COMMANDS:
        if pattern.search(command):
            return f"危险命令被拦截: {command}"
    return None


# ── 内部工具函数 ──────────────────────────────


def _get_terminal_manager() -> TerminalManager:
    """获取终端管理器实例"""
    if _terminal_manager is None:
        raise RuntimeError("MCP Server 尚未初始化，请先调用 init_mcp_server()")
    return _terminal_manager


def _get_tmux_manager() -> TmuxWindowManager:
    """获取 tmux 窗口管理器实例"""
    if _tmux_manager is None:
        raise RuntimeError("MCP Server 尚未初始化，请先调用 init_mcp_server()")
    return _tmux_manager


async def _publish_event(event_type: str, session_id: str, host_name: str, data: dict | None = None) -> None:
    """发布 SSE 事件"""
    await event_bus.publish(
        AgentEvent(
            event_type=EventType(event_type),
            session_id=session_id,
            host_name=host_name,
            data=data or {},
        )
    )


def _decrypt_host_password(host: Host) -> str | None:
    """解密主机密码"""
    if not host.password_encrypted:
        return None
    try:
        from src.utils.security import decrypt_password
        return decrypt_password(host.password_encrypted)
    except Exception as e:
        logger.warning("密码解密失败 (%s): %s", host.name, e)
        return None


# ── MCP 工具定义 ──────────────────────────────


@mcp.tool()
async def list_hosts(tag: Optional[str] = None) -> str:
    """列出所有可用的 SSH 主机

    返回树形结构：bastion 类型主机会列出其下的二级主机（jump_host）。
    可直接使用主机名连接，包括二级主机名。

    Args:
        tag: 可选，按标签过滤主机列表

    Returns:
        主机列表信息（JSON 格式）
    """
    async with async_session_factory() as session:
        mgr = HostManager(session)
        hosts = await mgr.list_hosts(tag=tag)

    result = []
    for h in hosts:
        # 跳过 jump_host（它们在 bastion 的 children 中展示）
        if h.host_type == HostType.JUMP_HOST:
            continue

        tags = [t.strip() for t in h.tags.split(",") if t.strip()] if h.tags else []
        host_info: dict = {
            "id": h.id,
            "name": h.name,
            "hostname": h.hostname,
            "port": h.port,
            "username": h.username,
            "description": h.description,
            "tags": tags,
            "type": h.host_type.value,
        }

        # bastion 类型展示二级主机列表
        if h.is_bastion and h.children:
            host_info["jump_hosts"] = [
                {
                    "name": c.name,
                    "target_ip": c.target_ip,
                    "description": c.description,
                }
                for c in h.children
            ]

        result.append(host_info)

    if not result:
        return "没有找到可用的主机。请先通过管理界面添加主机。"

    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def connect_host(host_name: str) -> str:
    """连接到指定的 SSH 主机

    自动识别主机类型并执行对应连接流程：
    - direct / bastion: 启动 WeTTY 实例 + PTY 会话
    - jump_host: 复用父堡垒机的 WeTTY 实例，自动编排跳板连接

    Args:
        host_name: 主机名称（在 list_hosts 中查看，包括二级主机名）

    Returns:
        连接结果，包含 session_id 供后续命令使用
    """
    # 查找主机（jump_host 需同时获取父堡垒机）
    async with async_session_factory() as session:
        mgr = HostManager(session)
        host = await mgr.get_host_by_name(host_name)
        bastion = None
        if host and host.is_jump_host and host.parent_id:
            bastion = await mgr.get_host_by_id(host.parent_id)

    if not host:
        return f"错误：未找到名为 '{host_name}' 的主机。请先用 list_hosts 查看可用主机。"

    # 按主机类型分发
    if host.is_jump_host:
        if not bastion:
            return f"错误：二级主机 '{host_name}' 的父堡垒机不存在。请检查配置。"
        return await _connect_jump_host(host, bastion)

    return await _connect_direct_host(host)


async def _connect_direct_host(host: Host) -> str:
    """直连主机 / 堡垒机的连接流程（Python PTY 直连）"""
    mgr = _get_terminal_manager()

    is_reusing = mgr.has_running_session(host.name)

    # 解密密码
    password = _decrypt_host_password(host)

    # 创建终端会话（已有则复用）
    try:
        session = await mgr.create_session(
            instance_name=host.name,
            host=host,
            decrypted_password=password,
        )
    except Exception as e:
        return f"错误：终端启动失败 - {e}"

    # 等待终端就绪
    if not is_reusing:
        try:
            await session.wait_for(
                pattern=r"[\$#>%]\s*$|Opt>|password:|Password:",
                timeout=15.0,
            )
        except TimeoutError:
            pass  # 超时不阻断，继续使用

    mode_label = "复用已有终端" if is_reusing else "新建终端"
    await _publish_event("session_created", session.session_id, host.name, {
        "hostname": host.hostname,
        "username": host.username,
        "mode": "pty",
    })

    return (
        f"已连接到 {host.username}@{host.hostname}:{host.port}"
        f"（{mode_label}）\n"
        f"Session ID: {session.session_id}\n"
        f"终端已在浏览器中实时显示。\n\n"
        f"提示：\n"
        f"- 用 run_command 执行命令并获取输出\n"
        f"- 用 read_terminal 查看当前终端屏幕"
    )


async def _connect_jump_host(jump_host: Host, bastion: Host) -> str:
    """二级主机的连接流程（Python PTY 直连）"""
    mgr = _get_terminal_manager()
    tmux_mgr = _get_tmux_manager()

    instance_name = f"{bastion.name}--{jump_host.name}"
    is_reusing = mgr.has_running_session(instance_name)

    # 解密堡垒机密码
    password = _decrypt_host_password(bastion)

    # 创建终端会话（使用堡垒机连接信息）
    try:
        session = await mgr.create_session(
            instance_name=instance_name,
            host=bastion,
            decrypted_password=password,
        )
    except Exception as e:
        return f"错误：终端启动失败 - {e}"

    # 复用模式：检测是否已登录，跳过编排
    if is_reusing:
        tmux_session = TmuxWindowManager.session_name_for(instance_name)
        if await tmux_mgr.is_session_logged_in(tmux_session):
            logger.info("connect_jump_host: 复用已有连接 (%s)", instance_name)
            await _publish_event("session_created", session.session_id, jump_host.name, {
                "hostname": jump_host.target_ip,
                "bastion": bastion.name,
                "mode": "pty",
            })
            return (
                f"已连接到 {jump_host.name}（{jump_host.target_ip}）\n"
                f"通过堡垒机: {bastion.name}\n"
                f"Session ID: {session.session_id}\n"
                f"复用已有连接\n\n"
                f"提示：\n"
                f"- 用 run_command 执行命令并获取输出\n"
                f"- 用 read_terminal 查看当前终端屏幕"
            )

    # 新建模式：执行跳板编排
    orchestrator = JumpOrchestrator(session)  # type: ignore[arg-type]
    result = await orchestrator.execute_jump(
        jump_host=jump_host,
        bastion=bastion,
        tmux_session_name=session.tmux_session_name,
        window_name="0",
        skip_window_creation=True,
    )

    if not result.success:
        await _publish_event("session_error", session.session_id, jump_host.name, {
            "error": result.message,
        })
        return f"错误：跳板连接失败 - {result.message}"

    await _publish_event("session_created", session.session_id, jump_host.name, {
        "hostname": jump_host.target_ip,
        "bastion": bastion.name,
        "mode": "pty",
        "instance_name": instance_name,
        "steps_executed": result.steps_executed,
    })

    msg = (
        f"已连接到 {jump_host.name}（{jump_host.target_ip}）\n"
        f"通过堡垒机: {bastion.name}\n"
        f"Session ID: {session.session_id}\n"
        f"{result.message}\n\n"
        f"提示：\n"
        f"- 用 run_command 执行命令并获取输出\n"
        f"- 用 read_terminal 查看当前终端屏幕"
    )

    if result.skipped_reason:
        msg += f"\n\n注意：{result.skipped_reason}"

    return msg


@mcp.tool()
async def run_command(session_id: str, command: str, timeout: int = 30) -> str:
    """在终端中执行命令并获取输出

    通过 PTY 终端发送命令，等待命令执行完成（检测到 shell 提示符），
    返回命令输出。所有操作在浏览器终端中实时可见。

    Args:
        session_id: 会话 ID（由 connect_host 返回）
        command: 要执行的 Shell 命令
        timeout: 命令超时时间（秒），默认 30

    Returns:
        命令执行结果（纯文本，已清除 ANSI 转义序列）
    """
    # 命令安全检查
    reject_reason = _validate_command(command)
    if reject_reason:
        return f"错误：{reject_reason}"

    mgr = _get_terminal_manager()
    session = mgr.get_session_by_id(session_id)
    if not session:
        return f"错误：会话 {session_id} 不存在。请先用 connect_host 建立连接。"

    if not session.running:
        return f"错误：会话 {session_id} 已断开。请重新用 connect_host 连接。"

    await _publish_event("command_start", session_id, session.instance_name, {"command": command})

    try:
        output = await session.send_command(
            command=command,
            # 匹配常见 shell 提示符：$, #, >, % 或 [user@host ~]# 中的 ]#
            wait_pattern=r"(?:[\$#>%])\s*$",
            timeout=float(timeout),
        )
    except TimeoutError as e:
        await _publish_event("command_error", session_id, session.instance_name, {
            "error": f"命令超时（{timeout}s）",
        })
        return f"错误：{e}"
    except ConnectionError as e:
        await _publish_event("command_error", session_id, session.instance_name, {
            "error": str(e),
        })
        return f"错误：连接异常 - {e}"

    await _publish_event("command_complete", session_id, session.instance_name, {
        "command": command,
    })

    return output


@mcp.tool()
async def send_input(session_id: str, text: str) -> str:
    """向终端发送任意输入

    适用于堡垒机菜单选择、交互式命令确认、密码输入等场景。
    输入内容会在浏览器终端中实时显示。

    Args:
        session_id: 会话 ID
        text: 要发送的文本。使用 \\n 表示回车键。

    Returns:
        发送确认
    """
    mgr = _get_terminal_manager()
    session = mgr.get_session_by_id(session_id)
    if not session:
        return f"错误：会话 {session_id} 不存在。"

    if not session.running:
        return f"错误：会话 {session_id} 已断开。"

    await _publish_event("command_start", session_id, session.instance_name, {
        "command": f"[input] {text.rstrip()}"
    })

    try:
        await session.send_input(text)
    except ConnectionError as e:
        return f"错误：发送失败 - {e}"

    return f"已发送: {repr(text)}"


@mcp.tool()
async def wait_for_output(
    session_id: str,
    pattern: str,
    timeout: int = 30,
) -> str:
    """等待终端输出中出现指定文本

    类似 expect 工具，持续监控终端输出直到匹配成功或超时。
    常用于等待堡垒机菜单出现、命令提示符、特定输出等。

    Args:
        session_id: 会话 ID
        pattern: 要等待的文本（支持正则表达式）
        timeout: 超时秒数，默认 30

    Returns:
        从等待开始到匹配成功之间的所有终端输出（已清除 ANSI）
    """
    mgr = _get_terminal_manager()
    session = mgr.get_session_by_id(session_id)
    if not session:
        return f"错误：会话 {session_id} 不存在。"

    if not session.running:
        return f"错误：会话 {session_id} 已断开。"

    try:
        output = await session.wait_for(
            pattern=pattern,
            timeout=float(timeout),
        )
    except TimeoutError as e:
        return f"超时：{e}"
    except ConnectionError as e:
        return f"错误：连接异常 - {e}"

    return output


@mcp.tool()
async def read_terminal(session_id: str, lines: int = 50) -> str:
    """读取当前终端屏幕内容

    返回终端输出缓冲区中最近 N 行的内容。
    适合在不确定终端当前状态时使用。

    Args:
        session_id: 会话 ID
        lines: 读取最近多少行，默认 50

    Returns:
        终端屏幕内容（已清除 ANSI 转义序列）
    """
    mgr = _get_terminal_manager()
    session = mgr.get_session_by_id(session_id)
    if not session:
        return f"错误：会话 {session_id} 不存在。"

    screen = session.read_screen(lines=lines)
    if not screen.strip():
        return "终端屏幕为空（可能正在等待输入或尚未有输出）。"

    return screen


@mcp.tool()
async def get_session_status(session_id: Optional[str] = None) -> str:
    """查询 SSH 会话状态

    Args:
        session_id: 可选，指定会话 ID。不传则返回所有会话。

    Returns:
        会话状态信息
    """
    mgr = _get_terminal_manager()

    if session_id:
        session = mgr.get_session_by_id(session_id)
        if not session:
            return f"会话不存在: {session_id}"
        info = session.info
        return json.dumps({
            "session_id": info.session_id,
            "instance_name": info.instance_name,
            "running": info.running,
            "mode": "pty",
            "created_at": info.created_at,
            "ws_clients": info.ws_clients,
        }, ensure_ascii=False, indent=2)

    sessions = mgr.list_sessions()
    if not sessions:
        return "当前没有活跃的会话。"

    result = []
    for s in sessions:
        result.append({
            "session_id": s.session_id,
            "instance_name": s.instance_name,
            "running": s.running,
            "mode": "pty",
        })
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def disconnect(session_id: str) -> str:
    """断开会话

    关闭 PTY 连接。注意：WeTTY 终端实例不会被停止，
    浏览器仍可继续使用终端。

    Args:
        session_id: 要断开的会话 ID

    Returns:
        断开结果
    """
    mgr = _get_terminal_manager()

    session = mgr.get_session_by_id(session_id)
    if not session:
        return f"会话不存在: {session_id}"

    host_name = session.instance_name
    closed = await mgr.stop_session(session.instance_name)

    if closed:
        await _publish_event("session_closed", session_id, host_name)
        return f"已断开与 {host_name} 的 PTY 连接。Session: {session_id[:8]}..."
    else:
        return f"断开失败：会话 {session_id} 不存在。"


@mcp.tool()
async def list_windows(bastion_name: str) -> str:
    """列出堡垒机的所有 tmux 窗口

    查看堡垒机 tmux 会话中的所有窗口（包括主窗口和二级主机窗口），
    当前活跃的窗口会标注 [active]。

    Args:
        bastion_name: 堡垒机名称

    Returns:
        窗口列表信息（JSON 格式）
    """
    tmux_mgr = _get_tmux_manager()
    tmux_session = TmuxWindowManager.session_name_for(bastion_name)

    if not await tmux_mgr.session_exists(tmux_session):
        return f"错误：堡垒机 '{bastion_name}' 的 tmux 会话不存在。请先连接堡垒机。"

    windows = await tmux_mgr.list_windows(tmux_session)
    if not windows:
        return f"堡垒机 '{bastion_name}' 当前没有打开的窗口。"

    result = [
        {
            "index": w.window_index,
            "name": w.window_name,
            "active": w.active,
        }
        for w in windows
    ]

    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def switch_window(bastion_name: str, window_name: str) -> str:
    """切换堡垒机的活跃 tmux 窗口

    在堡垒机的不同二级主机之间切换。切换后浏览器终端会实时显示
    目标窗口的内容，Agent 后续命令也作用于新的活跃窗口。

    Args:
        bastion_name: 堡垒机名称
        window_name: 目标窗口名（如二级主机名 m12、m15）

    Returns:
        切换结果
    """
    tmux_mgr = _get_tmux_manager()
    tmux_session = TmuxWindowManager.session_name_for(bastion_name)

    if not await tmux_mgr.session_exists(tmux_session):
        return f"错误：堡垒机 '{bastion_name}' 的 tmux 会话不存在。请先连接堡垒机。"

    success = await tmux_mgr.select_window(tmux_session, window_name)
    if not success:
        # 提供可用窗口列表辅助诊断
        windows = await tmux_mgr.list_windows(tmux_session)
        available = ", ".join(w.window_name for w in windows)
        return (
            f"错误：切换窗口失败。窗口 '{window_name}' 可能不存在。\n"
            f"可用窗口: {available or '无'}"
        )

    await _publish_event("window_switched", "", bastion_name, {
        "window_name": window_name,
        "tmux_session": tmux_session,
    })

    return f"已切换到窗口 '{window_name}'（{tmux_session}:{window_name}）"
