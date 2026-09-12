/**
 * F3-03 验收探针（走真实 mock REST）：安排查看、指导优先与使用时安全复核
 * （plans/stage3.md §5 F3-03 / §7 第 1、6、13 步）。
 *
 * 运行：node scripts/f3-03-probe.mjs
 * 覆盖：
 *  - /api/arrangements 种子：09-07 已接受且已调整；今日 09-11 尚无安排
 *  - ProfilePage 源码：尚无安排/已接受安排文案；ScheduleSection 无输入控件
 *  - 指导：接受今日安排前按计划；接受 deload 安排后按已接受安排
 *  - 安全阻断：腿日限制冲突 → 整份阻断且无处方；红旗 → 线下评估
 */
import path from "node:path";
import { fileURLToPath } from "node:url";
import { readFileSync } from "node:fs";
import { createServer as createViteServer } from "vite";
import {
  arrangementStatusLabel,
  classifyArrangementStatus,
} from "../src/lib/planView.ts";

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

async function run(session, message) {
  const started = await api("POST", "/api/runs", {
    session_id: session,
    message,
    client_request_id: `f3-03-${Date.now()}-${Math.random()}`,
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

/* ---------- 0. 源码断言：安排文案与只读约束 ---------- */
const profileSrc = readFileSync(
  path.join(root, "src/features/profile/ProfilePage.tsx"),
  "utf8",
);
check(
  "ProfilePage 源码含「尚无安排」",
  /尚无安排/.test(profileSrc),
);
check(
  "ProfilePage 源码含「已接受安排」相关文案（经 planView label）",
  /已接受安排|arrangementStatusLabel/.test(profileSrc) &&
    /已接受安排/.test(
      readFileSync(path.join(root, "src/lib/planView.ts"), "utf8"),
    ),
);
{
  const section = profileSrc.slice(
    profileSrc.indexOf("function ScheduleSection"),
    profileSrc.indexOf("function PlanCard"),
  );
  check(
    "ScheduleSection 无输入控件（无 input/textarea/select/button 标签）",
    !/<(input|textarea|select|button)\b/.test(section),
    section.slice(0, 100),
  );
}

/* ---------- 1. 种子安排：09-07 已接受已调整；今日无安排 ---------- */
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });

const arrangements0 = (await api("GET", "/api/arrangements")).body;

/* ---------- 1b. 徽章分类（classifyArrangementStatus 边界） ---------- */
{
  const mkEx = (item_key, work_sets, reps, rir, disposition, exercise_id = "e1") => ({
    item_key,
    exercise_id,
    record_type: "exercise",
    display_snapshot: { name: "X", equipment_variant: "" },
    progression: { method: "double_progression" },
    prescription: { kind: "reps", work_sets, reps_range: reps, target_rir: rir },
    disposition,
    load: null,
  });
  const planEx = [
    mkEx("a", 4, { min: 6, max: 8 }, { min: 1, max: 3 }, undefined),
    mkEx("b", 3, { min: 8, max: 10 }, { min: 1, max: 3 }, undefined, "e2"),
  ];
  const mkArr = (exercises) => ({
    id: "arr-unit",
    accepted_at: "2026-09-11T08:00:00.000Z",
    target: {
      schema_version: 1,
      scheduled_session_id: "sched-v2-2026-09-11",
      plan_version: "v2",
      plan_workout_key: "legs",
      scheduled_on: "2026-09-11",
      exercises,
    },
  });
  const identical = classifyArrangementStatus(
    mkArr([mkEx("a", 4, { min: 6, max: 8 }, { min: 1, max: 3 }, "keep"),
      mkEx("b", 3, { min: 8, max: 10 }, { min: 1, max: 3 }, "keep", "e2")]),
    planEx,
  );
  const onlyKeepRaise = classifyArrangementStatus(
    mkArr([mkEx("a", 4, { min: 6, max: 8 }, { min: 2, max: 3 }, "keep"),
      mkEx("b", 3, { min: 8, max: 10 }, { min: 1, max: 3 }, "keep", "e2")]),
    planEx,
  );
  const deloadSets = classifyArrangementStatus(
    mkArr([mkEx("a", 3, { min: 6, max: 8 }, { min: 1, max: 3 }, "deload"),
      mkEx("b", 3, { min: 8, max: 10 }, { min: 1, max: 3 }, "keep", "e2")]),
    planEx,
  );
  const rirLower = classifyArrangementStatus(
    mkArr([mkEx("a", 4, { min: 6, max: 8 }, { min: 1, max: 2 }, "keep"),
      mkEx("b", 3, { min: 8, max: 10 }, { min: 1, max: 3 }, "keep", "e2")]),
    planEx,
  );
  const missingItem = classifyArrangementStatus(
    mkArr([mkEx("a", 4, { min: 6, max: 8 }, { min: 1, max: 3 }, "keep")]),
    planEx,
  );
  check(
    "徽章分类：全等 → 未调整",
    identical === "identical" &&
      arrangementStatusLabel(identical) === "已接受安排 · 未调整",
    identical,
  );
  check(
    "徽章分类：仅 keep 升目标用力 → 目标更保守",
    onlyKeepRaise === "more_conservative" &&
      arrangementStatusLabel(onlyKeepRaise) === "已接受安排 · 目标更保守",
    onlyKeepRaise,
  );
  check(
    "徽章分类：减组 → 已调整",
    deloadSets === "adjusted" &&
      arrangementStatusLabel(deloadSets) === "已接受安排 · 已调整",
    deloadSets,
  );
  check(
    "徽章分类：降目标用力（服务端本应拒）不标未调整",
    rirLower === "adjusted",
    rirLower,
  );
  check(
    "徽章分类：条目集合不一致 → 已调整",
    missingItem === "adjusted",
    missingItem,
  );
}
const arr0907 = arrangements0.arrangements.find(
  (a) => a.target.scheduled_on === "2026-09-07",
);
const profile0 = (await api("GET", "/api/profile")).body;
const legs = profile0.plan.payload.plan_workouts.find(
  (w) => w.workout_key === "legs",
);
const push = profile0.plan.payload.plan_workouts.find(
  (w) => w.workout_key === "push",
);
const benchPlan = push.exercises.find((e) => e.exercise_id === "barbell-bench-press");
const benchArr = arr0907?.target.exercises.find(
  (e) => e.exercise_id === "barbell-bench-press",
);
check(
  "种子：09-07 安排已接受且卧推已调整（减组 4→3）",
  arr0907 != null &&
    typeof arr0907.accepted_at === "string" &&
    benchPlan?.prescription.work_sets === 4 &&
    benchArr?.prescription.work_sets === 3 &&
    benchArr?.disposition === "deload",
  JSON.stringify({
    accepted_at: arr0907?.accepted_at,
    sets: [benchPlan?.prescription.work_sets, benchArr?.prescription.work_sets],
  }),
);
const arr0831 = arrangements0.arrangements.find(
  (a) => a.target.scheduled_on === "2026-08-31",
);
const bench0831 = arr0831?.target.exercises.find(
  (e) => e.exercise_id === "barbell-bench-press",
);
check(
  "种子：08-31 keep 安排与计划全等（未调整）",
  arr0831 != null &&
    bench0831?.disposition === "keep" &&
    bench0831.prescription.work_sets === benchPlan?.prescription.work_sets,
  JSON.stringify({ d: bench0831?.disposition, sets: bench0831?.prescription.work_sets }),
);
check(
  "种子：09-07 分类器 → 已接受安排 · 已调整",
  arr0907 != null &&
    classifyArrangementStatus(arr0907, push.exercises) === "adjusted",
  classifyArrangementStatus(arr0907, push.exercises),
);
check(
  "种子：08-31 分类器 → 已接受安排 · 未调整",
  arr0831 != null && classifyArrangementStatus(arr0831, push.exercises) === "identical",
  arr0831 ? classifyArrangementStatus(arr0831, push.exercises) : "missing",
);
check(
  "种子：今日 09-11 尚无安排",
  !arrangements0.arrangements.some((a) => a.target.scheduled_on === "2026-09-11"),
  JSON.stringify(arrangements0.arrangements.map((a) => a.target.scheduled_on)),
);
{
  const todaySched = (profile0.schedules ?? []).filter(
    (s) => s.date === "2026-09-11" && s.stored_status !== "cancelled",
  );
  check(
    "种子：今日腿日按日期规则锁定",
    todaySched.length === 1 &&
      todaySched[0].plan_workout_key === "legs" &&
      todaySched[0].locked_effective === true &&
      todaySched[0].locked_by_date_rule === true,
    JSON.stringify(todaySched[0]),
  );
}

/* ---------- 2. 指导优先：接受前按计划 ---------- */
const gPlan = await run("s-f3-03-plan", "今天练什么");
const squatPlan = legs.exercises.find((e) => e.exercise_id === "barbell-back-squat");
check(
  "接受今日安排前：指导按计划腿日处方（深蹲 4 组）",
  gPlan.draft === null &&
    /2026-09-11/.test(gPlan.text ?? "") &&
    /杠铃背蹲/.test(gPlan.text ?? "") &&
    new RegExp(`${squatPlan.prescription.work_sets} 组`).test(gPlan.text ?? "") &&
    !/按已接受安排/.test(gPlan.text ?? ""),
  gPlan.text?.slice(0, 160),
);
check(
  "指导主文案无 RIR 缩写（§3.1 目标用力大白话）",
  !!gPlan.text &&
    !/RIR/.test(gPlan.text) &&
    /每一组结束还能再做/.test(gPlan.text),
  gPlan.text?.match(/还能再做[^\n]*/)?.[0] ?? gPlan.text?.slice(0, 120),
);

/* ---------- 3. 接受今日 deload 安排后：指导按安排目标 ---------- */
const arrToday = await run(
  "s-f3-03-today",
  "今天状态一般，轻一点少做几组，只改今天",
);
check(
  "生成今日 deload 安排草稿",
  arrToday.draft?.kind === "arrangement" &&
    arrToday.draft.payload.target.scheduled_on === "2026-09-11",
  arrToday.text?.slice(0, 80),
);
const confirmed = await confirm(arrToday.draft.id, arrToday.draft.revision);
check(
  "确认今日安排落盘",
  confirmed.status === 200 &&
    (await api("GET", "/api/arrangements")).body.arrangements.some(
      (a) => a.target.scheduled_on === "2026-09-11",
    ),
  JSON.stringify(confirmed.body),
);
const squatArr = (
  await api("GET", "/api/arrangements")
).body.arrangements.find((a) => a.target.scheduled_on === "2026-09-11");
const squatArrSets = squatArr.target.exercises.find(
  (e) => e.exercise_id === "barbell-back-squat",
)?.prescription.work_sets;
check(
  "今日安排深蹲已减组（< 计划 4 组）",
  typeof squatArrSets === "number" && squatArrSets < squatPlan.prescription.work_sets,
  `sets=${squatArrSets}`,
);

const gArr = await run("s-f3-03-arr", "今天练什么");
check(
  "接受后：指导按已接受安排（深蹲组数=安排、标明按已接受安排）",
  gArr.draft === null &&
    /按已接受安排/.test(gArr.text ?? "") &&
    new RegExp(`${squatArrSets} 组`).test(gArr.text ?? "") &&
    !new RegExp(`^- 杠铃背蹲 ${squatPlan.prescription.work_sets} 组`, "m").test(
      gArr.text ?? "",
    ),
  gArr.text?.slice(0, 200),
);

/* ---------- 4. 安全：腿日限制 → 整份阻断且无处方 ---------- */
const legHit = await run(
  "s-f3-03-safe",
  "补充档案：目标增肌，初级经验，每周 3 次，每次 60 分钟，可用器材：杠铃、哑铃、卧推架、引体架、绳索，体重 72.5kg，深蹲时膝部不适，没有其他不适。",
);
check(
  "经档案链路新增命中腿日限制（草稿可确认）",
  legHit.draft?.kind === "profile_update",
  `${legHit.draft?.kind} ${legHit.text?.slice(0, 60)}`,
);
if (legHit.draft) {
  const ok = await confirm(legHit.draft.id, legHit.draft.revision);
  check("确认腿日限制档案更新", ok.status === 200, JSON.stringify(ok.body));
}
const afterRestrict = (await api("GET", "/api/profile")).body;
check(
  "限制后安全复核整份不可用",
  afterRestrict.plan_safety?.usable === false &&
    (afterRestrict.plan_safety.conflicts.length > 0 ||
      afterRestrict.plan_safety.block_code === "plan_action_unavailable"),
  JSON.stringify(afterRestrict.plan_safety),
);
const gBlock = await run("s-f3-03-block", "今天练什么");
check(
  "限制冲突：整份阻断、不输出任何处方列表",
  gBlock.draft === null &&
    /整份计划指导已阻断/.test(gBlock.text ?? "") &&
    /杠铃背蹲|深蹲/.test(gBlock.text ?? "") &&
    !/^- .+ \d+ 组/m.test(gBlock.text ?? "") &&
    !/组 x |组 ×/.test(gBlock.text ?? ""),
  gBlock.text?.slice(0, 180),
);
check(
  "阻断时旧计划与日程仍可查看（文案保留）",
  /档案与限制/.test(gBlock.text ?? "") && /修改计划只能从对话发起/.test(gBlock.text ?? ""),
);

/* ---------- 5. 红旗：建议线下评估、无处方 ---------- */
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });
const red = await run(
  "s-f3-03-flag",
  "补充档案：目标增肌，初级经验，每周 3 次，每次 60 分钟，可用器材：杠铃、哑铃、卧推架、引体架、绳索，体重 72.5kg，最近胸部异常不适，没有其他不适。",
);
if (red.draft) await confirm(red.draft.id, red.draft.revision);
const gFlag = await run("s-f3-03-flag-g", "今天练什么");
check(
  "红旗：建议线下专业评估、无处方、无草稿",
  gFlag.draft === null &&
    /线下就医|专业评估/.test(gFlag.text ?? "") &&
    !/\d+ 组/.test(gFlag.text ?? "") &&
    !/按已接受安排/.test(gFlag.text ?? ""),
  gFlag.text?.slice(0, 160),
);

await vite.close();
console.log(failed === 0 ? "\n全部通过" : `\n${failed} 项失败`);
process.exit(failed === 0 ? 0 : 1);
