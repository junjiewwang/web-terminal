"""主机资产管理 REST API。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.database import get_db
from src.models.host import HostCreate, HostResponse, HostUpdate
from src.services.host_manager import HostManager

router = APIRouter(prefix="/api/hosts", tags=["hosts"])

DbSessionDep = Annotated[AsyncSession, Depends(get_db)]


def _get_manager(session: DbSessionDep) -> HostManager:
    return HostManager(session)


HostManagerDep = Annotated[HostManager, Depends(_get_manager)]


@router.get("", response_model=list[HostResponse])
async def list_hosts(
    manager: HostManagerDep,
    tag: str | None = None,
) -> list[HostResponse]:
    """获取递归主机树，支持按标签过滤。"""
    return await manager.list_host_responses(tag=tag)


@router.get("/{host_id}", response_model=HostResponse)
async def get_host(
    host_id: int,
    manager: HostManagerDep,
) -> HostResponse:
    host = await manager.get_host_by_id(host_id)
    if not host:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"主机不存在: {host_id}")
    return HostResponse.from_orm_model(host)


@router.post("", response_model=HostResponse, status_code=status.HTTP_201_CREATED)
async def create_host(
    data: HostCreate,
    manager: HostManagerDep,
) -> HostResponse:
    existing = await manager.get_host_by_name(data.name)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"主机名已存在: {data.name}",
        )
    host = await manager.create_host(data)
    return HostResponse.from_orm_model(host)


@router.put("/{host_id}", response_model=HostResponse)
async def update_host(
    host_id: int,
    data: HostUpdate,
    manager: HostManagerDep,
) -> HostResponse:
    host = await manager.update_host(host_id, data)
    if not host:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"主机不存在: {host_id}")
    return HostResponse.from_orm_model(host)


@router.delete("/{host_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_host(
    host_id: int,
    manager: HostManagerDep,
) -> None:
    deleted = await manager.delete_host(host_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"主机不存在: {host_id}")


@router.post("/sync", response_model=dict[str, object])
async def sync_hosts_from_yaml(
    manager: HostManagerDep,
) -> dict[str, object]:
    """从新的递归连接树 YAML 配置同步主机。"""
    yaml_path = Path(__file__).resolve().parent.parent.parent / "config" / "hosts.yaml"
    result = await manager.sync_from_yaml(yaml_path)

    if result.errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "hosts.yaml 校验失败，同步已中止",
                "errors": result.errors,
            },
        )

    return {
        **result.to_dict(),
        "message": f"同步完成: 新增 {result.added}, 更新 {result.updated}, 删除 {result.deleted}",
    }
