import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import HostList from "./components/HostList";
import AgentPanel from "./components/AgentPanel";
import TerminalView from "./components/TerminalView";
import TerminalTabs, {
  tabIdForHost,
  createTabForHost,
  type TerminalTab,
} from "./components/TerminalTabs";
import type { Host, AgentEvent } from "./services/api";
import {
  fetchHosts,
  fetchEventHistory,
  subscribeEvents,
  stopTerminal,
  fetchTerminals,
} from "./services/api";

const EVENT_LABELS: Record<string, string> = {
  command_start: "执行命令",
  command_output: "命令输出",
  command_complete: "执行完成",
  command_error: "执行错误",
  session_created: "建立连接",
  session_closed: "断开连接",
  session_error: "连接失败",
  window_switched: "切换窗口",
};
const MAX_UNREAD_EVENTS = 99;

function findHostByName(hosts: Host[], name: string): Host | undefined {
  for (const host of hosts) {
    if (host.name === name) return host;
    const child = findHostByName(host.children ?? [], name);
    if (child) return child;
  }
  return undefined;
}

function targetNameFromInstance(instanceName: string): string {
  const parts = instanceName.split("--");
  return parts[parts.length - 1] || instanceName;
}

function headerForTab(tab: TerminalTab | null): string {
  if (!tab) return "请选择一个主机";
  if (tab.instanceName) {
    return tab.instanceName.replaceAll("--", " → ");
  }
  return `${tab.host.username}@${tab.host.hostname}:${tab.host.port}`;
}

function eventSummary(event: AgentEvent | null): string | null {
  if (!event) return null;
  const label = EVENT_LABELS[event.event_type] ?? event.event_type;
  if (event.data.command != null) {
    return `${label} · ${String(event.data.command)}`;
  }
  return `${label} · ${event.host_name}`;
}

export default function App() {
  const [hosts, setHosts] = useState<Host[]>([]);
  const [hostsError, setHostsError] = useState<string | null>(null);
  const [hostsLoading, setHostsLoading] = useState(true);
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [tabs, setTabs] = useState<TerminalTab[]>([]);
  const [activeTabId, setActiveTabId] = useState<string | null>(null);
  const [isAgentPanelOpen, setIsAgentPanelOpen] = useState(false);
  const [unreadEventCount, setUnreadEventCount] = useState(0);

  const activeTab = tabs.find((t) => t.id === activeTabId) ?? null;
  const hostsRef = useRef<Host[]>([]);
  hostsRef.current = hosts;
  const tabsRef = useRef<TerminalTab[]>([]);
  tabsRef.current = tabs;
  const isAgentPanelOpenRef = useRef(false);

  const connectedHostIds = useMemo(() => new Set(tabs.map((t) => t.host.id)), [tabs]);
  const latestEvent = events.length > 0 ? events[events.length - 1] : null;
  const latestEventText = eventSummary(latestEvent);

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

  useEffect(() => {
    isAgentPanelOpenRef.current = isAgentPanelOpen;
    if (isAgentPanelOpen) {
      setUnreadEventCount(0);
    }
  }, [isAgentPanelOpen]);

  useEffect(() => {
    if (!isAgentPanelOpen) return undefined;
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setIsAgentPanelOpen(false);
      }
    };
    window.addEventListener("keydown", handleEscape);
    return () => window.removeEventListener("keydown", handleEscape);
  }, [isAgentPanelOpen]);

  useEffect(() => {
    fetchEventHistory().then((history) => {
      if (history.length > 0) {
        setEvents((prev) => {
          const existingKeys = new Set(prev.map((e) => `${e.timestamp}-${e.event_type}`));
          const newEvents = history.filter((e) => !existingKeys.has(`${e.timestamp}-${e.event_type}`));
          return [...newEvents, ...prev].slice(-100);
        });
      }
    });

    const cleanup = subscribeEvents((event) => {
      setEvents((prev) => [...prev.slice(-99), event]);
      if (!isAgentPanelOpenRef.current) {
        setUnreadEventCount((prev) => Math.min(prev + 1, MAX_UNREAD_EVENTS));
      }
      if (event.event_type === "session_created") {
        _handleSessionCreated(event);
      } else if (event.event_type === "session_closed") {
        _handleSessionClosed(event);
      }
    });
    return cleanup;
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function _handleSessionCreated(event: AgentEvent) {
    const allHosts = hostsRef.current;
    const currentTabs = tabsRef.current;
    const sessionId = event.session_id;
    const instanceName = (event.data.instance_name as string) || event.host_name;
    const matchedHost = findHostByName(allHosts, event.host_name)
      ?? findHostByName(allHosts, targetNameFromInstance(instanceName));

    if (!matchedHost) return;

    const tabId = tabIdForHost(matchedHost);
    if (currentTabs.some((t) => t.id === tabId)) return;

    const newTab: TerminalTab = {
      ...createTabForHost(matchedHost),
      instanceName,
      wsUrl: sessionId ? `/ws/terminal/${sessionId}` : undefined,
    };

    setTabs((prev) => [...prev, newTab]);
    setActiveTabId((prev) => prev ?? tabId);
  }

  function _handleSessionClosed(event: AgentEvent) {
    const target = event.host_name;
    setTabs((prev) => {
      const closingTab = prev.find((t) => t.host.name === target || t.instanceName === target);
      if (!closingTab) return prev;

      const remaining = prev.filter((t) => t.id !== closingTab.id);
      setActiveTabId((currentId) => {
        if (currentId !== closingTab.id) return currentId;
        const closedIdx = prev.findIndex((t) => t.id === closingTab.id);
        const nextTab = remaining[Math.min(closedIdx, remaining.length - 1)] ?? null;
        return nextTab?.id ?? null;
      });
      return remaining;
    });
  }

  useEffect(() => {
    if (hostsLoading || hosts.length === 0) return;

    fetchTerminals().then((sessions) => {
      if (sessions.length === 0) return;

      setTabs((prev) => {
        const existingIds = new Set(prev.map((t) => t.id));
        const newTabs: TerminalTab[] = [];

        for (const session of sessions) {
          if (!session.running) continue;
          const matchedHost = findHostByName(hosts, targetNameFromInstance(session.instance_name));
          if (!matchedHost) continue;

          const tabId = tabIdForHost(matchedHost);
          if (existingIds.has(tabId)) continue;
          existingIds.add(tabId);

          newTabs.push({
            ...createTabForHost(matchedHost),
            instanceName: session.instance_name,
            wsUrl: session.ws_url,
          });
        }

        if (newTabs.length === 0) return prev;
        setActiveTabId((currentId) => currentId ?? newTabs[0].id);
        return [...prev, ...newTabs];
      });
    });
  }, [hostsLoading, hosts]);

  const handleInstanceNameUpdate = useCallback((tabId: string, instanceName: string) => {
    setTabs((prev) => prev.map((t) => (t.id === tabId ? { ...t, instanceName } : t)));
  }, []);

  const handleTabSelect = useCallback((tabId: string) => {
    setActiveTabId(tabId);
  }, []);

  const handleHostSelect = useCallback((host: Host) => {
    const tabId = tabIdForHost(host);
    setTabs((prev) => {
      if (prev.some((t) => t.id === tabId)) return prev;
      return [...prev, createTabForHost(host)];
    });
    setActiveTabId(tabId);
  }, []);

  const handleTabClose = useCallback((tabId: string) => {
    const closingTab = tabs.find((t) => t.id === tabId);
    if (closingTab) {
      stopTerminal(closingTab.instanceName || closingTab.host.name).catch(() => {});
    }

    setTabs((prev) => {
      const remaining = prev.filter((t) => t.id !== tabId);
      if (tabId === activeTabId) {
        const closedIdx = prev.findIndex((t) => t.id === tabId);
        const nextTab = remaining[Math.min(closedIdx, remaining.length - 1)] ?? null;
        setActiveTabId(nextTab?.id ?? null);
      }
      return remaining;
    });
  }, [tabs, activeTabId]);

  const headerText = headerForTab(activeTab);

  return (
    <div className="relative flex h-screen overflow-hidden bg-[#050816] text-gray-100">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_top_left,rgba(16,185,129,0.16),transparent_34%),radial-gradient(circle_at_top_right,rgba(34,211,238,0.12),transparent_26%)]" />

      <aside className="relative z-10 flex w-72 flex-col border-r border-white/8 bg-gray-950/80 backdrop-blur-xl">
        <div className="border-b border-white/8 px-5 py-5">
          <p className="text-[11px] font-medium uppercase tracking-[0.28em] text-emerald-300/70">
            Workspace
          </p>
          <h1 className="mt-2 text-xl font-semibold tracking-tight text-white">WebTerminal</h1>
          <p className="mt-2 text-xs text-gray-500">Multi-hop SSH Workspace</p>

          <div className="mt-4 rounded-2xl border border-white/8 bg-white/4 px-3 py-2 shadow-[0_12px_40px_rgba(0,0,0,0.22)]">
            <div className="flex items-center justify-between text-[11px] text-gray-500">
              <span>当前工作区</span>
              <span>{tabs.length > 0 ? `${tabs.length} 个会话` : "等待连接"}</span>
            </div>
            <p className="mt-1 truncate text-xs text-gray-300">
              {activeTab ? headerText : "选择左侧节点开始连接"}
            </p>
          </div>
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

      <main className="relative z-10 flex min-w-0 flex-1 flex-col">
        <header className="h-14 shrink-0 border-b border-white/8 bg-gray-950/60 px-5 backdrop-blur-xl">
          <div className="flex h-full items-center justify-between gap-4">
            <div className="min-w-0">
              <p className="text-[11px] font-medium uppercase tracking-[0.28em] text-emerald-300/70">
                WebTerminal
              </p>
              <div className="mt-1 flex items-center gap-2">
                <span className="truncate text-sm font-medium text-gray-200">{headerText}</span>
                {activeTab && (
                  <span className="hidden rounded-full border border-white/8 bg-white/5 px-2 py-0.5 text-[10px] text-gray-400 sm:inline-flex">
                    {activeTab.host.host_type === "nested" ? "多跳节点" : "首跳节点"}
                  </span>
                )}
              </div>
            </div>

            <button
              type="button"
              onClick={() => setIsAgentPanelOpen((prev) => !prev)}
              className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/6 px-3 py-2 text-xs font-medium text-gray-200 transition-colors hover:border-emerald-400/40 hover:bg-emerald-400/10 hover:text-white"
            >
              <span>{isAgentPanelOpen ? "隐藏轨迹" : "操作轨迹"}</span>
              {unreadEventCount > 0 && (
                <span className="inline-flex min-w-5 items-center justify-center rounded-full bg-emerald-400 px-1.5 py-0.5 text-[10px] font-semibold text-gray-950">
                  {unreadEventCount}
                </span>
              )}
            </button>
          </div>
        </header>

        <TerminalTabs
          tabs={tabs}
          activeTabId={activeTabId}
          onSelectTab={handleTabSelect}
          onCloseTab={handleTabClose}
        />

        <div className="relative min-h-0 flex-1 bg-[#040712]">
          {tabs.length === 0 ? (
            <div className="absolute inset-0 flex items-center justify-center text-gray-600">
              <div className="max-w-md text-center">
                <div className="mb-4 text-5xl text-emerald-300/80">⌘_</div>
                <p className="text-lg font-medium text-gray-200">选择左侧主机开始使用</p>
                <p className="mt-2 text-sm text-gray-500">也可由 Agent 自动创建会话后在这里接管</p>
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
                  initialWsUrl={tab.wsUrl}
                  onInstanceNameUpdate={(instanceName) => handleInstanceNameUpdate(tab.id, instanceName)}
                />
              </div>
            ))
          )}

          {latestEventText && !isAgentPanelOpen && (
            <div className="pointer-events-none absolute right-4 top-4 z-20 hidden max-w-sm rounded-2xl border border-white/10 bg-gray-950/88 px-3 py-2 shadow-2xl backdrop-blur md:block">
              <div className="flex items-center gap-2 text-[11px] text-gray-500">
                <span className="inline-flex h-2 w-2 rounded-full bg-emerald-400" />
                最近轨迹
              </div>
              <p className="mt-1 truncate text-xs text-gray-300">{latestEventText}</p>
            </div>
          )}
        </div>
      </main>

      {isAgentPanelOpen && (
        <button
          type="button"
          className="absolute inset-0 z-20 bg-black/30 backdrop-blur-[1px]"
          aria-label="关闭操作轨迹面板"
          onClick={() => setIsAgentPanelOpen(false)}
        />
      )}

      <aside
        className={`absolute inset-y-0 right-0 z-30 w-[380px] max-w-[92vw] border-l border-white/10 bg-gray-950/92 shadow-[-24px_0_60px_rgba(0,0,0,0.35)] backdrop-blur-xl transition-transform duration-300 ${
          isAgentPanelOpen ? "translate-x-0" : "translate-x-full"
        }`}
      >
        <AgentPanel events={events} onClose={() => setIsAgentPanelOpen(false)} />
      </aside>
    </div>
  );
}
