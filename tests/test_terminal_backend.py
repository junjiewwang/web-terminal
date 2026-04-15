from __future__ import annotations

import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.api import terminal as terminal_api
from src.mcp_server import server as mcp_server
from src.services.terminal_backend import TerminalBackend
from src.services.terminal_manager import (
    ClientInfo,
    SessionExitReason,
    TerminalManager,
    TerminalSession,
)


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


# ── Sprint 2: 会话生命周期 ──────────────────────────


def test_classify_exit_normal():
    """正常退出（exit code 0）应归类为 NORMAL。"""
    # os.WIFEXITED + WEXITSTATUS(0)
    # 构造 raw_status: 正常退出 exit code 0 → raw_status = 0 << 8 = 0
    raw_status = 0 << 8  # exit code 0
    reason, code = TerminalSession._classify_exit(raw_status)
    assert reason == SessionExitReason.NORMAL
    assert code == 0


def test_classify_exit_ssh_failed():
    """SSH 连接失败（exit code 255）应归类为 SSH_FAILED。"""
    raw_status = 255 << 8  # exit code 255
    reason, code = TerminalSession._classify_exit(raw_status)
    assert reason == SessionExitReason.SSH_FAILED
    assert code == 255


def test_classify_exit_child_crashed():
    """子进程异常退出（非零非255 exit code）应归类为 CHILD_CRASHED。"""
    raw_status = 1 << 8  # exit code 1
    reason, code = TerminalSession._classify_exit(raw_status)
    assert reason == SessionExitReason.CHILD_CRASHED
    assert code == 1


def test_classify_exit_signaled():
    """子进程被信号终止应归类为 CHILD_CRASHED，exit_code 为负信号号。"""
    import signal
    # 构造被 SIGKILL 终止的 raw_status
    raw_status = signal.SIGKILL  # 9, WIFSIGNALED=True
    reason, code = TerminalSession._classify_exit(raw_status)
    assert reason == SessionExitReason.CHILD_CRASHED
    assert code == -signal.SIGKILL


def test_terminal_info_includes_exit_fields():
    """TerminalInfo 应包含 exit_reason 和 exit_code 字段。"""
    session = TerminalSession(
        session_id="test-session",
        instance_name="test",
        backend=TerminalBackend.BROKER,
    )
    # 初始状态：无退出信息
    info = session.info
    assert info.exit_reason is None
    assert info.exit_code is None

    # 模拟退出
    session._exit_reason = SessionExitReason.SSH_FAILED
    session._exit_code = 255
    info = session.info
    assert info.exit_reason == "ssh_failed"
    assert info.exit_code == 255


def test_on_exit_callback_fires():
    """on_exit 回调在 _fire_on_exit 时应被正确调用。"""
    session = TerminalSession(
        session_id="test-session",
        instance_name="test",
        backend=TerminalBackend.BROKER,
    )
    results: list[tuple] = []

    def callback(sid, reason, code):
        results.append((sid, reason, code))

    session.add_on_exit(callback)
    session._exit_reason = SessionExitReason.NORMAL
    session._exit_code = 0
    session._fire_on_exit()

    assert len(results) == 1
    assert results[0] == ("test-session", SessionExitReason.NORMAL, 0)


# ── Sprint 2: Scrollback 缓冲区 ──────────────────────


def test_scrollback_append_and_get():
    """scrollback 缓冲区能正确追加和读取数据。"""
    session = TerminalSession(
        session_id="test-session",
        instance_name="test",
        backend=TerminalBackend.BROKER,
        scrollback_capacity=100,
    )

    session._append_scrollback(b"hello ")
    session._append_scrollback(b"world\n")
    result = session.get_scrollback()
    assert result == b"hello world\n"


def test_scrollback_capacity_trim():
    """scrollback 超出容量时应裁剪头部。"""
    session = TerminalSession(
        session_id="test-session",
        instance_name="test",
        backend=TerminalBackend.BROKER,
        scrollback_capacity=10,
    )

    session._append_scrollback(b"1234567890")  # 恰好满
    assert len(session.get_scrollback()) == 10

    session._append_scrollback(b"ABC")  # 溢出 3
    result = session.get_scrollback()
    assert len(result) == 10
    assert result == b"4567890ABC"  # 头部被裁剪


# ── Sprint 3: ClientInfo + min-size resize ──────────────


def test_client_info_creation():
    """ClientInfo 应正确初始化默认值。"""
    ws_mock = AsyncMock()
    client = ClientInfo(ws=ws_mock, client_id="test-id")
    assert client.cols == 80
    assert client.rows == 24
    assert client.connected_at is not None


def test_compute_min_size_single_client():
    """单客户端时 min-size 就是该客户端的尺寸。"""
    session = TerminalSession(
        session_id="test-session",
        instance_name="test",
        backend=TerminalBackend.BROKER,
    )
    ws_mock = AsyncMock()
    session._ws_clients["c1"] = ClientInfo(ws=ws_mock, client_id="c1", cols=120, rows=30)

    cols, rows = session._compute_min_size()
    assert cols == 120
    assert rows == 30


def test_compute_min_size_multiple_clients():
    """多客户端时 min-size 取各客户端尺寸的最小值。"""
    session = TerminalSession(
        session_id="test-session",
        instance_name="test",
        backend=TerminalBackend.BROKER,
    )
    ws_mock = AsyncMock()
    session._ws_clients["c1"] = ClientInfo(ws=ws_mock, client_id="c1", cols=120, rows=30)
    session._ws_clients["c2"] = ClientInfo(ws=ws_mock, client_id="c2", cols=80, rows=24)
    session._ws_clients["c3"] = ClientInfo(ws=ws_mock, client_id="c3", cols=100, rows=20)

    cols, rows = session._compute_min_size()
    assert cols == 80
    assert rows == 20


def test_compute_min_size_enforces_lower_bound():
    """min-size 应有下限保护（10×3），避免异常小窗口。"""
    session = TerminalSession(
        session_id="test-session",
        instance_name="test",
        backend=TerminalBackend.BROKER,
    )
    ws_mock = AsyncMock()
    session._ws_clients["c1"] = ClientInfo(ws=ws_mock, client_id="c1", cols=5, rows=1)

    cols, rows = session._compute_min_size()
    assert cols == 10
    assert rows == 3


def test_compute_min_size_empty_returns_default():
    """无客户端时返回默认 80×24。"""
    session = TerminalSession(
        session_id="test-session",
        instance_name="test",
        backend=TerminalBackend.BROKER,
    )
    cols, rows = session._compute_min_size()
    assert cols == 80
    assert rows == 24


def test_add_ws_client_returns_client_id():
    """add_ws_client 应返回分配的 client_id。"""
    session = TerminalSession(
        session_id="test-session",
        instance_name="test",
        backend=TerminalBackend.TMUX,
    )
    ws_mock = AsyncMock()
    client_id = session.add_ws_client(ws_mock)
    assert isinstance(client_id, str)
    assert len(client_id) > 0
    assert client_id in session._ws_clients


def test_remove_ws_client_by_client_id():
    """remove_ws_client 应按 client_id 正确移除。"""
    session = TerminalSession(
        session_id="test-session",
        instance_name="test",
        backend=TerminalBackend.TMUX,
    )
    ws_mock = AsyncMock()
    client_id = session.add_ws_client(ws_mock)
    assert len(session._ws_clients) == 1

    session.remove_ws_client(client_id)
    assert len(session._ws_clients) == 0


def test_remove_ws_client_by_ws_compat():
    """remove_ws_client_by_ws 应按 WebSocket 实例兼容移除。"""
    session = TerminalSession(
        session_id="test-session",
        instance_name="test",
        backend=TerminalBackend.TMUX,
    )
    ws_mock = AsyncMock()
    session.add_ws_client(ws_mock)
    assert len(session._ws_clients) == 1

    session.remove_ws_client_by_ws(ws_mock)
    assert len(session._ws_clients) == 0


# ── Sprint 3: VirtualTerminal 基础测试 ──────────────


def test_virtual_terminal_feed_and_render():
    """VirtualTerminal.feed_and_render 应返回 ANSI 渲染输出。"""
    from src.services.virtual_terminal import VirtualTerminal

    vt = VirtualTerminal(cols=40, rows=5)
    result = vt.feed_and_render("hello world\r\n$ ")
    # 应返回非空的 ANSI 文本
    assert len(result) > 0
    assert "hello" in result or "world" in result


def test_virtual_terminal_full_screen_dump():
    """VirtualTerminal.full_screen_dump 应返回完整屏幕快照。"""
    from src.services.virtual_terminal import VirtualTerminal

    vt = VirtualTerminal(cols=20, rows=3)
    vt.feed_and_render("line1\r\nline2\r\n")
    # 清除 dirty 后再 dump
    dump = vt.full_screen_dump()
    assert len(dump) > 0
    # 快照应包含 ANSI 光标归位序列
    assert "\x1b[H" in dump


def test_virtual_terminal_resize():
    """VirtualTerminal.resize 应正确更新尺寸。"""
    from src.services.virtual_terminal import VirtualTerminal

    vt = VirtualTerminal(cols=80, rows=24)
    assert vt.cols == 80
    assert vt.rows == 24

    vt.resize(40, 12)
    assert vt.cols == 40
    assert vt.rows == 12


def test_broker_session_creates_vterm():
    """Broker 模式的 TerminalSession 应自动创建 VirtualTerminal。"""
    session = TerminalSession(
        session_id="test-session",
        instance_name="test",
        backend=TerminalBackend.BROKER,
    )
    assert session._vterm is not None


def test_tmux_session_no_vterm():
    """TMUX 模式的 TerminalSession 不应创建 VirtualTerminal。"""
    session = TerminalSession(
        session_id="test-session",
        instance_name="test",
        backend=TerminalBackend.TMUX,
    )
    assert session._vterm is None
