/**
 * LoginPage — 租户登录页面
 *
 * 设计风格与主应用一致：深色背景 + 玻璃态 + 渐变装饰。
 * 支持回车键提交、错误提示、加载状态。
 */

import { useState, useCallback } from "react";
import { login } from "../services/auth";

interface LoginPageProps {
  /** 登录成功回调 */
  onLoginSuccess: () => void;
}

export default function LoginPage({ onLoginSuccess }: LoginPageProps) {
  const [tenantId, setTenantId] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const handleSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      if (!tenantId.trim() || !password.trim()) {
        setError("请输入账号和密码");
        return;
      }

      setError(null);
      setLoading(true);

      try {
        await login(tenantId.trim(), password);
        onLoginSuccess();
      } catch (err) {
        setError(err instanceof Error ? err.message : "登录失败");
      } finally {
        setLoading(false);
      }
    },
    [tenantId, password, onLoginSuccess],
  );

  return (
    <div className="relative flex h-screen items-center justify-center overflow-hidden bg-[#050816] text-gray-100">
      {/* 背景装饰 */}
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_top_left,rgba(16,185,129,0.16),transparent_34%),radial-gradient(circle_at_top_right,rgba(34,211,238,0.12),transparent_26%)]" />
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_bottom_center,rgba(16,185,129,0.06),transparent_50%)]" />

      {/* 登录卡片 */}
      <div className="relative z-10 w-full max-w-sm">
        {/* Logo 区域 */}
        <div className="mb-8 text-center">
          <div className="mx-auto flex h-16 w-16 items-center justify-center rounded-3xl border border-emerald-400/20 bg-emerald-400/10 text-2xl font-bold text-emerald-300 shadow-[0_20px_40px_rgba(16,185,129,0.15)]">
            ⌘
          </div>
          <h1 className="mt-4 text-2xl font-semibold tracking-tight text-white">
            WebTerminal
          </h1>
          <p className="mt-2 text-sm text-gray-500">
            SSH 终端管理平台
          </p>
        </div>

        {/* 表单卡片 */}
        <form
          onSubmit={handleSubmit}
          className="rounded-2xl border border-white/8 bg-gray-950/80 p-6 shadow-2xl backdrop-blur-xl"
        >
          {/* 账号 */}
          <div className="mb-4">
            <label
              htmlFor="tenant-id"
              className="mb-1.5 block text-xs font-medium text-gray-400"
            >
              账号
            </label>
            <input
              id="tenant-id"
              type="text"
              value={tenantId}
              onChange={(e) => setTenantId(e.target.value)}
              placeholder="输入租户 ID"
              autoComplete="username"
              autoFocus
              disabled={loading}
              className="w-full rounded-xl border border-white/10 bg-white/5 px-4 py-2.5 text-sm text-white placeholder-gray-600 outline-none transition-colors focus:border-emerald-400/50 focus:ring-1 focus:ring-emerald-400/20 disabled:opacity-50"
            />
          </div>

          {/* 密码 */}
          <div className="mb-5">
            <label
              htmlFor="password"
              className="mb-1.5 block text-xs font-medium text-gray-400"
            >
              密码
            </label>
            <input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="输入密码"
              autoComplete="current-password"
              disabled={loading}
              className="w-full rounded-xl border border-white/10 bg-white/5 px-4 py-2.5 text-sm text-white placeholder-gray-600 outline-none transition-colors focus:border-emerald-400/50 focus:ring-1 focus:ring-emerald-400/20 disabled:opacity-50"
            />
          </div>

          {/* 错误提示 */}
          {error && (
            <div className="mb-4 rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-xs text-red-400">
              {error}
            </div>
          )}

          {/* 登录按钮 */}
          <button
            type="submit"
            disabled={loading}
            className="flex w-full items-center justify-center gap-2 rounded-xl bg-emerald-500/90 px-4 py-2.5 text-sm font-medium text-white transition-all hover:bg-emerald-500 hover:shadow-[0_8px_24px_rgba(16,185,129,0.25)] active:scale-[0.98] disabled:cursor-wait disabled:opacity-60"
          >
            {loading ? (
              <>
                <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white" />
                登录中...
              </>
            ) : (
              "登录"
            )}
          </button>
        </form>
      </div>
    </div>
  );
}
