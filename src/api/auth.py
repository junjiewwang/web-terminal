"""认证 REST API

提供登录/刷新/注销/密码修改等认证端点。

端点列表：
- GET  /api/auth/status   — 查询认证状态（前端判断是否需要登录页）
- POST /api/auth/login     — 登录（返回 access_token + refresh_token）
- POST /api/auth/refresh   — 刷新 Token（Token Rotation）
- POST /api/auth/logout    — 注销（撤销 refresh_token）
- PUT  /api/auth/password  — 修改密码（验证旧密码 + 新密码更新）
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

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


class LoginRequest(BaseModel):
    """登录请求"""
    password: str = Field(..., min_length=1, description="密码")


class LoginResponse(BaseModel):
    """登录响应"""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_at: str  # ISO 8601


class RefreshRequest(BaseModel):
    """Token 刷新请求"""
    refresh_token: str = Field(..., min_length=1)


class RefreshResponse(BaseModel):
    """Token 刷新响应"""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_at: str  # ISO 8601


class LogoutRequest(BaseModel):
    """注销请求"""
    refresh_token: str = Field(..., min_length=1)


class ChangePasswordRequest(BaseModel):
    """修改密码请求"""
    old_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=6, description="新密码（至少 6 个字符）")


class AuthStatusResponse(BaseModel):
    """认证状态响应（前端用于判断是否需要显示登录页）"""
    auth_required: bool


# ── API 端点 ──────────────────────────────────────


@router.get("/status", response_model=AuthStatusResponse)
async def auth_status() -> AuthStatusResponse:
    """查询后端认证状态

    前端启动时调用此端点判断是否需要显示登录页。
    未配置认证 → auth_required=false（开发模式）。
    """
    import os
    auth_svc = _get_auth_service() if _auth_service else None
    has_env_token = bool(os.environ.get("WETTY_API_TOKEN"))
    has_auth = auth_svc is not None and auth_svc.is_auth_enabled

    return AuthStatusResponse(
        auth_required=has_env_token or has_auth,
    )


@router.post("/login", response_model=LoginResponse)
async def login(
    req: LoginRequest,
    session: AsyncSession = Depends(get_db),
) -> LoginResponse:
    """登录

    验证密码后签发 access_token + refresh_token。
    """
    auth_svc = _get_auth_service()

    result = await auth_svc.authenticate_and_persist(req.password, session)
    if not result:
        logger.warning("登录失败")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="密码错误",
        )

    access_token, refresh_token = result

    expires_at = datetime.fromtimestamp(
        time.time() + auth_svc.access_token_expire_hours * 3600,
        tz=timezone.utc,
    ).isoformat()

    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
    )


@router.post("/refresh", response_model=RefreshResponse)
async def refresh_token(
    req: RefreshRequest,
    session: AsyncSession = Depends(get_db),
) -> RefreshResponse:
    """刷新 Token

    Token Rotation：旧 refresh_token 立即失效。
    """
    auth_svc = _get_auth_service()

    result = await auth_svc.refresh_access_token(req.refresh_token, session)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh Token 无效或已过期，请重新登录",
        )

    new_access, new_refresh = result

    expires_at = datetime.fromtimestamp(
        time.time() + auth_svc.access_token_expire_hours * 3600,
        tz=timezone.utc,
    ).isoformat()

    return RefreshResponse(
        access_token=new_access,
        refresh_token=new_refresh,
        expires_at=expires_at,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    req: LogoutRequest,
    session: AsyncSession = Depends(get_db),
) -> None:
    """注销

    撤销 refresh_token，客户端应同时清除 access_token。
    即使 refresh_token 无效也返回 204（幂等操作）。
    """
    auth_svc = _get_auth_service()
    await auth_svc.revoke_refresh_token(req.refresh_token, session)


@router.put("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    req: ChangePasswordRequest,
    session: AsyncSession = Depends(get_db),
) -> None:
    """修改密码

    验证旧密码后更新为新密码。
    操作后所有 refresh_token 失效（强制重新登录）。
    """
    auth_svc = _get_auth_service()

    success = await auth_svc.update_password(req.old_password, req.new_password, session)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="旧密码不正确",
        )

    logger.info("密码修改成功")
