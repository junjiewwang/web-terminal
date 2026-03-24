import type { AgentEvent } from "../services/api";

interface AgentPanelProps {
  events: AgentEvent[];
}

/** 事件类型 -> 展示配置 */
const EVENT_CONFIG: Record<string, { icon: string; color: string; label: string }> = {
  command_start: { icon: "▶", color: "text-blue-400", label: "执行命令" },
  command_output: { icon: "📝", color: "text-gray-400", label: "命令输出" },
  command_complete: { icon: "✅", color: "text-emerald-400", label: "执行完成" },
  command_error: { icon: "❌", color: "text-red-400", label: "执行错误" },
  session_created: { icon: "🔗", color: "text-cyan-400", label: "建立连接" },
  session_closed: { icon: "🔌", color: "text-yellow-400", label: "断开连接" },
};

/**
 * Agent 操作面板
 *
 * 实时展示 Agent 的操作日志，包含命令执行、会话管理等事件。
 */
export default function AgentPanel({ events }: AgentPanelProps) {
  return (
    <div className="flex flex-col h-full">
      <div className="p-3 border-b border-gray-800">
        <h2 className="text-sm font-semibold text-gray-300">🤖 Agent 操作日志</h2>
        <p className="text-xs text-gray-600 mt-0.5">
          实时追踪 AI Agent 操作
        </p>
      </div>

      <div className="flex-1 overflow-y-auto p-2 space-y-1">
        {events.length === 0 ? (
          <div className="text-center text-gray-600 text-sm py-8">
            等待 Agent 操作...
          </div>
        ) : (
          events.map((event, idx) => {
            const config = EVENT_CONFIG[event.event_type] ?? {
              icon: "•",
              color: "text-gray-500",
              label: event.event_type,
            };
            const time = new Date(event.timestamp).toLocaleTimeString("zh-CN");

            return (
              <div
                key={`${event.timestamp}-${idx}`}
                className="flex items-start gap-2 p-2 rounded bg-gray-900/50 hover:bg-gray-900"
              >
                <span className={`${config.color} mt-0.5`}>{config.icon}</span>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center justify-between">
                    <span className={`text-xs font-medium ${config.color}`}>
                      {config.label}
                    </span>
                    <span className="text-[10px] text-gray-600">{time}</span>
                  </div>
                  <div className="text-xs text-gray-400 mt-0.5">
                    {event.host_name}
                  </div>
                  {event.data.command != null && (
                    <code className="block text-xs text-gray-300 bg-gray-800 rounded px-1.5 py-0.5 mt-1 truncate">
                      $ {String(event.data.command)}
                    </code>
                  )}
                  {event.data.error != null && (
                    <div className="text-xs text-red-400 mt-0.5">
                      {String(event.data.error)}
                    </div>
                  )}
                  {event.data.exit_code !== undefined && (
                    <div className="text-[10px] text-gray-500 mt-0.5">
                      退出码: {String(event.data.exit_code)} | 耗时: {String(event.data.duration_ms)}ms
                    </div>
                  )}
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
