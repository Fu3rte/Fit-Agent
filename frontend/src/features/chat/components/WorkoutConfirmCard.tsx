import { useState } from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { DatePicker } from "@/components/ui/date-picker";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  confirmWorkout,
  listExercises,
  listPlanSessionCandidates,
} from "@/lib/api";
import { SET_TYPE_LABELS } from "@/lib/catalogLabels";
import type {
  ConfirmWorkoutResponseWire,
  SetTypeWire,
} from "@/lib/contract";
import {
  addSetRow,
  confirmBodyOf,
  initialCandidates,
  removeSetRow,
  rowsFromWorkout,
  type SessionChoice,
  type WorkoutDraftRow,
} from "@/features/chat/utils/workoutDraft";
import type { WorkoutDraft } from "@/features/chat/utils/chatRound";

/** 打卡确认表单：``waiting.workout`` 为编辑数据源，候选日程来自数据库查询 */
export default function WorkoutConfirmCard({
  draft,
  onConfirmed,
  onCancel,
}: {
  draft: WorkoutDraft;
  onConfirmed: (response: ConfirmWorkoutResponseWire) => void;
  onCancel: () => void;
}) {
  const queryClient = useQueryClient();
  const exercises = useQuery({
    queryKey: ["exercises"],
    queryFn: listExercises,
  });
  const [performedOn, setPerformedOn] = useState(draft.workout.performed_on);
  const [choice, setChoice] = useState<SessionChoice>(
    draft.workout.plan_session_id ?? "extra",
  );
  const [rows, setRows] = useState<WorkoutDraftRow[]>(() =>
    rowsFromWorkout(draft.workout.sets),
  );

  /**
   * 候选日程：``waiting.candidate_plan_sessions`` 只在它自己的日期上作为初始值，用户改日期后按新日期
   * 重查（``listPlanSessionCandidates`` 复用记录页的既有查询口径）。
   */
  const candidates = useQuery({
    queryKey: ["plan-session-candidates", performedOn],
    queryFn: () => listPlanSessionCandidates(performedOn),
    initialData: initialCandidates(
      performedOn,
      draft.workout.performed_on,
      draft.candidates,
    ),
    enabled: performedOn !== "",
  });
  const available = candidates.data?.sessions ?? [];
  /** 候选数量提示：0／1／多候选三种措辞；多候选必须让用户显式选择，否则提交被判为日程歧义 */
  const candidateHint =
    available.length === 0
      ? "当天没有可关联的计划日程：请显式选择「额外训练」，否则提交会被既有领域规则判为日程歧义。"
      : available.length === 1
        ? "当天恰有一个未完成日程；保持「未手动选择」即由服务端关联它，也可以改选。"
        : `当天有 ${available.length} 个未完成日程：请选择本次训练对应的那个，或显式选择「额外训练」；保持「未手动选择」会被既有领域规则判为日程歧义。`;
  const nameOf = (exerciseId: string) =>
    exercises.data?.exercises.find((exercise) => exercise.id === exerciseId)
      ?.standard_name_zh ?? exerciseId;

  const save = useMutation({
    mutationFn: () =>
      confirmWorkout(
        confirmBodyOf({
          chat_id: draft.chat_id,
          conversation_id: draft.conversation_id,
          performed_on: performedOn,
          rows,
          choice,
        }),
      ),
    onSuccess: async (data) => {
      toast.success("训练记录已确认写入");
      await queryClient.invalidateQueries();
      onConfirmed(data);
    },
    onError: (error) =>
      toast.error(error instanceof Error ? error.message : "确认写入失败"),
  });

  const updateRow = (index: number, patch: Partial<WorkoutDraftRow>) => {
    setRows((current) =>
      current.map((row, position) =>
        position === index ? { ...row, ...patch } : row,
      ),
    );
  };

  /**
   * 改日期：候选与关联选择都回到 waiting 的初始默认值——旧日期选中的日程 id 不得被带到新日期提交。
   */
  const changePerformedOn = (value: string) => {
    setPerformedOn(value);
    setChoice(draft.workout.plan_session_id ?? "extra");
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>确认训练</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-sm">
            日期
            <DatePicker
              value={performedOn}
              onChange={changePerformedOn}
              label="训练日期"
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            计划日程
            <Select
              value={String(choice)}
              onValueChange={(value) =>
                setChoice(
                  value === "auto" || value === "extra" ? value : Number(value),
                )
              }
            >
              <SelectTrigger aria-label="计划日程">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="extra">
                  额外训练（不关联计划日程）
                </SelectItem>
                <SelectItem value="auto">
                  未手动选择（恰一个候选时自动关联）
                </SelectItem>
                {available.map((session) => (
                  <SelectItem key={session.id} value={String(session.id)}>
                    计划 {session.plan_id} · {session.scheduled_on}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </label>
        </div>

        {performedOn !== "" && candidates.isPending && (
          <p className="text-xs text-muted-foreground">
            正在查询 {performedOn} 的计划日程…
          </p>
        )}
        {candidates.isError && (
          <p className="text-xs text-destructive">
            计划日程候选加载失败：{candidates.error.message}
          </p>
        )}
        {candidates.isSuccess && (
          <p className="text-xs text-muted-foreground">{candidateHint}</p>
        )}

        <div className="flex flex-col gap-3">
          {rows.map((row, index) => (
            <div
              key={index}
              className="flex flex-col gap-1 rounded-md border border-border/60 p-3"
            >
              <div className="flex flex-wrap items-end gap-2">
                <span className="text-sm">{nameOf(row.exercise_id)}</span>
                <label className="flex flex-col gap-1 text-xs text-muted-foreground">
                  组序号
                  <Input
                    type="number"
                    min={1}
                    max={50}
                    step={1}
                    value={row.set_no}
                    onChange={(event) =>
                      updateRow(index, { set_no: event.target.value })
                    }
                    className="w-24"
                  />
                </label>
                <label className="flex flex-col gap-1 text-xs text-muted-foreground">
                  组类型
                  <Select
                    value={row.set_type}
                    onValueChange={(value) =>
                      updateRow(index, { set_type: value as SetTypeWire })
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
                {row.timed ? (
                  <label className="flex flex-col gap-1 text-xs text-muted-foreground">
                    秒数
                    <Input
                      type="number"
                      min={1}
                      step={1}
                      value={row.duration_seconds}
                      onChange={(event) =>
                        updateRow(index, {
                          duration_seconds: event.target.value,
                        })
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
                {row.load_convention !== null && (
                  <label className="flex flex-col gap-1 text-xs text-muted-foreground">
                    重量（kg）
                    <Input
                      type="number"
                      min={0}
                      step={0.1}
                      value={row.weight_kg}
                      onChange={(event) =>
                        updateRow(index, { weight_kg: event.target.value })
                      }
                      className="w-28"
                    />
                  </label>
                )}
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() =>
                    setRows((current) => removeSetRow(current, index))
                  }
                  disabled={rows.length === 1}
                >
                  <Trash2 aria-hidden />
                  删除组
                </Button>
              </div>
            </div>
          ))}
          <div>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setRows((current) => addSetRow(current))}
            >
              添加组
            </Button>
          </div>
        </div>

        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onCancel} disabled={save.isPending}>
            取消
          </Button>
          <Button onClick={() => save.mutate()} disabled={save.isPending}>
            确认写入
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
