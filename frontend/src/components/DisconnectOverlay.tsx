/**
 * DisconnectOverlay — 终端断连状态覆盖层
 *
 * 当 SSH 连接断开时，在终端区域上层展示：
 * - 断连原因的人类友好描述
 * - 是否可重连的视觉提示
 * - 重连操作按钮
 *
 * 设计原则：
 * - 单一职责：只负责断连状态展示和重连操作入口
 * - 可扩展：通过 SessionExitInfo 接口接收结构化数据
 * - 样式与逻辑分离：不关心重连实现细节，仅回调通知父组件
 */

import type { SessionExitInfo } from "../hooks/useWebSocket";

/** 退出原因到默认展示信息的映射 */
const REASON_DISPLAY: Record<string, { icon: string; label: string; description: string }> = {
  ssh_disconnected: {
    icon: "🔌",
    label: "连接已断开",
    description: "与远程主机的 SSH 连接中断",
  },
  ssh_failed: {
    icon: "❌",
    label: "连接失败",
    description: "无法建立到远程主机的 SSH 连接",
  },
  pty_closed: {
    icon: "📴",
    label: "终端已关闭",
    description: "终端进程已退出",
  },
  normal: {
    icon: "✅",
    label: "会话已结束",
    description: "终端正常退出",
  },
  child_crashed: {
    icon: "💥",
    label: "进程异常退出",
    description: "终端子进程发生崩溃",
  },
  stopped: {
    icon: "⏹",
    label: "会话已停止",
    description: "终端会话被主动停止",
  },
};

const DEFAULT_DISPLAY = {
  icon: "⚠️",
  label: "连接中断",
  description: "终端连接已断开",
};

interface DisconnectOverlayProps {
  /** 结构化的退出信息 */
  exitInfo: SessionExitInfo;
  /** 重连回调 */
  onReconnect: () => void;
  /** 关闭覆盖层（回到 idle 状态） */
  onDismiss?: () => void;
}

export default function DisconnectOverlay({ exitInfo, onReconnect, onDismiss }: DisconnectOverlayProps) {
  const display = REASON_DISPLAY[exitInfo.reason] || DEFAULT_DISPLAY;
  // 如果后端返回了具体的 message，优先使用
  const description = exitInfo.message || display.description;

  return (
    <div className="absolute inset-0 z-10 flex items-center justify-center bg-black/85 backdrop-blur-sm">
      <div className="text-center max-w-md px-6">
        {/* 图标 */}
        <div className="text-4xl mb-4">{display.icon}</div>

        {/* 标题 */}
        <h3 className="text-base font-medium text-gray-200 mb-2">{display.label}</h3>

        {/* 描述 */}
        <p className="text-sm text-gray-400 mb-1">{description}</p>

        {/* 主机名称 */}
        {exitInfo.hostName && (
          <p className="text-xs text-gray-600 mb-4">
            实例: {exitInfo.hostName}
          </p>
        )}

        {/* 退出码（非正常退出时显示） */}
        {exitInfo.exitCode != null && exitInfo.reason !== "normal" && (
          <p className="text-xs text-gray-600 mb-4">
            退出码: {exitInfo.exitCode}
          </p>
        )}

        {/* 操作按钮 */}
        <div className="flex items-center justify-center gap-3 mt-5">
          {exitInfo.recoverable && (
            <button
              onClick={onReconnect}
              className="px-5 py-2 text-sm bg-emerald-700 hover:bg-emerald-600 text-white rounded-md transition-colors font-medium shadow-lg shadow-emerald-900/30"
            >
              重新连接
            </button>
          )}
          {onDismiss && (
            <button
              onClick={onDismiss}
              className="px-4 py-2 text-sm text-gray-400 hover:text-gray-200 hover:bg-white/5 rounded-md transition-colors"
            >
              关闭
            </button>
          )}
          {!exitInfo.recoverable && !onDismiss && (
            <p className="text-xs text-amber-500/80 mt-2">
              此问题需要修复后才能重新连接
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
