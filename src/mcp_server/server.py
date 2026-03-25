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
from src.models.host import Host, HostType
from src.services.event_service import AgentEvent, EventType, event_bus
from src.services.host_manager import HostManager
from src.services.jump_orchestrator import JumpOrchestrator
from src.services.pty_session import PTYSessionManager
from src.services.tmux_manager import TmuxWindowManager
from src.services.wetty_manager import WeTTYManager

logger = logging.getLogger(__name__)

# ── 全局引用（通过 init_mcp_server 注入，避免循环依赖）──
_wetty_manager: WeTTYManager | None = None
_pty_manager: PTYSessionManager | None = None
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


def init_mcp_server(wetty_manager: WeTTYManager, tmux_manager: TmuxWindowManager | None = None) -> None:
    """初始化 MCP Server 的依赖（由 main.py 在启动时调用）

    Args:
        wetty_manager: WeTTY 实例管理器
        tmux_manager: tmux 窗口管理器（可选，不传则内部创建）
    """
    global _wetty_manager, _pty_manager, _tmux_manager
    _wetty_manager = wetty_manager
    _pty_manager = PTYSessionManager()
    _tmux_manager = tmux_manager or TmuxWindowManager()
    logger.info("MCP Server 依赖注入完成（PTY 交互式模式 + tmux 多窗口）")


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
    """直连主机 / 堡垒机的连接流程（启动 WeTTY + PTY）"""
    wetty_mgr = _get_wetty_manager()
    pty_mgr = _get_pty_manager()

    is_attach_mode = wetty_mgr.has_running_instance(host.name)

    # 启动 WeTTY 实例（已有则复用）
    try:
        instance = await wetty_mgr.start_instance(host)
    except Exception as e:
        return f"错误：WeTTY 启动失败 - {e}"

    # 等待 WeTTY 进程就绪
    if is_attach_mode:
        await asyncio.sleep(0.5)
        logger.info("connect_host: attach 模式 — WeTTY 实例已存在 (%s)", host.name)
    else:
        await asyncio.sleep(2.0)
        logger.info("connect_host: new 模式 — 新 WeTTY 实例 (%s)", host.name)

    # 建立 PTY socket.io 连接
    base_path = f"/wetty/t/{host.name}"
    try:
        pty_session = await pty_mgr.create_session(
            host_name=host.name,
            wetty_port=instance.port,
            wetty_base_path=base_path,
            connect_timeout=15.0,
        )
    except ConnectionError as e:
        return f"错误：PTY 连接失败 - {e}"

    mode_label = "attach（共享浏览器终端）" if is_attach_mode else "new（新建终端）"
    await _publish_event("session_created", pty_session.session_id, host.name, {
        "hostname": host.hostname,
        "username": host.username,
        "mode": "pty",
        "tmux_mode": "attach" if is_attach_mode else "new",
    })

    return (
        f"已连接到 {host.username}@{host.hostname}:{host.port}"
        f"（PTY 交互式模式 — {mode_label}）\n"
        f"Session ID: {pty_session.session_id}\n"
        f"终端已在浏览器中实时显示。你的所有操作浏览器可实时看到。\n\n"
        f"提示：\n"
        f"- 如果是堡垒机，请用 send_input 输入目标主机 IP\n"
        f"- 用 wait_for_output 等待特定文本出现\n"
        f"- 用 run_command 执行命令并获取输出\n"
        f"- 用 read_terminal 查看当前终端屏幕"
    )


async def _connect_jump_host(jump_host: Host, bastion: Host) -> str:
    """二级主机的连接流程（复用堡垒机 WeTTY + tmux 新窗口 + 跳板编排）"""
    wetty_mgr = _get_wetty_manager()
    pty_mgr = _get_pty_manager()
    tmux_mgr = _get_tmux_manager()

    # Step 1: 确保堡垒机 WeTTY 实例运行中（已有则复用）
    is_new_bastion = not wetty_mgr.has_running_instance(bastion.name)
    try:
        instance = await wetty_mgr.start_instance(bastion)
    except Exception as e:
        return f"错误：堡垒机 WeTTY 启动失败 - {e}"

    if is_new_bastion:
        await asyncio.sleep(2.0)
        logger.info("connect_jump_host: 堡垒机 WeTTY 新启动 (%s)", bastion.name)
    else:
        await asyncio.sleep(0.5)
        logger.info("connect_jump_host: 堡垒机 WeTTY 已存在，复用 (%s)", bastion.name)

    # Step 2: 在堡垒机 tmux session 中创建新窗口
    tmux_session = TmuxWindowManager.session_name_for(bastion.name)
    window_name = jump_host.name

    if not await tmux_mgr.create_window(tmux_session, window_name):
        return f"错误：tmux 窗口创建失败 ({tmux_session}:{window_name})"

    # Step 3: 建立 PTY 连接（连接到堡垒机 WeTTY，绑定 tmux 窗口）
    base_path = f"/wetty/t/{bastion.name}"
    try:
        pty_session = await pty_mgr.create_session(
            host_name=jump_host.name,
            wetty_port=instance.port,
            wetty_base_path=base_path,
            connect_timeout=15.0,
            tmux_window=window_name,
        )
    except ConnectionError as e:
        return f"错误：PTY 连接失败 - {e}"

    # Step 4: 执行跳板编排
    orchestrator = JumpOrchestrator(pty_session)
    result = await orchestrator.execute_jump(
        jump_host=jump_host,
        bastion=bastion,
        tmux_session_name=tmux_session,
        window_name=window_name,
    )

    if not result.success:
        await _publish_event("session_error", pty_session.session_id, jump_host.name, {
            "error": result.message,
        })
        return f"错误：跳板连接失败 - {result.message}"

    await _publish_event("session_created", pty_session.session_id, jump_host.name, {
        "hostname": jump_host.target_ip,
        "bastion": bastion.name,
        "mode": "pty",
        "tmux_mode": "jump",
        "tmux_window": window_name,
        "steps_executed": result.steps_executed,
    })

    msg = (
        f"已通过堡垒机 {bastion.name} 连接到 {jump_host.name}"
        f"（{jump_host.target_ip}）\n"
        f"Session ID: {pty_session.session_id}\n"
        f"{result.message}\n"
        f"终端已在浏览器中实时显示（tmux 窗口: {window_name}）。\n\n"
        f"提示：\n"
        f"- 用 run_command 执行命令并获取输出\n"
        f"- 用 list_windows 查看当前堡垒机的所有窗口\n"
        f"- 用 switch_window 在不同二级主机之间切换"
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
