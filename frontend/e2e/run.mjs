/**
 * e2e 统一入口：串行执行设计系统与配色回归断言。
 *
 * 用法：
 *   npm run dev                       # 终端 A（默认 5173）
 *   npm run test:e2e                  # 终端 B
 *   BASE=http://host:port npm run test:e2e   # 线上冒烟
 *
 * 说明：其余 verify-*.mjs 是各次改动的历史回归脚本，BASE 不统一
 * （5173 与 127.0.0.1:18008 混用），未纳入统一入口，按各自 header 说明单独运行。
 */
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const scripts = [
  "verify-design-rules.mjs",
  "verify-accent-contrast.mjs",
];

let failed = 0;
for (const script of scripts) {
  console.log(`\n══════ ${script} ══════`);
  const r = spawnSync(process.execPath, [join(here, script)], {
    stdio: "inherit",
    env: process.env,
  });
  if (r.status !== 0) {
    failed++;
    console.log(`↳ ${script} 未通过 (exit ${r.status})`);
  }
}

console.log(`\n══════ 汇总：${scripts.length - failed}/${scripts.length} 脚本通过 ══════`);
process.exit(failed ? 1 : 0);
