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
from pathlib import Path
from typing import AsyncGenerator

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.host import Base

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
        # 3) 修复历史脏数据（如错误的 Enum 默认值）
        await conn.run_sync(_fix_enum_defaults)


def _auto_migrate_columns(connection) -> None:
    """检测并补齐已有表中缺失的列（SQLite ALTER TABLE ADD COLUMN）

    遍历 ORM metadata 中的所有表和列定义，
    与数据库实际列对比，缺失的列自动 ADD COLUMN。

    仅适用于 SQLite（其 ALTER TABLE 只支持 ADD COLUMN，
    不支持修改/删除列，正好满足向前兼容需求）。
    """
    inspector = inspect(connection)

    for table_name, table in Base.metadata.tables.items():
        # 获取数据库中已有的列名集合
        try:
            existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
        except Exception:
            # 表不存在（刚被 create_all 创建，无需迁移）
            continue

        # 遍历 ORM 定义的列，补齐缺失的
        for column in table.columns:
            if column.name in existing_columns:
                continue

            # 构造 ALTER TABLE ADD COLUMN 语句
            col_type = column.type.compile(dialect=connection.dialect)
            nullable = "NULL" if column.nullable else "NOT NULL"

            # SQLite ADD COLUMN 不支持 NOT NULL without DEFAULT
            # 对于 NOT NULL 列，添加合理的默认值
            default_clause = ""
            if not column.nullable:
                if column.default is not None:
                    default_val = column.default.arg
                    if callable(default_val):
                        # server_default 函数（如 func.now()）→ 放弃 NOT NULL
                        nullable = "NULL"
                    else:
                        # Enum 值需要取 .value（如 HostType.DIRECT → "direct"）
                        if isinstance(default_val, enum.Enum):
                            default_val = default_val.value
                        default_clause = f" DEFAULT '{default_val}'"
                else:
                    # NOT NULL 无默认值 → 改为 NULL（兼容已有数据行）
                    nullable = "NULL"

            sql = f"ALTER TABLE {table_name} ADD COLUMN {column.name} {col_type} {nullable}{default_clause}"
            logger.info("自动迁移: %s", sql)
            connection.execute(text(sql))


def _fix_enum_defaults(connection) -> None:
    """修复历史迁移产生的错误 Enum 默认值

    早期版本的 _auto_migrate_columns 将 Python Enum repr（如 'HostType.DIRECT'）
    写入 SQLite DEFAULT，导致已有行的 Enum 列存储了错误的值。
    此函数将这些值修正为 SQLAlchemy Enum 期望的格式（如 'DIRECT'）。
    """
    # 定义需要修复的 {table.column: {错误值: 正确值}}
    fixes = {
        ("hosts", "host_type"): {
            "HostType.DIRECT": "DIRECT",
            "HostType.BASTION": "BASTION",
            "HostType.JUMP_HOST": "JUMP_HOST",
        },
        ("hosts", "auth_type"): {
            "AuthType.KEY": "KEY",
            "AuthType.PASSWORD": "PASSWORD",
        },
    }

    for (table, column), value_map in fixes.items():
        for wrong_val, correct_val in value_map.items():
            result = connection.execute(
                text(f"UPDATE {table} SET {column} = :correct WHERE {column} = :wrong"),
                {"correct": correct_val, "wrong": wrong_val},
            )
            if result.rowcount > 0:
                logger.info(
                    "修复 Enum 脏数据: %s.%s '%s' → '%s' (%d 行)",
                    table, column, wrong_val, correct_val, result.rowcount,
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
