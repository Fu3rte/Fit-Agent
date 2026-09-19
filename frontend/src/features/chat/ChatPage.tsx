/**
 * /chat 对话页（stage6.md §2.5.1；LANGGRAPH_REFACTOR_PLAN.md §7.2.4）：自然语言输入 → Agent Run SSE →
 * `message` 只作可读摘要展示 → 以 `waiting` 双路径驱动确认 UI。
 *
 * - 计划路径 `waiting`（`{draft_plan_id}`）：渲染「确认启用／拒绝」，调 `confirmPlan`／`rejectPlan`；
 * - 打卡路径 `waiting`（`{workout, candidate_plan_sessions}`）：以 `waiting.workout` 初始化编辑表单、
 *   以 `waiting.candidate_plan_sessions` 渲染候选日程，确认调 `confirmWorkout`，成功后失效记录派生 Query。
 *
 * 硬边界：
 * - **不从 `message.text` 反解任何确认字段**（§2.5.2）：确认数据只来自 `waiting` 结构化字段；
 * - **多候不由系统自动选择**（§2.1）：未手动选择时提交 `plan_session_id=null` 且 `auto_link=true`，
 *   由既有领域规则给出日程歧义错误并原样展示（§3.3）；
 * - **不重算 PB／趋势、不复制领域校验**（§3.3）：`confirm-workout` 的失败按后端产品错误原文展示；
 * - **取消只清本地状态**（§4.1）：不发请求、不写库；
 * - 多轮历史只在本页内存（每次运行新建 UUID，即后端 Checkpointer 的 thread_id；刷新即丢）。
 */
import { useEffect, useMemo, useRef, useState } from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";
import { SendHorizontal, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  confirmPlan,
  confirmWorkout,
  listExercises,
  listPlanSessionCandidates,
  rejectPlan,
  runAgentStream,
} from "@/lib/api";
import type {
  AgentEventWire,
  ConfirmWorkoutDraftWire,
  ConfirmWorkoutResponseWire,
  PersonalBestWire,
  PlanSessionCandidateWire,
  RecordWire,
  SetTypeWire,
} from "@/lib/contract";
import {
  addSetRow,
  confirmBodyOf,
  initialCandidates,
  initialSessionChoice,
  removeSetRow,
  rowsFromWorkout,
  type SessionChoice,
  type WorkoutDraftRow,
} from "@/features/chat/workoutDraft";
import { useBubbleShrinkwrap } from "@/features/chat/useBubbleShrinkwrap";

/** 组类型固定三态（与后端 workout_sets CHECK 同集合） */
const SET_TYPE_LABELS: Record<SetTypeWire, string> = {
  work: "工作",
  warmup: "热身",
  assisted: "辅助",
};

/** 表单控件样式：与 components/ui/input 同规格的原生 select */
const selectClass =
  "h-10 rounded-md border border-input bg-transparent px-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40";

/**
 * 训练写入后失效记录派生 Query：沿用 ``RecordsPage.tsx`` 的既有口径（无 key 全量失效），
 * 不新造第二套 key 命名——枚举会在看板命名变化时静默漏失效。
 */
function invalidateRecordDerivedQueries(queryClient: QueryClient): Promise<void> {
  return queryClient.invalidateQueries();
}

/** 计划确认／拒绝成功后失效计划与日历相关 Query（沿用 ``PlansPage.tsx`` 的既有口径） */
async function invalidatePlanAndCalendarQueries(
  queryClient: QueryClient,
): Promise<void> {
  await queryClient.invalidateQueries({ queryKey: ["plans"] });
  await queryClient.invalidateQueries({ queryKey: ["calendar"] });
}

/** 本页内存里的一轮对话（每次运行一条 UUID，即后端 Checkpointer 的 thread_id） */
interface ChatRound {
  conversation_id: string;
  request: string;
  events: AgentEventWire[];
}

/** 打卡路径待确认载荷：``waiting`` 的两个字段 ＋ 产出它的那次运行的 conversation_id */
interface WorkoutDraft {
  conversation_id: string;
  workout: ConfirmWorkoutDraftWire;
  candidates: PlanSessionCandidateWire[];
}

/** 计划路径待确认载荷：``waiting.draft_plan_id`` ＋ 产出它的那次运行的 conversation_id */
interface PlanDraft {
  conversation_id: string;
  plan_id: number;
}

/** 一件事件的可读行；``message`` 的可见文本原样渲染，不解析 */
function eventText(event: AgentEventWire): string {
  switch (event.event) {
    case "node":
      return `节点 ${event.data.name}`;
    case "message":
      return event.data.text;
    case "waiting":
      return "draft_plan_id" in event.data
        ? `等待确认：计划 #${event.data.draft_plan_id}`
        : "解析结果待确认";
    case "done":
      return `结束：intent ${event.data.intent ?? "无"}，终止原因 ${
        event.data.termination_reason ?? "无"
      }`;
    case "error":
      return `错误：${event.data.message}`;
  }
}

/** 打卡确认表单：``waiting.workout`` 为编辑数据源，候选日程来自数据库查询 */
function WorkoutConfirmCard({
  draft,
  onConfirmed,
  onCancel,
}: {
  draft: WorkoutDraft;
  onConfirmed: (response: ConfirmWorkoutResponseWire) => void;
  onCancel: () => void;
}) {
  const queryClient = useQueryClient();
  const exercises = useQuery({ queryKey: ["exercises"], queryFn: listExercises });
  const [performedOn, setPerformedOn] = useState(draft.workout.performed_on);
  const [choice, setChoice] = useState<SessionChoice>(() =>
    initialSessionChoice(draft.workout.plan_session_id, draft.workout.auto_link),
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
  const nameOf = (exerciseId: string) =>
    exercises.data?.exercises.find((exercise) => exercise.id === exerciseId)
      ?.standard_name_zh ?? exerciseId;

  const save = useMutation({
    mutationFn: () =>
      confirmWorkout(
        confirmBodyOf({
          conversation_id: draft.conversation_id,
          performed_on: performedOn,
          rows,
          choice,
        }),
      ),
    onSuccess: async (data) => {
      toast.success("训练记录已确认写入");
      await invalidateRecordDerivedQueries(queryClient);
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
    setChoice(
      initialSessionChoice(
        draft.workout.plan_session_id,
        draft.workout.auto_link,
      ),
    );
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>解析结果确认</CardTitle>
        <CardDescription>
          数据源是本次解析的结构化结果为编辑起点；服务端会按提交的完整载荷重新校验。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-sm">
            日期
            <Input
              type="date"
              value={performedOn}
              onChange={(event) => changePerformedOn(event.target.value)}
              className="w-44"
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            计划日程
            <select
              className={selectClass}
              value={String(choice)}
              onChange={(event) => {
                const value = event.target.value;
                setChoice(
                  value === "auto" || value === "extra" ? value : Number(value),
                );
              }}
            >
              <option value="auto">未手动选择（恰一个候选时自动关联）</option>
              <option value="extra">额外训练（不关联计划日程）</option>
              {available.map((session) => (
                <option key={session.id} value={session.id}>
                  计划 {session.plan_id} · {session.scheduled_on}
                </option>
              ))}
            </select>
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
        {candidates.isSuccess && available.length === 0 && (
          <p className="text-xs text-muted-foreground">
            当天没有可关联的计划日程：请显式选择「额外训练」，否则提交会被既有领域规则判为日程歧义。
          </p>
        )}
        {candidates.isSuccess && available.length === 1 && (
          <p className="text-xs text-muted-foreground">
            当天恰有一个未完成日程；保持「未手动选择」即由服务端关联它，也可以改选。
          </p>
        )}
        {candidates.isSuccess && available.length > 1 && (
          <p className="text-xs text-muted-foreground">
            当天有 {available.length} 个未完成日程：请选择本次训练对应的那个，或显式选择「额外训练」；
            保持「未手动选择」会被既有领域规则判为日程歧义。
          </p>
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
                  <select
                    className={selectClass}
                    value={row.set_type}
                    onChange={(event) =>
                      updateRow(index, {
                        set_type: event.target.value as SetTypeWire,
                      })
                    }
                  >
                    {(Object.keys(SET_TYPE_LABELS) as SetTypeWire[]).map(
                      (value) => (
                        <option key={value} value={value}>
                          {SET_TYPE_LABELS[value]}
                        </option>
                      ),
                    )}
                  </select>
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

/** 计划路径待确认：确认启用或拒绝归档（详情在「训练计划」页） */
function PlanWaitingCard({
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
  return (
    <Card>
      <CardHeader>
        <CardTitle>待确认计划 #{planId}</CardTitle>
        <CardDescription>
          确认后启用为新 active；拒绝会归档该 draft，原计划保持不变。计划详情在「训练计划」页查看。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex justify-end gap-2">
        <Button onClick={onConfirm} disabled={busy}>
          确认启用
        </Button>
        <Button variant="outline" onClick={onReject} disabled={busy}>
          拒绝
        </Button>
      </CardContent>
    </Card>
  );
}

/** 确认写入结果：落库训练事实与后端重查的 PB，本页只展示不重算 */
function ConfirmedCard({
  session,
  bests,
}: {
  session: RecordWire;
  bests: PersonalBestWire[];
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>已写入的训练</CardTitle>
        <CardDescription>写入结果与 PB 都由后端给出。</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <span className="tabular-nums">{session.performed_on}</span>
          <Badge variant="secondary">
            {session.plan_session_id === null
              ? "额外训练"
              : `计划日程 #${session.plan_session_id}`}
          </Badge>
          <span className="text-xs text-muted-foreground">
            {session.sets.length} 组
          </span>
        </div>
        {bests.length === 0 ? (
          <p className="text-xs text-muted-foreground">本次写入没有刷新 PB。</p>
        ) : (
          <ul className="flex flex-col gap-1">
            {bests.map((best) => (
              <li
                key={`${best.exercise_id}-${best.pb_type}`}
                className="tabular-nums"
              >
                {best.exercise_name} · {best.pb_type} · {best.value} ·{" "}
                {best.performed_on} · 第 {best.set_no} 组
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

/** /chat 对话页：自然语言打卡确认与计划生成／调整入口 */
export default function ChatPage() {
  const queryClient = useQueryClient();
  const [request, setRequest] = useState("");
  const [rounds, setRounds] = useState<ChatRound[]>([]);
  const [workoutDraft, setWorkoutDraft] = useState<WorkoutDraft | null>(null);
  const [planDraft, setPlanDraft] = useState<PlanDraft | null>(null);
  const [confirmed, setConfirmed] = useState<ConfirmWorkoutResponseWire | null>(
    null,
  );
  /** 消息流滚动容器：新增一轮即贴底；也是帧层读取消息列宽的唯一来源 */
  const scrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [rounds]);

  /** 用户气泡节点与文本：帧层按文本量宽，直接写这两组节点的 maxWidth / width */
  const bubbleNodes = useRef<(HTMLDivElement | null)[]>([]);
  const bubbleTexts = useMemo(
    () => rounds.map((round) => round.request),
    [rounds],
  );
  useBubbleShrinkwrap(bubbleTexts, scrollRef, bubbleNodes);

  /** 事件进本轮的展示列表；`waiting` 按载荷判别路径，分别驱动两个确认 UI */
  const onEvent = (conversationId: string, event: AgentEventWire) => {
    setRounds((current) =>
      current.map((round) =>
        round.conversation_id === conversationId
          ? { ...round, events: [...round.events, event] }
          : round,
      ),
    );
    if (event.event !== "waiting") return;
    if ("draft_plan_id" in event.data) {
      setPlanDraft({
        conversation_id: conversationId,
        plan_id: event.data.draft_plan_id,
      });
    } else {
      setWorkoutDraft({
        conversation_id: conversationId,
        workout: event.data.workout,
        candidates: event.data.candidate_plan_sessions,
      });
    }
  };

  const run = useMutation({
    mutationFn: (payload: { conversationId: string; text: string }) =>
      runAgentStream(
        { conversation_id: payload.conversationId, request: payload.text },
        (event) => onEvent(payload.conversationId, event),
      ),
    onError: (error) =>
      toast.error(error instanceof Error ? error.message : "本次运行失败"),
  });

  /** 发送一轮：每次运行新建 UUID（即后端 Checkpointer 的 thread_id），请求文本原样发送 */
  const send = () => {
    const text = request.trim();
    if (text === "") return;
    const conversationId = crypto.randomUUID();
    setRounds((current) => [
      ...current,
      { conversation_id: conversationId, request: text, events: [] },
    ]);
    setRequest("");
    run.mutate({ conversationId, text });
  };

  const confirm = useMutation({
    mutationFn: (draft: PlanDraft) =>
      confirmPlan({
        conversation_id: draft.conversation_id,
        plan_id: draft.plan_id,
      }),
    onSuccess: async (data) => {
      toast.success(`计划 #${data.plan.id} 已启用`);
      setPlanDraft(null);
      await invalidatePlanAndCalendarQueries(queryClient);
    },
    onError: (error) =>
      toast.error(error instanceof Error ? error.message : "确认计划失败"),
  });

  const reject = useMutation({
    mutationFn: (draft: PlanDraft) =>
      rejectPlan({
        conversation_id: draft.conversation_id,
        plan_id: draft.plan_id,
      }),
    onSuccess: async (data) => {
      toast.success(`计划 #${data.plan.id} 已归档`);
      setPlanDraft(null);
      await invalidatePlanAndCalendarQueries(queryClient);
    },
    onError: (error) =>
      toast.error(error instanceof Error ? error.message : "拒绝计划失败"),
  });

  /** 取消确认：只清本地状态，不发请求、不写库（§4.1） */
  const cancelWorkoutDraft = () => setWorkoutDraft(null);

  return (
    <div className="relative mx-auto flex h-full w-full max-w-3xl flex-col px-6">
      <header className="pt-10 pb-4">
        <h2 className="font-display text-3xl font-light tracking-tight">
          对话
        </h2>
        <p className="mt-1 text-sm text-muted-foreground">
          自然语言记录训练并确认写入，也可以生成或调整计划；意图由后端判定，本页不解析可见文本。
        </p>
      </header>

      {/* 消息流：用户右气泡、助手左气泡；内部滚动，底部留出浮层空间 */}
      <div
        ref={scrollRef}
        className="flex flex-1 flex-col gap-4 overflow-y-auto pb-40 [scrollbar-gutter:stable]"
      >
        {rounds.map((round, index) => (
          <div key={round.conversation_id} className="flex flex-col gap-4">
            <div className="flex justify-end">
              <div
                ref={(node) => {
                  bubbleNodes.current[index] = node;
                }}
                className="bubble bg-bubble-out text-bubble-out-foreground inset-ring-1 inset-ring-bubble-out-border"
              >
                {round.request}
              </div>
            </div>
            <div className="flex justify-start">
              <div className="bubble bubble-assistant bg-bubble-in text-bubble-in-foreground inset-ring-1 inset-ring-border">
                {round.events.length === 0 && (
                  <p className="animate-pulse text-xs text-muted-foreground">
                    正在思考…
                  </p>
                )}
                {round.events.map((event, index) => (
                  <p
                    key={index}
                    className={
                      event.event === "error" ? "text-destructive" : undefined
                    }
                  >
                    <span className="mr-2 text-xs text-muted-foreground">
                      {event.event}
                    </span>
                    {eventText(event)}
                  </p>
                ))}
              </div>
            </div>
          </div>
        ))}

        {planDraft && (
          <PlanWaitingCard
            planId={planDraft.plan_id}
            busy={confirm.isPending || reject.isPending}
            onConfirm={() => confirm.mutate(planDraft)}
            onReject={() => reject.mutate(planDraft)}
          />
        )}

        {workoutDraft && (
          <WorkoutConfirmCard
            key={workoutDraft.conversation_id}
            draft={workoutDraft}
            onConfirmed={(data) => {
              setConfirmed(data);
              setWorkoutDraft(null);
            }}
            onCancel={cancelWorkoutDraft}
          />
        )}

        {confirmed && (
          <ConfirmedCard
            session={confirmed.workout_session}
            bests={confirmed.personal_bests}
          />
        )}
      </div>

      {/* 输入容器：贴底浮层 + 上方渐变蒙板，消息从下方滚过 */}
      <div className="absolute inset-x-0 bottom-0 z-10 bg-background px-6 pt-6 pb-8">
        {/* 蒙板：贴着容器上沿，从背景色向透明淡出 */}
        <div
          aria-hidden
          className="pointer-events-none absolute inset-x-0 bottom-full h-10 bg-linear-to-t from-background to-transparent"
        />

        {/* 运行中状态条 */}
        {run.isPending && (
          <div className="mb-2 flex items-center gap-2 rounded-lg bg-secondary px-3 py-1.5 text-xs text-secondary-foreground">
            <span
              className="size-1.5 animate-pulse rounded-full bg-current"
              aria-hidden
            />
            处理中
          </div>
        )}

        {/* 输入区：胶囊形输入框，发送按钮内嵌，仅有内容时显示 */}
        <div className="relative">
          <Textarea
            value={request}
            onChange={(event) => setRequest(event.target.value)}
            placeholder="用一句话记录训练，或生成／调整计划"
            disabled={run.isPending}
            rows={1}
            className="min-h-0 resize-none rounded-full border-0 bg-card py-4 pr-14 pl-5 shadow-md focus-visible:ring-0"
          />
          {request.trim() !== "" && (
            <Button
              size="icon"
              aria-label="发送"
              onClick={send}
              disabled={run.isPending}
              className="absolute top-1/2 right-5 size-8 -translate-y-1/2"
            >
              <SendHorizontal />
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
