"""认证相关数据模型

ORM 表：
- auth_config: 单行配置表（认证启用状态、密码哈希、JWT 配置等）
- refresh_tokens: Refresh Token 持久化（替代内存存储）

设计原则：
- auth_config 表固定单行（id=1），通过 upsert 保证幂等
- refresh_tokens 支持 Token Rotation + 自动过期清理
- 首次启动从 config/auth.yaml 种子初始化到 DB，后续以 DB 为 SSOT
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.models.host import Base


class AuthConfigModel(Base):
    """认证配置表（单行）

    始终只有一行记录（id=1），存储全局认证配置。
    """

    __tablename__ = "auth_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否启用密码保护")
    password_hash: Mapped[str] = mapped_column(Text, nullable=False, default="", comment="bcrypt 密码哈希")
    jwt_secret: Mapped[str] = mapped_column(
        String(256), nullable=False, default="change-me-in-production", comment="JWT 签名密钥"
    )
    access_token_expire_hours: Mapped[float] = mapped_column(
        Float, nullable=False, default=2.0, comment="Access Token 过期时间（小时）"
    )
    refresh_token_expire_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=7, comment="Refresh Token 过期时间（天）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now(), comment="最后更新时间"
    )


class RefreshTokenModel(Base):
    """Refresh Token 持久化表

    支持 Token Rotation：每次刷新生成新 token，旧 token 标记 revoked。
    过期和已撤销的 token 由定期清理任务删除。
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False, index=True, comment="Token 哈希（SHA-256）"
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="过期时间"
    )
    revoked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="是否已撤销"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), comment="创建时间"
    )
