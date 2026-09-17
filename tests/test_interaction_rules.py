"""交互规则服务与自动识别引擎测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.models.host import Base, EntrySpecSchema, EntryType, Host
from src.models.interaction_rule import (
    InteractionRule,
    InteractionRuleCreate,
    InteractionRuleUpdate,
)
from src.services.interaction_rule_service import InteractionRuleService
from src.services.jump_orchestrator import ConnectionOrchestrator
from src.services.terminal_manager import _merge_fragment_lines


# ── Service 测试 fixture ────────────────────────


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def service(session):
    return InteractionRuleService(session)


# ── Service CRUD 测试 ───────────────────────────


class TestInteractionRuleService:
    @pytest.mark.asyncio
    async def test_create_and_list(self, service, session):
        await service.create(
            InteractionRuleCreate(name="password", wait_pattern="password:", send_text="{{password}}")
        )
        await session.commit()

        rules = await service.list_rules()
        assert len(rules) == 1
        assert rules[0].name == "password"
        assert rules[0].enabled is True

    @pytest.mark.asyncio
    async def test_list_enabled_only(self, service, session):
        await service.create(
            InteractionRuleCreate(name="on", wait_pattern="a", send_text="b", enabled=True)
        )
        await service.create(
            InteractionRuleCreate(name="off", wait_pattern="c", send_text="d", enabled=False)
        )
        await session.commit()

        enabled = await service.list_enabled()
        assert [r.name for r in enabled] == ["on"]

    @pytest.mark.asyncio
    async def test_list_enabled_sorted_by_priority(self, service, session):
        await service.create(
            InteractionRuleCreate(name="low", wait_pattern="a", send_text="b", priority=10)
        )
        await service.create(
            InteractionRuleCreate(name="high", wait_pattern="c", send_text="d", priority=200)
        )
        await session.commit()

        enabled = await service.list_enabled()
        assert [r.name for r in enabled] == ["high", "low"]

    @pytest.mark.asyncio
    async def test_list_enabled_filtered_by_names(self, service, session):
        await service.create(InteractionRuleCreate(name="a", wait_pattern="x", send_text="y"))
        await service.create(InteractionRuleCreate(name="b", wait_pattern="z", send_text="w"))
        await session.commit()

        enabled = await service.list_enabled(["b"])
        assert [r.name for r in enabled] == ["b"]

    @pytest.mark.asyncio
    async def test_update(self, service, session):
        rule = await service.create(
            InteractionRuleCreate(name="r", wait_pattern="a", send_text="b")
        )
        await session.commit()

        updated = await service.update(rule.id, InteractionRuleUpdate(priority=50, enabled=False))
        assert updated is not None
        assert updated.priority == 50
        assert updated.enabled is False

    @pytest.mark.asyncio
    async def test_delete(self, service, session):
        rule = await service.create(
            InteractionRuleCreate(name="r", wait_pattern="a", send_text="b")
        )
        await session.commit()

        assert await service.delete(rule.id) is True
        assert await service.delete(rule.id) is False

    def test_get_examples(self):
        examples = InteractionRuleService.get_examples()
        assert len(examples) > 0
        assert all(ex.wait_pattern and ex.send_text for ex in examples)


# ── 自动识别引擎测试 ────────────────────────────


def _make_rule(
    name: str,
    wait: str,
    send: str,
    *,
    priority: int = 100,
    max_retries: int = 1,
    timeout: float = 15.0,
) -> InteractionRule:
    return InteractionRule(
        name=name,
        wait_pattern=wait,
        send_text=send,
        priority=priority,
        max_retries=max_retries,
        timeout=timeout,
        enabled=True,
    )


def _make_node(name: str = "target", username: str = "junjiewwang") -> Host:
    host = Host()
    host.name = name
    host.username = username
    host.hostname = "10.0.0.1"
    host.port = 22
    return host


def _pty() -> AsyncMock:
    session = AsyncMock()
    session.send_input = AsyncMock()
    session.wait_for = AsyncMock(return_value="matched")
    return session


class TestAutoInteract:
    @pytest.mark.asyncio
    async def test_password_rule_auto_input(self):
        pty = _pty()
        pty.wait_for = AsyncMock(side_effect=["junjiewwang's password: ", "Last login\n[root@h ~]# "])

        node = _make_node()
        entry = EntrySpecSchema(
            type=EntryType.MENU_SEND, value="10.22.2.146", success_pattern=r"Last login|[\$#>]\s*$"
        )
        rules = [_make_rule("password", r"(?i)password\s*[:：]\s*$", "{{password}}")]

        result = await ConnectionOrchestrator(pty)._auto_interact(
            node=node, entry=entry, success_pattern=r"Last login|[\$#>]\s*$",
            password="secret", rules=rules,
        )

        assert result.success is True
        pty.send_input.assert_any_call("secret\r")

    @pytest.mark.asyncio
    async def test_success_pattern_priority(self):
        pty = _pty()
        pty.wait_for = AsyncMock(return_value="[root@host ~]# ")

        node = _make_node()
        entry = EntrySpecSchema(
            type=EntryType.MENU_SEND, value="10.0.0.1", success_pattern=r"[\$#>]\s*$"
        )
        rules = [_make_rule("password", r"(?i)password", "{{password}}")]

        result = await ConnectionOrchestrator(pty)._auto_interact(
            node=node, entry=entry, success_pattern=r"[\$#>]\s*$",
            password="secret", rules=rules,
        )

        assert result.success is True
        pty.send_input.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_multi_step_user_id_then_password(self):
        pty = _pty()
        pty.wait_for = AsyncMock(side_effect=["ID> ", "password: ", "Last login"])

        node = _make_node()
        entry = EntrySpecSchema(
            type=EntryType.MENU_SEND,
            value="10.22.2.146",
            success_pattern=r"Last login|[\$#>]\s*$",
            rule_vars={"user_id": "1"},
        )
        rules = [
            _make_rule("password", r"(?i)password\s*[:：]\s*$", "{{password}}", priority=100),
            _make_rule("select_user", r"(?i)^\s*ID\s*>\s*$", "{{user_id}}", priority=90),
        ]

        result = await ConnectionOrchestrator(pty)._auto_interact(
            node=node, entry=entry, success_pattern=r"Last login|[\$#>]\s*$",
            password="secret", rules=rules,
        )

        assert result.success is True
        pty.send_input.assert_any_call("1\r")
        pty.send_input.assert_any_call("secret\r")

    @pytest.mark.asyncio
    async def test_password_missing_rejected(self):
        pty = _pty()
        pty.wait_for = AsyncMock(return_value="password: ")

        node = _make_node()
        entry = EntrySpecSchema(
            type=EntryType.MENU_SEND, value="10.0.0.1", success_pattern=r"[\$#>]\s*$"
        )
        rules = [_make_rule("password", r"(?i)password", "{{password}}")]

        result = await ConnectionOrchestrator(pty)._auto_interact(
            node=node, entry=entry, success_pattern=r"[\$#>]\s*$",
            password=None, rules=rules,
        )

        assert result.success is False
        assert "credential_ref" in result.message

    @pytest.mark.asyncio
    async def test_max_retries_skips_exhausted_rule(self):
        pty = _pty()
        pty.wait_for = AsyncMock(return_value="password: ")

        node = _make_node()
        entry = EntrySpecSchema(
            type=EntryType.MENU_SEND, value="10.0.0.1", success_pattern=r"[\$#>]\s*$"
        )
        rules = [_make_rule("password", r"(?i)password", "{{password}}", max_retries=1)]

        orch = ConnectionOrchestrator(pty)
        orch.MAX_AUTO_STEPS = 3

        result = await orch._auto_interact(
            node=node, entry=entry, success_pattern=r"[\$#>]\s*$",
            password="secret", rules=rules,
        )

        assert result.skipped_reason == "manual_input_required"
        assert pty.send_input.await_count == 1

    @pytest.mark.asyncio
    async def test_single_char_prompt_not_treated_as_success(self):
        """success_pattern 匹配到单字符提示符（如 Opt> 的 >）时不应误判登录成功。"""
        pty = _pty()
        pty.wait_for = AsyncMock(side_effect=[
            "Opt> Opt>",   # 单字符 ">" 误匹配，应被忽略
            "ID> ",        # select_user 规则
            "password: ",  # password 规则
            "Last login",  # 强标志，真正登录成功
        ])

        node = _make_node()
        entry = EntrySpecSchema(
            type=EntryType.MENU_SEND,
            value="10.22.2.146",
            success_pattern=r"Last login|\]#|\]\$|[\$#>]\s*$",
            rule_vars={"user_id": "1"},
        )
        rules = [
            _make_rule("password", r"(?i)password\s*[:：]\s*$", "{{password}}", priority=100),
            _make_rule("select_user", r"(?i)^\s*ID\s*>\s*$", "{{user_id}}", priority=90),
        ]

        result = await ConnectionOrchestrator(pty)._auto_interact(
            node=node, entry=entry,
            success_pattern=r"Last login|\]#|\]\$|[\$#>]\s*$",
            password="secret", rules=rules,
        )

        assert result.success is True
        pty.send_input.assert_any_call("1\r")
        pty.send_input.assert_any_call("secret\r")


class TestMergeFragmentLines:
    def test_merges_crlf_fragments(self):
        assert _merge_fragment_lines(["返回：B/b", "I\r", "D\r", ">\r"]) == "返回：B/b\nID>"

    def test_merges_lf_fragments(self):
        assert _merge_fragment_lines(["I", "D", ">"]) == "ID>"

    def test_preserves_normal_lines(self):
        assert _merge_fragment_lines(["hostname", "my-server", "[root@h ~]#"]) == (
            "hostname\nmy-server\n[root@h ~]#"
        )

    def test_merges_input_echo(self):
        assert _merge_fragment_lines(["1", "0", ".", "2", "2"]) == "10.22"
