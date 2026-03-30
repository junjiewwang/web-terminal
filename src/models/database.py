"""数据库连接与会话管理

统一管理 SQLAlchemy 异步引擎和会话工厂，
提供 get_db() 依赖注入给 FastAPI 路由使用。

自动迁移：
  init_db() 在建表后检测已有表是否缺少新增列，
  通过 ALTER TABLE ADD COLUMN 自动补齐，避免旧库启动报错。
"""

from __future__ import annotations

import enum
import logging
from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.host import AuthType, Base, HostType

logger = logging.getLogger(__name__)

# 数据库文件存放在项目根目录
_DB_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_DB_PATH = _DB_DIR / "wetty_mcp.db"
_DATABASE_URL = f"sqlite+aiosqlite:///{_DB_PATH}"

engine = create_async_engine(_DATABASE_URL, echo=False, future=True)

async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """初始化数据库：创建目录 & 建表 & 自动迁移缺失列"""
    _DB_DIR.mkdir(parents=True, exist_ok=True)
    async with engine.begin() as conn:
        # 1) 创建不存在的表
        await conn.run_sync(Base.metadata.create_all)
        # 2) 自动迁移：给已有表补齐缺失的列
        await conn.run_sync(_auto_migrate_columns)
        # 3) 若检测到旧版枚举数据，则清空 hosts 表，后续由 hosts.yaml 重新同步
        await conn.run_sync(reset_hosts_table_if_legacy_enum_found)


_HOST_TYPE_NAMES = frozenset(member.name for member in HostType)
_AUTH_TYPE_NAMES = frozenset(member.name for member in AuthType)


def serialize_default_value(default: object) -> str | None:
    """将 SQLAlchemy 列默认值序列化为数据库实际存储字面量。"""
    default_arg: object | None = getattr(default, "arg", None)
    if default_arg is None:
        return None
    if isinstance(default_arg, enum.Enum):
        return default_arg.name
    if callable(default_arg):
        return None
    return str(default_arg)


def _auto_migrate_columns(connection: Connection) -> None:
    """检测并补齐已有表中缺失的列（SQLite ALTER TABLE ADD COLUMN）。"""
    inspector = inspect(connection)

    for table_name, table in Base.metadata.tables.items():
        try:
            existing_columns = {str(col["name"]) for col in inspector.get_columns(table_name)}
        except Exception:
            continue

        for column in table.columns:
            if column.name in existing_columns:
                continue

            col_type = column.type.compile(dialect=connection.dialect)
            nullable = "NULL" if column.nullable else "NOT NULL"
            default_clause = ""

            if not column.nullable:
                default_literal = serialize_default_value(column.default)
                if default_literal is None:
                    nullable = "NULL"
                else:
                    default_clause = f" DEFAULT '{default_literal}'"

            sql = f"ALTER TABLE {table_name} ADD COLUMN {column.name} {col_type} {nullable}{default_clause}"
            logger.info("自动迁移: %s", sql)
            _ = connection.execute(text(sql))


def _load_distinct_column_values(connection: Connection, column_name: str) -> set[str]:
    query = text(f"SELECT DISTINCT {column_name} FROM hosts WHERE {column_name} IS NOT NULL")
    result = connection.execute(query)
    return {str(value) for value in result.scalars().all()}


def reset_hosts_table_if_legacy_enum_found(connection: Connection) -> None:
    """检测到旧版枚举数据时清空 hosts 表，后续由 hosts.yaml 重建。"""
    inspector = inspect(connection)
    try:
        table_names = {str(name) for name in inspector.get_table_names()}
    except Exception:
        return

    if "hosts" not in table_names:
        return

    invalid_host_types = _load_distinct_column_values(connection, "host_type") - _HOST_TYPE_NAMES
    invalid_auth_types = _load_distinct_column_values(connection, "auth_type") - _AUTH_TYPE_NAMES
    if not invalid_host_types and not invalid_auth_types:
        return

    delete_result = connection.execute(text("DELETE FROM hosts"))
    logger.warning(
        "检测到旧版 hosts 枚举数据，已清空 hosts 表并等待 hosts.yaml 重建: host_type=%s auth_type=%s deleted=%s",
        sorted(invalid_host_types),
        sorted(invalid_auth_types),
        delete_result.rowcount,
    )


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖注入：获取异步数据库会话"""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
