"""tmux 客户端管理与按键发送测试（补充 test_tmux_manager.py 未覆盖的方法）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.services.tmux_manager import TmuxClient, TmuxWindowManager


def _proc(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
    """构造 mock 子进程（communicate 风格）。"""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    return proc


def _wait_proc(returncode: int = 0):
    """构造 mock 子进程（wait 风格）。"""
    proc = AsyncMock()
    proc.wait = AsyncMock(return_value=None)
    proc.returncode = returncode
    return proc


# ══════════════════════════════════════════════
# list_clients
# ══════════════════════════════════════════════


class TestListClients:
    @pytest.mark.asyncio
    async def test_parses_client_lines(self):
        mgr = TmuxWindowManager()
        out = b"/dev/pts/3:m12:wetty-b\n/dev/pts/5:m15:wetty-b\n"

        with patch("asyncio.create_subprocess_exec", return_value=_proc(0, out)):
            clients = await mgr.list_clients("wetty-b")

        assert clients == [
            TmuxClient(tty="/dev/pts/3", window="m12", session="wetty-b"),
            TmuxClient(tty="/dev/pts/5", window="m15", session="wetty-b"),
        ]

    @pytest.mark.asyncio
    async def test_returns_empty_on_error(self):
        mgr = TmuxWindowManager()
        with patch("asyncio.create_subprocess_exec", return_value=_proc(1, b"", b"no server")):
            assert await mgr.list_clients("nope") == []

    @pytest.mark.asyncio
    async def test_empty_output_yields_empty_list(self):
        mgr = TmuxWindowManager()
        with patch("asyncio.create_subprocess_exec", return_value=_proc(0, b"")):
            assert await mgr.list_clients("wetty-b") == []

    @pytest.mark.asyncio
    async def test_skips_malformed_lines(self):
        mgr = TmuxWindowManager()
        out = b"/dev/pts/3:m12:wetty-b\nbroken-line\n\n"

        with patch("asyncio.create_subprocess_exec", return_value=_proc(0, out)):
            clients = await mgr.list_clients("wetty-b")

        assert len(clients) == 1


# ══════════════════════════════════════════════
# switch_client
# ══════════════════════════════════════════════


class TestSwitchClient:
    @pytest.mark.asyncio
    async def test_returns_true_on_success(self):
        mgr = TmuxWindowManager()
        with patch("asyncio.create_subprocess_exec", return_value=_proc(0)):
            assert await mgr.switch_client("/dev/pts/3", "wetty-b", "m12") is True

    @pytest.mark.asyncio
    async def test_returns_false_on_failure(self):
        mgr = TmuxWindowManager()
        with patch("asyncio.create_subprocess_exec", return_value=_proc(1, b"", b"no client")):
            assert await mgr.switch_client("/dev/pts/9", "wetty-b", "m12") is False

    @pytest.mark.asyncio
    async def test_passes_client_and_target_args(self):
        """必须带 -c <tty> 和 -t <session>:<window>，否则会切错客户端。"""
        mgr = TmuxWindowManager()
        with patch("asyncio.create_subprocess_exec", return_value=_proc(0)) as exec_mock:
            await mgr.switch_client("/dev/pts/3", "wetty-b", "m12")

        args = exec_mock.call_args.args
        assert "switch-client" in args
        assert "-c" in args and "/dev/pts/3" in args
        assert "-t" in args and "wetty-b:m12" in args


# ══════════════════════════════════════════════
# send_keys
# ══════════════════════════════════════════════


class TestSendKeys:
    @pytest.mark.asyncio
    async def test_returns_true_on_success(self):
        mgr = TmuxWindowManager()
        with patch("asyncio.create_subprocess_exec", return_value=_proc(0)):
            assert await mgr.send_keys("wetty-b", "m12", "ls -la") is True

    @pytest.mark.asyncio
    async def test_returns_false_on_failure(self):
        mgr = TmuxWindowManager()
        with patch("asyncio.create_subprocess_exec", return_value=_proc(1, b"", b"no window")):
            assert await mgr.send_keys("wetty-b", "nope", "ls") is False

    @pytest.mark.asyncio
    async def test_targets_session_and_window(self):
        mgr = TmuxWindowManager()
        with patch("asyncio.create_subprocess_exec", return_value=_proc(0)) as exec_mock:
            await mgr.send_keys("wetty-b", "m12", "whoami")

        args = exec_mock.call_args.args
        assert "send-keys" in args
        assert "wetty-b:m12" in args
        assert "whoami" in args


# ══════════════════════════════════════════════
# close_window / get_active_window
# ══════════════════════════════════════════════


class TestCloseWindow:
    @pytest.mark.asyncio
    async def test_returns_true_on_success(self):
        mgr = TmuxWindowManager()
        with patch("asyncio.create_subprocess_exec", return_value=_proc(0)):
            assert await mgr.close_window("wetty-b", "m12") is True

    @pytest.mark.asyncio
    async def test_returns_false_on_failure(self):
        mgr = TmuxWindowManager()
        with patch("asyncio.create_subprocess_exec", return_value=_proc(1, b"", b"can't find")):
            assert await mgr.close_window("wetty-b", "ghost") is False


class TestGetActiveWindow:
    @pytest.mark.asyncio
    async def test_returns_active_window_name(self):
        mgr = TmuxWindowManager()
        out = b"0:bash:0\n1:m12:1\n2:m15:0\n"

        with patch("asyncio.create_subprocess_exec", return_value=_proc(0, out)):
            assert await mgr.get_active_window("wetty-b") == "m12"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_active(self):
        mgr = TmuxWindowManager()
        out = b"0:bash:0\n1:m12:0\n"

        with patch("asyncio.create_subprocess_exec", return_value=_proc(0, out)):
            assert await mgr.get_active_window("wetty-b") is None

    @pytest.mark.asyncio
    async def test_returns_none_when_session_missing(self):
        mgr = TmuxWindowManager()
        with patch("asyncio.create_subprocess_exec", return_value=_proc(1, b"", b"no server")):
            assert await mgr.get_active_window("nope") is None


# ══════════════════════════════════════════════
# is_session_logged_in
# ══════════════════════════════════════════════


class TestIsSessionLoggedIn:
    @pytest.mark.asyncio
    async def test_false_when_session_absent(self):
        mgr = TmuxWindowManager()
        with patch("asyncio.create_subprocess_exec", return_value=_wait_proc(1)):
            assert await mgr.is_session_logged_in("ghost") is False

    @pytest.mark.asyncio
    async def test_true_when_pane_runs_ssh(self):
        mgr = TmuxWindowManager()
        # 第一次调用 has-session（wait 风格），第二次取 pane 命令（communicate 风格）
        procs = [_wait_proc(0), _proc(0, b"ssh\n")]

        with patch("asyncio.create_subprocess_exec", side_effect=procs):
            assert await mgr.is_session_logged_in("wetty-b") is True

    @pytest.mark.asyncio
    async def test_true_when_pane_runs_sshpass(self):
        mgr = TmuxWindowManager()
        procs = [_wait_proc(0), _proc(0, b"sshpass\n")]

        with patch("asyncio.create_subprocess_exec", side_effect=procs):
            assert await mgr.is_session_logged_in("wetty-b") is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("shell", [b"bash\n", b"zsh\n", b"sh\n"])
    async def test_plain_shell_also_counts_as_logged_in(self, shell):
        """当前实现把 bash/zsh/sh 也算作「已登录」。

        注意：docstring 只提到 ssh/sshpass，但实现的白名单包含普通 shell
        （src/services/tmux_manager.py:93）。此方法目前在 src/ 下无调用方，
        这里按实际行为固定住，改动实现时会被这条用例提示。
        """
        mgr = TmuxWindowManager()
        procs = [_wait_proc(0), _proc(0, shell)]

        with patch("asyncio.create_subprocess_exec", side_effect=procs):
            assert await mgr.is_session_logged_in("wetty-b") is True

    @pytest.mark.asyncio
    async def test_false_for_unrelated_command(self):
        mgr = TmuxWindowManager()
        procs = [_wait_proc(0), _proc(0, b"vim\n")]

        with patch("asyncio.create_subprocess_exec", side_effect=procs):
            assert await mgr.is_session_logged_in("wetty-b") is False

    @pytest.mark.asyncio
    async def test_false_when_list_panes_fails(self):
        mgr = TmuxWindowManager()
        procs = [_wait_proc(0), _proc(1, b"")]

        with patch("asyncio.create_subprocess_exec", side_effect=procs):
            assert await mgr.is_session_logged_in("wetty-b") is False
