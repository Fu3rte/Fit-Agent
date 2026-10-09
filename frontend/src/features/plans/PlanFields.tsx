import { Badge } from "@/components/ui/badge";
import { Field, FieldTitle } from "@/components/ui/field";
import { WORKOUT_LOAD_CONVENTION_LABELS } from "@/lib/catalogLabels";
import type {
  PlanContentWire,
  PlanDayWire,
  PlanExerciseWire,
} from "@/lib/contract";

/** 建议来源标识（plan-generation-contract §3.1）：`suggested_fields` 命中该字段路径或其任一祖先路径即视为助手补充 */
function suggestionMatcher(paths: readonly string[]) {
  return (path: string) =>
    paths.some((item) => path === item || path.startsWith(`${item}/`));
}

const DAY_KIND_LABELS = { training: "训练日", rest: "休息日" } as const;

/** 一个业务字段的可核对行：值与它自身的 JSON Pointer 路径 */
interface FactRow {
  path: string;
  text: string;
}

function FactRows({
  rows,
  suggested,
}: {
  rows: FactRow[];
  suggested: (path: string) => boolean;
}) {
  return (
    <ul className="flex flex-col gap-1 text-sm tabular-nums">
      {rows.map((row) => (
        <li key={row.path} className="flex flex-wrap items-center gap-2">
          <span>{row.text}</span>
          {suggested(row.path) && <Badge variant="outline">建议</Badge>}
        </li>
      ))}
    </ul>
  );
}

/** 动作数值（§3.1）：null 保持未知而不补 0；`rest_seconds` 的 0 与 null 分别是「不安排休息」与「未结构化指定」 */
function exerciseRows(exercise: PlanExerciseWire, path: string): FactRow[] {
  return [
    {
      path: `${path}/sets`,
      text: exercise.sets === null ? "组数未知" : `${exercise.sets} 组`,
    },
    {
      path: `${path}/reps`,
      text: exercise.reps === null ? "目标次数未知" : `目标 ${exercise.reps} 次`,
    },
    {
      path: `${path}/duration_seconds`,
      text:
        exercise.duration_seconds === null
          ? "时长未知"
          : `时长 ${exercise.duration_seconds} 秒`,
    },
    {
      path: `${path}/weight_kg`,
      text:
        exercise.weight_kg === null
          ? "重量未知"
          : `重量 ${exercise.weight_kg} kg`,
    },
    {
      path: `${path}/load_convention`,
      text:
        exercise.load_convention === null
          ? "重量口径未知"
          : `重量口径：${WORKOUT_LOAD_CONVENTION_LABELS[exercise.load_convention]}`,
    },
    {
      path: `${path}/rest_seconds`,
      text:
        exercise.rest_seconds === null
          ? "组间休息未结构化指定"
          : exercise.rest_seconds === 0
            ? "组间不安排休息"
            : `组间休息 ${exercise.rest_seconds} 秒`,
    },
  ];
}

/** 字段级建议标记：命中该字段自身的指针或其父级指针 */
function Suggested({ shown }: { shown: boolean }) {
  return shown ? <Badge variant="outline">建议</Badge> : null;
}

function ExerciseFacts({
  exercise,
  path,
  suggested,
}: {
  exercise: PlanExerciseWire;
  path: string;
  suggested: (path: string) => boolean;
}) {
  return (
    <Field className="gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <FieldTitle>{exercise.name}</FieldTitle>
        {exercise.exercise_id === null && (
          <Badge variant="outline">未在动作数据集内核实</Badge>
        )}
        <Suggested shown={suggested(path)} />
      </div>
      <FactRows rows={exerciseRows(exercise, path)} suggested={suggested} />
    </Field>
  );
}

function DayFacts({
  day,
  index,
  suggested,
}: {
  day: PlanDayWire;
  index: number;
  suggested: (path: string) => boolean;
}) {
  const path = `/days/${index}`;
  return (
    <Field className="gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <FieldTitle>{`第 ${index + 1} 天 · ${DAY_KIND_LABELS[day.kind]}`}</FieldTitle>
        <Suggested shown={suggested(path)} />
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm text-muted-foreground">训练主题</span>
        <p className="text-sm">{day.focus ?? "未知"}</p>
        <Suggested shown={suggested(`${path}/focus`)} />
      </div>
      {day.exercises.length > 0 && (
        <div className="flex flex-col gap-3">
          {day.exercises.map((exercise, at) => (
            <ExerciseFacts
              /* 重复动作按实际顺序展示，数组下标是唯一稳定的位置身份 */
              key={at}
              exercise={exercise}
              path={`${path}/exercises/${at}`}
              suggested={suggested}
            />
          ))}
        </div>
      )}
      <Field className="gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <FieldTitle>训练日提示</FieldTitle>
          <Suggested shown={suggested(`${path}/notes`)} />
        </div>
        <p className="text-sm whitespace-pre-wrap">{day.notes ?? "未知"}</p>
      </Field>
    </Field>
  );
}

/** 循环方式（§3.1）：`repeat=null` 保持未知语义 */
function repeatLabel(repeat: boolean | null): string {
  if (repeat === null) return "未知";
  return repeat ? "循环执行" : "按序列执行";
}

/**
 * 计划内容展示（plan-generation-contract §6）：循环方式、全部训练日与休息日、主题、动作顺序、
 * 组数、目标次数或时长、重量及口径、结构化休息、逐日提示与整体依据完整呈现，建议来源按
 * `suggested_fields` 逐字段标识；对话中的待确认展示、当前计划与历史版本共用同一口径。
 */
export default function PlanFields({ content }: { content: PlanContentWire }) {
  const suggested = suggestionMatcher(content.suggested_fields);
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <FieldTitle>循环方式</FieldTitle>
        <p className="text-sm">{repeatLabel(content.repeat)}</p>
        <Suggested shown={suggested("/repeat")} />
      </div>
      {content.days.map((day, index) => (
        <DayFacts key={index} day={day} index={index} suggested={suggested} />
      ))}
      <Field className="gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <FieldTitle>依据与注意事项</FieldTitle>
          <Suggested shown={suggested("/notes")} />
        </div>
        <p className="text-sm whitespace-pre-wrap">{content.notes ?? "未知"}</p>
      </Field>
    </div>
  );
}
