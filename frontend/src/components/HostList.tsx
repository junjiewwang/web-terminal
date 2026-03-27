import { useState, useCallback, useMemo, useRef, useEffect } from "react";
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

// ── 搜索匹配工具 ──────────────────────────────────

/** 判断主机是否匹配搜索词（名称/IP/描述/标签，大小写不敏感） */
function hostMatchesQuery(host: Host, query: string): boolean {
  const q = query.toLowerCase();
  if (host.name.toLowerCase().includes(q)) return true;
  if (host.hostname.toLowerCase().includes(q)) return true;
  if (host.target_ip?.toLowerCase().includes(q)) return true;
  if (host.description?.toLowerCase().includes(q)) return true;
  if (host.tags.some((t) => t.toLowerCase().includes(q))) return true;
  return false;
}

/** 判断 bastion 的 children 中是否有匹配项 */
function bastionChildrenMatch(host: Host, query: string): boolean {
  return host.children?.some((c) => hostMatchesQuery(c, query)) ?? false;
}

/**
 * 主机列表组件（树形结构 + 搜索过滤）
 *
 * 功能：
 * - 搜索：按名称/IP/描述/标签模糊匹配，实时过滤
 * - 树形：bastion 可展开/折叠显示 jump_host 子主机
 * - 状态：已连接主机显示绿色指示器
 * - 统计：显示主机总数/已连接数/搜索匹配数
 * - 快捷键：Ctrl+K / Cmd+K 聚焦搜索框
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
  const [query, setQuery] = useState("");
  const searchRef = useRef<HTMLInputElement>(null);

  // ── 快捷键 Ctrl+K 聚焦搜索 ──
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

  // ── 搜索过滤 ──
  const filteredHosts = useMemo(() => {
    if (!query.trim()) return hosts;
    return hosts.filter(
      (h) => hostMatchesQuery(h, query) || bastionChildrenMatch(h, query),
    );
  }, [hosts, query]);

  // ── 统计 ──
  const totalCount = useMemo(() => {
    let n = hosts.length;
    for (const h of hosts) n += h.children?.length ?? 0;
    return n;
  }, [hosts]);

  const connectedCount = connectedHostIds?.size ?? 0;

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
    <div className="flex-1 flex flex-col min-h-0">
      {/* 搜索栏（固定在顶部） */}
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
          {/* 搜索图标 */}
          <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-600 text-xs">
            ⌕
          </span>
          {/* 清除按钮 */}
          {query && (
            <button
              onClick={() => setQuery("")}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-600
                hover:text-gray-400 text-xs transition-colors"
              title="清除搜索"
            >
              ✕
            </button>
          )}
          {/* 快捷键提示（无搜索词时显示） */}
          {!query && (
            <span className="absolute right-2 top-1/2 -translate-y-1/2 text-[10px] text-gray-700">
              ⌘K
            </span>
          )}
        </div>
        {/* 统计栏 */}
        <div className="flex items-center gap-2 mt-1.5 text-[10px] text-gray-600">
          <span>{totalCount} 台主机</span>
          {connectedCount > 0 && (
            <span className="flex items-center gap-1">
              <span className="w-1 h-1 rounded-full bg-emerald-500 inline-block" />
              {connectedCount} 已连接
            </span>
          )}
          {query && (
            <span className="ml-auto text-gray-500">
              {filteredHosts.length === 0
                ? "无匹配"
                : `${filteredHosts.length} 项匹配`}
            </span>
          )}
        </div>
      </div>

      {/* 主机列表（可滚动） */}
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

// ── 单个主机项（支持递归渲染 children + 搜索高亮） ──

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
  const typeConfig = HOST_TYPE_CONFIG[host.host_type] ?? HOST_TYPE_CONFIG.direct;
  const isConnected = connectedHostIds?.has(host.id) ?? false;

  // 搜索时过滤子主机
  const filteredChildren = useMemo(() => {
    if (!searchQuery?.trim() || !hasChildren) return host.children ?? [];
    return (host.children ?? []).filter((c) => hostMatchesQuery(c, searchQuery));
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
          {/* 第一行：展开箭头 + 类型图标 + 名称 + 状态 */}
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
            <span className="text-xs shrink-0" title={typeConfig.label}>
              {typeConfig.icon}
            </span>
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

          {/* 第二行：连接信息（紧凑） */}
          <div className="text-[11px] text-gray-500 mt-0.5 ml-[18px] truncate">
            {host.host_type === "jump_host" && host.target_ip ? (
              <>
                → <_Highlight text={host.target_ip} query={searchQuery} />
              </>
            ) : (
              <_Highlight
                text={`${host.username}@${host.hostname}:${host.port}`}
                query={searchQuery}
              />
            )}
            {host.description && (
              <span className="text-gray-600 ml-1.5">
                · <_Highlight text={host.description} query={searchQuery} />
              </span>
            )}
          </div>

          {/* 标签（内联紧凑排列） */}
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

      {/* 递归渲染子主机 */}
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

// ── 搜索高亮组件 ──────────────────────────────────

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
