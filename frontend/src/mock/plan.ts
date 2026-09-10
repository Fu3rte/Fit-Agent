/**
 * mock 计划生成与安全前置校验（stage2 F2-02；plans/stage2.md §3.1–3.3）
 *
 * 输入 = 正式档案 + 当前有效限制，输出 = 拟议 PPL 计划版本、生效范围与具体日程。
 * 缺档案、档案含红旗症状、或排不进档案约束时一律不给处方（fail-closed），只说明原因。
 *
 * - 数据形状一律来自 src/lib/contract.ts（D1A），不另写第二份形状。
 * - 动作身份一律取自 ./catalog.ts 已拍 24 项（`planExercise` 构造期即拒绝目录外身份），
 *   再按可推荐最小标准、档案器械、具体动作／动作模式限制过滤（3.1）。
 * - 无可信训练记录：所有动作标为「需要校准」，不生成也不暗示具体起始重量（3.2）。
 * - 不建立覆盖任意频率的通用排程算法：候选日期与每周训练日按 3.3 已拍固定值给出（3.3）。
 * - 只依赖相对路径与 type-only 契约导入，因此可被 `node scripts/*.mjs` 直接探针。
 */
import type {
  Calibration,
  FieldDiff,
  PlanBlock,
  PlanCandidate,
  PlanDraftPayload,
  PlanExercise,
  PlanScheduleEntry,
  PlanSafetyConflict,
  PlanSafetyReview,
  PlanScope,
  PlanVersion,
  Profile,
  Restriction,
} from "../lib/contract";
import { CATALOG, isRecommendableCandidate } from "./catalog.ts";

/**
 * 3.2 已拍校准口径（本阶段 mock 无可信训练记录）：不生成也不暗示具体起始重量，
 * 只给逐级试重步骤与通过／停止标准；基于可信历史的负荷建议留到记录数据接入后的阶段。
 * 所有可执行计划动作统一引用本常量，不逐条手写、不伪造已校准重量。
 */
export const NEEDS_CALIBRATION: Calibration = {
  status: "needs_calibration",
  steps: [
    "从该动作最轻可用档位（自重动作取最轻辅助档）完成一组热身",
    "逐级加重，每级做 5 次，观察动作是否稳定",
    "以能稳定完成处方次数下限且落在目标 RIR 区间的档位为起始负荷",
  ],
  pass_criteria: "稳定完成处方次数下限，且落在目标 RIR 区间",
  stop_criteria:
    "出现疼痛或其他不适、动作明显失稳，或无法满足目标 RIR 时停止，不继续加重",
};

/**
 * 目录器械变式 → 档案器械词与展示文案（沿用 stage1 建档短语表词表，不引入新器械名）。
 * 档案器械里出现任一别名即视为该变式可用；未列出的变式一律视为不可用（fail-closed）。
 */
const EQUIPMENT: Record<string, { label: string; aliases: string[] }> = {
  barbell: { label: "杠铃", aliases: ["杠铃"] },
  dumbbell: { label: "哑铃", aliases: ["哑铃", "哑铃凳"] },
  cable: { label: "绳索", aliases: ["绳索", "龙门架"] },
  bodyweight: { label: "自重", aliases: ["引体架", "单杠", "自重", "徒手"] },
  leverage_machine: { label: "器械", aliases: ["器械", "固定器械"] },
  sled_machine: { label: "器械", aliases: ["器械", "固定器械"] },
};

/** 每周训练日展示文案（1-7，周一起） */
const WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

export function weekdayLabel(weekday: number): string {
  return WEEKDAY_LABELS[weekday - 1] ?? String(weekday);
}

/** 计划动作处方（身份与展示文案由目录给出，调用方只给处方与渐进方式） */
export interface PlanExerciseRx {
  sets: number;
  rep_range: string;
  target_rir: string;
  progression: string;
}

/**
 * 按目录身份构造计划动作：name／variant／equipment／modes 一律取自 CATALOG（唯一身份来源），
 * 身份不在已拍 24 项内时构造期即失败，使「可执行计划一律引用目录 ID」成为结构性约束，
 * 而不是靠人工核对（F2-01 种子与 F2-02 生成共用本构造器）。
 */
export function planExercise(
  exercise_id: string,
  rx: PlanExerciseRx,
): PlanExercise {
  const catalog = CATALOG.find((e) => e.id === exercise_id);
  if (!catalog)
    throw new Error(
      `计划引用了目录外动作身份：${exercise_id}（不得手写第二份身份）`,
    );
  const equipment = EQUIPMENT[catalog.equipment_variant];
  if (!equipment)
    throw new Error(
      `目录动作缺少器械变式映射：${exercise_id}（${catalog.equipment_variant}）`,
    );
  return {
    exercise_id: catalog.id,
    name: catalog.standard_name_zh,
    variant: equipment.label,
    equipment: catalog.equipment_variant,
    modes: [...catalog.modes],
    sets: rx.sets,
    rep_range: rx.rep_range,
    target_rir: rx.target_rir,
    progression: rx.progression,
    calibration: NEEDS_CALIBRATION,
  };
}

/**
 * 3.3 已拍候选日期（mock 当前日期 2026-09-10）：开始 2026-09-14、周一／周三／周五、
 * 复核 2026-10-12；只生成 `[开始日期, 复核日期)` 内的应训练日。
 * 该组合只服务本阶段确定性剧本，不是覆盖任意频率的通用排程算法。
 */
export const PLAN_CANDIDATE = {
  start_date: "2026-09-14",
  review_date: "2026-10-12",
  /** 周一 / 周三 / 周五 */
  weekdays: [1, 3, 5],
};

/**
 * PPL 首版模板（每周 3 个训练日）。动作身份全部来自已拍 24 项；同一训练日内不重复身份，
 * 跨训练日复用同一身份（悬垂举腿在拉日与腿日各一次）不判冲突（F2-02 已确认口径）。
 */
const PPL_TEMPLATE: readonly {
  name: string;
  weekday: number;
  exercises: readonly ({ id: string } & PlanExerciseRx)[];
}[] = [
  {
    name: "推日",
    weekday: 1,
    exercises: [
      {
        id: "barbell-bench-press",
        sets: 4,
        rep_range: "6-8",
        target_rir: "1-3",
        progression: "双重渐进",
      },
      {
        id: "seated-dumbbell-shoulder-press",
        sets: 3,
        rep_range: "8-12",
        target_rir: "1-3",
        progression: "双重渐进",
      },
      {
        id: "parallel-bar-dip",
        sets: 3,
        rep_range: "8-12",
        target_rir: "1-3",
        progression: "次数递增",
      },
      {
        id: "cable-pushdown",
        sets: 3,
        rep_range: "10-15",
        target_rir: "1-3",
        progression: "次数递增",
      },
    ],
  },
  {
    name: "拉日",
    weekday: 3,
    exercises: [
      {
        id: "pull-up",
        sets: 3,
        rep_range: "6-10",
        target_rir: "1-3",
        progression: "次数递增",
      },
      {
        id: "barbell-bent-over-row",
        sets: 4,
        rep_range: "8-10",
        target_rir: "1-3",
        progression: "双重渐进",
      },
      {
        id: "dumbbell-biceps-curl",
        sets: 3,
        rep_range: "10-12",
        target_rir: "1-3",
        progression: "次数递增",
      },
      {
        id: "hanging-leg-raise",
        sets: 3,
        rep_range: "10-15",
        target_rir: "1-3",
        progression: "次数递增",
      },
    ],
  },
  {
    name: "腿日",
    weekday: 5,
    exercises: [
      {
        id: "barbell-back-squat",
        sets: 4,
        rep_range: "6-8",
        target_rir: "1-3",
        progression: "双重渐进",
      },
      {
        id: "barbell-romanian-deadlift",
        sets: 3,
        rep_range: "8-10",
        target_rir: "1-3",
        progression: "双重渐进",
      },
      {
        id: "bulgarian-split-squat",
        sets: 3,
        rep_range: "8-12",
        target_rir: "1-3",
        progression: "次数递增",
      },
      {
        id: "hanging-leg-raise",
        sets: 3,
        rep_range: "10-15",
        target_rir: "1-3",
        progression: "次数递增",
      },
    ],
  },
];

/** 预计时长口径（确定性，不做个体建模）：热身 8 分钟 + 每组 3 分钟 */
function estimatedMinutes(exercises: PlanExercise[]): number {
  return 8 + 3 * exercises.reduce((n, e) => n + e.sets, 0);
}

/** 档案器械是否覆盖该目录器械变式 */
function equipmentAvailable(
  equipment_variant: string,
  profile_equipment: string[],
): boolean {
  const entry = EQUIPMENT[equipment_variant];
  if (!entry) return false;
  return entry.aliases.some((alias) => profile_equipment.includes(alias));
}

/**
 * 单个目录动作违反档案约束的原因（undefined = 可用）。生成过滤与草稿校验共用本口径：
 * 只接受可推荐目录动作，并逐项比对档案器械、具体动作限制（同名）与动作模式限制（模式交集）。
 */
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

/**
 * 计划草稿的可替换动作候选（F2-03）：只列当前档案器械与限制都放行的目录动作。
 * 草稿卡只用于「换动作」，不是完整目录浏览器；完整候选资格仍由 `planPayloadError` 把关。
 */
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
 * 具体日程：`[开始日期, 复核日期)` 内逐个应训练日（日历休息日不写成应训练日）。
 * 新生成计划的日程一律 scheduled；锁定（到期即锁）与取消（替换旧版）由启用事务写入。
 */
export function buildSchedules(
  plan_version: string,
  scope: PlanScope,
): PlanScheduleEntry[] {
  const out: PlanScheduleEntry[] = [];
  for (
    let date = scope.start_date;
    date < scope.review_date;
    date = dayAfter(date)
  ) {
    const weekday = weekdayOf(date);
    if (!scope.weekdays.includes(weekday)) continue;
    out.push({
      id: `sched-${plan_version}-${date}`,
      plan_version,
      weekday,
      date,
      status: "scheduled",
    });
  }
  return out;
}

/** 日期（YYYY-MM-DD）后一天；只按日历日推进，不做时区／夏令时换算 */
function dayAfter(date: string): string {
  const d = new Date(`${date}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + 1);
  return d.toISOString().slice(0, 10);
}

/** 有效日历日期（YYYY-MM-DD），且非 2026-02-31 之类的“看起来像日期”的字符串 */
function isIsoDate(date: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return false;
  const d = new Date(`${date}T00:00:00Z`);
  return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === date;
}

/** 每周第几天（1-7，周一起） */
function weekdayOf(date: string): number {
  return ((new Date(`${date}T00:00:00Z`).getUTCDay() + 6) % 7) + 1;
}

/**
 * 计划草稿展示 Diff（A4：字段级「旧值→新值」；新建计划无旧值 = 全部为新增）。
 * 替换计划（F2-04）只写「旧版 → 新版」的版本行；旧版未来未锁定日程取消清单不进产品 UI
 * 展示（owner 2026-09-10 呈现覆盖），取消本身仍由启用事务写入（04 4.4）。
 */
export function planDraftDiff(
  plan: PlanVersion,
  scope: PlanScope,
  schedules: PlanScheduleEntry[],
  replacement?: {
    previous_version: string;
  },
): FieldDiff[] {
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
      new_value: `${scope.start_date} 起，复核日期 ${scope.review_date}`,
    },
    {
      field: "每周训练日",
      new_value: `${scope.weekdays.map(weekdayLabel).join(" / ")}，共 ${schedules.length} 个应训练日`,
    },
    {
      field: "负荷",
      new_value: "无可信训练记录：不给起始重量，每个动作按「需要校准」逐级试重",
    },
  ];
  for (const block of plan.blocks) {
    rows.push({
      field: `${block.name} · 动作（预计 ${block.estimated_minutes} 分钟）`,
      new_value: block.exercises
        .map(
          (e) =>
            `${e.name} ${e.sets} 组 x ${e.rep_range} 次，目标 RIR ${e.target_rir}（${e.progression}）`,
        )
        .join(" → "),
    });
  }
  return rows;
}

/**
 * 六类安全症状（02 2.3 清单；plans/stage1.md §8 已拍：清单外继续澄清、不判定安全，
 * 不扩充医学规则）。只在读取时对 `body_conditions` 原文做确定性匹配——存储层不做医学判断，
 * 分类不写档案、不新增／解除限制。
 */
const SAFETY_SYMPTOM_PATTERNS: readonly { label: string; pattern: RegExp }[] = [
  { label: "胸部异常不适", pattern: /胸部(?:异常|明显|持续)(?:不适|闷|痛)/ },
  { label: "晕厥", pattern: /晕厥|昏厥|晕倒/ },
  { label: "异常气短", pattern: /气短|喘不上气|呼吸困难/ },
  { label: "锐痛", pattern: /锐痛|刺痛/ },
  { label: "麻木", pattern: /麻木|发麻/ },
  { label: "放射痛", pattern: /放射痛|放射到|放射至/ },
];

export interface BodyConditionClassification {
  /** 命中六类清单的原文子句（非空即阻断，建议线下专业评估） */
  confirmed: string[];
  /** 未命中清单的原文子句（不判定安全、需澄清；不得当作红旗阻断） */
  unlisted: string[];
}

/**
 * 读取时安全分类（02 2.3；本文件唯一分类入口）：按 `body_conditions` 原文逐条匹配六类清单。
 * 空数组 = 用户明确表示无身体情况；普通身体情况非空但未命中六类时不判红旗。
 */
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
  /** no_profile = 尚未建档；red_flag = 身体情况命中安全症状；unschedulable = 排不进档案约束 */
  code: "no_profile" | "red_flag" | "unschedulable";
  reason: string;
  /** code = red_flag 时给出命中六类清单的原文（文案由调用方组织，本模块不写对话文案） */
  red_flags: string[];
}

export interface PlanDraftReady {
  ok: true;
  plan: PlanVersion;
  scope: PlanScope;
  schedules: PlanScheduleEntry[];
  payload: PlanDraftPayload;
}

export type PlanDraftBuild = PlanDraftBlocked | PlanDraftReady;

/**
 * 从正式档案与当前有效限制生成 PPL 计划草稿（F2-02）。
 * 缺档案或身体情况命中六类安全症状时不生成任何处方；按 3.1 过滤后任一块没有可用动作、
 * 或生成结果未通过 `planPayloadError` 自检时同样不给处方（fail-closed）。
 */
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
  if (profile.weekly_frequency < PLAN_CANDIDATE.weekdays.length)
    return {
      ok: false,
      code: "unschedulable",
      reason: `档案每周频率 ${profile.weekly_frequency} 次，排不进候选的 ${PLAN_CANDIDATE.weekdays.length} 个训练日`,
      red_flags: [],
    };

  const blocks: PlanBlock[] = [];
  for (const template of PPL_TEMPLATE) {
    const exercises = template.exercises
      .filter(
        (e) => catalogViolation(e.id, profile, restrictions) === undefined,
      )
      .map((e) => planExercise(e.id, e));
    if (exercises.length === 0)
      return {
        ok: false,
        code: "unschedulable",
        reason: `${template.name}的动作全部被器械或限制过滤，没有可用动作`,
        red_flags: [],
      };
    blocks.push({
      name: template.name,
      weekday: template.weekday,
      estimated_minutes: estimatedMinutes(exercises),
      exercises,
    });
  }

  const plan: PlanVersion = {
    version: "v1",
    start_date: PLAN_CANDIDATE.start_date,
    review_date: PLAN_CANDIDATE.review_date,
    status: "active",
    blocks,
  };
  const scope: PlanScope = {
    start_date: PLAN_CANDIDATE.start_date,
    review_date: PLAN_CANDIDATE.review_date,
    weekdays: [...PLAN_CANDIDATE.weekdays],
  };
  const schedules = buildSchedules(plan.version, scope);
  const payload: PlanDraftPayload = {
    title: `PPL 训练计划（新建 ${plan.version}）`,
    diff: planDraftDiff(plan, scope, schedules),
    plan,
    scope,
    schedules,
    /** 首次生成计划没有旧版日程需要取消（替换计划的取消清单属后续阶段） */
    cancellations: [],
    /** 草稿卡「换动作」候选（F2-03）：按当前档案与限制过滤，不扩目录 */
    candidates: planCandidates(profile, restrictions),
  };

  const invalid = planPayloadError(payload, { profile, restrictions });
  if (invalid)
    return {
      ok: false,
      code: "unschedulable",
      reason: `生成的计划未通过安全前置校验：${invalid}`,
      red_flags: [],
    };
  return { ok: true, plan, scope, schedules, payload };
}

/**
 * 计划草稿的确定性安全前置校验（F2-02；生成自检与后续纠错／确认提交共用同一口径）。
 * 返回 undefined = 通过；否则返回具体违规说明。覆盖：缺档案／安全症状、频率、每次预计时长、
 * 器械、具体动作与动作模式限制、同一训练日重复动作身份（同一 weekday 跨板块一并去重；
 * 跨训练日复用不判冲突）、
 * 只引用可推荐目录动作、无可信记录一律校准（无起始重量）、日程与生效范围严格一致。
 */
export function planPayloadError(
  payload: PlanDraftPayload,
  ctx: { profile: Profile | null; restrictions: Restriction[]; today?: string },
): string | undefined {
  const { profile, restrictions } = ctx;
  if (!profile) return "尚未建档：不生成计划处方";
  const redFlags = classifyBodyConditions(profile.body_conditions).confirmed;
  if (redFlags.length > 0)
    return `身体情况命中安全症状（${redFlags.join("、")}）：不生成常规计划处方`;

  const { plan, scope, schedules } = payload;
  if (!plan || !scope || !schedules)
    return "缺少结构化计划载荷（计划版本／生效范围／具体日程）";
  if (
    plan.start_date !== scope.start_date ||
    plan.review_date !== scope.review_date
  )
    return "计划版本与生效范围的开始／复核日期不一致";
  if (!isIsoDate(scope.start_date) || !isIsoDate(scope.review_date))
    return "开始／复核日期须为有效日期（YYYY-MM-DD）";
  if (ctx.today !== undefined && scope.start_date < ctx.today)
    return `开始日期 ${scope.start_date} 早于当前日期 ${ctx.today}`;
  if (scope.start_date >= scope.review_date)
    return "生效范围须满足开始日期早于复核日期（[开始日期, 复核日期)）";
  if (scope.weekdays.length === 0) return "缺每周训练日";
  if (new Set(scope.weekdays).size !== scope.weekdays.length)
    return "每周训练日重复";
  if (scope.weekdays.some((w) => !Number.isInteger(w) || w < 1 || w > 7))
    return "每周训练日须在 1-7 内";
  if (scope.weekdays.length > profile.weekly_frequency)
    return `每周训练日 ${scope.weekdays.length} 天超过档案每周频率 ${profile.weekly_frequency} 次`;
  if (plan.blocks.length === 0) return "计划没有任何训练日板块";

  /* 同一训练日 = 同一 weekday：按 weekday 跨板块去重，同一训练日的多个板块不得重复同一动作身份 */
  const seenByWeekday = new Map<number, string[]>();
  for (const block of plan.blocks) {
    if (
      !Number.isInteger(block.weekday) ||
      block.weekday < 1 ||
      block.weekday > 7
    )
      return `${block.name}的训练日须为每周第 1-7 天`;
    if (!scope.weekdays.includes(block.weekday))
      return `${block.name}的训练日不在每周训练日内`;
    if (!(block.estimated_minutes > 0)) return `${block.name}缺预计时长`;
    if (block.estimated_minutes > profile.session_minutes)
      return `${block.name}预计 ${block.estimated_minutes} 分钟超过档案单次可用时长 ${profile.session_minutes} 分钟`;
    if (block.exercises.length === 0) return `${block.name}没有动作`;
    const seen = seenByWeekday.get(block.weekday) ?? [];
    seenByWeekday.set(block.weekday, seen);
    for (const ex of block.exercises) {
      if (seen.includes(ex.exercise_id))
        return `${block.name}在同一训练日重复同一动作身份：${ex.exercise_id}`;
      seen.push(ex.exercise_id);
      const violation = catalogViolation(ex.exercise_id, profile, restrictions);
      if (violation) return `${block.name}：${violation}`;
      if (!(ex.sets > 0))
        return `${block.name}动作组数须为正数：${ex.exercise_id}`;
      if (!/^\d+(-\d+)?$/.test(ex.rep_range))
        return `${block.name}动作次数区间须为「次数」或「下限-上限」：${ex.exercise_id}`;
      if (!/^\d+(-\d+)?$/.test(ex.target_rir))
        return `${block.name}动作目标 RIR 须为「值」或「下限-上限」：${ex.exercise_id}`;
      const calibration: Calibration | undefined = ex.calibration;
      if (
        !calibration ||
        calibration.status !== "needs_calibration" ||
        calibration.steps.length === 0 ||
        calibration.pass_criteria.trim() === "" ||
        calibration.stop_criteria.trim() === ""
      )
        return `${block.name}动作须给出完整校准说明且不得预设起始重量：${ex.exercise_id}`;
    }
  }

  return scheduleViolation(plan, scope, schedules);
}

/**
 * 使用时整份安全复核（F2-05；04 4.5、02 2.2/2.3）：按最新红旗与限制重查当前计划的全部动作。
 * 任一动作命中具体动作限制（同名）或动作模式限制（模式交集）即整份不可用（`usable: false`），
 * 不输出其余「未冲突」动作的处方；身体情况命中安全症状时独立阻断（`red_flag_blocked`）。
 * 复核只产出投影、不改写计划内容，也不新增计划状态：限制解除后重新复核即可恢复可用。
 * `context_version` 记录本次复核依据的业务版本（/profile 与指导请求都在请求时重算，不缓存结果）。
 */
export function reviewPlanSafety(input: {
  plan: PlanVersion;
  profile: Profile | null;
  restrictions: Restriction[];
  context_version: number;
}): PlanSafetyReview {
  const conflicts: PlanSafetyConflict[] = [];
  for (const block of input.plan.blocks) {
    for (const ex of block.exercises) {
      const catalog = CATALOG.find((c) => c.id === ex.exercise_id);
      const name = catalog?.standard_name_zh ?? ex.name;
      const modes = catalog?.modes ?? ex.modes;
      const restriction = input.restrictions.find((r) =>
        r.scope === "specific_action"
          ? r.name === name
          : modes.includes(r.name),
      );
      if (restriction)
        conflicts.push({
          exercise_id: ex.exercise_id,
          exercise_name: name,
          restriction,
        });
    }
  }
  /* 读取时分类（同一入口）：普通身体情况非空但未命中六类**不**阻断（02 2.3） */
  const red_flag_blocked =
    classifyBodyConditions(input.profile?.body_conditions).confirmed.length > 0;
  return {
    context_version: input.context_version,
    reviewed_at: new Date().toISOString(),
    red_flag_blocked,
    /* 未建档（无可复核依据）一律不可用（fail-closed），不猜「应该没问题」 */
    usable:
      input.profile !== null && !red_flag_blocked && conflicts.length === 0,
    conflicts,
  };
}

/** 具体日程须与生效范围严格一致：`[开始日期, 复核日期)` 内每个每周训练日各一条 */
function scheduleViolation(
  plan: PlanVersion,
  scope: PlanScope,
  schedules: PlanScheduleEntry[],
): string | undefined {
  const expected = buildSchedules(plan.version, scope);
  if (schedules.length !== expected.length)
    return `具体日程 ${schedules.length} 条，与生效范围应有的 ${expected.length} 条不一致`;
  for (const e of expected) {
    const hit = schedules.find((s) => s.date === e.date);
    if (!hit) return `具体日程缺 ${e.date}`;
    if (hit.weekday !== e.weekday)
      return `具体日程 ${e.date} 的训练日与生效范围不一致`;
    if (hit.plan_version !== plan.version)
      return `具体日程 ${e.date} 归属计划版本不一致`;
    if (hit.status !== "scheduled")
      return `新生成计划的日程状态须为 scheduled：${e.date}`;
  }
  return undefined;
}

/** 纠错后的计划动作：身份与展示文案按目录重建；目录外身份原样保留，交由校验以「目录外」拒绝 */
function canonicalExercise(e: PlanExercise): PlanExercise {
  if (!CATALOG.some((c) => c.id === e.exercise_id)) return e;
  return planExercise(e.exercise_id, e);
}

/** 计划载荷纠错结果：ok = false 时带具体违规说明，调用方不得落库、不得递增 revision */
export type PlanPayloadRevision =
  | { ok: true; payload: PlanDraftPayload }
  | { ok: false; error: string };

/**
 * 计划草稿纠错后的服务端归一化与复检（F2-03；`/api/drafts/:id/revise` 唯一入口）。
 * `stored` = 服务端当前草稿载荷，`requested` = 客户端提交的载荷。只从 `requested` 取
 * 允许纠错的字段：生效范围开始／复核日期、每个训练日板块的星期、每个动作的目录身份、
 * 组数、次数区间与目标 RIR；其余（标题、计划版本与状态、板块名、动作展示文案、渐进方式、
 * 校准、旧日程取消清单、档案补丁、Diff、日程、候选）一律以服务端存储／目录／档案为准，
 * 不从客户端接收（F2-03 只开放轻量纠错，没有完整编辑器）。因此纠错不能增删板块或动作，
 * 也不能改计划版本、状态或取消清单。
 * 归一化后：动作身份与文案按目录重建（`planExercise`），板块预计时长与具体日程按生效范围
 * 重算，展示 Diff 由归一化后的内容派生，动作候选按当前档案与限制刷新；任一项不通过即拒绝
 * （fail-closed）。每周训练日取自各板块的星期，因此训练日数量与板块数量始终一致；
 * 要改训练日数量属计划重构，不在轻量纠错内。
 */
export function normalizePlanPayload(
  stored: PlanDraftPayload,
  requested: PlanDraftPayload,
  ctx: { profile: Profile | null; restrictions: Restriction[]; today: string },
): PlanPayloadRevision {
  const basePlan = stored.plan;
  const baseScope = stored.scope;
  const plan = requested.plan;
  const scope = requested.scope;
  if (!basePlan || !baseScope)
    return {
      ok: false,
      error: "待确认草稿缺少结构化计划载荷（计划版本／生效范围／具体日程）",
    };
  if (!plan || !scope)
    return {
      ok: false,
      error: "缺少结构化计划载荷（计划版本／生效范围／具体日程）",
    };
  if (!isIsoDate(scope.start_date) || !isIsoDate(scope.review_date))
    return { ok: false, error: "开始／复核日期须为有效日期（YYYY-MM-DD）" };
  /* 板块数与每板块动作数固定：增删板块／动作属完整编辑器，不在轻量纠错内 */
  if (
    plan.blocks.length !== basePlan.blocks.length ||
    plan.blocks.some(
      (b, i) => b.exercises.length !== basePlan.blocks[i]?.exercises.length,
    )
  )
    return {
      ok: false,
      error: "轻量纠错不得增删训练日板块或动作",
    };

  const blocks: PlanBlock[] = basePlan.blocks.map((baseBlock, bi) => {
    const requestedBlock = plan.blocks[bi];
    const exercises: PlanExercise[] = baseBlock.exercises.map(
      (baseExercise, ei) => {
        const requestedExercise = requestedBlock.exercises[ei];
        return canonicalExercise({
          ...baseExercise,
          exercise_id: requestedExercise.exercise_id,
          sets: requestedExercise.sets,
          rep_range: requestedExercise.rep_range,
          target_rir: requestedExercise.target_rir,
        });
      },
    );
    return {
      ...baseBlock,
      weekday: requestedBlock.weekday,
      exercises,
      estimated_minutes: estimatedMinutes(exercises),
    };
  });
  const nextPlan: PlanVersion = {
    ...basePlan,
    start_date: scope.start_date,
    review_date: scope.review_date,
    blocks,
  };
  const nextScope: PlanScope = {
    ...baseScope,
    start_date: scope.start_date,
    review_date: scope.review_date,
    weekdays: [...new Set(blocks.map((b) => b.weekday))].sort((a, b) => a - b),
  };
  const schedules = buildSchedules(nextPlan.version, nextScope);
  const payload: PlanDraftPayload = {
    ...stored,
    plan: nextPlan,
    scope: nextScope,
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
      diff: planDraftDiff(nextPlan, nextScope, schedules),
    },
  };
}
