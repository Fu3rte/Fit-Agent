import { createParser } from "eventsource-parser";
import type { ReActContent, ReActEvent, ReActRunBody, ReActStopReason, SteeringAccepted, SteeringBody, SteeringStatus } from "@/lib/contract";
import type { ToolCallCardProps } from "../components/ToolCallCard";

export type ReActEntry =
  | { kind: "user"; id: string; request?: string; steering?: SteeringStatus }
  | { kind: "assistant"; id: string; content: ReActContent[]; stop_reason?: ReActStopReason }
  | ({ kind: "tool"; id: string } & ToolCallCardProps);

export interface ReActRound {
  id: string;
  run_id?: string;
  entries: ReActEntry[];
  error?: string;
  unknown_steering?: string[];
  status: "running" | "completed" | "failed" | "cancelled";
}

export function finishReActRound(round: ReActRound, status: "failed" | "cancelled", error?: string, toolId?: string | null): ReActRound {
  if (round.status !== "running") return round;
  return {
    ...round, status, error,
    entries: round.entries.map((entry) => {
      if (entry.kind !== "tool" || entry.status !== "running") return entry;
      const failed = status === "failed" && (toolId == null || entry.id === toolId);
      return { ...entry, status: failed ? "failed" : "cancelled", ...(failed ? { error } : {}) };
    }),
  };
}

export function acceptSteering(round: ReActRound, accepted: SteeringAccepted, request: string): ReActRound {
  if (round.run_id !== accepted.run_id) throw new Error("Steering 运行身份不匹配。");
  const existing = round.entries.find((entry) => entry.id === accepted.steering_id);
  if (existing && (existing.kind !== "user" || existing.request !== undefined)) throw new Error("重复 Steering 接受记录。");
  return { ...round, entries: existing
    ? round.entries.map((entry) => entry === existing ? { ...entry, request } : entry)
    : [...round.entries, { kind: "user", id: accepted.steering_id, request }] };
}

export function applyReActEvent(round: ReActRound, event: ReActEvent): ReActRound {
  if (round.status !== "running") throw new Error("终止后收到事件。");
  if (round.run_id !== event.data.run_id) throw new Error("运行身份不匹配。");
  const entries = [...round.entries];
  const data = event.data;
  switch (event.event) {
    case "message_start":
      if (entries.some((entry) => entry.id === event.data.message_id) || entries.some((entry) => entry.kind === "assistant" && !entry.stop_reason)) throw new Error("重复或重叠的助手消息。");
      entries.push({ kind: "assistant", id: event.data.message_id, content: event.data.content });
      break;
    case "message_update":
    case "message_end": {
      const index = entries.findIndex((entry) => entry.id === event.data.message_id);
      const entry = entries[index];
      if (!entry || entry.kind !== "assistant" || entry.stop_reason) throw new Error("助手消息生命周期无效。");
      if (event.event === "message_update") {
        const { content_index, update_type, content } = event.data;
        const kind = update_type.split("_")[0];
        const block = content.find((item) => item.content_index === content_index);
        if (!block || block.type !== (kind === "toolcall" ? "tool_call" : kind)) throw new Error("内容块类型不匹配。");
        for (const old of entry.content) {
          const next = content.find((item) => item.content_index === old.content_index);
          if (!next || next.type !== old.type || (old.type === "tool_call" && next.type === "tool_call" && next.tool_call_id !== old.tool_call_id)) throw new Error("内容块身份变化。");
        }
      } else {
        const reason = event.data.stop_reason;
        if ((reason === "length" || reason === "aborted") && event.data.content.some((block) => block.type === "tool_call")) throw new Error("截断或取消消息包含工具调用。");
      }
      entries[index] = { ...entry, content: event.data.content, ...(event.event === "message_end" ? { stop_reason: event.data.stop_reason } : {}) };
      break;
    }
    case "tool_start": {
      const { tool_call_id, name, arguments: args } = event.data;
      if (entries.some((entry) => entry.id === tool_call_id)) throw new Error("重复工具执行。");
      const assistant = entries.filter((entry) => entry.kind === "assistant").at(-1);
      if (assistant?.kind !== "assistant" || assistant.stop_reason !== "toolUse" || !assistant.content.some((block) => block.type === "tool_call" && block.tool_call_id === tool_call_id && block.name === name)) throw new Error("工具执行缺少最终模型调用。");
      entries.push({ kind: "tool", id: tool_call_id, name, arguments: args, status: "running" });
      break;
    }
    case "tool_result": {
      const index = entries.findIndex((entry) => entry.id === event.data.tool_call_id);
      const entry = entries[index];
      if (!entry || entry.kind !== "tool" || entry.status !== "running") throw new Error("工具结果缺少调用或重复返回。");
      entries[index] = { ...entry, content: event.data.content, status: event.data.is_error ? "failed" : "completed" };
      break;
    }
    case "steering_status": {
      const index = entries.findIndex((entry) => entry.id === event.data.steering_id);
      const entry = entries[index];
      const steering: SteeringStatus = event.data.status === "consumed" ? { status: "consumed" } : { status: "discarded", reason: event.data.reason };
      if (entry && (entry.kind !== "user" || entry.steering)) throw new Error("重复或无效 Steering 状态。");
      if (entry?.kind === "user") entries[index] = { ...entry, steering };
      else entries.push({ kind: "user", id: event.data.steering_id, steering });
      break;
    }
    case "done": {
      const assistants = entries.filter((entry) => entry.kind === "assistant");
      if (!assistants.length || assistants.some((entry) => !entry.stop_reason) || assistants.at(-1)?.stop_reason !== event.data.stop_reason || entries.some((entry) => entry.kind === "tool" && entry.status === "running") || entries.some((entry) => entry.kind === "user" && entry.id !== round.id && !entry.steering)) throw new Error("运行未完成。");
      return { ...round, status: "completed" };
    }
    case "error":
      if (event.data.tool_call_id !== null && !entries.some((entry) => entry.kind === "tool" && entry.id === event.data.tool_call_id && entry.status === "running")) throw new Error("错误关联工具无效。");
      return finishReActRound(round, event.data.status, event.data.message, event.data.tool_call_id);
  }
  return { ...round, entries, run_id: data.run_id };
}

function object(value: unknown): value is Record<string, unknown> { return value !== null && typeof value === "object" && !Array.isArray(value); }
const uuid = (value: unknown): value is string => typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
const stopReasons = ["stop", "toolUse", "length", "error", "aborted"];
const updateTypes = ["text_start", "text_delta", "text_end", "thinking_start", "thinking_delta", "thinking_end", "toolcall_start", "toolcall_delta", "toolcall_end"];

function validateContent(value: unknown): void {
  if (!Array.isArray(value)) throw new Error("消息内容无效。");
  let previous = -1;
  for (const block of value) {
    if (!object(block) || !Number.isSafeInteger(block.content_index) || (block.content_index as number) <= previous) throw new Error("内容块索引无效。");
    previous = block.content_index as number;
    const keys = ["content_index", "type"];
    if (block.type === "text" && typeof block.text === "string") keys.push("text");
    else if (block.type === "thinking" && typeof block.thinking === "string") keys.push("thinking");
    else if (block.type === "tool_call" && typeof block.tool_call_id === "string" && block.tool_call_id && typeof block.name === "string" && block.name && object(block.arguments)) keys.push("tool_call_id", "name", "arguments");
    else throw new Error("内容块字段无效。");
    if (Object.keys(block).some((key) => !keys.includes(key))) throw new Error("未知内容块字段。");
  }
}

export function createReActParser(onEvent: (event: ReActEvent) => void, runId: string) {
  if (!uuid(runId)) throw new Error("运行身份无效。");
  let terminal = false;
  const parser = createParser({
    onError(error) { throw error; },
    onEvent(message) {
      if (terminal) throw new Error("终止后收到事件。");
      const data: unknown = JSON.parse(message.data);
      if (!object(data) || data.run_id !== runId) throw new Error("事件运行身份无效。");
      const string = (key: string) => typeof data[key] === "string";
      switch (message.event) {
        case "message_start":
        case "message_update":
        case "message_end":
          if (!uuid(data.message_id)) throw new Error("消息身份无效。");
          validateContent(data.content);
          if (message.event === "message_update" && (!Number.isSafeInteger(data.content_index) || !updateTypes.includes(data.update_type as string) || !(data.content as ReActContent[]).some((block) => block.content_index === data.content_index))) throw new Error("消息更新无效。");
          if (message.event === "message_end" && !stopReasons.includes(data.stop_reason as string)) throw new Error("消息结束原因无效。");
          break;
        case "tool_start":
          if (!string("tool_call_id") || !data.tool_call_id || !string("name") || !data.name || !object(data.arguments)) throw new Error("工具调用无效。");
          break;
        case "tool_result":
          if (!string("tool_call_id") || !string("content") || typeof data.is_error !== "boolean") throw new Error("工具结果无效。");
          break;
        case "steering_status":
          if (!uuid(data.steering_id) || !(data.status === "consumed" || (data.status === "discarded" && ["completed", "run_failed", "cancelled"].includes(data.reason as string)))) throw new Error("Steering 状态无效。");
          break;
        case "done":
          if (data.status !== "completed" || !["stop", "length"].includes(data.stop_reason as string)) throw new Error("终止状态无效。");
          terminal = true;
          break;
        case "error":
          if (!string("message") || !(data.tool_call_id === null || string("tool_call_id")) || !((data.status === "failed" && data.code === "execution_failed") || (data.status === "cancelled" && data.code === "cancelled"))) throw new Error("错误事件无效。");
          terminal = true;
          break;
        default: throw new Error("未知事件。");
      }
      onEvent({ event: message.event, data } as ReActEvent);
    },
  });
  return {
    feed: (chunk: string) => parser.feed(chunk),
    finish() { if (!terminal) throw new Error("连接中断，未收到终止事件。"); },
    get terminal() { return terminal; },
  };
}

export function validChatInput(text: string): boolean { return !!text.trim() && Array.from(text).length <= 32000; }

export class ReActHttpError extends Error {}

export function markUnknownSteering(round: ReActRound, request: string): ReActRound {
  return { ...round, unknown_steering: [...(round.unknown_steering ?? []), request] };
}

export function canSubmitChatInput(text: string, unknownRequests: string[]): boolean {
  return validChatInput(text) && !unknownRequests.includes(text);
}

async function checkResponse(response: Response): Promise<void> {
  if (response.ok) return;
  const body: unknown = await response.json();
  if (!object(body) || !object(body.detail) || typeof body.detail.code !== "string" || typeof body.detail.message !== "string") throw new Error("错误响应格式无效。");
  throw new ReActHttpError(body.detail.message);
}

export async function submitSteering(runId: string, body: SteeringBody, signal: AbortSignal): Promise<SteeringAccepted> {
  if (!uuid(runId) || !validChatInput(body.message)) throw new Error("Steering 请求无效。");
  const response = await fetch(`/api/agent/runs/${runId}/steering`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal });
  await checkResponse(response);
  const data: unknown = await response.json();
  if (!object(data) || data.run_id !== runId || !uuid(data.steering_id) || data.status !== "accepted") throw new Error("Steering 接受响应无效。");
  return data as unknown as SteeringAccepted;
}

export async function runReActStream(body: ReActRunBody, onEvent: (event: ReActEvent) => void, signal: AbortSignal, onOpen: (runId: string) => void, endpoint = "/api/agent/run"): Promise<void> {
  if (!validChatInput(body.request)) throw new Error("消息必须包含 1 至 32000 个字符。");
  const response = await fetch(endpoint, { method: "POST", headers: { "Content-Type": "application/json", Accept: "text/event-stream" }, body: JSON.stringify(body), signal });
  await checkResponse(response);
  if (!response.body) throw new Error("响应缺少流。");
  const reader = response.body.getReader();
  try {
    if (!response.headers.get("content-type")?.startsWith("text/event-stream")) throw new Error("响应格式无效。");
    const runId = response.headers.get("X-Run-ID");
    if (!uuid(runId)) throw new Error("响应运行身份无效。");
    const decoder = new TextDecoder("utf-8", { fatal: true });
    const parser = createReActParser(onEvent, runId);
    onOpen(runId);
    while (!parser.terminal) {
      const { done, value } = await reader.read();
      if (done) break;
      parser.feed(decoder.decode(value, { stream: true }));
    }
    if (!parser.terminal) parser.feed(decoder.decode());
    parser.finish();
  } finally {
    try { await reader.cancel(); } finally { reader.releaseLock(); }
  }
}
