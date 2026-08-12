"""Sessions / tmux REST API 端点测试。

两个路由都通过模块级变量注入管理器，测试用替身注入并覆盖 503 分支。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import sessions as sessions_api
from src.api import tmux as tmux_api
from src.services.event_service import EventBus
from src.services.ssh_session import CommandResult, SessionInfo
from src.services.tmux_manager import TmuxClient, TmuxWindow


def _session_info(session_id: str = "s1", host_name: str = "web-1") -> SessionInfo:
    return SessionInfo(
        session_id=session_id,
        host_name=host_name,
        hostname=f"{host_name}.example.com",
        username="deploy",
        connected=True,
        created_at="2026-08-07T10:00:00",
        last_activity="2026-08-07T10:01:00",
    )


def _result(**kw) -> CommandResult:
    defaults = dict(
        session_id="s1",
        host_name="web-1",
        command="ls",
        stdout="out",
        stderr="",
        exit_code=0,
        duration_ms=12.5,
    )
    defaults.update(kw)
    return CommandResult(**defaults)


# ══════════════════════════════════════════════
# /api/sessions
# ══════════════════════════════════════════════


@pytest.fixture
def ssh_manager():
    return SimpleNamespace(
        list_sessions=lambda: [_session_info()],
        get_session_info=lambda sid: _session_info() if sid == "s1" else None,
        create_session=AsyncMock(return_value="new-sid"),
        execute_command=AsyncMock(return_value=_result()),
        close_session=AsyncMock(return_value=True),
    )


@pytest.fixture
def sessions_client(ssh_manager, monkeypatch):
    monkeypatch.setattr(sessions_api, "ssh_manager", ssh_manager)
    monkeypatch.setattr(sessions_api, "event_bus", EventBus())
    app = FastAPI()
    app.include_router(sessions_api.router)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def no_ssh_client(monkeypatch):
    monkeypatch.setattr(sessions_api, "ssh_manager", None)
    app = FastAPI()
    app.include_router(sessions_api.router)
    with TestClient(app) as c:
        yield c


class TestListSessions:
    def test_returns_active_sessions(self, sessions_client):
        body = sessions_client.get("/api/sessions").json()

        assert len(body) == 1
        assert body[0]["session_id"] == "s1"
        assert body[0]["connected"] is True

    def test_503_when_manager_missing(self, no_ssh_client):
        assert no_ssh_client.get("/api/sessions").status_code == 503


class TestExecuteCommand:
    def test_returns_command_output(self, sessions_client):
        resp = sessions_client.post("/api/sessions/s1/exec", json={"command": "ls"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["stdout"] == "out"
        assert body["exit_code"] == 0
        assert body["success"] is True

    def test_nonzero_exit_still_200_with_success_false(self, sessions_client, ssh_manager):
        ssh_manager.execute_command = AsyncMock(
            return_value=_result(exit_code=1, stderr="boom", stdout="")
        )

        body = sessions_client.post("/api/sessions/s1/exec", json={"command": "false"}).json()

        assert body["success"] is False
        assert body["exit_code"] == 1

    def test_unknown_session_returns_404(self, sessions_client, ssh_manager):
        ssh_manager.execute_command = AsyncMock(side_effect=KeyError("SSH 会话不存在: ghost"))

        assert sessions_client.post(
            "/api/sessions/ghost/exec", json={"command": "ls"}
        ).status_code == 404

    def test_timeout_returns_408(self, sessions_client, ssh_manager):
        ssh_manager.execute_command = AsyncMock(side_effect=TimeoutError("命令超时（1s）"))

        assert sessions_client.post(
            "/api/sessions/s1/exec", json={"command": "sleep 99", "timeout": 1}
        ).status_code == 408

    def test_connection_error_returns_502(self, sessions_client, ssh_manager):
        ssh_manager.execute_command = AsyncMock(side_effect=ConnectionError("broken pipe"))

        assert sessions_client.post(
            "/api/sessions/s1/exec", json={"command": "ls"}
        ).status_code == 502

    def test_empty_command_returns_422(self, sessions_client):
        assert sessions_client.post(
            "/api/sessions/s1/exec", json={"command": ""}
        ).status_code == 422

    def test_out_of_range_timeout_returns_422(self, sessions_client):
        assert sessions_client.post(
            "/api/sessions/s1/exec", json={"command": "ls", "timeout": 9999}
        ).status_code == 422

    def test_timeout_forwarded_to_manager(self, sessions_client, ssh_manager):
        sessions_client.post("/api/sessions/s1/exec", json={"command": "ls", "timeout": 7})

        assert ssh_manager.execute_command.await_args.kwargs["timeout"] == 7


class TestCloseSession:
    def test_returns_204(self, sessions_client):
        assert sessions_client.delete("/api/sessions/s1").status_code == 204

    def test_unknown_session_returns_404(self, sessions_client, ssh_manager):
        ssh_manager.close_session = AsyncMock(return_value=False)

        assert sessions_client.delete("/api/sessions/ghost").status_code == 404


class TestSessionEvents:
    def test_exec_publishes_start_and_complete(self, ssh_manager, monkeypatch):
        bus = EventBus()
        monkeypatch.setattr(sessions_api, "ssh_manager", ssh_manager)
        monkeypatch.setattr(sessions_api, "event_bus", bus)

        app = FastAPI()
        app.include_router(sessions_api.router)
        with TestClient(app) as c:
            c.post("/api/sessions/s1/exec", json={"command": "ls"})

        kinds = [e.event_type.value for e in bus.history]
        assert kinds == ["command_start", "command_complete"]

    def test_timeout_publishes_error_event(self, ssh_manager, monkeypatch):
        bus = EventBus()
        ssh_manager.execute_command = AsyncMock(side_effect=TimeoutError("超时"))
        monkeypatch.setattr(sessions_api, "ssh_manager", ssh_manager)
        monkeypatch.setattr(sessions_api, "event_bus", bus)

        app = FastAPI()
        app.include_router(sessions_api.router)
        with TestClient(app) as c:
            c.post("/api/sessions/s1/exec", json={"command": "sleep"})

        assert [e.event_type.value for e in bus.history] == ["command_start", "command_error"]


# ══════════════════════════════════════════════
# /api/tmux
# ══════════════════════════════════════════════


@pytest.fixture
def tmux_mgr():
    return SimpleNamespace(
        session_exists=AsyncMock(return_value=True),
        switch_client=AsyncMock(return_value=True),
        select_window=AsyncMock(return_value=True),
        list_windows=AsyncMock(
            return_value=[
                TmuxWindow(session_name="wetty-b", window_name="bash", window_index=0, active=False),
                TmuxWindow(session_name="wetty-b", window_name="m12", window_index=1, active=True),
            ]
        ),
        list_clients=AsyncMock(
            return_value=[TmuxClient(tty="/dev/pts/3", window="m12", session="wetty-b")]
        ),
    )


@pytest.fixture
def tmux_client(tmux_mgr, monkeypatch):
    monkeypatch.setattr(tmux_api, "tmux_manager", tmux_mgr)
    app = FastAPI()
    app.include_router(tmux_api.router)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def no_tmux_client(monkeypatch):
    monkeypatch.setattr(tmux_api, "tmux_manager", None)
    app = FastAPI()
    app.include_router(tmux_api.router)
    with TestClient(app) as c:
        yield c


class TestSwitchWindow:
    def test_per_client_mode_uses_switch_client(self, tmux_client, tmux_mgr):
        resp = tmux_client.post(
            "/api/tmux/switch-window",
            json={"bastion_name": "b", "window_name": "m12", "client_tty": "/dev/pts/3"},
        )

        assert resp.status_code == 200
        assert resp.json()["session"] == "wetty-b"
        tmux_mgr.switch_client.assert_awaited_once()
        tmux_mgr.select_window.assert_not_awaited()

    def test_global_mode_uses_select_window(self, tmux_client, tmux_mgr):
        resp = tmux_client.post(
            "/api/tmux/switch-window", json={"bastion_name": "b", "window_name": "m12"}
        )

        assert resp.status_code == 200
        tmux_mgr.select_window.assert_awaited_once()
        tmux_mgr.switch_client.assert_not_awaited()

    def test_missing_session_returns_404(self, tmux_client, tmux_mgr):
        tmux_mgr.session_exists = AsyncMock(return_value=False)

        resp = tmux_client.post(
            "/api/tmux/switch-window", json={"bastion_name": "ghost", "window_name": "m12"}
        )

        assert resp.status_code == 404
        assert "ghost" in resp.json()["detail"]

    def test_failed_switch_lists_available_windows(self, tmux_client, tmux_mgr):
        tmux_mgr.select_window = AsyncMock(return_value=False)

        resp = tmux_client.post(
            "/api/tmux/switch-window", json={"bastion_name": "b", "window_name": "ghost"}
        )

        assert resp.status_code == 404
        assert resp.json()["detail"]["available_windows"] == ["bash", "m12"]

    def test_503_when_manager_missing(self, no_tmux_client):
        resp = no_tmux_client.post(
            "/api/tmux/switch-window", json={"bastion_name": "b", "window_name": "m12"}
        )
        assert resp.status_code == 503


class TestListWindows:
    def test_returns_window_list(self, tmux_client):
        body = tmux_client.get("/api/tmux/windows/b").json()

        assert body == [
            {"index": 0, "name": "bash", "active": False},
            {"index": 1, "name": "m12", "active": True},
        ]

    def test_missing_session_returns_404(self, tmux_client, tmux_mgr):
        tmux_mgr.session_exists = AsyncMock(return_value=False)
        assert tmux_client.get("/api/tmux/windows/ghost").status_code == 404

    def test_503_when_manager_missing(self, no_tmux_client):
        assert no_tmux_client.get("/api/tmux/windows/b").status_code == 503


class TestGetClientTtys:
    def test_returns_clients(self, tmux_client):
        body = tmux_client.get("/api/tmux/client-ttys/b").json()

        assert body["session"] == "wetty-b"
        assert body["clients"] == [
            {"tty": "/dev/pts/3", "window": "m12", "session": "wetty-b"}
        ]

    def test_missing_session_returns_404(self, tmux_client, tmux_mgr):
        tmux_mgr.session_exists = AsyncMock(return_value=False)
        assert tmux_client.get("/api/tmux/client-ttys/ghost").status_code == 404

    def test_503_when_manager_missing(self, no_tmux_client):
        assert no_tmux_client.get("/api/tmux/client-ttys/b").status_code == 503
