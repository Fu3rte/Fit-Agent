/**
 * F5-06 验收探针（走真实 mock REST + 源码断言）：基于可信历史的渐进／负荷建议
 * （plans/stage5.md F5-06 / 04 §4.4 / PRD §5.4）。
 *
 * 运行：node scripts/f5-06-probe.mjs
 * 覆盖：
 *  - 源码：PROGRESSION_HINT / deriveProgressionCandidates / 最小增量 / 次数上限
 *  - 显式加重 → 文本含基于种子记录的候选（80→82.5）；无确认 cv/plan 不变
 *  - 「按最近表现加重」→ plan 草稿；确认后 cv+1、新版本启用；fail-next 回滚；幂等
 *  - empty 种子 → 不猜重量（无 kg 数字建议）
 *  - 非显式（只聊复盘）→ 不出负荷建议草稿
 *  - 生成复盘正文不被渐进建议改写
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
const confirmFailNext = () => api("POST", "/api/dev/confirm/fail-next", {});

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
  const planSrc = readFileSync(path.join(root, "src/mock/plan.ts"), "utf8");
  check(
    "1 PROGRESSION_HINT / MIN_LOAD_STEP / 次数上限常量存在",
    /PROGRESSION_HINT/.test(src) &&
      /MIN_LOAD_STEP/.test(src) &&
      /REPS_CAP_EXTERNAL = 12/.test(src) &&
      /REPS_CAP_BODYWEIGHT = 15/.test(src),
  );
  check(
    "1 deriveProgressionCandidates / progressionScriptReply 存在且接线",
    /function deriveProgressionCandidates/.test(src) &&
      /function progressionScriptReply/.test(src) &&
      /isProgression[\s\S]{0,200}progressionScriptReply/.test(src),
  );
  check(
    "1 planPayloadError 允许 verified（须 basis_record_revision_id）",
    /basis_record_revision_id/.test(planSrc) &&
      /load\.kind === "verified"/.test(planSrc),
  );
  check(
    "1 无新业务端点（不新增 /api/ 业务路由）",
    !/app\.(get|post|put|patch|delete)\(\s*["'`]/.test(src),
  );
}

/* ========== 2. 显式加重 → 文本候选；无确认不变 ========== */
console.log("\n--- 显式加重文本建议 ---");
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });
{
  const cvBefore = await cv();
  const planBefore = await planVersion();
  const res = await run("s-f5-06-text", "帮我加重", "f5-06-text");
  check(
    "2 显式「帮我加重」→ 文本含种子卧推候选 80→82.5",
    /卧推/.test(res.text) &&
      /80/.test(res.text) &&
      /82\.5/.test(res.text),
    res.text.slice(0, 200),
  );
  check(
    "2 未要求长期草稿时只出文本（不落 draft）；cv/plan 不变",
    res.draft == null &&
      res.draftCount === 0 &&
      (await cv()) === cvBefore &&
      (await planVersion()) === planBefore,
    JSON.stringify({ draft: res.draft?.id, n: res.draftCount }),
  );
  check(
    "2 文案声明不自动改计划 / 不猜重量边界",
    /未自动修改正式计划|不猜重量/.test(res.text),
  );
}

/* ========== 3. 按最近表现加重 → plan 草稿；确认/回滚/幂等 ========== */
console.log("\n--- 长期渐进草稿 ---");
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });
{
  const cvBefore = await cv();
  const planBefore = await planVersion();
  const schedBefore = await schedules();
  const futureBefore = schedBefore.filter(
    (s) => s.stored_status === "scheduled" && s.plan_version === "v2",
  ).length;
  const lockedBefore = schedBefore.filter(
    (s) => s.stored_status === "locked" && s.plan_version === "v2",
  ).length;

  const res = await run(
    "s-f5-06-long",
    "按最近表现加重调整计划",
    "f5-06-long",
  );
  const draft = res.draft;
  check(
    "3 显式长期加重 → plan 草稿（新版本；含负荷 diff）",
    draft?.kind === "plan" &&
      draft?.status === "pending" &&
      typeof draft?.payload?.plan?.version === "string" &&
      draft.payload.plan.version !== planBefore &&
      Array.isArray(draft.payload.diff) &&
      draft.payload.diff.some((d) => /负荷/.test(d.field ?? "")),
    JSON.stringify({
      kind: draft?.kind,
      ver: draft?.payload?.plan?.version,
      diff: draft?.payload?.diff?.slice(0, 3),
    }),
  );
  check(
    "3 确认前 plan/schedules/cv 不变",
    (await planVersion()) === planBefore &&
      (await schedules()).length === schedBefore.length &&
      (await cv()) === cvBefore,
  );

  // fail-next 回滚
  await confirmFailNext();
  const failedConfirm = await confirm(draft.id, draft.revision);
  const afterFail = await devStatus();
  check(
    "3 fail-next 确认 → 500 且回滚（cv/plan 不变、草稿仍 pending）",
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
    "3 确认成功：cv+1、新版本启用、旧版未来未锁定取消",
    committed.status === 200 &&
      committed.body.newly_committed === true &&
      afterOk.context_version === cvBefore + 1 &&
      newVer === draft.payload.plan.version &&
      afterOk.schedules.filter(
        (s) => s.stored_status === "cancelled" && s.plan_version === "v2",
      ).length === futureBefore &&
      afterOk.schedules.filter(
        (s) => s.stored_status === "locked" && s.plan_version === "v2",
      ).length === lockedBefore,
    JSON.stringify({ cv: afterOk.context_version, plan: newVer }),
  );

  // 幂等
  const again = await confirm(draft.id, draft.revision);
  check(
    "3 重复确认幂等：newly_committed=false，cv 不再 +1",
    again.status === 200 &&
      again.body.newly_committed === false &&
      (await cv()) === cvBefore + 1,
    JSON.stringify({ newly: again.body.newly_committed }),
  );
}

/* ========== 4. empty 种子 → 不猜重量 ========== */
console.log("\n--- empty 不猜重量 ---");
await reset("empty");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });
{
  const res = await run("s-f5-06-empty", "帮我加重", "f5-06-empty");
  const hasKgSuggest = /\d+(?:\.\d+)?\s*(?:→|->)\s*\d+(?:\.\d+)?\s*kg|建议加重到\s*\d/.test(
    res.text,
  );
  check(
    "4 empty + 显式加重 → 不给出具体重量数字建议",
    !hasKgSuggest && (res.draft == null || res.draft.kind !== "plan"),
    res.text.slice(0, 160),
  );
}

/* ========== 5. 非显式只聊复盘 → 不出负荷草稿 ========== */
console.log("\n--- 非显式 ---");
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });
{
  const cvBefore = await cv();
  const res = await run(
    "s-f5-06-quiet",
    "最近训练状态怎么样，聊聊复盘",
    "f5-06-quiet",
  );
  check(
    "5 非显式（只聊复盘）→ 无负荷建议草稿、cv 不变",
    res.draft == null &&
      res.draftCount === 0 &&
      (await cv()) === cvBefore &&
      !/80\s*(?:→|->)\s*82\.5/.test(res.text),
    res.text.slice(0, 120),
  );
}

/* ========== 6. 生成复盘正文不被改写 ========== */
console.log("\n--- 复盘正文不被渐进改写 ---");
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });
{
  await run("s-f5-06-rev", "生成复盘", "f5-06-rev");
  const before = await review();
  check("6 前置：生成复盘成功", before?.text?.length > 0);
  await run("s-f5-06-rev", "帮我加重", "f5-06-rev-prog");
  const after = await review();
  check(
    "6 渐进建议后复盘 text/generated_at 逐字不变",
    after?.text === before?.text &&
      after?.generated_at === before?.generated_at,
    JSON.stringify({
      textSame: after?.text === before?.text,
      atSame: after?.generated_at === before?.generated_at,
    }),
  );
}

await vite.close();

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
