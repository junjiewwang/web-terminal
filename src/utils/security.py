"""安全工具 - 密码加密 / Token 认证

统一提供密码加密解密和 API Token 认证功能，
各模块通过调用此模块实现安全功能，避免重复实现。
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# ── Bearer Token 认证 ──────────────────────────

_API_TOKEN: Optional[str] = None


def generate_api_token() -> str:
    """生成 API Bearer Token"""
    global _API_TOKEN
    _API_TOKEN = secrets.token_urlsafe(32)
    logger.info("API Token 已生成（长度: %d）", len(_API_TOKEN))
    return _API_TOKEN


def verify_api_token(token: str) -> bool:
    """验证 API Token"""
    if _API_TOKEN is None:
        # 未配置 Token 时放行（开发模式）
        return True
    return secrets.compare_digest(token, _API_TOKEN)


def get_current_token() -> Optional[str]:
    """获取当前 Token（仅供日志/调试）"""
    return _API_TOKEN


# ── 密码加密解密（Fernet 对称加密）──────────────

# 加密密钥：优先使用环境变量，否则自动生成（每次重启后旧数据无法解密）
_FERNET_KEY: bytes | None = None
_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    """获取或初始化 Fernet 加密器"""
    global _FERNET_KEY, _fernet
    if _fernet is not None:
        return _fernet

    env_key = os.environ.get("WETTY_ENCRYPTION_KEY")
    if env_key:
        _FERNET_KEY = env_key.encode()
    else:
        # 自动生成密钥并警告（适用于开发环境）
        _FERNET_KEY = Fernet.generate_key()
        logger.warning(
            "未设置 WETTY_ENCRYPTION_KEY 环境变量，自动生成加密密钥。"
            "重启后已加密的密码将无法解密！请在生产环境中配置固定密钥。"
        )

    _fernet = Fernet(_FERNET_KEY)
    return _fernet


def encrypt_password(password: str) -> str:
    """加密密码（Fernet 对称加密）

    Returns:
        base64 编码的加密字符串（fernet: 前缀标识）
    """
    f = _get_fernet()
    encrypted = f.encrypt(password.encode())
    return f"fernet:{encrypted.decode()}"


def decrypt_password(encrypted: str) -> str:
    """解密密码

    兼容旧版 base64 格式和新版 fernet: 前缀格式。

    Raises:
        ValueError: 解密失败
    """
    if encrypted.startswith("fernet:"):
        # 新版 Fernet 加密格式
        try:
            f = _get_fernet()
            return f.decrypt(encrypted[7:].encode()).decode()
        except InvalidToken as e:
            raise ValueError("密码解密失败（密钥可能不匹配）") from e
    else:
        # 旧版 base64 兼容（向后兼容已有数据）
        try:
            return base64.b64decode(encrypted.encode()).decode()
        except Exception as e:
            raise ValueError(f"密码解密失败: {e}") from e
