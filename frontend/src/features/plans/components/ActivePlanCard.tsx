import { useEffect, useMemo, useRef, useState } from "react";
import {
  CalendarCheck,
  ChevronLeft,
  ChevronRight,
  Target,
  X,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { DIMENSION_STYLE, type DimensionKey } from "@/lib/dimensionStyle";
import { formatTimestamp } from "@/lib/utils";
import {
  LOAD_CONVENTION_LABELS,
  RECORD_TYPE_LABELS,
} from "@/lib/catalogLabels";
import type {
  ExerciseWire,
  PlanDraftWire,
  PlannedExerciseWire,
  PlanWire,
  PrescriptionWire,
  TrainingDayWire,
} from "@/lib/contract";

const WEEKDAY_NAMES = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

/** 处方判别键 → 展示维度：三色与图标口径来自 ``DIMENSION_STYLE`` 的唯一出处 */
const PRESCRIPTION_DIMENSION: Record<PrescriptionWire["type"], DimensionKey> = {
  weighted_reps: "weight",
  bodyweight_reps: "reps",
  timed: "duration",
};

/**
 * 计划日期取星期：把 ``YYYY-MM-DD`` 按 UTC 解析后取 ``getUTCDay()``。
 *
 * 用本地时区解析会让西半球时区回退到前一天，星期因此错位。
 */
function weekdayName(isoDate: string): string {
  const [year, month, day] = isoDate.split("-").map(Number);
  return WEEKDAY_NAMES[new Date(Date.UTC(year, month - 1, day)).getUTCDay()];
}

function prescriptionRange(prescription: PrescriptionWire): string {
  return prescription.type === "timed"
    ? `${prescription.duration_seconds_min}-${prescription.duration_seconds_max} 秒`
    : `${prescription.reps_min}-${prescription.reps_max} 次`;
}

function prescriptionText(prescription: PrescriptionWire): string {
  if (prescription.type === "timed") {
    return withProgression(
      prescriptionRange(prescription),
      prescription.progression_note,
    );
  }
  const load =
    prescription.type === "bodyweight_reps"
      ? "自重"
      : prescription.load.status === "known"
        ? `${prescription.load.weight_kg} kg`
        : "待校准";
  return withProgression(
    `${load} × ${prescriptionRange(prescription)}`,
    prescription.progression_note,
  );
}

function withProgression(text: string, note: string | null): string {
  return note === null ? text : `${text} · ${note}`;
}

function Stat({
  label,
  value,
  unit,
}: {
  label: string;
  value: string | number;
  unit?: string;
}) {
  return (
    <div>
      <span className="text-xs text-muted-foreground">{label}</span>
      <p className="font-mono text-2xl font-bold tabular-nums">
        {value}
        {unit !== undefined && (
          <span className="ml-0.5 text-xs font-normal text-muted-foreground">
            {unit}
          </span>
        )}
      </p>
    </div>
  );
}

function ActivePlanBody({
  plan,
  exercises,
}: {
  plan: PlanWire;
  exercises: ExerciseWire[];
}) {
  const draft = plan.structured_content as PlanDraftWire;
  const catalogue = useMemo(
    () => new Map(exercises.map((exercise) => [exercise.id, exercise])),
    [exercises],
  );
  const [detail, setDetail] = useState<{
    day: TrainingDayWire;
    exercise: PlannedExerciseWire;
  } | null>(null);
  const slideRef = useRef<HTMLDivElement>(null);

  /** 计划引用的动作必在目录内（生成计划时后端按目录校验过），缺失即数据损坏 */
  const exerciseOf = (exerciseId: string): ExerciseWire => {
    const exercise = catalogue.get(exerciseId);
    if (exercise === undefined) {
      throw new Error(`动作目录缺少计划引用的动作：${exerciseId}`);
    }
    return exercise;
  };

  /* 一次滑动一个可视宽度：训练日不足一屏时 scrollBy 自动停在边界，不另做溢出判断 */
  const slide = (direction: -1 | 1) => {
    const lane = slideRef.current;
    if (lane === null) return;
    lane.scrollBy({ left: direction * lane.clientWidth, behavior: "smooth" });
  };

  return (
    <>
      <div className="border-b"></div>

      <p className="flex items-center gap-2 text-sm font-medium">
        <Target className="size-4 shrink-0" />
        {draft.goal}
      </p>

      <section className="flex items-center gap-2">
        <Button
          variant="outline"
          size="icon"
          className="size-7 shrink-0"
          aria-label="训练日展台向左切换"
          onClick={() => slide(-1)}
        >
          <ChevronLeft className="size-3.5" />
        </Button>

        <div
          ref={slideRef}
          className="flex min-w-0 flex-1 gap-3 overflow-x-auto pt-1 pb-2"
          style={{ scrollbarWidth: "none", msOverflowStyle: "none" }}
        >
          {draft.training_days.map((day) => (
            <div
              key={day.scheduled_on}
              className="flex w-56 shrink-0 flex-col gap-2 rounded-xl border bg-linear-to-b from-card to-muted/20 p-3"
            >
              <div className="flex items-center justify-between gap-1">
                <Badge variant="secondary" className="text-[10px] tabular-nums">
                  {day.scheduled_on.slice(5)}
                </Badge>
                <span className="text-[10px] text-muted-foreground">
                  {weekdayName(day.scheduled_on)} · {day.exercises.length}{" "}
                  个动作
                </span>
              </div>

              {day.exercises.map((exercise) => {
                const style =
                  DIMENSION_STYLE[
                    PRESCRIPTION_DIMENSION[exercise.prescription.type]
                  ];
                const prescriptionLabel = prescriptionText(
                  exercise.prescription,
                );
                return (
                  <button
                    key={exercise.exercise_id}
                    type="button"
                    onClick={() => setDetail({ day, exercise })}
                    className="flex flex-col gap-1.5 rounded-lg border border-border/60 p-2 text-left transition-colors hover:border-border hover:bg-muted/40"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate text-sm font-medium">
                        {exerciseOf(exercise.exercise_id).standard_name_zh}
                      </span>
                      <span className="flex shrink-0 items-center gap-1.5">
                        <span className="font-mono font-bold tabular-nums">
                          {exercise.sets}
                          <span className="ml-0.5 text-xs font-normal text-muted-foreground">
                            组
                          </span>
                        </span>
                        <span
                          className={`rounded-md p-1 ${style.bg} ${style.text}`}
                        >
                          <style.Icon className="size-3.5" />
                        </span>
                      </span>
                    </div>

                    <div className="flex min-w-0 items-center gap-1.5">
                      <Badge variant="outline" className="text-[10px]">
                        {style.label}
                      </Badge>
                      <span
                        className="truncate text-[10px] text-muted-foreground"
                        title={prescriptionLabel}
                      >
                        {prescriptionLabel}
                      </span>
                    </div>
                  </button>
                );
              })}
            </div>
          ))}
        </div>

        <Button
          variant="outline"
          size="icon"
          className="size-7 shrink-0"
          aria-label="训练日展台向右切换"
          onClick={() => slide(1)}
        >
          <ChevronRight className="size-3.5" />
        </Button>
      </section>

      {detail !== null && (
        <ExerciseDetailDialog
          day={detail.day}
          exercise={detail.exercise}
          entry={exerciseOf(detail.exercise.exercise_id)}
          onClose={() => setDetail(null)}
        />
      )}

      <div className="flex flex-col gap-1 border-t pt-3 text-xs text-muted-foreground">
        <p className="whitespace-pre-wrap">{draft.explanation}</p>
        <div className="flex flex-wrap items-center gap-2">
          <span className="tabular-nums">计划 #{plan.id}</span>
          {plan.source_plan_id !== null && (
            <span className="tabular-nums">
              来源计划 #{plan.source_plan_id}
            </span>
          )}
          <span className="tabular-nums">
            创建 {formatTimestamp(plan.created_at)}
          </span>
          {plan.confirmed_at !== null && (
            <span className="tabular-nums">
              启用 {formatTimestamp(plan.confirmed_at)}
            </span>
          )}
          {plan.archived_at !== null && (
            <span className="tabular-nums">
              归档 {formatTimestamp(plan.archived_at)}
            </span>
          )}
        </div>
      </div>
    </>
  );
}

/**
 * 动作详情弹层：展示后端的动作事实（处方、负荷与来源、目录口径与模式）与计划给的组数。
 */
function ExerciseDetailDialog({
  day,
  exercise,
  entry,
  onClose,
}: {
  day: TrainingDayWire;
  exercise: PlannedExerciseWire;
  entry: ExerciseWire;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    ref.current?.showModal();
  }, []);

  const style =
    DIMENSION_STYLE[PRESCRIPTION_DIMENSION[exercise.prescription.type]];

  return (
    <dialog
      ref={ref}
      aria-labelledby="exercise-detail-title"
      onClose={onClose}
      onClick={(event) => {
        if (event.target === ref.current) ref.current.close();
      }}
      className="m-auto w-[min(34rem,calc(100vw-2rem))] rounded-xl border bg-card p-0 text-card-foreground backdrop:bg-black/60"
    >
      <div className="flex flex-col gap-4 p-5">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2
              id="exercise-detail-title"
              className="font-display text-lg font-medium tracking-tight"
            >
              {entry.standard_name_zh}
            </h2>
            <p className="text-xs text-muted-foreground tabular-nums">
              {entry.id} · {weekdayName(day.scheduled_on)} {day.scheduled_on}
            </p>
          </div>
          <Button
            variant="outline"
            size="icon"
            className="size-7"
            aria-label="关闭动作详情"
            onClick={() => ref.current?.close()}
          >
            <X className="size-3.5" />
          </Button>
        </div>

        <div className="grid grid-cols-3 gap-4 border-b pb-3">
          <Stat label="组数" value={exercise.sets} unit="组" />
          <div>
            <span className="text-xs text-muted-foreground">
              {exercise.prescription.type === "timed" ? "时长处方" : "次数处方"}
            </span>
            <p className="font-mono text-lg font-bold tabular-nums">
              {prescriptionRange(exercise.prescription)}
            </p>
          </div>
          <div>
            <span className="text-xs text-muted-foreground">维度</span>
            <p
              className={`flex items-center gap-1 text-lg font-bold ${style.text}`}
            >
              <style.Icon className="size-4" />
              {style.label}
            </p>
          </div>
        </div>

        <section className="flex flex-col gap-1">
          <h3 className="text-xs font-medium">重量校准</h3>
          {exercise.prescription.type !== "weighted_reps" ? (
            <p className="text-xs text-muted-foreground">
              {RECORD_TYPE_LABELS[entry.record_type]}
              动作不记录负重，无重量校准。
            </p>
          ) : exercise.prescription.load.status === "known" ? (
            <>
              <p className="font-mono text-2xl font-bold tabular-nums">
                {exercise.prescription.load.weight_kg}
                <span className="ml-0.5 text-xs font-normal text-muted-foreground">
                  kg
                </span>
              </p>
              <p className="text-xs text-muted-foreground tabular-nums">
                来源：训练 #
                {exercise.prescription.load.source_workout_session_id} 第{" "}
                {exercise.prescription.load.source_set_no} 组
              </p>
            </>
          ) : (
            <p className="text-xs text-muted-foreground">
              待校准：没有可用作负荷来源的历史正式组，不猜重量。
            </p>
          )}
        </section>

        <section className="flex flex-col gap-1">
          <h3 className="text-xs font-medium">动作模式</h3>
          <div className="flex flex-wrap gap-1">
            {entry.modes.map((mode) => (
              <Badge key={mode} variant="outline" className="text-[10px]">
                {mode}
              </Badge>
            ))}
          </div>
        </section>

        <dl className="flex flex-col gap-1 text-xs">
          <div className="flex items-center justify-between gap-4">
            <dt className="text-muted-foreground">记录口径</dt>
            <dd>{RECORD_TYPE_LABELS[entry.record_type]}</dd>
          </div>
          <div className="flex items-center justify-between gap-4">
            <dt className="text-muted-foreground">器械变式</dt>
            <dd>{entry.equipment_variant}</dd>
          </div>
          {entry.load_convention !== null && (
            <div className="flex items-center justify-between gap-4">
              <dt className="text-muted-foreground">负重口径</dt>
              <dd>{LOAD_CONVENTION_LABELS[entry.load_convention]}</dd>
            </div>
          )}
          {entry.min_load_increment_kg !== null && (
            <div className="flex items-center justify-between gap-4">
              <dt className="text-muted-foreground">最小加重</dt>
              <dd className="tabular-nums">{entry.min_load_increment_kg} kg</dd>
            </div>
          )}
          {exercise.prescription.progression_note !== null && (
            <div className="flex items-center justify-between gap-4">
              <dt className="shrink-0 text-muted-foreground">渐进说明</dt>
              <dd className="text-right">
                {exercise.prescription.progression_note}
              </dd>
            </div>
          )}
        </dl>
      </div>
    </dialog>
  );
}

export default function ActivePlanCard({
  plan,
  exercises,
}: {
  plan: PlanWire | null;
  exercises: ExerciseWire[] | undefined;
}) {
  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <CardTitle className="flex items-center gap-2">
            <CalendarCheck className="size-4" />
            当前启用计划
          </CardTitle>
          {plan !== null && (
            <Badge variant="outline" className="tabular-nums">
              已启用 · v{plan.version}
            </Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {plan === null ? (
          <p className="text-sm text-muted-foreground">还没有启用的计划。</p>
        ) : exercises === undefined ? (
          <p className="text-sm text-muted-foreground">正在加载动作目录…</p>
        ) : (
          <ActivePlanBody plan={plan} exercises={exercises} />
        )}
      </CardContent>
    </Card>
  );
}
