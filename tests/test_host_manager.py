"""主机模型与 Schema 测试。"""

from unittest.mock import patch

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


class TestHostManagerCredentialRef:
    @pytest.mark.asyncio
    async def test_sync_from_yaml_resolves_credential_ref_for_root_and_nested(self, tmp_path):
        yaml_path = tmp_path / "hosts.yaml"
        yaml_path.write_text(
            """
credentials:
  bastion-login: "bastion-secret"
  hop-login:
    password: "hop-secret"
hosts:
  - name: bastion
    hostname: bastion.example.com
    port: 36000
    username: operator
    auth_type: password
    credential_ref: bastion-login
    ready_pattern: '\\[Host\\]>|Opt>'
    children:
      - name: prod-root-hop
        credential_ref: hop-login
        ready_pattern: 'Last login|[\\$#>]\\s*$'
        entry:
          type: ssh_command
          value: "ssh root@10.10.3.5 -p 36000"
          steps:
            - wait: "[Pp]assword:"
              send: "{{password}}"
""".strip(),
            encoding="utf-8",
        )

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with session_factory() as session:
            manager = HostManager(session)
            with patch("src.services.host_manager.encrypt_password", side_effect=lambda value: f"enc::{value}"):
                result = await manager.sync_from_yaml(yaml_path)

            root = await manager.get_host_by_name("bastion")
            nested = await manager.get_host_by_name("prod-root-hop")

        assert result.errors == []
        assert result.added == 2
        assert root is not None
        assert nested is not None
        assert root.password_encrypted == "enc::bastion-secret"
        assert root.credential_ref == "bastion-login"
        assert nested.entry_password_encrypted == "enc::hop-secret"
        assert nested.credential_ref == "hop-login"

        await engine.dispose()

    @pytest.mark.asyncio
    async def test_sync_from_yaml_rejects_missing_credential_ref(self, tmp_path):
        yaml_path = tmp_path / "hosts.yaml"
        yaml_path.write_text(
            """
hosts:
  - name: bastion
    hostname: bastion.example.com
    port: 36000
    username: operator
    auth_type: key
    children:
      - name: broken-hop
        credential_ref: missing-ref
        entry:
          type: ssh_command
          value: "ssh root@10.10.3.5 -p 36000"
          steps:
            - wait: "[Pp]assword:"
              send: "{{password}}"
""".strip(),
            encoding="utf-8",
        )

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with session_factory() as session:
            manager = HostManager(session)
            result = await manager.sync_from_yaml(yaml_path)
            responses = await manager.list_host_responses()

        assert result.added == 0
        assert result.updated == 0
        assert result.deleted == 0
        assert len(result.errors) == 1
        assert "missing-ref" in result.errors[0]
        assert responses == []

        await engine.dispose()
