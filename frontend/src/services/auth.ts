/**
 * 认证 & Token 管理服务
 *
 * 职责：
 * - localStorage 持久化 access_token / refresh_token / 租户信息
 * - JWT 过期检测（客户端 decode payload.exp）
 * - 自动静默刷新（access_token 过期时用 refresh_token 换新）
 * - 登录 / 注销 API 调用
 * - 401 时清理 Token 并通知上层跳转登录页
 *
 * 设计原则：
 * - 所有 Token 操作集中在此模块，其他模块只调用导出函数
 * - refresh 请求串行化（避免多个 401 同时触发多次 refresh）
 * - 模块级事件总线通知认证状态变更
 */

// ── 存储 Key ──────────────────────────────────

const KEY_ACCESS_TOKEN = "wetty_access_token";
const KEY_REFRESH_TOKEN = "wetty_refresh_token";
const KEY_TENANT_INFO = "wetty_tenant_info";

// ── 类型定义 ──────────────────────────────────

/** 租户信息（前端展示用） */
export interface TenantInfo {
  tenant_id: string;
  name: string;
  role: string;
}

/** 登录响应 */
export interface LoginResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  tenant_id: string;
  name: string;
  role: string;
  expires_at: string;
}

/** 刷新响应 */
interface RefreshResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_at: string;
}

/** 认证状态 */
export interface AuthState {
  /** 是否已登录（有有效 Token） */
  isAuthenticated: boolean;
  /** 当前租户信息（未登录时为 null） */
  tenant: TenantInfo | null;
}

/** 后端认证状态响应 */
export interface AuthStatus {
  /** 是否需要登录（后端启用了认证） */
  auth_required: boolean;
}

// ── 认证状态变更监听器 ──────────────────────────

type AuthChangeListener = (state: AuthState) => void;
const _listeners: Set<AuthChangeListener> = new Set();

/** 注册认证状态变更监听器 */
export function onAuthChange(listener: AuthChangeListener): () => void {
  _listeners.add(listener);
  return () => _listeners.delete(listener);
}

function _notifyAuthChange(): void {
  const state = getAuthState();
  for (const listener of _listeners) {
    try {
      listener(state);
    } catch {
      // 监听器异常不影响其他监听器
    }
  }
}

// ── Token 存储 ──────────────────────────────────

export function getAccessToken(): string | null {
  return localStorage.getItem(KEY_ACCESS_TOKEN);
}

export function getRefreshToken(): string | null {
  return localStorage.getItem(KEY_REFRESH_TOKEN);
}

export function getTenantInfo(): TenantInfo | null {
  try {
    const raw = localStorage.getItem(KEY_TENANT_INFO);
    return raw ? (JSON.parse(raw) as TenantInfo) : null;
  } catch {
    return null;
  }
}

function _saveTokens(
  accessToken: string,
  refreshToken: string,
  tenant?: TenantInfo,
): void {
  localStorage.setItem(KEY_ACCESS_TOKEN, accessToken);
  localStorage.setItem(KEY_REFRESH_TOKEN, refreshToken);
  if (tenant) {
    localStorage.setItem(KEY_TENANT_INFO, JSON.stringify(tenant));
  }
  _notifyAuthChange();
}

export function clearAuth(): void {
  localStorage.removeItem(KEY_ACCESS_TOKEN);
  localStorage.removeItem(KEY_REFRESH_TOKEN);
  localStorage.removeItem(KEY_TENANT_INFO);
  _notifyAuthChange();
}

// ── JWT 过期检测（客户端 decode） ──────────────

/**
 * 解析 JWT payload（不验证签名，仅读取 exp）
 */
function _decodeJwtPayload(token: string): Record<string, unknown> | null {
  try {
    const parts = token.split(".");
    if (parts.length !== 3) return null;
    const payload = atob(parts[1].replace(/-/g, "+").replace(/_/g, "/"));
    return JSON.parse(payload);
  } catch {
    return null;
  }
}

/**
 * 判断 access_token 是否已过期（或即将过期）
 *
 * @param bufferSeconds 提前多少秒视为过期（默认 60s），
 *                      用于在到期前主动刷新避免请求失败
 */
export function isAccessTokenExpired(bufferSeconds = 60): boolean {
  const token = getAccessToken();
  if (!token) return true;

  const payload = _decodeJwtPayload(token);
  if (!payload || typeof payload.exp !== "number") return true;

  return Date.now() / 1000 > payload.exp - bufferSeconds;
}

// ── 获取当前认证状态 ──────────────────────────

export function getAuthState(): AuthState {
  const token = getAccessToken();
  const tenant = getTenantInfo();

  if (!token || !tenant) {
    return { isAuthenticated: false, tenant: null };
  }

  return { isAuthenticated: true, tenant };
}

// ── 登录 API ──────────────────────────────────

export async function login(
  tenantId: string,
  password: string,
): Promise<LoginResponse> {
  const res = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tenant_id: tenantId, password }),
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || "登录失败");
  }

  const data: LoginResponse = await res.json();

  _saveTokens(data.access_token, data.refresh_token, {
    tenant_id: data.tenant_id,
    name: data.name,
    role: data.role,
  });

  return data;
}

// ── 注销 API ──────────────────────────────────

export async function logout(): Promise<void> {
  const refreshToken = getRefreshToken();

  // 先清除本地存储（即使后端调用失败也确保登出）
  clearAuth();

  if (refreshToken) {
    try {
      await fetch("/api/auth/logout", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refreshToken }),
      });
    } catch {
      // 注销是幂等操作，后端调用失败不阻塞
    }
  }
}

// ── Token 刷新（串行化） ──────────────────────

/**
 * 全局 refresh 锁：避免多个 401 并发触发多次 refresh。
 * 后续请求排队等待第一次 refresh 完成后复用结果。
 */
let _refreshPromise: Promise<boolean> | null = null;

/**
 * 静默刷新 access_token
 *
 * @returns true = 刷新成功，false = 需要重新登录
 */
export async function refreshAccessToken(): Promise<boolean> {
  // 串行化：如果已有 refresh 正在进行，等待其结果
  if (_refreshPromise) {
    return _refreshPromise;
  }

  _refreshPromise = _doRefresh();
  try {
    return await _refreshPromise;
  } finally {
    _refreshPromise = null;
  }
}

async function _doRefresh(): Promise<boolean> {
  const refreshToken = getRefreshToken();
  if (!refreshToken) return false;

  try {
    const res = await fetch("/api/auth/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });

    if (!res.ok) {
      // refresh_token 也过期了，需要重新登录
      clearAuth();
      return false;
    }

    const data: RefreshResponse = await res.json();

    // 保留现有租户信息，只更新 token
    const currentTenant = getTenantInfo();
    _saveTokens(data.access_token, data.refresh_token, currentTenant ?? undefined);

    return true;
  } catch {
    // 网络错误不清除 Token（可能只是暂时性故障）
    return false;
  }
}

// ── 后端认证状态检测 ──────────────────────────

/**
 * 检查后端是否启用了认证（用于决定是否显示登录页）
 *
 * 开发模式下（无 WETTY_API_TOKEN 且无 tenants.yaml），
 * 后端会返回 auth_required: false，前端跳过登录页。
 */
export async function checkAuthStatus(): Promise<AuthStatus> {
  try {
    const res = await fetch("/api/auth/status");
    if (!res.ok) {
      // 如果端点不存在（404），说明是旧版后端，默认不需要认证
      return { auth_required: false };
    }
    return await res.json();
  } catch {
    // 网络错误时保守处理：不需要认证
    return { auth_required: false };
  }
}
