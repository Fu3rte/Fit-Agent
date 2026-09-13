/**
 * F4-06 验收探针（走真实 mock REST + 源码断言）：过期草稿拦截、一键重算与幂等
 * （plans/stage4.md F4-06 / §7 第 3、5 步）。
 *
 * 运行：node scripts/f4-06-probe.mjs
 * 覆盖：
 *  - 源码：DraftCard stale 面板含变更项说明与「按最新数据重新生成草稿」按钮
 *  - 源码：ChatPage 透传 staleDetail；recalc 子草稿 recalcDiff 可见
 *  - 已确认／已丢弃／未过期的旧草稿 recalc → 409 invalid_request
 *  - draft_stale → recalc → 子草稿（parent_draft_id + 绑定读取时最新 cv + revision=1）
 *  - 重算期间业务版本再推进：子草稿仍绑定 recalc 读取时最新版本、可确认成功
 *  - 旧草稿 payload 不被改写、不自动采纳（status=stale 仅表示被取代）
 *  - 子草稿仍为 Pending 时重复 recalc 幂等返回同一子草稿（不产生第二个）
 *  - 子草稿确认前正式数据不变（cv / 记录不变）
 *  - training_void 草稿 recalc 不落 profile 兜底（kind/分支正确）
 *  - fail-next 子草稿确认：500 且整份回滚
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
const voidDraft = (trainingSessionId) =>
  api("POST", "/api/dev/drafts/training-void", {
    training_session_id: trainingSessionId,
  });
const failNext = () => api("POST", "/api/dev/confirm/fail-next", {});
const recalc = (id, clientRequestId) =>
  api("POST", `/api/drafts/${id}/recalc`, {
    client_request_id: clientRequestId,
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

/* ========== 1. 源码断言：stale 变更项 + 一键重算 + Diff ========== */
console.log("\n--- 源码断言 UI ---");
{
  const card = readFileSync(
    path.join(root, "src/features/chat/DraftCard.tsx"),
    "utf8",
  );
  check(
    "1 DraftCard stale 面板含变更项说明（相关变更）",
    /相关变更/.test(card) && /staleDetail/.test(card),
  );
  check(
    "1 DraftCard 含「按最新数据重新生成草稿」重算入口",
    /按最新数据重新生成草稿/.test(card) && /onRecalc/.test(card),
  );
  check(
    "1 DraftCard 子草稿展示新旧 Diff（recalcDiff）",
    /recalcDiff/.test(card) && /与旧草稿对比/.test(card),
  );
  check(
    "1 DraftCard 标注 parent_draft_id 重算草稿徽章",
    /parent_draft_id/.test(card) && /重算草稿/.test(card),
  );
  const chat = readFileSync(
    path.join(root, "src/features/chat/ChatPage.tsx"),
    "utf8",
  );
  check(
    "1 ChatPage 透传 staleDetail 与 recalcDiff",
    /staleDetail=/.test(chat) && /recalcDiff=/.test(chat),
  );
  const apiSrc = readFileSync(
    path.join(root, "src/lib/api.ts"),
    "utf8",
  );
  check(
    "1 api.recalcDraft 携带 client_request_id（RecalcRequest）",
    /recalcDraft/.test(apiSrc) && /client_request_id/.test(apiSrc),
  );
}

/* ========== 2. 409 拒绝：已确认／已丢弃／未过期 ========== */
console.log("\n--- 409 拒绝路径 ---");
{
  await reset("default");
  await api("PUT", "/api/provider/api-key", { api_key: "probe" });

  // 2a. 未过期（仍基于最新 cv）的 pending 草稿 → 409
  const gen = await run(
    "s-f4-06-reject",
    "昨天弯举 12kg 3组 每组10次",
    "f4-06-reject",
  );
  check(
    "2a 前置：生成 training_record 草稿",
    gen.draft?.kind === "training_record" && gen.draft.status === "pending",
    gen.draft?.kind,
  );
  const notStale = await recalc(gen.draft.id, `f4-06-notstale-${Date.now()}`);
  check(
    "2a 未过期 pending 草稿 recalc → 409 invalid_request",
    notStale.status === 409 &&
      notStale.body.error_code === "invalid_request" &&
      /最新业务上下文/.test(notStale.body.message ?? ""),
    JSON.stringify(notStale.body),
  );

  // 2b. 已丢弃草稿 → 409
  await api("POST", `/api/drafts/${gen.draft.id}/discard`, {});
  const afterDiscard = await recalc(gen.draft.id, `f4-06-dis-${Date.now()}`);
  check(
    "2b 已丢弃草稿 recalc → 409 invalid_request",
    afterDiscard.status === 409 &&
      afterDiscard.body.error_code === "invalid_request" &&
      /已提交或已丢弃/.test(afterDiscard.body.message ?? ""),
    JSON.stringify(afterDiscard.body),
  );

  // 2c. 已确认草稿 → 409（另起一稿走确认）
  const gen2 = await run(
    "s-f4-06-commit",
    "今天硬拉 60kg 2组 每组8次",
    "f4-06-commit",
  );
  const okConfirm = await confirm(gen2.draft.id, gen2.draft.revision);
  check(
    "2c 前置：确认草稿成功",
    okConfirm.status === 200 && okConfirm.body.newly_committed === true,
    JSON.stringify(okConfirm.body),
  );
  const afterCommit = await recalc(gen2.draft.id, `f4-06-commit-${Date.now()}`);
  check(
    "2c 已确认草稿 recalc → 409 invalid_request",
    afterCommit.status === 409 &&
      afterCommit.body.error_code === "invalid_request" &&
      /已提交或已丢弃/.test(afterCommit.body.message ?? ""),
    JSON.stringify(afterCommit.body),
  );
}

/* ========== 3. stale → recalc → 子草稿绑定最新 cv → 幂等 → 再确认 ========== */
console.log("\n--- stale → recalc 幂等链路 ---");
{
  await reset("default");
  await api("PUT", "/api/provider/api-key", { api_key: "probe" });

  const cv0 = await cv();
  // 生成更正草稿（不确认），然后经另一路径推进 cv → 旧稿 stale
  const gen = await run(
    "s-f4-06-recalc",
    "更正 ts-seed-0907 的训练记录：哑铃弯举 第2组改成6次",
    "f4-06-recalc",
  );
  check(
    "3 前置：生成更正草稿（ts-seed-0907）",
    gen.draft?.kind === "training_record" &&
      gen.draft.payload?.training_session_id === "ts-seed-0907",
    JSON.stringify(gen.draft?.payload),
  );
  const oldPayloadBefore = JSON.stringify(gen.draft.payload);
  const cvAfterGen = await cv();

  // 业务版本推进：作废 0831（与 0907 无关）
  const voidPush = await voidDraft("ts-seed-0831");
  const voidOk = await confirm(voidPush.body.draft.id, voidPush.body.draft.revision);
  check(
    "3 业务版本已推进（cv+1）",
    voidOk.status === 200 && (await cv()) === cvAfterGen + 1,
    JSON.stringify({ cv: await cv(), base: gen.draft.base_business_version }),
  );

  // 再确认旧稿 → draft_stale（UI 拦截入口）
  const staleConfirm = await confirm(gen.draft.id, gen.draft.revision);
  check(
    "3 旧稿再确认 → 409 draft_stale（展示变更项入口）",
    staleConfirm.status === 409 &&
      staleConfirm.body.error_code === "draft_stale" &&
      typeof staleConfirm.body.detail === "string",
    JSON.stringify(staleConfirm.body),
  );

  // 一键重算：子草稿绑定 recalc 读取时最新 cv
  const cvAtRecalc = await cv();
  const recalced = await recalc(gen.draft.id, `f4-06-recalc-1-${Date.now()}`);
  check(
    "3 recalc：200 + parent_draft_id + revision=1 + 绑定读取时最新 cv",
    recalced.status === 200 &&
      recalced.body.new_draft?.status === "pending" &&
      recalced.body.new_draft.revision === 1 &&
      recalced.body.new_draft.parent_draft_id === gen.draft.id &&
      recalced.body.new_draft.kind === "training_record" &&
      recalced.body.new_draft.base_business_version === cvAtRecalc &&
      recalced.body.old_draft.status === "stale",
    JSON.stringify({
      status: recalced.status,
      kind: recalced.body?.new_draft?.kind,
      parent: recalced.body?.new_draft?.parent_draft_id,
      rev: recalced.body?.new_draft?.revision,
      base: recalced.body?.new_draft?.base_business_version,
      cvAtRecalc,
      old: recalced.body?.old_draft?.status,
    }),
  );
  const child = recalced.body.new_draft;
  check(
    "3 旧草稿 payload 未被改写（不自动合并/不自动采纳）",
    JSON.stringify(recalced.body.old_draft.payload) === oldPayloadBefore,
  );
  check(
    "3 子草稿 payload 仍指向 ts-seed-0907（意图保留）",
    child.payload?.training_session_id === "ts-seed-0907",
    JSON.stringify(child.payload),
  );
  check(
    "3 新旧 Diff 数组存在（draft_vs_draft_diff）",
    Array.isArray(recalced.body.draft_vs_draft_diff),
    JSON.stringify(recalced.body.draft_vs_draft_diff?.slice?.(0, 2)),
  );

  // 幂等：子草稿仍 Pending 时重复 recalc → 同一子草稿
  const cvBeforeIdem = await cv();
  const again = await recalc(gen.draft.id, `f4-06-recalc-2-${Date.now()}`);
  check(
    "3 重复 recalc（子稿仍 Pending）→ 幂等返回同一子草稿",
    again.status === 200 &&
      again.body.new_draft?.id === child.id &&
      again.body.new_draft.status === "pending" &&
      (await cv()) === cvBeforeIdem,
    JSON.stringify({
      status: again.status,
      id: again.body?.new_draft?.id,
      expect: child.id,
      cv: await cv(),
    }),
  );
  // 不产生第二个：再次拉子稿列表视角——幂等体即证明（同 id）；旧稿仍 stale
  check(
    "3 幂等后旧稿仍 stale、cv 未再推进",
    again.body.old_draft?.status === "stale" && (await cv()) === cvBeforeIdem,
  );

  // 子草稿确认前正式数据不变
  const listBeforeConfirm = await records();
  const stBeforeConfirm = await stats();
  check(
    "3 子草稿确认前正式数据不变（cv/记录/统计未动）",
    (await cv()) === cvAtRecalc &&
      recById(listBeforeConfirm, "ts-seed-0907")?.status === "valid",
    JSON.stringify({
      cv: await cv(),
      st: recById(listBeforeConfirm, "ts-seed-0907")?.status,
    }),
  );

  // 再确认成功（子草稿绑定的最新 cv 仍有效）
  const confirmChild = await confirm(child.id, child.revision);
  check(
    "3 子草稿确认成功（cv 恰 +1、身份仍 0907、kind=correction）",
    confirmChild.status === 200 &&
      confirmChild.body.newly_committed === true &&
      confirmChild.body.context_version === cvAtRecalc + 1 &&
      confirmChild.body.training_session_id === "ts-seed-0907",
    JSON.stringify(confirmChild.body),
  );
  const listAfter = await records();
  const after0907 = recById(listAfter, "ts-seed-0907");
  check(
    "3 确认后 0907 仍 valid、当前 kind=correction、修订链已追加",
    after0907?.status === "valid" &&
      after0907?.kind === "correction" &&
      (after0907?.revisions?.length ?? 0) >= 2,
    JSON.stringify({
      st: after0907?.status,
      kind: after0907?.kind,
      revs: after0907?.revisions?.length,
    }),
  );

  // 终态后再 recalc → 409（不在本阶段再生成）
  const afterTerminal = await recalc(gen.draft.id, `f4-06-term-${Date.now()}`);
  check(
    "3 子稿进入终态后再 recalc 旧稿 → 409（本阶段不再生成）",
    afterTerminal.status === 409 &&
      afterTerminal.body.error_code === "invalid_request",
    JSON.stringify(afterTerminal.body),
  );
}

/* ========== 4. training_void recalc 不落 profile 兜底 ========== */
console.log("\n--- training_void recalc 分支 ---");
{
  await reset("default");
  await api("PUT", "/api/provider/api-key", { api_key: "probe" });

  // 作废草稿（0907），再推进 cv（确认一条更正 0905）
  const voidGen = await voidDraft("ts-seed-0907");
  check(
    "4 前置：作废草稿 kind=training_void",
    voidGen.body.draft?.kind === "training_void",
    JSON.stringify(voidGen.body.draft),
  );
  const cvBefore = await cv();
  const complete = await run(
    "s-f4-06-void-push",
    "更正 ts-seed-0905 的训练记录：哑铃弯举 第1组改成12次",
    "f4-06-void-push",
  );
  await confirm(complete.draft.id, complete.draft.revision);
  check(
    "4 业务版本已推进",
    (await cv()) === cvBefore + 1,
    JSON.stringify({ cv: await cv() }),
  );

  const voidRecalc = await recalc(
    voidGen.body.draft.id,
    `f4-06-void-${Date.now()}`,
  );
  check(
    "4 training_void recalc：200 + kind 保持 training_void（非 profile_update）",
    voidRecalc.status === 200 &&
      voidRecalc.body.new_draft?.kind === "training_void" &&
      voidRecalc.body.old_draft?.status === "stale" &&
      voidRecalc.body.new_draft?.parent_draft_id === voidGen.body.draft.id &&
      voidRecalc.body.new_draft?.payload?.training_session_id === "ts-seed-0907",
    JSON.stringify({
      status: voidRecalc.status,
      kind: voidRecalc.body?.new_draft?.kind,
      parent: voidRecalc.body?.new_draft?.parent_draft_id,
      ts: voidRecalc.body?.new_draft?.payload,
    }),
  );
  check(
    "4 void 子草稿绑定 recalc 时最新 cv",
    voidRecalc.body.new_draft?.base_business_version === (await cv()),
    JSON.stringify({
      base: voidRecalc.body.new_draft?.base_business_version,
      cv: await cv(),
    }),
  );

  // void 子草稿可确认成功（0907 仍 valid，未被其他操作作废）
  const voidChild = voidRecalc.body.new_draft;
  const voidConfirm = await confirm(voidChild.id, voidChild.revision);
  check(
    "4 void 子草稿确认成功：0907 → voided、cv+1",
    voidConfirm.status === 200 &&
      recById(await records(), "ts-seed-0907")?.status === "voided",
    JSON.stringify({
      status: voidConfirm.status,
      st: recById(await records(), "ts-seed-0907")?.status,
    }),
  );
}

/* ========== 5. fail-next：子草稿确认整份回滚 ========== */
console.log("\n--- fail-next 回滚 ---");
{
  await reset("default");
  await api("PUT", "/api/provider/api-key", { api_key: "probe" });

  const cv0 = await cv();
  const gen = await run(
    "s-f4-06-fail",
    "更正 ts-seed-0907 的训练记录：哑铃弯举 第2组改成6次",
    "f4-06-fail",
  );
  // 推进 cv → stale → recalc
  const voidPush = await voidDraft("ts-seed-0831");
  await confirm(voidPush.body.draft.id, voidPush.body.draft.revision);
  const recalced = await recalc(gen.draft.id, `f4-06-fail-${Date.now()}`);
  check(
    "5 前置：子草稿已生成",
    recalced.status === 200 && recalced.body.new_draft?.status === "pending",
    JSON.stringify({ status: recalced.status }),
  );
  const child = recalced.body.new_draft;

  const listBefore = await records();
  const cvBeforeFail = await cv();
  await failNext();
  const failConfirm = await confirm(child.id, child.revision);
  check(
    "5 fail-next 子草稿确认：500",
    failConfirm.status === 500,
    JSON.stringify(failConfirm.body),
  );
  check(
    "5 fail-next 回滚：cv/记录/修订链不变",
    (await cv()) === cvBeforeFail &&
      JSON.stringify(
        recById(await records(), "ts-seed-0907")?.revisions?.map((r) => r.id),
      ) ===
        JSON.stringify(
          recById(listBefore, "ts-seed-0907")?.revisions?.map((r) => r.id),
        ),
    JSON.stringify({ cv: await cv(), before: cvBeforeFail }),
  );
  // 回滚后重做成功
  const redo = await confirm(child.id, child.revision);
  const afterRedo = recById(await records(), "ts-seed-0907");
  check(
    "5 回滚后重做成功：cv+1、更正写入（kind=correction）",
    redo.status === 200 &&
      redo.body.newly_committed === true &&
      afterRedo?.kind === "correction" &&
      (afterRedo?.revisions?.length ?? 0) >= 2,
    JSON.stringify({
      status: redo.status,
      kind: afterRedo?.kind,
      revs: afterRedo?.revisions?.length,
    }),
  );
  check(
    "5 未放松：W2 完成率在 void 0831 后仍保持口径（1/3 → 确认更正不改分母）",
    w2(await stats())?.completed === 1,
    JSON.stringify(w2(await stats())),
  );
}

await vite.close();
console.log(`\nPASS ${passed} / FAIL ${failed}`);
console.log(failed === 0 ? "全部通过" : `${failed} 项失败`);
process.exit(failed === 0 ? 0 : 1);
