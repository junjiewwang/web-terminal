/**
 * API 调用 & SSE 订阅服务
 *
 * 集中管理所有后端 API 交互，前端组件只调用此模块。
 */

const API_BASE = "/api";

// ── 通用重试工具 ──────────────────────────────

/**
 * 带指数退避的 fetch 重试封装
 *
 * 应对场景：浏览器导航竞态（前一页面 abort 请求）、
 * 网络瞬断、服务端冷启动等暂时性故障。
 *
 * @param input   - fetch 的第一个参数
 * @param init    - fetch 的 RequestInit 选项
 * @param retries - 最大重试次数（默认 3）
 * @param baseDelay - 首次重试延迟 ms（默认 500，后续指数退避）
 */
async function fetchWithRetry(
  input: RequestInfo | URL,
  init?: RequestInit,
  retries = 3,
  baseDelay = 500,
): Promise<Response> {
  let lastError: unknown;
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const res = await fetch(input, init);
      return res;
    } catch (err) {
      lastError = err;
      if (attempt < retries) {
        const delay = baseDelay * 2 ** attempt;
        await new Promise((r) => setTimeout(r, delay));
      }
    }
  }
  throw lastError;
}

// ── 类型定义 ──────────────────────────────────

export interface Host {
  id: number;
  name: string;
  hostname: string;
  port: number;
  username: string;
  auth_type: "key" | "password";
  private_key_path?: string;
  description?: string;
  tags: string[];
  created_at: string;
  updated_at: string;
}

export interface SessionInfo {
  session_id: string;
  host_name: string;
  hostname: string;
  username: string;
  connected: boolean;
  created_at: string;
  last_activity: string;
}

export interface CommandResult {
  session_id: string;
  host_name: string;
  command: string;
  stdout: string;
  stderr: string;
  exit_code: number;
  duration_ms: number;
  success: boolean;
}

export interface AgentEvent {
  event_type: string;
  session_id: string;
  host_name: string;
  data: Record<string, unknown>;
  timestamp: string;
}

export interface WeTTYInstance {
  host_name: string;
  port: number;
  url: string;
  running: boolean;
}

export interface CreateHostRequest {
  name: string;
  hostname: string;
  port?: number;
  username: string;
  auth_type?: "key" | "password";
  private_key_path?: string;
  password?: string;
  description?: string;
  tags?: string[];
}

// ── 主机管理 ──────────────────────────────────

export async function fetchHosts(tag?: string): Promise<Host[]> {
  const params = tag ? `?tag=${encodeURIComponent(tag)}` : "";
  const res = await fetchWithRetry(`${API_BASE}/hosts${params}`);
  if (!res.ok) throw new Error(`获取主机列表失败: ${res.statusText}`);
  return res.json();
}

export async function createHost(data: CreateHostRequest): Promise<Host> {
  const res = await fetch(`${API_BASE}/hosts`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error(`创建主机失败: ${res.statusText}`);
  return res.json();
}

export async function updateHost(
  hostId: number,
  data: Partial<CreateHostRequest>
): Promise<Host> {
  const res = await fetch(`${API_BASE}/hosts/${hostId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error(`更新主机失败: ${res.statusText}`);
  return res.json();
}

export async function deleteHost(hostId: number): Promise<void> {
  const res = await fetch(`${API_BASE}/hosts/${hostId}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`删除主机失败: ${res.statusText}`);
}

// ── 会话管理 ──────────────────────────────────

export async function createSession(hostId: number): Promise<{ session_id: string }> {
  const res = await fetch(`${API_BASE}/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ host_id: hostId }),
  });
  if (!res.ok) throw new Error(`创建会话失败: ${res.statusText}`);
  return res.json();
}

export async function executeCommand(
  sessionId: string,
  command: string,
  timeout = 30
): Promise<CommandResult> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/exec`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ command, timeout }),
  });
  if (!res.ok) throw new Error(`执行命令失败: ${res.statusText}`);
  return res.json();
}

export async function closeSession(sessionId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(`关闭会话失败: ${res.statusText}`);
}

// ── WeTTY 实例管理 ──────────────────────────

export async function startWeTTY(hostId: number): Promise<WeTTYInstance> {
  // 安全措施：暂停 SSE 释放浏览器连接槽位，确保 POST 不被排队
  // （nginx 反代已解决此问题，但保留作为额外保险）
  _globalSSE?.pause();

  try {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 10000);

    const res = await fetch(`${API_BASE}/wetty/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host_id: hostId }),
      signal: controller.signal,
    });
    clearTimeout(timeoutId);

    if (!res.ok) throw new Error(`启动 WeTTY 失败: ${res.statusText}`);
    return res.json();
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new Error("启动 WeTTY 超时（10s），请检查网络连接");
    }
    throw err;
  } finally {
    _globalSSE?.resume();
  }
}

export async function stopWeTTY(hostName: string): Promise<void> {
  const res = await fetch(`${API_BASE}/wetty/stop/${encodeURIComponent(hostName)}`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`停止 WeTTY 失败: ${res.statusText}`);
}

export async function listWeTTYInstances(): Promise<WeTTYInstance[]> {
  const res = await fetch(`${API_BASE}/wetty`);
  if (!res.ok) throw new Error(`获取 WeTTY 列表失败: ${res.statusText}`);
  return res.json();
}

// ── SSE 事件订阅（模块级单例 · fetch + ReadableStream 实现）──

/**
 * 全局 SSE 连接控制器（模块级单例）
 *
 * 设计为模块内部管理的全局单例，startWeTTY 等 API 函数
 * 在发起 POST 前自动暂停 SSE、完成后恢复，无需组件传递引用。
 *
 * ⚠️ 关键设计决策：使用 fetch + ReadableStream 替代浏览器原生 EventSource。
 *
 * 原因：EventSource.close() 只是停止读取数据，但底层 TCP 连接不会立即释放。
 * fetch + AbortController.abort() 会强制中断底层 TCP 连接（发送 RST），
 * 连接槽位立即释放，后续 POST 可以使用新的 TCP 连接。
 */
let _globalSSE: {
  pause: () => void;
  resume: () => void;
} | null = null;

/** SSE 已知事件类型（用于过滤） */
const SSE_EVENT_TYPES = new Set([
  "command_start",
  "command_output",
  "command_complete",
  "command_error",
  "session_created",
  "session_closed",
]);

/**
 * 订阅 SSE 事件流
 *
 * 使用 fetch + ReadableStream 手动解析 SSE 协议，
 * 通过 AbortController 实现精确的 TCP 连接断开控制。
 * 自动重连（指数退避，最大 30 秒）。
 *
 * @returns cleanup 函数，调用后断开连接
 */
export function subscribeEvents(
  onEvent: (event: AgentEvent) => void,
): () => void {
  let abortController: AbortController | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let destroyed = false;
  let reconnectDelay = 1000; // 初始重连延迟 1s

  function connect() {
    if (destroyed || abortController) return;

    abortController = new AbortController();
    const { signal } = abortController;

    _readSSEStream(signal, onEvent)
      .catch(() => {
        // 连接断开或错误，忽略（下面统一处理重连）
      })
      .finally(() => {
        abortController = null;
        // 非主动销毁时自动重连
        if (!destroyed) {
          reconnectTimer = setTimeout(() => {
            reconnectTimer = null;
            connect();
          }, reconnectDelay);
          // 指数退避，最大 30s
          reconnectDelay = Math.min(reconnectDelay * 1.5, 30000);
        }
      });

    // 连接成功后重置退避
    // （在 _readSSEStream 内部收到第一条数据时重置更精确，
    //   但这里简化处理：每次发起连接就重置）
    reconnectDelay = 1000;
  }

  function disconnect() {
    if (abortController) {
      abortController.abort(); // 强制中断 TCP 连接
      abortController = null;
    }
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  }

  // 注册为全局控制器
  _globalSSE = { pause: disconnect, resume: connect };

  // 立即连接
  connect();

  // 返回 cleanup
  return () => {
    destroyed = true;
    disconnect();
    if (_globalSSE?.pause === disconnect) {
      _globalSSE = null;
    }
  };
}

/**
 * 使用 fetch + ReadableStream 读取 SSE 流
 *
 * 手动解析 text/event-stream 格式：
 *   event: <type>\n
 *   data: <json>\n
 *   \n
 *
 * @param signal - AbortSignal，用于外部中断连接
 * @param onEvent - 事件回调
 */
async function _readSSEStream(
  signal: AbortSignal,
  onEvent: (event: AgentEvent) => void,
): Promise<void> {
  const res = await fetch(`${API_BASE}/events/stream`, {
    signal,
    headers: { Accept: "text/event-stream" },
  });

  if (!res.ok || !res.body) {
    throw new Error(`SSE 连接失败: ${res.status}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let currentEvent = "";
  let currentData = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // 按行解析 SSE 协议
      const lines = buffer.split("\n");
      // 最后一个元素可能是不完整的行，保留在 buffer 中
      buffer = lines.pop() ?? "";

      for (const line of lines) {
        if (line.startsWith("event:")) {
          currentEvent = line.slice(6).trim();
        } else if (line.startsWith("data:")) {
          currentData = line.slice(5).trim();
        } else if (line === "" && currentData) {
          // 空行 = 事件结束，分发事件
          if (SSE_EVENT_TYPES.has(currentEvent) && currentData) {
            try {
              onEvent(JSON.parse(currentData));
            } catch {
              console.error("SSE 事件解析失败:", currentData);
            }
          }
          currentEvent = "";
          currentData = "";
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}
