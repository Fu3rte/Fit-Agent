/**
 * F4-07 证据补口探针（走真实 mock REST + 源码断言）：
 * stage4.md §7 第 9 步「空数据与无法定位」+ 第 1 步入口预填协议级核对。
 *
 * 运行：node scripts/f4-07-evidence-probe.mjs
 * 覆盖（仅 f4-01–06 未直接覆盖的分支；已覆盖项在 stage4-evidence.md 引用不重写）：
 *  - 源码：RecordsPage「发起更正」入口 + prefill 文案（§7.1 UI 预填协议级）
 *  - 源码：CHECKIN 门禁含 更正|数据有误|作废
 *  - empty 种子：records / stats / arrangements 空列表（f3-06 同口径重断言）
 *  - empty 种子：更正请求（日期+动作）→ 无定位对象、不生成草稿
 *  - empty 种子：作废请求（日期+动作）→ 无定位对象、不生成草稿
 *  - empty 种子：更正预填文案本身（模拟 /records 点击后发送）→ 同样不落草稿
 *  - 复位 default：种子基线仍在（避免探针污染后续归档解读）
 */
import path from "node:path";
import { fileURLToPath } from "node:url";
import { readFileSync } from "node:fs";
import { createServer as createViteServer } from "vite";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
let failed = 0;
let passed = 0;
const check = (name, ok, detail = "") => {
  if (!ok) failed += 1;
  else passed += 1;
  console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? ` — ${detail}` : ""}`);
};

const vite = await createViteServer({
  root,
  configFile: path.join(root, "vite.config.ts"),
  logLevel: "error",
  server: { host: "127.0.0.1", port: 0, strictPort: false },
  optimizeDeps: { noDiscovery: true },
});
await vite.listen();
const port = vite.httpServer.address().port;
const base = `http://127.0.0.1:${port}`;

const api = async (method, p, body) => {
  const res = await fetch(base + p, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  return { status: res.status, body: await res.json().catch(() => null) };
};

const reset = (seed) => api("POST", "/api/dev/reset", { seed });
const cv = async () =>
  (await api("GET", "/api/dev/status")).body?.context_version;
const records = async () => (await api("GET", "/api/records")).body?.records;
const stats = async () => (await api("GET", "/api/stats")).body;

async function run(session, message, prefix) {
  const started = await api("POST", "/api/runs", {
    session_id: session,
    message,
    client_request_id: `${prefix}-${Date.now()}-${Math.random()}`,
  });
  if (started.status !== 200)
    throw new Error(
      `发起 Run 失败：${started.status} ${JSON.stringify(started.body)}`,
    );
  const runId = started.body.run_id;
  for (let i = 0; i < 120; i += 1) {
    await new Promise((r) => setTimeout(r, 300));
    const active = (await api("GET", "/api/runs/active")).body?.run;
    if (!active || active.run_id !== runId) continue;
    if (["completed", "failed", "cancelled"].includes(active.status))
      return {
        text: active.saved_text,
        draft: active.drafts?.[active.drafts.length - 1] ?? null,
      };
  }
  throw new Error("Run 未在预期时间内进入终态");
}

/* ========== 1. 源码：入口预填 + CHECKIN 门禁 ========== */
console.log("\n--- 源码：入口预填 / CHECKIN ---");
{
  const recordsPage = readFileSync(
    path.join(root, "src/features/records/RecordsPage.tsx"),
    "utf8",
  );
  check("源码：RecordsPage 含「发起更正」入口", /发起更正/.test(recordsPage));
  check(
    "源码：预填文案含「我想更正 ${date} 的训练记录」模板（/records → 对话）",
    /prefill:\s*`我想更正 \$\{record\.date\} 的训练记录/.test(recordsPage),
  );
  check(
    "源码：预填跳转目标为对话 navigate + state.prefill",
    /navigate\("\/".*state:\s*\{\s*prefill:/s.test(recordsPage),
  );
  const server = readFileSync(path.join(root, "src/mock/server.ts"), "utf8");
  check(
    "源码：CHECKIN 门禁含 更正|数据有误|作废（协议级入口）",
    /打卡\|训练记录\|.*更正\|数据有误\|作废/.test(server) &&
      /CORRECTION_INTENT\s*=\s*\/更正\|数据有误\//.test(server) &&
      /VOID_INTENT\s*=\s*\/作废\//.test(server),
  );
}

/* ========== 2. empty 种子：空数据（引用 f3-06 同口径） ========== */
console.log("\n--- empty 空数据 ---");
{
  await reset("empty");
  const r = (await api("GET", "/api/records")).body;
  const s = (await api("GET", "/api/stats")).body;
  const a = (await api("GET", "/api/arrangements")).body;
  check(
    "empty：records 空列表（长度 0，非 null/错误）",
    Array.isArray(r?.records) && r.records.length === 0,
    JSON.stringify(r?.records),
  );
  check(
    "empty：stats 空（per_week []、buckets 0/0/0、prs []）",
    Array.isArray(s?.per_week) &&
      s.per_week.length === 0 &&
      s.buckets?.met === 0 &&
      s.buckets?.unmet === 0 &&
      s.buckets?.pending === 0 &&
      Array.isArray(s?.prs) &&
      s.prs.length === 0,
    JSON.stringify({
      weeks: s?.per_week,
      buckets: s?.buckets,
      prs: s?.prs,
    }),
  );
  check(
    "empty：arrangements 空列表",
    Array.isArray(a?.arrangements) && a.arrangements.length === 0,
  );
}

/* ========== 3. empty 种子：无法定位（更正 / 作废 / 预填文案） ========== */
console.log("\n--- empty 无法定位 ---");
{
  await reset("empty");
  await api("PUT", "/api/provider/api-key", { api_key: "probe" });
  const cv0 = await cv();

  const corr = await run(
    "s-f4-07-empty-corr",
    "更正 2026-09-07 的训练记录：杠铃平板卧推 第2组改成6次",
    "f4-07-empty-corr",
  );
  check(
    "empty 更正（日期+动作）：无草稿、明确回复不生成",
    corr.draft == null &&
      /不生成更正草稿|未能/.test(corr.text ?? "") &&
      (await cv()) === cv0 &&
      (await records()).length === 0,
    JSON.stringify({
      draft: corr.draft,
      cv: await cv(),
      n: (await records()).length,
      text: (corr.text ?? "").slice(0, 160),
    }),
  );

  const via = await run(
    "s-f4-07-empty-void",
    "整次录错，作废 2026-09-07 的杠铃平板卧推",
    "f4-07-empty-void",
  );
  check(
    "empty 作废（日期+动作）：无草稿、明确回复不生成",
    via.draft == null &&
      /不生成作废草稿|未能/.test(via.text ?? "") &&
      (await cv()) === cv0,
    JSON.stringify({
      draft: via.draft,
      cv: await cv(),
      text: (via.text ?? "").slice(0, 160),
    }),
  );

  // 模拟 /records「发起更正」预填文案在 empty 上发送（协议级等价于 UI 点击后发送）
  const prefill = await run(
    "s-f4-07-empty-prefill",
    "我想更正 2026-09-07 的训练记录：杠铃平板卧推 数据有误，需要更正。",
    "f4-07-empty-prefill",
  );
  check(
    "empty 预填文案发送：同样无草稿、不推 cv",
    prefill.draft == null &&
      /不生成|未能|日期|定位/.test(prefill.text ?? "") &&
      (await cv()) === cv0 &&
      (await records()).length === 0,
    JSON.stringify({
      draft: prefill.draft,
      cv: await cv(),
      n: (await records()).length,
      text: (prefill.text ?? "").slice(0, 160),
    }),
  );
}

/* ========== 4. 复位 default 基线（防污染解读） ========== */
console.log("\n--- 复位 default ---");
{
  await reset("default");
  const list = await records();
  const s = await stats();
  check(
    "default 复位：4 身份、W2 1/3、三桶 1/1/1",
    list.length === 4 &&
      s.per_week.find((w) => w.week === "W2")?.completed === 1 &&
      s.buckets?.met === 1 &&
      s.buckets?.unmet === 1 &&
      s.buckets?.pending === 1,
    JSON.stringify({
      n: list.length,
      w2: s.per_week.find((w) => w.week === "W2"),
      b: s.buckets,
    }),
  );
}

await vite.close();
console.log(`\nPASS ${passed} / FAIL ${failed}`);
console.log(failed === 0 ? "全部通过" : `${failed} 项失败`);
process.exit(failed === 0 ? 0 : 1);
