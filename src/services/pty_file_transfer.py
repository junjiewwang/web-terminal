"""PTY 文件传输协调器

基于 PTY 通道的文件传输服务，通过 base64 编码 + 标记协议实现
多跳 SSH 节点之间的文件上传/下载，无需 SCP/SFTP 直连。

架构：
  Agent → PtyFileTransfer.upload()  → send_input(base64 chunks) → ft_recv (远端)
  Agent ← PtyFileTransfer.download() ← wait_for(markers)       ← ft_send (远端)

协议标记（与 file-transfer-snippet.sh 一一对应）：
  接收方向 (upload):
    __FT_RECV_READY__          远端准备好接收
    __FT_RECV_OK__:<bytes>     接收完成
    __FT_RECV_ERR__:<msg>      接收失败

  发送方向 (download):
    __FT_SEND_BEGIN__:<size>   开始发送，附带文件大小
    __FT_CHUNK__:<data>        base64 数据块
    __FT_SEND_END__:<md5>      发送完成，附带 MD5

  校验（inline 命令，不依赖 snippet 函数）:
    __FT_MD5__:<md5>           MD5 校验结果

设计原则：
  - 与 SnippetRegistry + TerminalSession 松耦合，通过接口调用
  - 状态机驱动，每个操作有明确的状态转换
  - 超时可配置，默认基于文件大小动态计算
  - 传输过程中通过 EventBus 发布进度事件
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import logging
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


# ── 协议常量（与 file-transfer-snippet.sh 对应）──

class Marker:
    """PTY 传输协议标记常量"""

    # 上传（ft_recv 端）
    RECV_READY = "__FT_RECV_READY__"
    RECV_OK = "__FT_RECV_OK__"
    RECV_ERR = "__FT_RECV_ERR__"
    RECV_PROGRESS = "__FT_RECV_PROGRESS__"

    # ACK 确认（ft_recv → Python）
    ACK = "__FT_ACK__"
    # ACK 状态码
    ACK_OK = "OK"
    ACK_CORRUPT = "CORRUPT"
    ACK_SEQ_ERR = "SEQ_ERR"

    # 下载（ft_send 端）
    SEND_BEGIN = "__FT_SEND_BEGIN__"
    CHUNK = "__FT_CHUNK__"
    SEND_END = "__FT_SEND_END__"
    SEND_ERR = "__FT_SEND_ERR__"
    EOF = "__FT_EOF__"

    # 校验（O4: 改用 inline 命令，不再依赖 ft_checksum 函数）
    MD5_MARKER = "__FT_MD5__"


class TransferState(str, Enum):
    """传输状态"""
    IDLE = "idle"
    PREPARING = "preparing"      # 正在注入 snippet / 发命令
    TRANSFERRING = "transferring" # 数据传输中
    VERIFYING = "verifying"       # 校验中
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class TransferResult:
    """传输结果"""
    success: bool
    remote_path: str
    local_path: str = ""
    file_size: int = 0
    md5: str = ""
    message: str = ""
    state: TransferState = TransferState.COMPLETED


@dataclass
class TransferProgress:
    """传输进度"""
    state: TransferState
    total_bytes: int = 0
    transferred_bytes: int = 0
    chunks_sent: int = 0
    chunks_total: int = 0
    # 校验阶段子步骤：
    #   "decoding" — 远端正在 base64 解码并写入文件（压缩模式下含 gunzip 解压）
    #   "checksumming" — 远端正在计算 MD5 校验和
    #   空字符串 — 非校验阶段
    sub_step: str = ""
    # ★ 压缩传输信息（O2 优化）
    compressed: bool = False       # 是否启用了 gzip 压缩传输
    original_bytes: int = 0        # 原始文件大小（未压缩）
    compressed_bytes: int = 0      # 压缩后大小（0 表示未压缩）

    @property
    def percentage(self) -> float:
        if self.total_bytes == 0:
            return 0.0
        return min(100.0, self.transferred_bytes / self.total_bytes * 100)

    @property
    def compression_ratio(self) -> float:
        """压缩率（百分比），如 62.5 表示压缩掉了 62.5% 的数据"""
        if not self.compressed or self.original_bytes == 0:
            return 0.0
        return (1 - self.compressed_bytes / self.original_bytes) * 100


# 进度回调：上传/下载过程中每发送/接收一个 chunk 调用一次
ProgressCallback = Callable[[TransferProgress], None]


# ── 超时计算 ──────────────────────────────────

# 基础超时 + 每 MB 额外超时
_BASE_TIMEOUT_SECONDS = 60
_TIMEOUT_PER_MB = 30  # 每 MB 给 30 秒（PTY 通道速率约 50-200 KB/s）
_MAX_TIMEOUT_SECONDS = 600  # 最大 10 分钟


def _compute_timeout(file_size: int, custom_timeout: int | None = None) -> float:
    """根据文件大小动态计算超时时间"""
    if custom_timeout is not None:
        return float(custom_timeout)
    mb = file_size / (1024 * 1024)
    timeout = _BASE_TIMEOUT_SECONDS + mb * _TIMEOUT_PER_MB
    return min(timeout, _MAX_TIMEOUT_SECONDS)


# ── 分块参数 ──────────────────────────────────

# 默认块大小：36KB 原始数据 → 48KB base64
#
# ★ -icanon 大 chunk + ACK 确认协议 ★
# Shell 端使用 stty -icanon 解除 MAX_CANON (~4096字节) 行长度限制，
# 允许使用大 chunk 大幅减少传输轮次和 ACK echo 次数。
#
# 之前放弃 -icanon 是因为高速写入导致约 11% 行合并损坏，且无法检测恢复。
# 现在有了 ACK 确认协议：
#   - 每个 chunk 带序列号 + base64 校验
#   - 行合并 → CORRUPT/SEQ_ERR → 自动重传（自适应延迟翻倍）
#   - 即使偶发行合并也能自动恢复
#
# ★ Bugfix #15: chunk 0 失败 — PTY 内核缓冲区溢出 ★
# 之前 48KB raw → 64KB base64 + 前缀 ≈ 65.5KB，刚好超过部分系统的
# PTY 内核缓冲区大小（64KB = 65536 字节），导致行数据被截断。
# Shell 端 `read -r` 只能读到不完整的行（末尾没有 \n），一直在等完整行，
# 而 Python 端也在等 ACK，两边互相死锁 → chunk 0 就失败。
# 降为 36KB raw → 48KB base64 + 前缀 ≈ 49KB，远低于 64KB 限制。
#
# 36KB = 36864 → 取 3 的倍数 = 36864 (36864 / 3 = 12288 整除 ✅)
# base64 编码后：36864 × 4/3 = 49152 = 48KB
_DEFAULT_CHUNK_SIZE = 36 * 1024  # 36KB raw → 48KB base64

# ── ACK 确认参数 ─────────────────────────────
# O9 批量 ACK：一次发送 _BATCH_SIZE 个 chunk，然后批量收集 ACK。
# Shell 端仍逐 chunk 回复 ACK（保留精确错误定位），Python 端批量发+批量收。
# 节省的是 Python 端 ACK 等待间的空闲时间（pipeline overlap）。
_BATCH_SIZE = 5            # 每批发送 chunk 数（PTY 缓冲区安全范围内：5×49KB=245KB 文本数据）
_ACK_TIMEOUT = 15.0        # 批量 ACK 总超时（秒），需覆盖 Shell 处理整批的时间
_MAX_RETRIES = 5           # 每个 chunk 最大重传次数

# ── 动态自适应延迟参数 ─────────────────────────
# 核心思路：ACK 后需要一个延迟让 PTY 内核缓冲区清空，但最佳延迟值因环境而异。
# 采用类 TCP 拥塞控制的自适应策略：
#   - 初始延迟：30ms（大 chunk 需要更多缓冲区处理时间）
#   - 发生行合并（CORRUPT/SEQ_ERR/超时）→ 延迟翻倍（回退）
#   - 连续成功 _SPEEDUP_THRESHOLD 个 chunk → 延迟减半（加速）
#   - 延迟上限：500ms（大 chunk 回退空间更大）
#   - 延迟下限：5ms
_POST_ACK_DELAY_INIT = 0.03   # 初始 30ms（大 chunk 比小 chunk 需要更多处理时间）
_POST_ACK_DELAY_MIN = 0.005   # 下限 5ms
_POST_ACK_DELAY_MAX = 0.5     # 上限 500ms
_SPEEDUP_THRESHOLD = 20        # 连续成功 20 个 chunk 后尝试减半（大 chunk 总数少，更快加速）

# 最大推荐文件大小（PTY 通道传输本身较慢，大文件建议用 SCP/SFTP）
_MAX_RECOMMENDED_SIZE = 10 * 1024 * 1024  # 10MB

# ── 智能压缩参数（O2 优化）──────────────────────
# 传输前尝试 gzip 压缩，只有压缩率 > 阈值时才使用压缩传输。
# 对于文本/日志/配置文件等可压缩内容，可减少 50-80% 传输数据量。
# 对于已压缩文件（.zip/.tar.gz/.jpg 等），gzip 几乎无效，自动跳过。
_GZIP_COMPRESS_LEVEL = 6          # gzip 压缩级别（1-9，6 是速度/压缩率平衡点）
_COMPRESSION_THRESHOLD = 0.80     # 压缩后大小 < 原始大小 × 0.80 才使用（至少节省 20%）


# ── 核心传输类 ──────────────────────────────────

class PtyFileTransfer:
    """PTY 文件传输协调器

    与 TerminalSession 配合，通过 PTY 通道实现文件传输。
    需要先通过 SnippetRegistry 加载 ft 域脚本到远端。

    使用方式：
        transfer = PtyFileTransfer(session)
        result = await transfer.upload("/local/file.tar.gz", "/remote/path/file.tar.gz")
        result = await transfer.download("/remote/path/file.log", "/local/file.log")
    """

    def __init__(self, session, chunk_size: int = _DEFAULT_CHUNK_SIZE) -> None:
        """初始化传输器

        Args:
            session: TerminalSession 实例（需要 send_input / wait_for / send_command 方法）
            chunk_size: 分块大小（字节），默认 36KB（-icanon 模式 + ACK 确认）
        """
        self._session = session
        self._chunk_size = chunk_size
        # 确保 chunk_size 是 3 的倍数（避免 base64 填充碎片）
        self._chunk_size = self._chunk_size // 3 * 3

    # ── 上传（本地 → 远端）──────────────────────

    async def upload(
        self,
        local_path: str,
        remote_path: str,
        timeout: int | None = None,
        verify: bool = True,
        on_progress: ProgressCallback | None = None,
    ) -> TransferResult:
        """上传文件到远端节点

        流程：
        1. 读取本地文件并计算 MD5
        2. 在远端执行 ft_recv <remote_path>
        3. 等待 __FT_RECV_READY__
        4. 逐块发送 __FT_CHUNK__:<base64_data>
        5. 发送 __FT_EOF__ 结束标记
        6. 等待 __FT_RECV_OK__ 确认
        7. 可选：调用 ft_checksum 验证

        Args:
            local_path: 本地文件路径
            remote_path: 远端目标路径
            timeout: 超时秒数（None 时根据文件大小自动计算）
            verify: 是否传输后校验 MD5

        Returns:
            TransferResult
        """
        # 1. 验证本地文件
        local_file = Path(local_path)
        if not local_file.exists():
            return TransferResult(
                success=False, remote_path=remote_path,
                local_path=local_path,
                message=f"本地文件不存在: {local_path}",
                state=TransferState.FAILED,
            )

        if not local_file.is_file():
            return TransferResult(
                success=False, remote_path=remote_path,
                local_path=local_path,
                message=f"路径不是文件: {local_path}",
                state=TransferState.FAILED,
            )

        file_size = local_file.stat().st_size
        if file_size > _MAX_RECOMMENDED_SIZE:
            logger.warning(
                "文件 %s 大小 %.1fMB 超过推荐上限 %.1fMB，传输可能较慢",
                local_path, file_size / 1024 / 1024, _MAX_RECOMMENDED_SIZE / 1024 / 1024,
            )

        file_data = local_file.read_bytes()
        local_md5 = hashlib.md5(file_data).hexdigest()

        # ★ O2 优化：智能 gzip 压缩 ★
        # 尝试压缩，只有压缩率达到阈值才使用，否则直接传原始数据
        use_compression = False
        compressed_data = b""
        original_size = file_size

        try:
            compressed_data = gzip.compress(file_data, compresslevel=_GZIP_COMPRESS_LEVEL)
            if len(compressed_data) < len(file_data) * _COMPRESSION_THRESHOLD:
                use_compression = True
                logger.info(
                    "启用 gzip 压缩: %d → %d (节省 %.1f%%)",
                    file_size, len(compressed_data),
                    (1 - len(compressed_data) / file_size) * 100,
                )
            else:
                logger.info(
                    "跳过 gzip 压缩: %d → %d (仅节省 %.1f%%, 低于 %.0f%% 阈值)",
                    file_size, len(compressed_data),
                    (1 - len(compressed_data) / file_size) * 100,
                    (1 - _COMPRESSION_THRESHOLD) * 100,
                )
                compressed_data = b""  # 释放内存
        except Exception as e:
            logger.warning("gzip 压缩失败，使用原始数据传输: %s", e)
            compressed_data = b""

        # 确定实际传输的数据
        transfer_data = compressed_data if use_compression else file_data
        transfer_size = len(transfer_data)  # 实际要通过 PTY 传输的字节数
        transfer_timeout = _compute_timeout(transfer_size, timeout)

        logger.info(
            "开始上传: %s → %s (size=%d%s, md5=%s, timeout=%.0fs)",
            local_path, remote_path, file_size,
            f", compressed={transfer_size}" if use_compression else "",
            local_md5, transfer_timeout,
        )

        try:
            # 2. 发起远端接收命令（压缩模式加 --compressed 参数）
            ft_recv_cmd = f"ft_recv '{remote_path}'"
            if use_compression:
                ft_recv_cmd = f"ft_recv --compressed '{remote_path}'"
            # ★ Bugfix #20: 记录发送前的缓冲区位置，避免 wait_for 竞态 ★
            # Shell 响应可能在 send_input 返回后、wait_for 记录 start_pos 之前就到达 buffer，
            # 导致 wait_for 的扫描起始位置跳过了 __FT_RECV_READY__。
            # 与 ACK 循环（line ~405）使用相同的 pre_pos 模式。
            pre_recv_pos = len(self._session._raw_buffer)
            await self._session.send_input(ft_recv_cmd + "\n")

            # 3. 等待接收就绪
            ready_output = await self._session.wait_for(
                pattern=re.escape(Marker.RECV_READY) + "|" + re.escape(Marker.RECV_ERR),
                timeout=15.0,
                _start_pos=pre_recv_pos,
            )

            if Marker.RECV_ERR in ready_output:
                err_msg = self._extract_marker_value(ready_output, Marker.RECV_ERR)
                return TransferResult(
                    success=False, remote_path=remote_path,
                    local_path=local_path, file_size=file_size,
                    message=f"远端接收准备失败: {err_msg}",
                    state=TransferState.FAILED,
                )

            # 4. 批量 ACK 传输（O9 优化）
            chunks = self._split_into_chunks(transfer_data)
            total_chunks = len(chunks)
            # 预计算所有 chunk 的 base64 编码行
            chunks_b64: list[tuple[int, bytes, str]] = []  # (seq, raw_chunk, encoded_line)
            logger.info(
                "分块发送: %d 个块, 块大小=%d, 批大小=%d (batch-ACK, %s)",
                total_chunks, self._chunk_size, _BATCH_SIZE,
                "compressed" if use_compression else "raw",
            )

            # 压缩信息：直接在每个 TransferProgress 中显式传参（避免 **dict 展开的 pyright 类型推断问题）
            _is_compressed = use_compression
            _original_bytes = original_size
            _compressed_bytes = transfer_size if use_compression else 0

            transferred = 0
            # 自适应延迟状态
            current_delay = _POST_ACK_DELAY_INIT
            consecutive_ok = 0  # 连续成功计数
            total_retries = 0   # 全局重传统计

            # ★ 推送初始 0% 进度事件（Bugfix #17）
            # 让前端在 ft_recv 准备就绪后立即显示进度条，而不是停留在脉冲动画
            if on_progress:
                on_progress(TransferProgress(
                    state=TransferState.TRANSFERRING,
                    total_bytes=transfer_size,
                    transferred_bytes=0,
                    chunks_sent=0,
                    chunks_total=total_chunks,
                    compressed=_is_compressed,
                    original_bytes=_original_bytes,
                    compressed_bytes=_compressed_bytes,
                ))

            for seq, chunk in enumerate(chunks):
                b64_data = base64.b64encode(chunk).decode("ascii")
                chunks_b64.append((seq, chunk, f"{Marker.CHUNK}:{seq}:{b64_data}\n"))

            # ★ O9 批量 ACK 传输循环 ★
            # 每批发送 _BATCH_SIZE 个 chunk，然后批量收集 ACK。
            # 失败的 chunk 在批内重传（最多 _MAX_RETRIES 次）。
            batch_start = 0
            while batch_start < total_chunks:
                batch_end = min(batch_start + _BATCH_SIZE, total_chunks)
                batch = chunks_b64[batch_start:batch_end]

                # 4a. 批量发送
                pre_pos = len(self._session._raw_buffer)
                for _seq, _chunk, _line in batch:
                    await self._session.send_input(_line)
                    # chunk 间微延迟，让 PTY 内核缓冲区有时间处理
                    await asyncio.sleep(current_delay)

                # 4b. 批量收集 ACK
                ack_results: dict[int, str] = {}  # seq → status
                try:
                    ack_results = await self._collect_batch_acks(
                        expected_seqs=[s for s, _, _ in batch],
                        timeout=_ACK_TIMEOUT,
                        start_pos=pre_pos,
                    )
                except TimeoutError:
                    logger.warning(
                        "批量 ACK 超时 (batch %d-%d, %.1fs)，逐个重传",
                        batch[0][0], batch[-1][0], _ACK_TIMEOUT,
                    )

                # 检查是否有 RECV_ERR（远端致命错误）
                for _seq, status in ack_results.items():
                    if status == "_RECV_ERR":
                        return TransferResult(
                            success=False, remote_path=remote_path,
                            local_path=local_path, file_size=file_size,
                            message=f"远端接收失败 (chunk {_seq}): {status}",
                            state=TransferState.FAILED,
                        )

                # 4c. 处理失败的 chunk：逐个重传
                for _seq, _chunk, _line in batch:
                    status = ack_results.get(_seq, "")
                    if status == Marker.ACK_OK:
                        consecutive_ok += 1
                        if consecutive_ok >= _SPEEDUP_THRESHOLD:
                            old_delay = current_delay
                            current_delay = max(current_delay / 2, _POST_ACK_DELAY_MIN)
                            consecutive_ok = 0
                            if old_delay != current_delay:
                                logger.debug(
                                    "连续成功 %d 个，延迟减半: %.0fms → %.0fms",
                                    _SPEEDUP_THRESHOLD, old_delay * 1000, current_delay * 1000,
                                )
                        transferred += len(_chunk)
                        continue

                    # 失败（CORRUPT/SEQ_ERR/超时未收到）：逐个重传
                    consecutive_ok = 0
                    retry_ok = False
                    for retry in range(1, _MAX_RETRIES + 1):
                        current_delay = min(current_delay * 2, _POST_ACK_DELAY_MAX)
                        total_retries += 1
                        logger.warning(
                            "chunk %d/%d 第 %d 次重传 (status=%s, delay=%.0fms)",
                            _seq, total_chunks, retry, status, current_delay * 1000,
                        )
                        await asyncio.sleep(current_delay)

                        retry_pre = len(self._session._raw_buffer)
                        await self._session.send_input(_line)
                        try:
                            ack_output = await self._session.wait_for(
                                pattern=re.escape(Marker.ACK) + r"|" + re.escape(Marker.RECV_ERR),
                                timeout=_ACK_TIMEOUT,
                                _start_pos=retry_pre,
                            )
                        except TimeoutError:
                            status = "TIMEOUT"
                            continue

                        if Marker.RECV_ERR in ack_output:
                            err_msg = self._extract_marker_value(ack_output, Marker.RECV_ERR)
                            return TransferResult(
                                success=False, remote_path=remote_path,
                                local_path=local_path, file_size=file_size,
                                message=f"远端接收失败 (chunk {_seq} 重传): {err_msg}",
                                state=TransferState.FAILED,
                            )

                        retry_status = self._parse_ack(ack_output, _seq)
                        if retry_status == Marker.ACK_OK:
                            retry_ok = True
                            transferred += len(_chunk)
                            break
                        status = retry_status

                    if not retry_ok:
                        return TransferResult(
                            success=False, remote_path=remote_path,
                            local_path=local_path, file_size=file_size,
                            message=f"chunk {_seq}/{total_chunks} 重传 {_MAX_RETRIES} 次后仍失败 (status={status})",
                            state=TransferState.FAILED,
                        )

                # 4d. 批量进度回调
                if on_progress:
                    on_progress(TransferProgress(
                        state=TransferState.TRANSFERRING,
                        total_bytes=transfer_size,
                        transferred_bytes=transferred,
                        chunks_sent=batch_end,
                        chunks_total=total_chunks,
                        compressed=_is_compressed,
                        original_bytes=_original_bytes,
                        compressed_bytes=_compressed_bytes,
                    ))

                batch_start = batch_end

            # 发完所有块后短暂等待
            await asyncio.sleep(0.3)

            logger.info(
                "batch-ACK 传输完成: %d chunks, %d batches, %d retries, final_delay=%.0fms",
                total_chunks, (total_chunks + _BATCH_SIZE - 1) // _BATCH_SIZE,
                total_retries, current_delay * 1000,
            )

            # 5. 发送 EOF 标记
            # 记录当前缓冲区位置：后续 wait_for 从此处扫描，
            # 避免重新扫描大量 base64 回显数据（即使 stty -echo 生效，
            # 也可能有少量控制字符输出）
            eof_pos = len(self._session._raw_buffer)
            await self._session.send_input(f"{Marker.EOF}\n")

            # 通知进入"等待确认"阶段 — 子步骤：远端 base64 解码写入文件（压缩模式含 gunzip 解压）
            if on_progress:
                on_progress(TransferProgress(
                    state=TransferState.VERIFYING,
                    total_bytes=transfer_size,
                    transferred_bytes=transfer_size,
                    chunks_sent=total_chunks,
                    chunks_total=total_chunks,
                    sub_step="decoding",
                    compressed=_is_compressed,
                    original_bytes=_original_bytes,
                    compressed_bytes=_compressed_bytes,
                ))

            # 6. 等待接收确认（带 EOF 重发容错）
            # 第一次等待：给远端 30 秒处理（解码 + 写入）
            # 如果超时，可能是 EOF 在 PTY 缓冲区中丢失，重发一次
            _EOF_FIRST_WAIT = min(30.0, transfer_timeout * 0.5)
            recv_output = ""
            try:
                recv_output = await self._session.wait_for(
                    pattern=re.escape(Marker.RECV_OK) + "|" + re.escape(Marker.RECV_ERR),
                    timeout=_EOF_FIRST_WAIT,
                    _start_pos=eof_pos,
                )
            except TimeoutError:
                # EOF 可能丢失，重发一次
                logger.warning("首次等待 RECV_OK 超时(%.0fs)，重发 EOF", _EOF_FIRST_WAIT)
                eof_pos = len(self._session._raw_buffer)
                await self._session.send_input(f"{Marker.EOF}\n")
                # 继续等待剩余超时
                remaining_timeout = transfer_timeout - _EOF_FIRST_WAIT
                if remaining_timeout > 0:
                    recv_output = await self._session.wait_for(
                        pattern=re.escape(Marker.RECV_OK) + "|" + re.escape(Marker.RECV_ERR),
                        timeout=remaining_timeout,
                        _start_pos=eof_pos,
                    )

            if Marker.RECV_ERR in recv_output:
                err_msg = self._extract_marker_value(recv_output, Marker.RECV_ERR)
                return TransferResult(
                    success=False, remote_path=remote_path,
                    local_path=local_path, file_size=file_size,
                    message=f"远端写入失败: {err_msg}",
                    state=TransferState.FAILED,
                )

            remote_size_str = self._extract_marker_value(recv_output, Marker.RECV_OK)
            remote_size = int(remote_size_str) if remote_size_str.isdigit() else 0

            # 7. 可选校验
            remote_md5 = ""
            if verify:
                # 通知进入校验子步骤：MD5 校验
                if on_progress:
                    on_progress(TransferProgress(
                        state=TransferState.VERIFYING,
                        total_bytes=transfer_size,
                        transferred_bytes=transfer_size,
                        chunks_sent=total_chunks,
                        chunks_total=total_chunks,
                        sub_step="checksumming",
                        compressed=_is_compressed,
                        original_bytes=_original_bytes,
                        compressed_bytes=_compressed_bytes,
                    ))

                remote_md5 = await self._verify_checksum(remote_path, transfer_timeout)
                if remote_md5 and remote_md5 != local_md5 and remote_md5 != "unavailable":
                    return TransferResult(
                        success=False, remote_path=remote_path,
                        local_path=local_path, file_size=file_size,
                        md5=local_md5,
                        message=f"MD5 校验失败: local={local_md5}, remote={remote_md5}",
                        state=TransferState.FAILED,
                    )

            logger.info(
                "上传完成: %s → %s (size=%d→%s, md5=%s)",
                local_path, remote_path, file_size, remote_size_str, local_md5,
            )

            return TransferResult(
                success=True, remote_path=remote_path,
                local_path=local_path, file_size=file_size,
                md5=local_md5,
                message=f"上传成功: {file_size} 字节, MD5={local_md5}",
            )

        except TimeoutError as e:
            return TransferResult(
                success=False, remote_path=remote_path,
                local_path=local_path, file_size=file_size,
                message=f"上传超时: {e}",
                state=TransferState.FAILED,
            )
        except ConnectionError as e:
            return TransferResult(
                success=False, remote_path=remote_path,
                local_path=local_path, file_size=file_size,
                message=f"连接异常: {e}",
                state=TransferState.FAILED,
            )

    # ── 下载（远端 → 本地）──────────────────────

    async def download(
        self,
        remote_path: str,
        local_path: str,
        timeout: int | None = None,
        verify: bool = True,
    ) -> TransferResult:
        """从远端节点下载文件

        流程：
        1. 在远端执行 ft_send <remote_path>
        2. 等待 __FT_SEND_BEGIN__:<size>
        3. 收集所有 __FT_CHUNK__:<data> 块
        4. 等待 __FT_SEND_END__:<md5>
        5. 本地拼接 base64 并解码写入文件
        6. 可选：比对 MD5

        Args:
            remote_path: 远端文件路径
            local_path: 本地保存路径
            timeout: 超时秒数
            verify: 是否校验 MD5

        Returns:
            TransferResult
        """
        # 确保本地目录存在
        local_dir = Path(local_path).parent
        local_dir.mkdir(parents=True, exist_ok=True)

        logger.info("开始下载: %s → %s", remote_path, local_path)

        try:
            # 1. 发起远端发送命令
            # ★ Bugfix #20: 记录发送前的缓冲区位置，避免 wait_for 竞态 ★
            pre_pos = len(self._session._raw_buffer)
            await self._session.send_input(f"ft_send '{remote_path}'\n")

            # 2. 等待开始标记或错误
            begin_output = await self._session.wait_for(
                pattern=re.escape(Marker.SEND_BEGIN) + "|" + re.escape(Marker.SEND_ERR),
                timeout=15.0,
                _start_pos=pre_pos,
            )

            if Marker.SEND_ERR in begin_output:
                err_msg = self._extract_marker_value(begin_output, Marker.SEND_ERR)
                return TransferResult(
                    success=False, remote_path=remote_path,
                    local_path=local_path,
                    message=f"远端文件读取失败: {err_msg}",
                    state=TransferState.FAILED,
                )

            remote_size_str = self._extract_marker_value(begin_output, Marker.SEND_BEGIN)
            remote_size = int(remote_size_str) if remote_size_str.isdigit() else 0
            transfer_timeout = _compute_timeout(remote_size, timeout)

            logger.info("远端文件大小: %d 字节, 超时: %.0fs", remote_size, transfer_timeout)

            # 3. 收集数据块直到 SEND_END
            b64_chunks: list[str] = []
            end_output = await self._collect_chunks(b64_chunks, transfer_timeout)

            if Marker.SEND_ERR in end_output:
                err_msg = self._extract_marker_value(end_output, Marker.SEND_ERR)
                return TransferResult(
                    success=False, remote_path=remote_path,
                    local_path=local_path, file_size=remote_size,
                    message=f"远端发送失败: {err_msg}",
                    state=TransferState.FAILED,
                )

            remote_md5 = self._extract_marker_value(end_output, Marker.SEND_END)

            # 4. 解码并写入本地文件
            try:
                combined_b64 = "".join(b64_chunks)
                file_data = base64.b64decode(combined_b64)
            except Exception as e:
                return TransferResult(
                    success=False, remote_path=remote_path,
                    local_path=local_path, file_size=remote_size,
                    message=f"base64 解码失败: {e}",
                    state=TransferState.FAILED,
                )

            Path(local_path).write_bytes(file_data)
            local_size = len(file_data)
            local_md5 = hashlib.md5(file_data).hexdigest()

            # 5. 校验
            if verify and remote_md5 and remote_md5 != "unavailable":
                if local_md5 != remote_md5:
                    return TransferResult(
                        success=False, remote_path=remote_path,
                        local_path=local_path, file_size=local_size,
                        md5=local_md5,
                        message=f"MD5 校验失败: local={local_md5}, remote={remote_md5}",
                        state=TransferState.FAILED,
                    )

            logger.info(
                "下载完成: %s → %s (size=%d, chunks=%d, md5=%s)",
                remote_path, local_path, local_size, len(b64_chunks), local_md5,
            )

            return TransferResult(
                success=True, remote_path=remote_path,
                local_path=local_path, file_size=local_size,
                md5=local_md5,
                message=f"下载成功: {local_size} 字节, MD5={local_md5}",
            )

        except TimeoutError as e:
            return TransferResult(
                success=False, remote_path=remote_path,
                local_path=local_path,
                message=f"下载超时: {e}",
                state=TransferState.FAILED,
            )
        except ConnectionError as e:
            return TransferResult(
                success=False, remote_path=remote_path,
                local_path=local_path,
                message=f"连接异常: {e}",
                state=TransferState.FAILED,
            )

    # ── 内部方法 ──────────────────────────────────

    async def _collect_batch_acks(
        self,
        expected_seqs: list[int],
        timeout: float,
        start_pos: int,
    ) -> dict[int, str]:
        """批量收集 ACK 响应（O9 优化）

        从 _raw_buffer 的 start_pos 开始扫描，收集所有期望序列号的 ACK。
        Shell 端仍逐 chunk 回复 ACK，这里只是批量等待而非逐个等待。

        Args:
            expected_seqs: 期望收到 ACK 的序列号列表
            timeout: 总超时（秒）
            start_pos: 缓冲区扫描起始位置

        Returns:
            dict[seq, status]：已收到的 ACK 映射（seq → OK/CORRUPT/SEQ_ERR）

        Raises:
            TimeoutError: 超时前未收齐所有 ACK
        """
        from src.services.pty_session import strip_ansi

        results: dict[int, str] = {}
        pending = set(expected_seqs)
        prefix = f"{Marker.ACK}:"
        scan_pos = start_pos
        deadline = asyncio.get_event_loop().time() + timeout

        while pending:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(
                    f"批量 ACK 超时 ({timeout}s)，已收到 {len(results)}/{len(expected_seqs)} 个"
                )

            # 扫描缓冲区中新到达的行
            current_len = len(self._session._raw_buffer)
            if current_len > scan_pos:
                new_lines = list(self._session._raw_buffer)[scan_pos:current_len]
                scan_pos = current_len

                for line in new_lines:
                    clean = strip_ansi(line).strip()

                    # 检查 RECV_ERR（致命错误）
                    if Marker.RECV_ERR in clean:
                        for seq in pending:
                            results[seq] = "_RECV_ERR"
                        return results

                    # 解析 ACK
                    idx = clean.find(prefix)
                    if idx < 0:
                        continue
                    payload = clean[idx + len(prefix):]
                    parts = payload.split(":", 2)
                    if len(parts) < 2:
                        continue
                    try:
                        seq = int(parts[0])
                    except ValueError:
                        continue
                    status = parts[1]
                    results[seq] = status
                    pending.discard(seq)

                    if not pending:
                        return results

            # 等待新输出
            self._session._output_event.clear()
            try:
                await asyncio.wait_for(
                    self._session._output_event.wait(),
                    timeout=min(remaining, 0.5),
                )
            except asyncio.TimeoutError:
                continue

        return results

    def _parse_ack(self, output: str, expected_seq: int) -> str:
        """从 PTY 输出中解析 ACK 状态码

        ACK 格式：__FT_ACK__:<seq>:<status>[:<extra>]
          - __FT_ACK__:0:OK          → ACK_OK
          - __FT_ACK__:0:CORRUPT     → ACK_CORRUPT
          - __FT_ACK__:0:SEQ_ERR:5   → ACK_SEQ_ERR

        Args:
            output: wait_for 返回的 PTY 输出文本
            expected_seq: 期望的序列号

        Returns:
            ACK 状态码字符串（Marker.ACK_OK / ACK_CORRUPT / ACK_SEQ_ERR），
            或空字符串（解析失败）
        """
        prefix = f"{Marker.ACK}:"
        for line in output.splitlines():
            clean = line.strip()
            # 查找 ACK 标记
            idx = clean.find(prefix)
            if idx < 0:
                continue
            payload = clean[idx + len(prefix):]
            # payload = "<seq>:<status>[:<extra>]"
            parts = payload.split(":", 2)
            if len(parts) < 2:
                continue
            seq_str, status = parts[0], parts[1]
            try:
                seq = int(seq_str)
            except ValueError:
                continue
            # 序列号匹配检查（即使不匹配也返回状态让调用方决策）
            if seq != expected_seq:
                logger.warning(
                    "ACK 序列号不匹配: expected=%d, got=%d, status=%s",
                    expected_seq, seq, status,
                )
            return status
        return ""

    def _split_into_chunks(self, data: bytes) -> list[bytes]:
        """将文件数据拆分为固定大小的块"""
        chunks = []
        for i in range(0, len(data), self._chunk_size):
            chunks.append(data[i : i + self._chunk_size])
        return chunks

    async def _collect_chunks(
        self, b64_chunks: list[str], timeout: float
    ) -> str:
        """从 PTY 输出中收集 base64 数据块，直到遇到 SEND_END 或 SEND_ERR

        使用 wait_for 的低级接口，逐行扫描 _raw_buffer 中的新增数据。

        Returns:
            包含 SEND_END 或 SEND_ERR 的输出行
        """
        from src.services.pty_session import strip_ansi

        start_time = asyncio.get_event_loop().time()
        start_pos = len(self._session._raw_buffer)

        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed >= timeout:
                raise TimeoutError(
                    f"收集数据块超时（{timeout}s），已收到 {len(b64_chunks)} 个块"
                )

            current_len = len(self._session._raw_buffer)
            if current_len > start_pos:
                new_lines = list(self._session._raw_buffer)[start_pos:current_len]
                start_pos = current_len

                for line in new_lines:
                    clean = strip_ansi(line).strip()

                    if clean.startswith(f"{Marker.CHUNK}:"):
                        b64_data = clean[len(f"{Marker.CHUNK}:"):]
                        b64_chunks.append(b64_data)
                    elif Marker.SEND_END in clean:
                        return clean
                    elif Marker.SEND_ERR in clean:
                        return clean

            # 等待新输出
            self._session._output_event.clear()
            remaining = timeout - elapsed
            try:
                await asyncio.wait_for(
                    self._session._output_event.wait(),
                    timeout=min(remaining, 0.5),
                )
            except asyncio.TimeoutError:
                continue

    async def _verify_checksum(self, remote_path: str, timeout: float) -> str:
        """调用远端 inline 命令获取 MD5（不依赖 ft_checksum 函数）

        使用 inline shell 命令直接计算 MD5，兼容 Linux (md5sum) 和 macOS (md5)。
        O4 优化：从 snippet 中移除 ft_checksum 函数后改用 inline 方式，
        减少 snippet 体积。

        Returns:
            MD5 字符串，或空字符串（校验失败/超时）
        """
        # inline 命令：优先 md5sum，回退 md5 -q，都没有则输出 unavailable
        # 注意：echo 中的 $_md5 是 shell 变量，不是 Python 变量
        inline_cmd = (
            f"_md5=$(md5sum '{remote_path}' 2>/dev/null | awk '{{print $1}}') || "
            f"_md5=$(md5 -q '{remote_path}' 2>/dev/null) || _md5=unavailable; "
            f"echo \"__FT_MD5__:$_md5\""
        )
        # MD5 合法性正则：32 位十六进制或 "unavailable"
        _MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$|^unavailable$")
        try:
            output = await self._session.send_command(
                command=inline_cmd,
                wait_pattern=r"__FT_MD5__:",
                timeout=min(timeout, 30.0),
            )

            # ★ 从最后一行向上扫描：避免匹配到命令回显行（含未展开的 $_md5）
            for line in reversed(output.splitlines()):
                clean = line.strip()
                if "__FT_MD5__:" in clean:
                    md5_val = clean.split("__FT_MD5__:", 1)[1].strip()
                    # 过滤回显行：回显行值为 "$_md5" 而非真正的 hex 字符串
                    if md5_val and _MD5_RE.match(md5_val):
                        return md5_val

        except (TimeoutError, ConnectionError) as e:
            logger.warning("远端校验异常: %s", e)

        return ""

    @staticmethod
    def _extract_marker_value(text: str, marker: str) -> str:
        """从输出文本中提取标记后的值

        如 "__FT_RECV_OK__:12345" → "12345"
        """
        for line in text.splitlines():
            clean = line.strip()
            prefix = f"{marker}:"
            if clean.startswith(prefix):
                return clean[len(prefix):]
            # 也检查标记在行中间的情况
            idx = clean.find(prefix)
            if idx >= 0:
                return clean[idx + len(prefix):]
        return ""
