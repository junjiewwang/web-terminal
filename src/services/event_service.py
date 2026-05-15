"""事件服务 - SSE 事件推送

Agent 操作过程中产生的事件通过 SSE 推送到前端，
实现 Agent 操作的实时可见性。

租户隔离规则：
- 事件携带 tenant_id 字段（"*" 表示全局事件，所有人可见）
- SSE 订阅时传入 subscriber 的 tenant_id 和 is_admin 标志
- 分发过滤：admin 收全部，普通用户只收自己的 + 全局事件
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
    tenant_id: str = "*"  # "*" 表示全局事件，所有人可见

    def to_sse(self) -> str:
        """转为 SSE 格式字符串"""
        payload = json.dumps(asdict(self), ensure_ascii=False)
        return f"event: {self.event_type.value}\ndata: {payload}\n\n"


@dataclass
class _Subscriber:
    """SSE 订阅者元数据"""

    queue: asyncio.Queue[AgentEvent]
    tenant_id: str = ""  # 订阅者的租户 ID
    is_admin: bool = False  # 是否 admin


class EventBus:
    """事件总线 - 发布/订阅模式（支持租户隔离）

    所有 SSE 客户端订阅同一事件总线，
    分发时按租户过滤事件。
    """

    def __init__(self, max_history: int = 100) -> None:
        self._subscribers: list[_Subscriber] = []
        self._history: list[AgentEvent] = []
        self._max_history = max_history
        self._lock = asyncio.Lock()

    async def publish(self, event: AgentEvent) -> None:
        """发布事件到所有符合条件的订阅者"""
        async with self._lock:
            self._history.append(event)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]

            for sub in self._subscribers:
                if self._should_deliver(event, sub):
                    try:
                        sub.queue.put_nowait(event)
                    except asyncio.QueueFull:
                        logger.warning("事件队列已满，丢弃事件: %s", event.event_type)

    @staticmethod
    def _should_deliver(event: AgentEvent, subscriber: _Subscriber) -> bool:
        """判断事件是否应该投递给该订阅者。

        规则：
        - admin 收到所有事件
        - 全局事件（tenant_id="*"）所有人可见
        - 租户事件只投递给对应租户
        """
        if subscriber.is_admin:
            return True
        if event.tenant_id == "*":
            return True
        return event.tenant_id == subscriber.tenant_id

    async def subscribe(
        self,
        tenant_id: str = "",
        is_admin: bool = False,
    ) -> AsyncGenerator[AgentEvent, None]:
        """订阅事件流（用于 SSE 端点）

        Args:
            tenant_id: 订阅者的租户 ID
            is_admin: 是否 admin（admin 接收所有事件）

        使用带超时的 queue.get()，避免在无事件时无限期阻塞
        事件循环，确保 uvicorn 能够调度处理其他并发请求。
        超时后不 yield 任何内容，直接 continue 回循环顶部，
        由 sse_starlette 的 ping 机制负责保活。
        """
        sub = _Subscriber(
            queue=asyncio.Queue(maxsize=256),
            tenant_id=tenant_id,
            is_admin=is_admin,
        )

        async with self._lock:
            self._subscribers.append(sub)

        try:
            while True:
                try:
                    event = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
                    yield event
                except asyncio.TimeoutError:
                    # 超时释放控制权，让事件循环调度其他协程
                    continue
        finally:
            async with self._lock:
                self._subscribers.remove(sub)

    @property
    def history(self) -> list[AgentEvent]:
        """获取历史事件"""
        return list(self._history)


# 全局事件总线单例
event_bus = EventBus()
