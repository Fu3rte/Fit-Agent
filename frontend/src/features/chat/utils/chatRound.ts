import type {
  AgentEventWire,
  ConfirmWorkoutDraftWire,
  ConversationAssistantWire,
  ConversationConfirmationWire,
  ConversationRunStatusWire,
  PlanSessionCandidateWire,
} from "@/lib/contract";

/**
 * 消息列里的一轮对话：``conversation_id`` 是该轮的 LangGraph thread 身份。
 *
 * ``assistants``／``confirmations``／``run_status`` 只在服务端详情投影里存在，页面在途的一轮
 * 这三项分别是空数组与 null，落库后由服务端详情替换。
 */
export interface ChatRound {
  conversation_id: string;
  request: string;
  events: AgentEventWire[];
  /** 服务端投影的 Assistant 文本与状态（含失败／部分／中止） */
  assistants: ConversationAssistantWire[];
  /** 该轮已提交的确认投影：非空即这个等待已被用户处理过 */
  confirmations: ConversationConfirmationWire[];
  /** 服务端 Run 状态；页面在途轮次为 null */
  run_status: ConversationRunStatusWire | null;
}

/** 打卡路径待确认载荷：``waiting`` 的两个字段 ＋ 产出它的那次运行的会话身份 */
export interface WorkoutDraft {
  chat_id: string;
  conversation_id: string;
  workout: ConfirmWorkoutDraftWire;
  candidates: PlanSessionCandidateWire[];
}

/** 计划路径待确认载荷：``waiting.draft_plan_id`` ＋ 产出它的那次运行的会话身份 */
export interface PlanDraft {
  chat_id: string;
  conversation_id: string;
  plan_id: number;
}

/** 一件事件的可读行；``message`` 的可见文本原样渲染，不解析 */
export function eventText(event: AgentEventWire): string {
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
      return `结束：intent ${event.data.intent ?? "无"}，终止原因 ${event.data.termination_reason ?? "无"
        }`;
    case "error":
      return `错误：${event.data.message}`;
  }
}

/** 未完成的 Assistant 状态与 Run 状态的固定文案（失败／部分／中止都只用于展示） */
const INCOMPLETE_LABEL: Record<string, string> = {
  partial: "输出未完成",
  failed: "本次运行失败",
  aborted: "本次运行被中断",
  cancelled: "本次运行被中断",
};

/**
 * 一轮的失败／中断提示：优先用已提交 ``error`` Event 的可见文本（后端已脱敏），没有 error Event 时
 * 按 Assistant 投影状态或 Run 状态取固定文案；失败提示缺失（正常轮次）返回 undefined。
 */
export function interruptedNotice(round: ChatRound): string | undefined {
  const errorEvent = round.events.find((event) => event.event === "error");
  if (errorEvent?.event === "error") return errorEvent.data.message;
  const incomplete = round.assistants.find(
    (assistant) => assistant.status !== "complete",
  );
  const status = incomplete?.status ?? round.run_status;
  return status !== null && status !== undefined && status in INCOMPLETE_LABEL
    ? INCOMPLETE_LABEL[status]
    : undefined;
}
