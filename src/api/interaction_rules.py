"""交互规则 REST API

提供交互规则的增删改查和示例展示：

- GET    /api/interaction-rules            — 规则列表
- GET    /api/interaction-rules/examples   — 示例规则（页面参考）
- POST   /api/interaction-rules            — 创建规则
- PUT    /api/interaction-rules/{id}       — 更新规则
- DELETE /api/interaction-rules/{id}       — 删除规则
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.database import get_db
from src.models.interaction_rule import (
    InteractionRuleCreate,
    InteractionRuleExample,
    InteractionRuleResponse,
    InteractionRuleUpdate,
)
from src.services.interaction_rule_service import InteractionRuleService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/interaction-rules", tags=["interaction-rules"])


# ── 依赖注入 ──────────────────────────────────────


def _get_service(session: AsyncSession = Depends(get_db)) -> InteractionRuleService:
    return InteractionRuleService(session)


# ── API 端点 ──────────────────────────────────────


@router.get("", response_model=list[InteractionRuleResponse])
async def list_rules(
    service: InteractionRuleService = Depends(_get_service),
) -> list[InteractionRuleResponse]:
    """获取全部规则列表（含禁用）"""
    rules = await service.list_rules()
    return [InteractionRuleResponse.model_validate(rule) for rule in rules]


@router.get("/examples", response_model=list[InteractionRuleExample])
async def get_examples() -> list[InteractionRuleExample]:
    """获取示例规则（仅参考，不落库）"""
    return InteractionRuleService.get_examples()


@router.post("", response_model=InteractionRuleResponse, status_code=status.HTTP_201_CREATED)
async def create_rule(
    data: InteractionRuleCreate,
    service: InteractionRuleService = Depends(_get_service),
    session: AsyncSession = Depends(get_db),
) -> InteractionRuleResponse:
    """创建交互规则"""
    existing = await service.get_by_name(data.name)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"规则名称 '{data.name}' 已存在",
        )

    rule = await service.create(data)
    await session.commit()
    return InteractionRuleResponse.model_validate(rule)


@router.put("/{rule_id}", response_model=InteractionRuleResponse)
async def update_rule(
    rule_id: int,
    data: InteractionRuleUpdate,
    service: InteractionRuleService = Depends(_get_service),
    session: AsyncSession = Depends(get_db),
) -> InteractionRuleResponse:
    """更新交互规则（部分更新）"""
    # 名称唯一性检查
    if data.name is not None:
        existing = await service.get_by_name(data.name)
        if existing and existing.id != rule_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"规则名称 '{data.name}' 已存在",
            )

    rule = await service.update(rule_id, data)
    if rule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="规则不存在")
    await session.commit()
    return InteractionRuleResponse.model_validate(rule)


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(
    rule_id: int,
    service: InteractionRuleService = Depends(_get_service),
    session: AsyncSession = Depends(get_db),
) -> None:
    """删除交互规则"""
    success = await service.delete(rule_id)
    if not success:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="规则不存在")
    await session.commit()
