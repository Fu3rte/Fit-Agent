/**
 * F5-01 验收探针（走真实 mock REST + 源码断言）：契约与 mock 复盘模型收口
 * （plans/stage5.md F5-01 / §3.1 / §3.5）。
 *
 * 运行：node scripts/f5-01-probe.mjs
 * 覆盖：
 *  - default 种子统计现算不回归：W1 2/3、W2 1/3、三桶 1/1/1、PR 卧推 80×8
 *  - GET /api/review 返回最新条投影（text/stale/generated_at + basis 可读）
 *  - 种子旧稿 basis 与当时 stats 一致、stale=true
 *  - 内部保存路径（dev 桥 → saveReviewEntry）可追加：最新条可读回、旧条仍在存储、
 *    stale=false、generated_at 更新、context_version 不推进
 *  - 冻结快照与保存时 stats 一致（per_week/buckets/prs/data_updated_at 逐项）
 *  - 保存失败注入 → 整份不落（条目数与最新投影均不变）；一次性解除后重做成功
 *  - 业务数据变更 → 最新条 stale=true，正文与 generated_at 逐字不变
 *  - empty 种子 → 复盘空态可读、stale=false、不编造完成率
 *  - 源码断言：contract 仍是唯一形状来源；无新业务端点（新增仅 /api/dev/*）
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
const confirm = (id, revision) =>
  api("POST", `/api/drafts/${id}/confirm`, { revision });
const cv = async () =>
  (await api("GET", "/api/dev/status")).body?.context_version;
const reviewEntries = async () =>
  (await api("GET", "/api/dev/status")).body?.review_entries;
const stats = async () => (await api("GET", "/api/stats")).body;
const review = async () => (await api("GET", "/api/review")).body;
const saveReview = (text) => api("POST", "/api/dev/review/save", { text });
const reviewFailNext = () => api("POST", "/api/dev/review/fail-next", {});
const w1 = (s) => s.per_week.find((w) => w.week === "W1");
const w2 = (s) => s.per_week.find((w) => w.week === "W2");

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

const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

/* ========== 1. default 种子统计现算不回归 ========== */
console.log("\n--- default 种子统计基线 ---");
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });
{
  const s = await stats();
  check(
    "1 完成率：W1 2/3、W2 1/3",
    w1(s)?.planned === 3 &&
      w1(s)?.completed === 2 &&
      w2(s)?.planned === 3 &&
      w2(s)?.completed === 1,
    JSON.stringify({ w1: w1(s), w2: w2(s) }),
  );
  check(
    "1 三桶 1/1/1",
    s.buckets.met === 1 && s.buckets.unmet === 1 && s.buckets.pending === 1,
    JSON.stringify(s.buckets),
  );
  check(
    "1 PR 卧推 80×8",
    s.prs.some((p) => p.exercise.includes("卧推") && p.best_weight_kg === 80 && p.best_reps_at_weight === 8),
    JSON.stringify(s.prs),
  );
}

/* ========== 2. GET /api/review 最新条 + 种子 basis 与 stats 一致 ========== */
console.log("\n--- 最新条投影与种子 basis ---");
{
  const s = await stats();
  const rev = await review();
  const entries = await reviewEntries();
  check(
    "2 最新条投影可用字段（text/stale/generated_at）",
    typeof rev?.text === "string" &&
      rev.text.length > 0 &&
      typeof rev.stale === "boolean" &&
      typeof rev.generated_at === "string",
    JSON.stringify({ stale: rev?.stale, generated_at: rev?.generated_at }),
  );
  check(
    "2 default 种子：旧稿 stale=true、generated_at 固定",
    rev.stale === true && rev.generated_at === "2026-09-08T21:00:00+08:00",
    JSON.stringify({ stale: rev?.stale, generated_at: rev?.generated_at }),
  );
  check(
    "2 basis 可读且与当时 stats 一致（per_week/buckets/prs/data_updated_at）",
    rev.basis != null &&
      eq(rev.basis.per_week, s.per_week) &&
      eq(rev.basis.buckets, s.buckets) &&
      eq(rev.basis.prs, s.prs) &&
      rev.basis.data_updated_at === s.data_updated_at,
    JSON.stringify({
      basis_updated: rev.basis?.data_updated_at,
      stats_updated: s.data_updated_at,
    }),
  );
  check(
    "2 存储为追加列表：种子恰 1 条",
    Array.isArray(entries) && entries.length === 1,
    JSON.stringify(entries),
  );
}

/* ========== 3. 内部保存路径追加 + 旧条保留 + cv 不推进 ========== */
console.log("\n--- 保存追加（dev 桥 → saveReviewEntry） ---");
{
  const before = await review();
  const cvBefore = await cv();
  const saved = await saveReview("## 新复盘\n\nW1 2/3、W2 1/3；数字来自冻结快照。");
  check("3 保存成功返回条目与最新投影", saved.status === 200 && saved.body?.entry?.id != null);
  const rev = await review();
  const entries = await reviewEntries();
  const cvAfter = await cv();
  check(
    "3 最新条 = 新存正文、stale=false、generated_at 已更新",
    rev.text.startsWith("## 新复盘") &&
      rev.stale === false &&
      rev.generated_at !== before.generated_at,
    JSON.stringify({ stale: rev?.stale, generated_at: rev?.generated_at }),
  );
  check(
    "3 旧条仍在存储（append-only：2 条，含种子条）",
    entries.length === 2 && entries[0].generated_at === "2026-09-08T21:00:00+08:00",
    JSON.stringify(entries),
  );
  check("3 保存不推进 context_version", cvAfter === cvBefore, `${cvBefore} → ${cvAfter}`);
  // 再追加一条：重生成语义 = 追加，旧条（第 2 条）仍在
  await saveReview("## 重新生成复盘\n\n基于当下冻结事实。");
  const entries2 = await reviewEntries();
  const rev2 = await review();
  check(
    "3 重生成=再追加：3 条、最新条更新、前两条保留",
    entries2.length === 3 &&
      rev2.text.startsWith("## 重新生成复盘") &&
      entries2[1].id === saved.body.entry.id,
    JSON.stringify(entries2.map((e) => e.id)),
  );
}

/* ========== 4. 冻结快照与当时 stats 一致 ========== */
console.log("\n--- 冻结快照一致性 ---");
{
  const s0 = await stats();
  const saved = await saveReview("## 快照校验稿");
  const s1 = await stats();
  const basis = saved.body?.entry?.basis;
  check(
    "4 保存时 basis 与保存前/后 stats 一致（保存不触发重算）",
    basis != null &&
      eq(basis.per_week, s0.per_week) &&
      eq(basis.buckets, s0.buckets) &&
      eq(basis.prs, s0.prs) &&
      basis.data_updated_at === s0.data_updated_at &&
      eq(s1, s0),
    JSON.stringify({
      basis_updated: basis?.data_updated_at,
      s0: s0.data_updated_at,
      s1: s1.data_updated_at,
    }),
  );
  check(
    "4 basis.source_revision_ids 为非空 id 列表",
    Array.isArray(basis?.source_revision_ids) &&
      basis.source_revision_ids.length > 0 &&
      basis.source_revision_ids.every((x) => typeof x === "string"),
    JSON.stringify(basis?.source_revision_ids),
  );
  const rev = await review();
  check(
    "4 GET /api/review 挂上最新 basis（与保存返回一致）",
    rev.basis != null && eq(rev.basis, basis),
  );
}

/* ========== 5. 保存失败注入：整份不落 + 一次性解除 ========== */
console.log("\n--- 保存失败注入 ---");
{
  const beforeEntries = await reviewEntries();
  const beforeRev = await review();
  await reviewFailNext();
  const failedSave = await saveReview("## 这条不该落库");
  const afterEntries = await reviewEntries();
  const afterRev = await review();
  check(
    "5 注入后保存 500、条目数不变、最新投影逐字不变",
    failedSave.status === 500 &&
      afterEntries.length === beforeEntries.length &&
      afterRev.text === beforeRev.text &&
      afterRev.generated_at === beforeRev.generated_at,
    JSON.stringify({
      status: failedSave.status,
      n: `${beforeEntries.length}→${afterEntries.length}`,
    }),
  );
  const retry = await saveReview("## 注入解除后重做成功");
  check(
    "5 注入一次性：重做成功且最新条更新",
    retry.status === 200 && (await review()).text === "## 注入解除后重做成功",
  );
}

/* ========== 6. 业务数据变更 → 最新条 stale（正文不改写） ========== */
console.log("\n--- 业务变更置 stale ---");
{
  const before = await review();
  const res = await run(
    "s-f5-01-void",
    "整次录错，作废 2026-09-07 的杠铃平板卧推这次训练",
    "f5-01-void",
  );
  const ok = await confirm(res.draft.id, res.draft.revision);
  check("6 确认作废成功", ok.status === 200 && ok.body.newly_committed === true);
  const after = await review();
  check(
    "6 最新条 stale=true、正文与 generated_at 逐字不变",
    after.stale === true &&
      after.text === before.text &&
      after.generated_at === before.generated_at,
    JSON.stringify({
      stale: after?.stale,
      textSame: after?.text === before?.text,
      genSame: after?.generated_at === before?.generated_at,
    }),
  );
}

/* ========== 7. empty 种子：复盘空态 ========== */
console.log("\n--- empty 空态 ---");
await reset("empty");
{
  const s = await stats();
  const rev = await review();
  const entries = await reviewEntries();
  check(
    "7 空态可读：stale=false、text 说明无复盘、无 basis",
    rev.stale === false &&
      typeof rev.text === "string" &&
      rev.text.includes("没有可复盘") &&
      rev.basis == null &&
      typeof rev.generated_at === "string",
    JSON.stringify({ text: rev?.text, stale: rev?.stale }),
  );
  check(
    "7 不编造完成率：正文无 x/y 与百分比、stats 分母为空",
    !/\d+\s*\/\s*\d+/.test(rev.text) &&
      !/%/.test(rev.text) &&
      s.per_week.length === 0 &&
      s.prs.length === 0,
    JSON.stringify({ per_week: s.per_week, prs: s.prs }),
  );
  check("7 empty 无复盘条目", Array.isArray(entries) && entries.length === 0, JSON.stringify(entries));
  // 空态下保存仍可追加（F5-02 生成路径的存储口；正文策略归 F5-02）
  const saved = await saveReview("## 空种子说明稿\n\n无可复盘事实。");
  check(
    "7 空种子也可经保存路径追加（空态 → 最新条）",
    saved.status === 200 && (await review()).text.startsWith("## 空种子说明稿"),
  );
}

/* ========== 8. 源码断言：契约唯一形状来源；无新业务端点 ========== */
console.log("\n--- 源码断言 ---");
{
  const contract = readFileSync(
    path.join(root, "src/lib/contract.ts"),
    "utf8",
  );
  const server = readFileSync(path.join(root, "src/mock/server.ts"), "utf8");
  check(
    "8 contract 定义 ReviewEntry/ReviewBasis/ReviewDoc（唯一形状来源）",
    /export interface ReviewEntry\b/.test(contract) &&
      /export interface ReviewBasis\b/.test(contract) &&
      /export interface ReviewDoc\b/.test(contract),
  );
  check(
    "8 mock 从 contract 导入复盘类型、不自建同名 interface",
    /ReviewBasis,\s*\n\s*ReviewDoc,\s*\n\s*ReviewEntry,/.test(server) &&
      !/interface ReviewEntry\b/.test(server) &&
      !/interface ReviewBasis\b/.test(server),
  );
  check(
    "8 无新业务端点：契约 REST 清单仍只有 GET /api/review（无 POST/PUT review(s)）",
    /GET\s+\/api\/review\s+-> ReviewDoc/.test(contract) &&
      !/POST\s+\/api\/review/.test(contract) &&
      !/\/api\/reviews\b/.test(contract),
  );
  check(
    "8 复盘保存/失败注入仅走 /api/dev/*（dev 桥，非契约端点）",
    /\/api\/dev\/review\/save/.test(server) &&
      /\/api\/dev\/review\/fail-next/.test(server) &&
      !/path === "\/api\/review" && method === "POST"/.test(server),
  );
}

await vite.close();
console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
