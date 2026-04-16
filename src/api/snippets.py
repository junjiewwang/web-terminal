"""排障脚本片段 REST API 端点。

提供三个接口供前端消费：
- GET /api/snippets           — 领域列表（概要信息）
- GET /api/snippets/{domain}  — 领域详情（含命令列表）
- GET /api/snippets/{domain}/script — 获取 heredoc 加载命令
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from src.models.snippet import SnippetDomainSummary
from src.services.snippet_registry import SnippetRegistry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["snippets"])

# 由 main.py lifespan 注入
snippet_registry: SnippetRegistry | None = None


def _get_registry() -> SnippetRegistry:
    """获取 SnippetRegistry 实例（未初始化时返回 503）"""
    if snippet_registry is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Snippet Registry 未初始化",
        )
    return snippet_registry


# ── 响应模型 ──────────────────────────────────────


class ParamResponse(BaseModel):
    """参数详情"""

    name: str
    description: str
    default: str
    required: bool


class CommandResponse(BaseModel):
    """命令详情"""

    id: str
    name: str
    description: str
    syntax: str
    template: str
    timeout: int | None
    params: list[ParamResponse]


class DomainDetailResponse(BaseModel):
    """领域详情（含命令列表）"""

    id: str
    name: str
    icon: str
    description: str
    tags: list[str]
    script_file: str
    default_timeout: int | None
    commands: list[CommandResponse]


class ScriptLoaderResponse(BaseModel):
    """脚本加载命令响应"""

    domain_id: str
    probe_command: str = Field(description="检测脚本是否已加载的探测命令")
    heredoc_loader: str = Field(description="heredoc 注入命令（将脚本加载到远端）")


# ── 端点 ──────────────────────────────────────────


@router.get("/api/snippets", response_model=list[SnippetDomainSummary])
async def list_snippet_domains() -> list[SnippetDomainSummary]:
    """列出所有 Snippet 领域（概要信息，不含命令详情）"""
    registry = _get_registry()
    return registry.list_domain_summaries()


@router.get("/api/snippets/{domain_id}", response_model=DomainDetailResponse)
async def get_snippet_domain(domain_id: str) -> DomainDetailResponse:
    """获取 Snippet 领域详情（含命令列表和参数）"""
    registry = _get_registry()
    domain = registry.get_domain(domain_id)
    if not domain:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Snippet 领域不存在: {domain_id}",
        )
    return DomainDetailResponse(
        id=domain.id,
        name=domain.name,
        icon=domain.icon,
        description=domain.description,
        tags=domain.tags,
        script_file=domain.script_file,
        default_timeout=domain.default_timeout,
        commands=[
            CommandResponse(
                id=cmd.id,
                name=cmd.name,
                description=cmd.description,
                syntax=cmd.syntax,
                template=cmd.template,
                timeout=cmd.timeout,
                params=[
                    ParamResponse(
                        name=p.name,
                        description=p.description,
                        default=p.default,
                        required=p.required,
                    )
                    for p in cmd.params
                ],
            )
            for cmd in domain.commands
        ],
    )


@router.get("/api/snippets/{domain_id}/script", response_model=ScriptLoaderResponse)
async def get_snippet_script(domain_id: str) -> ScriptLoaderResponse:
    """获取领域脚本的 heredoc 加载命令和探测命令。

    前端通过此接口获取：
    1. probe_command: 检测脚本是否已在远端加载
    2. heredoc_loader: 将脚本通过 heredoc 注入远端 /tmp/ 并 source
    """
    registry = _get_registry()
    domain = registry.get_domain(domain_id)
    if not domain:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Snippet 领域不存在: {domain_id}",
        )

    probe = registry.get_probe_command(domain_id)
    loader = registry.build_heredoc_loader(domain_id)

    if not probe or not loader:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Snippet 领域 {domain_id} 无脚本文件或命令",
        )

    return ScriptLoaderResponse(
        domain_id=domain_id,
        probe_command=probe,
        heredoc_loader=loader,
    )
