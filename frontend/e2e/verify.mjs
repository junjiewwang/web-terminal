/**
 * e2e 验证脚本 — 验证本次 UI 改动（确认弹窗 / Esc 关闭 / 设计 token 统一）
 *
 * 用 Playwright route 拦截 mock 后端，无需真实 FastAPI。
 * 覆盖：
 *  1. 全局 Backend 切换 → 弹确认框 → Esc 关闭
 *  2. 主机删除 → 弹确认框 → Esc 关闭
 *  3. 凭据删除 → 弹确认框 → Esc 关闭
 *  4. 各阶段截图（供 Read 工具肉眼核对视觉）
 *
 * 运行：先起 dev server (npm run dev --port 5173)，再 node frontend/e2e/verify.mjs
 */

import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

const BASE = "http://localhost:5173";
const SHOT_DIR = "/tmp/wt-shots";
mkdirSync(SHOT_DIR, { recursive: true });

// ── mock 数据 ──────────────────────────────────
const hostsMock = [
  {
    id: 1, name: "prod-bastion", hostname: "10.0.0.1", port: 22, username: "root",
    auth_type: "key", private_key_path: "~/.ssh/id_rsa", credential_ref: null,
    description: "生产环境入口机", tags: ["prod", "bastion"], host_type: "root",
    parent_id: null, ready_pattern: null,
    entry: { type: "none", value: null, success_pattern: null, steps: [] },
    status: "active", children: [
      {
        id: 2, name: "app-server-01", hostname: "", port: 22, username: "root",
        auth_type: "key", tags: ["app"], host_type: "nested", parent_id: 1,
        entry: { type: "menu_send", value: "1", success_pattern: null, steps: [] },
        status: "active", children: [], created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
      },
    ],
    created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
  },
  {
    id: 3, name: "dev-box", hostname: "192.168.1.50", port: 22, username: "ubuntu",
    auth_type: "password", tags: ["dev"], host_type: "root", parent_id: null, ready_pattern: null,
    entry: { type: "none", value: null, success_pattern: null, steps: [] },
    status: "deprecated", children: [],
    created_at: "2026-01-02T00:00:00Z", updated_at: "2026-01-02T00:00:00Z",
  },
];

const credsMock = [
  { id: 1, name: "tce-login", description: "TCE 平台登录密码", has_password: true, ref_count: 2, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" },
  { id: 2, name: "db-readonly", description: null, has_password: true, ref_count: 0, created_at: "2026-01-03T00:00:00Z", updated_at: "2026-01-03T00:00:00Z" },
];

// ── 断言工具 ──────────────────────────────────
let failures = 0;
function check(name, cond) {
  console.log(`${cond ? "  PASS  " : "  FAIL  "}${name}`);
  if (!cond) failures++;
}

// ── mock 路由 ──────────────────────────────────
async function mockBackend(page) {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const p = url.pathname;
    const method = route.request().method();
    const json = (body) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });

    if (p === "/api/auth/status") return json({ auth_required: false });
    if (p === "/api/hosts" && method === "GET") return json(hostsMock);
    if (p === "/api/terminal/backend" && method === "GET") return json({ backend: "tmux" });
    if (p === "/api/credentials" && method === "GET") return json(credsMock);
    if (p === "/api/credentials/names") return json(credsMock.map((c) => ({ name: c.name, description: c.description })));
    if (p === "/api/events/history") return json([]);
    if (p === "/api/events/stream") return route.fulfill({ status: 200, contentType: "text/event-stream", body: "" });
    if (p === "/api/terminal" && method === "GET") return json([]);
    if (p === "/api/snippets") return json([]);
    // 其余（DELETE/POST 等）统一 200，避免 fetchWithRetry 反复重试
    return json({ ok: true, message: "mocked" });
  });
}

// ── 主流程 ──────────────────────────────────
async function main() {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await mockBackend(page);

  console.log("\n[1] 加载主界面...");
  await page.goto(BASE, { waitUntil: "networkidle" });
  await page.waitForSelector("text=/终端/", { timeout: 10000 });
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${SHOT_DIR}/01-terminal-page.png` });
  check("主界面加载（终端页 + 侧栏主机列表）", await page.getByText("prod-bastion").first().isVisible());

  // ── 2. Backend 切换确认框 ──
  console.log("\n[2] 全局 Backend 切换确认框...");
  // 顶栏 Tmux 切换按钮（带 "TMUX" 文本）
  const backendBtn = page.getByRole("button", { name: /TMUX|BROKER/ }).first();
  await backendBtn.click();
  await page.waitForTimeout(300);
  const switchDialogVisible = await page.getByText("切换终端后端").isVisible();
  check("点击 Backend 切换 → 弹出确认框", switchDialogVisible);
  await page.screenshot({ path: `${SHOT_DIR}/02-backend-switch-confirm.png` });
  // Esc 关闭
  await page.keyboard.press("Escape");
  await page.waitForTimeout(300);
  check("Esc 关闭 Backend 切换确认框", !(await page.getByText("切换终端后端").isVisible()));

  // ── 3. 主机删除确认框 ──
  console.log("\n[3] 主机删除确认框...");
  await page.getByRole("button", { name: /主机/, exact: false }).first().click();
  await page.waitForTimeout(400);
  await page.screenshot({ path: `${SHOT_DIR}/03-host-manage-page.png` });
  check("进入主机管理页", await page.getByText("主机管理").isVisible());

  // hover 行让删除按钮显隐 → 直接 force click
  const prodRow = page.locator("div.group", { hasText: "prod-bastion" }).first();
  await prodRow.getByRole("button", { name: "删除", exact: true }).click({ force: true });
  await page.waitForTimeout(300);
  const deleteHostVisible = await page.getByText("确认删除「prod-bastion」").isVisible();
  check("点击删除主机 → 弹出确认框（含子节点数文案）", deleteHostVisible);
  await page.screenshot({ path: `${SHOT_DIR}/04-host-delete-confirm.png` });
  await page.keyboard.press("Escape");
  await page.waitForTimeout(300);
  check("Esc 关闭主机删除确认框", !(await page.getByText("确认删除「prod-bastion」").isVisible()));

  // ── 4. 凭据删除确认框 ──
  console.log("\n[4] 凭据删除确认框...");
  await page.getByRole("button", { name: /凭据/ }).first().click();
  await page.waitForTimeout(400);
  await page.screenshot({ path: `${SHOT_DIR}/05-credential-page.png` });
  check("进入凭据管理页", await page.getByText("凭据管理").isVisible());

  const credRow = page.locator("div.group", { hasText: "tce-login" }).first();
  await credRow.getByRole("button", { name: "删除", exact: true }).click({ force: true });
  await page.waitForTimeout(300);
  const deleteCredVisible = await page.getByText("正在被 2 个主机引用").isVisible();
  check("点击删除凭据 → 弹出确认框（含 ref_count 文案）", deleteCredVisible);
  await page.screenshot({ path: `${SHOT_DIR}/06-credential-delete-confirm.png` });
  await page.keyboard.press("Escape");
  await page.waitForTimeout(300);
  check("Esc 关闭凭据删除确认框", !(await page.getByText("正在被 2 个主机引用").isVisible()));

  await browser.close();
  console.log(`\n结果：${failures === 0 ? "全部通过 ✅" : `${failures} 项失败 ❌`}`);
  console.log(`截图目录：${SHOT_DIR}/`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((err) => {
  console.error("脚本异常：", err);
  process.exit(2);
});
