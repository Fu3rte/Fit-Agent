/**
 * F2-01 验收探针（只读，无依赖）：目录身份／模式／器械一致性 + 可推荐谓词 + mock 计划种子身份。
 *
 * 运行：node scripts/f2-01-catalog-probe.mjs
 * 覆盖：24 项与后端 003 迁移逐字段一致且无第 25 项；未受检／inactive 动作被谓词拒绝；
 *      mock 计划种子只引用目录 ID；计划／日程结构化字段存在于契约。
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

/* 1. 身份／模式／器械：与后端 Stage 1 已拍 24 项清单逐字段对照 */
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

check("后端清单为 24 项", rows.length === 24, `实际 ${rows.length}`);
check(
  "前端镜像为 24 项（无第 25 项）",
  CATALOG.length === 24,
  `实际 ${CATALOG.length}`,
);

const byId = new Map(CATALOG.map((e) => [e.id, e]));
check(
  "身份集合完全一致",
  rows.every((r) => byId.has(r.id)) && byId.size === rows.length,
  `缺 ${
    rows
      .filter((r) => !byId.has(r.id))
      .map((r) => r.id)
      .join(",") || "无"
  }；多 ${
    [...byId.keys()].filter((id) => !rows.some((r) => r.id === id)).join(",") ||
    "无"
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

/* 3. mock 计划种子：可执行计划一律引用目录 ID（构造器见 src/mock/plan.ts） */
const server = read("../src/mock/server.ts");
const seeded = [...server.matchAll(/planExercise\("([^"]+)"/g)].map(
  (m) => m[1],
);
check(
  "计划种子已按目录身份生成（9 项）",
  seeded.length === 9,
  `实际 ${seeded.length}`,
);
check(
  "计划种子身份全部在已拍目录内且可推荐",
  seeded.every((id) => byId.has(id) && isRecommendableCandidate(byId.get(id))),
  `越界 ${seeded.filter((id) => !byId.has(id)).join(",") || "无"}`,
);
const legacy = [
  "杠铃卧推",
  "杠铃划船",
  "哑铃肩推",
  "面拉",
  "杠铃深蹲",
  "罗马尼亚硬拉",
  "双杠臂屈伸",
];
const leftovers = legacy.filter((n) =>
  server
    .split("\n")
    .filter((l) => /exercise:|name: "|field: ".+ · /.test(l))
    .some((l) => l.includes(`"${n}"`) || l.includes(`"${n} · `)),
);
check(
  "种子动作名不再出现目录外旧身份",
  leftovers.length === 0,
  leftovers.join(","),
);

/* 4. 结构化计划／日程形状在契约中（不是只塞文本 Diff） */
const contract = read("../src/lib/contract.ts");
const need = [
  /export interface PlanDraftPayload \{[\s\S]*?plan\?: PlanVersion;[\s\S]*?scope\?: PlanScope;[\s\S]*?schedules\?: PlanScheduleEntry\[\];[\s\S]*?cancellations\?: PlanScheduleEntry\[\];/,
  /export interface PlanScheduleEntry \{[\s\S]*?status: "scheduled" \| "locked" \| "cancelled";/,
  /export interface PlanSafetyReview \{[\s\S]*?red_flag_blocked: boolean;/,
  /export interface CatalogExercise \{/,
];
check(
  "计划／日程／安全复核为结构化契约字段",
  need.every((re) => re.test(contract)),
);

console.log(failed === 0 ? "\n全部通过" : `\n${failed} 项失败`);
process.exit(failed === 0 ? 0 : 1);
