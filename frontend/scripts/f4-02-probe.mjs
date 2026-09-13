/**
 * F4-02 验收探针（走真实 mock REST）：对话定位训练与更正草稿
 * （plans/stage4.md F4-02 / §7 1–3、9）。
 *
 * 运行：node scripts/f4-02-probe.mjs
 * 覆盖：
 *  - 预填更正定位 09-07 卧推 → training_session_id=ts-seed-0907；完整修订 3 组；
 *    未述字段保持（第1组 7 次/75kg；第3组无 reps）；继承 arr-seed-0907
 *  - 生成前后 records 4 身份、cv 不变、stats 不变
 *  - 回复含「更正」「不新增训练身份」；Diff 第2组次数 old=5 new=6
 *  - set_type：载荷全 work；确认后投影 working；revise 写回仍 work
 *  - revise 改 training_session_id / arrangement_revision_id → 拒且不部分接受
 *  - revise 事实 5→6 OK、revision+1、diff 同步；所见 revision 不匹配 → draft_modified
 *  - 确认：cv+1 恰一次、身份数仍 4、kind=correction、旧修订可追溯、重复确认幂等
 *  - 歧义：同日多练先询问不落草稿；回 ts-id 后生成
 *  - 无法定位/模糊 → 无草稿
 *  - voided 终态：作废后再更正 → 无草稿、文案可读、不推 cv
 *  - 三桶：09-07 第2组 5→6 确认后 1/1/1 → 2/0/1；W2 完成率仍 1/3
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
const voidDraft = (trainingSessionId) =>
  api("POST", "/api/dev/drafts/training-void", {
    training_session_id: trainingSessionId,
  });
const cv = async () =>
  (await api("GET", "/api/dev/status")).body?.context_version;
const records = async () => (await api("GET", "/api/records")).body?.records;
const stats = async () => (await api("GET", "/api/stats")).body;
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

/* ========== 1–3. 生成：定位 / 完整修订 / 未述保持 / Diff / 不动正式数据 ========== */
console.log("\n--- 更正草稿生成 ---");
{
  await reset("default");
  await api("PUT", "/api/provider/api-key", { api_key: "probe" });

  const cv0 = await cv();
  const list0 = await records();
  const s0 = await stats();

  const res = await run(
    "s-f4-02-gen",
    "更正 2026-09-07 的训练记录：杠铃平板卧推 第2组改成6次",
    "f4-02-gen",
  );
  const d = res.draft;
  const p = d?.payload;

  check(
    "1 定位：training_session_id=ts-seed-0907",
    d?.kind === "training_record" &&
      d.status === "pending" &&
      p?.training_session_id === "ts-seed-0907" &&
      p?.occurred_on === "2026-09-07",
    JSON.stringify({ kind: d?.kind, ts: p?.training_session_id, on: p?.occurred_on }),
  );
  check(
    "1 完整修订：1 动作 3 组；第1组 7 次/75kg 保持；第3组无 reps",
    p?.exercises?.length === 1 &&
      p.exercises[0].sets?.length === 3 &&
      p.exercises[0].sets[0].reps === 7 &&
      p.exercises[0].sets[0].load?.value_text === "75" &&
      p.exercises[0].sets[0].load?.unit === "kg" &&
      p.exercises[0].sets[1].reps === 6 &&
      p.exercises[0].sets[2].reps == null,
    JSON.stringify(p?.exercises?.[0]?.sets),
  );
  check(
    "2 继承 arrangement_revision_id=arr-seed-0907",
    p?.arrangement_revision_id === "arr-seed-0907",
    p?.arrangement_revision_id,
  );
  check(
    "2 生成前后：records 仍 4 身份、cv 不变、stats 不变",
    list0.length === 4 &&
      (await records()).length === 4 &&
      (await cv()) === cv0 &&
      JSON.stringify((await stats()).buckets) === JSON.stringify(s0.buckets) &&
      JSON.stringify((await stats()).per_week) === JSON.stringify(s0.per_week),
    JSON.stringify({
      before: list0.length,
      after: (await records()).length,
      cv0,
      cv1: await cv(),
    }),
  );
  check(
    "3 回复含「更正」「不新增训练身份 / 同一训练身份」语义",
    /更正/.test(res.text ?? "") &&
      /(不新增训练身份|同一训练身份)/.test(res.text ?? ""),
    (res.text ?? "").slice(0, 200),
  );
  const set2Diff = d?.diff?.find((x) => /第\s*2\s*组.*次数/.test(x.field ?? ""));
  check(
    "3 Diff 第2组次数 old=5 new=6",
    set2Diff?.old_value === "5" && set2Diff?.new_value === "6",
    JSON.stringify(set2Diff),
  );
  check(
    "3 Diff 含「归属训练身份 · 补充/更正既有 ts-seed-0907」",
    d?.diff?.some(
      (x) =>
        x.field === "归属训练身份" &&
        String(x.new_value).includes("ts-seed-0907"),
    ),
    JSON.stringify(d?.diff?.filter((x) => x.field === "归属训练身份")),
  );
}

/* ========== 4–8, 12. revise 护栏 / 事实更正 / draft_modified / 确认 / 三桶 ========== */
console.log("\n--- revise / confirm / 三桶 ---");
{
  await reset("default");
  await api("PUT", "/api/provider/api-key", { api_key: "probe" });

  const cv0 = await cv();
  // 生成时不带具体差异：草稿载入当前事实（第2组仍为 5），供 revise 5→6
  const res = await run(
    "s-f4-02-revise",
    "更正 2026-09-07 的训练记录：杠铃平板卧推",
    "f4-02-revise",
  );
  const d = res.draft;
  const basePayload = d.payload;
  check(
    "前置：更正稿载入第2组 reps=5、arr-seed-0907、全 set_type=work",
    basePayload.training_session_id === "ts-seed-0907" &&
      basePayload.arrangement_revision_id === "arr-seed-0907" &&
      basePayload.exercises[0].sets[1].reps === 5 &&
      basePayload.exercises.every((e) =>
        e.sets.every((s) => s.set_type === "work"),
      ),
    JSON.stringify(basePayload.exercises[0].sets),
  );

  // 5) 身份 / 安排关联不可改写
  const badTs = await revise(
    d.id,
    { ...basePayload, training_session_id: "ts-seed-0831" },
    d.revision,
  );
  check(
    "5 revise 改 training_session_id → 拒",
    badTs.status === 400 &&
      badTs.body.error_code === "invalid_request" &&
      /training_session_id 不可改写/.test(badTs.body.message ?? ""),
    JSON.stringify(badTs.body),
  );
  const badArr = await revise(
    d.id,
    { ...basePayload, arrangement_revision_id: null },
    d.revision,
  );
  check(
    "5 revise 改 arrangement_revision_id → 拒",
    badArr.status === 400 &&
      badArr.body.error_code === "invalid_request" &&
      /arrangement_revision_id 不可改写/.test(badArr.body.message ?? ""),
    JSON.stringify(badArr.body),
  );
  // 不部分接受：拒绝未改写草稿——仍可用原 revision=1 提交合法事实更正
  const editedPayload = JSON.parse(JSON.stringify(basePayload));
  editedPayload.exercises[0].sets[1].reps = 6;
  const okRevise = await revise(d.id, editedPayload, d.revision);
  check(
    "5 拒绝后不部分接受：仍以 revision=1 接受合法事实更正（身份/关联未变）",
    okRevise.status === 200 &&
      okRevise.body.draft?.revision === 2 &&
      okRevise.body.draft.payload.training_session_id === "ts-seed-0907" &&
      okRevise.body.draft.payload.arrangement_revision_id === "arr-seed-0907",
    JSON.stringify({
      status: okRevise.status,
      rev: okRevise.body?.draft?.revision,
      ts: okRevise.body?.draft?.payload?.training_session_id,
      arr: okRevise.body?.draft?.payload?.arrangement_revision_id,
    }),
  );

  // 4 + 6) revise 事实 5→6；写回仍 work
  check(
    "6 revise 第2组 5→6 → 200、revision+1",
    okRevise.status === 200 &&
      okRevise.body.draft?.revision === 2 &&
      okRevise.body.draft.payload.exercises[0].sets[1].reps === 6,
    JSON.stringify({
      status: okRevise.status,
      rev: okRevise.body?.draft?.revision,
      set2: okRevise.body?.draft?.payload?.exercises?.[0]?.sets?.[1]?.reps,
    }),
  );
  check(
    "4/6 写回后：载荷仍全 set_type=work（未渗入 working）",
    okRevise.body.draft.payload.exercises.every((e) =>
      e.sets.every((s) => s.set_type === "work"),
    ),
    JSON.stringify(okRevise.body.draft.payload.exercises[0].sets),
  );
  const set2Diff = okRevise.body.draft?.diff?.find((x) =>
    /第\s*2\s*组.*次数/.test(x.field ?? ""),
  );
  check(
    "6 revise 后 diff 同步 old_value=5 new_value=6",
    set2Diff?.old_value === "5" && set2Diff?.new_value === "6",
    JSON.stringify(set2Diff),
  );

  // 7) 所见 revision 不匹配 → draft_modified
  const staleView = await confirm(d.id, 1);
  check(
    "7 所见 revision=1（当前已是 2）→ 409 draft_modified",
    staleView.status === 409 &&
      staleView.body.error_code === "draft_modified",
    JSON.stringify(staleView.body),
  );

  // 8 + 12) 确认
  const sBefore = await stats();
  check(
    "前置三桶 1/1/1",
    sBefore.buckets.met === 1 &&
      sBefore.buckets.unmet === 1 &&
      sBefore.buckets.pending === 1,
    JSON.stringify(sBefore.buckets),
  );
  const ok = await confirm(d.id, 2);
  check(
    "8 确认成功：cv 恰好 +1、training_session_id=ts-seed-0907",
    ok.status === 200 &&
      ok.body.newly_committed === true &&
      ok.body.training_session_id === "ts-seed-0907" &&
      ok.body.context_version === cv0 + 1,
    JSON.stringify(ok.body),
  );

  const list = await records();
  const r0907 = recById(list, "ts-seed-0907");
  check(
    "8 确认后：身份数仍 4；kind=correction；第2组 6；set_type=working（投影）",
    list.length === 4 &&
      r0907?.kind === "correction" &&
      r0907.sets?.[1]?.reps === 6 &&
      r0907.sets.every((s) => s.set_type === "working"),
    JSON.stringify({
      n: list.length,
      kind: r0907?.kind,
      set2: r0907?.sets?.[1],
    }),
  );
  check(
    "8 旧修订可追溯：revisions 含 valid 历史（reps=5）与当前 correction（reps=6）",
    Array.isArray(r0907?.revisions) &&
      r0907.revisions.length >= 2 &&
      r0907.revisions.some(
        (rev) => rev.status === "valid" && rev.sets?.[1]?.reps === 5,
      ) &&
      r0907.revisions.some(
        (rev) => rev.status === "valid" && rev.sets?.[1]?.reps === 6,
      ),
    JSON.stringify(
      r0907?.revisions?.map((rev) => ({
        id: rev.id,
        set2: rev.sets?.[1]?.reps,
      })),
    ),
  );
  const again = await confirm(d.id, 2);
  check(
    "8 重复确认幂等：newly_committed=false、cv 不再 +1",
    again.status === 200 &&
      again.body.newly_committed === false &&
      again.body.context_version === ok.body.context_version &&
      (await cv()) === ok.body.context_version,
    JSON.stringify({
      newly: again.body.newly_committed,
      cv: again.body.context_version,
    }),
  );

  const s1 = await stats();
  check(
    "12 三桶：第2组 5→6 后 1/1/1 → 2/0/1（只看次数）",
    s1.buckets.met === 2 && s1.buckets.unmet === 0 && s1.buckets.pending === 1,
    JSON.stringify(s1.buckets),
  );
  const w2a = w2(s1);
  check(
    "12 完成率 W2 仍 1/3（同一次仍有效且关联同一日程）",
    w2a?.planned === 3 && w2a?.completed === 1,
    JSON.stringify(w2a),
  );
}

/* ========== 9–10. 歧义 / 无法定位 ========== */
console.log("\n--- 歧义与无法定位 ---");
{
  await reset("default");
  await api("PUT", "/api/provider/api-key", { api_key: "probe" });

  // 同日第二条卧推（新身份）
  const extra = await run(
    "s-f4-02-amb-extra",
    "2026-09-07 新增一练 卧推 70kg 3组 每组8次",
    "f4-02-amb-extra",
  );
  check(
    "9 前置：同日新增卧推草稿可生成",
    extra.draft?.kind === "training_record" &&
      extra.draft.payload?.occurred_on === "2026-09-07",
    JSON.stringify({ kind: extra.draft?.kind, on: extra.draft?.payload?.occurred_on }),
  );
  const extraOk = await confirm(extra.draft.id, extra.draft.revision);
  check(
    "9 前置：确认后 records=5（同日双卧推身份）",
    extraOk.status === 200 && (await records()).length === 5,
    JSON.stringify({ status: extraOk.status, n: (await records()).length }),
  );

  const amb = await run(
    "s-f4-02-amb",
    "更正 2026-09-07 的训练记录：杠铃平板卧推",
    "f4-02-amb",
  );
  check(
    "9 候选不唯一：询问且不落草稿",
    amb.draft == null &&
      /候选不唯一|要更正哪一次/.test(amb.text ?? "") &&
      /不落草稿|不生成/.test(amb.text ?? ""),
    (amb.text ?? "").slice(0, 240),
  );

  const byId = await run(
    "s-f4-02-byid",
    "更正 ts-seed-0907 的训练记录：杠铃平板卧推 第2组改成6次",
    "f4-02-byid",
  );
  check(
    "9 回 ts-seed-0907 后生成更正稿",
    byId.draft?.payload?.training_session_id === "ts-seed-0907" &&
      byId.draft.payload.exercises[0].sets[1].reps === 6,
    JSON.stringify({
      ts: byId.draft?.payload?.training_session_id,
      set2: byId.draft?.payload?.exercises?.[0]?.sets?.[1]?.reps,
    }),
  );

  const fuzzy = await run(
    "s-f4-02-fuzzy",
    "更正上周那次训练 数据有误",
    "f4-02-fuzzy",
  );
  check(
    "10 无日期/动作：回澄清且不生成草稿",
    fuzzy.draft == null &&
      /日期|定位/.test(fuzzy.text ?? "") &&
      /不生成草稿/.test(fuzzy.text ?? ""),
    (fuzzy.text ?? "").slice(0, 200),
  );

  const miss = await run(
    "s-f4-02-miss",
    "更正 2026-01-01 的训练记录：杠铃平板卧推 数据有误",
    "f4-02-miss",
  );
  check(
    "10 日期无记录：不生成草稿",
    miss.draft == null && /未能|不生成更正草稿/.test(miss.text ?? ""),
    (miss.text ?? "").slice(0, 200),
  );
}

/* ========== 11. voided 终态（生成 fail-closed） ========== */
console.log("\n--- voided 终态 ---");
{
  await reset("default");
  await api("PUT", "/api/provider/api-key", { api_key: "probe" });

  const voidRes = await voidDraft("ts-seed-0907");
  const voidOk = await confirm(voidRes.body.draft.id, voidRes.body.draft.revision);
  check(
    "11 前置：作废 09-07 成功",
    voidOk.status === 200 &&
      recById(await records(), "ts-seed-0907")?.status === "voided",
    JSON.stringify({ status: voidOk.status }),
  );
  const cvAfterVoid = await cv();

  const term = await run(
    "s-f4-02-voided",
    "更正 2026-09-07 的训练记录：杠铃平板卧推 第2组改成6次",
    "f4-02-voided",
  );
  check(
    "11 voided 再更正：无草稿、文案可读（终态）、不推 cv",
    term.draft == null &&
      /已作废|终态/.test(term.text ?? "") &&
      /不生成草稿|不接受/.test(term.text ?? "") &&
      (await cv()) === cvAfterVoid &&
      recById(await records(), "ts-seed-0907")?.status === "voided",
    JSON.stringify({
      draft: term.draft,
      cv: await cv(),
      text: (term.text ?? "").slice(0, 200),
    }),
  );

  const byIdTerm = await run(
    "s-f4-02-voided-id",
    "更正 ts-seed-0907 的训练记录：杠铃平板卧推",
    "f4-02-voided-id",
  );
  check(
    "11 按 id 指向 voided 身份：同样无草稿、不推 cv",
    byIdTerm.draft == null &&
      /已作废|终态/.test(byIdTerm.text ?? "") &&
      (await cv()) === cvAfterVoid,
    (byIdTerm.text ?? "").slice(0, 200),
  );
}

await vite.close();
console.log(`\nPASS ${passed} / FAIL ${failed}`);
console.log(failed === 0 ? "全部通过" : `${failed} 项失败`);
process.exit(failed === 0 ? 0 : 1);
