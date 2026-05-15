"""主机资产管理 REST API。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.database import get_db
from src.models.host import HostCreate, HostResponse, HostUpdate
from src.services.host_manager import HostManager
from src.utils.tenant_helpers import get_current_tenant

router = APIRouter(prefix="/api/hosts", tags=["hosts"])

DbSessionDep = Annotated[AsyncSession, Depends(get_db)]


def _get_manager(session: DbSessionDep) -> HostManager:
    return HostManager(session)


HostManagerDep = Annotated[HostManager, Depends(_get_manager)]


def _filter_hosts_by_tenant_tags(
    hosts: list[HostResponse],
    allowed_tags: list[str],
) -> list[HostResponse]:
    """递归过滤主机树：只保留 tags 与 allowed_tags 有交集的节点。

    过滤逻辑：
    - 如果一个主机的 tags 与 allowed_tags 有交集 → 保留（含所有子节点，因为子节点通过该主机访问）
    - 如果一个主机的 tags 无交集，但某个子节点匹配 → 也保留（作为路径中间节点）
    - 叶子节点无交集 → 移除
    """
    if not allowed_tags:
        return hosts  # allowed_tags 为空表示无限制

    tag_set = set(allowed_tags)
    filtered: list[HostResponse] = []

    for host in hosts:
        # 递归过滤子节点
        filtered_children = _filter_hosts_by_tenant_tags(host.children, allowed_tags)

        # 当前节点匹配 OR 有匹配的子节点 → 保留
        host_tags = set(host.tags) if host.tags else set()
        if host_tags & tag_set or filtered_children:
            # 浅拷贝 host，替换 children
            filtered_host = host.model_copy(update={"children": filtered_children})
            filtered.append(filtered_host)

    return filtered


@router.get("", response_model=list[HostResponse])
async def list_hosts(
    manager: HostManagerDep,
    request: Request,
    tag: str | None = None,
) -> list[HostResponse]:
    """获取递归主机树，支持按标签过滤。

    租户隔离规则：
    - admin 角色或 allowed_tags 为空 → 返回全部主机
    - 普通用户 + 有 allowed_tags → 只返回匹配标签的主机
    """
    hosts = await manager.list_host_responses(tag=tag)

    # 租户主机过滤
    tenant = get_current_tenant(request)
    if not tenant.is_admin and tenant.allowed_tags:
        hosts = _filter_hosts_by_tenant_tags(hosts, tenant.allowed_tags)

    return hosts


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
