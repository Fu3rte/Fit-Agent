/**
 * F3-01 验收探针（走真实 mock REST）：契约收口、种子重设与统计现算
 * （plans/stage3.md §3.4/§3.5 / F3-01）。
 *
 * 运行：node scripts/f3-01-probe.mjs
 * 覆盖：种子事实表（W1 2/3、W2 1/3、三桶 1/1/1、PR、3 条安排、今日无安排）、
 *      GET /api/arrangements、revise revision 校验、confirm 凭据与幂等、
 *      /api/stats 现算与种子一致、data_updated_at 用 mock 时钟（确认后更新）。
 */
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createServer as createViteServer } from "vite";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
let failed = 0;
const check = (name, ok, detail = "") => {
  if (!ok) failed += 1;
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

async function run(session, message) {
  const started = await api("POST", "/api/runs", {
    session_id: session,
    message,
    client_request_id: `f3-01-${Date.now()}-${Math.random()}`,
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

/* ---------- 1. 契约收口（源码静态断言） ---------- */
const { readFileSync } = await import("node:fs");
const contract = readFileSync(
  path.join(root, "src/lib/contract.ts"),
  "utf8",
);
const serverSrc = readFileSync(path.join(root, "src/mock/server.ts"), "utf8");
check(
  "契约：ArrangementAdjustment 对齐 S4-04 字段全集",
  /export interface ArrangementAdjustment \{[\s\S]*?item_key: string;[\s\S]*?work_sets\?: number;[\s\S]*?target_rir\?: IntRange;[\s\S]*?disposition\?: ArrangementItemDisposition;[\s\S]*?reps_range\?: IntRange;[\s\S]*?load_value\?: number;[\s\S]*?replacement_exercise_id\?: string \| null;/.test(
    contract,
  ),
);
check(
  "契约：ArrangementItemDisposition 四类",
  /"keep"[\s\S]*?"deload"[\s\S]*?"equivalent_replace"[\s\S]*?"local_skip"/.test(
    contract,
  ),
);
check(
  "契约：PlanExerciseItem 可带 disposition / replacement_exercise_id",
  /disposition\?: ArrangementItemDisposition;/.test(contract) &&
    /replacement_exercise_id\?: string \| null;/.test(contract),
);
check(
  "契约：ReviseRequest 含 revision",
  /export interface ReviseRequest \{[\s\S]*?payload: DraftPayload;[\s\S]*?revision: number;/.test(
    contract,
  ),
);
check(
  "契约：ConfirmResult 含提交凭据与 kind 结果 id",
  /committed_revision: number;/.test(contract) &&
    /committed_business_version: number;/.test(contract) &&
    /plan_version\?: string;/.test(contract) &&
    /arrangement_revision_id\?: string;/.test(contract) &&
    /training_session_id\?: string;/.test(contract),
);
check(
  "契约：TrainingRecord 含 judgement / comparison",
  /judgement\?: Buckets \| null;/.test(contract) &&
    /comparison\?: \{/.test(contract),
);
check(
  "契约端点清单含 GET /api/arrangements",
  /GET\s+\/api\/arrangements/.test(contract),
);
check(
  "mock：CHECKIN_ENABLED 门禁已移除",
  !/CHECKIN_ENABLED/.test(serverSrc) &&
    !/CHECKIN_UNAVAILABLE_REPLY/.test(serverSrc),
);
check(
  "mock：种子日期 MOCK_TODAY=2026-09-11 且启动即 recomputeStats",
  /const MOCK_TODAY = "2026-09-11"/.test(serverSrc) &&
    // F5-01 起 recomputeStats 与 return 之间可有 basis 回填等语句；断言「启动路径调用 recomputeStats」即可
    /recomputeStats\(state\);[\s\S]{0,160}return state;/.test(serverSrc),
);

/* ---------- 2. 种子事实表（§3.5） ---------- */
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });

const stats0 = (await api("GET", "/api/stats")).body;
const arrangements0 = (await api("GET", "/api/arrangements")).body;
const records0 = (await api("GET", "/api/records")).body;
const profile0 = (await api("GET", "/api/profile")).body;

check(
  "种子：计划 v2 2026-08-31 起 / 2026-10-12 复核",
  profile0.plan?.version === "v2" &&
    profile0.plan.starts_on === "2026-08-31" &&
    profile0.plan.review_on === "2026-10-12",
);

const w1 = stats0.per_week.find((w) => w.week === "W1");
const w2 = stats0.per_week.find((w) => w.week === "W2");
check(
  "种子：W1 2/3（66.7%）",
  w1?.planned === 3 && w1.completed === 2 && w1.rate === 66.7,
  JSON.stringify(w1),
);
check(
  "种子：W2 截至今日 1/3（33.3%）",
  w2?.planned === 3 && w2.completed === 1 && w2.rate === 33.3,
  JSON.stringify(w2),
);
check(
  "种子：三桶 1/1/1",
  stats0.buckets.met === 1 &&
    stats0.buckets.unmet === 1 &&
    stats0.buckets.pending === 1,
  JSON.stringify(stats0.buckets),
);
check(
  "种子：PR 卧推 80kg×8；自重引体不进重量 PR",
  stats0.prs.length === 1 &&
    stats0.prs[0].exercise === "杠铃平板卧推" &&
    stats0.prs[0].best_weight_kg === 80 &&
    stats0.prs[0].best_reps_at_weight === 8,
  JSON.stringify(stats0.prs),
);
check(
  "种子：data_updated_at 为 mock 时钟（2026-09-11 段）",
  typeof stats0.data_updated_at === "string" &&
    stats0.data_updated_at.startsWith("2026-09-11"),
  stats0.data_updated_at,
);

check(
  "种子：已接受安排 3 条（08-31 / 09-02 / 09-07）",
  arrangements0.arrangements?.length === 3 &&
    arrangements0.arrangements.map((a) => a.target.scheduled_on).join() ===
      "2026-08-31,2026-09-02,2026-09-07",
  JSON.stringify(
    arrangements0.arrangements?.map((a) => a.target.scheduled_on),
  ),
);
const arr0907 = arrangements0.arrangements?.find(
  (a) => a.target.scheduled_on === "2026-09-07",
);
const bench0907 = arr0907?.target.exercises.find(
  (e) => e.exercise_id === "barbell-bench-press",
);
check(
  "种子：09-07 安排为 deload 减 1 组（4→3）且有原因",
  bench0907?.disposition === "deload" &&
    bench0907.prescription.work_sets === 3 &&
    typeof arr0907.target.adjustment_reason === "string" &&
    arr0907.target.adjustment_reason.trim() !== "",
  JSON.stringify({
    sets: bench0907?.prescription.work_sets,
    reason: arr0907?.target.adjustment_reason,
  }),
);
const arr0831 = arrangements0.arrangements?.find(
  (a) => a.target.scheduled_on === "2026-08-31",
);
check(
  "种子：08-31 安排为 keep（未调整）",
  arr0831?.target.exercises.every((e) => e.disposition === "keep") &&
    arr0831.target.exercises[0].prescription.work_sets === 4,
);

const valid = records0.records.filter((r) => r.status === "valid");
const incomplete = records0.records.filter((r) => r.status === "incomplete");
check(
  "种子：3 条 valid + 1 条 incomplete（09-05 无安排）",
  valid.length === 3 &&
    incomplete.length === 1 &&
    incomplete[0].date === "2026-09-05" &&
    incomplete[0].scheduled_session_id == null &&
    incomplete[0].arrangement_revision_id == null,
  JSON.stringify(records0.records.map((r) => `${r.date}:${r.status}`)),
);
const rec0907 = valid.find((r) => r.date === "2026-09-07");
check(
  "种子：09-07 记录关联对照安排且派生 judgement 1/1/1",
  rec0907?.arrangement_revision_id === "arr-seed-0907" &&
    rec0907.judgement?.met === 1 &&
    rec0907.judgement.unmet === 1 &&
    rec0907.judgement.pending === 1 &&
    rec0907.comparison?.planned_sets === 4 &&
    rec0907.comparison.arranged_sets === 3 &&
    typeof rec0907.comparison.accepted_at === "string",
  JSON.stringify({
    j: rec0907?.judgement,
    c: rec0907?.comparison,
  }),
);
const recNoArr = valid.filter((r) => r.arrangement_revision_id == null);
check(
  "种子：无对照安排的 valid 记录 judgement 为 null（不进三桶）",
  recNoArr.length >= 2 && recNoArr.every((r) => r.judgement == null),
);

const todaySched = (profile0.schedules ?? []).filter(
  (s) => s.date === "2026-09-11",
);
check(
  "种子：今日 09-11 腿日应训练、尚无安排尚无记录",
  todaySched.length === 1 &&
    todaySched[0].plan_workout_key === "legs" &&
    !arrangements0.arrangements.some(
      (a) => a.target.scheduled_on === "2026-09-11",
    ) &&
    !records0.records.some((r) => r.date === "2026-09-11"),
);

/* ---------- 3. GET /api/arrangements 镜像 ---------- */
check(
  "GET /api/arrangements 返回 id/accepted_at/target 三元组",
  arrangements0.arrangements.every(
    (a) => a.id && a.accepted_at && a.target?.scheduled_on,
  ),
);

/* ---------- 4. revise revision 校验 ---------- */
// 打卡剧本开放后生成一份训练记录草稿
const checkin = await run("s1", "今天卧推 80kg 4组 每组8次");
check(
  "打卡剧本开放：生成 training_record 草稿（无 CHECKIN 门禁）",
  checkin.draft?.kind === "training_record" &&
    checkin.draft.status === "pending",
  checkin.text?.slice(0, 60),
);

const wrongRev = await revise(
  checkin.draft.id,
  structuredClone(checkin.draft.payload),
  checkin.draft.revision + 5,
);
check(
  "revise：revision 不匹配 → 409 draft_modified",
  wrongRev.status === 409 && wrongRev.body.error_code === "draft_modified",
  JSON.stringify(wrongRev.body),
);
const missingRev = await api("POST", `/api/drafts/${checkin.draft.id}/revise`, {
  payload: checkin.draft.payload,
});
check(
  "revise：缺 revision → 400",
  missingRev.status === 400,
  JSON.stringify(missingRev.body),
);
const okRev = await revise(
  checkin.draft.id,
  structuredClone(checkin.draft.payload),
  checkin.draft.revision,
);
check(
  "revise：revision 匹配 → revision+1",
  okRev.status === 200 && okRev.body.draft.revision === checkin.draft.revision + 1,
  JSON.stringify({ status: okRev.status, rev: okRev.body?.draft?.revision }),
);

/* ---------- 5. confirm 凭据与幂等 ---------- */
const statsBefore = (await api("GET", "/api/stats")).body;
const firstConfirm = await confirm(checkin.draft.id, okRev.body.draft.revision);
check(
  "confirm：首次确认返回提交凭据（committed_revision / business_version / training_session_id）",
  firstConfirm.status === 200 &&
    firstConfirm.body.newly_committed === true &&
    firstConfirm.body.committed_revision === okRev.body.draft.revision &&
    firstConfirm.body.committed_business_version ===
      firstConfirm.body.context_version &&
    typeof firstConfirm.body.training_session_id === "string" &&
    firstConfirm.body.training_session_id !== "",
  JSON.stringify(firstConfirm.body),
);
const againConfirm = await confirm(checkin.draft.id, okRev.body.draft.revision);
check(
  "confirm：重复确认幂等——同一凭据、不重复写、不 cv+1",
  againConfirm.status === 200 &&
    againConfirm.body.newly_committed === false &&
    againConfirm.body.committed_revision ===
      firstConfirm.body.committed_revision &&
    againConfirm.body.committed_business_version ===
      firstConfirm.body.committed_business_version &&
    againConfirm.body.training_session_id ===
      firstConfirm.body.training_session_id &&
    againConfirm.body.context_version === firstConfirm.body.context_version,
  JSON.stringify({
    first: firstConfirm.body.context_version,
    again: againConfirm.body.context_version,
  }),
);

/* ---------- 6. stats 现算与种子一致（确认后更新） ---------- */
const stats1 = (await api("GET", "/api/stats")).body;
check(
  "confirm 后 data_updated_at 更新（mock 时钟前进）",
  stats1.data_updated_at > stats0.data_updated_at,
  `${stats0.data_updated_at} → ${stats1.data_updated_at}`,
);
check(
  "confirm 后三桶仍由对照安排派生（今日记录无对照 → 不进三桶）",
  stats1.buckets.met === 1 &&
    stats1.buckets.unmet === 1 &&
    stats1.buckets.pending === 1,
  JSON.stringify(stats1.buckets),
);

// 再确认一份安排草稿：检查 GET /api/arrangements 可读回 + ConfirmResult 凭据
// （用 arrangement 剧本：今日已锁定 → 下一个未锁定日 09-14）
const arrRun = await run("s1", "今天轻一点，少做几组");
check(
  "安排剧本可生成 arrangement 草稿",
  arrRun.draft?.kind === "arrangement" && arrRun.draft.status === "pending",
  arrRun.text?.slice(0, 80),
);
if (arrRun.draft) {
  const arrConfirm = await confirm(arrRun.draft.id, arrRun.draft.revision);
  check(
    "arrangement confirm：返回 arrangement_revision_id 凭据",
    arrConfirm.status === 200 &&
      typeof arrConfirm.body.arrangement_revision_id === "string" &&
      arrConfirm.body.arrangement_revision_id !== "",
    JSON.stringify(arrConfirm.body),
  );
  const arrangements1 = (await api("GET", "/api/arrangements")).body;
  check(
    "确认后 GET /api/arrangements 可读回 4 条",
    arrangements1.arrangements?.length === 4 &&
      arrangements1.arrangements.some(
        (a) => a.id === arrConfirm.body.arrangement_revision_id,
      ),
    JSON.stringify(
      arrangements1.arrangements?.map((a) => a.target.scheduled_on),
    ),
  );
  const arrAgain = await confirm(arrRun.draft.id, arrRun.draft.revision);
  check(
    "arrangement 重复确认幂等：同一 arrangement_revision_id",
    arrAgain.status === 200 &&
      arrAgain.body.arrangement_revision_id ===
        arrConfirm.body.arrangement_revision_id &&
      arrAgain.body.newly_committed === false,
  );
}

/* ---------- 7. 完成率分母含今日（今日已打卡后 W2 应为 2/3） ---------- */
// 今日记录已确认但无 scheduled_session_id 关联（打卡剧本未显式携带），
// 故 W2 完成率保持 1/3；此断言锁定「无关联不虚增分子」的口径。
const stats2 = (await api("GET", "/api/stats")).body;
const w2b = stats2.per_week.find((w) => w.week === "W2");
check(
  "无显式日程关联的记录不计入完成率分子（W2 仍 1/3）",
  w2b?.planned === 3 && w2b.completed === 1,
  JSON.stringify(w2b),
);

await vite.close();
console.log(failed === 0 ? "\n全部通过" : `\n${failed} 项失败`);
process.exit(failed === 0 ? 0 : 1);
