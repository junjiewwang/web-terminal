"""主机管理器 CRUD / 路径回溯 / 树构建测试。

补充 test_host_manager.py 未覆盖的分支。
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.models.host import (
    AuthType,
    Base,
    EntrySpecSchema,
    EntryType,
    HostCreate,
    HostType,
    HostUpdate,
)
from src.services.host_manager import HostManager


@pytest_asyncio.fixture
async def manager(monkeypatch):
    from cryptography.fernet import Fernet

    from src.utils import security

    monkeypatch.setattr(security, "_fernet", None)
    monkeypatch.setattr(security, "_FERNET_KEY", None)
    monkeypatch.setenv("WETTY_ENCRYPTION_KEY", Fernet.generate_key().decode())

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield HostManager(session)

    await engine.dispose()


def _root(name: str = "bastion", **kw) -> HostCreate:
    return HostCreate(
        name=name,
        hostname=f"{name}.example.com",
        port=kw.pop("port", 22),
        username=kw.pop("username", "deploy"),
        host_type=HostType.ROOT,
        **kw,
    )


def _nested(name: str, parent_id: int, **kw) -> HostCreate:
    return HostCreate(
        name=name,
        hostname=kw.pop("hostname", "10.0.0.1"),
        port=kw.pop("port", 22),
        username=kw.pop("username", "root"),
        host_type=HostType.NESTED,
        parent_id=parent_id,
        entry=kw.pop("entry", EntrySpecSchema(type=EntryType.MENU_SEND, value="10.0.0.8")),
        **kw,
    )


# ══════════════════════════════════════════════
# 基本查询
# ══════════════════════════════════════════════


class TestHasHosts:
    @pytest.mark.asyncio
    async def test_false_when_empty(self, manager):
        assert await manager.has_hosts() is False

    @pytest.mark.asyncio
    async def test_true_after_create(self, manager):
        await manager.create_host(_root())
        assert await manager.has_hosts() is True


class TestLookups:
    @pytest.mark.asyncio
    async def test_get_by_id_and_name(self, manager):
        created = await manager.create_host(_root("web-1"))

        assert (await manager.get_host_by_id(created.id)).name == "web-1"
        assert (await manager.get_host_by_name("web-1")).id == created.id

    @pytest.mark.asyncio
    async def test_missing_lookups_return_none(self, manager):
        assert await manager.get_host_by_id(9999) is None
        assert await manager.get_host_by_name("ghost") is None


# ══════════════════════════════════════════════
# 创建
# ══════════════════════════════════════════════


class TestCreateHost:
    @pytest.mark.asyncio
    async def test_persists_basic_fields(self, manager):
        host = await manager.create_host(
            _root("web-1", description="生产机", tags=["prod", "web"])
        )

        assert host.id is not None
        assert host.description == "生产机"
        assert host.tags == "prod,web", "tags 以逗号拼接存储"

    @pytest.mark.asyncio
    async def test_no_tags_stored_as_none(self, manager):
        assert (await manager.create_host(_root())).tags is None

    @pytest.mark.asyncio
    async def test_password_is_encrypted(self, manager):
        host = await manager.create_host(
            _root("pw-host", auth_type=AuthType.PASSWORD, password="s3cret")
        )

        assert host.password_encrypted is not None
        assert "s3cret" not in host.password_encrypted

    @pytest.mark.asyncio
    async def test_password_ignored_for_key_auth(self, manager):
        """auth_type=KEY 时即使传了 password 也不应加密存储。"""
        host = await manager.create_host(
            _root("key-host", auth_type=AuthType.KEY, password="ignored")
        )
        assert host.password_encrypted is None

    @pytest.mark.asyncio
    async def test_entry_password_encrypted(self, manager):
        root = await manager.create_host(_root())
        child = await manager.create_host(
            _nested("hop", root.id, entry_password="hop-pw")
        )

        assert child.entry_password_encrypted is not None
        assert "hop-pw" not in child.entry_password_encrypted

    @pytest.mark.asyncio
    async def test_entry_spec_serialised_to_json(self, manager):
        root = await manager.create_host(_root())
        child = await manager.create_host(
            _nested(
                "hop",
                root.id,
                entry=EntrySpecSchema(type=EntryType.SSH_COMMAND, value="ssh root@10.0.0.5"),
            )
        )

        spec = json.loads(child.entry_spec)
        assert spec["type"] == "ssh_command"
        assert spec["value"] == "ssh root@10.0.0.5"


# ══════════════════════════════════════════════
# 更新（部分更新语义）
# ══════════════════════════════════════════════


class TestUpdateHost:
    @pytest.mark.asyncio
    async def test_unknown_id_returns_none(self, manager):
        assert await manager.update_host(9999, HostUpdate(description="x")) is None

    @pytest.mark.asyncio
    async def test_updates_only_provided_fields(self, manager):
        host = await manager.create_host(_root("web-1", description="original"))

        updated = await manager.update_host(host.id, HostUpdate(port=2222))

        assert updated.port == 2222
        assert updated.description == "original", "未提供的字段不应被清空"

    @pytest.mark.asyncio
    async def test_tags_replaced(self, manager):
        host = await manager.create_host(_root(tags=["old"]))

        updated = await manager.update_host(host.id, HostUpdate(tags=["a", "b"]))

        assert updated.tags == "a,b"

    @pytest.mark.asyncio
    async def test_empty_tags_clears_field(self, manager):
        host = await manager.create_host(_root(tags=["old"]))

        updated = await manager.update_host(host.id, HostUpdate(tags=[]))

        assert updated.tags is None

    @pytest.mark.asyncio
    async def test_password_updated_and_encrypted(self, manager):
        host = await manager.create_host(
            _root(auth_type=AuthType.PASSWORD, password="old-pw")
        )
        before = host.password_encrypted

        updated = await manager.update_host(host.id, HostUpdate(password="new-pw"))

        assert updated.password_encrypted != before
        assert "new-pw" not in updated.password_encrypted

    @pytest.mark.asyncio
    async def test_explicit_empty_password_clears_it(self, manager):
        host = await manager.create_host(
            _root(auth_type=AuthType.PASSWORD, password="old-pw")
        )

        updated = await manager.update_host(host.id, HostUpdate(password=""))

        assert updated.password_encrypted is None

    @pytest.mark.asyncio
    async def test_entry_can_be_replaced_and_cleared(self, manager):
        root = await manager.create_host(_root())
        child = await manager.create_host(_nested("hop", root.id))

        replaced = await manager.update_host(
            child.id,
            HostUpdate(entry=EntrySpecSchema(type=EntryType.SSH_COMMAND, value="ssh a@b")),
        )
        assert json.loads(replaced.entry_spec)["value"] == "ssh a@b"

        cleared = await manager.update_host(child.id, HostUpdate(entry=None))
        assert cleared.entry_spec is None


# ══════════════════════════════════════════════
# 删除
# ══════════════════════════════════════════════


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_host(self, manager):
        host = await manager.create_host(_root())

        assert await manager.delete_host(host.id) is True
        assert await manager.get_host_by_id(host.id) is None

    @pytest.mark.asyncio
    async def test_delete_unknown_returns_false(self, manager):
        assert await manager.delete_host(9999) is False

    @pytest.mark.asyncio
    async def test_delete_all_returns_count(self, manager):
        for i in range(3):
            await manager.create_host(_root(f"h{i}"))

        assert await manager.delete_all_hosts() == 3
        assert await manager.has_hosts() is False

    @pytest.mark.asyncio
    async def test_delete_all_on_empty_db(self, manager):
        assert await manager.delete_all_hosts() == 0


# ══════════════════════════════════════════════
# 连接路径回溯
# ══════════════════════════════════════════════


class TestConnectionPath:
    @pytest.mark.asyncio
    async def test_root_only_path(self, manager):
        root = await manager.create_host(_root())

        path = await manager.get_connection_path(root)

        assert [h.name for h in path] == ["bastion"]

    @pytest.mark.asyncio
    async def test_multi_hop_path_is_root_first(self, manager):
        root = await manager.create_host(_root())
        mid = await manager.create_host(_nested("mid", root.id))
        leaf = await manager.create_host(_nested("leaf", mid.id))

        path = await manager.get_connection_path(leaf)

        assert [h.name for h in path] == ["bastion", "mid", "leaf"]

    @pytest.mark.asyncio
    async def test_orphaned_parent_raises(self, manager):
        root = await manager.create_host(_root())
        child = await manager.create_host(_nested("orphan", root.id))
        await manager.delete_host(root.id)

        with pytest.raises(ValueError, match="父节点不存在"):
            await manager.get_connection_path(child)


class TestBuildInstanceName:
    @pytest.mark.asyncio
    async def test_joins_with_double_dash(self, manager):
        root = await manager.create_host(_root())
        child = await manager.create_host(_nested("hop", root.id))

        path = await manager.get_connection_path(child)

        assert HostManager.build_instance_name(path) == "bastion--hop"

    def test_single_node(self):
        from types import SimpleNamespace

        assert HostManager.build_instance_name([SimpleNamespace(name="solo")]) == "solo"


# ══════════════════════════════════════════════
# 树构建与标签过滤
# ══════════════════════════════════════════════


class TestHostTree:
    @pytest.mark.asyncio
    async def test_only_roots_at_top_level(self, manager):
        root = await manager.create_host(_root())
        await manager.create_host(_nested("child", root.id))

        tree = await manager.list_hosts()

        assert len(tree) == 1
        assert tree[0].name == "bastion"

    @pytest.mark.asyncio
    async def test_three_level_nesting(self, manager):
        root = await manager.create_host(_root())
        mid = await manager.create_host(_nested("mid", root.id))
        await manager.create_host(_nested("leaf", mid.id))

        responses = await manager.list_host_responses()

        assert responses[0].children[0].children[0].name == "leaf"

    @pytest.mark.asyncio
    async def test_tag_filter(self, manager):
        await manager.create_host(_root("prod-1", tags=["prod"]))
        await manager.create_host(_root("dev-1", tags=["dev"]))

        assert [h.name for h in await manager.list_hosts(tag="prod")] == ["prod-1"]

    @pytest.mark.asyncio
    async def test_tag_filter_no_match(self, manager):
        await manager.create_host(_root("h", tags=["prod"]))
        assert await manager.list_hosts(tag="staging") == []

    @pytest.mark.asyncio
    async def test_empty_db_returns_empty_tree(self, manager):
        assert await manager.list_hosts() == []
        assert await manager.list_host_responses() == []
