"""MCP Server 工具层测试。

覆盖危险命令拦截、会话守卫、错误映射、事件发布、
tmux 窗口工具与下载 URL 推导。

MCP 工具经 @mcp.tool() 装饰后仍是普通协程函数，可直接 await 调用。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.mcp_server import server as mcp
from src.services.event_service import EventBus
from src.services.tmux_manager import TmuxWindow


def make_session(
    session_id: str = "s1",
    *,
    running: bool = True,
    instance_name: str = "web-1",
    screen: str = "output",
) -> SimpleNamespace:
    return SimpleNamespace(
        session_id=session_id,
        instance_name=instance_name,
        running=running,
        send_command=AsyncMock(return_value="cmd-output"),
        send_input=AsyncMock(),
        wait_for=AsyncMock(return_value="matched-output"),
        read_screen=lambda lines=50: screen,
    )


@pytest.fixture
def session():
    return make_session()


@pytest.fixture
def wired(session, monkeypatch):
    """注入 terminal manager + 干净事件总线，返回 (session, bus, manager)。"""
    bus = EventBus()
    mgr = SimpleNamespace(
        get_session_by_id=lambda sid: session if sid == "s1" else None,
        stop_session=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(mcp, "_terminal_manager", mgr)
    monkeypatch.setattr(mcp, "event_bus", bus)
    return session, bus, mgr


# ══════════════════════════════════════════════
# 危险命令拦截（安全关键）
# ══════════════════════════════════════════════


class TestValidateCommand:
    SAFE = [
        "ls -la",
        "df -h",
        "rm -rf /tmp/build",          # 有具体路径，不是根目录
        "dd if=/dev/zero of=/tmp/f",  # 写文件不是写设备
        "cat /etc/hosts",
        "systemctl restart nginx",     # 重启服务不是重启机器
        "echo shutdown",               # 只是打印这个词
    ]

    BLOCKED = [
        ("rm -rf /", "rm -rf /"),
        ("rm -r /", "rm -r /"),
        ("mkfs.ext4 /dev/sda1", "mkfs"),
        ("dd if=/dev/zero of=/dev/sda", "dd of=/dev/"),
        ("echo x > /dev/sda", "redirect to disk"),
        ("shutdown -h now", "shutdown"),
        ("reboot", "reboot"),
        ("init 0", "init 0"),
        ("halt", "halt"),
        (":(){ :|:& };:", "fork bomb"),
    ]

    @pytest.mark.parametrize("cmd", SAFE)
    def test_safe_commands_allowed(self, cmd):
        assert mcp._validate_command(cmd) is None, f"误拦截安全命令: {cmd}"

    @pytest.mark.parametrize("cmd,label", BLOCKED, ids=[b[1] for b in BLOCKED])
    def test_dangerous_commands_blocked(self, cmd, label):
        reason = mcp._validate_command(cmd)
        assert reason is not None, f"未拦截危险命令: {cmd}"
        assert "危险命令被拦截" in reason

    def test_leading_whitespace_does_not_bypass(self):
        """前置空格不能绕过黑名单。"""
        assert mcp._validate_command("   shutdown -h now") is not None

    def test_empty_command_allowed(self):
        assert mcp._validate_command("") is None


# ══════════════════════════════════════════════
# run_command
# ══════════════════════════════════════════════


class TestRunCommand:
    @pytest.mark.asyncio
    async def test_returns_output(self, wired):
        result = await mcp.run_command("s1", "ls -la")
        assert result == "cmd-output"

    @pytest.mark.asyncio
    async def test_dangerous_command_rejected_before_session_lookup(self, wired):
        session, _bus, _mgr = wired

        result = await mcp.run_command("s1", "rm -rf /")

        assert "危险命令被拦截" in result
        session.send_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_session(self, wired):
        result = await mcp.run_command("ghost", "ls")
        assert "不存在" in result

    @pytest.mark.asyncio
    async def test_stopped_session(self, monkeypatch):
        stopped = make_session(running=False)
        monkeypatch.setattr(
            mcp, "_terminal_manager", SimpleNamespace(get_session_by_id=lambda s: stopped)
        )
        monkeypatch.setattr(mcp, "event_bus", EventBus())

        result = await mcp.run_command("s1", "ls")

        assert "已断开" in result

    @pytest.mark.asyncio
    async def test_timeout_returns_error_string(self, wired):
        session, _bus, _mgr = wired
        session.send_command = AsyncMock(side_effect=TimeoutError("等待超时"))

        result = await mcp.run_command("s1", "sleep 99", timeout=1)

        assert result.startswith("错误：")

    @pytest.mark.asyncio
    async def test_connection_error_returns_error_string(self, wired):
        session, _bus, _mgr = wired
        session.send_command = AsyncMock(side_effect=ConnectionError("broken"))

        result = await mcp.run_command("s1", "ls")

        assert "连接异常" in result

    @pytest.mark.asyncio
    async def test_publishes_start_and_complete_events(self, wired):
        _session, bus, _mgr = wired

        await mcp.run_command("s1", "ls")

        assert [e.event_type.value for e in bus.history] == [
            "command_start",
            "command_complete",
        ]

    @pytest.mark.asyncio
    async def test_timeout_publishes_error_event(self, wired):
        session, bus, _mgr = wired
        session.send_command = AsyncMock(side_effect=TimeoutError("超时"))

        await mcp.run_command("s1", "sleep 99")

        assert [e.event_type.value for e in bus.history] == [
            "command_start",
            "command_error",
        ]

    @pytest.mark.asyncio
    async def test_timeout_forwarded_as_float(self, wired):
        session, _bus, _mgr = wired

        await mcp.run_command("s1", "ls", timeout=7)

        assert session.send_command.await_args.kwargs["timeout"] == 7.0


# ══════════════════════════════════════════════
# send_input / wait_for_output / read_terminal
# ══════════════════════════════════════════════


class TestSendInput:
    @pytest.mark.asyncio
    async def test_confirms_sent_text(self, wired):
        result = await mcp.send_input("s1", "yes\n")
        assert "已发送" in result

    @pytest.mark.asyncio
    async def test_unknown_session(self, wired):
        assert "不存在" in await mcp.send_input("ghost", "x")

    @pytest.mark.asyncio
    async def test_connection_error_reported(self, wired):
        session, _bus, _mgr = wired
        session.send_input = AsyncMock(side_effect=ConnectionError("gone"))

        assert "发送失败" in await mcp.send_input("s1", "x")

    @pytest.mark.asyncio
    async def test_publishes_input_event(self, wired):
        _session, bus, _mgr = wired

        await mcp.send_input("s1", "secret-ip\n")

        assert bus.history[0].data["command"].startswith("[input]")


class TestWaitForOutput:
    @pytest.mark.asyncio
    async def test_returns_matched_output(self, wired):
        assert await mcp.wait_for_output("s1", r"\$") == "matched-output"

    @pytest.mark.asyncio
    async def test_unknown_session(self, wired):
        assert "不存在" in await mcp.wait_for_output("ghost", "x")

    @pytest.mark.asyncio
    async def test_timeout_prefixed(self, wired):
        session, _bus, _mgr = wired
        session.wait_for = AsyncMock(side_effect=TimeoutError("no match"))

        assert (await mcp.wait_for_output("s1", "nope")).startswith("超时：")

    @pytest.mark.asyncio
    async def test_connection_error_reported(self, wired):
        session, _bus, _mgr = wired
        session.wait_for = AsyncMock(side_effect=ConnectionError("dead"))

        assert "连接异常" in await mcp.wait_for_output("s1", "x")


class TestReadTerminal:
    @pytest.mark.asyncio
    async def test_returns_screen(self, wired):
        assert await mcp.read_terminal("s1") == "output"

    @pytest.mark.asyncio
    async def test_blank_screen_gives_hint(self, monkeypatch):
        blank = make_session(screen="   \n  ")
        monkeypatch.setattr(
            mcp, "_terminal_manager", SimpleNamespace(get_session_by_id=lambda s: blank)
        )

        assert "终端屏幕为空" in await mcp.read_terminal("s1")

    @pytest.mark.asyncio
    async def test_unknown_session(self, wired):
        assert "不存在" in await mcp.read_terminal("ghost")


# ══════════════════════════════════════════════
# disconnect
# ══════════════════════════════════════════════


class TestDisconnect:
    @pytest.mark.asyncio
    async def test_closes_and_reports(self, wired):
        result = await mcp.disconnect("s1")
        assert "已断开" in result

    @pytest.mark.asyncio
    async def test_unknown_session(self, wired):
        assert "会话不存在" in await mcp.disconnect("ghost")

    @pytest.mark.asyncio
    async def test_publishes_closed_event(self, wired):
        _session, bus, _mgr = wired

        await mcp.disconnect("s1")

        assert [e.event_type.value for e in bus.history] == ["session_closed"]

    @pytest.mark.asyncio
    async def test_stop_failure_reported(self, wired):
        _session, _bus, manager = wired
        manager.stop_session = AsyncMock(return_value=False)

        assert "断开失败" in await mcp.disconnect("s1")


# ══════════════════════════════════════════════
# tmux 窗口工具
# ══════════════════════════════════════════════


@pytest.fixture
def tmux(monkeypatch):
    """注入 tmux 管理器 + 干净事件总线，返回 (manager, bus)。"""
    mgr = SimpleNamespace(
        session_exists=AsyncMock(return_value=True),
        list_windows=AsyncMock(
            return_value=[
                TmuxWindow(session_name="wetty-b", window_name="bash", window_index=0, active=False),
                TmuxWindow(session_name="wetty-b", window_name="m12", window_index=1, active=True),
            ]
        ),
        select_window=AsyncMock(return_value=True),
    )
    bus = EventBus()
    monkeypatch.setattr(mcp, "_tmux_manager", mgr)
    monkeypatch.setattr(mcp, "event_bus", bus)
    return mgr, bus


class TestListWindows:
    @pytest.mark.asyncio
    async def test_returns_json_array(self, tmux):
        parsed = json.loads(await mcp.list_windows("b"))

        assert parsed == [
            {"index": 0, "name": "bash", "active": False},
            {"index": 1, "name": "m12", "active": True},
        ]

    @pytest.mark.asyncio
    async def test_missing_session(self, tmux):
        mgr, _bus = tmux
        mgr.session_exists = AsyncMock(return_value=False)

        assert "不存在" in await mcp.list_windows("ghost")

    @pytest.mark.asyncio
    async def test_no_windows(self, tmux):
        mgr, _bus = tmux
        mgr.list_windows = AsyncMock(return_value=[])

        assert "没有打开的窗口" in await mcp.list_windows("b")


class TestSwitchWindow:
    @pytest.mark.asyncio
    async def test_success(self, tmux):
        assert "已切换到窗口" in await mcp.switch_window("b", "m12")

    @pytest.mark.asyncio
    async def test_missing_session(self, tmux):
        mgr, _bus = tmux
        mgr.session_exists = AsyncMock(return_value=False)

        assert "不存在" in await mcp.switch_window("ghost", "m12")

    @pytest.mark.asyncio
    async def test_failure_lists_available_windows(self, tmux):
        mgr, _bus = tmux
        mgr.select_window = AsyncMock(return_value=False)

        result = await mcp.switch_window("b", "nope")

        assert "切换窗口失败" in result
        assert "bash" in result and "m12" in result

    @pytest.mark.asyncio
    async def test_publishes_switch_event(self, tmux):
        _mgr, bus = tmux

        await mcp.switch_window("b", "m12")

        assert [e.event_type.value for e in bus.history] == ["window_switched"]
        assert bus.history[0].data["window_name"] == "m12"

    @pytest.mark.asyncio
    async def test_failed_switch_publishes_nothing(self, tmux):
        mgr, bus = tmux
        mgr.select_window = AsyncMock(return_value=False)

        await mcp.switch_window("b", "nope")

        assert bus.history == [], "切换失败不应发布成功事件"


# ══════════════════════════════════════════════
# 依赖未初始化
# ══════════════════════════════════════════════


class TestUninitialisedDependencies:
    def test_terminal_manager_raises(self, monkeypatch):
        monkeypatch.setattr(mcp, "_terminal_manager", None)
        with pytest.raises(RuntimeError, match="尚未初始化"):
            mcp._get_terminal_manager()

    def test_tmux_manager_raises(self, monkeypatch):
        monkeypatch.setattr(mcp, "_tmux_manager", None)
        with pytest.raises(RuntimeError, match="尚未初始化"):
            mcp._get_tmux_manager()

    def test_snippet_registry_raises(self, monkeypatch):
        monkeypatch.setattr(mcp, "_snippet_registry", None)
        with pytest.raises(RuntimeError, match="Snippet Registry"):
            mcp._get_snippet_registry()


class TestInitMcpServer:
    def test_wires_all_dependencies(self, monkeypatch):
        term = SimpleNamespace()
        registry = SimpleNamespace()

        mcp.init_mcp_server(term, snippet_registry=registry)

        assert mcp._terminal_manager is term
        assert mcp._snippet_registry is registry
        assert mcp._tmux_manager is not None, "未传 tmux 管理器时应自动创建"

    def test_get_pty_manager_is_legacy_noop(self):
        assert mcp.get_pty_manager() is None


# ══════════════════════════════════════════════
# 密码解密
# ══════════════════════════════════════════════


class TestDecryptHostPassword:
    def test_none_when_no_password(self):
        assert mcp._decrypt_host_password(SimpleNamespace(password_encrypted=None)) is None

    def test_decrypts(self, monkeypatch):
        from src.utils import security

        monkeypatch.setattr(security, "decrypt_password", lambda blob: "plain")
        host = SimpleNamespace(password_encrypted="enc", name="h")

        assert mcp._decrypt_host_password(host) == "plain"

    def test_failure_returns_none(self, monkeypatch):
        from src.utils import security

        def boom(blob):
            raise ValueError("bad key")

        monkeypatch.setattr(security, "decrypt_password", boom)
        host = SimpleNamespace(password_encrypted="enc", name="h")

        assert mcp._decrypt_host_password(host) is None


# ══════════════════════════════════════════════
# 下载 URL 推导
# ══════════════════════════════════════════════


class TestDownloadBaseUrl:
    def test_request_host_wins(self, monkeypatch):
        mcp.set_mcp_request_host("10.0.0.5:8000")
        try:
            assert mcp._get_download_base_url() == "http://10.0.0.5:8000"
        finally:
            mcp.set_mcp_request_host("")

    def test_env_var_used_when_no_request_host(self, monkeypatch):
        mcp.set_mcp_request_host("")
        monkeypatch.setenv("WETTY_EXTERNAL_URL", "https://term.example.com/")

        assert mcp._get_download_base_url() == "https://term.example.com"

    def test_falls_back_to_container_ip(self, monkeypatch):
        mcp.set_mcp_request_host("")
        monkeypatch.delenv("WETTY_EXTERNAL_URL", raising=False)
        monkeypatch.setattr(mcp.socket, "gethostname", lambda: "container")
        monkeypatch.setattr(mcp.socket, "gethostbyname", lambda h: "172.17.0.2")

        assert mcp._get_download_base_url() == "http://172.17.0.2:8000"

    def test_falls_back_to_localhost_on_dns_failure(self, monkeypatch):
        mcp.set_mcp_request_host("")
        monkeypatch.delenv("WETTY_EXTERNAL_URL", raising=False)

        def boom(_h):
            raise OSError("no dns")

        monkeypatch.setattr(mcp.socket, "gethostbyname", boom)

        assert mcp._get_download_base_url() == "http://localhost:8000"
