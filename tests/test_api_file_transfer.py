"""文件传输 REST API 路由层测试。

聚焦路由层职责：会话查找、大小阈值校验、路径推导、
下载 token 生命周期、取消流程、依赖未注入时的 503。

传输主循环（PtyFileTransfer.upload/download）不在此覆盖，
通过 mock 隔离；这里只验证路由的决策与错误码映射。
"""

from __future__ import annotations

import asyncio
import gzip
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import file_transfer as ft
from src.api.file_transfer import (
    _MAX_RAW_SIZE,
    _MAX_TRANSFER_SIZE,
    _BackgroundCleanup,
    _cleanup_temp_file,
    _interrupt_pty,
    _register_download_token,
)


@pytest.fixture(autouse=True)
def clean_registries():
    """每个用例前后清空模块级注册表，避免互相污染。"""
    ft._download_tokens.clear()
    ft._active_uploads.clear()
    yield
    ft._download_tokens.clear()
    ft._active_uploads.clear()


def make_session(running: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        session_id="s1",
        running=running,
        send_input=AsyncMock(),
    )


@pytest.fixture
def session():
    return make_session()


@pytest.fixture
def client(session, monkeypatch):
    mgr = SimpleNamespace(
        get_session_by_id=lambda sid: session if sid == "s1" else None,
    )
    monkeypatch.setattr(ft, "terminal_manager", mgr)

    app = FastAPI()
    app.include_router(ft.router)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def no_manager_client(monkeypatch):
    monkeypatch.setattr(ft, "terminal_manager", None)
    app = FastAPI()
    app.include_router(ft.router)
    with TestClient(app) as c:
        yield c


# ══════════════════════════════════════════════
# 依赖注入守卫
# ══════════════════════════════════════════════


class TestManagerGuard:
    def test_upload_503_without_manager(self, no_manager_client):
        resp = no_manager_client.post(
            "/api/terminal/s1/upload", files={"file": ("a.txt", b"data")}
        )
        assert resp.status_code == 503

    def test_download_503_without_manager(self, no_manager_client):
        resp = no_manager_client.post(
            "/api/terminal/s1/download", params={"remote_path": "/tmp/a.txt"}
        )
        assert resp.status_code == 503


# ══════════════════════════════════════════════
# 会话查找
# ══════════════════════════════════════════════


class TestSessionLookup:
    def test_upload_unknown_session_404(self, client):
        resp = client.post(
            "/api/terminal/ghost/upload", files={"file": ("a.txt", b"data")}
        )
        assert resp.status_code == 404
        assert "ghost" in resp.json()["detail"]

    def test_download_unknown_session_404(self, client):
        resp = client.post(
            "/api/terminal/ghost/download", params={"remote_path": "/tmp/a.txt"}
        )
        assert resp.status_code == 404

    def test_upload_stopped_session_404(self, monkeypatch):
        stopped = make_session(running=False)
        monkeypatch.setattr(
            ft, "terminal_manager", SimpleNamespace(get_session_by_id=lambda sid: stopped)
        )
        app = FastAPI()
        app.include_router(ft.router)

        with TestClient(app) as c:
            resp = c.post("/api/terminal/s1/upload", files={"file": ("a.txt", b"data")})

        assert resp.status_code == 404, "已停止的会话不能接受传输"


# ══════════════════════════════════════════════
# 上传大小阈值（双阈值策略）
# ══════════════════════════════════════════════


class TestUploadSizeLimits:
    def test_empty_file_rejected_400(self, client):
        resp = client.post("/api/terminal/s1/upload", files={"file": ("a.txt", b"")})

        assert resp.status_code == 400
        assert "为空" in resp.json()["detail"]

    def test_exceeds_raw_cap_413(self, client):
        """超过 50MB 原始上限直接拒绝，不尝试压缩。"""
        oversized = b"\0" * (_MAX_RAW_SIZE + 1)

        resp = client.post("/api/terminal/s1/upload", files={"file": ("big.bin", oversized)})

        assert resp.status_code == 413
        assert "绝对上限" in resp.json()["detail"]

    def test_highly_compressible_large_file_allowed(self, client, monkeypatch):
        """20MB 全零文件压缩后极小，应放行（Optimization #2 双阈值）。"""
        payload = b"\0" * (_MAX_TRANSFER_SIZE + 5 * 1024 * 1024)
        assert len(gzip.compress(payload, compresslevel=6)) < _MAX_TRANSFER_SIZE

        _stub_streaming(monkeypatch)

        resp = client.post(
            "/api/terminal/s1/upload", files={"file": ("zeros.bin", payload)}
        )

        assert resp.status_code != 413, "可压缩的大文件不应被拒绝"
        assert resp.status_code == 200

    def test_incompressible_large_file_rejected_413(self, client):
        """随机数据压不动，超过传输上限应拒绝并建议 SCP。"""
        import os

        payload = os.urandom(_MAX_TRANSFER_SIZE + 2 * 1024 * 1024)

        resp = client.post(
            "/api/terminal/s1/upload", files={"file": ("rand.bin", payload)}
        )

        assert resp.status_code == 413
        assert "SCP" in resp.json()["detail"]


def _stub_streaming(monkeypatch) -> None:
    """把 SSE 流替换成普通响应。

    路由用的是 fastapi.responses.StreamingResponse（模块内 import 名），
    替换它即可在不跑真实传输的前提下走完所有前置校验。
    """
    from starlette.responses import PlainTextResponse

    monkeypatch.setattr(
        ft, "StreamingResponse", lambda gen, **kw: PlainTextResponse("sse-stub")
    )


# ══════════════════════════════════════════════
# 远端路径推导
# ══════════════════════════════════════════════


class TestRemotePathDerivation:
    def test_defaults_to_tmp_when_remote_path_blank(self, client, monkeypatch):
        """未指定远端路径时应回落到 /tmp/<filename>，且临时文件已落盘。"""
        _stub_streaming(monkeypatch)

        resp = client.post("/api/terminal/s1/upload", files={"file": ("report.txt", b"x")})

        assert resp.status_code == 200
        assert (ft._TEMP_DIR / "s1_report.txt").exists()

    def test_explicit_remote_path_accepted(self, client, monkeypatch):
        _stub_streaming(monkeypatch)

        resp = client.post(
            "/api/terminal/s1/upload",
            files={"file": ("a.txt", b"x")},
            data={"remote_path": "/opt/app/a.txt"},
        )

        assert resp.status_code == 200

    def test_whitespace_remote_path_treated_as_blank(self, client, monkeypatch):
        _stub_streaming(monkeypatch)

        resp = client.post(
            "/api/terminal/s1/upload",
            files={"file": ("ws.txt", b"x")},
            data={"remote_path": "   "},
        )

        assert resp.status_code == 200
        assert (ft._TEMP_DIR / "s1_ws.txt").exists()


class TestDownloadPathValidation:
    @pytest.mark.parametrize("bad_path", ["/", ".", ""])
    def test_pathless_input_rejected_400(self, client, bad_path):
        """提取不出文件名的路径应 400 而非 500。

        注意 '/var/log/' 的 Path.name 是 'log'（尾斜杠会被规范化掉），
        真正取不到名字的只有 '/'、'.' 和空串。
        """
        resp = client.post(
            "/api/terminal/s1/download", params={"remote_path": bad_path}
        )

        assert resp.status_code == 400
        assert "无效" in resp.json()["detail"]

    def test_remote_path_is_required(self, client):
        assert client.post("/api/terminal/s1/download").status_code == 422


# ══════════════════════════════════════════════
# 下载 token 生命周期
# ══════════════════════════════════════════════


class TestDownloadToken:
    def test_register_returns_distinct_tokens(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("x")

        t1 = _register_download_token(f, "a.txt")
        t2 = _register_download_token(f, "a.txt")

        assert t1 != t2
        assert t1 in ft._download_tokens

    def test_register_purges_expired_entries(self, tmp_path):
        """注册新 token 时应顺带清理过期条目并删除其临时文件。"""
        stale = tmp_path / "stale.txt"
        stale.write_text("old")
        ft._download_tokens["expired"] = (stale, "stale.txt", time.time() - 1)

        fresh = tmp_path / "fresh.txt"
        fresh.write_text("new")
        _register_download_token(fresh, "fresh.txt")

        assert "expired" not in ft._download_tokens
        assert not stale.exists(), "过期 token 的临时文件应被删除"

    def test_fetch_returns_file_content(self, client, tmp_path):
        f = tmp_path / "payload.txt"
        f.write_text("hello-download")
        token = _register_download_token(f, "payload.txt")

        resp = client.get(f"/api/terminal/s1/download/{token}")

        assert resp.status_code == 200
        assert resp.content == b"hello-download"

    def test_token_is_single_use(self, client, tmp_path):
        f = tmp_path / "once.txt"
        f.write_text("data")
        token = _register_download_token(f, "once.txt")

        first = client.get(f"/api/terminal/s1/download/{token}")
        second = client.get(f"/api/terminal/s1/download/{token}")

        assert first.status_code == 200
        assert second.status_code == 404, "token 必须一次性失效"

    def test_unknown_token_404(self, client):
        assert client.get("/api/terminal/s1/download/nope").status_code == 404

    def test_expired_token_410(self, client, tmp_path):
        f = tmp_path / "old.txt"
        f.write_text("data")
        ft._download_tokens["stale"] = (f, "old.txt", time.time() - 1)

        resp = client.get("/api/terminal/s1/download/stale")

        assert resp.status_code == 410
        assert not f.exists(), "过期时应清理临时文件"

    def test_missing_temp_file_404(self, client, tmp_path):
        ghost = tmp_path / "vanished.txt"
        token = _register_download_token(ghost, "vanished.txt")

        assert client.get(f"/api/terminal/s1/download/{token}").status_code == 404

    def test_content_disposition_carries_filename(self, client, tmp_path):
        f = tmp_path / "internal-name.txt"
        f.write_text("x")
        token = _register_download_token(f, "friendly.txt")

        resp = client.get(f"/api/terminal/s1/download/{token}")

        assert "friendly.txt" in resp.headers.get("content-disposition", "")


# ══════════════════════════════════════════════
# 取消上传
# ══════════════════════════════════════════════


class TestCancelUpload:
    def test_no_active_task_returns_false(self, client):
        body = client.post("/api/terminal/s1/upload/cancel").json()

        assert body == {"cancelled": False, "message": "无活跃上传任务"}

    @pytest.mark.asyncio
    async def test_cancels_running_task_and_deregisters(self, session, monkeypatch):
        monkeypatch.setattr(
            ft,
            "terminal_manager",
            SimpleNamespace(get_session_by_id=lambda sid: session),
        )

        async def long_running():
            await asyncio.sleep(30)

        task = asyncio.create_task(long_running())
        ft._active_uploads["s1"] = task

        result = await ft.cancel_upload("s1")

        assert result["cancelled"] is True
        assert task.cancelled() or task.done()
        assert "s1" not in ft._active_uploads

    @pytest.mark.asyncio
    async def test_sends_interrupt_sequence_to_pty(self, session, monkeypatch):
        """取消后必须先 stty sane 再发 __FT_EOF__，否则 Ctrl+C 会打断 SSH。"""
        monkeypatch.setattr(
            ft,
            "terminal_manager",
            SimpleNamespace(get_session_by_id=lambda sid: session),
        )
        monkeypatch.setattr(ft.asyncio, "sleep", AsyncMock())

        async def noop():
            return None

        ft._active_uploads["s1"] = asyncio.create_task(noop())
        await ft.cancel_upload("s1")

        sent = [c.args[0] for c in session.send_input.await_args_list]
        assert any("stty sane" in s for s in sent)
        assert any("__FT_EOF__" in s for s in sent)
        assert sent.index(next(s for s in sent if "stty sane" in s)) < sent.index(
            next(s for s in sent if "__FT_EOF__" in s)
        ), "stty sane 必须在 __FT_EOF__ 之前"


class TestInterruptPty:
    @pytest.mark.asyncio
    async def test_sends_three_step_sequence(self, session, monkeypatch):
        monkeypatch.setattr(ft.asyncio, "sleep", AsyncMock())

        await _interrupt_pty(session)

        assert session.send_input.await_count == 3

    @pytest.mark.asyncio
    async def test_connection_error_swallowed(self, monkeypatch):
        """PTY 已断开时中断失败不应向上抛。"""
        monkeypatch.setattr(ft.asyncio, "sleep", AsyncMock())
        broken = SimpleNamespace(send_input=AsyncMock(side_effect=ConnectionError("gone")))

        await _interrupt_pty(broken)  # 不应抛出

    @pytest.mark.asyncio
    async def test_os_error_swallowed(self, monkeypatch):
        monkeypatch.setattr(ft.asyncio, "sleep", AsyncMock())
        broken = SimpleNamespace(send_input=AsyncMock(side_effect=OSError("bad fd")))

        await _interrupt_pty(broken)


# ══════════════════════════════════════════════
# 临时文件清理
# ══════════════════════════════════════════════


class TestCleanupTempFile:
    @pytest.mark.asyncio
    async def test_removes_existing_file(self, tmp_path):
        f = tmp_path / "temp.bin"
        f.write_text("x")

        await _cleanup_temp_file(f)()

        assert not f.exists()

    @pytest.mark.asyncio
    async def test_missing_file_is_noop(self, tmp_path):
        await _cleanup_temp_file(tmp_path / "never-existed")()

    def test_factory_returns_background_cleanup(self, tmp_path):
        assert isinstance(_cleanup_temp_file(tmp_path / "x"), _BackgroundCleanup)


# ══════════════════════════════════════════════
# 模块常量
# ══════════════════════════════════════════════


class TestSizeConstants:
    def test_raw_cap_exceeds_transfer_cap(self):
        """原始上限必须大于传输上限，否则压缩放行逻辑无意义。"""
        assert _MAX_RAW_SIZE > _MAX_TRANSFER_SIZE

    def test_documented_values(self):
        assert _MAX_RAW_SIZE == 50 * 1024 * 1024
        assert _MAX_TRANSFER_SIZE == 10 * 1024 * 1024

    def test_temp_dir_exists(self):
        assert ft._TEMP_DIR.exists()
        assert ft._TEMP_DIR.is_dir()
