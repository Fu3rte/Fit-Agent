/**
 * F2-04 验收探针（走真实 mock REST）：D9 计划启用、替换与原子日程切换
 * （plans/stage2.md 第 7 节第 5—9 步中不依赖浏览器渲染的部分）。
 *
 * 运行：node scripts/f2-04-plan-commit-probe.mjs
 * 方式：以 vite 程序化启动本仓配置（mock 挂在 /api/*，无真实后端／模型调用）。
 * 覆盖：确认前隔离、首次启用 v1 与 12 个日程、幂等重复确认、长期器械组合草稿
 *      （档案补丁 + 新版本 + 新日程 + 旧版未来未锁定取消）、已锁定日程不动、
 *      故障注入整份回滚、draft_modified／draft_stale→重算、丢弃后拒确认。
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
const devStatus = async () => (await api("GET", "/api/dev/status")).body;
const profileProjection = async () => (await api("GET", "/api/profile")).body;
const confirm = (id, revision) =>
  api("POST", `/api/drafts/${id}/confirm`, { revision });
const revise = (id, payload, revision) =>
  api("POST", `/api/drafts/${id}/revise`, { payload, revision });
const recalc = (id) => api("POST", `/api/drafts/${id}/recalc`, {});
const discard = (id) => api("POST", `/api/drafts/${id}/discard`, {});

async function startRun(session, message) {
  const started = await api("POST", "/api/runs", {
    session_id: session,
    message,
    client_request_id: `f2-04-${Date.now()}-${Math.random()}`,
  });
  if (started.status !== 200)
    throw new Error(
      `发起 Run 失败：${started.status} ${JSON.stringify(started.body)}`,
    );
  return started.body.run_id;
}

async function waitRun(runId) {
  for (let i = 0; i < 120; i += 1) {
    await new Promise((r) => setTimeout(r, 300));
    const active = (await api("GET", "/api/runs/active")).body?.run;
    if (!active || active.run_id !== runId) continue;
    if (["completed", "failed", "cancelled"].includes(active.status))
      return {
        run: active,
        draft: active.drafts?.[active.drafts.length - 1] ?? null,
      };
  }
  throw new Error("Run 未在预期时间内进入终态");
}

async function run(session, message) {
  return waitRun(await startRun(session, message));
}

const scheduleCount = (status, storedStatus, planVersion) =>
  status.schedules.filter(
    (s) =>
      s.stored_status === storedStatus &&
      (planVersion === undefined || s.plan_version === planVersion),
  ).length;
const draftStatus = (status, id) =>
  status.drafts.find((d) => d.id === id)?.status;

/* ---------- 步骤 5—6：确认前隔离、纠错、首次启用 v1 与幂等 ---------- */
await reset("noplan");
let status = await devStatus();
check(
  "步骤 5 前置（noplan 种子）：无正式计划与日程",
  status.plan_version === null &&
    status.schedules.length === 0 &&
    status.plan_history.length === 0 &&
    status.context_version === 1,
);

const generated = await run("s1", "帮我生成一份训练计划");
const plan1 = generated.draft;
check(
  "步骤 5 生成结构化计划草稿（新建 v1、12 个应训练日、取消清单为空、kind=plan）",
  plan1?.kind === "plan" &&
    plan1.status === "pending" &&
    plan1.revision === 1 &&
    plan1.payload?.plan?.version === "v1" &&
    plan1.payload?.plan?.payload?.plan_workouts?.length === 3 &&
    plan1.payload?.plan?.payload?.calendar_cycle?.slots?.length > 0 &&
    plan1.payload?.schedules?.length === 12 &&
    (plan1.payload?.cancellations ?? []).length === 0,
);
status = await devStatus();
check(
  "步骤 5 确认前隔离：正式计划、日程与 context_version 均不变",
  status.plan_version === null &&
    status.schedules.length === 0 &&
    status.context_version === 1,
);

const edited = structuredClone(plan1.payload);
edited.plan.payload.plan_workouts[0].exercises[0].prescription = {
  ...edited.plan.payload.plan_workouts[0].exercises[0].prescription,
  work_sets: 5,
};
const revised = await revise(plan1.id, edited, plan1.revision);
check(
  "步骤 5 轻量纠错：revision+1、日程仍按生效范围重算、官方数据不动",
  revised.status === 200 &&
    revised.body.draft.revision === 2 &&
    revised.body.draft.payload.schedules.length === 12 &&
    revised.body.draft.payload.plan.payload.plan_workouts[0].exercises[0]
      .prescription.work_sets === 5,
);
status = await devStatus();
check(
  "步骤 5 纠错后仍未写入正式数据",
  status.plan_version === null &&
    status.schedules.length === 0 &&
    status.context_version === 1,
);

const first = await confirm(plan1.id, 2);
status = await devStatus();
check(
  "步骤 6 首次确认原子启用 v1（context_version 仅 +1、12 个日程 scheduled）",
  first.status === 200 &&
    first.body.newly_committed === true &&
    status.plan_version === "v1" &&
    scheduleCount(status, "scheduled", "v1") === 12 &&
    status.context_version === 2 &&
    first.body.context_version === 2,
);

const again = await confirm(plan1.id, 2);
status = await devStatus();
check(
  "步骤 6 重复确认幂等：不重复建版本或日程、不重复递增",
  again.status === 200 &&
    again.body.newly_committed === false &&
    again.body.context_version === 2 &&
    status.plan_version === "v1" &&
    status.schedules.length === 12 &&
    status.context_version === 2,
);

/* ---------- 步骤 7—8：组合草稿、确认前隔离、故障回滚、原子切换 ---------- */
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe-placeholder" });
status = await devStatus();
const lockedBefore = scheduleCount(status, "locked");
const futureBefore = scheduleCount(status, "scheduled", "v2");
check(
  "步骤 7 前置（默认种子）：含已锁定与未来未锁定日程",
  lockedBefore > 0 && futureBefore > 0 && status.context_version === 3,
  `${lockedBefore} locked / ${futureBefore} scheduled`,
);

const todayOnly = await run("s1", "今天只能用哑铃");
status = await devStatus();
let projection = await profileProjection();
check(
  "「今天只能用哑铃」不持久化：不生成草稿、档案与正式计划不变",
  todayOnly.draft === null &&
    projection.profile.equipment.join("、") ===
      "杠铃、哑铃、卧推架、引体架、绳索" &&
    projection.plan.version === "v2" &&
    status.context_version === 3,
);

const composite = await run("s1", "以后只能用哑铃");
const comp = composite.draft;
check(
  "步骤 7 组合草稿：档案器械补丁 + v3 提议 + 12 个新日程 + 旧版未来未锁定取消清单（不含已锁定）",
  comp?.kind === "plan" &&
    comp.status === "pending" &&
    comp.payload?.profile_patch?.profile?.equipment?.join("、") === "哑铃" &&
    comp.payload?.plan?.version === "v3" &&
    comp.payload?.schedules?.length === 12 &&
    (comp.payload?.cancellations ?? []).length === futureBefore &&
    (comp.payload?.cancellations ?? []).every((c) => c.scheduled_on),
);
projection = await profileProjection();
status = await devStatus();
check(
  "步骤 7 确认前隔离：正式档案、v2 计划与全部日程均不变",
  projection.profile.equipment.join("、") ===
    "杠铃、哑铃、卧推架、引体架、绳索" &&
    projection.plan.version === "v2" &&
    status.plan_version === "v2" &&
    status.plan_history.length === 0 &&
    status.schedules.length === lockedBefore + futureBefore &&
    status.context_version === 3,
);

await api("POST", "/api/dev/confirm/fail-next", {});
const failedConfirm = await confirm(comp.id, comp.revision);
status = await devStatus();
projection = await profileProjection();
check(
  "步骤 8 故障注入：确认失败，档案、计划与历史、全部日程、草稿状态、context_version 整份回滚",
  failedConfirm.status === 500 &&
    projection.profile.equipment.join("、") ===
      "杠铃、哑铃、卧推架、引体架、绳索" &&
    status.context_version === 3 &&
    status.plan_version === "v2" &&
    status.plan_history.length === 0 &&
    status.schedules.length === lockedBefore + futureBefore &&
    scheduleCount(status, "scheduled", "v2") === futureBefore &&
    scheduleCount(status, "cancelled") === 0 &&
    draftStatus(status, comp.id) === "pending",
);

const committed = await confirm(comp.id, comp.revision);
status = await devStatus();
projection = await profileProjection();
check(
  "步骤 8 原子切换：档案补丁 + v3 启用 + 新日程 + 旧版未来未锁定取消，context_version 仅 +1",
  committed.status === 200 &&
    projection.profile.equipment.join("、") === "哑铃" &&
    status.plan_version === "v3" &&
    status.plan_history.length === 1 &&
    status.plan_history[0].version === "v2" &&
    status.plan_history[0].status === "archived" &&
    scheduleCount(status, "scheduled", "v3") === 12 &&
    scheduleCount(status, "cancelled", "v2") === futureBefore &&
    scheduleCount(status, "scheduled", "v2") === 0 &&
    status.context_version === 4 &&
    committed.body.context_version === 4,
);
check(
  "步骤 8 已锁定日程不动",
  scheduleCount(status, "locked", "v2") === lockedBefore,
);

/* ---------- 步骤 9：revision 冲突、stale→重算、丢弃后拒绝 ---------- */
const draftA = (await run("s1", "最近很累，帮我调整计划")).draft;
check(
  "步骤 9 替换提议为结构化载荷（版本只追加）",
  draftA?.kind === "plan" &&
    draftA.payload?.plan?.version === "v4" &&
    draftA.payload?.schedules?.length === 12 &&
    draftA.payload?.cancellations?.length === 12,
);

const wrongRevision = await confirm(draftA.id, draftA.revision + 7);
status = await devStatus();
check(
  "步骤 9 revision 冲突：409 draft_modified 且正式数据不动",
  wrongRevision.status === 409 &&
    wrongRevision.body.error_code === "draft_modified" &&
    status.plan_version === "v3" &&
    status.context_version === 4,
);

const draftB = (await run("s1", "最近很累，帮我调整计划")).draft;
const committedB = await confirm(draftB.id, draftB.revision);
status = await devStatus();
check(
  "步骤 9 先提交另一草稿：v4 启用、context_version 推进",
  committedB.status === 200 &&
    committedB.body.newly_committed === true &&
    status.plan_version === "v4" &&
    status.context_version === 5,
);

const stale = await confirm(draftA.id, draftA.revision);
check(
  "步骤 9 基线落后：409 draft_stale",
  stale.status === 409 && stale.body.error_code === "draft_stale",
);

const recalced = await recalc(draftA.id);
status = await devStatus();
check(
  "步骤 9 一键重算：新草稿 revision 1、关联旧草稿、旧草稿置 stale、正式计划不动",
  recalced.status === 200 &&
    recalced.body.new_draft.revision === 1 &&
    recalced.body.new_draft.parent_draft_id === draftA.id &&
    recalced.body.old_draft.status === "stale" &&
    recalced.body.new_draft.payload.plan.version === "v5" &&
    draftStatus(status, draftA.id) === "stale" &&
    status.plan_version === "v4",
);

const committedNew = await confirm(
  recalced.body.new_draft.id,
  recalced.body.new_draft.revision,
);
status = await devStatus();
check(
  "步骤 9 重算后确认：v5 启用且 context_version 仅 +1",
  committedNew.status === 200 &&
    status.plan_version === "v5" &&
    status.context_version === 6,
);

const draftC = (await run("s1", "最近很累，帮我调整计划")).draft;
const discarded = await discard(draftC.id);
const confirmDiscarded = await confirm(draftC.id, draftC.revision);
status = await devStatus();
check(
  "步骤 9 丢弃后拒绝确认：409 且正式数据与 context_version 不动",
  discarded.status === 200 &&
    confirmDiscarded.status === 409 &&
    status.plan_version === "v5" &&
    status.context_version === 6,
);

/* ---------- 01 §1.3：base_business_version 绑定生成时版本 ---------- */
await reset("noplan");
const earlyProposal = (await run("s1", "帮我生成一份训练计划")).draft;
const streamingRun = await startRun("s1", "帮我生成一份训练计划");
for (let i = 0; i < 120; i += 1) {
  await new Promise((r) => setTimeout(r, 200));
  const active = (await api("GET", "/api/runs/active")).body?.run;
  if (
    active &&
    active.run_id === streamingRun &&
    active.status === "running" &&
    active.saved_text.length > 0
  )
    break;
  if (i === 119) throw new Error("Run 未在预期时间内开始流式输出");
}
const advanced = await confirm(earlyProposal.id, earlyProposal.revision);
const lateProposal = (await waitRun(streamingRun)).draft;
const lateConfirm = await confirm(lateProposal.id, lateProposal.revision);
status = await devStatus();
check(
  "01 1.3 流式期间推进 context_version：后生成的草稿仍绑定生成时版本，确认返回 409 draft_stale",
  advanced.status === 200 &&
    advanced.body.context_version === 2 &&
    lateProposal.base_business_version === 1 &&
    lateConfirm.status === 409 &&
    lateConfirm.body.error_code === "draft_stale" &&
    status.plan_version === "v1" &&
    status.context_version === 2,
);

await vite.close();
console.log(failed === 0 ? "\n全部通过" : `\n${failed} 项未通过`);
process.exit(failed === 0 ? 0 : 1);
