/**
 * F2-02 验收探针（只读，无依赖）：D9 PPL 计划生成与安全前置校验
 * （plans/stage2.md 第 7 节第 1—4、11 步中不依赖浏览器的确定性部分）。
 *
 * 运行：node scripts/f2-02-plan-probe.mjs
 * 覆盖：候选日期与 [starts_on, review_on) 内 12 个应训练日、频率／时长／器械／限制过滤、
 *      同一训练日重复身份拒绝、跨训练日复用放行、只引用可推荐目录、无可信记录不猜重、
 *      缺档案与红旗 fail-closed、非法纠错载荷被拒。
 */
import { readFileSync } from "node:fs";
import { CATALOG, isRecommendableCandidate } from "../src/mock/catalog.ts";
import {
  PLAN_CANDIDATE,
  buildPplDraft,
  planPayloadError,
} from "../src/mock/plan.ts";
import { derivePlanBlocks, weekdayOfDate } from "../src/lib/planView.ts";

const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
let failed = 0;
const check = (name, ok, detail = "") => {
  if (!ok) failed += 1;
  console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? ` — ${detail}` : ""}`);
};

const PROFILE = {
  goal: "增肌（肌肥大）",
  experience: "初级（有少量训练经验）",
  weekly_frequency: 3,
  session_minutes: 60,
  equipment: ["杠铃", "哑铃", "卧推架", "引体架", "绳索"],
  body_weight_kg: 72.5,
  body_conditions: [],
};
const build = (profile = PROFILE, restrictions = []) =>
  buildPplDraft({ profile, restrictions });
const idsOf = (plan) =>
  plan.payload.plan_workouts.flatMap((w) => w.exercises.map((e) => e.exercise_id));

/* 1. 候选日期与日程边界 */
const generated = build();
check(
  "可从已建档／无红旗／无计划档案生成计划草稿",
  generated.ok,
  generated.reason,
);
if (!generated.ok) process.exit(1);
const { plan, schedules, payload } = generated;
const blocks = derivePlanBlocks(plan.payload);
check(
  "候选与已拍 3.3 一致（09-14 起 / 复核 10-12）",
  PLAN_CANDIDATE.starts_on === "2026-09-14" &&
    PLAN_CANDIDATE.review_on === "2026-10-12" &&
    plan.starts_on === PLAN_CANDIDATE.starts_on &&
    plan.review_on === PLAN_CANDIDATE.review_on,
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
  "日程只落在周一／周三／周五且未到期（无 stored locked）",
  schedules.every(
    (s) =>
      [1, 3, 5].includes(s.weekday) &&
      s.weekday === weekdayOfDate(s.date) &&
      s.date < plan.review_on &&
      s.stored_status === "scheduled" &&
      s.locked_by_date_rule === false &&
      s.locked_effective === false &&
      s.plan_version === plan.version &&
      s.plan_workout_key,
  ),
  schedules.map((s) => s.date).join(","),
);
check(
  "休息日与复核日当天不生成名额",
  !schedules.some(
    (s) => s.date === "2026-10-12" || [6, 7].includes(weekdayOfDate(s.date)),
  ),
);
check(
  "D9 行字段与 calendar_cycle：starts_on/review_on/mode/payload 存在",
  plan.mode === "regular" &&
    plan.status === "active" &&
    plan.version === "v1" &&
    plan.payload.schema_version === 1 &&
    plan.payload.calendar_cycle.anchor_date &&
    plan.payload.calendar_cycle.slots.filter((s) => s.kind === "workout").length === 3 &&
    plan.payload.calendar_cycle.slots.some((s) => s.kind === "rest"),
);

/* 2. 频率与单次时长 */
check(
  "3 个 plan_workouts，展示 weekday 为 1/3/5",
  plan.payload.plan_workouts.length === 3 &&
    blocks.map((b) => b.weekday).join() === "1,3,5",
  blocks.map((b) => `${b.name}/${b.weekday}`).join(" "),
);
check(
  "每次预计时长不超过档案单次可用时长",
  plan.payload.plan_workouts.every(
    (w) => w.estimated_minutes > 0 && w.estimated_minutes <= 60,
  ),
  plan.payload.plan_workouts.map((w) => `${w.name} ${w.estimated_minutes}min`).join("；"),
);

/* 3. 只引用可推荐且器械匹配的目录动作 */
const byId = new Map(CATALOG.map((e) => [e.id, e]));
check(
  "计划动作全部在已拍 24 项目录内且可推荐",
  idsOf(plan).every((id) => byId.has(id) && isRecommendableCandidate(byId.get(id))),
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
    idsOf(noCable.plan).every((id) => byId.get(id).equipment_variant !== "cable"),
  noCable.ok ? idsOf(noCable.plan).join(",") : noCable.reason,
);

/* 4. 具体动作／动作模式限制过滤 */
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
  "动作模式限制命中者不进入草稿",
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
const flatIds = idsOf(plan);
const reusedAcrossDays = flatIds.filter(
  (id, _i, all) => all.filter((x) => x === id).length > 1,
);
check(
  "跨训练日复用同一动作身份不判冲突（悬垂举腿在拉日与腿日各一次）",
  reusedAcrossDays.length > 0 &&
    planPayloadError(payload, { profile: PROFILE, restrictions: [] }) === undefined,
  reusedAcrossDays.join(","),
);
const sameDayDup = structuredClone(payload);
const firstWorkout = sameDayDup.plan.payload.plan_workouts[0];
const cloneItem = structuredClone(firstWorkout.exercises[0]);
cloneItem.item_key = `${cloneItem.item_key}-dup`;
firstWorkout.exercises.push(cloneItem);
check(
  "同一训练日重复同一动作身份被校验拒绝",
  /重复同一动作身份/.test(
    planPayloadError(sameDayDup, { profile: PROFILE, restrictions: [] }) ?? "",
  ),
  planPayloadError(sameDayDup, { profile: PROFILE, restrictions: [] }) ?? "未拒绝",
);

/* 6. 无可信记录：只校准，不猜重量 */
const serialized = JSON.stringify(plan.payload);
const weightKey = (v) =>
  typeof v === "object" && v !== null
    ? Object.entries(v).some(
        ([k, val]) => /weight|(^|_)kg$/i.test(k) || weightKey(val),
      )
    : false;
check(
  "计划 payload 不含起始重量；外加负重动作一律 needs_calibration",
  !weightKey(plan.payload) &&
    !/\d+\s*(?:kg|公斤)/.test(serialized) &&
    plan.payload.plan_workouts.every((w) =>
      w.exercises.every((e) => {
        if (e.record_type !== "external_load_reps") return true;
        const load = e.load;
        return (
          load?.kind === "needs_calibration" &&
          load.steps.length > 0 &&
          load.pass_criteria !== "" &&
          !/RIR/.test(load.pass_criteria)
        );
      }),
    ),
);
check(
  "progression 为 method+rule 且 rule 非空",
  plan.payload.plan_workouts.every((w) =>
    w.exercises.every(
      (e) =>
        e.progression.method &&
        e.progression.rule.trim() !== "" &&
        e.prescription.work_sets > 0,
    ),
  ),
);

/* 7. 缺档案／红旗／频率不足 */
const noProfile = build(null);
check(
  "缺档案不生成处方",
  noProfile.ok === false && noProfile.code === "no_profile",
  noProfile.ok ? "仍生成了处方" : noProfile.reason,
);
const redFlag = build({
  ...PROFILE,
  body_conditions: ["胸部异常不适"],
});
check(
  "身体情况命中六类安全症状不生成处方且带回原文",
  redFlag.ok === false &&
    redFlag.code === "red_flag" &&
    redFlag.red_flags.join() === "胸部异常不适",
  redFlag.ok ? "仍生成了处方" : redFlag.reason,
);
check(
  "命中安全症状的计划载荷也被校验拒绝（请求指导时的整份复核口径）",
  /命中安全症状/.test(
    planPayloadError(payload, {
      profile: { ...PROFILE, body_conditions: ["锐痛"] },
      restrictions: [],
    }) ?? "",
  ),
);
check(
  "普通身体情况非空但未命中六类时不作红旗阻断",
  build({ ...PROFILE, body_conditions: ["肩部偶有不适"] }).ok === true,
);
const lowFrequency = build({ ...PROFILE, weekly_frequency: 2 });
check(
  "每周频率不足以排进 3 个训练日时不猜排程",
  lowFrequency.ok === false && lowFrequency.code === "unschedulable",
  lowFrequency.ok ? "仍生成了处方" : lowFrequency.reason,
);

/* 8. 非法纠错载荷被服务端校验拒绝 */
const withRestriction = structuredClone(payload);
withRestriction.plan.payload.plan_workouts[0].exercises[0].exercise_id =
  "barbell-bench-press";
check(
  "限制动作改入草稿被拒",
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
check("器械不符动作被拒", /器械/.test(wrongEquipment ?? ""), wrongEquipment ?? "未拒绝");
const unknownIdentity = structuredClone(payload);
unknownIdentity.plan.payload.plan_workouts[0].exercises[0].exercise_id =
  "face-pull";
check(
  "目录外动作身份被拒",
  /目录外动作身份/.test(
    planPayloadError(unknownIdentity, { profile: PROFILE, restrictions: [] }) ??
      "",
  ),
);
const outOfRange = structuredClone(payload);
outOfRange.schedules.push({
  id: "sched-v1-2026-10-12",
  plan_version: "v1",
  date: "2026-10-12",
  plan_workout_key: "push",
  weekday: 1,
  stored_status: "scheduled",
  locked_by_date_rule: false,
  locked_effective: false,
});
check(
  "越界日程（复核日及以后）被拒",
  planPayloadError(outOfRange, { profile: PROFILE, restrictions: [] }) !==
    undefined,
);
const weekendSchedule = structuredClone(payload);
weekendSchedule.schedules[0] = {
  ...weekendSchedule.schedules[0],
  date: "2026-09-19",
  weekday: 6,
};
check(
  "休息日日程被拒（与投影不一致）",
  planPayloadError(weekendSchedule, { profile: PROFILE, restrictions: [] }) !==
    undefined,
);
const longSession = structuredClone(payload);
longSession.plan.payload.plan_workouts[0].estimated_minutes = 90;
check(
  "超过档案单次可用时长被拒",
  /超过档案单次可用时长/.test(
    planPayloadError(longSession, { profile: PROFILE, restrictions: [] }) ?? "",
  ),
);
const badCycle = structuredClone(payload);
badCycle.plan.payload.calendar_cycle.slots = [
  { kind: "workout", workout_key: "not-exist" },
];
check(
  "calendar_cycle 引用不存在的 workout_key 被拒",
  /不存在的 workout_key/.test(
    planPayloadError(badCycle, { profile: PROFILE, restrictions: [] }) ?? "",
  ),
);

/* 9. mock 接入：noplan 种子 + 无计划时走生成分支 */
const server = read("../src/mock/server.ts");
check(
  "控制端点注册 noplan 种子",
  /seed !== "noplan"/.test(server) &&
    /function noPlanSeedState\(\): MockState \{[\s\S]*?emptySeedState\(\)[\s\S]*?has_api_key = true;/.test(
      server,
    ),
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
