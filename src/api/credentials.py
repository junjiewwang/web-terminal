"""共享凭据 REST API

提供凭据的增删改查端点，支持前端凭据管理面板和下拉选择。

端点列表：
- GET    /api/credentials        — 列表（脱敏，含引用数）
- GET    /api/credentials/names  — 名称列表（下拉选择用）
- POST   /api/credentials        — 创建凭据
- PUT    /api/credentials/{id}   — 更新凭据
- DELETE /api/credentials/{id}   — 删除凭据（被引用时拒绝）
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.credential import (
    CredentialCreate,
    CredentialNameItem,
    CredentialResponse,
    CredentialUpdate,
)
from src.models.database import get_db
from src.services.credential_service import CredentialService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/credentials", tags=["credentials"])


# ── 依赖注入 ──────────────────────────────────────


def _get_service(session: AsyncSession = Depends(get_db)) -> CredentialService:
    return CredentialService(session)


# ── API 端点 ──────────────────────────────────────


@router.get("", response_model=list[CredentialResponse])
async def list_credentials(
    service: CredentialService = Depends(_get_service),
) -> list[CredentialResponse]:
    """获取凭据列表（脱敏，含引用数）"""
    return await service.list_credentials()


@router.get("/names", response_model=list[CredentialNameItem])
async def list_credential_names(
    service: CredentialService = Depends(_get_service),
) -> list[CredentialNameItem]:
    """获取凭据名称列表（前端下拉选择用）"""
    return await service.list_names()


@router.post("", response_model=CredentialResponse, status_code=status.HTTP_201_CREATED)
async def create_credential(
    data: CredentialCreate,
    service: CredentialService = Depends(_get_service),
    session: AsyncSession = Depends(get_db),
) -> CredentialResponse:
    """创建凭据"""
    # 检查名称是否已存在
    existing = await service.get_by_name(data.name)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"凭据名称 '{data.name}' 已存在",
        )

    cred = await service.create(data)
    await session.commit()

    return CredentialResponse(
        id=cred.id,
        name=cred.name,
        description=cred.description,
        has_password=True,
        ref_count=0,
        created_at=cred.created_at,
        updated_at=cred.updated_at,
    )


@router.put("/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
async def update_credential(
    credential_id: int,
    data: CredentialUpdate,
    service: CredentialService = Depends(_get_service),
    session: AsyncSession = Depends(get_db),
) -> None:
    """更新凭据（部分更新）"""
    # 校验：至少有一个字段需要更新
    if data.password is None and data.description is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="至少提供 password 或 description 中的一个字段",
        )

    result = await service.update(credential_id, data)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="凭据不存在",
        )
    await session.commit()


@router.delete("/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_credential(
    credential_id: int,
    service: CredentialService = Depends(_get_service),
    session: AsyncSession = Depends(get_db),
) -> None:
    """删除凭据（被引用时返回 409）"""
    success, message = await service.delete(credential_id)
    if not success:
        if "不存在" in message:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)
        # 被引用时拒绝
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message)
    await session.commit()
