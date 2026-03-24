"""主机资产管理服务

封装主机 CRUD 操作和 YAML 配置同步逻辑，
所有数据库操作集中在此，上层（API / MCP）只调用服务方法。

设计原则：
- hosts.yaml 是 Single Source of Truth，DB 是运行时缓存
- sync_from_yaml() 实现完整的增/改/删同步（非仅导入）
- Pydantic 校验先行：任何一条格式错误则整批拒绝
- 事务原子性：所有变更在一个事务中完成
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.host import AuthType, Host, HostCreate, HostUpdate
from src.utils.security import decrypt_password, encrypt_password

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    """YAML 同步结果"""

    added: int = 0
    updated: int = 0
    deleted: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total_changes(self) -> int:
        return self.added + self.updated + self.deleted

    def to_dict(self) -> dict:
        return {
            "added": self.added,
            "updated": self.updated,
            "deleted": self.deleted,
            "errors": self.errors,
        }


class HostManager:
    """主机资产管理器"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── 查询 ──────────────────────────────────

    async def list_hosts(self, tag: Optional[str] = None) -> list[Host]:
        """列出所有主机，可按标签过滤"""
        stmt = select(Host).order_by(Host.name)
        if tag:
            stmt = stmt.where(Host.tags.contains(tag))
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_host_by_id(self, host_id: int) -> Optional[Host]:
        """按 ID 获取主机"""
        return await self._session.get(Host, host_id)

    async def get_host_by_name(self, name: str) -> Optional[Host]:
        """按名称获取主机"""
        stmt = select(Host).where(Host.name == name)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    # ── 创建 ──────────────────────────────────

    async def create_host(self, data: HostCreate) -> Host:
        """创建新主机"""
        host = Host(
            name=data.name,
            hostname=data.hostname,
            port=data.port,
            username=data.username,
            auth_type=data.auth_type,
            private_key_path=data.private_key_path,
            description=data.description,
            tags=",".join(data.tags) if data.tags else None,
        )
        # 密码认证时加密存储
        if data.auth_type == AuthType.PASSWORD and data.password:
            host.password_encrypted = encrypt_password(data.password)

        self._session.add(host)
        await self._session.flush()
        await self._session.refresh(host)
        return host

    # ── 更新 ──────────────────────────────────

    async def update_host(self, host_id: int, data: HostUpdate) -> Optional[Host]:
        """更新主机信息"""
        host = await self.get_host_by_id(host_id)
        if not host:
            return None

        update_data = data.model_dump(exclude_unset=True)

        # 特殊处理 tags（list -> CSV）
        if "tags" in update_data:
            tags = update_data.pop("tags")
            host.tags = ",".join(tags) if tags else None

        # 特殊处理 password（加密后存储）
        if "password" in update_data:
            password = update_data.pop("password")
            if password:
                host.password_encrypted = encrypt_password(password)

        for key, value in update_data.items():
            setattr(host, key, value)

        await self._session.flush()
        await self._session.refresh(host)
        return host

    # ── 删除 ──────────────────────────────────

    async def delete_host(self, host_id: int) -> bool:
        """删除主机"""
        host = await self.get_host_by_id(host_id)
        if not host:
            return False
        await self._session.delete(host)
        await self._session.flush()
        return True

    # ── YAML 同步（Single Source of Truth）──────

    async def sync_from_yaml(self, yaml_path: str | Path) -> SyncResult:
        """从 YAML 配置文件同步主机到数据库

        YAML 是唯一真相，DB 是运行时缓存。同步逻辑：
        1. 解析 YAML → Pydantic 逐条校验（任何一条错误则整批拒绝）
        2. 校验全部通过后：
           a. YAML 中有、DB 中无 → INSERT
           b. YAML 中有、DB 中有且字段有变化 → UPDATE
           c. YAML 中无、DB 中有 → DELETE

        Returns:
            SyncResult 同步结果
        """
        result = SyncResult()
        path = Path(yaml_path)

        if not path.exists():
            logger.warning("hosts.yaml 不存在: %s，跳过同步", path)
            return result

        # ── Step 1: 解析 YAML ──
        try:
            with open(path, encoding="utf-8") as f:
                config = yaml.safe_load(f)
        except yaml.YAMLError as e:
            result.errors.append(f"YAML 解析失败: {e}")
            logger.error("hosts.yaml 解析失败: %s", e)
            return result

        hosts_config = config.get("hosts") if config else None
        if not hosts_config:
            hosts_config = []

        # ── Step 2: Pydantic 逐条校验（全部通过才继续）──
        validated: list[HostCreate] = []
        for idx, item in enumerate(hosts_config):
            try:
                data = HostCreate(
                    name=item.get("name", ""),
                    hostname=item.get("hostname", ""),
                    port=item.get("port", 22),
                    username=item.get("username", ""),
                    auth_type=AuthType(item.get("auth_type", "key")),
                    private_key_path=item.get("private_key_path"),
                    password=item.get("password"),
                    description=item.get("description"),
                    tags=item.get("tags"),
                )
                validated.append(data)
            except (ValidationError, ValueError) as e:
                result.errors.append(f"第 {idx + 1} 条主机配置校验失败: {e}")

        if result.errors:
            logger.error("hosts.yaml 校验失败，拒绝同步: %s", result.errors)
            return result

        # ── Step 3: 检查 name 唯一性（YAML 内部去重）──
        yaml_names = [d.name for d in validated]
        duplicates = {n for n in yaml_names if yaml_names.count(n) > 1}
        if duplicates:
            result.errors.append(f"YAML 中存在重复的主机名: {duplicates}")
            logger.error("hosts.yaml 中存在重复主机名: %s", duplicates)
            return result

        # ── Step 4: 对比 DB，执行增/改/删 ──
        yaml_name_set = set(yaml_names)
        yaml_by_name = {d.name: d for d in validated}

        # 获取 DB 中所有主机
        db_hosts = await self.list_hosts()
        db_by_name = {h.name: h for h in db_hosts}
        db_name_set = set(db_by_name.keys())

        # 4a. INSERT — YAML 有，DB 无
        to_add = yaml_name_set - db_name_set
        for name in to_add:
            await self.create_host(yaml_by_name[name])
            result.added += 1
            logger.info("[SYNC] 新增主机: %s", name)

        # 4b. UPDATE — YAML 有，DB 有，字段有变化
        to_check = yaml_name_set & db_name_set
        for name in to_check:
            yaml_data = yaml_by_name[name]
            db_host = db_by_name[name]
            if self._host_needs_update(db_host, yaml_data):
                update_data = self._build_update_data(yaml_data)
                await self.update_host(db_host.id, update_data)
                result.updated += 1
                logger.info("[SYNC] 更新主机: %s", name)

        # 4c. DELETE — YAML 无，DB 有
        to_delete = db_name_set - yaml_name_set
        for name in to_delete:
            db_host = db_by_name[name]
            await self.delete_host(db_host.id)
            result.deleted += 1
            logger.info("[SYNC] 删除主机: %s (id=%d)", name, db_host.id)

        if result.total_changes:
            logger.info(
                "[SYNC] 同步完成: 新增 %d, 更新 %d, 删除 %d",
                result.added, result.updated, result.deleted,
            )
        else:
            logger.info("[SYNC] 配置无变化，跳过")

        return result

    # ── 内部辅助方法 ──────────────────────────────

    @staticmethod
    def _host_needs_update(db_host: Host, yaml_data: HostCreate) -> bool:
        """比较 DB 中的主机与 YAML 配置，判断是否需要更新"""
        if db_host.hostname != yaml_data.hostname:
            return True
        if db_host.port != yaml_data.port:
            return True
        if db_host.username != yaml_data.username:
            return True
        if db_host.auth_type != yaml_data.auth_type:
            return True
        if db_host.private_key_path != yaml_data.private_key_path:
            return True
        if db_host.description != yaml_data.description:
            return True

        # 比较 tags：DB 中是 CSV 字符串，YAML 中是 list
        db_tags = sorted(t.strip() for t in db_host.tags.split(",") if t.strip()) if db_host.tags else []
        yaml_tags = sorted(yaml_data.tags) if yaml_data.tags else []
        if db_tags != yaml_tags:
            return True

        # 密码变更检测：如果 YAML 中有新密码，视为需要更新
        # （实际生产中应比较解密后的值，但这里简化处理——有密码就触发更新）
        if yaml_data.password:
            return True

        return False

    @staticmethod
    def _build_update_data(yaml_data: HostCreate) -> HostUpdate:
        """从 HostCreate 构建 HostUpdate 对象"""
        return HostUpdate(
            hostname=yaml_data.hostname,
            port=yaml_data.port,
            username=yaml_data.username,
            auth_type=yaml_data.auth_type,
            private_key_path=yaml_data.private_key_path,
            password=yaml_data.password,
            description=yaml_data.description,
            tags=yaml_data.tags,
        )
