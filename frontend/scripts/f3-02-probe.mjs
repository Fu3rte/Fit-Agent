/**
 * F3-02 验收探针（走真实 mock REST）：当次安排草稿的内联调整与接受
 * （plans/stage3.md §3.1 / F3-02 / §7 第 2–5 步）。
 *
 * 运行：node scripts/f3-02-probe.mjs
 * 覆盖：
 *  - 越界调整服务端拒、正式数据不变
 *  - 明显状态差与正常输入不产生安排草稿
 *  - local_skip 标注可读回且目标保持计划值
 *  - 确认后 GET /api/arrangements 可读回
 *  - 重复确认幂等、cv 不再 +1
 *  - 页面主展示词无 RIR 缩写（DraftCard 源码字符串断言）
 *  - 今日锁定仍可接受减组（日程锁定 ≠ 处方锁定）
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
const confirm = (id, revision) =>
  api("POST", `/api/drafts/${id}/confirm`, { revision });
const revise = (id, payload, revision) =>
  api("POST", `/api/drafts/${id}/revise`, { payload, revision });

async function run(session, message) {
  const started = await api("POST", "/api/runs", {
    session_id: session,
    message,
    client_request_id: `f3-02-${Date.now()}-${Math.random()}`,
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

/* ---------- 0. 页面主展示词无 RIR 缩写（DraftCard 源码断言） ---------- */
const { readFileSync } = await import("node:fs");
const draftCardSrc = readFileSync(
  path.join(root, "src/features/chat/DraftCard.tsx"),
  "utf8",
);
// 主展示字段禁用 RIR 缩写；契约字段名 target_rir 可保留（编辑器 aria 可用）
const rirDisplayHits = draftCardSrc
  .split("\n")
  .filter((line) => /RIR/.test(line) && !/target_rir|targetRir|DISPOSITION|注释|\/\//.test(line));
check(
  "页面主展示词无 RIR 缩写（DraftCard 源码无可见 RIR 文案）",
  rirDisplayHits.length === 0,
  rirDisplayHits.slice(0, 3).join(" | ").slice(0, 120),
);

/* ---------- 1. 种子与今日锁定仍可安排 ---------- */
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });

const profile0 = (await api("GET", "/api/profile")).body;
const arrangements0 = (await api("GET", "/api/arrangements")).body;
const todaySched = (profile0.schedules ?? []).filter(
  (s) => s.date === "2026-09-11",
);
check(
  "种子：今日 09-11 腿日已按日期规则锁定、尚无安排",
  todaySched.length === 1 &&
    todaySched[0].plan_workout_key === "legs" &&
    todaySched[0].locked_effective === true &&
    !arrangements0.arrangements.some((a) => a.target.scheduled_on === "2026-09-11"),
  JSON.stringify(todaySched[0]),
);

/* ---------- 2. 状态档位：正常无草稿 / 明显状态差无草稿 ---------- */
const normal = await run("s-f3-02-normal", "今天状态正常，按原计划练");
check(
  "状态档位：正常 → 无安排草稿",
  normal.draft == null || normal.draft.kind !== "arrangement",
  normal.text?.slice(0, 80),
);

const severe = await run("s-f3-02-severe", "今天状态非常差，不太想练");
check(
  "状态档位：明显状态差 → 仅建议休息文案、无结构化草稿",
  severe.draft == null &&
    /休息/.test(severe.text ?? "") &&
    !/草稿/.test(severe.text ?? ""),
  severe.text?.slice(0, 80),
);

/* ---------- 3. 一般状态差 → 今日 deload 安排草稿（主路径） ---------- */
const arr = await run("s-f3-02-mild", "今天状态一般，轻一点少做几组，只改今天");
check(
  "一般状态差 → 生成今日 09-11 腿日 deload 草稿（锁定日仍可安排）",
  arr.draft?.kind === "arrangement" &&
    arr.draft.status === "pending" &&
    arr.draft.payload.target.scheduled_on === "2026-09-11" &&
    arr.draft.payload.target.exercises.every((e) => e.disposition === "deload"),
  arr.text?.slice(0, 100),
);

let draftId = arr.draft?.id;
let revision = arr.draft?.revision;
const cvBefore = (await api("GET", "/api/profile")).body.context_version;

/* ---------- 4. 越界调整服务端拒、正式数据不变 ---------- */
// 4a. 加组
{
  const bad = structuredClone(arr.draft.payload);
  bad.target.exercises[0].prescription.work_sets =
    bad.target.exercises[0].prescription.work_sets + 2;
  const r = await revise(draftId, bad, revision);
  check(
    "越界：加组 → 400 拒绝",
    r.status === 400 && /减组|增组/.test(r.body?.message ?? ""),
    r.body?.message?.slice(0, 80),
  );
}
// 4b. 降目标用力（向危险方向）—— 给 deload 项改 target_rir 也会被拒
{
  const bad = structuredClone(arr.draft.payload);
  bad.target.exercises[0].prescription.target_rir = { min: 0, max: 0 };
  const r = await revise(draftId, bad, revision);
  check(
    "越界：deload 改目标用力（属 keep 范畴）→ 拒绝",
    r.status === 400,
    r.body?.message?.slice(0, 80),
  );
}
// 4c. 空原因（有差异）
{
  const bad = structuredClone(arr.draft.payload);
  bad.target.adjustment_reason = "   ";
  const r = await revise(draftId, bad, revision);
  check(
    "越界：有差异但空白原因 → 拒绝",
    r.status === 400 && /reason|原因/.test(r.body?.message ?? ""),
    r.body?.message?.slice(0, 80),
  );
}
// 4d. 改绑定/增删动作
{
  const bad = structuredClone(arr.draft.payload);
  bad.target.exercises = bad.target.exercises.slice(0, 2);
  const r = await revise(draftId, bad, revision);
  check(
    "越界：增删动作 → 拒绝",
    r.status === 400 && /逐条|增删|对应/.test(r.body?.message ?? ""),
    r.body?.message?.slice(0, 80),
  );
}
// 4e. 减载改需校准动作负荷（无已验证重量）
{
  const bad = structuredClone(arr.draft.payload);
  bad.target.exercises[0].load = {
    kind: "verified",
    value: 60,
    unit: "kg",
    load_notation: "kg",
  };
  const r = await revise(draftId, bad, revision);
  check(
    "越界：需校准动作不得改负荷 → 拒绝",
    r.status === 400 && /已验证|负荷/.test(r.body?.message ?? ""),
    r.body?.message?.slice(0, 80),
  );
}
// 4f. 改绑定：session_id 改成其他日程
{
  const bad = structuredClone(arr.draft.payload);
  bad.target.scheduled_session_id = "sched-v2-2026-09-07";
  const r = await revise(draftId, bad, revision);
  check(
    "越界：改绑定 session_id → 拒绝",
    r.status === 400 && /session_id|日程/.test(r.body?.message ?? ""),
    r.body?.message?.slice(0, 80),
  );
}
// 4g. 改绑定：scheduled_on 改成任意日期
{
  const bad = structuredClone(arr.draft.payload);
  bad.target.scheduled_on = "2026-09-07";
  const r = await revise(draftId, bad, revision);
  check(
    "越界：改绑定 scheduled_on → 拒绝",
    r.status === 400 && /scheduled_on|日期/.test(r.body?.message ?? ""),
    r.body?.message?.slice(0, 80),
  );
}
// 4h. session 与 date 不一致（仍指向今日 session 但日期改掉）
{
  const bad = structuredClone(arr.draft.payload);
  bad.target.scheduled_on = "2026-09-14";
  const r = await revise(draftId, bad, revision);
  check(
    "越界：session 与 date 不一致 → 拒绝",
    r.status === 400 && /不一致/.test(r.body?.message ?? ""),
    r.body?.message?.slice(0, 80),
  );
}
{
  const arrangementsNow = (await api("GET", "/api/arrangements")).body;
  const profileNow = (await api("GET", "/api/profile")).body;
  check(
    "越界调整后正式数据不变（arrangements 仍 3 条、cv 不变）",
    arrangementsNow.arrangements.length === 3 &&
      profileNow.context_version === cvBefore,
    JSON.stringify({
      n: arrangementsNow.arrangements.length,
      cv: profileNow.context_version,
    }),
  );
}

/* ---------- 5. 合法纠错：只减组 + 原因 → revision+1；再确认 ---------- */
{
  const good = structuredClone(arr.draft.payload);
  // 再减一组（演示主路径 deload 方案 1）
  good.target.exercises[0].prescription.work_sets = Math.max(
    1,
    good.target.exercises[0].prescription.work_sets - 1,
  );
  good.target.adjustment_reason = "今日腿日状态一般：深蹲再减 1 组，其余保持";
  const r = await revise(draftId, good, revision);
  check(
    "合法纠错：deload 再减组 + 原因 → revision+1、diff 同步",
    r.status === 200 &&
      r.body.draft.revision === revision + 1 &&
      Array.isArray(r.body.draft.diff) &&
      r.body.draft.diff.some((row) => /组数/.test(row.field)),
    JSON.stringify({ status: r.status, rev: r.body?.draft?.revision }),
  );
  revision = r.body.draft.revision;
}

/* ---------- 6. 接受即落盘：cv+1、读回、幂等 ---------- */
{
  const c1 = await confirm(draftId, revision);
  check(
    "confirm：arrangement 返回 arrangement_revision_id 凭据、cv+1",
    c1.status === 200 &&
      c1.body.newly_committed === true &&
      typeof c1.body.arrangement_revision_id === "string" &&
      c1.body.arrangement_revision_id !== "" &&
      c1.body.context_version === cvBefore + 1,
    JSON.stringify(c1.body),
  );
  const arrangements1 = (await api("GET", "/api/arrangements")).body;
  const accepted = arrangements1.arrangements.find(
    (a) => a.id === c1.body.arrangement_revision_id,
  );
  check(
    "确认后 GET /api/arrangements 可读回完整目标快照",
    arrangements1.arrangements.length === 4 &&
      accepted?.target?.scheduled_on === "2026-09-11" &&
      typeof accepted.accepted_at === "string" &&
      accepted.target.exercises.every((e) => e.disposition === "deload"),
    JSON.stringify(
      arrangements1.arrangements.map((a) => a.target.scheduled_on),
    ),
  );
  const squat = accepted.target.exercises.find(
    (e) => e.exercise_id === "barbell-back-squat",
  );
  check(
    "今日锁定仍可接受减组（处方锁定 ≠ 日程锁定）",
    squat?.prescription.work_sets === 2 && squat.disposition === "deload",
    JSON.stringify({ sets: squat?.prescription.work_sets }),
  );
  const c2 = await confirm(draftId, revision);
  check(
    "重复确认幂等：同一凭据、cv 不再 +1",
    c2.status === 200 &&
      c2.body.newly_committed === false &&
      c2.body.arrangement_revision_id === c1.body.arrangement_revision_id &&
      c2.body.context_version === c1.body.context_version,
    JSON.stringify({ c1: c1.body.context_version, c2: c2.body.context_version }),
  );
  const arrangements2 = (await api("GET", "/api/arrangements")).body;
  check(
    "重复确认后 arrangements 仍 4 条（不重复落盘）",
    arrangements2.arrangements.length === 4,
  );
}

/* ---------- 7. local_skip：标注可读回、目标保持计划值 ---------- */
{
  const arrRun = await run("s-f3-02-skip", "今天状态一般，只改今天轻一点");
  check("local_skip 前置：生成草稿", arrRun.draft?.kind === "arrangement");
  if (arrRun.draft) {
    const p = structuredClone(arrRun.draft.payload);
    const ex = p.target.exercises[1];
    ex.disposition = "local_skip";
    // 恢复计划处方（保持计划值）：从深蹲之外的第二动作 —— 用计划对照
    // 深蹲 deload 保留；第二动作标记 local_skip 且处方保持计划
    const plan = profile0.plan;
    const legs = plan.payload.plan_workouts.find((w) => w.workout_key === "legs");
    const planned = legs.exercises[1];
    p.target.exercises[1] = {
      ...planned,
      disposition: "local_skip",
    };
    p.target.adjustment_reason = "今日状态一般：深蹲减组，罗马硬拉局部跳过保持计划目标";
    const r = await revise(arrRun.draft.id, p, arrRun.draft.revision);
    check(
      "local_skip：纠错可接受（目标保持计划值仅标注）",
      r.status === 200,
      r.body?.message?.slice(0, 100) ?? `rev=${r.body?.draft?.revision}`,
    );
    if (r.status === 200) {
      const c = await confirm(arrRun.draft.id, r.body.draft.revision);
      check("local_skip：确认落盘", c.status === 200, JSON.stringify(c.body));
      const arrangements3 = (await api("GET", "/api/arrangements")).body;
      const skipRow = arrangements3.arrangements.find(
        (a) => a.id === c.body.arrangement_revision_id,
      );
      const skipEx = skipRow?.target.exercises[1];
      check(
        "local_skip：读回标注且目标保持计划值",
        skipEx?.disposition === "local_skip" &&
          skipEx.prescription.work_sets === planned.prescription.work_sets &&
          JSON.stringify(skipEx.prescription.reps_range) ===
            JSON.stringify(planned.prescription.reps_range),
        JSON.stringify({
          d: skipEx?.disposition,
          sets: skipEx?.prescription.work_sets,
        }),
      );
    }
  }
}

/* ---------- 8. keep 主路径：只升目标用力（更保守） ---------- */
{
  const keepRun = await run("s-f3-02-keep", "今天状态一般，只升目标用力更保守一点，只改今天");
  check(
    "keep：生成只升目标用力草稿",
    keepRun.draft?.kind === "arrangement" &&
      keepRun.draft.payload.target.exercises.every(
        (e) => e.disposition === "keep",
      ),
    keepRun.text?.slice(0, 80),
  );
  if (keepRun.draft) {
    // 越界：keep 降目标用力 → 拒
    const bad = structuredClone(keepRun.draft.payload);
    bad.target.exercises[0].prescription.target_rir = { min: 0, max: 0 };
    const rBad = await revise(keepRun.draft.id, bad, keepRun.draft.revision);
    check(
      "keep：降目标用力（向危险方向）→ 拒绝",
      rBad.status === 400,
      rBad.body?.message?.slice(0, 80),
    );
    const c = await confirm(keepRun.draft.id, keepRun.draft.revision);
    check(
      "keep：确认摘要可标目标更保守",
      c.status === 200 && /保守/.test(c.body?.summary ?? ""),
      c.body?.summary?.slice(0, 100),
    );
  }
}

/* ---------- 9. equivalent_replace 字段校验（不做候选 UI） ---------- */
{
  const arrRun = await run("s-f3-02-rep", "今天状态一般，只改今天少做几组");
  if (arrRun.draft) {
    const p = structuredClone(arrRun.draft.payload);
    // 先恢复计划处方再标 equivalent_replace（本测验只校验替换字段，不测处方改动）
    const plan = profile0.plan;
    const legs = plan.payload.plan_workouts.find((w) => w.workout_key === "legs");
    p.target.exercises[0] = {
      ...legs.exercises[0],
      disposition: "equivalent_replace",
    };
    p.target.adjustment_reason = "今日状态一般：卧推改同等刺激替换（验证字段校验）";
    // 缺 replacement → 拒
    const r1 = await revise(arrRun.draft.id, p, arrRun.draft.revision);
    check(
      "equivalent_replace：缺 replacement_exercise_id → 拒绝",
      r1.status === 400 && /replacement/.test(r1.body?.message ?? ""),
      r1.body?.message?.slice(0, 80),
    );
    // 改处方 → 拒
    p.target.exercises[0].replacement_exercise_id = "dumbbell-bench-press";
    p.target.exercises[0].prescription.work_sets = 1;
    const r2 = await revise(arrRun.draft.id, p, arrRun.draft.revision);
    check(
      "equivalent_replace：改处方 → 拒绝",
      r2.status === 400,
      r2.body?.message?.slice(0, 80),
    );
  }
}

/* ---------- 10. 失败注入：确认整份回滚 ---------- */
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });
await api("POST", "/api/dev/confirm/fail-next");
{
  const arrRun = await run("s-f3-02-fail", "今天状态一般，少做几组只改今天");
  check("失败注入：先生成草稿", arrRun.draft?.kind === "arrangement");
  if (arrRun.draft) {
    const c = await confirm(arrRun.draft.id, arrRun.draft.revision);
    // 注入后确认失败；正式数据不变
    const arrangementsAfter = (await api("GET", "/api/arrangements")).body;
    check(
      "失败注入：确认失败整份回滚（无新安排落盘）",
      c.status >= 400 && arrangementsAfter.arrangements.length === 3,
      JSON.stringify({ status: c.status, n: arrangementsAfter.arrangements.length }),
    );
    // 清除注入位后可重试成功（mock 一次性注入已自清）
    const c2 = await confirm(arrRun.draft.id, arrRun.draft.revision);
    check(
      "失败注入后重做：可成功确认并落盘",
      c2.status === 200 &&
        (await api("GET", "/api/arrangements")).body.arrangements.length === 4,
      JSON.stringify({ status: c2.status }),
    );
  }
}

await vite.close();
console.log(failed === 0 ? "\n全部通过" : `\n${failed} 项失败`);
process.exit(failed === 0 ? 0 : 1);
