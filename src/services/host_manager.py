"""主机资产管理服务

职责：
- 封装主机 CRUD
- 将 `config/hosts.yaml` 同步到数据库
- 维护递归连接树结构
- 提供从目标节点回溯到根节点的多跳路径解析能力
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import yaml
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.host import (
    AuthType,
    CredentialSpecSchema,
    EntrySpecSchema,
    EntryType,
    Host,
    HostCreate,
    HostResponse,
    HostStatus,
    HostTreeYAMLSchema,
    HostType,
    HostUpdate,
)
from src.utils.security import encrypt_password
from src.services.credential_service import CredentialService

logger = logging.getLogger(__name__)

JsonDict = dict[str, object]
_UPDATABLE_FIELDS: tuple[str, ...] = (
    "name",
    "hostname",
    "port",
    "username",
    "auth_type",
    "private_key_path",
    "description",
    "ready_pattern",
    "credential_ref",
    "host_type",
    "parent_id",
    "status",
)


@dataclass
class SyncResult:
    """YAML 同步结果。"""

    added: int = 0
    updated: int = 0
    deleted: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total_changes(self) -> int:
        return self.added + self.updated + self.deleted

    def to_dict(self) -> JsonDict:
        return {
            "added": self.added,
            "updated": self.updated,
            "deleted": self.deleted,
            "errors": self.errors,
        }


@dataclass
class _RootConnection:
    hostname: str
    port: int
    username: str
    auth_type: AuthType
    private_key_path: str | None


@dataclass
class _FlattenedNode:
    data: HostCreate
    parent_name: str | None


class HostManager:
    """主机资产管理器。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session: AsyncSession = session

    # ── 查询 ──────────────────────────────────

    async def has_hosts(self) -> bool:
        """检查数据库中是否已有主机数据（用于判断是否需要首次初始化同步）。"""
        result = await self._session.execute(select(func.count(Host.id)))
        return (result.scalar_one() or 0) > 0

    async def list_hosts(self, tag: str | None = None) -> list[Host]:
        """列出主机树（仅返回顶层根节点）。"""
        hosts = await self._list_all_hosts(tag=tag)
        return self._build_host_tree(hosts)

    async def list_host_responses(self, tag: str | None = None) -> list[HostResponse]:
        """直接返回递归响应对象，避免路由层重复组装。"""
        return [HostResponse.from_orm_model(host) for host in await self.list_hosts(tag=tag)]

    async def get_host_by_id(self, host_id: int) -> Host | None:
        stmt = select(Host).where(Host.id == host_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_host_by_name(self, name: str) -> Host | None:
        stmt = select(Host).where(Host.name == name)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_connection_path(self, host: Host) -> list[Host]:
        """从当前节点回溯到根节点，返回 root -> target 的路径。"""
        path: list[Host] = [host]
        current = host
        while current.parent_id:
            parent = await self.get_host_by_id(current.parent_id)
            if not parent:
                raise ValueError(f"节点 '{current.name}' 的父节点不存在 (id={current.parent_id})")
            path.append(parent)
            current = parent
        path.reverse()
        return path

    @staticmethod
    def build_instance_name(path: list[Host]) -> str:
        """根据连接路径生成唯一终端实例名。"""
        return "--".join(node.name for node in path)

    # ── 创建 ──────────────────────────────────

    async def create_host(self, data: HostCreate) -> Host:
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
            ready_pattern=data.ready_pattern,
            credential_ref=data.credential_ref,
            status=data.status,
        )

        if data.auth_type == AuthType.PASSWORD and data.password:
            host.password_encrypted = encrypt_password(data.password)
        if data.entry_password:
            host.entry_password_encrypted = encrypt_password(data.entry_password)
        if data.entry:
            host.entry_spec = data.entry.model_dump_json()

        self._session.add(host)
        await self._session.flush()
        await self._session.refresh(host)
        return host

    # ── 更新 ──────────────────────────────────

    async def update_host(self, host_id: int, data: HostUpdate) -> Host | None:
        host = await self.get_host_by_id(host_id)
        if not host:
            return None

        fields_set = set(data.model_fields_set)

        if "tags" in fields_set:
            host.tags = ",".join(data.tags) if data.tags else None

        if "password" in fields_set:
            host.password_encrypted = encrypt_password(data.password) if data.password else None

        if "entry_password" in fields_set:
            host.entry_password_encrypted = encrypt_password(data.entry_password) if data.entry_password else None

        if "entry" in fields_set:
            host.entry_spec = data.entry.model_dump_json() if data.entry else None

        for field_name in _UPDATABLE_FIELDS:
            if field_name in fields_set:
                setattr(host, field_name, getattr(data, field_name))

        await self._session.flush()
        await self._session.refresh(host)
        return host

    # ── 删除 ──────────────────────────────────

    async def delete_host(self, host_id: int) -> bool:
        host = await self.get_host_by_id(host_id)
        if not host:
            return False
        await self._session.delete(host)
        await self._session.flush()
        return True

    async def delete_all_hosts(self) -> int:
        """删除所有主机记录（用于覆盖导入模式），返回删除数量。"""
        from sqlalchemy import delete as sa_delete

        result = await self._session.execute(sa_delete(Host))
        await self._session.flush()
        return result.rowcount  # type: ignore[return-value]

    # ── YAML 同步（Single Source of Truth）──────

    async def sync_from_yaml(self, yaml_path: str | Path) -> SyncResult:
        """从新的递归节点 YAML 结构同步到 DB。"""
        result = SyncResult()
        path = Path(yaml_path)
        if not path.exists():
            logger.warning("hosts.yaml 不存在: %s，跳过同步", path)
            return result

        try:
            with open(path, encoding="utf-8") as f:
                loaded: object = yaml.safe_load(f)
        except yaml.YAMLError as e:
            result.errors.append(f"YAML 解析失败: {e}")
            return result

        config: JsonDict = loaded if isinstance(loaded, dict) else {}
        try:
            credentials = self._parse_yaml_credentials(config.get("credentials"))
        except ValueError as e:
            result.errors.append(str(e))
            return result

        # 将 YAML 中的 credentials 同步到数据库（upsert）
        if credentials:
            cred_service = CredentialService(self._session)
            for cred_name, cred_password in credentials.items():
                await cred_service.upsert_from_yaml(cred_name, cred_password)

        raw_hosts: object = config.get("hosts")
        hosts_config: list[object] = raw_hosts if isinstance(raw_hosts, list) else []
        flattened: list[_FlattenedNode] = []

        for idx, item in enumerate(hosts_config):
            try:
                node = HostTreeYAMLSchema.model_validate(item)
                flattened.extend(self._flatten_yaml_node(node, credentials=credentials))
            except (ValidationError, ValueError) as e:
                result.errors.append(f"第 {idx + 1} 条主机配置校验失败: {e}")

        if result.errors:
            logger.error("hosts.yaml 校验失败，拒绝同步: %s", result.errors)
            return result

        all_names = [item.data.name for item in flattened]
        duplicates = {name for name in all_names if all_names.count(name) > 1}
        if duplicates:
            result.errors.append(f"YAML 中存在重复的主机名: {sorted(duplicates)}")
            return result

        existing_hosts = await self._list_all_hosts()
        existing_by_name = {host.name: host for host in existing_hosts}
        created_or_updated: dict[str, Host] = {}

        for item in flattened:
            parent_id: int | None = None
            if item.parent_name:
                parent = created_or_updated.get(item.parent_name) or existing_by_name.get(item.parent_name)
                if not parent:
                    result.errors.append(f"父节点不存在: {item.parent_name} -> {item.data.name}")
                    continue
                parent_id = parent.id

            data = item.data.model_copy(update={"parent_id": parent_id})
            existing = existing_by_name.get(data.name)
            if existing is None:
                host = await self.create_host(data)
                result.added += 1
                logger.info("[SYNC] 新增节点: %s", data.name)
            else:
                if self._host_needs_update(existing, data, parent_id):
                    _ = await self.update_host(existing.id, self._build_update_data(data, parent_id))
                    host = await self.get_host_by_id(existing.id)
                    result.updated += 1
                    logger.info("[SYNC] 更新节点: %s", data.name)
                else:
                    host = existing

            if host is None:
                result.errors.append(f"同步后无法读取节点: {data.name}")
                continue
            created_or_updated[data.name] = host

        yaml_names = {item.data.name for item in flattened}
        for name, host in existing_by_name.items():
            if name not in yaml_names:
                _ = await self.delete_host(host.id)
                result.deleted += 1
                logger.info("[SYNC] 删除节点: %s", name)

        if result.errors:
            logger.error("hosts.yaml 同步失败: %s", result.errors)
            return result

        if result.total_changes:
            logger.info("[SYNC] 同步完成: +%d ~%d -%d", result.added, result.updated, result.deleted)
        else:
            logger.info("[SYNC] 配置无变化")
        return result

    # ── 内部辅助方法 ──────────────────────────────

    async def _list_all_hosts(self, tag: str | None = None) -> list[Host]:
        stmt = select(Host).order_by(Host.name)
        if tag:
            stmt = stmt.where(Host.tags.contains(tag))
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    def _coerce_json_dict(value: object) -> JsonDict:
        if not isinstance(value, dict):
            return {}
        return {str(key): item for key, item in value.items()}

    @staticmethod
    def _parse_yaml_credentials(raw_credentials: object) -> dict[str, str]:
        if raw_credentials is None:
            return {}
        if not isinstance(raw_credentials, dict):
            raise ValueError("顶层 credentials 必须是对象映射")

        credentials: dict[str, str] = {}
        for raw_name, raw_value in raw_credentials.items():
            name = str(raw_name).strip()
            if not name:
                raise ValueError("credentials 中存在空名称引用")

            if isinstance(raw_value, str):
                password = raw_value.strip()
            elif isinstance(raw_value, dict):
                try:
                    spec = CredentialSpecSchema.model_validate(raw_value)
                except ValidationError as e:
                    raise ValueError(f"共享凭据 '{name}' 配置非法: {e}") from e
                password = spec.password.strip()
            else:
                raise ValueError(f"共享凭据 '{name}' 必须是字符串或 {{password: ...}} 对象")

            if not password:
                raise ValueError(f"共享凭据 '{name}' 的密码不能为空")
            credentials[name] = password
        return credentials

    @staticmethod
    def _runtime_children(host: Host) -> list[Host]:
        existing = host.__dict__.get("children")
        if isinstance(existing, list):
            return cast(list[Host], existing)

        runtime_children: list[Host] = []
        host.__dict__["children"] = runtime_children
        return runtime_children

    @classmethod
    def _build_host_tree(cls, hosts: list[Host]) -> list[Host]:
        by_id = {host.id: host for host in hosts}
        for host in hosts:
            cls._runtime_children(host).clear()

        roots: list[Host] = []
        for host in hosts:
            if host.parent_id and host.parent_id in by_id:
                cls._runtime_children(by_id[host.parent_id]).append(host)
            else:
                roots.append(host)

        def _sort_recursively(node: Host) -> None:
            runtime_children = cls._runtime_children(node)
            runtime_children.sort(key=lambda child: child.name.lower())
            for child in runtime_children:
                _sort_recursively(child)

        roots.sort(key=lambda node: node.name.lower())
        for root in roots:
            _sort_recursively(root)
        return roots

    @classmethod
    def _flatten_yaml_node(
        cls,
        node: HostTreeYAMLSchema,
        parent_name: str | None = None,
        root_conn: _RootConnection | None = None,
        credentials: dict[str, str] | None = None,
    ) -> list[_FlattenedNode]:
        credentials = credentials or {}
        if parent_name is None:
            if not node.hostname or not node.username:
                raise ValueError(f"根节点 '{node.name}' 必须配置 hostname 和 username")
            if node.entry and node.entry.type != EntryType.NONE:
                raise ValueError(f"根节点 '{node.name}' 不能配置入口动作 entry")
            root_conn = _RootConnection(
                hostname=node.hostname,
                port=node.port,
                username=node.username,
                auth_type=node.auth_type,
                private_key_path=node.private_key_path,
            )
            password, entry_password = cls._resolve_node_credentials(node, credentials, is_root=True)
            current = HostCreate(
                name=node.name,
                hostname=node.hostname,
                port=node.port,
                username=node.username,
                auth_type=node.auth_type,
                private_key_path=node.private_key_path,
                password=password,
                entry_password=entry_password,
                credential_ref=node.credential_ref,
                description=node.description,
                tags=node.tags,
                ready_pattern=node.ready_pattern,
                host_type=HostType.ROOT,
                entry=EntrySpecSchema(type=EntryType.NONE),
                status=node.status,
            )
        else:
            if root_conn is None:
                raise ValueError(f"节点 '{node.name}' 缺少根连接上下文")
            if not node.entry or node.entry.type == EntryType.NONE or not node.entry.value:
                raise ValueError(f"嵌套节点 '{node.name}' 必须配置有效的 entry")
            _, entry_password = cls._resolve_node_credentials(node, credentials, is_root=False)
            current = HostCreate(
                name=node.name,
                hostname=root_conn.hostname,
                port=root_conn.port,
                username=root_conn.username,
                auth_type=root_conn.auth_type,
                private_key_path=root_conn.private_key_path,
                password=None,
                entry_password=entry_password,
                credential_ref=node.credential_ref,
                description=node.description,
                tags=node.tags,
                ready_pattern=node.ready_pattern,
                host_type=HostType.NESTED,
                entry=node.entry,
                status=node.status,
            )

        flattened = [_FlattenedNode(data=current, parent_name=parent_name)]
        for child in node.children:
            flattened.extend(
                cls._flatten_yaml_node(child, parent_name=node.name, root_conn=root_conn, credentials=credentials)
            )
        return flattened

    @staticmethod
    def _resolve_node_credentials(
        node: HostTreeYAMLSchema,
        credentials: dict[str, str],
        *,
        is_root: bool,
    ) -> tuple[str | None, str | None]:
        shared_password: str | None = None
        if node.credential_ref:
            shared_password = credentials.get(node.credential_ref)
            if shared_password is None:
                raise ValueError(f"节点 '{node.name}' 引用了不存在的 credential_ref: {node.credential_ref}")

        password = node.password
        entry_password = node.entry_password

        if is_root and node.auth_type == AuthType.PASSWORD and password is None:
            password = shared_password
        if not is_root and entry_password is None:
            entry_password = shared_password

        return password, entry_password

    @staticmethod
    def _entry_dict(data: HostCreate) -> JsonDict:
        if data.entry is None:
            return {}
        entry_dump: dict[str, object] = data.entry.model_dump(exclude_none=True)
        return entry_dump

    @classmethod
    def _host_needs_update(cls, db_host: Host, yaml_data: HostCreate, parent_id: int | None) -> bool:
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
        if db_host.parent_id != parent_id:
            return True
        if db_host.ready_pattern != yaml_data.ready_pattern:
            return True
        if db_host.credential_ref != yaml_data.credential_ref:
            return True
        if db_host.status != yaml_data.status:
            return True

        db_tags = sorted(t.strip() for t in db_host.tags.split(",") if t.strip()) if db_host.tags else []
        yaml_tags = sorted(yaml_data.tags) if yaml_data.tags else []
        if db_tags != yaml_tags:
            return True

        db_entry = db_host.get_entry_spec()
        yaml_entry = cls._entry_dict(yaml_data)
        if db_entry != yaml_entry:
            return True

        if yaml_data.password is not None:
            return True
        if yaml_data.entry_password is not None:
            return True
        return False

    @staticmethod
    def _build_update_data(yaml_data: HostCreate, parent_id: int | None) -> HostUpdate:
        return HostUpdate(
            hostname=yaml_data.hostname,
            port=yaml_data.port,
            username=yaml_data.username,
            auth_type=yaml_data.auth_type,
            private_key_path=yaml_data.private_key_path,
            password=yaml_data.password,
            entry_password=yaml_data.entry_password,
            credential_ref=yaml_data.credential_ref,
            description=yaml_data.description,
            tags=yaml_data.tags,
            ready_pattern=yaml_data.ready_pattern,
            host_type=yaml_data.host_type,
            parent_id=parent_id,
            entry=yaml_data.entry,
            status=yaml_data.status,
        )
