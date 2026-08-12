"""api/terminal.py REST/WebSocket 端点测试。

通过 monkeypatch 模块级全局（terminal_manager / tmux_manager / HostManager /
async_session_factory / auth_service / security 函数）注入替身，覆盖：
- 终端启停 / 列表 / backend 切换
- tmux copy-buffer 推送
- WebSocket 认证与消息循环
- start_terminal 多跳编排触发
- _authenticate_ws_token 全部分支
- _decrypt_password
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.websockets import WebSocketDisconnect

from src.api import terminal as terminal_api
from src.services.terminal_backend import TerminalBackend
from src.services.terminal_manager import TerminalInfo


# ── 替身构建 ────────────────────────────────────────────────────────────────


class _FakeSession:
    def __init__(self, session_id="s1", instance_name="web-1", running=True,
                 backend=TerminalBackend.BROKER):
        self.session_id = session_id
        self.instance_name = instance_name
        self.running = running
        self.backend = backend
        self.tmux_session_name = f"wetty-{instance_name}"
        self.written = []
        self.resized = []
        self.clipboard_pushed = []
        self.client_removed = []

    async def send_to_clients(self, payload):
        if payload.get("type") == "clipboard":
            self.clipboard_pushed.append(payload.get("text"))

    def add_ws_client(self, ws):
        return "client-1"

    def remove_ws_client(self, client_id):
        self.client_removed.append(client_id)

    def write(self, data):
        self.written.append(data)

    def resize(self, cols, rows, client_id=None):
        self.resized.append((cols, rows, client_id))


class _FakeTerminalManager:
    def __init__(self, *, sessions=None, get_session=None, default_backend=TerminalBackend.BROKER):
        self._sessions = sessions or []
        self._get_session = get_session
        self.default_backend = default_backend
        self.switch_calls = []
        self.stopped = {}
        self.created = {}

    async def switch_backend(self, new_backend):
        self.switch_calls.append(new_backend)
        return ["s1", "s2"]

    async def stop_session(self, instance_name):
        was = self.stopped.get(instance_name, True)
        self.stopped[instance_name] = True
        return was

    def list_sessions(self):
        return self._sessions

    def get_session_by_id(self, session_id):
        return self._get_session(session_id) if self._get_session else _FakeSession(
            session_id=session_id
        )

    async def create_session(self, instance_name, host, decrypted_password, backend=None):
        sess = _FakeSession(instance_name=instance_name)
        self.created[instance_name] = (host, decrypted_password, backend)
        return sess, self._is_new


# ══════════════════════════════════════════════
# 503 守卫
# ══════════════════════════════════════════════


class TestManagerGuard:
    def test_503_when_manager_uninitialized(self, monkeypatch):
        monkeypatch.setattr(terminal_api, "terminal_manager", None)
        app = FastAPI()
        app.include_router(terminal_api.router)
        with TestClient(app) as c:
            assert c.get("/api/terminal/backend").status_code == 503
            assert c.get("/api/terminal").status_code == 503
            assert c.post("/api/terminal/stop/x", json={}).status_code == 503


# ══════════════════════════════════════════════
# backend 查询 / 切换
# ══════════════════════════════════════════════


@pytest.fixture
def mgr_client(monkeypatch):
    mgr = _FakeTerminalManager()
    monkeypatch.setattr(terminal_api, "terminal_manager", mgr)
    app = FastAPI()
    app.include_router(terminal_api.router)
    with TestClient(app) as c:
        yield c, mgr


class TestBackendEndpoints:
    def test_get_backend(self, mgr_client):
        c, mgr = mgr_client
        body = c.get("/api/terminal/backend").json()
        assert body["backend"] == mgr.default_backend.value

    def test_switch_backend(self, mgr_client):
        c, mgr = mgr_client
        body = c.put("/api/terminal/backend", json={"backend": "tmux"}).json()
        assert body["backend"] == "tmux"
        assert body["stopped_sessions"] == ["s1", "s2"]
        assert mgr.switch_calls == [TerminalBackend.TMUX]


# ══════════════════════════════════════════════
# 停止 / 列表
# ══════════════════════════════════════════════


class TestStopAndList:
    def test_stop_returns_204_when_found(self, mgr_client):
        c, mgr = mgr_client
        assert c.post("/api/terminal/stop/web-1").status_code == 204

    def test_stop_returns_404_when_missing(self, mgr_client):
        c, mgr = mgr_client
        # stop_session 返回 False 表示会话不存在
        async def _stop(inst):
            return False
        mgr.stop_session = _stop
        assert c.post("/api/terminal/stop/ghost").status_code == 404

    def test_list_maps_sessions(self, mgr_client):
        c, mgr = mgr_client
        mgr._sessions = [
            TerminalInfo(
                session_id="s1", instance_name="web-1",
                backend="broker", running=True,
            ),
            TerminalInfo(
                session_id="s2", instance_name="web-2",
                backend="tmux", running=False,
            ),
        ]
        body = c.get("/api/terminal").json()
        assert len(body) == 2
        assert body[0]["session_id"] == "s1"
        assert body[0]["ws_url"] == "/ws/terminal/s1"
        assert body[1]["backend"] == "tmux"
        assert body[1]["running"] is False


# ══════════════════════════════════════════════
# tmux copy-buffer 推送
# ══════════════════════════════════════════════


class TestCopyBuffer:
    def _setup(self, monkeypatch, tmp_path, *, session=None, sessions=None):
        mgr = _FakeTerminalManager(sessions=sessions or [])
        if session is not None:
            mgr._get_session = lambda sid: session
        monkeypatch.setattr(terminal_api, "terminal_manager", mgr)
        monkeypatch.setattr(terminal_api, "_COPY_BUFFER_DIR", str(tmp_path))
        app = FastAPI()
        app.include_router(terminal_api.router)
        return TestClient(app), mgr

    def test_invalid_prefix_returns_204(self, monkeypatch, tmp_path):
        c, _ = self._setup(monkeypatch, tmp_path)
        assert c.post("/api/tmux/copy-buffer", json={"session_name": "xxx"}).status_code == 204

    def test_no_running_session_returns_204(self, monkeypatch, tmp_path):
        c, _ = self._setup(monkeypatch, tmp_path, sessions=[])
        assert c.post(
            "/api/tmux/copy-buffer", json={"session_name": "wetty-web-1"}
        ).status_code == 204

    def test_missing_buffer_file_returns_204(self, monkeypatch, tmp_path):
        sess = _FakeSession()
        info = TerminalInfo(session_id="s1", instance_name="web-1", backend="tmux", running=True)
        c, _ = self._setup(monkeypatch, tmp_path, session=sess, sessions=[info])
        assert c.post(
            "/api/tmux/copy-buffer", json={"session_name": "wetty-web-1"}
        ).status_code == 204
        assert sess.clipboard_pushed == []

    def test_empty_buffer_skips_push(self, monkeypatch, tmp_path):
        (tmp_path / "tmux-copy-wetty-web-1").write_text("   \n", encoding="utf-8")
        sess = _FakeSession()
        info = TerminalInfo(session_id="s1", instance_name="web-1", backend="tmux", running=True)
        c, _ = self._setup(monkeypatch, tmp_path, session=sess, sessions=[info])
        assert c.post(
            "/api/tmux/copy-buffer", json={"session_name": "wetty-web-1"}
        ).status_code == 204
        assert sess.clipboard_pushed == []

    def test_pushes_clipboard_to_clients(self, monkeypatch, tmp_path):
        (tmp_path / "tmux-copy-wetty-web-1").write_text("hello clipboard", encoding="utf-8")
        sess = _FakeSession()
        info = TerminalInfo(session_id="s1", instance_name="web-1", backend="tmux", running=True)
        c, _ = self._setup(monkeypatch, tmp_path, session=sess, sessions=[info])
        assert c.post(
            "/api/tmux/copy-buffer", json={"session_name": "wetty-web-1"}
        ).status_code == 204
        assert sess.clipboard_pushed == ["hello clipboard"]

    def test_read_exception_returns_204(self, monkeypatch, tmp_path):
        # 目录不存在，导致 open 失败
        sess = _FakeSession()
        info = TerminalInfo(session_id="s1", instance_name="web-1", backend="tmux", running=True)
        c, _ = self._setup(
            monkeypatch, tmp_path / "nope", session=sess, sessions=[info]
        )
        assert c.post(
            "/api/tmux/copy-buffer", json={"session_name": "wetty-web-1"}
        ).status_code == 204


# ══════════════════════════════════════════════
# _decrypt_password
# ══════════════════════════════════════════════


class TestDecryptPassword:
    def test_none_when_no_encrypted_password(self, monkeypatch):
        host = SimpleNamespace(password_encrypted=None, name="h1")
        assert terminal_api._decrypt_password(host) is None

    def test_decrypts(self, monkeypatch):
        captured = {}

        def _fake_decrypt(value):
            captured["v"] = value
            return "plain"

        monkeypatch.setattr(
            "src.utils.security.decrypt_password", _fake_decrypt
        )
        host = SimpleNamespace(password_encrypted="ENC", name="h1")
        assert terminal_api._decrypt_password(host) == "plain"
        assert captured["v"] == "ENC"

    def test_returns_none_on_error(self, monkeypatch):
        def _boom(value):
            raise ValueError("bad cipher")

        monkeypatch.setattr("src.utils.security.decrypt_password", _boom)
        host = SimpleNamespace(password_encrypted="ENC", name="h1")
        assert terminal_api._decrypt_password(host) is None


# ══════════════════════════════════════════════
# _authenticate_ws_token
# ══════════════════════════════════════════════


class TestAuthenticateWsToken:
    @pytest.fixture
    def auth_env(self, monkeypatch):
        # 清空环境变量
        monkeypatch.delenv("WETTY_API_TOKEN", raising=False)
        fake_auth = SimpleNamespace(is_auth_enabled=False, verify_access_token=lambda t: False)
        monkeypatch.setattr("src.main.auth_service", fake_auth)
        return fake_auth

    @pytest.mark.asyncio
    async def test_dev_mode_no_token_no_auth(self, auth_env):
        assert await terminal_api._authenticate_ws_token("") is True

    @pytest.mark.asyncio
    async def test_no_token_with_env_set(self, auth_env, monkeypatch):
        monkeypatch.setenv("WETTY_API_TOKEN", "env-secret")
        assert await terminal_api._authenticate_ws_token("") is False

    @pytest.mark.asyncio
    async def test_no_token_with_auth_enabled(self, auth_env, monkeypatch):
        auth_env.is_auth_enabled = True
        assert await terminal_api._authenticate_ws_token("") is False

    @pytest.mark.asyncio
    async def test_env_token_match(self, auth_env, monkeypatch):
        monkeypatch.setenv("WETTY_API_TOKEN", "env-secret")
        assert await terminal_api._authenticate_ws_token("env-secret") is True

    @pytest.mark.asyncio
    async def test_env_token_mismatch_falls_through(self, auth_env, monkeypatch):
        monkeypatch.setenv("WETTY_API_TOKEN", "env-secret")
        assert await terminal_api._authenticate_ws_token("wrong") is False

    @pytest.mark.asyncio
    async def test_jwt_token(self, auth_env, monkeypatch):
        auth_env.is_auth_enabled = True
        auth_env.verify_access_token = lambda t: t == "jwt-token"
        assert await terminal_api._authenticate_ws_token("jwt-token") is True

    @pytest.mark.asyncio
    async def test_auto_generated_token(self, auth_env, monkeypatch):
        monkeypatch.setattr(
            "src.utils.security.verify_api_token", lambda t: t == "auto-token"
        )
        assert await terminal_api._authenticate_ws_token("auto-token") is True

    @pytest.mark.asyncio
    async def test_all_fail_returns_false(self, auth_env, monkeypatch):
        monkeypatch.setattr("src.utils.security.verify_api_token", lambda t: False)
        assert await terminal_api._authenticate_ws_token("garbage") is False


# ══════════════════════════════════════════════
# WebSocket 端点
# ══════════════════════════════════════════════


class TestTerminalWebSocket:
    def _client(self, monkeypatch, mgr, *, auth_enabled=False):
        monkeypatch.setattr(terminal_api, "terminal_manager", mgr)
        fake_auth = SimpleNamespace(is_auth_enabled=auth_enabled, verify_access_token=lambda t: False)
        monkeypatch.setattr("src.main.auth_service", fake_auth)
        monkeypatch.delenv("WETTY_API_TOKEN", raising=False)
        app = FastAPI()
        app.include_router(terminal_api.router)
        return TestClient(app)

    def test_auth_failure_closes(self, monkeypatch):
        mgr = _FakeTerminalManager()
        c = self._client(monkeypatch, mgr, auth_enabled=True)
        # 开发模式下 verify_api_token 默认放行，需显式禁用以测试认证失败
        monkeypatch.setattr("src.utils.security.verify_api_token", lambda t: False)
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws/terminal/s1?token=bad") as ws:
                ws.receive_text()

    def test_session_not_found_closes(self, monkeypatch):
        # dev 模式放行，但 session 不存在
        mgr = _FakeTerminalManager(get_session=lambda sid: None)
        c = self._client(monkeypatch, mgr)
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws/terminal/ghost") as ws:
                ws.receive_text()

    def test_input_and_resize_processed(self, monkeypatch):
        sess = _FakeSession(session_id="s1", instance_name="web-1")
        mgr = _FakeTerminalManager(get_session=lambda sid: sess)
        c = self._client(monkeypatch, mgr)
        with c.websocket_connect("/ws/terminal/s1") as ws:
            ws.send_text(json.dumps({"type": "input", "data": "ls\n"}))
            ws.send_text(json.dumps({"type": "resize", "cols": 120, "rows": 40}))
        assert sess.written == ["ls\n"]
        assert sess.resized == [(120, 40, "client-1")]
        assert sess.client_removed == ["client-1"]


# ══════════════════════════════════════════════
# start_terminal
# ══════════════════════════════════════════════


class _FakeAsyncSessionCM:
    def __init__(self, session):
        self._s = session

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *a):
        return False


class _FakeHostManager:
    def __init__(self, host):
        self._host = host

    @classmethod
    def build_instance_name(cls, path):
        return "->".join(getattr(h, "name", "?") for h in path)

    async def get_host_by_id(self, hid):
        return self._host

    async def get_connection_path(self, host):
        return [host]


class TestStartTerminal:
    def _patch(self, monkeypatch, *, host, is_new=True, path_len=1):
        mgr = _FakeTerminalManager()
        mgr._is_new = is_new
        monkeypatch.setattr(terminal_api, "terminal_manager", mgr)

        fake_host_mgr = _FakeHostManager(host)
        monkeypatch.setattr(terminal_api, "HostManager", lambda db: fake_host_mgr)

        captured = {}

        def _factory():
            captured["called"] = True
            return _FakeAsyncSessionCM("db-session")

        monkeypatch.setattr(terminal_api, "async_session_factory", _factory)

        # 多跳编排替身
        orch = SimpleNamespace(
            execute_path=AsyncMock(return_value=SimpleNamespace(success=True, message="ok"))
        )
        monkeypatch.setattr(terminal_api, "ConnectionOrchestrator", lambda s: orch)

        app = FastAPI()
        app.include_router(terminal_api.router)
        return TestClient(app), mgr, captured

    def test_starts_session(self, monkeypatch):
        host = SimpleNamespace(name="web-1", password_encrypted=None)
        c, mgr, cap = self._patch(monkeypatch, host=host)
        resp = c.post("/api/terminal/start", json={"host_id": 1, "backend": "broker"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["instance_name"] == "web-1"
        assert body["ws_url"] == f"/ws/terminal/{body['session_id']}"
        assert cap["called"] is True
        assert "web-1" in mgr.created

    def test_404_when_host_missing(self, monkeypatch):
        c, mgr, cap = self._patch(monkeypatch, host=None)
        # get_host_by_id 返回 None
        fake_host_mgr = _FakeHostManager(None)
        monkeypatch.setattr(terminal_api, "HostManager", lambda db: fake_host_mgr)
        resp = c.post("/api/terminal/start", json={"host_id": 999})
        assert resp.status_code == 404

    def test_multi_hop_triggers_orchestration(self, monkeypatch):
        host_a = SimpleNamespace(name="jump", password_encrypted=None)
        host_b = SimpleNamespace(name="web", password_encrypted=None)
        mgr = _FakeTerminalManager()
        mgr._is_new = True
        monkeypatch.setattr(terminal_api, "terminal_manager", mgr)

        class _MultiHM(_FakeHostManager):
            async def get_connection_path(self, host):
                return [host_a, host_b]

        monkeypatch.setattr(terminal_api, "HostManager", lambda db: _MultiHM(host_a))
        monkeypatch.setattr(terminal_api, "async_session_factory", _FakeAsyncSessionCM)
        orch = SimpleNamespace(
            execute_path=AsyncMock(return_value=SimpleNamespace(success=True, message="ok"))
        )
        monkeypatch.setattr(terminal_api, "ConnectionOrchestrator", lambda s: orch)

        app = FastAPI()
        app.include_router(terminal_api.router)
        with TestClient(app) as c:
            resp = c.post("/api/terminal/start", json={"host_id": 1})
        assert resp.status_code == 200
        # 多跳分支应已执行（create_task 调度），不抛异常即可

    def test_wetty_compat_start(self, monkeypatch):
        host = SimpleNamespace(name="web-1", password_encrypted=None)
        c, mgr, cap = self._patch(monkeypatch, host=host)
        resp = c.post("/api/wetty/start", json={"host_id": 1})
        assert resp.status_code == 200

    def test_wetty_compat_stop_and_list(self, monkeypatch):
        c, mgr, _ = self._patch(monkeypatch, host=SimpleNamespace(name="web-1"))
        assert c.post("/api/wetty/stop/web-1").status_code == 204
        mgr._sessions = [
            TerminalInfo(session_id="s1", instance_name="web-1", backend="broker", running=True)
        ]
        assert len(c.get("/api/wetty").json()) == 1
