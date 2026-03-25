/**
 * TerminalView — 终端视图组件
 *
 * 整合 useTerminal（xterm.js UI）+ useWettySocket（socket.io 连接），
 * 实现完整的 Web Terminal 功能。
 *
 * 多 Tab 独立终端模式：
 *  - 每个 Tab 持有独立的 TerminalView 实例（独立的 socket.io 连接 + xterm.js）
 *  - isActive 控制显隐：非活跃时 display:none 但保持连接不销毁
 *  - 连接成功后自动获取 client_tty，通知父组件用于 per-client tmux switch
 *
 * 数据流：
 *  用户键入 → onData → socket.emit('input') → WeTTY → SSH → 远程主机
 *  远程主机 → SSH → WeTTY → socket.on('data') → terminal.write()
 */

import { useState, useEffect, useCallback, useRef } from "react";
import type { Host, WeTTYInstance } from "../services/api";
import { startWeTTY, stopWeTTY, fetchClientTtys } from "../services/api";
import { useTerminal } from "../hooks/useTerminal";
import { useWettySocket, type SocketStatus } from "../hooks/useWettySocket";

// xterm.js 样式（必须导入，否则终端无法正确渲染）
import "@xterm/xterm/css/xterm.css";

// ── 终端连接状态 ──────────────────────────────
type ConnectionStatus = "idle" | "starting" | "connecting" | "connected" | "error";

interface TerminalViewProps {
  host: Host;
  /** 是否为当前活跃 Tab（控制显隐，非活跃时保持连接） */
  isActive: boolean;
  /** tmux client TTY 获取成功后的回调（用于 per-client 窗口切换） */
  onClientTtyReady?: (tty: string) => void;
}

export default function TerminalView({
  host,
  isActive,
  onClientTtyReady,
}: TerminalViewProps) {
  const [status, setStatus] = useState<ConnectionStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const [basePath, setBasePath] = useState<string | null>(null);
  const prevHostIdRef = useRef<number | null>(null);
  const clientTtyReportedRef = useRef(false);

  // ── xterm.js 终端 Hook（返回 callback ref） ──
  const terminal = useTerminal({
    onData: (data) => {
      wettySocket.sendInput(data);
    },
    onResize: (size) => {
      wettySocket.sendResize(size);
    },
  });

  // ── socket.io 连接 Hook ─────────────────────
  const wettySocket = useWettySocket({
    basePath,
    onData: (data) => {
      terminal.write(data);
    },
    onConnect: () => {
      setStatus("connected");
      requestAnimationFrame(() => {
        terminal.fit();
        terminal.focus();
        wettySocket.sendResize(terminal.getSize());
      });
    },
    onDisconnect: (reason) => {
      if (reason === "logout") {
        setStatus("idle");
        setBasePath(null);
        clientTtyReportedRef.current = false;
      }
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

  // ── 获取 client_tty 并上报（仅共享 WeTTY 模式需要）──
  // 独立 WeTTY 模式下每个 Tab 有独立的 WeTTY 实例，不需要 tmux 窗口切换
  useEffect(() => {
    if (status !== "connected" || clientTtyReportedRef.current || !onClientTtyReady) {
      return;
    }

    // 从 basePath 提取堡垒机名：/wetty/t/{bastion_name}
    const bastionFromPath = basePath?.match(/\/wetty\/t\/([^/]+)/)?.[1];

    if (!bastionFromPath) return;

    // 独立 WeTTY 实例名包含 "--"（如 tce-server--m12），不需要 tmux 窗口切换
    if (bastionFromPath.includes("--")) {
      console.log("独立 WeTTY 模式，跳过 tmux 窗口切换");
      clientTtyReportedRef.current = true;
      return;
    }

    // 延迟获取 client TTY（等待 tmux client 注册完成）
    const timer = setTimeout(async () => {
      try {
        const clients = await fetchClientTtys(bastionFromPath);
        
        if (clients.length === 0) {
          console.warn("没有找到 tmux client");
          return;
        }
        
        // 根据目标窗口名找到对应的 client
        // - bastion 类型：窗口名是 "0"（默认窗口）或 "sshpass"
        // - jump_host 类型：窗口名是 jump_host 名称（如 m12）
        const targetWindow = host.host_type === "jump_host" ? host.name : "0";
        
        // 优先匹配目标窗口的 client（但由于 tmux window 字段可能为空，这不总是可靠）
        let myClient = clients.find(c => c.window === targetWindow);
        
        // 如果没找到（窗口字段为空），取最后一个 client（最新连接的）
        if (!myClient) {
          myClient = clients[clients.length - 1];
        }
        
        if (myClient) {
          clientTtyReportedRef.current = true;
          onClientTtyReady(myClient.tty);
        }
      } catch {
        // 获取 client_tty 失败不阻断终端使用
        console.warn("获取 tmux client TTY 失败，Tab 切换将使用全局模式");
      }
    }, 1000);  // 增加延迟，等待后台 PTY 稳定

    return () => clearTimeout(timer);
  }, [status, basePath, host, onClientTtyReady]);

  // ── 活跃状态变化时 fit 终端（容器尺寸可能变化） ──
  useEffect(() => {
    if (isActive && status === "connected") {
      requestAnimationFrame(() => {
        terminal.fit();
        terminal.focus();
      });
    }
  }, [isActive, status, terminal]);

  // ── 启动 WeTTY 实例 ─────────────────────────
  const connectToHost = useCallback(async (targetHost: Host) => {
    setStatus("starting");
    setError(null);
    setBasePath(null);
    clientTtyReportedRef.current = false;

    try {
      const instance: WeTTYInstance = await startWeTTY(targetHost.id);

      let path: string;
      if (instance.bastion_name) {
        path = `/wetty/t/${instance.bastion_name}`;
      } else {
        path = instance.url.replace(/\/$/, "");
      }

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
    if (host && host.host_type !== "jump_host") {
      stopWeTTY(host.name).catch(console.error);
    }
    wettySocket.disconnect();
    setBasePath(null);
    setStatus("idle");
    setError(null);
    prevHostIdRef.current = null;
    clientTtyReportedRef.current = false;
  }, [host, wettySocket]);

  // ── 当 host 变化时自动启动 WeTTY ──
  useEffect(() => {
    if (!host) {
      if (basePath) {
        wettySocket.disconnect();
        setBasePath(null);
      }
      setStatus("idle");
      setError(null);
      prevHostIdRef.current = null;
      return;
    }

    if (host.id === prevHostIdRef.current) return;

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

      {/* 终端容器（使用 callback ref） */}
      <div
        ref={terminal.containerRef}
        className="flex-1 bg-[#0a0a0a] relative overflow-hidden"
        style={{
          minHeight: 0,
          display: isActive ? undefined : "none",
        }}
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
