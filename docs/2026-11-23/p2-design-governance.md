# P2 设计治理收尾 — 记录文档

- 日期：2026-11-23
- 状态：实施中
- 前置：`tool-app-design-skill-research.md` 与 `accent-palette-consolidation.md` 中列为 P2 的遗留项
- 选型：**A**（零第三方依赖、项目内自持），不执行 `npx skills add`

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
- [ ] 创建本记录文档（进行中）
- [ ] P2-1 transition 收敛
- [ ] P2-2 ARIA 播报
- [ ] P2-3 `.interface-design/system.md`
- [ ] P2-4 评审手册
- [ ] e2e 整理
- [ ] 验证 + 推送部署 + 更新文档

## 五、未完成任务

- 上述未勾选项

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
