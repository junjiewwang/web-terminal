import { useState, useEffect } from "react";
import HostList from "./components/HostList";
import AgentPanel from "./components/AgentPanel";
import TerminalView from "./components/TerminalView";
import type { Host, AgentEvent } from "./services/api";
import { fetchHosts, subscribeEvents } from "./services/api";

/**
 * 应用主布局：左侧主机列表 + 中间终端 + 右侧 Agent 面板
 */
export default function App() {
  const [hosts, setHosts] = useState<Host[]>([]);
  const [selectedHost, setSelectedHost] = useState<Host | null>(null);
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [hostsError, setHostsError] = useState<string | null>(null);
  const [hostsLoading, setHostsLoading] = useState(true);

  // 加载主机列表（含错误恢复）
  const loadHosts = () => {
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
  };

  useEffect(() => {
    loadHosts();
  }, []);

  // SSE 事件订阅
  // subscribeEvents 内部注册为全局单例，startWeTTY 会自动暂停/恢复 SSE，
  // 组件层无需额外管理 SSE 生命周期。
  useEffect(() => {
    const cleanup = subscribeEvents((event) => {
      setEvents((prev) => [...prev.slice(-99), event]);
    });
    return cleanup;
  }, []);

  return (
    <div className="flex h-screen bg-gray-950 text-gray-100">
      {/* 左侧：主机列表 */}
      <aside className="w-64 border-r border-gray-800 flex flex-col">
        <div className="p-4 border-b border-gray-800">
          <h1 className="text-lg font-bold text-emerald-400">
            🖥 MCP Terminal
          </h1>
          <p className="text-xs text-gray-500 mt-1">
            AI Agent SSH 终端管理
          </p>
        </div>
        <HostList
          hosts={hosts}
          selectedHost={selectedHost}
          onSelect={setSelectedHost}
          loading={hostsLoading}
          error={hostsError}
          onRetry={loadHosts}
        />
      </aside>

      {/* 中间：终端区域 */}
      <main className="flex-1 flex flex-col">
        <header className="h-10 bg-gray-900 border-b border-gray-800 flex items-center px-4">
          <span className="text-sm text-gray-400">
            {selectedHost
              ? `${selectedHost.username}@${selectedHost.hostname}:${selectedHost.port}`
              : "请选择一个主机"}
          </span>
        </header>
        <div className="flex-1">
          <TerminalView host={selectedHost} />
        </div>
      </main>

      {/* 右侧：Agent 面板 */}
      <aside className="w-80 border-l border-gray-800">
        <AgentPanel events={events} />
      </aside>
    </div>
  );
}
