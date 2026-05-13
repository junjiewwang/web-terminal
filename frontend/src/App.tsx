import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import HostList from "./components/HostList";
import AgentPanel from "./components/AgentPanel";
import TerminalView from "./components/TerminalView";
import TerminalTabs, {
  tabIdForHost,
  createTabForHost,
  type TerminalTab,
} from "./components/TerminalTabs";
import type { Host, AgentEvent, TerminalBackend } from "./services/api";
import {
  fetchHosts,
  fetchEventHistory,
  subscribeEvents,
  stopTerminal,
  fetchTerminals,
  startTerminal,
  fetchBackend,
  switchBackend,
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
  /** 全局 terminal backend（页面加载时从后端获取） */
  const [globalBackend, setGlobalBackend] = useState<TerminalBackend | null>(null);
  /** backend 切换进行中标志（防止重复点击） */
  const [backendSwitching, setBackendSwitching] = useState(false);

  const activeTab = tabs.find((t) => t.id === activeTabId) ?? null;
  const hostsRef = useRef<Host[]>([]);
  hostsRef.current = hosts;
  const tabsRef = useRef<TerminalTab[]>([]);
  tabsRef.current = tabs;
  const isAgentPanelOpenRef = useRef(false);

  const connectedHostIds = useMemo(() => new Set(tabs.map((t) => t.host.id)), [tabs]);
  const latestEvent = events.length > 0 ? events[events.length - 1] : null;
  const latestEventText = eventSummary(latestEvent);

  // Toast 通知：新事件到来时短暂显示，5 秒后自动消失
  const [showEventToast, setShowEventToast] = useState(false);
  const toastTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

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

  // 初始化全局 backend
  useEffect(() => {
    fetchBackend()
      .then(setGlobalBackend)
      .catch((err) => console.error("获取全局 backend 失败:", err));
  }, []);

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
        // 新事件到来时显示 toast
        setShowEventToast(true);
        if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
        toastTimerRef.current = setTimeout(() => setShowEventToast(false), 5000);
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
    const eventBackend = (event.data.backend as TerminalBackend) || undefined;
    const matchedHost = findHostByName(allHosts, event.host_name)
      ?? findHostByName(allHosts, targetNameFromInstance(instanceName));

    if (!matchedHost) return;

    const tabId = tabIdForHost(matchedHost);
    if (currentTabs.some((t) => t.id === tabId)) return;

    const newTab: TerminalTab = {
      ...createTabForHost(matchedHost),
      instanceName,
      wsUrl: sessionId ? `/ws/terminal/${sessionId}` : undefined,
      backend: eventBackend,
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
            backend: session.backend,
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

  /** 全局 backend 切换：通知后端停止所有会话，然后逐个 Tab 重新建立连接。
   *
   * 后端 PUT /api/terminal/backend 会：
   * 1. 更新 default_backend
   * 2. stop 所有现有会话
   * 前端收到响应后，逐个 Tab 调用 startTerminal 用新 backend 重建。
   */
  const handleGlobalBackendSwitch = useCallback(async () => {
    if (backendSwitching || !globalBackend) return;
    const newBackend: TerminalBackend = globalBackend === "tmux" ? "broker" : "tmux";

    setBackendSwitching(true);
    try {
      // 1. 通知后端切换（后端会 stop 所有会话）
      await switchBackend(newBackend);
      setGlobalBackend(newBackend);

      // 2. 逐个 Tab 重新建立连接
      const currentTabs = tabsRef.current;
      const reconnectResults = await Promise.allSettled(
        currentTabs.map((tab) => startTerminal(tab.host.id)),
      );

      // 3. 更新每个 Tab 的 wsUrl/backend/instanceName
      setTabs((prev) =>
        prev.map((tab, idx) => {
          const result = reconnectResults[idx];
          if (result.status === "fulfilled") {
            const inst = result.value;
            return {
              ...tab,
              instanceName: inst.instance_name,
              wsUrl: inst.ws_url,
              backend: inst.backend,
            };
          }
          // 重连失败的 Tab 清空 wsUrl，TerminalView 会显示 error 状态
          return { ...tab, wsUrl: undefined, backend: newBackend };
        }),
      );
    } catch (err) {
      console.error("全局 backend 切换失败:", err);
    } finally {
      setBackendSwitching(false);
    }
  }, [globalBackend, backendSwitching]);

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
  const sidebarSubline = activeTab
    ? `当前目标 · ${headerText}`
    : "Multi-hop SSH Workspace";

  return (
    <div className="relative flex h-screen overflow-hidden bg-[#050816] text-gray-100">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_top_left,rgba(16,185,129,0.16),transparent_34%),radial-gradient(circle_at_top_right,rgba(34,211,238,0.12),transparent_26%)]" />

      <aside className="relative z-10 flex w-[296px] flex-col border-r border-white/8 bg-gray-950/82 backdrop-blur-xl">
        <div className="border-b border-white/8 px-5 pb-4 pt-5">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl border border-emerald-400/20 bg-emerald-400/10 text-sm font-semibold text-emerald-300 shadow-[0_12px_28px_rgba(16,185,129,0.12)]">
              ⌘
            </div>
            <div className="min-w-0">
              <h1 className="text-lg font-semibold tracking-tight text-white">WebTerminal</h1>
              <p className="mt-1 truncate text-xs text-gray-500">{sidebarSubline}</p>
            </div>
          </div>

          <div className="mt-4 flex flex-wrap items-center gap-2 text-[11px] text-gray-500">
            <span className="inline-flex items-center rounded-full border border-white/8 bg-white/5 px-2.5 py-1">
              {tabs.length > 0 ? `${tabs.length} 个会话` : "等待连接"}
            </span>
            <span className="inline-flex items-center rounded-full border border-white/8 bg-white/5 px-2.5 py-1">
              {connectedHostIds.size > 0 ? `${connectedHostIds.size} 已连接` : "尚无活跃连接"}
            </span>
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

            <div className="ml-3 flex items-center gap-2 shrink-0">
              {/* 全局 Backend 切换按钮 */}
              {globalBackend && (
                <button
                  type="button"
                  onClick={handleGlobalBackendSwitch}
                  disabled={backendSwitching}
                  className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-2 text-xs font-medium transition-colors ${
                    globalBackend === "tmux"
                      ? "border-blue-500/30 bg-blue-500/10 text-blue-400 hover:border-blue-400/50 hover:bg-blue-500/20"
                      : "border-emerald-500/30 bg-emerald-500/10 text-emerald-400 hover:border-emerald-400/50 hover:bg-emerald-500/20"
                  } ${backendSwitching ? "opacity-50 cursor-wait" : ""}`}
                  title={`当前模式: ${globalBackend.toUpperCase()}，点击切换到 ${globalBackend === "tmux" ? "BROKER" : "TMUX"} 模式`}
                >
                  {backendSwitching ? (
                    <span className="inline-block h-3 w-3 animate-spin rounded-full border border-current border-t-transparent" />
                  ) : (
                    <span className="text-[10px]">⇄</span>
                  )}
                  <span>{globalBackend.toUpperCase()}</span>
                </button>
              )}

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
                  backend={tab.backend}
                  onInstanceNameUpdate={(instanceName) => handleInstanceNameUpdate(tab.id, instanceName)}
                />
              </div>
            ))
          )}

          {latestEventText && !isAgentPanelOpen && showEventToast && (
            <div
              role="button"
              tabIndex={0}
              onClick={() => setShowEventToast(false)}
              onKeyDown={(e) => e.key === "Escape" && setShowEventToast(false)}
              className="absolute right-4 top-4 z-20 hidden max-w-sm cursor-pointer rounded-2xl border border-white/10 bg-gray-950/88 px-3 py-2 shadow-2xl backdrop-blur transition-opacity duration-500 hover:border-emerald-400/30 md:block"
              title="点击关闭"
            >
              <div className="flex items-center gap-2 text-[11px] text-gray-500">
                <span className="inline-flex h-2 w-2 rounded-full bg-emerald-400" />
                最近轨迹
                <span className="ml-auto text-[10px] text-gray-600">点击关闭</span>
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
