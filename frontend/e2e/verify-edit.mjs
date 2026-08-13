/**
 * 验证 2.5：侧栏主机「编辑」入口跳转管理页
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

  // 侧栏主机行 hover 显示编辑按钮
  console.log("[1] 侧栏主机「编辑」入口");
  const row = p.locator("button.group", { hasText: "dev-cloud" }).first();
  await row.hover();
  await p.waitForTimeout(300);
  const editBtn = p.locator('[title="编辑主机配置"]').first();
  chk("hover 后编辑按钮可见", await editBtn.isVisible());

  // 点击编辑 → 跳转主机管理页
  await editBtn.click();
  await p.waitForTimeout(500);
  chk("跳转到主机管理页", await p.getByText("主机管理").isVisible());

  await b.close();
  console.log(`\n${f ? f + " FAIL ❌" : "ALL PASS ✅"}`);
  process.exit(f ? 1 : 0);
}
main().catch(e => { console.error("CRASH:", e.message); process.exit(2); });
