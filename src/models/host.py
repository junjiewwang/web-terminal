"""主机资产数据模型。"""

from __future__ import annotations

import enum
import json
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """SQLAlchemy 声明式基类。"""


class AuthType(str, enum.Enum):
    KEY = "key"
    PASSWORD = "password"


class HostType(str, enum.Enum):
    ROOT = "root"
    NESTED = "nested"


class EntryType(str, enum.Enum):
    NONE = "none"
    MENU_SEND = "menu_send"
    SSH_COMMAND = "ssh_command"


class Host(Base):
    """主机资产 ORM 模型。"""

    __tablename__ = "hosts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True, comment="主机别名")
    hostname: Mapped[str] = mapped_column(String(256), nullable=False, comment="根 SSH 入口地址")
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=22, comment="根 SSH 入口端口")
    username: Mapped[str] = mapped_column(String(128), nullable=False, comment="根 SSH 入口用户名")
    auth_type: Mapped[AuthType] = mapped_column(
        Enum(AuthType), nullable=False, default=AuthType.KEY, comment="根 SSH 入口认证方式"
    )
    private_key_path: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="根 SSH 私钥路径")
    password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True, comment="根 SSH 加密密码")
    entry_password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True, comment="当前节点入口动作使用的加密密码")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="主机描述")
    tags: Mapped[str | None] = mapped_column(Text, nullable=True, comment="标签，逗号分隔")

    host_type: Mapped[HostType] = mapped_column(
        Enum(HostType), nullable=False, default=HostType.ROOT, comment="节点类型: root | nested"
    )
    parent_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("hosts.id", ondelete="CASCADE"), nullable=True, comment="父节点 ID"
    )
    ready_pattern: Mapped[str | None] = mapped_column(Text, nullable=True, comment="当前节点进入成功后的可操作提示模式")
    entry_spec: Mapped[str | None] = mapped_column(Text, nullable=True, comment="入口动作 JSON: {type, value, success_pattern, steps}")

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), comment="更新时间")

    children: Mapped[list["Host"]] = relationship(
        "Host",
        back_populates="parent",
        cascade="all, delete-orphan",
        lazy="noload",
    )
    parent: Mapped["Host | None"] = relationship(
        "Host",
        back_populates="children",
        remote_side=lambda: [Host.id],
        lazy="noload",
    )

    @property
    def is_root(self) -> bool:
        return self.host_type == HostType.ROOT

    @property
    def is_nested(self) -> bool:
        return self.host_type == HostType.NESTED

    def get_entry_spec(self) -> dict[str, object]:
        if not self.entry_spec:
            return {}
        try:
            parsed = json.loads(self.entry_spec)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}


class LoginStepSchema(BaseModel):
    wait: str = Field(..., min_length=1, description="等待匹配的正则模式")
    send: str = Field(..., description="匹配后发送的内容（支持变量替换）")
    timeout: float = Field(default=15.0, ge=1.0, le=120.0, description="本步骤超时秒数")


class EntrySpecSchema(BaseModel):
    type: EntryType = Field(default=EntryType.NONE, description="入口动作类型")
    value: str | None = Field(default=None, description="入口动作内容（如 IP、ssh 命令）")
    success_pattern: str | None = Field(default=None, description="进入当前节点成功的匹配模式")
    steps: list[LoginStepSchema] = Field(default_factory=list, description="入口动作后的附加交互步骤")


class HostBase(BaseModel):
    hostname: str = Field(..., min_length=1, max_length=256, description="根 SSH 入口地址")
    port: int = Field(default=22, ge=1, le=65535, description="根 SSH 入口端口")
    username: str = Field(..., min_length=1, max_length=128, description="根 SSH 入口用户名")
    auth_type: AuthType = Field(default=AuthType.KEY, description="根 SSH 入口认证方式")
    private_key_path: str | None = Field(default=None, max_length=512, description="SSH 私钥路径")
    description: str | None = Field(default=None, description="主机描述")
    tags: list[str] | None = Field(default=None, description="标签列表")
    ready_pattern: str | None = Field(default=None, description="当前节点进入成功后的可操作提示模式")


class HostCreate(HostBase):
    name: str = Field(..., min_length=1, max_length=128, description="主机别名（唯一）")
    password: str | None = Field(default=None, description="根 SSH 密码（仅 auth_type=password 时使用）")
    entry_password: str | None = Field(default=None, description="入口动作使用的密码（供 {{password}} 变量替换）")
    host_type: HostType = Field(default=HostType.ROOT, description="节点类型")
    parent_id: int | None = Field(default=None, description="父节点 ID")
    entry: EntrySpecSchema | None = Field(default=None, description="从父节点进入当前节点的动作定义")


class HostUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    hostname: str | None = Field(default=None, min_length=1, max_length=256)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, min_length=1, max_length=128)
    auth_type: AuthType | None = None
    private_key_path: str | None = Field(default=None, max_length=512)
    password: str | None = Field(default=None, description="新 SSH 密码，传入时会重新加密")
    entry_password: str | None = Field(default=None, description="新的入口密码，传入时会重新加密")
    description: str | None = None
    tags: list[str] | None = None
    ready_pattern: str | None = None
    host_type: HostType | None = None
    parent_id: int | None = None
    entry: EntrySpecSchema | None = None


class HostResponse(BaseModel):
    id: int
    name: str
    hostname: str
    port: int
    username: str
    auth_type: AuthType
    private_key_path: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    ready_pattern: str | None = None
    host_type: HostType = Field(default=HostType.ROOT)
    parent_id: int | None = None
    entry: EntrySpecSchema = Field(default_factory=EntrySpecSchema)
    children: list["HostResponse"] = Field(default_factory=list, description="子节点列表")
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_orm_model(cls, host: Host) -> "HostResponse":
        tags = [t.strip() for t in host.tags.split(",") if t.strip()] if host.tags else []

        entry = EntrySpecSchema()
        raw_entry = host.get_entry_spec()
        if raw_entry:
            try:
                entry = EntrySpecSchema.model_validate(raw_entry)
            except Exception:
                pass

        raw_children = host.__dict__.get("children", [])
        children = [cls.from_orm_model(child) for child in raw_children if isinstance(child, Host)]
        children.sort(key=lambda item: item.name.lower())

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
            ready_pattern=host.ready_pattern,
            host_type=host.host_type,
            parent_id=host.parent_id,
            entry=entry,
            children=children,
            created_at=host.created_at,
            updated_at=host.updated_at,
        )


class HostTreeYAMLSchema(BaseModel):
    name: str = Field(..., min_length=1, max_length=128, description="节点名称")
    hostname: str | None = Field(default=None, min_length=1, max_length=256, description="根 SSH 地址")
    port: int = Field(default=22, ge=1, le=65535, description="根 SSH 端口")
    username: str | None = Field(default=None, min_length=1, max_length=128, description="根 SSH 用户名")
    auth_type: AuthType = Field(default=AuthType.KEY, description="根 SSH 认证方式")
    private_key_path: str | None = Field(default=None, max_length=512, description="根 SSH 私钥路径")
    password: str | None = Field(default=None, description="根 SSH 密码（仅根节点使用）")
    entry_password: str | None = Field(default=None, description="入口动作使用的密码")
    description: str | None = Field(default=None, description="节点描述")
    tags: list[str] = Field(default_factory=list, description="标签")
    ready_pattern: str | None = Field(default=None, description="当前节点就绪模式")
    entry: EntrySpecSchema | None = Field(default=None, description="入口动作")
    children: list["HostTreeYAMLSchema"] = Field(default_factory=list, description="子节点")


_ = HostResponse.model_rebuild()
_ = HostTreeYAMLSchema.model_rebuild()
