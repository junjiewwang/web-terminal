/**
 * ConfirmDialog — 共享确认弹窗
 *
 * 取代各处散落的 window.confirm()，统一为与编辑弹窗风格一致的玻璃态确认框。
 *
 * 特性：
 *  - Esc 键 / 点击遮罩 关闭
 *  - danger 变体：确认按钮使用红色（用于删除等破坏性操作）
 *  - 打开时自动聚焦确认按钮，回车即确认、Esc 取消
 *
 * 使用：父组件持有 open 状态与 pending 目标，onConfirm 执行实际动作，onCancel 关闭。
 */

import { useEffect, useRef } from "react";

interface ConfirmDialogProps {
  /** 是否显示 */
  open: boolean;
  /** 标题 */
  title: string;
  /** 正文（支持多行/React 节点） */
  message: React.ReactNode;
  /** 确认按钮文案，默认「确认」 */
  confirmText?: string;
  /** 取消按钮文案，默认「取消」 */
  cancelText?: string;
  /** 危险变体：确认按钮变红 */
  danger?: boolean;
  /** 确认回调 */
  onConfirm: () => void;
  /** 取消/关闭回调 */
  onCancel: () => void;
}

export default function ConfirmDialog({
  open,
  title,
  message,
  confirmText = "确认",
  cancelText = "取消",
  danger = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const confirmRef = useRef<HTMLButtonElement>(null);

  // Esc 关闭 + 打开时聚焦确认按钮
  useEffect(() => {
    if (!open) return;
    confirmRef.current?.focus();
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onCancel();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onCancel]);

  if (!open) return null;

  return (
    <>
      <button
        type="button"
        className="fixed inset-0 z-50 bg-black/50 backdrop-blur-sm"
        onClick={onCancel}
        aria-label="关闭"
      />
      <div
        role="alertdialog"
        aria-modal="true"
        aria-label={title}
        className="fixed left-1/2 top-1/2 z-[60] w-full max-w-sm -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-white/10 bg-gray-950/95 p-6 shadow-2xl backdrop-blur-xl"
      >
        <h3 className="text-base font-semibold text-white">{title}</h3>
        <div className="mt-2 text-sm leading-relaxed text-gray-400">{message}</div>

        <div className="mt-6 flex items-center justify-end gap-3">
          <button
            type="button"
            onClick={onCancel}
            className="rounded-lg border border-white/10 bg-white/5 px-4 py-2 text-sm text-gray-300 transition-colors hover:bg-white/10"
          >
            {cancelText}
          </button>
          <button
            ref={confirmRef}
            type="button"
            onClick={onConfirm}
            className={`rounded-lg px-4 py-2 text-sm font-medium transition-colors focus:outline-none focus-visible:ring-2 ${
              danger
                ? "border border-red-500/30 bg-red-500/15 text-red-300 hover:bg-red-500/25 focus-visible:ring-red-500/40"
                : "border border-emerald-500/30 bg-emerald-500/15 text-emerald-300 hover:border-emerald-400/50 hover:bg-emerald-500/25 focus-visible:ring-emerald-500/40"
            }`}
          >
            {confirmText}
          </button>
        </div>
      </div>
    </>
  );
}
