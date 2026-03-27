"""事件服务 - SSE 事件推送

Agent 操作过程中产生的事件通过 SSE 推送到前端，
实现 Agent 操作的实时可见性。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import AsyncGenerator

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    """事件类型"""

    COMMAND_START = "command_start"
    COMMAND_OUTPUT = "command_output"
    COMMAND_COMPLETE = "command_complete"
    COMMAND_ERROR = "command_error"
    SESSION_CREATED = "session_created"
    SESSION_CLOSED = "session_closed"
    SESSION_ERROR = "session_error"
    WINDOW_SWITCHED = "window_switched"


@dataclass
class AgentEvent:
    """Agent 操作事件"""

    event_type: EventType
    session_id: str
    host_name: str
    data: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_sse(self) -> str:
        """转为 SSE 格式字符串"""
        payload = json.dumps(asdict(self), ensure_ascii=False)
        return f"event: {self.event_type.value}\ndata: {payload}\n\n"


class EventBus:
    """事件总线 - 发布/订阅模式

    所有 SSE 客户端订阅同一事件总线，
    任何模块都可以通过 publish() 发送事件。
    """

    def __init__(self, max_history: int = 100) -> None:
        self._subscribers: list[asyncio.Queue[AgentEvent]] = []
        self._history: list[AgentEvent] = []
        self._max_history = max_history
        self._lock = asyncio.Lock()

    async def publish(self, event: AgentEvent) -> None:
        """发布事件到所有订阅者"""
        async with self._lock:
            self._history.append(event)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]

            for queue in self._subscribers:
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    logger.warning("事件队列已满，丢弃事件: %s", event.event_type)

    async def subscribe(self) -> AsyncGenerator[AgentEvent, None]:
        """订阅事件流（用于 SSE 端点）

        使用带超时的 queue.get()，避免在无事件时无限期阻塞
        事件循环，确保 uvicorn 能够调度处理其他并发请求。
        超时后不 yield 任何内容，直接 continue 回循环顶部，
        由 sse_starlette 的 ping 机制负责保活。
        """
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=256)

        async with self._lock:
            self._subscribers.append(queue)

        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                    yield event
                except asyncio.TimeoutError:
                    # 超时释放控制权，让事件循环调度其他协程
                    continue
        finally:
            async with self._lock:
                self._subscribers.remove(queue)

    @property
    def history(self) -> list[AgentEvent]:
        """获取历史事件"""
        return list(self._history)


# 全局事件总线单例
event_bus = EventBus()
