/**
 * CommandPalette — 全局命令面板（⌘K / Ctrl+K）
 *
 * 自研轻量实现（~150 行），不引第三方依赖。
 * 数据源：页面切换、主机（连接/编辑）、终端动作、backend 切换。
 * 支持键盘导航（↑↓ 选择、Enter 执行、Esc 关闭）+ 模糊搜索。
 */

import { useEffect, useRef, useState, useMemo } from "react";

export interface PaletteCommand {
  /** 唯一 id */
  id: string;
  /** 显示标题 */
  title: string;
  /** 副标题/描述 */
  subtitle?: string;
  /** 分组 */
  group: string;
  /** 图标 */
  icon?: string;
  /** 执行动作 */
  action: () => void;
}

interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
  commands: PaletteCommand[];
  /** 搜索关键词（内部维护，可由外部重置） */
  placeholder?: string;
}

export default function CommandPalette({
  open,
  onClose,
  commands,
  placeholder = "搜索命令、主机、页面…",
}: CommandPaletteProps) {
  const [query, setQuery] = useState("");
  const [activeIdx, setActiveIdx] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // 打开时重置 + 聚焦
  useEffect(() => {
    if (open) {
      setQuery("");
      setActiveIdx(0);
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  // 过滤
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return commands;
    return commands.filter(
      (c) =>
        c.title.toLowerCase().includes(q) ||
        (c.subtitle ?? "").toLowerCase().includes(q),
    );
  }, [commands, query]);

  // 键盘导航
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        setActiveIdx((i) => Math.min(i + 1, filtered.length - 1));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setActiveIdx((i) => Math.max(i - 1, 0));
      } else if (e.key === "Enter") {
        e.preventDefault();
        const cmd = filtered[activeIdx];
        if (cmd) {
          cmd.action();
          onClose();
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, filtered, activeIdx, onClose]);

  // 滚动到激活项
  useEffect(() => {
    listRef.current
      ?.querySelector('[data-active="true"]')
      ?.scrollIntoView({ block: "nearest" });
  }, [activeIdx]);

  if (!open) return null;

  // 按分组聚合
  const groups = new Map<string, PaletteCommand[]>();
  for (const c of filtered) {
    if (!groups.has(c.group)) groups.set(c.group, []);
    groups.get(c.group)!.push(c);
  }

  return (
    <>
      <button
        type="button"
        className="fixed inset-0 z-[70] bg-black/50 backdrop-blur-sm"
        onClick={onClose}
        aria-label="关闭命令面板"
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-label="命令面板"
        className="fixed left-1/2 top-[15%] z-[80] w-full max-w-lg -translate-x-1/2 overflow-hidden rounded-2xl border border-white/10 bg-gray-950/95 shadow-2xl backdrop-blur-xl"
      >
        {/* 输入框 */}
        <div className="flex items-center gap-2 border-b border-white/8 px-4 py-3">
          <span className="text-gray-500 text-sm">⌕</span>
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setActiveIdx(0);
            }}
            placeholder={placeholder}
            className="flex-1 bg-transparent text-sm text-gray-200 placeholder:text-gray-600 focus:outline-none"
          />
          <span className="rounded border border-white/10 bg-white/5 px-1.5 py-0.5 text-[10px] text-gray-500">
            Esc
          </span>
        </div>

        {/* 结果列表 */}
        <div ref={listRef} className="max-h-[50vh] overflow-y-auto p-2">
          {filtered.length === 0 ? (
            <div className="px-4 py-8 text-center text-sm text-gray-600">
              无匹配结果
            </div>
          ) : (
            [...groups.entries()].map(([group, items]) => (
              <div key={group} className="mb-1">
                <p className="px-3 pb-1 pt-2 text-[10px] uppercase tracking-wider text-gray-600">
                  {group}
                </p>
                {items.map((c) => {
                  const idx = filtered.indexOf(c);
                  const isActive = idx === activeIdx;
                  return (
                    <button
                      key={c.id}
                      type="button"
                      data-active={isActive}
                      onMouseEnter={() => setActiveIdx(idx)}
                      onClick={() => {
                        c.action();
                        onClose();
                      }}
                      className={`flex w-full items-center gap-3 rounded-lg px-3 py-2 text-left transition-colors ${
                        isActive ? "bg-emerald-500/10 text-white" : "text-gray-300"
                      }`}
                    >
                      {c.icon && <span className="text-base shrink-0">{c.icon}</span>}
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm">{c.title}</p>
                        {c.subtitle && (
                          <p className="truncate text-xs text-gray-500">{c.subtitle}</p>
                        )}
                      </div>
                    </button>
                  );
                })}
              </div>
            ))
          )}
        </div>
      </div>
    </>
  );
}
