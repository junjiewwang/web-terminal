"""PTY 文件传输纯逻辑测试。

覆盖超时计算、自适应 chunk 控制器、batch size 推导、
ACK 解析、标记提取与进度模型。不涉及真实 PTY / 网络。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.services.pty_file_transfer import (
    _BASE_TIMEOUT_SECONDS,
    _CHUNK_SIZE_INIT,
    _CHUNK_SIZE_MAX,
    _CHUNK_SIZE_MIN,
    _MAX_TIMEOUT_SECONDS,
    ChunkSizeController,
    Marker,
    PtyFileTransfer,
    TransferProgress,
    TransferResult,
    TransferState,
    _compute_batch_size,
    _compute_timeout,
)

_MB = 1024 * 1024


# ══════════════════════════════════════════════
# 超时计算
# ══════════════════════════════════════════════


class TestComputeTimeout:
    def test_custom_timeout_wins(self):
        assert _compute_timeout(100 * _MB, custom_timeout=5) == 5.0

    def test_custom_zero_is_respected(self):
        """显式传 0 不应被当成「未指定」。"""
        assert _compute_timeout(_MB, custom_timeout=0) == 0.0

    def test_empty_file_gets_base_timeout(self):
        assert _compute_timeout(0) == float(_BASE_TIMEOUT_SECONDS)

    def test_scales_with_size(self):
        small = _compute_timeout(_MB)
        large = _compute_timeout(5 * _MB)
        assert large > small

    def test_one_mb_adds_per_mb_budget(self):
        assert _compute_timeout(_MB) == pytest.approx(_BASE_TIMEOUT_SECONDS + 30)

    def test_capped_at_maximum(self):
        assert _compute_timeout(10_000 * _MB) == float(_MAX_TIMEOUT_SECONDS)

    def test_never_exceeds_cap(self):
        for size_mb in (1, 10, 100, 1000):
            assert _compute_timeout(size_mb * _MB) <= _MAX_TIMEOUT_SECONDS


# ══════════════════════════════════════════════
# batch size 推导
# ══════════════════════════════════════════════


class TestComputeBatchSize:
    def test_larger_chunk_yields_smaller_batch(self):
        """总量恒定：chunk 翻倍则 batch 减半，避免 PTY 缓冲区压力。"""
        assert _compute_batch_size(32 * 1024) < _compute_batch_size(16 * 1024)

    def test_always_at_least_one(self):
        assert _compute_batch_size(10 * _MB) >= 1

    def test_batch_times_chunk_stays_near_target(self):
        """batch × chunk 的 base64 体积应接近 400KB 目标。"""
        for chunk in (4 * 1024, 16 * 1024, 36 * 1024):
            b64_total = _compute_batch_size(chunk) * chunk * 4 // 3
            assert b64_total <= 400 * 1024 * 1.5


# ══════════════════════════════════════════════
# 自适应 chunk 控制器
# ══════════════════════════════════════════════


class TestChunkSizeControllerInit:
    def test_starts_at_init_size_and_probing(self):
        """初始 16KB 会被对齐到 3 的倍数（16384 → 16383）。"""
        ctl = ChunkSizeController()
        assert ctl.size == ChunkSizeController._align(_CHUNK_SIZE_INIT)
        assert ctl.phase == "probing"

    def test_size_aligned_to_multiple_of_three(self):
        """base64 无填充要求 chunk 是 3 的倍数。"""
        assert ChunkSizeController()._size % 3 == 0

    def test_odd_initial_size_is_aligned_down(self):
        ctl = ChunkSizeController(_size=1000)
        assert ctl.size == 999
        assert ctl.size % 3 == 0

    def test_batch_size_derived_from_chunk(self):
        ctl = ChunkSizeController()
        assert ctl.batch_size == _compute_batch_size(ctl.size)


class TestChunkGrowth:
    def test_grows_after_successful_batch(self):
        ctl = ChunkSizeController()
        before = ctl.size

        ctl.on_batch_ok()

        assert ctl.size > before

    def test_converges_to_max_and_goes_stable(self):
        ctl = ChunkSizeController()
        for _ in range(10):
            ctl.on_batch_ok()

        assert ctl.size == ChunkSizeController._align(_CHUNK_SIZE_MAX)
        assert ctl.phase == "stable"

    def test_never_exceeds_max(self):
        ctl = ChunkSizeController()
        for _ in range(50):
            ctl.on_batch_ok()

        assert ctl.size <= _CHUNK_SIZE_MAX

    def test_stable_phase_stops_growing(self):
        ctl = ChunkSizeController()
        for _ in range(10):
            ctl.on_batch_ok()
        at_max = ctl.size

        ctl.on_batch_ok()

        assert ctl.size == at_max

    def test_stays_aligned_while_growing(self):
        ctl = ChunkSizeController()
        for _ in range(6):
            ctl.on_batch_ok()
            assert ctl.size % 3 == 0


class TestChunkShrink:
    def test_halves_on_failure(self):
        ctl = ChunkSizeController()
        before = ctl.size

        ctl.on_fail()

        assert ctl.size < before
        assert ctl.size == pytest.approx(ChunkSizeController._align(before // 2))

    def test_never_below_minimum(self):
        ctl = ChunkSizeController()
        for _ in range(50):
            ctl.on_fail()

        assert ctl.size >= ChunkSizeController._align(_CHUNK_SIZE_MIN)

    def test_failure_returns_stable_to_probing(self):
        ctl = ChunkSizeController()
        for _ in range(10):
            ctl.on_batch_ok()
        assert ctl.phase == "stable"

        ctl.on_fail()

        assert ctl.phase == "probing", "回退后应重新探测"

    def test_failure_resets_success_streak(self):
        ctl = ChunkSizeController()
        ctl.on_fail()
        assert ctl._consecutive_ok == 0

    def test_recovers_after_failure(self):
        """失败缩小后仍能重新增长回上限。"""
        ctl = ChunkSizeController()
        ctl.on_fail()
        shrunk = ctl.size

        for _ in range(10):
            ctl.on_batch_ok()

        assert ctl.size > shrunk
        assert ctl.phase == "stable"

    def test_batch_size_grows_as_chunk_shrinks(self):
        ctl = ChunkSizeController()
        before = ctl.batch_size

        ctl.on_fail()

        assert ctl.batch_size > before


class TestAlign:
    @pytest.mark.parametrize("raw,expected", [(0, 0), (1, 0), (2, 0), (3, 3), (5, 3), (100, 99)])
    def test_rounds_down_to_multiple_of_three(self, raw, expected):
        assert ChunkSizeController._align(raw) == expected


# ══════════════════════════════════════════════
# ACK 解析
# ══════════════════════════════════════════════


@pytest.fixture
def transfer():
    """PtyFileTransfer 只需要 session 属性即可测试纯解析方法。"""
    return PtyFileTransfer(SimpleNamespace())


class TestParseAck:
    def test_parses_ok(self, transfer):
        assert transfer._parse_ack(f"{Marker.ACK}:0:OK", 0) == Marker.ACK_OK

    def test_parses_corrupt(self, transfer):
        assert transfer._parse_ack(f"{Marker.ACK}:3:CORRUPT", 3) == Marker.ACK_CORRUPT

    def test_parses_seq_err_with_extra_field(self, transfer):
        assert transfer._parse_ack(f"{Marker.ACK}:2:SEQ_ERR:5", 2) == Marker.ACK_SEQ_ERR

    def test_finds_ack_among_noise_lines(self, transfer):
        output = f"garbage\nmore noise\n{Marker.ACK}:7:OK\ntrailing"
        assert transfer._parse_ack(output, 7) == Marker.ACK_OK

    def test_handles_marker_mid_line(self, transfer):
        """回显可能让标记出现在行中间。"""
        assert transfer._parse_ack(f"echo prefix {Marker.ACK}:1:OK", 1) == Marker.ACK_OK

    def test_tolerates_surrounding_whitespace(self, transfer):
        assert transfer._parse_ack(f"   {Marker.ACK}:0:OK   ", 0) == Marker.ACK_OK

    def test_returns_status_even_on_seq_mismatch(self, transfer):
        """序列号不匹配也返回状态，由调用方决策。"""
        assert transfer._parse_ack(f"{Marker.ACK}:99:OK", 0) == Marker.ACK_OK

    def test_empty_output_returns_empty(self, transfer):
        assert transfer._parse_ack("", 0) == ""

    def test_no_ack_marker_returns_empty(self, transfer):
        assert transfer._parse_ack("just some shell output\n$ ", 0) == ""

    def test_missing_status_field_skipped(self, transfer):
        assert transfer._parse_ack(f"{Marker.ACK}:0", 0) == ""

    def test_non_numeric_seq_skipped(self, transfer):
        assert transfer._parse_ack(f"{Marker.ACK}:abc:OK", 0) == ""

    def test_first_valid_ack_wins(self, transfer):
        output = f"{Marker.ACK}:0:OK\n{Marker.ACK}:1:CORRUPT"
        assert transfer._parse_ack(output, 0) == Marker.ACK_OK

    def test_skips_malformed_then_parses_valid(self, transfer):
        output = f"{Marker.ACK}:bad:OK\n{Marker.ACK}:4:CORRUPT"
        assert transfer._parse_ack(output, 4) == Marker.ACK_CORRUPT


# ══════════════════════════════════════════════
# 标记值提取
# ══════════════════════════════════════════════


class TestExtractMarkerValue:
    def test_extracts_value_at_line_start(self):
        text = f"{Marker.RECV_OK}:12345"
        assert PtyFileTransfer._extract_marker_value(text, Marker.RECV_OK) == "12345"

    def test_extracts_value_mid_line(self):
        text = f"shell echo {Marker.MD5_MARKER}:abc123def"
        assert PtyFileTransfer._extract_marker_value(text, Marker.MD5_MARKER) == "abc123def"

    def test_returns_empty_when_absent(self):
        assert PtyFileTransfer._extract_marker_value("no markers", Marker.RECV_OK) == ""

    def test_returns_empty_for_empty_text(self):
        assert PtyFileTransfer._extract_marker_value("", Marker.RECV_OK) == ""

    def test_picks_first_occurrence(self):
        text = f"{Marker.RECV_OK}:first\n{Marker.RECV_OK}:second"
        assert PtyFileTransfer._extract_marker_value(text, Marker.RECV_OK) == "first"

    def test_strips_line_whitespace(self):
        text = f"   {Marker.RECV_OK}:trimmed   "
        assert PtyFileTransfer._extract_marker_value(text, Marker.RECV_OK) == "trimmed"

    def test_empty_value_after_colon(self):
        assert PtyFileTransfer._extract_marker_value(f"{Marker.RECV_OK}:", Marker.RECV_OK) == ""

    def test_scans_past_unrelated_lines(self):
        text = f"noise\nmore\n{Marker.SEND_END}:done"
        assert PtyFileTransfer._extract_marker_value(text, Marker.SEND_END) == "done"


# ══════════════════════════════════════════════
# 进度与结果模型
# ══════════════════════════════════════════════


class TestTransferProgress:
    def test_zero_total_yields_zero_percent(self):
        assert TransferProgress(state=TransferState.IDLE).percentage == 0.0

    def test_half_way(self):
        p = TransferProgress(
            state=TransferState.TRANSFERRING, total_bytes=1000, transferred_bytes=500
        )
        assert p.percentage == 50.0

    def test_capped_at_hundred(self):
        """压缩传输时 transferred 可能超过 total，百分比不能溢出。"""
        p = TransferProgress(
            state=TransferState.TRANSFERRING, total_bytes=100, transferred_bytes=250
        )
        assert p.percentage == 100.0

    def test_compression_ratio_zero_when_uncompressed(self):
        p = TransferProgress(state=TransferState.TRANSFERRING, compressed=False)
        assert p.compression_ratio == 0.0

    def test_compression_ratio_computed(self):
        p = TransferProgress(
            state=TransferState.TRANSFERRING,
            compressed=True,
            original_bytes=1000,
            compressed_bytes=250,
        )
        assert p.compression_ratio == 75.0

    def test_compression_ratio_zero_when_no_original(self):
        p = TransferProgress(
            state=TransferState.TRANSFERRING, compressed=True, original_bytes=0
        )
        assert p.compression_ratio == 0.0

    def test_defaults(self):
        p = TransferProgress(state=TransferState.PREPARING)
        assert p.transferred_bytes == 0
        assert p.sub_step == ""
        assert p.compressed is False


class TestTransferResult:
    def test_defaults_to_completed(self):
        result = TransferResult(success=True, remote_path="/tmp/f")
        assert result.state == TransferState.COMPLETED
        assert result.local_path == ""
        assert result.file_size == 0

    def test_failure_carries_message(self):
        result = TransferResult(
            success=False,
            remote_path="/tmp/f",
            message="连接中断",
            state=TransferState.FAILED,
        )
        assert result.success is False
        assert result.message == "连接中断"


class TestTransferState:
    def test_is_str_enum(self):
        assert TransferState.COMPLETED == "completed"
        assert isinstance(TransferState.FAILED, str)

    def test_all_states_distinct(self):
        values = [s.value for s in TransferState]
        assert len(values) == len(set(values))


class TestMarkerConstants:
    def test_upload_and_download_markers_differ(self):
        assert Marker.RECV_OK != Marker.SEND_END

    def test_ack_status_codes(self):
        assert Marker.ACK_OK == "OK"
        assert Marker.ACK_CORRUPT == "CORRUPT"
        assert Marker.ACK_SEQ_ERR == "SEQ_ERR"

    def test_markers_are_uniquely_delimited(self):
        """标记应有独特前后缀，降低与普通输出冲撞的概率。"""
        for name in ("RECV_READY", "RECV_OK", "ACK", "CHUNK", "SEND_END", "MD5_MARKER"):
            value = getattr(Marker, name)
            assert value.startswith("__FT_")
            assert value.endswith("__")
