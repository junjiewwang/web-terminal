"""单用户认证服务

提供密码保护的认证能力：
- 密码验证（bcrypt）
- JWT Access Token 签发 & 验证
- Refresh Token 签发 & 刷新 & 撤销（Token Rotation，持久化到 DB）
- 密码修改
- 配置数据库持久化

数据流：
  首次启动 → 从 config/auth.yaml 种子初始化写入 DB
  后续启动 → 直接从 DB 读取
  运行时    → 所有变更写入 DB（DB 是 SSOT）

依赖：bcrypt、PyJWT、SQLAlchemy
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import bcrypt
import jwt
import yaml
from pydantic import BaseModel
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.auth import AuthConfigModel, RefreshTokenModel

logger = logging.getLogger(__name__)


# ── 配置模型（内存缓存 + Pydantic 校验）──────────

class AuthConfig(BaseModel):
    """认证配置（对应 DB auth_config 表的内存映射）"""

    enabled: bool = True
    password_hash: str = ""
    jwt_secret: str = "change-me-in-production"
    access_token_expire_hours: float = 2.0
    refresh_token_expire_days: int = 7


# ── 认证服务 ──────────────────────────────────


class AuthService:
    """单用户认证服务（数据库持久化版）

    生命周期：
    1. 构造时为空壳
    2. init_from_db() 异步初始化：从 DB 加载配置，若 DB 为空则从 YAML 种子写入
    3. 运行时通过 DB 会话读写 refresh token
    """

    def __init__(self) -> None:
        self._config = AuthConfig()
        self._loaded = False

    @property
    def loaded(self) -> bool:
        """是否已加载配置"""
        return self._loaded

    @property
    def is_auth_enabled(self) -> bool:
        """是否启用了密码保护"""
        return self._loaded and self._config.enabled

    @property
    def jwt_secret(self) -> str:
        return self._config.jwt_secret

    @property
    def access_token_expire_hours(self) -> float:
        return self._config.access_token_expire_hours

    @property
    def config(self) -> AuthConfig:
        """获取当前配置快照（只读）"""
        return self._config.model_copy()

    # ── 初始化 ──────────────────────────────────────

    async def init_from_db(self, session: AsyncSession, yaml_path: Path | None = None) -> None:
        """从数据库加载认证配置，若 DB 为空则从 YAML 种子初始化。

        Args:
            session: 数据库会话
            yaml_path: auth.yaml 路径（种子文件），为 None 则不做种子初始化
        """
        # 尝试从 DB 加载
        result = await session.execute(select(AuthConfigModel).where(AuthConfigModel.id == 1))
        db_config = result.scalar_one_or_none()

        if db_config is not None:
            # DB 中已有配置，直接加载
            self._load_from_db_model(db_config)
            logger.info("认证配置从数据库加载完成: %s", "启用 ✓" if self._config.enabled else "未启用")
        else:
            # DB 为空，尝试从 YAML 种子初始化
            seed_config = self._load_yaml_seed(yaml_path)
            await self._seed_to_db(session, seed_config)
            self._config = seed_config
            self._loaded = True
            logger.info("认证配置从 auth.yaml 种子初始化到数据库")

        # 支持环境变量覆盖 JWT Secret
        env_secret = os.environ.get("WETTY_JWT_SECRET")
        if env_secret:
            self._config.jwt_secret = env_secret
            logger.info("JWT Secret 已被环境变量 WETTY_JWT_SECRET 覆盖")

    async def reload_from_db(self, session: AsyncSession) -> None:
        """从数据库重新加载配置（热刷新）"""
        result = await session.execute(select(AuthConfigModel).where(AuthConfigModel.id == 1))
        db_config = result.scalar_one_or_none()
        if db_config:
            self._load_from_db_model(db_config)

    def _load_from_db_model(self, model: AuthConfigModel) -> None:
        """从 ORM 模型加载到内存配置"""
        self._config = AuthConfig(
            enabled=model.enabled,
            password_hash=model.password_hash,
            jwt_secret=model.jwt_secret,
            access_token_expire_hours=model.access_token_expire_hours,
            refresh_token_expire_days=model.refresh_token_expire_days,
        )
        self._loaded = True

    def _load_yaml_seed(self, yaml_path: Path | None) -> AuthConfig:
        """从 YAML 文件加载种子配置"""
        if yaml_path and yaml_path.exists():
            try:
                with open(yaml_path, encoding="utf-8") as f:
                    raw = yaml.safe_load(f)
                if raw:
                    return AuthConfig.model_validate(raw)
            except Exception:
                logger.warning("auth.yaml 解析失败，使用默认配置")

        logger.warning("auth.yaml 不存在或为空，使用默认配置（密码保护未启用）")
        return AuthConfig(enabled=False)

    async def _seed_to_db(self, session: AsyncSession, config: AuthConfig) -> None:
        """将种子配置写入数据库"""
        db_model = AuthConfigModel(
            id=1,
            enabled=config.enabled,
            password_hash=config.password_hash,
            jwt_secret=config.jwt_secret,
            access_token_expire_hours=config.access_token_expire_hours,
            refresh_token_expire_days=config.refresh_token_expire_days,
        )
        session.add(db_model)
        await session.flush()

    # ── 认证 ──────────────────────────────────────

    def authenticate(self, password: str) -> tuple[str, str] | None:
        """验证密码，成功返回 (access_token, refresh_token_plaintext)，失败返回 None

        注意：调用者需要自行将 refresh_token 持久化到 DB。
        使用 authenticate_and_persist() 代替，可自动持久化。
        """
        if not self._config.password_hash:
            logger.warning("未配置密码哈希，认证失败")
            return None

        if not bcrypt.checkpw(
            password.encode("utf-8"),
            self._config.password_hash.encode("utf-8"),
        ):
            return None

        access_token = self._issue_access_token()
        refresh_token = self._generate_refresh_token()

        logger.info("登录成功")
        return access_token, refresh_token

    async def authenticate_and_persist(
        self, password: str, session: AsyncSession
    ) -> tuple[str, str] | None:
        """验证密码并持久化 refresh token 到数据库

        若首次验证失败，会尝试从 DB 重新加载配置后再试一次。
        这样 CLI reset_password 后无需重启即可生效。
        """
        result = self.authenticate(password)
        if not result:
            # 可能 CLI 已更新了 DB 中的密码，尝试从 DB 刷新后再试
            await self.reload_from_db(session)
            result = self.authenticate(password)
            if not result:
                return None

        access_token, refresh_token = result
        await self._persist_refresh_token(refresh_token, session)
        return access_token, refresh_token

    def verify_access_token(self, token: str) -> bool:
        """验证 JWT Access Token"""
        try:
            jwt.decode(token, self._config.jwt_secret, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            logger.debug("JWT Token 已过期")
            return False
        except jwt.InvalidTokenError as e:
            logger.debug("JWT Token 无效: %s", e)
            return False
        return True

    async def refresh_access_token(
        self, refresh_token: str, session: AsyncSession
    ) -> tuple[str, str] | None:
        """刷新 Token（Token Rotation）

        旧 refresh_token 立即撤销，签发新的。
        """
        token_hash = self._hash_token(refresh_token)

        # 查找有效的 token
        result = await session.execute(
            select(RefreshTokenModel).where(
                RefreshTokenModel.token_hash == token_hash,
                RefreshTokenModel.revoked == False,  # noqa: E712
            )
        )
        token_record = result.scalar_one_or_none()

        if not token_record:
            logger.debug("Refresh Token 不存在或已撤销")
            return None

        if token_record.expires_at < datetime.now(timezone.utc).replace(tzinfo=None):
            logger.debug("Refresh Token 已过期")
            # 标记为已撤销
            token_record.revoked = True
            await session.flush()
            return None

        # Token Rotation: 撤销旧 token
        token_record.revoked = True

        # 签发新 token
        new_access = self._issue_access_token()
        new_refresh = self._generate_refresh_token()
        await self._persist_refresh_token(new_refresh, session)

        logger.info("Token 刷新成功")
        return new_access, new_refresh

    async def revoke_refresh_token(self, refresh_token: str, session: AsyncSession) -> bool:
        """撤销单个 Refresh Token"""
        token_hash = self._hash_token(refresh_token)
        result = await session.execute(
            update(RefreshTokenModel)
            .where(RefreshTokenModel.token_hash == token_hash)
            .values(revoked=True)
        )
        if result.rowcount > 0:
            logger.info("Refresh Token 已撤销")
            return True
        return False

    async def revoke_all_refresh_tokens(self, session: AsyncSession) -> int:
        """撤销所有 Refresh Token（密码修改/重置时调用）"""
        result = await session.execute(
            update(RefreshTokenModel)
            .where(RefreshTokenModel.revoked == False)  # noqa: E712
            .values(revoked=True)
        )
        count = result.rowcount
        if count:
            logger.info("已撤销所有 Refresh Token: %d 个", count)
        return count

    # ── 密码修改 ──────────────────────────────────

    async def update_password(
        self, old_password: str, new_password: str, session: AsyncSession
    ) -> bool:
        """验证旧密码后更新为新密码

        成功时：DB 更新 + 内存更新 + 所有 Refresh Token 失效。
        """
        if not bcrypt.checkpw(
            old_password.encode("utf-8"),
            self._config.password_hash.encode("utf-8"),
        ):
            return False

        new_hash = bcrypt.hashpw(
            new_password.encode("utf-8"),
            bcrypt.gensalt(rounds=12),
        ).decode("utf-8")

        # 更新 DB
        await session.execute(
            update(AuthConfigModel)
            .where(AuthConfigModel.id == 1)
            .values(password_hash=new_hash)
        )

        # 更新内存
        self._config.password_hash = new_hash

        # 撤销所有 Refresh Token
        await self.revoke_all_refresh_tokens(session)

        logger.info("密码已更新")
        return True

    async def reset_password(self, new_password: str, session: AsyncSession) -> None:
        """强制重置密码（CLI 直连 DB 使用，不验证旧密码）"""
        new_hash = bcrypt.hashpw(
            new_password.encode("utf-8"),
            bcrypt.gensalt(rounds=12),
        ).decode("utf-8")

        await session.execute(
            update(AuthConfigModel)
            .where(AuthConfigModel.id == 1)
            .values(password_hash=new_hash)
        )

        # 撤销所有 Refresh Token
        await self.revoke_all_refresh_tokens(session)

        # 更新内存
        self._config.password_hash = new_hash
        logger.info("密码已重置（CLI 强制）")

    # ── 设置更新 ────────────────────────────────────

    async def update_config(
        self, session: AsyncSession, **kwargs: Any
    ) -> None:
        """更新认证配置（部分字段更新）

        支持的字段：enabled, jwt_secret, access_token_expire_hours, refresh_token_expire_days
        """
        allowed_fields = {"enabled", "jwt_secret", "access_token_expire_hours", "refresh_token_expire_days"}
        updates = {k: v for k, v in kwargs.items() if k in allowed_fields}

        if not updates:
            return

        await session.execute(
            update(AuthConfigModel).where(AuthConfigModel.id == 1).values(**updates)
        )

        # 同步更新内存
        for k, v in updates.items():
            setattr(self._config, k, v)

        logger.info("认证配置已更新: %s", list(updates.keys()))

    # ── 清理 ────────────────────────────────────────

    async def cleanup_expired_tokens(self, session: AsyncSession) -> int:
        """清理过期和已撤销的 Refresh Token"""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        result = await session.execute(
            delete(RefreshTokenModel).where(
                (RefreshTokenModel.revoked == True)  # noqa: E712
                | (RefreshTokenModel.expires_at < now)
            )
        )
        count = result.rowcount
        if count:
            logger.debug("清理了 %d 个过期/已撤销的 Refresh Token", count)
        return count

    # ── 内部方法 ──────────────────────────────────

    def _issue_access_token(self) -> str:
        """签发 JWT Access Token"""
        now = time.time()
        expire = now + self._config.access_token_expire_hours * 3600

        payload = {
            "sub": "owner",
            "iat": int(now),
            "exp": int(expire),
        }

        return jwt.encode(payload, self._config.jwt_secret, algorithm="HS256")

    def _generate_refresh_token(self) -> str:
        """生成 Refresh Token 明文（UUID + 随机字节）"""
        return str(uuid.uuid4()) + "-" + secrets.token_urlsafe(16)

    async def _persist_refresh_token(self, token: str, session: AsyncSession) -> None:
        """将 Refresh Token 哈希后持久化到数据库"""
        from datetime import timedelta

        token_hash = self._hash_token(token)
        expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
            days=self._config.refresh_token_expire_days
        )

        record = RefreshTokenModel(
            token_hash=token_hash,
            expires_at=expires_at,
        )
        session.add(record)
        await session.flush()

    @staticmethod
    def _hash_token(token: str) -> str:
        """SHA-256 哈希 Token（数据库只存哈希，不存明文）"""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()
