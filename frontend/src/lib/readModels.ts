/**
 * 只读看板传输形状 → 展示形状（F5/F6/F7/F8；stage6 F6-02b）。
 *
 * 后端传输面（api/dto.py）与页面展示契约（contract.ts 展示类型）之间的唯一映射点：
 * - 计划：PlanViewWire → PlanVersion + PlanScheduleEntry[]
 * - 记录：RecordListItemWire → TrainingRecord[]（存储契约 exercises[] → 扁平展示行）
 * - 统计：completion 比率 → 百分数；PR load_kg_key（×1000）→ kg
 * - 复盘：ReviewWire（body_markdown）→ 最新条投影
 *
 * 空数据：null / 无候选一律保持「暂无」语义，不编造 0%、100% 或假数字。
 */
import type {
  ActionRestrictionWire,
  Buckets,
  PlanPayload,
  PlanScheduleEntry,
  PlanScheduleWire,
  PlanVersion,
  PlanViewWire,
  PrWire,
  ProfileFactsDto,
  RecordListItemWire,
  RecordSet,
  Restriction,
  ReviewWire,
  SetFacts,
  TrainingRecord,
  WeekCompletion,
  WeekCompletionWire,
} from "./contract";

/** 百分比展示：backend rate 0–1 → 一位小数百分数；null 保持「暂无」 */
export function rateToPercent(rate: number | null): number | null {
  if (rate === null) return null;
  return Math.round(rate * 1000) / 10;
}

/** kg×1000 整数键 → kg 数值；null 保持 null */
export function loadKgKeyToKg(key: number | null): number | null {
  if (key === null) return null;
  return Math.round(key) / 1000;
}

/** 传输限制 → 展示限制（target 作 name；无展示名字段） */
export function mapRestrictionWire(r: ActionRestrictionWire): Restriction {
  return { name: r.target, scope: r.scope };
}

/** S2-07 三态限制列表 → 展示限制数组（unknown/denied → 空数组） */
export function mapProfileRestrictions(
  facts: ProfileFactsDto | null,
): Restriction[] {
  const fact = facts?.action_restrictions;
  if (!fact || fact.state !== "known" || !fact.value) return [];
  return fact.value.map(mapRestrictionWire);
}

/** 日程传输行 → 展示条目（锁定双态） */
export function mapScheduleWire(w: PlanScheduleWire): PlanScheduleEntry {
  return {
    id: w.id,
    plan_version: w.plan_version_id,
    date: w.scheduled_on,
    plan_workout_key: w.plan_workout_key,
    weekday: w.weekday,
    stored_status: w.cancelled
      ? "cancelled"
      : w.lock.stored
        ? "locked"
        : "scheduled",
    locked_by_date_rule: w.lock.by_business_date,
    locked_effective: w.lock.effective,
  };
}

/** 计划视图传输 → 展示 PlanVersion + 全部日程 */
export function mapPlanView(view: PlanViewWire): {
  plan: PlanVersion;
  schedules: PlanScheduleEntry[];
} {
  const plan: PlanVersion = {
    version: view.version,
    starts_on: view.starts_on,
    review_on: view.review_on,
    mode: view.mode,
    status: view.is_current ? "active" : "archived",
    payload: view.plan,
  };
  return { plan, schedules: view.schedules.map(mapScheduleWire) };
}

/** 单组存储事实 → 展示组（set_type 未明确不伪造成 warmup；负重保留原文解析） */
export function mapSetFacts(s: SetFacts): RecordSet {
  const weight =
    s.load && s.load.unit === "kg"
      ? Number(s.load.value_text)
      : Number.NaN;
  return {
    set_type: s.set_type === "warmup" ? "warmup" : "working",
    ...(Number.isFinite(weight) ? { weight_kg: weight } : {}),
    ...(s.reps !== null && s.reps !== undefined ? { reps: s.reps } : {}),
    ...(s.rir !== null && s.rir !== undefined ? { rir: s.rir } : {}),
    assisted: s.assistance === "assisted" || s.assistance === "spotter_only",
  };
}

/** 存储契约动作 → 展示用动作名（有目录名时展示；否则用稳定 id，不编造中文名） */
export function exerciseDisplayName(
  exerciseId: string,
  catalogName?: string,
): string {
  return catalogName && catalogName.length > 0 ? catalogName : exerciseId;
}

/**
 * 记录列表传输 → 扁平展示行：一个训练身份的每个 exercise 展开为一条
 * TrainingRecord（id 用 `sessionId:position` 保身份；训练身份保留在 training_session_id）。
 */
export function mapRecordList(
  items: RecordListItemWire[],
  nameOf?: (exerciseId: string) => string | undefined,
): TrainingRecord[] {
  return items.flatMap((item) => {
    const date = item.revision?.occurred_on ?? item.record.occurred_on;
    const status = item.revision?.status ?? "incomplete";
    const revisionNo = item.revision?.revision_no ?? 1;
    const kind: TrainingRecord["kind"] =
      revisionNo > 1 ? "correction" : "new";
    const isReturn = item.record.is_return_phase === true;
    return item.record.exercises.map((ex) => ({
      id: `${item.id}:${ex.position}`,
      date,
      kind,
      status,
      exercise: exerciseDisplayName(ex.exercise_id, nameOf?.(ex.exercise_id)),
      variant:
        ex.load_notation ?? (ex.record_type === "time" ? "计时" : "—"),
      sets: ex.sets.map(mapSetFacts),
      ...(ex.warmup_summary_text
        ? { warmup_summary: ex.warmup_summary_text }
        : {}),
      /** 应训练日程 id 不从安排反推；安排关联只透传存储契约显式字段 */
      scheduled_session_id: null,
      arrangement_revision_id: item.record.arrangement_revision_id ?? null,
      training_session_id: item.id,
      ...(isReturn ? { period: "return" as const } : {}),
    }));
  });
}

/** 完成率传输 → 展示周行（Wn；rate 比率→百分数，null=暂无） */
export function mapWeekCompletion(w: WeekCompletionWire): WeekCompletion {
  return {
    week: `W${w.week_no}`,
    planned: w.planned,
    completed: w.completed,
    rate: rateToPercent(w.rate),
  };
}

/** PR 传输 → 展示行（最高重量 kg × 该重量下单组最高次数） */
export function mapPrDisplay(
  pr: PrWire,
  nameOf?: (exerciseId: string) => string | undefined,
): {
  exercise: string;
  variant: string;
  best_weight_kg: number | null;
  best_reps_at_weight: number | null;
} | null {
  const kg = loadKgKeyToKg(pr.max_load_kg_key);
  if (kg === null) return null;
  return {
    exercise: exerciseDisplayName(pr.exercise_id, nameOf?.(pr.exercise_id)),
    variant: pr.load_notation,
    best_weight_kg: kg,
    best_reps_at_weight: pr.best_reps,
  };
}

/** 最新复盘条（列表末条）→ 展示投影；空列表 → null（暂无） */
export function pickLatestReview(
  reviews: ReviewWire[] | undefined,
): ReviewWire | null {
  if (!reviews || reviews.length === 0) return null;
  return reviews[reviews.length - 1];
}

/** 从计划 payload 收集 PR 查询键（exercise_id + load_notation） */
export function planPrKeys(
  payload: PlanPayload,
): Array<{ exercise_id: string; load_notation: string }> {
  const seen = new Map<string, { exercise_id: string; load_notation: string }>();
  for (const w of payload.plan_workouts) {
    for (const ex of w.exercises) {
      const notation = ex.display_snapshot.load_convention;
      if (!notation) continue;
      const key = `${ex.exercise_id}|${notation}`;
      if (!seen.has(key))
        seen.set(key, { exercise_id: ex.exercise_id, load_notation: notation });
    }
  }
  return [...seen.values()];
}

/** 从记录存储契约收集 PR 查询键 */
export function recordPrKeys(
  items: RecordListItemWire[],
): Array<{ exercise_id: string; load_notation: string }> {
  const seen = new Map<string, { exercise_id: string; load_notation: string }>();
  for (const item of items) {
    for (const ex of item.record.exercises) {
      if (!ex.load_notation) continue;
      const key = `${ex.exercise_id}|${ex.load_notation}`;
      if (!seen.has(key))
        seen.set(key, {
          exercise_id: ex.exercise_id,
          load_notation: ex.load_notation,
        });
    }
  }
  return [...seen.values()];
}

/** 合并计数三桶（judgement 聚合；空结果保持全零，不编造） */
export function emptyBuckets(): Buckets {
  return { met: 0, unmet: 0, pending: 0 };
}

export function addBuckets(
  total: Buckets,
  next: Buckets | null | undefined,
): Buckets {
  if (!next) return total;
  return {
    met: total.met + next.met,
    unmet: total.unmet + next.unmet,
    pending: total.pending + next.pending,
  };
}
