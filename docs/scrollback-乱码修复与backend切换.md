# Scrollback 乱码修复与 Backend 切换功能

## 需求背景

### 问题 1：刷新页面后终端追加乱码字符

**现象**：刷新页面后终端命令行出现 `1;2c0;276;0c1;2c0;276;0c` 等乱码。

**根因**：
- 刷新页面时，前端创建新的 xterm.js 实例并重新连接 WebSocket
- 后端 `add_ws_client()` 发送 scrollback 历史回放（TMUX 模式下为原始字节）
- scrollback 中包含终端查询转义序列（如 `\x1b[c` DA1、`\x1b[6n` CPR 等）
- xterm.js "回答"这些查询，生成 DA/CPR 响应，通过 `onData` 回调发送到 PTY
- 远端 shell 将响应当作普通文本回显 → 显示为乱码

### 问题 2：页面需要支持切换 tmux 和 broker 模式

**现象**：前端无法切换和标识当前 backend 模式（tmux/broker），后端已支持但前端未使用。

### 问题 3：Broker 模式下鼠标点击/滚动产生乱码字符

**现象**：Broker 模式下鼠标点击或滚动会在终端输入 `^[[<0;79;12M^[[<0;79;12m` 等乱码。

**根因**：
- xterm.js 检测到远端启用鼠标追踪（`\x1b[?1000h` / `\x1b[?1006h`）后，会将鼠标事件生成 SGR 鼠标报告序列
- TMUX 模式下，tmux 拦截并消费这些鼠标事件（用于 copy-mode、窗格选择等）
- Broker 模式下没有 tmux 中间层，鼠标事件直接发送到远端 PTY，远端 shell 不理解就回显为乱码
- 同时，远端未启用鼠标追踪时，xterm.js 本地滚动（scrollback 翻页）也被阻断

**智能过滤方案**：
- `VirtualTerminal` 新增 `mouse_tracking_enabled` 属性，通过 pyte `Screen.mode` 检测远端是否启用了鼠标追踪（?1000/?1002/?1003）
- `TerminalSession.write()` 在 Broker 模式下：
  - 远端**未启用**鼠标追踪 → 过滤掉鼠标事件序列，由 xterm.js 本地处理（如滚动 scrollback）
  - 远端**已启用**鼠标追踪（如 vim/less/htop）→ 放行鼠标事件，远端正常处理

## 修复方案

### 问题 1：根本修复 — TMUX 模式跳过 scrollback 回放

TMUX 本身具备会话恢复和屏幕渲染能力，新客户端连接时 tmux 会自动渲染当前屏幕内容。
之前在 TMUX 模式下额外回放 scrollback 是多余的，而且正是这个多余的回放中包含的
终端查询序列导致了乱码。

**修复方式**：直接删除 TMUX 模式下的 scrollback 回放逻辑，只保留 Broker 模式的全屏快照回放。

1. **后端**（`terminal_manager.py`）：
   - `add_ws_client()` 中只在 `self._vterm` 存在时（Broker 模式）发送 `full_screen_dump()` 快照
   - TMUX 模式不再发送任何历史回放
   - 保留 `_TERMINAL_QUERY_RE` 正则（用于 Broker 模式 scrollback 的防御性过滤）
   - 保留 `_safe_ws_send_history()` 方法，使用 `"history"` 消息类型发送 Broker 快照

2. **前端**（`useWebSocket.ts` + `TerminalView.tsx`）：
   - `useWebSocket` 新增 `onHistory` 回调，处理 `"history"` 类型消息
   - `TerminalView` 使用 `historyReplayRef` 标志位，在 Broker 模式回放期间屏蔽 `onData`
   - 防止 xterm.js 对快照中可能残留的查询序列生成响应

### 问题 2：Backend 切换与标识

- `api.ts`：`startTerminal()` 增加可选 `backend` 参数；`TerminalInstance` 增加 `backend` 字段
- `TerminalTabs.tsx`：Tab 上显示 `T`（TMUX）/ `B`（BROKER）小标识
- `TerminalView.tsx`：StatusBar 显示 backend badge（`[TMUX]` / `[BROKER]`），已连接时显示切换按钮
- `App.tsx`：实现 `handleBackendSwitch` — 停止当前会话 → 用新 backend 重新启动

## 修改文件清单

| 文件 | 改动 |
|------|------|
| `src/services/virtual_terminal.py` | `_render_line` 修复 CJK 宽字符占位符问题；新增 `mouse_tracking_enabled` 属性追踪远端鼠标追踪状态 |
| `src/services/terminal_manager.py` | 添加 `_TERMINAL_QUERY_RE` / `_MOUSE_EVENT_RE` 正则；添加 `_safe_ws_send_history` 方法；`add_ws_client` TMUX 模式跳过回放；`write()` Broker 模式智能过滤鼠标事件 |
| `frontend/src/hooks/useWebSocket.ts` | `UseWebSocketOptions` 增加 `onHistory` 回调；消息处理支持 `"history"` 类型 |
| `frontend/src/services/api.ts` | `TerminalInstance` 增加 `backend` 字段；`startTerminal()` 增加可选 `backend` 参数 |
| `frontend/src/components/TerminalView.tsx` | 增加 `historyReplayRef` 标志位；`onHistory` 回放时屏蔽 `onData`；`_StatusBar` 显示 backend badge + 切换按钮 |
| `frontend/src/components/TerminalTabs.tsx` | `TerminalTab` 增加 `backend` 字段；Tab 显示 backend 标识（T/B） |
| `frontend/src/App.tsx` | 导入 `TerminalBackend`；添加 `handleBackendSwitch`；传递 `backend`/`onBackendSwitch` props；fetchTerminals/SSE 同步 backend |

## 实施状态

- [x] 问题 1：后端 scrollback 过滤 + history 消息类型
- [x] 问题 1：前端 onHistory 回调 + 回放期间屏蔽 onData
- [x] 问题 2：api.ts startTerminal backend 参数 + TerminalInstance backend 字段
- [x] 问题 2：TerminalView StatusBar backend badge + 切换按钮
- [x] 问题 2：App.tsx/TerminalTabs backend 字段 + Tab 标识 + 切换逻辑
- [x] 问题 3：VirtualTerminal 鼠标追踪状态追踪（mouse_tracking_enabled）
- [x] 问题 3：TerminalSession.write() Broker 模式智能过滤鼠标事件序列
- [x] 文档记录

## 遗留问题

- 暂无
