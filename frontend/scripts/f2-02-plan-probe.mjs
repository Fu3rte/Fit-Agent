/**
 * F2-02 验收探针（只读，无依赖）：PPL 计划生成与安全前置校验
 * （plans/stage2.md 第 7 节第 1–4、11 步中不依赖浏览器的确定性部分）。
 *
 * 运行：node scripts/f2-02-plan-probe.mjs
 * 覆盖：候选日期与 `[开始, 复核)` 内的 12 个应训练日、频率／单次时长／器械／具体动作与
 *      动作模式限制过滤、同一训练日重复身份拒绝、跨训练日复用放行、只引用可推荐目录动作、
 *      无可信记录不给起始重量、缺档案与红旗不给处方、非法纠错载荷被服务端校验拒绝、
 *      计划生成种子（noplan）接入。
 * 不覆盖：草稿卡渲染与轻量纠错（F2-03）、确认与启用事务（F2-04）、`/profile` 计划卡与实际
 *        浏览器走查（F2-06）、真实后端 `recommendable` 与真实 Agent 生成质量。
 */
import { readFileSync } from "node:fs";
import { CATALOG, isRecommendableCandidate } from "../src/mock/catalog.ts";
import {
  PLAN_CANDIDATE,
  buildPplDraft,
  planPayloadError,
} from "../src/mock/plan.ts";

const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
let failed = 0;
const check = (name, ok, detail = "") => {
  if (!ok) failed += 1;
  console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? ` — ${detail}` : ""}`);
};

/** 计划生成种子（noplan）的档案镜像；器械只含杠铃／哑铃／推架／引体架／绳索 */
const PROFILE = {
  goal: "增肌（肌肥大）",
  experience: "初级（有少量训练经验）",
  weekly_frequency: 3,
  session_minutes: 60,
  equipment: ["杠铃", "哑铃", "卧推架", "引体架", "绳索"],
  body_weight_kg: 72.5,
  physical_state: { red_flags: [], notes: [] },
};
const build = (profile = PROFILE, restrictions = []) =>
  buildPplDraft({ profile, restrictions });
const at = (weekday) =>
  ((new Date(`${weekday}T00:00:00Z`).getUTCDay() + 6) % 7) + 1;
const idsOf = (plan) =>
  plan.blocks.flatMap((b) => b.exercises.map((e) => e.exercise_id));

/* 1. 候选日期与日程边界（第 7 节第 2、4 步） */
const generated = build();
check(
  "可从已建档／无红旗／无计划档案生成计划草稿",
  generated.ok,
  generated.reason,
);
if (!generated.ok) process.exit(1);
const { plan, scope, schedules, payload } = generated;
check(
  "候选与已拍 3.3 一致（09-14 起 / 一三五 / 10-12 复核）",
  PLAN_CANDIDATE.start_date === "2026-09-14" &&
    PLAN_CANDIDATE.review_date === "2026-10-12" &&
    PLAN_CANDIDATE.weekdays.join() === "1,3,5" &&
    at(PLAN_CANDIDATE.start_date) === 1 &&
    at(PLAN_CANDIDATE.review_date) === 1 &&
    scope.start_date === PLAN_CANDIDATE.start_date &&
    scope.review_date === PLAN_CANDIDATE.review_date,
  JSON.stringify(PLAN_CANDIDATE),
);
check(
  "恰好 12 个应训练日，09-14 至 10-09",
  schedules.length === 12 &&
    schedules[0].date === "2026-09-14" &&
    schedules[schedules.length - 1].date === "2026-10-09",
  `${schedules.length} 条：${schedules[0]?.date} ~ ${schedules[schedules.length - 1]?.date}`,
);
check(
  "日程只落在周一／周三／周五且全部早于复核日",
  schedules.every(
    (s) =>
      [1, 3, 5].includes(s.weekday) &&
      s.weekday === at(s.date) &&
      s.date < scope.review_date &&
      s.status === "scheduled" &&
      s.plan_version === plan.version,
  ),
  schedules.map((s) => s.date).join(","),
);
check(
  "休息日与复核日当天不生成名额",
  !schedules.some(
    (s) => s.date === "2026-10-12" || at(s.date) === 6 || at(s.date) === 7,
  ),
);
check(
  "计划版本与生效范围一致（[开始日期, 复核日期)）",
  plan.start_date === scope.start_date &&
    plan.review_date === scope.review_date &&
    plan.status === "active" &&
    plan.version === "v1" &&
    scope.weekdays.join() === "1,3,5",
);

/* 2. 频率与单次时长（验收标准：每周 3 个训练日、每次 ≤ 档案 60 分钟） */
check(
  "每周 3 个训练日且板块训练日都在生效范围内",
  plan.blocks.length === 3 &&
    plan.blocks.every((b) => scope.weekdays.includes(b.weekday)) &&
    plan.blocks.map((b) => b.weekday).join() === "1,3,5",
  plan.blocks.map((b) => `${b.name}/${b.weekday}`).join(" "),
);
check(
  "每次预计时长不超过档案单次可用时长",
  plan.blocks.every(
    (b) => b.estimated_minutes > 0 && b.estimated_minutes <= 60,
  ),
  plan.blocks.map((b) => `${b.name} ${b.estimated_minutes}min`).join("；"),
);

/* 3. 只引用可推荐且器械匹配的目录动作（第 7 节第 3 步） */
const byId = new Map(CATALOG.map((e) => [e.id, e]));
check(
  "计划动作全部在已拍 24 项目录内且可推荐",
  idsOf(plan).every(
    (id) => byId.has(id) && isRecommendableCandidate(byId.get(id)),
  ),
  idsOf(plan).join(","),
);
const dumbbellOnly = build({ ...PROFILE, equipment: ["哑铃"] });
check(
  "档案器械过滤生效：只有哑铃时只保留哑铃动作",
  dumbbellOnly.ok &&
    idsOf(dumbbellOnly.plan).every(
      (id) => byId.get(id).equipment_variant === "dumbbell",
    ) &&
    idsOf(dumbbellOnly.plan).length > 0,
  dumbbellOnly.ok ? idsOf(dumbbellOnly.plan).join(",") : dumbbellOnly.reason,
);
const noCable = build({
  ...PROFILE,
  equipment: ["杠铃", "哑铃", "卧推架", "引体架"],
});
check(
  "无绳索器械时绳索动作不进入草稿",
  noCable.ok &&
    idsOf(noCable.plan).every(
      (id) => byId.get(id).equipment_variant !== "cable",
    ),
  noCable.ok ? idsOf(noCable.plan).join(",") : noCable.reason,
);

/* 4. 具体动作／动作模式限制过滤（第 7 节第 3、10、11 步的服务端口径） */
const specific = build(PROFILE, [
  { name: "杠铃平板卧推", scope: "specific_action", note: "肩部不适" },
]);
check(
  "具体动作限制命中者不进入草稿且草稿仍通过校验",
  specific.ok && !idsOf(specific.plan).includes("barbell-bench-press"),
  specific.ok ? idsOf(specific.plan).join(",") : specific.reason,
);
const pattern = build(PROFILE, [{ name: "深蹲", scope: "movement_pattern" }]);
check(
  "动作模式限制命中者不进入草稿（含跨身份模式交集）",
  pattern.ok &&
    idsOf(pattern.plan).every((id) => !byId.get(id).modes.includes("深蹲")),
  pattern.ok ? idsOf(pattern.plan).join(",") : pattern.reason,
);
const allPushBlocked = build(PROFILE, [
  { name: "杠铃平板卧推", scope: "specific_action" },
  { name: "坐姿哑铃肩推", scope: "specific_action" },
  { name: "自重双杠臂屈伸", scope: "specific_action" },
  { name: "绳索下压", scope: "specific_action" },
]);
check(
  "某训练日动作被限制清空时整体不给处方（fail-closed）",
  allPushBlocked.ok === false && allPushBlocked.code === "unschedulable",
  allPushBlocked.ok ? "仍生成了处方" : allPushBlocked.reason,
);

/* 5. 同一训练日重复身份拒绝、跨训练日复用放行 */
const reusedAcrossDays = plan.blocks
  .flatMap((b) => b.exercises.map((e) => e.exercise_id))
  .filter((id, _i, all) => all.filter((x) => x === id).length > 1);
check(
  "跨训练日复用同一动作身份不判冲突（悬垂举腿在拉日与腿日各一次）",
  reusedAcrossDays.length > 0 &&
    planPayloadError(payload, { profile: PROFILE, restrictions: [] }) ===
      undefined,
  reusedAcrossDays.join(","),
);
const sameDayDup = structuredClone(payload);
sameDayDup.plan.blocks[0].exercises.push(
  structuredClone(sameDayDup.plan.blocks[0].exercises[0]),
);
check(
  "同一训练日重复同一动作身份被校验拒绝",
  /同一训练日重复/.test(
    planPayloadError(sameDayDup, { profile: PROFILE, restrictions: [] }) ?? "",
  ),
  planPayloadError(sameDayDup, { profile: PROFILE, restrictions: [] }) ??
    "未拒绝",
);
/* 同一训练日的两个板块（同 weekday）共享动作身份同样按「同一训练日」处理 */
const sameWeekdayBlocks = structuredClone(payload);
sameWeekdayBlocks.plan.blocks.push(
  structuredClone(sameWeekdayBlocks.plan.blocks[1]),
);
check(
  "同一训练日跨板块重复同一动作身份被校验拒绝",
  /同一训练日重复/.test(
    planPayloadError(sameWeekdayBlocks, {
      profile: PROFILE,
      restrictions: [],
    }) ?? "",
  ),
  planPayloadError(sameWeekdayBlocks, { profile: PROFILE, restrictions: [] }) ??
    "未拒绝",
);

/* 6. 无可信训练记录：只校准，不猜重量（第 7 节第 3 步） */
const serialized = JSON.stringify(plan);
/** 递归查找重量字段（equipment_variant = bodyweight 是器械名，不是重量值） */
const weightKey = (v) =>
  typeof v === "object" && v !== null
    ? Object.entries(v).some(
        ([k, val]) => /weight|(^|_)kg$/i.test(k) || weightKey(val),
      )
    : false;
check(
  "计划不含任何起始重量或猜测负荷",
  !weightKey(plan) &&
    !/\d+\s*(?:kg|公斤)/.test(serialized) &&
    plan.blocks.every((b) =>
      b.exercises.every(
        (e) =>
          e.calibration.status === "needs_calibration" &&
          e.calibration.steps.length > 0 &&
          e.calibration.pass_criteria !== "" &&
          e.calibration.stop_criteria !== "",
      ),
    ),
);

/* 7. 缺档案／红旗／频率不足不给处方（第 7 节第 11 步） */
const noProfile = build(null);
check(
  "缺档案不生成处方",
  noProfile.ok === false && noProfile.code === "no_profile",
  noProfile.ok ? "仍生成了处方" : noProfile.reason,
);
const redFlag = build({
  ...PROFILE,
  physical_state: { red_flags: ["胸部异常不适"], notes: [] },
});
check(
  "档案含红旗症状不生成处方且带回红旗原文",
  redFlag.ok === false &&
    redFlag.code === "red_flag" &&
    redFlag.red_flags.join() === "胸部异常不适",
  redFlag.ok ? "仍生成了处方" : redFlag.reason,
);
check(
  "红旗档案的计划载荷也被校验拒绝（requesting 指导时的整份复核口径）",
  /红旗/.test(
    planPayloadError(payload, {
      profile: {
        ...PROFILE,
        physical_state: { red_flags: ["锐痛"], notes: [] },
      },
      restrictions: [],
    }) ?? "",
  ),
);
const lowFrequency = build({ ...PROFILE, weekly_frequency: 2 });
check(
  "每周频率不足以排进 3 个训练日时不猜排程",
  lowFrequency.ok === false && lowFrequency.code === "unschedulable",
  lowFrequency.ok ? "仍生成了处方" : lowFrequency.reason,
);

/* 8. 非法纠错载荷被服务端校验拒绝（第 7 节第 11 步；revise 接线属 F2-03） */
const withRestriction = structuredClone(payload);
withRestriction.plan.blocks[0].exercises[0].exercise_id = "barbell-bench-press";
check(
  "限制动作改入草稿被拒绝",
  /限制/.test(
    planPayloadError(withRestriction, {
      profile: PROFILE,
      restrictions: [{ name: "杠铃平板卧推", scope: "specific_action" }],
    }) ?? "",
  ),
);
const wrongEquipment = planPayloadError(payload, {
  profile: { ...PROFILE, equipment: ["哑铃"] },
  restrictions: [],
});
check(
  "器械不符动作被拒绝",
  /器械/.test(wrongEquipment ?? ""),
  wrongEquipment ?? "未拒绝",
);
const unknownIdentity = structuredClone(payload);
unknownIdentity.plan.blocks[0].exercises[0].exercise_id = "face-pull";
check(
  "目录外动作身份被拒绝",
  /目录外动作身份/.test(
    planPayloadError(unknownIdentity, { profile: PROFILE, restrictions: [] }) ??
      "",
  ),
);
const outOfRange = structuredClone(payload);
outOfRange.schedules.push({
  id: "sched-v1-2026-10-12",
  plan_version: "v1",
  weekday: 1,
  date: "2026-10-12",
  status: "scheduled",
});
check(
  "越界日程（复核日及以后）被拒绝",
  planPayloadError(outOfRange, { profile: PROFILE, restrictions: [] }) !==
    undefined,
);
const weekendSchedule = structuredClone(payload);
weekendSchedule.schedules[0].date = "2026-09-19";
check(
  "休息日日程被拒绝",
  planPayloadError(weekendSchedule, { profile: PROFILE, restrictions: [] }) !==
    undefined,
);
const longSession = structuredClone(payload);
longSession.plan.blocks[0].estimated_minutes = 90;
check(
  "超过档案单次可用时长被拒绝",
  /超过档案单次可用时长/.test(
    planPayloadError(longSession, { profile: PROFILE, restrictions: [] }) ?? "",
  ),
);

/* 9. mock 接入：noplan 种子 + 无计划时走生成分支 */
const server = read("../src/mock/server.ts");
check(
  "控制端点注册 noplan 种子（已配置／已建档／无红旗／无计划）",
  /seed !== "noplan"/.test(server) &&
    /function noPlanSeedState\(\): MockState \{[\s\S]*?emptySeedState\(\)[\s\S]*?has_api_key = true;[\s\S]*?physical_state: \{ red_flags: \[\], notes: \[\] \},/.test(
      server,
    ) &&
    !/function noPlanSeedState[\s\S]*?\n {2}plan:/.test(server) &&
    !/function noPlanSeedState[\s\S]*?\n {2}records:/.test(server),
  "种子由空种子派生（plan: null、records: []）并写入无红旗档案",
);
check(
  "无正式计划时对话走计划生成分支",
  /if \(!state\.plan\) return newPlanReply\(state\);/.test(server) &&
    /function newPlanReply\(state: MockState\)[\s\S]*?buildPplDraft\(\{/.test(
      server,
    ),
);

console.log(failed === 0 ? "\n全部通过" : `\n${failed} 项失败`);
process.exit(failed === 0 ? 0 : 1);
