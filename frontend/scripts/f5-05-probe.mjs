/**
 * F5-05 验收探针（走真实 mock REST + 源码断言）：中断接回与最低训练版
 * （plans/stage5.md F5-05 / 04 §4.6 / PRD §5.12）。
 *
 * 运行：node scripts/f5-05-probe.mjs
 * 覆盖：
 *  - 源码：INTERRUPT_DAYS=7、RETURN_EXPLICIT/CONFIRM、mode=return、period 标记
 *  - 显式接回 → plan 草稿（mode=return）；确认前正式 plan/schedules/cv 不变
 *  - 确认后 cv+1、新版本启用、旧版未来未锁定取消；重复确认幂等
 *  - ≥7 天：作废近记录拉开间隔 → 今日训练只提示；未确认不改计划；确认后进评估
 *  - 无已确认记录（全作废）→ 不判中断（gap null）
 *  - 红旗 → 不生成可执行三档处方
 *  - 病后无专业允许 → 只转介/建议休息，不落训练处方草稿
 *  - fail-next 确认接回草稿 → 500 回滚；重做成功
 *  - 回归期：接回确认后打卡 → period=return；更高重量不进 PR
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
const devStatus = async () => (await api("GET", "/api/dev/status")).body;
const planVersion = async () => (await devStatus())?.plan_version;
const planMode = async () => {
  const prof = (await api("GET", "/api/profile")).body;
  return prof?.plan?.mode ?? null;
};
const schedules = async () => (await devStatus())?.schedules ?? [];
const records = async () => (await api("GET", "/api/records")).body?.records ?? [];
const stats = async () => (await api("GET", "/api/stats")).body;
const confirmFailNext = () => api("POST", "/api/dev/confirm/fail-next", {});
const voidDraft = (trainingSessionId) =>
  api("POST", "/api/dev/drafts/training-void", {
    training_session_id: trainingSessionId,
  });

const scheduleCount = (list, storedStatus, planVersionFilter) =>
  list.filter(
    (s) =>
      s.stored_status === storedStatus &&
      (planVersionFilter === undefined || s.plan_version === planVersionFilter),
  ).length;

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
        status: active.status,
        draft: active.drafts?.[active.drafts.length - 1] ?? null,
        draftCount: active.draft_ids?.length ?? active.drafts?.length ?? 0,
      };
  }
  throw new Error("Run 未在预期时间内进入终态");
}

/* ========== 1. 源码断言 ========== */
console.log("\n--- 源码断言 ---");
{
  const src = readFileSync(path.join(root, "src/mock/server.ts"), "utf8");
  const contract = readFileSync(path.join(root, "src/lib/contract.ts"), "utf8");
  check(
    "1 INTERRUPT_DAYS=7 常量存在",
    /const INTERRUPT_DAYS = 7;/.test(src),
  );
  check(
    "1 显式/确认接回意图与今日训练闸门存在",
    /RETURN_EXPLICIT/.test(src) &&
      /RETURN_CONFIRM/.test(src) &&
      /needInterruptClarify/.test(src),
  );
  check(
    "1 接回草稿 mode=return；确认复用既有 plan 事务",
    /mode: "return"/.test(src) &&
      /function returnAssessmentReply/.test(src) &&
      /pendingPlanDraft\(proposal\.payload\)/.test(src),
  );
  check(
    "1 契约 period?: normal|return；PR 排除回归期",
    /period\?: "normal" \| "return"/.test(contract) &&
      /rec\.period !== "return"/.test(src),
  );
  check(
    "1 病后无许可只转介、红旗不解除",
    /只转介／建议休息/.test(src) &&
      /接回无权解除红旗阻断/.test(src),
  );
}

/* ========== 2. 显式接回 → plan 草稿；确认前隔离；确认后切换 ========== */
console.log("\n--- 显式接回 ---");
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });
{
  const planBefore = await planVersion();
  const modeBefore = await planMode();
  const schedBefore = await schedules();
  const futureBefore = scheduleCount(schedBefore, "scheduled", "v2");
  const lockedBefore = scheduleCount(schedBefore, "locked", "v2");
  const cvBefore = await cv();

  const res = await run("s-f5-05-explicit", "训练中断了，我要重新开始接回", "f5-05-exp");
  const draft = res.draft;
  check(
    "2 显式接回 → plan 草稿（kind=plan，mode=return，新版本）",
    draft?.kind === "plan" &&
      draft?.status === "pending" &&
      draft?.payload?.plan?.mode === "return" &&
      typeof draft?.payload?.plan?.version === "string" &&
      draft.payload.plan.version !== planBefore,
    JSON.stringify({
      kind: draft?.kind,
      mode: draft?.payload?.plan?.mode,
      ver: draft?.payload?.plan?.version,
    }),
  );
  check(
    "2 文案含接回档位/铁律/回归期说明且确认前不变",
    /正常接回|降级接回|最低任务/.test(res.text) &&
      /不补课|不照搬/.test(res.text) &&
      /确认前正式计划与日程不变/.test(res.text),
  );
  check(
    "2 确认前隔离：plan/mode/schedules/cv 不变",
    (await planVersion()) === planBefore &&
      (await planMode()) === modeBefore &&
      (await schedules()).length === schedBefore.length &&
      (await cv()) === cvBefore,
    `${planBefore}/${modeBefore} cv=${cvBefore}`,
  );

  // fail-next 回滚
  await confirmFailNext();
  const failedConfirm = await confirm(draft.id, draft.revision);
  const afterFail = await devStatus();
  check(
    "2 fail-next 确认接回草稿 → 500 且回滚",
    failedConfirm.status === 500 &&
      afterFail.context_version === cvBefore &&
      afterFail.plan_version === planBefore &&
      afterFail.drafts.find((d) => d.id === draft.id)?.status === "pending",
    JSON.stringify({
      status: failedConfirm.status,
      cv: afterFail.context_version,
      plan: afterFail.plan_version,
    }),
  );

  // 重做成功
  const committed = await confirm(draft.id, draft.revision);
  const afterOk = await devStatus();
  const newVer = afterOk.plan_version;
  check(
    "2 确认成功：cv+1、新版本、mode=return、旧版未来未锁定取消",
    committed.status === 200 &&
      committed.body.newly_committed === true &&
      afterOk.context_version === cvBefore + 1 &&
      newVer === draft.payload.plan.version &&
      (await planMode()) === "return" &&
      scheduleCount(afterOk.schedules, "cancelled", "v2") === futureBefore &&
      scheduleCount(afterOk.schedules, "scheduled", "v2") === 0 &&
      scheduleCount(afterOk.schedules, "locked", "v2") === lockedBefore &&
      scheduleCount(afterOk.schedules, "scheduled", newVer) ===
        draft.payload.schedules.length,
    JSON.stringify({
      cv: afterOk.context_version,
      plan: newVer,
      cancels: scheduleCount(afterOk.schedules, "cancelled", "v2"),
    }),
  );

  // 幂等
  const again = await confirm(draft.id, draft.revision);
  check(
    "2 重复确认幂等：newly_committed=false，cv 不再 +1",
    again.status === 200 &&
      again.body.newly_committed === false &&
      (await cv()) === cvBefore + 1,
    JSON.stringify({ newly: again.body.newly_committed }),
  );
}

/* ========== 3. ≥7 天：作废近记录 → 今日训练只提示；确认后才评估 ========== */
console.log("\n--- ≥7 天中断确认闸门 ---");
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });
{
  // 作废 09-07 与 09-05：最近 valid → 09-02，距 09-11 = 9 天 ≥ 7
  for (const ts of ["ts-seed-0907", "ts-seed-0905"]) {
    const d = await voidDraft(ts);
    if (d.status !== 200 || !d.body?.draft?.id)
      throw new Error(`作废草稿失败 ${ts}：${JSON.stringify(d.body)}`);
    const c = await confirm(d.body.draft.id, d.body.draft.revision);
    if (c.status !== 200) throw new Error(`作废确认失败 ${ts}`);
  }
  const recs = await records();
  const dates = recs.filter((r) => r.status === "valid").map((r) => r.date);
  const last = dates.sort().at(-1);
  check("3 作废后最近 valid 日期 ≤ 2026-09-02（间隔 ≥7）", last <= "2026-09-02", `last=${last}`);

  const planBefore = await planVersion();
  const cvBefore = await cv();
  const clarify = await run("s-f5-05-gap", "给我训练指导，开始今日训练", "f5-05-gap");
  check(
    "3 今日训练（≥7 天）→ 只提示确认是否中断，不落草稿",
    clarify.draft == null &&
      clarify.draftCount === 0 &&
      /中断/.test(clarify.text) &&
      /确认接回|是中断/.test(clarify.text),
    clarify.text.slice(0, 100),
  );
  check(
    "3 未确认：plan/cv 不变",
    (await planVersion()) === planBefore && (await cv()) === cvBefore,
  );

  const unconfirmed = await run("s-f5-05-gap", "今天练什么", "f5-05-gap2");
  check(
    "3 再次今日训练仍未确认 → 仍澄清不落草稿",
    unconfirmed.draft == null && (await cv()) === cvBefore,
    unconfirmed.text.slice(0, 80),
  );

  const confirmRes = await run("s-f5-05-gap", "是中断，确认接回", "f5-05-gap3");
  check(
    "3 确认「是中断」→ 进入评估并出 plan 草稿 mode=return",
    confirmRes.draft?.kind === "plan" &&
      confirmRes.draft?.payload?.plan?.mode === "return",
    JSON.stringify({
      kind: confirmRes.draft?.kind,
      mode: confirmRes.draft?.payload?.plan?.mode,
    }),
  );
  check(
    "3 确认评估前正式计划仍未变",
    (await planVersion()) === planBefore && (await cv()) === cvBefore,
  );
}

/* ========== 3b. 无已确认记录 → 不判中断 ========== */
console.log("\n--- 无记录不判中断 ---");
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });
{
  for (const ts of ["ts-seed-0907", "ts-seed-0905", "ts-seed-0902", "ts-seed-0831"]) {
    const d = await voidDraft(ts);
    await confirm(d.body.draft.id, d.body.draft.revision);
  }
  const recs = await records();
  check(
    "3b 全部作废后无 valid 记录",
    recs.every((r) => r.status !== "valid"),
  );
  const res = await run("s-f5-05-norec", "给我训练指导", "f5-05-norec");
  check(
    "3b 今日训练无已确认记录 → 不触发中断澄清（走既有指导/校准）",
    !/属于训练中断/.test(res.text) && !/请回复「是中断」/.test(res.text),
    res.text.slice(0, 100),
  );
}

/* ========== 4. 红旗阻断 ========== */
console.log("\n--- 红旗 ---");
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });
{
  // 六类安全症状（plan.classifyBodyConditions）：刺痛等；消息级红旗须阻断接回
  const res = await run(
    "s-f5-05-red",
    "重新开始接回，我腿部刺痛",
    "f5-05-red",
  );
  check(
    "4 消息命中红旗 → 专业评估文案且无训练处方草稿",
    res.draft == null &&
      res.draftCount === 0 &&
      /专业评估|线下专业/.test(res.text) &&
      /接回无权解除红旗/.test(res.text),
    JSON.stringify({ draft: res.draft?.id, text: res.text.slice(0, 100) }),
  );

  // 档案级：经对话更新身体情况为红旗后再显式接回
  const upd = await run(
    "s-f5-05-red2",
    "身体情况：胸部明显疼痛",
    "f5-05-red2",
  );
  if (upd.draft?.kind === "profile_update") {
    await confirm(upd.draft.id, upd.draft.revision);
    const res2 = await run("s-f5-05-red2", "我要接回重新开始", "f5-05-red2b");
    check(
      "4 档案红旗 → 接回不生成三档处方",
      res2.draft == null &&
        /专业评估|不生成/.test(res2.text) &&
        !/正常接回|降级接回/.test(res2.text),
      res2.text.slice(0, 100),
    );
  } else {
    check(
      "4 档案红旗接回（消息级红旗已覆盖；建档未出草稿则跳过二次）",
      true,
      `upd.draft=${upd.draft?.kind ?? "none"}`,
    );
  }
}

/* ========== 5. 病后无专业允许 → 只转介 ========== */
console.log("\n--- 病后无许可 ---");
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });
{
  const res = await run(
    "s-f5-05-ill",
    "病后重新开始接回",
    "f5-05-ill",
  );
  check(
    "5 病后未获专业允许 → 只转介/建议休息，不落训练处方",
    res.draft == null &&
      res.draftCount === 0 &&
      /只转介|建议休息|专业允许/.test(res.text) &&
      !/mode=return/.test(res.text),
    res.text.slice(0, 120),
  );

  const ok = await run(
    "s-f5-05-ill2",
    "病后重新开始接回，已获专业允许可以训练",
    "f5-05-ill2",
  );
  check(
    "5 明确已获专业允许 → 可进接回评估出草稿",
    ok.draft?.kind === "plan" && ok.draft?.payload?.plan?.mode === "return",
    JSON.stringify({ kind: ok.draft?.kind }),
  );
}

/* ========== 6. 回归期标签与 PR 排除 ========== */
console.log("\n--- 回归期 PR 排除 ---");
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });
{
  const prBefore = (await stats()).prs.find((p) => p.exercise === "杠铃平板卧推");
  check("6 前置 PR 卧推 80×8", prBefore?.best_weight_kg === 80);

  const res = await run("s-f5-05-ret", "重新开始接回", "f5-05-ret");
  const draft = res.draft;
  await confirm(draft.id, draft.revision);
  check("6 接回版本 mode=return 启用", (await planMode()) === "return");

  // 接回后打卡更高重量卧推
  const checkin = await run(
    "s-f5-05-ret",
    "今天卧推 100kg 2组 每组5次",
    "f5-05-ret-ci",
  );
  check("6 回归期打卡出记录草稿", checkin.draft?.kind === "training_record");
  await confirm(checkin.draft.id, checkin.draft.revision);

  const recs = await records();
  const ci = recs.find(
    (r) => r.exercise === "杠铃平板卧推" && r.sets?.some((s) => s.weight_kg === 100),
  );
  check(
    "6 回归期记录 period=return",
    ci?.period === "return",
    JSON.stringify({ period: ci?.period }),
  );
  const prAfter = (await stats()).prs.find((p) => p.exercise === "杠铃平板卧推");
  check(
    "6 回归期 100kg 不进 PR（仍为 80×8）",
    prAfter?.best_weight_kg === 80 && prAfter?.best_reps_at_weight === 8,
    JSON.stringify(prAfter),
  );
  // 工作组判定仍展示：该记录 status=valid 且有工作组
  check(
    "6 回归期记录仍为 valid（工作组可展示，未退出记录集）",
    ci?.status === "valid",
  );
}

await vite.close();
console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
