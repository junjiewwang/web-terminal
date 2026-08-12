"""REST API 端点测试（hosts / credentials）。

使用 FastAPI TestClient + 内存 SQLite，覆盖路由层的
状态码、错误映射与序列化行为。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.api import credentials as credentials_api
from src.api import hosts as hosts_api
from src.models.database import get_db
from src.models.host import Base


@pytest_asyncio.fixture
async def engine(monkeypatch):
    """内存 SQLite + 固定加密密钥。"""
    from cryptography.fernet import Fernet

    from src.utils import security

    monkeypatch.setattr(security, "_fernet", None)
    monkeypatch.setattr(security, "_FERNET_KEY", None)
    monkeypatch.setenv("WETTY_ENCRYPTION_KEY", Fernet.generate_key().decode())

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
def client(engine):
    """挂载 hosts + credentials 路由，并覆盖 get_db 依赖。

    注意：整个 TestClient 生命周期共用一个 AsyncSession/连接，
    否则 :memory: 数据库在连接关闭后即丢失。
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    session_holder: dict[str, object] = {}

    async def override_get_db():
        if "session" not in session_holder:
            session_holder["session"] = factory()
        session = session_holder["session"]
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    app = FastAPI()
    app.include_router(hosts_api.router)
    app.include_router(credentials_api.router)
    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as c:
        yield c


def _host_payload(name: str = "web-1", **overrides) -> dict:
    payload = {
        "name": name,
        "hostname": f"{name}.example.com",
        "port": 22,
        "username": "deploy",
        "host_type": "root",
        "auth_type": "key",
    }
    payload.update(overrides)
    return payload


# ══════════════════════════════════════════════
# /api/hosts
# ══════════════════════════════════════════════


class TestListHosts:
    def test_empty_initially(self, client):
        resp = client.get("/api/hosts")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_created_hosts(self, client):
        client.post("/api/hosts", json=_host_payload("web-1"))
        client.post("/api/hosts", json=_host_payload("web-2"))

        body = client.get("/api/hosts").json()

        assert {h["name"] for h in body} == {"web-1", "web-2"}

    def test_filters_by_tag(self, client):
        client.post("/api/hosts", json=_host_payload("tagged", tags=["prod"]))
        client.post("/api/hosts", json=_host_payload("untagged", tags=["dev"]))

        body = client.get("/api/hosts", params={"tag": "prod"}).json()

        assert [h["name"] for h in body] == ["tagged"]

    def test_nests_children_under_parent(self, client):
        parent = client.post("/api/hosts", json=_host_payload("bastion")).json()
        client.post(
            "/api/hosts",
            json=_host_payload(
                "inner",
                host_type="nested",
                parent_id=parent["id"],
                entry={"type": "menu_send", "value": "10.0.0.8"},
            ),
        )

        body = client.get("/api/hosts").json()

        assert len(body) == 1, "嵌套主机不应出现在顶层"
        assert [c["name"] for c in body[0]["children"]] == ["inner"]


class TestCreateHost:
    def test_returns_201_and_body(self, client):
        resp = client.post("/api/hosts", json=_host_payload("web-1"))

        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "web-1"
        assert body["id"] is not None

    def test_duplicate_name_returns_409(self, client):
        client.post("/api/hosts", json=_host_payload("dup"))
        resp = client.post("/api/hosts", json=_host_payload("dup"))

        assert resp.status_code == 409
        assert "dup" in resp.json()["detail"]

    def test_invalid_port_returns_422(self, client):
        resp = client.post("/api/hosts", json=_host_payload("bad", port=99999))
        assert resp.status_code == 422

    def test_missing_required_field_returns_422(self, client):
        resp = client.post("/api/hosts", json={"name": "incomplete"})
        assert resp.status_code == 422

    def test_never_returns_password(self, client):
        resp = client.post(
            "/api/hosts",
            json=_host_payload("secret-host", auth_type="password", password="p@ss"),
        )

        assert resp.status_code == 201
        assert "p@ss" not in resp.text
        assert "password_encrypted" not in resp.json()


class TestGetHost:
    def test_returns_host_by_id(self, client):
        created = client.post("/api/hosts", json=_host_payload("web-1")).json()

        resp = client.get(f"/api/hosts/{created['id']}")

        assert resp.status_code == 200
        assert resp.json()["name"] == "web-1"

    def test_unknown_id_returns_404(self, client):
        resp = client.get("/api/hosts/9999")
        assert resp.status_code == 404
        assert "9999" in resp.json()["detail"]


class TestUpdateHost:
    def test_partial_update_applies(self, client):
        created = client.post("/api/hosts", json=_host_payload("web-1")).json()

        resp = client.put(f"/api/hosts/{created['id']}", json={"description": "updated"})

        assert resp.status_code == 200
        assert resp.json()["description"] == "updated"
        assert resp.json()["name"] == "web-1", "未提供的字段不应被改动"

    def test_unknown_id_returns_404(self, client):
        assert client.put("/api/hosts/9999", json={"description": "x"}).status_code == 404


class TestDeleteHost:
    def test_returns_204_and_removes(self, client):
        created = client.post("/api/hosts", json=_host_payload("doomed")).json()

        assert client.delete(f"/api/hosts/{created['id']}").status_code == 204
        assert client.get(f"/api/hosts/{created['id']}").status_code == 404

    def test_unknown_id_returns_404(self, client):
        assert client.delete("/api/hosts/9999").status_code == 404


class TestHostsYamlExport:
    def test_export_returns_yaml_document(self, client):
        client.post("/api/hosts", json=_host_payload("web-1"))

        resp = client.get("/api/hosts/yaml")

        assert resp.status_code == 200
        assert "web-1" in resp.text

    def test_import_rejects_non_yaml_extension(self, client):
        resp = client.post(
            "/api/hosts/import",
            files={"file": ("hosts.txt", b"hosts: []", "text/plain")},
        )
        assert resp.status_code == 400


# ══════════════════════════════════════════════
# /api/credentials
# ══════════════════════════════════════════════


class TestListCredentials:
    def test_empty_initially(self, client):
        assert client.get("/api/credentials").json() == []

    def test_lists_created_credentials(self, client):
        client.post("/api/credentials", json={"name": "c1", "password": "p"})

        body = client.get("/api/credentials").json()

        assert len(body) == 1
        assert body[0]["name"] == "c1"
        assert body[0]["has_password"] is True

    def test_response_never_contains_password(self, client):
        client.post("/api/credentials", json={"name": "c1", "password": "super-secret"})

        resp = client.get("/api/credentials")

        assert "super-secret" not in resp.text
        assert "password_encrypted" not in resp.text

    def test_names_endpoint_returns_name_and_description(self, client):
        client.post("/api/credentials", json={"name": "c1", "password": "p", "description": "d"})

        body = client.get("/api/credentials/names").json()

        assert body == [{"name": "c1", "description": "d"}]


class TestCreateCredential:
    def test_returns_201(self, client):
        resp = client.post("/api/credentials", json={"name": "c1", "password": "p"})

        assert resp.status_code == 201
        assert resp.json()["name"] == "c1"
        assert resp.json()["ref_count"] == 0

    def test_duplicate_name_returns_409(self, client):
        client.post("/api/credentials", json={"name": "dup", "password": "p"})
        resp = client.post("/api/credentials", json={"name": "dup", "password": "p"})

        assert resp.status_code == 409

    def test_empty_password_returns_422(self, client):
        resp = client.post("/api/credentials", json={"name": "c1", "password": ""})
        assert resp.status_code == 422


class TestUpdateCredential:
    def test_updates_description(self, client):
        created = client.post("/api/credentials", json={"name": "c1", "password": "p"}).json()

        resp = client.put(f"/api/credentials/{created['id']}", json={"description": "new"})

        assert resp.status_code == 204
        assert client.get("/api/credentials").json()[0]["description"] == "new"

    def test_empty_body_returns_400(self, client):
        created = client.post("/api/credentials", json={"name": "c1", "password": "p"}).json()

        resp = client.put(f"/api/credentials/{created['id']}", json={})

        assert resp.status_code == 400

    def test_unknown_id_returns_404(self, client):
        assert client.put("/api/credentials/9999", json={"description": "x"}).status_code == 404


class TestDeleteCredential:
    def test_deletes_unreferenced(self, client):
        created = client.post("/api/credentials", json={"name": "c1", "password": "p"}).json()

        assert client.delete(f"/api/credentials/{created['id']}").status_code == 204
        assert client.get("/api/credentials").json() == []

    def test_unknown_id_returns_404(self, client):
        assert client.delete("/api/credentials/9999").status_code == 404

    def test_referenced_credential_returns_409(self, client):
        created = client.post("/api/credentials", json={"name": "in-use", "password": "p"}).json()
        client.post(
            "/api/hosts",
            json=_host_payload("web-1", auth_type="password", credential_ref="in-use"),
        )

        resp = client.delete(f"/api/credentials/{created['id']}")

        assert resp.status_code == 409
        assert "web-1" in resp.json()["detail"]
