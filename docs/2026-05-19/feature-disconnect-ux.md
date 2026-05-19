# Feature: SSH 断连友好 UX

## 需求背景

当远程 SSH 连接因网络中断、服务端关闭等原因断开时，用户在终端中仅看到一条
`Connection reset by peer` 之类的英文错误，之后终端无任何反馈。需要提供：

1. **友好的中文断连描述**
2. **是否可重连的判断**
3. **一键重连操作入口**

## 架构设计（3 层）

```
Layer 1 (后端): PTY 缓冲区 → 断连模式识别 → 增强 WebSocket 消息
Layer 2 (协议): session_exit 消息增加 message / recoverable / host_name 字段
Layer 3 (前端): DisconnectOverlay 组件 → disconnected 状态
```

### 数据流

```
PTY EOF/Error → _handle_child_exit(PTY_CLOSED)
  → _async_exit_cleanup():
    ① 取 _raw_buffer 尾 20 行
    ② _extract_disconnect_info() 正则匹配
    ③ 匹配成功 → 升级 exit_reason 为 SSH_DISCONNECTED
    ④ 发送增强 session_exit: {reason, message, recoverable, host_name}
  → 前端 useWebSocket:
    ⑤ 解析 SessionExitInfo → onSessionExit 回调
  → TerminalView:
    ⑥ status="disconnected" + exitInfo 状态
    ⑦ 渲染 DisconnectOverlay
```

## 实施变更清单

### 后端 (`src/services/terminal_manager.py`)

- [x] `SessionExitReason` 枚举增加 `SSH_DISCONNECTED`
- [x] 新增 `_DISCONNECT_PATTERNS` 列表（10 种常见断连模式）
- [x] 新增 `_extract_disconnect_info()` 辅助函数
- [x] `_async_exit_cleanup()` 中增加断连模式匹配 + 增强 WebSocket 消息

### 前端 WebSocket Hook (`frontend/src/hooks/useWebSocket.ts`)

- [x] 新增 `SessionExitInfo` 接口（reason/exitCode/message/recoverable/hostName）
- [x] `UseWebSocketOptions` 新增 `onSessionExit` 回调
- [x] `session_exit` 消息解析增强

### 前端 UI (`frontend/src/components/`)

- [x] 新建 `DisconnectOverlay.tsx` — 断连状态覆盖层组件
- [x] `TerminalView.tsx`:
  - `ConnectionStatus` 增加 `"disconnected"` 值
  - 新增 `exitInfo` 状态
  - 注册 `onSessionExit` 回调
  - 渲染 `DisconnectOverlay`
  - `STATUS_MAP` 增加 disconnected 配色

## 支持的断连模式

| 模式 | 友好消息 | 可重连 |
|------|----------|--------|
| Connection reset by peer | 远程主机强制断开了连接 | ✅ |
| Connection closed by ... | 远程主机关闭了连接 | ✅ |
| Broken pipe | 网络连接中断 | ✅ |
| Connection timed out | 连接超时 | ✅ |
| Network is unreachable | 网络不可达 | ✅ |
| No route to host | 无法到达主机 | ✅ |
| Host key verification failed | 主机密钥验证失败 | ❌ |
| Permission denied | 认证失败 | ❌ |
| Connection refused | 连接被拒绝 | ✅ |
| Read from remote host: Connection reset | 远程主机断开了连接 | ✅ |

## 设计决策

1. **断连检测位置**：在 `_async_exit_cleanup()` 而非 `_on_pty_readable()`，因为只有确认退出后才需要匹配
2. **缓冲区扫描范围**：最后 20 行，足以覆盖 SSH 错误输出
3. **exit_reason 升级**：PTY_CLOSED → SSH_DISCONNECTED 仅在匹配到已知模式时
4. **前端 disconnected vs error**：`disconnected` 是可预期的断连，`error` 是 WebSocket 本身连接失败
5. **onDismiss 回调**：允许用户关闭覆盖层回到 idle 状态（不强制重连）

## 验证方式

1. 正常 SSH → `exit` 退出 → 应进入 idle 状态（无 overlay）
2. 手动 kill SSH 连接（如 `kill -9 <sshd_pid>`）→ 应展示 "远程主机强制断开了连接" + 重连按钮
3. 网络中断模拟 → 应展示超时相关信息 + 重连按钮
4. 密钥验证失败 → 应展示错误信息 + 不可重连提示
