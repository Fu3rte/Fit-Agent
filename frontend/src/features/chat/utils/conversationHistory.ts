import type {
  AgentEventWire,
  ConversationDetailWire,
} from "@/lib/contract";
import type {
  ChatRound,
  PlanDraft,
  WorkoutDraft,
} from "@/features/chat/utils/chatRound";

/** 标题截断上限：按 Unicode 码点计数，中文标题不会在中途截断成半个字符 */
const TITLE_CODE_POINTS = 30;

/**
 * 首条用户消息 → 会话标题；发送前已保证去空白后非空，此处只负责确定性截断。
 */
export function conversationTitle(text: string): string {
  return Array.from(text.trim()).slice(0, TITLE_CODE_POINTS).join("");
}

/** 服务端轮次 → 消息列的轮次；Event 去掉 ``sequence`` 后与 SSE 事件逐字同形 */
export function detailToRounds(detail: ConversationDetailWire): ChatRound[] {
  return detail.rounds.map((round) => ({
    conversation_id: round.conversation_id,
    request: round.request,
    events: round.events.map((event) => {
      const { sequence: _sequence, ...wire } = event;
      return wire as AgentEventWire;
    }),
    assistants: round.assistants,
    confirmations: round.confirmations,
    run_status: round.status,
  }));
}

/**
 * 服务端详情与页面在途轮次合并：服务端已有同一 thread 的轮次时以服务端投影为准，
 * 在途轮次只保留还没落库的那些（详情失效重取后自动收敛）。
 */
export function mergeRounds(
  server: ChatRound[],
  pending: ChatRound[],
): ChatRound[] {
  const known = new Set(server.map((round) => round.conversation_id));
  return [
    ...server,
    ...pending.filter((round) => !known.has(round.conversation_id)),
  ];
}

/**
 * 历史里仍待用户处理的 ``waiting``：最后一个未被确认的等待决定当前确认卡片。
 *
 * 已提交确认投影的轮次不再恢复卡片；``confirmed`` 卡片本身是当次会话的即时反馈，不从历史重建。
 */
export function waitingDrafts(
  rounds: ChatRound[],
  chatId: string,
): {
  plan: PlanDraft | null;
  workout: WorkoutDraft | null;
} {
  let plan: PlanDraft | null = null;
  let workout: WorkoutDraft | null = null;
  for (const round of rounds) {
    if (round.confirmations.length > 0) continue;
    for (const event of round.events) {
      if (event.event !== "waiting") continue;
      if ("draft_plan_id" in event.data) {
        plan = {
          chat_id: chatId,
          conversation_id: round.conversation_id,
          plan_id: event.data.draft_plan_id,
        };
        workout = null;
        continue;
      }
      workout = {
        chat_id: chatId,
        conversation_id: round.conversation_id,
        workout: event.data.workout,
        candidates: event.data.candidate_plan_sessions,
      };
      plan = null;
    }
  }
  return { plan, workout };
}

/** 空投影不抹本地 waiting；该轮已有确认投影时才清掉 */
export function mergeWaitingDrafts<
  T extends { conversation_id: string } | null,
>(local: T, restored: T, serverRounds: ChatRound[]): T {
  if (restored !== null) return restored;
  if (local === null) return local;
  return serverRounds.some(
    (round) =>
      round.conversation_id === local.conversation_id &&
      round.confirmations.length > 0,
  )
    ? restored
    : local;
}
