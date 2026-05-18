"""终端管理 REST API + WebSocket 端点。"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel

from src.models.database import async_session_factory
from src.models.host import Host
from src.services.host_manager import HostManager
from src.services.jump_orchestrator import ConnectionOrchestrator
from src.services.terminal_backend import TerminalBackend
from src.services.terminal_manager import TerminalManager, TerminalSession
from src.services.tmux_manager import TmuxWindowManager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["terminal"])

terminal_manager: TerminalManager | None = None
# 保留注入点，tmux copy-buffer 和脚本仍依赖 tmux 会话
tmux_manager: TmuxWindowManager | None = None

_COPY_BUFFER_DIR = "/tmp"


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


class StartTerminalRequest(BaseModel):
    host_id: int
    backend: TerminalBackend | None = None


class TerminalResponse(BaseModel):
    session_id: str
    instance_name: str
    backend: TerminalBackend
    running: bool
    ws_url: str


@router.post("/api/terminal/start", response_model=TerminalResponse)
async def start_terminal(req: StartTerminalRequest, request: Request) -> TerminalResponse:
    """启动终端会话。"""
    mgr = _get_terminal_manager()

    async with async_session_factory() as db_session:
        host_mgr = HostManager(db_session)
        target = await host_mgr.get_host_by_id(req.host_id)
        if not target:
            raise HTTPException(status_code=404, detail=f"主机不存在: {req.host_id}")

        path = await host_mgr.get_connection_path(target)
        root = path[0]
        instance_name = HostManager.build_instance_name(path)

    password = _decrypt_password(root)
    session, is_new = await mgr.create_session(
        instance_name=instance_name,
        host=root,
        decrypted_password=password,
        backend=req.backend,
    )

    # 仅在新建会话时触发多跳编排
    if is_new and len(path) > 1:
        asyncio.create_task(_run_path_orchestration(session, path))

    return TerminalResponse(
        session_id=session.session_id,
        instance_name=session.instance_name,
        backend=session.backend,
        running=session.running,
        ws_url=f"/ws/terminal/{session.session_id}",
    )


async def _run_path_orchestration(session: TerminalSession, path: list[Host]) -> None:
    orchestrator = ConnectionOrchestrator(session)  # type: ignore[arg-type]
    result = await orchestrator.execute_path(
        path=path,
        tmux_session_name=session.tmux_session_name,
        window_name="0",
        skip_window_creation=True,
    )

    if result.success:
        logger.info("多跳编排成功: %s", " -> ".join(node.name for node in path))
    else:
        logger.error("多跳编排失败: %s (%s)", " -> ".join(node.name for node in path), result.message)


class BackendResponse(BaseModel):
    backend: TerminalBackend


class SwitchBackendRequest(BaseModel):
    backend: TerminalBackend


class SwitchBackendResponse(BaseModel):
    backend: TerminalBackend
    stopped_sessions: list[str]


@router.get("/api/terminal/backend", response_model=BackendResponse)
async def get_backend() -> BackendResponse:
    """查询当前全局 terminal backend。"""
    mgr = _get_terminal_manager()
    return BackendResponse(backend=mgr.default_backend)


@router.put("/api/terminal/backend", response_model=SwitchBackendResponse)
async def switch_backend(req: SwitchBackendRequest) -> SwitchBackendResponse:
    """全局切换 terminal backend。"""
    mgr = _get_terminal_manager()
    stopped = await mgr.switch_backend(req.backend)
    return SwitchBackendResponse(backend=req.backend, stopped_sessions=stopped)


@router.post("/api/terminal/stop/{instance_name}", status_code=204)
async def stop_terminal(instance_name: str, request: Request) -> None:
    mgr = _get_terminal_manager()
    stopped = await mgr.stop_session(instance_name)
    if not stopped:
        raise HTTPException(status_code=404, detail=f"终端会话不存在: {instance_name}")


@router.get("/api/terminal", response_model=list[TerminalResponse])
async def list_terminals(request: Request) -> list[TerminalResponse]:
    mgr = _get_terminal_manager()
    sessions = mgr.list_sessions()
    return [
        TerminalResponse(
            session_id=s.session_id,
            instance_name=s.instance_name,
            backend=TerminalBackend(s.backend),
            running=s.running,
            ws_url=f"/ws/terminal/{s.session_id}",
        )
        for s in sessions
    ]


class CopyBufferRequest(BaseModel):
    """tmux copy-buffer 通知请求（由 tmux hook 调用）"""
    session_name: str


@router.post("/api/tmux/copy-buffer", status_code=204)
async def handle_copy_buffer(req: CopyBufferRequest) -> None:
    logger.info("收到 copy-buffer 请求: session_name=%s", req.session_name)

    mgr = _get_terminal_manager()
    session_name = req.session_name
    if not session_name.startswith("wetty-"):
        logger.warning("无效的 session_name 前缀: %s", session_name)
        return

    instance_name = session_name[len("wetty-"):]
    all_sessions = mgr.list_sessions()
    session = None
    for s_info in all_sessions:
        if s_info.instance_name == instance_name and s_info.running:
            session = mgr.get_session_by_id(s_info.session_id)
            break
    if not session or not session.running:
        logger.warning("找不到运行中的终端会话: instance_name=%s", instance_name)
        return

    buffer_path = f"{_COPY_BUFFER_DIR}/tmux-copy-{session_name}"
    try:
        with open(buffer_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read().strip()
        logger.info("读取 buffer 文件成功: %s (%d chars)", buffer_path, len(text))
    except FileNotFoundError:
        logger.warning("buffer 文件不存在: %s", buffer_path)
        return
    except Exception as e:
        logger.error("读取 buffer 文件失败: %s - %s", buffer_path, e)
        return

    if not text:
        logger.info("buffer 内容为空，跳过推送")
        return

    await session.send_to_clients({"type": "clipboard", "text": text})
    logger.info("tmux copy-buffer 已推送到前端: %s (%d chars)", session_name, len(text))


@router.websocket("/ws/terminal/{session_id}")
async def terminal_websocket(
    websocket: WebSocket,
    session_id: str,
    token: str = Query(default=""),
) -> None:
    """WebSocket 终端连接

    认证方式：通过 query param `token` 传递 Bearer Token。
    """
    mgr = _get_terminal_manager()

    # ── WebSocket Token 认证 ──
    authenticated = await _authenticate_ws_token(token)
    if not authenticated:
        await websocket.close(code=1008, reason="认证失败：无效的 Token")
        return

    session = mgr.get_session_by_id(session_id)

    if not session or not session.running:
        await websocket.close(code=1008, reason="终端会话不存在或已关闭")
        return

    await websocket.accept()

    client_id = session.add_ws_client(websocket)
    logger.debug(
        "WebSocket 已建立: session=%s, client=%s, backend=%s, instance=%s",
        session_id[:8], client_id[:8], session.backend.value, session.instance_name,
    )

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
                session.resize(cols, rows, client_id=client_id)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug("WebSocket 异常: %s - %s", session_id[:8], e)
    finally:
        session.remove_ws_client(client_id)


async def _authenticate_ws_token(token: str) -> bool:
    """验证 WebSocket 连接的 Token，返回是否认证通过。

    支持三种 Token 类型：
    1. 环境变量 WETTY_API_TOKEN
    2. JWT Token（密码保护模式）
    3. 自动生成 Token
    4. 开发模式（无 Token 配置）→ 放行
    """
    import os
    import secrets

    from src.services.auth_service import AuthService
    from src.utils.security import verify_api_token

    # 引用全局 auth_service
    from src.main import auth_service as _auth_service

    if not token:
        # 无 Token：开发模式放行
        if not os.environ.get("WETTY_API_TOKEN") and not _auth_service.is_auth_enabled:
            return True
        return False

    # 1. 环境变量 Token
    env_token = os.environ.get("WETTY_API_TOKEN")
    if env_token and secrets.compare_digest(token, env_token):
        return True

    # 2. JWT Token
    if _auth_service.is_auth_enabled:
        if _auth_service.verify_access_token(token):
            return True

    # 3. 自动生成 Token
    if verify_api_token(token):
        return True

    return False


@router.post("/api/wetty/start", response_model=TerminalResponse)
async def start_wetty_compat(req: StartTerminalRequest, request: Request) -> TerminalResponse:
    return await start_terminal(req, request)


@router.post("/api/wetty/stop/{instance_name}", status_code=204)
async def stop_wetty_compat(instance_name: str, request: Request) -> None:
    await stop_terminal(instance_name, request)


@router.get("/api/wetty", response_model=list[TerminalResponse])
async def list_wetty_compat(request: Request) -> list[TerminalResponse]:
    return await list_terminals(request)


def _decrypt_password(host: Host) -> str | None:
    if not host.password_encrypted:
        return None
    try:
        from src.utils.security import decrypt_password
        return decrypt_password(host.password_encrypted)
    except Exception as e:
        logger.warning("密码解密失败 (%s): %s", host.name, e)
        return None
