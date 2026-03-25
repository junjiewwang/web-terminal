/**
 * useWettySocket — WeTTY socket.io 连接管理 Hook
 *
 * 职责：
 *  - 管理与 WeTTY server 的 socket.io 连接生命周期
 *  - 桥接 socket.io 事件与 xterm.js 终端（通过 TerminalHandle）
 *  - 自动重连 + 连接状态追踪
 *  - 冷启动期间静默重试（不显示错误）
 *
 * WeTTY socket.io 事件协议：
 *  Client → Server:
 *    - input(string)   : 用户键入的字符
 *    - resize({cols, rows}) : 终端尺寸变化
 *  Server → Client:
 *    - data(string)    : SSH 输出数据
 *    - login           : 登录成功
 *    - logout          : 登出
 *    - disconnect      : 连接断开
 *
 * 设计原则：
 *  - 只负责「网络连接」，不涉及终端 UI（由 useTerminal 管理）
 *  - 连接路径从 WeTTY basePath 动态构造：`${basePath}/socket.io`
 *  - 通过 onData 回调将 server 数据传递给终端
 *  - 冷启动静默重试：WeTTY 进程启动需要时间，期间连接失败不报错
 */

import { useRef, useEffect, useCallback, useState } from "react";
import { io, type Socket } from "socket.io-client";
import type { TermSize } from "./useTerminal";

/** 连接状态 */
export type SocketStatus = "disconnected" | "connecting" | "connected" | "error";

/** Hook 配置 */
export interface UseWettySocketOptions {
  /** WeTTY 实例的 basePath（如 /wetty/t/tce-server） */
  basePath: string | null;
  /** 收到服务端数据时的回调（server → terminal.write） */
  onData?: (data: string) => void;
  /** 连接成功回调 */
  onConnect?: () => void;
  /** 连接断开回调 */
  onDisconnect?: (reason?: string) => void;
}

/** Hook 返回值 */
export interface WettySocketHandle {
  /** 当前连接状态 */
  status: SocketStatus;
  /** 发送用户输入到 WeTTY server */
  sendInput: (data: string) => void;
  /** 发送终端尺寸变化到 WeTTY server */
  sendResize: (size: TermSize) => void;
  /** 主动断开连接 */
  disconnect: () => void;
}

/** 冷启动静默重试配置 */
const COLD_START_CONFIG = {
  /** 首次重连延迟（毫秒）- WeTTY 通常在 500ms 内就绪 */
  reconnectionDelay: 300,
  /** 最大重连延迟 */
  reconnectionDelayMax: 2000,
  /** 静默重试次数 - 超过此次数才显示错误 */
  silentRetryCount: 3,
  /** 最大重连次数 */
  reconnectionAttempts: 10,
};

/**
 * WeTTY socket.io 连接 Hook
 *
 * @param options - 连接配置
 * @returns WettySocketHandle 操作句柄
 */
export function useWettySocket(options: UseWettySocketOptions): WettySocketHandle {
  const { basePath, onData, onConnect, onDisconnect } = options;
  const [status, setStatus] = useState<SocketStatus>("disconnected");
  const socketRef = useRef<Socket | null>(null);
  const connectionAttemptsRef = useRef(0);

  // 用 ref 存储回调避免 effect 依赖变化导致重连
  const onDataRef = useRef(onData);
  const onConnectRef = useRef(onConnect);
  const onDisconnectRef = useRef(onDisconnect);
  onDataRef.current = onData;
  onConnectRef.current = onConnect;
  onDisconnectRef.current = onDisconnect;

  // ── 连接生命周期 ──────────────────────────────
  useEffect(() => {
    if (!basePath) {
      setStatus("disconnected");
      return;
    }

    setStatus("connecting");
    connectionAttemptsRef.current = 0;

    // socket.io 连接路径：basePath + /socket.io
    // 例如 basePath = /wetty/t/tce-server → path = /wetty/t/tce-server/socket.io
    const socketPath = `${basePath.replace(/\/$/, "")}/socket.io`;

    const socket = io(window.location.origin, {
      path: socketPath,
      // socket.io 默认先 HTTP 轮询再升级 WebSocket，
      // 这里允许默认行为以确保兼容性
      transports: ["polling", "websocket"],
      // 超时配置（对齐 WeTTY server 端）
      timeout: 10000,
      // 冷启动优化：快速重连
      reconnection: true,
      reconnectionAttempts: COLD_START_CONFIG.reconnectionAttempts,
      reconnectionDelay: COLD_START_CONFIG.reconnectionDelay,
      reconnectionDelayMax: COLD_START_CONFIG.reconnectionDelayMax,
    });

    socketRef.current = socket;

    // ── socket.io 事件监听 ──

    socket.on("connect", () => {
      connectionAttemptsRef.current = 0; // 重置计数
      setStatus("connected");
      onConnectRef.current?.();
    });

    socket.on("data", (data: string) => {
      onDataRef.current?.(data);
    });

    socket.on("login", () => {
      // WeTTY 登录成功，可以在这里做额外的 UI 反馈
      // 目前仅透传，终端数据流已通过 data 事件传递
    });

    socket.on("logout", () => {
      setStatus("disconnected");
      onDisconnectRef.current?.("logout");
    });

    socket.on("disconnect", (reason: string) => {
      // 非主动断开时才设置 disconnected
      if (reason !== "io client disconnect") {
        setStatus("disconnected");
        onDisconnectRef.current?.(reason);
      }
    });

    socket.on("connect_error", (err: Error) => {
      connectionAttemptsRef.current += 1;

      // 冷启动静默重试：前 N 次失败不显示错误
      if (connectionAttemptsRef.current <= COLD_START_CONFIG.silentRetryCount) {
        console.log(
          `[WeTTY] 终端启动中，自动重连... (${connectionAttemptsRef.current}/${COLD_START_CONFIG.silentRetryCount})`
        );
        // 保持 "connecting" 状态，让 socket.io 自动重试
        return;
      }

      // 超过静默重试次数，显示错误
      console.error("[WeTTY socket] 连接错误:", err.message);
      setStatus("error");
    });

    socket.on("reconnect_failed", () => {
      console.error("[WeTTY socket] 重连失败，已达最大重试次数");
      setStatus("error");
    });

    // ── 清理 ──
    return () => {
      socket.removeAllListeners();
      socket.disconnect();
      socketRef.current = null;
      setStatus("disconnected");
    };
  }, [basePath]);

  // ── 操作句柄（稳定引用） ──────────────────────
  const sendInput = useCallback((data: string) => {
    socketRef.current?.emit("input", data);
  }, []);

  const sendResize = useCallback((size: TermSize) => {
    socketRef.current?.emit("resize", size);
  }, []);

  const disconnect = useCallback(() => {
    socketRef.current?.disconnect();
    setStatus("disconnected");
  }, []);

  return { status, sendInput, sendResize, disconnect };
}
