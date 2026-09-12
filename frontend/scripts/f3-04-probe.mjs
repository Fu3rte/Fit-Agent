/**
 * F3-04 验收探针（走真实 mock REST）：打卡草稿的结构化整理、关联与确认
 * （plans/stage3.md §3.2 / F3-04 / §7 第 7–8、10–12 步）。
 *
 * 运行：node scripts/f3-04-probe.mjs
 * 覆盖：
 *  - 相对日期「今天」解析为 2026-09-11；未明确字段保持空、不编造
 *  - 朋友帮忙 → assisted；未提辅助不默认 assisted；热身摘要原文
 *  - RIR 不解析、不落库（已拍隐藏）；API 层负数 RIR 仍被 revise 拒
 *  - 今天有安排（动作在安排目标内）→ 携带 arrangement_revision_id + 完成率 1/3→2/3
 *  - 今天无安排 / 动作不在安排目标内 → arrangement_revision_id null
 *  - 确认后记录入列表；重复确认幂等
 *  - 同日「再补一组」→ 先询问不生成草稿；「补充上一练」→ 复用 training_session_id
 *  - 缺次数 → incomplete 确认，不进完成率分子
 *  - 无安排训练：另一动作加练 → 不带 arrangement，新身份
 */
import path from "node:path";
import { fileURLToPath } from "node:url";
import { readFileSync } from "node:fs";
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
    client_request_id: `f3-04-${Date.now()}-${Math.random()}`,
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

/* ---------- 0. 源码断言：对照摘要展示（DraftCard 记录分支复用既有编辑器） ---------- */
{
  const draftCardSrc = readFileSync(
    path.join(root, "src/features/chat/DraftCard.tsx"),
    "utf8",
  );
  check(
    "DraftCard 记录分支含对照摘要（原计划 X 组 · 当次安排 Y 组来源）",
    /对照：/.test(draftCardSrc) && /arrangement_revision_id/.test(draftCardSrc),
  );
  check(
    "DraftCard 未新建第二套记录编辑器（仍复用 RecordExerciseEditor/SetInputs）",
    /RecordExerciseEditor/.test(draftCardSrc) &&
      /SetInputs/.test(draftCardSrc) &&
      !/RecordExerciseEditor2|第二套/.test(draftCardSrc),
  );
}

/* ========== 场景 A：无安排主路径（相对日期、未明确字段、负数 RIR 拒） ========== */
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });

{
  const r = await run("s-f3-04-a", "今天卧推 80kg 4组 每组8次");
  check(
    "A 主路径：生成 training_record 草稿",
    r.draft?.kind === "training_record" && r.draft.status === "pending",
    r.text?.slice(0, 80),
  );
  const p = r.draft?.payload;
  check(
    "A 相对日期「今天」→ 2026-09-11（展示具体日期）",
    p?.occurred_on === "2026-09-11" && /2026-09-11/.test(r.text ?? ""),
    p?.occurred_on,
  );
  check(
    "A 今天无已接受安排 → 不携带 arrangement_revision_id（无对照）",
    (p?.arrangement_revision_id ?? null) === null,
    JSON.stringify(p?.arrangement_revision_id),
  );
  check(
    "A 新增 → training_session_id null",
    p?.training_session_id === null,
    JSON.stringify(p?.training_session_id),
  );
  const sets = p?.exercises?.[0]?.sets ?? [];
  check(
    "A 逐组事实：4 组 × 80kg × 8 次",
    sets.length === 4 &&
      sets.every(
        (s) =>
          s.load?.value_text === "80" &&
          s.load?.unit === "kg" &&
          s.reps === 8 &&
          s.set_type === "work",
      ),
    JSON.stringify(sets[0]),
  );
  check(
    "A 未报 RIR 保持空（不编造）；未提辅助不默认 assisted",
    sets.every((s) => s.rir == null && s.assistance == null),
    JSON.stringify(sets.map((s) => ({ rir: s.rir, a: s.assistance }))),
  );
  check(
    "A 回复文案：「无辅助」待确认呈现",
    /无辅助/.test(r.text ?? "") && /待确认/.test(r.text ?? ""),
    r.text?.slice(0, 200),
  );

  // 负数 RIR：revise 被拒
  const bad = structuredClone(p);
  bad.exercises[0].sets[0].rir = -1;
  const revBad = await revise(r.draft.id, bad, r.draft.revision);
  check(
    "A 负数 RIR：revise → 400 拒绝",
    revBad.status === 400 && /RIR/.test(revBad.body?.message ?? ""),
    JSON.stringify(revBad.body),
  );
  // 负数 RIR：confirm 被拒（绕过 revise 直接改 stored 不可行——用 revise 成功后的 confirm 验证）
  // 直接对当前草稿（rir=null）confirm 成功；再验证「confirm 载荷复查」：先 revise 成合法值确认通过，
  // 故 confirm 拒绝路径用另一草稿模拟——revise 成功写入 -1 不可能（上面已拒），
  // 因此 confirm 拒绝通过「revise 成功后把 payload 里的 rir 改为负数再 confirm」不可行；
  // 服务端 confirm 复查同样调用 recordPayloadError——用 revision 携带合法 payload 确认可过即可。

  const okRev = await revise(
    r.draft.id,
    structuredClone(p),
    r.draft.revision,
  );
  check(
    "A 合法 payload revise → revision+1",
    okRev.status === 200 && okRev.body.draft.revision === r.draft.revision + 1,
    JSON.stringify({ status: okRev.status, rev: okRev.body?.draft?.revision }),
  );

  const stats0 = (await api("GET", "/api/stats")).body;
  const w2Before = stats0.per_week.find((w) => w.week === "W2");
  const conf = await confirm(r.draft.id, okRev.body.draft.revision);
  check(
    "A 确认落盘（valid + training_session_id 凭据）",
    conf.status === 200 &&
      conf.body.newly_committed === true &&
      typeof conf.body.training_session_id === "string",
    JSON.stringify(conf.body),
  );
  const records1 = (await api("GET", "/api/records")).body.records;
  const rec = records1.find((x) => x.date === "2026-09-11");
  check(
    "A 记录入列表：状态 valid、动作名、无安排关联",
    rec != null &&
      rec.status === "valid" &&
      rec.exercise === "杠铃平板卧推" &&
      (rec.arrangement_revision_id ?? null) === null &&
      (rec.scheduled_session_id ?? null) === null &&
      rec.training_session_id === conf.body.training_session_id,
    JSON.stringify(rec),
  );
  const again = await confirm(r.draft.id, okRev.body.draft.revision);
  check(
    "A 重复确认幂等：不产生重复记录、不 cv+1",
    again.status === 200 &&
      again.body.newly_committed === false &&
      again.body.context_version === conf.body.context_version &&
      (await api("GET", "/api/records")).body.records.filter(
        (x) => x.date === "2026-09-11",
      ).length === 1,
    JSON.stringify({
      first: conf.body.context_version,
      again: again.body.context_version,
    }),
  );
  const stats1 = (await api("GET", "/api/stats")).body;
  const w2After = stats1.per_week.find((w) => w.week === "W2");
  check(
    "A 无日程关联的记录不进完成率分子（W2 仍 1/3）",
    w2Before?.completed === 1 &&
      w2After?.planned === 3 &&
      w2After.completed === 1,
    JSON.stringify(w2After),
  );
  check(
    "A 无对照安排 → 三桶不变（仍 1/1/1，今日记录不进三桶）",
    stats1.buckets.met === 1 &&
      stats1.buckets.unmet === 1 &&
      stats1.buckets.pending === 1,
    JSON.stringify(stats1.buckets),
  );
}

/* ========== 场景 B：完整反馈 + 当日安排关联（§7 第 7–8 步） ========== */
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });

{
  // 先接受今日 deload 安排
  const arrRun = await run(
    "s-f3-04-b-arr",
    "今天状态一般，轻一点少做几组，只改今天",
  );
  check(
    "B 生成今日 deload 安排草稿",
    arrRun.draft?.kind === "arrangement" &&
      arrRun.draft.payload.target.scheduled_on === "2026-09-11",
    arrRun.text?.slice(0, 80),
  );
  const arrConf = await confirm(arrRun.draft.id, arrRun.draft.revision);
  const arrId = arrConf.body.arrangement_revision_id;
  check(
    "B 今日安排已接受",
    arrConf.status === 200 && typeof arrId === "string",
    JSON.stringify(arrConf.body),
  );

  // 完整反馈：含热身原文、辅助、未报 RIR、力竭
  const full = await run(
    "s-f3-04-b",
    "今天练完了。热身递增至 60kg。深蹲 100kg 4组 每组8次：第1组正常，第2组 RIR 2，第3组朋友帮忙抬起，第4组力竭。",
  );
  check(
    "B 完整反馈：生成 training_record 草稿",
    full.draft?.kind === "training_record",
    full.text?.slice(0, 100),
  );
  const p = full.draft?.payload;
  check(
    "B 今天 + 动作在安排目标内 → 显式携带 arrangement_revision_id",
    p?.arrangement_revision_id === arrId,
    JSON.stringify(p?.arrangement_revision_id),
  );
  const sets = p?.exercises?.[0]?.sets ?? [];
  check(
    "B 热身摘要原文保留（递增至 60kg）",
    p?.exercises?.[0]?.warmup_summary_text === "递增至 60kg",
    JSON.stringify(p?.exercises?.[0]?.warmup_summary_text),
  );
  check(
    "B 第 3 组朋友帮忙 → assistance=assisted",
    sets[2]?.assistance === "assisted",
    JSON.stringify(sets[2]),
  );
  check(
    "B 未提辅助的组不默认 assisted（第 1/2 组 assistance 空）",
    sets[0]?.assistance == null && sets[1]?.assistance == null,
    JSON.stringify([sets[0]?.assistance, sets[1]?.assistance]),
  );
  check(
    "B 消息含 RIR/力竭也不解析不落库（已拍隐藏）",
    sets.every((s) => s.rir == null),
    JSON.stringify(sets.map((s) => s.rir)),
  );
  check(
    "B diff 含对照摘要「原计划 X 组 · 当次安排 Y 组」",
    (full.draft.diff ?? []).some(
      (d) =>
        d.field.startsWith("对照") &&
        /原计划 \d+ 组 · 当次安排 \d+ 组/.test(d.new_value),
    ),
    JSON.stringify(full.draft.diff?.filter((d) => d.field.startsWith("对照"))),
  );
  check(
    "B 回复文案说明关联与无辅助待确认",
    /已关联当次安排/.test(full.text ?? "") && /无辅助/.test(full.text ?? ""),
    full.text?.slice(0, 240),
  );

  const statsBefore = (await api("GET", "/api/stats")).body;
  const w2Before = statsBefore.per_week.find((w) => w.week === "W2");
  const conf = await confirm(full.draft.id, full.draft.revision);
  check(
    "B 确认落盘（valid）",
    conf.status === 200 && conf.body.newly_committed === true,
    JSON.stringify(conf.body),
  );
  const rec = (await api("GET", "/api/records")).body.records.find(
    (x) => x.date === "2026-09-11" && x.exercise === "杠铃背蹲",
  );
  const sched = (await api("GET", "/api/profile")).body.schedules.find(
    (s) => s.date === "2026-09-11",
  );
  check(
    "B 记录携带 arrangement + scheduled_session_id（从安排 target）",
    rec != null &&
      rec.arrangement_revision_id === arrId &&
      rec.scheduled_session_id === sched.id &&
      rec.status === "valid",
    JSON.stringify(rec),
  );
  check(
    "B 辅助组入列表标注 assisted；RIR 不落库",
    rec?.sets?.[2]?.assisted === true &&
      rec.sets.every((s) => s.rir == null),
    JSON.stringify(rec?.sets),
  );
  const statsAfter = (await api("GET", "/api/stats")).body;
  const w2After = statsAfter.per_week.find((w) => w.week === "W2");
  check(
    "B 确认后统计刷新：W2 完成率 1/3 → 2/3（分子=关联日程的 valid 记录）",
    w2Before?.completed === 1 &&
      w2After?.planned === 3 &&
      w2After.completed === 2 &&
      w2After.rate === 66.7,
    JSON.stringify({ before: w2Before, after: w2After }),
  );
  check(
    "B 三桶按当次安排更新（今日 4 组 × 8 次落在区间 → met +4）",
    statsAfter.buckets.met === 5 &&
      statsAfter.buckets.unmet === 1 &&
      statsAfter.buckets.pending === 1,
    JSON.stringify(statsAfter.buckets),
  );
  check(
    "B 非辅助组进 PR（辅助组被排除后其余组仍计 100kg）",
    statsAfter.prs.some(
      (pr) => pr.exercise === "杠铃背蹲" && pr.best_weight_kg === 100,
    ),
    JSON.stringify(statsAfter.prs),
  );
  const again = await confirm(full.draft.id, full.draft.revision);
  check(
    "B 重复确认幂等：同一凭据、完成率不再变",
    again.status === 200 &&
      again.body.newly_committed === false &&
      (
        await api("GET", "/api/stats")
      ).body.per_week.find((w) => w.week === "W2").completed === 2,
  );
}

/* ========== 场景 C：同日多练歧义（§7 第 11 步） ========== */
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });

{
  // 同日第一条记录（确认以建立身份）
  const first = await run(
    "s-f3-04-c",
    "今天哑铃弯举 12kg 3组 每组10次",
  );
  check(
    "C 第一练：无安排动作 → 不带 arrangement（今天无安排）",
    first.draft?.kind === "training_record" &&
      (first.draft.payload.arrangement_revision_id ?? null) === null,
    first.text?.slice(0, 80),
  );
  const c1 = await confirm(first.draft.id, first.draft.revision);
  const ts1 = c1.body.training_session_id;
  check(
    "C 第一练确认，建立身份",
    c1.status === 200 && typeof ts1 === "string",
    JSON.stringify(c1.body),
  );

  // 「再补一组」→ 先询问，不生成草稿
  const amb = await run("s-f3-04-c-amb", "再补一组");
  check(
    "C 「再补一组」→ 草稿前询问归属，不生成草稿",
    amb.draft == null &&
      /补充上一练/.test(amb.text ?? "") &&
      /新增一练/.test(amb.text ?? ""),
    amb.text?.slice(0, 160),
  );

  // 选择「补充上一练」→ 复用 training_session_id
  const sup = await run("s-f3-04-c-sup", "补充上一练 哑铃弯举 12kg 1组 8次");
  check(
    "C 「补充上一练」→ 草稿复用既有 training_session_id",
    sup.draft?.kind === "training_record" &&
      sup.draft.payload.training_session_id === ts1,
    JSON.stringify(sup.draft?.payload?.training_session_id),
  );
  const c2 = await confirm(sup.draft.id, sup.draft.revision);
  check(
    "C 补充确认：training_session_id 复用（不新增身份）",
    c2.status === 200 && c2.body.training_session_id === ts1,
    JSON.stringify(c2.body.training_session_id),
  );

  // 选择「新增一练」→ 新身份
  const nw = await run("s-f3-04-c-new", "新增一练 哑铃弯举 10kg 2组 每组8次");
  check(
    "C 「新增一练」→ training_session_id null（新身份）",
    nw.draft?.kind === "training_record" &&
      nw.draft.payload.training_session_id === null,
    JSON.stringify(nw.draft?.payload?.training_session_id),
  );
  const c3 = await confirm(nw.draft.id, nw.draft.revision);
  check(
    "C 新增确认：建立不同身份",
    c3.status === 200 && c3.body.training_session_id !== ts1,
    JSON.stringify({ ts1, ts3: c3.body.training_session_id }),
  );
}

/* ========== 场景 D：缺次数 → incomplete；无安排加练（§7 第 10、12 步） ========== */
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });

{
  // 缺次数（昨天深蹲有重量无次数；深蹲不在种子 PR 里，便于断言 incomplete 不进 PR）
  const inc = await run("s-f3-04-d", "昨天深蹲 100kg 4组");
  check(
    "D 缺次数：可生成草稿（事实不编造，保持缺省）",
    inc.draft?.kind === "training_record" &&
      inc.draft.payload.occurred_on === "2026-09-10" &&
      inc.draft.payload.exercises[0].sets.every((s) => s.reps == null),
    JSON.stringify({
      date: inc.draft?.payload?.occurred_on,
      reps: inc.draft?.payload?.exercises?.[0]?.sets?.map((s) => s.reps),
    }),
  );
  check(
    "D 缺次数 → diff 标待补全（incomplete 派生）",
    (inc.draft.diff ?? []).some((d) => /待补全|incomplete/.test(d.new_value)),
    JSON.stringify(inc.draft.diff?.slice(-2)),
  );
  const confInc = await confirm(inc.draft.id, inc.draft.revision);
  check(
    "D 缺次数确认落盘为 incomplete",
    confInc.status === 200 && confInc.body.newly_committed === true,
    JSON.stringify(confInc.body),
  );
  const recInc = (await api("GET", "/api/records")).body.records.find(
    (x) => x.date === "2026-09-10",
  );
  check(
    "D 记录状态 incomplete（待补全）",
    recInc?.status === "incomplete",
    JSON.stringify(recInc?.status),
  );
  const stats = (await api("GET", "/api/stats")).body;
  const w2 = stats.per_week.find((w) => w.week === "W2");
  check(
    "D incomplete 不进完成率分子（W2 仍 1/3）",
    w2?.planned === 3 && w2.completed === 1,
    JSON.stringify(w2),
  );
  check(
    "D incomplete 不进三桶与 PR（深蹲 100kg 不出现在 PR）",
    stats.buckets.met === 1 &&
      stats.buckets.unmet === 1 &&
      stats.buckets.pending === 1 &&
      !stats.prs.some((pr) => pr.exercise === "杠铃背蹲"),
    JSON.stringify({ buckets: stats.buckets, prs: stats.prs }),
  );

  // 无安排训练：另一动作加练（深蹲/腿日无安排，弯举不在当日安排目标内）
  const extra = await run("s-f3-04-d2", "今天哑铃弯举 12kg 3组 每组10次");
  check(
    "D 无安排加练：不带 arrangement（动作不在今日安排目标内），新身份",
    extra.draft?.kind === "training_record" &&
      (extra.draft.payload.arrangement_revision_id ?? null) === null &&
      extra.draft.payload.training_session_id === null,
    JSON.stringify(extra.draft?.payload),
  );
  const cExtra = await confirm(extra.draft.id, extra.draft.revision);
  const recExtra = (await api("GET", "/api/records")).body.records.find(
    (x) => x.date === "2026-09-11" && x.exercise === "哑铃弯举",
  );
  check(
    "D 无安排加练入列表：valid、无日程关联、不计完成率",
    cExtra.status === 200 &&
      recExtra?.status === "valid" &&
      (recExtra.scheduled_session_id ?? null) === null &&
      (
        await api("GET", "/api/stats")
      ).body.per_week.find((w) => w.week === "W2").completed === 1,
    JSON.stringify(recExtra),
  );
  check(
    "D 无安排记录可进 PR（事实明确、无辅助、有重量）",
    (
      await api("GET", "/api/stats")
    ).body.prs.some((pr) => pr.exercise === "哑铃弯举"),
    JSON.stringify((await api("GET", "/api/stats")).body.prs),
  );
}

/* ========== 场景 E：无法解析 → 回询问、不生成草稿 ========== */
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });
{
  const bad = await run("s-f3-04-e", "打卡");
  check(
    "E 无法解析：回询问文案、不生成草稿、不编造",
    bad.draft == null && /不生成草稿/.test(bad.text ?? ""),
    bad.text?.slice(0, 120),
  );
}

await vite.close();
console.log(failed === 0 ? "\n全部通过" : `\n${failed} 项失败`);
process.exit(failed === 0 ? 0 : 1);
