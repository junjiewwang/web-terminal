"""WeTTY 实例管理 REST API

提供 WeTTY 实例的启动/停止/列出接口，
前端选择主机时调用此 API 获取 WeTTY 终端 URL。

jump_host 感知：
  当请求启动的主机是 jump_host 类型时，自动复用父堡垒机的 WeTTY 实例，
  创建 tmux 窗口并在后台执行跳板编排（发送 target_ip + login_steps），
  返回堡垒机的 WeTTY URL，前端通过 bastionName 连接到正确的 socket.io 路径。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from src.models.database import async_session_factory
from src.models.host import Host, HostType
from src.services.host_manager import HostManager
from src.services.jump_orchestrator import JumpOrchestrator
from src.services.pty_session import PTYSessionManager
from src.services.tmux_manager import TmuxWindowManager
from src.services.wetty_manager import WeTTYInstance, WeTTYManager, wait_for_port

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/wetty", tags=["wetty"])

# 全局服务实例（在 main.py 中注入）
wetty_manager: WeTTYManager | None = None
tmux_manager: TmuxWindowManager | None = None


def _get_wetty_manager() -> WeTTYManager:
    """获取全局 WeTTY 管理器"""
    if wetty_manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WeTTY 管理器未初始化",
        )
    return wetty_manager


def _get_tmux_manager() -> TmuxWindowManager:
    """获取全局 tmux 管理器"""
    if tmux_manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="tmux 管理器未初始化",
        )
    return tmux_manager


# ── 请求/响应 Schema ──────────────────────────


class StartWeTTYRequest(BaseModel):
    """启动 WeTTY 实例请求"""
    host_id: int


class WeTTYInstanceResponse(BaseModel):
    """WeTTY 实例信息响应

    jump_host 时 bastion_name 不为空，前端据此连接堡垒机的 socket.io 路径。
    """
    host_name: str
    port: int
    url: str
    running: bool
    bastion_name: str | None = None


# ── 路由 ──────────────────────────────────────


@router.post("/start", response_model=WeTTYInstanceResponse)
async def start_wetty(req: StartWeTTYRequest) -> WeTTYInstanceResponse:
    """为指定主机启动 WeTTY 实例

    自动识别主机类型：
    - direct / bastion: 直接启动该主机的 WeTTY 实例
    - jump_host: 启动父堡垒机的 WeTTY 实例，创建 tmux 窗口，
                 后台执行跳板编排，返回堡垒机 URL
    """
    manager = _get_wetty_manager()

    async with async_session_factory() as db_session:
        host_mgr = HostManager(db_session)
        host = await host_mgr.get_host_by_id(req.host_id)
        if not host:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"主机不存在: {req.host_id}",
            )

        # jump_host → 走堡垒机复用流程
        if host.host_type == HostType.JUMP_HOST:
            return await _start_jump_host(host, host_mgr, manager)

        # direct / bastion → 直接启动
        return await _start_direct_host(host, manager)


async def _start_direct_host(
    host: Host,
    manager: WeTTYManager,
) -> WeTTYInstanceResponse:
    """直连主机 / 堡垒机：直接启动 WeTTY 实例"""
    try:
        instance: WeTTYInstance = await manager.start_instance(host)
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="wetty 命令未找到，请先安装: npm install -g wetty",
        )
    except OSError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"WeTTY 启动失败: {e}",
        )

    return WeTTYInstanceResponse(
        host_name=instance.host_name,
        port=instance.port,
        url=instance.url,
        running=instance.running,
    )


async def _start_jump_host(
    jump_host: Host,
    host_mgr: HostManager,
    manager: WeTTYManager,
) -> WeTTYInstanceResponse:
    """jump_host：创建独立的 WeTTY 实例 + 后台跳板编排

    重要架构变更：每个 jump_host Tab 创建独立的 WeTTY 实例，而非复用堡垒机 WeTTY。
    这样可以实现输入隔离：每个 Tab 的输入只影响自己的终端。

    架构对比：
      旧架构：多 Tab 共享一个 WeTTY → 输入写入同一个 tmux active window
      新架构：每个 Tab 独立 WeTTY → 输入完全隔离

    流程：
    1. 查找父堡垒机获取连接信息
    2. 为 jump_host 创建独立的 WeTTY 实例（使用复合名称，如 tce-server--m12）
    3. 后台执行跳板编排（PTY 自动化登录到目标主机）
    4. 返回独立 WeTTY 的 URL（不设置 bastion_name，表示独立实例）
    """
    # Step 1: 获取父堡垒机的连接信息
    if not jump_host.parent_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"jump_host '{jump_host.name}' 未配置 parent_id",
        )
    bastion = await host_mgr.get_host_by_id(jump_host.parent_id)
    if not bastion:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"jump_host '{jump_host.name}' 的父堡垒机不存在 (id={jump_host.parent_id})",
        )

    # Step 2: 为 jump_host 创建独立的 WeTTY 实例
    # 使用复合名称（bastion_name--jump_host_name）确保唯一性
    instance_name = f"{bastion.name}--{jump_host.name}"

    # 如果已有该实例，直接返回（不再执行跳板编排，实例已连接到目标主机）
    if manager.has_running_instance(instance_name):
        instance = manager.get_instance(instance_name)
        if instance:
            return WeTTYInstanceResponse(
                host_name=instance.host_name,
                port=instance.port,
                url=instance.url,
                running=instance.running,
                bastion_name=instance_name,  # 返回实例名，前端需要它来关闭实例
            )
        # 实例已停止，继续创建新实例

    # 创建新实例：使用堡垒机的连接信息，但实例名用复合名称
    try:
        instance: WeTTYInstance = await manager.start_instance_for_jump_host(
            instance_name=instance_name,
            bastion=bastion,
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="wetty 命令未找到，请先安装: npm install -g wetty",
        )
    except OSError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"WeTTY 启动失败: {e}",
        )

    # 等待 WeTTY 进程就绪（主动探测端口，替代固定 sleep）
    try:
        await wait_for_port(instance.port)
    except TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"WeTTY 端口 {instance.port} 启动超时",
        )
    logger.info("start_jump_host: 独立 WeTTY 已启动 (%s)", instance_name)

    # Step 3: 后台执行跳板编排（PTY 自动化）
    tmux_session = TmuxWindowManager.session_name_for(instance_name)
    window_name = "0"  # 默认窗口

    asyncio.create_task(
        _run_jump_orchestration(
            jump_host, bastion, instance, tmux_session, window_name,
            is_independent_wetty=True,
        )
    )

    # Step 4: 返回独立 WeTTY 的 URL 和 bastion_name
    # 前端需要 bastion_name 来：
    # 1. 判断是否是独立 WeTTY 实例（包含 "--"）
    # 2. 正确关闭 WeTTY 实例
    return WeTTYInstanceResponse(
        host_name=instance.host_name,
        port=instance.port,
        url=instance.url,
        running=instance.running,
        bastion_name=instance_name,  # 独立实例名，如 "tce-server--m12"
    )


async def _run_jump_orchestration(
    jump_host: Host,
    bastion: Host,
    instance: WeTTYInstance,
    tmux_session: str,
    window_name: str,
    bastion_ssh_cmd: str | None = None,
    is_independent_wetty: bool = False,
) -> None:
    """后台执行跳板编排（PTY 自动化登录到目标主机）

    作为 fire-and-forget 后台任务运行，不阻塞 REST API 响应。

    两种模式：
    1. 独立 WeTTY 模式（is_independent_wetty=True）：
       - WeTTY 已连接到堡垒机，直接在默认窗口执行跳板编排
       - 不需要创建 tmux 窗口，不需要切换窗口
       - 输入完全隔离，每个 Tab 独立 WeTTY 实例

    2. 共享 WeTTY 模式（is_independent_wetty=False，已废弃）：
       - PTY 连接 → 创建 tmux 窗口 → 切换窗口 → 跳板编排
       - 输入会冲突，多个 Tab 共享同一个 WeTTY

    流程（独立模式）：
    1. PTY 连接 WeTTY（触发 tmux session 创建，WeTTY 自动 SSH 到堡垒机）
    2. 等待堡垒机 ready_pattern
    3. 发送 target_ip + login_steps 完成跳转
    4. 保持 PTY 会话运行

    Args:
        jump_host: 二级主机 ORM 对象
        bastion: 父堡垒机 ORM 对象
        instance: WeTTY 实例信息
        tmux_session: tmux 会话名
        window_name: 窗口名（独立模式下为 "0"）
        bastion_ssh_cmd: SSH 命令（独立模式下未使用）
        is_independent_wetty: 是否独立 WeTTY 模式
    """
    pty_mgr = PTYSessionManager()
    # 独立模式下，base_path 使用实例的实际名称（如 tce-server--m15）
    wetty_name = instance.host_name if is_independent_wetty else bastion.name
    base_path = f"/wetty/t/{wetty_name}"

    tmux_mgr = _get_tmux_manager()

    # Step 1: PTY 连接 WeTTY（触发 tmux session 创建）
    try:
        pty_session = await pty_mgr.create_session(
            host_name=jump_host.name,
            wetty_port=instance.port,
            wetty_base_path=base_path,
            connect_timeout=15.0,
            tmux_window=window_name,
        )
    except ConnectionError as e:
        logger.error("跳板编排 PTY 连接失败 (%s): %s", jump_host.name, e)
        return

    if not is_independent_wetty:
        # 共享模式（已废弃）：创建 tmux 窗口并切换
        clients_before_pty = await tmux_mgr.list_clients(tmux_session)

        if not await tmux_mgr.create_window(tmux_session, window_name, command=bastion_ssh_cmd):
            logger.error("跳板编排 tmux 窗口创建失败: %s:%s", tmux_session, window_name)
            await pty_mgr.close_session(pty_session.session_id)
            return

        clients_after_pty = await tmux_mgr.list_clients(tmux_session)
        ttys_before = {c.tty for c in clients_before_pty}
        ttys_after = {c.tty for c in clients_after_pty}
        new_ttys = ttys_after - ttys_before

        if new_ttys:
            pty_client_tty = list(new_ttys)[0]
            await tmux_mgr.switch_client(pty_client_tty, tmux_session, window_name)
        else:
            logger.warning(
                "跳板编排: 无法识别 PTY client (before=%d, after=%d)，降级为全局 select-window",
                len(clients_before_pty), len(clients_after_pty),
            )
            await tmux_mgr.select_window(tmux_session, window_name)

    # 防重入检测：检查 tmux session 是否已有活跃 SSH 连接
    # 如果上次的跳板编排已成功（session 残留了已登录的 SSH），跳过本次编排
    if is_independent_wetty and await tmux_mgr.is_session_logged_in(tmux_session):
        screen = pty_session.read_screen(lines=5)
        # 检查屏幕内容是否包含 shell 提示符（说明已登录到目标主机）
        import re
        if re.search(r"[\$#>]\s*$", screen, re.MULTILINE):
            logger.info(
                "跳板编排防重入: tmux session 已有活跃连接且有 shell 提示符，跳过编排 (%s)",
                jump_host.name,
            )
            return

    # Step 2: 执行跳板编排
    orchestrator = JumpOrchestrator(pty_session)
    result = await orchestrator.execute_jump(
        jump_host=jump_host,
        bastion=bastion,
        tmux_session_name=tmux_session,
        window_name=window_name,
        skip_window_creation=is_independent_wetty,  # 独立模式跳过窗口创建
    )

    if result.success:
        logger.info("跳板编排成功: %s → %s (%s)", bastion.name, jump_host.name, result.message)
    else:
        logger.error("跳板编排失败: %s → %s (%s)", bastion.name, jump_host.name, result.message)

    # 注意：保持 PTY 会话运行，不关闭
    # 这样可以保持 tmux client TTY 有效


@router.post("/stop/{host_name}", status_code=status.HTTP_204_NO_CONTENT)
async def stop_wetty(host_name: str) -> None:
    """停止指定主机的 WeTTY 实例"""
    manager = _get_wetty_manager()
    stopped = await manager.stop_instance(host_name)
    if not stopped:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"WeTTY 实例不存在: {host_name}",
        )


@router.get("", response_model=list[WeTTYInstanceResponse])
async def list_wetty_instances() -> list[WeTTYInstanceResponse]:
    """列出所有运行中的 WeTTY 实例"""
    manager = _get_wetty_manager()
    instances = manager.list_instances()
    return [
        WeTTYInstanceResponse(
            host_name=inst.host_name,
            port=inst.port,
            url=inst.url,
            running=inst.running,
        )
        for inst in instances
    ]
