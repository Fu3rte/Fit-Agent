/**
 * F5-02 验收探针（走真实 mock REST + 源码断言）：对话显式生成／重新生成复盘
 * （plans/stage5.md F5-02 / §3.1–3.2 / §7 第 1–2、8 步）。
 *
 * 运行：node scripts/f5-02-probe.mjs
 * 覆盖：
 *  - 显式「生成复盘」→ 冻结渲染 + append；最新条 text 含冻结数字（W1 2/3、W2 1/3、三桶 1/1/1、80）
 *  - generated_at 更新；旧条仍在 dev/status review_entries
 *  - 生成前后 cv 不变；计划/日程不变
 *  - 「重新生成复盘」→ 再追加，条数+1，最新条更新
 *  - 模糊消息（无生成意图）→ 不追加
 *  - empty 种子显式生成 → 可读拒绝，不编造数值（无 2/3、80kg 等）
 *  - fail-next + 显式生成 → 不落库；重做成功
 *  - 正文数字与 /api/stats 及 basis 一致（抽样）
 *  - 源码：REVIEW_REPLY 写死分支已替换；意图正则存在；无新业务端点
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
const reviewEntries = async () =>
  (await api("GET", "/api/dev/status")).body?.review_entries;
const stats = async () => (await api("GET", "/api/stats")).body;
const review = async () => (await api("GET", "/api/review")).body;
const planVersion = async () =>
  (await api("GET", "/api/dev/status")).body?.plan_version;
const schedules = async () =>
  (await api("GET", "/api/dev/status")).body?.schedules;
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

/* ========== 1. 显式「生成复盘」→ 冻结渲染 + append ========== */
console.log("\n--- 显式生成复盘 ---");
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });
{
  const s0 = await stats();
  const revBefore = await review();
  const entriesBefore = await reviewEntries();
  const cvBefore = await cv();
  const planBefore = await planVersion();
  const schedBefore = await schedules();

  const res = await run("s-f5-02-gen", "生成复盘", "f5-02-gen");
  const rev = await review();
  const entries = await reviewEntries();
  const cvAfter = await cv();
  const planAfter = await planVersion();
  const schedAfter = await schedules();

  check(
    "1 对话回复说明已保存与依据时间",
    /已保存复盘/.test(res.text) &&
      /生成时间/.test(res.text) &&
      /依据数据时间/.test(res.text),
    res.text.slice(-120),
  );
  check(
    "1 最新条 text 含冻结数字：W1 2/3、W2 1/3",
    /2\/3/.test(rev.text) &&
      /1\/3/.test(rev.text) &&
      rev.text.includes("W1") &&
      rev.text.includes("W2"),
  );
  check(
    "1 最新条 text 含三桶 1/1/1",
    /符合 1 组/.test(rev.text) &&
      /未符合 1 组/.test(rev.text) &&
      /待补全 1 组/.test(rev.text),
  );
  check(
    "1 最新条 text 含 PR 80",
    /80kg/.test(rev.text) || /80kg × 8/.test(rev.text),
    rev.text.match(/80[^\n]*/)?.[0] ?? "no-80",
  );
  check(
    "1 generated_at 更新且 stale=false",
    rev.stale === false && rev.generated_at !== revBefore.generated_at,
    JSON.stringify({ before: revBefore.generated_at, after: rev.generated_at }),
  );
  check(
    "1 旧条仍在存储（append：2 条，含种子）",
    entries.length === entriesBefore.length + 1 &&
      entries[0].generated_at === entriesBefore[0].generated_at,
    JSON.stringify(entries.map((e) => e.id)),
  );
  check("1 生成前后 cv 不变", cvAfter === cvBefore, `${cvBefore} → ${cvAfter}`);
  check(
    "1 计划/日程不变",
    eq(planAfter, planBefore) && eq(schedAfter, schedBefore),
  );
  check("1 对话不建草稿", res.draft == null);
  check(
    "1 正文数字与 /api/stats 一致",
    rev.basis != null &&
      eq(rev.basis.per_week, s0.per_week) &&
      eq(rev.basis.buckets, s0.buckets) &&
      rev.basis.data_updated_at === s0.data_updated_at &&
      `${w1(s0).completed}/${w1(s0).planned}` === "2/3" &&
      `${w2(s0).completed}/${w2(s0).planned}` === "1/3",
    JSON.stringify({
      basis_pw: rev.basis?.per_week,
      stats_pw: s0.per_week,
    }),
  );
}

/* ========== 2. 「重新生成复盘」→ 再追加 ========== */
console.log("\n--- 重新生成 ---");
{
  const before = await reviewEntries();
  const revBefore = await review();
  const res = await run("s-f5-02-gen", "重新生成复盘", "f5-02-regen");
  const entries = await reviewEntries();
  const rev = await review();
  check(
    "2 再追加一条：条数+1",
    entries.length === before.length + 1,
    `${before.length} → ${entries.length}`,
  );
  check(
    "2 最新条更新（generated_at 变化，正文仍含冻结数字）",
    rev.generated_at !== revBefore.generated_at &&
      /2\/3/.test(rev.text) &&
      /80kg/.test(rev.text),
  );
  check(
    "2 旧条（含首次生成条）仍在",
    entries[before.length - 1].generated_at ===
      revBefore.generated_at,
  );
  check("2 回复说明已保存", /已保存复盘/.test(res.text));
}

/* ========== 3. 模糊消息 → 不追加 ========== */
console.log("\n--- 模糊意图 ---");
{
  const before = await reviewEntries();
  const revBefore = await review();
  const res = await run("s-f5-02-fuzzy", "复盘是什么", "f5-02-fuzzy");
  const after = await reviewEntries();
  const rev = await review();
  check(
    "3 模糊消息不落库（条数不变）",
    after.length === before.length,
    `${before.length} → ${after.length}`,
  );
  check(
    "3 最新条逐字不变",
    rev.text === revBefore.text && rev.generated_at === revBefore.generated_at,
  );
  check(
    "3 回复为解释口径（不含「已保存复盘」）",
    !/已保存复盘/.test(res.text) && /复盘/.test(res.text),
    res.text.slice(0, 80),
  );
  // 另一个模糊样例
  const res2 = await run("s-f5-02-fuzzy", "最近怎么样", "f5-02-fuzzy2");
  const after2 = await reviewEntries();
  check(
    "3 非复盘询问也不落库",
    after2.length === before.length && !/已保存复盘/.test(res2.text),
  );
  // 「看/给我看一下复盘」→ 只读解释，不 append（2026-09-13 owner 收紧）
  for (const viewMsg of ["看复盘", "给我看一下复盘"]) {
    const resV = await run("s-f5-02-fuzzy", viewMsg, "f5-02-view");
    const afterV = await reviewEntries();
    check(
      `3 「${viewMsg}」不 append（条数不变）`,
      afterV.length === before.length && !/已保存复盘/.test(resV.text),
      `${before.length} → ${afterV.length}`,
    );
  }
}

/* ========== 4. empty 种子：可读拒绝、不编造 ========== */
console.log("\n--- empty 空数据 ---");
await reset("empty");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });
{
  const entriesBefore = await reviewEntries();
  const res = await run("s-f5-02-empty", "生成复盘", "f5-02-empty");
  const entries = await reviewEntries();
  const rev = await review();
  check(
    "4 empty 显式生成：可读拒绝（拒绝/没有可复盘）",
    /拒绝|没有可复盘|无法生成/.test(res.text),
    res.text.slice(0, 100),
  );
  check(
    "4 不编造完成率/PR（无 2/3、80kg）",
    !/2\s*\/\s*3/.test(res.text) &&
      !/80\s*kg/i.test(res.text) &&
      !/66\.7%/.test(res.text),
  );
  check(
    "4 empty 下不新增复盘条目（拒绝路径）",
    entries.length === entriesBefore.length,
    `${entriesBefore.length} → ${entries.length}`,
  );
  check(
    "4 空态投影仍可读、无 basis 编造",
    rev.stale === false && rev.basis == null,
  );
}

/* ========== 5. fail-next 注入：对话路径不落库；重做成功 ========== */
console.log("\n--- 保存失败注入 ---");
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });
{
  const before = await reviewEntries();
  const revBefore = await review();
  await reviewFailNext();
  const res = await run("s-f5-02-fail", "生成复盘", "f5-02-fail");
  const afterFail = await reviewEntries();
  const revFail = await review();
  check(
    "5 注入后对话回复说明失败且未保存",
    /保存失败|未落库/.test(res.text) && !/已保存复盘/.test(res.text),
    res.text.slice(-80),
  );
  check(
    "5 注入后条数不变、最新条逐字不变",
    afterFail.length === before.length &&
      revFail.text === revBefore.text &&
      revFail.generated_at === revBefore.generated_at,
    `${before.length} → ${afterFail.length}`,
  );
  const retry = await run("s-f5-02-fail", "生成复盘", "f5-02-retry");
  const afterOk = await reviewEntries();
  check(
    "5 重做成功：条数+1、回复含已保存",
    afterOk.length === before.length + 1 && /已保存复盘/.test(retry.text),
    `${before.length} → ${afterOk.length}`,
  );
}

/* ========== 6. 源码断言 ========== */
console.log("\n--- 源码断言 ---");
{
  const server = readFileSync(path.join(root, "src/mock/server.ts"), "utf8");
  const contract = readFileSync(path.join(root, "src/lib/contract.ts"), "utf8");
  check(
    "6 写死 REVIEW_REPLY 已替换为 saveReviewEntry 路径",
    !/const REVIEW_REPLY\s*=/.test(server) &&
      /function reviewGenerateReply/.test(server) &&
      /saveReviewEntry\(/.test(server),
  );
  check(
    "6 显式意图正则 REVIEW_GENERATE 存在",
    /const REVIEW_GENERATE\s*=/.test(server),
  );
  check(
    "6 正文由 basis 渲染（renderReviewText 接受 ReviewBasis）",
    /function renderReviewText\(basis: ReviewBasis\)/.test(server),
  );
  check(
    "6 无新业务端点：契约仍仅 GET /api/review",
    /GET\s+\/api\/review\s+-> ReviewDoc/.test(contract) &&
      !/POST\s+\/api\/review/.test(contract),
  );
}

await vite.close();
console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
