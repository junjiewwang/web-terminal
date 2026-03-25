import { useState, useCallback } from "react";
import type { Host, HostType } from "../services/api";

interface HostListProps {
  hosts: Host[];
  selectedHost: Host | null;
  onSelect: (host: Host) => void;
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;
  /** 已连接的主机 ID 集合（用于显示连接状态指标） */
  connectedHostIds?: Set<number>;
}

// ── 主机类型 → 视觉配置 ──────────────────────────

const HOST_TYPE_CONFIG: Record<HostType, { icon: string; label: string }> = {
  direct: { icon: "🖥", label: "直连" },
  bastion: { icon: "🏰", label: "堡垒机" },
  jump_host: { icon: "🔗", label: "跳板" },
};

/**
 * 主机列表组件（树形结构）
 *
 * bastion 类型主机可展开/折叠，显示其下的二级主机（jump_host）。
 * 支持选中高亮、标签展示、加载状态和错误恢复。
 */
export default function HostList({
  hosts,
  selectedHost,
  onSelect,
  loading,
  error,
  onRetry,
  connectedHostIds,
}: HostListProps) {
  // 加载中
  if (loading) {
    return (
      <div className="p-4 text-sm text-gray-500 flex items-center gap-2">
        <span className="animate-spin inline-block w-3 h-3 border border-gray-600 border-t-emerald-400 rounded-full" />
        加载主机列表...
      </div>
    );
  }

  // 加载失败
  if (error) {
    return (
      <div className="p-4 text-sm text-gray-500">
        <p className="text-red-400 mb-2">加载失败</p>
        <p className="text-xs text-gray-600 mb-3">{error}</p>
        {onRetry && (
          <button
            onClick={onRetry}
            className="text-xs px-3 py-1 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded transition-colors"
          >
            重试
          </button>
        )}
      </div>
    );
  }

  // 空列表
  if (hosts.length === 0) {
    return (
      <div className="p-4 text-sm text-gray-500">
        暂无可用主机
        <br />
        <span className="text-xs">请在 config/hosts.yaml 中配置</span>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto">
      {hosts.map((host) => (
        <_HostItem
          key={host.id}
          host={host}
          selectedHost={selectedHost}
          onSelect={onSelect}
          depth={0}
          connectedHostIds={connectedHostIds}
        />
      ))}
    </div>
  );
}

// ── 单个主机项（支持递归渲染 children） ─────────────

interface _HostItemProps {
  host: Host;
  selectedHost: Host | null;
  onSelect: (host: Host) => void;
  depth: number;
  connectedHostIds?: Set<number>;
}

function _HostItem({ host, selectedHost, onSelect, depth, connectedHostIds }: _HostItemProps) {
  const [expanded, setExpanded] = useState(true);

  const isSelected = selectedHost?.id === host.id;
  const hasChildren = host.children && host.children.length > 0;
  const typeConfig = HOST_TYPE_CONFIG[host.host_type] ?? HOST_TYPE_CONFIG.direct;
  const isConnected = connectedHostIds?.has(host.id) ?? false;

  const toggleExpand = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      setExpanded((prev) => !prev);
    },
    [],
  );

  return (
    <>
      <button
        onClick={() => onSelect(host)}
        className={`w-full text-left border-b border-gray-800/50 transition-colors
          ${isSelected ? "bg-emerald-900/30 border-l-2 border-l-emerald-400" : "hover:bg-gray-900"}
        `}
        style={{ paddingLeft: `${depth * 16 + 16}px` }}
      >
        <div className="py-2.5 pr-3">
          {/* 第一行：展开箭头 + 类型图标 + 名称 */}
          <div className="flex items-center gap-1.5">
            {/* 展开/折叠箭头（仅 bastion 有 children 时显示） */}
            {hasChildren ? (
              <span
                onClick={toggleExpand}
                className="text-gray-600 hover:text-gray-400 cursor-pointer select-none text-[10px] w-3 text-center transition-transform"
                style={{ transform: expanded ? "rotate(90deg)" : "rotate(0deg)" }}
              >
                ▶
              </span>
            ) : (
              <span className="w-3" />
            )}
            <span className="text-xs" title={typeConfig.label}>
              {typeConfig.icon}
            </span>
            <span className="font-medium text-sm truncate">{host.name}</span>
            {/* 连接状态指标 */}
            {isConnected && (
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 shrink-0" title="已连接" />
            )}
            {/* bastion 子主机数量 badge */}
            {hasChildren && (
              <span className="text-[10px] px-1.5 py-0 bg-gray-800 text-gray-500 rounded-full ml-auto">
                {host.children.length}
              </span>
            )}
          </div>

          {/* 第二行：连接信息 */}
          <div className="text-xs text-gray-500 mt-1 ml-[18px]">
            {host.host_type === "jump_host" && host.target_ip
              ? `→ ${host.target_ip}`
              : `${host.username}@${host.hostname}:${host.port}`}
          </div>

          {/* 描述 */}
          {host.description && (
            <div className="text-[10px] text-gray-600 mt-0.5 ml-[18px] truncate">
              {host.description}
            </div>
          )}

          {/* 标签 */}
          {host.tags.length > 0 && (
            <div className="flex gap-1 mt-1 ml-[18px]">
              {host.tags.map((tag) => (
                <span
                  key={tag}
                  className="text-[10px] px-1.5 py-0.5 bg-gray-800 text-gray-400 rounded"
                >
                  {tag}
                </span>
              ))}
            </div>
          )}
        </div>
      </button>

      {/* 递归渲染子主机（折叠时隐藏） */}
      {hasChildren && expanded && (
        <div>
          {host.children.map((child) => (
            <_HostItem
              key={child.id}
              host={child}
              selectedHost={selectedHost}
              onSelect={onSelect}
              depth={depth + 1}
              connectedHostIds={connectedHostIds}
            />
          ))}
        </div>
      )}
    </>
  );
}
