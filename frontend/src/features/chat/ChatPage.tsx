import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import ChatComposer from "./components/ChatComposer";
import ChatTranscript from "./components/ChatTranscript";
import { Button } from "@/components/ui/button";
import { createSession, getOperation, getRun, readSessionHistory, runReActStream, submitSteering, withdrawSteering } from "@/lib/api";
import type { DiscardReasonWire, EditRunBody, ReActRunBody, RegenerateRunBody, RunStatusWire, SendRunRepeatWire, SessionHistoryWire, SteeringReceiveStatusWire } from "@/lib/contract";
import {
  ReActHttpError,
  applyReActEvent,
  applySteeringStatus,
  attachOperationRun,
  canSubmitChatInput,
  concludeReActRound,
  confirmDraftCreated,
  failureOutcome,
  finishReActRound,
  forgetOperation,
  forgetRunOperations,
  isExpiredOperation,
  pendingExecOperation,
  readLedger,
  rememberOperation,
  resolveReActPending,
  resumeReActRound,
  retainDraftTitle,
  selectSession,
  unknownSteeringRequests,
  updateLedger,
  withPending,
  type PendingOperation,
  type ReActEntry,
  type ReActRound,
} from "./utils/reactAgent";
import { mergeHistoryRounds, operationRound, pruneFrom, reconcileLedger } from "./utils/sessionHistory";

/** 创建会话结果（§4）：created 推进发送，unknown 保留原值等待重试，rejected 形成终态 */
type SessionOutcome = { state: "created" } | { state: "unknown" | "rejected"; message: string };

/** 服务端运行快照（§11.2、§11.3）：重复受理结果与运行查询共用 */
type RunSnapshot = { run_id: string; request_entry_id: string; status: RunStatusWire; error_message?: string | null };

/** Steering 状态快照（§6.1、§7.1）：接收、撤回与操作查询共用同一形状 */
type SteeringSnapshot = { run_id: string; steering_id: string; status: SteeringReceiveStatusWire; entry_id: string | null; reason: DiscardReasonWire | null };

/** 终态查询节拍（§9.2、§5.3）：确认终态前持续补查 */
const RUN_POLL_MS = 1000;

/** 执行接口端点（§5、session-edit-regenerate-contract §3、§4）：三类操作共用同一响应形状 */
const EXEC_ENDPOINT: Record<"send" | "edit" | "regenerate", string> = {
  send: "/api/agent/run",
  edit: "/api/agent/edit",
  regenerate: "/api/agent/regenerate",
};

/**
 * 会话页面（session-history-contract §5）：URL 身份由宿主传入，页面按 session_id 恢复历史与账本。
 * 草稿（isDraft）沿用显式创建流程；持久化会话从后端历史重建展示。切换、卸载与浏览器前进后退
 * 均通过卸载清理本页 SSE、查询与定时任务。
 */
export default function ChatPage({ sessionId, isDraft }: { sessionId: string; isDraft: boolean }) {
  const navigate = useNavigate();
  const records = useRef<ReActRound[]>([]);
  const [rounds, setRounds] = useState<ReActRound[]>([]);
  const operations = useRef<PendingOperation[]>([]);
  const [operationsView, setOperationsView] = useState<PendingOperation[]>([]);
  const [busy, setBusy] = useState(false);
  const [ready, setReady] = useState(() => isDraft && pendingExecOperation(readLedger(sessionId)) === undefined);
  const [error, setError] = useState<string>();
  const [retrying, setRetrying] = useState<string>();
  const [historyStatus, setHistoryStatus] = useState<"loading" | "loaded" | "failed">(isDraft ? "loaded" : "loading");
  const alive = useRef(true);
  const draft = useRef(isDraft);
  const active = useRef<{ controller: AbortController; operation: PendingOperation; run_id?: string } | null>(null);
  const steering = useRef<AbortController | null>(null);
  /** 历史与查询请求身份（§7、§9.2）：与原执行流的取消所有权分离 */
  const inquiry = useRef(new AbortController());
  const watching = useRef<{ roundId?: string; timer?: number } | null>(null);
  /** 展示代际（session-edit-regenerate-contract §8）：受理删除后递增，早于删除发起的历史快照据此丢弃，不回流已删除内容 */
  const displayGeneration = useRef(0);
  /** 已应用受理删除的操作（session-position-contract §2、§3）：保证同一操作的本地删除只执行一次 */
  const appliedDeletions = useRef(new Set<string>());

  const unknownRequests = useMemo(() => unknownSteeringRequests(operationsView), [operationsView]);

  const applyOps = (next: PendingOperation[]): void => {
    operations.current = next;
    setOperationsView(next);
  };

  const update = (id: string, change: (round: ReActRound) => ReActRound): void => {
    records.current = records.current.map((round) => (round.id === id ? change(round) : round));
    setRounds(records.current);
  };

  /** 取消终态查询任务（§9.2）：清理定时轮询；在途查询结果按任务身份丢弃 */
  const stopWatch = (): void => {
    const watch = watching.current;
    if (watch === null) return;
    watching.current = null;
    if (watch.timer !== undefined) window.clearTimeout(watch.timer);
  };

  /** 历史并入展示（§5.2）：历史为已提交事实，账本只保留历史未覆盖的未确认操作 */
  const applyHistory = (history: SessionHistoryWire | null, mode: "initial" | "refresh"): void => {
    const recovery = reconcileLedger(history, readLedger(sessionId));
    const confirmed = new Set(recovery.accepted);
    records.current = mode === "refresh" ? mergeHistoryRounds(records.current, recovery.rounds, confirmed) : recovery.rounds;
    setRounds(records.current);
    if (confirmed.size > 0) applyOps(updateLedger(sessionId, (list) => list.filter((item) => !confirmed.has(item.operation_id))));
    setReady(!(recovery.watch.length > 0 || recovery.query.some((item) => item.operation.kind !== "steering")));
    for (const item of recovery.watch) watchRun(item.roundId, item.runId);
    for (const item of recovery.query) confirmOperation(item.roundId, item.operation);
  };

  const loadHistory = (signal: AbortSignal, mode: "initial" | "refresh"): void => {
    const generation = displayGeneration.current;
    if (mode === "initial") setHistoryStatus("loading");
    void readSessionHistory(sessionId, signal).then(
      (history) => {
        if (!alive.current || signal.aborted || generation !== displayGeneration.current) return;
        applyHistory(history, mode);
        setHistoryStatus("loaded");
        setError(undefined);
      },
      (failure: unknown) => {
        if (!alive.current || signal.aborted) return;
        setHistoryStatus("failed");
        setReady(false);
        setError(failure instanceof Error ? failure.message : "会话历史读取失败。");
      },
    );
  };

  /** 运行终态查询（§9.2、§5.3）：running 期间保持占用，终态后重新读取历史并成功合并才释放占用 */
  const watchRun = (roundId: string, runId: string): void => {
    stopWatch();
    setReady(false);
    const signal = inquiry.current.signal;
    const watch: { roundId?: string; timer?: number } = { roundId };
    watching.current = watch;
    const retryLater = (): void => {
      if (alive.current && !signal.aborted && watching.current === watch) watch.timer = window.setTimeout(poll, RUN_POLL_MS);
    };
    const poll = (): void => {
      void getRun(sessionId, runId, signal).then(
        (run) => {
          if (!alive.current || signal.aborted || watching.current !== watch) return;
          update(roundId, (round) => ({ ...round, run_id: run.run_id, request_entry_id: run.request_entry_id }));
          if (run.status === "running") {
            retryLater();
            return;
          }
          update(roundId, (round) => concludeReActRound(round, run.status, run.error_message ?? undefined));
          watching.current = null;
          loadHistory(signal, "refresh");
        },
        (failure: unknown) => {
          if (!alive.current || signal.aborted || watching.current !== watch) return;
          /* 运行不存在时用操作查询判定该账本是否已失效（session-edit-regenerate-contract §6.1、§8）：
           * 已删除运行会持续返回 404，直接重试会让页面一直停留在恢复占用。 */
          const operation = failure instanceof ReActHttpError && failure.http_status === 404
            ? operations.current.find((item) => item.run_id === runId && item.kind !== "steering")
            : undefined;
          if (operation === undefined) {
            retryLater();
            return;
          }
          void getOperation(sessionId, operation.operation_id, signal).then(retryLater, (probe: unknown) => {
            if (!alive.current || signal.aborted || watching.current !== watch) return;
            if (isExpiredOperation(probe)) expireOperation(roundId, operation);
            else retryLater();
          });
        },
      );
    };
    poll();
  };

  /** 操作查询确认受理结果（§7.1）：未受理表示原请求可能仍在处理中，保持未确认状态等待显式重试 */
  const confirmOperation = (roundId: string, operation: PendingOperation): void => {
    const signal = inquiry.current.signal;
    void getOperation(sessionId, operation.operation_id, signal).then(
      (result) => {
        if (!alive.current || signal.aborted || !result.accepted) return;
        if (result.kind === "steering") {
          if (result.steering !== null) applySteering(roundId, operation, result.steering);
          return;
        }
        if (result.run !== null) resolveRun(roundId, operation, result.run);
      },
      (failure: unknown) => {
        if (!alive.current || signal.aborted) return;
        if (isExpiredOperation(failure)) expireOperation(roundId, operation);
      },
    );
  };

  /** 服务端运行快照落地（§7.1、§9.2）：running 保持未确认，其余形成终态并清理该运行操作 */
  const settleRun = (roundId: string, operationId: string, run: RunSnapshot): void => {
    update(roundId, (round) => ({ ...round, run_id: run.run_id, request_entry_id: run.request_entry_id }));
    if (run.status === "running") return;
    applyOps(forgetOperation(sessionId, operationId));
    update(roundId, (round) => concludeReActRound(round, run.status, run.error_message ?? undefined));
    stopWatch();
  };

  const resolveRun = (roundId: string, operation: PendingOperation, run: RunSnapshot): void => {
    /* 查询确认受理时同样应用删除范围（session-edit-regenerate-contract §8）：网络响应丢失后
     * 靠操作查询恢复，若不删除，历史合并会把已被后端删除的旧轮次当作本页未提交内容保留。 */
    if (operation.kind === "edit" || operation.kind === "regenerate") acceptExec(operation, run.run_id, run.request_entry_id);
    if (run.status === "running") {
      applyOps(attachOperationRun(sessionId, operation.operation_id, run.run_id, run.request_entry_id));
      settleRun(roundId, operation.operation_id, run);
      watchRun(roundId, run.run_id);
      return;
    }
    settleRun(roundId, operation.operation_id, run);
    if (!draft.current) loadHistory(inquiry.current.signal, "refresh");
  };

  /** Steering 状态落地（§6.1、§6.2、§7.1）：先到终态优先；合并后已终态即清理操作。
   *  历史恢复的输入没有原操作账本时 operation 为空，仍按真实状态落地，不伪造操作身份。 */
  const applySteering = (roundId: string, operation: PendingOperation | undefined, snapshot: SteeringSnapshot): void => {
    const operationId = operation?.operation_id;
    let settled = false;
    records.current = records.current.map((round) => {
      if (round.id !== roundId) return round;
      const pruned = operationId === undefined || snapshot.steering_id === operationId ? round : { ...round, entries: round.entries.filter((entry) => !(entry.kind === "user" && entry.id === operationId)) };
      const next = applySteeringStatus(pruned, { ...snapshot, operation_id: operationId }, operation?.request);
      const entry = next.entries.find((item) => item.kind === "user" && item.id === snapshot.steering_id);
      settled = entry !== undefined && entry.kind === "user" && entry.steering !== undefined && entry.steering.status !== "pending";
      return operationId === undefined ? next : resolveReActPending(next, operationId);
    });
    setRounds(records.current);
    if (operationId === undefined) return;
    if (settled) applyOps(forgetOperation(sessionId, operationId));
    else applyOps(updateLedger(sessionId, (list) => list.map((item) => (item.operation_id === operationId ? { ...item, steering_id: snapshot.steering_id } : item))));
  };

  /** Steering 状态补查（§6.2、§7.1）：有原操作走操作查询，无原操作走真实历史查询 */
  const querySteering = (roundId: string, steeringId: string, operation: PendingOperation | undefined): void => {
    if (operation !== undefined) {
      confirmOperation(roundId, operation);
      return;
    }
    void readSessionHistory(sessionId, inquiry.current.signal).then(
      (history) => {
        if (!alive.current) return;
        const record = history.steering.find((item) => item.steering_id === steeringId);
        if (record === undefined) {
          setError("未能确认 Steering 状态。");
          return;
        }
        setError(undefined);
        applySteering(roundId, undefined, record);
      },
      () => {
        if (!alive.current) return;
        setError("Steering 状态查询失败，保持未确认。");
      },
    );
  };

  /** 创建会话（§4）：草稿沿用已保留的 session_id 与原请求标题；成功后打开对应会话 URL */
  const ensureSession = (title: string): Promise<SessionOutcome> => {
    if (!draft.current) return Promise.resolve({ state: "created" });
    return createSession({ session_id: sessionId, title }, inquiry.current.signal).then(
      (): SessionOutcome => {
        confirmDraftCreated();
        draft.current = false;
        setHistoryStatus("loaded");
        navigate(`/sessions/${sessionId}`);
        return { state: "created" };
      },
      (failure: unknown): SessionOutcome => {
        const message = failure instanceof Error ? failure.message : "创建会话结果未知。";
        return failureOutcome(failure) === "rejected" ? { state: "rejected", message } : { state: "unknown", message };
      },
    );
  };

  /** 编辑／重新生成受理成功后的删除与展示（session-position-contract §2、§3）：
   *  移除目标用户节点及其后的全部展示，编辑展示修改后的消息、重新生成保留目标用户节点及其祖先；
   *  同一操作只执行一次，并推进展示代际使早于删除发起的历史快照失效，避免已删除内容回流。 */
  const acceptExec = (operation: PendingOperation, runId: string, requestEntryId: string): void => {
    if (appliedDeletions.current.has(operation.operation_id)) return;
    appliedDeletions.current.add(operation.operation_id);
    displayGeneration.current += 1;
    const { kept, target } = pruneFrom(records.current.filter((round) => round.id !== operation.operation_id), operation.target_entry_id ?? "");
    const entries: ReActEntry[] = operation.kind === "edit"
      ? [{ kind: "user", id: operation.operation_id, request: operation.request }]
      : target !== undefined ? [target] : [];
    records.current = [...kept, { id: operation.operation_id, entries, status: "running", run_id: runId, request_entry_id: requestEntryId }];
    setRounds(records.current);
  };

  /** 一次执行流（§5、session-edit-regenerate-contract §5）：按 Content-Type 处理首次 SSE 与重复受理 JSON。
   *  onSettle 仅在明确受理（SSE 响应头或重复受理 JSON）时收到 true，失败或结果未知时不确认，
   *  以便编辑框只在确认受理后关闭。 */
  const beginExec = (operation: PendingOperation, endpoint: string, onSettle?: (accepted: boolean) => void): void => {
    const current = { controller: new AbortController(), operation, run_id: undefined as string | undefined };
    active.current = current;
    stopWatch();
    update(operation.operation_id, (round) => resumeReActRound(round));
    setBusy(true);
    setReady(false);
    const body: ReActRunBody | EditRunBody | RegenerateRunBody = operation.kind === "edit"
      ? { session_id: operation.session_id, operation_id: operation.operation_id, target_entry_id: operation.target_entry_id ?? "", request: operation.request }
      : operation.kind === "regenerate"
        ? { session_id: operation.session_id, operation_id: operation.operation_id, target_entry_id: operation.target_entry_id ?? "" }
        : { session_id: operation.session_id, operation_id: operation.operation_id, request: operation.request };
    void runReActStream(body, (event) => {
      if (!alive.current || active.current !== current) return;
      update(operation.operation_id, (round) => applyReActEvent(round, event));
      if (event.event === "done" || event.event === "error") {
        active.current = null;
        setBusy(false);
        setReady(true);
        applyOps(forgetRunOperations(sessionId, current.run_id ?? ""));
        if (!draft.current) loadHistory(inquiry.current.signal, "refresh");
      }
    }, current.controller.signal, (headers) => {
      if (!alive.current || active.current !== current) return;
      current.run_id = headers.run_id;
      if (operation.kind === "edit" || operation.kind === "regenerate") acceptExec(operation, headers.run_id, headers.request_entry_id);
      update(operation.operation_id, (round) => ({ ...round, run_id: headers.run_id, request_entry_id: headers.request_entry_id }));
      applyOps(attachOperationRun(sessionId, operation.operation_id, headers.run_id, headers.request_entry_id));
      setReady(true);
      onSettle?.(true);
    }, endpoint).then(
      (repeat) => {
        if (repeat === null || active.current !== current) return;
        active.current = null;
        setBusy(false);
        handleRepeat(operation, repeat);
        onSettle?.(true);
      },
      (failure: unknown) => {
        if (active.current !== current) return;
        active.current = null;
        setBusy(false);
        handleFailure(operation.operation_id, operation, failure, operation.kind === "edit" ? "编辑" : operation.kind === "regenerate" ? "重新生成" : "发送");
        onSettle?.(false);
      },
    );
  };

  /** 一次发送（§5）：先确认会话，再按服务端响应处理执行流 */
  const dispatchSend = (operation: PendingOperation): void => {
    if (draft.current) retainDraftTitle(operation.request);
    void ensureSession(operation.request).then((outcome) => {
      if (!alive.current) return;
      if (outcome.state === "created") {
        beginExec(operation, EXEC_ENDPOINT.send);
        return;
      }
      setError(outcome.message);
      if (outcome.state === "rejected") {
        applyOps(forgetOperation(sessionId, operation.operation_id));
        update(operation.operation_id, (round) => finishReActRound(resumeReActRound(round), "failed", outcome.message));
        setReady(true);
        return;
      }
      applyOps(rememberOperation(sessionId, operation));
      update(operation.operation_id, (round) => ({ ...withPending(round, operation), status: "unknown" }));
      setReady(false);
      void confirmOperation(operation.operation_id, operation);
    });
  };

  /** 提交编辑（session-edit-regenerate-contract §2、§3、§6）：新操作生成新 operation_id，携带修改后的完整文本。
   *  仅在明确受理后返回 true（关闭编辑框）；明确拒绝或结果未知返回 false，保留编辑内容供用户重试。 */
  const editMessage = (entryId: string, text: string): Promise<boolean> => {
    if (!ready || busy || !canSubmitChatInput(text, [])) return Promise.resolve(false);
    setError(undefined);
    const operation: PendingOperation = { operation_id: crypto.randomUUID(), session_id: sessionId, kind: "edit", run_id: null, request: text, created_at: Date.now(), target_entry_id: entryId };
    applyOps(rememberOperation(sessionId, operation));
    return new Promise<boolean>((resolve) => beginExec(operation, EXEC_ENDPOINT.edit, resolve));
  };

  /** 提交重新生成（session-edit-regenerate-contract §2、§4、§6）：新操作生成新 operation_id，目标为所选用户节点 */
  const regenerateMessage = (entryId: string): void => {
    if (!ready || busy) return;
    setError(undefined);
    const operation: PendingOperation = { operation_id: crypto.randomUUID(), session_id: sessionId, kind: "regenerate", run_id: null, request: "", created_at: Date.now(), target_entry_id: entryId };
    applyOps(rememberOperation(sessionId, operation));
    beginExec(operation, EXEC_ENDPOINT.regenerate);
  };

  /** 一次 Steering 提交（§6.1）：首次 accepted，重复返回原输入当前持久化状态 */
  const dispatchSteering = (roundId: string, operation: PendingOperation): void => {
    const target = operation.run_id;
    if (target === null) {
      setError("缺少 Steering 目标运行。");
      return;
    }
    const controller = new AbortController();
    steering.current = controller;
    void submitSteering(target, { session_id: operation.session_id, operation_id: operation.operation_id, message: operation.request }, controller.signal).then(
      (received) => {
        if (!alive.current || steering.current !== controller) return;
        steering.current = null;
        applySteering(roundId, operation, received);
      },
      (failure: unknown) => {
        if (!alive.current || steering.current !== controller) return;
        steering.current = null;
        handleFailure(roundId, operation, failure, "Steering 提交");
      },
    );
  };

  /** 撤回未消费的 Steering（§6.2）：用历史的 session_id/run_id/steering_id 发起，不依赖本地账本；
   *  消费冲突或结果未知时按真实状态补查（有原操作走操作查询，无原操作走历史查询）。 */
  const withdraw = (roundId: string, steeringId: string): void => {
    const round = records.current.find((item) => item.id === roundId);
    const entry = round?.entries.find((item) => item.id === steeringId && item.kind === "user");
    const target = round?.run_id;
    if (round === undefined || entry === undefined || entry.kind !== "user" || entry.steering?.status !== "pending" || target === undefined) return;
    const operation = entry.operation_id === undefined ? undefined : operations.current.find((item) => item.operation_id === entry.operation_id);
    setError(undefined);
    void withdrawSteering(target, steeringId, { session_id: sessionId }, inquiry.current.signal).then(
      (result) => {
        if (!alive.current) return;
        applySteering(roundId, operation, result);
      },
      (failure: unknown) => {
        if (!alive.current) return;
        const conflict = failure instanceof ReActHttpError && failure.code === "steering_consumption_conflict";
        if (conflict || failureOutcome(failure) === "unknown") {
          setError(conflict ? "Steering 消费已开始。" : "撤回结果未知，正在补查状态。");
          querySteering(roundId, steeringId, operation);
          return;
        }
        setError(failure instanceof Error ? failure.message : "撤回失败。");
      },
    );
  };

  /** 显式重试（§5.5）：先查询原操作，已受理只关联状态，未受理才沿用原身份与原正文重发 */
  const retry = (roundId: string, operationId: string): void => {
    const operation = operations.current.find((item) => item.operation_id === operationId);
    if (operation === undefined || retrying !== undefined) return;
    setError(undefined);
    setRetrying(operationId);
    void getOperation(sessionId, operationId, inquiry.current.signal).then(
      (result) => {
        if (!alive.current) return;
        setRetrying(undefined);
        if (result.accepted) {
          if (result.kind === "steering") {
            if (result.steering !== null) applySteering(roundId, operation, result.steering);
            return;
          }
          if (result.run !== null) resolveRun(roundId, operation, result.run);
          return;
        }
        if (operation.kind === "steering") dispatchSteering(roundId, operation);
        else if (operation.kind === "send") dispatchSend(operation);
        else beginExec(operation, EXEC_ENDPOINT[operation.kind]);
      },
      (failure: unknown) => {
        if (!alive.current) return;
        setRetrying(undefined);
        if (isExpiredOperation(failure)) {
          expireOperation(roundId, operation);
          return;
        }
        setError("重试查询结果未知，保持未确认。");
      },
    );
  };

  /** 未确认落地（§5.5）：保留原操作身份并留下显式重试入口；编辑／重新生成在受理前不改变历史展示 */
  const keepUnknown = (roundId: string, operation: PendingOperation): void => {
    applyOps(rememberOperation(sessionId, operation));
    if (records.current.some((round) => round.id === roundId)) {
      update(roundId, (round) => (operation.kind === "send" ? { ...withPending(round, operation), status: "unknown" } : withPending(round, operation)));
      return;
    }
    records.current = [...records.current, operationRound(operation)];
    setRounds(records.current);
  };

  /** 失效操作静默处理（用户约定）：删除本地账本与重试入口、停止该操作查询、重读历史；
   *  不弹该失效操作的错误提示、不恢复或重发旧请求，不影响其他操作与当前新运行。 */
  const expireOperation = (roundId: string, operation: PendingOperation): void => {
    applyOps(forgetOperation(sessionId, operation.operation_id));
    records.current = records.current.filter((round) => round.id !== roundId);
    setRounds(records.current);
    if (watching.current?.roundId === roundId) stopWatch();
    setError(undefined);
    setReady(false);
    if (!draft.current) loadHistory(inquiry.current.signal, "refresh");
  };

  /** 失败落地（§11.5）：4xx 为明确拒绝，其余保留原操作身份与未确认状态 */
  const handleFailure = (roundId: string, operation: PendingOperation, failure: unknown, label: string): void => {
    if (!alive.current) return;
    if (isExpiredOperation(failure)) {
      expireOperation(roundId, operation);
      return;
    }
    const message = failure instanceof Error ? failure.message : `${label}结果未知。`;
    if (failureOutcome(failure) === "rejected") {
      applyOps(forgetOperation(sessionId, operation.operation_id));
      setError(message);
      if (operation.kind === "send") {
        update(roundId, (round) => finishReActRound(round.status === "unknown" ? resumeReActRound(round) : round, "failed", message));
      } else if (operation.kind === "edit" || operation.kind === "regenerate") {
        records.current = records.current.filter((round) => round.id !== roundId);
        setRounds(records.current);
      }
      setReady(true);
      return;
    }
    keepUnknown(roundId, operation);
    setError(`${label}结果未知，可显式重试。`);
    setReady(false);
    void confirmOperation(roundId, operation);
  };

  /** 重复受理（§5、session-edit-regenerate-contract §5.2）：返回原运行当前持久化状态，不启动新的执行 */
  const handleRepeat = (operation: PendingOperation, repeat: SendRunRepeatWire): void => {
    if (!alive.current) return;
    if (operation.kind === "edit" || operation.kind === "regenerate") acceptExec(operation, repeat.run_id, repeat.request_entry_id);
    resolveRun(operation.operation_id, operation, repeat);
    if (repeat.status !== "running") return;
    update(operation.operation_id, (round) => ({ ...withPending(round, { ...operation, run_id: repeat.run_id }), status: "unknown" }));
    setReady(false);
    setError("该操作已受理，运行仍在执行。");
  };

  /** 用户停止（§9.1、§9.2）：关闭页面持有的连接，保留运行身份与占用，由独立查询确认终态 */
  const stop = (): void => {
    const run = active.current;
    if (run === null) return;
    active.current = null;
    run.controller.abort();
    setBusy(false);
    setReady(false);
    if (run.run_id !== undefined) {
      watchRun(run.operation.operation_id, run.run_id);
      return;
    }
    keepUnknown(run.operation.operation_id, run.operation);
    void confirmOperation(run.operation.operation_id, run.operation);
  };

  /** 重新读取历史（§5.3）：读取失败保留已有展示，未成功加载前禁止向该会话发送 */
  const reload = (): void => {
    if (draft.current || historyStatus === "loading") return;
    stopWatch();
    setError(undefined);
    setReady(false);
    loadHistory(inquiry.current.signal, "initial");
  };

  const send = (request: string): Promise<boolean> => {
    if (steering.current !== null || watching.current !== null || !canSubmitChatInput(request, unknownSteeringRequests(operations.current))) return Promise.resolve(false);
    setError(undefined);
    const run = active.current;
    if (run !== null) {
      if (run.run_id === undefined) return Promise.resolve(false);
      const operation: PendingOperation = { operation_id: crypto.randomUUID(), session_id: sessionId, kind: "steering", run_id: run.run_id, request, created_at: Date.now() };
      applyOps(rememberOperation(sessionId, operation));
      dispatchSteering(run.operation.operation_id, operation);
      return Promise.resolve(true);
    }
    if (!ready || pendingExecOperation(operations.current) !== undefined) return Promise.resolve(false);
    const operation: PendingOperation = { operation_id: crypto.randomUUID(), session_id: sessionId, kind: "send", run_id: null, request, created_at: Date.now() };
    applyOps(rememberOperation(sessionId, operation));
    records.current = [...records.current, { id: operation.operation_id, entries: [{ kind: "user", id: operation.operation_id, request }], status: "running" }];
    setRounds(records.current);
    dispatchSend(operation);
    return Promise.resolve(true);
  };

  useEffect(() => {
    alive.current = true;
    inquiry.current = new AbortController();
    if (!isDraft) selectSession(sessionId);
    const ops = readLedger(sessionId);
    applyOps(ops);
    if (isDraft) applyHistory(null, "initial");
    else loadHistory(inquiry.current.signal, "initial");
    return () => {
      alive.current = false;
      stopWatch();
      inquiry.current.abort();
      active.current?.controller.abort();
      active.current = null;
      steering.current?.abort();
      steering.current = null;
    };
    // 组件以会话身份为 key，仅在会话或草稿身份变化时重挂载（§5.1）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="relative flex h-full w-full flex-col overflow-hidden">
      <ChatTranscript rounds={rounds} onRetry={retry} onWithdraw={withdraw} retrying={retrying} canEdit={ready && !busy} onEdit={editMessage} onRegenerate={regenerateMessage} />
      {historyStatus === "failed" && <div className="mx-auto flex w-full max-w-4xl items-center gap-2 px-6 pt-2"><Button type="button" variant="secondary" size="sm" onClick={reload}>重新加载历史</Button></div>}
      <ChatComposer busy={busy} ready={ready} error={error} unknownRequests={unknownRequests} onSend={send} onStop={stop} />
    </div>
  );
}
