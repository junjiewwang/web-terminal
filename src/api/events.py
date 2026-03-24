"""SSE 事件推送端点

前端通过此端点订阅 Agent 操作事件流，
实现 Agent 操作过程的实时可见性。

关键设计说明：
  前端使用 fetch + ReadableStream（非 EventSource）连接此端点，
  通过 AbortController.abort() 可以强制关闭底层 TCP 连接。
  当 TCP 连接被关闭时，sse_starlette 的 _listen_for_disconnect
  会检测到断开并取消 task group，进而中断 _generate() 生成器。
  subscribe() 的 finally 块会自动清理订阅者队列。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict

from typing import Any

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from src.services.event_service import AgentEvent, event_bus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/events", tags=["events"])


@router.get("/stream")
async def event_stream() -> EventSourceResponse:
    """SSE 事件流端点

    前端连接此端点后，会实时收到 Agent 操作事件：
    - command_start: 命令开始执行
    - command_output: 命令输出
    - command_complete: 命令执行完成
    - command_error: 命令执行错误
    - session_created: 新会话创建
    - session_closed: 会话关闭

    注意：
    - ping=5 让 sse_starlette 每 5 秒发送心跳保活
    - subscribe() 内部使用 1 秒超时的 queue.get()，
      确保事件循环不会被 SSE 长连接无限期阻塞
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
