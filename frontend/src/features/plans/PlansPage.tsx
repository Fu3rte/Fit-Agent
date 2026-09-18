/**
 * /plans 计划页（stage5.md §3.10、Subtask 06）：active、唯一 draft（结构化计划与 Evaluator 摘要）、
 * archived／rejected 历史，以及生成／调整／确认／拒绝与五类 SSE 进度。
 *
 * 三条硬边界：
 *
 * - **intent 只由后端 Router 判定**：本页不复制词表、不推断意图。触发按钮只在输入为空时发送自身的
 *   封闭短语（「生成计划」「调整计划」）；输入非空时发送用户原文，因此输入「查看进步」得到的是后端
 *   现算的确定性统计与解释，而不会被前端改写成别的意图。
 * - **不重算业务数值**：结构化计划与 Evaluator 结果按后端字段原样展示（只做 JSON 缩进），PB／趋势／
 *   完成率等只展示后端文本，不在前端计算、补算或判断进步退步。
 * - **不做 Stage 6 的事**：没有自然语言打卡、拖拽排程或计划编辑器，本页不写任何训练记录。
 *
 * conversation_id：每次新的生成／调整都新建一个 UUID（即后端 Checkpointer 的 thread_id），并记下本次运行
 * 产出（或复用）的 draft 身份；confirm／reject 取该 draft 对应的那次运行的值。页面刷新后从
 * ``GET /api/plans`` 加载到的既有 draft 没有本次会话的 conversation_id，此时按 §3.3 的「唯一 draft 兜底」
 * 提交（checkpoint 恢复不到等待任务时后端回到业务库唯一 draft，并要求 plan_id 相等）。
 */
import { useState } from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";
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
import { Textarea } from "@/components/ui/textarea";
import {
  confirmPlan,
  getActivePlan,
  listPlans,
  rejectPlan,
  runAgentStream,
} from "@/lib/api";
import type { AgentEventWire, PlanWire } from "@/lib/contract";

/** 计划 Query key：``["plans"]`` 前缀同时覆盖 active 与全部版本列表 */
const ACTIVE_PLAN_KEY = ["plans", "active"];
const PLAN_LIST_KEY = ["plans", "list"];

const PLAN_STATUS_LABELS: Record<PlanWire["status"], string> = {
  draft: "待确认",
  active: "已启用",
  archived: "已归档",
  rejected: "评估未通过",
};

/**
 * 确认／拒绝成功后失效计划与日历相关 Query（§3.10）：激活会改写计划版本与 active，并重建／取消计划日程，
 * 月历随之变化。其余 Query（训练记录、PB、趋势）仍由记录页的写入失效负责。
 */
async function invalidatePlanAndCalendarQueries(
  queryClient: QueryClient,
): Promise<void> {
  await queryClient.invalidateQueries({ queryKey: ["plans"] });
  await queryClient.invalidateQueries({ queryKey: ["calendar"] });
}

/** 后端 JSON 字段原样展示：只缩进，不解释领域形状，也不派生任何业务字段 */
function JsonBlock({ value }: { value: unknown }) {
  return (
    <pre className="max-h-72 overflow-auto rounded-md border border-border/60 bg-muted/40 p-3 text-xs leading-relaxed">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

/** 一条 SSE 进度文案：事件名与载荷都来自后端事件，不加工数值、不推断结果 */
function progressText(event: AgentEventWire): string {
  switch (event.event) {
    case "node":
      return `节点 ${event.data.name}`;
    case "message":
      return event.data.text;
    case "waiting":
      return `等待确认：计划 #${event.data.draft_plan_id}`;
    case "done":
      return `结束：intent ${event.data.intent ?? "无"}，终止原因 ${
        event.data.termination_reason ?? "无"
      }，计划 ${event.data.draft_plan_id ?? "无"}`;
    case "error":
      return `错误：${event.data.message}`;
  }
}

/** 一次 Run 事件里被持久化或复用的 draft 身份；只有 ``waiting``／``done`` 带 ``draft_plan_id`` */
function draftPlanIdOf(event: AgentEventWire): number | null {
  if (event.event === "waiting") return event.data.draft_plan_id;
  if (event.event === "done") return event.data.draft_plan_id;
  return null;
}

/** 一个计划版本的身份与时间字段；不展示结构化内容（由卡片各自决定） */
function PlanMeta({ plan }: { plan: PlanWire }) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
      <Badge variant={plan.status === "active" ? "default" : "outline"}>
        {PLAN_STATUS_LABELS[plan.status]}
      </Badge>
      <span className="tabular-nums">#{plan.id}</span>
      <span className="tabular-nums">版本 {plan.version}</span>
      {plan.source_plan_id !== null && (
        <span className="tabular-nums">来源计划 #{plan.source_plan_id}</span>
      )}
      <span className="tabular-nums">创建 {plan.created_at}</span>
      {plan.confirmed_at !== null && (
        <span className="tabular-nums">启用 {plan.confirmed_at}</span>
      )}
      {plan.archived_at !== null && (
        <span className="tabular-nums">归档 {plan.archived_at}</span>
      )}
    </div>
  );
}

/** /plans 计划页：active、待确认 draft、历史版本与生成／调整／确认／拒绝 */
export default function PlansPage() {
  const queryClient = useQueryClient();
  const active = useQuery({ queryKey: ACTIVE_PLAN_KEY, queryFn: getActivePlan });
  const plans = useQuery({ queryKey: PLAN_LIST_KEY, queryFn: listPlans });

  const [request, setRequest] = useState("");
  const [progress, setProgress] = useState<AgentEventWire[]>([]);
  /**
   * 本次会话里产出或首次复用每个 draft 的那次运行的 conversation_id（draft_plan_id → UUID）。
   * 只记第一个值：同一 draft 被后续运行复用时，产出或首次复用它的那次运行的 checkpoint 才是等待中断所在。
   */
  const [runConversations, setRunConversations] = useState<Record<number, string>>(
    {},
  );

  const run = useMutation({
    mutationFn: (payload: { conversationId: string; request: string }) =>
      runAgentStream(
        { conversation_id: payload.conversationId, request: payload.request },
        (event) => {
          setProgress((current) => [...current, event]);
          const draftPlanId = draftPlanIdOf(event);
          if (draftPlanId !== null)
            setRunConversations((current) =>
              current[draftPlanId] === undefined
                ? { ...current, [draftPlanId]: payload.conversationId }
                : current,
            );
        },
      ),
    // 一次 Run 可能持久化了新 draft（或同类替换），因此结束时刷新计划版本；日历只在确认／拒绝后变化。
    onSettled: async () => {
      await queryClient.invalidateQueries({ queryKey: ["plans"] });
    },
    onError: (error) =>
      toast.error(error instanceof Error ? error.message : "本次运行失败"),
  });

  /**
   * 触发一次运行：每次生成／调整都是新的 UUID conversation_id（即后端 Checkpointer 的 thread_id）。
   * 输入为空时发送按钮自身的封闭短语；输入非空时原样发送用户文本（意图由后端 Router 判定）。
   */
  const startRun = (phrase: string) => {
    const nextConversationId = crypto.randomUUID();
    setProgress([]);
    const text = request.trim();
    run.mutate({
      conversationId: nextConversationId,
      request: text === "" ? phrase : text,
    });
  };

  /**
   * confirm／reject 的会话身份：优先用产出或首次复用该 draft 的那次运行的 conversation_id（即 §3.3 的 checkpoint
   * 优先）；页面刷新后加载到的既有 draft 没有本次会话的记录，此时新建一个，由后端按 §3.3 的唯一 draft
   * 兜底提交。
   */
  const draftConversationId = (plan: PlanWire) =>
    runConversations[plan.id] ?? crypto.randomUUID();

  const confirm = useMutation({
    mutationFn: (plan: PlanWire) =>
      confirmPlan({
        conversation_id: draftConversationId(plan),
        plan_id: plan.id,
      }),
    onSuccess: async (data) => {
      toast.success(`计划 #${data.plan.id} 已启用`);
      await invalidatePlanAndCalendarQueries(queryClient);
    },
    onError: (error) =>
      toast.error(error instanceof Error ? error.message : "确认计划失败"),
  });

  const reject = useMutation({
    mutationFn: (plan: PlanWire) =>
      rejectPlan({
        conversation_id: draftConversationId(plan),
        plan_id: plan.id,
      }),
    onSuccess: async (data) => {
      toast.success(`计划 #${data.plan.id} 已归档`);
      await invalidatePlanAndCalendarQueries(queryClient);
    },
    onError: (error) =>
      toast.error(error instanceof Error ? error.message : "拒绝计划失败"),
  });

  const versions = plans.data?.plans ?? [];
  const draft = versions.find((plan) => plan.status === "draft") ?? null;
  const history = versions
    .filter((plan) => plan.status === "archived" || plan.status === "rejected")
    .sort((a, b) => b.version - a.version);
  const busy = run.isPending || confirm.isPending || reject.isPending;

  return (
    <div className="mx-auto w-full max-w-3xl px-6 pb-10">
      <header className="pt-10 pb-6">
        <h2 className="font-display text-3xl font-light tracking-tight">
          训练计划
        </h2>
        <p className="mt-1 text-sm text-muted-foreground">
          生成或调整计划、确认启用或拒绝归档；计划内容与评估结果都由后端给出。
        </p>
      </header>

      <div className="flex flex-col gap-6">
        <Card>
          <CardHeader>
            <CardTitle>生成或调整计划</CardTitle>
            <CardDescription>
              输入留空时按钮发送自身的短语；输入非空时原样发送你写的请求，意图由后端判定
              （例如输入「查看进步」会得到后端现算的统计解释）。
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <Textarea
              value={request}
              onChange={(event) => setRequest(event.target.value)}
              placeholder="例如：生成计划／调整计划／查看进步"
              disabled={busy}
            />
            <div className="flex flex-wrap gap-2">
              <Button disabled={busy} onClick={() => startRun("生成计划")}>
                生成计划
              </Button>
              <Button
                variant="outline"
                disabled={busy}
                onClick={() => startRun("调整计划")}
              >
                调整计划
              </Button>
            </div>
          </CardContent>
        </Card>

        {progress.length > 0 && (
          <Card>
            <CardHeader>
              <CardTitle>本次运行进度</CardTitle>
              <CardDescription>
                节点、提示、等待确认、结束与错误都直接来自后端事件流。
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-1 text-sm">
              {progress.map((event, index) => (
                <p
                  key={index}
                  className={event.event === "error" ? "text-destructive" : ""}
                >
                  <span className="mr-2 text-xs text-muted-foreground">
                    {event.event}
                  </span>
                  {progressText(event)}
                </p>
              ))}
            </CardContent>
          </Card>
        )}

        {active.isPending && (
          <p className="text-sm text-muted-foreground">正在加载当前计划…</p>
        )}
        {active.isError && (
          <p className="text-sm text-destructive">
            加载当前计划失败：{active.error.message}，请刷新重试。
          </p>
        )}

        <Card>
          <CardHeader>
            <CardTitle>当前启用计划</CardTitle>
            <CardDescription>
              同一时刻至多一个 active；确认新计划时它会被归档。
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {active.data ? (
              active.data.plan === null ? (
                <p className="text-sm text-muted-foreground">
                  还没有启用的计划。
                </p>
              ) : (
                <>
                  <PlanMeta plan={active.data.plan} />
                  <JsonBlock value={active.data.plan.structured_content} />
                </>
              )
            ) : (
              <p className="text-xs text-muted-foreground">暂无数据。</p>
            )}
          </CardContent>
        </Card>

        {plans.isPending && (
          <p className="text-sm text-muted-foreground">正在加载计划版本…</p>
        )}
        {plans.isError && (
          <p className="text-sm text-destructive">
            加载计划版本失败：{plans.error.message}，请刷新重试。
          </p>
        )}

        {draft && (
          <Card>
            <CardHeader>
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="flex flex-col gap-1.5">
                  <CardTitle>待确认计划</CardTitle>
                  <CardDescription>
                    确认后启用为新 active；拒绝会归档该 draft，原计划保持不变。
                  </CardDescription>
                </div>
                <div className="flex gap-2">
                  <Button
                    disabled={busy}
                    onClick={() => confirm.mutate(draft)}
                  >
                    确认启用
                  </Button>
                  <Button
                    variant="outline"
                    disabled={busy}
                    onClick={() => reject.mutate(draft)}
                  >
                    拒绝
                  </Button>
                </div>
              </div>
            </CardHeader>
            <CardContent className="flex flex-col gap-3">
              <PlanMeta plan={draft} />
              <section className="flex flex-col gap-1.5">
                <h4 className="text-sm font-medium">结构化计划</h4>
                <JsonBlock value={draft.structured_content} />
              </section>
              <section className="flex flex-col gap-1.5">
                <h4 className="text-sm font-medium">Evaluator 摘要</h4>
                {draft.evaluator_result === null ? (
                  <p className="text-xs text-muted-foreground">
                    本次 draft 没有评估结果。
                  </p>
                ) : (
                  <JsonBlock value={draft.evaluator_result} />
                )}
              </section>
            </CardContent>
          </Card>
        )}

        <Card>
          <CardHeader>
            <CardTitle>历史版本</CardTitle>
            <CardDescription>
              已归档与评估未通过的版本；rejected 只表示 Evaluator
              二次阻断失败，不是用户拒绝。
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {history.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                还没有历史版本。
              </p>
            ) : (
              history.map((plan) => (
                <div
                  key={plan.id}
                  className="flex flex-col gap-2 rounded-md border border-border/60 p-3"
                >
                  <PlanMeta plan={plan} />
                  <JsonBlock value={plan.structured_content} />
                </div>
              ))
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
