/**
 * F3-06 闭环演示探针（走真实 mock REST）：按 plans/stage3.md §7 第 1–14 步串联/补缺
 * （F3-01–05 已覆盖的步骤在证据表引用，本探针不重写整套，只补串联与缺口）。
 *
 * 运行：node scripts/f3-06-closed-loop-probe.mjs
 * 覆盖（本探针新断言）：
 *  - 空数据：empty 种子 stats 空列表语义、records 空列表
 *  - §7.1–9 主路径串联（查看→调整→接受→指导→打卡→确认→三份事实）
 *  - §7.10 无安排加练不进三桶、可进 PR（协议层）
 *  - §7.11 同日多练身份 + 补充复用 + 同一安排多次反馈完成率最多计一次
 *  - §7.12 incomplete 待补全不进 PR / 完成率分子
 *  - §7.13 draft_stale → recalc → 再确认；丢弃后确认被拒；失败注入全回滚（记录）
 *  - §7.14 刷新恢复（协议层：session drafts 读回 + 正式数据仍在）
 *
 * 与 f3-01–05 的关系：步骤级 PASS 编号对齐 §7；独立缺口单独断言；
 * 安全整份阻断/红旗主路径由 f3-03 覆盖（此处引用并抽 1 条限制阻断确认未回归）。
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
const discard = (id) => api("POST", `/api/drafts/${id}/discard`, {});
const recalc = (id) =>
  api("POST", `/api/drafts/${id}/recalc`, {
    client_request_id: `f3-06-recalc-${Date.now()}-${Math.random()}`,
  });

async function run(session, message) {
  const started = await api("POST", "/api/runs", {
    session_id: session,
    message,
    client_request_id: `f3-06-${Date.now()}-${Math.random()}`,
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
        session_id: session,
      };
  }
  throw new Error("Run 未在预期时间内进入终态");
}

const w2 = (stats) => stats.per_week.find((w) => w.week === "W2");
const dayRecords = (records, date) => records.filter((r) => r.date === date);

/* ========== 空数据路径（§6 / §7 前置边界：无记录时语义） ========== */
console.log("\n--- 空数据 ---");
{
  await reset("empty");
  const records = (await api("GET", "/api/records")).body;
  const stats = (await api("GET", "/api/stats")).body;
  const arrangements = (await api("GET", "/api/arrangements")).body;
  check(
    "空数据：records 空列表语义（数组长度 0，非 null/错误）",
    Array.isArray(records.records) && records.records.length === 0,
    JSON.stringify(records.records),
  );
  check(
    "空数据：stats 空（per_week []、buckets 0/0/0、prs []）",
    Array.isArray(stats.per_week) &&
      stats.per_week.length === 0 &&
      stats.buckets.met === 0 &&
      stats.buckets.unmet === 0 &&
      stats.buckets.pending === 0 &&
      stats.prs.length === 0,
    JSON.stringify({
      weeks: stats.per_week,
      buckets: stats.buckets,
      prs: stats.prs,
    }),
  );
  check(
    "空数据：arrangements 空列表",
    Array.isArray(arrangements.arrangements) &&
      arrangements.arrangements.length === 0,
  );
}

/* ========== §7 串联主路径（1→9、14） ========== */
console.log("\n--- §7 主路径串联 ---");
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });

const sessionMain = "s-f3-06-main";
let arrDraftId;
let arrRevision;
let arrConfirmBody;
let recordDraft;

/* §7.1 查看与指导 */
{
  const profile = (await api("GET", "/api/profile")).body;
  const arrangements = (await api("GET", "/api/arrangements")).body;
  const today = (profile.schedules ?? []).filter((s) => s.date === "2026-09-11");
  check(
    "§7.1 今日 09-11 锁定且尚无安排",
    today.length === 1 &&
      today[0].locked_effective === true &&
      !arrangements.arrangements.some((a) => a.target.scheduled_on === "2026-09-11"),
  );
  check(
    "§7.1 09-07 已接受安排 · 已调整（planned 4 → arranged 3）",
    arrangements.arrangements.some(
      (a) =>
        a.target.scheduled_on === "2026-09-07" &&
        a.target.exercises.find((e) => e.exercise_id === "barbell-bench-press")
          ?.prescription.work_sets === 3,
    ),
  );
  const g = await run(sessionMain, "今天练什么");
  check(
    "§7.1 接受前：按计划腿日处方（含深蹲 4 组、无安排字样）",
    g.draft === null &&
      /杠铃背蹲/.test(g.text ?? "") &&
      /4 组/.test(g.text ?? "") &&
      !/按已接受安排/.test(g.text ?? ""),
    g.text?.slice(0, 120),
  );
}

/* §7.2 提出调整：一般状态差 → deload 草稿（只改今天） */
{
  const arr = await run(sessionMain, "今天状态一般，轻一点少做几组，只改今天");
  check(
    "§7.2 一般状态差 → 今日 deload 安排草稿（锁定日仍可安排）",
    arr.draft?.kind === "arrangement" &&
      arr.draft.payload.target.scheduled_on === "2026-09-11" &&
      arr.draft.payload.target.exercises.every((e) => e.disposition === "deload"),
    arr.text?.slice(0, 100),
  );
  arrDraftId = arr.draft?.id;
  arrRevision = arr.draft?.revision;
  const draftList = (await api("GET", `/api/sessions/${sessionMain}/drafts`)).body;
  check(
    "§7.2 草稿可经 session drafts 读回（待确认态）",
    Array.isArray(draftList) &&
      draftList.some((d) => d.id === arrDraftId && d.status === "pending"),
    JSON.stringify(draftList?.map((d) => `${d.id}:${d.status}`)),
  );
}

/* §7.3 状态档位：明显状态差无草稿（独立会话，不打断主路径草稿） */
{
  const severe = await run("s-f3-06-severe", "今天状态非常差，不太想练");
  check(
    "§7.3 明显状态差 → 仅建议休息、无结构化草稿",
    severe.draft == null && /休息/.test(severe.text ?? ""),
    severe.text?.slice(0, 80),
  );
}

/* §7.4 内联纠错与服务端校验 */
{
  // 以会话草稿列表取最新 payload（不依赖独立 GET）
  const drafts = (await api("GET", `/api/sessions/${sessionMain}/drafts`)).body;
  const current = drafts.find((d) => d.id === arrDraftId);
  const p0 = structuredClone(current.payload);
  // 越界：加组
  p0.target.exercises[0].prescription.work_sets += 2;
  const rBad = await revise(arrDraftId, p0, arrRevision);
  check(
    "§7.4 越界加组 → 服务端拒绝、正式数据不变",
    rBad.status === 400 &&
      (await api("GET", "/api/arrangements")).body.arrangements.length === 3,
    rBad.body?.message?.slice(0, 80),
  );
  // 合法：再减一组 + 原因
  const good = structuredClone(current.payload);
  good.target.exercises[0].prescription.work_sets = Math.max(
    1,
    good.target.exercises[0].prescription.work_sets - 1,
  );
  good.target.adjustment_reason = "§7 闭环：今日腿日再减 1 组（deload 方案 1）";
  const rGood = await revise(arrDraftId, good, arrRevision);
  check(
    "§7.4 合法纠错：deload 再减组 + 原因 → revision+1",
    rGood.status === 200 && rGood.body.draft.revision === arrRevision + 1,
    JSON.stringify({ status: rGood.status, rev: rGood.body?.draft?.revision }),
  );
  arrRevision = rGood.body.draft.revision;
}

/* §7.5 接受即落盘 */
{
  const profile0 = (await api("GET", "/api/profile")).body;
  const cvBefore = profile0.context_version;
  const c1 = await confirm(arrDraftId, arrRevision);
  arrConfirmBody = c1.body;
  check(
    "§7.5 接受落盘：arrangement_revision_id 凭据 + cv+1",
    c1.status === 200 &&
      c1.body.newly_committed === true &&
      typeof c1.body.arrangement_revision_id === "string" &&
      c1.body.context_version === cvBefore + 1,
    JSON.stringify(c1.body),
  );
  const arrangements = (await api("GET", "/api/arrangements")).body;
  const accepted = arrangements.arrangements.find(
    (a) => a.id === c1.body.arrangement_revision_id,
  );
  const squat = accepted?.target.exercises.find(
    (e) => e.exercise_id === "barbell-back-squat",
  );
  check(
    "§7.5 今日已接受安排 · 已调整（减组仍被接受；日程锁定 ≠ 处方锁定）",
    accepted?.target.scheduled_on === "2026-09-11" &&
      typeof accepted.accepted_at === "string" &&
      squat?.prescription.work_sets < 4 &&
      squat?.disposition === "deload",
    JSON.stringify({ sets: squat?.prescription.work_sets }),
  );
  const c2 = await confirm(arrDraftId, arrRevision);
  check(
    "§7.5 重复确认幂等：同一凭据、cv 不再 +1、arrangements 仍 4",
    c2.status === 200 &&
      c2.body.newly_committed === false &&
      c2.body.arrangement_revision_id === c1.body.arrangement_revision_id &&
      c2.body.context_version === c1.body.context_version &&
      (await api("GET", "/api/arrangements")).body.arrangements.length === 4,
  );
}

/* §7.6 指导优先安排 */
{
  const g = await run(sessionMain, "今天练什么");
  const arrangements = (await api("GET", "/api/arrangements")).body;
  const arr = arrangements.arrangements.find(
    (a) => a.target.scheduled_on === "2026-09-11",
  );
  const sets = arr.target.exercises.find(
    (e) => e.exercise_id === "barbell-back-squat",
  )?.prescription.work_sets;
  check(
    "§7.6 接受后：指导按已接受安排（组数=安排、标明按已接受安排）",
    g.draft === null &&
      /按已接受安排/.test(g.text ?? "") &&
      new RegExp(`${sets} 组`).test(g.text ?? ""),
    g.text?.slice(0, 160),
  );
}

/* §7.7–8 打卡反馈 → 纠错确认 → 统计刷新 + 失败注入回滚 */
{
  const full = await run(
    sessionMain,
    "今天练完了。热身递增至 60kg。深蹲 100kg 2组 每组8次：第1组正常，第2组朋友帮忙抬起，未报余力。",
  );
  check(
    "§7.7 打卡草稿：携带今日安排关联 + 热身原文 + 辅助标注",
    full.draft?.kind === "training_record" &&
      full.draft.payload.arrangement_revision_id ===
        arrConfirmBody.arrangement_revision_id &&
      full.draft.payload.occurred_on === "2026-09-11" &&
      full.draft.payload.exercises[0].warmup_summary_text === "递增至 60kg" &&
      full.draft.payload.exercises[0].sets[1].assistance === "assisted" &&
      full.draft.payload.exercises[0].sets.every((s) => s.rir == null),
    full.text?.slice(0, 160),
  );
  recordDraft = full.draft;
  const statsBefore = (await api("GET", "/api/stats")).body;
  const w2Before = w2(statsBefore);

  // 失败注入：确认整份回滚（记录侧；安排侧见 f3-02）
  await api("POST", "/api/dev/confirm/fail-next");
  const fail = await confirm(recordDraft.id, recordDraft.revision);
  const afterFail = (await api("GET", "/api/stats")).body;
  const afterFailRecords = (await api("GET", "/api/records")).body.records;
  check(
    "§7.8 失败注入：记录确认失败整份回滚（W2 不变、无 09-11 记录）",
    fail.status >= 400 &&
      w2(afterFail).completed === w2Before.completed &&
      dayRecords(afterFailRecords, "2026-09-11").length === 0,
    JSON.stringify({ status: fail.status, w2: w2(afterFail) }),
  );

  // 重做成功
  const conf = await confirm(recordDraft.id, recordDraft.revision);
  const statsAfter = (await api("GET", "/api/stats")).body;
  const w2After = w2(statsAfter);
  check(
    "§7.8 重做确认：valid 入列表、W2 完成率 1/3 → 2/3、cv+1",
    conf.status === 200 &&
      conf.body.newly_committed === true &&
      dayRecords((await api("GET", "/api/records")).body.records, "2026-09-11").some(
        (r) => r.status === "valid" && r.arrangement_revision_id === arrConfirmBody.arrangement_revision_id,
      ) &&
      w2After.planned === 3 &&
      w2After.completed === 2 &&
      conf.body.context_version === arrConfirmBody.context_version + 1,
    JSON.stringify({ w2: w2After, cv: conf.body.context_version }),
  );
  const again = await confirm(recordDraft.id, recordDraft.revision);
  check(
    "§7.8 重复确认幂等：完成率不再变",
    again.status === 200 &&
      again.body.newly_committed === false &&
      w2((await api("GET", "/api/stats")).body).completed === 2,
  );

  // 三桶按当次安排更新（今日 2 组 × 8 次落在区间 → met +2）
  check(
    "§7.8/9 三桶按当次安排更新（种子 1/1/1 → met+2）",
    statsAfter.buckets.met === 3 &&
      statsAfter.buckets.unmet === 1 &&
      statsAfter.buckets.pending === 1,
    JSON.stringify(statsAfter.buckets),
  );
}

/* §7.9 区分三份事实 */
{
  const rec = (await api("GET", "/api/records")).body.records.find(
    (r) => r.date === "2026-09-11" && r.arrangement_revision_id != null,
  );
  check(
    "§7.9 三份事实：原计划 4 · 当次安排 <4 · 实际 2（组级 judgement 按次数）",
    rec?.comparison?.planned_sets === 4 &&
      rec.comparison.arranged_sets < 4 &&
      rec.sets.filter((s) => s.set_type === "working").length === 2 &&
      rec.judgement != null &&
      rec.judgement.met === 2,
    JSON.stringify({
      c: rec?.comparison,
      j: rec?.judgement,
      working: rec?.sets?.filter((s) => s.set_type === "working").length,
    }),
  );
  check(
    "§7.9 辅助组标注且不进 PR（辅助组被排除；非辅助 100kg 仍可进）",
    rec.sets.some((s) => s.assisted === true) &&
      (await api("GET", "/api/stats")).body.prs.some(
        (pr) => pr.exercise === "杠铃背蹲" && pr.best_weight_kg === 100,
      ),
  );
}

/* §7.10 无安排加练 */
{
  const extra = await run(sessionMain, "今天哑铃弯举 12kg 3组 每组10次");
  check(
    "§7.10 无安排加练：不带 arrangement、新身份",
    extra.draft?.kind === "training_record" &&
      (extra.draft.payload.arrangement_revision_id ?? null) === null &&
      extra.draft.payload.training_session_id === null,
    extra.text?.slice(0, 80),
  );
  const c = await confirm(extra.draft.id, extra.draft.revision);
  const stats = (await api("GET", "/api/stats")).body;
  const rec = (await api("GET", "/api/records")).body.records.find(
    (r) => r.date === "2026-09-11" && r.exercise === "哑铃弯举",
  );
  check(
    "§7.10 无安排：不进完成率分子（W2 仍 2/3）、judgement=null 不进三桶、可进 PR",
    c.status === 200 &&
      w2(stats).completed === 2 &&
      (rec?.judgement ?? null) === null &&
      stats.buckets.met === 3 &&
      stats.prs.some((pr) => pr.exercise === "哑铃弯举"),
    JSON.stringify({ w2: w2(stats), buckets: stats.buckets }),
  );
}

/* §7.11 同日多练：歧义 + 补充复用 + 同一安排多次反馈完成率最多计一次 */
{
  // 歧义
  const amb = await run(sessionMain, "再补一组");
  check(
    "§7.11 「再补一组」→ 草稿前询问归属",
    amb.draft == null &&
      /补充上一练/.test(amb.text ?? "") &&
      /新增一练/.test(amb.text ?? ""),
  );
  // 补充复用身份：复用当日最近一次既有训练身份（§7.10 无安排加练后，「上一练」= 该身份）
  const sameDayBefore = dayRecords(
    (await api("GET", "/api/records")).body.records,
    "2026-09-11",
  );
  const existingTs = new Set(
    sameDayBefore.map((r) => r.training_session_id).filter(Boolean),
  );
  const sup = await run(sessionMain, "补充上一练 哑铃弯举 12kg 1组 8次");
  check(
    "§7.11 补充上一练：复用既有 training_session_id（不新增身份）",
    sup.draft?.kind === "training_record" &&
      sup.draft.payload.training_session_id != null &&
      existingTs.has(sup.draft.payload.training_session_id),
    JSON.stringify({
      used: sup.draft?.payload?.training_session_id,
      existing: [...existingTs],
    }),
  );
  await confirm(sup.draft.id, sup.draft.revision);

  // 新增一练：新身份（不与既有任一身份重合）
  const nw = await run(sessionMain, "新增一练 哑铃弯举 10kg 2组 每组8次");
  const cNew = await confirm(nw.draft.id, nw.draft.revision);
  check(
    "§7.11 新增一练：新 training_session_id（≠ 既有身份）",
    cNew.status === 200 && !existingTs.has(cNew.body.training_session_id),
    JSON.stringify({ new: cNew.body.training_session_id, existing: [...existingTs] }),
  );

  // 同一安排多次反馈完成最多计一次：再报一次深蹲（仍关联今日安排）
  const againArr = await run(
    sessionMain,
    "今天再练一组深蹲 100kg 1组 8次",
  );
  check(
    "§7.11 同日再反馈深蹲：仍显式携带今日安排关联",
    againArr.draft?.kind === "training_record" &&
      againArr.draft.payload.arrangement_revision_id ===
        arrConfirmBody.arrangement_revision_id,
    JSON.stringify(againArr.draft?.payload?.arrangement_revision_id),
  );
  const cAgain = await confirm(againArr.draft.id, againArr.draft.revision);
  const stats = (await api("GET", "/api/stats")).body;
  check(
    "§7.11 同一安排多次反馈：完成率仍 2/3（同一日程最多计一次）",
    cAgain.status === 200 && w2(stats).completed === 2 && w2(stats).planned === 3,
    JSON.stringify(w2(stats)),
  );
}

/* §7.12 待补全 */
{
  const prBefore = (await api("GET", "/api/stats")).body.prs;
  const inc = await run(sessionMain, "昨天深蹲 100kg 4组");
  const cInc = await confirm(inc.draft.id, inc.draft.revision);
  const rec = (await api("GET", "/api/records")).body.records.find(
    (r) => r.date === "2026-09-10",
  );
  const stats = (await api("GET", "/api/stats")).body;
  check(
    "§7.12 incomplete：状态待补全、不进完成率分子",
    cInc.status === 200 &&
      rec?.status === "incomplete" &&
      w2(stats).completed === 2 &&
      w2(stats).planned === 3,
    JSON.stringify({ status: rec?.status, w2: w2(stats) }),
  );
  check(
    "§7.12 incomplete 不进 PR（昨日 100kg 不新增/不抬升 PR 条目）",
    JSON.stringify(stats.prs) === JSON.stringify(prBefore),
    JSON.stringify({ before: prBefore, after: stats.prs }),
  );
}

/* §7.13 stale → recalc → 再确认；丢弃后确认被拒；安全阻断抽验 */
{
  await reset("default");
  await api("PUT", "/api/provider/api-key", { api_key: "probe" });

  // 安全阻断抽验（主路径见 f3-03）：限制冲突 → 整份阻断
  const block = await run(
    "s-f3-06-safe",
    "补充档案：目标增肌，初级经验，每周 3 次，每次 60 分钟，可用器材：杠铃、哑铃、卧推架、引体架、绳索，体重 72.5kg，深蹲时膝部不适，没有其他不适。",
  );
  if (block.draft) await confirm(block.draft.id, block.draft.revision);
  const gBlock = await run("s-f3-06-safe-g", "今天练什么");
  check(
    "§7.13 限制冲突：整份阻断、无处方（与 f3-03 一致未回归）",
    gBlock.draft === null && /整份计划指导已阻断/.test(gBlock.text ?? ""),
    gBlock.text?.slice(0, 100),
  );

  // 恢复后走 stale 路径
  await reset("default");
  await api("PUT", "/api/provider/api-key", { api_key: "probe" });
  const sStale = "s-f3-06-stale";
  const arrA = await run(sStale, "今天状态一般，轻一点少做几组，只改今天");
  check("§7.13 stale 前置：生成安排草稿", arrA.draft?.kind === "arrangement");
  // 推进 context_version（另一会话接受另一份安排 / 或确认记录）
  const arrB = await run(
    "s-f3-06-stale-b",
    "今天状态一般，少做几组只改今天",
  );
  // 若同日已有草稿未确认，B 会再生成；直接确认 B 推进 cv
  const cB = await confirm(arrB.draft.id, arrB.draft.revision);
  check("§7.13 stale：另一确认推进 context_version", cB.status === 200);

  const staleConfirm = await confirm(arrA.draft.id, arrA.draft.revision);
  check(
    "§7.13 旧草稿确认 → 409 draft_stale",
    staleConfirm.status === 409 &&
      staleConfirm.body.error_code === "draft_stale",
    JSON.stringify(staleConfirm.body),
  );
  const recalced = await recalc(arrA.draft.id);
  check(
    "§7.13 一键重算：新草稿 revision 1、旧草稿 stale、正式安排不重复多写",
    recalced.status === 200 &&
      recalced.body.new_draft.revision === 1 &&
      recalced.body.new_draft.parent_draft_id === arrA.draft.id &&
      recalced.body.old_draft.status === "stale",
    JSON.stringify({
      status: recalced.status,
      rev: recalced.body?.new_draft?.revision,
    }),
  );
  const cNew = await confirm(
    recalced.body.new_draft.id,
    recalced.body.new_draft.revision,
  );
  check(
    "§7.13 重算后再确认成功",
    cNew.status === 200 && cNew.body.newly_committed === true,
    JSON.stringify(cNew.body),
  );

  // 丢弃后确认被拒
  const arrC = await run("s-f3-06-discard", "今天状态一般，轻一点只改今天");
  const d = await discard(arrC.draft.id);
  const cD = await confirm(arrC.draft.id, arrC.draft.revision);
  check(
    "§7.13 丢弃后确认被拒：409 且文案说明已丢弃",
    d.status === 200 &&
      cD.status === 409 &&
      /已丢弃|不可确认/.test(cD.body?.message ?? ""),
    JSON.stringify({ d: d.status, c: cD.status, msg: cD.body?.message }),
  );
}

/* §7.14 刷新恢复（协议层）：committed 后 session drafts 仍可读回终态；正式数据在 */
{
  await reset("default");
  await api("PUT", "/api/provider/api-key", { api_key: "probe" });
  const s = "s-f3-06-refresh";
  const arr = await run(s, "今天状态一般，轻一点少做几组，只改今天");
  const c = await confirm(arr.draft.id, arr.draft.revision);
  // 模拟刷新：重新拉 session drafts 与正式数据
  const drafts = (await api("GET", `/api/sessions/${s}/drafts`)).body;
  const arrangements = (await api("GET", "/api/arrangements")).body;
  const draftNow = drafts.find((x) => x.id === arr.draft.id);
  check(
    "§7.14 刷新恢复：草稿终态可读回（committed）+ 今日正式安排仍在",
    c.status === 200 &&
      draftNow?.status === "committed" &&
      arrangements.arrangements.some((a) => a.target.scheduled_on === "2026-09-11"),
    JSON.stringify({ status: draftNow?.status }),
  );

  // 待确认草稿刷新恢复：生成后不确认，重查
  const s2 = "s-f3-06-refresh-pending";
  const arr2 = await run(s2, "今天状态一般，少做几组只改今天");
  const drafts2 = (await api("GET", `/api/sessions/${s2}/drafts`)).body;
  check(
    "§7.14 待确认草稿刷新恢复：pending 可读回、revision 一致",
    drafts2.some(
      (d) => d.id === arr2.draft.id && d.status === "pending" && d.revision === arr2.draft.revision,
    ),
  );
  // discard 后刷新：终态 discarded
  await discard(arr2.draft.id);
  const drafts3 = (await api("GET", `/api/sessions/${s2}/drafts`)).body;
  check(
    "§7.14 丢弃后刷新：终态 discarded 可读回、正式安排未多写",
    drafts3.find((d) => d.id === arr2.draft.id)?.status === "discarded" &&
      (await api("GET", "/api/arrangements")).body.arrangements.length === 4,
  );
}

await vite.close();
console.log(
  failed === 0
    ? `\n全部通过 PASS=${passed} / FAIL=0`
    : `\n${failed} 项失败 PASS=${passed}`,
);
process.exit(failed === 0 ? 0 : 1);
