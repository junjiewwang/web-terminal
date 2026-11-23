# 终端列宽不一致导致 zsh 智能提示显示错乱 — 修复记录

- 日期：2026-11-23
- 状态：实施中
- 关联组件：前端 xterm.js / useWebSocket / useTerminal；后端 terminal.py / terminal_manager.py

## 一、需求与背景

Web 终端对 zsh 智能提示插件（zsh-autosuggestions）兼容性不佳：**提示内容较长时显示错乱**，
表现为命令逐段显示、字符散落、行尾残留空白、光标乱跳。短提示时正常。

目标：让终端渲染列宽与后端 PTY / shell 认知的列宽保持一致，消除长提示重绘错位；
并把修复推送、部署到 dev 环境做验证。

## 二、现象与证据

排查使用了三条路径：本地代码分析、远程主机终端（MCP）、浏览器 CDP。

| 观测项 | 实测值 |
|--------|--------|
| 浏览器 xterm.js 渲染列数 | 约 136~200 列（宽窗口 + 14px 等宽字体） |
| 后端 PTY winsize (`stty size`) | 80 × 24 |
| shell 认知 `COLUMNS` | 80 |
| 长提示重绘时的清行补白 | 正好 80 个空格，且随 `stty cols` 改为 200 后变为 200 个空格 |

zsh 环境同时启用了 `zsh-autosuggestions`、`zsh-syntax-highlighting`、
`zsh-history-substring-search`，每次按键都会整行重绘。

**判定**：zsh ZLE 按 80 列计算折行与光标回退，xterm.js 按约 200 列渲染。
提示长度超过 80 列时触发 ZLE 多行折行重绘，`\r` / `\x1b[K` / 相对光标序列落到错误行列，
因此"提示越长越明显"。

## 三、根因（代码层）

1. `src/api/terminal.py` 建立 WebSocket 时调用 `add_ws_client(websocket)` 未传尺寸，
   `terminal_manager.py` 中 `ClientInfo` 默认 `cols=80, rows=24`。
2. Broker 模式 `min-size` 策略取所有客户端 `min(cols)`。只要有一个客户端未上报尺寸
   （或保持默认值），PTY 就被钉在 80×24，浏览器再宽也无效。
3. 后端广播的 `resize_hint`（含 effective_cols/rows）**前端未消费**：
   `useWebSocket.ts` 已定义 `onResizeHint` 回调，但 `TerminalView.tsx` 未传入，
   UI 既不提示、也不把 xterm.js 对齐到有效尺寸，两侧长期静默错配。
4. 叠加因素：前端仅剥离 OSC 52，其余 ANSI 序列全交给 xterm.js 解析，无补救层；
   后端 64KB 分块逐块独立 UTF-8 解码，长提示含中文且跨块时可能产生替换字符。

## 四、方案设计

遵循单一职责与高内聚低耦合：尺寸协商收敛到后端 `ClientInfo`，
渲染对齐收敛到 `useTerminal`，组件层只做装配，不散落特判。

| 层 | 改动 | 意图 |
|----|------|------|
| 后端 `ClientInfo` | 增加 `size_reported` 标记 | 区分"已上报真实尺寸"与"默认值" |
| 后端 `_compute_min_size()` | 仅统计已上报尺寸的客户端 | 消除默认 80×24 对 min 的污染 |
| 后端 `add_ws_client()` / `resize()` | 支持并维护尺寸上报状态 | 单一入口完成尺寸协商 |
| 后端 `terminal_websocket()` | 接受握手阶段的 cols/rows 参数 | 连接首帧即使用真实尺寸 |
| 前端 `useWebSocket` | 握手 URL 带初始尺寸；`resize_hint` 保持透传 | 网络层只负责传输 |
| 前端 `useTerminal` | 新增 `applyEffectiveSize()` 幂等对齐，并在 `fit()` 时施加约束 | 渲染层统一负责列宽对齐，避免 fit 与 hint 来回震荡 |
| 前端 `TerminalView` | 装配 `getInitialSize` 与 `onResizeHint` | 组件层只做接线 |

**防循环设计**：`applyEffectiveSize` 做幂等判断（尺寸相同则 no-op）；
`term.resize()` 触发 `onResize` → `sendResize` → 后端 min 不变 → 广播相同 hint → 前端 no-op，循环终止。

**取舍**：min-size 策略保留（多客户端共享 PTY 时防止小窗口被截断），
改为"所有客户端都按 effective size 渲染"，而不是让 PTY 盲目跟随最大窗口。

## 五、实施进展

- [x] 排查定位根因，完成方案设计（2026-11-23）
- [x] 后端尺寸协商改造
- [x] 前端列宽对齐改造
- [x] 本地构建 / lint / 测试
- [ ] 提交并推送
- [ ] dev 环境拉取更新并重新部署
- [ ] 浏览器验证长提示显示

### 具体改动

| 文件 | 改动 |
|------|------|
| `src/services/terminal_manager.py` | `ClientInfo` 增加 `size_reported`；`add_ws_client` 用 `None` 哨兵区分显式上报与默认；`resize()` 标记已上报；`_compute_min_size()` 只统计已上报客户端 |
| `src/api/terminal.py` | WebSocket 端点新增 `cols`/`rows` 握手参数 + 范围校验（10~500 列 / 3~200 行），注册时传入真实尺寸 |
| `frontend/src/hooks/useWebSocket.ts` | 新增 `getInitialSize` 回调；握手 URL 通过 `URLSearchParams` 携带 `cols`/`rows` |
| `frontend/src/hooks/useTerminal.ts` | 新增 `applyEffectiveSize()` 幂等约束；`getSize()` 改为返回容器自然尺寸（`proposeDimensions`）；`fit()` 后重新施加约束；约束对齐时抑制 `onResize` 上报 |
| `frontend/src/components/TerminalView.tsx` | 装配 `getInitialSize` / `onResizeHint`；断连清除约束；状态栏展示有效尺寸徽标 |
| `tests/test_terminal_backend.py` | 新增 4 个单测（忽略未上报客户端、全未上报回退、显式上报标记、握手尺寸校验） |

### 验证结果

- 前端 `npm run build`：`tsc -b && vite build` 通过
- 后端单测：改动前基线 `16 failed / 112 passed / 8 errors`，改动后
  `16 failed / 116 passed / 8 errors` —— 失败项完全一致（预存的
  `TerminalInfo` 构造参数、`bcrypt` 依赖缺失等），新增 4 个测试全部通过，**无回归**
- IDE lint：0 错误

## 六、未完成任务

- 提交并推送
- dev 环境拉取更新并重新部署
- 浏览器验证长提示显示

## 七、遗留问题与风险

1. **多客户端共享 PTY**：若两个窗口尺寸不同，effective 取 min，
   较大窗口的 xterm.js 会被对齐到较小尺寸，右侧出现留白（不错乱，但有空白）。
   后续可考虑"以活跃客户端为准"或 UI 显式提示。
2. **UTF-8 跨块解码**：`os.read(65536)` 后逐块 `decode(errors="replace")`，
   多字节字符跨块会产生替换字符。本次未改，列为后续优化（P1）。
3. **Alternate Screen 差分渲染**：`virtual_terminal.py` 生成的 `\x1b[2K` 等序列
   依赖列宽一致，本次修复后应同步改善，但未做专项验证。
4. **无自动化测试覆盖**：列宽一致性目前依赖浏览器人工/CDP 验证，
   建议后续补前端单测（resize_hint → term.resize 幂等）。

## 八、验证方式

1. 浏览器中连接终端，读取 xterm.js 的 `cols` 与后端 `stty size` / `echo $COLUMNS`，三者应一致。
2. 触发长历史命令提示（输入长命令前缀），确认提示完整显示、无逐字符散落与残留空白。
3. 缩放浏览器窗口，确认 `fit` 后列宽仍与 PTY 一致、无来回震荡。
