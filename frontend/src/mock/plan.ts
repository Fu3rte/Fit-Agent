/**
 * mock 计划生成与安全前置校验（对齐 stage3 D9 / backend domain/plan）
 *
 * 输入 = 正式档案 + 当前有效限制，输出 = 拟议 D9 PlanPayload（plan_workouts + calendar_cycle）。
 * - 数据形状一律来自 src/lib/contract.ts（D1A），不另写第二份形状。
 * - 动作身份一律取自 ./catalog.ts 已拍 24 项（planExercise 构造期即拒绝目录外身份）。
 * - 无可信训练记录：external_load_reps 一律 needs_calibration，不得出现 VerifiedLoad（D3）。
 * - 校准 pass 文案：稳定完成该组处方次数下限；RIR 不作通过硬性条件（D3）。
 * - 轻量纠错：开始/复核日期、训练日（cycle slot）、动作候选、组数、次数区间、目标 RIR。
 */
import type {
  CalendarCycle,
  CycleSlot,
  DisplaySnapshot,
  FieldDiff,
  IntRange,
  NeedsCalibration,
  PlanCandidate,
  PlanDraftPayload,
  PlanExerciseItem,
  PlanPayload,
  PlanPrescription,
  PlanScheduleEntry,
  PlanSafetyConflict,
  PlanSafetyReview,
  PlanVersion,
  PlanWorkout,
  PrescriptionRecordType,
  Progression,
  ProgressionMethod,
  Profile,
  RepsPrescription,
  Restriction,
} from "../lib/contract";
import {
  derivePlanBlocks,
  deriveRangeLabel,
  effortPlainLabel,
  isIsoDate,
  progressionLabel,
  projectSchedules,
  rebuildCycleSlots,
} from "../lib/planView.ts";
import { CATALOG, isRecommendableCandidate } from "./catalog.ts";

/** 目录 record_type → 处方 record_type（rules.CATALOG_RECORD_TYPE_TO_PRESCRIPTION） */
export const CATALOG_TO_PRESCRIPTION: Record<
  string,
  PrescriptionRecordType
> = {
  reps_weight: "external_load_reps",
  reps_bodyweight: "bodyweight_reps",
  time: "timed",
};

/** 渐进规则文本（抄后端 PROGRESSION_RULES；custom 也必须有 rule） */
export const PROGRESSION_RULES: Record<ProgressionMethod, string> = {
  double_progression:
    "在次数区间内稳定完成全部工作组后先加次数；达到区间上限后按器械允许的最小增量加重，并回到次数下限",
  repetition_progression:
    "先增加次数，达到次数区间上限后按最小增量加重（自重动作改增加次数或难度），并回到次数下限",
  duration_progression:
    "稳定达到时长区间上限后，按最小档位增加负荷或难度，并回到时长下限",
  custom: "按训练者与教练约定的明确规则渐进（须在本字段写清）",
};

/**
 * D3 校准口径：pass = 稳定完成该组处方次数下限；stop = 疼痛/失稳/完不成下限。
 * RIR 只作展示参考，不作通过硬性条件；结构上不携带任何重量。
 */
export const NEEDS_CALIBRATION: NeedsCalibration = {
  kind: "needs_calibration",
  steps: [
    "从该动作最轻可用档位（自重动作取最轻辅助档）完成一组热身",
    "逐级加重，每级做 5 次，观察动作是否稳定",
    "以能稳定完成处方次数下限的档位为起始负荷",
  ],
  pass_criteria: "稳定完成该组处方次数下限",
  stop_criteria: "出现疼痛或其他不适、动作明显失稳，或无法完成处方次数下限时停止，不继续加重",
};

/** 目录器械变式 → 档案器械词与展示文案 */
const EQUIPMENT: Record<string, { label: string; aliases: string[] }> = {
  barbell: { label: "杠铃", aliases: ["杠铃"] },
  dumbbell: { label: "哑铃", aliases: ["哑铃", "哑铃凳"] },
  cable: { label: "绳索", aliases: ["绳索", "龙门架"] },
  bodyweight: { label: "自重", aliases: ["引体架", "单杠", "自重", "徒手"] },
  leverage_machine: { label: "器械", aliases: ["器械", "固定器械"] },
  sled_machine: { label: "器械", aliases: ["器械", "固定器械"] },
};

export { weekdayLabel } from "../lib/planView.ts";

/** 计划动作处方（构造器入参） */
export interface PlanExerciseRx {
  work_sets: number;
  reps: IntRange;
  target_rir?: IntRange;
  /** 计时型：秒区间；给出时忽略 reps */
  duration_seconds?: IntRange;
  progression_method: ProgressionMethod;
}

/** 目录 → 处方口径 + 展示快照冻结 */
function snapshotOf(exercise_id: string): {
  catalog: (typeof CATALOG)[number];
  record_type: PrescriptionRecordType;
  display_snapshot: DisplaySnapshot;
} {
  const catalog = CATALOG.find((e) => e.id === exercise_id);
  if (!catalog)
    throw new Error(
      `计划引用了目录外动作身份：${exercise_id}（不得手写第二份身份）`,
    );
  const record_type = CATALOG_TO_PRESCRIPTION[catalog.record_type];
  if (!record_type)
    throw new Error(`目录记录口径无法映射处方口径：${catalog.record_type}`);
  return {
    catalog,
    record_type,
    display_snapshot: {
      name: catalog.standard_name_zh,
      equipment_variant: catalog.equipment_variant,
      load_convention: catalog.load_convention,
    },
  };
}

/**
 * 按目录身份构造 D9 PlanExerciseItem：item_key 由调给定（workout 内唯一）；
 * 无可信记录时 external_load_reps 一律 needs_calibration。
 */
export function planExercise(
  item_key: string,
  exercise_id: string,
  rx: PlanExerciseRx,
): PlanExerciseItem {
  const { record_type, display_snapshot } = snapshotOf(exercise_id);
  const progression: Progression = {
    method: rx.progression_method,
    rule: PROGRESSION_RULES[rx.progression_method],
  };
  let prescription: PlanPrescription;
  if (record_type === "timed") {
    prescription = {
      kind: "timed",
      work_sets: rx.work_sets,
      duration_seconds_range: rx.duration_seconds ?? { min: 30, max: 60 },
    };
  } else {
    const reps: RepsPrescription = {
      kind: "reps",
      work_sets: rx.work_sets,
      reps_range: rx.reps,
    };
    if (rx.target_rir) reps.target_rir = rx.target_rir;
    prescription = reps;
  }
  const item: PlanExerciseItem = {
    item_key,
    exercise_id,
    display_snapshot,
    record_type,
    prescription,
    progression,
  };
  if (record_type === "external_load_reps") {
    item.load = { ...NEEDS_CALIBRATION };
  }
  return item;
}

/** 候选日期（mock 当前 2026-09-10）：开始 2026-09-14（周一）、复核 2026-10-12 */
export const PLAN_CANDIDATE = {
  starts_on: "2026-09-14",
  review_on: "2026-10-12",
};

/** PPL 首版模板（与后端 service.PPL_CALENDAR_SLOTS 对齐）：push/rest/pull/rest/legs/rest/rest */
export const PPL_TEMPLATE_KEY = "ppl";

interface TemplateExercise {
  id: string;
  rx: PlanExerciseRx;
}

const PPL_WORKOUTS: readonly {
  workout_key: string;
  name: string;
  exercises: readonly TemplateExercise[];
}[] = [
  {
    workout_key: "push",
    name: "推日",
    exercises: [
      {
        id: "barbell-bench-press",
        rx: {
          work_sets: 4,
          reps: { min: 6, max: 8 },
          target_rir: { min: 1, max: 3 },
          progression_method: "double_progression",
        },
      },
      {
        id: "seated-dumbbell-shoulder-press",
        rx: {
          work_sets: 3,
          reps: { min: 8, max: 12 },
          target_rir: { min: 1, max: 3 },
          progression_method: "double_progression",
        },
      },
      {
        id: "parallel-bar-dip",
        rx: {
          work_sets: 3,
          reps: { min: 8, max: 12 },
          target_rir: { min: 1, max: 3 },
          progression_method: "repetition_progression",
        },
      },
      {
        id: "cable-pushdown",
        rx: {
          work_sets: 3,
          reps: { min: 10, max: 15 },
          target_rir: { min: 1, max: 3 },
          progression_method: "repetition_progression",
        },
      },
    ],
  },
  {
    workout_key: "pull",
    name: "拉日",
    exercises: [
      {
        id: "pull-up",
        rx: {
          work_sets: 3,
          reps: { min: 6, max: 10 },
          target_rir: { min: 1, max: 3 },
          progression_method: "repetition_progression",
        },
      },
      {
        id: "barbell-bent-over-row",
        rx: {
          work_sets: 4,
          reps: { min: 8, max: 10 },
          target_rir: { min: 1, max: 3 },
          progression_method: "double_progression",
        },
      },
      {
        id: "dumbbell-biceps-curl",
        rx: {
          work_sets: 3,
          reps: { min: 10, max: 12 },
          target_rir: { min: 1, max: 3 },
          progression_method: "repetition_progression",
        },
      },
      {
        id: "hanging-leg-raise",
        rx: {
          work_sets: 3,
          reps: { min: 10, max: 15 },
          target_rir: { min: 1, max: 3 },
          progression_method: "repetition_progression",
        },
      },
    ],
  },
  {
    workout_key: "legs",
    name: "腿日",
    exercises: [
      {
        id: "barbell-back-squat",
        rx: {
          work_sets: 4,
          reps: { min: 6, max: 8 },
          target_rir: { min: 1, max: 3 },
          progression_method: "double_progression",
        },
      },
      {
        id: "barbell-romanian-deadlift",
        rx: {
          work_sets: 3,
          reps: { min: 8, max: 10 },
          target_rir: { min: 1, max: 3 },
          progression_method: "double_progression",
        },
      },
      {
        id: "bulgarian-split-squat",
        rx: {
          work_sets: 3,
          reps: { min: 8, max: 12 },
          target_rir: { min: 1, max: 3 },
          progression_method: "repetition_progression",
        },
      },
      {
        id: "hanging-leg-raise",
        rx: {
          work_sets: 3,
          reps: { min: 10, max: 15 },
          target_rir: { min: 1, max: 3 },
          progression_method: "repetition_progression",
        },
      },
    ],
  },
];

/** 标准 PPL 7 日循环：push/rest/pull/rest/legs/rest/rest */
export const PPL_CALENDAR_SLOTS: readonly CycleSlot[] = [
  { kind: "workout", workout_key: "push" },
  { kind: "rest" },
  { kind: "workout", workout_key: "pull" },
  { kind: "rest" },
  { kind: "workout", workout_key: "legs" },
  { kind: "rest" },
  { kind: "rest" },
];

/** 预计时长口径（确定性）：热身 8 分钟 + 每组 3 分钟 */
function estimatedMinutes(exercises: PlanExerciseItem[]): number {
  return (
    8 +
    3 *
      exercises.reduce(
        (n, e) => n + e.prescription.work_sets,
        0,
      )
  );
}

function equipmentAvailable(
  equipment_variant: string,
  profile_equipment: string[],
): boolean {
  const entry = EQUIPMENT[equipment_variant];
  if (!entry) return false;
  return entry.aliases.some((alias) => profile_equipment.includes(alias));
}

function catalogViolation(
  exercise_id: string,
  profile: Profile,
  restrictions: Restriction[],
): string | undefined {
  const catalog = CATALOG.find((e) => e.id === exercise_id);
  if (!catalog) return `目录外动作身份：${exercise_id}`;
  if (!isRecommendableCandidate(catalog))
    return `动作不可推荐（未通过身份／来源／active／器械／记录与负重口径／模式完整性检查）：${catalog.standard_name_zh}`;
  if (!equipmentAvailable(catalog.equipment_variant, profile.equipment))
    return `动作所需器械不在档案器械内：${catalog.standard_name_zh}`;
  const hit = restrictions.find((r) =>
    r.scope === "specific_action"
      ? r.name === catalog.standard_name_zh
      : catalog.modes.includes(r.name),
  );
  if (hit)
    return `动作命中限制「${hit.name}」（${hit.scope}）：${catalog.standard_name_zh}`;
  return undefined;
}

export function planCandidates(
  profile: Profile,
  restrictions: Restriction[],
): PlanCandidate[] {
  return CATALOG.filter(
    (c) => catalogViolation(c.id, profile, restrictions) === undefined,
  ).map((c) => ({
    exercise_id: c.id,
    name: c.standard_name_zh,
    variant: EQUIPMENT[c.equipment_variant]?.label ?? c.equipment_variant,
  }));
}

/**
 * 展示 Diff（A4）：按 D9 payload 派生字段级「旧值→新值」。
 */
export function planDraftDiff(
  plan: PlanVersion,
  schedules: PlanScheduleEntry[],
  replacement?: { previous_version: string },
): FieldDiff[] {
  const blocks = derivePlanBlocks(plan.payload);
  const weekdays = blocks
    .map((b) => b.weekday)
    .filter((w): w is number => w !== undefined)
    .sort((a, b) => a - b);
  const rows: FieldDiff[] = [
    replacement
      ? {
          field: "计划 · 版本",
          old_value: replacement.previous_version,
          new_value: `${plan.version}（替换启用；旧版归档保留）`,
        }
      : { field: "计划 · 版本", new_value: `${plan.version}（新建）` },
    {
      field: "生效范围",
      new_value: `${plan.starts_on} 起，复核日期 ${plan.review_on}`,
    },
    {
      field: "每周训练日",
      new_value: `${[...new Set(weekdays)].join(" / ")}，共 ${schedules.length} 个应训练日`,
    },
    {
      field: "负荷",
      new_value:
        "无可信训练记录：不给起始重量，外加负重动作按「需要校准」逐级试重",
    },
  ];
  for (const block of blocks) {
    rows.push({
      field: `${block.name} · 动作（预计 ${block.estimated_minutes} 分钟）`,
      new_value: block.exercises
        .map((e) => {
          const effort =
            e.prescription.kind === "reps" && e.prescription.target_rir
              ? `，${effortPlainLabel(e.prescription.target_rir)}`
              : "";
          const sets = e.prescription.work_sets;
          const range =
            e.prescription.kind === "reps"
              ? deriveRangeLabel(e.prescription.reps_range)
              : `${deriveRangeLabel(e.prescription.duration_seconds_range)} 秒`;
          return `${e.display_snapshot.name} ${sets} 组 x ${range}${effort}（${progressionLabel(e.progression.method)}）`;
        })
        .join(" → "),
    });
  }
  return rows;
}

/** 六类安全症状（02 2.3）：读取时对 body_conditions 原文做确定性匹配 */
const SAFETY_SYMPTOM_PATTERNS: readonly { label: string; pattern: RegExp }[] = [
  { label: "胸部异常不适", pattern: /胸部(?:异常|明显|持续)(?:不适|闷|痛)/ },
  { label: "晕厥", pattern: /晕厥|昏厥|晕倒/ },
  { label: "异常气短", pattern: /气短|喘不上气|呼吸困难/ },
  { label: "锐痛", pattern: /锐痛|刺痛/ },
  { label: "麻木", pattern: /麻木|发麻/ },
  { label: "放射痛", pattern: /放射痛|放射到|放射至/ },
];

export interface BodyConditionClassification {
  confirmed: string[];
  unlisted: string[];
}

export function classifyBodyConditions(
  body_conditions: readonly string[] | undefined,
): BodyConditionClassification {
  const confirmed: string[] = [];
  const unlisted: string[] = [];
  for (const condition of body_conditions ?? []) {
    const hit = SAFETY_SYMPTOM_PATTERNS.some(({ pattern }) =>
      pattern.test(condition),
    );
    const bucket = hit ? confirmed : unlisted;
    if (!bucket.includes(condition)) bucket.push(condition);
  }
  return { confirmed, unlisted };
}

export interface PlanDraftBlocked {
  ok: false;
  code: "no_profile" | "red_flag" | "unschedulable";
  reason: string;
  red_flags: string[];
}

export interface PlanDraftReady {
  ok: true;
  plan: PlanVersion;
  schedules: PlanScheduleEntry[];
  payload: PlanDraftPayload;
}

export type PlanDraftBuild = PlanDraftBlocked | PlanDraftReady;

/** 构造 PPL PlanPayload（不写库；workout 被器械/限制过滤后整体 fail-closed） */
function buildPplPayload(
  profile: Profile,
  restrictions: Restriction[],
  anchor_date: string,
): { payload: PlanPayload } | { error: string } {
  const plan_workouts: PlanWorkout[] = [];
  for (const template of PPL_WORKOUTS) {
    const exercises: PlanExerciseItem[] = [];
    template.exercises.forEach((e, i) => {
      if (catalogViolation(e.id, profile, restrictions) === undefined) {
        exercises.push(
          planExercise(`${template.workout_key}-${i + 1}`, e.id, e.rx),
        );
      }
    });
    if (exercises.length === 0)
      return { error: `${template.name}的动作全部被器械或限制过滤，没有可用动作` };
    plan_workouts.push({
      workout_key: template.workout_key,
      name: template.name,
      estimated_minutes: estimatedMinutes(exercises),
      exercises,
    });
  }
  return {
    payload: {
      schema_version: 1,
      template_key: PPL_TEMPLATE_KEY,
      plan_workouts,
      calendar_cycle: {
        anchor_date,
        slots: PPL_CALENDAR_SLOTS.map((s) => ({ ...s })),
      },
    },
  };
}

/** 从正式档案与当前有效限制生成 PPL 计划草稿（D9） */
export function buildPplDraft(input: {
  profile: Profile | null;
  restrictions: Restriction[];
}): PlanDraftBuild {
  const { profile, restrictions } = input;
  if (!profile)
    return {
      ok: false,
      code: "no_profile",
      reason: "尚未建立正式档案，不生成计划处方",
      red_flags: [],
    };
  const red_flags = classifyBodyConditions(profile.body_conditions).confirmed;
  if (red_flags.length > 0)
    return {
      ok: false,
      code: "red_flag",
      reason: `身体情况命中安全症状：${red_flags.join("、")}`,
      red_flags,
    };

  const built = buildPplPayload(
    profile,
    restrictions,
    PLAN_CANDIDATE.starts_on,
  );
  if ("error" in built)
    return {
      ok: false,
      code: "unschedulable",
      reason: built.error,
      red_flags: [],
    };

  const payload = built.payload;
  /* 循环训练日折合每周次数 = workout 槽数；超档案频率即 fail-closed */
  const workoutDays = payload.calendar_cycle.slots.filter(
    (s) => s.kind === "workout",
  ).length;
  if (workoutDays > profile.weekly_frequency)
    return {
      ok: false,
      code: "unschedulable",
      reason: `档案每周频率 ${profile.weekly_frequency} 次，排不进循环的 ${workoutDays} 个训练日`,
      red_flags: [],
    };

  const plan: PlanVersion = {
    version: "v1",
    starts_on: PLAN_CANDIDATE.starts_on,
    review_on: PLAN_CANDIDATE.review_on,
    mode: "regular",
    status: "active",
    payload,
  };
  const schedules = projectSchedules(plan.version, payload, {
    starts_on: plan.starts_on,
    review_on: plan.review_on,
  });
  const draftPayload: PlanDraftPayload = {
    title: `PPL 训练计划（新建 ${plan.version}）`,
    diff: planDraftDiff(plan, schedules),
    plan,
    schedules,
    cancellations: [],
    candidates: planCandidates(profile, restrictions),
  };
  const invalid = planPayloadError(draftPayload, { profile, restrictions });
  if (invalid)
    return {
      ok: false,
      code: "unschedulable",
      reason: `生成的计划未通过安全前置校验：${invalid}`,
      red_flags: [],
    };
  return { ok: true, plan, schedules, payload: draftPayload };
}

/**
 * 计划草稿的确定性安全前置校验（生成自检与纠错／确认共用）。
 * 返回 undefined = 通过；否则返回具体违规说明。
 */
export function planPayloadError(
  payload: PlanDraftPayload,
  ctx: {
    profile: Profile | null;
    restrictions: Restriction[];
    today?: string;
  },
): string | undefined {
  const { profile, restrictions } = ctx;
  if (!profile) return "尚未建档：不生成计划处方";
  const redFlags = classifyBodyConditions(profile.body_conditions).confirmed;
  if (redFlags.length > 0)
    return `身体情况命中安全症状（${redFlags.join("、")}）：不生成常规计划处方`;

  const plan = payload.plan;
  const schedules = payload.schedules;
  if (!plan || !schedules)
    return "缺少结构化计划载荷（计划版本／具体日程）";
  if (!isIsoDate(plan.starts_on) || !isIsoDate(plan.review_on))
    return "开始／复核日期须为有效日期（YYYY-MM-DD）";
  if (ctx.today !== undefined && plan.starts_on < ctx.today)
    return `开始日期 ${plan.starts_on} 早于当前日期 ${ctx.today}`;
  if (plan.starts_on >= plan.review_on)
    return "生效范围须满足开始日期早于复核日期（[开始日期, 复核日期)）";
  if (plan.mode !== "regular" && plan.mode !== "return")
    return `计划模式不在已拍集合内：${plan.mode}`;
  if (plan.payload.schema_version !== 1)
    return `payload schema_version 必须为 1：${plan.payload.schema_version}`;
  if (plan.payload.plan_workouts.length === 0)
    return "计划没有任何 plan_workouts";

  const cycle = plan.payload.calendar_cycle;
  if (!isIsoDate(cycle.anchor_date)) return "日历循环锚点须为有效日期";
  if (cycle.slots.length === 0) return "日历循环 slots 不能为空";
  const workoutKeys = new Set(
    plan.payload.plan_workouts.map((w) => w.workout_key),
  );
  let hasWorkoutSlot = false;
  for (const slot of cycle.slots) {
    if (slot.kind === "workout") {
      if (!workoutKeys.has(slot.workout_key))
        return `循环槽引用了不存在的 workout_key：${slot.workout_key}`;
      hasWorkoutSlot = true;
    }
  }
  if (!hasWorkoutSlot) return "日历循环至少要有一个 workout 槽";

  for (const workout of plan.payload.plan_workouts) {
    if (!workout.workout_key.trim()) return "workout_key 不能为空";
    if (!workout.name.trim()) return `${workout.workout_key} 缺名称`;
    if (!(workout.estimated_minutes > 0))
      return `${workout.name}缺预计时长`;
    if (workout.estimated_minutes > profile.session_minutes)
      return `${workout.name}预计 ${workout.estimated_minutes} 分钟超过档案单次可用时长 ${profile.session_minutes} 分钟`;
    if (workout.exercises.length === 0) return `${workout.name}没有动作`;
    const itemKeys = new Set<string>();
    const exerciseIds = new Set<string>();
    for (const ex of workout.exercises) {
      if (itemKeys.has(ex.item_key))
        return `${workout.name}内 item_key 重复：${ex.item_key}`;
      itemKeys.add(ex.item_key);
      if (exerciseIds.has(ex.exercise_id))
        return `${workout.name}重复同一动作身份：${ex.exercise_id}`;
      exerciseIds.add(ex.exercise_id);
      const violation = catalogViolation(
        ex.exercise_id,
        profile,
        restrictions,
      );
      if (violation) return `${workout.name}：${violation}`;
      const err = itemStructuralError(ex, workout.name);
      if (err) return err;
    }
  }

  return scheduleViolation(plan, schedules);
}

function itemStructuralError(
  ex: PlanExerciseItem,
  label: string,
): string | undefined {
  if (!ex.item_key.trim()) return `${label}动作缺 item_key`;
  if (!ex.display_snapshot.name.trim())
    return `${label}动作缺展示名`;
  if (ex.record_type === "timed") {
    if (ex.prescription.kind !== "timed")
      return `${label} timed 处方必须是计时型：${ex.item_key}`;
    if (!(ex.prescription.work_sets > 0))
      return `${label}动作组数须为正数：${ex.item_key}`;
    const r = ex.prescription.duration_seconds_range;
    if (!(r.min >= 1 && r.max >= r.min))
      return `${label}时长区间须满足 1 ≤ min ≤ max：${ex.item_key}`;
    if (ex.load !== undefined)
      return `${label} 计时型不得携带负荷：${ex.item_key}`;
  } else {
    if (ex.prescription.kind !== "reps")
      return `${label} ${ex.record_type} 处方必须是次数型：${ex.item_key}`;
    if (!(ex.prescription.work_sets > 0))
      return `${label}动作组数须为正数：${ex.item_key}`;
    const r = ex.prescription.reps_range;
    if (!(r.min >= 1 && r.max >= r.min))
      return `${label}次数区间须满足 1 ≤ min ≤ max：${ex.item_key}`;
    const rir = ex.prescription.target_rir;
    if (rir && !(rir.min >= 0 && rir.max >= rir.min))
      return `${label}目标 RIR 须满足 0 ≤ min ≤ max：${ex.item_key}`;
  }
  if (ex.record_type === "external_load_reps") {
    const load = ex.load;
    // F5-06：有可信记录时允许 verified（basis_record_revision_id 必填）；
    // 无可信记录仍必须 needs_calibration（D3 不猜重）。种子/首版生成路径仍恒为 needs_calibration。
    if (!load) return `${label}外加负重动作必须携带负荷形态（verified | needs_calibration）：${ex.item_key}`;
    if (load.kind === "verified") {
      if (!(load.value > 0))
        return `${label}已验证负荷须为正数：${ex.item_key}`;
      if (!load.load_notation.trim())
        return `${label}已验证负荷缺负重口径 load_notation：${ex.item_key}`;
      if (!load.basis_record_revision_id?.trim())
        return `${label}已验证负荷必须标注 basis_record_revision_id（可信历史来源）：${ex.item_key}`;
    } else if (load.kind === "needs_calibration") {
      if (
        load.steps.length === 0 ||
        load.pass_criteria.trim() === "" ||
        load.stop_criteria.trim() === ""
      )
        return `${label}动作须给出完整校准说明：${ex.item_key}`;
      if (/RIR/.test(load.pass_criteria))
        return `${label}校准通过标准不得把 RIR 当硬性条件（D3）：${ex.item_key}`;
    } else {
      return `${label}负荷形态不在已拍两态内：${ex.item_key}`;
    }
  } else if (ex.load !== undefined) {
    return `${label} load 仅用于外加负重动作：${ex.item_key}`;
  }
  const allowed: ProgressionMethod[] =
    ex.record_type === "external_load_reps"
      ? ["double_progression", "repetition_progression", "custom"]
      : ex.record_type === "bodyweight_reps"
        ? ["repetition_progression", "custom"]
        : ["duration_progression", "custom"];
  if (!allowed.includes(ex.progression.method))
    return `${label}渐进方式 ${ex.progression.method} 与 ${ex.record_type} 不匹配：${ex.item_key}`;
  if (!ex.progression.rule.trim())
    return `${label}渐进规则文本不能为空：${ex.item_key}`;
  return undefined;
}

/** 具体日程须与 payload 投影严格一致（新生成一律 scheduled） */
function scheduleViolation(
  plan: PlanVersion,
  schedules: PlanScheduleEntry[],
): string | undefined {
  const expected = projectSchedules(plan.version, plan.payload, {
    starts_on: plan.starts_on,
    review_on: plan.review_on,
  });
  if (schedules.length !== expected.length)
    return `具体日程 ${schedules.length} 条，与生效范围应有的 ${expected.length} 条不一致`;
  for (const e of expected) {
    const hit = schedules.find((s) => s.date === e.date);
    if (!hit) return `具体日程缺 ${e.date}`;
    if (hit.plan_workout_key !== e.plan_workout_key)
      return `具体日程 ${e.date} 的训练日引用不一致`;
    if (hit.plan_version !== plan.version)
      return `具体日程 ${e.date} 归属计划版本不一致`;
    if (hit.stored_status !== "scheduled")
      return `新生成计划的日程状态须为 scheduled：${e.date}`;
  }
  return undefined;
}

/**
 * 使用时整份安全复核：目录身份读不到 → usable=false 且 block_code=plan_action_unavailable（S3-07）。
 */
export function reviewPlanSafety(input: {
  plan: PlanVersion;
  profile: Profile | null;
  restrictions: Restriction[];
  context_version: number;
}): PlanSafetyReview {
  const conflicts: PlanSafetyConflict[] = [];
  let catalogMissing = false;
  for (const workout of input.plan.payload.plan_workouts) {
    for (const ex of workout.exercises) {
      const catalog = CATALOG.find((c) => c.id === ex.exercise_id);
      if (!catalog) {
        catalogMissing = true;
        continue;
      }
      const name = catalog.standard_name_zh;
      const restriction = input.restrictions.find((r) =>
        r.scope === "specific_action"
          ? r.name === name
          : catalog.modes.includes(r.name),
      );
      if (restriction)
        conflicts.push({
          exercise_id: ex.exercise_id,
          exercise_name: name,
          restriction,
        });
    }
  }
  const red_flag_blocked =
    classifyBodyConditions(input.profile?.body_conditions).confirmed.length > 0;
  const usable =
    input.profile !== null &&
    !red_flag_blocked &&
    conflicts.length === 0 &&
    !catalogMissing;
  return {
    context_version: input.context_version,
    reviewed_at: new Date().toISOString(),
    red_flag_blocked,
    usable,
    conflicts,
    ...(catalogMissing
      ? ({ block_code: "plan_action_unavailable" } as const)
      : {}),
  };
}

/** 纠错后的计划动作：身份与展示文案按目录重建；目录外身份原样保留 */
function canonicalExercise(
  item_key: string,
  ex: PlanExerciseItem,
): PlanExerciseItem | { error: string } {
  const catalog = CATALOG.find((c) => c.id === ex.exercise_id);
  if (!catalog) return ex; // 交由校验以「目录外」拒绝
  try {
    const rx: PlanExerciseRx =
      ex.prescription.kind === "timed"
        ? {
            work_sets: ex.prescription.work_sets,
            reps: { min: 1, max: 1 },
            duration_seconds: ex.prescription.duration_seconds_range,
            progression_method: ex.progression.method,
          }
        : {
            work_sets: ex.prescription.work_sets,
            reps: ex.prescription.reps_range,
            target_rir: ex.prescription.target_rir,
            progression_method: ex.progression.method,
          };
    return planExercise(item_key, ex.exercise_id, rx);
  } catch (e) {
    return { error: e instanceof Error ? e.message : "动作重建失败" };
  }
}

export type PlanPayloadRevision =
  | { ok: true; payload: PlanDraftPayload }
  | { ok: false; error: string };

/**
 * 计划草稿纠错后的服务端归一化与复检（F2-03；`/api/drafts/:id/revise` 唯一入口）。
 * 只从 requested 取允许纠错的字段：starts_on/review_on、各 workout 的展示 weekday
 * （经 cycle 重建）、每个动作的目录身份、组数、次数区间与目标 RIR；其余以服务端存储为准。
 */
export function normalizePlanPayload(
  stored: PlanDraftPayload,
  requested: PlanDraftPayload,
  ctx: {
    profile: Profile | null;
    restrictions: Restriction[];
    today: string;
  },
): PlanPayloadRevision {
  const basePlan = stored.plan;
  const plan = requested.plan;
  if (!basePlan)
    return {
      ok: false,
      error: "待确认草稿缺少结构化计划载荷（计划版本）",
    };
  if (!plan)
    return {
      ok: false,
      error: "缺少结构化计划载荷（计划版本）",
    };
  if (!isIsoDate(plan.starts_on) || !isIsoDate(plan.review_on))
    return { ok: false, error: "开始／复核日期须为有效日期（YYYY-MM-DD）" };
  if (
    plan.payload.plan_workouts.length !== basePlan.payload.plan_workouts.length
  )
    return { ok: false, error: "轻量纠错不得增删训练日" };

  const blocks = derivePlanBlocks(basePlan.payload);
  const plan_workouts: PlanWorkout[] = [];
  for (let bi = 0; bi < basePlan.payload.plan_workouts.length; bi++) {
    const baseWorkout = basePlan.payload.plan_workouts[bi];
    const requestedWorkout = plan.payload.plan_workouts[bi];
    if (!baseWorkout || !requestedWorkout)
      return { ok: false, error: "轻量纠错不得增删训练日" };
    if (requestedWorkout.exercises.length !== baseWorkout.exercises.length)
      return { ok: false, error: "轻量纠错不得增删动作" };
    const exercises: PlanExerciseItem[] = [];
    for (let ei = 0; ei < baseWorkout.exercises.length; ei++) {
      const baseEx = baseWorkout.exercises[ei];
      const requestedEx = requestedWorkout.exercises[ei];
      if (!baseEx || !requestedEx)
        return { ok: false, error: "轻量纠错不得增删动作" };
      const rebuilt = canonicalExercise(baseEx.item_key, {
        ...baseEx,
        exercise_id: requestedEx.exercise_id,
        prescription: requestedEx.prescription,
      });
      if ("error" in rebuilt) return { ok: false, error: rebuilt.error };
      exercises.push(rebuilt);
    }
    plan_workouts.push({
      ...baseWorkout,
      exercises,
      estimated_minutes: estimatedMinutes(exercises),
    });
  }

  /* 训练日：按 requested 各 workout 的展示 weekday 重建 cycle（anchor = starts_on） */
  const weekdays = plan_workouts.map((w, i) => ({
    workout_key: w.workout_key,
    weekday:
      derivePlanBlocks(plan.payload).find(
        (b) => b.workout_key === w.workout_key,
      )?.weekday ??
      blocks[i]?.weekday ??
      1,
  }));
  /* 若 requested 未改 weekday（同 key 同 weekday），保留原 cycle 相位（anchor 不动） */
  const sameWeekdays = weekdays.every(
    (w, i) => w.weekday === blocks[i]?.weekday,
  );
  const calendar_cycle: CalendarCycle = sameWeekdays
    ? { ...basePlan.payload.calendar_cycle }
    : {
        anchor_date: plan.starts_on,
        slots: rebuildCycleSlots(plan.starts_on, weekdays),
      };

  const nextPayload: PlanPayload = {
    ...basePlan.payload,
    plan_workouts,
    calendar_cycle,
  };
  const nextPlan: PlanVersion = {
    ...basePlan,
    starts_on: plan.starts_on,
    review_on: plan.review_on,
    payload: nextPayload,
  };
  const schedules = projectSchedules(nextPlan.version, nextPayload, {
    starts_on: nextPlan.starts_on,
    review_on: nextPlan.review_on,
  });
  const payload: PlanDraftPayload = {
    ...stored,
    plan: nextPlan,
    schedules,
    ...(ctx.profile
      ? { candidates: planCandidates(ctx.profile, ctx.restrictions) }
      : {}),
  };
  const error = planPayloadError(payload, ctx);
  if (error) return { ok: false, error };
  return {
    ok: true,
    payload: {
      ...payload,
      diff: planDraftDiff(nextPlan, schedules),
    },
  };
}
