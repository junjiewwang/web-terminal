"""终端管理 REST API + WebSocket 端点

替代 wetty.py + wetty_proxy.py，提供：
- REST API: 终端会话的启动/停止/列出
- WebSocket: 浏览器 xterm.js 直连终端 PTY

WebSocket 协议（JSON 消息）：
  Client → Server:
    {"type": "input", "data": "ls\r"}
    {"type": "resize", "cols": 80, "rows": 24}
  Server → Client:
    {"type": "output", "data": "..."}
    {"type": "closed", "reason": "..."}
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel

from src.models.database import async_session_factory
from src.models.host import Host, HostType
from src.services.host_manager import HostManager
from src.services.jump_orchestrator import JumpOrchestrator
from src.services.terminal_manager import TerminalManager, TerminalSession
from src.services.tmux_manager import TmuxWindowManager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["terminal"])

# 全局服务实例（在 main.py 中注入）
terminal_manager: TerminalManager | None = None
tmux_manager: TmuxWindowManager | None = None


def _get_terminal_manager() -> TerminalManager:
    if terminal_manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="终端管理器未初始化",
        )
    return terminal_manager


def _get_tmux_manager() -> TmuxWindowManager:
    if tmux_manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="tmux 管理器未初始化",
        )
    return tmux_manager


# ── Schema ────────────────────────────────────


class StartTerminalRequest(BaseModel):
    host_id: int


class TerminalResponse(BaseModel):
    session_id: str
    instance_name: str
    running: bool
    ws_url: str
    bastion_name: str | None = None


# ── REST API ──────────────────────────────────


@router.post("/api/terminal/start", response_model=TerminalResponse)
async def start_terminal(req: StartTerminalRequest) -> TerminalResponse:
    """启动终端会话

    自动识别主机类型：
    - direct / bastion: 直接创建终端会话
    - jump_host: 创建独立终端 + 后台跳板编排
    """
    mgr = _get_terminal_manager()

    async with async_session_factory() as db_session:
        host_mgr = HostManager(db_session)
        host = await host_mgr.get_host_by_id(req.host_id)
        if not host:
            raise HTTPException(status_code=404, detail=f"主机不存在: {req.host_id}")

        if host.host_type == HostType.JUMP_HOST:
            return await _start_jump_host_terminal(host, host_mgr, mgr)

        return await _start_direct_terminal(host, mgr)


async def _start_direct_terminal(host: Host, mgr: TerminalManager) -> TerminalResponse:
    """直连主机/堡垒机：创建终端会话"""
    password = _decrypt_password(host)
    session = await mgr.create_session(
        instance_name=host.name,
        host=host,
        decrypted_password=password,
    )

    return TerminalResponse(
        session_id=session.session_id,
        instance_name=session.instance_name,
        running=session.running,
        ws_url=f"/ws/terminal/{session.session_id}",
    )


async def _start_jump_host_terminal(
    jump_host: Host,
    host_mgr: HostManager,
    mgr: TerminalManager,
) -> TerminalResponse:
    """jump_host：创建独立终端 + 后台跳板编排"""
    if not jump_host.parent_id:
        raise HTTPException(status_code=400, detail=f"jump_host '{jump_host.name}' 未配置 parent_id")

    bastion = await host_mgr.get_host_by_id(jump_host.parent_id)
    if not bastion:
        raise HTTPException(status_code=404, detail=f"父堡垒机不存在 (id={jump_host.parent_id})")

    instance_name = f"{bastion.name}--{jump_host.name}"

    # 复用已有会话
    if mgr.has_running_session(instance_name):
        session = mgr.get_session(instance_name)
        if session:
            return TerminalResponse(
                session_id=session.session_id,
                instance_name=session.instance_name,
                running=session.running,
                ws_url=f"/ws/terminal/{session.session_id}",
                bastion_name=instance_name,
            )

    # 创建新会话（使用堡垒机连接信息）
    password = _decrypt_password(bastion)
    session = await mgr.create_session(
        instance_name=instance_name,
        host=bastion,
        decrypted_password=password,
    )

    # 后台跳板编排
    asyncio.create_task(
        _run_jump_orchestration(session, jump_host, bastion)
    )

    return TerminalResponse(
        session_id=session.session_id,
        instance_name=session.instance_name,
        running=session.running,
        ws_url=f"/ws/terminal/{session.session_id}",
        bastion_name=instance_name,
    )


async def _run_jump_orchestration(
    session: TerminalSession,
    jump_host: Host,
    bastion: Host,
) -> None:
    """后台执行跳板编排"""
    tmux_mgr = _get_tmux_manager()

    # 防重入检测
    if await tmux_mgr.is_session_logged_in(session.tmux_session_name):
        screen = session.read_screen(lines=5)
        import re
        if re.search(r"[\$#>]\s*$", screen, re.MULTILINE):
            logger.info("跳板编排防重入: 已有活跃连接 (%s)", jump_host.name)
            return

    # 执行跳板编排（内部已有 _wait_for_ready 等待堡垒机就绪）
    orchestrator = JumpOrchestrator(session)  # type: ignore[arg-type]
    result = await orchestrator.execute_jump(
        jump_host=jump_host,
        bastion=bastion,
        tmux_session_name=session.tmux_session_name,
        window_name="0",
        skip_window_creation=True,
    )

    if result.success:
        logger.info("跳板编排成功: %s → %s", bastion.name, jump_host.name)
    else:
        logger.error("跳板编排失败: %s → %s (%s)", bastion.name, jump_host.name, result.message)


@router.post("/api/terminal/stop/{instance_name}", status_code=204)
async def stop_terminal(instance_name: str) -> None:
    """停止终端会话"""
    mgr = _get_terminal_manager()
    stopped = await mgr.stop_session(instance_name)
    if not stopped:
        raise HTTPException(status_code=404, detail=f"终端会话不存在: {instance_name}")


@router.get("/api/terminal", response_model=list[TerminalResponse])
async def list_terminals() -> list[TerminalResponse]:
    """列出所有终端会话"""
    mgr = _get_terminal_manager()
    sessions = mgr.list_sessions()
    return [
        TerminalResponse(
            session_id=s.session_id,
            instance_name=s.instance_name,
            running=s.running,
            ws_url=f"/ws/terminal/{s.session_id}",
        )
        for s in sessions
    ]


# ── WebSocket 端点 ────────────────────────────


@router.websocket("/ws/terminal/{session_id}")
async def terminal_websocket(websocket: WebSocket, session_id: str) -> None:
    """浏览器 xterm.js WebSocket 直连

    协议：
    - Client → Server: JSON {"type": "input"/"resize", ...}
    - Server → Client: JSON {"type": "output"/"closed", ...}
    """
    mgr = _get_terminal_manager()
    session = mgr.get_session_by_id(session_id)

    if not session or not session.running:
        await websocket.close(code=1008, reason="终端会话不存在或已关闭")
        return

    await websocket.accept()
    session.add_ws_client(websocket)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")
            if msg_type == "input":
                data = msg.get("data", "")
                if data:
                    session.write(data)
            elif msg_type == "resize":
                cols = msg.get("cols", 80)
                rows = msg.get("rows", 24)
                session.resize(cols, rows)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug("WebSocket 异常: %s - %s", session_id[:8], e)
    finally:
        session.remove_ws_client(websocket)


# ── 兼容旧 API（过渡期）──────────────────────

# 保留 /api/wetty/start 和 /api/wetty/stop 作为别名，
# 前端迁移完成后删除

@router.post("/api/wetty/start", response_model=TerminalResponse)
async def start_wetty_compat(req: StartTerminalRequest) -> TerminalResponse:
    """兼容旧 API: /api/wetty/start → /api/terminal/start"""
    return await start_terminal(req)


@router.post("/api/wetty/stop/{instance_name}", status_code=204)
async def stop_wetty_compat(instance_name: str) -> None:
    """兼容旧 API"""
    await stop_terminal(instance_name)


@router.get("/api/wetty", response_model=list[TerminalResponse])
async def list_wetty_compat() -> list[TerminalResponse]:
    """兼容旧 API"""
    return await list_terminals()


# ── 工具函数 ──────────────────────────────────


def _decrypt_password(host: Host) -> str | None:
    """解密主机密码"""
    if not host.password_encrypted:
        return None
    try:
        from src.utils.security import decrypt_password
        return decrypt_password(host.password_encrypted)
    except Exception as e:
        logger.warning("密码解密失败 (%s): %s", host.name, e)
        return None
