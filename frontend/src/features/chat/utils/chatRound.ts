/**
 * 对话页内存模型与事件可读行（无 React、无 DOM，边界见 stage6.md §2.4.3／§2.5.2）。
 */
import type {
  AgentEventWire,
  ConfirmWorkoutDraftWire,
  PlanSessionCandidateWire,
} from "@/lib/contract";

/** 本页内存里的一轮对话（每次运行一条 UUID，即后端 Checkpointer 的 thread_id） */
export interface ChatRound {
  conversation_id: string;
  request: string;
  events: AgentEventWire[];
}

/** 打卡路径待确认载荷：``waiting`` 的两个字段 ＋ 产出它的那次运行的 conversation_id */
export interface WorkoutDraft {
  conversation_id: string;
  workout: ConfirmWorkoutDraftWire;
  candidates: PlanSessionCandidateWire[];
}

/** 计划路径待确认载荷：``waiting.draft_plan_id`` ＋ 产出它的那次运行的 conversation_id */
export interface PlanDraft {
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
