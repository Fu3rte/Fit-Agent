/**
 * F2-03 验收探针（只读，无依赖）：D9 结构化计划草稿卡与轻量纠错的服务端口径
 * （plans/stage2.md 第 7 节第 3—5 步中不依赖浏览器的确定性部分）。
 *
 * 运行：node scripts/f2-03-draft-revise-probe.mjs
 * 覆盖：动作候选、纠错后按目录重建身份／时长／日程、展示 Diff 由服务端派生；
 *      starts_on/review_on、训练日（cycle）、动作、组数、次数区间、目标 RIR 纠错；
 *      不可推荐、器械不符、限制冲突、日期非法、同日重复身份的拒绝面。
 */
import { readFileSync } from "node:fs";
import { CATALOG, isRecommendableCandidate } from "../src/mock/catalog.ts";
import { buildPplDraft, normalizePlanPayload } from "../src/mock/plan.ts";
import { derivePlanBlocks, rebuildCycleSlots } from "../src/lib/planView.ts";

const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
const TODAY = "2026-09-11";
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

const generated = buildPplDraft({ profile: PROFILE, restrictions: [] });
if (!generated.ok) {
  console.log(`FAIL 无法生成计划草稿 — ${generated.reason}`);
  process.exit(1);
}
const payload = generated.payload;
const norm = (requested, ctx = {}) =>
  normalizePlanPayload(payload, requested, {
    profile: PROFILE,
    restrictions: [],
    today: TODAY,
    ...ctx,
  });
const edit = () => structuredClone(payload);
const minutesOf = (workout) =>
  8 +
  3 *
    workout.exercises.reduce(
      (n, e) => n + (e.prescription.kind === "reps" || e.prescription.kind === "timed" ? e.prescription.work_sets : 0),
      0,
    );

/* 1. 动作候选 */
const byId = new Map(CATALOG.map((e) => [e.id, e]));
const candidates = payload.candidates ?? [];
check(
  "草稿带动作候选且全部在已拍 24 项内、可推荐、名称对齐目录",
  candidates.length > 0 &&
    candidates.every((c) => {
      const e = byId.get(c.exercise_id);
      return (
        e !== undefined &&
        isRecommendableCandidate(e) &&
        c.name === e.standard_name_zh
      );
    }),
  `${candidates.length} 项`,
);
const restricted = buildPplDraft({
  profile: PROFILE,
  restrictions: [{ name: "杠铃平板卧推", scope: "specific_action" }],
});
check(
  "限制动作不进入候选",
  restricted.ok &&
    !(restricted.payload.candidates ?? []).some(
      (c) => c.exercise_id === "barbell-bench-press",
    ),
);

/* 2. 不改内容的纠错：结构化内容与展示 Diff 由服务端重算 */
const unchanged = norm(edit(), { restrictions: [] });
check(
  "未改动内容的纠错仍通过并派生展示 Diff",
  unchanged.ok &&
    unchanged.payload.schedules.length === 12 &&
    unchanged.payload.diff.some((r) => r.field === "生效范围") &&
    JSON.stringify(unchanged.payload.plan.payload.plan_workouts) ===
      JSON.stringify(payload.plan.payload.plan_workouts),
  unchanged.ok ? `${unchanged.payload.diff.length} 行` : unchanged.error,
);

/* 3. 客户端伪造身份／时长／日程／Diff／标题／版本／取消清单一律被重建或保留 */
const forged = edit();
const fItem = forged.plan.payload.plan_workouts[0].exercises[0];
fItem.display_snapshot = {
  name: "伪造动作名",
  equipment_variant: "cable",
  load_convention: null,
};
fItem.progression = { method: "custom", rule: "伪造渐进" };
forged.plan.payload.plan_workouts[0].estimated_minutes = 1;
forged.plan.payload.plan_workouts[0].name = "伪造板块";
forged.plan.version = "v99";
forged.plan.status = "archived";
forged.title = "伪造标题";
forged.schedules = [];
forged.diff = [{ field: "伪造 Diff", new_value: "伪造值" }];
forged.cancellations = [
  {
    scheduled_session_id: "sched-v1-2026-09-14",
    plan_version: "v1",
    plan_workout_key: "push",
    scheduled_on: "2026-09-14",
  },
];
forged.profile_patch = { profile: { weekly_frequency: 6 } };
const rebuilt = norm(forged);
const rebuiltBlocks = rebuilt.ok
  ? derivePlanBlocks(rebuilt.payload.plan.payload)
  : [];
const rebuiltItem = rebuiltBlocks[0]?.exercises[0];
check(
  "纠错后按目录重建身份／展示快照／预计时长／具体日程与 Diff（不信任客户端）",
  rebuilt.ok &&
    rebuiltItem?.display_snapshot.name === "杠铃平板卧推" &&
    rebuiltItem?.display_snapshot.equipment_variant === "barbell" &&
    rebuiltItem?.progression.rule === payload.plan.payload.plan_workouts[0].exercises[0].progression.rule &&
    rebuilt.payload.plan.payload.plan_workouts[0].estimated_minutes ===
      minutesOf(payload.plan.payload.plan_workouts[0]) &&
    rebuilt.payload.schedules.length === 12 &&
    !rebuilt.payload.diff.some((r) => r.field === "伪造 Diff"),
  rebuilt.ok ? "" : rebuilt.error,
);
check(
  "纠错只改允许字段：版本、状态、workout 名、标题、取消清单、档案补丁保持服务端草稿值",
  rebuilt.ok &&
    rebuilt.payload.plan.version === payload.plan.version &&
    rebuilt.payload.plan.status === "active" &&
    rebuilt.payload.plan.payload.plan_workouts[0].name ===
      payload.plan.payload.plan_workouts[0].name &&
    rebuilt.payload.title === payload.title &&
    JSON.stringify(rebuilt.payload.cancellations) ===
      JSON.stringify(payload.cancellations) &&
    rebuilt.payload.profile_patch === undefined,
  rebuilt.ok
    ? `version=${rebuilt.payload.plan.version} title=${rebuilt.payload.title}`
    : rebuilt.error,
);
const grown = edit();
grown.plan.payload.plan_workouts[0].exercises.push(
  structuredClone(grown.plan.payload.plan_workouts[0].exercises[0]),
);
const grownRes = norm(grown);
check(
  "纠错不得增删训练日或动作（无完整编辑器）",
  grownRes.ok === false && /不得增删/.test(grownRes.error),
  grownRes.ok ? "仍通过" : grownRes.error,
);

/* 4. 日期纠错：starts_on/review_on 可改，日程按新生效范围重算 */
const moved = edit();
moved.plan.starts_on = "2026-09-21";
moved.plan.review_on = "2026-10-19";
const movedRes = norm(moved);
check(
  "改开始／复核日期后日程只在 [starts_on, review_on) 内重算",
  movedRes.ok &&
    movedRes.payload.plan.starts_on === "2026-09-21" &&
    movedRes.payload.plan.review_on === "2026-10-19" &&
    movedRes.payload.schedules.length === 12 &&
    movedRes.payload.schedules[0].date === "2026-09-21" &&
    movedRes.payload.schedules.at(-1).date === "2026-10-16" &&
    movedRes.payload.schedules.every(
      (s) => s.date >= "2026-09-21" && s.date < "2026-10-19",
    ),
  movedRes.ok
    ? `${movedRes.payload.schedules[0].date} ~ ${movedRes.payload.schedules.at(-1).date}`
    : movedRes.error,
);

/* 5. 训练日纠错：改展示 weekday → cycle 重建，全部日程 weekday 同步 */
const shifted = edit();
const keys = shifted.plan.payload.plan_workouts.map((w) => w.workout_key);
shifted.plan.payload.calendar_cycle = {
  anchor_date: shifted.plan.starts_on,
  slots: rebuildCycleSlots(shifted.plan.starts_on, [
    { workout_key: keys[0], weekday: 2 },
    { workout_key: keys[1], weekday: 4 },
    { workout_key: keys[2], weekday: 6 },
  ]),
};
const shiftedRes = norm(shifted);
const shiftedWeekdays = shiftedRes.ok
  ? derivePlanBlocks(shiftedRes.payload.plan.payload).map((b) => b.weekday)
  : [];
check(
  "改训练日后每周训练日与全部日程同步移动",
  shiftedRes.ok &&
    shiftedWeekdays.join() === "2,4,6" &&
    shiftedRes.payload.schedules.length === 12 &&
    shiftedRes.payload.schedules.every((s) => [2, 4, 6].includes(s.weekday)),
  shiftedRes.ok ? shiftedWeekdays.join() : shiftedRes.error,
);

/* 6. 组数／次数区间／目标 RIR 纠错：时长按处方重算，超档案时长被拒 */
const raised = edit();
const raisedRx = raised.plan.payload.plan_workouts[0].exercises[0].prescription;
raised.plan.payload.plan_workouts[0].exercises[0].prescription = {
  ...raisedRx,
  work_sets: 6,
  reps_range: { min: 8, max: 10 },
  target_rir: { min: 2, max: 3 },
};
const raisedRes = norm(raised);
check(
  "组数／次数区间／RIR 纠错通过，预计时长按处方重算",
  raisedRes.ok &&
    raisedRes.payload.plan.payload.plan_workouts[0].estimated_minutes ===
      minutesOf(raised.plan.payload.plan_workouts[0]) &&
    raisedRes.payload.plan.payload.plan_workouts[0].exercises[0].prescription
      .target_rir?.max === 3 &&
    raisedRes.payload.diff.some((r) => r.new_value.includes("8-10") || r.new_value.includes("6")),
  raisedRes.ok
    ? `${raisedRes.payload.plan.payload.plan_workouts[0].estimated_minutes} 分钟`
    : raisedRes.error,
);
const tooLong = edit();
tooLong.plan.payload.plan_workouts[0].exercises.forEach((e) => {
  e.prescription = { ...e.prescription, work_sets: 6 };
});
const tooLongRes = norm(tooLong);
check(
  "纠错后超过档案单次可用时长被拒",
  tooLongRes.ok === false && /超过档案单次可用时长/.test(tooLongRes.error),
  tooLongRes.ok ? "仍通过" : tooLongRes.error,
);
const badRange = edit();
badRange.plan.payload.plan_workouts[0].exercises[0].prescription = {
  ...badRange.plan.payload.plan_workouts[0].exercises[0].prescription,
  reps_range: { min: 10, max: 6 },
};
const badRangeRes = norm(badRange);
check(
  "非法次数区间（min>max）被拒",
  badRangeRes.ok === false,
  badRangeRes.ok ? "仍通过" : badRangeRes.error,
);

/* 7. 拒绝面：目录外、器械不符、限制冲突、日期非法、同日重复身份 */
const unknown = edit();
unknown.plan.payload.plan_workouts[0].exercises[0].exercise_id = "face-pull";
const unknownRes = norm(unknown);
check(
  "目录外动作身份被拒",
  unknownRes.ok === false && /目录外动作身份/.test(unknownRes.error),
  unknownRes.ok ? "仍通过" : unknownRes.error,
);
const wrongEquip = norm(edit(), {
  profile: { ...PROFILE, equipment: ["哑铃"] },
});
check(
  "器械不符动作被拒",
  wrongEquip.ok === false && /器械/.test(wrongEquip.error),
  wrongEquip.ok ? "仍通过" : wrongEquip.error,
);
const hitRestriction = norm(edit(), {
  restrictions: [{ name: "杠铃平板卧推", scope: "specific_action" }],
});
check(
  "限制冲突动作被拒",
  hitRestriction.ok === false && /限制/.test(hitRestriction.error),
  hitRestriction.ok ? "仍通过" : hitRestriction.error,
);
const pastStart = edit();
pastStart.plan.starts_on = "2026-09-07";
check(
  "开始日期早于当前日期被拒",
  (() => {
    const r = norm(pastStart);
    return r.ok === false && /早于当前日期/.test(r.error);
  })(),
);
const reversed = edit();
reversed.plan.starts_on = "2026-10-12";
reversed.plan.review_on = "2026-09-14";
check(
  "开始日期不早于复核日期被拒",
  (() => {
    const r = norm(reversed);
    return r.ok === false && /早于复核日期/.test(r.error);
  })(),
);
const badFormat = edit();
badFormat.plan.starts_on = "2026/09/14";
check(
  "日期格式非法被拒",
  (() => {
    const r = norm(badFormat);
    return r.ok === false && /有效日期/.test(r.error);
  })(),
);
const dup = edit();
const dupItem = structuredClone(dup.plan.payload.plan_workouts[0].exercises[0]);
dupItem.item_key = `${dupItem.item_key}-x`;
dup.plan.payload.plan_workouts[0].exercises[1] = dupItem;
const dupRes = norm(dup);
check(
  "同一训练日重复同一动作身份被拒",
  dupRes.ok === false && /重复同一动作身份/.test(dupRes.error),
  dupRes.ok ? "仍通过" : dupRes.error,
);

/* 8. 接入：revise 走同一归一化；草稿卡不展示取消清单 */
const server = read("../src/mock/server.ts");
check(
  "revise 计划草稿以服务端存储草稿为基准做归一化并以 400 拒绝非法纠错",
  /else if \(draft\.kind === "plan"\) \{[\s\S]{0,800}?normalizePlanPayload\(/.test(
    server,
  ) && /计划载荷无效，纠错未生效/.test(server),
);
const card = read("../src/features/chat/DraftCard.tsx");
const planFields = read("../src/features/chat/PlanDraftFields.tsx");
check(
  "草稿卡展示版本／日期／训练日／处方／校准／日程",
  /计划版本/.test(planFields) &&
    /需要校准|校准说明/.test(planFields) &&
    /具体日程/.test(planFields) &&
    /PlanDraftFields/.test(card) &&
    !/cancellations/.test(card) &&
    !/cancellations/.test(planFields),
);
const profilePage = read("../src/features/profile/ProfilePage.tsx");
check(
  "档案页用 derivePlanBlocks 且日程用 locked_effective",
  /derivePlanBlocks/.test(profilePage) && /locked_effective/.test(profilePage),
);

console.log(failed === 0 ? "\n全部通过" : `\n${failed} 项失败`);
process.exit(failed === 0 ? 0 : 1);
