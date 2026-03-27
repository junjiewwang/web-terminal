/**
 * useWebSocket — 原生 WebSocket 终端连接 Hook
 *
 * 替代 useWettySocket（socket.io），使用浏览器原生 WebSocket 直连后端 PTY。
 *
 * 协议（JSON 消息）：
 *  Client → Server:
 *    {"type": "input", "data": "ls\r"}
 *    {"type": "resize", "cols": 80, "rows": 24}
 *  Server → Client:
 *    {"type": "output", "data": "..."}
 *    {"type": "closed", "reason": "..."}
 *
 * 设计原则：
 *  - 接口与 useWettySocket 保持一致（SocketStatus / sendInput / sendResize / disconnect）
 *  - 自动重连 + 冷启动静默重试
 *  - 只负责「网络连接」，不涉及终端 UI
 */

import { useRef, useEffect, useCallback, useState } from "react";
import type { TermSize } from "./useTerminal";

/** 连接状态 */
export type SocketStatus = "disconnected" | "connecting" | "connected" | "error";

/** Hook 配置 */
export interface UseWebSocketOptions {
  /** WebSocket URL（如 /ws/terminal/{session_id}），null 时不连接 */
  wsUrl: string | null;
  /** 收到终端输出数据的回调 */
  onData?: (data: string) => void;
  /** 连接成功回调 */
  onConnect?: () => void;
  /** 连接断开回调 */
  onDisconnect?: (reason?: string) => void;
}

/** Hook 返回值（与 useWettySocket 兼容） */
export interface WebSocketHandle {
  status: SocketStatus;
  sendInput: (data: string) => void;
  sendResize: (size: TermSize) => void;
  disconnect: () => void;
}

/** 重连配置 */
const RECONNECT_CONFIG = {
  /** 初始重连延迟 ms */
  initialDelay: 500,
  /** 最大重连延迟 ms */
  maxDelay: 5000,
  /** 静默重试次数（前 N 次失败不报错） */
  silentRetryCount: 3,
  /** 最大重连次数 */
  maxAttempts: 10,
};

/**
 * 原生 WebSocket 终端连接 Hook
 */
export function useWebSocket(options: UseWebSocketOptions): WebSocketHandle {
  const { wsUrl, onData, onConnect, onDisconnect } = options;
  const [status, setStatus] = useState<SocketStatus>("disconnected");
  const wsRef = useRef<WebSocket | null>(null);
  const attemptsRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const destroyedRef = useRef(false);

  // 用 ref 存储回调避免 effect 依赖变化导致重连
  const onDataRef = useRef(onData);
  const onConnectRef = useRef(onConnect);
  const onDisconnectRef = useRef(onDisconnect);
  onDataRef.current = onData;
  onConnectRef.current = onConnect;
  onDisconnectRef.current = onDisconnect;

  // ── 连接生命周期 ──────────────────────────────
  useEffect(() => {
    if (!wsUrl) {
      setStatus("disconnected");
      return;
    }

    destroyedRef.current = false;
    attemptsRef.current = 0;

    function connect() {
      if (destroyedRef.current) return;

      setStatus("connecting");

      // 构造完整 WebSocket URL
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const fullUrl = `${protocol}//${window.location.host}${wsUrl}`;

      const ws = new WebSocket(fullUrl);
      wsRef.current = ws;

      ws.onopen = () => {
        attemptsRef.current = 0;
        setStatus("connected");
        onConnectRef.current?.();
      };

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === "output" && msg.data) {
            onDataRef.current?.(msg.data);
          } else if (msg.type === "closed") {
            setStatus("disconnected");
            onDisconnectRef.current?.(msg.reason || "closed");
          }
        } catch {
          // 非 JSON 消息，当作原始终端输出
          onDataRef.current?.(event.data);
        }
      };

      ws.onclose = (event) => {
        wsRef.current = null;
        if (destroyedRef.current) return;

        // 非主动关闭，尝试重连
        if (event.code !== 1000) {
          attemptsRef.current += 1;

          if (attemptsRef.current <= RECONNECT_CONFIG.silentRetryCount) {
            // 静默重试
            console.log(
              `[WS] 终端连接中... (${attemptsRef.current}/${RECONNECT_CONFIG.silentRetryCount})`
            );
          } else if (attemptsRef.current > RECONNECT_CONFIG.maxAttempts) {
            setStatus("error");
            onDisconnectRef.current?.("max_attempts");
            return;
          } else {
            setStatus("error");
          }

          const delay = Math.min(
            RECONNECT_CONFIG.initialDelay * Math.pow(1.5, attemptsRef.current - 1),
            RECONNECT_CONFIG.maxDelay
          );
          reconnectTimerRef.current = setTimeout(connect, delay);
        } else {
          setStatus("disconnected");
          onDisconnectRef.current?.(event.reason || "normal");
        }
      };

      ws.onerror = () => {
        // error 事件后通常会触发 close 事件，由 onclose 处理重连
      };
    }

    connect();

    // ── 清理 ──
    return () => {
      destroyedRef.current = true;
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      if (wsRef.current) {
        wsRef.current.onclose = null; // 防止触发重连
        wsRef.current.close(1000, "cleanup");
        wsRef.current = null;
      }
      setStatus("disconnected");
    };
  }, [wsUrl]);

  // ── 操作句柄（稳定引用） ──────────────────────
  const sendInput = useCallback((data: string) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "input", data }));
    }
  }, []);

  const sendResize = useCallback((size: TermSize) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "resize", cols: size.cols, rows: size.rows }));
    }
  }, []);

  const disconnect = useCallback(() => {
    destroyedRef.current = true;
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    if (wsRef.current) {
      wsRef.current.close(1000, "user_disconnect");
      wsRef.current = null;
    }
    setStatus("disconnected");
  }, []);

  return { status, sendInput, sendResize, disconnect };
}
