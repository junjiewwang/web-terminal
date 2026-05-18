"""密码重置 CLI 工具

用法：
    python -m src.utils.reset_password "new-password"
    python -m src.utils.reset_password  # 交互式输入

功能：
    直连数据库重置登录密码，无需验证旧密码。
    同时吊销所有 Refresh Token，强制已登录用户重新认证。

适用场景：
    - 忘记密码
    - 运维人员紧急重置
    - 初始化部署时设置初始密码
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import bcrypt


def _hash_password(password: str) -> str:
    """生成 bcrypt 密码哈希"""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


async def _reset_password(new_password: str) -> None:
    """异步执行密码重置"""
    # 延迟导入，避免模块加载时就连接数据库
    from sqlalchemy import select, update

    from src.models.auth import AuthConfigModel, RefreshTokenModel
    from src.models.database import async_session_factory, engine, init_db

    # 确保表存在
    await init_db()

    new_hash = _hash_password(new_password)

    async with async_session_factory() as session:
        # 检查 auth_config 是否存在
        result = await session.execute(select(AuthConfigModel).where(AuthConfigModel.id == 1))
        existing = result.scalar_one_or_none()

        if existing:
            # 更新密码
            await session.execute(
                update(AuthConfigModel)
                .where(AuthConfigModel.id == 1)
                .values(password_hash=new_hash, enabled=True)
            )
        else:
            # 首次初始化：插入配置
            config = AuthConfigModel(
                id=1,
                enabled=True,
                password_hash=new_hash,
                jwt_secret=_generate_jwt_secret(),
            )
            session.add(config)

        # 吊销所有 Refresh Token
        await session.execute(
            update(RefreshTokenModel).values(revoked=True)
        )

        await session.commit()

    # 显式关闭引擎，避免 aiomysql 连接 GC 警告
    await engine.dispose()

    print(f"\n✓ 密码已重置")
    print(f"  密码哈希: {new_hash[:20]}...")
    print(f"  所有登录会话已失效")
    print(f"\n  新密码将在下次登录时自动生效（无需重启服务）")


def _generate_jwt_secret() -> str:
    """生成随机 JWT Secret"""
    import secrets
    return secrets.token_urlsafe(32)


def main() -> None:
    """CLI 入口"""
    if len(sys.argv) > 1:
        new_password = sys.argv[1]
    else:
        import getpass
        new_password = getpass.getpass("请输入新密码: ")
        if not new_password:
            print("错误：密码不能为空", file=sys.stderr)
            sys.exit(1)
        confirm = getpass.getpass("请再次输入确认: ")
        if new_password != confirm:
            print("错误：两次输入不一致", file=sys.stderr)
            sys.exit(1)

    if len(new_password) < 6:
        print("错误：密码长度至少 6 个字符", file=sys.stderr)
        sys.exit(1)

    print(f"\n⚠ 警告：此操作将重置登录密码并使所有登录会话失效")
    print(f"  数据库将被直接修改")

    # 非交互式模式（传参数时）跳过确认
    if len(sys.argv) <= 1:
        confirm = input("\n确认重置? [y/N] ")
        if confirm.lower() != "y":
            print("已取消")
            sys.exit(0)

    asyncio.run(_reset_password(new_password))


if __name__ == "__main__":
    main()
