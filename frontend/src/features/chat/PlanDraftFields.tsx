import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import type {
  DraftPayload,
  IntRange,
  PlanDraftPayload,
  PlanExerciseItem,
  PlanScheduleEntry,
  PlanVersion,
} from "@/lib/contract";
import {
  derivePlanBlocks,
  deriveRangeLabel,
  progressionLabel,
  rebuildCycleSlots,
  weekdayLabel,
} from "@/lib/planView";
import { FieldLine, selectClass, toNumber } from "./draftFields";

const SCHEDULE_STATUS_LABEL: Record<PlanScheduleEntry["stored_status"], string> =
  {
    scheduled: "应训练",
    locked: "已锁定",
    cancelled: "已取消",
  };

const scheduleLine = (s: PlanScheduleEntry) => {
  const lock = s.locked_effective ? "已锁定" : SCHEDULE_STATUS_LABEL[s.stored_status];
  return `${s.date}（${weekdayLabel(s.weekday)}）· ${lock}`;
};

/** 把 IntRange 编辑框文本解析为区间；空串返回 undefined */
function parseRange(text: string): IntRange | undefined | "invalid" {
  if (text.trim() === "") return undefined;
  const m = text.trim().match(/^(\d+)(?:-(\d+))?$/);
  if (!m) return "invalid";
  const min = Number(m[1]);
  const max = m[2] !== undefined ? Number(m[2]) : min;
  if (min > max) return "invalid";
  return { min, max };
}

/**
 * 计划草稿结构化卡（D9）：行字段 + payload + 日程。轻量纠错只改待确认草稿：
 * 开始/复核日期、训练日（cycle slot 的展示 weekday）、动作候选、组数、次数区间、目标 RIR。
 * 展示 weekday 经 derive 函数派生，不把展示字段写进契约真相。
 */
export function PlanDraftFields({
  payload,
  disabled,
  onChange,
}: {
  payload: PlanDraftPayload;
  disabled: boolean;
  onChange: (payload: DraftPayload) => void;
}) {
  const plan = payload.plan;
  if (!plan) {
    return (
      <p className="text-[11px] text-muted-foreground">
        本草稿未携带结构化计划载荷，仅展示服务端变更 Diff。
      </p>
    );
  }
  const candidates = payload.candidates ?? [];
  const schedules = payload.schedules ?? [];
  const blocks = derivePlanBlocks(plan.payload);
  const calibration = plan.payload.plan_workouts
    .flatMap((w) => w.exercises)
    .find((e) => e.load?.kind === "needs_calibration")?.load;

  const patchPlan = (p: Partial<Pick<PlanVersion, "starts_on" | "review_on">>) =>
    onChange({
      ...payload,
      plan: { ...plan, ...p },
    });

  const patchItem = (
    workout_key: string,
    item_key: string,
    p: Partial<PlanExerciseItem>,
  ) =>
    onChange({
      ...payload,
      plan: {
        ...plan,
        payload: {
          ...plan.payload,
          plan_workouts: plan.payload.plan_workouts.map((w) =>
            w.workout_key === workout_key
              ? {
                  ...w,
                  exercises: w.exercises.map((e) =>
                    e.item_key === item_key ? { ...e, ...p } : e,
                  ),
                }
              : w,
          ),
        },
      },
    });

  const pickExercise = (
    workout_key: string,
    item_key: string,
    exercise_id: string,
  ) => {
    const candidate = candidates.find((c) => c.exercise_id === exercise_id);
    const prev = plan.payload.plan_workouts
      .flatMap((w) => w.exercises)
      .find((e) => e.item_key === item_key);
    patchItem(workout_key, item_key, {
      exercise_id,
      ...(candidate
        ? {
            display_snapshot: {
              name: candidate.name,
              equipment_variant: prev?.display_snapshot.equipment_variant ?? "barbell",
              load_convention: prev?.display_snapshot.load_convention ?? null,
            },
          }
        : {}),
    });
  };

  /** 改训练日：重建 cycle slots，把该 workout 放到目标 weekday（展示派生 weekday） */
  const setWeekday = (workout_key: string, weekday: number) => {
    const anchor = plan.starts_on;
    const offsets = plan.payload.plan_workouts.map((w) => ({
      workout_key: w.workout_key,
      weekday:
        w.workout_key === workout_key
          ? weekday
          : (blocks.find((b) => b.workout_key === w.workout_key)?.weekday ?? 1),
    }));
    onChange({
      ...payload,
      plan: {
        ...plan,
        payload: {
          ...plan.payload,
          calendar_cycle: {
            anchor_date: anchor,
            slots: rebuildCycleSlots(anchor, offsets),
          },
        },
      },
    });
  };

  const setRepsRange = (
    workout_key: string,
    item_key: string,
    text: string,
  ) => {
    const r = parseRange(text);
    if (r === "invalid") return;
    const ex = plan.payload.plan_workouts
      .flatMap((w) => w.exercises)
      .find((e) => e.item_key === item_key);
    if (!ex || ex.prescription.kind !== "reps") return;
    patchItem(workout_key, item_key, {
      prescription: {
        ...ex.prescription,
        reps_range: r ?? { min: 1, max: 1 },
      },
    });
  };

  const setRir = (workout_key: string, item_key: string, text: string) => {
    const r = parseRange(text);
    if (r === "invalid") return;
    const ex = plan.payload.plan_workouts
      .flatMap((w) => w.exercises)
      .find((e) => e.item_key === item_key);
    if (!ex || ex.prescription.kind !== "reps") return;
    const next = { ...ex.prescription };
    if (r) next.target_rir = r;
    else delete next.target_rir;
    patchItem(workout_key, item_key, { prescription: next });
  };

  return (
    <div className="mt-3 space-y-2.5 rounded-lg border border-border bg-muted/30 p-3">
      <p className="text-xs font-medium">
        计划版本 {plan.version}
        （拟议启用；确认前正式计划与日程不变）
      </p>

      <FieldLine label="开始日期">
        <Input
          type="date"
          value={plan.starts_on}
          disabled={disabled}
          onChange={(e) => patchPlan({ starts_on: e.target.value })}
          className="h-7 w-36 text-xs"
          aria-label="计划开始日期"
        />
      </FieldLine>
      <FieldLine label="复核日期">
        <Input
          type="date"
          value={plan.review_on}
          disabled={disabled}
          onChange={(e) => patchPlan({ review_on: e.target.value })}
          className="h-7 w-36 text-xs"
          aria-label="计划复核日期"
        />
      </FieldLine>
      <FieldLine label="每周安排">
        <span>
          {blocks
            .map((b) => (b.weekday !== undefined ? weekdayLabel(b.weekday) : "—"))
            .join(" / ")}
          ，共 {schedules.length} 个应训练日（复核日当天不排）
        </span>
      </FieldLine>

      {plan.payload.plan_workouts.map((workout) => {
        const block = blocks.find((b) => b.workout_key === workout.workout_key);
        return (
          <div
            key={workout.workout_key}
            className="rounded-lg border border-border bg-card/60 p-2.5"
          >
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className="font-medium">{workout.name}</span>
              <select
                aria-label={`${workout.name}的训练日`}
                className={selectClass}
                value={block?.weekday ?? 1}
                disabled={disabled}
                onChange={(e) =>
                  setWeekday(workout.workout_key, Number(e.target.value))
                }
              >
                {[1, 2, 3, 4, 5, 6, 7].map((w) => (
                  <option key={w} value={w}>
                    {weekdayLabel(w)}
                  </option>
                ))}
              </select>
              <span className="text-muted-foreground">
                预计 {workout.estimated_minutes} 分钟
              </span>
            </div>
            {workout.exercises.map((ex) => {
              const reps =
                ex.prescription.kind === "reps"
                  ? deriveRangeLabel(ex.prescription.reps_range)
                  : "";
              const rir =
                ex.prescription.kind === "reps"
                  ? deriveRangeLabel(ex.prescription.target_rir)
                  : "";
              const timedRange =
                ex.prescription.kind === "timed"
                  ? deriveRangeLabel(ex.prescription.duration_seconds_range)
                  : "";
              return (
                <div
                  key={ex.item_key}
                  className="mt-1.5 flex flex-wrap items-center gap-1.5 text-xs"
                >
                  <span className="w-5 text-muted-foreground">
                    {workout.exercises.indexOf(ex) + 1}.
                  </span>
                  <select
                    aria-label={`${workout.name}动作 ${ex.display_snapshot.name}`}
                    className={`${selectClass} max-w-56`}
                    value={ex.exercise_id}
                    disabled={disabled}
                    onChange={(e) =>
                      pickExercise(
                        workout.workout_key,
                        ex.item_key,
                        e.target.value,
                      )
                    }
                  >
                    {!candidates.some((c) => c.exercise_id === ex.exercise_id) && (
                      <option value={ex.exercise_id}>
                        {ex.display_snapshot.name}（不在当前候选内）
                      </option>
                    )}
                    {candidates.map((c) => (
                      <option key={c.exercise_id} value={c.exercise_id}>
                        {c.name}（{c.variant}）
                      </option>
                    ))}
                  </select>
                  <Input
                    type="number"
                    min={1}
                    value={ex.prescription.work_sets}
                    disabled={disabled}
                    onChange={(e) => {
                      const n = toNumber(e.target.value);
                      if (n === undefined || n < 1) return;
                      patchItem(workout.workout_key, ex.item_key, {
                        prescription: {
                          ...ex.prescription,
                          work_sets: n,
                        },
                      });
                    }}
                    className="h-7 w-14 text-xs"
                    aria-label={`${ex.display_snapshot.name}组数`}
                  />
                  <span className="text-muted-foreground">组 ×</span>
                  {ex.prescription.kind === "reps" ? (
                    <>
                      <Input
                        value={reps}
                        disabled={disabled}
                        onChange={(e) =>
                          setRepsRange(
                            workout.workout_key,
                            ex.item_key,
                            e.target.value,
                          )
                        }
                        className="h-7 w-16 text-xs"
                        aria-label={`${ex.display_snapshot.name}次数区间`}
                      />
                      <span className="text-muted-foreground">次 · 目标用力</span>
                      <Input
                        value={rir}
                        disabled={disabled}
                        onChange={(e) =>
                          setRir(
                            workout.workout_key,
                            ex.item_key,
                            e.target.value,
                          )
                        }
                        className="h-7 w-16 text-xs"
                        aria-label={`${ex.display_snapshot.name}目标 RIR`}
                      />
                    </>
                  ) : (
                    <>
                      <Input
                        value={timedRange}
                        disabled
                        className="h-7 w-16 text-xs"
                        aria-label={`${ex.display_snapshot.name}时长区间`}
                      />
                      <span className="text-muted-foreground">秒</span>
                    </>
                  )}
                  <span className="text-muted-foreground">
                    {progressionLabel(ex.progression.method)}
                  </span>
                  {ex.load?.kind === "needs_calibration" && (
                    <Badge variant="secondary" className="text-[10px]">
                      需要校准
                    </Badge>
                  )}
                </div>
              );
            })}
          </div>
        );
      })}

      {calibration && calibration.kind === "needs_calibration" && (
        <div className="rounded-lg border border-dashed border-border p-2.5 text-xs">
          <p className="font-medium">
            校准说明（无可信训练记录：不给起始重量）
          </p>
          <ol className="mt-1 list-decimal space-y-0.5 pl-4 text-muted-foreground">
            {calibration.steps.map((step) => (
              <li key={step}>{step}</li>
            ))}
          </ol>
          <p className="mt-1 text-muted-foreground">
            通过：{calibration.pass_criteria}；停止：
            {calibration.stop_criteria}
          </p>
        </div>
      )}

      <div className="text-xs">
        <div className="mb-1.5 text-muted-foreground">
          具体日程（{plan.starts_on} 起至复核日 {plan.review_on} 前）
        </div>
        {schedules.length === 0 ? (
          <p className="text-muted-foreground">尚无日程</p>
        ) : (
          <div className="flex max-h-40 flex-wrap gap-1 overflow-y-auto">
            {schedules.map((s) => (
              <span
                key={s.id}
                className="rounded bg-bubble-out px-1.5 py-0.5 text-[11px] text-bubble-out-foreground"
              >
                {scheduleLine(s)}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
