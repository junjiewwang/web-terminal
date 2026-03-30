"""主机模型与 Schema 测试。"""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.models.host import Base, EntrySpecSchema, EntryType, HostCreate, HostType, HostUpdate
from src.services.host_manager import HostManager


class TestHostCreate:
    def test_valid_root_create(self):
        data = HostCreate(
            name="test-server",
            hostname="192.168.1.100",
            port=22,
            username="deploy",
            description="测试服务器",
            tags=["dev", "test"],
            host_type=HostType.ROOT,
        )
        assert data.name == "test-server"
        assert data.port == 22
        assert data.host_type == HostType.ROOT

    def test_valid_nested_create(self):
        data = HostCreate(
            name="ssh-hop",
            hostname="192.168.1.100",
            username="deploy",
            host_type=HostType.NESTED,
            parent_id=1,
            entry=EntrySpecSchema(type=EntryType.SSH_COMMAND, value="ssh root@10.0.0.1 -p 36000"),
        )
        assert data.host_type == HostType.NESTED
        assert data.entry is not None
        assert data.entry.type == EntryType.SSH_COMMAND

    def test_invalid_port(self):
        with pytest.raises(Exception):
            HostCreate(
                name="bad-port",
                hostname="10.0.0.1",
                username="root",
                port=99999,
            )


class TestHostUpdate:
    def test_partial_update(self):
        data = HostUpdate(ready_pattern="[\\$#>]\\s*$")
        dump = data.model_dump(exclude_unset=True)
        assert dump == {"ready_pattern": "[\\$#>]\\s*$"}

    def test_empty_update(self):
        data = HostUpdate()
        dump = data.model_dump(exclude_unset=True)
        assert dump == {}


class TestHostManagerTree:
    @pytest.mark.asyncio
    async def test_list_host_responses_builds_nested_tree_without_lazy_loading(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with session_factory() as session:
            manager = HostManager(session)
            root = await manager.create_host(
                HostCreate(
                    name="root-node",
                    hostname="192.168.1.10",
                    port=22,
                    username="deploy",
                    host_type=HostType.ROOT,
                )
            )
            await manager.create_host(
                HostCreate(
                    name="nested-node",
                    hostname="192.168.1.10",
                    port=22,
                    username="deploy",
                    host_type=HostType.NESTED,
                    parent_id=root.id,
                    entry=EntrySpecSchema(type=EntryType.MENU_SEND, value="10.0.0.8"),
                )
            )
            await session.commit()

        async with session_factory() as session:
            manager = HostManager(session)
            responses = await manager.list_host_responses()

        assert len(responses) == 1
        assert responses[0].name == "root-node"
        assert len(responses[0].children) == 1
        assert responses[0].children[0].name == "nested-node"
        assert responses[0].children[0].entry.type == EntryType.MENU_SEND

        await engine.dispose()
