/**
 * FileTransferPanel — 文件传输面板组件（Tab 式布局）
 *
 * 提供浏览器端文件上传/下载到远端节点的 UI：
 * - Tab 切换：上传 / 下载 / 传输记录
 * - 拖拽上传区域 (Drag & Drop Zone) + 文件选择
 * - 远端路径输入框（支持 localStorage 路径记忆）
 * - 两阶段上传进度（browser→server → server→remote）
 * - 下载进度（SSE 流式）
 * - 传输历史记录（带清除功能）
 * - Toast 通知自动消失
 *
 * 设计原则：
 * - Tab 式布局：每个功能独占全宽，280px 有限高度下信息密度最大化
 * - 低耦合：仅依赖 sessionId prop，通过 api.ts 与后端通信
 * - 错误处理健壮：网络中断、文件过大、远端路径不存在等场景都有友好提示
 */

import { useState, useCallback, useRef, useEffect } from "react";
import { uploadFile, downloadFile, cancelUpload } from "../services/api";
import type { FileUploadResponse, PtyTransferProgress, DownloadCompleteResult } from "../services/api";

// ── 类型定义 ──────────────────────────────────

type TransferStatus = "idle" | "uploading" | "downloading" | "success" | "error";

/** 面板 Tab 页 */
type PanelTab = "upload" | "download" | "history";

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

/** 下载进度详情 */
interface DownloadProgressInfo {
  /** 远端文件总字节数 */
  totalBytes: number;
  /** 已接收字节数 */
  transferredBytes: number;
  /** 进度百分比 0-100 */
  percentage: number;
  /** PTY 传输状态 */
  state: string;
  /** 校验子步骤 */
  subStep: string;
  /** 开始时间 */
  startedAt: number;
  /** 实时速度 */
  speed: number;
  /** ★ O12 压缩信息 */
  compressed: boolean;
  /** 原始文件大小（未压缩） */
  originalBytes: number;
  /** 压缩后大小（0 表示未压缩） */
  compressedBytes: number;
  /** 压缩率百分比 */
  compressionRatio: number;
}

/** Toast 通知 */
interface ToastInfo {
  id: string;
  type: "success" | "error";
  message: string;
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

// ★ Optimization #2: 双阈值文件大小限制 ★
const SOFT_WARN_SIZE = 10 * 1024 * 1024;  // 10MB: 超过此值弹确认框
const MAX_RAW_SIZE = 50 * 1024 * 1024;    // 50MB: 原始大小绝对上限（硬拒绝）
const MAX_HISTORY = 20;
const TOAST_DURATION = 5000; // Toast 自动消失时间 (ms)
const STORAGE_KEY_UPLOAD_PATH = "ft-upload-path";
const STORAGE_KEY_DOWNLOAD_PATH = "ft-download-path";

// ── 工具函数 ──────────────────────────────────

function generateId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function truncateError(msg: string, maxLen = 200): string {
  if (msg.length <= maxLen) return msg;
  return msg.slice(0, maxLen) + "…";
}

function formatSize(bytes: number): string {
  if (bytes === 0) return "0B";
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)}MB`;
}

function formatSpeed(bytesPerSec: number): string {
  if (bytesPerSec <= 0) return "计算中...";
  if (bytesPerSec < 1024) return `${bytesPerSec.toFixed(0)} B/s`;
  if (bytesPerSec < 1024 * 1024) return `${(bytesPerSec / 1024).toFixed(1)} KB/s`;
  return `${(bytesPerSec / 1024 / 1024).toFixed(1)} MB/s`;
}

function formatElapsed(ms: number): string {
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainSec = seconds % 60;
  return `${minutes}m${remainSec}s`;
}

/** 安全读取 localStorage */
function readStoredPath(key: string, fallback: string): string {
  try {
    return localStorage.getItem(key) || fallback;
  } catch {
    return fallback;
  }
}

/** 安全写入 localStorage */
function storePathValue(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // 忽略 quota/安全错误
  }
}

// ── 主组件 ──────────────────────────────────

export default function FileTransferPanel({
  sessionId,
  visible,
  onClose,
  isConnected,
}: FileTransferPanelProps) {
  // ── Tab 状态 ──
  const [activeTab, setActiveTab] = useState<PanelTab>("upload");

  // ── 传输状态 ──
  const [status, setStatus] = useState<TransferStatus>("idle");
  const [progressInfo, setProgressInfo] = useState<UploadProgressInfo | null>(null);
  const [remotePath, setRemotePath] = useState(() => readStoredPath(STORAGE_KEY_UPLOAD_PATH, "/tmp/"));
  const [downloadPath, setDownloadPath] = useState(() => readStoredPath(STORAGE_KEY_DOWNLOAD_PATH, ""));
  const [history, setHistory] = useState<TransferRecord[]>([]);
  const [dragOver, setDragOver] = useState(false);
  const [downloadProgress, setDownloadProgress] = useState<DownloadProgressInfo | null>(null);

  // ── Toast 通知（自动消失） ──
  const [toast, setToast] = useState<ToastInfo | null>(null);
  const toastTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const downloadAbortRef = useRef<AbortController | null>(null);
  const speedSampleRef = useRef<{ time: number; loaded: number }>({ time: 0, loaded: 0 });
  const dlSpeedSampleRef = useRef<{ time: number; loaded: number }>({ time: 0, loaded: 0 });

  // ── Toast 管理 ──
  const showToast = useCallback((type: "success" | "error", message: string) => {
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
    const id = generateId();
    setToast({ id, type, message });
    toastTimerRef.current = setTimeout(() => {
      setToast((prev) => prev?.id === id ? null : prev);
    }, TOAST_DURATION);
  }, []);

  const dismissToast = useCallback(() => {
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
    setToast(null);
  }, []);

  // ── 上传路径持久化 ──
  const updateRemotePath = useCallback((value: string) => {
    setRemotePath(value);
    storePathValue(STORAGE_KEY_UPLOAD_PATH, value);
  }, []);

  // ── 下载路径持久化 ──
  const updateDownloadPath = useCallback((value: string) => {
    setDownloadPath(value);
    storePathValue(STORAGE_KEY_DOWNLOAD_PATH, value);
  }, []);

  // ── 传输中自动切换 Tab ──
  useEffect(() => {
    if (status === "uploading") setActiveTab("upload");
    if (status === "downloading") setActiveTab("download");
  }, [status]);

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

  // ── 清空历史 ──
  const clearHistory = useCallback(() => {
    setHistory([]);
  }, []);

  // ── 上传逻辑 ──
  const handleUpload = useCallback(async (file: File) => {
    if (!isConnected) {
      showToast("error", "终端未连接，无法传输文件");
      return;
    }

    if (file.size > MAX_RAW_SIZE) {
      showToast("error", `文件 ${formatSize(file.size)} 超过上限 ${formatSize(MAX_RAW_SIZE)}，请使用 SCP/SFTP`);
      return;
    }

    if (file.size > SOFT_WARN_SIZE) {
      const confirmed = window.confirm(
        `文件大小 ${formatSize(file.size)} 超过推荐上限 ${formatSize(SOFT_WARN_SIZE)}。\n` +
        `如果文件可压缩（文本/日志等），后端会 gzip 压缩后传输。\n` +
        `如果压缩后仍超过 ${formatSize(SOFT_WARN_SIZE)}，上传会被拒绝。\n\n` +
        `确定继续上传吗？`
      );
      if (!confirmed) return;
    }

    if (file.size === 0) {
      showToast("error", "文件为空，无法上传");
      return;
    }

    const now = Date.now();
    setStatus("uploading");
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
    dismissToast();
    speedSampleRef.current = { time: now, loaded: 0 };

    const targetPath = remotePath.endsWith("/")
      ? remotePath + file.name
      : remotePath;

    abortRef.current = new AbortController();

    try {
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
          const nowMs = Date.now();
          const sample = speedSampleRef.current;
          let speed = 0;
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
        showToast("success", `上传成功: ${file.name} → ${result.remote_path}（${formatSize(result.file_size)}，${elapsed}）`);
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
        showToast("error", truncateError(result.message || "上传失败"));
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
      showToast("error", msg);
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
  }, [sessionId, remotePath, isConnected, addHistory, showToast, dismissToast]);

  // ── 下载逻辑 ──
  const handleDownload = useCallback(async () => {
    if (!isConnected) {
      showToast("error", "终端未连接，无法传输文件");
      return;
    }

    if (!downloadPath.trim()) {
      showToast("error", "请输入远端文件路径");
      return;
    }

    const now = Date.now();
    setStatus("downloading");
    dismissToast();
    dlSpeedSampleRef.current = { time: now, loaded: 0 };
    setDownloadProgress({
      totalBytes: 0,
      transferredBytes: 0,
      percentage: 0,
      state: "transferring",
      subStep: "",
      startedAt: now,
      speed: 0,
      compressed: false,
      originalBytes: 0,
      compressedBytes: 0,
      compressionRatio: 0,
    });

    downloadAbortRef.current = new AbortController();

    try {
      const result: DownloadCompleteResult = await downloadFile(
        sessionId,
        downloadPath.trim(),
        (progress: PtyTransferProgress) => {
          const nowMs = Date.now();
          const sample = dlSpeedSampleRef.current;
          let speed = 0;
          if (nowMs - sample.time >= 500 && progress.transferred > sample.loaded) {
            speed = ((progress.transferred - sample.loaded) / (nowMs - sample.time)) * 1000;
            dlSpeedSampleRef.current = { time: nowMs, loaded: progress.transferred };
          }

          setDownloadProgress((prev) => ({
            totalBytes: progress.total || prev?.totalBytes || 0,
            transferredBytes: progress.transferred,
            percentage: progress.percentage,
            state: progress.state,
            subStep: progress.sub_step || "",
            startedAt: prev?.startedAt || now,
            speed: speed > 0 ? speed : prev?.speed || 0,
            compressed: progress.compressed ?? prev?.compressed ?? false,
            originalBytes: progress.original_bytes ?? prev?.originalBytes ?? 0,
            compressedBytes: progress.compressed_bytes ?? prev?.compressedBytes ?? 0,
            compressionRatio: progress.compression_ratio ?? prev?.compressionRatio ?? 0,
          }));
        },
        downloadAbortRef.current.signal,
      );

      if (result.success) {
        const filename = result.filename || downloadPath.split("/").pop() || "download";
        const elapsed = formatElapsed(Date.now() - now);
        setStatus("success");
        showToast("success", `下载完成: ${filename}（${formatSize(result.file_size || 0)}，${elapsed}）`);
        addHistory({
          type: "download",
          filename,
          remotePath: downloadPath.trim(),
          size: result.file_size || 0,
          status: "success",
          message: result.message,
        });
      } else {
        setStatus("error");
        showToast("error", truncateError(result.message || "下载失败"));
        addHistory({
          type: "download",
          filename: downloadPath.split("/").pop() || "file",
          remotePath: downloadPath.trim(),
          size: 0,
          status: "error",
          message: result.message,
        });
      }
    } catch (err) {
      const msg = truncateError(err instanceof Error ? err.message : "下载失败");
      setStatus("error");
      showToast("error", msg);
      addHistory({
        type: "download",
        filename: downloadPath.split("/").pop() || "file",
        remotePath: downloadPath.trim(),
        size: 0,
        status: "error",
        message: msg,
      });
    } finally {
      downloadAbortRef.current = null;
      setDownloadProgress(null);
    }
  }, [sessionId, downloadPath, isConnected, addHistory, showToast, dismissToast]);

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
    e.target.value = "";
  }, [handleUpload]);

  // ── 取消上传 ──
  const handleCancel = useCallback(() => {
    abortRef.current?.abort();
    cancelUpload(sessionId);
    setStatus("idle");
    setProgressInfo(null);
  }, [sessionId]);

  // ── 历史记录点击：按类型填充路径并跳转对应 Tab ──
  const fillFromHistory = useCallback((record: TransferRecord) => {
    if (record.type === "upload") {
      // 上传记录 → 填充上传目标路径（取目录部分）
      const dir = record.remotePath.replace(/\/[^/]*$/, "/");
      updateRemotePath(dir);
      setActiveTab("upload");
    } else {
      // 下载记录 → 填充下载文件路径
      updateDownloadPath(record.remotePath);
      setActiveTab("download");
    }
  }, [updateRemotePath, updateDownloadPath]);

  if (!visible) return null;

  const historyCount = history.length;

  return (
    <div className="h-full flex flex-col bg-gray-950 border-t border-white/8 text-gray-300">
      {/* ── 标题栏 + Tab 导航 ── */}
      <div className="flex items-center justify-between px-3 py-0 border-b border-white/8 bg-gray-900/50 shrink-0">
        <div className="flex items-center gap-0.5">
          {/* Tab: 上传 */}
          <_TabButton
            active={activeTab === "upload"}
            onClick={() => setActiveTab("upload")}
            badge={status === "uploading" ? "●" : undefined}
            badgeColor="text-blue-400"
          >
            ⬆ 上传
          </_TabButton>

          {/* Tab: 下载 */}
          <_TabButton
            active={activeTab === "download"}
            onClick={() => setActiveTab("download")}
            badge={status === "downloading" ? "●" : undefined}
            badgeColor="text-blue-400"
          >
            ⬇ 下载
          </_TabButton>

          {/* Tab: 记录 */}
          <_TabButton
            active={activeTab === "history"}
            onClick={() => setActiveTab("history")}
            badge={historyCount > 0 ? String(historyCount) : undefined}
            badgeColor="text-gray-400"
          >
            📋 记录
          </_TabButton>
        </div>

        <button
          type="button"
          onClick={onClose}
          className="text-gray-600 hover:text-gray-300 transition-colors p-1.5 rounded hover:bg-white/5"
          title="关闭面板"
        >
          ✕
        </button>
      </div>

      {/* ── Tab 内容区 ── */}
      <div className="flex-1 min-h-0 overflow-y-auto">
        {activeTab === "upload" && (
          <_UploadTab
            remotePath={remotePath}
            onRemotePathChange={updateRemotePath}
            dragOver={dragOver}
            status={status}
            progressInfo={progressInfo}
            isConnected={isConnected}
            fileInputRef={fileInputRef}
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
            onFileSelect={handleFileSelect}
            onCancel={handleCancel}
          />
        )}

        {activeTab === "download" && (
          <_DownloadTab
            downloadPath={downloadPath}
            onDownloadPathChange={updateDownloadPath}
            status={status}
            downloadProgress={downloadProgress}
            isConnected={isConnected}
            onDownload={handleDownload}
          />
        )}

        {activeTab === "history" && (
          <_HistoryTab
            history={history}
            onClear={clearHistory}
            onSelect={fillFromHistory}
          />
        )}
      </div>

      {/* ── Toast 通知（自动消失） ── */}
      {toast && (
        <div
          className={`shrink-0 px-3 py-1.5 border-t text-[11px] flex items-center justify-between gap-2 animate-[fadeIn_0.2s_ease-out] ${
            toast.type === "error"
              ? "border-red-500/20 bg-red-500/5 text-red-400"
              : "border-emerald-500/20 bg-emerald-500/5 text-emerald-400"
          }`}
        >
          <span className="flex items-center gap-1.5 min-w-0">
            <span className="shrink-0">{toast.type === "error" ? "✗" : "✓"}</span>
            <span className="truncate">{toast.message}</span>
          </span>
          <button
            type="button"
            onClick={dismissToast}
            className="shrink-0 text-gray-600 hover:text-gray-400 transition-colors p-0.5"
          >
            ✕
          </button>
        </div>
      )}
    </div>
  );
}

// ── Tab 按钮子组件 ──────────────────────────────

function _TabButton({
  active,
  onClick,
  badge,
  badgeColor = "text-gray-400",
  children,
}: {
  active: boolean;
  onClick: () => void;
  badge?: string;
  badgeColor?: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`relative px-3 py-2 text-[11px] font-medium transition-colors ${
        active
          ? "text-gray-200 border-b-2 border-emerald-400"
          : "text-gray-500 hover:text-gray-300 border-b-2 border-transparent"
      }`}
    >
      <span className="flex items-center gap-1">
        {children}
        {badge && (
          <span className={`text-[9px] ${
            badge === "●"
              ? `${badgeColor} animate-pulse`
              : "bg-gray-700/80 text-gray-300 rounded-full px-1.5 py-0 min-w-[16px] text-center leading-[16px]"
          }`}>
            {badge}
          </span>
        )}
      </span>
    </button>
  );
}

// ── 上传 Tab ──────────────────────────────────

function _UploadTab({
  remotePath,
  onRemotePathChange,
  dragOver,
  status,
  progressInfo,
  isConnected,
  fileInputRef,
  onDragOver,
  onDragLeave,
  onDrop,
  onFileSelect,
  onCancel,
}: {
  remotePath: string;
  onRemotePathChange: (v: string) => void;
  dragOver: boolean;
  status: TransferStatus;
  progressInfo: UploadProgressInfo | null;
  isConnected: boolean;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  onDragOver: (e: React.DragEvent) => void;
  onDragLeave: (e: React.DragEvent) => void;
  onDrop: (e: React.DragEvent) => void;
  onFileSelect: (e: React.ChangeEvent<HTMLInputElement>) => void;
  onCancel: () => void;
}) {
  return (
    <div className="p-3 flex flex-col gap-2.5 h-full">
      {/* 远端路径输入 */}
      <div className="flex items-center gap-2">
        <label className="text-[11px] text-gray-500 shrink-0 w-16">目标路径</label>
        <input
          type="text"
          value={remotePath}
          onChange={(e) => onRemotePathChange(e.target.value)}
          placeholder="/tmp/"
          className="flex-1 bg-gray-800/60 border border-white/8 rounded-md px-2.5 py-1.5 text-xs text-gray-300 placeholder-gray-600 focus:outline-none focus:border-emerald-500/50 focus:ring-1 focus:ring-emerald-500/20 min-w-0 transition-colors"
        />
      </div>

      {/* 拖拽上传区 + 进度区 */}
      <div
        className={`relative flex-1 min-h-[90px] rounded-lg border-2 border-dashed transition-all cursor-pointer flex items-center justify-center ${
          dragOver
            ? "border-emerald-400/60 bg-emerald-500/8 scale-[1.005]"
            : status === "uploading"
              ? "border-blue-400/30 bg-blue-500/5 cursor-default"
              : isConnected
                ? "border-white/10 hover:border-emerald-500/30 hover:bg-white/[0.02]"
                : "border-white/6 opacity-50 cursor-not-allowed"
        }`}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
        onClick={() => status !== "uploading" && isConnected && fileInputRef.current?.click()}
      >
        <input
          ref={fileInputRef}
          type="file"
          className="hidden"
          onChange={onFileSelect}
        />

        {status === "uploading" && progressInfo ? (
          <_UploadProgress info={progressInfo} onCancel={(e) => { e.stopPropagation(); onCancel(); }} />
        ) : (
          <div className="text-center px-6 py-3">
            <div className="text-2xl mb-1.5 opacity-80">{dragOver ? "📥" : "📤"}</div>
            <p className="text-[12px] text-gray-400 font-medium">
              {dragOver ? "释放文件开始上传" : isConnected ? "拖拽文件到此处上传" : "终端未连接"}
            </p>
            {isConnected && !dragOver && (
              <p className="text-[11px] text-gray-600 mt-1">
                或 <span className="text-emerald-500/70 underline underline-offset-2">点击选择文件</span>
                <span className="mx-1.5 text-gray-700">·</span>
                推荐 ≤10MB
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// ── 下载 Tab ──────────────────────────────────

function _DownloadTab({
  downloadPath,
  onDownloadPathChange,
  status,
  downloadProgress,
  isConnected,
  onDownload,
}: {
  downloadPath: string;
  onDownloadPathChange: (v: string) => void;
  status: TransferStatus;
  downloadProgress: DownloadProgressInfo | null;
  isConnected: boolean;
  onDownload: () => void;
}) {
  return (
    <div className="p-3 flex flex-col gap-3 h-full">
      {/* 远端路径输入 + 下载按钮 */}
      <div className="flex items-center gap-2">
        <label className="text-[11px] text-gray-500 shrink-0 w-16">文件路径</label>
        <input
          type="text"
          value={downloadPath}
          onChange={(e) => onDownloadPathChange(e.target.value)}
          placeholder="输入远端文件路径，按 Enter 下载"
          className="flex-1 bg-gray-800/60 border border-white/8 rounded-md px-2.5 py-1.5 text-xs text-gray-300 placeholder-gray-600 focus:outline-none focus:border-emerald-500/50 focus:ring-1 focus:ring-emerald-500/20 min-w-0 transition-colors"
          onKeyDown={(e) => e.key === "Enter" && onDownload()}
        />
        <button
          type="button"
          onClick={onDownload}
          disabled={status === "downloading" || !isConnected}
          className="shrink-0 px-3 py-1.5 rounded-md text-[11px] font-medium bg-emerald-600/80 hover:bg-emerald-500/80 text-white disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
        >
          {status === "downloading" ? "下载中..." : "下载"}
        </button>
      </div>

      {/* 下载进度 */}
      {status === "downloading" && downloadProgress ? (
        <div className="flex-1 flex items-center justify-center">
          <div className="w-full max-w-lg">
            <_DownloadProgress info={downloadProgress} />
          </div>
        </div>
      ) : (
        /* 空状态引导 */
        <div className="flex-1 flex items-center justify-center">
          <div className="text-center">
            <div className="text-2xl mb-1.5 opacity-60">⬇</div>
            <p className="text-[12px] text-gray-500">
              {isConnected ? "输入远端文件绝对路径开始下载" : "终端未连接"}
            </p>
            {isConnected && (
              <p className="text-[11px] text-gray-600 mt-1">
                如 <span className="font-mono text-gray-500">/var/log/app.log</span>
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ── 历史记录 Tab ──────────────────────────────

function _HistoryTab({
  history,
  onClear,
  onSelect,
}: {
  history: TransferRecord[];
  onClear: () => void;
  onSelect: (record: TransferRecord) => void;
}) {
  return (
    <div className="p-3 flex flex-col gap-2 h-full">
      {/* 标题 + 清除按钮 */}
      {history.length > 0 && (
        <div className="flex items-center justify-between">
          <span className="text-[11px] text-gray-500">
            共 {history.length} 条记录
          </span>
          <button
            type="button"
            onClick={onClear}
            className="text-[10px] text-gray-600 hover:text-red-400 transition-colors px-1.5 py-0.5 rounded hover:bg-white/5"
          >
            清除全部
          </button>
        </div>
      )}

      {/* 记录列表 */}
      <div className="flex-1 min-h-0 overflow-y-auto">
        {history.length === 0 ? (
          <div className="flex items-center justify-center h-full">
            <div className="text-center">
              <div className="text-2xl mb-1.5 opacity-40">📋</div>
              <p className="text-[12px] text-gray-600">暂无传输记录</p>
              <p className="text-[11px] text-gray-700 mt-1">上传或下载文件后，记录将显示在这里</p>
            </div>
          </div>
        ) : (
          <div className="space-y-1">
            {history.map((record) => (
              <_HistoryItem
                key={record.id}
                record={record}
                onSelect={onSelect}
              />
            ))}
          </div>
        )}
      </div>
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
    <div className="text-center px-4 w-full space-y-2" onClick={(e) => e.stopPropagation()}>
      {/* 文件信息 */}
      <div className="text-[11px] text-gray-400 truncate">
        {info.filename}
        <span className="text-gray-600 ml-1">({formatSize(info.fileSize)})</span>
        {info.compressed && (
          <span className="ml-1.5 text-emerald-500/80" title={`gzip: ${formatSize(info.originalBytes)} → ${formatSize(info.compressedBytes)}`}>
            🗜️ -{info.compressionRatio.toFixed(0)}%
          </span>
        )}
      </div>

      {/* 两阶段步骤指示器 */}
      <div className="flex items-center gap-2 justify-center text-[11px]">
        <_StepIndicator step={1} label="上传到服务器" isActive={isPhase1} isCompleted={isPhase2} />
        <div className={`w-8 h-px ${isPhase2 ? "bg-emerald-500/60" : "bg-gray-700"}`} />
        <_StepIndicator step={2} label="传输到远端" isActive={isPhase2} isCompleted={false} />
      </div>

      {/* 进度条 */}
      {isPhase1 ? (
        <>
          <div className="w-full bg-gray-800 rounded-full h-2 overflow-hidden">
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
          <div className="w-full bg-gray-800 rounded-full h-2 overflow-hidden">
            <div className="bg-emerald-500 h-full rounded-full transition-all duration-500" style={{ width: "100%" }} />
          </div>
          <div className="flex items-center justify-center gap-2 text-[11px] text-emerald-400/80">
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
          <div className="w-full bg-gray-800 rounded-full h-2 overflow-hidden">
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
        className="text-[11px] text-red-400/80 hover:text-red-400 transition-colors px-2 py-0.5 rounded hover:bg-red-500/10"
      >
        取消上传
      </button>
    </div>
  );
}

// ── 步骤指示器 ──────────────────────────────────

function _StepIndicator({
  step,
  label,
  isActive,
  isCompleted,
}: {
  step: number;
  label: string;
  isActive: boolean;
  isCompleted: boolean;
}) {
  return (
    <div className={`flex items-center gap-1 ${
      isActive ? "text-blue-400" : isCompleted ? "text-emerald-500" : "text-gray-600"
    }`}>
      <span
        className="w-4 h-4 rounded-full border flex items-center justify-center text-[9px] shrink-0"
        style={{
          borderColor: isActive ? "rgb(96 165 250)" : isCompleted ? "rgb(16 185 129)" : "rgb(75 85 99)",
          backgroundColor: isCompleted ? "rgb(16 185 129)" : "transparent",
          color: isCompleted ? "white" : "inherit",
        }}
      >
        {isCompleted ? "✓" : step}
      </span>
      <span className="text-[10px]">{label}</span>
    </div>
  );
}

// ── 下载进度子组件 ──────────────────────────────

function _DownloadProgress({ info }: { info: DownloadProgressInfo }) {
  const isVerifying = info.state === "verifying";

  return (
    <div className="space-y-2">
      {/* 压缩标签 */}
      {info.compressed && (
        <div className="flex items-center gap-1.5 text-[11px] text-emerald-500/80">
          <span title={`gzip: ${formatSize(info.originalBytes)} → ${formatSize(info.compressedBytes)}`}>
            🗜️ 压缩传输 -{info.compressionRatio.toFixed(0)}%
          </span>
          <span className="text-gray-600">
            ({formatSize(info.originalBytes)} → {formatSize(info.compressedBytes)})
          </span>
        </div>
      )}

      {isVerifying ? (
        <>
          <div className="w-full bg-gray-800 rounded-full h-2 overflow-hidden">
            <div className="bg-emerald-500 h-full rounded-full transition-all duration-500" style={{ width: "100%" }} />
          </div>
          <div className="flex items-center justify-center gap-2 text-[11px] text-emerald-400/80">
            <span className="inline-block w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
            <span>
              {info.subStep === "checksumming"
                ? "🔍 MD5 校验中..."
                : info.compressed
                  ? "📦 解压写入中..."
                  : "📦 解码写入中..."}
            </span>
            <_ElapsedTimer startedAt={info.startedAt} />
          </div>
        </>
      ) : (
        <>
          <div className="w-full bg-gray-800 rounded-full h-2 overflow-hidden">
            <div
              className="bg-blue-500 h-full rounded-full transition-all duration-300"
              style={{ width: `${Math.max(info.percentage, info.totalBytes > 0 ? 1 : 0)}%` }}
            />
          </div>
          <div className="flex items-center justify-between text-[11px] text-gray-500">
            <span>
              {info.totalBytes > 0
                ? `${formatSize(info.transferredBytes)} / ${formatSize(info.totalBytes)}`
                : `${formatSize(info.transferredBytes)}`}
              {info.compressed && <span className="text-emerald-500/60 ml-0.5">gz</span>}
            </span>
            {info.percentage > 0 && <span>{info.percentage}%</span>}
            {info.speed > 0 && <span>{formatSpeed(info.speed)}</span>}
            <_ElapsedTimer startedAt={info.startedAt} />
          </div>
        </>
      )}
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

// ── 历史记录条目子组件 ──────────────────────────

function _HistoryItem({
  record,
  onSelect,
}: {
  record: TransferRecord;
  onSelect: (record: TransferRecord) => void;
}) {
  const isUpload = record.type === "upload";
  const isSuccess = record.status === "success";
  const [expanded, setExpanded] = useState(!isSuccess); // 失败记录默认展开
  const time = new Date(record.timestamp).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });

  const clickHint = isUpload
    ? "点击填充目标路径到「上传」"
    : "点击填充文件路径到「下载」";

  const hasError = !isSuccess && record.message;

  return (
    <div
      className={`rounded-md transition-colors ${
        isSuccess
          ? "bg-white/[0.02] hover:bg-white/[0.05]"
          : "bg-red-500/[0.03] hover:bg-red-500/[0.06] border border-red-500/10"
      }`}
    >
      {/* 主行 */}
      <div
        className="flex items-center gap-2 px-2 py-1.5 cursor-pointer group"
        onClick={() => onSelect(record)}
        title={`${record.remotePath}\n${clickHint}`}
      >
        {/* 类型标签 */}
        <span className={`shrink-0 inline-flex items-center gap-0.5 rounded px-1.5 py-0.5 text-[10px] font-medium ${
          isUpload
            ? "bg-blue-500/10 text-blue-400 border border-blue-500/20"
            : "bg-emerald-500/10 text-emerald-400 border border-emerald-500/20"
        }`}>
          {isUpload ? "⬆ 上传" : "⬇ 下载"}
        </span>

        {/* 文件名 */}
        <span className="truncate flex-1 text-[11px] text-gray-400 group-hover:text-gray-300 transition-colors">
          {record.filename}
        </span>

        {/* 文件大小 */}
        {record.size > 0 && (
          <span className="shrink-0 text-[10px] text-gray-600">
            {formatSize(record.size)}
          </span>
        )}

        {/* 状态 */}
        <span className={`shrink-0 text-[10px] ${
          isSuccess ? "text-emerald-400" : "text-red-400"
        }`}>
          {isSuccess ? "✓" : "✗"}
        </span>

        {/* 错误展开/收起切换 */}
        {hasError && (
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); setExpanded((v) => !v); }}
            className="shrink-0 text-[10px] text-gray-600 hover:text-gray-400 transition-colors px-0.5"
            title={expanded ? "收起错误详情" : "展开错误详情"}
          >
            {expanded ? "▾" : "▸"}
          </button>
        )}

        {/* 时间 */}
        <span className="shrink-0 text-[10px] text-gray-700 tabular-nums">{time}</span>
      </div>

      {/* 错误详情（第二行） */}
      {hasError && expanded && (
        <div className="px-2 pb-1.5 pt-0">
          <div className="flex items-start gap-1.5 pl-[52px]">
            <span className="shrink-0 text-[10px] text-red-500/60">原因:</span>
            <span className="text-[10px] text-red-400/80 break-all leading-relaxed">
              {record.message}
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
