"""SSH 会话管理器测试 — 连接建立 / 命令执行 / 会话生命周期。

asyncssh.connect 被 mock，不产生真实网络连接。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import asyncssh
import pytest

from src.models.host import AuthType
from src.services.ssh_session import (
    DEFAULT_COMMAND_TIMEOUT,
    CommandResult,
    SessionInfo,
    SSHSessionManager,
    _SSHSession,
)


def make_host(
    name: str = "web-1",
    *,
    auth_type: AuthType = AuthType.KEY,
    private_key_path: str | None = "/root/.ssh/id_rsa",
    password_encrypted: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        hostname=f"{name}.example.com",
        port=22,
        username="deploy",
        auth_type=auth_type,
        private_key_path=private_key_path,
        password_encrypted=password_encrypted,
    )


def make_conn(stdout: str = "ok", stderr: str = "", exit_status: int = 0):
    """构造 mock 的 asyncssh 连接。"""
    conn = AsyncMock()
    conn.run = AsyncMock(
        return_value=SimpleNamespace(stdout=stdout, stderr=stderr, exit_status=exit_status)
    )
    conn.close = lambda: None
    return conn


# ══════════════════════════════════════════════
# 连接建立
# ══════════════════════════════════════════════


class TestConnect:
    @pytest.mark.asyncio
    async def test_key_auth_passes_client_keys(self):
        session = _SSHSession("sid", make_host())

        with patch("asyncssh.connect", AsyncMock(return_value=make_conn())) as connect:
            await session.connect()

        kwargs = connect.await_args.kwargs
        assert kwargs["client_keys"] == ["/root/.ssh/id_rsa"]
        assert kwargs["host"] == "web-1.example.com"
        assert kwargs["port"] == 22
        assert kwargs["username"] == "deploy"
        assert "password" not in kwargs

    @pytest.mark.asyncio
    async def test_password_auth_decrypts_secret(self):
        host = make_host(
            auth_type=AuthType.PASSWORD,
            private_key_path=None,
            password_encrypted="enc-blob",
        )
        session = _SSHSession("sid", host)

        with patch("asyncssh.connect", AsyncMock(return_value=make_conn())) as connect, patch(
            "src.utils.security.decrypt_password", return_value="plain-pw"
        ):
            await session.connect()

        assert connect.await_args.kwargs["password"] == "plain-pw"
        assert "client_keys" not in connect.await_args.kwargs

    @pytest.mark.asyncio
    async def test_key_auth_without_path_falls_through(self):
        """auth_type=KEY 但没配置密钥路径时不应传 client_keys。"""
        session = _SSHSession("sid", make_host(private_key_path=None))

        with patch("asyncssh.connect", AsyncMock(return_value=make_conn())) as connect:
            await session.connect()

        assert "client_keys" not in connect.await_args.kwargs

    @pytest.mark.asyncio
    async def test_host_key_check_disabled(self):
        session = _SSHSession("sid", make_host())

        with patch("asyncssh.connect", AsyncMock(return_value=make_conn())) as connect:
            await session.connect()

        assert connect.await_args.kwargs["known_hosts"] is None

    @pytest.mark.asyncio
    async def test_os_error_becomes_connection_error(self):
        session = _SSHSession("sid", make_host())

        with patch("asyncssh.connect", AsyncMock(side_effect=OSError("refused"))):
            with pytest.raises(ConnectionError, match="无法连接到"):
                await session.connect()

    @pytest.mark.asyncio
    async def test_asyncssh_error_becomes_connection_error(self):
        session = _SSHSession("sid", make_host())
        err = asyncssh.Error(code=1, reason="auth failed")

        with patch("asyncssh.connect", AsyncMock(side_effect=err)):
            with pytest.raises(ConnectionError):
                await session.connect()

    @pytest.mark.asyncio
    async def test_error_message_includes_target(self):
        session = _SSHSession("sid", make_host())

        with patch("asyncssh.connect", AsyncMock(side_effect=OSError("refused"))):
            with pytest.raises(ConnectionError) as exc:
                await session.connect()

        assert "web-1.example.com:22" in str(exc.value)


# ══════════════════════════════════════════════
# 命令执行
# ══════════════════════════════════════════════


class TestExecCommand:
    @pytest.mark.asyncio
    async def test_returns_populated_result(self):
        session = _SSHSession("sid", make_host())
        session._conn = make_conn(stdout="hello\n", stderr="", exit_status=0)

        result = await session.exec_command("echo hello")

        assert isinstance(result, CommandResult)
        assert result.stdout == "hello\n"
        assert result.exit_code == 0
        assert result.success is True
        assert result.command == "echo hello"
        assert result.host_name == "web-1"
        assert result.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_nonzero_exit_marks_failure(self):
        session = _SSHSession("sid", make_host())
        session._conn = make_conn(stdout="", stderr="not found", exit_status=127)

        result = await session.exec_command("nope")

        assert result.exit_code == 127
        assert result.success is False
        assert result.stderr == "not found"

    @pytest.mark.asyncio
    async def test_none_outputs_normalised_to_empty_string(self):
        session = _SSHSession("sid", make_host())
        session._conn = make_conn(stdout=None, stderr=None, exit_status=None)

        result = await session.exec_command("cmd")

        assert result.stdout == ""
        assert result.stderr == ""
        assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_raises_when_not_connected(self):
        session = _SSHSession("sid", make_host())

        with pytest.raises(ConnectionError, match="未建立"):
            await session.exec_command("ls")

    @pytest.mark.asyncio
    async def test_timeout_raises_timeout_error(self):
        session = _SSHSession("sid", make_host())
        conn = AsyncMock()

        async def never_returns(*args, **kwargs):
            import asyncio

            await asyncio.sleep(10)

        conn.run = never_returns
        session._conn = conn

        with pytest.raises(TimeoutError, match="命令超时"):
            await session.exec_command("sleep 100", timeout=0.01)

    @pytest.mark.asyncio
    async def test_updates_last_activity(self):
        session = _SSHSession("sid", make_host())
        session._conn = make_conn()
        before = session.info.last_activity

        await session.exec_command("ls")

        assert session.info.last_activity >= before

    @pytest.mark.asyncio
    async def test_run_invoked_with_check_false(self):
        """check=False 才能拿到非零退出码而不抛异常。"""
        session = _SSHSession("sid", make_host())
        conn = make_conn()
        session._conn = conn

        await session.exec_command("false")

        assert conn.run.await_args.kwargs["check"] is False


class TestSessionInfo:
    @pytest.mark.asyncio
    async def test_connected_flag_tracks_connection(self):
        session = _SSHSession("sid", make_host())
        assert session.info.connected is False

        session._conn = make_conn()
        assert session.info.connected is True

        await session.close()
        assert session.info.connected is False

    def test_info_carries_host_fields(self):
        info = _SSHSession("sid", make_host()).info
        assert isinstance(info, SessionInfo)
        assert info.session_id == "sid"
        assert info.host_name == "web-1"
        assert info.hostname == "web-1.example.com"
        assert info.username == "deploy"

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self):
        session = _SSHSession("sid", make_host())
        await session.close()
        await session.close()  # 不应抛异常


# ══════════════════════════════════════════════
# 管理器
# ══════════════════════════════════════════════


class TestSSHSessionManager:
    @pytest.mark.asyncio
    async def test_create_session_registers_and_returns_id(self):
        mgr = SSHSessionManager()

        with patch("asyncssh.connect", AsyncMock(return_value=make_conn())):
            sid = await mgr.create_session(make_host())

        assert sid
        assert len(mgr.list_sessions()) == 1
        assert mgr.get_session_info(sid).host_name == "web-1"

    @pytest.mark.asyncio
    async def test_session_ids_are_unique(self):
        mgr = SSHSessionManager()

        with patch("asyncssh.connect", AsyncMock(return_value=make_conn())):
            ids = {await mgr.create_session(make_host(f"h{i}")) for i in range(3)}

        assert len(ids) == 3

    @pytest.mark.asyncio
    async def test_failed_connect_is_not_registered(self):
        mgr = SSHSessionManager()

        with patch("asyncssh.connect", AsyncMock(side_effect=OSError("refused"))):
            with pytest.raises(ConnectionError):
                await mgr.create_session(make_host())

        assert mgr.list_sessions() == []

    @pytest.mark.asyncio
    async def test_execute_command_routes_to_session(self):
        mgr = SSHSessionManager()

        with patch("asyncssh.connect", AsyncMock(return_value=make_conn(stdout="routed"))):
            sid = await mgr.create_session(make_host())
            result = await mgr.execute_command(sid, "echo routed")

        assert result.stdout == "routed"

    @pytest.mark.asyncio
    async def test_execute_on_unknown_session_raises_keyerror(self):
        mgr = SSHSessionManager()
        with pytest.raises(KeyError, match="会话不存在"):
            await mgr.execute_command("ghost", "ls")

    @pytest.mark.asyncio
    async def test_close_session_removes_it(self):
        mgr = SSHSessionManager()

        with patch("asyncssh.connect", AsyncMock(return_value=make_conn())):
            sid = await mgr.create_session(make_host())

        assert await mgr.close_session(sid) is True
        assert mgr.list_sessions() == []
        assert mgr.get_session_info(sid) is None

    @pytest.mark.asyncio
    async def test_close_unknown_session_returns_false(self):
        assert await SSHSessionManager().close_session("ghost") is False

    @pytest.mark.asyncio
    async def test_close_all_clears_registry(self):
        mgr = SSHSessionManager()

        with patch("asyncssh.connect", AsyncMock(return_value=make_conn())):
            for i in range(3):
                await mgr.create_session(make_host(f"h{i}"))

        await mgr.close_all()

        assert mgr.list_sessions() == []

    @pytest.mark.asyncio
    async def test_close_all_on_empty_manager(self):
        await SSHSessionManager().close_all()  # 不应抛异常

    def test_get_session_info_unknown_returns_none(self):
        assert SSHSessionManager().get_session_info("ghost") is None


class TestDefaults:
    def test_default_timeout_is_30s(self):
        assert DEFAULT_COMMAND_TIMEOUT == 30
