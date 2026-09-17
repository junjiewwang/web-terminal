"""交互规则业务逻辑服务

职责：
- 规则 CRUD
- 为连接引擎提供启用规则查询（按优先级排序）
- 提供示例规则（页面参考，不落库）

设计原则：
- 规则是通用的"匹配正则 → 发送内容"抽象，无业务语义
- 引擎查询只返回 enabled=True 的规则
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.interaction_rule import (
    InteractionRule,
    InteractionRuleCreate,
    InteractionRuleExample,
    InteractionRuleUpdate,
)

logger = logging.getLogger(__name__)


class InteractionRuleService:
    """交互规则服务"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── 查询 ──────────────────────────────────────

    async def list_rules(self) -> list[InteractionRule]:
        """获取全部规则（含禁用），按优先级降序"""
        result = await self._session.execute(
            select(InteractionRule).order_by(InteractionRule.priority.desc(), InteractionRule.name)
        )
        return list(result.scalars().all())

    async def list_enabled(self, names: list[str] | None = None) -> list[InteractionRule]:
        """获取启用的规则，按优先级降序。

        Args:
            names: 规则名称列表过滤；None 表示全部启用规则。
        """
        query = select(InteractionRule).where(InteractionRule.enabled.is_(True))
        if names:
            query = query.where(InteractionRule.name.in_(names))
        query = query.order_by(InteractionRule.priority.desc(), InteractionRule.name)
        result = await self._session.execute(query)
        return list(result.scalars().all())

    async def get_by_id(self, rule_id: int) -> InteractionRule | None:
        result = await self._session.execute(
            select(InteractionRule).where(InteractionRule.id == rule_id)
        )
        return result.scalar_one_or_none()

    async def get_by_name(self, name: str) -> InteractionRule | None:
        result = await self._session.execute(
            select(InteractionRule).where(InteractionRule.name == name)
        )
        return result.scalar_one_or_none()

    # ── 创建 / 更新 / 删除 ──────────────────────

    async def create(self, data: InteractionRuleCreate) -> InteractionRule:
        rule = InteractionRule(
            name=data.name,
            description=data.description,
            priority=data.priority,
            wait_pattern=data.wait_pattern,
            send_text=data.send_text,
            timeout=data.timeout,
            max_retries=data.max_retries,
            enabled=data.enabled,
        )
        self._session.add(rule)
        await self._session.flush()
        # 刷新以加载 server_default 生成的 created_at/updated_at，
        # 避免后续 model_validate 在同步上下文触发懒加载（MissingGreenlet）
        await self._session.refresh(rule)
        logger.info("创建交互规则: %s", data.name)
        return rule

    async def update(self, rule_id: int, data: InteractionRuleUpdate) -> InteractionRule | None:
        rule = await self.get_by_id(rule_id)
        if rule is None:
            return None

        fields_set = set(data.model_fields_set)
        for field_name in fields_set:
            setattr(rule, field_name, getattr(data, field_name))

        await self._session.flush()
        # 刷新以加载 onupdate 更新的 updated_at，避免懒加载
        await self._session.refresh(rule)
        logger.info("更新交互规则: %s (id=%d)", rule.name, rule.id)
        return rule

    async def delete(self, rule_id: int) -> bool:
        rule = await self.get_by_id(rule_id)
        if rule is None:
            return False
        await self._session.delete(rule)
        await self._session.flush()
        logger.info("删除交互规则: %s (id=%d)", rule.name, rule.id)
        return True

    # ── 示例（页面参考，不落库）──────────────────

    @staticmethod
    def get_examples() -> list[InteractionRuleExample]:
        """返回示例规则，供页面参考展示。"""
        return [
            InteractionRuleExample(
                name="password",
                description="匹配密码输入提示，自动发送连接密码",
                priority=100,
                wait_pattern=r"(?i)(password|passwd|密码|口令)\s*[:：]\s*$",
                send_text="{{password}}",
                timeout=15.0,
                max_retries=1,
            ),
            InteractionRuleExample(
                name="select_user",
                description="匹配用户选择菜单（ID>），发送指定用户编号",
                priority=90,
                wait_pattern=r"(?i)^\s*ID\s*>\s*$",
                send_text="{{user_id}}",
                timeout=10.0,
                max_retries=1,
            ),
            InteractionRuleExample(
                name="host_key_confirm",
                description="匹配 SSH 首次连接主机密钥确认，发送 yes",
                priority=80,
                wait_pattern=r"(?i)Are you sure you want to continue connecting",
                send_text="yes",
                timeout=10.0,
                max_retries=1,
            ),
            InteractionRuleExample(
                name="confirm_yes",
                description="匹配 yes/no 确认，发送 y",
                priority=70,
                wait_pattern=r"(?i)(yes/no|\(y/n\)|continue\s*\?)\s*[:：]?\s*$",
                send_text="y",
                timeout=10.0,
                max_retries=1,
            ),
        ]
