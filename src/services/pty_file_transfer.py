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
    __FT_SEND_BEGIN__:<size>               开始发送（无压缩），附传输大小
    __FT_SEND_BEGIN__:<csize>:<osize>:C    开始发送（压缩），附压缩/原始大小
    __FT_CHUNK__:<data>        base64 数据块
    __FT_SEND_END__:<md5>      发送完成，附原始文件 MD5
    __FT_SEND_ERR__:<msg>      发送失败

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

# ★ Bugfix #23b: 清洗 base64 数据中残留的非法字符 ★
# PTY 传输过程中可能混入 ANSI 转义序列残留、控制字符等，
# strip_ansi() 无法完全清除所有情况。只保留 base64 合法字符。
_NON_B64_RE = re.compile(r"[^A-Za-z0-9+/=]")


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


# ── 自适应分块参数（O11 优化）──────────────────────
#
# ★ 借鉴 trzsz 自适应 chunk 策略 ★
# 替代固定 36KB chunk size，采用 slow-start + 指数增长 + 失败回退：
#   - 以 16KB 起步（PTY 中极为安全，兼顾效率和兼容性）
#   - 1 批成功即翻倍（16→32→36KB，2 批内收敛到上限）
#   - 遇到 CORRUPT/SEQ_ERR/超时立即减半
#   - 上限 36KB（PTY 64KB 内核缓冲区安全线：36KB raw → 48KB base64 + 前缀 ≈ 49KB）
#
# ★ -icanon 大 chunk + ACK 确认协议 ★
# Shell 端使用 stty -icanon 解除 MAX_CANON (~4096字节) 行长度限制，
# 允许使用大 chunk。ACK 确认协议提供可靠性保障：
#   - 每个 chunk 带序列号 + base64 校验
#   - 行合并 → CORRUPT/SEQ_ERR → 自动重传（自适应延迟翻倍）
#
# ★ Bugfix #15: chunk 0 失败 — PTY 内核缓冲区溢出 ★
# 48KB raw → 64KB base64 + 前缀 ≈ 65.5KB 超过 PTY 内核缓冲区（64KB），
# 降为 36KB raw → 48KB base64 + 前缀 ≈ 49KB 作为上限。
#
_CHUNK_SIZE_INIT = 16 * 1024      # 初始 16KB（PTY 中极为安全，快速起步）
_CHUNK_SIZE_MIN = 2 * 1024        # 下限 2KB（再低效率太差）
_CHUNK_SIZE_MAX = 36 * 1024       # 上限 36KB（PTY 64KB 缓冲区安全线）
_CHUNK_GROW_THRESHOLD = 1         # 连续成功 1 批即增长（快速收敛，失败有减半兜底）
_CHUNK_GROW_FACTOR = 2            # 成功时翻倍
_CHUNK_SHRINK_FACTOR = 2          # 失败时减半

# ── 动态 batch size ──────────────────────────
# batch 总数据量保持恒定（~400KB base64 文本），chunk 越大 → batch 越小
_BATCH_TARGET_BYTES = 400 * 1024  # 目标：每批 ~400KB base64 文本


def _compute_batch_size(chunk_size: int) -> int:
    """根据当前 chunk size 动态计算 batch size
    
    保持每批总数据量恒定（~400KB base64 文本），
    chunk 越大 → batch 越小，避免 PTY 缓冲区压力。
    
    Args:
        chunk_size: 当前 raw chunk 大小（字节）
        
    Returns:
        batch size（至少为 1）
    """
    # chunk_size raw → base64 后约为 chunk_size * 4/3
    b64_per_chunk = chunk_size * 4 // 3
    batch = max(1, _BATCH_TARGET_BYTES // b64_per_chunk)
    return batch


@dataclass
class ChunkSizeController:
    """自适应 chunk size 控制器（O11 优化）
    
    借鉴 trzsz 的自适应策略：slow-start + 指数增长 + 失败回退。
    以批（batch）为粒度统计成功/失败，而非单个 chunk，
    因为单 chunk 失败会触发重传，批级别更能反映链路稳定性。
    
    状态机：
      probing → (达到 MAX) → stable
      stable  → (遇到失败) → probing（缩小后重新探测）
    """
    _size: int = _CHUNK_SIZE_INIT
    _consecutive_ok: int = 0          # 连续成功批次数
    _phase: str = "probing"           # probing | stable
    
    def __post_init__(self) -> None:
        # 确保是 3 的倍数（避免 base64 填充碎片）
        self._size = self._align(self._size)
    
    @staticmethod
    def _align(size: int) -> int:
        """对齐到 3 的倍数（base64 无填充）"""
        return size // 3 * 3
    
    @property
    def size(self) -> int:
        return self._size
    
    @property
    def phase(self) -> str:
        return self._phase
    
    @property
    def batch_size(self) -> int:
        """当前 chunk size 对应的 batch size"""
        return _compute_batch_size(self._size)
    
    def on_batch_ok(self) -> None:
        """一个批次全部成功"""
        if self._phase == "stable":
            return  # 已达上限，不再增长
        
        self._consecutive_ok += 1
        if self._consecutive_ok >= _CHUNK_GROW_THRESHOLD:
            old = self._size
            new = min(self._size * _CHUNK_GROW_FACTOR, _CHUNK_SIZE_MAX)
            new = self._align(new)
            if new != old:
                self._size = new
                logger.info(
                    "chunk 增长: %dKB → %dKB (连续 %d 批成功, batch=%d)",
                    old // 1024, new // 1024,
                    self._consecutive_ok, self.batch_size,
                )
            self._consecutive_ok = 0
            
            if self._size >= _CHUNK_SIZE_MAX:
                self._phase = "stable"
                logger.info("chunk 探测完成，进入 stable 阶段: %dKB", self._size // 1024)
    
    def on_fail(self) -> None:
        """批次中有 chunk 失败（CORRUPT/SEQ_ERR/超时）"""
        old = self._size
        new = max(self._size // _CHUNK_SHRINK_FACTOR, _CHUNK_SIZE_MIN)
        new = self._align(new)
        self._consecutive_ok = 0
        self._phase = "probing"  # 回退后重新探测
        
        if new != old:
            self._size = new
            logger.warning(
                "chunk 缩小: %dKB → %dKB (失败回退, batch=%d)",
                old // 1024, new // 1024, self.batch_size,
            )


# ── ACK 确认参数 ─────────────────────────────
_ACK_TIMEOUT = 15.0        # 批量 ACK 总超时（秒），需覆盖 Shell 处理整批的时间
_MAX_RETRIES = 5           # 每个 chunk 最大重传次数

# ── 动态自适应延迟参数 ─────────────────────────
# 核心思路：ACK 后需要一个延迟让 PTY 内核缓冲区清空，但最佳延迟值因环境而异。
# 采用类 TCP 拥塞控制的自适应策略：
#   - 初始延迟：15ms（O10a: 30→15ms，自适应机制兜底，慢环境自动翻倍回退）
#   - 发生行合并（CORRUPT/SEQ_ERR/超时）→ 延迟翻倍（回退）
#   - 连续成功 _SPEEDUP_THRESHOLD 个 chunk → 延迟减半（加速）
#   - 延迟上限：500ms（大 chunk 回退空间更大）
#   - 延迟下限：5ms
_POST_ACK_DELAY_INIT = 0.015  # 初始 15ms（O10a: 30→15ms，有自适应翻倍兜底）
_POST_ACK_DELAY_MIN = 0.005   # 下限 5ms
_POST_ACK_DELAY_MAX = 0.5     # 上限 500ms
_SPEEDUP_THRESHOLD = 20        # 连续成功 20 个 chunk 后尝试减半（大 chunk 总数少，更快加速）

# ★ Optimization #2: 双阈值文件大小限制 ★
# PTY 传输瓶颈是压缩后数据量。大文件如果可压缩（日志/文本），
# 压缩后 <= 10MB 即可通过 PTY 传输。
_MAX_RECOMMENDED_SIZE = 10 * 1024 * 1024  # 10MB：压缩后传输推荐上限（软警告）
_MAX_RAW_SIZE = 50 * 1024 * 1024          # 50MB：原始大小绝对上限

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

    def __init__(self, session) -> None:
        """初始化传输器

        Args:
            session: TerminalSession 实例（需要 send_input / wait_for / send_command 方法）
        """
        self._session = session

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
        if file_size > _MAX_RAW_SIZE:
            return TransferResult(
                success=False, remote_path=remote_path,
                local_path=local_path, file_size=file_size,
                message=(
                    f"文件 {local_path} 大小 {file_size / 1024 / 1024:.1f}MB "
                    f"超过绝对上限 {_MAX_RAW_SIZE / 1024 / 1024:.0f}MB，"
                    "建议使用 SCP/SFTP 传输大文件"
                ),
                state=TransferState.FAILED,
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

        # ★ Optimization #2: 基于压缩后大小的传输推荐上限软警告
        if transfer_size > _MAX_RECOMMENDED_SIZE:
            logger.warning(
                "文件 %s 传输大小 %.1fMB%s 超过推荐上限 %.1fMB，传输可能较慢",
                local_path, transfer_size / 1024 / 1024,
                f"（原始 {file_size / 1024 / 1024:.1f}MB gzip→ {transfer_size / 1024 / 1024:.1f}MB）"
                if use_compression else "",
                _MAX_RECOMMENDED_SIZE / 1024 / 1024,
            )

        transfer_timeout = _compute_timeout(transfer_size, timeout)

        logger.info(
            "开始上传: %s → %s (size=%d%s, md5=%s, timeout=%.0fs)",
            local_path, remote_path, file_size,
            f", compressed={transfer_size}" if use_compression else "",
            local_md5, transfer_timeout,
        )

        try:
            # ★ Bugfix #22c: 静默 WebSocket 广播 ★
            # upload 期间所有 PTY 输出（ft_recv 命令回显、__FT_CHUNK__ 数据回显、
            # ACK 确认、__FT_RECV_OK__ 标记等）不发送到浏览器终端。
            # Agent 缓冲区不受影响，wait_for / _collect_batch_acks 仍正常工作。
            self._session.set_ws_muted(True)

            # 2. 发起远端接收命令（压缩模式加 --compressed 参数）
            ft_recv_cmd = f"ft_recv '{remote_path}'"
            if use_compression:
                ft_recv_cmd = f"ft_recv --compressed '{remote_path}'"
            # ★ Bugfix #20: 记录发送前的缓冲区位置，避免 wait_for 竞态 ★
            # ★ Bugfix #21d: 使用 _buffer_write_seq 替代 len(_raw_buffer)
            pre_recv_pos = self._session._buffer_write_seq
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

            # 4. 自适应 batch-ACK 传输（O9 + O11 优化）
            # ★ O11: 惰性切分 — 不再预切所有 chunk，用 offset 指针 + controller.size 实时切片
            controller = ChunkSizeController()
            # 预估 chunk 总数（随 chunk size 增长会减少，用于进度条展示）
            est_total_chunks = max(1, -(-transfer_size // controller.size))  # ceil division

            logger.info(
                "分块发送: ~%d 个块, 初始块大小=%dKB, 批大小=%d (adaptive batch-ACK, %s)",
                est_total_chunks, controller.size // 1024, controller.batch_size,
                "compressed" if use_compression else "raw",
            )

            # 压缩信息：直接在每个 TransferProgress 中显式传参（避免 **dict 展开的 pyright 类型推断问题）
            _is_compressed = use_compression
            _original_bytes = original_size
            _compressed_bytes = transfer_size if use_compression else 0

            transferred = 0
            offset = 0      # 惰性切分偏移指针
            seq = 0          # 全局序列号（递增，不受 chunk size 变化影响）
            # 自适应延迟状态
            current_delay = _POST_ACK_DELAY_INIT
            delay_consecutive_ok = 0  # 延迟加速用连续成功计数
            total_retries = 0         # 全局重传统计

            # ★ 推送初始 0% 进度事件（Bugfix #17）
            # 让前端在 ft_recv 准备就绪后立即显示进度条，而不是停留在脉冲动画
            if on_progress:
                on_progress(TransferProgress(
                    state=TransferState.TRANSFERRING,
                    total_bytes=transfer_size,
                    transferred_bytes=0,
                    chunks_sent=0,
                    chunks_total=est_total_chunks,
                    compressed=_is_compressed,
                    original_bytes=_original_bytes,
                    compressed_bytes=_compressed_bytes,
                ))

            # ★ O9 + O11 自适应批量 ACK 传输循环 ★
            # 每批发送 controller.batch_size 个 chunk，然后批量收集 ACK。
            # chunk size 和 batch size 都由 controller 动态决定。
            # 失败的 chunk 在批内重传（最多 _MAX_RETRIES 次）。
            while offset < transfer_size:
                # 惰性切分：按当前 controller.size 实时切出本批 chunk
                batch_size = controller.batch_size
                batch: list[tuple[int, bytes, str]] = []  # (seq, raw_chunk, encoded_line)
                for _ in range(batch_size):
                    if offset >= transfer_size:
                        break
                    chunk_end = min(offset + controller.size, transfer_size)
                    chunk = transfer_data[offset:chunk_end]
                    b64_data = base64.b64encode(chunk).decode("ascii")
                    batch.append((seq, chunk, f"{Marker.CHUNK}:{seq}:{b64_data}\n"))
                    offset += len(chunk)
                    seq += 1

                if not batch:
                    break

                # 更新预估总 chunk 数（chunk size 增长后，剩余数据需要的 chunk 更少）
                remaining = transfer_size - offset + sum(len(c) for _, c, _ in batch)
                est_total_chunks = seq - len(batch) + max(1, -(-remaining // controller.size))

                # 4a. 批量发送
                pre_pos = self._session._buffer_write_seq
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

                # 4c. 处理每个 chunk 的 ACK 结果
                batch_has_failure = False
                for _seq, _chunk, _line in batch:
                    status = ack_results.get(_seq, "")
                    if status == Marker.ACK_OK:
                        delay_consecutive_ok += 1
                        if delay_consecutive_ok >= _SPEEDUP_THRESHOLD:
                            old_delay = current_delay
                            current_delay = max(current_delay / 2, _POST_ACK_DELAY_MIN)
                            delay_consecutive_ok = 0
                            if old_delay != current_delay:
                                logger.debug(
                                    "连续成功 %d 个，延迟减半: %.0fms → %.0fms",
                                    _SPEEDUP_THRESHOLD, old_delay * 1000, current_delay * 1000,
                                )
                        transferred += len(_chunk)
                        continue

                    # 失败（CORRUPT/SEQ_ERR/超时未收到）：标记 + 逐个重传
                    batch_has_failure = True
                    delay_consecutive_ok = 0
                    retry_ok = False
                    for retry in range(1, _MAX_RETRIES + 1):
                        current_delay = min(current_delay * 2, _POST_ACK_DELAY_MAX)
                        total_retries += 1
                        logger.warning(
                            "chunk %d 第 %d 次重传 (status=%s, delay=%.0fms, chunk_size=%dKB)",
                            _seq, retry, status, current_delay * 1000, controller.size // 1024,
                        )
                        await asyncio.sleep(current_delay)

                        retry_pre = self._session._buffer_write_seq
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
                            message=f"chunk {_seq} 重传 {_MAX_RETRIES} 次后仍失败 (status={status})",
                            state=TransferState.FAILED,
                        )

                # 4d. 通知 ChunkSizeController（批粒度）
                if batch_has_failure:
                    controller.on_fail()
                else:
                    controller.on_batch_ok()

                # 4e. 批量进度回调
                if on_progress:
                    on_progress(TransferProgress(
                        state=TransferState.TRANSFERRING,
                        total_bytes=transfer_size,
                        transferred_bytes=transferred,
                        chunks_sent=seq,
                        chunks_total=max(seq, est_total_chunks),
                        compressed=_is_compressed,
                        original_bytes=_original_bytes,
                        compressed_bytes=_compressed_bytes,
                    ))

            # 发完所有块后短暂等待
            await asyncio.sleep(0.3)
            total_chunks = seq  # 实际发送的 chunk 总数

            logger.info(
                "adaptive batch-ACK 传输完成: %d chunks, %d retries, "
                "final_chunk=%dKB, final_delay=%.0fms, phase=%s",
                total_chunks, total_retries,
                controller.size // 1024, current_delay * 1000, controller.phase,
            )

            # 5. 发送 EOF 标记
            # 记录当前缓冲区位置：后续 wait_for 从此处扫描，
            # 避免重新扫描大量历史数据
            eof_pos = self._session._buffer_write_seq
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
                eof_pos = self._session._buffer_write_seq
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
        finally:
            # ★ Bugfix #22c: 恢复 WebSocket 广播（无论传输成功/失败/超时）★
            self._session.set_ws_muted(False)

    # ── 下载（远端 → 本地）──────────────────────

    async def download(
        self,
        remote_path: str,
        local_path: str,
        timeout: int | None = None,
        verify: bool = True,
        on_progress: ProgressCallback | None = None,
    ) -> TransferResult:
        """从远端节点下载文件

        流程（O12 优化：压缩传输 + 大 chunk + 高效 dd）：
        1. 在远端执行 ft_send --compressed <remote_path>
        2. 等待 __FT_SEND_BEGIN__:<size>[:<orig_size>:C]
        3. 收集所有 __FT_CHUNK__:<data> 块（带进度回调）
        4. 等待 __FT_SEND_END__:<md5>
        5. 本地拼接 base64 → 解码 → 如压缩则 gunzip → 写入文件
        6. 可选：比对 MD5（远端 MD5 是对原始文件计算的）

        Args:
            remote_path: 远端文件路径
            local_path: 本地保存路径
            timeout: 超时秒数
            verify: 是否校验 MD5
            on_progress: 进度回调（可选）

        Returns:
            TransferResult
        """
        # 确保本地目录存在
        local_dir = Path(local_path).parent
        local_dir.mkdir(parents=True, exist_ok=True)

        logger.info("开始下载: %s → %s", remote_path, local_path)

        try:
            # ★ Bugfix #22c: 静默 WebSocket 广播 ★
            # download 期间所有 PTY 输出（ft_send 命令回显、__FT_SEND_BEGIN__、
            # __FT_CHUNK__ 数据、__FT_SEND_END__ 标记等）不发送到浏览器终端。
            # Agent 缓冲区不受影响，wait_for / _collect_chunks 仍正常工作。
            self._session.set_ws_muted(True)

            # 1. 发起远端发送命令（默认请求压缩传输）
            # ★ O12: --compressed 让远端 gzip 压缩后再 base64 分块传输
            # ★ Bugfix #20: 记录发送前的缓冲区位置，避免 wait_for 竞态 ★
            # ★ Bugfix #21d: 使用 _buffer_write_seq 替代 len(_raw_buffer)
            pre_pos = self._session._buffer_write_seq
            await self._session.send_input(f"ft_send --compressed '{remote_path}'\n")

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

            # ★ O12: 解析新 SEND_BEGIN 格式
            # 无压缩: __FT_SEND_BEGIN__:<size>
            # 有压缩: __FT_SEND_BEGIN__:<compressed_size>:<original_size>:C
            begin_value = self._extract_marker_value(begin_output, Marker.SEND_BEGIN)
            is_compressed = False
            original_size = 0
            compressed_size = 0
            transfer_size = 0  # 实际通过 PTY 传输的字节数（压缩后 or 原始）

            parts = begin_value.split(":")
            if len(parts) >= 3 and parts[-1] == "C":
                # 压缩模式: <compressed_size>:<original_size>:C
                is_compressed = True
                compressed_size = int(parts[0]) if parts[0].isdigit() else 0
                original_size = int(parts[1]) if parts[1].isdigit() else 0
                transfer_size = compressed_size
                logger.info(
                    "远端压缩传输: 原始 %d → 压缩 %d (节省 %.1f%%), 超时基于压缩大小",
                    original_size, compressed_size,
                    (1 - compressed_size / original_size) * 100 if original_size > 0 else 0,
                )
            else:
                # 无压缩模式: <size>
                transfer_size = int(parts[0]) if parts[0].isdigit() else 0
                original_size = transfer_size

            transfer_timeout = _compute_timeout(transfer_size, timeout)

            logger.info(
                "远端文件大小: %d 字节%s, 超时: %.0fs",
                original_size,
                f" (compressed={compressed_size})" if is_compressed else "",
                transfer_timeout,
            )

            # 压缩进度信息（传递给进度回调）
            _is_compressed = is_compressed
            _original_bytes = original_size
            _compressed_bytes = compressed_size if is_compressed else 0

            # ★ 推送初始 0% 进度
            if on_progress:
                on_progress(TransferProgress(
                    state=TransferState.TRANSFERRING,
                    total_bytes=transfer_size,
                    transferred_bytes=0,
                    chunks_sent=0,
                    chunks_total=0,
                    compressed=_is_compressed,
                    original_bytes=_original_bytes,
                    compressed_bytes=_compressed_bytes,
                ))

            # 3. 收集数据块直到 SEND_END（带进度回调）
            b64_chunks: list[str] = []
            end_output = await self._collect_chunks(
                b64_chunks, transfer_timeout, transfer_size, on_progress,
                is_compressed=_is_compressed,
                original_bytes=_original_bytes,
                compressed_bytes=_compressed_bytes,
            )

            if Marker.SEND_ERR in end_output:
                err_msg = self._extract_marker_value(end_output, Marker.SEND_ERR)
                return TransferResult(
                    success=False, remote_path=remote_path,
                    local_path=local_path, file_size=original_size,
                    message=f"远端发送失败: {err_msg}",
                    state=TransferState.FAILED,
                )

            remote_md5 = self._extract_marker_value(end_output, Marker.SEND_END)

            # 通知进入校验阶段
            if on_progress:
                on_progress(TransferProgress(
                    state=TransferState.VERIFYING,
                    total_bytes=transfer_size,
                    transferred_bytes=transfer_size,
                    chunks_sent=len(b64_chunks),
                    chunks_total=len(b64_chunks),
                    sub_step="decoding",
                    compressed=_is_compressed,
                    original_bytes=_original_bytes,
                    compressed_bytes=_compressed_bytes,
                ))

            # 4. 逐 chunk 独立解码（★ Bugfix #23 + #23d）
            # 每个 chunk 是 Shell 端 dd+base64 独立编码的，逐个解码可以：
            #   a) 精确定位截断/损坏的 chunk 序号
            #   b) 避免 padding '=' 出现在拼接中间导致整体解码失败
            #   c) 续行拼接后需重新规范化 padding
            try:
                raw_parts: list[bytes] = []
                for i, b64 in enumerate(b64_chunks):
                    # ★ 解码前规范化: 清洗 + padding 补齐
                    # 续行拼接后可能混入非法字符或缺少 padding
                    clean_b64 = _NON_B64_RE.sub("", b64)
                    stripped = clean_b64.rstrip("=")
                    rem = len(stripped) % 4
                    if rem == 2:
                        clean_b64 = stripped + "=="
                    elif rem == 3:
                        clean_b64 = stripped + "="
                    elif rem == 0:
                        clean_b64 = stripped
                    else:
                        # rem == 1: 非法状态，丢失数据无法恢复
                        logger.error(
                            "chunk #%d/%d data chars 余1非法 (len=%d), 无法修复",
                            i, len(b64_chunks), len(stripped),
                        )
                        clean_b64 = stripped[:-1]  # 最后手段：截尾

                    try:
                        raw_parts.append(base64.b64decode(clean_b64))
                    except Exception as chunk_err:
                        logger.error(
                            "chunk #%d/%d base64 解码失败 (len=%d): %s",
                            i, len(b64_chunks), len(clean_b64), chunk_err,
                        )
                        return TransferResult(
                            success=False, remote_path=remote_path,
                            local_path=local_path, file_size=original_size,
                            message=(
                                f"base64 解码失败: chunk #{i}/{len(b64_chunks)}, "
                                f"b64_len={len(clean_b64)}, {chunk_err}"
                            ),
                            state=TransferState.FAILED,
                        )
                raw_data = b"".join(raw_parts)
            except Exception as e:
                return TransferResult(
                    success=False, remote_path=remote_path,
                    local_path=local_path, file_size=original_size,
                    message=f"base64 解码失败: {e}",
                    state=TransferState.FAILED,
                )

            # ★ 数据完整性检查：解码字节数 vs 预期
            logger.info(
                "base64 解码完成: %d chunks, 解码后 %d 字节, 预期 %d 字节, 差异 %d",
                len(b64_chunks), len(raw_data), transfer_size,
                len(raw_data) - transfer_size,
            )

            # ★ O12: 压缩模式下 gunzip 解压
            if is_compressed:
                try:
                    file_data = gzip.decompress(raw_data)
                    logger.info(
                        "gunzip 解压完成: %d → %d 字节",
                        len(raw_data), len(file_data),
                    )
                except Exception as e:
                    return TransferResult(
                        success=False, remote_path=remote_path,
                        local_path=local_path, file_size=original_size,
                        message=f"gzip 解压失败: {e}",
                        state=TransferState.FAILED,
                    )
            else:
                file_data = raw_data

            Path(local_path).write_bytes(file_data)
            local_size = len(file_data)
            local_md5 = hashlib.md5(file_data).hexdigest()

            # 5. 校验（远端 MD5 是对原始未压缩文件计算的）
            if verify and remote_md5 and remote_md5 != "unavailable":
                if on_progress:
                    on_progress(TransferProgress(
                        state=TransferState.VERIFYING,
                        total_bytes=transfer_size,
                        transferred_bytes=transfer_size,
                        chunks_sent=len(b64_chunks),
                        chunks_total=len(b64_chunks),
                        sub_step="checksumming",
                        compressed=_is_compressed,
                        original_bytes=_original_bytes,
                        compressed_bytes=_compressed_bytes,
                    ))

                if local_md5 != remote_md5:
                    return TransferResult(
                        success=False, remote_path=remote_path,
                        local_path=local_path, file_size=local_size,
                        md5=local_md5,
                        message=f"MD5 校验失败: local={local_md5}, remote={remote_md5}",
                        state=TransferState.FAILED,
                    )

            logger.info(
                "下载完成: %s → %s (size=%d%s, chunks=%d, md5=%s)",
                remote_path, local_path, local_size,
                f", compressed={compressed_size}" if is_compressed else "",
                len(b64_chunks), local_md5,
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
        finally:
            # ★ Bugfix #22c: 恢复 WebSocket 广播 ★
            self._session.set_ws_muted(False)

    # ── 内部方法 ──────────────────────────────────

    async def _collect_batch_acks(
        self,
        expected_seqs: list[int],
        timeout: float,
        start_pos: int,
    ) -> dict[int, str]:
        """批量收集 ACK 响应（O9 优化）

        从 start_pos（_buffer_write_seq 值）开始扫描，收集所有期望序列号的 ACK。
        Shell 端仍逐 chunk 回复 ACK，这里只是批量等待而非逐个等待。

        ★ Bugfix #21d: start_pos 语义变更为 _buffer_write_seq 值，
        使用 seq-based 扫描替代 len()-based 扫描，修复 deque 满后失效问题。

        Args:
            expected_seqs: 期望收到 ACK 的序列号列表
            timeout: 总超时（秒）
            start_pos: 扫描起始位置（_buffer_write_seq 值）

        Returns:
            dict[seq, status]：已收到的 ACK 映射（seq → OK/CORRUPT/SEQ_ERR）

        Raises:
            TimeoutError: 超时前未收齐所有 ACK
        """
        from src.services.pty_session import strip_ansi

        results: dict[int, str] = {}
        pending = set(expected_seqs)
        prefix = f"{Marker.ACK}:"
        scan_seq = start_pos  # ★ Bugfix #21d: seq-based 位置追踪
        deadline = asyncio.get_event_loop().time() + timeout

        while pending:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(
                    f"批量 ACK 超时 ({timeout}s)，已收到 {len(results)}/{len(expected_seqs)} 个"
                )

            # ★ Bugfix #21d: 使用 _buffer_write_seq 检测新数据
            current_seq = self._session._buffer_write_seq
            if current_seq > scan_seq:
                buf_len = len(self._session._raw_buffer)
                oldest_seq = current_seq - buf_len
                effective_start = max(scan_seq, oldest_seq)
                start_idx = effective_start - oldest_seq
                end_idx = buf_len

                if start_idx < end_idx:
                    new_lines = list(self._session._raw_buffer)[start_idx:end_idx]

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

                scan_seq = current_seq

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

    # ── 下载相关内部方法 ──────────────────────────

    async def _collect_chunks(
        self,
        b64_chunks: list[str],
        timeout: float,
        total_size: int = 0,
        on_progress: ProgressCallback | None = None,
        *,
        is_compressed: bool = False,
        original_bytes: int = 0,
        compressed_bytes: int = 0,
    ) -> str:
        """从 PTY 输出中收集 base64 数据块，直到遇到 SEND_END 或 SEND_ERR

        使用 wait_for 的低级接口，逐行扫描 _raw_buffer 中的新增数据。

        ★ Bugfix #21d: 使用 _buffer_write_seq 做 seq-based 扫描，
        替代 len(_raw_buffer) 的 index-based 扫描。

        Args:
            b64_chunks: 收集到的 base64 数据块列表（输出参数）
            timeout: 超时秒数
            total_size: 远端文件总字节数（用于进度计算）
            on_progress: 进度回调（可选）
            is_compressed: 是否为压缩传输模式
            original_bytes: 原始文件大小（未压缩）
            compressed_bytes: 压缩后大小（0 表示未压缩）

        Returns:
            包含 SEND_END 或 SEND_ERR 的输出行
        """
        from src.services.pty_session import strip_ansi

        start_time = asyncio.get_event_loop().time()
        scan_seq = self._session._buffer_write_seq  # ★ Bugfix #21d
        # 进度节流：每累计 ~50KB b64 数据或每 10 个 chunk 推送一次
        _last_progress_chunks = 0
        _PROGRESS_CHUNK_INTERVAL = 10

        # ★ Bugfix #23d: 行拼接（line reassembly）状态 ★
        # os.read(fd, 65536) 不保证返回完整行。ft_send 输出的
        # __FT_CHUNK__:...base64... 行长达 ~49KB，会跨多次 os.read 调用，
        # 被 text.split("\n") 切成多段写入 _raw_buffer。
        # _pending_partial 暂存当前不完整的 chunk base64 数据，
        # 直到下一个协议标记行出现时才 finalize（commit 到 b64_chunks）。
        _pending_partial: str | None = None  # 当前正在拼接的 chunk 数据
        _reassemble_count = 0  # 续行拼接计数（诊断用）

        def _finalize_pending() -> None:
            """将 pending partial chunk 提交到 b64_chunks"""
            nonlocal _pending_partial
            if _pending_partial is not None:
                b64_chunks.append(_pending_partial)
                _pending_partial = None

        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed >= timeout:
                _finalize_pending()
                raise TimeoutError(
                    f"收集数据块超时（{timeout}s），已收到 {len(b64_chunks)} 个块"
                )

            # ★ Bugfix #21d: 使用 _buffer_write_seq 检测新数据
            current_seq = self._session._buffer_write_seq
            if current_seq > scan_seq:
                buf_len = len(self._session._raw_buffer)
                oldest_seq = current_seq - buf_len
                effective_start = max(scan_seq, oldest_seq)
                start_idx = effective_start - oldest_seq
                end_idx = buf_len

                if start_idx < end_idx:
                    new_lines = list(self._session._raw_buffer)[start_idx:end_idx]

                    for line in new_lines:
                        clean = strip_ansi(line).strip()

                        if clean.startswith(f"{Marker.CHUNK}:"):
                            # 新 chunk 开始 → 先 finalize 上一个 pending
                            _finalize_pending()

                            b64_data = clean[len(f"{Marker.CHUNK}:"):]

                            # ★ Bugfix #23b: 清洗非 base64 合法字符
                            raw_len = len(b64_data)
                            b64_data = _NON_B64_RE.sub("", b64_data)
                            if len(b64_data) != raw_len:
                                logger.warning(
                                    "chunk #%d base64 清洗: %d → %d (移除 %d 个非法字符)",
                                    len(b64_chunks), raw_len, len(b64_data),
                                    raw_len - len(b64_data),
                                )

                            # 暂存为 pending（可能还有续行）
                            _pending_partial = b64_data

                            # 进度回调（节流，基于已完成的 chunks）
                            # +1 因为 pending 还没 commit
                            chunk_count = len(b64_chunks) + 1
                            if (on_progress
                                    and chunk_count - _last_progress_chunks >= _PROGRESS_CHUNK_INTERVAL):
                                _last_progress_chunks = chunk_count
                                received_b64 = (sum(len(c) for c in b64_chunks)
                                                + len(b64_data))
                                received_bytes = received_b64 * 3 // 4
                                on_progress(TransferProgress(
                                    state=TransferState.TRANSFERRING,
                                    total_bytes=total_size,
                                    transferred_bytes=min(received_bytes, total_size),
                                    chunks_sent=chunk_count,
                                    chunks_total=0,
                                    compressed=is_compressed,
                                    original_bytes=original_bytes,
                                    compressed_bytes=compressed_bytes,
                                ))

                        elif Marker.SEND_END in clean:
                            _finalize_pending()
                            if _reassemble_count > 0:
                                logger.info(
                                    "行拼接统计: %d 次续行拼接, %d 个 chunks",
                                    _reassemble_count, len(b64_chunks),
                                )
                            return clean
                        elif Marker.SEND_ERR in clean:
                            _finalize_pending()
                            return clean
                        elif _pending_partial is not None:
                            # ★ 续行拼接：当前行不是协议标记，且有 pending chunk
                            # → 视为上一个 __FT_CHUNK__ 行被 os.read 截断后的续行
                            continuation = _NON_B64_RE.sub("", clean)
                            if continuation:
                                _pending_partial += continuation
                                _reassemble_count += 1
                                logger.debug(
                                    "chunk #%d 续行拼接: +%d chars (累计 %d)",
                                    len(b64_chunks), len(continuation),
                                    len(_pending_partial),
                                )

                scan_seq = current_seq

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
