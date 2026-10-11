import { Fragment } from "react";
import { useQuery } from "@tanstack/react-query";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardFooter, CardHeader } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { getExerciseEquipment } from "@/lib/api";
import { WORKOUT_LOAD_CONVENTION_LABELS } from "@/lib/catalogLabels";
import type { PlanContentWire, PlanExerciseWire } from "@/lib/contract";
import { EXERCISE_EQUIPMENT_QUERY_KEY } from "@/lib/query";

function exerciseParameters(
  exercise: PlanExerciseWire,
  equipment: Readonly<Record<string, string>>,
): string[] {
  const parts: string[] = [];
  const { sets, reps, duration_seconds, weight_kg, load_convention } = exercise;
  if (reps !== null)
    parts.push(sets === null ? `${reps}次` : `${sets}×${reps}`);
  if (duration_seconds !== null)
    parts.push(
      sets === null ? `${duration_seconds}s` : `${sets}×${duration_seconds}s`,
    );
  if (reps === null && duration_seconds === null)
    parts.push(sets === null ? "自由安排" : `${sets}组`);
  if (
    exercise.exercise_id !== null &&
    !Object.hasOwn(equipment, exercise.exercise_id)
  )
    throw new Error(`动作器械目录缺少 ${exercise.exercise_id}。`);
  const bodyWeight =
    exercise.exercise_id !== null &&
    equipment[exercise.exercise_id] === "body weight";
  if (
    weight_kg !== null &&
    (!bodyWeight ||
      load_convention === "added_weight" ||
      load_convention === "assistance_weight")
  ) {
    if (load_convention === null) throw new Error("计划动作缺少重量口径。");
    parts.push(
      `${weight_kg}kg（${WORKOUT_LOAD_CONVENTION_LABELS[load_convention]}）`,
    );
  }
  return parts;
}

export function PlanBoard({
  content,
  equipment,
}: {
  content: PlanContentWire;
  equipment: Readonly<Record<string, string>>;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-3 whitespace-normal wrap-anywhere">
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
        {content.days.map((day, index) => (
          <Card key={index} className="min-w-0">
            <CardHeader className="flex-row items-start justify-between gap-2 p-3">
              <span className="min-w-0 text-xs font-semibold">
                {`Day ${index + 1}${day.focus === null ? "" : ` · ${day.focus}`}`}
              </span>
              <Badge variant="secondary" className="px-1.5 py-0 text-[10px]">
                {day.kind === "training" ? "训练日" : "休息日"}
              </Badge>
            </CardHeader>
            {day.kind === "training" ? (
              day.exercises.length > 0 && (
                <CardContent className="px-3 pb-3 pt-0">
                  <ul className="flex flex-col gap-2">
                    {day.exercises.map((exercise, at) => (
                      <li
                        key={at}
                        className="flex min-w-0 items-start justify-between gap-2 text-xs"
                      >
                        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-1">
                          <span>{exercise.name}</span>
                          {exercise.exercise_id === null && (
                            <Badge
                              variant="outline"
                              className="max-w-full whitespace-normal px-1 py-0 text-[10px]"
                            >
                              未在动作数据集内核实
                            </Badge>
                          )}
                        </div>
                        <div className="flex min-w-0 flex-1 flex-wrap justify-end gap-x-1.5 gap-y-1 text-right tabular-nums">
                          {exerciseParameters(exercise, equipment).map(
                            (part, partIndex) => (
                              <Fragment key={partIndex}>
                                {partIndex > 0 && <span aria-hidden>·</span>}
                                <span
                                  className={
                                    part === "自由安排"
                                      ? "text-muted-foreground"
                                      : undefined
                                  }
                                >
                                  {part}
                                </span>
                              </Fragment>
                            ),
                          )}
                        </div>
                      </li>
                    ))}
                  </ul>
                </CardContent>
              )
            ) : (
              day.notes !== null && (
                <CardContent className="px-3 pb-3 pt-0">
                  <p className="max-w-full whitespace-pre-wrap text-xs text-muted-foreground">
                    {day.notes}
                  </p>
                </CardContent>
              )
            )}
            {day.kind === "training" && day.notes !== null && (
              <CardFooter className="mt-auto items-start gap-2 px-3 pb-3 pt-0">
                <Separator />
                <p className="max-w-full whitespace-pre-wrap text-xs text-muted-foreground">
                  {day.notes}
                </p>
              </CardFooter>
            )}
          </Card>
        ))}
      </div>
      {content.notes !== null && (
        <div className="border-l-2 pl-3 text-xs text-muted-foreground">
          <p className="whitespace-pre-wrap">{content.notes}</p>
        </div>
      )}
    </div>
  );
}

export default function PlanFields({ content }: { content: PlanContentWire }) {
  const equipment = useQuery({
    queryKey: EXERCISE_EQUIPMENT_QUERY_KEY,
    queryFn: ({ signal }) => getExerciseEquipment(signal),
    retry: false,
  });
  if (equipment.isError) throw equipment.error;
  if (equipment.data === undefined) return null;
  return <PlanBoard content={content} equipment={equipment.data} />;
}
