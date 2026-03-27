"""FastAPI 应用入口

整合所有路由、生命周期事件、中间件和 MCP Server 挂载。
支持 hosts.yaml 文件监听热加载（watchfiles + 防抖）。
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from src.api import events, hosts, sessions, tmux
from src.api import terminal as terminal_api
from src.mcp_server.server import get_pty_manager, init_mcp_server, mcp
from src.models.database import async_session_factory, init_db
from src.services.host_manager import HostManager
from src.services.ssh_session import SSHSessionManager
from src.services.terminal_manager import TerminalManager
from src.services.tmux_manager import TmuxWindowManager
from src.utils.security import generate_api_token, verify_api_token

logger = logging.getLogger(__name__)

# 全局服务实例
ssh_manager = SSHSessionManager()
terminal_manager = TerminalManager()
tmux_manager_instance = TmuxWindowManager()

# hosts.yaml 路径
_HOSTS_YAML = Path(__file__).resolve().parent.parent / "config" / "hosts.yaml"

# 文件监听防抖间隔（秒）
_WATCH_DEBOUNCE_SECONDS = 2.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动 & 关闭"""
    # ── 启动 ──
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("WeTTY + MCP Terminal Service 启动中...")

    # 初始化数据库
    await init_db()
    logger.info("数据库初始化完成")

    # 从 hosts.yaml 同步主机配置（启动时首次同步）
    await _sync_hosts_from_yaml()

    # 注入全局服务实例到 API 模块
    sessions.ssh_manager = ssh_manager
    tmux.tmux_manager = tmux_manager_instance
    terminal_api.terminal_manager = terminal_manager
    terminal_api.tmux_manager = tmux_manager_instance

    # 初始化 MCP Server 依赖
    init_mcp_server(terminal_manager, tmux_manager=tmux_manager_instance)

    # 生成 API Token（设置了 WETTY_API_TOKEN 环境变量时启用认证）
    env_token = os.environ.get("WETTY_API_TOKEN")
    if env_token:
        logger.info("API Token 认证已启用（来源: 环境变量）")
    else:
        token = generate_api_token()
        logger.info("API Token 认证已启用（自动生成）: %s", token)

    # 启动 hosts.yaml 文件监听后台任务
    watch_task = asyncio.create_task(_watch_hosts_yaml())

    # 启动 zombie tmux session 定期清理后台任务
    zombie_cleanup_task = asyncio.create_task(_cleanup_zombie_sessions_loop())

    # 启动 MCP Session Manager（app.mount 的子应用 lifespan 不会被 FastAPI 触发，
    # 需要在主应用 lifespan 中手动启动，否则请求会报 "Task group is not initialized"）
    async with mcp.session_manager.run():
        logger.info("MCP Session Manager 已启动 ✓")
        logger.info("服务启动完成 ✓")
        yield

        # ── 关闭 ──
        logger.info("服务关闭中...")
        watch_task.cancel()
        zombie_cleanup_task.cancel()
        try:
            await watch_task
        except asyncio.CancelledError:
            pass
        try:
            await zombie_cleanup_task
        except asyncio.CancelledError:
            pass
        # 关闭 MCP PTY 会话（socket.io 连接）
        pty_mgr = get_pty_manager()
        if pty_mgr:
            await pty_mgr.close_all()
        await ssh_manager.close_all()
        await terminal_manager.stop_all()
    logger.info("服务已关闭 ✓")


async def _sync_hosts_from_yaml() -> None:
    """执行一次 hosts.yaml → DB 同步"""
    async with async_session_factory() as db_session:
        manager = HostManager(db_session)
        result = await manager.sync_from_yaml(_HOSTS_YAML)
        await db_session.commit()
        if result.total_changes:
            logger.info(
                "hosts.yaml 同步完成: 新增 %d, 更新 %d, 删除 %d",
                result.added, result.updated, result.deleted,
            )
        if result.errors:
            logger.error("hosts.yaml 同步错误: %s", result.errors)


async def _watch_hosts_yaml() -> None:
    """后台任务：监听 hosts.yaml 文件变更，自动触发同步

    使用 watchfiles 异步监听 + 防抖机制，避免频繁触发。
    如果 watchfiles 不可用（开发环境未安装），静默退出。
    """
    try:
        from watchfiles import awatch
    except ImportError:
        logger.warning("watchfiles 未安装，hosts.yaml 热加载已禁用（可通过 API 手动触发同步）")
        return

    yaml_dir = _HOSTS_YAML.parent
    yaml_name = _HOSTS_YAML.name

    logger.info("启动 hosts.yaml 文件监听: %s", _HOSTS_YAML)

    try:
        async for changes in awatch(yaml_dir, debounce=int(_WATCH_DEBOUNCE_SECONDS * 1000)):
            # 只关心 hosts.yaml 的变更
            relevant = any(
                Path(path).name == yaml_name
                for _, path in changes
            )
            if not relevant:
                continue

            logger.info("检测到 hosts.yaml 变更，触发同步...")
            try:
                await _sync_hosts_from_yaml()
            except Exception:
                logger.exception("hosts.yaml 热加载同步失败")
    except asyncio.CancelledError:
        logger.info("hosts.yaml 文件监听已停止")
        raise


# zombie tmux session 清理间隔（秒）
_ZOMBIE_CLEANUP_INTERVAL = 60


async def _cleanup_zombie_sessions_loop() -> None:
    """后台任务：定期清理 zombie tmux session

    扫描所有 wetty- 前缀的 tmux session，清理没有对应活跃 WeTTY 实例的残留 session。
    避免 SSH 连接和系统资源泄漏。
    """
    logger.info("启动 zombie tmux session 定期清理（间隔 %ds）", _ZOMBIE_CLEANUP_INTERVAL)

    # 首次启动等待服务就绪
    await asyncio.sleep(10)

    try:
        while True:
            try:
                cleaned = await terminal_manager.cleanup_zombie_sessions()
                if cleaned:
                    logger.info("定期清理: 清理了 %d 个 zombie tmux session", cleaned)
            except Exception:
                logger.exception("zombie session 清理异常")
            await asyncio.sleep(_ZOMBIE_CLEANUP_INTERVAL)
    except asyncio.CancelledError:
        logger.info("zombie session 定期清理已停止")
        raise


# ── 创建应用 ──────────────────────────────────

app = FastAPI(
    title="WeTTY + MCP Terminal Service",
    description="AI Agent 可控的 SSH 终端管理服务",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS 中间件（开发阶段放行所有源）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Bearer Token 认证中间件 ──────────────────

# 无需认证的路径白名单
_AUTH_WHITELIST = {"/health", "/docs", "/openapi.json", "/redoc"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Bearer Token 认证中间件

    - /health、/docs 等路径免认证
    - /mcp/ 路径免认证（MCP 协议自身管理认证）
    - 其他 /api/ 路径需要 Bearer Token
    """
    path = request.url.path

    # 白名单路径、MCP 路径、非 API 路径免认证
    if path in _AUTH_WHITELIST or path.startswith("/mcp/") or not path.startswith("/api/"):
        return await call_next(request)

    # 检查 Bearer Token
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        # 优先检查环境变量 Token
        env_token = os.environ.get("WETTY_API_TOKEN")
        if env_token:
            import secrets
            if secrets.compare_digest(token, env_token):
                return await call_next(request)
        elif verify_api_token(token):
            return await call_next(request)

        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "无效的 API Token"},
        )

    # 未提供 Token 时：开发模式放行（未配置环境变量 Token 时视为开发模式）
    if not os.environ.get("WETTY_API_TOKEN"):
        return await call_next(request)

    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"detail": "缺少 Authorization 头"},
        headers={"WWW-Authenticate": "Bearer"},
    )


# ── 注册路由 ──────────────────────────────────

app.include_router(hosts.router)
app.include_router(sessions.router)
app.include_router(events.router)
app.include_router(terminal_api.router)
app.include_router(tmux.router)

# ── 挂载 MCP Server（SSE 模式）──────────────
# FastMCP 通过 streamable_http 模式挂载到 /mcp 路径
app.mount("/mcp", mcp.streamable_http_app())


@app.get("/health", tags=["system"])
async def health_check() -> dict:
    """健康检查"""
    return {
        "status": "healthy",
        "service": "wetty-mcp-terminal",
        "version": "0.1.0",
    }


# ── 前端静态文件（生产模式）──────────────────
# 前端构建产物放在 /app/static，由 Dockerfile COPY 进来。
# 开发模式用 vite dev server，此目录不存在时自动跳过。
# 注意：必须放在所有 router / mount 之后，作为最低优先级的 fallback。

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

if _STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")
