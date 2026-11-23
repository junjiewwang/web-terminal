/**
 * 实心 accent 控件（.ui-accent-bright）四态 WCAG 对比度校验
 *
 * 与项目内其它 e2e 脚本一致：Playwright + page.route mock 后端，可离线跑。
 * 优先级：若设置 BASE 环境变量则直连该地址（用于线上冒烟，不做 mock）；
 *         否则使用 http://localhost:5173 + mock（需先 npm run dev）。
 *
 * 用法：
 *   npm run dev                              # 终端 A
 *   node frontend/e2e/verify-accent-contrast.mjs          # 终端 B
 *   BASE=http://web-term.devcloud:18008/ node ...mjs      # 线上冒烟
 *
 * 退出码：全部通过 0，有 FAIL 则 1。
 */
import { launchBrowser } from "./browser.mjs";

const BASE = process.env.BASE ?? "http://localhost:5173";
const MIN_RATIO = 4.5;
const CANVAS_BG = "#050816";
const USE_MOCK = !process.env.BASE;

const EXPRESSION = `(() => {
  const cv = document.createElement("canvas");
  cv.width = cv.height = 1;
  const cx = cv.getContext("2d", { willReadFrequently: true });
  const bgPage = [5, 8, 22];

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

  const out = { tokens: vars, states: [], rules: {}, dom: {} };
  const inkRGB = parse(vars.ink || "#050816");
  const inkDisRGB = parse(literal(vars.inkDisabled) || "oklch(0.72 0.02 260)");

  for (const state of ["default", "hover", "active", "disabled"]) {
    const css = literal(vars[state]);
    const bg = parse(css);
    const ink = state === "disabled" ? inkDisRGB : inkRGB;
    out.states.push({ state, bgCss: css, ratio: ratio(bg, ink) });
  }

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

  // 真实 DOM 采样（登录页在未登录态会有 1 个实心按钮）
  const els = [...document.querySelectorAll(".ui-accent-bright")];
  out.dom.count = els.length;
  out.dom.samples = els.slice(0, 4).map((e) => {
    const s = getComputedStyle(e);
    return { tag: e.tagName, bg: s.backgroundColor, color: s.color, opacity: s.opacity };
  });
  return JSON.stringify(out);
})()`;

async function mockBackend(page) {
  await page.route("**/api/**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ auth_required: true }),
    })
  );
}

async function main() {
  const browser = await launchBrowser();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  if (USE_MOCK) await mockBackend(page);

  try {
    await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 20000 });
    await page.waitForTimeout(800);
    const data = JSON.parse(await page.evaluate(EXPRESSION));

    console.log(`target: ${BASE}${USE_MOCK ? " (mock)" : " (live)"}`);
    console.log("tokens:", JSON.stringify(data.tokens));
    console.log("");
    let failed = 0;
    for (const s of data.states) {
      const ok = s.ratio >= MIN_RATIO;
      if (!ok) failed++;
      console.log(
        `${ok ? "PASS" : "FAIL"}  ${s.state.padEnd(10)} ${String(s.ratio).padStart(6)}:1  bg=${s.bgCss}`
      );
    }
    console.log("");
    for (const [k, v] of Object.entries(data.rules)) {
      if (!v) failed++;
      console.log(`${v ? "PASS" : "FAIL"}  rule:${k}`);
    }
    console.log("");
    console.log(`DOM .ui-accent-bright: ${data.dom.count}`, JSON.stringify(data.dom.samples));
    console.log(`阈值 ${MIN_RATIO}:1 — ${failed === 0 ? "全部通过" : failed + " 项未通过"}`);
    await browser.close();
    process.exit(failed ? 1 : 0);
  } catch (e) {
    await browser.close();
    console.error("执行失败:", e.message);
    console.error(USE_MOCK
      ? "请确认已启动 dev server：npm run dev（默认 5173），或用 BASE 指定地址"
      : "请确认 BASE 地址可访问");
    process.exit(2);
  }
}

main();
