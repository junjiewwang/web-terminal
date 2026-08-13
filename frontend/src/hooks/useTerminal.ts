/**
 * useTerminal — xterm.js 终端生命周期管理 Hook
 *
 * 职责：
 *  - 创建 / 销毁 Terminal 实例
 *  - 自动挂载到容器 DOM 元素
 *  - 自适应容器大小（FitAddon）
 *  - 暴露 write / resize 等操作接口供外部驱动
 *
 * 设计原则：
 *  - 只负责「终端 UI 渲染」，不涉及任何网络连接（socket.io 由 useWettySocket 管理）
 *  - 通过回调（onData / onResize）向上游传递用户输入和尺寸变化
 *  - 组件卸载时自动清理所有资源
 */

import { useRef, useEffect, useCallback, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";

/** 终端尺寸 */
export interface TermSize {
  cols: number;
  rows: number;
}

/** Hook 配置 */
export interface UseTerminalOptions {
  /** 用户输入回调（键盘 → socket.emit('input')） */
  onData?: (data: string) => void;
  /** 终端尺寸变化回调（resize → socket.emit('resize')） */
  onResize?: (size: TermSize) => void;
  /** 用户选中文本回调（Shift+鼠标拖拽 → 写入剪贴板） */
  onSelectionChange?: (text: string) => void;
}

/** Hook 返回的操作句柄 */
export interface TerminalHandle {
  /**
   * Callback ref — 绑定到终端容器 div 的 ref 属性。
   * 使用 callback ref 而非 useRef，因为容器 DOM 可能在条件渲染
   * （如 host 为 null 时渲染空状态）中延迟挂载，callback ref 能
   * 感知挂载/卸载时机并触发 Terminal 初始化。
   */
  containerRef: (node: HTMLDivElement | null) => void;
  /** 写入数据到终端（server → terminal） */
  write: (data: string) => void;
  /** 清屏（清空 scrollback + 当前屏幕） */
  clear: () => void;
  /**
   * 重置终端状态（backend 切换专用）。
   *
   * 清空屏幕内容 + scrollback，同时重置 xterm.js 的 DEC Private Mode 状态：
   * - 关闭鼠标追踪（?1000l/?1002l/?1003l/?1006l）
   * - 关闭 Bracketed Paste（?2004l）
   * - 关闭 Application Cursor（?1l）
   * - 关闭 Alternate Screen（?1049l）
   *
   * 原因：TMUX 模式会向 xterm.js 发送 ?1000h/?1002h 等鼠标追踪序列，
   * 切换到 Broker 后 xterm.js 实例被复用，这些 mode 状态会残留，
   * 导致鼠标滚轮被编码为鼠标序列而非本地 scrollback 滚动。
   */
  reset: () => void;
  /** 手动触发 fit（容器尺寸变化时调用） */
  fit: () => void;
  /** 聚焦终端 */
  focus: () => void;
  /** 获取当前尺寸 */
  getSize: () => TermSize;
  /** 获取当前选中文本（复制工具栏用） */
  getSelection: () => string;
}

/**
 * xterm.js 终端 Hook
 *
 * 使用 callback ref 模式：当容器 DOM 元素挂载到页面时自动创建 Terminal，
 * 卸载时自动销毁。这解决了条件渲染导致 useRef + useEffect 错过容器挂载的问题。
 *
 * @param options - 回调配置
 * @returns TerminalHandle 操作句柄（含 containerRef）
 */
export function useTerminal(
  options: UseTerminalOptions = {},
): TerminalHandle {
  const termRef = useRef<Terminal | null>(null);
  const fitAddonRef = useRef<FitAddon | null>(null);
  // 追踪容器 DOM 节点，用于 effect 触发
  const [container, setContainer] = useState<HTMLDivElement | null>(null);
  // 用 ref 存储回调避免 effect 频繁重建
  const onDataRef = useRef(options.onData);
  const onResizeRef = useRef(options.onResize);
  const onSelectionChangeRef = useRef(options.onSelectionChange);
  onDataRef.current = options.onData;
  onResizeRef.current = options.onResize;
  onSelectionChangeRef.current = options.onSelectionChange;

  /**
   * Callback ref：React 在 DOM 挂载时调用 (node)，卸载时调用 (null)。
   * 通过 setContainer 触发 state 变化 → useEffect 重新运行。
   */
  const containerRef = useCallback((node: HTMLDivElement | null) => {
    setContainer(node);
  }, []);

  // ── 终端生命周期 ──────────────────────────────
  useEffect(() => {
    if (!container) return;

    // 创建终端实例
    const term = new Terminal({
      cursorBlink: true,
      fontSize: 14,
      fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', Menlo, Monaco, 'Courier New', monospace",
      theme: {
        background: "#0a0a0a",
        foreground: "#e4e4e7",
        cursor: "#34d399",
        selectionBackground: "rgba(52, 211, 153, 0.3)",
        black: "#18181b",
        red: "#ef4444",
        green: "#22c55e",
        yellow: "#eab308",
        blue: "#3b82f6",
        magenta: "#a855f7",
        cyan: "#06b6d4",
        white: "#e4e4e7",
        brightBlack: "#52525b",
        brightRed: "#f87171",
        brightGreen: "#4ade80",
        brightYellow: "#facc15",
        brightBlue: "#60a5fa",
        brightMagenta: "#c084fc",
        brightCyan: "#22d3ee",
        brightWhite: "#fafafa",
      },
      allowProposedApi: true,
    });

    // 加载插件
    const fitAddon = new FitAddon();
    term.loadAddon(fitAddon);
    term.loadAddon(new WebLinksAddon());

    // 挂载到 DOM
    term.open(container);

    // 延迟 fit，确保容器已完成布局
    requestAnimationFrame(() => {
      try {
        fitAddon.fit();
      } catch {
        // 首次 fit 可能因容器尺寸为 0 而失败，忽略
      }
    });

    // 监听用户输入
    const dataDisposable = term.onData((data: string) => {
      onDataRef.current?.(data);
    });

    // 监听终端尺寸变化（由 fitAddon.fit() 触发）
    const resizeDisposable = term.onResize((size: { cols: number; rows: number }) => {
      onResizeRef.current?.({ cols: size.cols, rows: size.rows });
    });

    // 监听选区变化（Shift+鼠标拖拽选中 / 双击选词）
    const selectionDisposable = term.onSelectionChange(() => {
      const text = term.getSelection();
      onSelectionChangeRef.current?.(text ?? "");
    });

    // ── 容器尺寸变化 → debounce fit ──────────────
    // 使用 ResizeObserver 替代 window.resize 事件：
    //  - window.resize 仅感知浏览器窗口尺寸变化
    //  - ResizeObserver 能感知容器 DOM 本身的尺寸变化（侧边栏折叠/展开、面板拖拽等）
    // 使用 requestAnimationFrame + 标志位实现 debounce：
    //  - 避免拖拽期间高频 fit() 导致 DOM 重排和终端闪烁
    //  - 比 setTimeout debounce 更流畅，与浏览器渲染帧同步
    let rafId: number | null = null;
    const debouncedFit = () => {
      if (rafId !== null) return; // 当前帧已有排队，跳过
      rafId = requestAnimationFrame(() => {
        rafId = null;
        try {
          fitAddon.fit();
        } catch {
          // 忽略（容器尺寸为 0 等边界场景）
        }
      });
    };

    const resizeObserver = new ResizeObserver(() => {
      debouncedFit();
    });
    resizeObserver.observe(container);

    // 保留 window.resize 作为兜底（某些浏览器 ResizeObserver 可能遗漏全屏切换等场景）
    window.addEventListener("resize", debouncedFit);

    // 存储引用
    termRef.current = term;
    fitAddonRef.current = fitAddon;

    // ── 清理 ──
    return () => {
      if (rafId !== null) cancelAnimationFrame(rafId);
      resizeObserver.disconnect();
      window.removeEventListener("resize", debouncedFit);
      dataDisposable.dispose();
      resizeDisposable.dispose();
      selectionDisposable.dispose();
      term.dispose();
      termRef.current = null;
      fitAddonRef.current = null;
    };
  }, [container]);

  // ── 操作句柄（稳定引用） ──────────────────────
  const write = useCallback((data: string) => {
    termRef.current?.write(data);
  }, []);

  const clear = useCallback(() => {
    termRef.current?.clear();
  }, []);

  /**
   * 重置终端状态序列（ANSI）：
   * - \x1b[?1000l  关闭基本鼠标追踪
   * - \x1b[?1002l  关闭按钮事件追踪
   * - \x1b[?1003l  关闭任意事件追踪
   * - \x1b[?1006l  关闭 SGR 鼠标扩展
   * - \x1b[?2004l  关闭 Bracketed Paste
   * - \x1b[?1l     关闭 Application Cursor (DECCKM)
   * - \x1b[?1049l  退出 Alternate Screen
   * - \x1b>        Normal Keypad (DECKPNM)
   */
  const RESET_MODES_SEQ =
    "\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l" +
    "\x1b[?2004l\x1b[?1l\x1b[?1049l\x1b>";

  const reset = useCallback(() => {
    const term = termRef.current;
    if (!term) return;
    // 先写入重置序列让 xterm.js 清除残留的 DEC Private Mode 状态
    term.write(RESET_MODES_SEQ);
    // 再清空屏幕内容和 scrollback buffer
    term.clear();
  }, []);

  const fit = useCallback(() => {
    try {
      fitAddonRef.current?.fit();
    } catch {
      // 忽略
    }
  }, []);

  const focus = useCallback(() => {
    termRef.current?.focus();
  }, []);

  const getSize = useCallback((): TermSize => {
    const term = termRef.current;
    return { cols: term?.cols ?? 80, rows: term?.rows ?? 24 };
  }, []);

  const getSelection = useCallback((): string => {
    return termRef.current?.getSelection() ?? "";
  }, []);

  return { containerRef, write, clear, reset, fit, focus, getSize, getSelection };
}
