/**
 * Run 级 SSE 订阅治理（F0-04A + F6-02a 按 Run 订阅）：
 * 单例 EventSource + 最后接收时间 + 45 秒无事件计时器；订阅目标是
 * `GET /api/runs/{run_id}/events`（不是全局 /api/events）。
 *
 * 正本规则（08 8.7 断线、刷新与心跳规则）：
 * - 规则 4：收到业务事件或 heartbeat 均更新最后接收时间；
 * - 规则 5：连续 45 秒未收到事件、或触发原生 error → 主动 close() 并转查询，
 *   不依赖浏览器自动重连（避免形成第二套恢复路径）、不使用 Last-Event-ID 补读；
 * - 规则 7：页面恢复可见时立即复查连接（后台标签页计时器会被节流）；终态或页面卸载
 *   时关闭连接、清理计时器。
 *
 * 本 hook 只负责「发现连接不可用 → 关闭 + 通知」；查询、轮询、「连接中断，正在恢复」
 * 提示与草稿卡恢复由调用方经 onConnectionLost 承接（F0-04B），这里不做自动重连。
 * runId 为 null 时不建连（无进行中 Run / 恢复周期内刻意不接回 SSE）。
 */
import { useEffect, useRef } from "react";
import { createRunEventsSource } from "@/lib/api";
import type { SseEvent } from "@/lib/contract";

type Listener = (event: SseEvent) => void;

/** 连接不可用原因：45s 无事件 / 原生 error / 恢复可见时复查发现已关闭或已过期 */
export type ConnectionLostReason = "timeout" | "error" | "stale";

type ConnectionListener = (reason: ConnectionLostReason) => void;

export interface UseChatEventsOptions {
  /**
   * 连接中断回调（08 8.7 规则 1/5/7）：仅通知，不自动重连，调用方据此转查询恢复。
   * 不传时连接治理照常执行（close + 计时器清理），只是无人接收通知。
   */
  onConnectionLost?: (reason: ConnectionLostReason) => void;
}

/** 无事件判死阈值（08 8.7 规则 5；heartbeat 每 15 秒一次，3 个周期无事件即断线） */
const NO_EVENT_TIMEOUT_MS = 45_000;

/** 契约 v1 事件全集（contract.ts SseEvent；backend EVENT_KINDS，08 8.7） */
const EVENT_NAMES = [
  "status",
  "answer",
  "rationale",
  "draft",
  "compression",
  "heartbeat",
] as const;

let source: EventSource | null = null;
/** 当前订阅的 Run；null = 未订阅 */
let sourceRunId: string | null = null;
/** 最后一次收到任意事件（含 heartbeat）的时刻，用于过期判定（08 8.7 规则 4） */
let lastReceivedAt = 0;
/** 45 秒无事件计时器：每个事件到达即重置，关闭/清理时必被清除，不跨重订阅存活 */
let watchdog: ReturnType<typeof setTimeout> | null = null;

const listeners = new Set<Listener>();
const connectionListeners = new Set<ConnectionListener>();

function clearWatchdog(): void {
  if (watchdog === null) return;
  clearTimeout(watchdog);
  watchdog = null;
}

/** 收到事件（含 heartbeat）：更新最后接收时间并重新计时（08 8.7 规则 4/5） */
function touch(): void {
  lastReceivedAt = Date.now();
  clearWatchdog();
  watchdog = setTimeout(() => {
    watchdog = null;
    dropConnection("timeout");
  }, NO_EVENT_TIMEOUT_MS);
}

function closeSource(): void {
  if (!source) return;
  source.close();
  source = null;
  sourceRunId = null;
}

/** 关闭连接并通知调用方转查询；不重连（08 8.7 规则 5） */
function dropConnection(reason: ConnectionLostReason): void {
  clearWatchdog();
  closeSource();
  // 快照遍历：回调内可能退订，不能边遍历活动 Set 边调用
  for (const listener of [...connectionListeners]) listener(reason);
}

/**
 * 页面恢复可见时立即复查（08 8.7 规则 7）：后台标签页的 setTimeout 被节流，返回前台
 * 可能早已超过 45 秒；连接已关闭、已被浏览器置为 CLOSED、或时间戳过期都按断线处理。
 */
function handleVisibilityChange(): void {
  if (document.visibilityState !== "visible") return;
  const stale = Date.now() - lastReceivedAt >= NO_EVENT_TIMEOUT_MS;
  const gone = !source || source.readyState === EventSource.CLOSED;
  if (gone || stale) dropConnection("stale");
}

/** 建连（或换 Run 重连）：同一 runId 复用现有连接；不同 runId 先关旧再开新 */
function ensureSource(runId: string): void {
  if (source && sourceRunId === runId) return;
  if (source) closeSource();
  sourceRunId = runId;
  source = createRunEventsSource(runId);
  for (const name of EVENT_NAMES) {
    source.addEventListener(name, (e) => {
      const message = e as MessageEvent<string>;
      // 活性判定看「收到事件」，与载荷能否解析无关（heartbeat 按契约无载荷）
      touch();
      let data: object = {};
      if (message.data) {
        try {
          data = JSON.parse(message.data) as object;
        } catch {
          return;
        }
      }
      const event = { event: name, ...data } as SseEvent;
      for (const listener of listeners) listener(event);
    });
  }
  // 原生 error（含浏览器准备自动重连前的那一次）立即关闭转查询（08 8.7 规则 5）
  source.addEventListener("error", () => {
    dropConnection("error");
  });
  document.addEventListener("visibilitychange", handleVisibilityChange);
  // 建连即开始计时：连不上、或连上后服务端静默，都会在 45 秒内被发现
  touch();
}

/** 关闭连接 + 清理全部计时器与可见性监听；不通知（清理不是断线事件） */
function teardown(): void {
  clearWatchdog();
  closeSource();
  document.removeEventListener("visibilitychange", handleVisibilityChange);
  lastReceivedAt = 0;
}

/**
 * 订阅一个 Run 的事件流（runId=null 不建连）；两个回调均经 ref 转发，
 * 避免随渲染重建订阅。runId 变化时换新连接；订阅者归零时关闭连接并清理计时器。
 */
export function useChatEvents(
  runId: string | null,
  onEvent: Listener,
  options: UseChatEventsOptions = {},
): void {
  const eventRef = useRef(onEvent);
  const optionsRef = useRef(options);
  useEffect(() => {
    eventRef.current = onEvent;
    optionsRef.current = options;
  }, [onEvent, options]);
  useEffect(() => {
    if (runId === null) {
      // 无订阅目标：清掉可能残留的旧连接
      if (sourceRunId !== null) teardown();
      return;
    }
    const listener: Listener = (event) => eventRef.current(event);
    const connectionListener: ConnectionListener = (reason) =>
      optionsRef.current.onConnectionLost?.(reason);
    listeners.add(listener);
    connectionListeners.add(connectionListener);
    ensureSource(runId);
    return () => {
      listeners.delete(listener);
      connectionListeners.delete(connectionListener);
      if (listeners.size === 0) teardown();
    };
  }, [runId]);
}

/**
 * 显式关闭单例连接并清理计时器（08 8.7 规则 7「终态…关闭连接、清理计时器」）。
 * 查询恢复轮询到终态后由调用方调用（F0-04B）；调用后需重新订阅（换 runId 挂载）
 * 才会再接收事件——刻意不提供「重开」入口。
 */
export function closeChatEvents(): void {
  teardown();
}
