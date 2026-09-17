/**
 * 交互规则管理面板
 *
 * 功能：
 * - 列表展示所有交互规则（名称、匹配正则、发送内容、优先级、启用开关）
 * - 新增 / 编辑 / 删除规则
 * - 示例规则展示（仅文字参考）
 * - 变量说明
 *
 * 规则是通用的「匹配正则 → 发送内容」抽象，由用户自由配置，
 * 节点 steps 为空时连接引擎会自动按优先级应用。
 */

import { useState, useCallback, useEffect } from "react";
import type { InteractionRuleItem, InteractionRuleExample } from "../services/api";
import {
  listInteractionRules,
  listInteractionRuleExamples,
  createInteractionRule,
  updateInteractionRule,
  deleteInteractionRule,
} from "../services/api";
import ConfirmDialog from "./ConfirmDialog";

// ── 支持的变量说明 ──────────────────────────────

const VARIABLES: { name: string; desc: string }[] = [
  { name: "{{password}}", desc: "节点入口密码（来自 credential_ref 或 entry_password）" },
  { name: "{{user_id}}", desc: "节点 rule_vars.user_id 配置的值" },
  { name: "{{username}}", desc: "节点 SSH 用户名" },
  { name: "{{manual}}", desc: "标记为需人工输入（如 MFA / 验证码）" },
];

// ── 主组件 ────────────────────────────────────

export default function InteractionRulePanel() {
  const [rules, setRules] = useState<InteractionRuleItem[]>([]);
  const [examples, setExamples] = useState<InteractionRuleExample[]>([]);
  const [loading, setLoading] = useState(true);
  const [showExamples, setShowExamples] = useState(false);
  const [editModalOpen, setEditModalOpen] = useState(false);
  const [editingRule, setEditingRule] = useState<InteractionRuleItem | null>(null);
  const [pendingDelete, setPendingDelete] = useState<InteractionRuleItem | null>(null);
  const [toast, setToast] = useState<{ type: "success" | "error"; text: string } | null>(null);

  const showToast = useCallback((type: "success" | "error", text: string) => {
    setToast({ type, text });
    setTimeout(() => setToast(null), 4000);
  }, []);

  const refresh = useCallback(async () => {
    try {
      const data = await listInteractionRules();
      setRules(data);
    } catch (err) {
      showToast("error", err instanceof Error ? err.message : "加载交互规则失败");
    } finally {
      setLoading(false);
    }
  }, [showToast]);

  useEffect(() => {
    refresh();
    listInteractionRuleExamples()
      .then(setExamples)
      .catch(() => {});
  }, [refresh]);

  const handleCreate = useCallback(() => {
    setEditingRule(null);
    setEditModalOpen(true);
  }, []);

  const handleEdit = useCallback((rule: InteractionRuleItem) => {
    setEditingRule(rule);
    setEditModalOpen(true);
  }, []);

  const confirmDelete = useCallback(async () => {
    if (!pendingDelete) return;
    const rule = pendingDelete;
    setPendingDelete(null);
    try {
      await deleteInteractionRule(rule.id);
      showToast("success", `已删除规则「${rule.name}」`);
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
          <h2 className="text-lg font-semibold text-white">交互规则</h2>
          <p className="mt-0.5 text-xs text-gray-500">{rules.length} 条规则 · 节点 steps 为空时自动应用</p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowExamples((p) => !p)}
            className="rounded-lg border border-white/10 bg-white/5 px-3 py-1.5 text-xs font-medium text-gray-300 transition-colors hover:bg-white/10"
          >
            {showExamples ? "隐藏示例" : "查看示例"}
          </button>
          <button
            onClick={handleCreate}
            className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-1.5 text-xs font-medium text-emerald-300 transition-colors hover:border-emerald-400/50 hover:bg-emerald-500/20"
          >
            + 新增规则
          </button>
        </div>
      </div>

      {/* 变量说明 */}
      <div className="border-b border-white/8 px-6 py-3">
        <p className="mb-1.5 text-[11px] text-gray-500">发送内容支持以下变量：</p>
        <div className="flex flex-wrap gap-2">
          {VARIABLES.map((v) => (
            <span
              key={v.name}
              title={v.desc}
              className="rounded-md border border-white/8 bg-white/[0.03] px-2 py-1 font-mono text-[11px] text-emerald-300/90"
            >
              {v.name}
            </span>
          ))}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-6 py-4">
        {/* 示例区 */}
        {showExamples && (
          <div className="mb-4 rounded-xl border border-blue-500/20 bg-blue-500/[0.04] p-4">
            <div className="mb-2 flex items-center gap-2">
              <span className="text-xs font-medium text-blue-300">示例（参考）</span>
              <span className="text-[10px] text-gray-500">示例仅作配置参考，不会自动生效，需手动创建</span>
            </div>
            {examples.length === 0 ? (
              <p className="text-xs text-gray-500">暂无示例</p>
            ) : (
              <div className="space-y-2">
                {examples.map((ex) => (
                  <div key={ex.name} className="rounded-lg border border-white/8 bg-white/[0.02] px-3 py-2">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-xs font-medium text-gray-200">{ex.name}</span>
                      <span className="rounded-full border border-white/8 bg-white/5 px-1.5 py-0.5 text-[10px] text-gray-500">
                        优先级 {ex.priority}
                      </span>
                    </div>
                    <p className="mt-0.5 text-[11px] text-gray-500">{ex.description}</p>
                    <div className="mt-1.5 grid grid-cols-2 gap-1 text-[10px]">
                      <div>
                        <span className="text-gray-600">匹配正则：</span>
                        <span className="break-all font-mono text-gray-400">{ex.wait_pattern}</span>
                      </div>
                      <div>
                        <span className="text-gray-600">发送：</span>
                        <span className="break-all font-mono text-gray-400">{ex.send_text}</span>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {loading ? (
          <div className="flex flex-col items-center justify-center py-20 text-gray-500">
            <p className="text-sm">加载中...</p>
          </div>
        ) : rules.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-20 text-gray-500">
            <div className="mb-3 text-4xl">🧩</div>
            <p className="text-sm">暂无交互规则</p>
            <p className="mt-1 text-xs text-gray-600">点击「查看示例」参考配置，或「新增规则」创建</p>
          </div>
        ) : (
          <div className="space-y-2">
            {rules.map((rule) => (
              <div
                key={rule.id}
                className="group flex items-center justify-between rounded-xl border border-white/8 bg-white/[0.02] px-4 py-3 transition-colors hover:border-white/12 hover:bg-white/[0.04]"
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-sm font-medium text-gray-100">{rule.name}</span>
                    <span className="shrink-0 rounded-full border border-white/8 bg-white/5 px-1.5 py-0.5 text-[10px] text-gray-500">
                      优先级 {rule.priority}
                    </span>
                    {!rule.enabled && (
                      <span className="shrink-0 rounded-full border border-gray-500/20 bg-gray-500/10 px-1.5 py-0.5 text-[10px] text-gray-500">
                        已禁用
                      </span>
                    )}
                  </div>
                  {rule.description && (
                    <p className="mt-0.5 truncate text-[11px] text-gray-500">{rule.description}</p>
                  )}
                  <div className="mt-1 flex items-center gap-3 text-[10px]">
                    <span className="text-gray-600">匹配：</span>
                    <span className="truncate font-mono text-gray-400">{rule.wait_pattern}</span>
                    <span className="text-gray-600">发送：</span>
                    <span className="truncate font-mono text-gray-400">{rule.send_text}</span>
                  </div>
                </div>

                {/* Actions */}
                <div className="flex shrink-0 items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100">
                  <button
                    onClick={() => handleEdit(rule)}
                    className="rounded-md border border-white/8 bg-white/5 px-2 py-1 text-[10px] text-gray-400 transition-colors hover:border-blue-400/30 hover:text-blue-300"
                  >
                    编辑
                  </button>
                  <button
                    onClick={() => setPendingDelete(rule)}
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
        <InteractionRuleEditModal
          rule={editingRule}
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
          title="删除交互规则"
          message={`确认删除规则「${pendingDelete.name}」？此操作不可撤销。`}
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

// ── 规则编辑弹窗 ──────────────────────────────

interface InteractionRuleEditModalProps {
  rule: InteractionRuleItem | null; // null = 创建模式
  onClose: () => void;
  onSuccess: () => void;
  showToast: (type: "success" | "error", text: string) => void;
}

function InteractionRuleEditModal({ rule, onClose, onSuccess, showToast }: InteractionRuleEditModalProps) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const isEdit = rule !== null;
  const [name, setName] = useState(rule?.name ?? "");
  const [description, setDescription] = useState(rule?.description ?? "");
  const [priority, setPriority] = useState(String(rule?.priority ?? 100));
  const [waitPattern, setWaitPattern] = useState(rule?.wait_pattern ?? "");
  const [sendText, setSendText] = useState(rule?.send_text ?? "");
  const [timeout, setTimeoutVal] = useState(String(rule?.timeout ?? 15));
  const [maxRetries, setMaxRetries] = useState(String(rule?.max_retries ?? 1));
  const [enabled, setEnabled] = useState(rule?.enabled ?? true);
  const [loading, setLoading] = useState(false);

  const handleSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      if (!name.trim() || !waitPattern.trim() || !sendText.trim()) return;

      setLoading(true);
      const payload = {
        name: name.trim(),
        description: description.trim() || null,
        priority: Number(priority) || 100,
        wait_pattern: waitPattern.trim(),
        send_text: sendText.trim(),
        timeout: Number(timeout) || 15,
        max_retries: Number(maxRetries) || 1,
        enabled,
      };
      try {
        if (isEdit) {
          await updateInteractionRule(rule!.id, payload);
          showToast("success", `规则「${rule!.name}」已更新`);
        } else {
          await createInteractionRule(payload);
          showToast("success", `规则「${name.trim()}」创建成功`);
        }
        onSuccess();
      } catch (err) {
        showToast("error", err instanceof Error ? err.message : "操作失败");
      } finally {
        setLoading(false);
      }
    },
    [isEdit, name, description, priority, waitPattern, sendText, timeout, maxRetries, enabled, rule, onSuccess, showToast],
  );

  const inputCls =
    "w-full rounded-lg border border-white/10 bg-white/5 px-3 py-2 text-sm text-gray-200 placeholder:text-gray-600 focus:border-emerald-500/40 focus:outline-none";
  const labelCls = "mb-1 block text-[11px] text-gray-500";

  return (
    <>
      <button
        type="button"
        className="fixed inset-0 z-40 bg-black/50 backdrop-blur-sm"
        onClick={onClose}
        aria-label="关闭"
      />
      <div className="fixed left-1/2 top-1/2 z-50 w-full max-w-lg -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-white/10 bg-gray-950/95 shadow-2xl backdrop-blur-xl">
        <form onSubmit={handleSubmit}>
          {/* Header */}
          <div className="flex items-center justify-between border-b border-white/8 px-6 py-4">
            <h3 className="text-base font-semibold text-white">
              {isEdit ? `编辑规则 · ${rule!.name}` : "新增交互规则"}
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
          <div className="max-h-[60vh] space-y-4 overflow-y-auto px-6 py-5">
            {/* 名称 */}
            <div>
              <label className={labelCls}>规则名称</label>
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="如 password"
                className={`${inputCls} font-mono`}
                autoFocus={!isEdit}
              />
            </div>

            {/* 说明 */}
            <div>
              <label className={labelCls}>说明（可选）</label>
              <input
                type="text"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="规则用途说明"
                className={inputCls}
              />
            </div>

            {/* 匹配正则 + 发送内容 */}
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className={labelCls}>匹配正则（wait）</label>
                <textarea
                  value={waitPattern}
                  onChange={(e) => setWaitPattern(e.target.value)}
                  placeholder={"(?i)password\\s*[:：]\\s*$"}
                  rows={2}
                  className={`${inputCls} resize-none font-mono`}
                />
              </div>
              <div>
                <label className={labelCls}>发送内容（send）</label>
                <textarea
                  value={sendText}
                  onChange={(e) => setSendText(e.target.value)}
                  placeholder="{{password}}"
                  rows={2}
                  className={`${inputCls} resize-none font-mono`}
                />
              </div>
            </div>

            {/* 优先级 / 超时 / 最大次数 */}
            <div className="grid grid-cols-3 gap-3">
              <div>
                <label className={labelCls}>优先级</label>
                <input
                  type="number"
                  value={priority}
                  onChange={(e) => setPriority(e.target.value)}
                  className={inputCls}
                />
              </div>
              <div>
                <label className={labelCls}>超时（秒）</label>
                <input
                  type="number"
                  min={1}
                  max={120}
                  value={timeout}
                  onChange={(e) => setTimeoutVal(e.target.value)}
                  className={inputCls}
                />
              </div>
              <div>
                <label className={labelCls}>最大次数</label>
                <input
                  type="number"
                  min={1}
                  max={10}
                  value={maxRetries}
                  onChange={(e) => setMaxRetries(e.target.value)}
                  className={inputCls}
                />
              </div>
            </div>

            {/* 启用开关 */}
            <label className="flex cursor-pointer items-center justify-between rounded-lg border border-white/8 bg-white/[0.02] px-3 py-2.5">
              <span className="text-sm text-gray-300">启用此规则</span>
              <button
                type="button"
                role="switch"
                aria-checked={enabled}
                onClick={() => setEnabled((p) => !p)}
                className={`relative h-5 w-9 rounded-full transition-colors ${
                  enabled ? "bg-emerald-500" : "bg-gray-600"
                }`}
              >
                <span
                  className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-transform ${
                    enabled ? "translate-x-4" : "translate-x-0.5"
                  }`}
                />
              </button>
            </label>

            {/* 变量提示 */}
            <div className="rounded-lg border border-white/8 bg-white/[0.02] px-3 py-2">
              <p className="mb-1 text-[10px] text-gray-500">发送内容可用变量：</p>
              <div className="flex flex-wrap gap-1.5">
                {VARIABLES.map((v) => (
                  <span key={v.name} title={v.desc} className="font-mono text-[10px] text-emerald-300/80">
                    {v.name}
                  </span>
                ))}
              </div>
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
              disabled={loading || !name.trim() || !waitPattern.trim() || !sendText.trim()}
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
