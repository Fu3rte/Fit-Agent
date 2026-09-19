import { useState } from "react";
import {
  useMutation,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";
import { toast } from "sonner";
import ChatComposer from "@/features/chat/components/ChatComposer";
import ChatTranscript from "@/features/chat/components/ChatTranscript";
import ConfirmedCard from "@/features/chat/components/ConfirmedCard";
import PlanWaitingCard from "@/features/chat/components/PlanWaitingCard";
import WorkoutConfirmCard from "@/features/chat/components/WorkoutConfirmCard";
import { MessageScrollerItem } from "@/components/ui/message-scroller";
import { confirmPlan, rejectPlan, runAgentStream } from "@/lib/api";
import type {
  AgentEventWire,
  ConfirmWorkoutResponseWire,
} from "@/lib/contract";
import type {
  ChatRound,
  PlanDraft,
  WorkoutDraft,
} from "@/features/chat/utils/chatRound";

/** 计划确认／拒绝成功后失效计划与日历相关 Query（沿用 ``PlansPage.tsx`` 的既有口径） */
async function invalidatePlanAndCalendarQueries(
  queryClient: QueryClient,
): Promise<void> {
  await queryClient.invalidateQueries({ queryKey: ["plans"] });
  await queryClient.invalidateQueries({ queryKey: ["calendar"] });
}

/** /chat 对话页：自然语言打卡确认与计划生成／调整入口 */
export default function ChatPage() {
  const queryClient = useQueryClient();
  const [rounds, setRounds] = useState<ChatRound[]>([]);
  const [workoutDraft, setWorkoutDraft] = useState<WorkoutDraft | null>(null);
  const [planDraft, setPlanDraft] = useState<PlanDraft | null>(null);
  const [confirmed, setConfirmed] = useState<{
    conversation_id: string;
    data: ConfirmWorkoutResponseWire;
  } | null>(null);

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
  const send = (text: string) => {
    const conversationId = crypto.randomUUID();
    setRounds((current) => [
      ...current,
      { conversation_id: conversationId, request: text, events: [] },
    ]);
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

  return (
    <div className="relative flex h-full w-full flex-col overflow-hidden">
      <div className="flex h-full w-full min-h-0 flex-1 flex-col">
        <ChatTranscript rounds={rounds}>
          {planDraft && (
            <MessageScrollerItem messageId={`plan-wait:${planDraft.plan_id}`}>
              <PlanWaitingCard
                planId={planDraft.plan_id}
                busy={confirm.isPending || reject.isPending}
                onConfirm={() => confirm.mutate(planDraft)}
                onReject={() => reject.mutate(planDraft)}
              />
            </MessageScrollerItem>
          )}

          {workoutDraft && (
            <MessageScrollerItem
              key={workoutDraft.conversation_id}
              messageId={`workout-wait:${workoutDraft.conversation_id}`}
            >
              <WorkoutConfirmCard
                draft={workoutDraft}
                onConfirmed={(data) => {
                  setConfirmed({
                    conversation_id: workoutDraft.conversation_id,
                    data,
                  });
                  setWorkoutDraft(null);
                }}
                onCancel={() => setWorkoutDraft(null)}
              />
            </MessageScrollerItem>
          )}

          {confirmed && (
            <MessageScrollerItem
              messageId={`confirmed:${confirmed.conversation_id}`}
            >
              <ConfirmedCard
                session={confirmed.data.workout_session}
                bests={confirmed.data.personal_bests}
              />
            </MessageScrollerItem>
          )}
        </ChatTranscript>
      </div>

      <ChatComposer busy={run.isPending} onSend={send} />
    </div>
  );
}
