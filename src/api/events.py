"""SSE 事件推送端点

前端通过此端点订阅 Agent 操作事件流，
实现 Agent 操作过程的实时可见性。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from src.services.event_service import AgentEvent, event_bus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/events", tags=["events"])


@router.get("/stream")
async def event_stream(request: Request) -> EventSourceResponse:
    """SSE 事件流端点

    前端连接此端点后，会实时收到 Agent 操作事件。
    所有事件广播给所有连接的客户端（单用户模式，无隔离）。
    """
    logger.info("SSE 客户端已连接")

    async def _generate():
        try:
            async for event in event_bus.subscribe():
                yield {
                    "event": event.event_type.value,
                    "data": _event_to_json(event),
                }
        finally:
            logger.info("SSE 客户端已断开")

    return EventSourceResponse(_generate(), ping=5)


@router.get("/history")
async def get_event_history() -> list[dict[str, Any]]:
    """获取历史事件（最近 100 条）"""
    return [asdict(e) for e in event_bus.history]


def _event_to_json(event: AgentEvent) -> str:
    """事件序列化为 JSON 字符串"""
    return json.dumps(asdict(event), ensure_ascii=False)
