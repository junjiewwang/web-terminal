# 工具型应用设计 Skill 调研与主题回归修复 — 记录文档

- 日期：2026-11-23
- 状态：**已完成并部署验证**
- 触发：发现线上「broker 切换按钮黑底黑字」，并反思上一轮评审是否选对了 skill
- 提交：`bbf3fa6`

## 一、问题：主题回归（P0）

### 现象

`⇄ BROKER` 切换按钮、Activity Bar 图标按钮在深色背景上文字变成近黑色，几乎不可见。

### 实测证据（线上页面 computed style）

| 按钮 | 背景 | 被改后的文字色 |
|---|---|---|
| `📡` 工作台 | `bg-emerald-500/15`（15% 透明，深色底上近乎黑） | `rgb(5,8,22)` |
| `⇄ BROKER` | `bg-emerald-500/10` | `rgb(5,8,22)` |

### 根因

`design-system.css` 里的 `[class*="bg-emerald-500"]` **子串选择器过宽**：
本意只修登录页那个实心按钮（白字 2.48:1），但子串匹配把 `/5`~`/25`
的半透明背景也一并命中。

影响面扫描：`bg-emerald-500*` 共 **48 处**，其中真正实心（`/90`、`/80`）
仅 **2 处**，被误伤的半透明背景 **46 处**。

### 更深层的方法论问题

用 CSS 属性子串选择器去「猜测 Tailwind 类名」，是脆弱且作用域不可控的做法。
本次调研的 `interface-design` skill 明确反对：**「Bind to semantic tokens,
not hardcoded literals」**，以及 **「Use What Exists」**。

## 二、调研：开源工具型应用设计 Skill

### 结论：上一轮选错了 skill

| Skill | 定位 | 来源 |
|---|---|---|
| **`interface-design`** | 明确 `Use for: Dashboards, admin panels, SaaS apps, tools, settings pages`；`Not for: Landing pages, marketing sites` | Infrasity-Labs / dammyjay93 等，MIT |
| **`web-design-guidelines`** | 纯审计型，约 190 条规则，输出 `file:line` 违规清单 | vercel-labs/agent-skills（skills.sh 约 570K 安装） |
| `jakubkrehel/skills` | 13 份 Markdown 设计规范（圆角、WCAG 2.2 对比度、空状态文案） | 约 11.6k star |
| `design-taste-frontend`（taste-skill） | `figma.com` / `x.ai` 级**公司营销网站** | 上一轮误用 |

taste-skill 的 `one-section`、`container-edges`、`copy-fact`、`moment-none`
等规则本就不该进入工具型应用的评审清单。

### 直接命中本次实现的规则

| 规则 | 来源 | 本项目现状 |
|---|---|---|
| 语义 token，不用硬编码字面量 / 类名反查 | interface-design | 用 `[class*="bg-emerald-500"]` 反查类名 → 本次 bug |
| Use What Exists（先看项目已有系统，别手搓） | interface-design | 用 unlayered CSS 覆盖框架工具类，方向反了 |
| **Never let two hit areas overlap** | interface-design | 全局 `::after` 给所有 button/a 扩 44px，紧排 `gap-1` 工具栏必重叠 |
| 视觉 <24px 才需扩到 ≥24px；移动端 44px | Vercel | 无差别 44px 过宽 |
| 移动端 `<input>` 字号 ≥16px（防 iOS 缩放） | Vercel | 只设了 `min-height:44px`，字号仍 13px |
| **Interactions increase contrast** | Vercel | 给绿底按钮压深色文字是反向操作 |
| `color-scheme: dark` + `theme-color` meta | Vercel | 项目两者都缺 |
| Never `transition: all` | Vercel + interface-design | 9 处（LoginPage 1 / FileTransferPanel 6 / SnippetPanel 2） |
| 状态完整 default/hover/active/focus/disabled + loading/empty/error | interface-design | 表单缺 `role="status"`（对应 checker `form-fields` WARN） |
| 嵌套圆角 concentric、tabular-nums、text-wrap: balance | 两者 | 未做 |

### 可借鉴的方法论（比数值更重要）

1. **按产品类型选 skill**：工具型应用 → `interface-design` + `web-design-guidelines`；营销页 → taste-skill。
2. **`.interface-design/system.md` 持久化设计契约**：方向、深度策略、间距基数、type scale、组件尺寸写成机器可读文件，后续会话自动沿用，比散文式 docs 更适合作为「活的设计系统」。
3. **`design-review` / `design-deslop` 命令**：前者带审批门槛并渲染 before/after，后者是 diff 范围快速去 slop，适合提交前检查。
4. **审计输出 `file:line`**：Vercel skill 把违规定位到具体文件行，比页面级 FAIL 更可操作。
5. **Intent First**：先定义「谁在用 / 要完成什么动词 / 该是什么感觉」（终端工具 ≈ cold like a terminal, dense like a trading floor），再反推 token。上一轮直接跳到数值整改。

## 三、本轮方案

遵循「组件显式声明意图」，不再用类名子串猜测。

| 优先级 | 改动 |
|---|---|
| P0 | 删除 2 条 `bg-emerald-500` 子串规则；登录页实心按钮改用显式 `ui-on-accent` 语义类，仅该按钮用深色文字 |
| P1 | 移除会重叠的全局 44px `::after`；改为「仅孤立小控件」按 Vercel 规则扩热区（视觉 <24px → ≥24px），并保留 `.ui-tap-expand` 显式 opt-in |
| P1 | `:root { color-scheme: dark }` + `<meta name="theme-color">` |
| P1 | 移动端 `input/textarea/select` 字号 16px（独立于全站 13px 下限） |
| P2（本轮不做） | 收敛 9 处 `transition-all`、补表单 `role="status"`、引入 `.interface-design/system.md`、评审流程改用工具型 skill |

## 四、实施进展

- [x] 调研与影响面分析
- [x] 创建本记录文档
- [x] P0 主题回归修复
- [x] P1 热区规则收敛
- [x] P1 暗色模式基础声明
- [x] P1 移动端输入框字号
- [x] 构建 + 浏览器实测（含 390px 与热区重叠检查）
- [x] 提交推送 + dev 部署 + 线上验证

### 改动清单

| 文件 | 改动 |
|---|---|
| `frontend/src/styles/design-system.css` | 删除 2 条 `bg-emerald-500` 子串规则；新增 `--ui-on-accent` / `--ui-input-mobile-size` token；`.ui-on-accent` 显式语义类；`color-scheme: dark`；全局 44px `::after` 收敛为 `.ui-tap-expand` opt-in；字号主规则特异性降为 0；移动端 input 16px |
| `frontend/src/components/LoginPage.tsx` | 唯一实心按钮显式加 `ui-on-accent` |
| `frontend/index.html` | 新增 `<meta name="theme-color" content="#050816">` |

其余 46 处半透明 accent 按钮、密集工具栏组件**未改动**。

### 遇到的一个级联陷阱（已修）

移动端 input 初测仍是 13px：字号下限规则带 `:not(.ui-micro)`，
特异性被抬到 `(0,1,0)`，压过了移动端 `16px` 规则的 `(0,0,0)`。
改为纯 `:where()`（特异性 0），并让 `.ui-micro` 靠自身 `(0,1,0)` 生效，
桌面 13px / 移动端 16px / micro 11px 三者同时正确。

### 验证结果

**本地构建产物（真实浏览器 computed style）**
- 半透明 `bg-emerald-500/15` → `oklch(0.845)` 亮绿、`/10` → `oklch(0.765)` 亮绿（黑底黑字消除）
- 显式 `.ui-on-accent` 实心按钮 → `rgb(5,8,22)` 深色文字
- 普通按钮 `::after` → `none`；`.ui-tap-expand` → `content:""` + 24px min（无全局重叠）
- `.ui-micro` → 11px、普通最小字号 → 13px
- `color-scheme: dark`、`theme-color: #050816`

**390px 移动端**
- input `16px` / `min-height 44px`
- 普通按钮带热区伪元素数量：**0**
- skip-link `position: absolute`、页面无横向溢出（`docOverflow = 0`）

**线上部署后**
- 用 wetty-terminal MCP 在 `dev-cloud:/root/go/src/github.com/web-terminal` 执行
  `git pull --ff-only` → `bbf3fa6`；`docker compose build` + `up -d`（后者由宿主机
  直连 SSH 完成，避免 MCP 服务端随容器重建中断会话）
- 服务 HTTP 200，bundle `index-9dHBe7Zq.css`
- 线上实测：`📡` = `oklch(0.845 0.143 164.978)`、`⇄ BROKER` =
  `oklch(0.765 0.177 163.223)`，均恢复亮色可读；截图确认视觉正常
- checker 复跑：`FAIL 17→12`、`WARN 11→9`，**新增 FAIL 0 / 新增 WARN 0**，
  `text-contrast` 保持已修复

## 五、未完成任务

- 本轮范围内无未完成项
- P2 待排期：见下节遗留问题 2

## 六、遗留问题与风险

1. **集中式 CSS 覆盖的固有局限**：覆盖的是工具类选择器，内联样式不受约束；本轮已移除最脆弱的类名子串反查，但字号/灰阶/圆角覆盖仍是「覆盖框架」而非「语义 token」，属于技术债。
2. **P2 未做**：`transition-all`、表单状态播报、system.md 契约、评审工具切换，需单独排期。
3. **tap-size 未按预期回退**：原以为移除无差别 44px 会让 390px 的小按钮重新被报 WARN，
   但线上复跑 `WARN 11→9` 且无新增 WARN——因为表单控件已补 `min-height:44px`，
   checker 主要命中的正是输入框。密集小图标按钮仍 <44px，属工具型 UI 密度的
   有意取舍，不应为了过检查而撑爆布局。
4. **营销页规则误报**：taste-skill 的 `one-section` / `container-edges` / `copy-fact` 等仍会 FAIL，属刻意豁免。
5. **无视觉回归基线**：目前依赖人工截图 + checker，建议后续补基线截图对比。
