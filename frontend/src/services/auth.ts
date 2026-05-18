/**
 * 认证 & Token 管理服务
 *
 * 职责：
 * - localStorage 持久化 access_token / refresh_token
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

// ── 类型定义 ──────────────────────────────────

/** 登录响应 */
export interface LoginResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
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

function _saveTokens(accessToken: string, refreshToken: string): void {
  localStorage.setItem(KEY_ACCESS_TOKEN, accessToken);
  localStorage.setItem(KEY_REFRESH_TOKEN, refreshToken);
  _notifyAuthChange();
}

export function clearAuth(): void {
  localStorage.removeItem(KEY_ACCESS_TOKEN);
  localStorage.removeItem(KEY_REFRESH_TOKEN);
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
  return { isAuthenticated: !!token };
}

// ── 登录 API ──────────────────────────────────

export async function login(password: string): Promise<LoginResponse> {
  const res = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || "登录失败");
  }

  const data: LoginResponse = await res.json();
  _saveTokens(data.access_token, data.refresh_token);
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

let _refreshPromise: Promise<boolean> | null = null;

/**
 * 静默刷新 access_token
 *
 * @returns true = 刷新成功，false = 需要重新登录
 */
export async function refreshAccessToken(): Promise<boolean> {
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
      clearAuth();
      return false;
    }

    const data: RefreshResponse = await res.json();
    _saveTokens(data.access_token, data.refresh_token);
    return true;
  } catch {
    return false;
  }
}

// ── 后端认证状态检测 ──────────────────────────

/**
 * 检查后端是否启用了认证（用于决定是否显示登录页）
 */
export async function checkAuthStatus(): Promise<AuthStatus> {
  try {
    const res = await fetch("/api/auth/status");
    if (!res.ok) {
      return { auth_required: false };
    }
    return await res.json();
  } catch {
    return { auth_required: false };
  }
}
