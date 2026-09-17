"""交互规则数据模型

ORM 表：interaction_rules - 用户自定义的登录交互规则

设计原则：
- 规则是通用的"匹配正则 → 发送内容"抽象，不含任何业务语义
- 规则由用户通过页面配置，连接引擎在节点 steps 为空时按优先级自动应用
- send_text 支持变量：{{password}} / {{user_id}} / {{username}} / {{manual}}
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.models.host import Base


class InteractionRule(Base):
    """交互规则 ORM 模型"""

    __tablename__ = "interaction_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False, index=True, comment="规则名称（唯一）"
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="规则说明")
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, comment="匹配优先级，数字越大越优先"
    )
    wait_pattern: Mapped[str] = mapped_column(Text, nullable=False, comment="触发正则")
    send_text: Mapped[str] = mapped_column(
        Text, nullable=False, comment="匹配后发送的内容（支持变量替换）"
    )
    timeout: Mapped[float] = mapped_column(Float, nullable=False, default=15.0, comment="超时秒数")
    max_retries: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, comment="单次连接中最多应用次数"
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否启用")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), comment="更新时间"
    )


# ── Pydantic Schemas ──────────────────────────────


class InteractionRuleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128, description="规则名称（唯一）")
    description: str | None = Field(default=None, description="规则说明")
    priority: int = Field(default=100, ge=0, le=10000, description="优先级，数字越大越优先")
    wait_pattern: str = Field(..., min_length=1, description="触发正则")
    send_text: str = Field(..., min_length=1, description="匹配后发送的内容")
    timeout: float = Field(default=15.0, ge=1.0, le=120.0, description="超时秒数")
    max_retries: int = Field(default=1, ge=1, le=10, description="单次连接最多应用次数")
    enabled: bool = Field(default=True, description="是否启用")


class InteractionRuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    priority: int | None = Field(default=None, ge=0, le=10000)
    wait_pattern: str | None = Field(default=None, min_length=1)
    send_text: str | None = Field(default=None, min_length=1)
    timeout: float | None = Field(default=None, ge=1.0, le=120.0)
    max_retries: int | None = Field(default=None, ge=1, le=10)
    enabled: bool | None = None


class InteractionRuleResponse(BaseModel):
    id: int
    name: str
    description: str | None = None
    priority: int
    wait_pattern: str
    send_text: str
    timeout: float
    max_retries: int
    enabled: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class InteractionRuleExample(BaseModel):
    """示例规则（页面参考展示，不落库、不参与匹配）"""

    name: str
    description: str
    priority: int
    wait_pattern: str
    send_text: str
    timeout: float
    max_retries: int
