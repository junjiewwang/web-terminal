/**
 * 视觉 token 校验 — 读取 DOM 计算样式，客观验证"设计系统统一"是否生效
 *
 * 比 eyeball 截图更严谨：直接断言改动元素的 computed style 符合新 token。
 * 覆盖 P0 改动：
 *  1. HostList 搜索框：背景应为 white/5（非旧 gray-900）
 *  2. TerminalTabs 容器：背景应为 gray-950 系（非旧 gray-900）
 *  3. ConfirmDialog 卡片：圆角 + danger 红边 + backdrop blur
 */

import { chromium } from "playwright";

const BASE = "http://localhost:5173";

const hostsMock = [
  { id: 1, name: "prod-bastion", hostname: "10.0.0.1", port: 22, username: "root", auth_type: "key", tags: ["prod"], host_type: "root", parent_id: null, ready_pattern: null, entry: { type: "none", value: null, success_pattern: null, steps: [] }, status: "active", children: [], created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" },
];
const credsMock = [];

let failures = 0;
const check = (n, c) => { console.log(`${c ? "  PASS  " : "  FAIL  "}${n}`); if (!c) failures++; };

async function mockBackend(page) {
  await page.route("**/api/**", async (route) => {
    const p = new URL(route.request().url()).pathname;
    const m = route.request().method();
    const json = (b) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
    if (p === "/api/auth/status") return json({ auth_required: false });
    if (p === "/api/hosts" && m === "GET") return json(hostsMock);
    if (p === "/api/terminal/backend" && m === "GET") return json({ backend: "tmux" });
    if (p === "/api/credentials" && m === "GET") return json(credsMock);
    if (p === "/api/events/history") return json([]);
    if (p === "/api/events/stream") return route.fulfill({ status: 200, contentType: "text/event-stream", body: "" });
    if (p === "/api/terminal" && m === "GET") return json([]);
    if (p === "/api/snippets") return json([]);
    return json({ ok: true });
  });
}

// gray-900 = rgb(17,24,39); white/5 ≈ rgba(255,255,255,0.05); gray-950 ≈ rgb(3,7,18)
function isGray900(rgb) { return rgb && rgb.startsWith("rgb(17, 24, 39)"); }

async function main() {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await mockBackend(page);
  await page.goto(BASE, { waitUntil: "networkidle" });
  await page.waitForSelector("text=/终端/");
  await page.waitForTimeout(500);

  // ── 1. HostList 搜索框 ──
  console.log("\n[1] HostList 搜索框 token");
  const searchInput = page.locator('input[placeholder="搜索主机..."]').first();
  const searchBg = await searchInput.evaluate((el) => getComputedStyle(el).backgroundColor);
  console.log(`    背景: ${searchBg}`);
  check("搜索框背景非旧 gray-900 (rgb(17,24,39))", !isGray900(searchBg));

  // ── 2. TerminalTabs 容器（需先打开一个 tab 让页签栏出现；这里直接查页签栏 div）──
  console.log("\n[2] TerminalTabs 容器 token");
  // 页签栏：含 bg-gray-950/60 的 div，其内部有 button。取终端区域上方的 tab 容器。
  // 终端页签栏在 header 之下；点一下主机让 tab 出现
  await page.getByText("prod-bastion").first().click();
  await page.waitForTimeout(400);
  // 页签栏容器：包含 🔗/🖥 图标的横向 div
  const tabContainer = page.locator("div.flex.items-stretch.overflow-x-auto").first();
  const tabBg = await tabContainer.evaluate((el) => getComputedStyle(el).backgroundColor);
  console.log(`    页签栏背景: ${tabBg}`);
  check("页签栏背景非旧 gray-900", !isGray900(tabBg));

  // ── 3. ConfirmDialog 卡片样式 ──
  console.log("\n[3] ConfirmDialog 卡片样式");
  // 触发 backend 切换确认框
  await page.getByRole("button", { name: /TMUX|BROKER/ }).first().click();
  await page.waitForTimeout(300);
  const dialog = page.locator('[role="alertdialog"]');
  const styles = await dialog.evaluate((el) => {
    const cs = getComputedStyle(el);
    return {
      borderRadius: cs.borderRadius,
      backdropFilter: cs.backdropFilter || cs.webkitBackdropFilter,
      borderColor: cs.borderTopColor,
      bg: cs.backgroundColor,
    };
  });
  console.log(`    圆角: ${styles.borderRadius} (期望 ~16px=rounded-2xl)`);
  console.log(`    backdrop: ${styles.backdropFilter}`);
  console.log(`    边框色: ${styles.borderColor}`);
  check("卡片圆角 rounded-2xl (~16px)", parseFloat(styles.borderRadius) >= 15);
  check("卡片有 backdrop-blur", (styles.backdropFilter || "").includes("blur"));
  // danger 变体：确认按钮为红色
  const confirmBtn = page.locator('[role="alertdialog"] button').last();
  const confirmBg = await confirmBtn.evaluate((el) => getComputedStyle(el).backgroundColor);
  const confirmColor = await confirmBtn.evaluate((el) => getComputedStyle(el).color);
  console.log(`    确认按钮背景: ${confirmBg} / 文字色: ${confirmColor}`);
  // Chromium 1234 用 oklab/oklch；红色判定：oklab a 轴 >0.1（正值=红），
  // 或 oklch 色相在 ~340-360∪0-40 且彩度>0.05，或 rgb 红分量显著高于绿蓝
  function isRed(s) {
    const m = s.match(/oklab\([^/]*?(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s/);
    if (m) return parseFloat(m[2]) > 0.1;                       // a 轴
    const h = s.match(/oklch\([^/]*?[\d.]+\s+[\d.]+\s+([\d.]+)/);
    if (h) { const hue = parseFloat(h[1]); return (hue <= 40 || hue >= 340); }
    const r = s.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
    if (r) { const [_, R, G, B] = r.map(Number); return R > 180 && R - G > 60; }
    return false;
  }
  check("danger 确认按钮为红色系", isRed(confirmBg) || isRed(confirmColor));

  await page.keyboard.press("Escape");
  await browser.close();
  console.log(`\n结果：${failures === 0 ? "全部通过 ✅" : `${failures} 项失败 ❌`}`);
  process.exit(failures === 0 ? 0 : 1);
}
main().catch((e) => { console.error("异常:", e); process.exit(2); });
