/**
 * 验证：工作台/控制台两级导航 + YAML 导入导出提升顶层
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

  await p.goto(BASE, { waitUntil: "networkidle" });
  await p.fill("#password", "admin123");
  await p.click('button[type="submit"]');
  await p.waitForSelector("text=工作台", { timeout: 15000 });
  await p.waitForTimeout(500);

  console.log("[1] 两级导航");
  chk("显示「工作台」按钮", await p.getByRole("button", { name: /工作台/ }).isVisible());
  chk("显示「控制台」标签", await p.getByText(/控制台/).first().isVisible());

  console.log("[2] 进入控制台 → 主机");
  await p.getByText("主机", { exact: true }).first().click();
  await p.waitForTimeout(500);
  chk("进入主机管理", await p.getByText("主机管理").isVisible());
  // YAML 导入/导出在顶层
  chk("顶层「导入」按钮", await p.getByRole("button", { name: /导入/ }).first().isVisible());
  chk("顶层「导出」按钮", await p.getByRole("button", { name: /导出/ }).first().isVisible());
  await p.screenshot({ path: "/tmp/wt-console.png" });

  console.log("[3] 控制台 → 凭据");
  await p.getByText("凭据", { exact: true }).first().click();
  await p.waitForTimeout(500);
  chk("进入凭据管理", await p.getByText("凭据管理").isVisible());

  console.log("[4] JS 错误:", errs.length ? errs.join(" | ") : "无");

  await b.close();
  console.log(`\n${f ? f + " FAIL ❌" : "ALL PASS ✅"}`);
  process.exit(f ? 1 : 0);
}
main().catch(e => { console.error("CRASH:", e.message); process.exit(2); });
