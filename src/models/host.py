"""主机资产数据模型

包含 SQLAlchemy ORM 模型和 Pydantic Schema，
实现数据层与接口层的职责分离。

主机类型：
  - direct: 可直连的普通主机
  - bastion: 堡垒机（可包含二级跳板主机）
  - jump_host: 二级跳板主机（通过堡垒机中转连接）
"""

from __future__ import annotations

import enum
import json
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, relationship


# ──────────────────────────────────────────────
# SQLAlchemy ORM 模型
# ──────────────────────────────────────────────


class Base(DeclarativeBase):
    """SQLAlchemy 声明式基类

    __allow_unmapped__: 允许裸类型注解（非 Mapped[] 包裹），
    兼容 `from __future__ import annotations` (PEP 563) 下
    SQLAlchemy 2.0 Annotated Declarative Table 的解析行为。
    """

    __allow_unmapped__ = True


class AuthType(str, enum.Enum):
    """SSH 认证方式"""

    KEY = "key"
    PASSWORD = "password"


class HostType(str, enum.Enum):
    """主机类型"""

    DIRECT = "direct"       # 普通直连主机
    BASTION = "bastion"     # 堡垒机（可包含二级主机）
    JUMP_HOST = "jump_host" # 二级跳板主机（通过堡垒机中转）


class Host(Base):
    """主机资产 ORM 模型

    支持三种主机类型：
    - direct: 普通直连主机（默认）
    - bastion: 堡垒机，可包含多个 jump_host 子主机
    - jump_host: 二级跳板主机，parent_id 指向所属堡垒机

    堡垒机特有字段（存储在 jump_config JSON 中）：
    - ready_pattern: 堡垒机就绪模式（如 "[Host]>"）
    - login_success_pattern: 登录目标主机成功标志

    二级主机特有字段：
    - parent_id: 所属堡垒机 ID
    - target_ip: 目标主机 IP（在堡垒机中输入此 IP 跳转）
    - login_steps: 登录步骤链 JSON（wait/send 步骤序列）
    """

    __tablename__ = "hosts"

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    name: str = Column(String(128), unique=True, nullable=False, index=True, comment="主机别名")
    hostname: str = Column(String(256), nullable=False, comment="IP 或域名")
    port: int = Column(Integer, nullable=False, default=22, comment="SSH 端口")
    username: str = Column(String(128), nullable=False, comment="SSH 用户名")
    auth_type: AuthType = Column(
        Enum(AuthType), nullable=False, default=AuthType.KEY, comment="认证方式: key | password"
    )
    private_key_path: Optional[str] = Column(String(512), nullable=True, comment="私钥路径 (auth_type=key)")
    password_encrypted: Optional[str] = Column(Text, nullable=True, comment="加密后的密码 (auth_type=password)")
    description: Optional[str] = Column(Text, nullable=True, comment="主机描述")
    tags: Optional[str] = Column(Text, nullable=True, comment="标签，逗号分隔存储")

    # ── 跳板主机相关字段 ──
    host_type: HostType = Column(
        Enum(HostType), nullable=False, default=HostType.DIRECT,
        comment="主机类型: direct | bastion | jump_host",
    )
    parent_id: Optional[int] = Column(
        Integer, ForeignKey("hosts.id", ondelete="CASCADE"), nullable=True,
        comment="所属堡垒机 ID（仅 jump_host 类型）",
    )
    target_ip: Optional[str] = Column(
        String(256), nullable=True,
        comment="目标 IP（jump_host 在堡垒机中输入此 IP 跳转）",
    )
    jump_config: Optional[str] = Column(
        Text, nullable=True,
        comment="堡垒机配置 JSON: {ready_pattern, login_success_pattern}",
    )
    login_steps: Optional[str] = Column(
        Text, nullable=True,
        comment="登录步骤链 JSON: [{wait, send, timeout?}, ...]",
    )

    created_at: datetime = Column(DateTime, server_default=func.now(), comment="创建时间")
    updated_at: datetime = Column(DateTime, server_default=func.now(), onupdate=func.now(), comment="更新时间")

    # ── ORM 关系 ──
    # 堡垒机 → 二级主机（一对多）
    children: list[Host] = relationship(
        "Host",
        back_populates="parent",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    # 二级主机 → 堡垒机（多对一）
    parent: Optional[Host] = relationship(
        "Host",
        back_populates="children",
        remote_side=[id],
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<Host(id={self.id}, name='{self.name}', type='{self.host_type.value}')>"

    # ── 便捷方法 ──

    @property
    def is_bastion(self) -> bool:
        return self.host_type == HostType.BASTION

    @property
    def is_jump_host(self) -> bool:
        return self.host_type == HostType.JUMP_HOST

    def get_jump_config(self) -> dict:
        """解析堡垒机配置 JSON，返回字典"""
        if not self.jump_config:
            return {}
        try:
            return json.loads(self.jump_config)
        except (json.JSONDecodeError, TypeError):
            return {}

    def get_login_steps(self) -> list[dict]:
        """解析登录步骤链 JSON，返回步骤列表"""
        if not self.login_steps:
            return []
        try:
            steps = json.loads(self.login_steps)
            return steps if isinstance(steps, list) else []
        except (json.JSONDecodeError, TypeError):
            return []


# ──────────────────────────────────────────────
# Pydantic Schema（API 层数据校验）
# ──────────────────────────────────────────────


class LoginStepSchema(BaseModel):
    """登录交互步骤

    描述堡垒机跳转过程中的单个交互步骤：
    等待终端输出匹配 wait 模式，然后发送 send 内容。

    特殊变量：
    - {{password}}: 替换为 jump_host 的 password 字段
    - {{manual}}: 需要用户在浏览器终端手动输入
    """

    wait: str = Field(..., min_length=1, description="等待匹配的正则模式")
    send: str = Field(..., description="匹配后发送的内容（支持变量替换）")
    timeout: float = Field(default=15.0, ge=1.0, le=120.0, description="本步骤超时秒数")


class JumpHostConfigSchema(BaseModel):
    """堡垒机跳板配置（存储在 bastion 的 jump_config 中）"""

    ready_pattern: str = Field(
        default=r"\$\s*$",
        description="堡垒机就绪模式（登录堡垒机后出现的标志性输出）",
    )
    login_success_pattern: str = Field(
        default=r"Last login|\\]#|\\]\$",
        description="二级主机登录成功标志",
    )


class JumpHostYAMLSchema(BaseModel):
    """YAML 中二级主机的配置格式"""

    name: str = Field(..., min_length=1, max_length=128, description="二级主机别名")
    target_ip: str = Field(..., min_length=1, max_length=256, description="目标 IP")
    description: Optional[str] = Field(default=None, description="主机描述")
    login_steps: Optional[list[LoginStepSchema]] = Field(default=None, description="登录步骤链")
    password: Optional[str] = Field(default=None, description="目标主机密码（用于 {{password}} 变量）")


class HostBase(BaseModel):
    """主机基础字段"""

    hostname: str = Field(..., min_length=1, max_length=256, description="IP 或域名")
    port: int = Field(default=22, ge=1, le=65535, description="SSH 端口")
    username: str = Field(..., min_length=1, max_length=128, description="SSH 用户名")
    auth_type: AuthType = Field(default=AuthType.KEY, description="认证方式")
    private_key_path: Optional[str] = Field(default=None, max_length=512, description="私钥路径")
    description: Optional[str] = Field(default=None, description="主机描述")
    tags: Optional[list[str]] = Field(default=None, description="标签列表")


class HostCreate(HostBase):
    """创建主机请求"""

    name: str = Field(..., min_length=1, max_length=128, description="主机别名（唯一）")
    password: Optional[str] = Field(default=None, description="SSH 密码（仅 auth_type=password 时使用）")

    # ── 跳板主机相关字段 ──
    host_type: HostType = Field(default=HostType.DIRECT, description="主机类型")
    parent_id: Optional[int] = Field(default=None, description="所属堡垒机 ID（仅 jump_host）")
    target_ip: Optional[str] = Field(default=None, max_length=256, description="目标 IP（仅 jump_host）")
    jump_config: Optional[JumpHostConfigSchema] = Field(default=None, description="堡垒机配置（仅 bastion）")
    login_steps: Optional[list[LoginStepSchema]] = Field(default=None, description="登录步骤链（仅 jump_host）")


class HostUpdate(BaseModel):
    """更新主机请求（所有字段可选）"""

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    hostname: Optional[str] = Field(default=None, min_length=1, max_length=256)
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    username: Optional[str] = Field(default=None, min_length=1, max_length=128)
    auth_type: Optional[AuthType] = None
    private_key_path: Optional[str] = Field(default=None, max_length=512)
    password: Optional[str] = Field(default=None, description="新密码，传入时会重新加密")
    description: Optional[str] = None
    tags: Optional[list[str]] = None

    # ── 跳板主机相关字段 ──
    host_type: Optional[HostType] = None
    parent_id: Optional[int] = None
    target_ip: Optional[str] = Field(default=None, max_length=256)
    jump_config: Optional[JumpHostConfigSchema] = None
    login_steps: Optional[list[LoginStepSchema]] = None


class HostResponse(BaseModel):
    """主机响应（返回给前端）

    支持树形结构：bastion 类型的主机会携带 children 列表。
    """

    id: int
    name: str
    hostname: str
    port: int
    username: str
    auth_type: AuthType
    private_key_path: Optional[str] = None
    description: Optional[str] = None
    tags: list[str] = Field(default_factory=list)

    # ── 跳板主机字段 ──
    host_type: HostType = Field(default=HostType.DIRECT)
    parent_id: Optional[int] = None
    target_ip: Optional[str] = None
    jump_config: Optional[JumpHostConfigSchema] = None
    login_steps: Optional[list[LoginStepSchema]] = None
    children: list[HostResponse] = Field(default_factory=list, description="二级主机列表（仅 bastion）")

    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_model(cls, host: Host, include_children: bool = True) -> HostResponse:
        """从 ORM 模型构建响应，处理 tags CSV→list、JSON 字段解析、children 递归

        Args:
            host: ORM Host 对象
            include_children: 是否包含子主机列表（bastion 类型时递归构建）
        """
        tags = [t.strip() for t in host.tags.split(",") if t.strip()] if host.tags else []

        # 解析 jump_config JSON → Pydantic 对象
        jump_config = None
        if host.jump_config:
            try:
                jump_config = JumpHostConfigSchema(**json.loads(host.jump_config))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        # 解析 login_steps JSON → Pydantic 列表
        login_steps = None
        if host.login_steps:
            try:
                raw_steps = json.loads(host.login_steps)
                if isinstance(raw_steps, list):
                    login_steps = [LoginStepSchema(**s) for s in raw_steps]
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        # 递归构建子主机列表
        children: list[HostResponse] = []
        if include_children and host.is_bastion and host.children:
            children = [
                cls.from_orm_model(child, include_children=False)
                for child in host.children
            ]

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
            host_type=host.host_type,
            parent_id=host.parent_id,
            target_ip=host.target_ip,
            jump_config=jump_config,
            login_steps=login_steps,
            children=children,
            created_at=host.created_at,
            updated_at=host.updated_at,
        )
