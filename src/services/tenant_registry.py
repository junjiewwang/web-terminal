"""租户核心注册表

YAML SSOT 的内存表示，提供：
- 租户认证（bcrypt 密码验证 + JWT 签发）
- Token 验证（JWT 解析 + 过期检查）
- Refresh Token 管理（签发 + 刷新 + 撤销 + Token Rotation）
- 租户查询（按 ID 获取租户信息）
- 并发登录限制（基于 Refresh Token 数量的 FIFO 踢出）

依赖：bcrypt、PyJWT
"""

from __future__ import annotations

import logging
import secrets
import time
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import bcrypt
import jwt
import yaml
from pydantic import ValidationError

from src.models.tenant import (
    RefreshTokenInfo,
    Tenant,
    TenantConfig,
    TenantRole,
    TenantsConfig,
)

logger = logging.getLogger(__name__)

# ── ContextVar：跨 MCP SDK 传递当前租户身份 ──────

current_tenant_var: ContextVar[Tenant | None] = ContextVar(
    "current_tenant", default=None
)
"""MCP 工具中获取当前租户身份的 ContextVar。

由 auth_middleware 在请求处理开始时设置，
MCP 工具通过 current_tenant_var.get() 获取。
"""


class TenantRegistry:
    """租户核心注册表

    线程安全说明：
    - _tenants / _config 在 load/reload 时整体替换（引用赋值是原子的）
    - _refresh_tokens 是内存字典，单线程 asyncio 事件循环内无竞态
    """

    def __init__(self) -> None:
        self._config: TenantsConfig = TenantsConfig()
        self._tenants: dict[str, TenantConfig] = {}
        self._yaml_path: Path | None = None

        # Refresh Token 存储：key = token_value, value = RefreshTokenInfo
        self._refresh_tokens: dict[str, RefreshTokenInfo] = {}

        # 是否已加载配置（用于判断是否启用多租户模式）
        self._loaded = False

    @property
    def loaded(self) -> bool:
        """是否已加载租户配置"""
        return self._loaded

    @property
    def jwt_secret(self) -> str:
        return self._config.jwt_secret

    # ── 配置加载 ──────────────────────────────────

    def load_from_yaml(self, yaml_path: Path) -> None:
        """从 YAML 文件加载租户配置

        Raises:
            FileNotFoundError: YAML 文件不存在
            ValidationError: YAML 结构校验失败
        """
        if not yaml_path.exists():
            logger.warning("tenants.yaml 不存在: %s，多租户模式未启用", yaml_path)
            return

        self._yaml_path = yaml_path

        with open(yaml_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        if not raw:
            logger.warning("tenants.yaml 内容为空，多租户模式未启用")
            return

        try:
            config = TenantsConfig.model_validate(raw)
        except ValidationError as e:
            logger.error("tenants.yaml 校验失败: %s", e)
            raise

        # 构建 ID → TenantConfig 映射
        tenants_map: dict[str, TenantConfig] = {}
        for tc in config.tenants:
            if tc.id in tenants_map:
                logger.warning("重复的租户 ID: %s，后者覆盖前者", tc.id)
            tenants_map[tc.id] = tc

        # 原子替换
        self._config = config
        self._tenants = tenants_map
        self._loaded = True

        logger.info(
            "tenants.yaml 加载完成: %d 个租户 (JWT secret: %s...)",
            len(tenants_map),
            config.jwt_secret[:8] if len(config.jwt_secret) > 8 else "***",
        )

    def reload(self) -> None:
        """热加载：重新读取 YAML 文件"""
        if self._yaml_path:
            self.load_from_yaml(self._yaml_path)

    # ── 认证 ──────────────────────────────────────

    def authenticate(
        self, tenant_id: str, password: str
    ) -> tuple[str, str] | None:
        """验证凭据，成功返回 (access_token, refresh_token)，失败返回 None

        流程：
        1. 查找租户配置
        2. bcrypt 验证密码
        3. 检查租户是否启用
        4. 签发 access_token + refresh_token
        5. 并发登录限制（FIFO 踢出）
        """
        tc = self._tenants.get(tenant_id)
        if not tc:
            return None

        # bcrypt 验证（内置常数时间比较）
        if not bcrypt.checkpw(
            password.encode("utf-8"),
            tc.password_hash.encode("utf-8"),
        ):
            return None

        # 检查租户是否启用
        if not tc.enabled:
            logger.warning("已禁用的租户尝试登录: %s", tenant_id)
            return None

        # 签发 Token
        access_token = self._issue_access_token(tc)
        refresh_token = self._issue_refresh_token(tc)

        # 并发登录限制
        self._enforce_max_sessions(tc)

        logger.info("租户登录成功: %s (role=%s)", tenant_id, tc.role.value)
        return access_token, refresh_token

    def verify_access_token(self, token: str) -> Tenant | None:
        """验证 JWT Access Token，成功返回 Tenant 对象，失败返回 None"""
        try:
            payload = jwt.decode(
                token,
                self._config.jwt_secret,
                algorithms=["HS256"],
            )
        except jwt.ExpiredSignatureError:
            logger.debug("JWT Token 已过期")
            return None
        except jwt.InvalidTokenError as e:
            logger.debug("JWT Token 无效: %s", e)
            return None

        tenant_id = payload.get("sub")
        if not tenant_id:
            return None

        # 从当前配置中获取最新的租户信息（而非 Token 中的快照）
        tc = self._tenants.get(tenant_id)
        if not tc:
            logger.debug("JWT Token 对应的租户不存在: %s", tenant_id)
            return None

        if not tc.enabled:
            logger.debug("JWT Token 对应的租户已禁用: %s", tenant_id)
            return None

        return tc.to_tenant()

    def refresh_access_token(
        self, refresh_token: str
    ) -> tuple[str, str] | None:
        """刷新 Token，成功返回新的 (access_token, refresh_token)

        Token Rotation：旧 refresh_token 失效，签发新的。

        Returns:
            新的 (access_token, refresh_token) 元组，或 None（失败时）
        """
        # 查找并删除旧 refresh_token（Token Rotation）
        info = self._refresh_tokens.pop(refresh_token, None)
        if not info:
            logger.debug("Refresh Token 不存在或已使用")
            return None

        if info.is_expired:
            logger.debug("Refresh Token 已过期: tenant=%s", info.tenant_id)
            return None

        tc = self._tenants.get(info.tenant_id)
        if not tc:
            logger.debug("Refresh Token 对应的租户不存在: %s", info.tenant_id)
            return None

        if not tc.enabled:
            logger.debug("Refresh Token 对应的租户已禁用: %s", info.tenant_id)
            return None

        # 签发新 Token 对
        new_access = self._issue_access_token(tc)
        new_refresh = self._issue_refresh_token(tc)

        logger.info("Token 刷新成功: tenant=%s", info.tenant_id)
        return new_access, new_refresh

    def revoke_refresh_token(self, refresh_token: str) -> bool:
        """撤销 Refresh Token（注销时调用）

        Returns:
            True 表示成功撤销，False 表示 Token 不存在
        """
        info = self._refresh_tokens.pop(refresh_token, None)
        if info:
            logger.info("Refresh Token 已撤销: tenant=%s", info.tenant_id)
            return True
        return False

    def revoke_all_refresh_tokens(self, tenant_id: str) -> int:
        """撤销指定租户的所有 Refresh Token（密码修改/重置时调用）

        Returns:
            撤销的 Token 数量
        """
        to_remove = [
            k for k, v in self._refresh_tokens.items()
            if v.tenant_id == tenant_id
        ]
        for k in to_remove:
            del self._refresh_tokens[k]

        if to_remove:
            logger.info(
                "已撤销租户 %s 的所有 Refresh Token: %d 个",
                tenant_id,
                len(to_remove),
            )
        return len(to_remove)

    # ── 查询 ──────────────────────────────────────

    def get_tenant(self, tenant_id: str) -> Tenant | None:
        """获取租户信息（不含敏感字段）"""
        tc = self._tenants.get(tenant_id)
        return tc.to_tenant() if tc else None

    def get_tenant_config(self, tenant_id: str) -> TenantConfig | None:
        """获取完整租户配置（含 password_hash，仅内部使用）"""
        return self._tenants.get(tenant_id)

    def get_allowed_tags(self, tenant_id: str) -> list[str] | None:
        """获取租户可见的 host tags"""
        tc = self._tenants.get(tenant_id)
        if not tc:
            return None
        # admin 角色不受 tags 限制
        if tc.role.is_admin():
            return None  # None 表示不过滤
        return tc.allowed_tags

    def list_tenants(self) -> list[Tenant]:
        """列出所有租户（不含敏感字段）"""
        return [tc.to_tenant() for tc in self._tenants.values()]

    def get_online_tenant_ids(self) -> set[str]:
        """获取在线租户 ID 集合（拥有有效 Refresh Token 的租户）"""
        self._cleanup_expired_tokens()
        return {
            info.tenant_id
            for info in self._refresh_tokens.values()
        }

    def get_tenant_refresh_token_count(self, tenant_id: str) -> int:
        """获取租户当前有效的 Refresh Token 数量"""
        return sum(
            1 for info in self._refresh_tokens.values()
            if info.tenant_id == tenant_id and not info.is_expired
        )

    # ── YAML 写回（密码修改时使用）──────────────────

    def update_password_hash(
        self, tenant_id: str, new_hash: str
    ) -> bool:
        """更新租户密码哈希（内存 + YAML 文件原子写入）

        Returns:
            True 表示成功，False 表示租户不存在
        """
        tc = self._tenants.get(tenant_id)
        if not tc:
            return False

        # 更新内存
        tc.password_hash = new_hash

        # 写回 YAML（原子写入：先写 .tmp 再 rename）
        if self._yaml_path:
            self._write_yaml()

        # 撤销所有 Refresh Token（强制重新登录）
        self.revoke_all_refresh_tokens(tenant_id)

        logger.info("租户密码已更新: %s", tenant_id)
        return True

    def _write_yaml(self) -> None:
        """将当前配置写回 YAML 文件（原子写入）"""
        if not self._yaml_path:
            return

        data: dict[str, Any] = {
            "jwt_secret": self._config.jwt_secret,
            "access_token_expire_hours": self._config.access_token_expire_hours,
            "refresh_token_expire_days": self._config.refresh_token_expire_days,
            "tenants": [],
        }

        for tc in self._tenants.values():
            tenant_data: dict[str, Any] = {
                "id": tc.id,
                "name": tc.name,
                "password_hash": tc.password_hash,
                "role": tc.role.value,
                "allowed_tags": tc.allowed_tags,
                "max_sessions": tc.max_sessions,
            }
            if not tc.enabled:
                tenant_data["enabled"] = False
            data["tenants"].append(tenant_data)

        tmp_path = self._yaml_path.with_suffix(".yaml.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False)

        tmp_path.rename(self._yaml_path)
        logger.info("tenants.yaml 已写回: %s", self._yaml_path)

    # ── 内部方法 ──────────────────────────────────

    def _issue_access_token(self, tc: TenantConfig) -> str:
        """签发 JWT Access Token"""
        now = time.time()
        expire = now + self._config.access_token_expire_hours * 3600

        payload = {
            "sub": tc.id,
            "name": tc.name,
            "role": tc.role.value,
            "iat": int(now),
            "exp": int(expire),
        }

        return jwt.encode(payload, self._config.jwt_secret, algorithm="HS256")

    def _issue_refresh_token(self, tc: TenantConfig) -> str:
        """签发 Refresh Token（随机 UUID + 内存存储）"""
        token_value = str(uuid.uuid4()) + "-" + secrets.token_urlsafe(16)
        expire = time.time() + self._config.refresh_token_expire_days * 86400

        self._refresh_tokens[token_value] = RefreshTokenInfo(
            tenant_id=tc.id,
            expires_at=expire,
        )

        return token_value

    def _enforce_max_sessions(self, tc: TenantConfig) -> None:
        """并发登录限制：超过 max_sessions 时踢掉最早的

        admin 角色不受限。
        """
        if tc.role.is_admin():
            return

        max_sessions = tc.max_sessions or 3

        # 收集该租户的所有有效 Refresh Token，按创建时间排序
        tenant_tokens = sorted(
            [
                (k, v) for k, v in self._refresh_tokens.items()
                if v.tenant_id == tc.id and not v.is_expired
            ],
            key=lambda x: x[1].created_at,
        )

        # 超限时踢掉最早的
        while len(tenant_tokens) > max_sessions:
            oldest_key, oldest_info = tenant_tokens.pop(0)
            del self._refresh_tokens[oldest_key]
            logger.info(
                "并发登录限制：踢出最早登录 (tenant=%s, created_at=%s)",
                tc.id,
                oldest_info.created_at,
            )

    def _cleanup_expired_tokens(self) -> None:
        """清理过期的 Refresh Token"""
        expired_keys = [
            k for k, v in self._refresh_tokens.items()
            if v.is_expired
        ]
        for k in expired_keys:
            del self._refresh_tokens[k]

        if expired_keys:
            logger.debug("清理了 %d 个过期的 Refresh Token", len(expired_keys))
