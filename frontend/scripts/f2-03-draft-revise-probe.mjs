/**
 * F2-03 验收探针（只读，无依赖）：结构化计划草稿卡与轻量纠错的服务端口径
 * （plans/stage2.md 第 7 节第 3–5 步中不依赖浏览器的确定性部分）。
 *
 * 运行：node scripts/f2-03-draft-revise-probe.mjs
 * 覆盖：动作候选、纠错后按目录重建身份、预计时长与具体日程重算、展示 Diff 由服务端派生、
 *      日期／训练日／动作／组数／次数区间／RIR 纠错，以及不可推荐、器械不符、限制冲突、
 *      日期非法、同日重复动作的拒绝面与 revise 接线。
 * 不覆盖：真实浏览器渲染走查（F2-06）、确认启用与替换事务（F2-04）。
 * 呈现覆盖（owner 2026-09-10）：旧版未来未锁定日程取消清单与历史版本日程不进产品 UI，
 *       故本节断言草稿卡／档案页源码不引用取消数据，且替换 Diff 不再输出取消行
 *       （取消事务与载荷仍由 F2-04／F2-05 探针验证）。
 */
import { readFileSync } from "node:fs";
import { CATALOG, isRecommendableCandidate } from "../src/mock/catalog.ts";
import { buildPplDraft, normalizePlanPayload } from "../src/mock/plan.ts";

const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
const TODAY = "2026-09-10";
let failed = 0;
const check = (name, ok, detail = "") => {
  if (!ok) failed += 1;
  console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? ` — ${detail}` : ""}`);
};

/** 计划生成种子（noplan）的档案镜像（与 f2-02 探针同口径） */
const PROFILE = {
  goal: "增肌（肌肥大）",
  experience: "初级（有少量训练经验）",
  weekly_frequency: 3,
  session_minutes: 60,
  equipment: ["杠铃", "哑铃", "卧推架", "引体架", "绳索"],
  body_weight_kg: 72.5,
  physical_state: { red_flags: [], notes: [] },
};

const generated = buildPplDraft({ profile: PROFILE, restrictions: [] });
if (!generated.ok) {
  console.log(`FAIL 无法生成计划草稿 — ${generated.reason}`);
  process.exit(1);
}
const payload = generated.payload;
/** 服务端已存草稿 = 生成载荷；requested = 客户端提交的纠错载荷 */
const norm = (requested, ctx = {}) =>
  normalizePlanPayload(payload, requested, {
    profile: PROFILE,
    restrictions: [],
    today: TODAY,
    ...ctx,
  });
const edit = () => structuredClone(payload);
const at = (date) => ((new Date(`${date}T00:00:00Z`).getUTCDay() + 6) % 7) + 1;
const minutesOf = (block) =>
  8 + 3 * block.exercises.reduce((n, e) => n + e.sets, 0);

/* 1. 动作候选（草稿卡「换动作」的可选项） */
const byId = new Map(CATALOG.map((e) => [e.id, e]));
const candidates = payload.candidates ?? [];
check(
  "草稿带动作候选且全部在已拍 24 项内、可推荐、器械匹配",
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
  "限制动作不进入候选（候选按当前档案与限制过滤）",
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
    unchanged.payload.diff.some(
      (r) =>
        r.field === "生效范围" &&
        r.new_value.includes(payload.scope.start_date) &&
        r.new_value.includes(payload.scope.review_date),
    ) &&
    JSON.stringify(unchanged.payload.plan.blocks) ===
      JSON.stringify(payload.plan.blocks),
  unchanged.ok ? `${unchanged.payload.diff.length} 行` : unchanged.error,
);

/* 3. 客户端伪造身份／时长／日程／Diff／标题／版本／取消清单一律被服务端重建或保留 */
const forged = edit();
forged.plan.blocks[0].exercises[0].name = "伪造动作名";
forged.plan.blocks[0].exercises[0].equipment = "cable";
forged.plan.blocks[0].exercises[0].modes = ["伪造模式"];
forged.plan.blocks[0].exercises[0].progression = "伪造渐进";
forged.plan.blocks[0].estimated_minutes = 1;
forged.plan.blocks[0].name = "伪造板块";
forged.plan.version = "v99";
forged.plan.status = "archived";
forged.title = "伪造标题";
forged.schedules = [];
forged.diff = [{ field: "伪造 Diff", new_value: "伪造值" }];
forged.cancellations = [
  {
    id: "sched-v1-2026-09-14",
    plan_version: "v1",
    weekday: 1,
    date: "2026-09-14",
    status: "cancelled",
  },
];
forged.profile_patch = {
  profile: { weekly_frequency: 6 },
};
const rebuilt = norm(forged);
check(
  "纠错后按目录重建身份／预计时长／具体日程与 Diff（不信任客户端提交值）",
  rebuilt.ok &&
    rebuilt.payload.plan.blocks[0].exercises[0].name === "杠铃平板卧推" &&
    rebuilt.payload.plan.blocks[0].exercises[0].equipment === "barbell" &&
    rebuilt.payload.plan.blocks[0].exercises[0].modes.join() === "水平推" &&
    rebuilt.payload.plan.blocks[0].estimated_minutes ===
      minutesOf(payload.plan.blocks[0]) &&
    rebuilt.payload.schedules.length === 12 &&
    !rebuilt.payload.diff.some((r) => r.field === "伪造 Diff"),
  rebuilt.ok ? "" : rebuilt.error,
);
check(
  "纠错只改允许字段：版本／状态／板块名／渐进／标题／取消清单／档案补丁保持服务端草稿值",
  rebuilt.ok &&
    rebuilt.payload.plan.version === payload.plan.version &&
    rebuilt.payload.plan.status === "active" &&
    rebuilt.payload.plan.blocks[0].name === payload.plan.blocks[0].name &&
    rebuilt.payload.plan.blocks[0].exercises[0].progression ===
      payload.plan.blocks[0].exercises[0].progression &&
    rebuilt.payload.title === payload.title &&
    JSON.stringify(rebuilt.payload.cancellations) ===
      JSON.stringify(payload.cancellations) &&
    rebuilt.payload.profile_patch === undefined,
  rebuilt.ok
    ? `version=${rebuilt.payload.plan.version} title=${rebuilt.payload.title}`
    : rebuilt.error,
);
const grown = edit();
grown.plan.blocks[0].exercises.push(
  structuredClone(grown.plan.blocks[0].exercises[0]),
);
const grownRes = norm(grown);
check(
  "纠错不得增删训练日板块或动作（无完整编辑器）",
  grownRes.ok === false && /不得增删/.test(grownRes.error),
  grownRes.ok ? "仍通过" : grownRes.error,
);

/* 4. 日期纠错：开始／复核日期可改，日程按新生效范围重算（第 7 节第 5 步） */
const moved = edit();
moved.scope.start_date = "2026-09-21";
moved.scope.review_date = "2026-10-19";
const movedRes = norm(moved);
check(
  "改开始／复核日期后日程只在 [开始, 复核) 内重算",
  movedRes.ok &&
    movedRes.payload.scope.start_date === "2026-09-21" &&
    movedRes.payload.scope.review_date === "2026-10-19" &&
    movedRes.payload.plan.start_date === "2026-09-21" &&
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

/* 5. 训练日纠错：改板块星期，每周训练日随之重算（训练日数量与板块数量一致） */
const shifted = edit();
shifted.plan.blocks[0].weekday = 2;
shifted.plan.blocks[1].weekday = 4;
shifted.plan.blocks[2].weekday = 6;
const shiftedRes = norm(shifted);
check(
  "改训练日后每周训练日与全部日程同步移动",
  shiftedRes.ok &&
    shiftedRes.payload.scope.weekdays.join() === "2,4,6" &&
    shiftedRes.payload.schedules.length === 12 &&
    shiftedRes.payload.schedules.every(
      (s) => [2, 4, 6].includes(s.weekday) && s.weekday === at(s.date),
    ),
  shiftedRes.ok ? shiftedRes.payload.scope.weekdays.join() : shiftedRes.error,
);

/* 6. 组数／次数区间／RIR 纠错：会话时长按处方重算，超档案时长被拒 */
const raised = edit();
raised.plan.blocks[0].exercises[0].sets = 6;
raised.plan.blocks[0].exercises[0].rep_range = "8-10";
raised.plan.blocks[0].exercises[0].target_rir = "2-3";
const raisedRes = norm(raised);
check(
  "组数／次数区间／RIR 纠错通过，预计时长按处方重算并写入 Diff",
  raisedRes.ok &&
    raisedRes.payload.plan.blocks[0].estimated_minutes ===
      minutesOf(raised.plan.blocks[0]) &&
    raisedRes.payload.plan.blocks[0].exercises[0].target_rir === "2-3" &&
    raisedRes.payload.diff.some((r) => r.new_value.includes("8-10 次")),
  raisedRes.ok
    ? `${raisedRes.payload.plan.blocks[0].estimated_minutes} 分钟`
    : raisedRes.error,
);
const tooLong = edit();
tooLong.plan.blocks[0].exercises.forEach((e) => {
  e.sets = 6;
});
const tooLongRes = norm(tooLong);
check(
  "纠错后超过档案单次可用时长被拒绝",
  tooLongRes.ok === false && /超过档案单次可用时长/.test(tooLongRes.error),
  tooLongRes.ok ? "仍通过" : tooLongRes.error,
);
const badRange = edit();
badRange.plan.blocks[0].exercises[0].rep_range = "八次";
const badRangeRes = norm(badRange);
check(
  "非法次数区间／目标 RIR 被拒绝",
  badRangeRes.ok === false &&
    /次数区间/.test(badRangeRes.error) &&
    (() => {
      const p = edit();
      p.plan.blocks[0].exercises[0].target_rir = "RIR 2";
      const r = norm(p);
      return r.ok === false && /目标 RIR/.test(r.error);
    })(),
  badRangeRes.ok ? "仍通过" : badRangeRes.error,
);

/* 7. 拒绝面：不可推荐／器械不符／限制冲突／日期非法／同日重复身份（第 7 节第 11 步） */
const unknown = edit();
unknown.plan.blocks[0].exercises[0].exercise_id = "face-pull";
const unknownRes = norm(unknown);
check(
  "目录外／不可推荐动作身份被拒绝",
  unknownRes.ok === false && /目录外动作身份/.test(unknownRes.error),
  unknownRes.ok ? "仍通过" : unknownRes.error,
);
const wrongEquip = norm(edit(), {
  profile: { ...PROFILE, equipment: ["哑铃"] },
});
check(
  "器械不符动作被拒绝",
  wrongEquip.ok === false && /器械/.test(wrongEquip.error),
  wrongEquip.ok ? "仍通过" : wrongEquip.error,
);
const hitRestriction = norm(edit(), {
  restrictions: [{ name: "杠铃平板卧推", scope: "specific_action" }],
});
check(
  "限制冲突动作被拒绝",
  hitRestriction.ok === false && /限制/.test(hitRestriction.error),
  hitRestriction.ok ? "仍通过" : hitRestriction.error,
);
const pastStart = edit();
pastStart.scope.start_date = "2026-09-07";
check(
  "开始日期早于当前日期被拒绝",
  (() => {
    const r = norm(pastStart);
    return r.ok === false && /早于当前日期/.test(r.error);
  })(),
);
const reversed = edit();
reversed.scope.start_date = "2026-10-12";
reversed.scope.review_date = "2026-09-14";
check(
  "开始日期不早于复核日期被拒绝",
  (() => {
    const r = norm(reversed);
    return r.ok === false && /早于复核日期/.test(r.error);
  })(),
);
const badFormat = edit();
badFormat.scope.start_date = "2026/09/14";
check(
  "日期格式非法被拒绝",
  (() => {
    const r = norm(badFormat);
    return r.ok === false && /有效日期/.test(r.error);
  })(),
);
const dup = edit();
dup.plan.blocks[1].weekday = dup.plan.blocks[0].weekday;
dup.plan.blocks[1].exercises[0].exercise_id =
  dup.plan.blocks[0].exercises[0].exercise_id;
const dupRes = norm(dup);
check(
  "同一训练日重复同一动作身份被拒绝",
  dupRes.ok === false && /同一训练日重复/.test(dupRes.error),
  dupRes.ok ? "仍通过" : dupRes.error,
);

/* 8. 接入：revise 走同一归一化口径；草稿卡与档案页不展示取消／历史日程数据 */
const server = read("../src/mock/server.ts");
check(
  "revise 计划草稿以服务端存储草稿为基准做归一化并以 400 拒绝非法纠错",
  /else if \(draft\.kind === "plan_adjust"\) \{[\s\S]{0,500}?normalizePlanPayload\(\n\s*draft\.payload as PlanDraftPayload,\n\s*body\.payload as PlanDraftPayload,/.test(
    server,
  ) &&
    /http_status: 400,[\s\S]{0,120}?计划载荷无效，纠错未生效/.test(server) &&
    !/else if \(draft\.kind === "plan_adjust"\) \{\n\s*const p = body\.payload/.test(
      server,
    ),
  "旧「信任客户端 diff」分支已移除",
);
check(
  "生成与纠错共用同一候选函数",
  /candidates: planCandidates\(profile, restrictions\)/.test(
    read("../src/mock/plan.ts"),
  ),
);
const card = read("../src/features/chat/DraftCard.tsx");
check(
  "草稿卡展示版本／日期／训练日／处方／校准／日程",
  /计划版本 \{plan\.version\}/.test(card) &&
    /aria-label="计划开始日期"/.test(card) &&
    /aria-label="计划复核日期"/.test(card) &&
    /的训练日`\}/.test(card) &&
    /aria-label={`\$\{ex\.name\}组数`\}/.test(card) &&
    /aria-label={`\$\{ex\.name\}次数区间`\}/.test(card) &&
    /aria-label={`\$\{ex\.name\}目标 RIR`\}/.test(card) &&
    /需要校准/.test(card) &&
    /校准说明（无可信训练记录：不给起始重量）/.test(card) &&
    /具体日程（/.test(card) &&
    !/cancellations/.test(card) &&
    !/取消预览/.test(card) &&
    !/EditablePlanDiff/.test(card),
);
check(
  "替换 Diff 不再输出旧版日程取消行",
  !/field: "旧版日程"/.test(read("../src/mock/plan.ts")),
);
const profilePage = read("../src/features/profile/ProfilePage.tsx");
check(
  "档案页只取当前版本未取消日程：无历史版本分组与已取消行",
  /s\.plan_version === plan\.version && s\.status !== "cancelled"/.test(
    profilePage,
  ) &&
    !/历史，已归档/.test(profilePage) &&
    !/\{cancellations/.test(profilePage),
);

console.log(failed === 0 ? "\n全部通过" : `\n${failed} 项失败`);
process.exit(failed === 0 ? 0 : 1);
