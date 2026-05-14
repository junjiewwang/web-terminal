"""文件传输 REST API 端点

提供浏览器端文件上传/下载能力：
- POST /api/terminal/{session_id}/upload          浏览器上传文件到远端节点（SSE 流式进度）
- POST /api/terminal/{session_id}/upload/cancel    取消正在进行的上传（中断 PTY 传输）
- GET  /api/terminal/{session_id}/download         从远端节点下载文件到浏览器

架构：
  浏览器 ─[multipart/form-data]─> 此 API ─[PtyFileTransfer]─> PTY ─> 远端节点
  浏览器 <─[SSE progress events]── 此 API <─[on_progress callback]─ PtyFileTransfer
  浏览器 <─[file stream]─────────── 此 API <─[PtyFileTransfer]─< PTY <─ 远端节点

取消机制（三层）：
  ① 前端 abort SSE fetch → 后端 generator 被中断
  ② 后端 finally 中 cancel upload_task → 停止 PtyFileTransfer chunk 发送
  ③ 后端向 PTY 发送 __FT_EOF__ → 远端 ft_recv 正常退出并恢复终端
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from src.services.pty_file_transfer import (
    PtyFileTransfer,
    TransferProgress,
    TransferResult,
    TransferState,
)
from src.services.terminal_manager import TerminalManager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["file-transfer"])

# 注入点，由 main.py lifespan 设置
terminal_manager: TerminalManager | None = None

# 临时文件存储目录
_TEMP_DIR = Path(tempfile.gettempdir()) / "wetty-file-transfer"
_TEMP_DIR.mkdir(parents=True, exist_ok=True)

# 上传文件大小限制（10MB，与 PtyFileTransfer 推荐上限一致）
_MAX_UPLOAD_SIZE = 10 * 1024 * 1024

# 活跃上传任务注册表：session_id → asyncio.Task
# 用于取消 API 查找并 cancel 正在进行的上传
_active_uploads: dict[str, asyncio.Task] = {}

# SSE 心跳间隔（秒）：校验阶段无进度事件时，每隔此间隔发送心跳
_SSE_HEARTBEAT_INTERVAL = 1.5


def _get_terminal_manager() -> TerminalManager:
    if terminal_manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="终端管理器未初始化",
        )
    return terminal_manager


# ── 响应模型 ──────────────────────────────────

class UploadResponse(BaseModel):
    """文件上传响应"""
    success: bool
    remote_path: str
    file_size: int
    md5: str
    message: str


class DownloadResponse(BaseModel):
    """文件下载元信息响应"""
    success: bool
    filename: str
    file_size: int
    md5: str
    message: str


# ── 上传端点 ──────────────────────────────────

@router.post(
    "/api/terminal/{session_id}/upload",
    summary="上传文件到远端节点（SSE 流式进度）",
)
async def upload_file(
    session_id: str,
    file: UploadFile = File(..., description="要上传的文件"),
    remote_path: str = Form(
        default="",
        description="远端目标路径（为空时使用 /tmp/<filename>）",
    ),
):
    """接收浏览器上传的文件，通过 PTY 通道传输到远端节点。

    返回 SSE 事件流（text/event-stream），实时推送传输进度：
    - event: progress  — 传输进度（含 state/chunks_sent/chunks_total/transferred/total）
    - event: complete  — 传输完成（含最终结果）
    - event: error     — 传输失败

    心跳机制：校验阶段（verifying）无进度事件时，每 1.5s 自动推送心跳 progress，
    前端可据此展示"校验中..."状态，避免看起来卡住。

    取消机制：前端 abort fetch 或调用 cancel API → 后端 cancel task + Ctrl+C PTY。
    """
    mgr = _get_terminal_manager()

    # 查找会话
    session = mgr.get_session_by_id(session_id)
    if not session or not session.running:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"终端会话不存在或已关闭: {session_id}",
        )

    # 读取上传文件
    content = await file.read()
    if len(content) > _MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"文件大小 {len(content)} 字节超过限制 {_MAX_UPLOAD_SIZE} 字节（10MB）",
        )

    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="上传文件为空",
        )

    # 确定远端路径
    filename = file.filename or "uploaded_file"
    if not remote_path.strip():
        remote_path = f"/tmp/{filename}"

    # 保存到临时文件
    temp_file = _TEMP_DIR / f"{session_id}_{filename}"
    temp_file.write_bytes(content)

    async def _sse_upload_generator():
        """SSE 事件生成器：执行上传并实时推送进度 + 心跳"""
        # 用 asyncio.Queue 桥接 on_progress callback 和异步 generator
        progress_queue: asyncio.Queue[TransferProgress | None] = asyncio.Queue()
        # 最近一次进度快照，用于心跳重发
        last_progress: TransferProgress | None = None

        def _on_progress(p: TransferProgress) -> None:
            """PtyFileTransfer 的进度回调（在 async 上下文中调用）"""
            progress_queue.put_nowait(p)

        async def _do_upload() -> TransferResult:
            """在后台执行实际上传"""
            try:
                await _ensure_ft_snippet_loaded(session)
                transfer = PtyFileTransfer(session)
                return await transfer.upload(
                    local_path=str(temp_file),
                    remote_path=remote_path,
                    verify=True,
                    on_progress=_on_progress,
                )
            except asyncio.CancelledError:
                logger.info("上传任务被取消: %s → %s", filename, remote_path)
                return TransferResult(
                    success=False,
                    remote_path=remote_path,
                    local_path=str(temp_file),
                    file_size=len(content),
                    message="上传已取消",
                    state=TransferState.FAILED,
                )
            except Exception as e:
                return TransferResult(
                    success=False,
                    remote_path=remote_path,
                    local_path=str(temp_file),
                    file_size=len(content),
                    message=f"传输异常: {e}",
                    state=TransferState.FAILED,
                )
            finally:
                # 通知生成器上传任务结束
                progress_queue.put_nowait(None)

        # 启动上传任务并注册到活跃表
        upload_task = asyncio.create_task(_do_upload())
        _active_uploads[session_id] = upload_task

        def _format_progress_event(p: TransferProgress) -> str:
            """格式化进度事件为 SSE 字符串"""
            event_data = {
                "state": p.state.value,
                "transferred": p.transferred_bytes,
                "total": p.total_bytes,
                "chunks_sent": p.chunks_sent,
                "chunks_total": p.chunks_total,
                "percentage": round(p.percentage, 1),
                "sub_step": p.sub_step,
                # ★ O2 压缩信息
                "compressed": p.compressed,
                "original_bytes": p.original_bytes,
                "compressed_bytes": p.compressed_bytes,
                "compression_ratio": round(p.compression_ratio, 1),
            }
            return f"event: progress\ndata: {json.dumps(event_data)}\n\n"

        # 持续读取进度队列并输出 SSE 事件（带心跳超时）
        try:
            while True:
                try:
                    progress = await asyncio.wait_for(
                        progress_queue.get(),
                        timeout=_SSE_HEARTBEAT_INTERVAL,
                    )
                except asyncio.TimeoutError:
                    # 超时：无新进度事件（校验阶段等），发送心跳
                    if last_progress is not None:
                        yield _format_progress_event(last_progress)
                    continue

                if progress is None:
                    break  # 上传任务结束

                last_progress = progress
                yield _format_progress_event(progress)

            # 获取最终结果
            result = await upload_task
            result_data = {
                "success": result.success,
                "remote_path": result.remote_path,
                "file_size": result.file_size,
                "md5": result.md5,
                "message": result.message,
            }

            if result.success:
                yield f"event: complete\ndata: {json.dumps(result_data)}\n\n"
            else:
                yield f"event: error\ndata: {json.dumps(result_data)}\n\n"

        except (asyncio.CancelledError, GeneratorExit):
            # 前端断开 SSE 连接（abort fetch）或服务端主动取消
            logger.info("SSE 流中断，取消上传任务: session=%s", session_id)
            upload_task.cancel()
            try:
                await upload_task
            except (asyncio.CancelledError, Exception):
                pass
            # 向 PTY 发送 __FT_EOF__ 让远端 ft_recv 正常退出
            await _interrupt_pty(session)
        except Exception as e:
            error_data = {"success": False, "message": f"SSE 流异常: {e}"}
            yield f"event: error\ndata: {json.dumps(error_data)}\n\n"
        finally:
            # 从活跃表移除
            _active_uploads.pop(session_id, None)
            # 清理临时文件
            if temp_file.exists():
                temp_file.unlink()

    return StreamingResponse(
        _sse_upload_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁止 Nginx 缓冲 SSE
        },
    )


# ── 取消上传端点 ──────────────────────────────

@router.post(
    "/api/terminal/{session_id}/upload/cancel",
    summary="取消正在进行的上传",
)
async def cancel_upload(session_id: str):
    """取消指定会话的正在进行的文件上传。

    三层取消机制：
    1. cancel asyncio.Task → 停止 PtyFileTransfer chunk 发送
    2. 向 PTY 发送 __FT_EOF__ → 远端 ft_recv 正常退出并恢复终端
    3. 远端 ft_recv 读到 __FT_EOF__ → break 退出 → _ft_cleanup 恢复 stty
    """
    task = _active_uploads.get(session_id)
    if task is None:
        return {"cancelled": False, "message": "无活跃上传任务"}

    # 1. 取消 asyncio task
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass

    # 2. 向 PTY 发送 Ctrl+C 中断远端 shell
    mgr = _get_terminal_manager()
    session = mgr.get_session_by_id(session_id)
    if session and session.running:
        await _interrupt_pty(session)

    _active_uploads.pop(session_id, None)
    logger.info("上传已取消: session=%s", session_id)
    return {"cancelled": True, "message": "上传已取消"}


async def _interrupt_pty(session) -> None:
    """中断远端 ft_recv 并恢复终端。

    ★ Bugfix #18: 取消上传后 Ctrl+C 穿透 SSH 断连 ★

    根因：ft_recv 启动时执行 `stty -echo -icanon`，进入非规范模式。
    在此模式下：
    - `\x03` (Ctrl+C) 不被 line discipline 解释为 SIGINT
    - 而是直接透传到 SSH 传输层，导致 SSH 连接关闭

    修复流程（三步走）：
    1. 先发 `stty sane` 恢复终端为规范模式（确保后续 Ctrl+C 正常工作）
    2. 发送 `__FT_EOF__` 让 ft_recv 的 while-read 循环正常退出
    3. 发送回车恢复 prompt

    如果 ft_recv 已经退出（比如 _ft_signal_handler 已触发），
    这些命令都是无害的（stty sane 本身无害，__FT_EOF__ 会被当作未知命令）。
    """
    try:
        # 步骤 1：恢复终端模式（最关键——解除 -icanon 对 Ctrl+C 的影响）
        await session.send_input("stty sane 2>/dev/null\n")
        await asyncio.sleep(0.2)
        # 步骤 2：通知 ft_recv 正常退出
        await session.send_input("__FT_EOF__\n")
        await asyncio.sleep(0.5)
        # 步骤 3：恢复 prompt
        await session.send_input("\n")
        logger.debug("已向 PTY 发送 stty sane + __FT_EOF__ 中断远端传输")
    except (ConnectionError, OSError) as e:
        logger.warning("发送中断命令失败: %s", e)


# ── 下载端点（两步式：SSE 进度 + token 取文件）──

# 已完成下载的临时文件注册表：token → (file_path, filename, expires_at)
import time
import secrets

_download_tokens: dict[str, tuple[Path, str, float]] = {}
_DOWNLOAD_TOKEN_TTL = 120  # token 有效期 120 秒


def _register_download_token(file_path: Path, filename: str) -> str:
    """注册已下载的临时文件，返回一次性 token"""
    token = secrets.token_urlsafe(16)
    _download_tokens[token] = (file_path, filename, time.time() + _DOWNLOAD_TOKEN_TTL)
    # 清理过期 token
    now = time.time()
    expired = [k for k, (_, _, exp) in _download_tokens.items() if exp < now]
    for k in expired:
        p, _, _ = _download_tokens.pop(k)
        if p.exists():
            p.unlink()
    return token


@router.post(
    "/api/terminal/{session_id}/download",
    summary="从远端节点下载文件（SSE 流式进度）",
)
async def download_file(
    session_id: str,
    remote_path: str = Query(..., description="远端文件路径"),
):
    """从远端节点下载文件，返回 SSE 事件流实时推送下载进度。

    SSE 事件：
    - event: progress  — 下载进度（含 state/transferred/total/percentage）
    - event: complete  — 下载完成（含 token，前端用 token 发 GET 请求取文件）
    - event: error     — 下载失败

    前端流程：
    1. POST /download?remote_path=... → 读取 SSE 流显示进度
    2. 收到 complete 事件 → GET /download/{token} 触发浏览器下载
    """
    mgr = _get_terminal_manager()

    # 查找会话
    session = mgr.get_session_by_id(session_id)
    if not session or not session.running:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"终端会话不存在或已关闭: {session_id}",
        )

    # 从 remote_path 提取文件名
    filename = Path(remote_path).name
    if not filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的远端路径",
        )

    # 临时保存路径
    temp_file = _TEMP_DIR / f"{session_id}_dl_{filename}"

    async def _sse_download_generator():
        """SSE 事件生成器：执行下载并实时推送进度"""
        progress_queue: asyncio.Queue[TransferProgress | None] = asyncio.Queue()

        def _on_progress(p: TransferProgress) -> None:
            progress_queue.put_nowait(p)

        async def _do_download() -> TransferResult:
            try:
                await _ensure_ft_snippet_loaded(session)
                transfer = PtyFileTransfer(session)
                return await transfer.download(
                    remote_path=remote_path,
                    local_path=str(temp_file),
                    verify=True,
                    on_progress=_on_progress,
                )
            except asyncio.CancelledError:
                return TransferResult(
                    success=False, remote_path=remote_path,
                    local_path=str(temp_file),
                    message="下载已取消",
                    state=TransferState.FAILED,
                )
            except Exception as e:
                return TransferResult(
                    success=False, remote_path=remote_path,
                    local_path=str(temp_file),
                    message=f"下载异常: {e}",
                    state=TransferState.FAILED,
                )
            finally:
                progress_queue.put_nowait(None)

        download_task = asyncio.create_task(_do_download())

        def _format_progress(p: TransferProgress) -> str:
            event_data = {
                "state": p.state.value,
                "transferred": p.transferred_bytes,
                "total": p.total_bytes,
                "chunks_sent": p.chunks_sent,
                "chunks_total": p.chunks_total,
                "percentage": round(p.percentage, 1),
                "sub_step": p.sub_step,
                # ★ O12 压缩信息（与上传 SSE 一致）
                "compressed": p.compressed,
                "original_bytes": p.original_bytes,
                "compressed_bytes": p.compressed_bytes,
                "compression_ratio": round(p.compression_ratio, 1),
            }
            return f"event: progress\ndata: {json.dumps(event_data)}\n\n"

        try:
            while True:
                try:
                    progress = await asyncio.wait_for(
                        progress_queue.get(),
                        timeout=_SSE_HEARTBEAT_INTERVAL,
                    )
                except asyncio.TimeoutError:
                    # 心跳
                    yield f"event: heartbeat\ndata: {{}}\n\n"
                    continue

                if progress is None:
                    break

                yield _format_progress(progress)

            # 获取最终结果
            result = await download_task

            if result.success:
                # 注册临时文件 token
                token = _register_download_token(temp_file, filename)
                result_data = {
                    "success": True,
                    "token": token,
                    "filename": filename,
                    "file_size": result.file_size,
                    "md5": result.md5,
                    "message": result.message,
                }
                yield f"event: complete\ndata: {json.dumps(result_data)}\n\n"
            else:
                # 下载失败，清理临时文件
                if temp_file.exists():
                    temp_file.unlink()
                error_data = {
                    "success": False,
                    "message": result.message,
                }
                yield f"event: error\ndata: {json.dumps(error_data)}\n\n"

        except (asyncio.CancelledError, GeneratorExit):
            download_task.cancel()
            try:
                await download_task
            except (asyncio.CancelledError, Exception):
                pass
            if temp_file.exists():
                temp_file.unlink()
        except Exception as e:
            error_data = {"success": False, "message": f"SSE 流异常: {e}"}
            yield f"event: error\ndata: {json.dumps(error_data)}\n\n"
            if temp_file.exists():
                temp_file.unlink()

    return StreamingResponse(
        _sse_download_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/api/terminal/{session_id}/download/{token}",
    summary="通过 token 获取已下载的文件",
)
async def download_file_by_token(
    session_id: str,
    token: str,
):
    """用下载完成后返回的 token 获取文件。

    token 一次性有效，使用后自动清理。
    """
    entry = _download_tokens.pop(token, None)
    if not entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="下载 token 无效或已过期",
        )

    file_path, filename, expires_at = entry
    if time.time() > expires_at:
        if file_path.exists():
            file_path.unlink()
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="下载 token 已过期",
        )

    if not file_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="临时文件不存在",
        )

    return FileResponse(
        path=str(file_path),
        filename=filename,
        media_type="application/octet-stream",
        background=_cleanup_temp_file(file_path),
    )


# ── 内部工具函数 ──────────────────────────────

async def _ensure_ft_snippet_loaded(session) -> None:
    """确保 ft (file-transfer) snippet 已加载到远端会话且版本最新。

    委托给公共方法 ensure_snippet_loaded()，仅负责错误格式转换（→ HTTPException）。

    ★ Bugfix #22: 注入前抑制 PTY 回显，避免 base64 刷屏终端（逻辑在公共方法中）。
    """
    from src.services.snippet_registry import ensure_snippet_loaded

    # 获取全局 snippet_registry 实例
    from src.main import snippet_registry

    if not snippet_registry:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Snippet Registry 未初始化，无法执行文件传输",
        )

    error = await ensure_snippet_loaded(session, snippet_registry, "ft")
    if error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=error,
        )


class _BackgroundCleanup:
    """FileResponse 的后台清理任务"""

    def __init__(self, path: Path):
        self._path = path

    async def __call__(self):
        if self._path.exists():
            self._path.unlink()


def _cleanup_temp_file(path: Path) -> _BackgroundCleanup:
    """创建文件下载完成后的清理任务"""
    return _BackgroundCleanup(path)
