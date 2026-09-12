/**
 * F2-05 验收探针（走真实 mock REST）：当前计划投影与使用时安全阻断
 * （plans/stage2.md 第 7 节第 6、10、11 步中不依赖浏览器渲染的部分）。
 *
 * 运行：node scripts/f2-05-plan-safety-probe.mjs
 * 覆盖：空态→启用→替换的 /api/profile 投影一致与刷新一致；确认前隔离与重复确认幂等；
 *      请求指导时按最新限制整份阻断；红旗独立阻断；阻断时计划与日程仍可查看。
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
const ofProfile = async () => (await api("GET", "/api/profile")).body;
const confirm = (id, revision) =>
  api("POST", `/api/drafts/${id}/confirm`, { revision });

async function run(session, message) {
  const started = await api("POST", "/api/runs", {
    session_id: session,
    message,
    client_request_id: `f2-05-${Date.now()}-${Math.random()}`,
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

const byDate = (schedules) =>
  schedules.map((s) => `${s.date}:${s.stored_status}`).sort();
const versionSchedules = (profile, version) =>
  (profile.schedules ?? []).filter((s) => s.plan_version === version);

async function enablePlanV1(session) {
  const plan = await run(session, "帮我生成一份计划");
  const ok = await confirm(plan.draft.id, plan.draft.revision);
  return { planDraft: plan, confirm: ok };
}

async function addProfileFacts(extraClause) {
  const r = await run(
    "s1",
    `补充档案：目标增肌，初级经验，每周 3 次，每次 60 分钟，可用器材：杠铃、哑铃、卧推架、引体架、绳索，体重 72.5kg，${extraClause}`,
  );
  if (!r.draft) throw new Error(`未生成档案草稿：${r.text}`);
  const ok = await confirm(r.draft.id, r.draft.revision);
  return { ...r, confirm: ok };
}

/* ---------- 1. 空态：无计划、无日程、无安全复核 ---------- */
await reset("noplan");
const empty = await ofProfile();
check(
  "空态：已建档但无 plan / schedules / plan_safety",
  empty.profile !== null &&
    empty.plan === undefined &&
    empty.schedules === undefined &&
    empty.plan_safety === undefined,
  `context_version=${empty.context_version}`,
);

/* ---------- 2. 步骤 6：确认前隔离、首次启用、刷新一致、幂等 ---------- */
const first = await run("s1", "帮我生成一份计划");
check("生成计划草稿（kind=plan）", first.draft?.kind === "plan");
const beforeConfirm = await ofProfile();
check(
  "确认前正式计划与日程不变（/profile 仍无计划）",
  beforeConfirm.plan === undefined &&
    beforeConfirm.schedules === undefined &&
    beforeConfirm.context_version === empty.context_version,
  `plan=${beforeConfirm.plan?.version} v=${beforeConfirm.context_version}`,
);
const firstConfirm = await confirm(first.draft.id, first.draft.revision);
check(
  "确认成功并递增 context_version",
  firstConfirm.status === 200 &&
    firstConfirm.body.newly_committed === true &&
    firstConfirm.body.context_version === empty.context_version + 1,
  JSON.stringify(firstConfirm.body),
);

const active = await ofProfile();
const activeSchedules = versionSchedules(active, "v1");
check(
  "启用后 /profile 给出计划版本／状态／日期（starts_on/review_on）",
  active.plan?.version === "v1" &&
    active.plan.status === "active" &&
    active.plan.starts_on === "2026-09-14" &&
    active.plan.review_on === "2026-10-12",
  JSON.stringify({
    v: active.plan?.version,
    s: active.plan?.status,
    d: [active.plan?.starts_on, active.plan?.review_on],
  }),
);
const workouts = active.plan?.payload?.plan_workouts ?? [];
check(
  "处方含身份／组次／区间／校准（无起始重量）；D9 load 互斥",
  workouts.length === 3 &&
    workouts.every((w) =>
      w.exercises.every((e) => {
        const rx = e.prescription;
        const sets = rx.work_sets > 0;
        const rangeOk =
          rx.kind === "reps"
            ? rx.reps_range && rx.reps_range.min <= rx.reps_range.max
            : rx.duration_seconds_range;
        const loadOk =
          e.record_type !== "external_load_reps" ||
          (e.load?.kind === "needs_calibration" &&
            e.load.steps.length > 0 &&
            !/RIR/.test(e.load.pass_criteria));
        return e.exercise_id && sets && rangeOk && loadOk;
      }),
    ) &&
    !/\d+\s*(?:kg|公斤)/.test(JSON.stringify(active.plan)),
);
check(
  "具体日程 12 条应为应训练日且 stored_status 全为 scheduled",
  activeSchedules.length === 12 &&
    activeSchedules.every(
      (s) => s.stored_status === "scheduled" && s.locked_effective === false,
    ),
  `${activeSchedules.length} 条`,
);
check(
  "可用态安全复核：usable 且无冲突、无红旗、block_code 缺省",
  active.plan_safety?.usable === true &&
    active.plan_safety.red_flag_blocked === false &&
    active.plan_safety.conflicts.length === 0 &&
    active.plan_safety.block_code === undefined &&
    active.plan_safety.context_version === active.context_version,
);

const activeAgain = await ofProfile();
check(
  "刷新一致：再次查询计划／日程完全相同",
  JSON.stringify(activeAgain.plan) === JSON.stringify(active.plan) &&
    JSON.stringify(byDate(activeAgain.schedules)) ===
      JSON.stringify(byDate(active.schedules)),
);
const repeat = await confirm(first.draft.id, first.draft.revision);
const afterRepeat = await ofProfile();
check(
  "重复确认幂等：不重复建版本／日程，版本号不递增",
  repeat.status === 200 &&
    repeat.body.newly_committed === false &&
    afterRepeat.context_version === active.context_version &&
    afterRepeat.plan.version === "v1" &&
    afterRepeat.schedules.length === active.schedules.length,
);

const guidanceOk = await run("s1", "给我周三的训练指导");
check(
  "可用态指导给出下个应训练日处方且不生成草稿",
  guidanceOk.draft === null &&
    /下一个应训练日：2026-09-14/.test(guidanceOk.text) &&
    /需要校准|不给出具体起始重量/.test(guidanceOk.text),
);

/* ---------- 3. 步骤 10：具体动作限制 → 整份阻断 ---------- */
const specific = await addProfileFacts("最近卧推不适，没有其他不适。");
check(
  "经建档链路确认新增具体动作限制",
  specific.confirm.status === 200 &&
    /杠铃平板卧推/.test(JSON.stringify(specific.draft.payload)),
);
const blockedBySpecific = await ofProfile();
check(
  "新增限制后整份不可用且指出冲突动作",
  blockedBySpecific.plan_safety?.usable === false &&
    blockedBySpecific.plan_safety.conflicts.length === 1 &&
    blockedBySpecific.plan_safety.conflicts[0].exercise_name ===
      "杠铃平板卧推" &&
    blockedBySpecific.plan_safety.conflicts[0].restriction.name ===
      "杠铃平板卧推",
  JSON.stringify(blockedBySpecific.plan_safety?.conflicts),
);
check(
  "阻断时正式计划与全部日程仍由接口投影返回（不隐藏、不伪造「部分可用」）",
  blockedBySpecific.plan?.version === "v1" &&
    blockedBySpecific.schedules.length === 12 &&
    blockedBySpecific.plan_safety.usable === false &&
    !("partial" in blockedBySpecific.plan_safety) &&
    blockedBySpecific.plan.status === "active",
);

const dayA = await run("s1", "给我周三的训练指导");
const dayB = await run("s1", "今天练什么");
check(
  "任一训练日说法都整份阻断且不输出任何处方",
  dayA.draft === null &&
    dayB.draft === null &&
    /整份计划指导已阻断/.test(dayA.text) &&
    /整份计划指导已阻断/.test(dayB.text) &&
    /杠铃平板卧推/.test(dayA.text) &&
    !/自重引体向上|绳索下压|杠铃背蹲/.test(dayA.text) &&
    !/一组.*x /.test(dayA.text),
);
check(
  "阻断文案引导从对话发起修订草稿",
  /修改计划只能从对话发起/.test(dayA.text) && dayA.draft === null,
);
check(
  "阻断文案只声称当前计划与当前日程可查看",
  /正式计划 v1 与它的当前日程仍可在「档案与限制」页查看/.test(dayA.text) &&
    !/历史/.test(dayA.text) &&
    dayA.draft === null,
);

/* ---------- 4. 步骤 10：动作模式限制 → 整份阻断 ---------- */
await reset("noplan");
await enablePlanV1("s1");
const pattern = await addProfileFacts("深蹲时膝部不适，没有其他不适。");
const blockedByPattern = await ofProfile();
const names = (blockedByPattern.plan_safety?.conflicts ?? []).map(
  (c) => c.exercise_name,
);
check(
  "动作模式限制命中多个动作时整份不可用并逐条列出冲突",
  pattern.confirm.status === 200 &&
    blockedByPattern.plan_safety?.usable === false &&
    blockedByPattern.plan_safety.conflicts.every(
      (c) => c.restriction.scope === "movement_pattern",
    ) &&
    names.includes("杠铃背蹲") &&
    names.includes("保加利亚分腿蹲"),
  names.join("、"),
);
const patternGuidance = await run("s1", "给我周五的训练指导");
check(
  "模式冲突下指导同样整份阻断、不输出未冲突动作处方",
  patternGuidance.draft === null &&
    /整份计划指导已阻断/.test(patternGuidance.text) &&
    !/杠铃平板卧推|绳索下压/.test(patternGuidance.text),
);

/* ---------- 5. 步骤 11：红旗独立阻断 ---------- */
await reset("noplan");
await enablePlanV1("s1");
const redFlag = await addProfileFacts(
  "最近胸部异常不适，没有动作限制，没有其他不适。",
);
const blockedByFlag = await ofProfile();
check(
  "身体情况命中安全症状即写入档案原文且复核为独立阻断（无限制冲突）",
  redFlag.confirm.status === 200 &&
    blockedByFlag.profile.body_conditions.length > 0 &&
    blockedByFlag.plan_safety?.red_flag_blocked === true &&
    blockedByFlag.plan_safety.usable === false &&
    blockedByFlag.plan_safety.conflicts.length === 0,
  JSON.stringify(blockedByFlag.profile.body_conditions),
);
const flagGuidance = await run("s1", "给我周三的训练指导");
check(
  "红旗指导：仅建议线下专业评估、无处方、无草稿",
  flagGuidance.draft === null &&
    /线下就医|专业评估/.test(flagGuidance.text) &&
    !/组 x /.test(flagGuidance.text),
);
const flagPlan = await run("s1", "帮我生成一份计划");
check(
  "红旗请求计划：不给计划草稿，正式计划保持不变",
  flagPlan.draft === null &&
    /未生成草稿/.test(flagPlan.text) &&
    (await ofProfile()).plan.version === "v1",
);

await reset("noplan");
await addProfileFacts("最近胸部异常不适，没有动作限制，没有其他不适。");
const flagSeedProfile = await ofProfile();
const flagSeedPlan = await run("s1", "帮我生成一份计划");
const flagSeedGuidance = await run("s1", "今天练什么");
check(
  "红旗种子（无计划）：请求计划与指导都不给草稿、不给处方",
  flagSeedProfile.plan === undefined &&
    flagSeedProfile.plan_safety === undefined &&
    flagSeedPlan.draft === null &&
    /不会生成任何计划处方/.test(flagSeedPlan.text) &&
    flagSeedGuidance.draft === null &&
    /线下就医|专业评估/.test(flagSeedGuidance.text) &&
    !/组 x /.test(flagSeedGuidance.text),
);

/* ---------- 6. 替换态：投影一致；旧版取消保留在接口 ---------- */
await reset("noplan");
await enablePlanV1("s1");
const dumbbell = await run("s1", "以后只能用哑铃");
const dumbbellConfirm = await confirm(
  dumbbell.draft.id,
  dumbbell.draft.revision,
);
const replaced = await ofProfile();
const v1After = versionSchedules(replaced, "v1");
const v2After = versionSchedules(replaced, "v2");
check(
  "替换确认后 /profile 给出 v2 与新版日程",
  dumbbellConfirm.status === 200 &&
    replaced.plan?.version === "v2" &&
    replaced.plan.status === "active" &&
    v2After.length === 12 &&
    v2After.every((s) => s.stored_status === "scheduled"),
  `${replaced.plan?.version} / v2 ${v2After.length} 条`,
);
check(
  "旧版日程取消仍保留在 /api/profile 投影中（产品 UI 呈现层不展示）",
  v1After.length === 12 &&
    v1After.every((s) => s.stored_status === "cancelled"),
  byDate(v1After).join(","),
);
check(
  "替换后安全复核按新计划重算",
  replaced.plan_safety?.usable === true &&
    replaced.plan_safety.conflicts.length === 0 &&
    replaced.plan_safety.context_version === replaced.context_version,
);
const replacedAgain = await ofProfile();
check(
  "替换态刷新一致",
  JSON.stringify(replacedAgain.plan) === JSON.stringify(replaced.plan) &&
    JSON.stringify(byDate(replacedAgain.schedules)) ===
      JSON.stringify(byDate(replaced.schedules)),
);

/* ---------- 7. 步骤 11：把冲突动作改入草稿被服务端拒绝 ---------- */
await reset("noplan");
await enablePlanV1("s1");
await addProfileFacts("最近卧推不适，没有其他不适。");
const adjust = await run("s1", "帮我调整计划");
const beforeRejected = await ofProfile();
const tampered = structuredClone(adjust.draft.payload);
tampered.plan.payload.plan_workouts[0].exercises[0].exercise_id =
  "barbell-bench-press";
const rejected = await api("POST", `/api/drafts/${adjust.draft.id}/revise`, {
  payload: tampered,
  revision: adjust.draft.revision,
});
const afterRejected = await ofProfile();
check(
  "把受限动作改入草稿被服务端拒绝",
  rejected.status === 400 &&
    /计划载荷无效/.test(rejected.body.message) &&
    /杠铃平板卧推|限制/.test(rejected.body.message),
  JSON.stringify(rejected.body),
);
check(
  "被拒纠错不改正式数据（计划／日程／版本不变）",
  JSON.stringify(afterRejected.plan) === JSON.stringify(beforeRejected.plan) &&
    JSON.stringify(byDate(afterRejected.schedules)) ===
      JSON.stringify(byDate(beforeRejected.schedules)) &&
    afterRejected.context_version === beforeRejected.context_version,
);

await vite.close();
console.log(failed === 0 ? "\n全部通过" : `\n${failed} 项失败`);
process.exit(failed === 0 ? 0 : 1);
