# 实心 Accent 配色收敛 — 记录文档

- 日期：2026-11-23
- 状态：**已完成并部署验证**
- 前置：`tool-app-design-skill-research.md`（主题回归修复）中列为 P2 的遗留项
- 范围：`frontend/src` 实心绿底按钮 / toast / 徽标
- 提交：`ebd4140`

## 一、需求与背景

上一轮修复黑底黑字后，仍有一批实心 accent 按钮用白字。当时判断「底色更暗，
深色文字反而对比度不足，需要单独设计配色」。本轮用真实合成色测量验证，发现
该判断只对一半：真正的问题是**底色亮度跨越了对比度反转点**，不是「深色字一律不行」。

### 实测数据（叠在 `#050816` 深色画布上，WCAG 2 对比度）

| 实心底色 | 合成 RGB | 深墨字 | 白字 | 正确选择 |
|---|---|---|---|---|
| `emerald-500 solid` | `(0,188,125)` | **8.06** | 2.47 ✗ | 深墨 |
| `emerald-500 @90%`（登录按钮） | `(0,170,115)` | **6.65** | 3.00 ✗ | 深墨 |
| `emerald-600 solid` | `(0,153,102)` | **5.46** | 3.65 ✗ | 深墨 |
| `emerald-600 @90%`（toast） | `(0,139,94)` | **4.61** | 4.33 ✗ | 深墨（勉强） |
| `emerald-600 @80%`（上传/脚本按钮） | `(1,124,86)` | 3.82 ✗ | **5.23** | 白 |
| `emerald-700 solid`（重连） | `(0,122,85)` | 3.72 ✗ | **5.36** | 白 |

**反转点在 `emerald-600 @85%` 附近**。现有代码里 `600/700` 用白字其实是对的，
唯一真正不达标的是 `emerald-600/90` toast（白字 4.33:1 < 4.5）。

### 根本问题：7 种实心绿底是历史堆积

`500/90`、`500/80`、`600/80`、`600/90`、`600`、`700`、`500` 并存，
没有统一语义，也没有维护亮度反转规则。符合 `interface-design` 的
「one button style / one accent」原则的做法是收敛，而不是维护两档语义类。

## 二、方案：统一到一条 accent 档

| 项 | 值 | 对比度（深墨字） |
|---|---|---|
| default | `emerald-500` | 8.06 |
| hover | `emerald-400` | 10.29（更亮，符合 Vercel「interactions increase contrast」） |
| active | `emerald-600` | 5.46 |

- 前景统一 `--ui-on-accent`（`#050816` 深墨）
- 半透明幽灵按钮（`500/5`~`/25`、`400/10` 等 **46 处**）**一律不动**，
  它们用亮绿字对比度 16~18:1，本来就很好；这也是上一轮黑底黑字的教训
- 计数徽标 `bg-emerald-400 text-gray-950` 已是 10.29:1，仅把 `text-gray-950`
  换成语义 token

### disabled 态（独立问题）

实测 `opacity-40` 双重衰减：底色降到 `(3,80,63)`，深墨字 1.46:1、白字 2.85:1，
两种前景都不达标。改为**显式中性灰底 + muted 文字**，不用 opacity。

## 三、设计原则

| 原则 | 体现 |
|---|---|
| 单一职责 / 高内聚 | 配色决策全部收敛到 `design-system.css` 的语义类，组件只声明「我是主操作」 |
| 显式优于推断 | 组件加 `.ui-accent-bright`，不再靠 `[class*="bg-emerald..."]` 反查类名 |
| DRY | 7 种散落 className → 1 个语义类 |
| 语义 token | `--ui-accent-surface/hover/active/on-accent/disabled-*`，无硬编码色值散落 |
| 不扩大改动面 | 半透明按钮不动，`transition-all` 等其它 P2 项不在本轮范围 |

## 四、实施进展

- [x] 配色实测与方案设计
- [x] 创建本记录文档
- [x] CSS：token 与 `.ui-accent-bright` 三态 + disabled 中性态
- [x] 组件：7 个文件 8 处改用语义类
- [x] 构建 + 浏览器四态对比度实测 + 断言脚本
- [x] 提交推送 + dev 部署 + 线上验证
- [x] 更新记录文档收尾

### 改动清单

| 文件 | 改动 |
|---|---|
| `frontend/src/styles/design-system.css` | 新增 `--ui-accent-surface/hover/active`、`--ui-accent-surface-disabled`、`--ui-accent-ink-disabled`；实现 `.ui-accent-bright` 三态 + disabled；删除已失效的 `.ui-on-accent` 别名 |
| `frontend/src/components/LoginPage.tsx` | 登录按钮 → `ui-accent-bright` |
| `frontend/src/components/TerminalView.tsx` | toast、断连重连按钮 → `ui-accent-bright` |
| `frontend/src/components/DisconnectOverlay.tsx` | 重连按钮 → `ui-accent-bright` |
| `frontend/src/components/SnippetPanel.tsx` | 执行命令按钮启用态 → `ui-accent-bright` |
| `frontend/src/components/FileTransferPanel.tsx` | 下载按钮 → `ui-accent-bright` |
| `frontend/src/App.tsx` | 计数徽标 → `ui-accent-bright` + 保留 `ui-micro` |
| `frontend/e2e/verify-accent-contrast.mjs` | **新增**：CDP 对比度断言脚本 |

**未改动**：46 处半透明幽灵按钮（含 `CommandPalette` 选中项 17.87:1）、
`emerald-800` token（探针解析异常，未引入）。

### 验证结果

**对比度断言脚本（真实浏览器解析 oklch 后计算）**

| 状态 | 背景 | 前景 | 对比度 | 判定 |
|---|---|---|---|---|
| default | `emerald-500` | 深墨 `#050816` | **8.06:1** | PASS |
| hover | `emerald-400` | 深墨 | **10.29:1** | PASS |
| active | `emerald-600` | 深墨 | **5.46:1** | PASS |
| disabled | 中性灰 `rgba(255,255,255,.06)` | `--ui-text-muted` | **7.25:1** | PASS |

规则完整性检查全 PASS：base / hover / active / disabled 规则均存在、
无 `opacity-40` 双重衰减。

**线上部署**
- wetty-terminal MCP 在 `dev-cloud:/root/go/src/github.com/web-terminal` 执行
  `git pull --ff-only` → `ebd4140`；`docker compose build` + `up -d`
  （后者由宿主机直连 SSH 完成，避免 MCP 服务端随容器重建中断会话）
- 服务 HTTP 200，bundle `index-pbprThRK.css`
- 线上断言脚本四态全部 PASS
- 登录页真实 DOM 实测：`bg = oklch(0.696 0.17 162.48)`（emerald-500）、
  `color = rgb(5,8,22)`、`opacity = 1`
- checker 线上复跑：`FAIL 17→12`、`WARN 11→9`，
  **`text-contrast: PASS`、新增 FAIL 0 / 新增 WARN 0**

### 实施中发现并修正的两处

1. **disabled 的 opacity 是独立属性**：组件里 `disabled:opacity-40` 不会因为
   语义类改了 background/color 就消失，必须显式 `opacity: 1` 抵消，
   否则仍是双重衰减（实测仅 1.46:1）。
2. **断言脚本选中了空壳标签页**：多个同 URL tab 中部分未加载样式表，
   token 读为空。改为逐个探测 `--ui-accent-surface` 后自动选出有效页。
   另外静态 dist 重建后需 `Page.reload({ignoreCache:true})` 才会取到新 CSS。

## 五、未完成任务

- 本轮范围内无未完成项
- 其余 P2 见下节遗留问题 6

## 六、遗留问题与风险

1. **hover 态已端到端验证**：组件里原有 `hover:bg-emerald-*` / `transition-colors`
   已从 8 处 className 中移除，语义类统一接管三态。用 CDP
   `CSS.forcePseudoState` 强制 `:hover` / `:active` 后读真实计算样式：
   - default `oklch(0.696 0.17 162.48)`（emerald-500）
   - hover `oklch(0.765 0.177 163.223)`（emerald-400）
   - active `oklch(0.596 0.145 163.225)`（emerald-600）
   - 三态前景恒为 `rgb(5,8,22)`、`opacity` 恒为 1，无旧 className 抢优先级。
2. **toast / 徽标是 div/span 而非 button**：语义类不能限定元素类型，用 `:where()` 保持低特异性。
3. **disabled 态需同时覆盖 `:disabled` 与 aria-disabled**，避免遗漏。
4. **emerald-800 token 解析异常**：探针里 `var(--color-emerald-800)` 返回白色，
   故 active 用已验证的 `emerald-600`，不引入未验证 token。
5. **对比度断言脚本依赖浏览器环境**：放入 `frontend/e2e/` 供手动/CI 调用，
   不接入构建流程（避免增加构建复杂度）。
6. **其它 P2 未做**：9 处 `transition-all`、表单 `role="status"`、
   `.interface-design/system.md` 设计契约、评审工具切换。
