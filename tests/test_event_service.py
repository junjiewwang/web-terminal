"""事件总线与终端后端解析测试。"""

from __future__ import annotations

import asyncio
import json

import pytest

from src.services.event_service import AgentEvent, EventBus, EventType
from src.services.terminal_backend import (
    DEFAULT_TERMINAL_BACKEND,
    TERMINAL_BACKEND_ENV_VAR,
    TerminalBackend,
    read_default_terminal_backend,
    resolve_terminal_backend,
)

# ══════════════════════════════════════════════
# terminal_backend
# ══════════════════════════════════════════════


class TestResolveTerminalBackend:
    def test_none_returns_default_fallback(self):
        assert resolve_terminal_backend(None) is DEFAULT_TERMINAL_BACKEND

    def test_none_honours_explicit_fallback(self):
        assert resolve_terminal_backend(None, fallback=TerminalBackend.TMUX) is TerminalBackend.TMUX

    def test_empty_and_whitespace_use_fallback(self):
        for raw in ("", "   ", "\t"):
            assert resolve_terminal_backend(raw, fallback=TerminalBackend.TMUX) is TerminalBackend.TMUX

    def test_parses_valid_strings(self):
        assert resolve_terminal_backend("tmux") is TerminalBackend.TMUX
        assert resolve_terminal_backend("broker") is TerminalBackend.BROKER

    def test_normalizes_case_and_whitespace(self):
        assert resolve_terminal_backend("  TMUX  ") is TerminalBackend.TMUX
        assert resolve_terminal_backend("Broker") is TerminalBackend.BROKER

    def test_enum_passes_through(self):
        assert resolve_terminal_backend(TerminalBackend.BROKER) is TerminalBackend.BROKER

    def test_invalid_value_raises_with_options_listed(self):
        with pytest.raises(ValueError, match="tmux \\| broker"):
            resolve_terminal_backend("kubernetes")

    def test_default_backend_is_broker(self):
        assert DEFAULT_TERMINAL_BACKEND is TerminalBackend.BROKER


class TestReadDefaultTerminalBackend:
    def test_reads_from_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(TERMINAL_BACKEND_ENV_VAR, "tmux")
        assert read_default_terminal_backend() is TerminalBackend.TMUX

    def test_falls_back_when_env_unset(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv(TERMINAL_BACKEND_ENV_VAR, raising=False)
        assert read_default_terminal_backend() is DEFAULT_TERMINAL_BACKEND

    def test_invalid_env_raises(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(TERMINAL_BACKEND_ENV_VAR, "bogus")
        with pytest.raises(ValueError):
            read_default_terminal_backend()


# ══════════════════════════════════════════════
# event_service
# ══════════════════════════════════════════════


def _event(session_id: str = "s1", host: str = "web-1") -> AgentEvent:
    return AgentEvent(
        event_type=EventType.COMMAND_START,
        session_id=session_id,
        host_name=host,
        data={"command": "ls -la"},
    )


class TestAgentEvent:
    def test_timestamp_autopopulated(self):
        assert _event().timestamp

    def test_data_defaults_to_empty_dict(self):
        ev = AgentEvent(event_type=EventType.SESSION_CLOSED, session_id="s", host_name="h")
        assert ev.data == {}

    def test_to_sse_uses_event_and_data_frames(self):
        sse = _event().to_sse()
        assert sse.startswith("event: command_start\n")
        assert sse.endswith("\n\n"), "SSE 帧必须以空行结尾"
        assert "\ndata: " in sse

    def test_to_sse_payload_is_valid_json(self):
        sse = _event().to_sse()
        payload = json.loads(sse.split("\ndata: ", 1)[1].rstrip("\n"))
        assert payload["session_id"] == "s1"
        assert payload["host_name"] == "web-1"
        assert payload["data"]["command"] == "ls -la"

    def test_to_sse_keeps_unicode_readable(self):
        ev = AgentEvent(
            event_type=EventType.COMMAND_ERROR,
            session_id="s",
            host_name="主机",
            data={"error": "连接失败"},
        )
        assert "连接失败" in ev.to_sse()

    def test_to_sse_payload_is_single_line(self):
        """含换行的数据不能破坏 SSE 帧结构。"""
        ev = AgentEvent(
            event_type=EventType.COMMAND_OUTPUT,
            session_id="s",
            host_name="h",
            data={"output": "line1\nline2"},
        )
        body = ev.to_sse()[: -len("\n\n")]
        assert len(body.split("\n")) == 2, "event 行 + data 行"


class TestEventBusHistory:
    @pytest.mark.asyncio
    async def test_publish_records_history(self):
        bus = EventBus()
        await bus.publish(_event())
        assert len(bus.history) == 1

    @pytest.mark.asyncio
    async def test_history_trimmed_to_max(self):
        bus = EventBus(max_history=3)
        for i in range(5):
            await bus.publish(_event(session_id=f"s{i}"))

        history = bus.history
        assert len(history) == 3
        # 保留最新的 3 条
        assert [e.session_id for e in history] == ["s2", "s3", "s4"]

    @pytest.mark.asyncio
    async def test_history_returns_copy(self):
        """history 应返回副本，外部改动不影响内部状态。"""
        bus = EventBus()
        await bus.publish(_event())
        bus.history.clear()
        assert len(bus.history) == 1

    def test_history_empty_initially(self):
        assert EventBus().history == []


class TestEventBusSubscribe:
    @pytest.mark.asyncio
    async def test_subscriber_receives_published_event(self):
        bus = EventBus()
        received: list[AgentEvent] = []

        async def consume() -> None:
            async for ev in bus.subscribe():
                received.append(ev)
                break

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)  # 等待订阅者注册队列
        await bus.publish(_event(session_id="broadcast"))
        await asyncio.wait_for(task, timeout=3)

        assert len(received) == 1
        assert received[0].session_id == "broadcast"

    @pytest.mark.asyncio
    async def test_all_subscribers_receive_broadcast(self):
        bus = EventBus()
        received: list[str] = []

        async def consume(tag: str) -> None:
            async for _ev in bus.subscribe():
                received.append(tag)
                break

        tasks = [asyncio.create_task(consume(f"c{i}")) for i in range(3)]
        await asyncio.sleep(0.05)
        await bus.publish(_event())
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=3)

        assert sorted(received) == ["c0", "c1", "c2"]

    @pytest.mark.asyncio
    async def test_subscriber_unregistered_on_exit(self):
        """订阅生成器退出后应从订阅者列表移除，避免泄漏。"""
        bus = EventBus()

        async def consume() -> None:
            async for _ev in bus.subscribe():
                break

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        assert len(bus._subscribers) == 1

        await bus.publish(_event())
        await asyncio.wait_for(task, timeout=3)

        assert len(bus._subscribers) == 0

    @pytest.mark.asyncio
    async def test_publish_without_subscribers_is_noop(self):
        bus = EventBus()
        await bus.publish(_event())  # 不应抛异常
        assert len(bus.history) == 1
