"""MCP Server - AI Agent 工具接口

基于 FastMCP 实现 Agent 工具，支持两种模式：
1. PTY 交互式模式（默认）：通过 WeTTY socket.io 共享终端，支持堡垒机交互，浏览器实时回显
2. exec 直连模式（备用）：通过 asyncssh 直接执行命令，适合可直连的目标主机

PTY 模式工具（堡垒机 + 浏览器回显）：
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
from src.services.event_service import AgentEvent, EventType, event_bus
from src.services.host_manager import HostManager
from src.services.pty_session import PTYSessionManager
from src.services.wetty_manager import WeTTYManager

logger = logging.getLogger(__name__)

# ── 全局引用（通过 init_mcp_server 注入，避免循环依赖）──
_wetty_manager: WeTTYManager | None = None
_pty_manager: PTYSessionManager | None = None

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


def init_mcp_server(wetty_manager: WeTTYManager) -> None:
    """初始化 MCP Server 的依赖（由 main.py 在启动时调用）"""
    global _wetty_manager, _pty_manager
    _wetty_manager = wetty_manager
    _pty_manager = PTYSessionManager()
    logger.info("MCP Server 依赖注入完成（PTY 交互式模式）")


def get_pty_manager() -> PTYSessionManager | None:
    """获取 PTY 会话管理器实例（供外部模块使用，如 lifespan 清理）"""
    return _pty_manager


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


def _get_wetty_manager() -> WeTTYManager:
    """获取 WeTTY 管理器实例"""
    if _wetty_manager is None:
        raise RuntimeError("MCP Server 尚未初始化，请先调用 init_mcp_server()")
    return _wetty_manager


def _get_pty_manager() -> PTYSessionManager:
    """获取 PTY 会话管理器实例"""
    if _pty_manager is None:
        raise RuntimeError("MCP Server 尚未初始化，请先调用 init_mcp_server()")
    return _pty_manager


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


# ── MCP 工具定义 ──────────────────────────────


@mcp.tool()
async def list_hosts(tag: Optional[str] = None) -> str:
    """列出所有可用的 SSH 主机

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
        tags = [t.strip() for t in h.tags.split(",") if t.strip()] if h.tags else []
        result.append({
            "id": h.id,
            "name": h.name,
            "hostname": h.hostname,
            "port": h.port,
            "username": h.username,
            "description": h.description,
            "tags": tags,
        })

    if not result:
        return "没有找到可用的主机。请先通过管理界面添加主机。"

    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def connect_host(host_name: str) -> str:
    """连接到指定的 SSH 主机

    自动启动 WeTTY 终端实例并建立 PTY 交互式会话。
    如果浏览器已连接（WeTTY 实例已运行），则直接 attach 到同一个 tmux 会话，
    浏览器和 Agent 共享终端，互相可见。

    Args:
        host_name: 主机名称（在 list_hosts 中查看）

    Returns:
        连接结果，包含 session_id 供后续命令使用
    """
    # 查找主机
    async with async_session_factory() as session:
        mgr = HostManager(session)
        host = await mgr.get_host_by_name(host_name)

    if not host:
        return f"错误：未找到名为 '{host_name}' 的主机。请先用 list_hosts 查看可用主机。"

    wetty_mgr = _get_wetty_manager()
    pty_mgr = _get_pty_manager()

    # 检测是否已有运行中的 WeTTY 实例（浏览器可能已连接）
    is_attach_mode = wetty_mgr.has_running_instance(host_name)

    # Step 1: 启动 WeTTY 实例（已有则复用）
    try:
        instance = await wetty_mgr.start_instance(host)
    except Exception as e:
        return f"错误：WeTTY 启动失败 - {e}"

    # Step 2: 等待 WeTTY 进程就绪
    if is_attach_mode:
        # attach 模式：WeTTY 实例已运行，tmux 会话已存在，短暂等待即可
        await asyncio.sleep(0.5)
        logger.info("connect_host: attach 模式 — WeTTY 实例已存在，tmux 会话 attach")
    else:
        # new 模式：新启动的 WeTTY 实例，需要等待进程启动 + SSH 建立
        await asyncio.sleep(2.0)
        logger.info("connect_host: new 模式 — 新 WeTTY 实例，等待 SSH 建立")

    # Step 3: 建立 PTY socket.io 连接
    base_path = f"/wetty/t/{host_name}"
    try:
        pty_session = await pty_mgr.create_session(
            host_name=host_name,
            wetty_port=instance.port,
            wetty_base_path=base_path,
            connect_timeout=15.0,
        )
    except ConnectionError as e:
        return f"错误：PTY 连接失败 - {e}"

    mode_label = "attach（共享浏览器终端）" if is_attach_mode else "new（新建终端）"
    await _publish_event("session_created", pty_session.session_id, host_name, {
        "hostname": host.hostname,
        "username": host.username,
        "mode": "pty",
        "tmux_mode": "attach" if is_attach_mode else "new",
    })

    return (
        f"已连接到 {host.username}@{host.hostname}:{host.port}（PTY 交互式模式 — {mode_label}）\n"
        f"Session ID: {pty_session.session_id}\n"
        f"终端已在浏览器中实时显示。你的所有操作浏览器可实时看到。\n\n"
        f"提示：\n"
        f"- 如果是堡垒机，请用 send_input 输入目标主机 IP\n"
        f"- 用 wait_for_output 等待特定文本出现\n"
        f"- 用 run_command 执行命令并获取输出\n"
        f"- 用 read_terminal 查看当前终端屏幕"
    )


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

    pty_mgr = _get_pty_manager()
    session = pty_mgr.get_session(session_id)
    if not session:
        return f"错误：会话 {session_id} 不存在。请先用 connect_host 建立连接。"

    if not session.connected:
        return f"错误：会话 {session_id} 已断开。请重新用 connect_host 连接。"

    await _publish_event("command_start", session_id, session.host_name, {"command": command})

    try:
        output = await session.send_command(
            command=command,
            # 匹配常见 shell 提示符：$, #, >, % 或 [user@host ~]# 中的 ]#
            wait_pattern=r"(?:[\$#>%])\s*$",
            timeout=float(timeout),
        )
    except TimeoutError as e:
        await _publish_event("command_error", session_id, session.host_name, {
            "error": f"命令超时（{timeout}s）",
        })
        return f"错误：{e}"
    except ConnectionError as e:
        await _publish_event("command_error", session_id, session.host_name, {
            "error": str(e),
        })
        return f"错误：连接异常 - {e}"

    await _publish_event("command_complete", session_id, session.host_name, {
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
    pty_mgr = _get_pty_manager()
    session = pty_mgr.get_session(session_id)
    if not session:
        return f"错误：会话 {session_id} 不存在。"

    if not session.connected:
        return f"错误：会话 {session_id} 已断开。"

    await _publish_event("command_start", session_id, session.host_name, {
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
    pty_mgr = _get_pty_manager()
    session = pty_mgr.get_session(session_id)
    if not session:
        return f"错误：会话 {session_id} 不存在。"

    if not session.connected:
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
    pty_mgr = _get_pty_manager()
    session = pty_mgr.get_session(session_id)
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
    pty_mgr = _get_pty_manager()

    if session_id:
        session = pty_mgr.get_session(session_id)
        if not session:
            return f"会话不存在: {session_id}"
        info = session.info
        return json.dumps({
            "session_id": info.session_id,
            "host_name": info.host_name,
            "connected": info.connected,
            "mode": "pty",
            "wetty_port": info.wetty_port,
            "screen_lines": info.screen_lines,
            "created_at": info.created_at,
            "last_activity": info.last_activity,
        }, ensure_ascii=False, indent=2)

    sessions = pty_mgr.list_sessions()
    if not sessions:
        return "当前没有活跃的会话。"

    result = []
    for s in sessions:
        result.append({
            "session_id": s.session_id,
            "host_name": s.host_name,
            "connected": s.connected,
            "mode": "pty",
            "last_activity": s.last_activity,
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
    pty_mgr = _get_pty_manager()

    session = pty_mgr.get_session(session_id)
    if not session:
        return f"会话不存在: {session_id}"

    host_name = session.host_name
    closed = await pty_mgr.close_session(session_id)

    if closed:
        await _publish_event("session_closed", session_id, host_name)
        return f"已断开与 {host_name} 的 PTY 连接。Session: {session_id[:8]}..."
    else:
        return f"断开失败：会话 {session_id} 不存在。"
