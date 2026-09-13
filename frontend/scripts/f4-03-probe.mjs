/**
 * F4-03 验收探针（走真实 mock REST）：作废草稿与确认
 * （plans/stage4.md F4-03 / §3.2 / §7 第 5–6、11 步）。
 *
 * 运行：node scripts/f4-03-probe.mjs
 * 覆盖：
 *  - 对话作废意图（非 dev 桥）：定位 ts-seed-0907 → training_void 草稿
 *    （载荷只含身份；diff 含事实摘要与影响范围）
 *  - 生成前后正式数据/统计/cv 不变
 *  - 确认：cv+1、status=voided、旧修订仍在、W2 1/3→0/3、三桶退出该次、PR 不变
 *  - 重复确认幂等：newly_committed=false、cv 不再 +1
 *  - 丢弃后的作废草稿确认被拒
 *  - fail-next 作废确认：500 且整份回滚；重做成功
 *  - 作废后更正/复活（含补全）生成 fail-closed：无草稿、cv 不变、修订链不变
 *  - 歧义：同日多练先询问不落草稿；回 ts-id 后生成
 *  - 模糊无日期/动作 → 澄清不落草稿
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
const discard = (id) => api("POST", `/api/drafts/${id}/discard`, {});
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

/* ========== 1–3. 对话生成作废草稿（非 dev 桥） ========== */
console.log("\n--- 对话作废草稿生成 ---");
{
  await reset("default");
  await api("PUT", "/api/provider/api-key", { api_key: "probe" });

  const cv0 = await cv();
  const list0 = await records();
  const s0 = await stats();

  const res = await run(
    "s-f4-03-gen",
    "整次录错，作废 2026-09-07 的杠铃平板卧推这次训练",
    "f4-03-gen",
  );
  const d = res.draft;
  const p = d?.payload;

  check(
    "1 对话生成：kind=training_void、pending、身份 ts-seed-0907",
    d?.kind === "training_void" &&
      d.status === "pending" &&
      p?.training_session_id === "ts-seed-0907" &&
      Object.keys(p).length === 1,
    JSON.stringify({ kind: d?.kind, payload: p }),
  );
  check(
    "1 载荷只承载身份：无事实字段（occurred_on/exercises 均无）",
    !("occurred_on" in (p ?? {})) && !("exercises" in (p ?? {})),
    JSON.stringify(p),
  );
  check(
    "2 diff 含身份/事实摘要/影响范围三行",
    d?.diff?.some((x) => x.field === "作废训练身份" && String(x.new_value).includes("ts-seed-0907")) &&
      d.diff.some((x) => x.field === "事实摘要（只读）") &&
      d.diff.some(
        (x) =>
          x.field === "影响范围" &&
          /完成率|三桶|PR/.test(String(x.new_value)) &&
          /历史|保留/.test(String(x.new_value)),
      ),
    JSON.stringify(d?.diff),
  );
  check(
    "2 回复含身份、影响范围（退出统计/历史保留）、终态语义",
    /ts-seed-0907/.test(res.text ?? "") &&
      /(退出完成率|退出).*(三桶|PR)|完成率\/三桶\/PR/.test(res.text ?? "") &&
      /历史修订保留|不物理删除/.test(res.text ?? "") &&
      /终态/.test(res.text ?? ""),
    (res.text ?? "").slice(0, 300),
  );
  check(
    "2 生成前后：records 仍 4 身份全非 voided、cv 不变、stats 不变",
    list0.length === 4 &&
      (await records()).length === 4 &&
      recById(await records(), "ts-seed-0907")?.status !== "voided" &&
      (await cv()) === cv0 &&
      JSON.stringify((await stats()).buckets) === JSON.stringify(s0.buckets) &&
      JSON.stringify((await stats()).per_week) === JSON.stringify(s0.per_week),
    JSON.stringify({
      n: (await records()).length,
      st: recById(await records(), "ts-seed-0907")?.status,
      cv0,
      cv1: await cv(),
    }),
  );
}

/* ========== 4–8. 确认 / 幂等 / 丢弃 / fail-next / 终态拒绝 ========== */
console.log("\n--- 确认 / 幂等 / 回滚 / 终态 ---");
{
  await reset("default");
  await api("PUT", "/api/provider/api-key", { api_key: "probe" });

  const cv0 = await cv();
  const s0 = await stats();
  check(
    "前置：W2 1/3、三桶 1/1/1、PR 卧推 80kg×8",
    w2(s0)?.completed === 1 &&
      w2(s0)?.planned === 3 &&
      s0.buckets.met === 1 &&
      s0.buckets.unmet === 1 &&
      s0.buckets.pending === 1 &&
      s0.prs.some((e) => e.best_weight_kg === 80 && e.best_reps_at_weight === 8),
    JSON.stringify({ w2: w2(s0), buckets: s0.buckets, prs: s0.prs }),
  );

  // 丢弃路径：生成后丢弃 → 确认被拒
  const gen1 = await run(
    "s-f4-03-discard",
    "整次录错，作废 2026-09-07 的杠铃平板卧推",
    "f4-03-discard",
  );
  check(
    "3 丢弃前：草稿可用",
    gen1.draft?.kind === "training_void" &&
      gen1.draft.payload?.training_session_id === "ts-seed-0907",
    JSON.stringify({ kind: gen1.draft?.kind }),
  );
  const dis = await discard(gen1.draft.id);
  const disConfirm = await confirm(gen1.draft.id, gen1.draft.revision);
  check(
    "3 丢弃后的作废草稿确认被拒（409/400 invalid_request）",
    dis.status === 200 &&
      disConfirm.status >= 400 &&
      disConfirm.body?.error_code === "invalid_request",
    JSON.stringify({ dis: dis.status, confirm: disConfirm.body }),
  );
  check(
    "3 丢弃路径未改正式数据：cv 不变、仍 valid",
    (await cv()) === cv0 &&
      recById(await records(), "ts-seed-0907")?.status === "valid",
    JSON.stringify({ cv: await cv() }),
  );

  // fail-next 回滚
  const gen2 = await run(
    "s-f4-03-fail",
    "整次录错，作废 ts-seed-0907 这次训练",
    "f4-03-fail",
  );
  check(
    "4 按身份 id 定位生成作废稿",
    gen2.draft?.payload?.training_session_id === "ts-seed-0907",
    JSON.stringify(gen2.draft?.payload),
  );
  await failNext();
  const failConfirm = await confirm(gen2.draft.id, gen2.draft.revision);
  check(
    "4 fail-next 作废确认：500",
    failConfirm.status === 500,
    JSON.stringify(failConfirm.body),
  );
  check(
    "4 fail-next 回滚：记录仍 valid、修订链未增、cv 不变、W2 仍 1/3",
    recById(await records(), "ts-seed-0907")?.status === "valid" &&
      recById(await records(), "ts-seed-0907")?.revisions?.length === 1 &&
      (await cv()) === cv0 &&
      w2(await stats())?.completed === 1,
    JSON.stringify({
      st: recById(await records(), "ts-seed-0907")?.status,
      revs: recById(await records(), "ts-seed-0907")?.revisions?.length,
      cv: await cv(),
    }),
  );
  // 回滚后同一草稿重做成功（注入已解除；草稿仍 pending 且 revision 未变）
  const redo = await confirm(gen2.draft.id, gen2.draft.revision);
  check(
    "4 回滚后重做成功：cv+1、voided、旧修订仍在",
    redo.status === 200 &&
      redo.body.newly_committed === true &&
      redo.body.context_version === cv0 + 1 &&
      recById(await records(), "ts-seed-0907")?.status === "voided",
    JSON.stringify(redo.body),
  );

  const r0907 = recById(await records(), "ts-seed-0907");
  check(
    "5 确认后：该次退出统计（W2 0/3、三桶 0/0/0）、PR 不变（80kg×8 来自 08-31）",
    w2(await stats())?.completed === 0 &&
      (await stats()).buckets.met === 0 &&
      (await stats()).buckets.unmet === 0 &&
      (await stats()).buckets.pending === 0 &&
      (await stats()).prs.some(
        (e) => e.best_weight_kg === 80 && e.best_reps_at_weight === 8,
      ),
    JSON.stringify({
      w2: w2(await stats()),
      buckets: (await stats()).buckets,
    }),
  );
  check(
    "5 确认后：旧修订仍在（原 valid 事实可追溯）且当前 voided",
    r0907?.status === "voided" &&
      Array.isArray(r0907?.revisions) &&
      r0907.revisions.length >= 2 &&
      r0907.revisions.some((rev) => rev.status === "valid"),
    JSON.stringify(
      r0907?.revisions?.map((rev) => ({ id: rev.id, status: rev.status })),
    ),
  );

  // 重复确认幂等
  const again = await confirm(gen2.draft.id, gen2.draft.revision);
  check(
    "6 重复确认幂等：newly_committed=false、cv 不再 +1",
    again.status === 200 &&
      again.body.newly_committed === false &&
      again.body.context_version === cv0 + 1 &&
      (await cv()) === cv0 + 1,
    JSON.stringify({
      newly: again.body.newly_committed,
      cv: again.body.context_version,
    }),
  );

  // 终态：更正 / 按 id 更正 / 补全 → 生成 fail-closed
  const cvAfterVoid = await cv();
  const revsAfterVoid = JSON.stringify(
    recById(await records(), "ts-seed-0907")?.revisions?.map((x) => x.id),
  );

  const termCorr = await run(
    "s-f4-03-term-corr",
    "更正 2026-09-07 的训练记录：杠铃平板卧推 第2组改成6次",
    "f4-03-term-corr",
  );
  check(
    "7 作废后再更正：无草稿、文案含终态、cv 不变",
    termCorr.draft == null &&
      /已作废|终态/.test(termCorr.text ?? "") &&
      /不生成草稿|不接受/.test(termCorr.text ?? "") &&
      (await cv()) === cvAfterVoid,
    (termCorr.text ?? "").slice(0, 200),
  );

  const termById = await run(
    "s-f4-03-term-id",
    "更正 ts-seed-0907 的训练记录：杠铃平板卧推",
    "f4-03-term-id",
  );
  check(
    "7 作废后按 id 更正：同样无草稿、不推 cv",
    termById.draft == null &&
      /已作废|终态/.test(termById.text ?? "") &&
      (await cv()) === cvAfterVoid,
    (termById.text ?? "").slice(0, 200),
  );

  // 补全指向已作废身份：带日期的「补充上一练」命中 09-07 最近身份（voided）
  const termSup = await run(
    "s-f4-03-term-sup",
    "补充上一练 2026-09-07 卧推 80kg 3组 每组8次",
    "f4-03-term-sup",
  );
  check(
    "7 作废后补全（补充上一练 09-07）：无草稿、文案含终态",
    termSup.draft == null &&
      /已作废|终态/.test(termSup.text ?? "") &&
      /不接受|不生成/.test(termSup.text ?? ""),
    (termSup.text ?? "").slice(0, 200),
  );

  check(
    "7 终态后修订链不变、状态仍 voided",
    JSON.stringify(
      recById(await records(), "ts-seed-0907")?.revisions?.map((x) => x.id),
    ) === revsAfterVoid &&
      recById(await records(), "ts-seed-0907")?.status === "voided" &&
      (await cv()) === cvAfterVoid,
    JSON.stringify({ revsAfterVoid, cv: await cv() }),
  );

  // 作废意图对已作废身份：不落二次作废草稿
  const termVoid = await run(
    "s-f4-03-term-void",
    "整次录错，作废 2026-09-07 的杠铃平板卧推",
    "f4-03-term-void",
  );
  check(
    "7 已作废身份再发作废：无草稿、文案可读、cv 不变",
    termVoid.draft == null &&
      /已作废|终态/.test(termVoid.text ?? "") &&
      /不生成草稿/.test(termVoid.text ?? "") &&
      (await cv()) === cvAfterVoid,
    (termVoid.text ?? "").slice(0, 200),
  );
}

/* ========== 9–10. 歧义 / 模糊澄清 ========== */
console.log("\n--- 歧义与模糊澄清 ---");
{
  await reset("default");
  await api("PUT", "/api/provider/api-key", { api_key: "probe" });

  // 同日第二条卧推（新身份）
  const extra = await run(
    "s-f4-03-amb-extra",
    "2026-09-07 新增一练 卧推 70kg 3组 每组8次",
    "f4-03-amb-extra",
  );
  check(
    "9 前置：同日新增卧推草稿可生成",
    extra.draft?.kind === "training_record" &&
      extra.draft.payload?.occurred_on === "2026-09-07",
    JSON.stringify({ kind: extra.draft?.kind }),
  );
  const extraOk = await confirm(extra.draft.id, extra.draft.revision);
  check(
    "9 前置：确认后 records=5",
    extraOk.status === 200 && (await records()).length === 5,
    JSON.stringify({ status: extraOk.status, n: (await records()).length }),
  );

  const amb = await run(
    "s-f4-03-amb",
    "整次录错，作废 2026-09-07 的杠铃平板卧推",
    "f4-03-amb",
  );
  check(
    "9 候选不唯一：询问且不落草稿",
    amb.draft == null &&
      /候选不唯一|要作废哪一次/.test(amb.text ?? "") &&
      /不落草稿|不生成/.test(amb.text ?? ""),
    (amb.text ?? "").slice(0, 240),
  );

  const byId = await run(
    "s-f4-03-byid",
    "整次录错，作废 ts-seed-0907 这次训练",
    "f4-03-byid",
  );
  check(
    "9 回 ts-seed-0907 后生成作废稿",
    byId.draft?.kind === "training_void" &&
      byId.draft.payload?.training_session_id === "ts-seed-0907",
    JSON.stringify(byId.draft?.payload),
  );

  const fuzzy = await run(
    "s-f4-03-fuzzy",
    "整次录错了，帮我作废那次训练",
    "f4-03-fuzzy",
  );
  check(
    "10 无日期/动作/id：回澄清且不生成草稿",
    fuzzy.draft == null &&
      /日期|定位/.test(fuzzy.text ?? "") &&
      /不生成草稿/.test(fuzzy.text ?? ""),
    (fuzzy.text ?? "").slice(0, 200),
  );

  const miss = await run(
    "s-f4-03-miss",
    "整次录错，作废 2026-01-01 的杠铃平板卧推",
    "f4-03-miss",
  );
  check(
    "10 日期无记录：不生成草稿",
    miss.draft == null && /未能|不生成作废草稿/.test(miss.text ?? ""),
    (miss.text ?? "").slice(0, 200),
  );
}

await vite.close();
console.log(`\nPASS ${passed} / FAIL ${failed}`);
console.log(failed === 0 ? "全部通过" : `${failed} 项失败`);
process.exit(failed === 0 ? 0 : 1);
