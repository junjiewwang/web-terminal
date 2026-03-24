"""WeTTY 反向代理

将 /wetty/t/{host_name}/ 下的所有请求（HTTP + WebSocket）
透明转发到容器内对应 WeTTY 实例的端口。

架构说明：
  - 每个 WeTTY 实例通过 --base /wetty/t/{host_name} 启动
  - WeTTY 内部资源路径已包含完整前缀（如 /wetty/t/xxx/assets/...）
  - 反代只需将请求原样转发到内部端口即可
  - HTTP 请求使用 httpx 转发
  - WebSocket 请求使用 websockets 双向桥接
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect

from src.services.wetty_manager import WeTTYManager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["wetty-proxy"])

# 全局 WeTTY 管理器引用（在 main.py 中注入）
wetty_manager: WeTTYManager | None = None

# 复用 httpx 客户端（连接池）
_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    """获取或创建全局 httpx 客户端"""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=30.0)
    return _http_client


async def shutdown_proxy() -> None:
    """关闭 httpx 客户端（在应用关闭时调用）"""
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None


def _resolve_target(host_name: str) -> int | None:
    """解析主机名对应的 WeTTY 内部端口"""
    if wetty_manager is None:
        return None
    return wetty_manager.get_instance_port(host_name)


# ── HTTP 反向代理 ──────────────────────────────


@router.api_route(
    "/wetty/t/{host_name}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def proxy_http_root(host_name: str, request: Request) -> Response:
    """根路径重定向：/wetty/t/{host_name} → /wetty/t/{host_name}/"""
    return Response(
        status_code=301,
        headers={"Location": f"/wetty/t/{host_name}/"},
    )


@router.api_route(
    "/wetty/t/{host_name}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def proxy_http(host_name: str, path: str, request: Request) -> Response:
    """HTTP 请求反向代理

    将 /wetty/t/{host_name}/xxx 原样转发到
    http://127.0.0.1:{port}/wetty/t/{host_name}/xxx

    WeTTY 的 --base 参数使其所有路径都以 /wetty/t/{host_name} 为前缀，
    因此转发时需要保留完整路径。
    """
    port = _resolve_target(host_name)
    if port is None:
        return Response(
            content=f"WeTTY 实例未运行: {host_name}",
            status_code=502,
        )

    # 保留完整路径（WeTTY 内部已包含 base 前缀）
    full_path = f"/wetty/t/{host_name}/{path}" if path else f"/wetty/t/{host_name}"
    target_url = f"http://127.0.0.1:{port}{full_path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    # 转发请求头（过滤跳级头）
    forward_headers = _filter_hop_headers(dict(request.headers))
    forward_headers["host"] = f"127.0.0.1:{port}"

    client = _get_http_client()

    try:
        body = await request.body()
        upstream_resp = await client.request(
            method=request.method,
            url=target_url,
            headers=forward_headers,
            content=body,
            follow_redirects=False,
        )
    except httpx.ConnectError:
        logger.warning("WeTTY 实例连接失败: %s (port %d)", host_name, port)
        return Response(
            content=f"WeTTY 实例连接失败: {host_name}",
            status_code=502,
        )
    except httpx.TimeoutException:
        return Response(content="WeTTY 请求超时", status_code=504)

    # 构建响应
    response_headers = _filter_hop_headers(dict(upstream_resp.headers))
    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers=response_headers,
    )


# ── WebSocket 反向代理 ─────────────────────────


@router.websocket("/wetty/t/{host_name}/{path:path}")
async def proxy_websocket(websocket: WebSocket, host_name: str, path: str) -> None:
    """WebSocket 反向代理

    WeTTY 使用 socket.io（基于 WebSocket）进行终端交互。
    此路由建立双向桥接：浏览器 ↔ FastAPI ↔ WeTTY。
    """
    port = _resolve_target(host_name)
    if port is None:
        await websocket.close(code=1008, reason=f"WeTTY 实例未运行: {host_name}")
        return

    await websocket.accept()

    # 保留完整路径
    full_path = f"/wetty/t/{host_name}/{path}" if path else f"/wetty/t/{host_name}"
    ws_url = f"ws://127.0.0.1:{port}{full_path}"
    query = websocket.scope.get("query_string", b"").decode()
    if query:
        ws_url += f"?{query}"

    try:
        import websockets

        async with websockets.connect(
            ws_url,
            additional_headers=_build_ws_headers(websocket),
            ping_interval=20,
            ping_timeout=20,
            max_size=2**20,
        ) as upstream_ws:
            # 双向桥接：并发转发两个方向的消息
            done, pending = await asyncio.wait(
                [
                    asyncio.ensure_future(
                        _forward_client_to_upstream(websocket, upstream_ws)
                    ),
                    asyncio.ensure_future(
                        _forward_upstream_to_client(upstream_ws, websocket)
                    ),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
    except (ConnectionRefusedError, OSError) as e:
        logger.warning(
            "WeTTY WebSocket 连接失败: %s (port %d): %s", host_name, port, e
        )
        try:
            await websocket.close(code=1011, reason="WeTTY 连接失败")
        except Exception:
            pass
    except WebSocketDisconnect:
        logger.debug("客户端 WebSocket 断开: %s", host_name)
    except Exception:
        logger.exception("WeTTY WebSocket 代理异常: %s", host_name)
        try:
            await websocket.close(code=1011, reason="内部代理错误")
        except Exception:
            pass


async def _forward_client_to_upstream(client_ws: WebSocket, upstream_ws) -> None:
    """浏览器 → WeTTY：转发客户端消息到上游"""
    try:
        while True:
            data = await client_ws.receive()
            if "text" in data:
                await upstream_ws.send(data["text"])
            elif "bytes" in data:
                await upstream_ws.send(data["bytes"])
            else:
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


async def _forward_upstream_to_client(upstream_ws, client_ws: WebSocket) -> None:
    """WeTTY → 浏览器：转发上游消息到客户端"""
    try:
        async for message in upstream_ws:
            if isinstance(message, str):
                await client_ws.send_text(message)
            elif isinstance(message, bytes):
                await client_ws.send_bytes(message)
    except Exception:
        pass


# ── 工具函数 ───────────────────────────────────

# HTTP 跳级头（不应转发）
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "content-length",  # httpx 会自动处理
    }
)


def _filter_hop_headers(headers: dict[str, str]) -> dict[str, str]:
    """过滤 HTTP 跳级头"""
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


def _build_ws_headers(websocket: WebSocket) -> dict[str, str]:
    """从客户端 WebSocket 提取需要转发的头部"""
    forward = {}
    for key, value in websocket.headers.items():
        lower = key.lower()
        if lower not in {
            "host",
            "upgrade",
            "connection",
            "sec-websocket-key",
            "sec-websocket-version",
            "sec-websocket-extensions",
        }:
            forward[key] = value
    return forward
