# Backend 切换丝滑度 & Broker 鼠标滚动问题分析

## 问题概述

### 问题 1：tmux/broker 切换不够丝滑
切换 backend 时没有释放对方的资源。比如从 tmux 切到 broker，tmux 的 session 资源应该被释放；反之亦然。

### 问题 2：Broker 模式下浏览器终端不能通过鼠标滚轮滚动
Broker 模式下，鼠标滚轮滚动似乎没有走 xterm.js 的本地 scrollback 滚动。

---

# 🎩 六顶思考帽分析

## 🤍 白帽 — 事实与数据

### 问题 1 的事实链：

1. **当前切换流程**（`App.tsx` → `handleBackendSwitch`）：
   - 调用 `stopTerminal(instanceName)` 停止旧会话
   - 调用 `startTerminal(host.id, newBackend)` 创建新会话
   - 更新 Tab 的 `wsUrl`、`instanceName`、`backend`

2. **后端 `TerminalManager.create_session()` 逻辑**（`terminal_manager.py:923-967`）：
   - 如果 `existing.running && existing.backend == selected_backend` → 直接复用，返回旧 session
   - 如果 backend 不同 → `pop` 旧 session → 创建新 session → `await previous_session.stop()`
   - **关键问题**：前端 `handleBackendSwitch` 先调用了 `stopTerminal`（REST API），这会从 `_sessions` dict 中删除旧 session 并 stop 它。然后 `startTerminal` 调用 `create_session`，此时 `_sessions` 中已经没有旧 session 了，不会再走 pop + stop 逻辑。
   - 这意味着：**前端层面已经做了 stop → start 的串行操作，后端层面也能正确执行。**

3. **实际不丝滑的体验**：
   - `stopTerminal` 是 REST POST，等待旧会话完全 stop（包括 SIGTERM 子进程、清理 fd、关闭 WebSocket）
   - 然后 `startTerminal` 是另一个 REST POST，等待新会话创建 + SSH 连接建立
   - **串行等待**两次网络往返 + 两次资源操作 = 用户感知到明显延迟
   
4. **资源释放问题**：
   - 从 tmux → broker：`TerminalSession.stop()` 会调用 `_cleanup_resources()` → `_cleanup_tmux_session()`，即 `tmux kill-session`。✅ tmux session 会被清理
   - 从 broker → tmux：`TerminalSession.stop()` 会走 `_cleanup_resources()`，但 broker 没有额外的 tmux session 需要清理。不过 broker 的 PTY 子进程（bash → ssh）会被 SIGTERM 杀掉。✅ 资源正确释放
   - **结论：后端资源释放逻辑是完整的，但前端串行操作导致体验不丝滑。**

5. **前端状态问题**：
   - 切换时 `TerminalView` 检测到 `initialWsUrl` 变化会断开旧 WebSocket → 连接新 WebSocket
   - 但中间有一个空白期（旧终端已断、新终端未连），用户看到的是 loading 状态
   - xterm.js 实例没有 reset/clear，旧终端内容还残留在屏幕上，直到新 WebSocket 连接成功

### 问题 2 的事实链：

1. **xterm.js 滚动机制**：
   - xterm.js 内置 scrollback buffer（默认 1000 行），用户可以鼠标滚轮上下滚动查看历史
   - **前提条件**：远端没有启用鼠标追踪模式（?1000h/?1002h/?1003h）时，xterm.js 拦截鼠标滚轮事件本地处理
   - 如果远端启用了鼠标追踪，鼠标事件会被编码为 ANSI 序列发送到远端

2. **当前 Broker 模式鼠标处理**（`terminal_manager.py:461-477`）：
   ```python
   def write(self, data: str) -> None:
       if self._vterm and not self._vterm.mouse_tracking_enabled:
           data = _MOUSE_EVENT_RE.sub("", data)
           if not data:
               return
   ```
   - 当远端**未启用**鼠标追踪时，过滤掉鼠标事件序列 → `return`
   - 这意味着鼠标事件被后端吃掉了，**但问题在于鼠标事件是否到达了后端**

3. **关键洞察 — xterm.js 的鼠标模式判断**：
   - xterm.js 根据远端发来的 `\x1b[?1000h` 等序列来决定是否开启"鼠标报告模式"
   - **Broker 模式使用 VirtualTerminal（pyte）差分渲染**，PTY 原始输出经 pyte 解析后转为差分 ANSI
   - pyte 的 `feed_and_render()` 输出是**字符矩阵差分**（光标定位 + 文本），**不会透传原始的 DEC Private Mode 切换序列**（如 `\x1b[?1000h`）
   - 也就是说：**即使远端 shell 没有启用鼠标追踪，pyte 差分渲染后发送给 xterm.js 的数据也不包含 mode 切换序列**

4. **但这不应该影响滚动**，因为：
   - xterm.js 默认不开启鼠标报告模式
   - 没有收到 `\x1b[?1000h` 时，xterm.js 应该用本地 scrollback 处理鼠标滚轮
   - **真正的问题可能在别处**

5. **Broker 模式下 scrollback 的实际情况**：
   - Broker 模式：PTY 输出 → `feed_and_render()` → 差分渲染 ANSI → 发送给 xterm.js
   - 差分渲染的特点：**每次只发送变更行，使用光标绝对定位 `\x1b[{line};1H`**
   - xterm.js 收到的全部是**光标定位到固定行 + 覆写内容**，而不是自然的"向上滚动"流
   - **这正是问题所在**：xterm.js 的 scrollback 靠的是"新行从底部推入，老行被推到 scrollback buffer"。但差分渲染是"直接定位到固定行位置覆写"，不会触发 xterm.js 的滚动机制
   - 换言之：**Broker 模式下 xterm.js 的 scrollback buffer 基本是空的**，因为所有内容都是通过绝对定位写入的，没有"向上滚出"的行

6. **对比 TMUX 模式**：
   - TMUX 模式：PTY 输出直通（原始 ANSI 字节流） → 发送给 xterm.js
   - 自然的 `\r\n` 换行会推动 xterm.js 的内部 scrollback
   - 所以 TMUX 模式下鼠标滚轮滚动正常

---

## ❤️ 红帽 — 情感与直觉

1. **问题 1（切换不丝滑）**：感觉不是架构问题，更像是 UX 优化问题。串行 stop+start 导致的延迟是可感知的，但后端逻辑是正确的。改成异步/并行应该能明显改善。

2. **问题 2（Broker 滚动）**：直觉上这是 pyte 差分渲染架构的一个**根本性副作用**。差分渲染本身是为了解决多客户端共享 PTY 的问题，但它破坏了 xterm.js 的自然滚动。这是一个需要认真权衡的架构取舍。

3. 觉得问题 2 比问题 1 更难修，因为它涉及到 pyte VirtualTerminal 的渲染策略，而不是简单的调用顺序优化。

---

## 🖤 黑帽 — 风险与批判

### 问题 1 风险：
1. **并行化 stop + start 可能导致端口/session 冲突**：旧 session 还没完全释放时新 session 就创建了，可能出现 tmux session name 冲突
2. **前端 xterm.js 实例不 reset**：切换后旧终端残像会短暂闪现
3. **SSE 事件竞态**：stop 触发 `session_closed` 事件，start 触发 `session_created` 事件，如果处理顺序不对，可能导致 Tab 被误删后又创建

### 问题 2 风险：
1. **差分渲染是 Broker 模式的核心设计决策**，改动渲染策略影响面大
2. **如果回退到直通模式**：失去了差分渲染的带宽优化和全屏快照恢复能力
3. **如果用混合模式**（正常文本直通 + 全屏程序差分）：复杂度显著增加，且"正常"和"全屏"的边界难以精确判断
4. **xterm.js 的 scrollback 机制是行级推入**：差分渲染本质上绕过了这个机制，要修复需要在 pyte 和 xterm.js 之间增加一个"滚动检测+模拟"层

---

## 💛 黄帽 — 价值与乐观

### 问题 1：
1. 修复相对简单：后端 `create_session` 已经内置了 "backend 不同则 stop 旧 + start 新" 的逻辑，前端不需要先 stop 再 start，可以**直接调用 `startTerminal(hostId, newBackend)`**，后端自动处理旧会话
2. 前端可以加过渡动画（如 fade-out → loading → fade-in），体验会好很多
3. xterm.js reset 清屏可以消除残像

### 问题 2：
1. 有一个巧妙的解决思路：**在差分渲染输出中注入"滚动信号"**
2. pyte Screen 的行为可以被追踪：当新行从底部推入时（即 screen 发生滚动），可以先发送 `\r\n` 让 xterm.js 滚动，然后再发送差分内容
3. 或者更简单：**普通 shell 交互时直通原始 ANSI，只在全屏程序（检测到 alternate screen）时启用差分渲染**

---

## 💚 绿帽 — 创意与可能性

### 问题 1 创意方案：
1. **一步切换法**：前端直接调用 `startTerminal(hostId, newBackend)` 不先 stop，让后端的 `create_session` 自动 pop + stop 旧 session → 减少一次 RTT
2. **乐观 UI**：点击切换按钮后立刻显示新 backend 的 loading 状态 + 清屏，不等旧 session 完全 stop
3. **预创建**：切换按钮 hover 时就预先准备新 backend 的 SSH 连接（过于激进，不推荐）

### 问题 2 创意方案：
1. **Alternate Screen 检测 + 双模式渲染**：
   - 正常模式（主屏幕 / normal screen）：**直通原始 ANSI**，xterm.js 正常滚动
   - 全屏模式（alternate screen / ?1049h）：**启用 pyte 差分渲染**，此时不需要 scrollback
   - pyte 可以检测 `\x1b[?1049h`（进入 alternate screen）和 `\x1b[?1049l`（离开）
   
2. **Scroll Region 检测**：pyte 可以追踪 `Screen._scroll()`（滚动区域滚动事件），当检测到滚动时，先输出 `\r\n` 模拟滚动

3. **前端 xterm.js 层面的 workaround**：强制 xterm.js 进入 `scrollOnOutput = true` 模式，并在收到 `output` 消息时模拟行推入而非绝对定位写入 — 但这与差分渲染的光标定位冲突

4. **最务实的方案**：在 `_on_pty_readable` 中，**总是将原始 ANSI 直通给 xterm.js**，pyte 只作为"旁路解析器"用于 `mouse_tracking_enabled` 检测和 `full_screen_dump()` 快照，不参与实际的输出渲染路径

---

## 💙 蓝帽 — 综合结论

### 问题 1：Backend 切换优化

**根因**：前端串行 `stopTerminal` → `startTerminal` 两次 RTT，且后端 `create_session` 本身已内置旧会话清理逻辑，前端多做了一次无效的 stop。

**推荐方案**：
1. 前端 `handleBackendSwitch` **跳过 `stopTerminal`**，直接调用 `startTerminal(hostId, newBackend)`
2. 后端 `create_session` 检测到 backend 不同时，自动 stop 旧 session + 创建新 session（已实现）
3. 前端在发起切换时**立刻清屏 xterm.js**（`terminal.write('\x1b[2J\x1b[H')`），避免残像
4. 保持 loading overlay 直到新 WebSocket 连接成功

**效果**：减少一次网络往返 + 前端感知更丝滑

### 问题 2：Broker 模式鼠标滚动

**根因**：pyte 差分渲染使用绝对光标定位覆写行，绕过了 xterm.js 的行级滚动机制，导致 xterm.js scrollback buffer 为空，鼠标滚轮无内容可滚。

**推荐方案（Alternate Screen 双模式）**：

改造 `VirtualTerminal` 和 `TerminalSession._on_pty_readable()` 的输出路径：

- **Normal Screen（普通 shell 交互）**：直通原始 ANSI → xterm.js 自然滚动 ✅
- **Alternate Screen（vim/top/less 等全屏程序）**：启用 pyte 差分渲染 → 全屏程序无需 scrollback ✅

pyte 可以通过 `Screen.mode` 中的 `?1049`（alternate screen buffer）来检测当前状态。

**效果**：普通 shell 下鼠标滚轮正常工作；全屏程序下保留差分渲染优势；`full_screen_dump()` 快照功能不受影响（仅 alternate screen 时需要快照恢复）。

---

## 实施优先级

| 编号 | 任务 | 优先级 | 预估工作量 | 状态 |
|------|------|--------|-----------|------|
| 1 | 问题 1：前端去掉多余 stop，直接 start | P0 | 0.5h | ✅ 已完成 |
| 2 | 问题 1：切换时清屏 xterm.js | P0 | 0.5h | ✅ 已完成 |
| 3 | 问题 2：VirtualTerminal 增加 alternate screen 检测 | P0 | 1h | ✅ 已完成 |
| 4 | 问题 2：输出路径改为 Normal=直通 / Alternate=差分 | P0 | 2h | ✅ 已完成 |
| 5 | 问题 2-bugfix：切换时重置 xterm.js DEC Private Mode | P0 | 0.5h | ✅ 已完成 |
| 6 | 问题 3：修复 is_reusing 多跳编排 bug | P0 | 1h | ✅ 已完成 |
| 7 | 问题 4：全局 backend 切换（Per-Tab → Global） | P0 | 2h | ✅ 已完成 |
| 9 | 问题 5a：Broker 模式 vim 退出后鼠标滚轮失效 — alt→normal 切换帧 mode reset 注入 | P0 | 1h | ✅ 已完成（被 5b 替代） |
| 10 | 问题 5b：vim 退出画面残留 — alt→normal 切换帧改为直通原始数据 | P0 | 0.5h | ✅ 已完成 |
| 11 | 回归测试 | P0 | 1h | ⏳ 待验证 |

## 变更清单

### 问题 1 修改文件

| 文件 | 改动 |
|------|------|
| `frontend/src/App.tsx` | `handleBackendSwitch` 移除多余的 `stopTerminal` 调用，直接 `startTerminal`（后端 `create_session` 自动处理旧会话清理），减少一次 RTT |
| `frontend/src/hooks/useTerminal.ts` | `TerminalHandle` 接口新增 `clear()` 方法，用于清空 xterm.js 屏幕和 scrollback |
| `frontend/src/components/TerminalView.tsx` | 检测 `initialWsUrl` 变化（backend 切换）时调用 `terminal.clear()` 消除旧终端残像 |

### 问题 2 修改文件

| 文件 | 改动 |
|------|------|
| `src/services/virtual_terminal.py` | 新增 `_ALTERNATE_SCREEN_MODES` 常量（检测 `?1049h`/`?1047h`/`?47h`）；新增 `alternate_screen_active` 属性；新增 `feed_only()` 方法（仅 feed pyte 不做差分渲染） |
| `src/services/terminal_manager.py` | `_on_pty_readable()` 输出路径改为双模式：Normal Screen → 直通原始 ANSI + `feed_only`；Alternate Screen → `feed_and_render` 差分渲染 |
| `src/services/terminal_manager.py` | `add_ws_client()` 历史恢复适配双模式：Alternate Screen → `full_screen_dump()` 快照；Normal Screen → scrollback 缓冲区回放（过滤终端查询序列） |

### 问题 2 bugfix：xterm.js DEC Private Mode 残留

| 文件 | 改动 |
|------|------|
| `frontend/src/hooks/useTerminal.ts` | 新增 `reset()` 方法：写入 DEC Private Mode 重置序列（关闭 `?1000`/`?1002`/`?1003`/`?1006` 鼠标追踪、`?2004` Bracketed Paste、`?1` DECCKM、`?1049` Alternate Screen）+ 清空屏幕和 scrollback |
| `frontend/src/components/TerminalView.tsx` | Backend 切换时从 `terminal.clear()` 改为 `terminal.reset()`，确保重置 TMUX 遗留的鼠标追踪状态 |
| `src/services/terminal_manager.py` | 移除 `_on_pty_readable()` 中的临时诊断日志 |

### 问题 3：修复 is_reusing 多跳编排 bug

**根因**：`is_reusing` 标志在 `create_session()` 之前通过 `has_running_session()` 检查，但 `create_session()` 在 backend 不同时会销毁旧会话并创建新会话。导致 `is_reusing=True` 但实际是新会话 → 多跳编排被跳过。

**修复方案**：`create_session()` 返回 `tuple[TerminalSession, bool]`，其中 `bool` 为 `is_new`（`True` = 新建，`False` = 复用），调用方直接使用 `is_new` 判断是否执行编排。

| 文件 | 改动 |
|------|------|
| `src/services/terminal_manager.py` | `create_session()` 返回类型从 `TerminalSession` 改为 `tuple[TerminalSession, bool]`，复用时 `return existing, False`，新建时 `return session, True` |
| `src/api/terminal.py` | `start_terminal()` 适配 `(session, is_new)` 元组，用 `is_new` 替代 `is_reusing` 判断编排触发 |
| `src/mcp_server/server.py` | `_connect_path()` 适配 `(session, is_new)` 元组，同步修复编排判断逻辑 |

### 问题 4：全局 Backend 切换（Per-Tab → Global）

**需求**：Backend 模式应为全局设置，切换后所有终端会话统一使用新模式。

**设计**：
1. 后端 `TerminalManager` 新增 `switch_backend()` 方法：更新 `_default_backend`、stop 所有现有会话、返回停止的实例名列表
2. 新增 REST API：`GET /api/terminal/backend`（查询当前模式）、`PUT /api/terminal/backend`（全局切换）
3. 前端 App.tsx 维护 `globalBackend` state，Header 区域渲染全局切换按钮
4. 切换时：`PUT backend` → 后端停止所有会话 → 前端 `Promise.allSettled` 逐个 Tab 重连 → 更新 Tab 状态
5. TerminalView `_StatusBar` 保留只读 backend badge，移除 per-tab 切换按钮

| 文件 | 改动 |
|------|------|
| `src/services/terminal_manager.py` | 新增 `default_backend` setter、`switch_backend()` 方法 |
| `src/api/terminal.py` | 新增 `BackendResponse`、`SwitchBackendRequest`、`SwitchBackendResponse` 模型；新增 `GET/PUT /api/terminal/backend` 端点 |
| `frontend/src/services/api.ts` | 新增 `fetchBackend()`、`switchBackend()`、`SwitchBackendResult` 接口 |
| `frontend/src/App.tsx` | 新增 `globalBackend`/`backendSwitching` state、`handleGlobalBackendSwitch` 回调、Header 全局切换按钮；移除 per-tab `onBackendSwitch` prop 传递 |
| `frontend/src/components/TerminalView.tsx` | 移除 `onBackendSwitch` prop、移除 `_StatusBar` per-tab 切换按钮，保留只读 backend badge |

### 问题 5a：Broker 模式 vim 退出后鼠标滚轮失效（已被 5b 替代）

**根因**：`_on_pty_readable()` 在 Alternate Screen 模式下使用 `feed_and_render()` 差分渲染。当 vim/less 退出时，原始数据中的 `\x1b[?1000l`（禁用鼠标追踪）和 `\x1b[?1049l`（退出 Alternate Screen）序列被 pyte 内部消化，差分渲染只输出字符矩阵变更，不会透传这些 DEC Private Mode reset 序列。xterm.js 未收到 `\x1b[?1000l` → 鼠标追踪状态残留 → 滚轮事件被编码为鼠标序列 → 后端 `write()` 检测到 pyte `mouse_tracking_enabled=False` 后过滤丢弃 → 滚轮完全失效。

**初版修复**：在 `_on_pty_readable()` 中捕获 `feed_and_render` 前后的 mode 状态（`was_alt`/`now_alt`、`was_mouse`/`now_mouse`），当检测到 alt→normal 切换时，显式注入 DEC Private Mode reset 序列（`_build_mode_reset_seq()`）+ `full_screen_dump()` 替代差分渲染输出。

**问题**：`full_screen_dump()` 内部先执行 `\x1b[2J`（清屏），会抹掉 xterm.js 通过 `\x1b[?1049l` 应该恢复出来的 normal screen 画面。导致 vim 退出后屏幕残留 vim 内容（实际是 pyte normal screen buffer 的字符矩阵覆写），不符合终端标准交互。

### 问题 5b：vim 退出画面残留 — alt→normal 切换帧改为直通原始数据

**根因**：5a 的修复用 `_build_mode_reset_seq() + full_screen_dump()` 手动重建退出画面，但 `full_screen_dump()` 的 `\x1b[2J` 清屏破坏了 xterm.js 通过 `\x1b[?1049l` 恢复的正确 normal screen 画面和 scrollback buffer。

**修复方案**：alt→normal 切换帧改为**直通原始 `text`** 给 xterm.js。原始数据中包含完整的终端控制序列：
- `\x1b[?1049l` → xterm.js 自行切回 normal screen 并恢复之前的画面
- `\x1b[?1000l` → xterm.js 自行关闭鼠标追踪
- shell prompt 等后续输出 → 正确显示

同时删除不再需要的 `_build_mode_reset_seq()` 辅助函数（无调用方）。

| 文件 | 改动 |
|------|------|
| `src/services/terminal_manager.py` | `_on_pty_readable()` alt→normal 分支：移除 `_build_mode_reset_seq() + full_screen_dump()`，改为直通原始 `text`（`self._broadcast_output(text)`）；`feed_and_render` 仅用于让 pyte 状态同步 |
| `src/services/terminal_manager.py` | 删除无调用方的 `_build_mode_reset_seq()` 模块级函数；移除 `was_mouse`/`now_mouse` 变量（不再需要跟踪鼠标状态变化） |

## 调试排查记录

### 2026-04-15 第一轮排查：Broker 直通模式验证

**用户反馈**：部署后 Broker 模式仍无法鼠标滚轮滚动。

**排查步骤**：

1. **容器诊断日志确认** — 所有 Broker 输出都走了 `NORMAL 直通` 路径 ✅
2. **远端 shell 鼠标追踪检测** — 远端未启用 `?1000h`/`?1002h`/`?1003h`，仅有 DECCKM + BracketedPaste ✅
3. **远端 shell prompt ANSI 序列分析** — 有 `\r\n` 自然换行，scrollback 正常 ✅
4. **pyte HistoryScreen 模拟** — scrollback 正常积累 ✅

**初步结论**：后端直通逻辑正确，需前端验证。

### 2026-04-15 第二轮排查：xterm.js 状态残留

**用户反馈**：切换为 Broker 后无法滚动，但**刷新页面后就可以了**。

**根因分析**：

1. TMUX 模式运行时，tmux 向 xterm.js 发送 `\x1b[?1000h`（MouseTracking SET）+ `\x1b[?1002h`（MouseBtnTrack SET）+ `\x1b[?1006h`（SGRMouse SET）
2. 点击切换到 Broker 时，`terminal.clear()` 只清空屏幕内容和 scrollback buffer，**不重置 xterm.js 的 DEC Private Mode 状态**
3. Broker 连接后，远端 shell 不会发送 `\x1b[?1000l`（它不知道之前 TMUX 启用了鼠标追踪）
4. xterm.js 继续认为鼠标追踪是开启的 → 鼠标滚轮事件被编码为鼠标序列（通过 `onData` 发送到后端）
5. 后端 `write()` 方法检测到 pyte `mouse_tracking_enabled=False`，过滤掉鼠标事件 → **滚轮被完全吞掉**

**修复**：新增 `terminal.reset()` 方法，在 backend 切换时先写入 DEC Private Mode 重置序列再清屏，确保 xterm.js 鼠标追踪状态回归初始。

## 遗留问题

- 回归测试待验证：全局切换后所有 Tab 自动重连、多跳编排正常触发、xterm.js 滚动正常
- 问题 5 验证场景：Broker 模式下 vim → `:q` 退出 → 鼠标滚轮应恢复正常滚动；less/top 等全屏程序退出后同理
- 边缘场景：Normal→Alternate 切换帧（进入 vim）目前走 `else` 分支直通，`\x1b[?1049h` 原始序列直接广播给 xterm.js，应可正常切换
