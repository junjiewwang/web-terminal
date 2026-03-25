"""主机资产管理 REST API

提供主机的 CRUD 接口和 YAML 导入功能，供前端管理 UI 使用。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.database import get_db
from src.models.host import HostCreate, HostResponse, HostType, HostUpdate
from src.services.host_manager import HostManager

router = APIRouter(prefix="/api/hosts", tags=["hosts"])


def _get_manager(session: AsyncSession = Depends(get_db)) -> HostManager:
    """依赖注入：获取 HostManager 实例"""
    return HostManager(session)


@router.get("", response_model=list[HostResponse])
async def list_hosts(
    tag: str | None = None,
    manager: HostManager = Depends(_get_manager),
) -> list[HostResponse]:
    """获取主机列表（树形结构），支持按标签过滤

    返回顶层主机列表（direct / bastion），不包含 jump_host 类型。
    jump_host 作为其所属 bastion 的 children 子列表返回。
    """
    hosts = await manager.list_hosts(tag=tag)
    # jump_host 通过 bastion.children 递归返回，不出现在顶层列表
    top_level = [h for h in hosts if h.host_type != HostType.JUMP_HOST]
    return [HostResponse.from_orm_model(h) for h in top_level]


@router.get("/{host_id}", response_model=HostResponse)
async def get_host(
    host_id: int,
    manager: HostManager = Depends(_get_manager),
) -> HostResponse:
    """获取单个主机详情"""
    host = await manager.get_host_by_id(host_id)
    if not host:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"主机不存在: {host_id}")
    return HostResponse.from_orm_model(host)


@router.post("", response_model=HostResponse, status_code=status.HTTP_201_CREATED)
async def create_host(
    data: HostCreate,
    manager: HostManager = Depends(_get_manager),
) -> HostResponse:
    """创建新主机"""
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
    manager: HostManager = Depends(_get_manager),
) -> HostResponse:
    """更新主机信息"""
    host = await manager.update_host(host_id, data)
    if not host:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"主机不存在: {host_id}")
    return HostResponse.from_orm_model(host)


@router.delete("/{host_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_host(
    host_id: int,
    manager: HostManager = Depends(_get_manager),
) -> None:
    """删除主机"""
    deleted = await manager.delete_host(host_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"主机不存在: {host_id}")


@router.post("/sync", response_model=dict)
async def sync_hosts_from_yaml(
    manager: HostManager = Depends(_get_manager),
) -> dict:
    """从 config/hosts.yaml 同步主机配置到数据库

    YAML 是唯一真相，执行完整的增/改/删同步：
    - YAML 中有、DB 中无 → 新增
    - YAML 中有、DB 中有且字段变化 → 更新
    - YAML 中无、DB 中有 → 删除

    如果 YAML 格式校验失败，整批拒绝，不做任何变更。
    """
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
