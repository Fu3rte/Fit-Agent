/**
 * D9 计划展示派生（契约真相 = PlanPayload；本文件只做 UI 投影，不写第二份真相）。
 *
 * - deriveWeekday / derivePlanBlocks：从 calendar_cycle 推导每周安排展示；
 * - deriveRangeLabel / progressionLabel / prescriptionLabel：处方与渐进的中文展示；
 * - projectSchedules：按 [starts_on, review_on) 逐日投影应训练日（不生成 review_on 当日）。
 */
import type {
  CalendarCycle,
  CycleSlot,
  IntRange,
  PlanExerciseItem,
  PlanPayload,
  PlanPrescription,
  PlanScheduleEntry,
  PlanWorkout,
  ProgressionMethod,
} from "./contract";

/** 每周第几天（1-7，周一起） */
export function weekdayOfDate(date: string): number {
  return ((new Date(`${date}T00:00:00Z`).getUTCDay() + 6) % 7) + 1;
}

/** 每周训练日展示文案（1-7，周一起） */
const WEEKDAY_LABELS = [
  "周一",
  "周二",
  "周三",
  "周四",
  "周五",
  "周六",
  "周日",
];

export function weekdayLabel(weekday: number): string {
  return WEEKDAY_LABELS[weekday - 1] ?? String(weekday);
}

/** 日期（YYYY-MM-DD）后一天；只按日历日推进 */
export function dayAfter(date: string): string {
  const d = new Date(`${date}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + 1);
  return d.toISOString().slice(0, 10);
}

/** 有效日历日期（YYYY-MM-DD），且非 2026-02-31 之类的伪日期 */
export function isIsoDate(date: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return false;
  const d = new Date(`${date}T00:00:00Z`);
  return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === date;
}

/** 从 cycle 中该 workout 首次出现的 slot 下标 + anchor_date 推 weekday（1-7 周一起） */
export function deriveWeekday(
  cycle: CalendarCycle,
  workout_key: string,
): number | undefined {
  const idx = cycle.slots.findIndex(
    (s) => s.kind === "workout" && s.workout_key === workout_key,
  );
  if (idx < 0) return undefined;
  const anchorMs = Date.parse(`${cycle.anchor_date}T00:00:00Z`);
  const day = new Date(anchorMs + idx * 86_400_000);
  return weekdayOfDate(day.toISOString().slice(0, 10));
}

/** 展示用板块（非契约真相）：从 D9 payload 派生的每周安排视图 */
export interface DisplayBlock {
  name: string;
  weekday: number | undefined;
  estimated_minutes: number;
  exercises: PlanExerciseItem[];
  workout_key: string;
}

/** 从 plan payload 派生展示用 blocks/weekday（不把展示字段写进契约真相） */
export function derivePlanBlocks(plan: PlanPayload): DisplayBlock[] {
  return plan.plan_workouts.map((w) => ({
    name: w.name,
    weekday: deriveWeekday(plan.calendar_cycle, w.workout_key),
    estimated_minutes: w.estimated_minutes,
    exercises: w.exercises,
    workout_key: w.workout_key,
  }));
}

/** 区间展示：`1-3` 或 `""` */
export function deriveRangeLabel(r?: IntRange | null): string {
  if (!r) return "";
  if (r.min === r.max) return String(r.min);
  return `${r.min}-${r.max}`;
}

/** 渐进方式中文 label */
export function progressionLabel(method: ProgressionMethod): string {
  switch (method) {
    case "double_progression":
      return "双重渐进";
    case "repetition_progression":
      return "次数递增";
    case "duration_progression":
      return "时长递进";
    case "custom":
      return "自定义";
  }
}

/** 处方单行展示：`4 组 × 6-8 次` / `3 组 × 45-60 秒` */
export function prescriptionLabel(p: PlanPrescription): string {
  if (p.kind === "timed") {
    return `${p.work_sets} 组 × ${deriveRangeLabel(p.duration_seconds_range)} 秒`;
  }
  return `${p.work_sets} 组 × ${deriveRangeLabel(p.reps_range)} 次`;
}

/** 目标 RIR 展示（仅次数型；无 RIR 返回 ""） */
export function targetRirLabel(p: PlanPrescription): string {
  if (p.kind === "timed") return "";
  return deriveRangeLabel(p.target_rir);
}

/** 训练日内按 workout_key 查找 */
export function findWorkout(
  plan: PlanPayload,
  workout_key: string,
): PlanWorkout | undefined {
  return plan.plan_workouts.find((w) => w.workout_key === workout_key);
}

/**
 * 日程投影：按 [starts_on, review_on) 逐日 `slot_index = floor(date-anchor) mod slots.length`；
 * workout 槽生成一条，rest 槽不生成；不生成 review_on 当日。
 * weekday 按业务时区日期规则派生（mock 用 UTC 日历日）。
 */
export function projectSchedules(
  plan_version: string,
  plan: PlanPayload,
  opts: {
    starts_on: string;
    review_on: string;
    /** 注入业务日期：默认不计算锁定，全部以 scheduled 投影 */
    today?: string;
  },
): PlanScheduleEntry[] {
  const { slots, anchor_date } = plan.calendar_cycle;
  if (slots.length === 0) return [];
  if (opts.starts_on >= opts.review_on) return [];
  const out: PlanScheduleEntry[] = [];
  for (
    let date = opts.starts_on;
    date < opts.review_on;
    date = dayAfter(date)
  ) {
    const offset = dayDiff(anchor_date, date);
    const slot = slots[((offset % slots.length) + slots.length) % slots.length];
    if (slot.kind !== "workout") continue;
    const locked_by_date_rule =
      opts.today !== undefined ? opts.today >= date : false;
    out.push({
      id: `sched-${plan_version}-${date}`,
      plan_version,
      date,
      plan_workout_key: slot.workout_key,
      weekday: weekdayOfDate(date),
      stored_status: locked_by_date_rule ? "locked" : "scheduled",
      locked_by_date_rule,
      locked_effective: locked_by_date_rule,
    });
  }
  return out;
}

/** 日期差（日历日）：to - from */
export function dayDiff(from: string, to: string): number {
  const a = Date.parse(`${from}T00:00:00Z`);
  const b = Date.parse(`${to}T00:00:00Z`);
  return Math.round((b - a) / 86_400_000);
}

/**
 * 按目标 weekday 重建 7 日循环槽（轻量纠错「改训练日」用）。
 * workouts 按给定顺序放在目标 weekday 上，其余为 rest；anchor 固定。
 */
export function rebuildCycleSlots(
  anchor_date: string,
  workouts: { workout_key: string; weekday: number }[],
): CycleSlot[] {
  const anchorWd = weekdayOfDate(anchor_date);
  const byOffset = new Map<number, string>();
  for (const w of workouts) {
    byOffset.set(((w.weekday - anchorWd) % 7 + 7) % 7, w.workout_key);
  }
  return Array.from({ length: 7 }, (_, i) => {
    const key = byOffset.get(i);
    return key
      ? ({ kind: "workout", workout_key: key } as const)
      : ({ kind: "rest" } as const);
  });
}
