import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { getPlan, listExercises } from "@/lib/api";
import type { PlanDraftWire, PrescriptionWire } from "@/lib/contract";

function loadText(prescription: PrescriptionWire): string {
  if (prescription.type === "timed") {
    return `${prescription.duration_seconds_min}-${prescription.duration_seconds_max} 秒`;
  }
  const range = `${prescription.reps_min}-${prescription.reps_max} 次`;
  if (prescription.type === "bodyweight_reps") {
    return `自重 × ${range}`;
  }
  return `${
    prescription.load.status === "known"
      ? `${prescription.load.weight_kg} kg`
      : "待校准"
  } × ${range}`;
}

/** 计划路径待确认：展示 draft 正文供用户判断，确认启用或拒绝归档 */
export default function PlanWaitingCard({
  planId,
  busy,
  onConfirm,
  onReject,
}: {
  planId: number;
  busy: boolean;
  onConfirm: () => void;
  onReject: () => void;
}) {
  const plan = useQuery({
    queryKey: ["plans", planId],
    queryFn: () => getPlan(planId),
  });
  const exercises = useQuery({
    queryKey: ["exercises"],
    queryFn: listExercises,
  });

  const nameOf = (exerciseId: string) =>
    exercises.data?.exercises.find((exercise) => exercise.id === exerciseId)
      ?.standard_name_zh ?? exerciseId;

  const wire = plan.data?.plan ?? null;
  const draft =
    wire === null ? null : (wire.structured_content as PlanDraftWire);
  const actionsDisabled = busy || plan.isPending;

  return (
    <Card>
      <CardHeader>
        <CardTitle>待确认计划 #{planId}</CardTitle>
        <CardDescription>
          确认后启用为新 active；拒绝会归档该 draft，原计划保持不变。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {plan.isPending && (
          <p className="text-sm text-muted-foreground">正在加载计划内容…</p>
        )}
        {plan.isError && (
          <p className="text-sm text-destructive">
            计划内容加载失败：{plan.error.message}
          </p>
        )}
        {wire !== null && draft !== null && (
          <>
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <span className="tabular-nums">版本 {wire.version}</span>
              {wire.source_plan_id !== null && (
                <span className="tabular-nums">
                  来源计划 #{wire.source_plan_id}
                </span>
              )}
              <span className="tabular-nums">起始日 {draft.starts_on}</span>
              <span className="tabular-nums">
                每周 {draft.weekly_frequency} 次
              </span>
            </div>
            <p className="text-sm font-medium">{draft.goal}</p>
            <p className="whitespace-pre-wrap text-xs text-muted-foreground">
              {draft.explanation}
            </p>
            <div className="flex flex-col gap-2">
              {draft.training_days.map((day) => (
                <div
                  key={day.scheduled_on}
                  className="flex flex-col gap-1.5 rounded-lg border border-border/60 p-2.5"
                >
                  <div className="text-xs text-muted-foreground tabular-nums">
                    {day.scheduled_on} · {day.exercises.length} 个动作
                  </div>
                  <ul className="flex flex-col gap-1">
                    {day.exercises.map((exercise) => (
                      <li
                        key={exercise.exercise_id}
                        className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-sm"
                      >
                        <span className="font-medium">
                          {nameOf(exercise.exercise_id)}
                        </span>
                        <span className="tabular-nums text-xs text-muted-foreground">
                          {exercise.sets} 组 · {loadText(exercise.prescription)}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          </>
        )}
        <div className="flex justify-end gap-2">
          <Button onClick={onConfirm} disabled={actionsDisabled}>
            确认启用
          </Button>
          <Button
            variant="outline"
            onClick={onReject}
            disabled={actionsDisabled}
          >
            拒绝
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
