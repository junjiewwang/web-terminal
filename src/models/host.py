"""主机资产数据模型

包含 SQLAlchemy ORM 模型和 Pydantic Schema，
实现数据层与接口层的职责分离。
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, Enum, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase


# ──────────────────────────────────────────────
# SQLAlchemy ORM 模型
# ──────────────────────────────────────────────


class Base(DeclarativeBase):
    """SQLAlchemy 声明式基类"""


class AuthType(str, enum.Enum):
    """SSH 认证方式"""

    KEY = "key"
    PASSWORD = "password"


class Host(Base):
    """主机资产 ORM 模型"""

    __tablename__ = "hosts"

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    name: str = Column(String(128), unique=True, nullable=False, index=True, comment="主机别名")
    hostname: str = Column(String(256), nullable=False, comment="IP 或域名")
    port: int = Column(Integer, nullable=False, default=22, comment="SSH 端口")
    username: str = Column(String(128), nullable=False, comment="SSH 用户名")
    auth_type: AuthType = Column(
        Enum(AuthType), nullable=False, default=AuthType.KEY, comment="认证方式: key | password"
    )
    private_key_path: Optional[str] = Column(String(512), nullable=True, comment="私钥路径 (auth_type=key)")
    password_encrypted: Optional[str] = Column(Text, nullable=True, comment="加密后的密码 (auth_type=password)")
    description: Optional[str] = Column(Text, nullable=True, comment="主机描述")
    tags: Optional[str] = Column(Text, nullable=True, comment="标签，逗号分隔存储")
    created_at: datetime = Column(DateTime, server_default=func.now(), comment="创建时间")
    updated_at: datetime = Column(DateTime, server_default=func.now(), onupdate=func.now(), comment="更新时间")

    def __repr__(self) -> str:
        return f"<Host(id={self.id}, name='{self.name}', hostname='{self.hostname}')>"


# ──────────────────────────────────────────────
# Pydantic Schema（API 层数据校验）
# ──────────────────────────────────────────────


class HostBase(BaseModel):
    """主机基础字段"""

    hostname: str = Field(..., min_length=1, max_length=256, description="IP 或域名")
    port: int = Field(default=22, ge=1, le=65535, description="SSH 端口")
    username: str = Field(..., min_length=1, max_length=128, description="SSH 用户名")
    auth_type: AuthType = Field(default=AuthType.KEY, description="认证方式")
    private_key_path: Optional[str] = Field(default=None, max_length=512, description="私钥路径")
    description: Optional[str] = Field(default=None, description="主机描述")
    tags: Optional[list[str]] = Field(default=None, description="标签列表")


class HostCreate(HostBase):
    """创建主机请求"""

    name: str = Field(..., min_length=1, max_length=128, description="主机别名（唯一）")
    password: Optional[str] = Field(default=None, description="SSH 密码（仅 auth_type=password 时使用）")


class HostUpdate(BaseModel):
    """更新主机请求（所有字段可选）"""

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    hostname: Optional[str] = Field(default=None, min_length=1, max_length=256)
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    username: Optional[str] = Field(default=None, min_length=1, max_length=128)
    auth_type: Optional[AuthType] = None
    private_key_path: Optional[str] = Field(default=None, max_length=512)
    password: Optional[str] = Field(default=None, description="新密码，传入时会重新加密")
    description: Optional[str] = None
    tags: Optional[list[str]] = None


class HostResponse(BaseModel):
    """主机响应（返回给前端）"""

    id: int
    name: str
    hostname: str
    port: int
    username: str
    auth_type: AuthType
    private_key_path: Optional[str] = None
    description: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_model(cls, host: Host) -> HostResponse:
        """从 ORM 模型构建响应，处理 tags 的 CSV -> list 转换"""
        tags = [t.strip() for t in host.tags.split(",") if t.strip()] if host.tags else []
        return cls(
            id=host.id,
            name=host.name,
            hostname=host.hostname,
            port=host.port,
            username=host.username,
            auth_type=host.auth_type,
            private_key_path=host.private_key_path,
            description=host.description,
            tags=tags,
            created_at=host.created_at,
            updated_at=host.updated_at,
        )
