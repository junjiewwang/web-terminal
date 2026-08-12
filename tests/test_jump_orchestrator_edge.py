"""多跳编排器补充测试 — 边界路径 / entry_spec 解析 / 凭据回退 / 窗口创建。

补充 test_jump_orchestrator.py 未覆盖的分支。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.models.host import AuthType, EntryType, Host, HostType
from src.services.jump_orchestrator import (
    ConnectionOrchestrator,
    ConnectionResult,
    JumpOrchestrator,
    JumpResult,
)


def _host(name: str, *, entry: dict | None = None, **kw) -> Host:
    host = Host()
    host.id = abs(hash(name)) % 10000
    host.name = name
    host.hostname = kw.pop("hostname", "10.0.0.1")
    host.port = kw.pop("port", 22)
    host.username = kw.pop("username", "root")
    host.auth_type = AuthType.KEY
    host.host_type = kw.pop("host_type", HostType.NESTED)
    host.parent_id = kw.pop("parent_id", None)
    host.ready_pattern = kw.pop("ready_pattern", None)
    host.entry_spec = json.dumps(entry, ensure_ascii=False) if entry else None
    host.entry_password_encrypted = kw.pop("entry_password_encrypted", None)
    host.credential_ref = kw.pop("credential_ref", None)
    host.password_encrypted = None
    host.private_key_path = None
    host.description = None
    host.tags = None
    host.children = []
    return host


def _pty() -> AsyncMock:
    session = AsyncMock()
    session.send_input = AsyncMock()
    session.wait_for = AsyncMock(return_value="matched")
    return session


# ══════════════════════════════════════════════
# 路径边界
# ══════════════════════════════════════════════


class TestExecutePathEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_path_fails(self):
        result = await ConnectionOrchestrator(_pty()).execute_path(
            path=[], tmux_session_name="s", window_name="0"
        )

        assert result.success is False
        assert "路径为空" in result.message

    @pytest.mark.asyncio
    async def test_single_node_is_immediate_success(self):
        """只有根节点时无需任何跳转动作。"""
        pty = _pty()

        result = await ConnectionOrchestrator(pty).execute_path(
            path=[_host("root", host_type=HostType.ROOT)],
            tmux_session_name="s",
            window_name="0",
        )

        assert result.success is True
        assert "根节点" in result.message
        pty.send_input.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ready_timeout_reports_pattern(self):
        pty = _pty()
        pty.wait_for = AsyncMock(side_effect=TimeoutError("timeout"))
        root = _host("root", host_type=HostType.ROOT, ready_pattern=r"\[Host\]>")
        child = _host(
            "child",
            parent_id=root.id,
            entry={"type": EntryType.MENU_SEND.value, "value": "10.0.0.8"},
        )

        result = await ConnectionOrchestrator(pty).execute_path(
            path=[root, child],
            tmux_session_name="s",
            window_name="0",
            skip_window_creation=True,
        )

        assert result.success is False
        assert "就绪超时" in result.message
        assert r"\[Host\]>" in result.message
        assert result.actions_executed == 0


# ══════════════════════════════════════════════
# tmux 窗口创建
# ══════════════════════════════════════════════


class TestWindowCreation:
    @pytest.mark.asyncio
    async def test_creates_window_when_not_skipped(self):
        pty = _pty()
        root = _host("root", host_type=HostType.ROOT)
        child = _host(
            "child",
            parent_id=root.id,
            entry={"type": EntryType.MENU_SEND.value, "value": "10.0.0.8"},
        )

        with patch("asyncio.sleep", AsyncMock()):
            await ConnectionOrchestrator(pty).execute_path(
                path=[root, child],
                tmux_session_name="wetty-root",
                window_name="w1",
                skip_window_creation=False,
            )

        sent = [c.args[0] for c in pty.send_input.await_args_list]
        assert any("new-window" in s and "wetty-root" in s for s in sent)
        assert any("select-window" in s for s in sent)

    @pytest.mark.asyncio
    async def test_window_creation_failure_reported(self):
        pty = _pty()
        pty.send_input = AsyncMock(side_effect=RuntimeError("tmux 挂了"))
        root = _host("root", host_type=HostType.ROOT)
        child = _host(
            "child",
            parent_id=root.id,
            entry={"type": EntryType.MENU_SEND.value, "value": "x"},
        )

        result = await ConnectionOrchestrator(pty).execute_path(
            path=[root, child],
            tmux_session_name="s",
            window_name="w1",
            skip_window_creation=False,
        )

        assert result.success is False
        assert "创建 tmux 窗口失败" in result.message


# ══════════════════════════════════════════════
# entry_spec 解析
# ══════════════════════════════════════════════


class TestParseEntrySpec:
    def test_missing_spec_returns_default(self):
        spec = ConnectionOrchestrator._parse_entry_spec(_host("bare"))
        assert spec.value == "" or spec.value is None

    def test_valid_spec_parsed(self):
        host = _host(
            "hop", entry={"type": EntryType.SSH_COMMAND.value, "value": "ssh a@b"}
        )
        spec = ConnectionOrchestrator._parse_entry_spec(host)

        assert spec.type == EntryType.SSH_COMMAND
        assert spec.value == "ssh a@b"

    def test_malformed_spec_falls_back_to_default(self):
        """损坏的 entry_spec 不应让编排崩溃。"""
        host = _host("broken")
        host.entry_spec = json.dumps({"type": "not-a-valid-type", "value": "x"})

        spec = ConnectionOrchestrator._parse_entry_spec(host)

        assert spec is not None


# ══════════════════════════════════════════════
# 入口密码解密
# ══════════════════════════════════════════════


class TestDecryptEntryPassword:
    @pytest.mark.asyncio
    async def test_uses_stored_encrypted_password(self):
        host = _host("hop", entry_password_encrypted="enc-blob")
        orch = ConnectionOrchestrator(_pty())

        with patch("src.utils.security.decrypt_password", return_value="plain"):
            assert await orch._decrypt_entry_password(host) == "plain"

    @pytest.mark.asyncio
    async def test_decrypt_failure_returns_none(self):
        host = _host("hop", entry_password_encrypted="corrupt")
        orch = ConnectionOrchestrator(_pty())

        with patch("src.utils.security.decrypt_password", side_effect=ValueError("bad key")):
            assert await orch._decrypt_entry_password(host) is None

    @pytest.mark.asyncio
    async def test_no_password_and_no_ref_returns_none(self):
        orch = ConnectionOrchestrator(_pty())
        assert await orch._decrypt_entry_password(_host("plain")) is None

    @pytest.mark.asyncio
    async def test_falls_back_to_credential_ref(self):
        """entry_password 为空时应回退查共享凭据表。"""
        host = _host("hop", credential_ref="shared-cred")
        orch = ConnectionOrchestrator(_pty())

        cred = SimpleNamespace(password_encrypted="enc-from-table")
        service = SimpleNamespace(get_by_name=AsyncMock(return_value=cred))

        class _Ctx:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *a):
                return False

        with patch("src.models.database.async_session_factory", lambda: _Ctx()), patch(
            "src.services.credential_service.CredentialService", lambda s: service
        ), patch("src.utils.security.decrypt_password", return_value="from-cred-table"):
            assert await orch._decrypt_entry_password(host) == "from-cred-table"

    @pytest.mark.asyncio
    async def test_missing_credential_returns_none(self):
        host = _host("hop", credential_ref="ghost-cred")
        orch = ConnectionOrchestrator(_pty())

        service = SimpleNamespace(get_by_name=AsyncMock(return_value=None))

        class _Ctx:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *a):
                return False

        with patch("src.models.database.async_session_factory", lambda: _Ctx()), patch(
            "src.services.credential_service.CredentialService", lambda s: service
        ):
            assert await orch._decrypt_entry_password(host) is None

    @pytest.mark.asyncio
    async def test_credential_lookup_error_returns_none(self):
        host = _host("hop", credential_ref="boom")
        orch = ConnectionOrchestrator(_pty())

        with patch(
            "src.models.database.async_session_factory", side_effect=RuntimeError("db down")
        ):
            assert await orch._decrypt_entry_password(host) is None


# ══════════════════════════════════════════════
# 文本发送
# ══════════════════════════════════════════════


class TestSendText:
    @pytest.mark.asyncio
    async def test_appends_carriage_return(self):
        pty = _pty()
        await ConnectionOrchestrator(pty)._send_text("ls")
        pty.send_input.assert_awaited_once_with("ls\r")

    @pytest.mark.asyncio
    async def test_preserves_existing_terminator(self):
        pty = _pty()
        await ConnectionOrchestrator(pty)._send_text("ls\n")
        pty.send_input.assert_awaited_once_with("ls\n")


# ══════════════════════════════════════════════
# 兼容别名与结果模型
# ══════════════════════════════════════════════


class TestBackwardCompatAliases:
    def test_orchestrator_alias(self):
        assert JumpOrchestrator is ConnectionOrchestrator

    def test_result_alias(self):
        assert JumpResult is ConnectionResult


class TestConnectionResult:
    def test_defaults(self):
        result = ConnectionResult(success=True, message="ok")
        assert result.actions_executed == 0
        assert result.skipped_reason is None
