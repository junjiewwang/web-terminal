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

import json
import logging
import re

from mcp.server.fastmcp import FastMCP

from src.models.database import async_session_factory
from src.models.host import Host, HostResponse
from src.services.event_service import AgentEvent, EventType, event_bus
from src.services.host_manager import HostManager
from src.services.jump_orchestrator import ConnectionOrchestrator
from src.services.terminal_manager import TerminalManager
from src.services.tmux_manager import TmuxWindowManager

logger = logging.getLogger(__name__)

JsonDict = dict[str, object]

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
_BLOCKED_COMMANDS: list[re.Pattern[str]] = [
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


async def _publish_event(event_type: str, session_id: str, host_name: str, data: JsonDict | None = None) -> None:
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
async def list_hosts(tag: str | None = None) -> str:
    """列出所有可用的 SSH 主机

    返回递归树结构：任意节点都可以继续包含 children，
    适用于 root -> nested -> nested 的多跳链路。
    """
    async with async_session_factory() as session:
        mgr = HostManager(session)
        hosts = await mgr.list_host_responses(tag=tag)

    if not hosts:
        return "没有找到可用的主机。请先通过管理界面添加主机。"

    def _to_dict(host: HostResponse) -> JsonDict:
        entry_data: JsonDict = host.entry.model_dump(exclude_none=True)
        return {
            "id": host.id,
            "name": host.name,
            "hostname": host.hostname,
            "port": host.port,
            "username": host.username,
            "description": host.description,
            "tags": host.tags,
            "type": host.host_type.value,
            "entry": entry_data,
            "children": [_to_dict(child) for child in host.children],
        }

    return json.dumps([_to_dict(host) for host in hosts], ensure_ascii=False, indent=2)


@mcp.tool()
async def connect_host(host_name: str) -> str:
    """连接到指定的 SSH 主机节点。"""
    async with async_session_factory() as session:
        mgr = HostManager(session)
        host = await mgr.get_host_by_name(host_name)
        if host:
            path = await mgr.get_connection_path(host)
        else:
            path = []

    if not host:
        return f"错误：未找到名为 '{host_name}' 的主机。请先用 list_hosts 查看可用主机。"

    return await _connect_path(path)


async def _connect_path(path: list[Host]) -> str:
    mgr = _get_terminal_manager()
    target = path[-1]
    root = path[0]
    instance_name = HostManager.build_instance_name(path)
    is_reusing = mgr.has_running_session(instance_name)

    password = _decrypt_host_password(root)

    try:
        session = await mgr.create_session(
            instance_name=instance_name,
            host=root,
            decrypted_password=password,
        )
    except Exception as e:
        return f"错误：终端启动失败 - {e}"

    if not is_reusing and len(path) > 1:
        orchestrator = ConnectionOrchestrator(session)  # type: ignore[arg-type]
        result = await orchestrator.execute_path(
            path=path,
            tmux_session_name=session.tmux_session_name,
            window_name="0",
            skip_window_creation=True,
        )
        if not result.success:
            await _publish_event("session_error", session.session_id, target.name, {
                "error": result.message,
                "instance_name": instance_name,
            })
            return f"错误：多跳连接失败 - {result.message}"

    mode_label = "复用已有终端" if is_reusing else "新建终端"
    await _publish_event("session_created", session.session_id, target.name, {
        "hostname": root.hostname,
        "username": root.username,
        "mode": "pty",
        "instance_name": instance_name,
        "path": [node.name for node in path],
    })

    path_text = " -> ".join(node.name for node in path)
    return (
        f"已连接到 {target.name}（{mode_label}）\n"
        f"连接路径: {path_text}\n"
        f"Session ID: {session.session_id}\n"
        f"终端已在浏览器中实时显示。\n\n"
        f"提示：\n"
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
async def get_session_status(session_id: str | None = None) -> str:
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

    result: list[JsonDict] = []
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
