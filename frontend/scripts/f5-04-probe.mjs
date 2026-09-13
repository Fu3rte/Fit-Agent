/**
 * F5-04 验收探针（走真实 mock REST + 源码断言）：复盘后后续安排／新计划草稿
 * （plans/stage5.md F5-04 / §3.3 / §7 第 3、4、8 步）。
 *
 * 运行：node scripts/f5-04-probe.mjs
 * 覆盖：
 *  - 源码：isSuggestLong/isSuggestAdjust 分支存在；生成复盘不落 draft
 *  - 生成复盘 → draft 数 0；cv 不变；复盘条目不改写
 *  - 「按复盘建议调整计划」/「换计划」→ plan 草稿；确认前 plan/schedules 不变
 *  - 确认后 cv+1、旧版未来未锁定日程取消；复盘 text/generated_at 不变（stale 可变）
 *  - 「今天按建议轻一点」→ arrangement 草稿
 *  - 模糊「你觉得呢」→ 不落草稿
 *  - fail-next 确认计划草稿 → 500 回滚；重做成功
 *  - 重复确认幂等（newly_committed=false）
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
const schedules = async () => (await devStatus())?.schedules ?? [];
const review = async () => (await api("GET", "/api/review")).body;
const reviewEntries = async () => (await devStatus())?.review_entries;
const confirmFailNext = () => api("POST", "/api/dev/confirm/fail-next", {});

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

/* ========== 1. 源码断言：意图分支与生成复盘不落 draft ========== */
console.log("\n--- 源码断言 ---");
{
  const src = readFileSync(path.join(root, "src/mock/server.ts"), "utf8");
  check(
    "1 isSuggestLong/isSuggestAdjust 意图定义存在",
    /isSuggestLong/.test(src) && /isSuggestAdjust/.test(src),
  );
  check(
    "1 建议分支复用 planScriptReply / arrangementScriptReply（无第二套草稿路径）",
    /isSuggestLong[\s\S]{0,200}planScriptReply/.test(src) &&
      /isArrangement \|\| isSuggestAdjust[\s\S]{0,200}arrangementScriptReply/.test(
        src,
      ),
  );
  const genFn = src.match(
    /function reviewGenerateReply[\s\S]*?\n\}/,
  )?.[0] ?? "";
  check(
    "1 生成复盘回复不携带 draft（正文建议纯文本）",
    /reviewGenerateReply/.test(src) &&
      !/draft\s*[:=]/.test(genFn) &&
      /不会自动创建草稿|不会修改计划或日程/.test(src),
  );
  check(
    "1 REVIEW_GENERATE 仍优先且不产出 draft",
    /else if \(isReviewGenerate\)/.test(src) &&
      /reviewGenerateReply\(state\)/.test(src),
  );
}

/* ========== 2. 生成复盘 → 不落草稿、cv/条目不变 ========== */
console.log("\n--- 生成复盘不建草稿 ---");
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });
{
  const cvBefore = await cv();
  const entriesBefore = await reviewEntries();
  const planBefore = await planVersion();
  const res = await run("s-f5-04-gen", "生成复盘", "f5-04-gen");
  const entriesAfter = await reviewEntries();
  const cvAfter = await cv();

  check(
    "2 生成复盘回复含「已保存复盘」且建议为纯文本",
    /已保存复盘/.test(res.text) && /不会自动创建草稿/.test(res.text),
  );
  check(
    "2 Run 未提出任何 draft",
    res.draft == null && res.draftCount === 0,
    JSON.stringify({ draft: res.draft?.id, n: res.draftCount }),
  );
  check(
    "2 cv 不变、计划不变、复盘条数+1（仅追加）",
    cvAfter === cvBefore &&
      (await planVersion()) === planBefore &&
      entriesAfter.length === entriesBefore.length + 1,
    `${cvBefore}→${cvAfter}`,
  );
}

/* ========== 3. 「按复盘建议调整计划」→ plan 草稿；确认前隔离；确认后切换 ========== */
console.log("\n--- 按复盘建议调整计划 → plan ---");
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });
let planDraftId = null;
let planDraftRev = null;
let rev0 = null;
{
  await run("s-f5-04-plan", "生成复盘", "f5-04-plan-gen");
  rev0 = await review();
  check("3 前置：生成后 stale=false", rev0.stale === false);

  const schedBefore = await schedules();
  const futureBefore = scheduleCount(schedBefore, "scheduled", "v2");
  const lockedBefore = scheduleCount(schedBefore, "locked", "v2");
  const cvBefore = await cv();
  const planBefore = await planVersion();

  const res = await run("s-f5-04-plan", "按复盘建议调整计划", "f5-04-plan");
  const draft = res.draft;
  planDraftId = draft?.id ?? null;
  planDraftRev = draft?.revision ?? null;

  check(
    "3 「按复盘建议调整计划」→ plan 草稿（kind=plan，含新版本与取消清单）",
    draft?.kind === "plan" &&
      draft?.status === "pending" &&
      typeof draft?.payload?.plan?.version === "string" &&
      draft.payload.plan.version !== "v2" &&
      Array.isArray(draft.payload.schedules) &&
      draft.payload.schedules.length > 0 &&
      (draft.payload.cancellations ?? []).length === futureBefore,
    JSON.stringify({
      kind: draft?.kind,
      ver: draft?.payload?.plan?.version,
      cancels: draft?.payload?.cancellations?.length,
      futureBefore,
    }),
  );
  check(
    "3 对话文案说明只追加新版本、确认前不变",
    /只追加新版本|不静默覆盖/.test(res.text) &&
      /确认前正式计划与日程不变/.test(res.text),
  );

  const schedMid = await schedules();
  check(
    "3 确认前隔离：plan/schedules/cv 不变",
    (await planVersion()) === planBefore &&
      schedMid.length === schedBefore.length &&
      cvBefore === (await cv()),
    `${planBefore} cv=${cvBefore}`,
  );

  // fail-next 确认 → 500 回滚
  await confirmFailNext();
  const failedConfirm = await confirm(draft.id, draft.revision);
  const afterFail = await devStatus();
  check(
    "3 fail-next 确认计划草稿 → 500 且回滚（cv/plan/schedules/草稿 pending）",
    failedConfirm.status === 500 &&
      afterFail.context_version === cvBefore &&
      afterFail.plan_version === "v2" &&
      afterFail.schedules.length === schedBefore.length &&
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
    "3 确认成功：cv+1、新版本启用、旧版未来未锁定取消展示、已锁定不动",
    committed.status === 200 &&
      committed.body.newly_committed === true &&
      afterOk.context_version === cvBefore + 1 &&
      newVer === draft.payload.plan.version &&
      scheduleCount(afterOk.schedules, "cancelled", "v2") === futureBefore &&
      scheduleCount(afterOk.schedules, "scheduled", "v2") === 0 &&
      scheduleCount(afterOk.schedules, "locked", "v2") === lockedBefore &&
      scheduleCount(afterOk.schedules, "scheduled", newVer) ===
        draft.payload.schedules.length,
    JSON.stringify({
      cv: afterOk.context_version,
      plan: newVer,
      cancels: scheduleCount(afterOk.schedules, "cancelled", "v2"),
      locked: scheduleCount(afterOk.schedules, "locked", "v2"),
    }),
  );

  // 重复确认幂等
  const again = await confirm(draft.id, draft.revision);
  const afterAgain = await devStatus();
  check(
    "3 重复确认幂等：newly_committed=false，cv 不再 +1",
    again.status === 200 &&
      again.body.newly_committed === false &&
      afterAgain.context_version === cvBefore + 1,
    JSON.stringify({
      newly: again.body.newly_committed,
      cv: afterAgain.context_version,
    }),
  );

  // 复盘正文/generated_at 不变；stale 可变
  const rev1 = await review();
  check(
    "3 调整确认后复盘 text/generated_at 逐字不变（stale 可翻转）",
    rev1.text === rev0.text && rev1.generated_at === rev0.generated_at,
    JSON.stringify({
      textSame: rev1.text === rev0.text,
      genSame: rev1.generated_at === rev0.generated_at,
      stale: rev1.stale,
    }),
  );
}

/* ========== 4. 「换计划」→ plan 草稿（另一意图词） ========== */
console.log("\n--- 换计划 ---");
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });
{
  const res = await run("s-f5-04-swap", "换计划", "f5-04-swap");
  check(
    "4 「换计划」→ plan 草稿",
    res.draft?.kind === "plan" && typeof res.draft?.payload?.plan?.version === "string",
    JSON.stringify({ kind: res.draft?.kind, ver: res.draft?.payload?.plan?.version }),
  );
}

/* ========== 5. 「今天按建议轻一点」→ arrangement 草稿 ========== */
console.log("\n--- 今天按建议轻一点 → arrangement ---");
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });
{
  const planBefore = await planVersion();
  const cvBefore = await cv();
  const res = await run("s-f5-04-arr", "今天按建议轻一点", "f5-04-arr");
  check(
    "5 「今天按建议轻一点」→ arrangement 草稿（不改计划版本）",
    res.draft?.kind === "arrangement" &&
      (await planVersion()) === planBefore &&
      (await cv()) === cvBefore,
    JSON.stringify({
      kind: res.draft?.kind,
      reason: res.draft?.payload?.target?.adjustment_reason,
    }),
  );
}

/* ========== 6. 模糊「你觉得呢」→ 不落草稿 ========== */
console.log("\n--- 模糊不落草稿 ---");
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });
{
  const res = await run("s-f5-04-vague", "你觉得呢", "f5-04-vague");
  check(
    "6 「你觉得呢」不落草稿",
    res.draft == null && res.draftCount === 0,
    JSON.stringify({ draft: res.draft?.id }),
  );
}

/* ========== 7. 裸「按建议」→ 安排链路澄清、不落草稿 ========== */
console.log("\n--- 裸按建议澄清 ---");
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });
{
  const res = await run("s-f5-04-bare", "按建议", "f5-04-bare");
  check(
    "7 裸「按建议」→ 澄清/档位文案且不落草稿",
    res.draft == null && res.draftCount === 0 && (res.text ?? "").length > 0,
    (res.text ?? "").slice(0, 80),
  );
}

await vite.close();
console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
