import type { Host } from "../services/api";

interface HostListProps {
  hosts: Host[];
  selectedHost: Host | null;
  onSelect: (host: Host) => void;
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;
}

/**
 * 主机列表组件
 *
 * 展示所有可用的 SSH 主机，支持选中高亮、标签展示、
 * 加载状态和错误恢复。
 */
export default function HostList({
  hosts,
  selectedHost,
  onSelect,
  loading,
  error,
  onRetry,
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
      {hosts.map((host) => {
        const isSelected = selectedHost?.id === host.id;
        return (
          <button
            key={host.id}
            onClick={() => onSelect(host)}
            className={`w-full text-left px-4 py-3 border-b border-gray-800/50 transition-colors
              ${isSelected ? "bg-emerald-900/30 border-l-2 border-l-emerald-400" : "hover:bg-gray-900"}
            `}
          >
            <div className="flex items-center gap-2">
              <span className="text-emerald-400 text-xs">●</span>
              <span className="font-medium text-sm">{host.name}</span>
            </div>
            <div className="text-xs text-gray-500 mt-1 ml-4">
              {host.username}@{host.hostname}:{host.port}
            </div>
            {host.tags.length > 0 && (
              <div className="flex gap-1 mt-1 ml-4">
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
          </button>
        );
      })}
    </div>
  );
}
