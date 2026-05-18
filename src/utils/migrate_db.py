"""SQLite → MySQL 数据迁移 CLI 工具

从 SQLite 读取所有 hosts 数据，写入 MySQL。
用于从旧版本升级到 MySQL 模式时的一次性迁移。

用法：
    # 确保 DATABASE_URL 环境变量指向目标 MySQL
    python -m src.utils.migrate_db

    # 指定源 SQLite 文件
    python -m src.utils.migrate_db --source data/wetty_mcp.db

    # Docker 容器内执行
    docker exec -it wetty-mcp python -m src.utils.migrate_db
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# 需要在 import 之前确保 PYTHONPATH 正确
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.models.host import Base, Host  # noqa: E402

logger = logging.getLogger(__name__)


async def _read_sqlite_hosts(sqlite_path: Path) -> list[dict]:
    """从 SQLite 读取所有 hosts 行。"""
    url = f"sqlite+aiosqlite:///{sqlite_path}"
    sqlite_engine = create_async_engine(url, echo=False)

    rows: list[dict] = []
    async with sqlite_engine.connect() as conn:
        result = await conn.execute(text("SELECT * FROM hosts ORDER BY id"))
        columns = list(result.keys())
        for row in result.fetchall():
            rows.append(dict(zip(columns, row)))

    await sqlite_engine.dispose()
    return rows


async def _write_mysql_hosts(rows: list[dict]) -> int:
    """将 hosts 数据写入 MySQL（使用当前配置的 DATABASE_URL）。"""
    # 延迟导入，确保读取到正确的 DATABASE_URL
    from src.models.database import engine, init_db

    # 初始化目标数据库（建表）
    await init_db()

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # 构建 id 映射（旧 id → 新 id），用于修复 parent_id 引用
    old_id_to_new: dict[int, int] = {}
    written = 0

    async with session_factory() as session:
        # 先清空目标表
        await session.execute(text("DELETE FROM hosts"))

        # 按 id 顺序插入（确保 parent 在 child 之前）
        sorted_rows = sorted(rows, key=lambda r: r.get("id", 0))

        for row in sorted_rows:
            old_id = row.pop("id", None)

            # 修复 parent_id 引用
            old_parent_id = row.get("parent_id")
            if old_parent_id is not None:
                row["parent_id"] = old_id_to_new.get(old_parent_id, old_parent_id)

            # 移除 SQLAlchemy 不识别的额外字段
            row.pop("_sa_instance_state", None)

            host = Host(**row)
            session.add(host)
            await session.flush()  # 获取新的 auto-increment id

            if old_id is not None:
                old_id_to_new[old_id] = host.id

            written += 1

        await session.commit()

    return written


async def migrate(sqlite_path: Path) -> None:
    """执行 SQLite → MySQL 迁移。"""
    if not sqlite_path.exists():
        print(f"✗ SQLite 文件不存在: {sqlite_path}", file=sys.stderr)
        sys.exit(1)

    print(f"读取 SQLite: {sqlite_path}")
    rows = await _read_sqlite_hosts(sqlite_path)
    print(f"  → 读取到 {len(rows)} 条 hosts 记录")

    if not rows:
        print("  → SQLite 中无数据，无需迁移")
        return

    print("写入 MySQL...")
    written = await _write_mysql_hosts(rows)
    print(f"  → 成功写入 {written} 条记录")
    print("\n✓ 迁移完成")


def main() -> None:
    """CLI 入口。"""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="SQLite → MySQL 数据迁移工具")
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("data/wetty_mcp.db"),
        help="源 SQLite 文件路径（默认: data/wetty_mcp.db）",
    )
    args = parser.parse_args()

    asyncio.run(migrate(args.source))


if __name__ == "__main__":
    main()
