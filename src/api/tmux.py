"""tmux 窗口管理 REST API

为前端 Tab 切换提供 tmux 窗口操作接口。
与 MCP Server 中的 switch_window / list_windows 工具共享同一个 TmuxWindowManager 实例。

前端场景：
  - 用户点击 Tab 切换到 jump_host → 前端获取 client_tty → 调用 switch-window（per-client）
  - 多 Tab 终端各自独立的窗口视图，互不影响

架构要点：
  - tmux select-window：全局切换，所有 attached client 都会看到目标窗口（已废弃）
  - tmux switch-client -c <tty>：只切换指定 client 的视图，其他 client 不受影响
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from src.services.tmux_manager import TmuxWindowManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tmux", tags=["tmux"])

# 全局 TmuxWindowManager 实例（在 main.py 中注入）
tmux_manager: TmuxWindowManager | None = None


def _get_tmux_manager() -> TmuxWindowManager:
    """获取全局 tmux 管理器"""
    if tmux_manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="tmux 管理器未初始化",
        )
    return tmux_manager


# ── 请求/响应 Schema ──────────────────────────


class SwitchWindowRequest(BaseModel):
    """切换 tmux 窗口请求"""
    bastion_name: str
    window_name: str
    client_tty: str | None = None


class TmuxWindowResponse(BaseModel):
    """tmux 窗口信息"""
    index: int
    name: str
    active: bool


# ── 路由 ──────────────────────────────────────


@router.post("/switch-window", status_code=status.HTTP_200_OK)
async def switch_window(req: SwitchWindowRequest) -> dict:
    """切换 tmux 窗口

    支持两种模式：
    - 指定 client_tty: 只切换该 client 的视图（per-client，多 Tab 独立视图）
    - 不指定 client_tty: 全局切换所有 client（兼容旧逻辑）

    Args:
        req: 包含 bastion_name、window_name，可选 client_tty
    """
    mgr = _get_tmux_manager()
    session_name = TmuxWindowManager.session_name_for(req.bastion_name)

    if not await mgr.session_exists(session_name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"堡垒机 '{req.bastion_name}' 的 tmux 会话不存在",
        )

    if req.client_tty:
        # per-client 模式：只切换指定 client
        success = await mgr.switch_client(req.client_tty, session_name, req.window_name)
    else:
        # 全局模式：切换所有 client（向后兼容）
        success = await mgr.select_window(session_name, req.window_name)

    if not success:
        windows = await mgr.list_windows(session_name)
        available = [w.window_name for w in windows]
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "message": f"窗口 '{req.window_name}' 不存在",
                "available_windows": available,
            },
        )

    logger.info(
        "REST API 切换 tmux 窗口: %s:%s (client=%s)",
        session_name, req.window_name, req.client_tty or "all",
    )
    return {"success": True, "session": session_name, "window": req.window_name}


class TmuxClientResponse(BaseModel):
    """tmux 客户端信息"""
    tty: str          # 客户端 TTY（如 /dev/pts/3）
    window: str       # 当前窗口名
    session: str      # 会话名


@router.get("/client-ttys/{bastion_name}")
async def get_client_ttys(bastion_name: str) -> dict:
    """获取堡垒机 tmux 会话的所有客户端信息

    前端在 socket.io 连接建立后调用此接口，获取当前所有 client。
    返回每个客户端的 TTY、当前窗口和会话信息，前端可以据此识别自己的 client。

    Args:
        bastion_name: 堡垒机名称
    """
    mgr = _get_tmux_manager()
    session_name = TmuxWindowManager.session_name_for(bastion_name)

    if not await mgr.session_exists(session_name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"堡垒机 '{bastion_name}' 的 tmux 会话不存在",
        )

    clients = await mgr.list_clients(session_name)
    return {
        "session": session_name,
        "clients": [
            {"tty": c.tty, "window": c.window, "session": c.session}
            for c in clients
        ],
    }


@router.get("/windows/{bastion_name}", response_model=list[TmuxWindowResponse])
async def list_windows(bastion_name: str) -> list[TmuxWindowResponse]:
    """列出堡垒机的所有 tmux 窗口

    Args:
        bastion_name: 堡垒机名称
    """
    mgr = _get_tmux_manager()
    session_name = TmuxWindowManager.session_name_for(bastion_name)

    if not await mgr.session_exists(session_name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"堡垒机 '{bastion_name}' 的 tmux 会话不存在",
        )

    windows = await mgr.list_windows(session_name)
    return [
        TmuxWindowResponse(
            index=w.window_index,
            name=w.window_name,
            active=w.active,
        )
        for w in windows
    ]
