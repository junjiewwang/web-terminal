"""tmux 窗口管理器测试

TmuxWindowManager 的核心逻辑测试，通过 mock asyncio.create_subprocess_exec
避免依赖真实 tmux 进程。
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.tmux_manager import TmuxWindow, TmuxWindowManager


class TestTmuxWindowManagerSessionName:
    """session_name_for 静态方法测试"""

    def test_generates_prefixed_name(self):
        assert TmuxWindowManager.session_name_for("my-bastion") == "wetty-my-bastion"

    def test_handles_special_characters(self):
        assert TmuxWindowManager.session_name_for("tce-server") == "wetty-tce-server"


class TestTmuxWindowManagerSessionExists:
    """session_exists 方法测试"""

    @pytest.mark.asyncio
    async def test_returns_true_when_session_exists(self):
        mgr = TmuxWindowManager()
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=None)
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await mgr.session_exists("wetty-my-bastion")

        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_session_missing(self):
        mgr = TmuxWindowManager()
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=None)
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await mgr.session_exists("wetty-nonexistent")

        assert result is False


class TestTmuxWindowManagerListWindows:
    """list_windows 方法测试"""

    @pytest.mark.asyncio
    async def test_parses_tmux_output(self):
        mgr = TmuxWindowManager()
        mock_proc = AsyncMock()
        # tmux list-windows -F "#{window_index}:#{window_name}:#{window_active}"
        mock_proc.communicate = AsyncMock(return_value=(
            b"0:bash:0\n1:m12:1\n2:m15:0\n",
            b"",
        ))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            windows = await mgr.list_windows("wetty-my-bastion")

        assert len(windows) == 3
        assert windows[0] == TmuxWindow(
            session_name="wetty-my-bastion",
            window_name="bash",
            window_index=0,
            active=False,
        )
        assert windows[1].window_name == "m12"
        assert windows[1].active is True
        assert windows[2].window_name == "m15"
        assert windows[2].active is False

    @pytest.mark.asyncio
    async def test_returns_empty_on_error(self):
        mgr = TmuxWindowManager()
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"no server running"))
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            windows = await mgr.list_windows("wetty-nonexistent")

        assert windows == []


class TestTmuxWindowManagerCreateWindow:
    """create_window 方法测试"""

    @pytest.mark.asyncio
    async def test_creates_new_window(self):
        mgr = TmuxWindowManager()

        # Mock list_windows: 返回空（窗口不存在）
        # Mock create subprocess: 成功
        list_proc = AsyncMock()
        list_proc.communicate = AsyncMock(return_value=(b"0:bash:1\n", b""))
        list_proc.returncode = 0

        create_proc = AsyncMock()
        create_proc.communicate = AsyncMock(return_value=(b"", b""))
        create_proc.returncode = 0

        call_count = 0

        async def mock_subprocess(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return list_proc  # list_windows 调用
            return create_proc  # new-window 调用

        with patch("asyncio.create_subprocess_exec", side_effect=mock_subprocess):
            result = await mgr.create_window("wetty-my-bastion", "m12")

        assert result is True

    @pytest.mark.asyncio
    async def test_returns_true_if_window_exists(self):
        """幂等性：窗口已存在时直接返回 True"""
        mgr = TmuxWindowManager()

        list_proc = AsyncMock()
        list_proc.communicate = AsyncMock(return_value=(b"0:bash:0\n1:m12:1\n", b""))
        list_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=list_proc):
            result = await mgr.create_window("wetty-my-bastion", "m12")

        assert result is True


class TestTmuxWindowManagerSelectWindow:
    """select_window 方法测试"""

    @pytest.mark.asyncio
    async def test_selects_window_successfully(self):
        mgr = TmuxWindowManager()
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await mgr.select_window("wetty-my-bastion", "m12")

        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_invalid_window(self):
        mgr = TmuxWindowManager()
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"can't find window: m99"))
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await mgr.select_window("wetty-my-bastion", "m99")

        assert result is False


class TestTmuxWindowManagerCloseWindow:
    """close_window 方法测试"""

    @pytest.mark.asyncio
    async def test_closes_window_successfully(self):
        mgr = TmuxWindowManager()
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await mgr.close_window("wetty-my-bastion", "m12")

        assert result is True


class TestTmuxWindowManagerGetActiveWindow:
    """get_active_window 方法测试"""

    @pytest.mark.asyncio
    async def test_returns_active_window_name(self):
        mgr = TmuxWindowManager()
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(
            b"0:bash:0\n1:m12:0\n2:m15:1\n",
            b"",
        ))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            active = await mgr.get_active_window("wetty-my-bastion")

        assert active == "m15"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_active(self):
        mgr = TmuxWindowManager()
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"no server running"))
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            active = await mgr.get_active_window("wetty-nonexistent")

        assert active is None
