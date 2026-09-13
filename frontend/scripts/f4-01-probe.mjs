/**
 * F4-01 验收探针（走真实 mock REST）：契约与 mock 修订模型收口
 * （plans/stage4.md F4-01 / 05 5.3）。
 *
 * 运行：node scripts/f4-01-probe.mjs
 * 覆盖：
 *  - 种子：09-05 incomplete（缺每组次数派生）、12kg+working、无对照、revision_note 保留
 *  - 种子基线：W1 2/3、W2 1/3、三桶 1/1/1、PR 卧推 80kg×8、09-07 judgement 1/1/1
 *  - 契约/存储：records = 当前修订投影（每身份一条）；每条 revisions 含当前
 *  - 作废事务：dev 桥作废 09-07 → voided、旧修订仍在、W2 0/3、三桶 0/0/0、PR 不变、cv+1
 *  - 幂等：重复确认 newly_committed=false、cv 不再 +1
 *  - fail-next 作废确认：500 且记录/cv 回滚；重做成功
 *  - 终态（F4-02 合法路径）：作废前先生成指向该身份的更正草稿 → 作废推进 cv →
 *    旧稿 409 draft_stale → recalc 刷新基线 → 子草稿确认命中 409 invalid_request 终态；
 *    再作废 → 409；修订链不变、cv 不推进
 *    （旧路径 revise 改 training_session_id 已被 F4-02 身份不可改写护栏拒绝，不再使用）
 *  - recalc：缺 client_request_id → 400；带则通过 body 校验；过期后完整重算
 *  - 旧修订不进统计（并入作废后断言）
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

/* ========== 1–2. 种子基线 ========== */
console.log("\n--- 种子基线 ---");
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });

{
  const list = await records();
  const r0905 = recById(list, "ts-seed-0905");
  check(
    "种子：09-05 incomplete（由缺每组次数派生，非字面量）",
    r0905?.status === "incomplete" && r0905.date === "2026-09-05",
    JSON.stringify({ status: r0905?.status, date: r0905?.date }),
  );
  check(
    "种子：09-05 保留 12kg+working、无每组次数、无对照",
    r0905?.sets?.length === 1 &&
      r0905.sets[0].weight_kg === 12 &&
      r0905.sets[0].set_type === "working" &&
      r0905.sets[0].reps == null &&
      r0905.arrangement_revision_id == null &&
      r0905.scheduled_session_id == null,
    JSON.stringify(r0905?.sets),
  );
  check(
    "种子：09-05 revision_note 保留",
    r0905?.revision_note === "无安排加练，待补全",
    r0905?.revision_note,
  );

  const s0 = await stats();
  const w1 = s0.per_week.find((w) => w.week === "W1");
  const w2a = w2(s0);
  check(
    "种子：W1 2/3、W2 1/3",
    w1?.planned === 3 &&
      w1.completed === 2 &&
      w2a?.planned === 3 &&
      w2a.completed === 1,
    JSON.stringify({ w1, w2: w2a }),
  );
  check(
    "种子：三桶 1/1/1",
    s0.buckets.met === 1 && s0.buckets.unmet === 1 && s0.buckets.pending === 1,
    JSON.stringify(s0.buckets),
  );
  check(
    "种子：PR 卧推 80kg×8（仅此一条）",
    s0.prs.length === 1 &&
      s0.prs[0].exercise === "杠铃平板卧推" &&
      s0.prs[0].best_weight_kg === 80 &&
      s0.prs[0].best_reps_at_weight === 8,
    JSON.stringify(s0.prs),
  );
  const r0907 = recById(list, "ts-seed-0907");
  check(
    "种子：09-07 judgement 1/1/1、valid",
    r0907?.status === "valid" &&
      r0907.judgement?.met === 1 &&
      r0907.judgement.unmet === 1 &&
      r0907.judgement.pending === 1,
    JSON.stringify({ status: r0907?.status, j: r0907?.judgement }),
  );
}

/* ========== 3. 契约/存储：当前修订投影 + revisions ========== */
console.log("\n--- 契约/存储 ---");
{
  const list = await records();
  const ids = list.map((r) => r.training_session_id);
  check(
    "records 为当前修订投影：每身份恰好一条（4 条）",
    list.length === 4 && new Set(ids).size === 4,
    JSON.stringify(ids),
  );
  check(
    "每条 records 含 revisions 且至少含自身当前修订",
    list.every(
      (r) =>
        Array.isArray(r.revisions) &&
        r.revisions.length >= 1 &&
        r.revisions.some(
          (rev) => rev.status === r.status && rev.occurred_on === r.date,
        ),
    ),
    JSON.stringify(
      list.map((r) => ({
        id: r.training_session_id,
        revCount: r.revisions?.length,
      })),
    ),
  );
  check(
    "契约静态：RecalcRequest 收口 {client_request_id}",
    /export interface RecalcRequest \{[\s\S]*?client_request_id: string;/.test(
      readFileSync(path.join(root, "src/lib/contract.ts"), "utf8"),
    ),
  );
}

/* ========== 4–7. 作废事务 / 幂等 / fail-next / 终态 ========== */
console.log("\n--- 作废事务 ---");
{
  const cvBefore = await cv();

  // 终态前置（F4-02 合法路径）：先在身份仍 valid 时生成指向 ts-seed-0907 的更正草稿；
  // 旧路径「revise 改 training_session_id」已被 F4-02 身份不可改写护栏拒绝。
  const correction = await run(
    "s-f4-01-terminal-pre",
    "更正 2026-09-07 的训练记录：杠铃平板卧推 第2组改成6次",
    "f4-01-terminal-pre",
  );
  check(
    "终态前置：作废前生成指向 ts-seed-0907 的更正草稿",
    correction.draft?.kind === "training_record" &&
      correction.draft.status === "pending" &&
      correction.draft.payload?.training_session_id === "ts-seed-0907",
    JSON.stringify({
      kind: correction.draft?.kind,
      ts: correction.draft?.payload?.training_session_id,
    }),
  );
  const termDraft = correction.draft;

  const voidRes = await voidDraft("ts-seed-0907");
  check(
    "dev 桥：为 ts-seed-0907 生成 training_void 待确认草稿",
    voidRes.status === 200 &&
      voidRes.body.draft?.kind === "training_void" &&
      voidRes.body.draft.status === "pending",
    JSON.stringify({ status: voidRes.status, draft: voidRes.body?.draft?.id }),
  );
  const draft = voidRes.body.draft;

  // 6) fail-next：500 且整份回滚
  await failNext();
  const failedConfirm = await confirm(draft.id, draft.revision);
  const listAfterFail = await records();
  const statsAfterFail = await stats();
  const cvAfterFail = await cv();
  check(
    "fail-next 作废确认：500",
    failedConfirm.status === 500,
    JSON.stringify(failedConfirm.body),
  );
  check(
    "fail-next 回滚：记录仍 valid、W2 仍 1/3、cv 不变",
    recById(listAfterFail, "ts-seed-0907")?.status === "valid" &&
      w2(statsAfterFail)?.completed === 1 &&
      cvAfterFail === cvBefore,
    JSON.stringify({
      status: recById(listAfterFail, "ts-seed-0907")?.status,
      w2: w2(statsAfterFail),
      cv: cvAfterFail,
    }),
  );

  // 重做成功（4）
  const voidOk = await confirm(draft.id, draft.revision);
  check(
    "重做作废确认成功：training_session_id + cv 恰好 +1",
    voidOk.status === 200 &&
      voidOk.body.newly_committed === true &&
      voidOk.body.training_session_id === "ts-seed-0907" &&
      voidOk.body.context_version === cvBefore + 1,
    JSON.stringify(voidOk.body),
  );

  const list = await records();
  const s1 = await stats();
  const r0907 = recById(list, "ts-seed-0907");
  check(
    "作废后：当前修订 status=voided、revision_note 标注作废",
    r0907?.status === "voided" &&
      typeof r0907.revision_note === "string" &&
      /作废/.test(r0907.revision_note),
    JSON.stringify({ status: r0907?.status, note: r0907?.revision_note }),
  );
  check(
    "作废后：旧修订仍在 revisions（含 valid 历史）",
    Array.isArray(r0907?.revisions) &&
      r0907.revisions.length >= 2 &&
      r0907.revisions.some((rev) => rev.status === "valid") &&
      r0907.revisions.some((rev) => rev.status === "voided"),
    JSON.stringify(r0907?.revisions?.map((rev) => rev.status)),
  );
  check(
    "作废后：W2 0/3（作废退出完成率分子，分母不变）",
    w2(s1)?.planned === 3 && w2(s1)?.completed === 0,
    JSON.stringify(w2(s1)),
  );
  check(
    "作废后：三桶 0/0/0（旧修订不进统计）",
    s1.buckets.met === 0 && s1.buckets.unmet === 0 && s1.buckets.pending === 0,
    JSON.stringify(s1.buckets),
  );
  check(
    "作废后：PR 不变（该次非 PR 来源，80kg×8 来自 08-31）",
    s1.prs.length === 1 &&
      s1.prs[0].exercise === "杠铃平板卧推" &&
      s1.prs[0].best_weight_kg === 80 &&
      s1.prs[0].best_reps_at_weight === 8,
    JSON.stringify(s1.prs),
  );

  // 5) 幂等
  const again = await confirm(draft.id, draft.revision);
  check(
    "重复确认作废：newly_committed=false、cv 不再 +1",
    again.status === 200 &&
      again.body.newly_committed === false &&
      again.body.context_version === voidOk.body.context_version &&
      (await cv()) === voidOk.body.context_version,
    JSON.stringify({
      newly: again.body.newly_committed,
      cv: again.body.context_version,
    }),
  );

  // 7) 终态：voided 后对仍指向该身份的更正草稿确认 → 409
  // 合法路径：作废推进 cv 后旧稿 stale → recalc 刷新基线 → 子草稿确认命中终态
  const staleConfirm = await confirm(termDraft.id, termDraft.revision);
  check(
    "终态路径：作废后直接确认旧更正稿 → 409 draft_stale（cv 已推进）",
    staleConfirm.status === 409 &&
      staleConfirm.body.error_code === "draft_stale",
    JSON.stringify(staleConfirm.body),
  );

  const recalced = await api("POST", `/api/drafts/${termDraft.id}/recalc`, {
    client_request_id: `f4-01-terminal-recalc-${Date.now()}`,
  });
  check(
    "终态路径：recalc 生成子草稿（payload 仍指向 ts-seed-0907）",
    recalced.status === 200 &&
      recalced.body.new_draft?.status === "pending" &&
      recalced.body.new_draft.payload?.training_session_id === "ts-seed-0907" &&
      recalced.body.old_draft.status === "stale",
    JSON.stringify({
      status: recalced.status,
      ts: recalced.body?.new_draft?.payload?.training_session_id,
      old: recalced.body?.old_draft?.status,
    }),
  );
  const termChild = recalced.body.new_draft;
  const terminalConfirm = await confirm(termChild.id, termChild.revision);
  check(
    "终态：voided 后 training_record 确认 → 409 invalid_request",
    terminalConfirm.status === 409 &&
      terminalConfirm.body.error_code === "invalid_request" &&
      /终态/.test(terminalConfirm.body.message ?? ""),
    JSON.stringify(terminalConfirm.body),
  );

  const reVoid = await voidDraft("ts-seed-0907");
  check(
    "终态：对已作废身份再发作废 → 409 invalid_request",
    reVoid.status === 409 && reVoid.body.error_code === "invalid_request",
    JSON.stringify(reVoid.body),
  );
  const listAfterReVoid = await records();
  const chainAfterReject = recById(listAfterReVoid, "ts-seed-0907")?.revisions?.map(
    (r) => r.id,
  );
  const chainSame =
    JSON.stringify(chainAfterReject) ===
    JSON.stringify(r0907.revisions.map((r) => r.id));
  check(
    "终态拒绝后：修订链不变、cv 不变",
    chainSame && (await cv()) === voidOk.body.context_version,
    JSON.stringify({
      before: r0907.revisions.map((r) => r.id),
      after: chainAfterReject,
    }),
  );
}

/* ========== 8. recalc 请求体收口 ========== */
console.log("\n--- recalc body ---");
{
  await reset("default");
  await api("PUT", "/api/provider/api-key", { api_key: "probe" });

  // 用 training_record 草稿（走 training_record 重算分支，语义干净）
  const checkin = await run(
    "s-f4-01-recalc",
    "昨天弯举 12kg 3组 每组10次",
    "f4-01-recalc",
  );
  check(
    "recalc 前置：生成 training_record 草稿",
    checkin.draft?.kind === "training_record",
    checkin.draft?.kind,
  );
  const draftId = checkin.draft.id;

  const missing = await api("POST", `/api/drafts/${draftId}/recalc`, {});
  check(
    "recalc 缺 client_request_id → 400 invalid_request",
    missing.status === 400 &&
      missing.body.error_code === "invalid_request" &&
      /client_request_id/.test(missing.body.message ?? ""),
    JSON.stringify(missing.body),
  );

  const withKey = await api("POST", `/api/drafts/${draftId}/recalc`, {
    client_request_id: `f4-01-recalc-${Date.now()}`,
  });
  // body 校验通过：不再因缺 client_request_id 而 400；
  // 草稿仍基于最新上下文 → 409 无需重算（证明已过 body 门）
  check(
    "recalc 带 client_request_id：通过 body 校验（不再 400 缺字段）",
    !(
      withKey.status === 400 &&
      /client_request_id/.test(withKey.body?.message ?? "")
    ),
    JSON.stringify(withKey.body),
  );

  // 完整链路：推进 cv 后重算，确认 RecalcRequest 全语义
  const voidOther = await voidDraft("ts-seed-0907");
  await confirm(voidOther.body.draft.id, voidOther.body.draft.revision);
  const recalced = await api("POST", `/api/drafts/${draftId}/recalc`, {
    client_request_id: `f4-01-recalc-full-${Date.now()}`,
  });
  check(
    "recalc 过期草稿：200 + 子草稿 parent_draft_id + 旧稿 stale",
    recalced.status === 200 &&
      recalced.body.new_draft?.status === "pending" &&
      recalced.body.new_draft.revision === 1 &&
      recalced.body.new_draft.parent_draft_id === draftId &&
      recalced.body.new_draft.kind === "training_record" &&
      recalced.body.old_draft.status === "stale",
    JSON.stringify({
      status: recalced.status,
      kind: recalced.body?.new_draft?.kind,
      parent: recalced.body?.new_draft?.parent_draft_id,
      old: recalced.body?.old_draft?.status,
    }),
  );
}

await vite.close();
console.log(`\nPASS ${passed} / FAIL ${failed}`);
console.log(failed === 0 ? "全部通过" : `${failed} 项失败`);
process.exit(failed === 0 ? 0 : 1);
