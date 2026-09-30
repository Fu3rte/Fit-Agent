import { createParser } from "eventsource-parser";
import type { ReActEvent, ReActRunBody } from "@/lib/contract";
import type { ToolCallCardProps } from "../components/ToolCallCard";

export interface ReActRound {
  id: string;
  request: string;
  tools: (ToolCallCardProps & { id: string })[];
  text?: string;
  error?: string;
  status: "running" | "completed" | "failed";
}

export function applyReActEvent(round: ReActRound, event: ReActEvent): ReActRound {
  if (round.status !== "running") throw new Error("终止后收到事件");
  switch (event.event) {
    case "tool_start":
      if (round.tools.some((tool) => tool.id === event.data.tool_call_id)) throw new Error("重复工具调用");
      return { ...round, tools: [...round.tools, { id: event.data.tool_call_id, name: event.data.name, arguments: event.data.arguments, status: "running" }] };
    case "tool_result": {
      const tool = round.tools.find((item) => item.id === event.data.tool_call_id);
      if (!tool || tool.status !== "running") throw new Error("工具结果缺少调用");
      return { ...round, tools: round.tools.map((item) => item === tool ? { ...item, status: "completed", content: event.data.content } : item) };
    }
    case "message": return { ...round, text: event.data.text };
    case "done":
      if (round.tools.some((tool) => tool.status === "running") || round.text === undefined) throw new Error("运行未完成");
      return { ...round, status: "completed" };
    case "error": return failReActRound(round, event.data.message, event.data.tool_call_id);
  }
}

export function failReActRound(round: ReActRound, error: string, toolId?: string | null): ReActRound {
  return { ...round, status: "failed", error, tools: round.tools.map((tool) => tool.status === "running" && (toolId == null || tool.id === toolId) ? { ...tool, status: "failed", error } : tool) };
}

export function createReActParser(onEvent: (event: ReActEvent) => void) {
  let terminal = false;
  const parser = createParser({
    onError(error) { throw error; },
    onEvent(message) {
      if (terminal) throw new Error("终止后收到事件");
      const data = JSON.parse(message.data);
      if (data === null || typeof data !== "object" || Array.isArray(data)) throw new Error("事件数据无效");
      const string = (key: string) => typeof data[key] === "string";
      switch (message.event) {
        case "tool_start":
          if (!string("tool_call_id") || !string("name") || data.arguments === null || typeof data.arguments !== "object" || Array.isArray(data.arguments)) throw new Error("工具调用无效");
          break;
        case "tool_result":
          if (!string("tool_call_id") || !string("content")) throw new Error("工具结果无效");
          break;
        case "message": if (!string("text")) throw new Error("回答无效"); break;
        case "done": if (data.status !== "completed") throw new Error("终止状态无效"); terminal = true; break;
        case "error":
          if (!string("message") || !(data.tool_call_id === null || string("tool_call_id"))) throw new Error("错误事件无效");
          terminal = true;
          break;
        default: throw new Error("未知事件");
      }
      onEvent({ event: message.event, data } as ReActEvent);
    },
  });
  return {
    feed: (chunk: string) => parser.feed(chunk),
    finish() {
      if (!terminal) throw new Error("连接中断，未收到终止事件。");
    },
  };
}

export async function runReActStream(body: ReActRunBody, onEvent: (event: ReActEvent) => void, signal: AbortSignal, endpoint = "/api/agent/run"): Promise<void> {
  if (!body.request.trim() || Array.from(body.request).length > 32000) throw new Error("请输入 1 至 32000 字符。");
  const response = await fetch(endpoint, { method: "POST", headers: { "Content-Type": "application/json", Accept: "text/event-stream" }, body: JSON.stringify(body), signal });
  if (!response.ok) throw new Error(response.status === 409 ? "已有任务正在执行，请稍后发送。" : `请求失败（${response.status}）。`);
  if (!response.body || !response.headers.get("content-type")?.startsWith("text/event-stream")) throw new Error("响应格式无效。");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parser = createReActParser(onEvent);
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      parser.feed(decoder.decode(value, { stream: true }));
    }
    parser.feed(decoder.decode());
    parser.finish();
  } finally {
    await reader.cancel();
    reader.releaseLock();
  }
}
