"""认证 REST API

提供租户登录/刷新/注销/密码修改等认证端点。

端点列表：
- POST /api/auth/login     — 登录（返回 access_token + refresh_token）
- POST /api/auth/refresh   — 刷新 Token（Token Rotation）
- POST /api/auth/logout    — 注销（撤销 refresh_token）
- PUT  /api/auth/password  — 修改密码（验证旧密码 + 新密码更新）
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import bcrypt
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from src.models.tenant import Tenant, TenantRole

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# ── 注入点（由 main.py 设置）──────────────────────
# 延迟导入避免循环引用，使用模块级变量注入
_tenant_registry = None


def _get_registry():
    """获取 TenantRegistry 实例"""
    if _tenant_registry is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="认证服务未初始化",
        )
    return _tenant_registry


# ── 请求/响应模型 ──────────────────────────────────


class LoginRequest(BaseModel):
    """登录请求"""
    tenant_id: str = Field(..., min_length=1, description="租户 ID")
    password: str = Field(..., min_length=1, description="密码")


class LoginResponse(BaseModel):
    """登录响应"""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    tenant_id: str
    name: str
    role: str
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
    开发模式（无 tenants.yaml 且无 WETTY_API_TOKEN）返回 auth_required=false。
    """
    import os
    registry = _get_registry() if _tenant_registry else None
    has_env_token = bool(os.environ.get("WETTY_API_TOKEN"))
    has_tenants = registry is not None and registry.loaded

    return AuthStatusResponse(
        auth_required=has_env_token or has_tenants,
    )


@router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest) -> LoginResponse:
    """租户登录

    验证凭据后签发 access_token + refresh_token。
    access_token 有效期短（默认 2h），refresh_token 有效期长（默认 7d）。
    """
    registry = _get_registry()

    result = registry.authenticate(req.tenant_id, req.password)
    if not result:
        logger.warning("登录失败: tenant_id=%s", req.tenant_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="账号或密码错误",
        )

    access_token, refresh_token = result

    tenant = registry.get_tenant(req.tenant_id)
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="内部错误：认证成功但无法获取租户信息",
        )

    # 计算过期时间
    expire_hours = registry._config.access_token_expire_hours
    expires_at = datetime.fromtimestamp(
        time.time() + expire_hours * 3600,
        tz=timezone.utc,
    ).isoformat()

    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        tenant_id=tenant.id,
        name=tenant.name,
        role=tenant.role.value,
        expires_at=expires_at,
    )


@router.post("/refresh", response_model=RefreshResponse)
async def refresh_token(req: RefreshRequest) -> RefreshResponse:
    """刷新 Token

    使用 refresh_token 换取新的 access_token + refresh_token。
    Token Rotation：旧 refresh_token 立即失效。
    """
    registry = _get_registry()

    result = registry.refresh_access_token(req.refresh_token)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh Token 无效或已过期，请重新登录",
        )

    new_access, new_refresh = result

    expire_hours = registry._config.access_token_expire_hours
    expires_at = datetime.fromtimestamp(
        time.time() + expire_hours * 3600,
        tz=timezone.utc,
    ).isoformat()

    return RefreshResponse(
        access_token=new_access,
        refresh_token=new_refresh,
        expires_at=expires_at,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(req: LogoutRequest) -> None:
    """注销

    撤销 refresh_token，客户端应同时清除 access_token。
    即使 refresh_token 无效也返回 204（幂等操作）。
    """
    registry = _get_registry()
    registry.revoke_refresh_token(req.refresh_token)


@router.put("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    req: ChangePasswordRequest,
    request: Request,
) -> None:
    """修改密码

    验证旧密码后更新为新密码。
    操作后该租户的所有 refresh_token 失效（强制重新登录）。
    """
    registry = _get_registry()

    # 从 request.state 获取当前租户
    tenant: Tenant | None = getattr(request.state, "tenant", None)
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录",
        )

    # 获取完整配置（含 password_hash）
    tc = registry.get_tenant_config(tenant.id)
    if not tc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="租户不存在",
        )

    # 验证旧密码
    if not bcrypt.checkpw(
        req.old_password.encode("utf-8"),
        tc.password_hash.encode("utf-8"),
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="旧密码不正确",
        )

    # 生成新哈希
    new_hash = bcrypt.hashpw(
        req.new_password.encode("utf-8"),
        bcrypt.gensalt(rounds=12),
    ).decode("utf-8")

    # 更新密码（内存 + YAML + 撤销 refresh_token）
    registry.update_password_hash(tenant.id, new_hash)

    logger.info("密码修改成功: tenant=%s", tenant.id)
