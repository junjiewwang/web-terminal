/**
 * TerminalView — 终端视图组件
 *
 * 整合 useTerminal（xterm.js UI）+ useWebSocket（原生 WebSocket），
 * 实现完整的 Web Terminal 功能。
 *
 * 新架构（替代 WeTTY socket.io）：
 *  - 后端 Python PTY 直接通过 FastAPI WebSocket 连接
 *  - 无需 socket.io、无需独立端口、无需 nginx 反代
 *  - 连接即时（无 Node.js 冷启动延迟）
 *
 * 数据流：
 *  用户键入 → onData → ws.send({type:"input"}) → FastAPI → PTY → tmux → SSH → 远端
 *  远端 → SSH → tmux → PTY → FastAPI → ws.onmessage({type:"output"}) → terminal.write()
 */

import { useState, useEffect, useCallback, useRef } from "react";
import type { Host, TerminalInstance, TerminalBackend } from "../services/api";
import { startTerminal } from "../services/api";
import { useTerminal } from "../hooks/useTerminal";
import { useWebSocket, type SocketStatus } from "../hooks/useWebSocket";

// xterm.js 样式（必须导入，否则终端无法正确渲染）
import "@xterm/xterm/css/xterm.css";

// ── 终端连接状态 ──────────────────────────────
type ConnectionStatus = "idle" | "starting" | "connecting" | "connected" | "error";

interface TerminalViewProps {
  host: Host;
  /** 是否为当前活跃 Tab（控制显隐，非活跃时保持连接） */
  isActive: boolean;
  /** instanceName 更新回调（用于 Tab 关闭时 stop 正确的实例） */
  onInstanceNameUpdate?: (instanceName: string) => void;
  /**
   * 外部传入的 WebSocket URL（如 /ws/terminal/{session_id}）。
   * 当 Agent 通过 MCP 已创建会话时，前端通过 SSE 感知后直接传入 ws_url，
   * 跳过 startTerminal API 调用，直接建立 WebSocket 连接。
   */
  initialWsUrl?: string | null;
  /** 当前使用的 backend 类型 */
  backend?: TerminalBackend | null;
}

export default function TerminalView({
  host,
  isActive,
  onInstanceNameUpdate,
  initialWsUrl,
  backend,
}: TerminalViewProps) {
  const [status, setStatus] = useState<ConnectionStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const [wsUrl, setWsUrl] = useState<string | null>(null);
  const prevHostIdRef = useRef<number | null>(null);
  /** 当前会话实际使用的 backend（从后端响应中获取） */
  const [currentBackend, setCurrentBackend] = useState<TerminalBackend | null>(backend ?? null);

  // ── scrollback 历史回放标志 ──
  // 当正在回放 scrollback 时，屏蔽 xterm.js 的 onData 输出到 WebSocket，
  // 防止 xterm.js 对回放中的终端查询序列生成响应（如 DA response）发送到 PTY 导致乱码。
  const historyReplayRef = useRef(false);

  // ── xterm.js 终端 Hook ──
  const terminal = useTerminal({
    onData: (data) => {
      // 回放历史期间屏蔽所有 onData（xterm.js 对查询序列的自动响应也走这里）
      if (historyReplayRef.current) return;
      ws.sendInput(data);
    },
    onResize: (size) => {
      ws.sendResize(size);
    },
  });

  // ── 原生 WebSocket 连接 Hook ──
  const [toast, setToast] = useState<string | null>(null);
  const toastTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const showCopyToast = useCallback((text: string) => {
    // 使用 textarea + execCommand 复制到剪贴板
    // 这种方式不需要浏览器权限提示，用户体验更好
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.cssText = "position:fixed;left:-9999px;opacity:0;pointer-events:none";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();

    let success = false;
    try {
      success = document.execCommand("copy");
    } catch {
      success = false;
    }

    document.body.removeChild(ta);

    // 显示 toast（只在成功时显示）
    if (success) {
      if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
      setToast("已复制到剪贴板");
      toastTimerRef.current = setTimeout(() => setToast(null), 2000);
    } else {
      if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
      setToast("复制失败，请手动复制");
      toastTimerRef.current = setTimeout(() => setToast(null), 3000);
    }
  }, []);

  const ws = useWebSocket({
    wsUrl,
    onData: (data) => {
      terminal.write(data);
    },
    onHistory: (data) => {
      // Scrollback 历史回放：写入 xterm.js 但屏蔽 onData 回调，
      // 防止 xterm.js 对回放中的终端查询序列生成 DA/CPR 响应发送到 PTY
      historyReplayRef.current = true;
      terminal.write(data);
      // 使用 requestAnimationFrame 确保 xterm.js 完成处理后再恢复
      requestAnimationFrame(() => {
        historyReplayRef.current = false;
      });
    },
    onClipboard: showCopyToast,
    onConnect: () => {
      setStatus("connected");
      requestAnimationFrame(() => {
        terminal.fit();
        terminal.focus();
        ws.sendResize(terminal.getSize());
      });
    },
    onDisconnect: (reason) => {
      if (reason === "closed" || reason === "normal") {
        setStatus("idle");
        setWsUrl(null);
      }
    },
  });

  // 同步 WebSocket 状态到组件状态
  useEffect(() => {
    if (ws.status === "error" && status !== "error") {
      setStatus("error");
      setError("WebSocket 连接失败，请检查终端服务是否正常运行");
    }
    if (ws.status === "connecting" && status === "starting") {
      setStatus("connecting");
    }
  }, [ws.status, status]);

  // ── 活跃状态变化时 fit 终端 ──
  useEffect(() => {
    if (isActive && status === "connected") {
      requestAnimationFrame(() => {
        terminal.fit();
        terminal.focus();
      });
    }
  }, [isActive, status, terminal]);

  // ── 启动终端会话 ──
  const connectToHost = useCallback(async (targetHost: Host, requestBackend?: TerminalBackend) => {
    setStatus("starting");
    setError(null);
    setWsUrl(null);

    try {
      const instance: TerminalInstance = await startTerminal(targetHost.id, requestBackend);

      // 更新 Tab 的 instanceName（用于关闭时 stop 正确的实例）
      if (onInstanceNameUpdate) {
        onInstanceNameUpdate(instance.instance_name);
      }

      // 记录后端实际使用的 backend
      setCurrentBackend(instance.backend);

      setWsUrl(instance.ws_url);
      setStatus("connecting");
    } catch (err) {
      const msg = err instanceof Error ? err.message : "启动终端失败";
      setError(msg);
      setStatus("error");
    }
  }, [onInstanceNameUpdate]);

  // ── 当 host 变化或 initialWsUrl 变化（backend 切换）时自动连接 ──
  const prevInitialWsUrlRef = useRef<string | null | undefined>(initialWsUrl);
  useEffect(() => {
    if (!host) {
      if (wsUrl) {
        ws.disconnect();
        setWsUrl(null);
      }
      setStatus("idle");
      setError(null);
      prevHostIdRef.current = null;
      prevInitialWsUrlRef.current = initialWsUrl;
      return;
    }

    const hostChanged = host.id !== prevHostIdRef.current;
    const wsUrlChanged = initialWsUrl !== prevInitialWsUrlRef.current;

    // 既没有 host 变化也没有 wsUrl 变化（backend 切换），跳过
    if (!hostChanged && !wsUrlChanged) return;

    // backend 切换时重置 xterm.js 状态：清屏 + 关闭残留的 DEC Private Mode
    // （TMUX 会启用 ?1000h/?1002h 鼠标追踪，切到 Broker 后若不重置，
    //  鼠标滚轮会被编码为鼠标序列而非本地 scrollback 滚动）
    if (wsUrlChanged && !hostChanged) {
      terminal.reset();
    }

    if (wsUrl) {
      ws.disconnect();
      setWsUrl(null);
    }

    prevHostIdRef.current = host.id;
    prevInitialWsUrlRef.current = initialWsUrl;

    // 更新 backend 状态
    if (backend) {
      setCurrentBackend(backend);
    }

    // 如果外部传入了 wsUrl（Agent 已创建会话 或 backend 切换后），直接连接 WebSocket
    if (initialWsUrl) {
      setWsUrl(initialWsUrl);
      setStatus("connecting");
    } else {
      connectToHost(host);
    }
  }, [host, connectToHost, wsUrl, ws, initialWsUrl, backend]);

  // ── 空状态 ──
  if (!host) {
    return (
      <div className="flex h-full items-center justify-center text-gray-600">
        <div className="text-center">
          <div className="mb-4 text-4xl">🖥</div>
          <p className="text-lg">选择左侧主机开始使用</p>
          <p className="mt-2 text-sm text-gray-700">
            也可由 Agent 自动创建会话后在此接管
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col">
      <_StatusBar
        host={host}
        status={status}
        socketStatus={ws.status}
        backend={currentBackend}
        onReconnect={() => connectToHost(host)}
      />

      {/* 终端容器 */}
      <div
        ref={terminal.containerRef}
        className="flex-1 bg-[#0a0a0a] relative overflow-hidden"
        style={{
          minHeight: 0,
          display: isActive ? undefined : "none",
        }}
      >
        {/* tmux copy-mode 复制 toast */}
        {toast && isActive && (
          <div className="absolute top-2 right-2 z-20 px-3 py-1.5 bg-emerald-600/90 text-white text-xs rounded shadow-lg pointer-events-none animate-pulse">
            {toast}
          </div>
        )}

        {(status === "starting" || status === "connecting") && (
          <div className="absolute inset-0 z-10 flex items-center justify-center bg-black/80">
            <div className="text-center">
              <div className="animate-spin inline-block w-6 h-6 border-2 border-gray-600 border-t-emerald-400 rounded-full mb-3" />
              <p className="text-sm text-gray-400">
                {status === "starting" ? "正在启动终端..." : "正在连接..."}
              </p>
              <p className="text-xs text-gray-600 mt-1">
                {host.username}@{host.hostname}
              </p>
            </div>
          </div>
        )}

        {status === "error" && (
          <div className="absolute inset-0 z-10 flex items-center justify-center bg-black/80">
            <div className="text-center max-w-sm">
              <div className="text-3xl mb-3">⚠️</div>
              <p className="text-sm text-red-400 mb-2">终端连接失败</p>
              <p className="text-xs text-gray-600 mb-4">{error}</p>
              <button
                onClick={() => connectToHost(host)}
                className="px-4 py-1.5 text-xs bg-emerald-700 hover:bg-emerald-600 text-white rounded transition-colors"
              >
                重新连接
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── 状态栏子组件 ──────────────────────────────

const STATUS_MAP: Record<ConnectionStatus, { dot: string; label: string }> = {
  idle: { dot: "text-gray-500", label: "未连接" },
  starting: { dot: "text-yellow-400 animate-pulse", label: "启动中..." },
  connecting: { dot: "text-yellow-400 animate-pulse", label: "连接中..." },
  connected: { dot: "text-emerald-500", label: "已连接" },
  error: { dot: "text-red-500", label: "连接失败" },
};

/** Backend badge 配置 */
const BACKEND_CONFIG: Record<TerminalBackend, { label: string; color: string }> = {
  tmux: { label: "TMUX", color: "border-blue-500/30 bg-blue-500/10 text-blue-400" },
  broker: { label: "BROKER", color: "border-emerald-500/30 bg-emerald-500/10 text-emerald-400" },
};

function _StatusBar({
  host,
  status,
  socketStatus,
  backend,
  onReconnect,
}: {
  host: Host;
  status: ConnectionStatus;
  socketStatus: SocketStatus;
  backend?: TerminalBackend | null;
  onReconnect?: () => void;
}) {
  const cfg = STATUS_MAP[status];
  const backendCfg = backend ? BACKEND_CONFIG[backend] : null;

  return (
    <div className="flex items-center justify-between border-b border-white/8 bg-gray-950/70 px-3 py-2 text-xs text-gray-500 backdrop-blur-sm">
      <span className="flex items-center gap-2 truncate">
        <span className="truncate">
          WebTerminal · {host.name}
          {status === "connected" && socketStatus === "connected" && (
            <span className="ml-2 text-gray-700">(ws ✓)</span>
          )}
        </span>
        {backendCfg && (
          <span className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-medium ${backendCfg.color}`}>
            {backendCfg.label}
          </span>
        )}
      </span>
      <div className="ml-3 flex items-center gap-2 shrink-0">
        <span className={cfg.dot}>●</span>
        <span>{cfg.label}</span>
        {status === "error" && onReconnect && (
          <button
            onClick={onReconnect}
            className="ml-2 rounded px-1.5 py-0.5 text-[10px] text-gray-500 transition-colors hover:bg-gray-800 hover:text-emerald-400"
            title="重新连接"
          >
            ↻
          </button>
        )}
      </div>
    </div>
  );
}
