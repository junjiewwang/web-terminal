import { useCallback } from "react";
import type { Host } from "../services/api";

// ── 终端 Tab 数据模型 ──────────────────────────────

export interface TerminalTab {
  id: string;
  label: string;
  host: Host;
  /** 完整实例名，格式为 root--child--target */
  instanceName?: string;
  /** Agent 已创建会话时的 WebSocket URL（跳过 startTerminal） */
  wsUrl?: string;
}

export function tabIdForHost(host: Host): string {
  return `tab-${host.id}`;
}

export function createTabForHost(host: Host): TerminalTab {
  return {
    id: tabIdForHost(host),
    label: host.name,
    host,
    instanceName: host.host_type === "root" ? host.name : undefined,
  };
}

interface TerminalTabsProps {
  tabs: TerminalTab[];
  activeTabId: string | null;
  onSelectTab: (tabId: string) => void;
  onCloseTab: (tabId: string) => void;
}

function _instancePrefix(instanceName?: string, label?: string): string | null {
  if (!instanceName) return null;
  const parts = instanceName.split("--");
  if (parts.length <= 1) return null;
  const prefix = parts.slice(0, -1).join("/");
  return prefix === label ? null : prefix;
}

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

  if (tabs.length === 0) return null;

  return (
    <div className="flex items-stretch bg-gray-900 border-b border-gray-800 overflow-x-auto">
      {tabs.map((tab) => {
        const isActive = tab.id === activeTabId;
        const prefix = _instancePrefix(tab.instanceName, tab.label);
        return (
          <button
            key={tab.id}
            onClick={() => onSelectTab(tab.id)}
            className={`group flex items-center gap-1.5 px-3 py-1.5 text-xs border-r border-gray-800/50
              transition-colors whitespace-nowrap min-w-0 max-w-[220px]
              ${isActive
                ? "bg-gray-950 text-emerald-400 border-b-2 border-b-emerald-500"
                : "text-gray-500 hover:text-gray-300 hover:bg-gray-800/50"
              }
            `}
          >
            <span className="text-[10px] shrink-0">
              {tab.host.host_type === "nested" ? "🔗" : "🖥"}
            </span>

            <span className="truncate">
              {prefix && (
                <span className="text-gray-600 mr-0.5">{prefix}/</span>
              )}
              {tab.label}
            </span>

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
