"""共享凭据服务测试 — CRUD / 加密存储 / 引用保护。"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.models.credential import Credential, CredentialCreate, CredentialUpdate
from src.models.host import AuthType, Base, Host, HostType
from src.services.credential_service import CredentialService


@pytest_asyncio.fixture
async def session(monkeypatch):
    # 固定加密密钥，保证用例内加解密可往返
    from cryptography.fernet import Fernet

    from src.utils import security

    monkeypatch.setattr(security, "_fernet", None)
    monkeypatch.setattr(security, "_FERNET_KEY", None)
    monkeypatch.setenv("WETTY_ENCRYPTION_KEY", Fernet.generate_key().decode())

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s

    await engine.dispose()


@pytest_asyncio.fixture
async def service(session):
    return CredentialService(session)


async def _add_host(session, name: str, credential_ref: str | None) -> Host:
    host = Host(
        name=name,
        hostname=f"{name}.example.com",
        port=22,
        username="root",
        auth_type=AuthType.PASSWORD,
        host_type=HostType.ROOT,
        credential_ref=credential_ref,
    )
    session.add(host)
    await session.flush()
    return host


# ══════════════════════════════════════════════
# 创建
# ══════════════════════════════════════════════


class TestCreate:
    @pytest.mark.asyncio
    async def test_creates_credential_with_encrypted_password(self, service, session):
        cred = await service.create(
            CredentialCreate(name="bastion", password="s3cret", description="堡垒机")
        )

        assert cred.id is not None
        assert cred.name == "bastion"
        assert cred.description == "堡垒机"
        # 密码必须加密存储
        assert cred.password_encrypted != "s3cret"
        assert "s3cret" not in cred.password_encrypted

    @pytest.mark.asyncio
    async def test_stored_password_is_decryptable(self, service):
        await service.create(CredentialCreate(name="c1", password="round-trip"))
        assert await service.get_decrypted_password("c1") == "round-trip"

    @pytest.mark.asyncio
    async def test_description_optional(self, service):
        cred = await service.create(CredentialCreate(name="c1", password="p"))
        assert cred.description is None


# ══════════════════════════════════════════════
# 查询
# ══════════════════════════════════════════════


class TestQueries:
    @pytest.mark.asyncio
    async def test_get_by_name_found_and_missing(self, service):
        await service.create(CredentialCreate(name="known", password="p"))

        assert (await service.get_by_name("known")) is not None
        assert (await service.get_by_name("unknown")) is None

    @pytest.mark.asyncio
    async def test_get_by_id_found_and_missing(self, service):
        cred = await service.create(CredentialCreate(name="c1", password="p"))

        assert (await service.get_by_id(cred.id)) is not None
        assert (await service.get_by_id(9999)) is None

    @pytest.mark.asyncio
    async def test_list_is_sorted_by_name(self, service):
        for name in ("zeta", "alpha", "mid"):
            await service.create(CredentialCreate(name=name, password="p"))

        names = [c.name for c in await service.list_credentials()]
        assert names == ["alpha", "mid", "zeta"]

    @pytest.mark.asyncio
    async def test_list_never_exposes_password(self, service):
        await service.create(CredentialCreate(name="c1", password="p"))
        response = (await service.list_credentials())[0]

        assert response.has_password is True
        assert not hasattr(response, "password")
        assert not hasattr(response, "password_encrypted")

    @pytest.mark.asyncio
    async def test_list_includes_reference_count(self, service, session):
        await service.create(CredentialCreate(name="shared", password="p"))
        await _add_host(session, "h1", "shared")
        await _add_host(session, "h2", "shared")
        await _add_host(session, "h3", None)

        response = (await service.list_credentials())[0]
        assert response.ref_count == 2

    @pytest.mark.asyncio
    async def test_list_names_returns_name_and_description(self, service):
        await service.create(CredentialCreate(name="c1", password="p", description="d1"))
        items = await service.list_names()

        assert len(items) == 1
        assert items[0].name == "c1"
        assert items[0].description == "d1"

    @pytest.mark.asyncio
    async def test_list_empty(self, service):
        assert await service.list_credentials() == []
        assert await service.list_names() == []


class TestGetDecryptedPassword:
    @pytest.mark.asyncio
    async def test_missing_credential_returns_none(self, service):
        assert await service.get_decrypted_password("nope") is None

    @pytest.mark.asyncio
    async def test_undecryptable_value_returns_none(self, service, session):
        """密钥不匹配时应返回 None 而非抛异常。"""
        session.add(Credential(name="broken", password_encrypted="fernet:garbage"))
        await session.flush()

        assert await service.get_decrypted_password("broken") is None


# ══════════════════════════════════════════════
# 更新
# ══════════════════════════════════════════════


class TestUpdate:
    @pytest.mark.asyncio
    async def test_updates_password_only(self, service):
        cred = await service.create(
            CredentialCreate(name="c1", password="old", description="keep-me")
        )

        await service.update(cred.id, CredentialUpdate(password="new"))

        assert await service.get_decrypted_password("c1") == "new"
        assert (await service.get_by_id(cred.id)).description == "keep-me"

    @pytest.mark.asyncio
    async def test_updates_description_only(self, service):
        cred = await service.create(CredentialCreate(name="c1", password="pw"))

        await service.update(cred.id, CredentialUpdate(description="new-desc"))

        assert (await service.get_by_id(cred.id)).description == "new-desc"
        assert await service.get_decrypted_password("c1") == "pw", "密码不应被改动"

    @pytest.mark.asyncio
    async def test_empty_update_is_noop(self, service):
        cred = await service.create(CredentialCreate(name="c1", password="pw", description="d"))

        await service.update(cred.id, CredentialUpdate())

        assert await service.get_decrypted_password("c1") == "pw"
        assert (await service.get_by_id(cred.id)).description == "d"

    @pytest.mark.asyncio
    async def test_missing_credential_returns_none(self, service):
        assert await service.update(9999, CredentialUpdate(password="x")) is None


# ══════════════════════════════════════════════
# 删除（引用保护）
# ══════════════════════════════════════════════


class TestDelete:
    @pytest.mark.asyncio
    async def test_deletes_unreferenced_credential(self, service, session):
        cred = await service.create(CredentialCreate(name="orphan", password="p"))

        ok, message = await service.delete(cred.id)

        assert ok is True
        assert message == "删除成功"
        assert (await session.execute(select(Credential))).scalars().all() == []

    @pytest.mark.asyncio
    async def test_missing_credential_returns_error(self, service):
        ok, message = await service.delete(9999)
        assert ok is False
        assert message == "凭据不存在"

    @pytest.mark.asyncio
    async def test_refuses_when_referenced_and_lists_hosts(self, service, session):
        cred = await service.create(CredentialCreate(name="in-use", password="p"))
        await _add_host(session, "web-1", "in-use")

        ok, message = await service.delete(cred.id)

        assert ok is False
        assert "web-1" in message
        # 凭据仍在
        assert await service.get_by_id(cred.id) is not None

    @pytest.mark.asyncio
    async def test_truncates_long_reference_list(self, service, session):
        """引用超过 5 个时只列前 5 个并给出总数。"""
        cred = await service.create(CredentialCreate(name="popular", password="p"))
        for i in range(7):
            await _add_host(session, f"host-{i}", "popular")

        ok, message = await service.delete(cred.id)

        assert ok is False
        assert "等 7 个" in message


# ══════════════════════════════════════════════
# YAML 同步 upsert
# ══════════════════════════════════════════════


class TestUpsertFromYaml:
    @pytest.mark.asyncio
    async def test_inserts_when_absent(self, service):
        await service.upsert_from_yaml("new-cred", "pw", "desc")

        cred = await service.get_by_name("new-cred")
        assert cred is not None
        assert cred.description == "desc"
        assert await service.get_decrypted_password("new-cred") == "pw"

    @pytest.mark.asyncio
    async def test_updates_password_when_present(self, service):
        await service.create(CredentialCreate(name="existing", password="old", description="d"))

        await service.upsert_from_yaml("existing", "rotated")

        assert await service.get_decrypted_password("existing") == "rotated"

    @pytest.mark.asyncio
    async def test_none_description_preserves_existing(self, service):
        await service.create(CredentialCreate(name="c1", password="p", description="original"))

        await service.upsert_from_yaml("c1", "p2", description=None)

        assert (await service.get_by_name("c1")).description == "original"

    @pytest.mark.asyncio
    async def test_does_not_duplicate_on_repeat(self, service, session):
        await service.upsert_from_yaml("c1", "p")
        await service.upsert_from_yaml("c1", "p")

        assert len((await session.execute(select(Credential))).scalars().all()) == 1
