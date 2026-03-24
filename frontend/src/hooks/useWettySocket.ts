/**
 * useWettySocket — WeTTY socket.io 连接管理 Hook
 *
 * 职责：
 *  - 管理与 WeTTY server 的 socket.io 连接生命周期
 *  - 桥接 socket.io 事件与 xterm.js 终端（通过 TerminalHandle）
 *  - 自动重连 + 连接状态追踪
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
      // 自动重连
      reconnection: true,
      reconnectionAttempts: 10,
      reconnectionDelay: 1000,
      reconnectionDelayMax: 5000,
    });

    socketRef.current = socket;

    // ── socket.io 事件监听 ──

    socket.on("connect", () => {
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
      setStatus("disconnected");
      onDisconnectRef.current?.(reason);
    });

    socket.on("connect_error", (err: Error) => {
      console.error("[WeTTY socket] 连接错误:", err.message);
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
