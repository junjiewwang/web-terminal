/**
 * 验证：侧栏「编辑」→ 直接打开编辑抽屉（无需再手动选主机）
 */
import { chromium } from "playwright";
const BASE = "http://127.0.0.1:18008";
let f = 0;
const chk = (n, c) => { console.log(`${c ? "PASS" : "FAIL"} ${n}`); if (!c) f++; };

async function main() {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1440, height: 900 } });

  await p.goto(BASE, { waitUntil: "networkidle" });
  await p.fill("#password", "admin123");
  await p.click('button[type="submit"]');
  await p.waitForSelector("text=/终端/", { timeout: 15000 });
  await p.waitForTimeout(500);

  console.log("[1] 侧栏主机「编辑」→ 直接打开编辑抽屉");
  const row = p.locator("button.group", { hasText: "dev-cloud" }).first();
  await row.hover();
  await p.waitForTimeout(300);
  await p.locator('[title="编辑主机配置"]').first().click();
  await p.waitForTimeout(800);

  // 关键断言：编辑抽屉直接打开，标题「编辑 — dev-cloud」
  const drawer = p.locator('h3', { hasText: "编辑 — dev-cloud" });
  chk("编辑抽屉直接打开（标题=编辑 — dev-cloud）", await drawer.isVisible());

  await p.screenshot({ path: "/tmp/wt-edit-drawer.png" });

  await b.close();
  console.log(`\n${f ? f + " FAIL ❌" : "ALL PASS ✅"}`);
  process.exit(f ? 1 : 0);
}
main().catch(e => { console.error("CRASH:", e.message); process.exit(2); });
