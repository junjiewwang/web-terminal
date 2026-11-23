/**
 * 设计规则回归断言
 *
 * 把 system.md 中可自动化的规则做成断言，防止后续 UI 改动悄悄回退。
 * 与项目内 e2e 约定一致：Playwright + page.route mock 后端，可离线跑。
 * 设置 BASE 环境变量可直连线上做冒烟（不做 mock）。
 *
 * 用法：
 *   npm run dev                            # 终端 A
 *   node frontend/e2e/verify-design-rules.mjs
 *   BASE=http://web-term.devcloud:18008/ node ...mjs
 *
 * 退出码：全部通过 0，有 FAIL 则 1，环境错误 2。
 */
import { launchBrowser } from "./browser.mjs";

const BASE = process.env.BASE ?? "http://localhost:5173";
const USE_MOCK = !process.env.BASE;

const EXPRESSION = `(() => {
  const out = {};
  const root = getComputedStyle(document.documentElement);
  out.tokens = {
    textMin: root.getPropertyValue("--ui-text-min").trim(),
    inputMobile: root.getPropertyValue("--ui-input-mobile-size").trim(),
    radiusControl: root.getPropertyValue("--ui-radius-control").trim(),
    radiusCard: root.getPropertyValue("--ui-radius-card").trim(),
    radiusPanel: root.getPropertyValue("--ui-radius-panel").trim(),
    focus: root.getPropertyValue("--ui-focus").trim(),
    colorScheme: root.colorScheme,
    themeColor: document.querySelector('meta[name="theme-color"]')?.getAttribute("content") ?? null,
  };

  // 1. 最小字号：所有可见文本节点（排除显式 ui-micro 例外）
  const tiny = [...document.querySelectorAll("*")]
    .filter((e) => !e.children.length && e.innerText && e.innerText.trim())
    .filter((e) => !e.classList.contains("ui-micro"))
    .filter((e) => parseFloat(getComputedStyle(e).fontSize) < 13);
  out.tinyCount = tiny.length;
  out.tinySamples = tiny.slice(0, 5).map((e) => ({
    text: e.innerText.trim().slice(0, 24), fs: getComputedStyle(e).fontSize,
  }));

  // 2. 圆角 family：合成探针
  const mk = (cls) => {
    const e = document.createElement("div");
    e.className = cls;
    document.body.appendChild(e);
    const r = getComputedStyle(e).borderRadius;
    e.remove();
    return r;
  };
  out.radius = {
    control: mk("rounded-md"),
    card: mk("rounded-lg"),
    panel: mk("rounded-3xl"),
  };

  // 3. 焦点环：真实 Tab 无法在 evaluate 内触发，检查 CSS 规则存在性
  const all = [];
  for (const sheet of document.styleSheets) {
    try { for (const r of sheet.cssRules) all.push(r.cssText); } catch (e) {}
  }
  const css = all.join("\\n");
  out.cssRules = {
    focusVisible: css.includes(":focus-visible"),
    focusOutline: css.includes("outline") && css.includes("--ui-focus"),
    reducedMotion: css.includes("prefers-reduced-motion"),
    skipLink: css.includes("skip-link"),
    colorScheme: css.includes("color-scheme"),
    // 不应再出现 transition: all
    noTransitionAll: !css.includes("transition: all") && !css.includes("transition:all"),
  };

  // 4. 热区：普通按钮不应带 44px 伪元素（避免密集工具栏重叠）
  const buttons = [...document.querySelectorAll("button")];
  const withTapAfter = buttons.filter(
    (b) => !b.classList.contains("ui-tap-expand") &&
           getComputedStyle(b, "::after").content !== "none"
  );
  out.ordinaryButtonTapAfter = withTapAfter.length;
  out.buttonCount = buttons.length;

  // 5. ARIA 状态播报
  out.aria = {
    alertNodes: document.querySelectorAll('[role="alert"]').length,
    statusNodes: document.querySelectorAll('[role="status"]').length,
    busyNodes: document.querySelectorAll('[aria-busy]').length,
    liveNodes: document.querySelectorAll('[aria-live]').length,
  };

  // 6. 页面无横向溢出
  out.docOverflow = document.documentElement.scrollWidth - document.documentElement.clientWidth;

  // 7. skip-link 定位必须 absolute（否则会进 flex 布局推偏页面）
  const skip = document.querySelector(".skip-link");
  out.skipPosition = skip ? getComputedStyle(skip).position : "missing";

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

let failures = 0;
const check = (name, cond, detail = "") => {
  const ok = !!cond;
  if (!ok) failures++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  → " + detail : ""}`);
};

async function main() {
  const browser = await launchBrowser();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  if (USE_MOCK) await mockBackend(page);

  try {
    await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 20000 });
    await page.waitForTimeout(800);
    const d = JSON.parse(await page.evaluate(EXPRESSION));

    console.log(`target: ${BASE}${USE_MOCK ? " (mock)" : " (live)"}`);
    console.log("tokens:", JSON.stringify(d.tokens));
    console.log("");

    check("token: --ui-text-min = 13px", d.tokens.textMin === "13px", d.tokens.textMin);
    check("token: 移动端 input = 16px", d.tokens.inputMobile === "16px", d.tokens.inputMobile);
    check("color-scheme: dark", d.tokens.colorScheme === "dark", d.tokens.colorScheme);
    check("theme-color meta 存在", d.tokens.themeColor === "#050816", String(d.tokens.themeColor));

    check("无 <13px 可见文本（ui-micro 例外）", d.tinyCount === 0,
      `${d.tinyCount} 处 ${JSON.stringify(d.tinySamples)}`);
    check("圆角 family = 8/12/16",
      d.radius.control === "8px" && d.radius.card === "12px" && d.radius.panel === "16px",
      JSON.stringify(d.radius));

    for (const [k, v] of Object.entries(d.cssRules)) check(`CSS 规则: ${k}`, v);

    check("普通按钮无全局热区伪元素", d.ordinaryButtonTapAfter === 0,
      `${d.ordinaryButtonTapAfter}/${d.buttonCount}`);
    check("页面无横向溢出", d.docOverflow === 0, String(d.docOverflow));
    check("skip-link 为 absolute", d.skipPosition === "absolute", d.skipPosition);

    // ARIA：登录页（auth_required=true）应至少有 1 个 aria-busy 节点
    check("存在 ARIA 状态节点", d.aria.busyNodes > 0 || d.aria.statusNodes > 0,
      JSON.stringify(d.aria));

    console.log("");
    console.log(failures === 0 ? "全部通过" : `${failures} 项未通过`);
    await browser.close();
    process.exit(failures ? 1 : 0);
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
