import { useState, useEffect, useCallback, useMemo } from "react";
import HostList from "./components/HostList";
import AgentPanel from "./components/AgentPanel";
import TerminalView from "./components/TerminalView";
import TerminalTabs, {
  tabIdForHost,
  createTabForHost,
  type TerminalTab,
} from "./components/TerminalTabs";
import type { Host, AgentEvent } from "./services/api";
import { fetchHosts, fetchEventHistory, subscribeEvents, stopTerminal } from "./services/api";

/**
 * 应用主布局：左侧主机列表 + 中间（Tab 栏 + 终端）+ 右侧 Agent 面板
 *
 * 状态管理：
 * - hosts: 主机列表（树形，bastion 含 children）
 * - tabs: 已打开的终端 Tab 列表
 * - activeTabId: 当前活跃的 Tab ID
 * - events: Agent 事件列表
 *
 * 多 Tab 独立终端模式：
 * - 每个 Tab 持有独立的 TerminalView 实例（独立的 socket.io 连接 + xterm.js）
 * - 切换 Tab 时非活跃的 TerminalView 保持连接不销毁
 * - Tab 切换时通过 tmux switch-client -c 只切换该 Tab 的 tmux client 视图，
 *   不影响其他 Tab 的终端内容
 */
export default function App() {
  const [hosts, setHosts] = useState<Host[]>([]);
  const [hostsError, setHostsError] = useState<string | null>(null);
  const [hostsLoading, setHostsLoading] = useState(true);
  const [events, setEvents] = useState<AgentEvent[]>([]);

  // ── Tab 状态 ──
  const [tabs, setTabs] = useState<TerminalTab[]>([]);
  const [activeTabId, setActiveTabId] = useState<string | null>(null);

  const activeTab = tabs.find((t) => t.id === activeTabId) ?? null;

  // 已连接的主机 ID 集合（从已打开的 Tab 推断）
  const connectedHostIds = useMemo(
    () => new Set(tabs.map((t) => t.host.id)),
    [tabs],
  );

  // ── 加载主机列表 ──
  const loadHosts = useCallback(() => {
    setHostsLoading(true);
    setHostsError(null);
    fetchHosts()
      .then((data) => {
        setHosts(data);
        setHostsLoading(false);
      })
      .catch((err) => {
        console.error("加载主机列表失败:", err);
        setHostsError(err instanceof Error ? err.message : "未知错误");
        setHostsLoading(false);
      });
  }, []);

  useEffect(() => {
    loadHosts();
  }, [loadHosts]);

  // ── SSE 事件订阅 + 历史事件加载 ──
  useEffect(() => {
    fetchEventHistory().then((history) => {
      if (history.length > 0) {
        setEvents((prev) => {
          const existingKeys = new Set(
            prev.map((e) => `${e.timestamp}-${e.event_type}`),
          );
          const newEvents = history.filter(
            (e) => !existingKeys.has(`${e.timestamp}-${e.event_type}`),
          );
          return [...newEvents, ...prev].slice(-100);
        });
      }
    });

    const cleanup = subscribeEvents((event) => {
      setEvents((prev) => [...prev.slice(-99), event]);
    });
    return cleanup;
  }, []);

  // ── 更新 Tab 数据中的 instanceName（终端启动后由 TerminalView 回调更新）──
  const handleInstanceNameUpdate = useCallback((tabId: string, instanceName: string) => {
    setTabs((prev) =>
      prev.map((t) => (t.id === tabId ? { ...t, bastionName: instanceName } : t)),
    );
  }, []);

  // ── Tab 切换（新架构下每个 Tab 有独立 PTY，不需要 tmux switch）──
  const handleTabSelect = useCallback(
    (tabId: string) => {
      setActiveTabId(tabId);
    },
    [],
  );
  const handleHostSelect = useCallback(
    (host: Host) => {
      const tabId = tabIdForHost(host);

      // 已有 Tab → 切换到它
      const existingTab = tabs.find((t) => t.id === tabId);
      if (existingTab) {
        setActiveTabId(tabId);
        return;
      }

      // 新建 Tab（不再传递 bastionName，等待 API 返回后由 onBastionNameUpdate 更新）
      const newTab = createTabForHost(host);
      setTabs((prev) => [...prev, newTab]);
      setActiveTabId(tabId);
    },
    [tabs],
  );

  // ── 关闭 Tab ──
  const handleTabClose = useCallback(
    (tabId: string) => {
      const closingTab = tabs.find((t) => t.id === tabId);
      if (closingTab) {
        // 关闭终端会话：使用 instanceName（bastionName 字段存储）
        const instanceName = closingTab.bastionName || closingTab.host.name;
        stopTerminal(instanceName).catch(() => {});
      }

      setTabs((prev) => {
        const remaining = prev.filter((t) => t.id !== tabId);

        if (tabId === activeTabId) {
          const closedIdx = prev.findIndex((t) => t.id === tabId);
          const nextTab =
            remaining[Math.min(closedIdx, remaining.length - 1)] ?? null;
          setActiveTabId(nextTab?.id ?? null);
        }

        return remaining;
      });
    },
    [tabs, activeTabId],
  );

  // ── 终端区域 header 信息 ──
  const headerText = activeTab
    ? activeTab.host.host_type === "jump_host" && activeTab.host.target_ip
      ? `${activeTab.bastionName?.split("--")[0] ?? "bastion"} → ${activeTab.host.name} (${activeTab.host.target_ip})`
      : `${activeTab.host.username}@${activeTab.host.hostname}:${activeTab.host.port}`
    : "请选择一个主机";

  return (
    <div className="flex h-screen bg-gray-950 text-gray-100">
      {/* 左侧：主机列表 */}
      <aside className="w-64 border-r border-gray-800 flex flex-col">
        <div className="p-4 border-b border-gray-800">
          <h1 className="text-lg font-bold text-emerald-400">
            MCP Terminal
          </h1>
          <p className="text-xs text-gray-500 mt-1">
            AI Agent SSH Terminal
          </p>
        </div>
        <HostList
          hosts={hosts}
          selectedHost={activeTab?.host ?? null}
          onSelect={handleHostSelect}
          loading={hostsLoading}
          error={hostsError}
          onRetry={loadHosts}
          connectedHostIds={connectedHostIds}
        />
      </aside>

      {/* 中间：Tab 栏 + 终端区域 */}
      <main className="flex-1 flex flex-col min-w-0">
        {/* header */}
        <header className="h-10 bg-gray-900 border-b border-gray-800 flex items-center px-4 shrink-0">
          <span className="text-sm text-gray-400 truncate">{headerText}</span>
        </header>

        {/* Tab 栏（无 Tab 时不渲染） */}
        <TerminalTabs
          tabs={tabs}
          activeTabId={activeTabId}
          onSelectTab={handleTabSelect}
          onCloseTab={handleTabClose}
        />

        {/* 多终端视图层叠（每个 Tab 一个 TerminalView，仅 active 的可见） */}
        <div className="flex-1 min-h-0 relative">
          {tabs.length === 0 ? (
            /* 无 Tab 空状态 */
            <div className="absolute inset-0 flex items-center justify-center text-gray-600">
              <div className="text-center">
                <div className="text-4xl mb-4">🖥</div>
                <p className="text-lg">选择左侧主机开始使用</p>
                <p className="text-sm mt-2 text-gray-700">
                  或通过 MCP Client 连接 Agent 工具
                </p>
              </div>
            </div>
          ) : (
            tabs.map((tab) => (
              <div
                key={tab.id}
                className="absolute inset-0"
                style={{ display: tab.id === activeTabId ? undefined : "none" }}
              >
                <TerminalView
                  host={tab.host}
                  isActive={tab.id === activeTabId}
                  onInstanceNameUpdate={(instanceName) =>
                    handleInstanceNameUpdate(tab.id, instanceName)
                  }
                />
              </div>
            ))
          )}
        </div>
      </main>

      {/* 右侧：Agent 面板 */}
      <aside className="w-80 border-l border-gray-800">
        <AgentPanel events={events} />
      </aside>
    </div>
  );
}
