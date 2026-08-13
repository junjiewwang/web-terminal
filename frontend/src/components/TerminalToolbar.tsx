/**
 * TerminalToolbar — 终端快捷工具栏
 *
 * 常驻终端状态栏右侧的高频操作（SVG 图标，跨平台一致）：
 *  - 复制：复制当前选中文本到剪贴板
 *  - 清屏：清空终端 scrollback + 当前屏幕
 *  - 全屏：进入浏览器全屏 + 隐藏侧栏（Esc 退出）
 *  - 传文件：切换 FileTransferPanel
 *  - 排障：切换 SnippetPanel
 *
 * 设计：图标按钮，active 态高亮；title 提示 + aria-label 无障碍。
 */

interface TerminalToolbarProps {
  /** 是否有选中文本（控制复制按钮可用态） */
  canCopy: boolean;
  /** 复制选中文本 */
  onCopy: () => void;
  /** 清屏 */
  onClear: () => void;
  /** 是否全屏 */
  isFullscreen: boolean;
  /** 切换全屏 */
  onToggleFullscreen: () => void;
  /** 文件传输面板是否展开 */
  fileTransferOpen: boolean;
  /** 切换文件传输面板 */
  onToggleFileTransfer: () => void;
  /** 排障面板是否展开 */
  snippetOpen: boolean;
  /** 切换排障面板 */
  onToggleSnippet: () => void;
}

function IconButton({
  title,
  active = false,
  disabled = false,
  onClick,
  children,
}: {
  title: string;
  active?: boolean;
  disabled?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-label={title}
      className={`flex h-7 w-7 items-center justify-center rounded-md border transition-colors ${
        active
          ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-400"
          : "border-transparent text-gray-500 hover:bg-white/5 hover:text-gray-300"
      } ${disabled ? "opacity-40 cursor-not-allowed" : ""}`}
    >
      {children}
    </button>
  );
}

export default function TerminalToolbar({
  canCopy,
  onCopy,
  onClear,
  isFullscreen,
  onToggleFullscreen,
  fileTransferOpen,
  onToggleFileTransfer,
  snippetOpen,
  onToggleSnippet,
}: TerminalToolbarProps) {
  return (
    <div className="flex items-center gap-0.5 shrink-0">
      <IconButton title="复制（有选区时）" disabled={!canCopy} onClick={onCopy}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
          <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
        </svg>
      </IconButton>

      <IconButton title="清屏" onClick={onClear}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="4 17 10 11 4 5" />
          <line x1="12" y1="19" x2="20" y2="19" />
        </svg>
      </IconButton>

      <IconButton title={isFullscreen ? "退出全屏 (Esc)" : "全屏"} active={isFullscreen} onClick={onToggleFullscreen}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M8 3H5a2 2 0 0 0-2 2v3" />
          <path d="M21 8V5a2 2 0 0 0-2-2h-3" />
          <path d="M3 16v3a2 2 0 0 0 2 2h3" />
          <path d="M16 21h3a2 2 0 0 0 2-2v-3" />
        </svg>
      </IconButton>

      <IconButton title={fileTransferOpen ? "收起文件传输" : "文件传输"} active={fileTransferOpen} onClick={onToggleFileTransfer}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
          <polyline points="7 10 12 15 17 10" />
          <line x1="12" y1="15" x2="12" y2="3" />
        </svg>
      </IconButton>

      <IconButton title={snippetOpen ? "收起排障脚本" : "排障脚本"} active={snippetOpen} onClick={onToggleSnippet}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
        </svg>
      </IconButton>
    </div>
  );
}
