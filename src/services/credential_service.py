"""共享凭据业务逻辑服务

职责：
- 凭据 CRUD（加密存储、脱敏返回）
- 引用检查（删除前检查是否有主机引用）
- YAML 同步时的 upsert 支持

设计原则：
- 密码加密/解密统一使用 src.utils.security 模块
- 对外不暴露明文密码（API 层不返回密码）
- 删除前检查引用，防止悬挂引用
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.credential import (
    Credential,
    CredentialCreate,
    CredentialNameItem,
    CredentialResponse,
    CredentialUpdate,
)
from src.models.host import Host
from src.utils.security import decrypt_password, encrypt_password

logger = logging.getLogger(__name__)


class CredentialService:
    """共享凭据服务"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── 查询 ──────────────────────────────────────

    async def list_credentials(self) -> list[CredentialResponse]:
        """获取凭据列表（脱敏，附带引用数）"""
        result = await self._session.execute(
            select(Credential).order_by(Credential.name)
        )
        credentials = result.scalars().all()

        responses: list[CredentialResponse] = []
        for cred in credentials:
            ref_count = await self._count_refs(cred.name)
            responses.append(
                CredentialResponse(
                    id=cred.id,
                    name=cred.name,
                    description=cred.description,
                    has_password=bool(cred.password_encrypted),
                    ref_count=ref_count,
                    created_at=cred.created_at,
                    updated_at=cred.updated_at,
                )
            )
        return responses

    async def list_names(self) -> list[CredentialNameItem]:
        """获取凭据名称列表（下拉选择用）"""
        result = await self._session.execute(
            select(Credential.name, Credential.description).order_by(Credential.name)
        )
        rows = result.all()
        return [CredentialNameItem(name=row.name, description=row.description) for row in rows]

    async def get_by_name(self, name: str) -> Credential | None:
        """按名称获取凭据"""
        result = await self._session.execute(
            select(Credential).where(Credential.name == name)
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, credential_id: int) -> Credential | None:
        """按 ID 获取凭据"""
        result = await self._session.execute(
            select(Credential).where(Credential.id == credential_id)
        )
        return result.scalar_one_or_none()

    async def get_decrypted_password(self, name: str) -> str | None:
        """获取凭据的解密密码（内部使用，不暴露给 API）"""
        cred = await self.get_by_name(name)
        if cred is None or not cred.password_encrypted:
            return None
        try:
            return decrypt_password(cred.password_encrypted)
        except ValueError:
            logger.error("凭据 '%s' 密码解密失败", name)
            return None

    # ── 创建 ──────────────────────────────────────

    async def create(self, data: CredentialCreate) -> Credential:
        """创建凭据"""
        encrypted = encrypt_password(data.password)
        cred = Credential(
            name=data.name,
            password_encrypted=encrypted,
            description=data.description,
        )
        self._session.add(cred)
        await self._session.flush()
        logger.info("创建凭据: %s", data.name)
        return cred

    # ── 更新 ──────────────────────────────────────

    async def update(self, credential_id: int, data: CredentialUpdate) -> Credential | None:
        """更新凭据（部分更新）"""
        cred = await self.get_by_id(credential_id)
        if cred is None:
            return None

        if data.password is not None:
            cred.password_encrypted = encrypt_password(data.password)
        if data.description is not None:
            cred.description = data.description

        await self._session.flush()
        logger.info("更新凭据: %s (id=%d)", cred.name, cred.id)
        return cred

    # ── 删除 ──────────────────────────────────────

    async def delete(self, credential_id: int) -> tuple[bool, str]:
        """删除凭据

        Returns:
            (success, message) — 被引用时拒绝删除并返回引用列表
        """
        cred = await self.get_by_id(credential_id)
        if cred is None:
            return False, "凭据不存在"

        # 检查引用
        refs = await self._find_refs(cred.name)
        if refs:
            ref_names = ", ".join(refs[:5])
            suffix = f"等 {len(refs)} 个" if len(refs) > 5 else ""
            return False, f"凭据 '{cred.name}' 正在被以下主机引用: {ref_names}{suffix}"

        await self._session.delete(cred)
        await self._session.flush()
        logger.info("删除凭据: %s (id=%d)", cred.name, cred.id)
        return True, "删除成功"

    # ── YAML 同步 upsert ──────────────────────────

    async def upsert_from_yaml(self, name: str, password: str, description: str | None = None) -> None:
        """YAML 同步时 upsert 凭据（按 name 匹配）"""
        cred = await self.get_by_name(name)
        encrypted = encrypt_password(password)

        if cred is None:
            cred = Credential(
                name=name,
                password_encrypted=encrypted,
                description=description,
            )
            self._session.add(cred)
            logger.debug("YAML 同步 - 新增凭据: %s", name)
        else:
            cred.password_encrypted = encrypted
            if description is not None:
                cred.description = description
            logger.debug("YAML 同步 - 更新凭据: %s", name)

        await self._session.flush()

    # ── 内部辅助 ──────────────────────────────────

    async def _count_refs(self, name: str) -> int:
        """统计凭据被引用次数"""
        result = await self._session.execute(
            select(func.count(Host.id)).where(Host.credential_ref == name)
        )
        return result.scalar_one()

    async def _find_refs(self, name: str) -> list[str]:
        """获取引用该凭据的主机名列表"""
        result = await self._session.execute(
            select(Host.name).where(Host.credential_ref == name)
        )
        return [row[0] for row in result.all()]
