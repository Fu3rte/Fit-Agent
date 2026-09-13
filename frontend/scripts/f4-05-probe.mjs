/**
 * F4-05 验收探针（走真实 mock REST + 源码断言）：记录页修订追溯与统计／复盘刷新
 * （plans/stage4.md F4-05 / §3.4 / §7 第 4、8 步）。
 *
 * 运行：node scripts/f4-05-probe.mjs
 * 覆盖：
 *  - 源码：RecordsPage 含 voided/已作废、历史修订、修订说明；无 RIR/输入控件
 *  - 源码：ReviewPage 含 stale 徽章文案「依据已变更」「可重新生成」、无输入控件
 *  - 源码：App 路由未新增（records/review 各一条）
 *  - 更正后三桶按最新有效修订与原对照安排重判（渲染现算结果）
 *  - 作废后统计不含该次、历史仍可查；/records 标注已作废；完成率分母不变
 *  - 复盘正文与 generated_at 与变更前逐字一致且 stale=true；徽章文案正确
 *  - 补全文案：第 1 组次数 — → 12（无 undefined）
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
const records = async () => (await api("GET", "/api/records")).body?.records;
const stats = async () => (await api("GET", "/api/stats")).body;
const review = async () => (await api("GET", "/api/review")).body;
const w2 = (s) => s.per_week.find((w) => w.week === "W2");
const recById = (list, id) => list.find((r) => r.training_session_id === id);

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

/* ========== 1. 源码断言：RecordsPage 展示消费 ========== */
console.log("\n--- 源码断言 RecordsPage ---");
{
  const src = readFileSync(
    path.join(root, "src/features/records/RecordsPage.tsx"),
    "utf8",
  );
  check(
    "1 RecordsPage 含 voided/已作废徽章（作废保留展示）",
    /voided/.test(src) && /已作废/.test(src),
  );
  check(
    "1 RecordsPage 含历史修订/只读追溯段（revisions 消费）",
    /revisions/.test(src) &&
      (/历史修订|只读追溯|RevisionRow/.test(src)),
  );
  check(
    "1 RecordsPage 含修订说明与组级三桶摘要",
    /修订说明/.test(src) && /组级判定|JudgementBuckets/.test(src),
  );
  check(
    "1 RecordsPage 保持只读：无 input/select/textarea",
    !/<input|<select|<textarea/i.test(src),
  );
  check(
    "1 RecordsPage 主文案无 RIR 缩写（已拍隐藏）",
    !/\bRIR\b/.test(src),
  );
  check(
    "1 RecordsPage 仍含归属徽章（新增/更正）与状态文案（有效/待补全）",
    /新增/.test(src) && /更正/.test(src) && /有效/.test(src) && /待补全/.test(src),
  );
}

/* ========== 2. 源码断言：ReviewPage 复盘刷新 ========== */
console.log("\n--- 源码断言 ReviewPage ---");
{
  const src = readFileSync(
    path.join(root, "src/features/review/ReviewPage.tsx"),
    "utf8",
  );
  check(
    "2 ReviewPage 含 stale 徽章文案「依据已变更」「可重新生成」",
    /依据已变更/.test(src) && /可重新生成/.test(src) && /stale/.test(src),
  );
  check(
    "2 ReviewPage 消费现算统计（getStats + data_updated_at）",
    /getStats/.test(src) && /data_updated_at/.test(src),
  );
  check(
    "2 ReviewPage 消费复盘正文（getReview + generated_at）",
    /getReview/.test(src) && /generated_at/.test(src),
  );
  check(
    "2 ReviewPage 保持只读：无 input/select/textarea",
    !/<input|<select|<textarea/i.test(src),
  );
  check(
    "2 ReviewPage 主文案无 RIR 缩写",
    !/\bRIR\b/.test(src),
  );
}

/* ========== 3. 源码断言：App 路由未新增 ========== */
console.log("\n--- 源码断言 App 路由 ---");
{
  const src = readFileSync(path.join(root, "src/app/App.tsx"), "utf8");
  const recordsRoutes = src.match(/path="\/records"/g) ?? [];
  const reviewRoutes = src.match(/path="\/review"/g) ?? [];
  const detailRoutes =
    src.match(/path="\/(?:records|review)\/[^"]+"/g) ?? [];
  check(
    "3 App 路由未新增：/records 与 /review 各恰一条，无详情子路由",
    recordsRoutes.length === 1 &&
      reviewRoutes.length === 1 &&
      detailRoutes.length === 0,
    JSON.stringify({
      records: recordsRoutes.length,
      review: reviewRoutes.length,
      detail: detailRoutes,
    }),
  );
}

/* ========== 4. 补全文案：第 1 组次数 — → 12（无 undefined） ========== */
console.log("\n--- 补全文案可读 ---");
{
  await reset("default");
  await api("PUT", "/api/provider/api-key", { api_key: "probe" });

  const res = await run(
    "s-f4-05-polish",
    "更正 2026-09-05 的训练记录：哑铃弯举 第1组改成12次",
    "f4-05-polish",
  );
  check(
    "4 补全文案不含 undefined：「次数 — → 12」",
    /次数\s*—\s*→\s*12/.test(res.text ?? "") &&
      !/undefined/.test(res.text ?? ""),
    (res.text ?? "").slice(0, 240),
  );
  const d = res.draft;
  check(
    "4 草稿 diff 仍是 — → 12（FieldDiff 未回归）",
    d?.diff?.some(
      (x) => /次数/.test(x.field ?? "") && x.old_value === "—" && x.new_value === "12",
    ),
    JSON.stringify(d?.diff?.filter((x) => /次数/.test(x.field ?? ""))),
  );
  const ok = await confirm(d.id, d.revision);
  check(
    "4 确认补全：status=valid（为后续展示段准备）",
    ok.status === 200 &&
      recById(await records(), "ts-seed-0905")?.status === "valid",
    JSON.stringify(recById(await records(), "ts-seed-0905")?.status),
  );
}

/* ========== 5. 更正后三桶按最新有效修订重判（展示渲染现算） ========== */
console.log("\n--- 更正后三桶重判 ---");
{
  await reset("default");
  await api("PUT", "/api/provider/api-key", { api_key: "probe" });

  const s0 = await stats();
  const r0907 = recById(await records(), "ts-seed-0907");
  check(
    "5 前置：09-07 valid、组级三桶 1/1/1（与种子一致）",
    r0907?.status === "valid" &&
      r0907?.judgement?.met === 1 &&
      r0907?.judgement?.unmet === 1 &&
      r0907?.judgement?.pending === 1,
    JSON.stringify(r0907?.judgement),
  );
  check(
    "5 前置统计：全局三桶 1/1/1、W2 1/3",
    s0.buckets.met === 1 &&
      s0.buckets.unmet === 1 &&
      s0.buckets.pending === 1 &&
      w2(s0)?.completed === 1 &&
      w2(s0)?.planned === 3,
    JSON.stringify({ b: s0.buckets, w2: w2(s0) }),
  );

  // 更正：未符合那组（第2组 5 次）改为 7 次 → 应 met
  const res = await run(
    "s-f4-05-rejudge",
    "更正 2026-09-07 的训练记录：杠铃平板卧推 第2组改成7次",
    "f4-05-rejudge",
  );
  const d = res.draft;
  check(
    "5 更正稿：定位 ts-seed-0907、第2组 reps=7、保留对照安排",
    d?.payload?.training_session_id === "ts-seed-0907" &&
      d.payload.exercises[0].sets[1].reps === 7 &&
      d.payload.arrangement_revision_id != null,
    JSON.stringify({
      ts: d?.payload?.training_session_id,
      set2: d?.payload?.exercises?.[0]?.sets?.[1],
      arr: d?.payload?.arrangement_revision_id,
    }),
  );
  const ok = await confirm(d.id, d.revision);
  check("5 确认成功", ok.status === 200 && ok.body.newly_committed === true);

  const r1 = recById(await records(), "ts-seed-0907");
  const s1 = await stats();
  check(
    "5 更正后：09-07 组级三桶重判为 2/0/1（第2组 5→7 变 met；第3组仍 pending）",
    r1?.judgement?.met === 2 &&
      r1?.judgement?.unmet === 0 &&
      r1?.judgement?.pending === 1,
    JSON.stringify(r1?.judgement),
  );
  check(
    "5 更正后统计：全局三桶 2/0/1、W2 仍 1/3（完成率不因组内更正变化）",
    s1.buckets.met === 2 &&
      s1.buckets.unmet === 0 &&
      s1.buckets.pending === 1 &&
      w2(s1)?.completed === 1 &&
      w2(s1)?.planned === 3,
    JSON.stringify({ b: s1.buckets, w2: w2(s1) }),
  );
  check(
    "5 旧修订可只读追溯：revisions 含原 incomplete? 不，含旧 valid（组2=5）与新 valid（组2=7）",
    Array.isArray(r1?.revisions) &&
      r1.revisions.length >= 2 &&
      r1.revisions.some(
        (rev) =>
          rev.id !== r1.id &&
          rev.status === "valid" &&
          rev.sets?.[1]?.reps === 5,
      ) &&
      r1.revisions.some(
        (rev) =>
          rev.id === r1.id &&
          rev.status === "valid" &&
          rev.sets?.[1]?.reps === 7,
      ) &&
      r1.revisions.every((rev) => typeof rev.confirmed_at === "string"),
    JSON.stringify(
      r1?.revisions?.map((rev) => ({
        id: rev.id === r1.id ? "cur" : "old",
        set2: rev.sets?.[1]?.reps,
        at: rev.confirmed_at,
      })),
    ),
  );
  // 修订说明：契约字段可选；展示层消费（RecordsPage 源码已断言文案）
  check(
    "5 旧修订带 confirmed_at 可追溯时间",
    r1.revisions.every((rev) => typeof rev.confirmed_at === "string" && rev.confirmed_at.length > 0),
    JSON.stringify(r1.revisions.map((rev) => rev.confirmed_at)),
  );
}

/* ========== 6. 作废后统计退出 + /records 保留展示 + 分母不变 ========== */
console.log("\n--- 作废退出统计 / 保留展示 ---");
{
  await reset("default");
  await api("PUT", "/api/provider/api-key", { api_key: "probe" });

  const s0 = await stats();
  const rev0 = await review();
  const w2Before = w2(s0);

  // 作废 09-07（三桶 1/1/1 来源 + W2 完成 1 次）
  const res = await run(
    "s-f4-05-void",
    "整次录错，作废 2026-09-07 的杠铃平板卧推这次训练",
    "f4-05-void",
  );
  const d = res.draft;
  check(
    "6 作废稿：kind=training_void、载荷只含 ts-seed-0907",
    d?.kind === "training_void" &&
      d?.payload?.training_session_id === "ts-seed-0907",
    JSON.stringify(d?.payload),
  );
  const ok = await confirm(d.id, d.revision);
  check("6 确认作废成功", ok.status === 200 && ok.body.newly_committed === true);

  const r0907 = recById(await records(), "ts-seed-0907");
  const s1 = await stats();
  const rev1 = await review();
  check(
    "6 /records 标注已作废：status=voided、kind 保留、旧修订仍可查（≥2 条且含旧 valid）",
    r0907?.status === "voided" &&
      Array.isArray(r0907?.revisions) &&
      r0907.revisions.length >= 2 &&
      r0907.revisions.some(
        (rev) => rev.status === "valid" && rev.sets?.length > 0,
      ),
    JSON.stringify({
      status: r0907?.status,
      n: r0907?.revisions?.length,
      statuses: r0907?.revisions?.map((x) => x.status),
    }),
  );
  check(
    "6 统计退出该次：全局三桶 0/0/0（09-07 三组退出；09-05 incomplete 本就不进三桶）",
    s1.buckets.met === 0 && s1.buckets.unmet === 0 && s1.buckets.pending === 0,
    JSON.stringify(s1.buckets),
  );
  check(
    "6 完成率分母不变：W2 planned 仍 3、completed 0/1→0",
    w2(s1)?.planned === w2Before?.planned &&
      w2(s1)?.planned === 3 &&
      w2(s1)?.completed === 0,
    JSON.stringify({ before: w2Before, after: w2(s1) }),
  );
  check(
    "6 PR 现算不含作废来源（09-07 非 PR 源则 PR 不变；仍含 80×8）",
    s1.prs.some((e) => e.best_weight_kg === 80 && e.best_reps_at_weight === 8),
    JSON.stringify(s1.prs),
  );
  check(
    "6 复盘 stale：正文与 generated_at 与变更前逐字一致、stale=true",
    rev1.stale === true &&
      rev1.generated_at === rev0.generated_at &&
      rev1.text === rev0.text,
    JSON.stringify({
      stale: rev1.stale,
      genSame: rev1.generated_at === rev0.generated_at,
      textSame: rev1.text === rev0.text,
    }),
  );
  check(
    "6 统计 data_updated_at 为字符串（页脚消费字段仍在）",
    typeof s1.data_updated_at === "string" && s1.data_updated_at.length > 0,
    s1.data_updated_at,
  );
}

await vite.close();
console.log(`\nPASS ${passed} / FAIL ${failed}`);
console.log(failed === 0 ? "全部通过" : `${failed} 项失败`);
process.exit(failed === 0 ? 0 : 1);
