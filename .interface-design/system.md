# WebTerminal — Interface Design System

> 本文件是 `interface-design` skill 的设计契约。文件存在时，后续 UI 工作
> **直接读取并照此执行**（decisions are made），不再重新探索方向。
> 所有数值逐条对照 `frontend/src/styles/design-system.css` 与线上实测，
> 修改设计时须同步更新本文件。

## Direction and feel

- **Who**：终端运维/研发人员，正在排查线上问题或执行远程命令；
  五分钟前在看告警，五分钟后要重启服务。不是浏览内容的普通用户。
- **Verb**：连上主机、执行命令、传文件、看日志。
- **Feel**：`cold like a terminal, dense like a workbench`。
  冷、密、克制、技术性；不要营销页的暖色、大留白、入场动画、装饰渐变。
- **Focal point**：终端工作区永远主导；三栏（活动条 / 主机树 / 终端）
  中终端占满主区，侧栏服务内容而非与之竞争。
- **60/30/10**：深色中性结构占绝大部分，单一 emerald accent 仅用于状态与主操作。

## Depth strategy + spacing base

- **Depth = borders-only**。暗色下阴影读不出来，层级靠低透明度边框：
  `rgba(255,255,255,0.06–0.12)`。侧栏与画布同色，只用一条细边分隔。
  不要混用 layered shadows / surface-color tints。
- **Surface elevation**：base `#050816` → 每级仅 +7%~12% 亮度；
  input 比周围**更暗**（inset），popover 比父表面高一级。
- **Spacing base = 4px**，只用其倍数。工具面板 padding 12–16px，
  密集控件区 `gap-1`，分组之间才给真实留白。
- 全局已声明 `color-scheme: dark` 与 `<meta name="theme-color" content="#050816">`。

## Hierarchy decisions

- **Type scale**：工具型密集 UI，body 13–14px。
  **最小字号 13px**（`--ui-text-min`），唯一例外 `.ui-micro` 11px
  （微型圆形计数徽标等放不下 13px 的容器）；移动端 input 强制 16px
  （防 iOS Safari 聚焦自动缩放）。
- **四级文本色阶**（不要压成两级）：
  | 级 | token | 值 |
  |---|---|---|
  | primary | `--ui-text-body` | `oklch(0.93 0.01 260)` |
  | secondary | `--ui-text-secondary` | `oklch(0.83 0.015 260)` |
  | muted | `--ui-text-muted` | `oklch(0.72 0.02 260)` |
  | subtle/meta | `--ui-text-subtle` | `oklch(0.68 0.02 260)` |
  四级在 `#050816` 上均 ≥4.5:1，正文 ≥7:1。
- **Weight + color 承担层级**，不只靠字号；动态数字用 `tabular-nums`。
- **Density**：工具面板 12–16px padding；不要给终端区塞大留白。

## Color + accent（单一档）

- **One accent**：`emerald`，与终端光标色一致。不要引入第二强调色。
- **实心控件**：只用 `.ui-accent-bright` 一个语义类，由组件显式声明意图，
  **禁止用 `[class*="bg-emerald-..."]` 之类类名子串反查**（曾造成黑底黑字回归）。
  | 状态 | token | 实测对比度（前景深墨 `#050816`） |
  |---|---|---|
  | default | `--ui-accent-surface` = emerald-500 | 8.06:1 |
  | hover | `--ui-accent-hover` = emerald-400 | 10.29:1（更亮） |
  | active | `--ui-accent-active` = emerald-600 | 5.46:1 |
  | disabled | 中性灰底 + `--ui-text-muted` | 7.25:1，`opacity: 1` |
- disabled **不用 `opacity-40`**：双重衰减会让两种前景都掉到 3:1 以下。
- **幽灵控件**：`bg-emerald-500/5`~`/25` 等半透明底配亮绿字（16–18:1），
  共 46 处，**一律不改前景色**。它们是深色底上的近黑背景。
- 回归断言：`frontend/e2e/verify-accent-contrast.mjs`（四态 + 规则存在性）。

## Radius family

- `--ui-radius-control: 8px`（按钮/输入）、
  `--ui-radius-card: 12px`（卡片）、
  `--ui-radius-panel: 16px`（大容器）、`rounded-full` 仅用于状态徽标 pill。
- 嵌套圆角遵循 concentric：`outerRadius = innerRadius + padding`。

## Key component patterns

| 组件 | 规格 |
|---|---|
| 主操作按钮 | 36–42px 高 · 12px 16px padding · 8px radius · 14px/500 · `.ui-accent-bright` |
| 工具栏图标按钮 | 24–36px 视觉，`gap-1` 紧排；**不做全局 44px 伪元素热区**（相邻会重叠） |
| `.ui-tap-expand` | 仅孤立小控件显式 opt-in：桌面 ≥24px，粗指针/窄屏 44px |
| `.ui-skip-link` / `.skip-link` | absolute 定位，默认 `top:-60px`，focus 时 `12px`；`main#main-content` 锚点 |
| 焦点环 | `:focus-visible` → `2px solid #34d399` + `2px offset`；xterm 内用 inset shadow 等效替代 |
| 表单状态 | 错误 `role="alert"` + `aria-live="assertive"`；`aria-busy` 标记提交/加载；可交互子树**不**整体做 live region，用独立 `sr-only` status |
| 进度条 | `transition-[width] duration-300`（显式命名，不用 `transition-all`） |

## Motion

- UI 动效 **< 300ms**（按钮 100–160ms，popover/toast ≤300ms）。
- 只命名具体过渡属性：`transition-colors` / `transition-[width]` /
  `transition-[border-color,background-color,transform]`。
  **Never `transition: all`**。
- ease-out 优先；press 反馈 `scale(0.97~0.98)`，不低于 0.95。
- 高频操作（命令面板、快捷键）不加动画。
- 全局 `prefers-reduced-motion: reduce` 已实现。

## Governance / review

- 评审用工具型 skill：`interface-design`（craft/hierarchy/token）+ Vercel
  `web-design-guidelines`（file:line 审计）。**不要用营销页 skill**
  （`design-taste-frontend`）评审本产品。
- 可自动化规则断言：`frontend/e2e/verify-design-rules.mjs`（`npm run test:e2e`）。
- **刻意豁免的营销页规则**：`one-section`、`container-edges`、`copy-fact`、
  `moment-none`、`copy-headline`、`share-meta`、`page-meta`、
  真实照片、hero 入场、dealt moment。它们是 checker 的既有 FAIL/WARN，
  **不是回归**。详见 `docs/2026-11-23/design-review-handbook.md`。
- 修改任何 token 后：同步本文件 → 跑 `npm run test:e2e` → 线上 checker 复跑
  确认「新增 FAIL/WARN = 0」。

## Visual direction notes

- 深色画布 `#050816`，左上角极淡的 emerald/cyan radial 光晕（装饰，非渐变按钮）。
- 终端区与 UI 用同一深色体系融合，xterm.js 不额外描边。
- 参考气质：Linear / Vercel 的克制与密度，但保持终端工具的冷感；
  不追求「作品集感」，追求长时间使用不疲劳。
