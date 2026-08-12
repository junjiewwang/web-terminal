"""TerminalSession / TerminalManager 深度测试。

覆盖 write 短写与 EAGAIN 重试、resize min-size 策略、scrollback、
WebSocket 客户端管理、Agent 接口（wait_for/send_command/read_screen）
以及管理器注册表操作。

不 fork 真实 PTY：通过直接设置 _fd/_running 并 mock os.write/ioctl 驱动。
"""

from __future__ import annotations

import asyncio
import errno
import os
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.terminal_backend import TerminalBackend
from src.services.terminal_manager import (
    ClientInfo,
    SessionExitReason,
    TerminalManager,
    TerminalSession,
)


def make_session(
    backend: TerminalBackend = TerminalBackend.BROKER,
    *,
    running: bool = True,
    fd: int | None = 7,
    **kw,
) -> TerminalSession:
    session = TerminalSession(
        session_id="test-session-id",
        instance_name=kw.pop("instance_name", "web-1"),
        backend=backend,
        **kw,
    )
    session._running = running
    session._fd = fd
    return session


def make_host(name: str = "web-1", **kw) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        hostname=kw.pop("hostname", f"{name}.example.com"),
        port=kw.pop("port", 22),
        username=kw.pop("username", "deploy"),
        private_key_path=kw.pop("private_key_path", None),
        password_encrypted=None,
    )


# ══════════════════════════════════════════════
# write：短写 / EAGAIN / 鼠标过滤
# ══════════════════════════════════════════════


class TestWrite:
    def test_writes_encoded_payload(self, monkeypatch):
        session = make_session(TerminalBackend.TMUX)
        written: list[bytes] = []
        monkeypatch.setattr(os, "write", lambda fd, b: (written.append(b), len(b))[1])

        session.write("hello")

        assert b"".join(written) == b"hello"

    def test_short_write_loops_until_complete(self, monkeypatch):
        """os.write 每次只消费 1 字节时，必须循环直到写完。"""
        session = make_session(TerminalBackend.TMUX)
        written: list[bytes] = []

        def one_byte_at_a_time(fd, buf):
            written.append(buf[:1])
            return 1

        monkeypatch.setattr(os, "write", one_byte_at_a_time)

        session.write("abcde")

        assert b"".join(written) == b"abcde"

    def test_eagain_retries_after_select(self, monkeypatch):
        """PTY 缓冲区满（EAGAIN）时应等待可写并重试，而不是丢数据。"""
        session = make_session(TerminalBackend.TMUX)
        calls = {"n": 0}
        written: list[bytes] = []

        def flaky_write(fd, buf):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError(errno.EAGAIN, "temporarily unavailable")
            written.append(buf)
            return len(buf)

        monkeypatch.setattr(os, "write", flaky_write)
        monkeypatch.setattr(
            "src.services.terminal_manager.select.select", lambda r, w, x, t: ([], [7], [])
        )

        session.write("payload")

        assert b"".join(written) == b"payload", "EAGAIN 后数据必须重试写入，不能丢弃"

    def test_non_eagain_oserror_aborts(self, monkeypatch):
        session = make_session(TerminalBackend.TMUX)
        monkeypatch.setattr(
            os, "write", MagicMock(side_effect=OSError(errno.EIO, "io error"))
        )

        session.write("data")  # 不应抛出

    def test_zero_write_breaks_loop(self, monkeypatch):
        session = make_session(TerminalBackend.TMUX)
        monkeypatch.setattr(os, "write", lambda fd, b: 0)

        session.write("data")  # 不应死循环

    def test_noop_when_not_running(self, monkeypatch):
        session = make_session(TerminalBackend.TMUX, running=False)
        monkeypatch.setattr(os, "write", MagicMock(side_effect=AssertionError("不应写入")))

        session.write("data")

    def test_noop_when_fd_missing(self, monkeypatch):
        session = make_session(TerminalBackend.TMUX, fd=None)
        monkeypatch.setattr(os, "write", MagicMock(side_effect=AssertionError("不应写入")))

        session.write("data")


class TestMouseEventFiltering:
    def test_mouse_events_filtered_when_tracking_disabled(self, monkeypatch):
        """普通 shell 下鼠标序列应被过滤，交给 xterm.js 本地滚动。"""
        session = make_session(TerminalBackend.BROKER)
        written: list[bytes] = []
        monkeypatch.setattr(os, "write", lambda fd, b: (written.append(b), len(b))[1])

        session.write("\x1b[<64;10;5M")

        assert written == [], "鼠标事件应被完全过滤"

    def test_mouse_events_passed_when_tracking_enabled(self, monkeypatch):
        """vim/htop 启用鼠标追踪后，事件应直通远端。"""
        session = make_session(TerminalBackend.BROKER)
        session._vterm.feed_only("\x1b[?1000h")
        written: list[bytes] = []
        monkeypatch.setattr(os, "write", lambda fd, b: (written.append(b), len(b))[1])

        session.write("\x1b[<64;10;5M")

        assert b"".join(written) == b"\x1b[<64;10;5M"

    def test_normal_text_unaffected_by_filter(self, monkeypatch):
        session = make_session(TerminalBackend.BROKER)
        written: list[bytes] = []
        monkeypatch.setattr(os, "write", lambda fd, b: (written.append(b), len(b))[1])

        session.write("ls -la\r")

        assert b"".join(written) == b"ls -la\r"


# ══════════════════════════════════════════════
# resize / min-size
# ══════════════════════════════════════════════


class TestResize:
    def test_tmux_mode_writes_requested_size(self, monkeypatch):
        session = make_session(TerminalBackend.TMUX)
        sizes: list[tuple[int, int]] = []
        monkeypatch.setattr(session, "_set_pty_size", lambda c, r: sizes.append((c, r)))

        session.resize(120, 40)

        assert sizes == [(120, 40)]

    def test_broker_without_client_id_writes_directly(self, monkeypatch):
        session = make_session(TerminalBackend.BROKER)
        sizes: list[tuple[int, int]] = []
        monkeypatch.setattr(session, "_set_pty_size", lambda c, r: sizes.append((c, r)))

        session.resize(120, 40)

        assert sizes == [(120, 40)]

    @pytest.mark.asyncio
    async def test_broker_uses_min_across_clients(self, monkeypatch):
        """多客户端时 PTY 取各客户端的最小尺寸，避免大窗口渲染越界。

        用 async 是因为 _broadcast_resize_hint 内部 asyncio.create_task，
        需要运行中的事件循环（生产环境由 WebSocket handler 提供）。
        """
        session = make_session(TerminalBackend.BROKER)
        c1 = session.add_ws_client(AsyncMock(), cols=200, rows=50)
        session.add_ws_client(AsyncMock(), cols=80, rows=24)

        sizes: list[tuple[int, int]] = []
        monkeypatch.setattr(session, "_set_pty_size", lambda c, r: sizes.append((c, r)))

        session.resize(200, 50, client_id=c1)

        assert sizes[-1] == (80, 24)

    @pytest.mark.asyncio
    async def test_broker_updates_client_record(self):
        session = make_session(TerminalBackend.BROKER)
        cid = session.add_ws_client(AsyncMock(), cols=80, rows=24)

        session.resize(100, 30, client_id=cid)

        assert (session._ws_clients[cid].cols, session._ws_clients[cid].rows) == (100, 30)

    @pytest.mark.asyncio
    async def test_vterm_resized_alongside_pty(self, monkeypatch):
        session = make_session(TerminalBackend.BROKER)
        cid = session.add_ws_client(AsyncMock(), cols=80, rows=24)
        monkeypatch.setattr(session, "_set_pty_size", lambda c, r: None)

        session.resize(100, 30, client_id=cid)

        assert (session._vterm.cols, session._vterm.rows) == (100, 30)

    def test_noop_when_not_running(self, monkeypatch):
        session = make_session(TerminalBackend.TMUX, running=False)
        monkeypatch.setattr(
            session, "_set_pty_size", MagicMock(side_effect=AssertionError("不应调用"))
        )

        session.resize(100, 30)

    @pytest.mark.asyncio
    async def test_unknown_client_id_still_applies_min_size(self, monkeypatch):
        session = make_session(TerminalBackend.BROKER)
        session.add_ws_client(AsyncMock(), cols=90, rows=25)
        sizes: list[tuple[int, int]] = []
        monkeypatch.setattr(session, "_set_pty_size", lambda c, r: sizes.append((c, r)))

        session.resize(200, 60, client_id="ghost")

        assert sizes[-1] == (90, 25)

    @pytest.mark.asyncio
    async def test_broadcasts_resize_hint_to_clients(self):
        """min-size 生效后应把有效尺寸推给所有客户端。"""
        session = make_session(TerminalBackend.BROKER, fd=None, running=False)
        ws = AsyncMock()
        session._ws_clients["c1"] = ClientInfo(ws=ws, client_id="c1", cols=80, rows=24)

        session._broadcast_resize_hint(80, 24)
        await asyncio.sleep(0)  # 让 create_task 出来的协程跑一轮

        ws.send_json.assert_awaited_once()
        assert ws.send_json.await_args.args[0]["type"] == "resize_hint"


class TestSetPtySize:
    def test_ioctl_failure_is_swallowed(self, monkeypatch):
        session = make_session(TerminalBackend.TMUX)
        monkeypatch.setattr(
            "src.services.terminal_manager.fcntl.ioctl",
            MagicMock(side_effect=OSError("bad fd")),
        )

        session._set_pty_size(80, 24)  # 不应抛出

    def test_packs_rows_before_cols(self, monkeypatch):
        """TIOCSWINSZ 的 winsize 结构是 (rows, cols, 0, 0)，顺序不能颠倒。"""
        import struct

        captured: list[bytes] = []
        monkeypatch.setattr(
            "src.services.terminal_manager.fcntl.ioctl",
            lambda fd, op, arg: captured.append(arg),
        )
        session = make_session(TerminalBackend.TMUX)

        session._set_pty_size(120, 40)

        rows, cols, _, _ = struct.unpack("HHHH", captured[0])
        assert (rows, cols) == (40, 120)


# ══════════════════════════════════════════════
# scrollback
# ══════════════════════════════════════════════


class TestScrollback:
    def test_empty_initially(self):
        assert make_session().get_scrollback() == b""

    def test_accumulates_in_order(self):
        session = make_session()
        session._append_scrollback(b"one ")
        session._append_scrollback(b"two")

        assert session.get_scrollback() == b"one two"

    def test_trims_oldest_on_overflow(self):
        session = make_session(scrollback_capacity=5)
        session._append_scrollback(b"abcdefgh")

        assert session.get_scrollback() == b"defgh"

    def test_exact_capacity_not_trimmed(self):
        session = make_session(scrollback_capacity=4)
        session._append_scrollback(b"abcd")

        assert session.get_scrollback() == b"abcd"

    def test_returns_immutable_copy(self):
        session = make_session()
        session._append_scrollback(b"data")

        snapshot = session.get_scrollback()
        session._append_scrollback(b"more")

        assert snapshot == b"data", "已取出的快照不应随后续写入变化"


# ══════════════════════════════════════════════
# WebSocket 客户端管理
# ══════════════════════════════════════════════


class TestWsClients:
    def test_add_returns_unique_ids(self):
        session = make_session(TerminalBackend.TMUX)
        ids = {session.add_ws_client(AsyncMock()) for _ in range(3)}

        assert len(ids) == 3
        assert len(session._ws_clients) == 3

    def test_add_records_dimensions(self):
        session = make_session(TerminalBackend.TMUX)
        cid = session.add_ws_client(AsyncMock(), cols=120, rows=40)

        assert (session._ws_clients[cid].cols, session._ws_clients[cid].rows) == (120, 40)

    def test_remove_by_id(self):
        session = make_session(TerminalBackend.TMUX)
        cid = session.add_ws_client(AsyncMock())

        session.remove_ws_client(cid)

        assert session._ws_clients == {}

    def test_remove_unknown_id_is_noop(self):
        session = make_session(TerminalBackend.TMUX)
        session.add_ws_client(AsyncMock())

        session.remove_ws_client("ghost")

        assert len(session._ws_clients) == 1

    def test_remove_by_ws_instance(self):
        session = make_session(TerminalBackend.TMUX)
        ws = AsyncMock()
        session.add_ws_client(ws)
        session.add_ws_client(AsyncMock())

        session.remove_ws_client_by_ws(ws)

        assert len(session._ws_clients) == 1

    def test_remove_by_unknown_ws_is_noop(self):
        session = make_session(TerminalBackend.TMUX)
        session.add_ws_client(AsyncMock())

        session.remove_ws_client_by_ws(AsyncMock())

        assert len(session._ws_clients) == 1

    def test_info_reports_client_count(self):
        session = make_session(TerminalBackend.TMUX)
        session.add_ws_client(AsyncMock())
        session.add_ws_client(AsyncMock())

        assert session.info.ws_clients == 2


class TestSendToClients:
    @pytest.mark.asyncio
    async def test_broadcasts_to_all(self):
        session = make_session(TerminalBackend.TMUX)
        a, b = AsyncMock(), AsyncMock()
        session.add_ws_client(a)
        session.add_ws_client(b)

        await session.send_to_clients({"type": "ping"})

        a.send_json.assert_awaited_once_with({"type": "ping"})
        b.send_json.assert_awaited_once_with({"type": "ping"})

    @pytest.mark.asyncio
    async def test_drops_dead_clients(self):
        """发送失败的客户端应被摘除，避免持续向死连接写入。"""
        session = make_session(TerminalBackend.TMUX)
        good = AsyncMock()
        dead = AsyncMock()
        dead.send_json = AsyncMock(side_effect=RuntimeError("closed"))
        session.add_ws_client(good)
        session.add_ws_client(dead)

        await session.send_to_clients({"type": "ping"})

        assert len(session._ws_clients) == 1

    @pytest.mark.asyncio
    async def test_no_clients_is_noop(self):
        await make_session().send_to_clients({"type": "ping"})


class TestClientInfoDefaults:
    def test_defaults_and_timestamp(self):
        client = ClientInfo(ws=AsyncMock(), client_id="c1")
        assert (client.cols, client.rows) == (80, 24)
        assert client.connected_at is not None


# ══════════════════════════════════════════════
# 静默开关 / 退出回调
# ══════════════════════════════════════════════


class TestMute:
    def test_toggles_flag(self):
        session = make_session()
        assert session._ws_muted is False

        session.set_ws_muted(True)
        assert session._ws_muted is True

        session.set_ws_muted(False)
        assert session._ws_muted is False


class TestExitCallbacks:
    def test_multiple_callbacks_all_fire(self):
        session = make_session()
        seen: list[str] = []
        session.add_on_exit(lambda sid, reason, code: seen.append("a"))
        session.add_on_exit(lambda sid, reason, code: seen.append("b"))

        session._exit_reason = SessionExitReason.NORMAL
        session._exit_code = 0
        session._fire_on_exit()

        assert seen == ["a", "b"]

    def test_failing_callback_does_not_block_others(self):
        session = make_session()
        seen: list[str] = []

        def boom(sid, reason, code):
            raise RuntimeError("callback failed")

        session.add_on_exit(boom)
        session.add_on_exit(lambda sid, reason, code: seen.append("survived"))

        session._exit_reason = SessionExitReason.NORMAL
        session._exit_code = 0
        session._fire_on_exit()

        assert seen == ["survived"]


class TestClassifyExit:
    def test_normal_exit(self):
        assert TerminalSession._classify_exit(0) == (SessionExitReason.NORMAL, 0)

    def test_ssh_failure_255(self):
        assert TerminalSession._classify_exit(255 << 8) == (SessionExitReason.SSH_FAILED, 255)

    def test_generic_crash(self):
        assert TerminalSession._classify_exit(1 << 8) == (SessionExitReason.CHILD_CRASHED, 1)

    def test_signal_termination_returns_negative_code(self):
        reason, code = TerminalSession._classify_exit(signal.SIGKILL)
        assert reason == SessionExitReason.CHILD_CRASHED
        assert code == -signal.SIGKILL


# ══════════════════════════════════════════════
# backend argv 构建
# ══════════════════════════════════════════════


class TestBuildBackendArgv:
    def test_broker_uses_ssh_command(self):
        session = make_session(TerminalBackend.BROKER)

        argv = session._build_backend_argv(make_host(), None)

        assert argv[:2] == ["bash", "-lc"]
        assert "ssh " in argv[2]
        assert "web-1.example.com" in argv[2]

    def test_broker_password_uses_sshpass(self):
        session = make_session(TerminalBackend.BROKER)

        argv = session._build_backend_argv(make_host(), "s3cret")

        assert argv[2].startswith("sshpass -p 's3cret'")

    def test_broker_key_path_included(self):
        session = make_session(TerminalBackend.BROKER)
        host = make_host(private_key_path="/root/.ssh/id_rsa")

        argv = session._build_backend_argv(host, None)

        assert "-i /root/.ssh/id_rsa" in argv[2]

    def test_tmux_passes_session_and_host_args(self, monkeypatch):
        session = make_session(TerminalBackend.TMUX, instance_name="bastion")
        monkeypatch.setattr(
            "src.services.terminal_manager._TMUX_SCRIPT_PATH",
            SimpleNamespace(exists=lambda: True, __str__=lambda self: "/app/scripts/t.sh"),
        )

        argv = session._build_backend_argv(make_host(), "pw")

        assert argv[0] == "bash"
        assert "wetty-bastion" in argv
        assert "web-1.example.com" in argv
        assert "22" in argv
        assert "pw" in argv

    def test_tmux_missing_script_raises(self, monkeypatch):
        session = make_session(TerminalBackend.TMUX)
        monkeypatch.setattr(
            "src.services.terminal_manager._TMUX_SCRIPT_PATH",
            SimpleNamespace(exists=lambda: False, __str__=lambda self: "/missing.sh"),
        )

        with pytest.raises(FileNotFoundError):
            session._build_backend_argv(make_host(), None)


# ══════════════════════════════════════════════
# Agent 接口
# ══════════════════════════════════════════════


class TestSendInput:
    @pytest.mark.asyncio
    async def test_raises_when_not_running(self):
        session = make_session(running=False)
        with pytest.raises(ConnectionError, match="未运行"):
            await session.send_input("ls")

    @pytest.mark.asyncio
    async def test_forwards_to_write(self, monkeypatch):
        session = make_session(TerminalBackend.TMUX)
        seen: list[str] = []
        monkeypatch.setattr(session, "write", lambda d: seen.append(d))

        await session.send_input("ls\r")

        assert seen == ["ls\r"]


class TestReadScreen:
    def test_strips_ansi(self):
        session = make_session()
        session._raw_buffer.extend(["\x1b[31mred\x1b[0m", "plain"])

        assert session.read_screen() == "red\nplain"

    def test_limits_to_requested_lines(self):
        session = make_session()
        session._raw_buffer.extend(str(i) for i in range(10))

        assert session.read_screen(lines=3) == "7\n8\n9"

    def test_empty_buffer(self):
        assert make_session().read_screen() == ""

    def test_filters_tmux_status_line(self):
        session = make_session()
        session._raw_buffer.extend(
            ["real output", '[wetty-a:b*   "h" 09:15 27-Mar-26']
        )

        assert session.read_screen() == "real output"


class TestWaitFor:
    def _feed(self, session: TerminalSession, line: str) -> None:
        session._raw_buffer.append(line)
        session._buffer_write_seq += 1
        session._output_event.set()

    @pytest.mark.asyncio
    async def test_raises_when_not_running(self):
        session = make_session(running=False)
        with pytest.raises(ConnectionError):
            await session.wait_for("x", timeout=0.1)

    @pytest.mark.asyncio
    async def test_matches_pattern_already_buffered(self):
        session = make_session()

        async def feeder():
            await asyncio.sleep(0.01)
            self._feed(session, "root@host:~#")

        asyncio.create_task(feeder())
        result = await session.wait_for(r"[\$#>]\s*$", timeout=3)

        assert "root@host" in result

    @pytest.mark.asyncio
    async def test_times_out_without_match(self):
        session = make_session()

        with pytest.raises(TimeoutError, match="超时"):
            await session.wait_for("never-appears", timeout=0.2)

    @pytest.mark.asyncio
    async def test_timeout_message_includes_recent_output(self):
        """超时信息应带上等待期间收到的输出，便于定位卡在哪一步。

        注意：wait_for 默认只扫描「调用之后」的新输出（scan_seq 从当前
        _buffer_write_seq 起算），所以这里必须在调用后再喂数据。
        """
        session = make_session()

        async def feeder():
            await asyncio.sleep(0.01)
            self._feed(session, "some noise")

        asyncio.create_task(feeder())

        with pytest.raises(TimeoutError) as exc:
            await session.wait_for("never", timeout=0.3)

        assert "some noise" in str(exc.value)

    @pytest.mark.asyncio
    async def test_only_scans_output_after_call(self):
        """调用前已在缓冲区里的内容不参与匹配（除非显式传 _start_pos）。"""
        session = make_session()
        self._feed(session, "PRE-EXISTING")

        with pytest.raises(TimeoutError):
            await session.wait_for("PRE-EXISTING", timeout=0.2)

    @pytest.mark.asyncio
    async def test_start_pos_includes_earlier_output(self):
        session = make_session()
        start = session._buffer_write_seq
        self._feed(session, "EARLIER")

        result = await session.wait_for("EARLIER", timeout=1, _start_pos=start)

        assert "EARLIER" in result

    @pytest.mark.asyncio
    async def test_invalid_regex_falls_back_to_literal(self):
        """非法正则应退化为字面量匹配，而不是抛 re.error。"""
        session = make_session()

        async def feeder():
            await asyncio.sleep(0.01)
            self._feed(session, "cost is [unclosed")

        asyncio.create_task(feeder())
        result = await session.wait_for("[unclosed", timeout=3)

        assert "unclosed" in result

    @pytest.mark.asyncio
    async def test_ignores_tmux_status_lines(self):
        session = make_session()

        async def feeder():
            await asyncio.sleep(0.01)
            self._feed(session, '[wetty-a:b*   "h" 09:15 27-Mar-26')
            await asyncio.sleep(0.01)
            self._feed(session, "actual-prompt$")

        asyncio.create_task(feeder())
        result = await session.wait_for(r"\$", timeout=3)

        assert "wetty-a" not in result

    @pytest.mark.asyncio
    async def test_survives_buffer_overflow(self):
        """deque 满后 seq 映射仍需正确（Bugfix #21d 回归）。"""
        session = make_session()
        start = session._buffer_write_seq

        for i in range(session.MAX_BUFFER_LINES + 50):
            self._feed(session, f"line-{i}")
        self._feed(session, "FINAL-MARKER")

        result = await session.wait_for("FINAL-MARKER", timeout=3, _start_pos=start)

        assert "FINAL-MARKER" in result


class TestSendCommand:
    @pytest.mark.asyncio
    async def test_appends_carriage_return(self, monkeypatch):
        session = make_session()
        sent: list[str] = []
        monkeypatch.setattr(session, "send_input", AsyncMock(side_effect=lambda t: sent.append(t)))
        monkeypatch.setattr(session, "wait_for", AsyncMock(return_value="done"))

        await session.send_command("ls")

        assert sent == ["ls\r"]

    @pytest.mark.asyncio
    async def test_does_not_double_terminate(self, monkeypatch):
        session = make_session()
        sent: list[str] = []
        monkeypatch.setattr(session, "send_input", AsyncMock(side_effect=lambda t: sent.append(t)))
        monkeypatch.setattr(session, "wait_for", AsyncMock(return_value="done"))

        await session.send_command("ls\n")

        assert sent == ["ls\n"]


# ══════════════════════════════════════════════
# TerminalManager 注册表
# ══════════════════════════════════════════════


class TestManagerRegistry:
    @pytest.fixture
    def mgr_with_session(self):
        mgr = TerminalManager(default_backend=TerminalBackend.BROKER)
        session = make_session(TerminalBackend.BROKER)
        mgr._sessions["web-1"] = session
        return mgr, session

    def test_get_session_returns_running(self, mgr_with_session):
        mgr, session = mgr_with_session
        assert mgr.get_session("web-1") is session

    def test_get_session_hides_stopped(self, mgr_with_session):
        mgr, session = mgr_with_session
        session._running = False
        assert mgr.get_session("web-1") is None

    def test_get_session_unknown(self, mgr_with_session):
        mgr, _ = mgr_with_session
        assert mgr.get_session("ghost") is None

    def test_get_session_by_id(self, mgr_with_session):
        mgr, session = mgr_with_session
        assert mgr.get_session_by_id("test-session-id") is session
        assert mgr.get_session_by_id("nope") is None

    def test_has_running_session(self, mgr_with_session):
        mgr, session = mgr_with_session
        assert mgr.has_running_session("web-1") is True

        session._running = False
        assert mgr.has_running_session("web-1") is False

    def test_list_sessions_returns_info(self, mgr_with_session):
        mgr, _ = mgr_with_session
        infos = mgr.list_sessions()
        assert len(infos) == 1
        assert infos[0].instance_name == "web-1"

    @pytest.mark.asyncio
    async def test_stop_session_by_id(self, mgr_with_session, monkeypatch):
        mgr, session = mgr_with_session
        monkeypatch.setattr(TerminalSession, "stop", AsyncMock())

        assert await mgr.stop_session_by_id("test-session-id") is True
        assert mgr._sessions == {}

    @pytest.mark.asyncio
    async def test_stop_session_by_unknown_id(self, mgr_with_session):
        mgr, _ = mgr_with_session
        assert await mgr.stop_session_by_id("ghost") is False


class TestDefaultBackendProperty:
    def test_getter_and_setter(self):
        mgr = TerminalManager(default_backend=TerminalBackend.TMUX)
        assert mgr.default_backend is TerminalBackend.TMUX

        mgr.default_backend = TerminalBackend.BROKER
        assert mgr.default_backend is TerminalBackend.BROKER


class TestSwitchBackend:
    @pytest.mark.asyncio
    async def test_stops_all_and_returns_names(self, monkeypatch):
        mgr = TerminalManager(default_backend=TerminalBackend.TMUX)
        for name in ("a", "b"):
            mgr._sessions[name] = make_session(TerminalBackend.TMUX, instance_name=name)
        monkeypatch.setattr(TerminalSession, "stop", AsyncMock())

        stopped = await mgr.switch_backend(TerminalBackend.BROKER)

        assert sorted(stopped) == ["a", "b"]
        assert mgr.default_backend is TerminalBackend.BROKER
        assert mgr._sessions == {}

    @pytest.mark.asyncio
    async def test_no_sessions_returns_empty(self):
        mgr = TerminalManager(default_backend=TerminalBackend.TMUX)

        assert await mgr.switch_backend(TerminalBackend.BROKER) == []
        assert mgr.default_backend is TerminalBackend.BROKER


class TestCleanupZombieSessions:
    @pytest.mark.asyncio
    async def test_returns_zero_when_tmux_absent(self, monkeypatch):
        async def raise_missing(*a, **k):
            raise FileNotFoundError("tmux not installed")

        monkeypatch.setattr(
            "src.services.terminal_manager.asyncio.create_subprocess_exec", raise_missing
        )
        mgr = TerminalManager(default_backend=TerminalBackend.BROKER)

        assert await mgr.cleanup_zombie_sessions() == 0

    @pytest.mark.asyncio
    async def test_returns_zero_on_tmux_error(self, monkeypatch):
        async def fake_exec(*a, **k):
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.returncode = 1
            return proc

        monkeypatch.setattr(
            "src.services.terminal_manager.asyncio.create_subprocess_exec", fake_exec
        )
        mgr = TerminalManager(default_backend=TerminalBackend.BROKER)

        assert await mgr.cleanup_zombie_sessions() == 0
