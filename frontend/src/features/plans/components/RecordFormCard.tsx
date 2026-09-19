import type * as React from "react";
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { DatePicker } from "@/components/ui/date-picker";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  createRecord,
  listPlanSessionCandidates,
  updateRecord,
} from "@/lib/api";
import type {
  ExerciseWire,
  RecordWire,
  SetTypeWire,
  WorkoutSetInputWire,
} from "@/lib/contract";
import {
  LOAD_CONVENTION_LABELS,
  RECORD_TYPE_LABELS,
  SET_TYPE_LABELS,
} from "@/lib/catalogLabels";

/** 客户端本地自然日（表单默认值；业务日期一律由服务端按业务时区判定） */
function todayIso(): string {
  const now = new Date();
  const month = `${now.getMonth() + 1}`.padStart(2, "0");
  const day = `${now.getDate()}`.padStart(2, "0");
  return `${now.getFullYear()}-${month}-${day}`;
}

interface SetRow {
  exerciseId: string;
  setType: SetTypeWire;
  reps: string;
  weight: string;
  /** 计时动作的秒数；非计时动作必须为空（提交时送 null，不送 0） */
  duration: string;
}

function emptySetRow(): SetRow {
  return {
    exerciseId: "",
    setType: "work",
    reps: "",
    weight: "",
    duration: "",
  };
}

/** 既有训练的组 → 表单行：按目录记录口径只回填适用字段，其余字段留空（不保留不适用的旧值） */
function setRowsFromRecord(record: RecordWire): SetRow[] {
  return record.sets.map((set) => ({
    exerciseId: set.exercise_id,
    setType: set.set_type,
    reps: set.reps === null ? "" : String(set.reps),
    weight: set.weight_kg === null ? "" : String(set.weight_kg),
    duration: set.duration_seconds === null ? "" : String(set.duration_seconds),
  }));
}

/**
 * 表单行 → 提交事实：按所选动作的目录记录口径只送适用字段（外加重量：口径 + 重量 + 次数；
 * 纯自重：次数；计时：秒数）；不适用的字段一律送 null，与目录不符的口径在前端不可选，
 * 后端仍会复验（含时长不小于 1 秒的领域唯一规则）。
 */
function toSetInputs(
  rows: SetRow[],
  catalogue: Map<string, ExerciseWire>,
): WorkoutSetInputWire[] {
  if (rows.length === 0) throw new Error("至少需要一组");
  return rows.map((row, index) => {
    const position = index + 1;
    const exercise = catalogue.get(row.exerciseId);
    if (exercise === undefined) {
      throw new Error(`第 ${position} 组：请先选择动作`);
    }
    if (exercise.record_type === "time") {
      const duration = Number(row.duration);
      if (
        row.duration.trim() === "" ||
        !Number.isInteger(duration) ||
        duration < 1
      ) {
        throw new Error(
          `第 ${position} 组：计时动作请填写不小于 1 秒的整数秒数`,
        );
      }
      return {
        exercise_id: exercise.id,
        set_type: row.setType,
        reps: null,
        load_convention: null,
        weight_kg: null,
        duration_seconds: duration,
      };
    }
    const reps = Number(row.reps);
    if (row.reps.trim() === "" || !Number.isInteger(reps)) {
      throw new Error(`第 ${position} 组：次数必须是整数`);
    }
    if (exercise.load_convention === null) {
      return {
        exercise_id: exercise.id,
        set_type: row.setType,
        reps,
        load_convention: null,
        weight_kg: null,
        duration_seconds: null,
      };
    }
    const weight = Number.parseFloat(row.weight);
    if (row.weight.trim() === "" || Number.isNaN(weight)) {
      throw new Error(`第 ${position} 组：请填写重量`);
    }
    return {
      exercise_id: exercise.id,
      set_type: row.setType,
      reps,
      load_convention: exercise.load_convention,
      weight_kg: weight,
      duration_seconds: null,
    };
  });
}

/**
 * 训练记录新增／编辑表单；record 为 null 即新增（日期默认取 ``initialDate``，缺省用本地当天）。
 */
export function RecordFormCard({
  record,
  exercises,
  initialDate,
  onDone,
}: {
  record: RecordWire | null;
  exercises: ExerciseWire[];
  initialDate?: string;
  onDone: () => void;
}) {
  const queryClient = useQueryClient();
  const catalogue = useMemo(
    () => new Map(exercises.map((exercise) => [exercise.id, exercise])),
    [exercises],
  );
  const [performedOn, setPerformedOn] = useState(
    record?.performed_on ?? initialDate ?? todayIso(),
  );
  /* auto = 未手动选择（当天恰有一个候选时关联它）；extra = 明确额外训练；number = 显式日程 */
  const [sessionChoice, setSessionChoice] = useState<"auto" | "extra" | number>(
    record === null ? "auto" : (record.plan_session_id ?? "extra"),
  );
  /* 初始组行：编辑取该条记录的组，新增留一行空行（动作、次数、重量全为空） */
  const [rows, setRows] = useState<SetRow[]>(() =>
    record !== null ? setRowsFromRecord(record) : [emptySetRow()],
  );

  const candidates = useQuery({
    queryKey: ["plan-session-candidates", performedOn],
    queryFn: () => listPlanSessionCandidates(performedOn),
    enabled: performedOn !== "",
  });
  const available = candidates.data?.sessions ?? [];

  /* 编辑既有记录时它自己关联的日程已不在候选里（已被占用），但仍必须是可选值。 */
  const options = useMemo(() => {
    const items = available.map((session) => ({
      id: session.id,
      label: `计划 ${session.plan_id} · ${session.scheduled_on}`,
    }));
    const current = record?.plan_session_id ?? null;
    if (current !== null && !items.some((item) => item.id === current)) {
      items.push({ id: current, label: `#${current}（当前关联的日程）` });
    }
    return items;
  }, [available, record?.plan_session_id]);

  const selected: "extra" | number =
    sessionChoice === "auto"
      ? available.length === 1
        ? available[0].id
        : "extra"
      : sessionChoice === "extra" ||
          options.some((option) => option.id === sessionChoice)
        ? sessionChoice
        : "extra";

  const save = useMutation({
    mutationFn: () => {
      const body = {
        performed_on: performedOn,
        plan_session_id: selected === "extra" ? null : selected,
        sets: toSetInputs(rows, catalogue),
      };
      return record === null
        ? createRecord(body)
        : updateRecord(record.id, body);
    },
    onSuccess: async () => {
      toast.success(record === null ? "训练记录已保存" : "训练记录已更新");
      await queryClient.invalidateQueries();
      onDone();
    },
    onError: (error) =>
      toast.error(error instanceof Error ? error.message : "训练记录保存失败"),
  });

  const updateRow = (index: number, patch: Partial<SetRow>) => {
    setRows((current) =>
      current.map((row, position) =>
        position === index ? { ...row, ...patch } : row,
      ),
    );
  };

  return (
    <Card className="border-0 bg-transparent">
      <CardHeader>
        <CardTitle>
          {record === null ? "新增训练记录" : "编辑训练记录"}
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-sm">
            日期
            <DatePicker
              value={performedOn}
              onChange={setPerformedOn}
              label="训练日期"
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            计划日程
            <Select
              value={String(selected)}
              onValueChange={(value) =>
                setSessionChoice(value === "extra" ? "extra" : Number(value))
              }
            >
              <SelectTrigger aria-label="计划日程">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="extra">
                  额外训练（不关联计划日程）
                </SelectItem>
                {options.map((option) => (
                  <SelectItem key={option.id} value={String(option.id)}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </label>
        </div>

        {candidates.isPending && (
          <p className="text-xs text-muted-foreground">正在查询当天计划日程…</p>
        )}
        {candidates.isError && (
          <p className="text-xs text-destructive">
            计划日程候选加载失败：{candidates.error.message}
          </p>
        )}
        {candidates.isSuccess && available.length === 0 && (
          <p className="text-xs text-muted-foreground">
            当天没有可关联的计划日程；保存后按「额外训练」记录。
          </p>
        )}
        {candidates.isSuccess && available.length === 1 && (
          <p className="text-xs text-muted-foreground">
            当天恰有一个计划日程，默认关联它；也可以改为额外训练。
          </p>
        )}
        {candidates.isSuccess && available.length > 1 && (
          <p className="text-xs text-muted-foreground">
            当天有 {available.length}{" "}
            个计划日程候选，请选择要完成的那个，或明确选择额外训练。
          </p>
        )}

        <div className="flex flex-col gap-3">
          <Button
            className="self-end"
            variant="outline"
            size="sm"
            onClick={() => setRows((current) => [...current, emptySetRow()])}
          >
            <Plus aria-hidden />
            添加组
          </Button>

          {rows.map((row, index) => {
            const exercise = catalogue.get(row.exerciseId);
            const convention = exercise?.load_convention ?? null;
            const recordType = exercise?.record_type ?? null;
            return (
              <div
                key={index}
                className="flex flex-col gap-1 rounded-md border border-border/60 p-3"
              >
                <div className="flex flex-wrap items-end gap-2">
                  <label className="flex flex-col gap-1 text-xs text-muted-foreground">
                    动作
                    <Select
                      value={row.exerciseId}
                      onValueChange={(value) =>
                        updateRow(index, {
                          exerciseId: value,
                          /* 切换动作后清除不再适用的旧值：重量、次数与秒数都不跨动作保留 */
                          reps: "",
                          weight: "",
                          duration: "",
                        })
                      }
                    >
                      <SelectTrigger aria-label="动作">
                        <SelectValue placeholder="请选择动作" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="">请选择动作</SelectItem>
                        {exercises.map((item) => (
                          <SelectItem key={item.id} value={item.id}>
                            {item.standard_name_zh}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </label>
                  <label className="flex flex-col gap-1 text-xs text-muted-foreground">
                    组类型
                    <Select
                      value={row.setType}
                      onValueChange={(value) =>
                        updateRow(index, { setType: value as SetTypeWire })
                      }
                    >
                      <SelectTrigger aria-label="组类型">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {(Object.keys(SET_TYPE_LABELS) as SetTypeWire[]).map(
                          (value) => (
                            <SelectItem key={value} value={value}>
                              {SET_TYPE_LABELS[value]}
                            </SelectItem>
                          ),
                        )}
                      </SelectContent>
                    </Select>
                  </label>
                  {recordType === "time" ? (
                    <label className="flex flex-col gap-1 text-xs text-muted-foreground">
                      秒数
                      <Input
                        type="number"
                        min={1}
                        step={1}
                        value={row.duration}
                        onChange={(event) =>
                          updateRow(index, { duration: event.target.value })
                        }
                        className="w-28"
                      />
                    </label>
                  ) : (
                    <label className="flex flex-col gap-1 text-xs text-muted-foreground">
                      次数
                      <Input
                        type="number"
                        min={1}
                        max={100}
                        value={row.reps}
                        onChange={(event) =>
                          updateRow(index, { reps: event.target.value })
                        }
                        className="w-24"
                      />
                    </label>
                  )}
                  {convention !== null && (
                    <label className="flex flex-col gap-1 text-xs text-muted-foreground">
                      重量（kg）
                      <Input
                        type="number"
                        min={0}
                        step={0.1}
                        value={row.weight}
                        onChange={(event) =>
                          updateRow(index, { weight: event.target.value })
                        }
                        className="w-28"
                      />
                    </label>
                  )}
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() =>
                      setRows((current) =>
                        current.filter((_, position) => position !== index),
                      )
                    }
                    disabled={rows.length === 1}
                  >
                    <Trash2 aria-hidden />
                    删除组
                  </Button>
                </div>
                {exercise !== undefined && (
                  <p className="text-xs text-muted-foreground">
                    {recordType === "time"
                      ? `${RECORD_TYPE_LABELS.time}型动作：只记录秒数，不记录负重口径、重量与次数`
                      : convention === null
                        ? `${RECORD_TYPE_LABELS[exercise.record_type]}型动作：只记录次数，不记录负重口径与重量`
                        : `负重口径：${LOAD_CONVENTION_LABELS[convention]}${
                            exercise.min_load_increment_kg === null
                              ? ""
                              : ` · 最小加重 ${exercise.min_load_increment_kg}kg`
                          }`}
                  </p>
                )}
              </div>
            );
          })}
        </div>

        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onDone} disabled={save.isPending}>
            取消
          </Button>
          <Button onClick={() => save.mutate()} disabled={save.isPending}>
            {record === null ? "保存训练记录" : "保存修改"}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

/**
 * 记录表单弹层：新增与编辑共用，Esc／点遮罩／「取消」都回到面板；
 * 用 shadcn Dialog（Radix）承载，表单里的 Select／Tooltip 等同源 Radix 弹层才能正常弹出；
 * 组多时表单在弹层内部滚动（内容区 max-h 85dvh），不把双栏面板挤出视口。
 */
export function RecordFormDialog(
  props: React.ComponentProps<typeof RecordFormCard>,
) {
  const title = props.record === null ? "新增训练记录" : "编辑训练记录";
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) props.onDone();
      }}
    >
      <DialogContent
        aria-describedby={undefined}
        className="max-h-[85dvh] max-w-3xl p-0"
      >
        <DialogTitle className="sr-only">{title}</DialogTitle>
        <div className="min-h-0 flex-1 overflow-y-auto p-2 pt-6 [scrollbar-color:var(--color-border)_transparent] scrollbar-thin [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-border [&::-webkit-scrollbar]:w-2">
          <RecordFormCard {...props} />
        </div>
      </DialogContent>
    </Dialog>
  );
}
