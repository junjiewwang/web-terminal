"""数据库迁移兼容性测试。"""

from sqlalchemy import create_engine, text

from src.models.database import reset_hosts_table_if_legacy_enum_found, serialize_default_value
from src.models.host import Host


def test_serialize_default_value_uses_enum_names() -> None:
    assert serialize_default_value(Host.__table__.c.host_type.default) == "ROOT"
    assert serialize_default_value(Host.__table__.c.auth_type.default) == "KEY"


def test_reset_hosts_table_if_legacy_enum_found_clears_legacy_rows() -> None:
    engine = create_engine("sqlite:///:memory:")

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE hosts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    host_type TEXT NOT NULL,
                    auth_type TEXT NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO hosts (name, host_type, auth_type) VALUES
                    ('legacy-node', 'DIRECT', 'KEY'),
                    ('current-node', 'ROOT', 'PASSWORD')
                """
            )
        )

        reset_hosts_table_if_legacy_enum_found(conn)

        remaining = conn.execute(text("SELECT COUNT(*) FROM hosts")).scalar_one()

    assert remaining == 0


def test_reset_hosts_table_if_legacy_enum_found_keeps_current_rows() -> None:
    engine = create_engine("sqlite:///:memory:")

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE hosts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    host_type TEXT NOT NULL,
                    auth_type TEXT NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO hosts (name, host_type, auth_type) VALUES
                    ('root-node', 'ROOT', 'KEY'),
                    ('nested-node', 'NESTED', 'PASSWORD')
                """
            )
        )

        reset_hosts_table_if_legacy_enum_found(conn)

        remaining = conn.execute(text("SELECT COUNT(*) FROM hosts")).scalar_one()

    assert remaining == 2
