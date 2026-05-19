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
 *    {"type": "session_exit", "reason": "normal|ssh_failed|...", "exit_code": 0}
 *    {"type": "resize_hint", "effective_cols": 80, "effective_rows": 24}
 *    {"type": "clipboard", "text": "..."}
 *
 * 设计原则：
 *  - 接口与 useWettySocket 保持一致（SocketStatus / sendInput / sendResize / disconnect）
 *  - 自动重连 + 冷启动静默重试
 *  - 只负责「网络连接」，不涉及终端 UI
 */

import { useRef, useEffect, useCallback, useState } from "react";
import type { TermSize } from "./useTerminal";
import { getAccessToken } from "../services/auth";

/** 连接状态 */
export type SocketStatus = "disconnected" | "connecting" | "connected" | "error";

/** resize_hint 消息载荷 */
export interface ResizeHint {
  effective_cols: number;
  effective_rows: number;
}

/** 会话退出信息（增强的断连通知） */
export interface SessionExitInfo {
  /** 退出原因分类 */
  reason: string;
  /** 进程退出码 */
  exitCode?: number | null;
  /** 人类友好的断连消息（如 "远程主机强制断开了连接"） */
  message?: string;
  /** 是否可重连 */
  recoverable: boolean;
  /** 主机/实例名称 */
  hostName?: string;
}

/** Hook 配置 */
export interface UseWebSocketOptions {
  /** WebSocket URL（如 /ws/terminal/{session_id}），null 时不连接 */
  wsUrl: string | null;
  /** 收到终端输出数据的回调 */
  onData?: (data: string) => void;
  /** 收到 scrollback 历史回放数据的回调（前端应临时屏蔽用户输入发送，防止 xterm.js 查询响应乱码） */
  onHistory?: (data: string) => void;
  /** 连接成功回调 */
  onConnect?: () => void;
  /** 连接断开回调 */
  onDisconnect?: (reason?: string) => void;
  /** 会话退出回调（含增强断连信息） */
  onSessionExit?: (info: SessionExitInfo) => void;
  /** 收到 tmux clipboard 推送的回调 */
  onClipboard?: (text: string) => void;
  /** 收到 resize_hint 消息的回调（Broker 模式 min-size 策略通知有效尺寸） */
  onResizeHint?: (hint: ResizeHint) => void;
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

function _decodeBase64Utf8(base64Text: string): string {
  const binary = atob(base64Text);
  const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
  return new TextDecoder("utf-8").decode(bytes);
}

/**
 * 原生 WebSocket 终端连接 Hook
 */
export function useWebSocket(options: UseWebSocketOptions): WebSocketHandle {
  const { wsUrl, onData, onHistory, onConnect, onDisconnect, onSessionExit, onClipboard, onResizeHint } = options;
  const [status, setStatus] = useState<SocketStatus>("disconnected");
  const wsRef = useRef<WebSocket | null>(null);
  const attemptsRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const destroyedRef = useRef(false);

  // 用 ref 存储回调避免 effect 依赖变化导致重连
  const onDataRef = useRef(onData);
  const onHistoryRef = useRef(onHistory);
  const onConnectRef = useRef(onConnect);
  const onDisconnectRef = useRef(onDisconnect);
  const onSessionExitRef = useRef(onSessionExit);
  const onClipboardRef = useRef(onClipboard);
  const onResizeHintRef = useRef(onResizeHint);
  onDataRef.current = onData;
  onHistoryRef.current = onHistory;
  onConnectRef.current = onConnect;
  onDisconnectRef.current = onDisconnect;
  onSessionExitRef.current = onSessionExit;
  onClipboardRef.current = onClipboard;
  onResizeHintRef.current = onResizeHint;

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

      // 构造完整 WebSocket URL（含认证 Token）
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const token = getAccessToken();
      const tokenParam = token ? `?token=${encodeURIComponent(token)}` : "";
      const fullUrl = `${protocol}//${window.location.host}${wsUrl}${tokenParam}`;

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
            // 添加调试日志：检查是否包含 OSC 52
            if (msg.data.includes('\x1B]52;;')) {
              console.log('[WebSocket] 收到包含 OSC 52 的消息');
            }
            
            // 检查是否包含 OSC 52 剪贴板序列
            // 格式: \x1B]52;;<base64-encoded-text>\x07
            const osc52Regex = /\x1B\]52;;([A-Za-z0-9+/=]+)\x07/g;
            let match;
            let processedData = msg.data;
            
            while ((match = osc52Regex.exec(msg.data)) !== null) {
              const base64Text = match[1];
              try {
                const text = _decodeBase64Utf8(base64Text);
                console.log('[WebSocket] 解析到 OSC 52 剪贴板内容:', text);
                onClipboardRef.current?.(text);

                // 从数据中移除 OSC 52 序列，避免 xterm.js 处理可能导致的问题
                processedData = processedData.replace(match[0], '');
              } catch (e) {
                console.error('[WebSocket] 解码 OSC 52 失败:', e);
              }
            }
            
            // 将处理后的数据传递给终端显示（OSC 52 序列已被移除）
            if (processedData) {
              onDataRef.current?.(processedData);
            }
          } else if (msg.type === "history" && msg.data) {
            // Scrollback 历史回放：使用专用回调，前端在回放期间屏蔽用户输入发送
            // 防止 xterm.js 对回放中的终端查询序列生成响应并发送到 PTY 导致乱码
            onHistoryRef.current?.(msg.data);
          } else if (msg.type === "closed") {
            setStatus("disconnected");
            onDisconnectRef.current?.(msg.reason || "closed");
          } else if (msg.type === "session_exit") {
            // Broker 模式会话退出通知（增强：含断连友好信息）
            const reason = msg.reason || "session_exit";
            const exitInfo: SessionExitInfo = {
              reason,
              exitCode: msg.exit_code ?? null,
              message: msg.message,
              recoverable: msg.recoverable ?? true,
              hostName: msg.host_name,
            };
            console.log(`[WebSocket] 会话退出: reason=${reason}, message=${exitInfo.message || "N/A"}, recoverable=${exitInfo.recoverable}`);
            setStatus("disconnected");
            onSessionExitRef.current?.(exitInfo);
            onDisconnectRef.current?.(reason);
          } else if (msg.type === "resize_hint") {
            // Broker 模式 min-size 策略有效尺寸通知
            onResizeHintRef.current?.({
              effective_cols: msg.effective_cols,
              effective_rows: msg.effective_rows,
            });
          } else if (msg.type === "clipboard" && msg.text) {
            // 兼容后端 API 推送的 clipboard 消息
            onClipboardRef.current?.(msg.text);
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
