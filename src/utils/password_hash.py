"""密码哈希生成 CLI 工具

用法：
    python -m src.utils.password_hash "your-password"
    python -m src.utils.password_hash  # 交互式输入

输出 bcrypt 哈希字符串。

推荐使用 python -m src.utils.reset_password 直接重置数据库中的密码。
"""

from __future__ import annotations

import sys

import bcrypt


def hash_password(password: str, rounds: int = 12) -> str:
    """生成 bcrypt 密码哈希

    Args:
        password: 明文密码
        rounds: bcrypt cost（默认 12，安全推荐最低值）

    Returns:
        bcrypt 哈希字符串
    """
    salt = bcrypt.gensalt(rounds=rounds)
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed.decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """验证密码是否与哈希匹配"""
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))


def main() -> None:
    """CLI 入口"""
    if len(sys.argv) > 1:
        password = sys.argv[1]
    else:
        import getpass
        password = getpass.getpass("请输入密码: ")

    if not password:
        print("错误：密码不能为空", file=sys.stderr)
        sys.exit(1)

    hashed = hash_password(password)
    print(f"\n密码哈希：")
    print(hashed)
    print(f"\n提示：推荐使用 python -m src.utils.reset_password 直接重置数据库密码")

    # 验证哈希正确性
    if verify_password(password, hashed):
        print("\n✓ 哈希验证通过")
    else:
        print("\n✗ 哈希验证失败！", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
