"""认证服务测试 — 密码校验 / JWT / Refresh Token Rotation。

使用内存 SQLite + 真实 ORM，覆盖 AuthService 的持久化路径。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.models.auth import AuthConfigModel, RefreshTokenModel
from src.models.host import Base
from src.services.auth_service import AuthConfig, AuthService

# bcrypt rounds=4 是允许的最小值，仅用于加速测试
FAST_ROUNDS = 4
PASSWORD = "correct-horse-battery"
# HS256 建议密钥 >= 32 字节，避免 InsecureKeyLengthWarning
JWT_SECRET = "test-secret-" + "x" * 32


def _hash(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=FAST_ROUNDS)).decode()


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
    """已启用认证、密码已设置的服务实例。"""
    svc = AuthService()
    session.add(
        AuthConfigModel(
            id=1,
            enabled=True,
            password_hash=_hash(PASSWORD),
            jwt_secret=JWT_SECRET,
            access_token_expire_hours=2.0,
            refresh_token_expire_days=7,
        )
    )
    await session.flush()
    await svc.init_from_db(session)
    return svc


# ══════════════════════════════════════════════
# 初始化
# ══════════════════════════════════════════════


class TestInitFromDb:
    @pytest.mark.asyncio
    async def test_loads_existing_config_from_db(self, session):
        session.add(
            AuthConfigModel(
                id=1, enabled=True, password_hash="h", jwt_secret="from-db",
                access_token_expire_hours=5.0, refresh_token_expire_days=3,
            )
        )
        await session.flush()

        svc = AuthService()
        await svc.init_from_db(session)

        assert svc.loaded is True
        assert svc.is_auth_enabled is True
        assert svc.jwt_secret == "from-db"
        assert svc.access_token_expire_hours == 5.0

    @pytest.mark.asyncio
    async def test_seeds_defaults_when_db_empty_and_no_yaml(self, session):
        svc = AuthService()
        await svc.init_from_db(session, yaml_path=None)

        # 无种子文件 → 默认关闭密码保护
        assert svc.loaded is True
        assert svc.is_auth_enabled is False

        # 种子应已写入 DB
        row = (await session.execute(select(AuthConfigModel))).scalar_one()
        assert row.id == 1

    @pytest.mark.asyncio
    async def test_seeds_from_yaml_when_db_empty(self, session, tmp_path):
        yaml_file = tmp_path / "auth.yaml"
        yaml_file.write_text(
            "enabled: true\npassword_hash: seeded-hash\njwt_secret: yaml-secret\n",
            encoding="utf-8",
        )

        svc = AuthService()
        await svc.init_from_db(session, yaml_path=yaml_file)

        assert svc.is_auth_enabled is True
        assert svc.jwt_secret == "yaml-secret"
        row = (await session.execute(select(AuthConfigModel))).scalar_one()
        assert row.password_hash == "seeded-hash"

    @pytest.mark.asyncio
    async def test_malformed_yaml_falls_back_to_disabled(self, session, tmp_path):
        yaml_file = tmp_path / "auth.yaml"
        yaml_file.write_text("enabled: [not, a, bool]\n", encoding="utf-8")

        svc = AuthService()
        await svc.init_from_db(session, yaml_path=yaml_file)

        assert svc.is_auth_enabled is False

    @pytest.mark.asyncio
    async def test_env_var_overrides_jwt_secret(self, session, monkeypatch):
        monkeypatch.setenv("WETTY_JWT_SECRET", "env-override")
        session.add(AuthConfigModel(id=1, enabled=True, password_hash="h", jwt_secret="db-secret"))
        await session.flush()

        svc = AuthService()
        await svc.init_from_db(session)

        assert svc.jwt_secret == "env-override"

    @pytest.mark.asyncio
    async def test_not_loaded_before_init(self):
        svc = AuthService()
        assert svc.loaded is False
        assert svc.is_auth_enabled is False

    @pytest.mark.asyncio
    async def test_config_property_returns_copy(self, service):
        snapshot = service.config
        snapshot.jwt_secret = "mutated"
        assert service.jwt_secret == JWT_SECRET


class TestReloadFromDb:
    @pytest.mark.asyncio
    async def test_picks_up_external_password_change(self, service, session):
        """CLI 直改 DB 后，reload 应让新密码生效。"""
        new_hash = _hash("new-password")
        row = (await session.execute(select(AuthConfigModel))).scalar_one()
        row.password_hash = new_hash
        await session.flush()

        await service.reload_from_db(session)

        assert service.authenticate("new-password") is not None
        assert service.authenticate(PASSWORD) is None


# ══════════════════════════════════════════════
# 认证 & JWT
# ══════════════════════════════════════════════


class TestAuthenticate:
    @pytest.mark.asyncio
    async def test_correct_password_returns_token_pair(self, service):
        result = service.authenticate(PASSWORD)
        assert result is not None
        access, refresh = result
        assert access and refresh
        assert access != refresh

    @pytest.mark.asyncio
    async def test_wrong_password_returns_none(self, service):
        assert service.authenticate("wrong") is None

    @pytest.mark.asyncio
    async def test_empty_password_returns_none(self, service):
        assert service.authenticate("") is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_hash_configured(self, session):
        svc = AuthService()
        svc._config = AuthConfig(enabled=True, password_hash="")
        assert svc.authenticate("anything") is None

    @pytest.mark.asyncio
    async def test_refresh_tokens_are_unique_per_login(self, service):
        _, first = service.authenticate(PASSWORD)
        _, second = service.authenticate(PASSWORD)
        assert first != second


class TestAuthenticateAndPersist:
    @pytest.mark.asyncio
    async def test_persists_hashed_refresh_token(self, service, session):
        result = await service.authenticate_and_persist(PASSWORD, session)
        assert result is not None
        _, refresh = result

        rows = (await session.execute(select(RefreshTokenModel))).scalars().all()
        assert len(rows) == 1
        # DB 只存哈希，不存明文
        assert rows[0].token_hash != refresh
        assert rows[0].token_hash == AuthService._hash_token(refresh)
        assert rows[0].revoked is False

    @pytest.mark.asyncio
    async def test_wrong_password_persists_nothing(self, service, session):
        assert await service.authenticate_and_persist("wrong", session) is None
        assert (await session.execute(select(RefreshTokenModel))).scalars().all() == []

    @pytest.mark.asyncio
    async def test_retries_after_reloading_db_password(self, service, session):
        """密码被 CLI 改过时，首次失败应自动 reload 后重试成功。"""
        row = (await session.execute(select(AuthConfigModel))).scalar_one()
        row.password_hash = _hash("cli-changed")
        await session.flush()

        assert await service.authenticate_and_persist("cli-changed", session) is not None


class TestVerifyAccessToken:
    @pytest.mark.asyncio
    async def test_accepts_freshly_issued_token(self, service):
        access, _ = service.authenticate(PASSWORD)
        assert service.verify_access_token(access) is True

    @pytest.mark.asyncio
    async def test_rejects_garbage(self, service):
        assert service.verify_access_token("not.a.jwt") is False

    @pytest.mark.asyncio
    async def test_rejects_token_signed_with_other_secret(self, service):
        forged = jwt.encode({"sub": "owner"}, "attacker-secret-" + "y" * 32, algorithm="HS256")
        assert service.verify_access_token(forged) is False

    @pytest.mark.asyncio
    async def test_rejects_expired_token(self, service):
        expired = jwt.encode(
            {"sub": "owner", "exp": int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp())},
            service.jwt_secret,
            algorithm="HS256",
        )
        assert service.verify_access_token(expired) is False

    @pytest.mark.asyncio
    async def test_access_token_carries_expected_claims(self, service):
        access, _ = service.authenticate(PASSWORD)
        payload = jwt.decode(access, service.jwt_secret, algorithms=["HS256"])
        assert payload["sub"] == "owner"
        assert payload["exp"] > payload["iat"]


# ══════════════════════════════════════════════
# Refresh Token Rotation
# ══════════════════════════════════════════════


class TestRefreshAccessToken:
    @pytest.mark.asyncio
    async def test_rotation_issues_new_pair_and_revokes_old(self, service, session):
        _, old_refresh = await service.authenticate_and_persist(PASSWORD, session)

        result = await service.refresh_access_token(old_refresh, session)
        assert result is not None
        _, new_refresh = result
        assert new_refresh != old_refresh

        old_hash = AuthService._hash_token(old_refresh)
        old_row = (
            await session.execute(select(RefreshTokenModel).where(RefreshTokenModel.token_hash == old_hash))
        ).scalar_one()
        assert old_row.revoked is True, "旧 token 必须被撤销"

    @pytest.mark.asyncio
    async def test_old_token_cannot_be_reused(self, service, session):
        _, old_refresh = await service.authenticate_and_persist(PASSWORD, session)
        await service.refresh_access_token(old_refresh, session)

        assert await service.refresh_access_token(old_refresh, session) is None

    @pytest.mark.asyncio
    async def test_unknown_token_returns_none(self, service, session):
        assert await service.refresh_access_token("never-issued", session) is None

    @pytest.mark.asyncio
    async def test_expired_token_rejected_and_marked_revoked(self, service, session):
        _, refresh = await service.authenticate_and_persist(PASSWORD, session)

        row = (await session.execute(select(RefreshTokenModel))).scalar_one()
        row.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
        await session.flush()

        assert await service.refresh_access_token(refresh, session) is None

        await session.refresh(row)
        assert row.revoked is True

    @pytest.mark.asyncio
    async def test_revoked_token_rejected(self, service, session):
        _, refresh = await service.authenticate_and_persist(PASSWORD, session)
        await service.revoke_refresh_token(refresh, session)

        assert await service.refresh_access_token(refresh, session) is None


class TestRevoke:
    @pytest.mark.asyncio
    async def test_revoke_single_token(self, service, session):
        _, refresh = await service.authenticate_and_persist(PASSWORD, session)
        assert await service.revoke_refresh_token(refresh, session) is True

    @pytest.mark.asyncio
    async def test_revoke_unknown_token_returns_false(self, service, session):
        assert await service.revoke_refresh_token("nope", session) is False

    @pytest.mark.asyncio
    async def test_revoke_all_counts_only_active(self, service, session):
        for _ in range(3):
            await service.authenticate_and_persist(PASSWORD, session)

        assert await service.revoke_all_refresh_tokens(session) == 3
        # 再次撤销时已无活跃 token
        assert await service.revoke_all_refresh_tokens(session) == 0


# ══════════════════════════════════════════════
# 密码修改 / 重置
# ══════════════════════════════════════════════


class TestUpdatePassword:
    @pytest.mark.asyncio
    async def test_updates_hash_and_invalidates_sessions(self, service, session):
        await service.authenticate_and_persist(PASSWORD, session)

        assert await service.update_password(PASSWORD, "brand-new-pw", session) is True

        # 新密码可用，旧密码失效
        assert service.authenticate("brand-new-pw") is not None
        assert service.authenticate(PASSWORD) is None

        # 所有 refresh token 被撤销
        rows = (await session.execute(select(RefreshTokenModel))).scalars().all()
        assert all(r.revoked for r in rows)

    @pytest.mark.asyncio
    async def test_wrong_old_password_rejected(self, service, session):
        assert await service.update_password("wrong-old", "new", session) is False
        # 原密码仍有效
        assert service.authenticate(PASSWORD) is not None

    @pytest.mark.asyncio
    async def test_persists_new_hash_to_db(self, service, session):
        await service.update_password(PASSWORD, "db-persisted", session)
        row = (await session.execute(select(AuthConfigModel))).scalar_one()
        assert bcrypt.checkpw(b"db-persisted", row.password_hash.encode())


class TestResetPassword:
    @pytest.mark.asyncio
    async def test_resets_without_old_password(self, service, session):
        await service.reset_password("forced-reset", session)

        assert service.authenticate("forced-reset") is not None
        assert service.authenticate(PASSWORD) is None

    @pytest.mark.asyncio
    async def test_reset_revokes_all_tokens(self, service, session):
        await service.authenticate_and_persist(PASSWORD, session)
        await service.reset_password("forced", session)

        rows = (await session.execute(select(RefreshTokenModel))).scalars().all()
        assert all(r.revoked for r in rows)


# ══════════════════════════════════════════════
# 配置更新 & 清理
# ══════════════════════════════════════════════


class TestUpdateConfig:
    @pytest.mark.asyncio
    async def test_updates_allowed_fields(self, service, session):
        await service.update_config(session, enabled=False, access_token_expire_hours=9.0)

        assert service.config.enabled is False
        assert service.access_token_expire_hours == 9.0

        row = (await session.execute(select(AuthConfigModel))).scalar_one()
        assert row.enabled is False

    @pytest.mark.asyncio
    async def test_ignores_unknown_fields(self, service, session):
        await service.update_config(session, password_hash="hacked", bogus="x")
        # password_hash 不在白名单内，应保持原值
        assert service.authenticate(PASSWORD) is not None

    @pytest.mark.asyncio
    async def test_no_op_when_nothing_allowed(self, service, session):
        await service.update_config(session, bogus="x")  # 不应抛异常


class TestCleanupExpiredTokens:
    @pytest.mark.asyncio
    async def test_deletes_revoked_and_expired_only(self, service, session):
        # 活跃 token
        await service.authenticate_and_persist(PASSWORD, session)
        # 已撤销
        _, revoked = await service.authenticate_and_persist(PASSWORD, session)
        await service.revoke_refresh_token(revoked, session)
        # 已过期
        _, expired = await service.authenticate_and_persist(PASSWORD, session)
        row = (
            await session.execute(
                select(RefreshTokenModel).where(
                    RefreshTokenModel.token_hash == AuthService._hash_token(expired)
                )
            )
        ).scalar_one()
        row.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
        await session.flush()

        assert await service.cleanup_expired_tokens(session) == 2

        remaining = (await session.execute(select(RefreshTokenModel))).scalars().all()
        assert len(remaining) == 1
        assert remaining[0].revoked is False

    @pytest.mark.asyncio
    async def test_returns_zero_when_nothing_to_clean(self, service, session):
        await service.authenticate_and_persist(PASSWORD, session)
        assert await service.cleanup_expired_tokens(session) == 0


class TestHashToken:
    def test_is_deterministic_sha256_hex(self):
        digest = AuthService._hash_token("token")
        assert digest == AuthService._hash_token("token")
        assert len(digest) == 64
        assert digest != "token"

    def test_differs_for_different_input(self):
        assert AuthService._hash_token("a") != AuthService._hash_token("b")
