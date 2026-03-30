import { useState, useCallback, useMemo, useRef, useEffect } from "react";
import type { Host, HostType } from "../services/api";

interface HostListProps {
  hosts: Host[];
  selectedHost: Host | null;
  onSelect: (host: Host) => void;
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;
  connectedHostIds?: Set<number>;
}

const HOST_TYPE_CONFIG: Record<HostType, { icon: string; label: string }> = {
  root: { icon: "🖥", label: "根节点" },
  nested: { icon: "🔗", label: "嵌套节点" },
};

function entryDisplay(host: Host): string {
  if (host.host_type === "root") {
    return `${host.username}@${host.hostname}:${host.port}`;
  }
  const entry = host.entry;
  if (entry.type === "menu_send") return `→ ${entry.value ?? ""}`;
  if (entry.type === "ssh_command") return `$ ${entry.value ?? ""}`;
  return host.name;
}

function hostMatchesQuery(host: Host, query: string): boolean {
  const q = query.toLowerCase();
  if (host.name.toLowerCase().includes(q)) return true;
  if (host.hostname.toLowerCase().includes(q)) return true;
  if (entryDisplay(host).toLowerCase().includes(q)) return true;
  if (host.description?.toLowerCase().includes(q)) return true;
  if (host.tags.some((t) => t.toLowerCase().includes(q))) return true;
  return host.children?.some((child) => hostMatchesQuery(child, query)) ?? false;
}

function countHosts(hosts: Host[]): number {
  return hosts.reduce((sum, host) => sum + 1 + countHosts(host.children ?? []), 0);
}

export default function HostList({
  hosts,
  selectedHost,
  onSelect,
  loading,
  error,
  onRetry,
  connectedHostIds,
}: HostListProps) {
  const [query, setQuery] = useState("");
  const searchRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        searchRef.current?.focus();
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  const filteredHosts = useMemo(() => {
    if (!query.trim()) return hosts;
    return hosts.filter((host) => hostMatchesQuery(host, query));
  }, [hosts, query]);

  const totalCount = useMemo(() => countHosts(hosts), [hosts]);
  const connectedCount = connectedHostIds?.size ?? 0;

  if (loading) {
    return (
      <div className="p-4 text-sm text-gray-500 flex items-center gap-2">
        <span className="animate-spin inline-block w-3 h-3 border border-gray-600 border-t-emerald-400 rounded-full" />
        加载主机列表...
      </div>
    );
  }

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

  if (hosts.length === 0) {
    return (
      <div className="p-4 text-sm text-gray-500">
        暂无可用主机
        <br />
        <span className="text-xs">请在 `config/hosts.yaml` 中配置</span>
      </div>
    );
  }

  return (
    <div className="flex-1 flex flex-col min-h-0">
      <div className="px-3 py-2 border-b border-gray-800/50 shrink-0">
        <div className="relative">
          <input
            ref={searchRef}
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索主机..."
            className="w-full bg-gray-900 text-sm text-gray-300 rounded px-3 py-1.5 pl-7
              border border-gray-800 focus:border-emerald-700 focus:outline-none
              placeholder:text-gray-600 transition-colors"
          />
          <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-600 text-xs">⌕</span>
          {query && (
            <button
              onClick={() => setQuery("")}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-600 hover:text-gray-400 text-xs transition-colors"
              title="清除搜索"
            >
              ✕
            </button>
          )}
          {!query && (
            <span className="absolute right-2 top-1/2 -translate-y-1/2 text-[10px] text-gray-700">
              ⌘K
            </span>
          )}
        </div>
        <div className="flex items-center gap-2 mt-1.5 text-[10px] text-gray-600">
          <span>{totalCount} 个节点</span>
          {connectedCount > 0 && (
            <span className="flex items-center gap-1">
              <span className="w-1 h-1 rounded-full bg-emerald-500 inline-block" />
              {connectedCount} 已连接
            </span>
          )}
          {query && (
            <span className="ml-auto text-gray-500">
              {filteredHosts.length === 0 ? "无匹配" : `${filteredHosts.length} 项匹配`}
            </span>
          )}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto host-list-scroll">
        {filteredHosts.length === 0 && query ? (
          <div className="p-4 text-center text-sm text-gray-600">
            <div className="text-2xl mb-2">🔍</div>
            未找到匹配「{query}」的主机
          </div>
        ) : (
          filteredHosts.map((host) => (
            <_HostItem
              key={host.id}
              host={host}
              selectedHost={selectedHost}
              onSelect={onSelect}
              depth={0}
              connectedHostIds={connectedHostIds}
              searchQuery={query}
            />
          ))
        )}
      </div>
    </div>
  );
}

interface _HostItemProps {
  host: Host;
  selectedHost: Host | null;
  onSelect: (host: Host) => void;
  depth: number;
  connectedHostIds?: Set<number>;
  searchQuery?: string;
}

function _HostItem({
  host,
  selectedHost,
  onSelect,
  depth,
  connectedHostIds,
  searchQuery,
}: _HostItemProps) {
  const [expanded, setExpanded] = useState(true);

  const isSelected = selectedHost?.id === host.id;
  const hasChildren = host.children && host.children.length > 0;
  const typeConfig = HOST_TYPE_CONFIG[host.host_type] ?? HOST_TYPE_CONFIG.root;
  const isConnected = connectedHostIds?.has(host.id) ?? false;

  const filteredChildren = useMemo(() => {
    if (!searchQuery?.trim() || !hasChildren) return host.children ?? [];
    return (host.children ?? []).filter((child) => hostMatchesQuery(child, searchQuery));
  }, [host.children, hasChildren, searchQuery]);

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
        className={`w-full text-left transition-colors group
          ${isSelected
            ? "bg-emerald-900/30 border-l-2 border-l-emerald-400"
            : "hover:bg-gray-900/80 border-l-2 border-l-transparent"
          }
        `}
        style={{ paddingLeft: `${depth * 14 + 12}px` }}
      >
        <div className="py-2 pr-3">
          <div className="flex items-center gap-1.5">
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
            <span className="text-xs shrink-0" title={typeConfig.label}>{typeConfig.icon}</span>
            <span className="font-medium text-sm truncate">
              <_Highlight text={host.name} query={searchQuery} />
            </span>
            {isConnected && (
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 shrink-0 animate-pulse" title="已连接" />
            )}
            {hasChildren && (
              <span className="text-[10px] px-1.5 bg-gray-800 text-gray-500 rounded-full ml-auto shrink-0">
                {filteredChildren.length}
              </span>
            )}
          </div>

          <div className="text-[11px] text-gray-500 mt-0.5 ml-[18px] truncate">
            <_Highlight text={entryDisplay(host)} query={searchQuery} />
            {host.description && (
              <span className="text-gray-600 ml-1.5">
                · <_Highlight text={host.description} query={searchQuery} />
              </span>
            )}
          </div>

          {host.tags.length > 0 && (
            <div className="flex flex-wrap gap-1 mt-1 ml-[18px]">
              {host.tags.map((tag) => (
                <span
                  key={tag}
                  className="text-[10px] px-1 py-0 bg-gray-800/80 text-gray-500 rounded"
                >
                  <_Highlight text={tag} query={searchQuery} />
                </span>
              ))}
            </div>
          )}
        </div>
      </button>

      {hasChildren && expanded && (
        <div>
          {filteredChildren.map((child) => (
            <_HostItem
              key={child.id}
              host={child}
              selectedHost={selectedHost}
              onSelect={onSelect}
              depth={depth + 1}
              connectedHostIds={connectedHostIds}
              searchQuery={searchQuery}
            />
          ))}
        </div>
      )}
    </>
  );
}

function _Highlight({ text, query }: { text: string; query?: string }) {
  if (!query?.trim()) return <>{text}</>;

  const idx = text.toLowerCase().indexOf(query.toLowerCase());
  if (idx === -1) return <>{text}</>;

  return (
    <>
      {text.slice(0, idx)}
      <span className="text-emerald-400 font-semibold">
        {text.slice(idx, idx + query.length)}
      </span>
      {text.slice(idx + query.length)}
    </>
  );
}
