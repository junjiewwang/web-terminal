import { chromium } from "playwright";
const BASE = "http://127.0.0.1:18008";
let f = 0;
const chk = (n, c) => { console.log(`${c ? "PASS" : "FAIL"} ${n}`); if (!c) f++; };

// 直接传函数引用给 evaluate（Playwright 会序列化执行）
const leftGone = () => {
  const asides = [...document.querySelectorAll("aside")];
  return !asides.some(a => a.querySelector('[aria-label="折叠侧栏"]'));
};

async function main() {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1440, height: 900 } });
  await p.goto(BASE, { waitUntil: "networkidle" });
  await p.fill("#password", "admin123");
  await p.click('button[type="submit"]');
  await p.waitForSelector('[aria-label="工作台"]', { timeout: 15000 });
  await p.waitForTimeout(600);

  chk("Activity Bar 48px 在 x=0", await p.evaluate(() => {
    const btn = document.querySelector('[aria-label="工作台"]');
    const bar = btn.parentElement.parentElement;
    return bar.getBoundingClientRect().x === 0 && Math.round(bar.getBoundingClientRect().width) === 48;
  }));
  chk("默认显示主机列表", await p.getByText("dev-cloud").first().isVisible());

  await p.locator('[aria-label="折叠侧栏"]').click();
  await p.waitForTimeout(500);
  chk("折叠后左侧主机栏消失", await p.evaluate(leftGone));

  await p.locator('[aria-label="工作台"]').click();
  await p.waitForTimeout(500);
  chk("点工作台恢复侧栏", await p.evaluate(() => {
    const asides = [...document.querySelectorAll("aside")];
    return asides.some(a => a.querySelector('[aria-label="折叠侧栏"]'));
  }));

  await p.locator('[aria-label="控制台"]').click();
  await p.waitForTimeout(500);
  chk("控制台侧栏显示主机/凭据管理",
    await p.getByRole("button", { name: /主机管理/ }).isVisible() &&
    await p.getByRole("button", { name: /凭据管理/ }).isVisible());

  await b.close();
  console.log(`\n${f ? f + " FAIL ❌" : "ALL PASS ✅"}`);
  process.exit(f ? 1 : 0);
}
main().catch(e => { console.error("CRASH:", e.message); process.exit(2); });
