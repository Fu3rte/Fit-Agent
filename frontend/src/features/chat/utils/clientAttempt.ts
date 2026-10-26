import type {
  ClientAttemptBody,
  ClientTerminalKindWire,
  ReActEvent,
} from "@/lib/contract";
import {
  ReActHttpError,
  ReActStreamError,
  failureOutcome,
} from "./reactAgent";

/** 一次发送传输尝试（Trace 契约 §6.2）：只存在于当前浏览器文档内存 */
export interface ClientAttempt {
  client_attempt_id: string;
  session_id: string;
  operation_id: string;
  run_id: string | null;
  started_at: number;
  first_text_ms: number | null;
  duration_ms: number | null;
  terminal_kind: ClientTerminalKindWire | null;
  /** 上报在途标记：同一尝试不并发提交，失败后可在既有查询节奏上重试 */
  reporting: boolean;
  /** 在途期间的补报触发：该请求以可重试失败结束时消费一次 */
  pending_report: boolean;
}

/** 发送门禁通过后、创建操作身份前计时；同一操作的不同传输尝试使用独立身份 */
export function startClientAttempt(
  sessionId: string,
  operationId: string,
  startedAt: number,
): ClientAttempt {
  return {
    client_attempt_id: crypto.randomUUID(),
    session_id: sessionId,
    operation_id: operationId,
    run_id: null,
    started_at: startedAt,
    first_text_ms: null,
    duration_ms: null,
    terminal_kind: null,
    reporting: false,
    pending_report: false,
  };
}

/** 首正文只记录一次：已通过业务投影的消息快照中的安全可见非空正文 */
export function observeFirstText(
  attempt: ClientAttempt,
  event: ReActEvent,
  now: number,
): void {
  if (attempt.first_text_ms !== null) return;
  if (
    event.event !== "message_start" &&
    event.event !== "message_update" &&
    event.event !== "message_end"
  )
    return;
  const visible = event.data.content.some(
    (block) => block.type === "text" && block.text.trim() !== "",
  );
  if (!visible) return;
  attempt.first_text_ms = now - attempt.started_at;
}

/** 一次尝试只结算一次；查询恢复等场景只确认可上报，不补造完成时长 */
export function settleClientAttempt(
  attempt: ClientAttempt,
  kind: ClientTerminalKindWire,
  now: number,
  measureDuration = true,
): boolean {
  if (attempt.terminal_kind !== null) return false;
  attempt.terminal_kind = kind;
  attempt.duration_ms = measureDuration ? now - attempt.started_at : null;
  return true;
}

/** 上报请求体：只包含已取得的枚举与测量，未知测量直接省略 */
export function clientAttemptBody(
  attempt: ClientAttempt,
): ClientAttemptBody | null {
  if (attempt.terminal_kind === null) return null;
  const body: ClientAttemptBody = {
    session_id: attempt.session_id,
    operation_id: attempt.operation_id,
    run_id: attempt.run_id,
    client_attempt_id: attempt.client_attempt_id,
    attempt_kind: "send",
    client_terminal_kind: attempt.terminal_kind,
  };
  if (attempt.duration_ms !== null)
    body.client_duration_ms = attempt.duration_ms;
  if (attempt.first_text_ms !== null)
    body.client_first_text_ms = attempt.first_text_ms;
  return body;
}

/** 传输失败分类（Trace 契约 §6.2）：主动中止由停止入口结算，被拒操作不生成样本 */
export function streamFailureAction(failure: unknown): {
  report: boolean;
  kind: ClientTerminalKindWire | null;
} {
  if (failure instanceof ReActStreamError)
    return failure.kind === "ended"
      ? { report: true, kind: "disconnected" }
      : { report: true, kind: "unknown" };
  if (failure instanceof DOMException && failure.name === "AbortError")
    return { report: false, kind: null };
  if (failure instanceof ReActHttpError)
    return failureOutcome(failure) === "rejected"
      ? { report: false, kind: null }
      : { report: true, kind: "disconnected" };
  if (failure instanceof TypeError)
    return { report: true, kind: "disconnected" };
  return { report: true, kind: "unknown" };
}

/** 永久拒收（Trace 契约 §6.3）：4xx 结束该次补报，run_not_terminal 等待既有查询确认 */
export function isPermanentAttemptRejection(failure: unknown): boolean {
  return (
    failure instanceof ReActHttpError &&
    failure.http_status !== null &&
    failure.http_status >= 400 &&
    failure.http_status < 500 &&
    failure.code !== "run_not_terminal"
  );
}
