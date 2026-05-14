/**
 * FileTransferPanel — 文件传输面板组件
 *
 * 提供浏览器端文件上传/下载到远端节点的 UI：
 * - 拖拽区域 (Drag & Drop Zone)
 * - 文件选择按钮 (File Picker)
 * - 远端路径输入框
 * - 上传进度条
 * - 下载区域：远端路径输入 + 下载按钮
 * - 传输历史记录
 *
 * 设计原则：
 * - 低耦合：仅依赖 sessionId prop，通过 api.ts 与后端通信
 * - 参考 SnippetPanel 的面板展开/收起模式
 * - 错误处理健壮：网络中断、文件过大、远端路径不存在等场景都有友好提示
 */

import { useState, useCallback, useRef, useEffect } from "react";
import { uploadFile, downloadFile, cancelUpload } from "../services/api";
import type { FileUploadResponse, PtyTransferProgress } from "../services/api";

// ── 类型定义 ──────────────────────────────────

type TransferStatus = "idle" | "uploading" | "downloading" | "success" | "error";

/** 上传阶段 */
type UploadPhase = "browser-to-server" | "server-to-remote";

/** PTY 传输子状态 */
type PtyTransferState = "transferring" | "verifying";

/** 校验阶段子步骤 */
type VerifySubStep = "decoding" | "checksumming" | "";

/** 上传进度详情（含两阶段 + 速度信息） */
interface UploadProgressInfo {
  phase: UploadPhase;
  /** 当前阶段进度百分比 (0-100) */
  percent: number;
  /** 文件名 */
  filename: string;
  /** 文件大小（字节） */
  fileSize: number;
  /** 已上传字节数（阶段1） */
  loaded: number;
  /** 阶段1开始时间 */
  startedAt: number;
  /** 阶段2开始时间 */
  phase2StartedAt: number;
  /** 实时上传速度（字节/秒） */
  speed: number;
  /** PTY 传输子状态（transferring / verifying） */
  ptyState: PtyTransferState;
  /** 校验子步骤（decoding: 解码写入 / checksumming: MD5 校验） */
  ptySubStep: VerifySubStep;
  /** ★ O2 压缩信息 */
  compressed: boolean;
  /** 原始文件大小（未压缩） */
  originalBytes: number;
  /** 压缩后大小（0 表示未压缩） */
  compressedBytes: number;
  /** 压缩率百分比（如 62.5 = 节省 62.5%） */
  compressionRatio: number;
}

interface TransferRecord {
  id: string;
  type: "upload" | "download";
  filename: string;
  remotePath: string;
  size: number;
  status: "success" | "error";
  message: string;
  timestamp: number;
}

interface FileTransferPanelProps {
  /** 当前终端会话 ID */
  sessionId: string;
  /** 面板可见性 */
  visible: boolean;
  /** 关闭面板回调 */
  onClose: () => void;
  /** 终端是否已连接 */
  isConnected: boolean;
}

// ── 常量 ──────────────────────────────────────

const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10MB
const MAX_HISTORY = 20;

/** 生成唯一 ID（兼容 HTTP 环境，crypto.randomUUID 仅在 Secure Context 可用） */
function generateId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

/** 截断错误消息，避免超时错误包含大量 base64 数据 */
function truncateError(msg: string, maxLen = 200): string {
  if (msg.length <= maxLen) return msg;
  return msg.slice(0, maxLen) + "…";
}

/** 格式化文件大小 */
function formatSize(bytes: number): string {
  if (bytes === 0) return "0B";
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)}MB`;
}

/** 格式化速度（字节/秒 → 人类可读） */
function formatSpeed(bytesPerSec: number): string {
  if (bytesPerSec <= 0) return "计算中...";
  if (bytesPerSec < 1024) return `${bytesPerSec.toFixed(0)} B/s`;
  if (bytesPerSec < 1024 * 1024) return `${(bytesPerSec / 1024).toFixed(1)} KB/s`;
  return `${(bytesPerSec / 1024 / 1024).toFixed(1)} MB/s`;
}

/** 格式化耗时 */
function formatElapsed(ms: number): string {
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainSec = seconds % 60;
  return `${minutes}m${remainSec}s`;
}

// ── 主组件 ──────────────────────────────────

export default function FileTransferPanel({
  sessionId,
  visible,
  onClose,
  isConnected,
}: FileTransferPanelProps) {
  // ── 状态 ──
  const [status, setStatus] = useState<TransferStatus>("idle");
  const [progressInfo, setProgressInfo] = useState<UploadProgressInfo | null>(null);
  const [remotePath, setRemotePath] = useState("/tmp/");
  const [downloadPath, setDownloadPath] = useState("");
  const [errorMsg, setErrorMsg] = useState("");
  const [successMsg, setSuccessMsg] = useState("");
  const [history, setHistory] = useState<TransferRecord[]>([]);
  const [dragOver, setDragOver] = useState(false);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  /** 速度计算：上次采样点 */
  const speedSampleRef = useRef<{ time: number; loaded: number }>({ time: 0, loaded: 0 });

  // ── 添加历史记录 ──
  const addHistory = useCallback((record: Omit<TransferRecord, "id" | "timestamp">) => {
    setHistory((prev) => {
      const newRecord: TransferRecord = {
        ...record,
        id: generateId(),
        timestamp: Date.now(),
      };
      return [newRecord, ...prev].slice(0, MAX_HISTORY);
    });
  }, []);

  // ── 上传逻辑 ──
  const handleUpload = useCallback(async (file: File) => {
    if (!isConnected) {
      setErrorMsg("终端未连接，无法传输文件");
      setStatus("error");
      return;
    }

    if (file.size > MAX_FILE_SIZE) {
      setErrorMsg(`文件大小 ${formatSize(file.size)} 超过限制 10MB`);
      setStatus("error");
      return;
    }

    if (file.size === 0) {
      setErrorMsg("文件为空");
      setStatus("error");
      return;
    }

    const now = Date.now();
    setStatus("uploading");
    // 阶段 1：上传到服务器（fetch POST，内网通常 <1s 完成）
    setProgressInfo({
      phase: "browser-to-server",
      percent: 0,
      filename: file.name,
      fileSize: file.size,
      loaded: 0,
      startedAt: now,
      phase2StartedAt: 0,
      speed: 0,
      ptyState: "transferring",
      ptySubStep: "",
      compressed: false,
      originalBytes: 0,
      compressedBytes: 0,
      compressionRatio: 0,
    });
    setErrorMsg("");
    setSuccessMsg("");
    speedSampleRef.current = { time: now, loaded: 0 };

    const targetPath = remotePath.endsWith("/")
      ? remotePath + file.name
      : remotePath;

    abortRef.current = new AbortController();

    try {
      // 切换到阶段 2（fetch 发出后服务器已收到文件，SSE 流开始）
      const phase2Start = Date.now();
      setProgressInfo((prev) => prev ? {
        ...prev,
        phase: "server-to-remote",
        percent: 0,
        phase2StartedAt: phase2Start,
        speed: 0,
      } : prev);

      const result: FileUploadResponse = await uploadFile(
        sessionId,
        file,
        targetPath,
        (progress: PtyTransferProgress) => {
          // SSE 进度回调：更新阶段 2 的真实进度
          const nowMs = Date.now();
          const sample = speedSampleRef.current;
          let speed = 0;
          // 每 500ms 采样一次计算 PTY 传输速度
          if (nowMs - sample.time >= 500 && progress.transferred > sample.loaded) {
            speed = ((progress.transferred - sample.loaded) / (nowMs - sample.time)) * 1000;
            speedSampleRef.current = { time: nowMs, loaded: progress.transferred };
          }

          const ptyState = progress.state === "verifying" ? "verifying" as const : "transferring" as const;
          const ptySubStep = (progress.sub_step === "decoding" || progress.sub_step === "checksumming")
            ? progress.sub_step as VerifySubStep
            : "" as const;

          setProgressInfo((prev) => {
            if (!prev) return prev;
            return {
              ...prev,
              phase: "server-to-remote",
              percent: progress.percentage,
              loaded: progress.transferred,
              speed: speed > 0 ? speed : prev.speed,
              ptyState,
              ptySubStep,
              compressed: progress.compressed ?? false,
              originalBytes: progress.original_bytes ?? 0,
              compressedBytes: progress.compressed_bytes ?? 0,
              compressionRatio: progress.compression_ratio ?? 0,
            };
          });
        },
        abortRef.current.signal,
      );

      if (result.success) {
        setStatus("success");
        const elapsed = formatElapsed(Date.now() - now);
        setSuccessMsg(`上传成功: ${file.name} → ${result.remote_path}（${formatSize(result.file_size)}，耗时 ${elapsed}）`);
        addHistory({
          type: "upload",
          filename: file.name,
          remotePath: result.remote_path,
          size: result.file_size,
          status: "success",
          message: result.message,
        });
      } else {
        setStatus("error");
        setErrorMsg(truncateError(result.message || "上传失败"));
        addHistory({
          type: "upload",
          filename: file.name,
          remotePath: targetPath,
          size: file.size,
          status: "error",
          message: result.message,
        });
      }
    } catch (err) {
      const msg = truncateError(err instanceof Error ? err.message : "上传失败");
      setStatus("error");
      setErrorMsg(msg);
      addHistory({
        type: "upload",
        filename: file.name,
        remotePath: targetPath,
        size: file.size,
        status: "error",
        message: msg,
      });
    } finally {
      abortRef.current = null;
      setProgressInfo(null);
    }
  }, [sessionId, remotePath, isConnected, addHistory]);

  // ── 下载逻辑 ──
  const handleDownload = useCallback(async () => {
    if (!isConnected) {
      setErrorMsg("终端未连接，无法传输文件");
      setStatus("error");
      return;
    }

    if (!downloadPath.trim()) {
      setErrorMsg("请输入远端文件路径");
      setStatus("error");
      return;
    }

    setStatus("downloading");
    setErrorMsg("");
    setSuccessMsg("");

    try {
      await downloadFile(sessionId, downloadPath.trim());

      const filename = downloadPath.split("/").pop() || "download";
      setStatus("success");
      setSuccessMsg(`下载完成: ${filename}`);
      addHistory({
        type: "download",
        filename,
        remotePath: downloadPath.trim(),
        size: 0,
        status: "success",
        message: "下载成功",
      });
    } catch (err) {
      const msg = truncateError(err instanceof Error ? err.message : "下载失败");
      setStatus("error");
      setErrorMsg(msg);
      addHistory({
        type: "download",
        filename: downloadPath.split("/").pop() || "file",
        remotePath: downloadPath.trim(),
        size: 0,
        status: "error",
        message: msg,
      });
    }
  }, [sessionId, downloadPath, isConnected, addHistory]);

  // ── 拖拽处理 ──
  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDragOver(true);
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDragOver(false);
  }, []);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDragOver(false);

    const files = e.dataTransfer.files;
    if (files.length > 0) {
      handleUpload(files[0]);
    }
  }, [handleUpload]);

  // ── 文件选择 ──
  const handleFileSelect = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (files && files.length > 0) {
      handleUpload(files[0]);
    }
    // 重置 input 以允许重复选择同一文件
    e.target.value = "";
  }, [handleUpload]);

  // ── 取消上传 ──
  const handleCancel = useCallback(() => {
    // 1. Abort SSE fetch 流（触发后端 generator 中断 → cancel task + Ctrl+C PTY）
    abortRef.current?.abort();
    // 2. 显式调用取消 API（双重保障，即使 SSE 断连信号传递不及时）
    cancelUpload(sessionId);
    setStatus("idle");
    setProgressInfo(null);
  }, [sessionId]);

  // ── 重置状态 ──
  const resetStatus = useCallback(() => {
    setStatus("idle");
    setErrorMsg("");
    setSuccessMsg("");
    setProgressInfo(null);
  }, []);

  if (!visible) return null;

  return (
    <div className="h-full flex flex-col bg-gray-950 border-t border-white/8 text-xs text-gray-300">
      {/* 标题栏 */}
      <div className="flex items-center justify-between px-3 py-1.5 border-b border-white/8 bg-gray-900/50 shrink-0">
        <span className="font-medium text-gray-400">📁 文件传输</span>
        <button
          type="button"
          onClick={onClose}
          className="text-gray-600 hover:text-gray-400 transition-colors px-1"
          title="关闭"
        >
          ✕
        </button>
      </div>

      {/* 内容区域 */}
      <div className="flex-1 overflow-y-auto px-3 py-2 flex gap-3 min-h-0">
        {/* 左栏：上传 */}
        <div className="flex-1 flex flex-col gap-2 min-w-0">
          <h3 className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider">⬆ 上传</h3>

          {/* 远端路径 */}
          <div className="flex items-center gap-1.5">
            <label className="text-gray-600 shrink-0">目标路径:</label>
            <input
              type="text"
              value={remotePath}
              onChange={(e) => setRemotePath(e.target.value)}
              placeholder="/tmp/"
              className="flex-1 bg-gray-800/60 border border-white/8 rounded px-2 py-1 text-xs text-gray-300 placeholder-gray-700 focus:outline-none focus:border-emerald-500/40 min-w-0"
            />
          </div>

          {/* 拖拽上传区域 */}
          <div
            className={`relative flex-1 min-h-[80px] rounded-lg border-2 border-dashed transition-all cursor-pointer flex items-center justify-center ${
              dragOver
                ? "border-emerald-400/60 bg-emerald-500/5"
                : status === "uploading"
                  ? "border-blue-400/40 bg-blue-500/5"
                  : "border-white/10 hover:border-emerald-500/30 hover:bg-white/2"
            }`}
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
            onClick={() => status !== "uploading" && fileInputRef.current?.click()}
          >
            <input
              ref={fileInputRef}
              type="file"
              className="hidden"
              onChange={handleFileSelect}
            />

            {status === "uploading" && progressInfo ? (
              <_UploadProgress info={progressInfo} onCancel={(e) => { e.stopPropagation(); handleCancel(); }} />
            ) : (
              <div className="text-center px-4">
                <div className="text-lg mb-1">{dragOver ? "📥" : "📤"}</div>
                <p className="text-gray-500">
                  {dragOver ? "释放文件" : "拖拽文件到此处"}
                </p>
                <p className="text-gray-700 mt-0.5">
                  或点击选择文件（最大 10MB）
                </p>
              </div>
            )}
          </div>
        </div>

        {/* 中间分隔线 */}
        <div className="w-px bg-white/8 shrink-0" />

        {/* 右栏：下载 + 历史 */}
        <div className="flex-1 flex flex-col gap-2 min-w-0">
          <h3 className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider">⬇ 下载</h3>

          {/* 远端路径输入 + 下载按钮 */}
          <div className="flex items-center gap-1.5">
            <input
              type="text"
              value={downloadPath}
              onChange={(e) => setDownloadPath(e.target.value)}
              placeholder="远端文件路径，如 /var/log/app.log"
              className="flex-1 bg-gray-800/60 border border-white/8 rounded px-2 py-1 text-xs text-gray-300 placeholder-gray-700 focus:outline-none focus:border-emerald-500/40 min-w-0"
              onKeyDown={(e) => e.key === "Enter" && handleDownload()}
            />
            <button
              type="button"
              onClick={handleDownload}
              disabled={status === "downloading" || !isConnected}
              className="shrink-0 px-2.5 py-1 rounded text-[10px] font-medium bg-emerald-600/80 hover:bg-emerald-500/80 text-white disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              {status === "downloading" ? "下载中..." : "下载"}
            </button>
          </div>

          {/* 历史记录 */}
          <div className="flex-1 min-h-0 overflow-y-auto">
            <h4 className="text-[10px] font-semibold text-gray-600 mb-1">传输记录</h4>
            {history.length === 0 ? (
              <p className="text-gray-700 text-center mt-4">暂无传输记录</p>
            ) : (
              <div className="space-y-1">
                {history.map((record) => (
                  <_HistoryItem key={record.id} record={record} />
                ))}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* 状态栏 */}
      {(errorMsg || successMsg) && (
        <div
          className={`shrink-0 px-3 py-1.5 border-t text-[10px] flex items-center justify-between ${
            errorMsg
              ? "border-red-500/20 bg-red-500/5 text-red-400"
              : "border-emerald-500/20 bg-emerald-500/5 text-emerald-400"
          }`}
        >
          <span className="truncate">{errorMsg || successMsg}</span>
          <button
            type="button"
            onClick={resetStatus}
            className="shrink-0 ml-2 text-gray-600 hover:text-gray-400"
          >
            ✕
          </button>
        </div>
      )}
    </div>
  );
}

// ── 上传进度子组件 ──────────────────────────────

function _UploadProgress({
  info,
  onCancel,
}: {
  info: UploadProgressInfo;
  onCancel: (e: React.MouseEvent) => void;
}) {
  const isPhase1 = info.phase === "browser-to-server";
  const isPhase2 = info.phase === "server-to-remote";

  return (
    <div className="text-center px-3 w-full space-y-2" onClick={(e) => e.stopPropagation()}>
      {/* 文件信息 */}
      <div className="text-[10px] text-gray-500 truncate">
        {info.filename}（{formatSize(info.fileSize)}）
        {info.compressed && (
          <span className="ml-1.5 text-emerald-500/80" title={`gzip 压缩: ${formatSize(info.originalBytes)} → ${formatSize(info.compressedBytes)}`}>
            🗜️ -{info.compressionRatio.toFixed(0)}%
          </span>
        )}
      </div>

      {/* 两阶段步骤指示器 */}
      <div className="flex items-center gap-1.5 justify-center text-[10px]">
        {/* 步骤 1 */}
        <div className={`flex items-center gap-0.5 ${isPhase1 ? "text-blue-400" : isPhase2 ? "text-emerald-500" : "text-gray-600"}`}>
          <span className="w-3.5 h-3.5 rounded-full border flex items-center justify-center text-[8px] shrink-0"
            style={{
              borderColor: isPhase1 ? "rgb(96 165 250)" : isPhase2 ? "rgb(16 185 129)" : "rgb(75 85 99)",
              backgroundColor: isPhase2 ? "rgb(16 185 129)" : "transparent",
              color: isPhase2 ? "white" : "inherit",
            }}
          >
            {isPhase2 ? "✓" : "1"}
          </span>
          <span>上传到服务器</span>
        </div>

        {/* 连接线 */}
        <div className={`w-6 h-px ${isPhase2 ? "bg-emerald-500/60" : "bg-gray-700"}`} />

        {/* 步骤 2 */}
        <div className={`flex items-center gap-0.5 ${isPhase2 ? "text-blue-400" : "text-gray-600"}`}>
          <span className="w-3.5 h-3.5 rounded-full border flex items-center justify-center text-[8px] shrink-0"
            style={{
              borderColor: isPhase2 ? "rgb(96 165 250)" : "rgb(75 85 99)",
            }}
          >
            2
          </span>
          <span>传输到远端</span>
        </div>
      </div>

      {/* 进度条 */}
      {isPhase1 ? (
        <>
          <div className="w-full bg-gray-800 rounded-full h-1.5 overflow-hidden">
            <div
              className="bg-blue-500 h-full rounded-full transition-all duration-300"
              style={{ width: `${info.percent}%` }}
            />
          </div>
          <div className="flex items-center justify-between text-[10px] text-gray-500">
            <span>{formatSize(info.loaded)} / {formatSize(info.fileSize)}</span>
            <span>{info.percent}%</span>
            <span>{formatSpeed(info.speed)}</span>
          </div>
        </>
      ) : info.ptyState === "verifying" ? (
        <>
          {/* 阶段 2 — 校验中：数据传输完毕，远端正在处理 */}
          <div className="w-full bg-gray-800 rounded-full h-1.5 overflow-hidden">
            <div className="bg-emerald-500 h-full rounded-full transition-all duration-500" style={{ width: "100%" }} />
          </div>
          <div className="flex items-center justify-center gap-2 text-[10px] text-emerald-400/80">
            <span className="inline-block w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
            <span>
              {info.ptySubStep === "checksumming"
                ? "🔍 MD5 校验中..."
                : info.compressed
                  ? "📦 解压写入中..."
                  : "📦 解码写入中..."}
            </span>
            <_ElapsedTimer startedAt={info.phase2StartedAt} />
          </div>
        </>
      ) : (
        <>
          {/* 阶段 2 — 传输中：SSE 实时进度（Bugfix #17: percent>=0 就显示进度条） */}
          <div className="w-full bg-gray-800 rounded-full h-1.5 overflow-hidden">
            <div
              className="bg-blue-500 h-full rounded-full transition-all duration-300"
              style={{ width: `${Math.max(info.percent, 1)}%` }}
            />
          </div>
          <div className="flex items-center justify-between text-[10px] text-gray-500">
            <span>
              {formatSize(info.loaded)} / {formatSize(info.compressed ? info.compressedBytes : info.fileSize)}
              {info.compressed && <span className="text-emerald-500/60 ml-0.5">gz</span>}
            </span>
            <span>{info.percent}%</span>
            <span>{formatSpeed(info.speed)}</span>
            <_ElapsedTimer startedAt={info.phase2StartedAt} />
          </div>
        </>
      )}

      <button
        type="button"
        onClick={onCancel}
        className="text-[10px] text-red-400/80 hover:text-red-400 transition-colors"
      >
        取消
      </button>
    </div>
  );
}

// ── 实时耗时计时器 ──────────────────────────────

function _ElapsedTimer({ startedAt }: { startedAt: number }) {
  const [, setTick] = useState(0);

  useEffect(() => {
    if (startedAt <= 0) return;
    const timer = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(timer);
  }, [startedAt]);

  const elapsed = startedAt > 0 ? Date.now() - startedAt : 0;
  return <span className="text-gray-600">{formatElapsed(elapsed)}</span>;
}

// ── 历史记录子组件 ──────────────────────────────

function _HistoryItem({ record }: { record: TransferRecord }) {
  const icon = record.type === "upload" ? "⬆" : "⬇";
  const statusIcon = record.status === "success" ? "✓" : "✗";
  const statusColor = record.status === "success" ? "text-emerald-500" : "text-red-400";
  const time = new Date(record.timestamp).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });

  return (
    <div className="flex items-center gap-1.5 px-1.5 py-1 rounded bg-white/2 hover:bg-white/4 transition-colors">
      <span className="shrink-0 text-[10px]">{icon}</span>
      <span className="truncate flex-1 text-gray-400">{record.filename}</span>
      {record.size > 0 && (
        <span className="shrink-0 text-gray-700">
          {record.size > 1024 * 1024
            ? `${(record.size / 1024 / 1024).toFixed(1)}MB`
            : `${(record.size / 1024).toFixed(0)}KB`}
        </span>
      )}
      <span className={`shrink-0 ${statusColor}`}>{statusIcon}</span>
      <span className="shrink-0 text-gray-700">{time}</span>
    </div>
  );
}
