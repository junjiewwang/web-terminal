"""WeTTY 实例管理 REST API

提供 WeTTY 实例的启动/停止/列出接口，
前端选择主机时调用此 API 获取 WeTTY 终端 URL。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from src.models.database import async_session_factory
from src.services.host_manager import HostManager
from src.services.wetty_manager import WeTTYInstance, WeTTYManager

router = APIRouter(prefix="/api/wetty", tags=["wetty"])

# 全局 WeTTY 管理器（在 main.py 中注入）
wetty_manager: WeTTYManager | None = None


def _get_wetty_manager() -> WeTTYManager:
    """获取全局 WeTTY 管理器"""
    if wetty_manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WeTTY 管理器未初始化",
        )
    return wetty_manager


# ── 请求/响应 Schema ──────────────────────────


class StartWeTTYRequest(BaseModel):
    """启动 WeTTY 实例请求"""
    host_id: int


class WeTTYInstanceResponse(BaseModel):
    """WeTTY 实例信息响应"""
    host_name: str
    port: int
    url: str
    running: bool


# ── 路由 ──────────────────────────────────────


@router.post("/start", response_model=WeTTYInstanceResponse)
async def start_wetty(req: StartWeTTYRequest) -> WeTTYInstanceResponse:
    """为指定主机启动 WeTTY 实例

    如果该主机已有运行中的实例，直接返回现有实例信息。
    """
    manager = _get_wetty_manager()

    async with async_session_factory() as db_session:
        host_mgr = HostManager(db_session)
        host = await host_mgr.get_host_by_id(req.host_id)
        if not host:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"主机不存在: {req.host_id}",
            )

        try:
            instance: WeTTYInstance = await manager.start_instance(host)
        except FileNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="wetty 命令未找到，请先安装: npm install -g wetty",
            )
        except OSError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"WeTTY 启动失败: {e}",
            )

    return WeTTYInstanceResponse(
        host_name=instance.host_name,
        port=instance.port,
        url=instance.url,
        running=instance.running,
    )


@router.post("/stop/{host_name}", status_code=status.HTTP_204_NO_CONTENT)
async def stop_wetty(host_name: str) -> None:
    """停止指定主机的 WeTTY 实例"""
    manager = _get_wetty_manager()
    stopped = await manager.stop_instance(host_name)
    if not stopped:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"WeTTY 实例不存在: {host_name}",
        )


@router.get("", response_model=list[WeTTYInstanceResponse])
async def list_wetty_instances() -> list[WeTTYInstanceResponse]:
    """列出所有运行中的 WeTTY 实例"""
    manager = _get_wetty_manager()
    instances = manager.list_instances()
    return [
        WeTTYInstanceResponse(
            host_name=inst.host_name,
            port=inst.port,
            url=inst.url,
            running=inst.running,
        )
        for inst in instances
    ]
