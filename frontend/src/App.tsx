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

export default function App() {
  const [hosts, setHosts] = useState<Host[]>([]);
  const [hostsError, setHostsError] = useState<string | null>(null);
  const [hostsLoading, setHostsLoading] = useState(true);
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [tabs, setTabs] = useState<TerminalTab[]>([]);
  const [activeTabId, setActiveTabId] = useState<string | null>(null);

  const activeTab = tabs.find((t) => t.id === activeTabId) ?? null;
  const hostsRef = useRef<Host[]>([]);
  hostsRef.current = hosts;
  const tabsRef = useRef<TerminalTab[]>([]);
  tabsRef.current = tabs;

  const connectedHostIds = useMemo(() => new Set(tabs.map((t) => t.host.id)), [tabs]);

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
    <div className="flex h-screen bg-gray-950 text-gray-100">
      <aside className="w-64 border-r border-gray-800 flex flex-col">
        <div className="p-4 border-b border-gray-800">
          <h1 className="text-lg font-bold text-emerald-400">MCP Terminal</h1>
          <p className="text-xs text-gray-500 mt-1">AI Agent SSH Terminal</p>
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

      <main className="flex-1 flex flex-col min-w-0">
        <header className="h-10 bg-gray-900 border-b border-gray-800 flex items-center px-4 shrink-0">
          <span className="text-sm text-gray-400 truncate">{headerText}</span>
        </header>

        <TerminalTabs
          tabs={tabs}
          activeTabId={activeTabId}
          onSelectTab={handleTabSelect}
          onCloseTab={handleTabClose}
        />

        <div className="flex-1 min-h-0 relative">
          {tabs.length === 0 ? (
            <div className="absolute inset-0 flex items-center justify-center text-gray-600">
              <div className="text-center">
                <div className="text-4xl mb-4">🖥</div>
                <p className="text-lg">选择左侧主机开始使用</p>
                <p className="text-sm mt-2 text-gray-700">或通过 MCP Client 连接 Agent 工具</p>
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
        </div>
      </main>

      <aside className="w-80 border-l border-gray-800">
        <AgentPanel events={events} />
      </aside>
    </div>
  );
}
