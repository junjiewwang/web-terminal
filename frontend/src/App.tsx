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
import { fetchHosts, fetchEventHistory, subscribeEvents, stopTerminal, fetchTerminals } from "./services/api";

/**
 * 应用主布局：左侧主机列表 + 中间（Tab 栏 + 终端）+ 右侧 Agent 面板
 *
 * 状态管理：
 * - hosts: 主机列表（树形，bastion 含 children）
 * - tabs: 已打开的终端 Tab 列表
 * - activeTabId: 当前活跃的 Tab ID
 * - events: Agent 事件列表
 *
 * Agent ↔ 浏览器状态同步（SSE 事件驱动）：
 * - session_created: Agent 通过 MCP 连接主机后，前端自动创建 Tab + WebSocket
 * - session_closed: Agent 断开连接后，前端自动移除对应 Tab
 * - 页面加载时一次性轮询 /api/terminal 同步已有会话（覆盖 Agent 先于浏览器连接的场景）
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

  // 用 ref 追踪最新的 hosts/tabs（SSE 回调中需要访问最新值，避免闭包陈旧问题）
  const hostsRef = useRef<Host[]>([]);
  hostsRef.current = hosts;
  const tabsRef = useRef<TerminalTab[]>([]);
  tabsRef.current = tabs;

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

  // ── SSE 事件订阅 + 历史事件加载 + Agent 操作联动 ──
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

      // ── SSE 事件驱动 UI 联动 ──
      if (event.event_type === "session_created") {
        _handleSessionCreated(event);
      } else if (event.event_type === "session_closed") {
        _handleSessionClosed(event);
      }
    });
    return cleanup;
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /**
   * Agent session_created → 自动匹配主机并创建 Tab
   *
   * 查找策略：
   * 1. 从 event.host_name 匹配顶层主机（direct/bastion）
   * 2. 从 event.host_name 匹配二级主机（jump_host，在 bastion.children 中）
   * 3. 从 event.data.instance_name 匹配（兜底，覆盖 bastion--jump_host 格式）
   */
  function _handleSessionCreated(event: AgentEvent) {
    const allHosts = hostsRef.current;
    const currentTabs = tabsRef.current;
    const hostName = event.host_name;
    const sessionId = event.session_id;
    const instanceName = (event.data.instance_name as string) || "";

    // 查找匹配的 Host 对象
    let matchedHost: Host | undefined;

    // 策略1：顶层主机名匹配
    matchedHost = allHosts.find((h) => h.name === hostName);

    // 策略2：二级主机（在 bastion.children 中查找）
    if (!matchedHost) {
      for (const h of allHosts) {
        if (h.children?.length) {
          const child = h.children.find((c) => c.name === hostName);
          if (child) {
            matchedHost = child;
            break;
          }
        }
      }
    }

    if (!matchedHost) return;

    // 检查是否已有对应 Tab（避免重复创建）
    const tabId = tabIdForHost(matchedHost);
    if (currentTabs.some((t) => t.id === tabId)) return;

    // 构造 wsUrl
    const wsUrl = sessionId ? `/ws/terminal/${sessionId}` : undefined;

    // 创建新 Tab（携带 wsUrl，TerminalView 将直接连接 WebSocket 而非调 startTerminal）
    const newTab: TerminalTab = {
      ...createTabForHost(matchedHost),
      bastionName: instanceName || matchedHost.name,
      wsUrl,
    };

    setTabs((prev) => [...prev, newTab]);
    // 如果当前无活跃 Tab，自动切换到新 Tab
    setActiveTabId((prev) => prev ?? tabId);
  }

  /**
   * Agent session_closed → 自动移除对应 Tab
   *
   * 查找策略：通过 host_name 或 instance_name 匹配已有 Tab
   */
  function _handleSessionClosed(event: AgentEvent) {
    const hostName = event.host_name;

    setTabs((prev) => {
      // 通过 host_name 匹配 Tab（Tab 的 host.name 或 bastionName）
      const closingTab = prev.find(
        (t) => t.host.name === hostName || t.bastionName === hostName,
      );
      if (!closingTab) return prev;

      const remaining = prev.filter((t) => t.id !== closingTab.id);

      // 如果关闭的是当前活跃 Tab，切换到相邻 Tab
      setActiveTabId((currentId) => {
        if (currentId !== closingTab.id) return currentId;
        const closedIdx = prev.findIndex((t) => t.id === closingTab.id);
        const nextTab =
          remaining[Math.min(closedIdx, remaining.length - 1)] ?? null;
        return nextTab?.id ?? null;
      });

      return remaining;
    });
  }

  // ── 页面加载时同步已有终端会话（覆盖 Agent 先于浏览器打开的场景）──
  useEffect(() => {
    // 等待主机列表加载完成后再同步
    if (hostsLoading || hosts.length === 0) return;

    fetchTerminals().then((sessions) => {
      if (sessions.length === 0) return;

      setTabs((prev) => {
        const existingIds = new Set(prev.map((t) => t.id));
        const newTabs: TerminalTab[] = [];

        for (const session of sessions) {
          if (!session.running) continue;

          // 从 instance_name 解析出主机名
          // 格式: "host_name" 或 "bastion--jump_host"
          const parts = session.instance_name.split("--");
          const targetName = parts.length > 1 ? parts[1] : parts[0];

          // 在 hosts 中查找匹配的主机
          let matchedHost: Host | undefined;
          for (const h of hosts) {
            if (h.name === targetName) {
              matchedHost = h;
              break;
            }
            if (h.children?.length) {
              const child = h.children.find((c) => c.name === targetName);
              if (child) {
                matchedHost = child;
                break;
              }
            }
          }

          if (!matchedHost) continue;

          const tabId = tabIdForHost(matchedHost);
          if (existingIds.has(tabId)) continue;
          existingIds.add(tabId);

          newTabs.push({
            ...createTabForHost(matchedHost),
            bastionName: session.instance_name,
            wsUrl: session.ws_url,
          });
        }

        if (newTabs.length === 0) return prev;

        // 自动选中第一个新 Tab（如果当前没有活跃 Tab）
        setActiveTabId((currentId) => currentId ?? newTabs[0].id);
        return [...prev, ...newTabs];
      });
    });
  }, [hostsLoading, hosts]);

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
        let instanceName = closingTab.bastionName || closingTab.host.name;
        // jump_host fallback：如果 onInstanceNameUpdate 还没执行，从 hosts 推断 bastion--name
        if (!closingTab.bastionName && closingTab.host.host_type === "jump_host") {
          const bastion = hosts.find(h => h.children?.some(c => c.id === closingTab.host.id));
          if (bastion) {
            instanceName = `${bastion.name}--${closingTab.host.name}`;
          }
        }
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
                  initialWsUrl={tab.wsUrl}
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
