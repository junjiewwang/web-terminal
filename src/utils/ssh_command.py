"""SSH 命令构造工具

将 SSH 命令构造逻辑从 wetty_manager._WeTTYProcess 中抽取为公共函数，
供 WeTTY 进程启动和 tmux 窗口创建共用，遵循 DRY 原则。

使用场景：
  - wetty_manager: WeTTY --command 参数调用 tmux-session.sh 时需要 SSH 命令参数
  - wetty.py: jump_host 后台编排在新 tmux 窗口中启动 SSH 连接到堡垒机

SSH 命令格式：
  - 密钥认证: ssh -o StrictHostKeyChecking=no ... -i <key> -p <port> <user>@<host>
  - 密码认证: sshpass -p '<password>' ssh -o StrictHostKeyChecking=no ... -p <port> <user>@<host>
  - 交互认证: ssh -o StrictHostKeyChecking=no ... -p <port> <user>@<host>
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 公共 SSH 选项：禁用 host key 检查（容器内无 known_hosts）
_SSH_OPTS = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"


def build_ssh_command(
    hostname: str,
    port: int,
    username: str,
    password: Optional[str] = None,
    key_path: Optional[str] = None,
) -> str:
    """构造 SSH 连接命令字符串

    与 scripts/tmux-session.sh 中的 build_ssh_command() 保持一致。

    Args:
        hostname: SSH 目标主机地址
        port: SSH 端口
        username: SSH 用户名
        password: SSH 密码（密码认证时使用）
        key_path: SSH 私钥路径（密钥认证时使用）

    Returns:
        完整的 SSH 命令字符串（可直接在 shell 中执行）
    """
    if key_path:
        return f"ssh {_SSH_OPTS} -i {key_path} -p {port} {username}@{hostname}"
    elif password:
        return f"sshpass -p '{password}' ssh {_SSH_OPTS} -p {port} {username}@{hostname}"
    else:
        return f"ssh {_SSH_OPTS} -p {port} {username}@{hostname}"


def build_ssh_command_for_host(host, decrypted_password: Optional[str] = None) -> str:
    """从 Host ORM 对象构造 SSH 命令

    便捷包装：自动从 Host 对象中提取连接参数。

    Args:
        host: Host ORM 对象（需要 hostname, port, username, private_key_path 属性）
        decrypted_password: 已解密的密码（调用方负责解密，避免本模块依赖加密逻辑）

    Returns:
        完整的 SSH 命令字符串
    """
    return build_ssh_command(
        hostname=host.hostname,
        port=host.port,
        username=host.username,
        password=decrypted_password,
        key_path=host.private_key_path,
    )
