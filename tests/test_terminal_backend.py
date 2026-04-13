from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.api import terminal as terminal_api
from src.mcp_server import server as mcp_server
from src.services.terminal_backend import TerminalBackend
from src.services.terminal_manager import TerminalManager, TerminalSession


class DummyHostManager:
    def __init__(self, _session: object) -> None:
        self._target = make_host("root")

    async def get_host_by_id(self, host_id: int):
        if host_id != 1:
            return None
        return self._target

    async def get_connection_path(self, target):
        return [target]

    @staticmethod
    def build_instance_name(path):
        return "--".join(node.name for node in path)


@asynccontextmanager
async def dummy_session_factory():
    yield object()


def make_host(name: str):
    return SimpleNamespace(
        name=name,
        hostname=f"{name}.example.com",
        port=22,
        username="root",
        private_key_path=None,
        password_encrypted=None,
    )


@pytest.mark.asyncio
async def test_create_session_reuses_existing_session_for_same_backend(monkeypatch: pytest.MonkeyPatch):
    started: list[str] = []
    stopped: list[str] = []

    async def fake_start(self, host, decrypted_password=None):
        del host, decrypted_password
        started.append(self.backend.value)
        self._running = True

    async def fake_stop(self):
        stopped.append(self.backend.value)
        self._running = False

    monkeypatch.setattr(TerminalSession, "start", fake_start)
    monkeypatch.setattr(TerminalSession, "stop", fake_stop)

    manager = TerminalManager(default_backend=TerminalBackend.TMUX)
    host = make_host("root")

    first = await manager.create_session("root", host, backend=TerminalBackend.TMUX)
    second = await manager.create_session("root", host, backend=TerminalBackend.TMUX)

    assert first is second
    assert first.backend == TerminalBackend.TMUX
    assert started == ["tmux"]
    assert stopped == []


@pytest.mark.asyncio
async def test_create_session_replaces_session_when_backend_changes(monkeypatch: pytest.MonkeyPatch):
    started: list[str] = []
    stopped: list[str] = []

    async def fake_start(self, host, decrypted_password=None):
        del host, decrypted_password
        started.append(self.backend.value)
        self._running = True

    async def fake_stop(self):
        stopped.append(self.backend.value)
        self._running = False

    monkeypatch.setattr(TerminalSession, "start", fake_start)
    monkeypatch.setattr(TerminalSession, "stop", fake_stop)

    manager = TerminalManager(default_backend=TerminalBackend.TMUX)
    host = make_host("root")

    first = await manager.create_session("root", host, backend=TerminalBackend.TMUX)
    second = await manager.create_session("root", host, backend=TerminalBackend.BROKER)

    assert first is not second
    assert second.backend == TerminalBackend.BROKER
    assert started == ["tmux", "broker"]
    assert stopped == ["tmux"]


@pytest.mark.asyncio
async def test_cleanup_zombie_sessions_skips_when_tmux_missing(monkeypatch: pytest.MonkeyPatch):
    async def raise_file_not_found(*args, **kwargs):
        del args, kwargs
        raise FileNotFoundError("tmux not found")

    monkeypatch.setattr(
        "src.services.terminal_manager.asyncio.create_subprocess_exec",
        raise_file_not_found,
    )

    manager = TerminalManager(default_backend=TerminalBackend.BROKER)
    cleaned = await manager.cleanup_zombie_sessions()

    assert cleaned == 0


@pytest.mark.asyncio
async def test_start_terminal_forwards_backend_and_returns_backend(monkeypatch: pytest.MonkeyPatch):
    create_session = AsyncMock(return_value=SimpleNamespace(
        session_id="session-1",
        instance_name="root",
        running=True,
        backend=TerminalBackend.BROKER,
        tmux_session_name="wetty-root",
    ))
    manager = SimpleNamespace(
        has_running_session=lambda instance_name: False,
        create_session=create_session,
    )

    monkeypatch.setattr(terminal_api, "terminal_manager", manager)
    monkeypatch.setattr(terminal_api, "HostManager", DummyHostManager)
    monkeypatch.setattr(terminal_api, "async_session_factory", dummy_session_factory)
    monkeypatch.setattr(terminal_api, "_decrypt_password", lambda host: None)

    response = await terminal_api.start_terminal(
        terminal_api.StartTerminalRequest(host_id=1, backend=TerminalBackend.BROKER)
    )

    assert response.backend == TerminalBackend.BROKER
    assert response.instance_name == "root"
    assert create_session.await_args.kwargs["backend"] == TerminalBackend.BROKER


@pytest.mark.asyncio
async def test_mcp_connect_path_forwards_backend_and_publishes_backend(monkeypatch: pytest.MonkeyPatch):
    session = SimpleNamespace(
        session_id="session-1",
        instance_name="root",
        running=True,
        backend=TerminalBackend.BROKER,
        tmux_session_name="wetty-root",
    )
    create_session = AsyncMock(return_value=session)
    publish_event = AsyncMock()
    manager = SimpleNamespace(
        has_running_session=lambda instance_name: False,
        create_session=create_session,
    )

    monkeypatch.setattr(mcp_server, "_get_terminal_manager", lambda: manager)
    monkeypatch.setattr(mcp_server, "_decrypt_host_password", lambda host: None)
    monkeypatch.setattr(mcp_server, "_publish_event", publish_event)

    root = make_host("root")
    message = await mcp_server._connect_path([root], backend="broker")

    assert "Backend: broker" in message
    assert create_session.await_args.kwargs["backend"] == "broker"
    assert publish_event.await_args.args[3]["backend"] == "broker"


@pytest.mark.asyncio
async def test_get_session_status_includes_backend(monkeypatch: pytest.MonkeyPatch):
    info = SimpleNamespace(
        session_id="session-1",
        instance_name="root",
        running=True,
        backend="broker",
        created_at="2026-04-13T17:30:00",
        ws_clients=1,
    )
    session = SimpleNamespace(info=info)
    manager = SimpleNamespace(
        get_session_by_id=lambda session_id: session if session_id == "session-1" else None,
        list_sessions=lambda: [info],
    )

    monkeypatch.setattr(mcp_server, "_get_terminal_manager", lambda: manager)

    detail = await mcp_server.get_session_status("session-1")
    listing = await mcp_server.get_session_status()

    assert '"backend": "broker"' in detail
    assert '"backend": "broker"' in listing
