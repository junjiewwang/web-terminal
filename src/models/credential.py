"""共享凭据数据模型

ORM 表：
- credentials: 存储共享凭据（密码加密存储）

设计原则：
- 凭据独立管理，按 name 唯一引用
- 主机节点通过 credential_ref 字段引用凭据名称
- 密码使用 Fernet 对称加密存储（同 host 密码加密方式）
- YAML 同步时 upsert 到本表，Web 创建的凭据也持久化在同一张表中
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.models.host import Base


class Credential(Base):
    """共享凭据 ORM 模型"""

    __tablename__ = "credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False, index=True, comment="凭据名称（唯一标识）"
    )
    password_encrypted: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Fernet 加密后的密码"
    )
    description: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="凭据用途描述"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), comment="更新时间"
    )


# ── Pydantic Schemas ──────────────────────────────


class CredentialCreate(BaseModel):
    """创建凭据请求"""

    name: str = Field(..., min_length=1, max_length=128, description="凭据名称（唯一）")
    password: str = Field(..., min_length=1, description="密码明文（存储时加密）")
    description: str | None = Field(default=None, description="凭据用途描述")


class CredentialUpdate(BaseModel):
    """更新凭据请求（部分更新）"""

    password: str | None = Field(default=None, min_length=1, description="新密码（留空不更新）")
    description: str | None = Field(default=None, description="新描述")


class CredentialResponse(BaseModel):
    """凭据列表响应（脱敏，不返回密码）"""

    id: int
    name: str
    description: str | None = None
    has_password: bool = Field(default=True, description="是否已设置密码")
    ref_count: int = Field(default=0, description="被引用次数")
    created_at: datetime
    updated_at: datetime


class CredentialNameItem(BaseModel):
    """凭据名称条目（下拉选择用）"""

    name: str
    description: str | None = None
