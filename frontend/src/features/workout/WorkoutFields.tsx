import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldTitle } from "@/components/ui/field";
import { WORKOUT_LOAD_CONVENTION_LABELS } from "@/lib/catalogLabels";
import type {
  WorkoutContentWire,
  WorkoutExerciseWire,
  WorkoutProposalWire,
  WorkoutSetWire,
} from "@/lib/contract";

/** 未知数值保持未知语义，不补 0 */
const UNKNOWN = "—";

/** 逐组数据表：组别、重量、次数、时长四列，已知组的数值全为 null 时逐列保持未知 */
function SetTable({ sets }: { sets: WorkoutSetWire[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[22rem] text-sm tabular-nums">
        <caption className="sr-only">逐组数据</caption>
        <thead>
          <tr className="text-xs text-muted-foreground">
            <th scope="col" className="py-1.5 pr-3 text-left font-medium">
              组别
            </th>
            <th scope="col" className="py-1.5 pl-3 text-right font-medium">
              重量
            </th>
            <th scope="col" className="py-1.5 pl-3 text-right font-medium">
              次数
            </th>
            <th scope="col" className="py-1.5 pl-3 text-right font-medium">
              时长
            </th>
          </tr>
        </thead>
        <tbody>
          {sets.map((set, index) => (
            <tr key={index} className="border-t border-border">
              <th scope="row" className="py-1.5 pr-3 text-left font-normal">
                {`第 ${index + 1} 组`}
              </th>
              <td className="py-1.5 pl-3 text-right">
                {set.weight_kg === null ? UNKNOWN : `${set.weight_kg} kg`}
              </td>
              <td className="py-1.5 pl-3 text-right">{set.reps ?? UNKNOWN}</td>
              <td className="py-1.5 pl-3 text-right">
                {set.duration_seconds === null
                  ? UNKNOWN
                  : `${set.duration_seconds} 秒`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** 一个实际动作：名称、目录引用情况、重量口径、组数未知状态与逐组数据 */
function ExerciseCard({ exercise }: { exercise: WorkoutExerciseWire }) {
  const setsUnknown = exercise.sets.length === 0;
  return (
    <Card className="rounded-lg">
      <CardHeader className="px-4 py-3.5">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <CardTitle>{exercise.name}</CardTitle>
          {exercise.exercise_id === null && (
            <Badge variant="outline">目录外动作</Badge>
          )}
          {exercise.load_convention !== null && (
            <Badge variant="secondary">
              {WORKOUT_LOAD_CONVENTION_LABELS[exercise.load_convention]}
            </Badge>
          )}
          {setsUnknown && <Badge variant="outline">组数未知</Badge>}
        </div>
      </CardHeader>
      {!setsUnknown && (
        <CardContent className="px-4 pb-4 pt-0">
          <SetTable sets={exercise.sets} />
        </CardContent>
      )}
    </Card>
  );
}

/**
 * 训练内容的展示口径（workout-http-sse-contract §5）：动作、逐组数据与感受全部呈现，
 * 未知值表达为未知而不补 0；对话中的待确认展示与训练记录查询区域共用同一组件。
 */
export default function WorkoutFields({
  content,
}: {
  content: WorkoutContentWire;
}) {
  return (
    <div className="flex flex-col gap-3">
      {content.exercises.map((exercise, index) => (
        <ExerciseCard key={index} exercise={exercise} />
      ))}
      {content.notes !== null && (
        <div className="flex flex-col gap-1.5 rounded-lg bg-muted px-4 py-3.5">
          <span className="text-xs text-muted-foreground">训练感受</span>
          <p className="text-sm leading-relaxed whitespace-pre-wrap break-words">
            {content.notes}
          </p>
        </div>
      )}
    </div>
  );
}

/** 待确认快照展示：具体训练日期 ＋ 完整训练内容 */
export function WorkoutProposalFields({
  proposal,
}: {
  proposal: WorkoutProposalWire;
}) {
  return (
    <div className="flex flex-col gap-3">
      <Field className="gap-2">
        <FieldTitle>训练日期</FieldTitle>
        <p className="text-sm tabular-nums">{proposal.performed_on}</p>
      </Field>
      <WorkoutFields content={proposal.payload} />
    </div>
  );
}
