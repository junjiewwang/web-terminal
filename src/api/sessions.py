"""SSH 会话管理 REST API

提供会话创建/关闭、命令执行等接口。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from src.services.event_service import AgentEvent, EventType, event_bus
from src.services.ssh_session import CommandResult, SessionInfo, SSHSessionManager

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

# 全局 SSH 会话管理器（在 main.py 中注入）
ssh_manager: SSHSessionManager | None = None


def get_ssh_manager() -> SSHSessionManager:
    """获取全局 SSH 会话管理器"""
    if ssh_manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SSH 会话管理器未初始化",
        )
    return ssh_manager


# ── 请求 Schema ──────────────────────────────


class CreateSessionRequest(BaseModel):
    """创建会话请求"""
    host_id: int = Field(..., description="目标主机 ID")


class ExecuteCommandRequest(BaseModel):
    """执行命令请求"""
    command: str = Field(..., min_length=1, description="Shell 命令")
    timeout: int = Field(default=30, ge=1, le=300, description="超时秒数")


class CreateSessionResponse(BaseModel):
    """创建会话响应"""
    session_id: str
    host_name: str
    message: str


class CommandResponse(BaseModel):
    """命令执行响应"""
    session_id: str
    host_name: str
    command: str
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: float
    success: bool


# ── 路由 ──────────────────────────────────────


@router.get("", response_model=list[SessionInfo])
async def list_sessions() -> list[SessionInfo]:
    """列出所有活跃的 SSH 会话"""
    manager = get_ssh_manager()
    return manager.list_sessions()


@router.post("", response_model=CreateSessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(req: CreateSessionRequest) -> CreateSessionResponse:
    """创建新的 SSH 会话

    需要传入目标主机 ID，服务会建立到目标主机的 SSH 连接。
    """
    from src.models.database import async_session_factory
    from src.services.host_manager import HostManager

    manager = get_ssh_manager()

    # 查找主机
    async with async_session_factory() as db_session:
        host_mgr = HostManager(db_session)
        host = await host_mgr.get_host_by_id(req.host_id)
        if not host:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"主机不存在: {req.host_id}")

        try:
            session_id = await manager.create_session(host)
        except ConnectionError as e:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e)) from e

        # 发布事件
        await event_bus.publish(
            AgentEvent(
                event_type=EventType.SESSION_CREATED,
                session_id=session_id,
                host_name=host.name,
                data={"hostname": host.hostname, "username": host.username},
            )
        )

        return CreateSessionResponse(
            session_id=session_id,
            host_name=host.name,
            message=f"SSH 会话已建立: {host.username}@{host.hostname}",
        )


@router.post("/{session_id}/exec", response_model=CommandResponse)
async def execute_command(session_id: str, req: ExecuteCommandRequest) -> CommandResponse:
    """在指定会话上执行命令"""
    manager = get_ssh_manager()

    # 发布 command_start 事件
    session_info = manager.get_session_info(session_id)
    host_name = session_info.host_name if session_info else "unknown"

    await event_bus.publish(
        AgentEvent(
            event_type=EventType.COMMAND_START,
            session_id=session_id,
            host_name=host_name,
            data={"command": req.command},
        )
    )

    try:
        result: CommandResult = await manager.execute_command(
            session_id=session_id,
            command=req.command,
            timeout=req.timeout,
        )
    except KeyError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except TimeoutError as e:
        await event_bus.publish(
            AgentEvent(
                event_type=EventType.COMMAND_ERROR,
                session_id=session_id,
                host_name=host_name,
                data={"error": str(e)},
            )
        )
        raise HTTPException(status_code=status.HTTP_408_REQUEST_TIMEOUT, detail=str(e)) from e
    except ConnectionError as e:
        await event_bus.publish(
            AgentEvent(
                event_type=EventType.COMMAND_ERROR,
                session_id=session_id,
                host_name=host_name,
                data={"error": str(e)},
            )
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e)) from e

    # 发布 command_complete 事件
    await event_bus.publish(
        AgentEvent(
            event_type=EventType.COMMAND_COMPLETE,
            session_id=session_id,
            host_name=host_name,
            data={
                "command": result.command,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
            },
        )
    )

    return CommandResponse(
        session_id=result.session_id,
        host_name=result.host_name,
        command=result.command,
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        success=result.success,
    )


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def close_session(session_id: str) -> None:
    """关闭指定的 SSH 会话"""
    manager = get_ssh_manager()
    session_info = manager.get_session_info(session_id)

    closed = await manager.close_session(session_id)
    if not closed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"会话不存在: {session_id}")

    if session_info:
        await event_bus.publish(
            AgentEvent(
                event_type=EventType.SESSION_CLOSED,
                session_id=session_id,
                host_name=session_info.host_name,
            )
        )
