/**
 * 集成验证 — 直连真实后端（Docker 172.30.0.2:8000）
 * 密码 admin123，不执行真正删除。
 *
 * 已验证的发现：headless Chromium 下 keyboard.press("Escape") 可能不冒泡到 window
 * → 降级方案：点弹窗内的取消按钮关闭（ConfirmDialog 会调 onCancel）。
 */

import { chromium } from "playwright";

const BASE = "http://172.30.0.2:8000";

let failures = 0;
const check = (n, c) => { console.log(`${c ? "  PASS  " : "  FAIL  "}${n}`); if (!c) failures++; };

/** 关闭当前弹窗：先 Esc，失败则点弹窗内取消按钮 + 再 Esc 兜底 */
async function closeDialog(page) {
  await page.keyboard.press("Escape");
  await page.waitForTimeout(500);
  // 如果弹窗还在，点取消按钮
  const dlg = page.locator('[role="alertdialog"]');
  if (await dlg.isVisible()) {
    await dlg.getByRole("button").first().click(); // 取消按钮
    await page.waitForTimeout(500);
  }
  // 最后的兜底
  if (await dlg.isVisible()) {
    await page.keyboard.press("Escape");
    await page.waitForTimeout(400);
  }
}

async function main() {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

  // ── 1. 登录 ──
  console.log("[1] 登录...");
  await page.goto(BASE, { waitUntil: "networkidle" });
  await page.waitForSelector("#password", { timeout: 10000 });
  await page.fill("#password", "admin123");
  await page.click('button[type="submit"]');
  await page.waitForSelector("text=终端", { timeout: 15000 });
  await page.waitForTimeout(1000);
  check("登录成功进入主界面（侧栏可见 dev-cloud）", await page.getByText("dev-cloud").first().isVisible());

  // ── 2. Backend 切换确认框 ──
  console.log("\n[2] Backend 切换确认框...");
  await page.getByRole("button", { name: /BROKER|TMUX/ }).first().click();
  await page.waitForTimeout(700);
  check("弹出确认框「切换终端后端」", await page.getByText("切换终端后端").isVisible());
  await closeDialog(page);
  check("关闭 Backend 确认框", !(await page.locator('[role="alertdialog"]').isVisible()));

  // ── 3. 主机删除确认框（tce-server 下有 TCE-V113 → 应显示子节点数） ──
  console.log("\n[3] 主机删除确认框...");
  await page.getByRole("button", { name: /主机/ }).first().click();
  await page.waitForTimeout(600);
  check("进入主机管理页", await page.getByText("主机管理").isVisible());

  const tceRow = page.locator("div.group", { hasText: "tce-server" }).first();
  await tceRow.getByRole("button", { name: "删除", exact: true }).click({ force: true });
  await page.waitForTimeout(600);
  const delHostText = await page.getByText(/确认删除/).textContent();
  check("弹出删除确认框（含子节点文案）", (delHostText || "").includes("子节点"));
  await closeDialog(page);
  check("关闭主机删除确认框", !(await page.locator('[role="alertdialog"]').isVisible()));

  // ── 4. 凭据删除确认框（tce-server-login ref_count=6 → 应显示引用数） ──
  console.log("\n[4] 凭据删除确认框...");
  await page.getByRole("button", { name: /凭据/ }).first().click();
  await page.waitForTimeout(600);
  check("进入凭据管理页", await page.getByText("凭据管理").isVisible());

  const credRow = page.locator("div.group", { hasText: "tce-server-login" }).first();
  await credRow.getByRole("button", { name: "删除", exact: true }).click({ force: true });
  await page.waitForTimeout(600);
  check("弹出凭据删除确认框（含 ref_count 文案）", await page.getByText("正在被 6 个主机引用").isVisible());
  await closeDialog(page);
  check("关闭凭据删除确认框", !(await page.locator('[role="alertdialog"]').isVisible()));

  await browser.close();
  console.log(`\n结果：${failures === 0 ? "全部通过 ✅" : `${failures} 项失败 ❌`}`);
  process.exit(failures === 0 ? 0 : 1);
}
main().catch(err => { console.error("\n异常:", err.message); process.exit(2); });
