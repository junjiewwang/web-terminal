"""多跳连接编排器测试。"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from src.models.host import AuthType, EntryType, Host, HostType
from src.services.jump_orchestrator import ConnectionOrchestrator, _resolve_variables


def _make_host(
    *,
    name: str,
    host_type: HostType,
    hostname: str = "10.0.0.1",
    port: int = 22,
    username: str = "root",
    ready_pattern: str | None = None,
    parent_id: int | None = None,
    entry: dict | None = None,
    entry_password_encrypted: str | None = None,
) -> Host:
    host = Host()
    host.id = abs(hash(name)) % 10000
    host.name = name
    host.hostname = hostname
    host.port = port
    host.username = username
    host.auth_type = AuthType.KEY
    host.host_type = host_type
    host.parent_id = parent_id
    host.ready_pattern = ready_pattern
    host.entry_spec = json.dumps(entry, ensure_ascii=False) if entry else None
    host.entry_password_encrypted = entry_password_encrypted
    host.password_encrypted = None
    host.private_key_path = None
    host.description = None
    host.tags = None
    host.children = []
    return host


def _make_root(name: str = "tce-server", ready_pattern: str = r"\[Host\]>") -> Host:
    return _make_host(
        name=name,
        host_type=HostType.ROOT,
        hostname="10.0.0.100",
        port=36000,
        username="tester",
        ready_pattern=ready_pattern,
    )


def _make_nested(
    name: str,
    *,
    parent_id: int,
    entry_type: EntryType,
    entry_value: str,
    success_pattern: str | None = r"Last login|[\$#>]\s*$",
    steps: list[dict] | None = None,
    ready_pattern: str | None = r"[\$#>]\s*$",
    entry_password_encrypted: str | None = None,
) -> Host:
    return _make_host(
        name=name,
        host_type=HostType.NESTED,
        parent_id=parent_id,
        ready_pattern=ready_pattern,
        entry={
            "type": entry_type.value,
            "value": entry_value,
            "success_pattern": success_pattern,
            "steps": steps or [],
        },
        entry_password_encrypted=entry_password_encrypted,
    )


def _mock_pty_session() -> AsyncMock:
    session = AsyncMock()
    session.send_input = AsyncMock()
    session.wait_for = AsyncMock(return_value="matched output")
    return session


class TestResolveVariables:
    def test_replaces_password_variable(self):
        text, needs_manual = _resolve_variables("{{password}}", password="s3cret")
        assert text == "s3cret"
        assert needs_manual is False

    def test_manual_variable_sets_flag(self):
        text, needs_manual = _resolve_variables("{{manual}}", password=None)
        assert text == ""
        assert needs_manual is True

    def test_unknown_variable_preserved(self):
        text, needs_manual = _resolve_variables("hello {{unknown}} world", password=None)
        assert text == "hello {{unknown}} world"
        assert needs_manual is False


class TestExecutePath:
    @pytest.mark.asyncio
    async def test_executes_menu_send_path(self):
        pty = _mock_pty_session()
        pty.wait_for = AsyncMock(side_effect=[
            "[Host]>",
            "Last login on 2026-03-30",
        ])

        root = _make_root()
        child = _make_nested(
            "tcs235",
            parent_id=root.id,
            entry_type=EntryType.MENU_SEND,
            entry_value="10.202.16.3",
        )
        orchestrator = ConnectionOrchestrator(pty)

        result = await orchestrator.execute_path(
            path=[root, child],
            tmux_session_name="wetty-tce-server--tcs235",
            window_name="0",
            skip_window_creation=True,
        )

        assert result.success is True
        pty.send_input.assert_any_call("10.202.16.3\r")

    @pytest.mark.asyncio
    async def test_executes_multi_hop_chain(self):
        pty = _mock_pty_session()
        pty.wait_for = AsyncMock(side_effect=[
            "[Host]>",
            "Last login",
            "root@tcs235:~#",
            "Last login",
        ])

        root = _make_root()
        child = _make_nested(
            "tcs235",
            parent_id=root.id,
            entry_type=EntryType.MENU_SEND,
            entry_value="10.202.16.3",
            ready_pattern=r"root@tcs235:~#",
        )
        grandchild = _make_nested(
            "tcs235-root-10.23.3.5",
            parent_id=child.id,
            entry_type=EntryType.SSH_COMMAND,
            entry_value="ssh root@10.23.3.5 -p 36000",
        )
        orchestrator = ConnectionOrchestrator(pty)

        result = await orchestrator.execute_path(
            path=[root, child, grandchild],
            tmux_session_name="wetty-tce-server--tcs235--tcs235-root-10.23.3.5",
            window_name="0",
            skip_window_creation=True,
        )

        assert result.success is True
        pty.send_input.assert_any_call("10.202.16.3\r")
        pty.send_input.assert_any_call("ssh root@10.23.3.5 -p 36000\r")

    @pytest.mark.asyncio
    async def test_executes_entry_steps(self):
        pty = _mock_pty_session()
        pty.wait_for = AsyncMock(side_effect=[
            "[Host]>",
            "password:",
            "Last login",
        ])

        root = _make_root()
        child = _make_nested(
            "ssh-hop",
            parent_id=root.id,
            entry_type=EntryType.SSH_COMMAND,
            entry_value="ssh root@10.23.3.5 -p 36000",
            steps=[{"wait": "password:", "send": "{{password}}", "timeout": 5}],
            entry_password_encrypted="encrypted-data",
        )
        orchestrator = ConnectionOrchestrator(pty)

        with patch("src.utils.security.decrypt_password", return_value="hop-secret"):
            result = await orchestrator.execute_path(
                path=[root, child],
                tmux_session_name="wetty-root--ssh-hop",
                window_name="0",
                skip_window_creation=True,
            )

        assert result.success is True
        pty.send_input.assert_any_call("ssh root@10.23.3.5 -p 36000\r")
        pty.send_input.assert_any_call("hop-secret\r")

    @pytest.mark.asyncio
    async def test_missing_entry_fails(self):
        pty = _mock_pty_session()
        pty.wait_for = AsyncMock(side_effect=["[Host]>"])

        root = _make_root()
        broken = _make_host(name="broken", host_type=HostType.NESTED, parent_id=root.id)
        orchestrator = ConnectionOrchestrator(pty)

        result = await orchestrator.execute_path(
            path=[root, broken],
            tmux_session_name="wetty-root--broken",
            window_name="0",
            skip_window_creation=True,
        )

        assert result.success is False
        assert "入口动作" in result.message

    @pytest.mark.asyncio
    async def test_manual_input_pauses_successfully(self):
        pty = _mock_pty_session()
        pty.wait_for = AsyncMock(side_effect=[
            "[Host]>",
            "MFA token:",
        ])

        root = _make_root()
        child = _make_nested(
            "mfa-hop",
            parent_id=root.id,
            entry_type=EntryType.SSH_COMMAND,
            entry_value="ssh root@10.23.3.5 -p 36000",
            steps=[{"wait": "MFA token", "send": "{{manual}}", "timeout": 10}],
        )
        orchestrator = ConnectionOrchestrator(pty)

        result = await orchestrator.execute_path(
            path=[root, child],
            tmux_session_name="wetty-root--mfa-hop",
            window_name="0",
            skip_window_creation=True,
        )

        assert result.success is True
        assert result.skipped_reason == "manual_input_required"

    @pytest.mark.asyncio
    async def test_step_timeout_fails(self):
        pty = _mock_pty_session()
        pty.wait_for = AsyncMock(side_effect=[
            "[Host]>",
            TimeoutError("等待模式超时"),
        ])

        root = _make_root()
        child = _make_nested(
            "slow-hop",
            parent_id=root.id,
            entry_type=EntryType.SSH_COMMAND,
            entry_value="ssh root@10.23.3.5 -p 36000",
            steps=[{"wait": "password:", "send": "{{password}}", "timeout": 5}],
        )
        orchestrator = ConnectionOrchestrator(pty)

        result = await orchestrator.execute_path(
            path=[root, child],
            tmux_session_name="wetty-root--slow-hop",
            window_name="0",
            skip_window_creation=True,
        )

        assert result.success is False
        assert "超时" in result.message
