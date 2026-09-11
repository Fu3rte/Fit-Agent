/**
 * F2-01 验收探针（只读，无依赖）：目录身份／模式／器械一致性 + 可推荐谓词 + mock 计划种子身份 + D9 契约形状。
 *
 * 运行：node scripts/f2-01-catalog-probe.mjs
 * 覆盖：24 项与后端 003 迁移逐字段一致且无第 25 项；未受检／inactive 被谓词拒绝；
 *      mock 计划种子只引用目录 ID；D9 计划／日程／安全复核结构化字段存在于契约。
 * 不覆盖：后端真实 `recommendable`、真实计划域事务、渲染与浏览器行为。
 */
import { readFileSync } from "node:fs";
import { CATALOG, isRecommendableCandidate } from "../src/mock/catalog.ts";

const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
let failed = 0;
const check = (name, ok, detail = "") => {
  if (!ok) failed += 1;
  console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? ` — ${detail}` : ""}`);
};

/* 1. 身份／模式／器械：与后端 Stage 1 已拍 24 项清单【逐字段对照】 */
const sql = read("../../backend/storage/migrations/003_stage1_action_seed.sql");
const rows = [
  ...sql.matchAll(
    /^\s*\('([^']+)', '([^']+)', '([^']+)', '([^']+)', (NULL|'[^']+'), (\d), \d, (\d), '(\[[^\]]*\])', '(\[[^\]]*\])', '([^']+)'/gm,
  ),
].map((m) => ({
  id: m[1],
  standard_name_zh: m[2],
  equipment_variant: m[3],
  record_type: m[4],
  load_convention: m[5] === "NULL" ? null : m[5].slice(1, -1),
  unilateral: m[6] === "1",
  active: m[7] === "1",
  modes: JSON.parse(m[9]),
  source_ref: m[10],
}));

check("后端清单共 24 项", rows.length === 24, `实际 ${rows.length}`);
check(
  "前端镜像共 24 项（无第 25 项）",
  CATALOG.length === 24,
  `实际 ${CATALOG.length}`,
);

const byId = new Map(CATALOG.map((e) => [e.id, e]));
check(
  "身份集合完全一致",
  rows.every((r) => byId.has(r.id)) && byId.size === rows.length,
  `缺 ${rows.filter((r) => !byId.has(r.id)).map((r) => r.id).join(",") || "无"}；多 ${
    [...byId.keys()].filter((id) => !rows.some((r) => r.id === id)).join(",") || "无"
  }`,
);

const fields = [
  "standard_name_zh",
  "equipment_variant",
  "record_type",
  "load_convention",
  "unilateral",
  "active",
  "source_ref",
];
const drift = [];
for (const r of rows) {
  const e = byId.get(r.id);
  if (!e) continue;
  for (const f of fields)
    if (JSON.stringify(e[f]) !== JSON.stringify(r[f]))
      drift.push(
        `${r.id}.${f}: 后端 ${JSON.stringify(r[f])} ≠ 前端 ${JSON.stringify(e[f])}`,
      );
  if ([...e.modes].sort().join() !== [...r.modes].sort().join())
    drift.push(`${r.id}.modes: 后端 ${r.modes} ≠ 前端 ${e.modes}`);
}
check("名称／器械／口径／模式逐字段一致", drift.length === 0, drift.join("；"));

/* 2. 谓词：未受检或 inactive 动作不得进入候选 */
check("24 项全部通过可推荐谓词", CATALOG.every(isRecommendableCandidate));
check(
  "inactive 动作被拒",
  !isRecommendableCandidate({ ...CATALOG[0], active: false }),
);
check(
  "来源未核对（source_ref 空）被拒",
  !isRecommendableCandidate({ ...CATALOG[0], source_ref: "" }),
);
check(
  "负重次数型缺负重口径被拒",
  !isRecommendableCandidate({
    ...CATALOG.find((e) => e.record_type === "reps_weight"),
    load_convention: null,
  }),
);
check(
  "自重型带负重口径被拒",
  !isRecommendableCandidate({
    ...CATALOG.find((e) => e.record_type === "reps_bodyweight"),
    load_convention: "dumbbell_per_hand",
  }),
);
check(
  "无动作模式被拒",
  !isRecommendableCandidate({ ...CATALOG[0], modes: [] }),
);

/* 3. mock 计划种子：只引用目录 ID（构造器 + 种子源码静态检查） */
const server = read("../src/mock/server.ts");
const planSource = read("../src/mock/plan.ts");
const seededIds = [
  ...server.matchAll(/id: "([a-z0-9-]+)"/g),
  ...planSource.matchAll(/id: "([a-z0-9-]+)"/g),
].map((m) => m[1]);
const catalogIds = new Set(byId.keys());
const seedIds = [...new Set(seededIds)].filter((id) => catalogIds.has(id));
check(
  "种子／模板引用的目录身份不少于 9 项",
  seedIds.length >= 9,
  `命中 ${seedIds.length} 项`,
);
check(
  "种子经 planExercise 构造（拒绝目录外身份）",
  /planExercise\(/.test(server) && /planExercise\(/.test(planSource),
);
check(
  "PPL 模板 workout_key 对齐 push/pull/legs",
  /workout_key: "push"/.test(planSource) &&
    /workout_key: "pull"/.test(planSource) &&
    /workout_key: "legs"/.test(planSource),
);

/* 4. D9 契约形状（不是旧 blocks+weekday 真相） */
const contract = read("../src/lib/contract.ts");
const need = [
  /export interface PlanPayload \{[\s\S]*?plan_workouts: PlanWorkout\[\];[\s\S]*?calendar_cycle: CalendarCycle;/,
  /export interface PlanVersion \{[\s\S]*?starts_on: string;[\s\S]*?review_on: string;[\s\S]*?payload: PlanPayload;/,
  /export interface PlanScheduleEntry \{[\s\S]*?stored_status: "scheduled" \| "locked" \| "cancelled";[\s\S]*?locked_by_date_rule: boolean;[\s\S]*?locked_effective: boolean;/,
  /export interface PlanDraftPayload \{[\s\S]*?plan\?: PlanVersion;[\s\S]*?schedules\?: PlanScheduleEntry\[\];[\s\S]*?cancellations\?: ProposedSessionCancellation\[\];/,
  /export type DraftKind =[\s\S]*"plan"[\s\S]*"arrangement"/,
  /export type PrescriptionRecordType =[\s\S]*external_load_reps[\s\S]*bodyweight_reps[\s\S]*timed/,
  /plan_action_unavailable/,
  /export interface PlanSafetyReview \{[\s\S]*?red_flag_blocked: boolean;/,
];
check(
  "契约含 D9 payload／锁定双态／DraftKind=plan+arrangement／plan_action_unavailable",
  need.every((re) => re.test(contract)),
  need.map((re, i) => (re.test(contract) ? null : `#${i + 1}`)).filter(Boolean).join(","),
);
check(
  "契约不再把 PlanBlock／PlanScope 当作真相导出",
  !/export interface PlanBlock \{/.test(contract) &&
    !/export interface PlanScope \{/.test(contract),
);

console.log(failed === 0 ? "\n全部通过" : `\n${failed} 项失败`);
process.exit(failed === 0 ? 0 : 1);
