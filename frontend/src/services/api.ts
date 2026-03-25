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

/** 主机类型 */
export type HostType = "direct" | "bastion" | "jump_host";

/** 登录交互步骤（堡垒机跳转时的 wait→send 原子操作） */
export interface LoginStep {
  wait: string;
  send: string;
  timeout: number;
}

/** 堡垒机跳板配置 */
export interface JumpHostConfig {
  ready_pattern: string;
  login_success_pattern: string;
}

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

  // ── 跳板主机字段 ──
  host_type: HostType;
  parent_id?: number | null;
  target_ip?: string | null;
  jump_config?: JumpHostConfig | null;
  login_steps?: LoginStep[] | null;
  /** 二级主机列表（仅 bastion 类型） */
  children: Host[];

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
  /** 堡垒机名称（仅 jump_host 时由后端返回） */
  bastion_name?: string | null;
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

  // ── 跳板主机字段 ──
  host_type?: HostType;
  parent_id?: number;
  target_ip?: string;
  jump_config?: JumpHostConfig;
  login_steps?: LoginStep[];
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

// ── tmux 窗口管理 ──────────────────────────

/** tmux 客户端信息 */
export interface TmuxClient {
  tty: string;      // 客户端 TTY（如 /dev/pts/3）
  window: string;   // 当前窗口名
  session: string;  // 会话名
}

/**
 * 获取堡垒机 tmux 会话的所有客户端信息
 *
 * 前端 socket.io 连接建立后调用，获取当前所有 client。
 * 返回每个客户端的 TTY、当前窗口和会话信息，前端可以据此识别自己的 client。
 *
 * @param bastionName - 堡垒机名称
 */
export async function fetchClientTtys(bastionName: string): Promise<TmuxClient[]> {
  const res = await fetch(`${API_BASE}/tmux/client-ttys/${encodeURIComponent(bastionName)}`);
  if (!res.ok) throw new Error(`获取 tmux client 列表失败: ${res.statusText}`);
  const data = await res.json();
  return data.clients ?? [];
}

/**
 * 切换 tmux 窗口（per-client 模式）
 *
 * 支持两种模式：
 * - 提供 clientTty: 只切换该 client 的视图（多 Tab 独立视图）
 * - 不提供 clientTty: 全局切换所有 client（向后兼容）
 *
 * @param bastionName - 堡垒机名称
 * @param windowName - 目标窗口名（如二级主机名 m12、m15）
 * @param clientTty - 可选，指定 tmux client TTY
 */
export async function switchTmuxWindow(
  bastionName: string,
  windowName: string,
  clientTty?: string,
): Promise<void> {
  const body: Record<string, string> = {
    bastion_name: bastionName,
    window_name: windowName,
  };
  if (clientTty) {
    body.client_tty = clientTty;
  }

  const res = await fetch(`${API_BASE}/tmux/switch-window`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => res.statusText);
    throw new Error(`切换 tmux 窗口失败: ${detail}`);
  }
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

/** SSE 已知业务事件类型（用于过滤） */
const SSE_EVENT_TYPES = new Set([
  "command_start",
  "command_output",
  "command_complete",
  "command_error",
  "session_created",
  "session_closed",
  "session_error",
  "window_switched",
]);

/**
 * 获取历史事件（最近 100 条）
 *
 * 用于前端初始化时补充加载：SSE 只能推送连接之后的事件，
 * 页面刷新或首次加载时需要从后端拉取已有的历史事件。
 */
export async function fetchEventHistory(): Promise<AgentEvent[]> {
  try {
    const res = await fetchWithRetry(`${API_BASE}/events/history`);
    if (!res.ok) return [];
    const data = await res.json();
    // 后端返回的 event_type 是枚举值字符串，直接兼容前端
    return Array.isArray(data) ? data : [];
  } catch {
    console.warn("加载历史事件失败，将依赖 SSE 实时推送");
    return [];
  }
}

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
  let paused = false; // 标记是否被 pause() 暂停（区分主动暂停与连接断开）
  let reconnectDelay = 1000; // 初始重连延迟 1s

  function connect() {
    if (destroyed || paused || abortController) return;

    abortController = new AbortController();
    const { signal } = abortController;

    _readSSEStream(signal, onEvent, () => {
      // onFirstData 回调：收到第一条数据时重置退避延迟
      reconnectDelay = 1000;
    })
      .catch(() => {
        // 连接断开或错误，忽略（下面统一处理重连）
      })
      .finally(() => {
        abortController = null;
        // 非主动销毁、非暂停时自动重连
        if (!destroyed && !paused) {
          reconnectTimer = setTimeout(() => {
            reconnectTimer = null;
            connect();
          }, reconnectDelay);
          // 指数退避，最大 30s
          reconnectDelay = Math.min(reconnectDelay * 1.5, 30000);
        }
      });
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

  function pause() {
    paused = true;
    disconnect();
  }

  function resume() {
    paused = false;
    // 恢复时立即重连，重置退避延迟
    reconnectDelay = 1000;
    connect();
  }

  // 注册为全局控制器
  _globalSSE = { pause, resume };

  // 立即连接
  connect();

  // 返回 cleanup
  return () => {
    destroyed = true;
    disconnect();
    if (_globalSSE?.pause === pause) {
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
 * @param onFirstData - 收到第一条有效数据时的回调（用于重置退避）
 */
async function _readSSEStream(
  signal: AbortSignal,
  onEvent: (event: AgentEvent) => void,
  onFirstData?: () => void,
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
  let firstDataFired = false;

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      // 收到任何数据即视为连接成功，触发一次 onFirstData
      if (!firstDataFired) {
        firstDataFired = true;
        onFirstData?.();
      }

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
        } else if (line.startsWith(":")) {
          // SSE 注释行（包括 sse_starlette 的 ping 心跳 ": ping"）
          // 忽略，但这些行说明连接是活跃的
          continue;
        } else if (line === "" && currentData) {
          // 空行 = 事件结束，分发业务事件
          if (SSE_EVENT_TYPES.has(currentEvent)) {
            try {
              onEvent(JSON.parse(currentData));
            } catch {
              console.error("SSE 事件解析失败:", currentData);
            }
          }
          // ping 等非业务事件默默消化，不分发
          currentEvent = "";
          currentData = "";
        } else if (line === "") {
          // 空行但没有 data（如 ping 后的空行），重置解析状态
          currentEvent = "";
          currentData = "";
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}
