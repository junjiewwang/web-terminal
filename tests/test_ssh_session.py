"""SSH 会话管理服务测试"""

import pytest

from src.services.ssh_session import CommandResult, SSHSessionManager


class TestCommandResult:
    """CommandResult 数据类测试"""

    def test_success_result(self):
        result = CommandResult(
            session_id="test-session",
            host_name="dev-server",
            command="echo hello",
            stdout="hello\n",
            stderr="",
            exit_code=0,
            duration_ms=12.5,
        )
        assert result.success is True
        assert result.exit_code == 0

    def test_failed_result(self):
        result = CommandResult(
            session_id="test-session",
            host_name="dev-server",
            command="false",
            stdout="",
            stderr="command failed",
            exit_code=1,
            duration_ms=5.0,
        )
        assert result.success is False


class TestSSHSessionManager:
    """SSHSessionManager 测试"""

    def test_list_sessions_empty(self):
        manager = SSHSessionManager()
        sessions = manager.list_sessions()
        assert sessions == []

    def test_get_session_info_not_found(self):
        manager = SSHSessionManager()
        info = manager.get_session_info("nonexistent")
        assert info is None
