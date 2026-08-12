"""snippet_registry 注入逻辑测试 — ensure_snippet_loaded / heredoc / 探测命令。

覆盖 snippet_registry.py 中未被 test_snippet_registry.py 覆盖的部分：
- get_probe_command / get_version_probe_command（探测命令生成）
- build_heredoc_loader(compressed=False) / _build_heredoc_loader（heredoc 注入）
- _extract_last_nonempty_line / _extract_version_from_output / _extract_md5_from_output
- ensure_snippet_loaded 三步探测注入逻辑（全部分支）
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from src.services.snippet_registry import (
    SnippetRegistry,
    ensure_snippet_loaded,
)


SAFE_SCRIPT = "#!/bin/bash\nes() { curl -s localhost:9200/_cat/indices; }\n"


def _write_config(tmp_path: Path, config: dict, script: str | None = SAFE_SCRIPT) -> Path:
    yaml_path = tmp_path / "snippets.yaml"
    import yaml

    yaml_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    if script is not None:
        (tmp_path / "ts-es.sh").write_text(script, encoding="utf-8")
    return yaml_path


def _basic_config(**domain_overrides) -> dict:
    domain = {
        "id": "es",
        "name": "Elasticsearch",
        "icon": "🔍",
        "description": "ES 排查",
        "script_file": "ts-es.sh",
        "tags": ["search"],
        "commands": [
            {
                "id": "health",
                "name": "集群健康",
                "template": "curl -s {{host}}/_cluster/health",
                "params": [{"name": "host", "required": True, "description": "ES 地址"}],
            }
        ],
    }
    domain.update(domain_overrides)
    return {"domains": [domain]}


# ── FakeSession ────────────────────────────────────────────────────────────
# 通过路由表根据命令子串返回不同输出，精确控制 ensure_snippet_loaded 的分支走向。


class FakeSession:
    def __init__(
        self,
        *,
        routes: dict[str, str] | None = None,
        inject_confirm: bool = True,
        post_probe_confirm: bool = True,
    ):
        self._routes = routes or {}
        self._inject_confirm = inject_confirm
        self._post_probe_confirm = post_probe_confirm
        self.muted_states: list[bool] = []
        self.sent_inputs: list[str] = []
        self._buffer_write_seq = 0
        self.wait_for_calls: list[dict] = []
        self.command_calls: list[str] = []

    async def send_command(self, command: str, wait_pattern=None, timeout=None) -> str:
        self.command_calls.append(command)
        for needle, resp in self._routes.items():
            if needle in command:
                return resp
        return ""

    async def send_input(self, data: str) -> None:
        self.sent_inputs.append(data)

    def set_ws_muted(self, muted: bool) -> None:
        self.muted_states.append(muted)

    async def wait_for(self, pattern=None, timeout=None, _start_pos=None) -> None:
        self.wait_for_calls.append(
            {"pattern": pattern, "timeout": timeout, "_start_pos": _start_pos}
        )
        if not self._inject_confirm:
            raise TimeoutError("inject timeout")
        if not self._post_probe_confirm:
            # post-inject 探测返回 NO
            pass


# ══════════════════════════════════════════════
# 探测命令生成
# ══════════════════════════════════════════════


class TestProbeCommands:
    def test_get_probe_command_targets_first_command(self, registry, tmp_path):
        registry.load_from_yaml(_write_config(tmp_path, _basic_config()))
        probe = registry.get_probe_command("es")

        assert probe is not None
        assert "type health" in probe
        assert SnippetRegistry.PROBE_YES in probe
        assert SnippetRegistry.PROBE_NO in probe

    def test_get_probe_command_unknown_domain_returns_none(self, registry, tmp_path):
        registry.load_from_yaml(_write_config(tmp_path, _basic_config()))
        assert registry.get_probe_command("nope") is None

    def test_get_probe_command_no_commands_returns_none(self, registry, tmp_path):
        config = _basic_config(commands=[])
        registry.load_from_yaml(_write_config(tmp_path, config, script=None))
        assert registry.get_probe_command("es") is None

    def test_get_version_probe_command(self, registry, tmp_path):
        registry.load_from_yaml(_write_config(tmp_path, _basic_config()))
        probe = registry.get_version_probe_command("es")

        assert probe is not None
        assert "__PROBE_VER_RESULT__" in probe
        assert "__ES_SNIPPET_VERSION__" in probe

    def test_get_version_probe_command_unknown_domain(self, registry, tmp_path):
        registry.load_from_yaml(_write_config(tmp_path, _basic_config()))
        assert registry.get_version_probe_command("nope") is None


# ══════════════════════════════════════════════
# Heredoc 注入构建
# ══════════════════════════════════════════════


class TestHeredocLoader:
    def test_compressed_default_uses_base64_pipeline(self, registry, tmp_path):
        registry.load_from_yaml(_write_config(tmp_path, _basic_config()))
        loader = registry.build_heredoc_loader("es", compressed=True)

        assert loader is not None
        assert "base64 -d" in loader
        assert "gunzip" in loader
        assert SnippetRegistry.INJECT_DONE in loader

    def test_compressed_false_uses_heredoc(self, registry, tmp_path):
        registry.load_from_yaml(_write_config(tmp_path, _basic_config()))
        loader = registry.build_heredoc_loader("es", compressed=False)

        assert loader is not None
        assert "cat << 'SNIPPET_EOF'" in loader
        assert "SNIPPET_EOF" in loader
        assert "source /tmp/ts-es.sh" in loader
        assert SnippetRegistry.INJECT_DONE in loader

    def test_none_when_domain_missing(self, registry, tmp_path):
        registry.load_from_yaml(_write_config(tmp_path, _basic_config()))
        assert registry.build_heredoc_loader("nope") is None

    def test_none_when_script_missing(self, registry, tmp_path):
        registry.load_from_yaml(_write_config(tmp_path, _basic_config(), script=None))
        assert registry.build_heredoc_loader("es") is None

    def test_internal_heredoc_builder(self, registry, tmp_path):
        registry.load_from_yaml(_write_config(tmp_path, _basic_config()))
        loader = registry._build_heredoc_loader(SAFE_SCRIPT, "ts-es.sh")

        assert loader.startswith("cat << 'SNIPPET_EOF' > /tmp/ts-es.sh\n")
        assert SAFE_SCRIPT in loader
        assert loader.strip().endswith(f"echo '{SnippetRegistry.INJECT_DONE}'")


# ══════════════════════════════════════════════
# 输出提取辅助函数
# ══════════════════════════════════════════════


class TestExtractHelpers:
    def test_last_nonempty_line_basic(self):
        from src.services.snippet_registry import _extract_last_nonempty_line

        assert _extract_last_nonempty_line("a\n\nb\n  \nc") == "c"

    def test_last_nonempty_line_empty(self):
        from src.services.snippet_registry import _extract_last_nonempty_line

        assert _extract_last_nonempty_line("") == ""
        assert _extract_last_nonempty_line("\n  \n\t\n") == ""

    def test_extract_version_skips_echo(self):
        from src.services.snippet_registry import _extract_version_from_output

        out = (
            'echo "__PROBE_VER_RESULT__:${__ES_SNIPPET_VERSION__:-none}"\n'
            "__PROBE_VER_RESULT__:2026.05.14.3\n"
        )
        assert _extract_version_from_output(out) == "2026.05.14.3"

    def test_extract_version_none_when_missing(self):
        from src.services.snippet_registry import _extract_version_from_output

        assert _extract_version_from_output("no version here") is None

    def test_extract_md5_valid(self):
        from src.services.snippet_registry import _extract_md5_from_output

        md5 = "abc123def456abc123def456abc123de"
        out = f'echo "__PROBE_MD5_RESULT__:{md5}"\n__PROBE_MD5_RESULT__:{md5}\n'
        assert _extract_md5_from_output(out) == md5

    def test_extract_md5_none_value(self):
        from src.services.snippet_registry import _extract_md5_from_output

        out = 'echo "__PROBE_MD5_RESULT__:none"\n__PROBE_MD5_RESULT__:none\n'
        assert _extract_md5_from_output(out) == "none"

    def test_extract_md5_skips_echo_line(self):
        from src.services.snippet_registry import _extract_md5_from_output

        md5 = "abc123def456abc123def456abc123de"
        out = (
            'echo "__PROBE_MD5_RESULT__:${_ft_md5}"\n'
            f"__PROBE_MD5_RESULT__:{md5}\n"
        )
        assert _extract_md5_from_output(out) == md5

    def test_extract_md5_rejects_invalid_hex(self):
        from src.services.snippet_registry import _extract_md5_from_output

        out = "__PROBE_MD5_RESULT__:not-a-valid-md5!!\n"
        assert _extract_md5_from_output(out) is None

    def test_extract_md5_none_when_missing(self):
        from src.services.snippet_registry import _extract_md5_from_output

        assert _extract_md5_from_output("no md5 here") is None


# ══════════════════════════════════════════════
# ensure_snippet_loaded 三步探测
# ══════════════════════════════════════════════


class TestEnsureSnippetLoaded:
    def _loaded_registry(self, registry, tmp_path) -> SnippetRegistry:
        registry.load_from_yaml(_write_config(tmp_path, _basic_config()))
        return registry

    @pytest.mark.asyncio
    async def test_unknown_domain_returns_error(self, registry, tmp_path):
        self._loaded_registry(registry, tmp_path)
        session = FakeSession()
        result = await ensure_snippet_loaded(session, registry, "nope")
        assert result is not None
        assert "nope" in result

    @pytest.mark.asyncio
    async def test_md5_match_skips_injection(self, registry, tmp_path):
        """步骤1 函数存在 + 步骤2 MD5 一致 → 不注入，返回 None。"""
        self._loaded_registry(registry, tmp_path)
        local_md5 = registry.get_script_md5("es")
        session = FakeSession(
            routes={
                "type health": f"__PROBE_YES__",
                "md5sum": f"__PROBE_MD5_RESULT__:{local_md5}",
                "gunzip": "__GZ_YES__",
            }
        )
        result = await ensure_snippet_loaded(session, registry, "es")

        assert result is None
        assert session.sent_inputs == [], "MD5 一致不应注入"

    @pytest.mark.asyncio
    async def test_md5_mismatch_triggers_injection(self, registry, tmp_path):
        """步骤1 函数存在 + 步骤2 MD5 不一致 → 重新注入。"""
        self._loaded_registry(registry, tmp_path)
        wrong_md5 = "0" * 32
        session = FakeSession(
            routes={
                "type health": "__PROBE_YES__",
                "md5sum": f"__PROBE_MD5_RESULT__:{wrong_md5}",
                "gunzip": "__GZ_YES__",
            }
        )
        result = await ensure_snippet_loaded(session, registry, "es")

        assert result is None
        assert session.sent_inputs, "MD5 不匹配应触发注入"

    @pytest.mark.asyncio
    async def test_md5_probe_timeout_triggers_injection(self, registry, tmp_path):
        """步骤2 MD5 探测超时 → 保守注入。"""
        self._loaded_registry(registry, tmp_path)

        async def _boom(*a, **k):
            raise TimeoutError("md5 probe timeout")

        session = FakeSession(routes={"type health": "__PROBE_YES__", "gunzip": "__GZ_YES__"})
        # 让 md5 探测超时：monkey-patch send_command 使 md5 路由抛异常
        orig = session.send_command

        async def _routed(command, wait_pattern=None, timeout=None):
            if "md5sum" in command:
                raise TimeoutError("md5 probe timeout")
            return await orig(command, wait_pattern, timeout)

        session.send_command = _routed

        result = await ensure_snippet_loaded(session, registry, "es")
        assert result is None
        assert session.sent_inputs, "MD5 探测超时后仍需注入"

    @pytest.mark.asyncio
    async def test_no_local_md5_returns_none(self, registry, tmp_path):
        """本地无脚本（local_md5 为空）→ 已加载即足够，返回 None。"""
        # 脚本文件缺失 → get_script_md5 返回 None
        registry.load_from_yaml(_write_config(tmp_path, _basic_config(), script=None))
        session = FakeSession(routes={"type health": "__PROBE_YES__"})
        result = await ensure_snippet_loaded(session, registry, "es")
        assert result is None

    @pytest.mark.asyncio
    async def test_function_absent_triggers_injection(self, registry, tmp_path):
        """步骤1 函数不存在（__PROBE_NO__）→ 注入。"""
        self._loaded_registry(registry, tmp_path)
        session = FakeSession(
            routes={"type health": "__PROBE_NO__", "gunzip": "__GZ_YES__"}
        )
        result = await ensure_snippet_loaded(session, registry, "es")
        assert result is None
        assert session.sent_inputs, "函数不存在应注入"

    @pytest.mark.asyncio
    async def test_probe_timeout_triggers_injection(self, registry, tmp_path):
        """步骤1 探测超时 → 直接注入。"""
        self._loaded_registry(registry, tmp_path)

        async def _boom(command, wait_pattern=None, timeout=None):
            raise ConnectionError("probe failed")

        session = FakeSession(routes={"gunzip": "__GZ_YES__"})
        session.send_command = _boom
        result = await ensure_snippet_loaded(session, registry, "es")
        assert result is None
        assert session.sent_inputs, "探测超时后需注入"

    @pytest.mark.asyncio
    async def test_gunzip_available_uses_compressed(self, registry, tmp_path):
        """远端有 gunzip → 压缩注入（base64 pipeline）。"""
        self._loaded_registry(registry, tmp_path)
        session = FakeSession(
            routes={"type health": "__PROBE_NO__", "gunzip": "__GZ_YES__"}
        )
        result = await ensure_snippet_loaded(session, registry, "es")
        assert result is None
        assert any("gunzip" in inp for inp in session.sent_inputs)

    @pytest.mark.asyncio
    async def test_gunzip_unavailable_uses_heredoc(self, registry, tmp_path):
        """远端无 gunzip → heredoc 降级注入。"""
        self._loaded_registry(registry, tmp_path)
        session = FakeSession(
            routes={"type health": "__PROBE_NO__", "gunzip": "__GZ_NO__"}
        )
        result = await ensure_snippet_loaded(session, registry, "es")
        assert result is None
        assert any("SNIPPET_EOF" in inp for inp in session.sent_inputs)

    @pytest.mark.asyncio
    async def test_gunzip_probe_timeout_falls_back_heredoc(self, registry, tmp_path):
        """gunzip 探测超时 → 保守降级为 heredoc。"""
        self._loaded_registry(registry, tmp_path)

        async def _routed(command, wait_pattern=None, timeout=None):
            if "gunzip" in command:
                raise TimeoutError("gunzip probe timeout")
            return "__PROBE_NO__"

        session = FakeSession()
        session.send_command = _routed
        result = await ensure_snippet_loaded(session, registry, "es")
        assert result is None
        assert any("SNIPPET_EOF" in inp for inp in session.sent_inputs)

    @pytest.mark.asyncio
    async def test_injection_success_mutes_ws(self, registry, tmp_path):
        """注入期间应静音 WebSocket 广播，完成后恢复。"""
        self._loaded_registry(registry, tmp_path)
        session = FakeSession(
            routes={"type health": "__PROBE_NO__", "gunzip": "__GZ_YES__"}
        )
        result = await ensure_snippet_loaded(session, registry, "es")
        assert result is None
        assert session.muted_states[0] is True, "注入开始时应静音"
        assert session.muted_states[-1] is False, "完成后应恢复广播"

    @pytest.mark.asyncio
    async def test_loader_none_returns_error(self, registry, tmp_path):
        """脚本文件缺失导致 loader 为 None → 返回错误信息。"""
        registry.load_from_yaml(_write_config(tmp_path, _basic_config(), script=None))
        session = FakeSession(
            routes={"type health": "__PROBE_NO__", "gunzip": "__GZ_YES__"}
        )
        # 让 send_command 不触发注入确认所需的 wait_for —— 这里 loader 为 None 直接返回
        result = await ensure_snippet_loaded(session, registry, "es")
        assert result is not None
        assert "脚本文件不存在" in result

    @pytest.mark.asyncio
    async def test_injection_connection_error_returns_error(self, registry, tmp_path):
        """send_input 抛出 ConnectionError → 返回错误信息。"""
        self._loaded_registry(registry, tmp_path)

        session = FakeSession(
            routes={"type health": "__PROBE_NO__", "gunzip": "__GZ_YES__"},
            inject_confirm=False,
        )

        async def _send_input_raises(data):
            raise ConnectionError("pty closed")

        session.send_input = _send_input_raises
        result = await ensure_snippet_loaded(session, registry, "es")
        assert result is not None
        assert "脚本注入失败" in result

    @pytest.mark.asyncio
    async def test_inject_timeout_post_probe_confirms(self, registry, tmp_path):
        """注入等待超时，但 post-inject 探测确认函数可用 → 视为成功。"""
        self._loaded_registry(registry, tmp_path)
        session = FakeSession(
            routes={
                "type health": "__PROBE_NO__",
                "gunzip": "__GZ_YES__",
            },
            inject_confirm=False,  # wait_for 抛 TimeoutError
        )
        # post-inject 探测（type health）返回 YES
        session._routes["type health"] = "__PROBE_YES__"
        result = await ensure_snippet_loaded(session, registry, "es")
        assert result is None, f"post-inject 确认应视为成功: {result}"

    @pytest.mark.asyncio
    async def test_inject_timeout_post_probe_not_found_returns_error(self, registry, tmp_path):
        """注入超时且 post-inject 探测确认函数不可用 → 返回错误。"""
        self._loaded_registry(registry, tmp_path)
        session = FakeSession(
            routes={"type health": "__PROBE_NO__", "gunzip": "__GZ_YES__"},
            inject_confirm=False,
        )
        result = await ensure_snippet_loaded(session, registry, "es")
        assert result is not None
        assert "函数不可用" in result or "未成功加载" in result

    @pytest.mark.asyncio
    async def test_inject_timeout_post_probe_timeout_returns_error(self, registry, tmp_path):
        """注入超时且 post-inject 探测也超时 → 返回错误。"""
        self._loaded_registry(registry, tmp_path)

        call_count = {"n": 0}

        async def _routed(command, wait_pattern=None, timeout=None):
            if "gunzip" in command:
                return "__GZ_YES__"
            if "type health" in command:
                call_count["n"] += 1
                if call_count["n"] >= 2:
                    # 第二次（post-inject）超时
                    raise TimeoutError("post probe timeout")
                return "__PROBE_NO__"
            return ""

        session = FakeSession(inject_confirm=False)
        session.send_command = _routed
        result = await ensure_snippet_loaded(session, registry, "es")
        assert result is not None
        assert "函数不可用" in result or "未成功加载" in result


# 让 registry fixture 可用（复用 test_snippet_registry 中的同名 fixture 约定）
@pytest.fixture
def registry() -> SnippetRegistry:
    return SnippetRegistry()
