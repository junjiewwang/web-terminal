# P2 设计治理收尾 — 记录文档

- 日期：2026-11-23
- 状态：**已完成并部署验证**
- 前置：`tool-app-design-skill-research.md` 与 `accent-palette-consolidation.md` 中列为 P2 的遗留项
- 选型：**A**（零第三方依赖、项目内自持），不执行 `npx skills add`
- 最终提交：`95b5a34`

## 一、需求

收尾四件事，并把可自动化的规则做成项目内回归断言：

1. 8 处 `transition-all` 按元素语义收敛，UI 动效时长 ≤300ms
2. 表单/面板状态补齐 ARIA 播报
3. 落地 `.interface-design/system.md` 设计契约
4. 写评审手册（工具型 skill 指引 + 本项目豁免表）
5. 整理 e2e：CDP 断言改造为 Playwright 版、新增设计规则断言、`test:e2e` 统一入口

## 二、调研发现（决定方案形态）

### `system.md` 是「决策持久化产物」而非模板

`interface-design` 的 SKILL.md 原文工作流：
- 若 `.interface-design/system.md` 存在 → **直接读、照做**（decisions are made）
- 不存在 → 领域探索 → 提议 → 确认 → 构建 → offer to save
- 保存字段：Direction and feel / Depth strategy + spacing base /
  Hierarchy decisions / Key component patterns（带具体尺寸）/ Visual direction notes

所以不是填 TODO 骨架，而是把本项目这几轮**已定稿的真实决策**写进去，
且必须放在**项目根** `.interface-design/system.md`（skill 硬编码读此路径，不是 docs/）。

### 项目已有 e2e 约定，上轮新增的脚本不合群

`frontend/e2e/` 已有 8 个脚本，统一为 **Playwright + `page.route` mock 后端 +
`localhost:5173`**，可离线跑。上轮我加的 `verify-accent-contrast.mjs` 依赖
CDP 9222 + 线上页面，既不能离线跑也不进 CI。本次改造对齐现有约定。

### `transition-all` 实测 8 处（此前记成 9 处，已更正）

| 文件 | 处数 | 用途 |
|---|---|---|
| `FileTransferPanel.tsx` | 6 | 拖拽区 1 + 进度条 5 |
| `SnippetPanel.tsx` | 2 | 卡片 1 + 列表项 1 |

5 个进度条是 `transition-all duration-300/500` 配内联 `width`。
`interface-design` 要求「只 animate transform/opacity，Never transition:all」；
但进度条本质就是宽度变化，改 `transform: scaleX` 需重构 DOM 结构，
本轮按最小改动收敛为**显式 `transition-[width]`**（命名准确、不再 all），
并把 `duration-500` 收到 `duration-300`。

## 三、方案与验收标准

| 项 | 落地 | 验收 |
|---|---|---|
| transition | 卡片/列表项 → `transition-colors`；进度条 → `transition-[width]`；时长 ≤300ms | grep 无 `transition-all`、无 `duration-500`；hover 视觉复核 |
| ARIA | 错误块 `role="alert"` + `aria-live`；提交中 `aria-busy`；面板 error/loading 同理 | Playwright 查询 DOM 属性 |
| system.md | 项目根，逐条对照现有 CSS 实际值写，不凭记忆 | 字段与 skill build flow 对齐；数值与 CSS 一致 |
| 评审手册 | `docs/2026-11-23/design-review-handbook.md` | 含工具型 skill 指引、豁免表、安装命令（仅文档） |
| e2e | CDP 断言 → Playwright；新增 `verify-design-rules.mjs`；`package.json` 加 `test:e2e` | `npm run lint` + `npm run build` + `npm run test:e2e` 全 PASS |

**回归标准**：线上 checker `text-contrast: PASS`、新增 FAIL/WARN = 0；
hover/active/disabled 视觉与改动前一致。

## 四、实施进展

- [x] 调研与选型确认（A）
- [x] 创建本记录文档
- [x] P2-1 transition 收敛
- [x] P2-2 ARIA 播报
- [x] P2-3 `.interface-design/system.md`
- [x] P2-4 评审手册
- [x] e2e 整理
- [x] 验证 + 推送部署 + 更新文档

### 改动清单

| 文件 | 改动 |
|---|---|
| `frontend/src/components/FileTransferPanel.tsx` | 拖拽区 + 5 个进度条显式命名过渡；`aria-busy` 容器 + 状态文本 live region |
| `frontend/src/components/SnippetPanel.tsx` | 2 处过渡收敛；`sr-only` 独立状态播报 + 错误 `role="alert"` |
| `frontend/src/components/LoginPage.tsx` | `aria-busy`、错误 `role="alert"`、常驻 `role="status"` 状态行 |
| `frontend/src/App.tsx` | toast 动效 500 → 300ms |
| `.interface-design/system.md` | **新建**：项目根设计契约（skill 硬编码读此路径） |
| `docs/2026-11-23/design-review-handbook.md` | **新建**：评审手册 + 基线 + 豁免表 + 陷阱记录 |
| `frontend/e2e/browser.mjs` | **新建**：公共浏览器启动器，优先复用系统 Chrome |
| `frontend/e2e/run.mjs` | **新建**：`test:e2e` 串行入口 |
| `frontend/e2e/verify-design-rules.mjs` | **新建**：设计规则回归断言 |
| `frontend/e2e/verify-accent-contrast.mjs` | CDP 版 → Playwright + mock 版，支持 `BASE` 线上冒烟 |
| `frontend/package.json` | 新增 `test:e2e` script |

### 关键设计决策

- **`system.md` 放项目根**，不是 docs/：`interface-design` skill 硬编码读
  `.interface-design/system.md`，文件存在时「decisions are made」直接照做。
  内容逐条对照 `design-system.css` 真实值写，不是填模板骨架。
- **可交互子树不整体做 live region**：`SnippetPanel` 内容区、
  `FileTransferPanel` 拖拽区都含可点击控件，若包成 `role="status"` 会让
  读屏把子树当状态区。改为容器仅 `aria-busy` + 独立 `sr-only` status 播报。
- **e2e 对齐既有约定**：Playwright + `page.route` mock + `localhost:5173`，
  可离线跑；`BASE` 环境变量切线上冒烟。公共 `browser.mjs` 消除重复启动逻辑。

### 验证结果

| 项 | 结果 |
|---|---|
| `npm run build` | exit 0 |
| `npm run test:e2e`（本地 mock） | **2/2 脚本全 PASS** |
| `BASE=<线上> npm run test:e2e` | **2/2 脚本全 PASS** |
| IDE lint | 0 错误 |
| Playwright 真实 `hover()` | default `oklch(0.696)` → hover `oklch(0.765)`，前景恒深墨 |
| checker 线上复跑 | `FAIL 17→12`、`WARN 11→8`；**新增 FAIL/WARN 均为 0** |
| checker 明细 | `text-contrast: PASS`、`form-fields: PASS`、`focus-visible` 已修复 |

### 部署

- wetty-terminal MCP 在 `dev-cloud:/root/go/src/github.com/web-terminal` 执行
  `git pull --ff-only` → `95b5a34`；`docker compose build` + `up -d`（后者由
  宿主机直连 SSH 完成，避免 MCP 服务端随容器重建中断会话）
- 服务 HTTP 200；最终 bundle 已加载

### 实施中发现并修的两处

1. **`form-fields` WARN 是真问题**：checker 明细显示「表单无 `role="status"`
   状态行，sending/sent/errors 从不播报」。我们只加了 `role="alert"`（错误）
   和 `aria-busy`（表单），缺常驻 status。补 `sr-only` 状态行后复跑 PASS。
2. **CDP `forcePseudoState` 偶发失效**：曾读到 hover 背景仍是 default 色。
   改用 Playwright 真实 `hover()` 复核，确认是伪状态强制时机问题而非代码回归。

## 五、未完成任务

- 本轮范围内无未完成项
- 遗留待排期：见下节

## 六、遗留问题与风险

1. `system.md` 成为后续所有 UI 改动的约束源，写不准会误导后续会话 →
   逐条对照现有 CSS 实际值，不凭记忆。
2. 动效 500→300ms 改变进度条观感，属刻意的规范收敛，会截图复核。
3. 现有 e2e 脚本 BASE 不统一（`5173` / `127.0.0.1:18008` 混用），
   本轮不强行统一（避免扩大改动面），仅新增统一入口脚本时使用约定值。
4. `integration.mjs` 内含明文口令，属既有问题，不在本轮范围；
   后续应改为环境变量读取。
5. **`npm run lint` 本来就是坏的**（与本轮无关）：`package.json` 声明了
   `eslint .`，但 `eslint` 既不在 devDependencies、项目也没有 eslint 配置文件。
   本轮不擅自引入 lint 工具链（会扩大范围），验证改用 IDE lint（0 错误）+
   `npm run build` + `npm run test:e2e`。后续需单独补齐 eslint 配置与依赖。
6. **`playwright` 浏览器二进制未安装**：已在 `e2e/browser.mjs` 里优先复用
   系统 Chrome（`E2E_CHROME_PATH` 可覆盖），避免下载约 150MB 二进制；
   CI 若无系统 Chrome 需先 `npx playwright install chromium`。
7. 两份文档均不含凭证等敏感信息。
