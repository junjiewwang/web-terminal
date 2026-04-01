import { useMemo } from "react";
import type { AgentEvent } from "../services/api";

interface AgentPanelProps {
  events: AgentEvent[];
  onClose?: () => void;
}

/** 事件类型 -> 展示配置 */
const EVENT_CONFIG: Record<string, { icon: string; color: string; label: string }> = {
  command_start: { icon: "▶", color: "text-blue-400", label: "执行命令" },
  command_output: { icon: "📝", color: "text-gray-400", label: "命令输出" },
  command_complete: { icon: "✅", color: "text-emerald-400", label: "执行完成" },
  command_error: { icon: "❌", color: "text-red-400", label: "执行错误" },
  session_created: { icon: "🔗", color: "text-cyan-400", label: "建立连接" },
  session_closed: { icon: "🔌", color: "text-yellow-400", label: "断开连接" },
  session_error: { icon: "⚠️", color: "text-red-400", label: "连接失败" },
  window_switched: { icon: "🔀", color: "text-purple-400", label: "切换窗口" },
};

/**
 * Agent 操作面板
 *
 * 实时展示 Agent 的操作日志，包含命令执行、会话管理等事件。
 */
export default function AgentPanel({ events, onClose }: AgentPanelProps) {
  const displayEvents = useMemo(() => [...events].reverse(), [events]);

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-white/8 px-4 py-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <p className="text-[11px] font-medium uppercase tracking-[0.28em] text-emerald-300/70">
              Activity
            </p>
            <h2 className="mt-2 text-base font-semibold text-gray-100">操作轨迹</h2>
            <p className="mt-1 text-xs text-gray-500">查看连接、命令与会话事件</p>
          </div>

          {onClose && (
            <button
              type="button"
              onClick={onClose}
              className="rounded-full border border-white/10 bg-white/5 px-2.5 py-1.5 text-xs text-gray-400 transition-colors hover:border-emerald-400/40 hover:bg-emerald-400/10 hover:text-white"
              aria-label="关闭操作轨迹面板"
            >
              关闭
            </button>
          )}
        </div>

        <div className="mt-4 inline-flex items-center gap-2 rounded-full border border-white/8 bg-white/4 px-3 py-1 text-[11px] text-gray-400">
          <span className="inline-flex h-2 w-2 rounded-full bg-emerald-400" />
          共 {events.length} 条事件
        </div>
      </div>

      <div className="flex-1 space-y-2 overflow-y-auto p-3">
        {displayEvents.length === 0 ? (
          <div className="flex h-full min-h-56 items-center justify-center text-center">
            <div>
              <div className="text-3xl text-emerald-300/70">◎</div>
              <p className="mt-3 text-sm font-medium text-gray-200">暂无操作轨迹</p>
              <p className="mt-1 text-xs text-gray-500">默认已隐藏，需要排障时再展开查看即可</p>
            </div>
          </div>
        ) : (
          displayEvents.map((event, idx) => {
            const config = EVENT_CONFIG[event.event_type] ?? {
              icon: "•",
              color: "text-gray-500",
              label: event.event_type,
            };
            const time = new Date(event.timestamp).toLocaleTimeString("zh-CN");

            return (
              <div
                key={`${event.timestamp}-${idx}`}
                className="rounded-2xl border border-white/8 bg-white/[0.03] p-3 transition-colors hover:bg-white/[0.05]"
              >
                <div className="flex items-start gap-3">
                  <span className={`mt-0.5 text-sm ${config.color}`}>{config.icon}</span>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center justify-between gap-3">
                      <span className={`text-xs font-medium ${config.color}`}>
                        {config.label}
                      </span>
                      <span className="shrink-0 text-[10px] text-gray-600">{time}</span>
                    </div>
                    <div className="mt-1 text-xs text-gray-400">{event.host_name}</div>
                    {event.data.command != null && (
                      <code className="mt-2 block truncate rounded-lg bg-gray-900/80 px-2 py-1 text-xs text-gray-200">
                        $ {String(event.data.command)}
                      </code>
                    )}
                    {event.data.error != null && (
                      <div className="mt-1 text-xs text-red-400">{String(event.data.error)}</div>
                    )}
                    {event.data.exit_code !== undefined && (
                      <div className="mt-1 text-[10px] text-gray-500">
                        退出码: {String(event.data.exit_code)} · 耗时: {String(event.data.duration_ms)}ms
                      </div>
                    )}
                  </div>
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
