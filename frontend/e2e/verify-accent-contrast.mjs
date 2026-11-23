/**
 * verify-accent-contrast.mjs
 *
 * 校验实心 accent 控件（.ui-accent-bright）各交互态的 WCAG 对比度。
 * 用 CDP 连到已打开的页面，在真实浏览器里解析 oklch CSS 颜色，
 * 再按 #050816 画布合成计算对比度，避免理论值与实渲染不一致。
 *
 * 用法：
 *   1. Chrome 以 --remote-debugging-port=9222 启动并打开目标页面
 *   2. node frontend/e2e/verify-accent-contrast.mjs [urlSubstring]
 *
 * 退出码：全部通过 0，有 FAIL 则 1。
 * 依赖：Node 22+（内置 WebSocket），无 npm 依赖。
 */
import { setTimeout as sleep } from "node:timers/promises";

const CDP = "http://localhost:9222";
const TARGET = process.argv[2] ?? "web-term.devcloud";
const MIN_RATIO = 4.5;
const CANVAS_BG = "#050816";

async function findPage() {
  const res = await fetch(`${CDP}/json/list`);
  const pages = (await res.json()).filter(
    (p) => p.type === "page" && p.url?.includes(TARGET)
  );
  if (!pages.length) throw new Error(`未找到包含 "${TARGET}" 的标签页`);

  // 可能有多个同 URL 标签页（部分尚未加载完样式表）。逐个探测，
  // 选出确实定义了 --ui-accent-surface 的那个。
  for (const p of pages) {
    try {
      const ws = new WebSocket(p.webSocketDebuggerUrl);
      await new Promise((resolve, reject) => {
        ws.onopen = resolve;
        ws.onerror = reject;
        setTimeout(() => reject(new Error("connect timeout")), 3000);
      });
      const val = await new Promise((resolve) => {
        ws.onmessage = (e) => {
          const m = JSON.parse(e.data);
          if (m.id === 1) resolve(m.result?.result?.value);
        };
        ws.send(JSON.stringify({
          id: 1,
          method: "Runtime.evaluate",
          params: {
            expression: "getComputedStyle(document.documentElement).getPropertyValue('--ui-accent-surface').trim()",
            returnByValue: true,
          },
        }));
        setTimeout(() => resolve(""), 3000);
      });
      ws.close();
      if (val) return p.webSocketDebuggerUrl;
    } catch { /* 试下一个 */ }
  }
  throw new Error(`包含 "${TARGET}" 的标签页都未加载设计系统 CSS`);
}

const EXPRESSION = `(() => {
  const cv = document.createElement("canvas");
  cv.width = cv.height = 1;
  const cx = cv.getContext("2d", { willReadFrequently: true });
  const bgPage = [5, 8, 22]; // #050816

  // 解析任意 CSS 颜色（含 oklch）为不透明 sRGB；支持 alpha 后缀
  const parse = (css) => {
    cx.clearRect(0, 0, 1, 1);
    cx.fillStyle = "${CANVAS_BG}"; cx.fillRect(0, 0, 1, 1);
    cx.fillStyle = css; cx.fillRect(0, 0, 1, 1);
    const d = cx.getImageData(0, 0, 1, 1).data;
    return [d[0], d[1], d[2]];
  };
  const lum = (rgb) => {
    const f = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(rgb[0]) + 0.7152 * f(rgb[1]) + 0.0722 * f(rgb[2]);
  };
  const ratio = (a, b) => {
    const A = lum(a), B = lum(b);
    return +((Math.max(A, B) + 0.05) / (Math.min(A, B) + 0.05)).toFixed(2);
  };

  // 把 var(--token) 解析成浏览器能识别的字面量
  const literal = (cssColor) => {
    const e = document.createElement("span");
    e.style.color = cssColor;
    document.body.appendChild(e);
    const v = getComputedStyle(e).color;
    e.remove();
    return v;
  };

  const root = getComputedStyle(document.documentElement);
  const vars = {
    default:  root.getPropertyValue("--ui-accent-surface").trim(),
    hover:    root.getPropertyValue("--ui-accent-hover").trim(),
    active:   root.getPropertyValue("--ui-accent-active").trim(),
    disabled: root.getPropertyValue("--ui-accent-surface-disabled").trim(),
    ink:      root.getPropertyValue("--ui-on-accent").trim(),
    inkDisabled: root.getPropertyValue("--ui-accent-ink-disabled").trim(),
  };

  const out = { tokens: vars, states: [], rules: {} };
  const inkRGB = parse(vars.ink || "#050816");
  const inkDisRGB = parse(literal(vars.inkDisabled) || "oklch(0.72 0.02 260)");

  for (const state of ["default", "hover", "active", "disabled"]) {
    const css = literal(vars[state]);
    const bg = parse(css);
    const ink = state === "disabled" ? inkDisRGB : inkRGB;
    out.states.push({
      state, bgCss: css, bgRGB: bg, inkRGB: ink,
      ratio: ratio(bg, ink),
    });
  }

  // 确认三态规则确实存在于样式表（防止 token 定义了但规则被删）
  const all = [];
  for (const sheet of document.styleSheets) {
    try { for (const r of sheet.cssRules) all.push(r.cssText); } catch (e) {}
  }
  out.rules = {
    base: all.some((t) => t.includes("ui-accent-bright") && t.includes("background-color")),
    hover: all.some((t) => t.includes("ui-accent-bright") && t.includes(":hover")),
    active: all.some((t) => t.includes("ui-accent-bright") && t.includes(":active")),
    disabled: all.some((t) => t.includes("ui-accent-bright") && t.includes(":disabled")),
    noOpacityFade: !all.some((t) => t.includes("ui-accent-bright") && t.includes("opacity-40")),
  };

  // 采样实际 DOM 上的控件，确认前景色不是白色
  const els = [...document.querySelectorAll(".ui-accent-bright")];
  out.domCount = els.length;
  out.domSamples = els.slice(0, 6).map((e) => {
    const s = getComputedStyle(e);
    return { tag: e.tagName, bg: s.backgroundColor, color: s.color };
  });

  return JSON.stringify(out);
})()`;

async function main() {
  const ws = new WebSocket(await findPage());
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });

  let id = 0;
  const pending = new Map();
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  };
  const send = (method, params = {}) => new Promise((resolve) => {
    const n = ++id; pending.set(n, resolve);
    ws.send(JSON.stringify({ id: n, method, params }));
  });

  await send("Page.enable");
  await sleep(300);
  const r = await send("Runtime.evaluate", { expression: EXPRESSION, returnByValue: true });
  ws.close();

  if (r.result?.exceptionDetails) {
    console.error("页面脚本异常:", r.result.exceptionDetails.text);
    process.exit(2);
  }
  const d = JSON.parse(r.result.result.value);

  console.log("tokens:", JSON.stringify(d.tokens));
  console.log("DOM .ui-accent-bright 数量:", d.domCount);
  console.log("");
  let failed = 0;
  for (const s of d.states) {
    const ok = s.ratio >= MIN_RATIO;
    if (!ok) failed++;
    console.log(
      `${ok ? "PASS" : "FAIL"}  ${s.state.padEnd(10)} ${String(s.ratio).padStart(6)}:1  ` +
      `bg=${s.bgCss} rgb=${s.bgRGB.join(",")} ink=${s.inkRGB.join(",")}`
    );
  }
  console.log("");
  for (const [k, v] of Object.entries(d.rules)) {
    if (!v) failed++;
    console.log(`${v ? "PASS" : "FAIL"}  rule:${k}`);
  }
  console.log("");
  console.log("DOM 采样:", JSON.stringify(d.domSamples));
  console.log(`阈值 ${MIN_RATIO}:1 — ${failed === 0 ? "全部通过" : failed + " 项未通过"}`);
  process.exit(failed ? 1 : 0);
}

main().catch((e) => { console.error(e.message); process.exit(2); });
