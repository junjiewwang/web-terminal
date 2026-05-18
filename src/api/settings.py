"""系统设置 REST API

提供认证配置和数据库状态的查询/更新端点。

端点列表：
- GET  /api/settings/auth       — 查询认证配置（脱敏）
- PUT  /api/settings/auth       — 更新认证配置
- GET  /api/settings/database   — 查询数据库连接状态
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.database import get_db, get_db_info

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])

# ── 注入点（由 main.py 设置）──────────────────────
_auth_service = None


def _get_auth_service():
    """获取 AuthService 实例"""
    if _auth_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="认证服务未初始化",
        )
    return _auth_service


# ── 请求/响应模型 ──────────────────────────────────


class AuthSettingsResponse(BaseModel):
    """认证配置响应（脱敏）"""
    enabled: bool
    has_password: bool = Field(description="是否已设置密码")
    jwt_secret_prefix: str = Field(description="JWT Secret 前 8 字符（脱敏）")
    access_token_expire_hours: float
    refresh_token_expire_days: int


class AuthSettingsUpdateRequest(BaseModel):
    """认证配置更新请求（部分更新）"""
    enabled: bool | None = None
    access_token_expire_hours: float | None = Field(None, gt=0, le=720)
    refresh_token_expire_days: int | None = Field(None, gt=0, le=90)


class DatabaseInfoResponse(BaseModel):
    """数据库信息响应"""
    type: str = Field(description="数据库类型: sqlite / mysql")
    url: str = Field(description="连接 URL（脱敏）")


# ── API 端点 ──────────────────────────────────────


@router.get("/auth", response_model=AuthSettingsResponse)
async def get_auth_settings() -> AuthSettingsResponse:
    """查询认证配置（脱敏）"""
    auth_svc = _get_auth_service()
    config = auth_svc.config

    secret = config.jwt_secret
    secret_prefix = secret[:8] + "..." if len(secret) > 8 else "***"

    return AuthSettingsResponse(
        enabled=config.enabled,
        has_password=bool(config.password_hash),
        jwt_secret_prefix=secret_prefix,
        access_token_expire_hours=config.access_token_expire_hours,
        refresh_token_expire_days=config.refresh_token_expire_days,
    )


@router.put("/auth", status_code=status.HTTP_204_NO_CONTENT)
async def update_auth_settings(
    req: AuthSettingsUpdateRequest,
    session: AsyncSession = Depends(get_db),
) -> None:
    """更新认证配置

    仅支持部分字段更新（enabled、过期时间配置）。
    密码修改请使用 PUT /api/auth/password。
    """
    auth_svc = _get_auth_service()

    updates = req.model_dump(exclude_none=True)
    if not updates:
        return

    await auth_svc.update_config(session, **updates)
    logger.info("认证配置已通过 API 更新: %s", list(updates.keys()))


@router.get("/database", response_model=DatabaseInfoResponse)
async def get_database_info() -> DatabaseInfoResponse:
    """查询数据库连接状态"""
    db_info = get_db_info()
    return DatabaseInfoResponse(
        type=db_info["type"],
        url=db_info["url"],
    )
