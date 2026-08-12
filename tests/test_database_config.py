"""数据库配置与自动迁移测试。

补充 test_database_migrations.py 未覆盖的分支：URL 构建、类型检测、
引擎参数、脱敏输出、缺列自动补齐。
"""

from __future__ import annotations

import enum

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect, text

from src.models import database as db
from src.models.database import (
    _auto_migrate_columns,
    _build_database_url,
    _create_engine,
    _detect_db_type,
    get_db_info,
    get_db_type,
    is_mysql,
    is_sqlite,
    serialize_default_value,
)


# ══════════════════════════════════════════════
# URL 构建
# ══════════════════════════════════════════════


class TestBuildDatabaseUrl:
    def test_env_url_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "mysql+aiomysql://u:p@host/db")
        assert _build_database_url() == "mysql+aiomysql://u:p@host/db"

    def test_whitespace_is_stripped(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "  mysql+aiomysql://u:p@h/d  ")
        assert _build_database_url() == "mysql+aiomysql://u:p@h/d"

    def test_blank_env_falls_back_to_sqlite(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "   ")
        assert _build_database_url().startswith("sqlite+aiosqlite:///")

    def test_unset_env_falls_back_to_sqlite(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        url = _build_database_url()
        assert url.startswith("sqlite+aiosqlite:///")
        assert url.endswith("wetty_mcp.db")


# ══════════════════════════════════════════════
# 类型检测
# ══════════════════════════════════════════════


class TestDetectDbType:
    @pytest.mark.parametrize(
        "url",
        ["mysql://u:p@h/d", "mysql+aiomysql://u:p@h/d", "mysql+pymysql://u:p@h/d"],
    )
    def test_mysql_variants(self, url):
        assert _detect_db_type(url) == "mysql"

    @pytest.mark.parametrize(
        "url", ["sqlite:///x.db", "sqlite+aiosqlite:///x.db", "postgresql://h/d"]
    )
    def test_non_mysql_defaults_to_sqlite(self, url):
        assert _detect_db_type(url) == "sqlite"


class TestTypeHelpers:
    def test_helpers_agree_with_current_type(self):
        current = get_db_type()
        assert current in {"sqlite", "mysql"}
        assert is_sqlite() == (current == "sqlite")
        assert is_mysql() == (current == "mysql")

    def test_exactly_one_helper_true(self):
        assert is_sqlite() != is_mysql()


# ══════════════════════════════════════════════
# 引擎创建
# ══════════════════════════════════════════════


class TestCreateEngine:
    def test_sqlite_engine_created(self):
        engine = _create_engine("sqlite+aiosqlite:///:memory:")
        assert engine is not None

    def test_mysql_engine_gets_pool_settings(self):
        """MySQL 需要连接池配置，SQLite 不需要。"""
        engine = _create_engine("mysql+aiomysql://u:p@localhost/db")
        assert engine.pool.size() == 5


# ══════════════════════════════════════════════
# 连接信息脱敏
# ══════════════════════════════════════════════


class TestGetDbInfo:
    def test_returns_type_and_url(self):
        info = get_db_info()
        assert info["type"] in {"sqlite", "mysql"}
        assert info["url"]

    def test_mysql_password_masked(self, monkeypatch):
        monkeypatch.setattr(db, "_db_type", "mysql")
        monkeypatch.setattr(db, "_database_url", "mysql+aiomysql://root:hunter2@dbhost:3306/app")

        info = get_db_info()

        assert "hunter2" not in info["url"], "密码必须脱敏"
        assert "***" in info["url"]
        assert "root" in info["url"]
        assert "dbhost:3306/app" in info["url"]

    def test_sqlite_url_returned_verbatim(self, monkeypatch):
        monkeypatch.setattr(db, "_db_type", "sqlite")
        monkeypatch.setattr(db, "_database_url", "sqlite+aiosqlite:////data/x.db")

        assert get_db_info() == {"type": "sqlite", "url": "sqlite+aiosqlite:////data/x.db"}


# ══════════════════════════════════════════════
# 默认值序列化
# ══════════════════════════════════════════════


class _Colour(enum.Enum):
    RED = "red_value"


class TestSerializeDefaultValue:
    def test_none_default(self):
        assert serialize_default_value(None) is None

    def test_enum_uses_name_not_value(self):
        """DB 存的是枚举 name，序列化必须一致。"""
        default = type("D", (), {"arg": _Colour.RED})()
        assert serialize_default_value(default) == "RED"

    def test_callable_default_returns_none(self):
        default = type("D", (), {"arg": lambda ctx: 1})()
        assert serialize_default_value(default) is None

    def test_scalar_stringified(self):
        assert serialize_default_value(type("D", (), {"arg": 42})()) == "42"
        assert serialize_default_value(type("D", (), {"arg": "txt"})()) == "txt"

    def test_object_without_arg(self):
        assert serialize_default_value(object()) is None


# ══════════════════════════════════════════════
# 自动补列迁移
# ══════════════════════════════════════════════


class TestAutoMigrateColumns:
    def test_adds_missing_nullable_column(self, monkeypatch):
        """老表缺列时应 ALTER TABLE 补齐，而不是报错。"""
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE widget (id INTEGER PRIMARY KEY, name TEXT)"))

            metadata = MetaData()
            Table(
                "widget",
                metadata,
                Column("id", Integer, primary_key=True),
                Column("name", String(50)),
                Column("note", String(200), nullable=True),  # 新增列
            )
            monkeypatch.setattr(db.Base, "metadata", metadata)

            _auto_migrate_columns(conn)

            cols = {c["name"] for c in inspect(conn).get_columns("widget")}

        assert "note" in cols

    def test_existing_columns_untouched(self, monkeypatch):
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE widget (id INTEGER PRIMARY KEY, name TEXT)"))
            conn.execute(text("INSERT INTO widget (id, name) VALUES (1, 'keep-me')"))

            metadata = MetaData()
            Table(
                "widget",
                metadata,
                Column("id", Integer, primary_key=True),
                Column("name", String(50)),
            )
            monkeypatch.setattr(db.Base, "metadata", metadata)

            _auto_migrate_columns(conn)

            value = conn.execute(text("SELECT name FROM widget WHERE id=1")).scalar_one()

        assert value == "keep-me", "已有数据不应被迁移破坏"

    def test_missing_table_is_skipped(self, monkeypatch):
        """元数据里有但库中没有的表应被跳过，不抛异常。"""
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            metadata = MetaData()
            Table("never_created", metadata, Column("id", Integer, primary_key=True))
            monkeypatch.setattr(db.Base, "metadata", metadata)

            _auto_migrate_columns(conn)  # 不应抛出

    def test_not_null_column_without_default_added_as_nullable(self, monkeypatch):
        """NOT NULL 但无默认值时必须降级为 NULL，否则 ALTER 会失败。"""
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE widget (id INTEGER PRIMARY KEY)"))

            metadata = MetaData()
            Table(
                "widget",
                metadata,
                Column("id", Integer, primary_key=True),
                Column("required", String(20), nullable=False),
            )
            monkeypatch.setattr(db.Base, "metadata", metadata)

            _auto_migrate_columns(conn)

            info = {c["name"]: c for c in inspect(conn).get_columns("widget")}

        assert "required" in info
        assert info["required"]["nullable"] is True

    def test_not_null_with_default_keeps_constraint(self, monkeypatch):
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE widget (id INTEGER PRIMARY KEY)"))

            metadata = MetaData()
            Table(
                "widget",
                metadata,
                Column("id", Integer, primary_key=True),
                Column("flag", String(10), nullable=False, default="on"),
            )
            monkeypatch.setattr(db.Base, "metadata", metadata)

            _auto_migrate_columns(conn)

            conn.execute(text("INSERT INTO widget (id) VALUES (1)"))
            value = conn.execute(text("SELECT flag FROM widget WHERE id=1")).scalar_one()

        assert value == "on"
