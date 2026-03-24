/**
 * TerminalView — 终端视图组件
 *
 * 整合 useTerminal（xterm.js UI）+ useWettySocket（socket.io 连接），
 * 实现完整的 Web Terminal 功能。
 *
 * 替代原来的 iframe 方案：
 *  - 消除 iframe sandbox / SSE 连接阻塞 / web_modules 缺失等问题
 *  - 终端 UI 完全可控（主题、字体、尺寸自适应）
 *  - 与 React 组件树同处一个上下文，便于 Agent 面板联动
 *
 * 数据流：
 *  用户键入 → onData → socket.emit('input') → WeTTY → SSH → 远程主机
 *  远程主机 → SSH → WeTTY → socket.on('data') → terminal.write()
 */

import { useState, useEffect, useCallback, useRef } from "react";
import type { Host, WeTTYInstance } from "../services/api";
import { startWeTTY, stopWeTTY } from "../services/api";
import { useTerminal } from "../hooks/useTerminal";
import { useWettySocket, type SocketStatus } from "../hooks/useWettySocket";

// xterm.js 样式（必须导入，否则终端无法正确渲染）
import "@xterm/xterm/css/xterm.css";

// ── 终端连接状态 ──────────────────────────────
type ConnectionStatus = "idle" | "starting" | "connecting" | "connected" | "error";

interface TerminalViewProps {
  host: Host | null;
}

export default function TerminalView({ host }: TerminalViewProps) {
  const [status, setStatus] = useState<ConnectionStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const [basePath, setBasePath] = useState<string | null>(null);
  const prevHostIdRef = useRef<number | null>(null);

  // ── xterm.js 终端 Hook（返回 callback ref） ──
  const terminal = useTerminal({
    onData: (data) => {
      // 用户输入 → WeTTY server
      wettySocket.sendInput(data);
    },
    onResize: (size) => {
      // 终端尺寸变化 → WeTTY server
      wettySocket.sendResize(size);
    },
  });

  // ── socket.io 连接 Hook ─────────────────────
  const wettySocket = useWettySocket({
    basePath,
    onData: (data) => {
      // WeTTY server 输出 → 终端渲染
      terminal.write(data);
    },
    onConnect: () => {
      setStatus("connected");
      // 连接成功后聚焦终端并同步初始尺寸
      requestAnimationFrame(() => {
        terminal.fit();
        terminal.focus();
        // 发送初始尺寸给 WeTTY
        wettySocket.sendResize(terminal.getSize());
      });
    },
    onDisconnect: (reason) => {
      if (reason === "logout") {
        setStatus("idle");
        setBasePath(null);
      }
      // transport close / io server disconnect 等非主动断开，
      // socket.io 会自动重连，不改状态
    },
  });

  // 同步 socket 状态到组件状态
  useEffect(() => {
    if (wettySocket.status === "error" && status !== "error") {
      setStatus("error");
      setError("WebSocket 连接失败，请检查终端服务是否正常运行");
    }
    if (wettySocket.status === "connecting" && status === "starting") {
      setStatus("connecting");
    }
  }, [wettySocket.status, status]);

  // ── 启动 WeTTY 实例 ─────────────────────────
  const connectToHost = useCallback(async (targetHost: Host) => {
    setStatus("starting");
    setError(null);
    setBasePath(null);

    try {
      const instance: WeTTYInstance = await startWeTTY(targetHost.id);
      // instance.url 格式如 /wetty/t/tce-server/
      // basePath 需要去掉尾部斜杠
      const path = instance.url.replace(/\/$/, "");
      setBasePath(path);
      setStatus("connecting");
    } catch (err) {
      const msg = err instanceof Error ? err.message : "启动终端失败";
      setError(msg);
      setStatus("error");
    }
  }, []);

  // ── 断开连接 ────────────────────────────────
  const disconnectFromHost = useCallback(() => {
    if (host) {
      stopWeTTY(host.name).catch(console.error);
    }
    wettySocket.disconnect();
    setBasePath(null);
    setStatus("idle");
    setError(null);
    prevHostIdRef.current = null;
  }, [host, wettySocket]);

  // ── 当选中主机变化时，自动启动 WeTTY ──
  useEffect(() => {
    if (!host) {
      // 取消选中：清理状态
      if (basePath) {
        wettySocket.disconnect();
        setBasePath(null);
      }
      setStatus("idle");
      setError(null);
      prevHostIdRef.current = null;
      return;
    }

    // 同一主机无需重复启动
    if (host.id === prevHostIdRef.current) return;

    // 切换主机：先断开旧连接
    if (basePath) {
      wettySocket.disconnect();
      setBasePath(null);
    }

    prevHostIdRef.current = host.id;
    connectToHost(host);
  }, [host, connectToHost, basePath, wettySocket]);

  // ── 空状态：未选中主机 ──
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
        socketStatus={wettySocket.status}
        onDisconnect={disconnectFromHost}
        onReconnect={() => connectToHost(host)}
      />

      {/* 终端容器（使用 callback ref，容器挂载时自动初始化 xterm.js） */}
      <div
        ref={terminal.containerRef}
        className="flex-1 bg-[#0a0a0a] relative overflow-hidden"
        style={{ minHeight: 0 }}
      >
        {/* 连接中遮罩 */}
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

        {/* 错误遮罩 */}
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
  onDisconnect,
  onReconnect,
}: {
  host: Host;
  status: ConnectionStatus;
  socketStatus: SocketStatus;
  onDisconnect?: () => void;
  onReconnect?: () => void;
}) {
  const cfg = STATUS_MAP[status];
  return (
    <div className="flex items-center justify-between px-3 py-1.5 bg-gray-900/80 text-xs text-gray-500">
      <span>
        Terminal: {host.name}
        {status === "connected" && socketStatus === "connected" && (
          <span className="ml-2 text-gray-700">(socket.io ✓)</span>
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
        {(status === "connected" || status === "connecting") && onDisconnect && (
          <button
            onClick={onDisconnect}
            className="ml-2 px-1.5 py-0.5 text-[10px] text-gray-500 hover:text-red-400 hover:bg-gray-800 rounded transition-colors"
            title="断开终端"
          >
            ✕
          </button>
        )}
      </div>
    </div>
  );
}
