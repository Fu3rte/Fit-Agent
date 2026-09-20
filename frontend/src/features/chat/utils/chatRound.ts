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
