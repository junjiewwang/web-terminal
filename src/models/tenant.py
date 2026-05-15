"""租户数据模型

定义多租户认证系统的核心数据结构：
- TenantRole: 角色枚举（super_admin / admin / user）
- Tenant: 单个租户信息（不含敏感字段如 password_hash）
- TenantConfig: 单个租户配置（含 password_hash，用于 YAML 加载）
- TenantsConfig: 租户配置文件整体结构
- RefreshTokenInfo: Refresh Token 元数据
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from pydantic import BaseModel, Field


class TenantRole(str, Enum):
    """租户角色枚举

    三级角色：
    - SUPER_ADMIN: 超级管理员（完全权限 + 管理其他 admin）
    - ADMIN: 管理员（全部主机可见 + 查看所有会话/审计日志）
    - USER: 普通用户（仅 allowed_tags 匹配的主机 + 仅自己的会话）
    """

    SUPER_ADMIN = "super_admin"
    ADMIN = "admin"
    USER = "user"

    def is_admin(self) -> bool:
        """是否具有管理员权限（admin 或 super_admin）"""
        return self in (TenantRole.SUPER_ADMIN, TenantRole.ADMIN)


class Tenant(BaseModel):
    """租户身份信息（不含敏感字段）

    用于 request.state.tenant 注入和 API 响应，
    不包含 password_hash 等敏感字段。
    """

    id: str
    name: str
    role: TenantRole = TenantRole.USER
    allowed_tags: list[str] = Field(default_factory=list)
    max_sessions: int = 3

    @property
    def is_admin(self) -> bool:
        """是否具有管理员权限"""
        return self.role.is_admin()


class TenantConfig(BaseModel):
    """单个租户配置（含密码哈希，用于 YAML 加载）

    比 Tenant 多了 password_hash 字段，
    仅在 TenantRegistry 内部使用。
    """

    id: str
    name: str
    password_hash: str
    role: TenantRole = TenantRole.USER
    allowed_tags: list[str] = Field(default_factory=list)
    max_sessions: int = 3
    enabled: bool = True

    def to_tenant(self) -> Tenant:
        """转换为不含敏感字段的 Tenant 对象"""
        return Tenant(
            id=self.id,
            name=self.name,
            role=self.role,
            allowed_tags=self.allowed_tags,
            max_sessions=self.max_sessions,
        )


class TenantsConfig(BaseModel):
    """租户配置文件整体结构（对应 config/tenants.yaml）"""

    jwt_secret: str = "change-me-in-production"
    access_token_expire_hours: float = 2.0
    refresh_token_expire_days: int = 7
    tenants: list[TenantConfig] = Field(default_factory=list)


@dataclass
class RefreshTokenInfo:
    """Refresh Token 元数据

    存储在 TenantRegistry 内存字典中，
    key 为 refresh_token 值。
    """

    tenant_id: str
    expires_at: float  # Unix timestamp
    created_at: float = field(default_factory=time.time)

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at


# ── 系统租户（开发模式 / 环境变量 Token 使用）──────

SYSTEM_TENANT = Tenant(
    id="_system",
    name="System",
    role=TenantRole.SUPER_ADMIN,
    allowed_tags=[],
    max_sessions=999,
)
"""系统内置租户

用于以下场景：
- 开发模式（无 tenants.yaml 且无 WETTY_API_TOKEN）
- 环境变量 WETTY_API_TOKEN 认证（全局 Token，视为 admin 身份）
- 自动生成 Token 认证（旧版向后兼容）
"""
