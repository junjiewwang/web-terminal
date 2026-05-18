"""数据库连接与会话管理

支持 SQLite（默认）和 MySQL 双数据库引擎。

配置方式：
  - 环境变量 DATABASE_URL 有值 → MySQL 模式
  - 留空或未设置 → 默认 SQLite（data/wetty_mcp.db）

自动迁移：
  init_db() 在建表后检测已有表是否缺少新增列，
  通过 ALTER TABLE ADD COLUMN 自动补齐，避免旧库启动报错。
"""

from __future__ import annotations

import enum
import logging
import os
import re
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.models.host import AuthType, Base, HostType
from src.models.auth import AuthConfigModel, RefreshTokenModel  # noqa: F401 — 注册到 Base.metadata
from src.models.credential import Credential  # noqa: F401 — 注册到 Base.metadata

logger = logging.getLogger(__name__)

# ── 项目根目录 ──────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# ── 数据库类型枚举 ──────────────────────────────
_SQLITE = "sqlite"
_MYSQL = "mysql"


# ── 配置解析 ────────────────────────────────────

def _build_database_url() -> str:
    """构建数据库连接 URL。

    规则：
      - DATABASE_URL 环境变量有值 → 使用该值（MySQL）
      - 留空或未设置 → 默认 SQLite
    """
    env_url = os.environ.get("DATABASE_URL", "").strip()
    if env_url:
        logger.info("数据库配置来源: DATABASE_URL 环境变量")
        return env_url

    # 默认 SQLite
    db_path = _PROJECT_ROOT / "data" / "wetty_mcp.db"
    logger.info("数据库配置来源: 默认 SQLite (%s)", db_path)
    return f"sqlite+aiosqlite:///{db_path}"


# ── 数据库类型检测 ──────────────────────────────

def _detect_db_type(url: str) -> str:
    """根据 URL 判断数据库类型。"""
    if url.startswith("mysql"):
        return _MYSQL
    return _SQLITE


# ── 引擎创建 ────────────────────────────────────

# MySQL 连接池默认配置
_DEFAULT_POOL_SIZE = 5
_DEFAULT_MAX_OVERFLOW = 10
_DEFAULT_POOL_RECYCLE = 3600


def _create_engine(url: str) -> AsyncEngine:
    """根据 URL 创建异步引擎，MySQL 额外配置连接池。"""
    kwargs: dict[str, Any] = {"echo": False, "future": True}
    db_type = _detect_db_type(url)

    if db_type == _MYSQL:
        kwargs.update({
            "pool_size": _DEFAULT_POOL_SIZE,
            "max_overflow": _DEFAULT_MAX_OVERFLOW,
            "pool_recycle": _DEFAULT_POOL_RECYCLE,
        })
    # SQLite 不需要连接池配置（单文件，默认即可）

    created_engine = create_async_engine(url, **kwargs)

    # SQLite 需要启用外键约束
    if db_type == _SQLITE:
        @event.listens_for(created_engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn: Any, _connection_record: Any) -> None:
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return created_engine


# ── 模块级引擎和会话工厂 ──────────────────────────

_database_url = _build_database_url()
_db_type = _detect_db_type(_database_url)

engine = _create_engine(_database_url)
async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# ── 公共查询接口 ────────────────────────────────

def get_db_type() -> str:
    """获取当前数据库类型：'sqlite' 或 'mysql'。"""
    return _db_type


def is_sqlite() -> bool:
    """当前是否使用 SQLite。"""
    return _db_type == _SQLITE


def is_mysql() -> bool:
    """当前是否使用 MySQL。"""
    return _db_type == _MYSQL


def get_db_info() -> dict[str, str]:
    """获取数据库连接信息（脱敏）。"""
    url = _database_url
    if _db_type == _MYSQL:
        # 脱敏：隐藏密码
        masked = re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", url)
        return {"type": "mysql", "url": masked}
    return {"type": "sqlite", "url": url}


# ── 数据库初始化 ──────────────────────────────────

async def init_db() -> None:
    """初始化数据库：创建目录(SQLite) / 建表 / 自动迁移缺失列。"""
    if is_sqlite():
        db_dir = _PROJECT_ROOT / "data"
        db_dir.mkdir(parents=True, exist_ok=True)

    async with engine.begin() as conn:
        # 1) 创建不存在的表
        await conn.run_sync(Base.metadata.create_all)
        # 2) 自动迁移：给已有表补齐缺失的列
        await conn.run_sync(_auto_migrate_columns)
        # 3) 若检测到旧版枚举数据，则清空 hosts 表
        await conn.run_sync(reset_hosts_table_if_legacy_enum_found)


# ── 自动迁移 ────────────────────────────────────

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
    """检测并补齐已有表中缺失的列（ALTER TABLE ADD COLUMN）。

    SQLite 和 MySQL 都支持此语法。
    """
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
    query = text(f"SELECT DISTINCT {column_name} FROM hosts WHERE {column_name} IS NOT NULL")  # noqa: S608
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
        "检测到旧版 hosts 枚举数据，已清空 hosts 表并等待重建: host_type=%s auth_type=%s deleted=%s",
        sorted(invalid_host_types),
        sorted(invalid_auth_types),
        delete_result.rowcount,
    )


# ── FastAPI 依赖注入 ────────────────────────────

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖注入：获取异步数据库会话。"""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
