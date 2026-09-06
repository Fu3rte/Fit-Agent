/**
 * SSE 订阅（A2：原生 EventSource，GET + Last-Event-ID 补读）。
 * 页面级单例：EventSource 全页只建一次；断线由浏览器自动重连并携带
 * Last-Event-ID 头，mock/后端按 id 补读。
 */
import { useEffect, useRef } from "react";
import { createEventSource } from "@/lib/api";
import type { SseEvent } from "@/lib/contract";

type Listener = (event: SseEvent) => void;

const EVENT_NAMES = [
  "run.started",
  "message.delta",
  "draft.proposed",
  "context.compacted",
  "run.completed",
  "run.cancelled",
  "run.failed",
  "error",
] as const;

let source: EventSource | null = null;
const listeners = new Set<Listener>();

function ensureSource(): void {
  if (source) return;
  source = createEventSource();
  for (const name of EVENT_NAMES) {
    source.addEventListener(name, (e) => {
      const message = e as MessageEvent<string>;
      // 原生连接错误事件没有 data：忽略，EventSource 会自动重连
      if (name === "error" && message.data === undefined) return;
      let data: object;
      try {
        data = JSON.parse(message.data) as object;
      } catch {
        return;
      }
      const event = { event: name, ...data } as SseEvent;
      for (const listener of listeners) listener(event);
    });
  }
}

/** 订阅事件流；listener 经 ref 转发，避免随渲染重建订阅 */
export function useChatEvents(onEvent: Listener): void {
  const ref = useRef(onEvent);
  useEffect(() => {
    ref.current = onEvent;
  }, [onEvent]);
  useEffect(() => {
    ensureSource();
    const listener: Listener = (event) => ref.current(event);
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  }, []);
}
