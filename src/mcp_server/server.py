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

文件传输工具（基于 PTY 通道，支持多跳 SSH）：
  - upload_file: 上传本地文件到远端节点
  - download_file: 从远端节点下载文件到本地
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from src.models.database import async_session_factory
from src.models.host import Host, HostResponse
from src.services.event_service import AgentEvent, EventType, event_bus
from src.services.host_manager import HostManager
from src.services.jump_orchestrator import ConnectionOrchestrator
from src.services.pty_file_transfer import PtyFileTransfer, TransferResult
from src.services.snippet_registry import SnippetRegistry
from src.services.terminal_manager import TerminalManager
from src.services.tmux_manager import TmuxWindowManager

if TYPE_CHECKING:
    from src.services.terminal_manager import TerminalSession

logger = logging.getLogger(__name__)

JsonDict = dict[str, object]

# ── 全局引用（通过 init_mcp_server 注入）──
_terminal_manager: TerminalManager | None = None
_tmux_manager: TmuxWindowManager | None = None
_snippet_registry: SnippetRegistry | None = None

# 创建 MCP Server 实例
# DNS rebinding 保护已关闭：MCP 端点需要公开给外部 Agent 访问，
# 安全性由调用方 Header 鉴权保障（如 Bearer Token），无需限制 Host。
mcp = FastMCP(
    name="wetty-mcp-terminal",
    # streamable_http_path="/" 避免与 FastAPI app.mount("/mcp", ...) 路径双重前缀
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
    instructions=(
        "你是一个 SSH 终端管理助手。你可以连接到预配置的远程主机，"
        "通过交互式终端执行命令。你的所有操作会在用户的浏览器终端中实时显示。\n\n"
        "使用流程：\n"
        "1. list_hosts 查看可用主机\n"
        "2. connect_host 建立连接（自动启动终端）\n"
        "3. 等待终端就绪后，使用 run_command 执行命令\n"
        "4. 如果是堡垒机场景，先用 send_input 输入主机IP + wait_for_output 等待跳转\n"
        "5. 完成后用 disconnect 断开\n\n"
        "排障脚本工具：\n"
        "1. list_snippet_domains 查看可用排障领域（ES/K8s/MySQL/Redis 等）\n"
        "2. load_snippet_domain 将排障脚本加载到远端终端（自动检测是否已加载）\n"
        "3. run_snippet_command 执行排障命令（自动填参数、配置超时）\n\n"
        "文件传输工具（多跳节点，无需 SCP 直连）：\n"
        "1. 先 load_snippet_domain 加载 'ft' 域到目标终端\n"
        "2. upload_file 上传本地文件到远端节点\n"
        "3. download_file 从远端节点下载文件到本地"
    ),
)


def init_mcp_server(
    terminal_manager: TerminalManager,
    tmux_manager: TmuxWindowManager | None = None,
    snippet_registry: SnippetRegistry | None = None,
) -> None:
    """初始化 MCP Server 的依赖（由 main.py 在启动时调用）"""
    global _terminal_manager, _tmux_manager, _snippet_registry
    _terminal_manager = terminal_manager
    _tmux_manager = tmux_manager or TmuxWindowManager()
    _snippet_registry = snippet_registry
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


def _get_snippet_registry() -> SnippetRegistry:
    """获取 Snippet 注册表实例"""
    if _snippet_registry is None:
        raise RuntimeError("Snippet Registry 未初始化，请检查 snippets.yaml 配置")
    return _snippet_registry


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


def _get_session(session_id: str) -> "TerminalSession | None":
    """获取会话（无归属检查）"""
    mgr = _get_terminal_manager()
    return mgr.get_session_by_id(session_id)


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
async def list_hosts(tag: str | None = None, verbose: bool = False) -> str:
    """列出所有可用的 SSH 主机

    返回递归树结构：任意节点都可以继续包含 children，
    适用于 root -> nested -> nested 的多跳链路。

    Args:
        tag: 可选，按标签过滤主机
        verbose: 是否返回完整详情（含 port/username/entry 等）。默认 False 仅返回核心字段以避免结果截断。

    注意：结果可能因客户端文本长度限制被截断，如被截断请用 verbose=false（默认）。
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

    def _to_compact(host: HostResponse) -> JsonDict:
        """精简输出：仅返回 Agent 做路由决策所需的核心字段，避免客户端截断。"""
        return {
            "name": host.name,
            "hostname": host.hostname,
            "description": host.description or "",
            "type": host.host_type.value,
            "children": [_to_compact(child) for child in host.children],
        }

    if verbose:
        return json.dumps([_to_dict(host) for host in hosts], ensure_ascii=False, indent=2)

    return json.dumps([_to_compact(host) for host in hosts], ensure_ascii=False)


@mcp.tool()
async def connect_host(host_name: str, backend: str | None = None) -> str:
    """连接到指定的 SSH 主机节点。

    Args:
        host_name: 目标主机名
        backend: 可选终端后端，支持 tmux / broker；不传则使用服务默认值
    """
    async with async_session_factory() as session:
        mgr = HostManager(session)
        host = await mgr.get_host_by_name(host_name)
        if host:
            path = await mgr.get_connection_path(host)
        else:
            path = []

    if not host:
        return f"错误：未找到名为 '{host_name}' 的主机。请先用 list_hosts 查看可用主机。"

    return await _connect_path(path, backend=backend)


async def _connect_path(path: list[Host], backend: str | None = None) -> str:
    mgr = _get_terminal_manager()
    target = path[-1]
    root = path[0]
    instance_name = HostManager.build_instance_name(path)

    password = _decrypt_host_password(root)

    try:
        session, is_new = await mgr.create_session(
            instance_name=instance_name,
            host=root,
            decrypted_password=password,
            backend=backend,
        )
    except Exception as e:
        return f"错误：终端启动失败 - {e}"

    if is_new and len(path) > 1:
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

    mode_label = "复用已有终端" if not is_new else "新建终端"
    await _publish_event("session_created", session.session_id, target.name, {
        "hostname": root.hostname,
        "username": root.username,
        "mode": "pty",
        "backend": session.backend.value,
        "instance_name": instance_name,
        "path": [node.name for node in path],
    })

    path_text = " -> ".join(node.name for node in path)
    return (
        f"已连接到 {target.name}（{mode_label}）\n"
        f"连接路径: {path_text}\n"
        f"Backend: {session.backend.value}\n"
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

    session = _get_session(session_id)
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
    session = _get_session(session_id)
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
    session = _get_session(session_id)
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
    session = _get_session(session_id)
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
        session = _get_session(session_id)
        if not session:
            return f"会话不存在: {session_id}"
        info = session.info
        return json.dumps({
            "session_id": info.session_id,
            "instance_name": info.instance_name,
            "running": info.running,
            "mode": "pty",
            "backend": info.backend,
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
            "backend": s.backend,
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

    session = _get_session(session_id)
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


# ── Snippet 排障脚本工具 ──────────────────────────


@mcp.tool()
async def list_snippet_domains() -> str:
    """列出所有可用的排障脚本领域

    返回支持的领域列表（如 ES、K8s、MySQL、Redis），
    每个领域包含名称、描述、标签和可用命令数量。

    使用流程：
    1. 先调用此工具查看有哪些排障领域可用
    2. 调用 load_snippet_domain 将领域脚本加载到目标终端
    3. 调用 run_snippet_command 执行具体排障命令

    Returns:
        领域列表（JSON 格式）
    """
    registry = _get_snippet_registry()
    summaries = registry.list_domain_summaries()

    if not summaries:
        return "没有可用的排障脚本领域。请检查 snippets.yaml 配置。"

    result = [s.model_dump() for s in summaries]
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def load_snippet_domain(session_id: str, domain_id: str) -> str:
    """将排障脚本加载到远端终端

    自动检测脚本是否已在远端加载（通过 `type` 命令探测），
    如果已加载则跳过，否则通过 heredoc 注入脚本并 source。

    脚本加载后，该领域的所有命令函数即可在终端中直接使用。

    Args:
        session_id: 终端会话 ID（由 connect_host 返回）
        domain_id: 领域 ID（如 es、k8s、mysql、redis，由 list_snippet_domains 返回）

    Returns:
        加载结果（已加载/新加载/失败）
    """
    registry = _get_snippet_registry()

    session = _get_session(session_id)
    if not session:
        return f"错误：会话 {session_id} 不存在。请先用 connect_host 建立连接。"
    if not session.running:
        return f"错误：会话 {session_id} 已断开。请重新连接。"

    domain = registry.get_domain(domain_id)
    if not domain:
        available = ", ".join(d.id for d in registry.list_domains())
        return f"错误：领域 '{domain_id}' 不存在。可用领域: {available or '无'}"

    # 使用公共注入方法（含探测 + 版本检查 + echo 抑制）
    from src.services.snippet_registry import ensure_snippet_loaded

    error = await ensure_snippet_loaded(session, registry, domain_id)
    if error:
        return f"错误：{error}"

    logger.info("领域 %s 脚本已注入远端", domain_id)
    return (
        f"领域 '{domain.name}' 脚本已成功加载到远端终端。\n"
        f"可用命令: {', '.join(c.id for c in domain.commands)}\n\n"
        f"提示：使用 run_snippet_command 执行具体命令，"
        f"或直接用 run_command 执行命令名（如 `es 9200`）。"
    )


@mcp.tool()
async def run_snippet_command(
    session_id: str,
    domain_id: str,
    command_id: str,
    params: dict[str, str] | None = None,
) -> str:
    """执行排障脚本命令

    解析命令模板，填入参数后在终端中执行。
    自动使用配置中的超时时间（命令级 > 领域级 > 全局 30s）。

    Args:
        session_id: 终端会话 ID
        domain_id: 领域 ID（如 es、k8s、mysql、redis）
        command_id: 命令 ID（如 es、esl、ki、my、rd 等）
        params: 命令参数（键值对），如 {"port": "9200", "index": "my-index"}

    Returns:
        命令执行输出
    """
    registry = _get_snippet_registry()

    session = _get_session(session_id)
    if not session:
        return f"错误：会话 {session_id} 不存在。"
    if not session.running:
        return f"错误：会话 {session_id} 已断开。"

    # 参数标准化：确保所有值为字符串
    param_dict: dict[str, str] = {k: str(v) for k, v in params.items()} if params else {}

    # 参数校验
    errors = registry.validate_params(domain_id, command_id, param_dict)
    if errors:
        return f"参数校验失败:\n" + "\n".join(f"  - {e}" for e in errors)

    # 解析命令模板
    resolved = registry.resolve_command(domain_id, command_id, param_dict)
    if not resolved:
        cmd = registry.get_command(domain_id, command_id)
        if not cmd:
            return f"错误：命令 '{domain_id}/{command_id}' 不存在。"
        return f"错误：必填参数缺失。命令语法: {cmd.syntax}"

    # 获取超时配置
    timeout = registry.get_timeout(domain_id, command_id)

    # 执行命令
    await _publish_event("command_start", session_id, session.instance_name, {
        "command": resolved,
        "snippet": f"{domain_id}/{command_id}",
    })

    try:
        output = await session.send_command(
            command=resolved,
            wait_pattern=r"(?:[\$#>%])\s*$",
            timeout=float(timeout),
        )
    except TimeoutError:
        await _publish_event("command_error", session_id, session.instance_name, {
            "error": f"命令超时（{timeout}s）",
            "snippet": f"{domain_id}/{command_id}",
        })
        return f"错误：命令执行超时（{timeout}s）。命令: {resolved}"
    except ConnectionError as e:
        await _publish_event("command_error", session_id, session.instance_name, {
            "error": str(e),
            "snippet": f"{domain_id}/{command_id}",
        })
        return f"错误：连接异常 - {e}"

    await _publish_event("command_complete", session_id, session.instance_name, {
        "command": resolved,
        "snippet": f"{domain_id}/{command_id}",
    })

    return output


# ── 文件传输工具 ──────────────────────────────


async def _ensure_ft_snippet_loaded(session: "TerminalSession") -> str | None:
    """确保文件传输 snippet 已加载到远端终端。

    委托给公共方法 ensure_snippet_loaded()，仅做薄封装。

    ★ Bugfix #22: 注入前抑制 PTY 回显，避免 base64 刷屏终端（逻辑在公共方法中）。

    Returns:
        None 表示已加载成功，字符串表示错误信息。
    """
    from src.services.snippet_registry import ensure_snippet_loaded

    registry = _get_snippet_registry()
    return await ensure_snippet_loaded(session, registry, "ft")


@mcp.tool()
async def upload_file(
    session_id: str,
    local_path: str,
    remote_path: str,
    timeout: int | None = None,
    verify: bool = True,
) -> str:
    """上传本地文件到远端节点

    通过 PTY 通道传输文件，适用于多跳 SSH 场景（无需 SCP 直连）。
    文件通过 base64 编码分块传输，自动进行 MD5 完整性校验。

    前提条件：终端会话已连接到目标节点。

    性能参考：
    - 传输速率约 50-200 KB/s
    - 推荐文件大小 ≤ 10MB，超过 10MB 建议使用其他方式传输

    Args:
        session_id: 终端会话 ID（由 connect_host 返回）
        local_path: 本地文件路径
        remote_path: 远端目标路径（如 /tmp/app.tar.gz）
        timeout: 超时秒数（不传则根据文件大小自动计算）
        verify: 传输后是否 MD5 校验（默认 True）

    Returns:
        传输结果（包含文件大小、MD5 等信息）
    """
    session = _get_session(session_id)
    if not session:
        return f"错误：会话 {session_id} 不存在。请先用 connect_host 建立连接。"
    if not session.running:
        return f"错误：会话 {session_id} 已断开。请重新连接。"

    # 自动加载 ft snippet
    load_err = await _ensure_ft_snippet_loaded(session)
    if load_err:
        return load_err

    await _publish_event("command_start", session_id, session.instance_name, {
        "command": f"[upload] {local_path} → {remote_path}",
    })

    transfer = PtyFileTransfer(session)
    result: TransferResult = await transfer.upload(
        local_path=local_path,
        remote_path=remote_path,
        timeout=timeout,
        verify=verify,
    )

    event_type = "command_complete" if result.success else "command_error"
    await _publish_event(event_type, session_id, session.instance_name, {
        "command": f"[upload] {local_path} → {remote_path}",
        "file_size": result.file_size,
        "md5": result.md5,
        "success": result.success,
    })

    return result.message


@mcp.tool()
async def download_file(
    session_id: str,
    remote_path: str,
    local_path: str,
    timeout: int | None = None,
    verify: bool = True,
) -> str:
    """从远端节点下载文件到本地

    通过 PTY 通道传输文件，适用于多跳 SSH 场景（无需 SCP 直连）。
    文件通过 base64 编码分块接收，自动进行 MD5 完整性校验。

    前提条件：终端会话已连接到目标节点。

    性能参考：
    - 传输速率约 50-200 KB/s
    - 推荐文件大小 ≤ 10MB，超过 10MB 建议使用其他方式传输

    Args:
        session_id: 终端会话 ID（由 connect_host 返回）
        remote_path: 远端文件路径（如 /var/log/app.log）
        local_path: 本地保存路径
        timeout: 超时秒数（不传则根据文件大小自动计算）
        verify: 是否 MD5 校验（默认 True）

    Returns:
        传输结果（包含文件大小、MD5 等信息）
    """
    session = _get_session(session_id)
    if not session:
        return f"错误：会话 {session_id} 不存在。请先用 connect_host 建立连接。"
    if not session.running:
        return f"错误：会话 {session_id} 已断开。请重新连接。"

    # 自动加载 ft snippet
    load_err = await _ensure_ft_snippet_loaded(session)
    if load_err:
        return load_err

    await _publish_event("command_start", session_id, session.instance_name, {
        "command": f"[download] {remote_path} → {local_path}",
    })

    transfer = PtyFileTransfer(session)
    result: TransferResult = await transfer.download(
        remote_path=remote_path,
        local_path=local_path,
        timeout=timeout,
        verify=verify,
    )

    event_type = "command_complete" if result.success else "command_error"
    await _publish_event(event_type, session_id, session.instance_name, {
        "command": f"[download] {remote_path} → {local_path}",
        "file_size": result.file_size,
        "md5": result.md5,
        "success": result.success,
    })

    return result.message
