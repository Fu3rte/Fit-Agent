import { useState } from "react";
import {
  CalendarDays,
  Clock,
  Dumbbell,
  Pencil,
  Plus,
  Timer,
  Trash2,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { SET_TYPE_LABELS } from "@/lib/catalogLabels";
import type {
  CalendarPlanSessionWire,
  LoadConvention,
  RecordWire,
  SetTypeWire,
} from "@/lib/contract";

/** 组类型徽标配色：热身组偏暖、辅助组偏冷、正式组中性 */
const SET_TYPE_BADGE: Record<SetTypeWire, string> = {
  work: "border-slate-500/30 bg-slate-500/10",
  warmup:
    "border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400",
  assisted: "border-sky-500/30 bg-sky-500/10 text-sky-700 dark:text-sky-300",
};

const STATUS_BADGE = {
  completed: {
    label: "已完成",
    className:
      "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400",
  },
  scheduled: {
    label: "计划日程",
    className: "border-sky-500/30 bg-sky-500/10 text-sky-700 dark:text-sky-300",
  },
  extra: {
    label: "额外训练",
    className:
      "border-purple-500/30 bg-purple-500/10 text-purple-700 dark:text-purple-300",
  },
} as const;

interface SetGroup {
  exerciseId: string;
  convention: LoadConvention | null;
  sets: RecordWire["sets"];
}

/** 组按动作归并：服务端只保证同动作内组序号唯一，提交顺序不保证动作连续 */
function groupByExercise(sets: RecordWire["sets"]): SetGroup[] {
  const groups = new Map<string, SetGroup>();
  for (const set of sets) {
    const group = groups.get(set.exercise_id);
    if (group === undefined) {
      groups.set(set.exercise_id, {
        exerciseId: set.exercise_id,
        convention: set.load_convention,
        sets: [set],
      });
    } else {
      group.sets.push(set);
    }
  }
  return [...groups.values()];
}

function setAmount(set: RecordWire["sets"][number]): string {
  if (set.duration_seconds !== null) return `${set.duration_seconds} 秒`;
  return `${set.weight_kg === null ? "自重" : `${set.weight_kg} kg`} × ${set.reps} 次`;
}

/** 一次训练：日程／额外训练身份、动作分组明细与编辑／删除入口 */
function RecordBlock({
  record,
  nameOf,
  onEdit,
  onDelete,
}: {
  record: RecordWire;
  nameOf: (exerciseId: string) => string;
  onEdit: () => void;
  onDelete: () => Promise<void>;
}) {
  const [confirming, setConfirming] = useState(false);
  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border/60 bg-muted/20 p-3">
      <div className="flex items-center justify-between gap-2">
        <span className="flex min-w-0 items-center gap-2">
          <Badge variant="secondary" className="tabular-nums">
            {record.plan_session_id === null
              ? "额外训练"
              : `计划日程 #${record.plan_session_id}`}
          </Badge>
          <span className="text-xs text-muted-foreground tabular-nums">
            {record.sets.length} 组
          </span>
        </span>
        <span className="flex shrink-0 items-center gap-0.5">
          <Button
            variant="ghost"
            size="icon"
            className="size-7"
            aria-label="编辑训练记录"
            onClick={onEdit}
          >
            <Pencil className="size-3.5" />
          </Button>
          <Button
            variant={confirming ? "destructive" : "ghost"}
            size="icon"
            className="size-7"
            aria-label={confirming ? "确认删除训练记录" : "删除训练记录"}
            onBlur={() => setConfirming(false)}
            onClick={() => {
              if (!confirming) {
                setConfirming(true);
                return;
              }
              void onDelete();
            }}
          >
            <Trash2 className="size-3.5" />
          </Button>
        </span>
      </div>

      {groupByExercise(record.sets).map((group) => (
        <div key={group.exerciseId} className="flex flex-col gap-1">
          <div className="flex min-w-0 items-center gap-1.5">
            <Dumbbell className="size-3.5 shrink-0 text-primary" />
            <span className="truncate text-sm font-medium">
              {nameOf(group.exerciseId)}
            </span>
            <span className="shrink-0 text-[11px] text-muted-foreground tabular-nums">
              {group.sets.length} 组
            </span>
          </div>
          <div className="flex flex-col gap-1">
            {group.sets.map((set) => (
              <div
                key={set.set_no}
                className="flex items-center justify-between gap-2 rounded-md border border-border/60 bg-background px-2 py-1 text-xs"
              >
                <span className="flex shrink-0 items-center gap-1.5">
                  <span className="font-mono text-muted-foreground tabular-nums">
                    #{set.set_no}
                  </span>
                  <Badge
                    variant="outline"
                    className={`text-[10px] ${SET_TYPE_BADGE[set.set_type]}`}
                  >
                    {SET_TYPE_LABELS[set.set_type]}
                  </Badge>
                </span>
                <span className="flex items-center gap-1 font-mono font-medium tabular-nums">
                  {set.duration_seconds !== null && (
                    <Timer className="size-3 text-muted-foreground" />
                  )}
                  {setAmount(set)}
                </span>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

/**
 * 选中日期的事实面板：当天实际训练（可编辑、删除）与未完成的计划日程；没有训练时给出补录入口。
 *
 * 日程状态与训练事实都取自服务端（月历查询 + 训练记录查询），本组件不重算任何完成度口径。
 */
export default function RecordDayPanel({
  date,
  records,
  sessions,
  nameOf,
  onCreate,
  onEdit,
  onDelete,
}: {
  date: string;
  records: RecordWire[];
  sessions: CalendarPlanSessionWire[];
  nameOf: (exerciseId: string) => string;
  onCreate: () => void;
  onEdit: (recordId: number) => void;
  onDelete: (recordId: number) => Promise<void>;
}) {
  const totalSets = records.reduce(
    (sum, record) => sum + record.sets.length,
    0,
  );
  const pending =
    sessions.find((session) => session.status === "incomplete") ?? null;
  const status: keyof typeof STATUS_BADGE | null =
    records.length === 0
      ? pending === null
        ? null
        : "scheduled"
      : records.every((record) => record.plan_session_id === null)
        ? "extra"
        : "completed";

  return (
    <Card className="@min-[60rem]:h-full">
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <CardTitle className="font-mono text-lg tabular-nums">
                {date}
              </CardTitle>
              {status !== null && (
                <Badge
                  variant="outline"
                  className={STATUS_BADGE[status].className}
                >
                  {STATUS_BADGE[status].label}
                </Badge>
              )}
            </div>
            <CardDescription className="mt-1 tabular-nums">
              {records.length === 0
                ? pending === null
                  ? "当天没有计划日程或训练记录"
                  : `日程 #${pending.id} · 未完成`
                : `${records.length} 次训练 · 共 ${totalSets} 组`}
            </CardDescription>
          </div>
          <Button
            variant="outline"
            size="sm"
            className="shrink-0"
            onClick={onCreate}
          >
            <Plus aria-hidden />
            新增
          </Button>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 @min-[60rem]:min-h-0 @min-[60rem]:flex-1 @min-[60rem]:overflow-y-auto scrollbar-none [&::-webkit-scrollbar]:hidden">
        {records.map((record) => (
          <RecordBlock
            key={record.id}
            record={record}
            nameOf={nameOf}
            onEdit={() => onEdit(record.id)}
            onDelete={() => onDelete(record.id)}
          />
        ))}

        {records.length === 0 && pending !== null && (
          <div className="flex flex-col items-center gap-2 py-8 text-center">
            <span className="flex size-10 items-center justify-center rounded-full bg-sky-500/10 text-sky-600 dark:text-sky-300">
              <Clock className="size-5" />
            </span>
            <div>
              <h4 className="text-sm font-medium">该日程尚未执行</h4>
              <p className="mt-1 text-xs text-muted-foreground">
                新增训练记录时默认关联日程 #{pending.id}，也可以改为额外训练。
              </p>
            </div>
            <Button size="sm" onClick={onCreate}>
              <Plus aria-hidden />
              新增训练记录
            </Button>
          </div>
        )}

        {records.length === 0 && pending === null && (
          <div className="flex flex-col items-center gap-2 py-10 text-center">
            <span className="flex size-10 items-center justify-center rounded-full bg-muted text-muted-foreground">
              <CalendarDays className="size-5" />
            </span>
            <div>
              <h4 className="text-sm font-medium">无训练记录</h4>
              <p className="mt-1 text-xs text-muted-foreground">
                当天没有安排计划，也没有训练打卡事实。
              </p>
            </div>
            <Button variant="outline" size="sm" onClick={onCreate}>
              <Plus aria-hidden />
              补录一次额外训练
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
