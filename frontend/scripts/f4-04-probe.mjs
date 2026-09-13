/**
 * F4-04 验收探针（走真实 mock REST）：补全 incomplete 转 valid
 * （plans/stage4.md F4-04 / §3.3 / §7 第 7 步）。
 *
 * 运行：node scripts/f4-04-probe.mjs
 * 覆盖：
 *  - 前置：ts-seed-0905 incomplete（缺每组次数）、无对照、不在 PR、三桶/W2 不变
 *  - 更正定位 09-05 哑铃弯举 → 同一训练身份完整修订；载荷保留 12kg 与组类型
 *  - 「第1组改成12次」补齐次数 → draft diff 次数 — → 12；未述字段保持（不编造）
 *  - 生成前后正式数据/统计/cv 不变
 *  - 确认：status=valid、kind=correction、身份数不变、cv+1
 *  - 转 valid 后 PR 现算自动包含哑铃弯举 12kg×N（独立完成、无辅助）
 *  - 无对照安排：仍不进三桶、不增加计划完成次数（W2 仍 1/3）
 *  - 仍不完整时可继续保存 incomplete 修订（不编造缺失次数）
 *  - 缺必填时不允许被判定为 valid（派生规则：无任何组有次数/时长 → incomplete）
 */
import path from "node:path";
import { fileURLToPath } from "node:url";
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
const revise = (id, payload, revision) =>
  api("POST", `/api/drafts/${id}/revise`, { payload, revision });
const cv = async () =>
  (await api("GET", "/api/dev/status")).body?.context_version;
const records = async () => (await api("GET", "/api/records")).body?.records;
const stats = async () => (await api("GET", "/api/stats")).body;
const w2 = (s) => s.per_week.find((w) => w.week === "W2");
const recById = (list, id) => list.find((r) => r.training_session_id === id);
const curlPr = (s) =>
  s.prs.find((e) => e.exercise === "哑铃弯举" || /弯举/.test(e.exercise ?? ""));

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

/* ========== 1. 前置：incomplete 种子不进 PR/三桶/完成率 ========== */
console.log("\n--- 前置基线 ---");
{
  await reset("default");
  await api("PUT", "/api/provider/api-key", { api_key: "probe" });

  const list0 = await records();
  const s0 = await stats();
  const r0905 = recById(list0, "ts-seed-0905");
  check(
    "1 前置：ts-seed-0905 incomplete、缺 reps、保留 12kg 与 working、无对照",
    r0905?.status === "incomplete" &&
      r0905.sets?.length === 1 &&
      r0905.sets[0].weight_kg === 12 &&
      r0905.sets[0].reps == null &&
      r0905.sets[0].set_type === "working" &&
      r0905.arrangement_revision_id == null,
    JSON.stringify({ status: r0905?.status, sets: r0905?.sets }),
  );
  check(
    "1 前置统计：三桶 1/1/1、W2 1/3、PR 仅卧推 80×8（无哑铃弯举）",
    s0.buckets.met === 1 &&
      s0.buckets.unmet === 1 &&
      s0.buckets.pending === 1 &&
      w2(s0)?.planned === 3 &&
      w2(s0)?.completed === 1 &&
      !curlPr(s0) &&
      s0.prs.some((e) => e.best_weight_kg === 80 && e.best_reps_at_weight === 8),
    JSON.stringify({ buckets: s0.buckets, prs: s0.prs }),
  );
}

/* ========== 2. 更正补齐次数 → 生成草稿（不动正式数据） ========== */
console.log("\n--- 更正生成 ---");
{
  await reset("default");
  await api("PUT", "/api/provider/api-key", { api_key: "probe" });

  const cv0 = await cv();
  const list0 = await records();
  const s0 = await stats();

  const res = await run(
    "s-f4-04-gen",
    "更正 2026-09-05 的训练记录：哑铃弯举 第1组改成12次",
    "f4-04-gen",
  );
  const d = res.draft;
  const p = d?.payload;

  check(
    "2 定位：ts-seed-0905、同一训练身份、pending",
    d?.kind === "training_record" &&
      d.status === "pending" &&
      p?.training_session_id === "ts-seed-0905" &&
      p?.occurred_on === "2026-09-05",
    JSON.stringify({ kind: d?.kind, ts: p?.training_session_id, on: p?.occurred_on }),
  );
  check(
    "2 补齐次数：第1组 reps=12；12kg 与 set_type=work 保持（不编造未述字段）",
    p?.exercises?.length === 1 &&
      p.exercises[0].sets?.length === 1 &&
      p.exercises[0].sets[0].reps === 12 &&
      p.exercises[0].sets[0].load?.value_text === "12" &&
      p.exercises[0].sets[0].load?.unit === "kg" &&
      p.exercises[0].sets[0].set_type === "work",
    JSON.stringify(p?.exercises?.[0]?.sets),
  );
  check(
    "2 无对照安排：不携带 arrangement_revision_id",
    p?.arrangement_revision_id == null,
    JSON.stringify(p?.arrangement_revision_id),
  );
  check(
    "2 生成前后：records 仍 4 身份、0905 仍 incomplete、cv 不变、stats 不变",
    list0.length === 4 &&
      (await records()).length === 4 &&
      recById(await records(), "ts-seed-0905")?.status === "incomplete" &&
      (await cv()) === cv0 &&
      JSON.stringify((await stats()).buckets) === JSON.stringify(s0.buckets) &&
      !curlPr(await stats()),
    JSON.stringify({
      n: (await records()).length,
      st: recById(await records(), "ts-seed-0905")?.status,
      cv0,
      cv1: await cv(),
    }),
  );
  check(
    "2 回复含同一训练身份语义（不新增训练身份）",
    /(不新增训练身份|同一训练身份)/.test(res.text ?? ""),
    (res.text ?? "").slice(0, 200),
  );
  const setDiff = d?.diff?.find((x) => /第\s*1\s*组.*次数/.test(x.field ?? ""));
  check(
    "2 Diff 第1组次数 old=— new=12（原缺失）",
    setDiff != null && (setDiff.old_value === "—" || setDiff.old_value == null) && setDiff.new_value === "12",
    JSON.stringify(setDiff),
  );
}

/* ========== 3. 确认：转 valid + PR 自动纳入 + 无对照不进三桶 ========== */
console.log("\n--- 确认转 valid / PR ---");
{
  await reset("default");
  await api("PUT", "/api/provider/api-key", { api_key: "probe" });

  const cv0 = await cv();
  const s0 = await stats();

  const res = await run(
    "s-f4-04-confirm",
    "更正 ts-seed-0905 的训练记录：哑铃弯举 第1组改成12次",
    "f4-04-confirm",
  );
  const d = res.draft;
  check(
    "3 前置：更正稿补齐 reps=12",
    d?.payload?.training_session_id === "ts-seed-0905" &&
      d.payload.exercises[0].sets[0].reps === 12,
    JSON.stringify(d?.payload?.exercises?.[0]?.sets),
  );

  const ok = await confirm(d.id, d.revision);
  check(
    "3 确认成功：cv 恰好 +1、training_session_id=ts-seed-0905",
    ok.status === 200 &&
      ok.body.newly_committed === true &&
      ok.body.training_session_id === "ts-seed-0905" &&
      ok.body.context_version === cv0 + 1,
    JSON.stringify(ok.body),
  );

  const list = await records();
  const r0905 = recById(list, "ts-seed-0905");
  check(
    "3 转 valid：status=valid、kind=correction、身份数仍 4（身份不变）",
    list.length === 4 &&
      r0905?.status === "valid" &&
      r0905?.kind === "correction" &&
      r0905?.training_session_id === "ts-seed-0905" &&
      r0905?.date === "2026-09-05" &&
      r0905?.sets?.[0]?.reps === 12 &&
      r0905?.sets?.[0]?.weight_kg === 12,
    JSON.stringify({
      n: list.length,
      status: r0905?.status,
      kind: r0905?.kind,
      set0: r0905?.sets?.[0],
    }),
  );
  check(
    "3 旧修订可追溯：revisions 含 incomplete 历史（无 reps）与当前 valid（reps=12）",
    Array.isArray(r0905?.revisions) &&
      r0905.revisions.length >= 2 &&
      r0905.revisions.some((rev) => rev.status === "incomplete") &&
      r0905.revisions.some(
        (rev) => rev.status === "valid" && rev.sets?.[0]?.reps === 12,
      ),
    JSON.stringify(
      r0905?.revisions?.map((rev) => ({
        status: rev.status,
        reps: rev.sets?.[0]?.reps,
      })),
    ),
  );

  const s1 = await stats();
  const pr = curlPr(s1);
  check(
    "3 PR 现算自动包含：哑铃弯举 12kg×12（独立完成、无辅助）",
    pr != null &&
      pr.best_weight_kg === 12 &&
      pr.best_reps_at_weight === 12 &&
      s1.prs.some((e) => e.best_weight_kg === 80 && e.best_reps_at_weight === 8),
    JSON.stringify(pr ?? s1.prs),
  );
  check(
    "3 无对照安排：三桶仍 1/1/1、W2 仍 1/3（不进三桶、不增加计划完成次数）",
    s1.buckets.met === 1 &&
      s1.buckets.unmet === 1 &&
      s1.buckets.pending === 1 &&
      w2(s1)?.planned === 3 &&
      w2(s1)?.completed === 1 &&
      r0905?.arrangement_revision_id == null &&
      r0905?.judgement == null,
    JSON.stringify({
      buckets: s1.buckets,
      w2: w2(s1),
      arr: r0905?.arrangement_revision_id,
      judgement: r0905?.judgement,
    }),
  );

  const again = await confirm(d.id, d.revision);
  check(
    "3 重复确认幂等：newly_committed=false、cv 不再 +1",
    again.status === 200 &&
      again.body.newly_committed === false &&
      (await cv()) === cv0 + 1,
    JSON.stringify({
      newly: again.body.newly_committed,
      cv: again.body.context_version,
    }),
  );
}

/* ========== 4. 仍不完整可继续保存 incomplete；未述字段不编造 ========== */
console.log("\n--- 仍 incomplete 可继续保存 ---");
{
  await reset("default");
  await api("PUT", "/api/provider/api-key", { api_key: "probe" });

  const cv0 = await cv();
  // 不补齐次数：直接确认当前载荷（仍缺 reps）→ 应仍为 incomplete
  const res = await run(
    "s-f4-04-stay",
    "更正 ts-seed-0905 的训练记录：哑铃弯举",
    "f4-04-stay",
  );
  const d = res.draft;
  check(
    "4 前置：更正稿载入当前事实（仍无 reps、12kg 保持、不编造）",
    d?.payload?.training_session_id === "ts-seed-0905" &&
      d.payload.exercises[0].sets[0].reps == null &&
      d.payload.exercises[0].sets[0].load?.value_text === "12" &&
      d.payload.exercises[0].sets[0].set_type === "work",
    JSON.stringify(d?.payload?.exercises?.[0]?.sets),
  );

  const ok = await confirm(d.id, d.revision);
  const list = await records();
  const r0905 = recById(list, "ts-seed-0905");
  check(
    "4 仍不完整确认：status 仍 incomplete、不进 PR、身份数不变、cv+1",
    ok.status === 200 &&
      r0905?.status === "incomplete" &&
      !curlPr(await stats()) &&
      (await records()).length === 4 &&
      r0905.sets?.[0]?.reps == null &&
      r0905.sets?.[0]?.weight_kg === 12,
    JSON.stringify({
      confirm: ok.body,
      status: r0905?.status,
      set0: r0905?.sets?.[0],
      prs: (await stats()).prs,
    }),
  );

  // 缺必填不允许 valid：再经 revise 补齐 → 转 valid（同链路可再次补全）
  const ed = await run(
    "s-f4-04-later",
    "更正 ts-seed-0905 的训练记录：哑铃弯举 第1组改成12次",
    "f4-04-later",
  );
  const edOk = await confirm(ed.draft.id, ed.draft.revision);
  const after = recById(await records(), "ts-seed-0905");
  check(
    "4 缺必填不得判 valid；补齐后同一链路再确认 → valid 且进 PR",
    edOk.status === 200 &&
      after?.status === "valid" &&
      after.sets?.[0]?.reps === 12 &&
      curlPr(await stats())?.best_weight_kg === 12 &&
      curlPr(await stats())?.best_reps_at_weight === 12,
    JSON.stringify({
      status: after?.status,
      reps: after?.sets?.[0]?.reps,
      pr: curlPr(await stats()),
    }),
  );
  check(
    "4 补全后身份仍不变：ts-seed-0905、身份数 4",
    after?.training_session_id === "ts-seed-0905" &&
      (await records()).length === 4 &&
      (await cv()) === cv0 + 2,
    JSON.stringify({
      ts: after?.training_session_id,
      n: (await records()).length,
      cv: await cv(),
    }),
  );
}

/* ========== 5. revise 补齐次数路径（对话未述时草稿卡内联改） ========== */
console.log("\n--- revise 补齐 ---");
{
  await reset("default");
  await api("PUT", "/api/provider/api-key", { api_key: "probe" });

  const res = await run(
    "s-f4-04-revise",
    "更正 2026-09-05 的训练记录：哑铃弯举",
    "f4-04-revise",
  );
  const d = res.draft;
  check(
    "5 前置：按日期+动作定位、载入 incomplete 事实",
    d?.payload?.training_session_id === "ts-seed-0905" &&
      d.payload.exercises[0].sets[0].reps == null,
    JSON.stringify(d?.payload?.exercises?.[0]?.sets),
  );

  const payload = JSON.parse(JSON.stringify(d.payload));
  payload.exercises[0].sets[0].reps = 12;
  const rev = await revise(d.id, payload, d.revision);
  check(
    "5 revise 补 reps=12 → 200、revision+1、diff 同步",
    rev.status === 200 &&
      rev.body.draft?.revision === 2 &&
      rev.body.draft.payload.exercises[0].sets[0].reps === 12 &&
      rev.body.draft.diff?.some(
        (x) => /第\s*1\s*组.*次数/.test(x.field ?? "") && x.new_value === "12",
      ),
    JSON.stringify({
      status: rev.status,
      rev: rev.body?.draft?.revision,
      diff: rev.body?.draft?.diff?.filter((x) => /次数/.test(x.field ?? "")),
    }),
  );

  const ok = await confirm(d.id, 2);
  const r0905 = recById(await records(), "ts-seed-0905");
  check(
    "5 确认：valid、PR 自动含 12kg×12、三桶仍 1/1/1",
    ok.status === 200 &&
      r0905?.status === "valid" &&
      r0905.sets?.[0]?.reps === 12 &&
      curlPr(await stats())?.best_weight_kg === 12 &&
      curlPr(await stats())?.best_reps_at_weight === 12 &&
      (await stats()).buckets.met === 1 &&
      (await stats()).buckets.unmet === 1 &&
      (await stats()).buckets.pending === 1,
    JSON.stringify({
      status: r0905?.status,
      pr: curlPr(await stats()),
      buckets: (await stats()).buckets,
    }),
  );
}

await vite.close();
console.log(`\nPASS ${passed} / FAIL ${failed}`);
console.log(failed === 0 ? "全部通过" : `${failed} 项失败`);
process.exit(failed === 0 ? 0 : 1);
