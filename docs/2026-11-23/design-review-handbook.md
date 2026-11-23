# WebTerminal 前端设计评审手册

- 日期：2026-11-23
- 适用范围：本仓库 `frontend/` 下所有 UI
- 配套契约：[`.interface-design/system.md`](../../.interface-design/system.md)（项目根）

## 一、选对 skill（最容易犯的错）

WebTerminal 是**工具型应用**（终端工作区 + 主机树 + 面板），不是营销网站。

| 用途 | 该用 | 不该用 |
|---|---|---|
| 设计/重构 UI | `interface-design`（明确 `Not for: Landing pages, marketing sites`） | `design-taste-frontend` / taste-skill |
| 规则审计 | Vercel `web-design-guidelines`（约 190 条，输出 `file:line`） | 同上 |
| 品牌/落地页 | 营销型 skill | — |

曾误用 taste-skill 做评审，结果把 `one-section`、`container-edges`、
`copy-fact`、`moment-none` 等营销页规则当成缺陷去「修」，属于方向性错误。

### 参考资源（本项目不安装，仅文档指引）

```
# 如需在本地临时使用（会装到全局 skills 目录，不进仓库）
npx skills add vercel-labs/agent-skills --skill web-design-guidelines
```

- `interface-design` 原文：`Infrasity-Labs/dev-gtm-claude-skills`
  的 `.claude/skills/interface-design/SKILL.md`（MIT）
- Vercel `Web Interface Guidelines`：`vercel-labs/web-interface-guidelines`
- 本项目选择 **零第三方依赖**：规则已固化在 `system.md` + `frontend/e2e/`
  断言脚本中，可离线复现、不随上游漂移。

## 二、评审流程

1. 读 `.interface-design/system.md`，确认方向/token/组件规格（decisions are made）
2. 改动 UI 前按 skill 的 checkpoint 说明 Intent / Hierarchy / Palette /
   Depth / Surfaces / Typography / Spacing 的 **why**
3. 改完跑（`npm run lint` 目前不可用，见下文遗留项）：
   ```bash
   cd frontend
   npm run build                 # tsc -b && vite build
   npm run dev                   # 终端 A，默认 5173
   npm run test:e2e              # 终端 B
   BASE=http://web-term.devcloud:18008/ npm run test:e2e   # 线上冒烟
   ```
   `test:e2e` 会串行跑 `verify-design-rules.mjs` 与
   `verify-accent-contrast.mjs`，优先复用系统 Chrome（可用
   `E2E_CHROME_PATH` 覆盖）。
4. 视觉复核：桌面 1440 + 移动 390 截图；hover/active/disabled 用
   CDP `CSS.forcePseudoState` 或 Playwright `hover()` 验证
5. 线上部署后用 checker 复跑，**与基线对比「新增 FAIL/WARN = 0」**

## 三、基线数据（复跑对比用）

使用 taste-skill 的 `consistency.mjs`（纯审计用途，可继续用）：
```
CHROME_PATH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
node skills/taste-skill/scripts/consistency.mjs \
  "http://web-term.devcloud:18008/" --quick --widths 1440,390 \
  --page software --out /tmp/taste-check --json
```

| 指标 | 整改前基线 | 当前 |
|---|---|---|
| FAIL（违规条数） | 17 | 12 |
| WARN（违规条数） | 11 | **8** |
| PASS 检查项 | — | 126 |

> `counts.FAIL/WARN` 是**违规条数**，`checks` 字典是**检查项种类**（去重）。
> 例如当前 4 个 FAIL 检查项对应 12 条违规。对比时两者不要混用。

**已修复**：`focus-visible`、`text-contrast`（FAIL）；
`tap-size`、`form-fields`（WARN）

## 四、豁免表（刻意不修，不是回归）

| 检查项 | 级别 | 豁免理由 |
|---|---|---|
| `one-section` | FAIL | 营销页「每屏一节」；工具型应用是持续工作区 |
| `container-edges` | FAIL | 营销页等边距规则；三栏布局有自己的对齐逻辑 |
| `copy-fact` | FAIL | 要求首屏有硬性事实数据；登录页/终端不适用 |
| `moment-none` | WARN | 营销页入场/dealt moment；终端工具刻意不加装饰动效 |
| `copy-headline` | WARN | 营销标题文案规则 |
| `share-meta` / `page-meta` | WARN | 分享卡片元数据；内部运维工具无分享需求 |
| `skip-link` | WARN | checker 按启发式判断首 Tab stop；实测首焦点**就是** skip-link，误报 |
| `focus-ring` | FAIL（2 条） | checker **静态扫描 CSS 文本**，命中 xterm.js 自带与 Tailwind 生成的 `outline:none`；补偿规则紧随其后，真实 Tab 有可见 2px 焦点环，误报 |
| ~~`form-fields`~~ | ~~WARN~~ | **已修复**：补常驻 `role="status"` 状态行后复跑 PASS |

## 五、项目内规则断言

| 脚本 | 覆盖 |
|---|---|
| `frontend/e2e/verify-accent-contrast.mjs` | accent 四态对比度 + 三态规则存在性 |
| `frontend/e2e/verify-design-rules.mjs` | 焦点环、最小字号、圆角 family、无 `transition-all`、热区不重叠、ARIA 播报 |
| `frontend/e2e/verify-styles.mjs` | 视觉 token（搜索框/页签栏/弹窗） |
| 其余 `verify-*.mjs` | 布局、交互、控制台等既有回归 |

统一入口：`npm run test:e2e`。

## 六、规则速查（从工具型 skill 提取，可直接对照）

**必须**
- 语义 token，不用硬编码字面量；不用类名子串反查
- 每个交互元素有 default/hover/active/focus/disabled；数据有 loading/empty/error
- `:focus-visible` 环必须可见；`outline:none` 必须有等效补偿
- 最小字号 13px；移动端 input 16px
- 文本 ≥4.5:1（正文 ≥7:1）；交互态对比度**更高**
- `color-scheme: dark` + `theme-color`
- 动效 <300ms，只命名具体属性，Never `transition: all`
- `prefers-reduced-motion` 生效
- 热区互不重叠；孤立小控件才 opt-in 扩展
- 高频操作不加动画

**避免**
- `nav` 里塞 inline link 冒充导航、icon-only 按钮无 `aria-label`
- `touch-action` 缺失导致双击缩放
- `transition-all`、从 `scale(0)` 出现、装饰性渐变/发光按钮
- 多种圆角、多个 accent、暗色下用阴影做层级
- 为了过 checker 而给工具型 UI 加 hero/照片/分节

## 七、已知遗留

- `npm run lint` 不可用：`eslint` 未声明为 devDependency，也无配置文件。
  属既有问题，需单独补齐工具链；当前以 IDE lint + build + e2e 兜底。
- 其余 `verify-*.mjs` 的 BASE 不统一（`5173` 与 `127.0.0.1:18008` 混用），
  未纳入 `test:e2e` 统一入口，按各脚本 header 单独运行。
- 无视觉回归基线截图，目前依赖人工截图 + checker 复跑。

## 八、常见陷阱（本项目踩过的）

1. **类名子串选择器**：`[class*="bg-emerald-500"]` 命中 48 处，
   把 46 处半透明幽灵按钮改成深色文字 → 黑底黑字。改为组件显式语义类。
2. **`:not()` 抬高特异性**：字号规则带 `:not(.ui-micro)` 变 `(0,1,0)`，
   压过移动端 `16px` 规则。用纯 `:where()` 保持 0。
3. **热区伪元素覆盖定位**：给所有 `<a>` 加 `position:relative`，
   把 skip-link 的 `absolute` 改掉，成为 flex 首子项，登录卡片右移 46px。
4. **disabled 双重衰减**：`opacity-40` 同时淡背景和文字，两种前景都 <3:1。
   改为显式中性态 + `opacity:1`。
5. **静态 dist 无后端**：直接对 `file://` 或纯静态 dist 跑 checker，
   会因 React 渲染不完整产生 `no-visual`/`cta` 等环境性 FAIL，
   **不能当回归依据**。
