# 前端设计系统整改 — 记录文档

- 日期：2026-11-23
- 状态：实施中
- 依据：使用 `taste-skill`（`design-taste-frontend`）对线上页面做的设计评审
- 范围：`frontend/src` 全量组件

## 一、需求与背景

用 taste-skill 的 checker（`consistency.mjs`）与人工评审，对 WebTerminal 页面做设计质量检查。
结论：这是一个**工具型应用**（登录页 + 主机树 + 终端工作区），不是营销网站，
因此 taste-skill 的营销页规则（每屏一节、hero 入场、dealt moment、真实照片、地址格式）不适用；
但其中通用的设计工程质量规则应该落地。

### 客观检查结果（登录页，1440 + 390）

`6 FAIL / 7 WARN`：

| 类型 | 检查项 | 是否适用 |
|---|---|---|
| FAIL | `focus-ring` / `focus-visible` | 适用，需修 |
| FAIL | `text-contrast` | 适用，需修 |
| FAIL | `one-section` / `container-edges` / `copy-fact` | 不适用（营销页分节与首屏事实规则） |
| WARN | `tap-size` | 适用，需修 |
| WARN | `skip-link` | 适用，需修 |
| WARN | `moment-none` / `copy-headline` / `share-meta` / `page-meta` / `form-fields` | 不适用 |

基础可访问性多项 PASS：`heading-order`、`img-alt`、`field-label`、`a11y-name`、`lang`、
`tabindex`、`button-contrast`、`font-load`、`section-tile`、`empty-area`。

### 实测问题

| 指标 | 实测 | 规则 | 判定 |
|---|---|---|---|
| `<13px` 文本节点 | 91 个（10px/12px 为主） | 最小 13px | 不合规 |
| 工具栏按钮 | 24×24 / 28×28 / 36×36 | 44px 热区 | 不合规 |
| 圆角种类 | 8px / 6px / 全圆 / 0px 混用（7 类 utility） | 单一 radius family | 不合规 |
| 键盘焦点 | `outline: none`、`boxShadow: none` | 可见焦点环 | 不合规 |
| 低对比度灰字 | `oklch(0.373)` / `0.446` / `0.551` | 正文 ≥7:1、`muted` ≥4.5:1 | 不合规 |
| CSS 变量 | `:root` 上 0 个 | 单一设计系统靠 token 驱动 | 不合规 |

## 二、方案设计

### 核心决策：集中式 token 覆盖，不改散落的 className

项目是 Tailwind CSS v4（CSS-first，`@theme` 定义 token）。若逐个替换 200+ 行字号、
240+ 行灰字、90 处按钮，会：
- 产生大量重复改动，违反 DRY 与高内聚低耦合
- 后续新增组件仍会重新引入不合规写法
- 无法保证一致性

因此采用**分层收敛**：

```
index.css
  └─ @import "tailwindcss"          （框架层，工具类在 @layer utilities）
  └─ @import styles/design-system.css（设计系统层，unlayered，优先级天然高于 @layer）
```

CSS cascade 规范中，**unlayered 规则优先级高于任何 `@layer` 内的规则**，
所以集中式 CSS 可以统一覆盖 Tailwind 原子类，无需改 JSX，也不会被框架覆盖。

| 层 | 职责 |
|---|---|
| `design-system.css` | 唯一设计决策来源：`@theme` token + 全局兜底覆盖 |
| `App.tsx` | 仅插入 skip-link 与主内容锚点（唯一动 JSX 处） |
| 其余组件 | 不动，自动继承整改结果 |

### 设计取舍（重要）

- **点击热区**：桌面端保持工具型 UI 的紧凑密度，仅对**粗指针 / 窄屏**
  （`@media (pointer: coarse), (max-width: 640px)`）用伪元素把小按钮热区扩到 44px。
  全量强制 44px 会挤爆密集终端工具栏，损害主要使用场景。
- **圆角**：收敛为「控件 8px / 卡片 12px / 大容器 16px / 状态徽标 pill」一套 family，
  `rounded-full` 保留给胶囊徽标（符合 taste-skill 允许 pill 标记状态的规则）。
- **字号**：最小 13px；对确实放不下 13px 的微型圆形徽标，用语义类 `.ui-micro` 显式例外，
  避免溢出（例外必须有名字，不靠魔法数字散落）。
- **营销页规则**：明确不实施，避免为了过 checker 而损害工具型产品可用性。

## 三、实施进展

- [x] 设计评审与 checker 客观检查
- [x] 建立统一设计 token 层（`styles/design-system.css`）
- [x] P0：全局 `:focus-visible` 焦点环
- [x] P0：`<13px` 文本提升到 13px
- [x] P1：低对比度灰字提升 + 移动端 44px 热区
- [x] P2：圆角收敛 + skip-link
- [x] 构建验证 + 浏览器实测
- [ ] 提交推送 + dev 环境部署验证

### 改动清单

| 文件 | 改动 |
|------|------|
| `frontend/src/styles/design-system.css` | **新建**：token 定义 + 焦点环 + 字号下限 + 对比度色阶 + 移动端热区 + 圆角 family + skip-link + reduced-motion |
| `frontend/src/index.css` | 在 `@import "tailwindcss"` 之后引入设计系统层 |
| `frontend/src/App.tsx` | 插入 `<a class="ui-skip-link">` 与 `<main id="main-content">` 锚点 |

**未改动**：其余 15 个组件（自动继承整改结果）——这是本次方案的核心价值。

### 本地验证结果

- `npm run build`（`tsc -b && vite build`）通过；IDE lint 0 错误
- 真实浏览器 computed style 实测（构建产物）：
  - `text-[9px]/[10px]/[12px]/text-xs` → **13px**；`.ui-micro` 例外 → 11px
  - `text-gray-500` → `oklch(0.72 0.02 260)`、`600/700` → `oklch(0.68 0.02 260)`
  - 圆角 `rounded`/`md` → 8px、`lg` → 12px、`xl/2xl/3xl` → 16px、`full` → pill
  - skip-link 默认 `top: -60px`（隐藏）
- 真实键盘 Tab 验证：首焦点命中 `ui-skip-link`，`matches(':focus-visible') = true`，
  outline 为 `2px solid rgb(52,211,153)`，skip-link 滑到 `top: 12px`，主内容 `id=main-content` 就位
- 说明：曾用纯静态 dist 起本地服务复跑 checker，因无后端 API、React 渲染不完整，
  产生 `no-visual`/`cta`/`empty-area` 等环境性 FAIL，**不能作为回归依据**；
  有效验证以部署后的线上页面为准

## 四、未完成任务

- 提交推送 + dev 环境部署验证

## 五、遗留问题与风险

1. **集中式覆盖的局限**：覆盖的是工具类选择器，若后续组件改用
   `style={{ fontSize: ... }}` 内联样式则不受约束，需在 code review 中留意。
2. **视觉密度变化**：10px → 13px 会略微增大标签类文本，密集列表可能需要微调间距；
   实施后必须浏览器实测确认无溢出/换行破坏。
3. **登录页与主界面差异**：checker 只能评未登录页；主界面需登录后人工验证。
4. **营销页 FAIL 保留**：`one-section` / `container-edges` / `copy-fact` /
   `moment-none` 等不修复，属刻意豁免，不应视为回归。
5. **无自动化视觉回归**：目前依赖 checker + 人工截图，建议后续补视觉回归基线。
