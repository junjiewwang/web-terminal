/**
 * UI 重构验证 — 直连 Docker 后端
 * 验证：侧栏折叠 / 命令面板 / 终端工具栏
 */
import { chromium } from "playwright";

const BASE = "http://127.0.0.1:18008";
let f = 0;
const chk = (n, c) => { console.log(`${c ? "PASS" : "FAIL"} ${n}`); if (!c) f++; };

async function main() {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1440, height: 900 } });
  const errs = [];
  p.on("pageerror", e => errs.push(e.message));

  console.log("[1] 登录");
  await p.goto(BASE, { waitUntil: "networkidle" });
  await p.fill("#password", "admin123");
  await p.click('button[type="submit"]');
  await p.waitForSelector("text=/终端/", { timeout: 15000 });
  await p.waitForTimeout(500);
  chk("登录成功", await p.getByRole("button", { name: /终端/ }).isVisible());

  // ── 测试 1: 侧栏折叠 ──
  console.log("\n[2] 侧栏折叠");
  const collapseBtn = p.locator('[aria-label="折叠侧栏"]');
  chk("折叠按钮存在", await collapseBtn.isVisible());
  // 点击折叠
  await collapseBtn.click();
  await p.waitForTimeout(400);
  // 折叠后侧栏宽 48px，展开按钮出现
  const expandBtn = p.locator('[aria-label="展开侧栏"]');
  chk("折叠后出现「展开侧栏」按钮", await expandBtn.isVisible());
  await p.screenshot({ path: "/tmp/wt-sidebar-collapsed.png" });
  // 恢复展开
  await expandBtn.click();
  await p.waitForTimeout(300);
  chk("恢复展开", await p.locator('[aria-label="折叠侧栏"]').isVisible());

  // ── 测试 2: 命令面板 ──
  console.log("\n[3] 命令面板 ⌘K");
  await p.keyboard.press("Control+k");
  await p.waitForTimeout(400);
  chk("⌘K 打开命令面板", await p.locator('[role="dialog"][aria-label="命令面板"]').isVisible());
  await p.screenshot({ path: "/tmp/wt-palette.png" });
  // 搜索主机
  await p.fill('input[placeholder*="搜索命令"]', "dev-cloud");
  await p.waitForTimeout(300);
  chk("搜索到 dev-cloud 主机", await p.getByText("连接 dev-cloud").first().isVisible());
  // Esc 关闭
  await p.keyboard.press("Escape");
  await p.waitForTimeout(300);
  chk("Esc 关闭命令面板", !(await p.locator('[role="dialog"][aria-label="命令面板"]').isVisible()));

  // ── 测试 3: 终端工具栏 ──
  console.log("\n[4] 终端工具栏");
  await p.getByText("dev-cloud").first().click();
  await p.waitForTimeout(5000);
  // 工具栏 5 个按钮：复制/清屏/全屏/文件传输/排障
  const toolbarBtns = ["复制", "清屏", "全屏", "文件传输", "排障脚本"];
  let ok = true;
  for (const t of toolbarBtns) {
    const visible = await p.locator(`[aria-label*="${t}"]`).first().isVisible();
    if (!visible) { ok = false; console.log(`  缺按钮: ${t}`); }
  }
  chk("终端工具栏 5 个按钮齐全", ok);
  await p.screenshot({ path: "/tmp/wt-toolbar.png" });

  // 全屏测试
  await p.locator('[aria-label="全屏"]').first().click();
  await p.waitForTimeout(500);
  const fs = await p.evaluate(() => !!document.fullscreenElement);
  chk("全屏进入", fs);

  console.log("\n[5] JS 错误:", errs.length ? errs.join(" | ") : "无");

  await b.close();
  console.log(`\n${f ? f + " FAIL ❌" : "ALL PASS ✅"}`);
  process.exit(f ? 1 : 0);
}
main().catch(e => { console.error("CRASH:", e.message); process.exit(2); });
