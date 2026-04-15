# PTY Broker 演进路线图与实施进展

## 1. 背景与问题定义

当前项目已经具备基于 `tmux` 的共享终端能力，但随着浏览器终端、MCP Agent、多跳 SSH、复制粘贴、历史缓冲和重连恢复等需求逐步增强，单纯依赖 `tmux` 作为核心承载层会带来几个问题：

- 浏览器体验与服务端会话能力之间存在边界，历史记录、复制粘贴、重连恢复等能力不够统一。
- 终端承载逻辑过度依附 `tmux`，不利于做更细粒度的观测、诊断、回放与产品能力沉淀。
- 想尝试新的会话能力时，缺少一个可灰度、可切换、可回滚的演进路径。

因此本项目的目标不是简单“移除 `tmux`”，而是**在保证现有稳定性的前提下，逐步演进出平台自身可控的 PTY Broker 能力**。

---

## 2. 总体目标效果（North Star）

最终期望达到的效果：

### 2.1 用户侧效果
- 普通用户默认无感知，打开终端即可使用。
- 浏览器终端在历史记录、复制粘贴、重连恢复方面表现更加稳定一致。
- 多跳 SSH、堡垒机跳转、Agent 接管等场景可以共享同一条终端能力链路。

### 2.2 平台侧效果
- 终端后端支持 `tmux` / `broker` 双栈可切换。
- 会话创建、复用、切换、断开、恢复具备统一抽象，而不是分散在多条实现路径中。
- 系统能够明确知道某个会话当前由哪个 backend 承载，并具备基础观测能力。
- 后续可以在 `broker` 路径上继续扩展：
  - 服务端历史缓冲
  - 更细粒度的复制粘贴控制
  - 重连恢复
  - 会话诊断与回放
  - 更强的多端共享能力

### 2.3 架构目标
- **短期**：保留 `tmux` 作为默认稳定路径。
- **中期**：孵化 `broker` 的最小可用版本（MVP），支持调试切换与对比验证。
- **长期**：基于实际稳定性、可维护性和产品能力证据，决定是否让 `broker` 成为默认承载方案。

---

## 3. 演进原则

本路线按以下原则推进：

- **保持现状可用**：默认 `tmux` 不回归，不因为引入 `broker` 影响当前主流程。
- **双栈灰度**：`tmux` / `broker` 并行存在，通过 feature flag 或参数切换。
- **单会话单后端**：同一个会话只绑定一个 backend，不做双写双活，避免状态不一致。
- **先打底再增强**：先解决“能切换、能复用、能观测”，再进入历史、复制粘贴、恢复等增强能力。
- **先证据后决策**：是否替换默认 backend，不靠预期判断，而靠稳定性和体验数据。

---

## 4. Roadmap

## Sprint 1：抽象层打底（已完成）

### 目标
把 `tmux` 从“唯一会话承载方式”升级为“后端实现之一”，完成统一抽象和开关链路打底。

### 范围
- 引入 `TerminalBackend` 抽象。
- `TerminalManager` 支持按 backend 创建、复用与切换会话。
- REST API、MCP、前端请求链路支持透传 backend。
- 默认仍走 `tmux`，显式指定时可切换到 `broker`。
- 增加定向测试，验证基础切换与复用行为。

### 验收标准
- 默认行为与原有 `tmux` 路径保持一致。
- 显式指定 `broker` 时可成功创建并承载终端会话。
- backend 切换时会停止旧会话并重建新会话。
- 不引入 `tmux` 缺失环境下的后台清理报错。

### 当前结论
**已完成。** 目前已经具备“后端可切换”的基础能力，但 `broker` 仍处于承载层 MVP 的起点阶段。

---

## Sprint 2：Broker MVP 可用化（下一阶段优先项）

### 目标
让 `broker` 不只是“可以直连 SSH”，而是“可以稳定承载一个真实终端会话”。

### 实施原则
- 先做 **单跳直连稳定性**，暂不把多跳/堡垒机复杂编排混入同一阶段。
- 先保证 **浏览器与 MCP 共用同一会话**，再做更高级的历史回放与恢复。
- 先建立 **可观测性与诊断能力**，避免问题出现后无法判断卡在哪一层。
- 本阶段默认不做大规模 UI 扩展，除非它是验证 `broker` 能力所必需的。

### 实施任务清单

#### Task 2.1：Broker 会话生命周期收口（P0）
**目标**：让 `broker` 路径下的会话启动、运行、退出、清理具备稳定一致的生命周期。

- **实施项**：
  - 梳理 `TerminalSession.start()` / `stop()` 在 `broker` 路径下的行为边界。
  - 明确 broker 子进程退出后的状态收口方式，包括 `_running`、fd 清理、WebSocket 收尾。
  - 补齐 `broker` 模式下的异常退出日志，至少能区分“SSH 失败”“PTY 关闭”“子进程异常退出”。
  - 检查 backend 切换时旧 broker 会话是否被可靠停止，避免残留子进程或 fd。
- **交付物**：
  - `terminal_manager` 中 `broker` 生命周期处理逻辑收口。
  - 对应的单元测试或定向回归测试。
- **验收标准**：
  - `broker` 会话可正常启动与停止。
  - 关闭标签页、显式 stop、backend 切换时不会残留僵尸 broker 进程。
  - 会话异常退出后状态一致，不出现“前端以为活着、服务端已经断了”的分裂状态。

#### Task 2.2：浏览器 / MCP 共会话稳定性（P0）
**目标**：验证 `broker` 下浏览器终端与 MCP Agent 操作的是同一个会话，而不是两个相互独立的 PTY。

- **实施项**：
  - 验证 `TerminalManager.create_session()` 在 `broker` backend 下的复用逻辑。
  - 用 MCP 建立 `broker` 会话后，浏览器接管时确认能够看到同一终端状态。
  - 用浏览器先建立 `broker` 会话后，再由 MCP 执行命令，确认输出落在同一缓冲区。
  - 补充事件与状态字段，便于判断当前复用的是哪一个 session。
- **交付物**：
  - 定向测试用例与手工验证记录。
  - 必要的状态字段补充。
- **验收标准**：
  - MCP 与浏览器能共享同一个 `broker` 会话。
  - 任一端输入后，另一端可观察到结果。
  - 不出现同一实例名下重复创建多个 broker 会话的情况。

#### Task 2.3：Broker I/O 与终端行为补齐（P0）
**目标**：确保 `broker` 会话在最基础的终端交互上具备可用性。

- **实施项**：
  - 验证并补齐 `resize` 行为，确保窗口变化不会导致 broker 会话显示错乱。
  - 检查常见输入路径：普通命令、回车、方向键、长文本粘贴。
  - 检查 `wait_for` / `send_command` 在 broker 下的行为是否与 tmux 路径兼容。
  - 确认 broker 下常见 shell 提示符匹配是否稳定。
- **交付物**：
  - `broker` 交互兼容性修复。
  - 针对关键输入路径的测试或验证脚本。
- **验收标准**：
  - 常见命令输入和回显稳定。
  - 终端尺寸变化后不出现明显布局异常。
  - MCP 的 `run_command` / `wait_for_output` 在 broker 下可正常使用。

#### Task 2.4：基础可观测性与错误诊断（P1）
**目标**：当 broker 失败时，能够快速判断问题发生在 PTY、WebSocket、SSH 还是复用逻辑。

- **实施项**：
  - 增加 broker 场景下的关键日志：启动参数摘要、退出原因、backend 切换结果、会话复用命中情况。
  - 补充会话状态字段，至少要能看到 `backend`、运行状态、最近错误来源。
  - 统一 `API` / `MCP` 返回中的 broker 失败提示，避免出现模糊错误。
- **交付物**：
  - 统一后的日志与错误信息策略。
  - 必要的会话状态扩展。
- **验收标准**：
  - 出错时能快速判断在哪一层失败。
  - 常见失败提示可直接支持排障，不需要阅读大量源代码才能定位。

#### Task 2.5：Broker 定向验证闭环（P1）
**目标**：建立一组针对 `broker` MVP 的固定验证清单，作为后续迭代的回归基线。

- **实施项**：
  - 整理 broker 手工验证 checklist：启动、复用、切换、停止、MCP 接管、浏览器接管、resize、异常退出。
  - 增补自动化测试，优先覆盖本阶段最核心行为。
  - 在文档中沉淀验证命令、期望结果和已知限制。
- **交付物**：
  - Sprint 2 的验证 checklist。
  - 新增或增强的自动化测试。
  - 文档中的验证证据与已知问题记录。
- **验收标准**：
  - 每次修改 broker 相关逻辑，都有固定回归入口。
  - 能明确判断某次改动是否让 broker 更接近可灰度状态。

### 推荐推进顺序
建议按以下顺序推进，避免多线并发导致问题难以归因：

1. **Task 2.1**：先收口生命周期。
2. **Task 2.2**：再验证浏览器 / MCP 共会话。
3. **Task 2.3**：补基础交互与 resize。
4. **Task 2.4**：增强可观测性与错误诊断。
5. **Task 2.5**：最后整理成稳定的验证闭环。

### Sprint 2 完成判定
当以下条件同时满足时，可以认为 Sprint 2 基本完成：

- `broker` 可稳定承载单跳 SSH 会话。
- 浏览器与 MCP 可以共享同一 broker 会话。
- 常见输入、回显、resize、停止行为稳定。
- 出错时能够判断问题所在层级。
- 已形成一套可重复执行的 broker 定向验证流程。

### 交付重点
这一阶段的重点不是 UI，而是**证明 broker 已具备最小生产可验证性**。

---

## Sprint 3：用户体验增强

### 目标
开始逼近 `tmux` 当前积累的终端体验，并补上 broker 路径更适合承载的能力。

### 计划推进项
- 服务端历史缓冲与重连后补历史。
- 复制粘贴增强（尤其中文、多字节文本、特殊终端序列兼容）。
- 统一浏览器终端体验，减少对本地 `xterm.js` scrollback 的单点依赖。
- 增加显式 backend 展示与可视化切换能力（如果需要对普通用户开放）。

### 验收标准
- `broker` 会话断线后能恢复基本上下文。
- 中文复制粘贴稳定，行为与 `tmux` 路径至少等价。
- 用户或开发者能明确知道当前终端会话使用的 backend。

---

## Sprint 5（原 Sprint 4）：替换决策与灰度推广

### 目标
不是盲目替换，而是基于证据决定 `broker` 的角色。

### 需要比较的维度
- 稳定性
- 用户体验
- 多跳 SSH / 堡垒机场景适配复杂度
- 维护成本
- 故障诊断效率
- 后续产品化能力扩展空间

### 可能结果
- 保持双栈：`tmux` 稳定，`broker` 继续试验。
- 默认切换到 `broker`，`tmux` 作为兜底兼容方案保留。
- 分场景切换：例如直连默认 `broker`，复杂多跳暂保留 `tmux`。

---

## 5. 当前阶段判断

如果按上述 roadmap 来看，当前项目处于：

- **Sprint 1 已完成** — 后端抽象层打底
- **Sprint 2 已完成** — Broker 会话生命周期与稳定性
- **Sprint 3 已完成** — 多客户端 min-size + VirtualTerminal + 端点改造
- **准备进入 Sprint 4** — 用户体验增强

也就是说：

- Broker 模式已具备完整的核心能力：原生 PTY 直连 SSH、统一退出处理、scrollback 缓冲区、min-size resize 策略、VirtualTerminal 差分渲染、全屏快照恢复。
- TMUX 模式行为完全不受影响，双栈并行可切换。
- 下一步重点是验证真实场景下的稳定性与用户体验，并评估是否需要进入体验增强阶段。

---

## 6. 已完成内容

### Sprint 1：后端抽象层打底

#### 6.1 后端抽象层
- 新增 `src/services/terminal_backend.py`：
  - `TerminalBackend`
  - 默认后端读取
  - `WETTY_SESSION_BACKEND` 环境变量解析

#### 6.2 会话管理改造
- 完成 `src/services/terminal_manager.py` 改造：
  - `TerminalSession` 支持 `tmux` / `broker`
  - `TerminalInfo` 增加 `backend`
  - `TerminalManager.create_session()` 支持 backend 复用与切换
  - `cleanup_zombie_sessions()` 在无 `tmux` 环境下安全跳过

#### 6.3 API 与 MCP 链路贯通
- `src/api/terminal.py`
  - `StartTerminalRequest` 支持可选 `backend`
  - `TerminalResponse` / 终端列表返回实际 backend
- `src/mcp_server/server.py`
  - `connect_host()` 支持可选 `backend`
  - `_connect_path()` 将 backend 透传到 `TerminalManager`
  - 会话创建事件、连接结果、状态查询返回 backend

#### 6.4 前端调试开关
- `frontend/src/services/api.ts`
  - 新增 `TerminalBackend` 类型
  - `TerminalInstance.backend`
  - `startTerminal(hostId, backend?)`
  - 支持通过 URL 参数 `terminalBackend` 或 `localStorage` 的 `wetty-terminal-backend` 做调试切换

### Sprint 2：Broker 会话生命周期与稳定性

#### 6.5 会话退出处理
- 新增 `SessionExitReason` 枚举（NORMAL / SSH_FAILED / PTY_CLOSED / CHILD_CRASHED / STOPPED / UNKNOWN）
- 新增 `_classify_exit()` 静态方法：分析 `os.waitpid()` raw_status
- 新增 `_handle_child_exit()` 统一退出处理入口
- 新增 `_async_exit_cleanup()` 异步资源清理 + `session_exit` WebSocket 通知
- 新增 `on_exit` 回调机制（`OnExitCallback` 类型、`add_on_exit()` / `_fire_on_exit()`）
- `TerminalInfo` 增加 `exit_reason` / `exit_code` 字段

#### 6.6 Scrollback 缓冲区
- `_scrollback: bytearray`（可配置容量，默认 256KB）
- `_append_scrollback(data)` 超出容量时裁剪头部
- `get_scrollback() -> bytes` 用于新客户端历史回放

#### 6.7 可观测性增强
- `start()` 增加结构化日志（host/port/user/backend）
- `stop()` 记录 reason
- 子进程退出日志包含 session_id / pid / reason / exit_code

### Sprint 3：多客户端 + VirtualTerminal + 端点改造

#### 6.8 ClientInfo + min-size resize
- 新增 `ClientInfo` dataclass：`ws` / `client_id` / `cols` / `rows` / `connected_at`
- `_ws_clients` 从 `list[WebSocket]` 升级为 `dict[str, ClientInfo]`
- `add_ws_client()` 返回 `client_id`，新客户端自动发送历史/快照
- `remove_ws_client()` 按 client_id 移除 + 自动重算 min-size
- `remove_ws_client_by_ws()` 兼容旧调用方式
- `resize(cols, rows, client_id=None)` 双路径：Broker → min-size 策略；TMUX → 直写 PTY
- `_compute_min_size()` 取 min(cols) × min(rows)，下限 10×3
- `_broadcast_resize_hint()` 通知所有客户端有效尺寸

#### 6.9 VirtualTerminal (pyte) 集成
- `src/services/virtual_terminal.py` 从 DEPRECATED 恢复为活跃模块
- 懒加载 `_get_virtual_terminal_class()` 避免 pyte 未安装影响 TMUX 模式
- Broker 模式 `__init__` 自动创建 VirtualTerminal
- 输出路径分支：Broker+vterm → `feed_and_render()` → 差分 ANSI；TMUX → 直通
- 新客户端连接分支：Broker+vterm → `full_screen_dump()` 快照；TMUX → scrollback 回放
- 依赖新增：`pyte>=0.8.0`（requirements.txt + pyproject.toml）

#### 6.10 terminal.py WebSocket 端点改造
- `client_id = session.add_ws_client(websocket)` → 捕获并透传
- `session.resize(cols, rows, client_id=client_id)` → 支持 min-size 策略
- `session.remove_ws_client(client_id)` → finally 中按 client_id 清理

#### 6.11 前端 resize_hint / session_exit 消息处理
- `useWebSocket.ts` 新增 `resize_hint` 和 `session_exit` 消息类型
- `resize_hint` → 触发 `onResizeHint` 回调
- `session_exit` → 触发 `onDisconnect(reason)` 回调

### 6.12 测试总览
- `tests/test_terminal_backend.py`：27 个测试全部通过
  - Sprint 1 基础测试（6 个）：复用/切换/zombie 清理/API 透传/MCP 透传/状态查询
  - Sprint 2 生命周期测试（8 个）：exit 分类×4 / TerminalInfo 退出字段 / on_exit 回调 / scrollback×2
  - Sprint 3 ClientInfo/min-size 测试（8 个）：创建/单客户端/多客户端/下限/空/add/remove/compat
  - Sprint 3 VirtualTerminal 测试（5 个）：feed_and_render / full_screen_dump / resize / broker 自动创建 / tmux 无 vterm

---

## 7. 验证结果

### Sprint 1 验证
- `python3 -m compileall src tests` ✅
- `cd frontend && npm run build` ✅
- `python -m pytest -q tests/test_terminal_backend.py` — 6 passed ✅

### Sprint 2 + Sprint 3 验证
- `python -m pytest -q tests/test_terminal_backend.py` — **27 passed in 0.99s** ✅
- `cd frontend && npm run build` ✅
- 依赖验证：`pip install "pyte>=0.8.0"` ✅

### 当前验证结论
- backend 抽象链路已经贯通。
- Broker 模式具备完整的生命周期管理：启动 / 退出分类 / 资源清理 / 回调通知。
- 多客户端 min-size resize 策略已实现并通过测试。
- VirtualTerminal (pyte) 差分渲染和全屏快照已集成并通过测试。
- TMUX 模式行为完全不受影响（双路径分支设计）。
- 前端已支持 `resize_hint` 和 `session_exit` 新消息类型。

---

## 8. 下一步推进建议（按优先级）

### P0：真实场景端到端验证
- 部署到测试环境，验证 Broker 模式的单跳 SSH 真实连接稳定性。
- 验证多浏览器客户端共享 Broker 会话时的 min-size 策略和 VirtualTerminal 渲染效果。
- 验证 vim / top / less 等全屏程序在 VirtualTerminal 模式下的兼容性。

### P1：用户体验增强（Sprint 4）
- 断线重连恢复（利用 VirtualTerminal 全屏快照）。
- 复制粘贴增强（中文、特殊终端序列、bracketed paste）。
- 前端 `resize_hint` StatusBar 展示（当前已有回调，需要 UI 消费）。

### P2：前端显式切换 UI
- 当前已支持 URL / `localStorage` / REST / MCP 四种切换方式。
- 如果要让普通用户或测试人员更方便地切换，可补一个可视化 backend 切换控件。

### P2：会话录制与回放
- VirtualTerminal 的 Screen 状态可序列化，为后续会话录制 / 回放 / 审计提供基础。

---

## 9. 已知遗留问题

### 9.1 全量 `pytest` 不是当前有效验收门槛
原因有两类：

- 本机全局 Python 缺少项目依赖，需要使用工作区 `.venv`。
- `tests/test_concurrency.py` 仍引用已不存在的 `src.services.wetty_manager`，属于既有历史测试问题，不是本轮 PTY Broker 抽象层引入的回归。

### 9.2 前端还没有显式 backend 切换控件
- 当前已经支持 URL / `localStorage` / REST / MCP 四种切换方式。
- 是否继续做 UI，要看下一阶段是否需要对更多测试角色开放。

### 9.3 VirtualTerminal 高速输出场景
- `pyte` 是纯 Python 实现，`cat` 大文件等高速输出场景可能有延迟。
- 当前已实现 `FULL_DUMP_THRESHOLD` 保护（dirty 行数 > 80% 时回退全屏 dump）。
- 后续可评估是否需要更激进的降级策略（如输出速率检测 + 临时直通）。

---

## 10. 当前文档的使用方式

后续每次推进时，建议按以下规则维护本文件：

- **目标效果**：一般不改，除非路线发生调整。
- **Roadmap**：阶段推进完成后更新状态。
- **本轮已完成内容**：记录当前实际落地项。
- **验证结果**：记录真实验证证据。
- **下一步推进建议**：每轮结束后重排优先级。
- **已知遗留问题**：只记录真实阻塞，不混入主观推测。

这样这份文档就不只是“做过什么”，而是能持续回答：

- 总目标是什么
- 当前在哪个阶段
- 下一步应该推进什么
- 为什么这么推进
