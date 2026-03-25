"""主机资产管理服务

封装主机 CRUD 操作和 YAML 配置同步逻辑，
所有数据库操作集中在此，上层（API / MCP）只调用服务方法。

设计原则：
- hosts.yaml 是 Single Source of Truth，DB 是运行时缓存
- sync_from_yaml() 实现完整的增/改/删同步（非仅导入）
- Pydantic 校验先行：任何一条格式错误则整批拒绝
- 事务原子性：所有变更在一个事务中完成

跳板主机支持：
- bastion 类型主机可包含嵌套的 jump_hosts 二级主机配置
- sync_from_yaml 自动解析 jump_hosts，建立父子关系
- jump_host 的 parent_id 指向所属堡垒机，实现树形结构
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models.host import (
    AuthType,
    Host,
    HostCreate,
    HostType,
    HostUpdate,
    JumpHostConfigSchema,
    JumpHostYAMLSchema,
    LoginStepSchema,
)
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
        """列出所有主机，可按标签过滤

        显式 eager load children 关系，确保异步模式下
        不会触发 lazy load 导致 MissingGreenlet 错误。
        """
        stmt = select(Host).options(selectinload(Host.children)).order_by(Host.name)
        if tag:
            stmt = stmt.where(Host.tags.contains(tag))
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_host_by_id(self, host_id: int) -> Optional[Host]:
        """按 ID 获取主机"""
        stmt = select(Host).options(selectinload(Host.children)).where(Host.id == host_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_host_by_name(self, name: str) -> Optional[Host]:
        """按名称获取主机"""
        stmt = select(Host).options(selectinload(Host.children)).where(Host.name == name)
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
            host_type=data.host_type,
            parent_id=data.parent_id,
            target_ip=data.target_ip,
        )
        # 密码认证时加密存储
        if data.auth_type == AuthType.PASSWORD and data.password:
            host.password_encrypted = encrypt_password(data.password)

        # 堡垒机配置 → JSON
        if data.jump_config:
            host.jump_config = data.jump_config.model_dump_json()

        # 登录步骤链 → JSON
        if data.login_steps:
            host.login_steps = json.dumps(
                [s.model_dump() for s in data.login_steps],
                ensure_ascii=False,
            )

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

        # 特殊处理 jump_config（Pydantic → JSON string）
        if "jump_config" in update_data:
            jc = update_data.pop("jump_config")
            if jc is not None:
                host.jump_config = JumpHostConfigSchema(**jc).model_dump_json() if isinstance(jc, dict) else jc.model_dump_json()
            else:
                host.jump_config = None

        # 特殊处理 login_steps（Pydantic list → JSON string）
        if "login_steps" in update_data:
            ls = update_data.pop("login_steps")
            if ls is not None:
                steps = [
                    (LoginStepSchema(**s).model_dump() if isinstance(s, dict) else s.model_dump())
                    for s in ls
                ]
                host.login_steps = json.dumps(steps, ensure_ascii=False)
            else:
                host.login_steps = None

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
        """从 YAML 同步主机到 DB，支持 bastion + jump_hosts 嵌套"""
        result = SyncResult()
        path = Path(yaml_path)
        if not path.exists():
            logger.warning("hosts.yaml 不存在: %s，跳过同步", path)
            return result

        try:
            with open(path, encoding="utf-8") as f:
                config = yaml.safe_load(f)
        except yaml.YAMLError as e:
            result.errors.append(f"YAML 解析失败: {e}")
            return result

        hosts_config = config.get("hosts") if config else []
        if not hosts_config:
            hosts_config = []

        # ── 校验顶层主机 + 收集 jump_hosts ──
        validated_top: list[HostCreate] = []
        jump_hosts_map: dict[str, list[JumpHostYAMLSchema]] = {}

        for idx, item in enumerate(hosts_config):
            try:
                host_type = HostType(item.get("type", "direct"))
                jump_config = None
                if host_type == HostType.BASTION:
                    jc_data = {}
                    if item.get("ready_pattern"):
                        jc_data["ready_pattern"] = item["ready_pattern"]
                    if item.get("login_success_pattern"):
                        jc_data["login_success_pattern"] = item["login_success_pattern"]
                    if jc_data:
                        jump_config = JumpHostConfigSchema(**jc_data)

                data = HostCreate(
                    name=item.get("name", ""), hostname=item.get("hostname", ""),
                    port=item.get("port", 22), username=item.get("username", ""),
                    auth_type=AuthType(item.get("auth_type", "key")),
                    private_key_path=item.get("private_key_path"),
                    password=item.get("password"), description=item.get("description"),
                    tags=item.get("tags"), host_type=host_type, jump_config=jump_config,
                )
                validated_top.append(data)

                # 解析嵌套 jump_hosts
                if host_type == HostType.BASTION and item.get("jump_hosts"):
                    jh_list: list[JumpHostYAMLSchema] = []
                    for jidx, jitem in enumerate(item["jump_hosts"]):
                        try:
                            jh_list.append(JumpHostYAMLSchema(**jitem))
                        except (ValidationError, ValueError) as je:
                            result.errors.append(
                                f"主机 '{item.get('name', '?')}' 的第 {jidx + 1} 个 jump_host 校验失败: {je}"
                            )
                    if jh_list:
                        jump_hosts_map[data.name] = jh_list
            except (ValidationError, ValueError) as e:
                result.errors.append(f"第 {idx + 1} 条主机配置校验失败: {e}")

        if result.errors:
            logger.error("hosts.yaml 校验失败，拒绝同步: %s", result.errors)
            return result

        # ── 全局 name 唯一性检查 ──
        all_names = [d.name for d in validated_top]
        for jh_list in jump_hosts_map.values():
            all_names.extend(jh.name for jh in jh_list)
        duplicates = {n for n in all_names if all_names.count(n) > 1}
        if duplicates:
            result.errors.append(f"YAML 中存在重复的主机名: {duplicates}")
            return result

        # ── 同步顶层主机（direct / bastion）──
        yaml_top_names = {d.name for d in validated_top}
        yaml_top_by_name = {d.name: d for d in validated_top}
        db_hosts = await self.list_hosts()
        db_top = {h.name: h for h in db_hosts if h.host_type != HostType.JUMP_HOST}

        for name in yaml_top_names - set(db_top):
            await self.create_host(yaml_top_by_name[name])
            result.added += 1
            logger.info("[SYNC] 新增主机: %s", name)

        for name in yaml_top_names & set(db_top):
            if self._host_needs_update(db_top[name], yaml_top_by_name[name]):
                await self.update_host(db_top[name].id, self._build_update_data(yaml_top_by_name[name]))
                result.updated += 1
                logger.info("[SYNC] 更新主机: %s", name)

        for name in set(db_top) - yaml_top_names:
            await self.delete_host(db_top[name].id)
            result.deleted += 1
            logger.info("[SYNC] 删除主机: %s (id=%d)", name, db_top[name].id)

        # ── 同步 jump_hosts（二级主机）──
        for bastion_name, jh_list in jump_hosts_map.items():
            bastion = await self.get_host_by_name(bastion_name)
            if not bastion:
                continue
            db_jh = {h.name: h for h in await self._list_jump_hosts(bastion.id)}
            yaml_jh = {jh.name: jh for jh in jh_list}

            for jname in set(yaml_jh) - set(db_jh):
                await self.create_host(self._build_jump_host_create(yaml_jh[jname], bastion))
                result.added += 1
                logger.info("[SYNC] 新增二级主机: %s → %s", jname, bastion_name)

            for jname in set(yaml_jh) & set(db_jh):
                if self._jump_host_needs_update(db_jh[jname], yaml_jh[jname]):
                    await self.update_host(db_jh[jname].id, self._build_jump_host_update(yaml_jh[jname]))
                    result.updated += 1
                    logger.info("[SYNC] 更新二级主机: %s", jname)

            for jname in set(db_jh) - set(yaml_jh):
                await self.delete_host(db_jh[jname].id)
                result.deleted += 1
                logger.info("[SYNC] 删除二级主机: %s", jname)

        if result.total_changes:
            logger.info("[SYNC] 同步完成: +%d ~%d -%d", result.added, result.updated, result.deleted)
        else:
            logger.info("[SYNC] 配置无变化")
        return result

    # ── 查询辅助 ──────────────────────────────────

    async def _list_jump_hosts(self, bastion_id: int) -> list[Host]:
        """列出指定堡垒机下的所有 jump_host"""
        stmt = (
            select(Host)
            .where(Host.parent_id == bastion_id, Host.host_type == HostType.JUMP_HOST)
            .order_by(Host.name)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    # ── 内部辅助方法 ──────────────────────────────

    @staticmethod
    def _host_needs_update(db_host: Host, yaml_data: HostCreate) -> bool:
        """比较顶层主机是否需要更新"""
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
        if db_host.host_type != yaml_data.host_type:
            return True
        db_tags = sorted(t.strip() for t in db_host.tags.split(",") if t.strip()) if db_host.tags else []
        yaml_tags = sorted(yaml_data.tags) if yaml_data.tags else []
        if db_tags != yaml_tags:
            return True
        if yaml_data.password:
            return True
        return False

    @staticmethod
    def _build_update_data(yaml_data: HostCreate) -> HostUpdate:
        """从 HostCreate 构建 HostUpdate"""
        return HostUpdate(
            hostname=yaml_data.hostname, port=yaml_data.port,
            username=yaml_data.username, auth_type=yaml_data.auth_type,
            private_key_path=yaml_data.private_key_path,
            password=yaml_data.password, description=yaml_data.description,
            tags=yaml_data.tags, host_type=yaml_data.host_type,
            jump_config=yaml_data.jump_config,
        )

    @staticmethod
    def _jump_host_needs_update(db_host: Host, jh: JumpHostYAMLSchema) -> bool:
        """比较二级主机是否需要更新"""
        if db_host.target_ip != jh.target_ip:
            return True
        if db_host.description != jh.description:
            return True
        # login_steps 比较（JSON 序列化后比较）
        db_steps = db_host.get_login_steps()
        yaml_steps = [s.model_dump() for s in jh.login_steps] if jh.login_steps else []
        if db_steps != yaml_steps:
            return True
        if jh.password:
            return True
        return False

    @staticmethod
    def _build_jump_host_create(jh: JumpHostYAMLSchema, bastion: Host) -> HostCreate:
        """从 JumpHostYAMLSchema 构建 HostCreate（二级主机）"""
        return HostCreate(
            name=jh.name,
            # jump_host 继承堡垒机的连接信息（自身不直连）
            hostname=bastion.hostname, port=bastion.port,
            username=bastion.username, auth_type=bastion.auth_type,
            description=jh.description,
            host_type=HostType.JUMP_HOST,
            parent_id=bastion.id,
            target_ip=jh.target_ip,
            login_steps=jh.login_steps,
            password=jh.password,
        )

    @staticmethod
    def _build_jump_host_update(jh: JumpHostYAMLSchema) -> HostUpdate:
        """从 JumpHostYAMLSchema 构建 HostUpdate（二级主机）"""
        return HostUpdate(
            target_ip=jh.target_ip,
            description=jh.description,
            login_steps=jh.login_steps,
            password=jh.password,
        )
