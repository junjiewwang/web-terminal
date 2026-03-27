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
import type { Host, TerminalInstance } from "../services/api";
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
}

export default function TerminalView({
  host,
  isActive,
  onInstanceNameUpdate,
  initialWsUrl,
}: TerminalViewProps) {
  const [status, setStatus] = useState<ConnectionStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const [wsUrl, setWsUrl] = useState<string | null>(null);
  const prevHostIdRef = useRef<number | null>(null);

  // ── xterm.js 终端 Hook ──
  const terminal = useTerminal({
    onData: (data) => {
      ws.sendInput(data);
    },
    onResize: (size) => {
      ws.sendResize(size);
    },
  });

  // ── 原生 WebSocket 连接 Hook ──
  const ws = useWebSocket({
    wsUrl,
    onData: (data) => {
      terminal.write(data);
    },
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
  const connectToHost = useCallback(async (targetHost: Host) => {
    setStatus("starting");
    setError(null);
    setWsUrl(null);

    try {
      const instance: TerminalInstance = await startTerminal(targetHost.id);

      // 更新 Tab 的 instanceName（用于关闭时 stop 正确的实例）
      if (onInstanceNameUpdate) {
        onInstanceNameUpdate(instance.instance_name);
      }

      setWsUrl(instance.ws_url);
      setStatus("connecting");
    } catch (err) {
      const msg = err instanceof Error ? err.message : "启动终端失败";
      setError(msg);
      setStatus("error");
    }
  }, [onInstanceNameUpdate]);

  // ── 当 host 变化时自动连接 ──
  useEffect(() => {
    if (!host) {
      if (wsUrl) {
        ws.disconnect();
        setWsUrl(null);
      }
      setStatus("idle");
      setError(null);
      prevHostIdRef.current = null;
      return;
    }

    if (host.id === prevHostIdRef.current) return;

    if (wsUrl) {
      ws.disconnect();
      setWsUrl(null);
    }

    prevHostIdRef.current = host.id;

    // 如果外部传入了 wsUrl（Agent 已创建会话），直接连接 WebSocket
    if (initialWsUrl) {
      setWsUrl(initialWsUrl);
      setStatus("connecting");
    } else {
      connectToHost(host);
    }
  }, [host, connectToHost, wsUrl, ws, initialWsUrl]);

  // ── 空状态 ──
  if (!host) {
    return (
      <div className="flex items-center justify-center h-full text-gray-600">
        <div className="text-center">
          <div className="text-4xl mb-4">🖥</div>
          <p className="text-lg">选择左侧主机开始使用</p>
          <p className="text-sm mt-2 text-gray-700">
            或通过 MCP Client 连接 Agent 工具
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

function _StatusBar({
  host,
  status,
  socketStatus,
  onReconnect,
}: {
  host: Host;
  status: ConnectionStatus;
  socketStatus: SocketStatus;
  onReconnect?: () => void;
}) {
  const cfg = STATUS_MAP[status];
  return (
    <div className="flex items-center justify-between px-3 py-1.5 bg-gray-900/80 text-xs text-gray-500">
      <span>
        Terminal: {host.name}
        {status === "connected" && socketStatus === "connected" && (
          <span className="ml-2 text-gray-700">(ws ✓)</span>
        )}
      </span>
      <div className="flex items-center gap-2">
        <span className={cfg.dot}>●</span>
        <span>{cfg.label}</span>
        {status === "error" && onReconnect && (
          <button
            onClick={onReconnect}
            className="ml-2 px-1.5 py-0.5 text-[10px] text-gray-500 hover:text-emerald-400 hover:bg-gray-800 rounded transition-colors"
            title="重新连接"
          >
            ↻
          </button>
        )}
      </div>
    </div>
  );
}
