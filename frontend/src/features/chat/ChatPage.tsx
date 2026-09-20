import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  useMutation,
  useQuery,
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
import {
  confirmPlan,
  createConversation,
  readConversation,
  rejectPlan,
  runAgentStream,
} from "@/lib/api";
import type {
  AgentEventWire,
  ConfirmWorkoutResponseWire,
} from "@/lib/contract";
import {
  conversationTitle,
  detailToRounds,
  mergeRounds,
  mergeWaitingDrafts,
  waitingDrafts,
} from "@/features/chat/utils/conversationHistory";
import type {
  ChatRound,
  PlanDraft,
  WorkoutDraft,
} from "@/features/chat/utils/chatRound";

/** 本页在途的一轮：带所属会话身份，切换会话时既不属于当前会话的在途轮次不显示 */
interface PendingRound extends ChatRound {
  chat_id: string;
}

/** 计划确认／拒绝成功后失效计划与日历相关 Query（沿用 ``PlansPage.tsx`` 的既有口径） */
async function invalidatePlanAndCalendarQueries(
  queryClient: QueryClient,
): Promise<void> {
  await queryClient.invalidateQueries({ queryKey: ["plans"] });
  await queryClient.invalidateQueries({ queryKey: ["calendar"] });
}

/** 会话历史失效：列表与详情的唯一收敛点（前缀匹配同时覆盖当前会话与其它会话） */
async function invalidateConversationQueries(
  queryClient: QueryClient,
): Promise<void> {
  await queryClient.invalidateQueries({ queryKey: ["conversations"] });
  await queryClient.invalidateQueries({ queryKey: ["conversation"] });
}

/**
 * 对话页：根路径是尚未建会话的新会话状态，``/chat/:chatId`` 用服务端详情恢复历史。
 *
 * 每次发送创建一轮（新的 LangGraph thread ＋ 新的幂等键），SSE 事件先进本页在途状态，
 * 运行结束后失效详情 Query，页面临时状态随即被服务端投影替换。
 */
export default function ChatPage() {
  const { chatId } = useParams<{ chatId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const activeChatId = useRef(chatId);
  activeChatId.current = chatId;
  const [pending, setPending] = useState<PendingRound[]>([]);
  const [workoutDraft, setWorkoutDraft] = useState<WorkoutDraft | null>(null);
  const [planDraft, setPlanDraft] = useState<PlanDraft | null>(null);
  const [confirmed, setConfirmed] = useState<{
    conversation_id: string;
    data: ConfirmWorkoutResponseWire;
  } | null>(null);

  const detail = useQuery({
    queryKey: ["conversation", chatId],
    queryFn: () => readConversation(chatId as string),
    enabled: chatId !== undefined,
  });

  const serverRounds =
    detail.data === undefined ? [] : detailToRounds(detail.data);

  const rounds = mergeRounds(
    serverRounds,
    chatId === undefined
      ? []
      : pending.filter((round) => round.chat_id === chatId),
  );

  // 切换会话丢弃上一会话的确认卡与即时反馈；``confirmed`` 不由历史重建
  useEffect(() => {
    setPlanDraft(null);
    setWorkoutDraft(null);
    setConfirmed(null);
  }, [chatId]);

  /** 事件进本轮的展示列表；`waiting` 按载荷判别路径，分别驱动两个确认 UI */
  const onEvent = (
    targetChatId: string,
    threadId: string,
    event: AgentEventWire,
  ) => {
    setPending((current) =>
      current.map((round) =>
        round.chat_id === targetChatId && round.conversation_id === threadId
          ? { ...round, events: [...round.events, event] }
          : round,
      ),
    );
    if (event.event !== "waiting" || activeChatId.current !== targetChatId)
      return;
    if ("draft_plan_id" in event.data) {
      setPlanDraft({
        chat_id: targetChatId,
        conversation_id: threadId,
        plan_id: event.data.draft_plan_id,
      });
    } else {
      setWorkoutDraft({
        chat_id: targetChatId,
        conversation_id: threadId,
        workout: event.data.workout,
        candidates: event.data.candidate_plan_sessions,
      });
    }
  };

  const run = useMutation({
    mutationFn: async (payload: {
      text: string;
      threadId: string;
      clientRequestId: string;
    }) => {
      // 新会话状态：标题取首条用户消息，落库后跳转到该会话的地址
      const targetChatId =
        chatId ??
        (await createConversation({ title: conversationTitle(payload.text) }))
          .id;
      setPending((current) => [
        ...current,
        {
          chat_id: targetChatId,
          conversation_id: payload.threadId,
          request: payload.text,
          events: [],
          assistants: [],
          confirmations: [],
          run_status: null,
        },
      ]);
      if (chatId === undefined) {
        activeChatId.current = targetChatId;
        await queryClient.invalidateQueries({ queryKey: ["conversations"] });
        navigate(`/chat/${targetChatId}`);
      }
      await runAgentStream(
        {
          chat_id: targetChatId,
          conversation_id: payload.threadId,
          client_request_id: payload.clientRequestId,
          request: payload.text,
        },
        (event) => onEvent(targetChatId, payload.threadId, event),
      );
    },
    onError: (error) =>
      toast.error(error instanceof Error ? error.message : "本次运行失败"),
    onSettled: () => invalidateConversationQueries(queryClient),
  });

  // 空投影不抹本地 waiting；已确认不复活
  useEffect(() => {
    if (chatId === undefined || detail.data === undefined || run.isPending) {
      return;
    }
    const restored = waitingDrafts(serverRounds, chatId);
    setPlanDraft((current) =>
      mergeWaitingDrafts(current, restored.plan, serverRounds),
    );
    setWorkoutDraft((current) =>
      mergeWaitingDrafts(current, restored.workout, serverRounds),
    );
  }, [chatId, detail.data, run.isPending]);

  /** 发送一轮：本轮 thread 与幂等键都是一次性 UUID，请求文本原样发送 */
  const send = (text: string) => {
    if (text.trim() === "") return;
    run.mutate({
      text,
      threadId: crypto.randomUUID(),
      clientRequestId: crypto.randomUUID(),
    });
  };

  const confirm = useMutation({
    mutationFn: (draft: PlanDraft) =>
      confirmPlan({
        chat_id: draft.chat_id,
        conversation_id: draft.conversation_id,
        plan_id: draft.plan_id,
      }),
    onSuccess: async (data) => {
      toast.success(`计划 #${data.plan.id} 已启用`);
      setPlanDraft(null);
      await invalidatePlanAndCalendarQueries(queryClient);
      await invalidateConversationQueries(queryClient);
    },
    onError: (error) =>
      toast.error(error instanceof Error ? error.message : "确认计划失败"),
  });

  const reject = useMutation({
    mutationFn: (draft: PlanDraft) =>
      rejectPlan({
        chat_id: draft.chat_id,
        conversation_id: draft.conversation_id,
        plan_id: draft.plan_id,
      }),
    onSuccess: async (data) => {
      toast.success(`计划 #${data.plan.id} 已归档`);
      setPlanDraft(null);
      await invalidatePlanAndCalendarQueries(queryClient);
      await invalidateConversationQueries(queryClient);
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
