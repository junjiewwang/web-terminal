"""认证 REST API 端点测试 — 登录 / 刷新 / 注销 / 改密。"""

from __future__ import annotations

import bcrypt
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.api import auth as auth_api
from src.models.auth import AuthConfigModel
from src.models.database import get_db
from src.models.host import Base
from src.services.auth_service import AuthService

PASSWORD = "correct-horse-battery"
JWT_SECRET = "test-secret-" + "x" * 32
FAST_ROUNDS = 4


@pytest_asyncio.fixture
async def svc_and_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    session = factory()
    session.add(
        AuthConfigModel(
            id=1,
            enabled=True,
            password_hash=bcrypt.hashpw(
                PASSWORD.encode(), bcrypt.gensalt(rounds=FAST_ROUNDS)
            ).decode(),
            jwt_secret=JWT_SECRET,
            access_token_expire_hours=2.0,
            refresh_token_expire_days=7,
        )
    )
    await session.flush()

    svc = AuthService()
    await svc.init_from_db(session)

    yield svc, session

    await session.close()
    await engine.dispose()


@pytest.fixture
def client(svc_and_session, monkeypatch):
    svc, session = svc_and_session
    monkeypatch.setattr(auth_api, "_auth_service", svc)

    async def override_get_db():
        yield session

    app = FastAPI()
    app.include_router(auth_api.router)
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c


@pytest.fixture
def no_service_client(monkeypatch):
    monkeypatch.setattr(auth_api, "_auth_service", None)
    app = FastAPI()
    app.include_router(auth_api.router)
    with TestClient(app) as c:
        yield c


# ══════════════════════════════════════════════
# /status
# ══════════════════════════════════════════════


class TestAuthStatus:
    def test_required_when_auth_enabled(self, client):
        assert client.get("/api/auth/status").json() == {"auth_required": True}

    def test_not_required_when_service_absent(self, no_service_client, monkeypatch):
        monkeypatch.delenv("WETTY_API_TOKEN", raising=False)
        assert no_service_client.get("/api/auth/status").json() == {"auth_required": False}

    def test_required_when_env_token_set(self, no_service_client, monkeypatch):
        monkeypatch.setenv("WETTY_API_TOKEN", "some-token")
        assert no_service_client.get("/api/auth/status").json() == {"auth_required": True}

    def test_not_required_when_auth_disabled(self, svc_and_session, monkeypatch):
        svc, _session = svc_and_session
        svc._config.enabled = False
        monkeypatch.setattr(auth_api, "_auth_service", svc)
        monkeypatch.delenv("WETTY_API_TOKEN", raising=False)

        app = FastAPI()
        app.include_router(auth_api.router)
        with TestClient(app) as c:
            assert c.get("/api/auth/status").json() == {"auth_required": False}


# ══════════════════════════════════════════════
# /login
# ══════════════════════════════════════════════


class TestLogin:
    def test_correct_password_returns_tokens(self, client):
        resp = client.post("/api/auth/login", json={"password": PASSWORD})

        assert resp.status_code == 200
        body = resp.json()
        assert body["access_token"]
        assert body["refresh_token"]
        assert body["token_type"] == "bearer"
        assert body["expires_at"]

    def test_wrong_password_returns_401(self, client):
        resp = client.post("/api/auth/login", json={"password": "wrong"})

        assert resp.status_code == 401
        assert resp.json()["detail"] == "密码错误"

    def test_empty_password_returns_422(self, client):
        assert client.post("/api/auth/login", json={"password": ""}).status_code == 422

    def test_missing_field_returns_422(self, client):
        assert client.post("/api/auth/login", json={}).status_code == 422

    def test_response_does_not_echo_password(self, client):
        resp = client.post("/api/auth/login", json={"password": PASSWORD})
        assert PASSWORD not in resp.text

    def test_503_when_service_missing(self, no_service_client):
        resp = no_service_client.post("/api/auth/login", json={"password": "x"})
        assert resp.status_code == 503


# ══════════════════════════════════════════════
# /refresh
# ══════════════════════════════════════════════


class TestRefresh:
    def test_valid_token_rotates(self, client):
        login = client.post("/api/auth/login", json={"password": PASSWORD}).json()

        resp = client.post("/api/auth/refresh", json={"refresh_token": login["refresh_token"]})

        assert resp.status_code == 200
        assert resp.json()["refresh_token"] != login["refresh_token"], "必须轮换"

    def test_old_token_rejected_after_rotation(self, client):
        login = client.post("/api/auth/login", json={"password": PASSWORD}).json()
        old = login["refresh_token"]
        client.post("/api/auth/refresh", json={"refresh_token": old})

        resp = client.post("/api/auth/refresh", json={"refresh_token": old})

        assert resp.status_code == 401

    def test_unknown_token_returns_401(self, client):
        resp = client.post("/api/auth/refresh", json={"refresh_token": "never-issued"})
        assert resp.status_code == 401

    def test_empty_token_returns_422(self, client):
        assert client.post("/api/auth/refresh", json={"refresh_token": ""}).status_code == 422


# ══════════════════════════════════════════════
# /logout
# ══════════════════════════════════════════════


class TestLogout:
    def test_revokes_token_and_returns_204(self, client):
        login = client.post("/api/auth/login", json={"password": PASSWORD}).json()

        assert client.post(
            "/api/auth/logout", json={"refresh_token": login["refresh_token"]}
        ).status_code == 204

        # 注销后该 token 不能再用于刷新
        assert client.post(
            "/api/auth/refresh", json={"refresh_token": login["refresh_token"]}
        ).status_code == 401

    def test_unknown_token_is_idempotent_204(self, client):
        """注销不存在的 token 也返回 204（幂等）。"""
        assert client.post(
            "/api/auth/logout", json={"refresh_token": "never-issued"}
        ).status_code == 204


# ══════════════════════════════════════════════
# /password
# ══════════════════════════════════════════════


class TestChangePassword:
    def test_correct_old_password_updates(self, client):
        resp = client.put(
            "/api/auth/password",
            json={"old_password": PASSWORD, "new_password": "brand-new-secret"},
        )

        assert resp.status_code == 204
        assert client.post("/api/auth/login", json={"password": "brand-new-secret"}).status_code == 200
        assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 401

    def test_wrong_old_password_returns_400(self, client):
        resp = client.put(
            "/api/auth/password",
            json={"old_password": "wrong", "new_password": "brand-new-secret"},
        )

        assert resp.status_code == 400
        assert resp.json()["detail"] == "旧密码不正确"

    def test_short_new_password_returns_422(self, client):
        resp = client.put(
            "/api/auth/password",
            json={"old_password": PASSWORD, "new_password": "short"},
        )
        assert resp.status_code == 422

    def test_change_invalidates_existing_refresh_tokens(self, client):
        login = client.post("/api/auth/login", json={"password": PASSWORD}).json()

        client.put(
            "/api/auth/password",
            json={"old_password": PASSWORD, "new_password": "brand-new-secret"},
        )

        resp = client.post("/api/auth/refresh", json={"refresh_token": login["refresh_token"]})
        assert resp.status_code == 401, "改密后旧会话必须失效"
