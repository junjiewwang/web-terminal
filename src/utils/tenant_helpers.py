"""租户权限工具

提供 FastAPI 请求中获取租户身份的公共 helper 函数，
避免在各 API 模块中重复 getattr(request.state, "tenant", ...) 逻辑。

使用方式：
    from src.utils.tenant_helpers import get_current_tenant, require_admin

    @router.get("/api/xxx")
    async def handler(request: Request):
        tenant = get_current_tenant(request)  # 永远返回 Tenant，开发模式返回 SYSTEM_TENANT
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from src.models.tenant import SYSTEM_TENANT, Tenant


def get_current_tenant(request: Request) -> Tenant:
    """从 request.state 获取当前租户，开发模式降级为 SYSTEM_TENANT。

    auth_middleware 已将 Tenant 注入 request.state.tenant，
    此函数仅做安全提取。
    """
    return getattr(request.state, "tenant", SYSTEM_TENANT)


def require_admin(request: Request) -> Tenant:
    """要求当前用户具有 admin 权限，否则抛出 403。

    用于 admin API 的前置校验。
    """
    tenant = get_current_tenant(request)
    if not tenant.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return tenant


def require_super_admin(request: Request) -> Tenant:
    """要求当前用户为 super_admin，否则抛出 403。"""
    tenant = get_current_tenant(request)
    if tenant.role.value != "super_admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要超级管理员权限",
        )
    return tenant
