"""Snippet / Settings / Events API 端点测试。

这些路由通过模块级变量注入依赖（snippet_registry、_auth_service），
测试用 monkeypatch 注入替身，并覆盖未初始化时的 503 分支。
"""

from __future__ import annotations

import base64
import gzip

import pytest
import pytest_asyncio
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.api import events as events_api
from src.api import settings as settings_api
from src.api import snippets as snippets_api
from src.models.auth import AuthConfigModel
from src.models.database import get_db
from src.models.host import Base
from src.services.auth_service import AuthService
from src.services.event_service import AgentEvent, EventBus, EventType
from src.services.snippet_registry import SnippetRegistry

SCRIPT = "#!/bin/bash\nes() { curl -s localhost:9200/_cat/indices; }\n"

CONFIG = {
    "domains": [
        {
            "id": "es",
            "name": "Elasticsearch",
            "icon": "🔍",
            "description": "ES 排查",
            "script_file": "ts-es.sh",
            "default_timeout": 45,
            "tags": ["search"],
            "commands": [
                {
                    "id": "health",
                    "name": "集群健康",
                    "description": "查看集群健康",
                    "syntax": "health <host>",
                    "template": "curl -s {{host}}/_cluster/health",
                    "timeout": 10,
                    "params": [
                        {"name": "host", "description": "ES 地址", "required": True},
                    ],
                }
            ],
        }
    ]
}


@pytest.fixture
def registry(tmp_path) -> SnippetRegistry:
    yaml_path = tmp_path / "snippets.yaml"
    yaml_path.write_text(yaml.safe_dump(CONFIG, allow_unicode=True), encoding="utf-8")
    (tmp_path / "ts-es.sh").write_text(SCRIPT, encoding="utf-8")

    reg = SnippetRegistry()
    reg.load_from_yaml(yaml_path)
    return reg


# ══════════════════════════════════════════════
# /api/snippets
# ══════════════════════════════════════════════


@pytest.fixture
def snippet_client(registry, monkeypatch):
    monkeypatch.setattr(snippets_api, "snippet_registry", registry)
    app = FastAPI()
    app.include_router(snippets_api.router)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def uninitialised_snippet_client(monkeypatch):
    monkeypatch.setattr(snippets_api, "snippet_registry", None)
    app = FastAPI()
    app.include_router(snippets_api.router)
    with TestClient(app) as c:
        yield c


class TestListSnippetDomains:
    def test_returns_summaries(self, snippet_client):
        resp = snippet_client.get("/api/snippets")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["id"] == "es"
        assert body[0]["command_count"] == 1

    def test_503_when_registry_missing(self, uninitialised_snippet_client):
        assert uninitialised_snippet_client.get("/api/snippets").status_code == 503


class TestGetSnippetDomain:
    def test_returns_domain_with_commands_and_params(self, snippet_client):
        body = snippet_client.get("/api/snippets/es").json()

        assert body["id"] == "es"
        assert body["default_timeout"] == 45
        assert len(body["commands"]) == 1

        cmd = body["commands"][0]
        assert cmd["id"] == "health"
        assert cmd["timeout"] == 10
        assert cmd["params"][0] == {
            "name": "host",
            "description": "ES 地址",
            "default": "",
            "required": True,
        }

    def test_unknown_domain_returns_404(self, snippet_client):
        resp = snippet_client.get("/api/snippets/nope")
        assert resp.status_code == 404
        assert "nope" in resp.json()["detail"]

    def test_503_when_registry_missing(self, uninitialised_snippet_client):
        assert uninitialised_snippet_client.get("/api/snippets/es").status_code == 503


class TestGetSnippetScript:
    def test_returns_probe_and_loader(self, snippet_client):
        body = snippet_client.get("/api/snippets/es/script").json()

        assert body["domain_id"] == "es"
        assert SnippetRegistry.PROBE_YES in body["probe_command"]
        assert SnippetRegistry.PROBE_NO in body["probe_command"]
        assert "/tmp/ts-es.sh" in body["heredoc_loader"]

    def test_loader_payload_decodes_back_to_script(self, snippet_client):
        """压缩注入命令中的 base64 应能还原出原始脚本。"""
        loader = snippet_client.get("/api/snippets/es/script").json()["heredoc_loader"]

        blob = loader.split("'")[1]
        restored = gzip.decompress(base64.b64decode(blob)).decode()

        assert restored == SCRIPT

    def test_unknown_domain_returns_404(self, snippet_client):
        assert snippet_client.get("/api/snippets/nope/script").status_code == 404

    def test_domain_without_script_returns_404(self, tmp_path, monkeypatch):
        config = {"domains": [{"id": "bare", "name": "Bare", "script_file": "",
                               "commands": [{"id": "c", "name": "C", "template": "x"}]}]}
        yaml_path = tmp_path / "snippets.yaml"
        yaml_path.write_text(yaml.safe_dump(config), encoding="utf-8")
        reg = SnippetRegistry()
        reg.load_from_yaml(yaml_path)

        monkeypatch.setattr(snippets_api, "snippet_registry", reg)
        app = FastAPI()
        app.include_router(snippets_api.router)
        with TestClient(app) as c:
            assert c.get("/api/snippets/bare/script").status_code == 404


# ══════════════════════════════════════════════
# /api/settings
# ══════════════════════════════════════════════


@pytest_asyncio.fixture
async def auth_service_and_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    session = factory()
    session.add(
        AuthConfigModel(
            id=1,
            enabled=True,
            password_hash="hashed-value",
            jwt_secret="super-secret-value-1234567890",
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
def settings_client(auth_service_and_session, monkeypatch):
    svc, session = auth_service_and_session
    monkeypatch.setattr(settings_api, "_auth_service", svc)

    async def override_get_db():
        yield session

    app = FastAPI()
    app.include_router(settings_api.router)
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c


class TestGetAuthSettings:
    def test_returns_masked_config(self, settings_client):
        body = settings_client.get("/api/settings/auth").json()

        assert body["enabled"] is True
        assert body["has_password"] is True
        assert body["access_token_expire_hours"] == 2.0
        assert body["refresh_token_expire_days"] == 7

    def test_never_leaks_full_secret_or_hash(self, settings_client):
        resp = settings_client.get("/api/settings/auth")

        assert "super-secret-value-1234567890" not in resp.text
        assert "hashed-value" not in resp.text
        assert resp.json()["jwt_secret_prefix"] == "super-se..."

    def test_short_secret_fully_masked(self, auth_service_and_session, monkeypatch):
        svc, session = auth_service_and_session
        svc._config.jwt_secret = "short"
        monkeypatch.setattr(settings_api, "_auth_service", svc)

        app = FastAPI()
        app.include_router(settings_api.router)
        with TestClient(app) as c:
            assert c.get("/api/settings/auth").json()["jwt_secret_prefix"] == "***"

    def test_503_when_service_missing(self, monkeypatch):
        monkeypatch.setattr(settings_api, "_auth_service", None)
        app = FastAPI()
        app.include_router(settings_api.router)
        with TestClient(app) as c:
            assert c.get("/api/settings/auth").status_code == 503


class TestUpdateAuthSettings:
    def test_updates_enabled_flag(self, settings_client):
        resp = settings_client.put("/api/settings/auth", json={"enabled": False})

        assert resp.status_code == 204
        assert settings_client.get("/api/settings/auth").json()["enabled"] is False

    def test_empty_body_is_noop(self, settings_client):
        assert settings_client.put("/api/settings/auth", json={}).status_code == 204

    def test_rejects_out_of_range_values(self, settings_client):
        assert settings_client.put(
            "/api/settings/auth", json={"access_token_expire_hours": 0}
        ).status_code == 422
        assert settings_client.put(
            "/api/settings/auth", json={"refresh_token_expire_days": 91}
        ).status_code == 422


class TestGetDatabaseInfo:
    def test_returns_type_and_url(self, settings_client):
        body = settings_client.get("/api/settings/database").json()

        assert body["type"] in {"sqlite", "mysql"}
        assert body["url"]

    def test_mysql_url_is_masked(self, settings_client, monkeypatch):
        monkeypatch.setattr(
            settings_api,
            "get_db_info",
            lambda: {"type": "mysql", "url": "mysql://user:***@db:3306/app"},
        )

        body = settings_client.get("/api/settings/database").json()

        assert "***" in body["url"]


# ══════════════════════════════════════════════
# /api/events
# ══════════════════════════════════════════════


@pytest.fixture
def events_client(monkeypatch):
    bus = EventBus()
    monkeypatch.setattr(events_api, "event_bus", bus)
    app = FastAPI()
    app.include_router(events_api.router)
    with TestClient(app) as c:
        yield c, bus


class TestEventHistory:
    def test_empty_initially(self, events_client):
        client, _bus = events_client
        assert client.get("/api/events/history").json() == []

    @pytest.mark.asyncio
    async def test_returns_published_events(self, events_client):
        client, bus = events_client
        await bus.publish(
            AgentEvent(
                event_type=EventType.COMMAND_START,
                session_id="s1",
                host_name="web-1",
                data={"command": "ls"},
            )
        )

        body = client.get("/api/events/history").json()

        assert len(body) == 1
        assert body[0]["session_id"] == "s1"
        assert body[0]["data"]["command"] == "ls"


class TestEventSerialization:
    def test_event_to_json_keeps_unicode(self):
        event = AgentEvent(
            event_type=EventType.COMMAND_ERROR,
            session_id="s",
            host_name="主机",
            data={"error": "连接失败"},
        )

        payload = events_api._event_to_json(event)

        assert "连接失败" in payload
        assert "\\u" not in payload
