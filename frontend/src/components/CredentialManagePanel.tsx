/**
 * 共享凭据管理面板
 *
 * 功能：
 * - 列表展示所有凭据（名称、描述、引用数）
 * - 新增 / 编辑 / 删除凭据
 * - 脱敏显示（不展示密码明文）
 */

import { useState, useCallback, useEffect } from "react";
import type { CredentialItem } from "../services/api";
import {
  listCredentials,
  createCredential,
  updateCredential,
  deleteCredential,
} from "../services/api";
import ConfirmDialog from "./ConfirmDialog";

// ── 主组件 ────────────────────────────────────

export default function CredentialManagePanel() {
  const [credentials, setCredentials] = useState<CredentialItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [editModalOpen, setEditModalOpen] = useState(false);
  const [editingCred, setEditingCred] = useState<CredentialItem | null>(null);
  const [pendingDelete, setPendingDelete] = useState<CredentialItem | null>(null);
  const [toast, setToast] = useState<{ type: "success" | "error"; text: string } | null>(null);

  const showToast = useCallback((type: "success" | "error", text: string) => {
    setToast({ type, text });
    setTimeout(() => setToast(null), 4000);
  }, []);

  const refresh = useCallback(async () => {
    try {
      const data = await listCredentials();
      setCredentials(data);
    } catch (err) {
      showToast("error", err instanceof Error ? err.message : "加载凭据失败");
    } finally {
      setLoading(false);
    }
  }, [showToast]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const handleCreate = useCallback(() => {
    setEditingCred(null);
    setEditModalOpen(true);
  }, []);

  const handleEdit = useCallback((cred: CredentialItem) => {
    setEditingCred(cred);
    setEditModalOpen(true);
  }, []);

  const handleDelete = useCallback((cred: CredentialItem) => {
    setPendingDelete(cred);
  }, []);

  const confirmDelete = useCallback(async () => {
    if (!pendingDelete) return;
    const cred = pendingDelete;
    setPendingDelete(null);
    try {
      await deleteCredential(cred.id);
      showToast("success", `已删除凭据「${cred.name}」`);
      refresh();
    } catch (err) {
      showToast("error", err instanceof Error ? err.message : "删除失败");
    }
  }, [pendingDelete, refresh, showToast]);

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-white/8 px-6 py-4">
        <div>
          <h2 className="text-lg font-semibold text-white">凭据管理</h2>
          <p className="mt-0.5 text-xs text-gray-500">{credentials.length} 个共享凭据</p>
        </div>
        <button
          onClick={handleCreate}
          className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-1.5 text-xs font-medium text-emerald-300 transition-colors hover:border-emerald-400/50 hover:bg-emerald-500/20"
        >
          + 新增凭据
        </button>
      </div>

      {/* List */}
      <div className="flex-1 overflow-y-auto px-6 py-4">
        {loading ? (
          <div className="flex flex-col items-center justify-center py-20 text-gray-500">
            <p className="text-sm">加载中...</p>
          </div>
        ) : credentials.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-20 text-gray-500">
            <div className="mb-3 text-4xl">🔑</div>
            <p className="text-sm">暂无共享凭据</p>
            <p className="mt-1 text-xs text-gray-600">点击「新增凭据」创建，或通过 YAML 同步导入</p>
          </div>
        ) : (
          <div className="space-y-2">
            {credentials.map((cred) => (
              <div
                key={cred.id}
                className="group flex items-center justify-between rounded-xl border border-white/8 bg-white/[0.02] px-4 py-3 transition-colors hover:border-white/12 hover:bg-white/[0.04]"
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-sm font-medium text-gray-100">{cred.name}</span>
                    {cred.ref_count > 0 && (
                      <span className="shrink-0 rounded-full border border-blue-500/20 bg-blue-500/10 px-1.5 py-0.5 text-[10px] text-blue-400">
                        {cred.ref_count} 引用
                      </span>
                    )}
                  </div>
                  {cred.description && (
                    <p className="mt-0.5 truncate text-[11px] text-gray-500">{cred.description}</p>
                  )}
                  <p className="mt-0.5 text-[10px] text-gray-600">
                    创建于 {new Date(cred.created_at).toLocaleDateString()}
                  </p>
                </div>

                {/* Actions */}
                <div className="flex shrink-0 items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100">
                  <button
                    onClick={() => handleEdit(cred)}
                    className="rounded-md border border-white/8 bg-white/5 px-2 py-1 text-[10px] text-gray-400 transition-colors hover:border-blue-400/30 hover:text-blue-300"
                  >
                    编辑
                  </button>
                  <button
                    onClick={() => handleDelete(cred)}
                    className="rounded-md border border-white/8 bg-white/5 px-2 py-1 text-[10px] text-gray-400 transition-colors hover:border-red-400/30 hover:text-red-300"
                  >
                    删除
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 编辑弹窗 */}
      {editModalOpen && (
        <CredentialEditModal
          credential={editingCred}
          onClose={() => setEditModalOpen(false)}
          onSuccess={() => {
            setEditModalOpen(false);
            refresh();
          }}
          showToast={showToast}
        />
      )}

      {/* 删除确认弹窗 */}
      {pendingDelete && (
        <ConfirmDialog
          open={!!pendingDelete}
          danger
          title="删除凭据"
          message={
            pendingDelete.ref_count > 0
              ? `凭据「${pendingDelete.name}」正在被 ${pendingDelete.ref_count} 个主机引用，删除后这些引用将失效。确认删除？`
              : `确认删除凭据「${pendingDelete.name}」？此操作不可撤销。`
          }
          confirmText="删除"
          onConfirm={confirmDelete}
          onCancel={() => setPendingDelete(null)}
        />
      )}

      {/* Toast */}
      {toast && (
        <div
          className={`fixed bottom-6 right-6 z-50 rounded-xl border px-4 py-3 text-sm shadow-2xl backdrop-blur ${
            toast.type === "success"
              ? "border-emerald-500/30 bg-emerald-950/90 text-emerald-300"
              : "border-red-500/30 bg-red-950/90 text-red-300"
          }`}
        >
          {toast.text}
        </div>
      )}
    </div>
  );
}

// ── 凭据编辑弹窗 ──────────────────────────────

interface CredentialEditModalProps {
  credential: CredentialItem | null; // null = 创建模式
  onClose: () => void;
  onSuccess: () => void;
  showToast: (type: "success" | "error", text: string) => void;
}

function CredentialEditModal({ credential, onClose, onSuccess, showToast }: CredentialEditModalProps) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const isEdit = credential !== null;
  const [name, setName] = useState(credential?.name ?? "");
  const [password, setPassword] = useState("");
  const [description, setDescription] = useState(credential?.description ?? "");
  const [showPassword, setShowPassword] = useState(false);
  const [loading, setLoading] = useState(false);

  const handleSubmit = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    if (!isEdit && !name.trim()) return;
    if (!isEdit && !password) return;

    setLoading(true);
    try {
      if (isEdit) {
        const updates: { password?: string; description?: string } = {};
        if (password) updates.password = password;
        if (description !== (credential?.description ?? "")) updates.description = description;
        if (Object.keys(updates).length === 0) {
          showToast("error", "请至少修改一个字段");
          setLoading(false);
          return;
        }
        await updateCredential(credential!.id, updates);
        showToast("success", `凭据「${credential!.name}」已更新`);
      } else {
        await createCredential({
          name: name.trim(),
          password,
          description: description.trim() || undefined,
        });
        showToast("success", `凭据「${name.trim()}」创建成功`);
      }
      onSuccess();
    } catch (err) {
      showToast("error", err instanceof Error ? err.message : "操作失败");
    } finally {
      setLoading(false);
    }
  }, [isEdit, name, password, description, credential, onSuccess, showToast]);

  return (
    <>
      <button
        type="button"
        className="fixed inset-0 z-40 bg-black/50 backdrop-blur-sm"
        onClick={onClose}
        aria-label="关闭"
      />
      <div className="fixed left-1/2 top-1/2 z-50 w-full max-w-md -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-white/10 bg-gray-950/95 shadow-2xl backdrop-blur-xl">
        <form onSubmit={handleSubmit}>
          {/* Header */}
          <div className="flex items-center justify-between border-b border-white/8 px-6 py-4">
            <h3 className="text-base font-semibold text-white">
              {isEdit ? `编辑凭据 · ${credential!.name}` : "新增凭据"}
            </h3>
            <button
              type="button"
              onClick={onClose}
              className="rounded-full p-1.5 text-gray-500 transition-colors hover:bg-white/10 hover:text-white"
            >
              ✕
            </button>
          </div>

          {/* Body */}
          <div className="space-y-4 px-6 py-5">
            {/* 名称 */}
            <div>
              <label className="mb-1 block text-[11px] text-gray-500">凭据名称</label>
              {isEdit ? (
                <p className="font-mono text-sm text-gray-300">{credential!.name}</p>
              ) : (
                <input
                  type="text"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="如 tce-server-login"
                  className="w-full rounded-lg border border-white/10 bg-white/5 px-3 py-2 font-mono text-sm text-gray-200 placeholder:text-gray-600 focus:border-emerald-500/40 focus:outline-none"
                  autoFocus
                />
              )}
            </div>

            {/* 密码 */}
            <div>
              <label className="mb-1 block text-[11px] text-gray-500">
                密码{isEdit ? "（留空则不修改）" : ""}
              </label>
              <div className="flex gap-1">
                <input
                  type={showPassword ? "text" : "password"}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder={isEdit ? "输入新密码以更新" : "密码明文（存储时加密）"}
                  className="flex-1 rounded-lg border border-white/10 bg-white/5 px-3 py-2 text-sm text-gray-200 placeholder:text-gray-600 focus:border-emerald-500/40 focus:outline-none"
                />
                <button
                  type="button"
                  onClick={() => setShowPassword((p) => !p)}
                  className="rounded-lg border border-white/10 bg-white/5 px-2.5 text-xs text-gray-500 transition-colors hover:text-gray-300"
                  title={showPassword ? "隐藏" : "显示"}
                >
                  {showPassword ? "🙈" : "👁"}
                </button>
              </div>
            </div>

            {/* 描述 */}
            <div>
              <label className="mb-1 block text-[11px] text-gray-500">描述（可选）</label>
              <input
                type="text"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="凭据用途说明"
                className="w-full rounded-lg border border-white/10 bg-white/5 px-3 py-2 text-sm text-gray-200 placeholder:text-gray-600 focus:border-emerald-500/40 focus:outline-none"
              />
            </div>
          </div>

          {/* Footer */}
          <div className="flex items-center justify-end gap-3 border-t border-white/8 px-6 py-4">
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg border border-white/10 bg-white/5 px-4 py-2 text-sm text-gray-300 transition-colors hover:bg-white/10"
            >
              取消
            </button>
            <button
              type="submit"
              disabled={loading || (!isEdit && (!name.trim() || !password))}
              className="rounded-lg border border-emerald-500/30 bg-emerald-500/15 px-4 py-2 text-sm font-medium text-emerald-300 transition-colors hover:border-emerald-400/50 hover:bg-emerald-500/25 disabled:opacity-50"
            >
              {loading ? "保存中..." : isEdit ? "保存" : "创建"}
            </button>
          </div>
        </form>
      </div>
    </>
  );
}
