import { useCallback } from "react";
import type { Host } from "../services/api";

// ── 终端 Tab 数据模型 ──────────────────────────────

/** 单个终端 Tab 信息 */
export interface TerminalTab {
  /** 唯一标识（host.id 或堡垒机 bastion.id + 二级主机 jump_host.id 组合） */
  id: string;
  /** 显示名称 */
  label: string;
  /** 关联的主机对象 */
  host: Host;
  /** 所属堡垒机名称（仅 jump_host 类型） */
  bastionName?: string;
  /** tmux 窗口名（仅 jump_host 类型） */
  tmuxWindow?: string;
  /** 该 Tab 对应的 tmux client TTY（socket.io 连接建立后获取） */
  clientTty?: string;
}

// ── 辅助函数 ──────────────────────────────────────

/** 根据 Host 对象生成 Tab ID */
export function tabIdForHost(host: Host): string {
  return `tab-${host.id}`;
}

/** 根据 Host 对象创建 TerminalTab */
export function createTabForHost(host: Host, bastionName?: string): TerminalTab {
  return {
    id: tabIdForHost(host),
    label: host.name,
    host,
    // bastionName: jump_host 填父堡垒机名；bastion 自身也填（用于 tmux switch API）
    // 但 bastion 自身不需要显示前缀，TerminalTabs 组件通过 host_type 判断
    bastionName: host.host_type === "jump_host" ? bastionName : host.host_type === "bastion" ? host.name : undefined,
    tmuxWindow: host.host_type === "jump_host"
      ? host.name
      : host.host_type === "bastion"
        ? "0" // 堡垒机默认窗口（window index 0）
        : undefined,
  };
}

// ── 组件 Props ────────────────────────────────────

interface TerminalTabsProps {
  tabs: TerminalTab[];
  activeTabId: string | null;
  onSelectTab: (tabId: string) => void;
  onCloseTab: (tabId: string) => void;
}

/**
 * 终端 Tab 栏组件
 *
 * 显示当前已打开的终端连接（多窗口），支持切换和关闭。
 * 类似浏览器 Tab 栏或 IDE 编辑器 Tab 的交互模式。
 *
 * 设计原则：
 * - 无 Tab 时不渲染（终端区域可使用完整高度）
 * - 活跃 Tab 用 emerald 底边框高亮
 * - jump_host Tab 显示堡垒机前缀，便于区分来源
 */
export default function TerminalTabs({
  tabs,
  activeTabId,
  onSelectTab,
  onCloseTab,
}: TerminalTabsProps) {
  const handleClose = useCallback(
    (e: React.MouseEvent, tabId: string) => {
      e.stopPropagation();
      onCloseTab(tabId);
    },
    [onCloseTab],
  );

  // 无 Tab 时不渲染
  if (tabs.length === 0) return null;

  return (
    <div className="flex items-stretch bg-gray-900 border-b border-gray-800 overflow-x-auto">
      {tabs.map((tab) => {
        const isActive = tab.id === activeTabId;
        return (
          <button
            key={tab.id}
            onClick={() => onSelectTab(tab.id)}
            className={`group flex items-center gap-1.5 px-3 py-1.5 text-xs border-r border-gray-800/50
              transition-colors whitespace-nowrap min-w-0 max-w-[180px]
              ${isActive
                ? "bg-gray-950 text-emerald-400 border-b-2 border-b-emerald-500"
                : "text-gray-500 hover:text-gray-300 hover:bg-gray-800/50"
              }
            `}
          >
            {/* Tab 类型图标 */}
            <span className="text-[10px] shrink-0">
              {tab.host.host_type === "jump_host" ? "🔗" : "🖥"}
            </span>

            {/* Tab 名称（仅 jump_host 显示 bastion 前缀） */}
            <span className="truncate">
              {tab.host.host_type === "jump_host" && tab.bastionName && (
                <span className="text-gray-600 mr-0.5">{tab.bastionName}/</span>
              )}
              {tab.label}
            </span>

            {/* 关闭按钮 */}
            <span
              onClick={(e) => handleClose(e, tab.id)}
              className={`shrink-0 w-4 h-4 flex items-center justify-center rounded
                text-[10px] transition-colors
                ${isActive
                  ? "text-gray-500 hover:text-red-400 hover:bg-gray-800"
                  : "text-transparent group-hover:text-gray-600 hover:!text-red-400 hover:!bg-gray-800"
                }
              `}
              title="关闭"
            >
              ✕
            </span>
          </button>
        );
      })}
    </div>
  );
}
