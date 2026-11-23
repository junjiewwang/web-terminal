/**
 * e2e 公共浏览器启动器
 *
 * 统一处理：优先用系统 Chrome（避免下载 Playwright 浏览器二进制），
 * 找不到时回退到 Playwright 自带 chromium。
 * 可通过 E2E_CHROME_PATH 指定可执行文件。
 */
import { chromium } from "playwright";
import { existsSync } from "node:fs";

const DEFAULT_MAC = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";

export async function launchBrowser() {
  const explicit = process.env.E2E_CHROME_PATH;
  const chromePath = explicit || (existsSync(DEFAULT_MAC) ? DEFAULT_MAC : null);

  if (chromePath) {
    return chromium.launch({ executablePath: chromePath });
  }
  // 回退：需要 `npx playwright install chromium`
  return chromium.launch();
}
