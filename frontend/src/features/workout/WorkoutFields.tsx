import { Badge } from "@/components/ui/badge";
import { Field, FieldTitle } from "@/components/ui/field";
import { WORKOUT_LOAD_CONVENTION_LABELS } from "@/lib/catalogLabels";
import type {
  WorkoutContentWire,
  WorkoutExerciseWire,
  WorkoutProposalWire,
  WorkoutSetWire,
} from "@/lib/contract";

/** 一组的已知数值：null 保持未知语义而不补 0，全部为 null 时仍保留该组 */
function describeSet(set: WorkoutSetWire): string {
  const facts = [
    set.reps !== null ? `${set.reps} 次` : null,
    set.weight_kg !== null ? `${set.weight_kg} kg` : null,
    set.duration_seconds !== null ? `${set.duration_seconds} 秒` : null,
  ].filter((fact) => fact !== null);
  return facts.length === 0 ? "具体数据未知" : facts.join(" · ");
}

/** 一个实际动作：目录引用情况、重量口径、组数或组数未知状态与逐组数据 */
function ExerciseFacts({ exercise }: { exercise: WorkoutExerciseWire }) {
  return (
    <Field className="gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <FieldTitle>{exercise.name}</FieldTitle>
        {exercise.exercise_id === null && (
          <Badge variant="outline">目录外动作</Badge>
        )}
        {exercise.load_convention !== null && (
          <Badge variant="secondary">
            {WORKOUT_LOAD_CONVENTION_LABELS[exercise.load_convention]}
          </Badge>
        )}
      </div>
      {exercise.sets.length === 0 ? (
        <p className="text-sm">组数未知</p>
      ) : (
        <ul className="flex flex-col gap-1 text-sm tabular-nums">
          {exercise.sets.map((set, index) => (
            <li key={index}>{`第 ${index + 1} 组：${describeSet(set)}`}</li>
          ))}
        </ul>
      )}
    </Field>
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
    <div className="flex flex-col gap-4">
      {content.exercises.map((exercise, index) => (
        <ExerciseFacts key={index} exercise={exercise} />
      ))}
      <Field className="gap-2">
        <FieldTitle>训练感受</FieldTitle>
        <p className="text-sm whitespace-pre-wrap">{content.notes ?? "未知"}</p>
      </Field>
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
    <div className="flex flex-col gap-4">
      <Field className="gap-2">
        <FieldTitle>训练日期</FieldTitle>
        <p className="text-sm tabular-nums">{proposal.performed_on}</p>
      </Field>
      <WorkoutFields content={proposal.payload} />
    </div>
  );
}
