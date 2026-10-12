import {
  createSession,
  getOperation,
  getRun,
  getSaveConfirmation,
  postSaveConfirmation,
  readSessionHistory,
  runReActStream,
  submitSteering,
  withdrawSteering,
} from "@/lib/api";
import {
  PLAN_QUERY_KEY,
  PROVIDER_QUERY_KEY,
  WORKOUT_QUERY_KEY,
  queryClient,
} from "@/lib/query";
import {
  NO_VALID_MODEL_CONFIG,
  hasValidProviderConfig,
} from "@/features/provider/providerConfig";
import type {
  AttachmentWire,
  ConfirmationBusinessKindWire,
  DiscardReasonWire,
  EditRunBody,
  PendingProposalReferenceWire,
  ProviderStatusWire,
  ReActRunBody,
  RegenerateRunBody,
  RunStatusWire,
  SaveConfirmationOperationWire,
  SaveConfirmationRequestWire,
  SendRunRepeatWire,
  SessionHistoryWire,
  SteeringReceiveStatusWire,
} from "@/lib/contract";
import {
  attachmentInputs,
  draftSessionTitle,
  retainedAttachmentDrafts,
  type AttachmentDraft,
} from "./attachments";
import {
  attemptFromLedger,
  confirmationOccupying,
  forgetConfirmationLedger,
  newConfirmationRequest,
  readConfirmationLedger,
  removeConfirmationAttempt,
  updateConfirmationLedger,
  upsertConfirmationAttempt,
  applyConfirmationOperation,
  type ConfirmationAttempt,
} from "./confirmations";
import {
  ReActHttpError,
  SESSION_DELETED_EVENT,
  applyReActEvent,
  applySteeringStatus,
  attachOperationRun,
  attachmentDisplay,
  canSubmitChatInput,
  concludeReActRound,
  confirmDraftCreated,
  failureOutcome,
  finishReActRound,
  forgetOperation,
  forgetRunOperations,
  isConfirmationNotFound,
  isExpiredOperation,
  pendingExecOperation,
  readLedger,
  rememberOperation,
  resolveReActPending,
  resumeReActRound,
  retainDraftTitle,
  selectSession,
  unknownSteeringKeys,
  updateLedger,
  withPending,
  type PendingOperation,
  type ReActEntry,
  type ReActRound,
} from "./reactAgent";
import {
  mergeHistoryRounds,
  operationRound,
  pruneFrom,
  reconcileLedger,
} from "./sessionHistory";

/** 创建会话结果（§4）：created 推进发送，unknown 保留原值等待重试，rejected 形成终态 */
type SessionOutcome =
  { state: "created" } | { state: "unknown" | "rejected"; message: string };

/** 服务端运行快照（§11.2、§11.3）：重复受理结果与运行查询共用 */
type RunSnapshot = {
  run_id: string;
  request_entry_id: string;
  status: RunStatusWire;
  error_message?: string | null;
};

/** Steering 状态快照（§6.1、§7.1、附件契约 §4）：接收、撤回与操作查询共用同一形状 */
type SteeringSnapshot = {
  run_id: string;
  steering_id: string;
  status: SteeringReceiveStatusWire;
  entry_id: string | null;
  reason: DiscardReasonWire | null;
  /** 受理时的有序附件集合，撤回响应与列表查询按契约不含该投影 */
  attachments?: AttachmentWire[];
};

/** 页面渲染所需的会话运行快照（§5.1）：管理层持有，页面只读订阅 */
export interface SessionRunView {
  /** 本地草稿已创建为持久会话（§4）：页面据此切换 URL */
  persistent: boolean;
  history: "loading" | "loaded" | "failed";
  rounds: ReActRound[];
  operations: PendingOperation[];
  busy: boolean;
  ready: boolean;
  /** 未决与已核实的确认尝试（确认契约 §7.3、§7.5）：按原操作身份恢复 */
  confirmations: ConfirmationAttempt[];
  /** 当前有效待确认快照引用（§7.6）：确认按钮可用性的唯一依据 */
  proposals: PendingProposalReferenceWire[];
  /** 确认处理中或结果未知（§7.1）：禁用新的发送、编辑、重新生成及其它确认操作 */
  confirming: boolean;
  error?: string;
  retrying?: string;
}

/** 终态查询节拍（§9.2、§5.3）：确认终态前持续补查 */
const RUN_POLL_MS = 1000;

/** 执行接口端点（§5、session-edit-regenerate-contract §3、§4）：三类操作共用同一响应形状 */
const EXEC_ENDPOINT: Record<"send" | "edit" | "regenerate", string> = {
  send: "/api/agent/run",
  edit: "/api/agent/edit",
  regenerate: "/api/agent/regenerate",
};

/**
 * 单会话运行记录（backend-http-sse-contract §9.1、session-history-contract §5.1）：
 * 执行连接、取消控制、实时状态、Steering、账本、操作查询与终态定时任务全部由本记录持有，
 * 生命周期覆盖站内导航与页面卸载；卸载仅解除订阅，删除目标会话才关闭连接并隔离迟到回调。
 */
class SessionRunRecord {
  readonly sessionId: string;
  private readonly listeners = new Set<() => void>();
  private readonly inquiry = new AbortController();
  private readonly appliedDeletions = new Set<string>();
  private view: SessionRunView;
  private exec: {
    controller: AbortController;
    operation: PendingOperation;
    run_id?: string;
  } | null = null;
  private steering: AbortController | null = null;
  /** 确认提交在途控制器（确认契约 §7.2）：会话删除或页面卸载时关闭，服务端继续收尾原操作 */
  private confirm: AbortController | null = null;
  /** 原操作核实定时任务（§7.5）：仅在存在未决确认时排期 */
  private confirmWatch: { timer?: ReturnType<typeof setTimeout> } | null = null;
  private watch: {
    roundId?: string;
    timer?: ReturnType<typeof setTimeout>;
  } | null = null;
  /** 历史代际：新执行、展示剪裁及后续历史查询使在途旧响应失效 */
  private generation = 0;
  private initialized = false;
  /** 已随会话删除移出管理层：迟到 SSE、查询与定时回调一律丢弃（session-delete-contract §5） */
  private removed = false;

  constructor(sessionId: string, isDraft: boolean) {
    this.sessionId = sessionId;
    this.view = {
      persistent: !isDraft,
      history: isDraft ? "loaded" : "loading",
      rounds: [],
      operations: [],
      busy: false,
      ready:
        isDraft && pendingExecOperation(readLedger(sessionId)) === undefined,
      confirmations: [],
      proposals: [],
      confirming: false,
    };
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  snapshot = (): SessionRunView => this.view;

  /** 页面挂载：登记当前选择，首次接触该会话时读取历史与账本；返回会话只读当前快照（§5.1） */
  mount = (isDraft: boolean): void => {
    if (!isDraft) selectSession(this.sessionId);
    if (this.initialized) return;
    this.initialized = true;
    const operations = readLedger(this.sessionId);
    this.setOperations(operations);
    if (isDraft) this.applyHistory(null, "initial");
    else this.loadHistory(this.inquiry.signal, "initial");
  };

  /** 会话删除：关闭原执行流触发后端协作取消，清理查询、定时任务与订阅（session-delete-contract §3、§5） */
  dispose(): void {
    this.removed = true;
    this.stopWatch();
    this.stopConfirmWatch();
    this.exec?.controller.abort();
    this.exec = null;
    this.steering?.abort();
    this.steering = null;
    this.confirm?.abort();
    this.confirm = null;
    this.inquiry.abort();
    forgetConfirmationLedger(this.sessionId);
    this.listeners.clear();
  }

  private emit(): void {
    for (const listener of [...this.listeners]) listener();
  }

  private patch(change: Partial<SessionRunView>): void {
    this.view = { ...this.view, ...change };
    this.emit();
  }

  private setRounds(rounds: ReActRound[]): void {
    this.patch({ rounds });
  }

  private update(id: string, change: (round: ReActRound) => ReActRound): void {
    this.setRounds(
      this.view.rounds.map((round) =>
        round.id === id ? change(round) : round,
      ),
    );
  }

  private setOperations(next: PendingOperation[]): void {
    this.patch({ operations: next });
  }

  /** 取消终态查询任务（§9.2）：清理定时轮询；在途查询结果按任务身份丢弃 */
  private stopWatch(): void {
    const watch = this.watch;
    if (watch === null) return;
    this.watch = null;
    if (watch.timer !== undefined) clearTimeout(watch.timer);
  }

  /** 历史并入展示（§5.2）：历史为已提交事实，账本只保留历史未覆盖的未确认操作 */
  private applyHistory(
    history: SessionHistoryWire | null,
    mode: "initial" | "refresh",
  ): void {
    const recovery = reconcileLedger(history, readLedger(this.sessionId));
    const confirmed = new Set(recovery.accepted);
    this.setRounds(
      mode === "refresh"
        ? mergeHistoryRounds(this.view.rounds, recovery.rounds, confirmed)
        : recovery.rounds,
    );
    if (confirmed.size > 0)
      this.setOperations(
        updateLedger(this.sessionId, (list) =>
          list.filter((item) => !confirmed.has(item.operation_id)),
        ),
      );
    this.patch({
      ready: !(
        recovery.watch.length > 0 ||
        recovery.query.some((item) => item.operation.kind !== "steering")
      ),
    });
    for (const item of recovery.watch) this.watchRun(item.roundId, item.runId);
    for (const item of recovery.query)
      this.confirmOperation(item.roundId, item.operation);
    this.applyConfirmations(history);
  }

  private loadHistory(signal: AbortSignal, mode: "initial" | "refresh"): void {
    if (this.exec !== null) return;
    const generation = ++this.generation;
    if (mode === "initial") this.patch({ history: "loading" });
    void readSessionHistory(this.sessionId, signal).then(
      (history) => {
        if (this.removed || signal.aborted || generation !== this.generation)
          return;
        this.applyHistory(history, mode);
        this.patch({ history: "loaded", error: undefined });
        if (mode === "refresh")
          void queryClient.invalidateQueries({ queryKey: ["sessions"] });
      },
      (failure: unknown) => {
        if (this.removed || signal.aborted || generation !== this.generation)
          return;
        this.patch({
          history: "failed",
          ready: false,
          error:
            failure instanceof Error ? failure.message : "会话历史读取失败。",
        });
      },
    );
  }

  /** 运行终态查询（§9.2、§5.3）：running 期间保持占用，终态后重新读取历史并成功合并才释放占用 */
  private watchRun(roundId: string, runId: string): void {
    this.stopWatch();
    this.patch({ ready: false });
    const signal = this.inquiry.signal;
    const watch: { roundId?: string; timer?: ReturnType<typeof setTimeout> } = {
      roundId,
    };
    this.watch = watch;
    const retryLater = (): void => {
      if (!this.removed && !signal.aborted && this.watch === watch)
        watch.timer = setTimeout(poll, RUN_POLL_MS);
    };
    const poll = (): void => {
      void getRun(this.sessionId, runId, signal).then(
        (run) => {
          if (this.removed || signal.aborted || this.watch !== watch) return;
          this.update(roundId, (round) => ({
            ...round,
            run_id: run.run_id,
            request_entry_id: run.request_entry_id,
          }));
          if (run.status === "running") {
            retryLater();
            return;
          }
          this.update(roundId, (round) =>
            concludeReActRound(
              round,
              run.status,
              run.error_message ?? undefined,
            ),
          );
          this.watch = null;
          this.loadHistory(signal, "refresh");
        },
        (failure: unknown) => {
          if (this.removed || signal.aborted || this.watch !== watch) return;
          /* 运行不存在时用操作查询判定该账本是否已失效（session-edit-regenerate-contract §6.1、§8）：
           * 已删除运行会持续返回 404，直接重试会让页面一直停留在恢复占用。 */
          const operation =
            failure instanceof ReActHttpError && failure.http_status === 404
              ? this.view.operations.find(
                  (item) => item.run_id === runId && item.kind !== "steering",
                )
              : undefined;
          if (operation === undefined) {
            retryLater();
            return;
          }
          void getOperation(
            this.sessionId,
            operation.operation_id,
            signal,
          ).then(retryLater, (probe: unknown) => {
            if (this.removed || signal.aborted || this.watch !== watch) return;
            if (isExpiredOperation(probe))
              this.expireOperation(roundId, operation);
            else retryLater();
          });
        },
      );
    };
    poll();
  }

  /** 操作查询确认受理结果（§7.1）：未受理表示原请求可能仍在处理中，保持未确认状态等待显式重试 */
  private confirmOperation(roundId: string, operation: PendingOperation): void {
    const signal = this.inquiry.signal;
    void getOperation(this.sessionId, operation.operation_id, signal).then(
      (result) => {
        if (
          this.removed ||
          signal.aborted ||
          !result.accepted ||
          this.exec?.operation.operation_id === operation.operation_id
        )
          return;
        if (result.kind === "steering") {
          if (result.steering !== null)
            this.applySteering(roundId, operation, result.steering);
          return;
        }
        if (result.run !== null)
          this.resolveRun(roundId, operation, result.run);
      },
      (failure: unknown) => {
        if (
          this.removed ||
          signal.aborted ||
          this.exec?.operation.operation_id === operation.operation_id
        )
          return;
        if (isExpiredOperation(failure))
          this.expireOperation(roundId, operation);
      },
    );
  }

  /* ===== 结构化保存确认（structured-save-confirmation §7.2、§7.5、§7.6）===== */

  /** 确认阶段落地并同步占用标记（§7.1） */
  private setConfirmations(list: ConfirmationAttempt[]): void {
    this.patch({
      confirmations: list,
      confirming: confirmationOccupying(list),
    });
  }

  /** 停止原操作核实任务（§7.5） */
  private stopConfirmWatch(): void {
    const watch = this.confirmWatch;
    if (watch === null) return;
    this.confirmWatch = null;
    if (watch.timer !== undefined) clearTimeout(watch.timer);
  }

  /** 下一拍核实（§7.5）：processing 与查询失败持续查询，未登记保持未知并留下显式重试入口 */
  private scheduleConfirmPoll(): void {
    if (this.removed) return;
    this.stopConfirmWatch();
    const watch: { timer?: ReturnType<typeof setTimeout> } = {};
    this.confirmWatch = watch;
    watch.timer = setTimeout(() => {
      if (!this.removed && this.confirmWatch === watch) this.verifyConfirmations();
    }, RUN_POLL_MS);
  }

  /** 专用状态查询（§7.3）：只读核实原操作，不创建确认节点也不触发保存；
   *  未登记时按结果未知的处理策略交回调用方，其余失败保持占用并继续下一拍查询 */
  private probeOriginal(
    operationId: string,
    onMissing?: () => void,
  ): void {
    if (this.removed) return;
    const watch = this.confirmWatch ?? {};
    this.confirmWatch = watch;
    const signal = this.inquiry.signal;
    void getSaveConfirmation(this.sessionId, operationId, signal).then(
      (operation) => {
        if (this.removed || this.confirmWatch !== watch || signal.aborted) return;
        this.settleConfirmation(operation);
      },
      (failure: unknown) => {
        if (this.removed || this.confirmWatch !== watch || signal.aborted) return;
        // 未查到操作不证明原请求已经结束（§7.5）
        if (isConfirmationNotFound(failure)) {
          onMissing?.();
          return;
        }
        this.scheduleConfirmPoll();
      },
    );
  }

  /** 未决确认的核实（§7.5）：processing 与结果未知持续查询，未登记保持未知并留下显式重试入口 */
  private verifyConfirmations(): void {
    const attempt = this.view.confirmations.find(
      (item) => item.phase === "pending" || item.phase === "processing",
    );
    if (attempt === undefined) {
      this.stopConfirmWatch();
      return;
    }
    // 每个未决操作只保有一条核实链路：显式重试与轮询复用同一 watch 身份
    this.stopConfirmWatch();
    this.probeOriginal(attempt.request.operation_id);
  }

  /** 真实操作对象落地（§7.3、§7.6）：saved 刷新业务查询与会话列表，终态后重读历史 */
  private settleConfirmation(operation: SaveConfirmationOperationWire): void {
    this.stopConfirmWatch();
    this.setConfirmations(
      applyConfirmationOperation(this.view.confirmations, operation),
    );
    if (operation.status === "processing") {
      this.scheduleConfirmPoll();
      return;
    }
    updateConfirmationLedger(this.sessionId, (list) =>
      list.filter(
        (item) => item.request.operation_id !== operation.operation_id,
      ),
    );
    if (operation.status === "saved") {
      this.invalidateBusiness(operation.business_kind);
      void queryClient.invalidateQueries({ queryKey: ["sessions"] });
    }
    // 失败保持已保存业务页面原值，仅刷新待确认状态（§7.6）
    if (this.view.persistent) this.loadHistory(this.inquiry.signal, "refresh");
  }

  /** saved 后刷新对应业务查询（§7.6）：画像、计划与训练记录各自的重取入口 */
  private invalidateBusiness(kind: ConfirmationBusinessKindWire): void {
    if (kind === "profile")
      queryClient.invalidateQueries({ queryKey: ["profile"] });
    if (kind === "plan")
      queryClient.invalidateQueries({ queryKey: PLAN_QUERY_KEY });
    if (kind === "workout")
      queryClient.invalidateQueries({ queryKey: WORKOUT_QUERY_KEY });
  }

  /** 移出账本与展示（§7.4：授权拒绝不创建操作） */
  private dropConfirmation(operationId: string): void {
    updateConfirmationLedger(this.sessionId, (list) =>
      list.filter((item) => item.request.operation_id !== operationId),
    );
    this.setConfirmations(
      removeConfirmationAttempt(this.view.confirmations, operationId),
    );
  }

  /** 一次确认提交（§7.2）：处理期间与 Agent 运行互斥，无需模型配置 */
  private dispatchConfirmation(request: SaveConfirmationRequestWire): void {
    const controller = new AbortController();
    this.confirm = controller;
    this.stopConfirmWatch();
    this.patch({ ready: false, error: undefined });
    void postSaveConfirmation(this.sessionId, request, controller.signal).then(
      (operation) => {
        if (
          this.removed ||
          this.confirm !== controller ||
          controller.signal.aborted
        )
          return;
        this.confirm = null;
        this.settleConfirmation(operation);
      },
      (failure: unknown) => {
        if (this.removed || this.confirm !== controller) return;
        this.confirm = null;
        this.rejectConfirmation(request, failure);
      },
    );
  }

  /** 确认失败落地（§7.4、§7.5）：明确拒绝先核实是否已登记操作，超时断连与协议异常一律按结果未知 */
  private rejectConfirmation(
    request: SaveConfirmationRequestWire,
    failure: unknown,
  ): void {
    const message =
      failure instanceof Error ? failure.message : "确认保存结果未知。";
    if (isExpiredOperation(failure)) {
      this.dropConfirmation(request.operation_id);
      if (this.view.persistent) this.loadHistory(this.inquiry.signal, "refresh");
      return;
    }
    if (failureOutcome(failure) !== "rejected") {
      this.keepConfirmationUnknown(request, message);
      this.verifyConfirmations();
      return;
    }
    // 核实前先记为结果未知，专用查询确认未登记时再移出
    this.keepConfirmationUnknown(request, message);
    this.probeOriginal(request.operation_id, () => {
      // 服务端明确拒绝且未登记操作：该快照仍可再次点击确认，占用随之解除
      this.dropConfirmation(request.operation_id);
      this.patch({ error: message, ready: true });
    });
  }

  /** 结果未知保留（§7.5）：不换 ID 自动重发，原操作身份与四字段请求保持可显式重试 */
  private keepConfirmationUnknown(
    request: SaveConfirmationRequestWire,
    notice: string,
  ): void {
    this.setConfirmations(
      upsertConfirmationAttempt(this.view.confirmations, {
        session_id: this.sessionId,
        request,
        phase: "pending",
        operation: null,
        notice,
      }),
    );
  }

  /** 历史与确认账本合并（§7.5、§7.6）：历史已覆盖的操作移出账本与展示，未决操作转入专用查询 */
  private applyConfirmations(history: SessionHistoryWire | null): void {
    const covered = new Set(
      (history === null ? [] : history.confirmations).map(
        (item) => item.operation_id,
      ),
    );
    const stored = readConfirmationLedger(this.sessionId);
    const unresolved = stored.filter(
      (item) => !covered.has(item.request.operation_id),
    );
    if (unresolved.length !== stored.length)
      updateConfirmationLedger(this.sessionId, () => unresolved);
    let list = this.view.confirmations.filter(
      (item) => !covered.has(item.request.operation_id),
    );
    for (const item of unresolved)
      list = list.some(
        (current) => current.request.operation_id === item.request.operation_id,
      )
        ? list
        : upsertConfirmationAttempt(list, attemptFromLedger(item));
    this.patch({
      proposals: history === null ? [] : history.pending_proposals,
      confirmations: list,
      confirming: confirmationOccupying(list),
    });
    if (list.some((item) => item.phase === "pending" || item.phase === "processing"))
      this.verifyConfirmations();
  }

  /** 提交一份快照的确认保存（§7.2、§7.6）：目标只由点击的快照与当前有效引用决定，
   *  普通聊天文字不参与目标推断；执行期间、失效快照与其它未决确认均不开放新的提交 */
  confirmProposal = (
    kind: ConfirmationBusinessKindWire,
    proposalId: string,
    displayEntryId: string,
  ): void => {
    if (
      !this.view.persistent ||
      !this.view.ready ||
      this.view.busy ||
      this.view.confirming ||
      this.exec !== null ||
      this.confirm !== null
    )
      return;
    const reference = this.view.proposals.find(
      (item) =>
        item.business_kind === kind &&
        item.proposal_id === proposalId &&
        item.display_entry_id === displayEntryId &&
        item.status === "pending",
    );
    if (reference === undefined) return;
    const request = newConfirmationRequest(kind, proposalId, displayEntryId);
    updateConfirmationLedger(this.sessionId, (list) => [
      ...list,
      { session_id: this.sessionId, request },
    ]);
    this.setConfirmations(
      upsertConfirmationAttempt(this.view.confirmations, {
        session_id: this.sessionId,
        request,
        phase: "pending",
        operation: null,
      }),
    );
    this.dispatchConfirmation(request);
  };

  /** 显式重试原确认请求（§7.4、§7.5）：沿用原操作 ID 与完整四字段请求 */
  retryConfirmation = (operationId: string): void => {
    const attempt = this.view.confirmations.find(
      (item) => item.request.operation_id === operationId,
    );
    if (
      attempt === undefined ||
      this.exec !== null ||
      this.confirm !== null ||
      !this.view.persistent
    )
      return;
    this.stopConfirmWatch();
    this.keepConfirmationUnknown(attempt.request, "正在重试原确认请求。");
    this.dispatchConfirmation(attempt.request);
  };

  /** 服务端运行快照落地（§7.1、§9.2）：running 保持未确认，其余形成终态并清理该运行操作 */
  private settleRun(
    roundId: string,
    operationId: string,
    run: RunSnapshot,
  ): void {
    this.update(roundId, (round) => ({
      ...round,
      run_id: run.run_id,
      request_entry_id: run.request_entry_id,
    }));
    if (run.status === "running") return;
    this.setOperations(forgetOperation(this.sessionId, operationId));
    this.update(roundId, (round) =>
      concludeReActRound(round, run.status, run.error_message ?? undefined),
    );
    this.stopWatch();
  }

  private resolveRun(
    roundId: string,
    operation: PendingOperation,
    run: RunSnapshot,
  ): void {
    /* 查询确认受理时同样应用删除范围（session-edit-regenerate-contract §8）：网络响应丢失后
     * 靠操作查询恢复，若不删除，历史合并会把已被后端删除的旧轮次当作本页未提交内容保留。 */
    if (operation.kind === "edit" || operation.kind === "regenerate")
      this.acceptExec(operation, run.run_id, run.request_entry_id);
    if (run.status === "running") {
      this.setOperations(
        attachOperationRun(
          this.sessionId,
          operation.operation_id,
          run.run_id,
          run.request_entry_id,
        ),
      );
      this.settleRun(roundId, operation.operation_id, run);
      this.watchRun(roundId, run.run_id);
      return;
    }
    this.settleRun(roundId, operation.operation_id, run);
    if (this.view.persistent) this.loadHistory(this.inquiry.signal, "refresh");
  }

  /** Steering 状态落地（§6.1、§6.2、§7.1）：先到终态优先；合并后已终态即清理操作。
   *  历史恢复的输入没有原操作账本时 operation 为空，仍按真实状态落地，不伪造操作身份。 */
  private applySteering(
    roundId: string,
    operation: PendingOperation | undefined,
    snapshot: SteeringSnapshot,
  ): void {
    const operationId = operation?.operation_id;
    let settled = false;
    const rounds = this.view.rounds.map((round) => {
      if (round.id !== roundId) return round;
      const pruned =
        operationId === undefined || snapshot.steering_id === operationId
          ? round
          : {
              ...round,
              entries: round.entries.filter(
                (entry) => !(entry.kind === "user" && entry.id === operationId),
              ),
            };
      const next = applySteeringStatus(
        pruned,
        { ...snapshot, operation_id: operationId },
        operation?.request,
        snapshot.attachments !== undefined
          ? retainedAttachmentDrafts(snapshot.attachments)
          : operation?.attachments,
      );
      const entry = next.entries.find(
        (item) => item.kind === "user" && item.id === snapshot.steering_id,
      );
      settled =
        entry !== undefined &&
        entry.kind === "user" &&
        entry.steering !== undefined &&
        entry.steering.status !== "pending";
      return operationId === undefined
        ? next
        : resolveReActPending(next, operationId);
    });
    this.setRounds(rounds);
    if (operationId === undefined) return;
    if (settled)
      this.setOperations(forgetOperation(this.sessionId, operationId));
    else
      this.setOperations(
        updateLedger(this.sessionId, (list) =>
          list.map((item) =>
            item.operation_id === operationId
              ? { ...item, steering_id: snapshot.steering_id }
              : item,
          ),
        ),
      );
  }

  /** Steering 状态补查（§6.2、§7.1）：有原操作走操作查询，无原操作走真实历史查询 */
  private querySteering(
    roundId: string,
    steeringId: string,
    operation: PendingOperation | undefined,
  ): void {
    if (operation !== undefined) {
      this.confirmOperation(roundId, operation);
      return;
    }
    void readSessionHistory(this.sessionId, this.inquiry.signal).then(
      (history) => {
        if (this.removed) return;
        const record = history.steering.find(
          (item) => item.steering_id === steeringId,
        );
        if (record === undefined) {
          this.patch({ error: "未能确认 Steering 状态。" });
          return;
        }
        this.patch({ error: undefined });
        this.applySteering(roundId, undefined, record);
      },
      () => {
        if (this.removed) return;
        this.patch({ error: "Steering 状态查询失败，保持未确认。" });
      },
    );
  }

  /** 创建会话（§4）：草稿沿用已保留的 session_id 与原请求标题；成功后由页面切到对应会话 URL */
  private ensureSession(title: string): Promise<SessionOutcome> {
    if (this.view.persistent) return Promise.resolve({ state: "created" });
    return createSession(
      { session_id: this.sessionId, title },
      this.inquiry.signal,
    ).then(
      (): SessionOutcome => {
        if (!this.removed) {
          confirmDraftCreated(this.sessionId);
          this.patch({ persistent: true, history: "loaded" });
          void queryClient
            .cancelQueries({ queryKey: ["sessions"] }, { revert: false })
            .then(() =>
              queryClient.invalidateQueries({ queryKey: ["sessions"] }),
            );
        }
        return { state: "created" };
      },
      (failure: unknown): SessionOutcome => {
        const message =
          failure instanceof Error ? failure.message : "创建会话结果未知。";
        return failureOutcome(failure) === "rejected"
          ? { state: "rejected", message }
          : { state: "unknown", message };
      },
    );
  }

  /** 编辑／重新生成受理成功后的删除与展示（session-position-contract §2、§3）：
   *  移除目标用户节点及其后的全部展示，编辑展示修改后的消息、重新生成保留目标用户节点及其祖先；
   *  同一操作只执行一次，并推进展示代际使早于删除发起的历史快照失效，避免已删除内容回流。 */
  private acceptExec(
    operation: PendingOperation,
    runId: string,
    requestEntryId: string,
  ): void {
    if (this.appliedDeletions.has(operation.operation_id)) return;
    this.appliedDeletions.add(operation.operation_id);
    this.generation += 1;
    const { kept, target } = pruneFrom(
      this.view.rounds.filter((round) => round.id !== operation.operation_id),
      operation.target_entry_id ?? "",
    );
    const entries: ReActEntry[] =
      operation.kind === "edit"
        ? [
            {
              kind: "user",
              id: operation.operation_id,
              request: operation.request,
              ...attachmentDisplay(operation.attachments),
            },
          ]
        : target !== undefined
          ? [target]
          : [];
    this.setRounds([
      ...kept,
      {
        id: operation.operation_id,
        entries,
        status: "running",
        run_id: runId,
        request_entry_id: requestEntryId,
      },
    ]);
  }

  /** 一次执行流（§5、session-edit-regenerate-contract §5）：按 Content-Type 处理首次 SSE 与重复受理 JSON。
   *  onSettle 仅在明确受理（SSE 响应头或重复受理 JSON）时收到 true，失败或结果未知时不确认，
   *  以便编辑框只在确认受理后关闭。 */
  private beginExec(
    operation: PendingOperation,
    endpoint: string,
    onSettle?: (accepted: boolean) => void,
  ): void {
    const current = {
      controller: new AbortController(),
      operation,
      run_id: undefined as string | undefined,
    };
    this.exec = current;
    this.generation += 1;
    this.stopWatch();
    this.update(operation.operation_id, (round) => resumeReActRound(round));
    this.patch({ busy: true, ready: false });
    const attachments = attachmentInputs(operation.attachments);
    const body: ReActRunBody | EditRunBody | RegenerateRunBody =
      operation.kind === "edit"
        ? {
            session_id: operation.session_id,
            operation_id: operation.operation_id,
            target_entry_id: operation.target_entry_id ?? "",
            request: operation.request,
            ...(operation.legacy_edit ? {} : { attachments }),
          }
        : operation.kind === "regenerate"
          ? {
              session_id: operation.session_id,
              operation_id: operation.operation_id,
              target_entry_id: operation.target_entry_id ?? "",
            }
          : {
              session_id: operation.session_id,
              operation_id: operation.operation_id,
              request: operation.request,
              attachments,
            };
    void runReActStream(
      body,
      (event) => {
        if (this.removed || this.exec !== current) return;
        this.update(operation.operation_id, (round) =>
          applyReActEvent(round, event),
        );
        /* 业务查询刷新只由结构化确认的真实终态触发（确认契约 §7.6）：模型运行不再执行保存。 */
        if (event.event === "done" || event.event === "error") {
          this.exec = null;
          this.patch({ busy: false, ready: true });
          this.setOperations(
            forgetRunOperations(this.sessionId, current.run_id ?? ""),
          );
          if (this.view.persistent)
            this.loadHistory(this.inquiry.signal, "refresh");
        }
      },
      current.controller.signal,
      (headers) => {
        if (this.removed || this.exec !== current) return;
        current.run_id = headers.run_id;
        if (operation.kind === "edit" || operation.kind === "regenerate")
          this.acceptExec(operation, headers.run_id, headers.request_entry_id);
        this.update(operation.operation_id, (round) => ({
          ...round,
          run_id: headers.run_id,
          request_entry_id: headers.request_entry_id,
        }));
        this.setOperations(
          attachOperationRun(
            this.sessionId,
            operation.operation_id,
            headers.run_id,
            headers.request_entry_id,
          ),
        );
        void queryClient.invalidateQueries({ queryKey: ["sessions"] });
        this.patch({ ready: true });
        onSettle?.(true);
      },
      endpoint,
    ).then(
      (repeat) => {
        if (repeat === null || this.exec !== current) return;
        this.exec = null;
        this.patch({ busy: false });
        this.handleRepeat(operation, repeat);
        onSettle?.(true);
      },
      (failure: unknown) => {
        if (this.exec !== current) return;
        this.exec = null;
        this.patch({ busy: false });
        this.handleFailure(
          operation.operation_id,
          operation,
          failure,
          operation.kind === "edit"
            ? "编辑"
            : operation.kind === "regenerate"
              ? "重新生成"
              : "发送",
        );
        onSettle?.(false);
      },
    );
  }

  /** 一次发送（§5、附件契约 §3.1）：先确认会话，再按服务端响应处理执行流；
   *  草稿标题在文本含非空白内容时沿用原文，纯文件消息按附件顺序以换行连接文件名 */
  private dispatchSend(operation: PendingOperation): void {
    const title = draftSessionTitle(operation.request, operation.attachments);
    if (!this.view.persistent) retainDraftTitle(this.sessionId, title);
    void this.ensureSession(title).then((outcome) => {
      if (this.removed) return;
      if (outcome.state === "created") {
        this.beginExec(operation, EXEC_ENDPOINT.send);
        return;
      }
      this.patch({ error: outcome.message });
      if (outcome.state === "rejected") {
        this.setOperations(
          forgetOperation(this.sessionId, operation.operation_id),
        );
        this.update(operation.operation_id, (round) =>
          finishReActRound(resumeReActRound(round), "failed", outcome.message),
        );
        this.patch({ ready: true });
        return;
      }
      this.setOperations(rememberOperation(this.sessionId, operation));
      this.update(operation.operation_id, (round) => ({
        ...withPending(round, operation),
        status: "unknown",
      }));
      this.patch({ ready: false });
      void this.confirmOperation(operation.operation_id, operation);
    });
  }

  /** 一次 Steering 提交（§6.1、附件契约 §3.4）：首次 accepted，重复返回原输入当前持久化状态。
   *  返回 false 表示服务端明确拒绝该输入（如目标运行已 run_closed 停止接收），
   *  此时账本已清理且调用方保留输入草稿；受理或结果未知返回 true，未确认输入走显式重试。 */
  private dispatchSteering(
    roundId: string,
    operation: PendingOperation,
  ): Promise<boolean> {
    const target = operation.run_id;
    if (target === null) {
      this.patch({ error: "缺少 Steering 目标运行。" });
      return Promise.resolve(false);
    }
    const controller = new AbortController();
    this.steering = controller;
    return submitSteering(
      target,
      {
        session_id: operation.session_id,
        operation_id: operation.operation_id,
        message: operation.request,
        attachments: attachmentInputs(operation.attachments),
      },
      controller.signal,
    ).then(
      (received) => {
        if (this.removed || this.steering !== controller) return true;
        this.steering = null;
        this.applySteering(roundId, operation, received);
        return true;
      },
      (failure: unknown) => {
        if (this.removed || this.steering !== controller) return true;
        this.steering = null;
        this.handleFailure(roundId, operation, failure, "Steering 提交");
        return failureOutcome(failure) !== "rejected";
      },
    );
  }

  /** 未确认落地（§5.5）：保留原操作身份并留下显式重试入口；编辑／重新生成在受理前不改变历史展示 */
  private keepUnknown(roundId: string, operation: PendingOperation): void {
    this.setOperations(rememberOperation(this.sessionId, operation));
    if (this.view.rounds.some((round) => round.id === roundId)) {
      this.update(roundId, (round) =>
        operation.kind === "send"
          ? { ...withPending(round, operation), status: "unknown" }
          : withPending(round, operation),
      );
      return;
    }
    this.setRounds([...this.view.rounds, operationRound(operation)]);
  }

  /** 失效操作静默处理（§6.1、§8）：删除本地账本与重试入口、停止该操作查询、重读历史；
   *  不弹该失效操作的错误提示、不恢复或重发旧请求，不影响其他操作与当前新运行。 */
  private expireOperation(roundId: string, operation: PendingOperation): void {
    this.setOperations(forgetOperation(this.sessionId, operation.operation_id));
    this.setRounds(this.view.rounds.filter((round) => round.id !== roundId));
    if (this.watch?.roundId === roundId) this.stopWatch();
    this.patch({ error: undefined, ready: false });
    if (this.view.persistent) this.loadHistory(this.inquiry.signal, "refresh");
  }

  /** 失败落地（§11.5）：4xx 为明确拒绝，其余保留原操作身份与未确认状态 */
  private handleFailure(
    roundId: string,
    operation: PendingOperation,
    failure: unknown,
    label: string,
  ): void {
    if (this.removed) return;
    if (isExpiredOperation(failure)) {
      this.expireOperation(roundId, operation);
      return;
    }
    const message =
      failure instanceof Error ? failure.message : `${label}结果未知。`;
    if (failureOutcome(failure) === "rejected") {
      this.setOperations(
        forgetOperation(this.sessionId, operation.operation_id),
      );
      this.patch({ error: message });
      if (operation.kind === "send") {
        this.update(roundId, (round) =>
          finishReActRound(
            round.status === "unknown" ? resumeReActRound(round) : round,
            "failed",
            message,
          ),
        );
      } else if (operation.kind === "edit" || operation.kind === "regenerate") {
        this.setRounds(
          this.view.rounds.filter((round) => round.id !== roundId),
        );
      }
      this.patch({ ready: true });
      return;
    }
    this.keepUnknown(roundId, operation);
    this.patch({ error: `${label}结果未知，可显式重试。`, ready: false });
    void this.confirmOperation(roundId, operation);
  }

  /** 重复受理（§5、session-edit-regenerate-contract §5.2）：返回原运行当前持久化状态，不启动新的执行 */
  private handleRepeat(
    operation: PendingOperation,
    repeat: SendRunRepeatWire,
  ): void {
    if (this.removed) return;
    if (operation.kind === "edit" || operation.kind === "regenerate")
      this.acceptExec(operation, repeat.run_id, repeat.request_entry_id);
    this.resolveRun(operation.operation_id, operation, repeat);
    if (repeat.status !== "running") return;
    this.update(operation.operation_id, (round) => ({
      ...withPending(round, { ...operation, run_id: repeat.run_id }),
      status: "unknown",
    }));
    this.patch({ ready: false, error: "该操作已受理，运行仍在执行。" });
  }

  // 仅检查新运行，既有运行的 Steering 使用原配置快照。
  private canStartRun(): boolean {
    const configured = hasValidProviderConfig(
      queryClient.getQueryData<ProviderStatusWire>(PROVIDER_QUERY_KEY),
    );
    if (!configured) this.patch({ error: NO_VALID_MODEL_CONFIG });
    return configured;
  }

  /** 编辑用户消息（附件契约 §3.2）：提交编辑后的完整附件集合，保留项为引用、新增或替换项为上传，全部移除为 `[]` */
  editMessage = (
    entryId: string,
    text: string,
    attachments: AttachmentDraft[],
  ): Promise<boolean> => {
    if (
      !this.view.ready ||
      this.view.busy ||
      this.view.confirming ||
      !canSubmitChatInput(text, attachments, []) ||
      !this.canStartRun()
    )
      return Promise.resolve(false);
    this.patch({ error: undefined });
    const operation: PendingOperation = {
      operation_id: crypto.randomUUID(),
      session_id: this.sessionId,
      kind: "edit",
      run_id: null,
      request: text,
      attachments,
      created_at: Date.now(),
      target_entry_id: entryId,
    };
    this.setOperations(rememberOperation(this.sessionId, operation));
    return new Promise<boolean>((resolve) =>
      this.beginExec(operation, EXEC_ENDPOINT.edit, resolve),
    );
  };

  regenerateMessage = (entryId: string): void => {
    if (
      !this.view.ready ||
      this.view.busy ||
      this.view.confirming ||
      !this.canStartRun()
    )
      return;
    this.patch({ error: undefined });
    const operation: PendingOperation = {
      operation_id: crypto.randomUUID(),
      session_id: this.sessionId,
      kind: "regenerate",
      run_id: null,
      request: "",
      attachments: [],
      created_at: Date.now(),
      target_entry_id: entryId,
    };
    this.setOperations(rememberOperation(this.sessionId, operation));
    this.beginExec(operation, EXEC_ENDPOINT.regenerate);
  };

  /** 撤回未消费的 Steering（§6.2）：用历史的 session_id/run_id/steering_id 发起，不依赖本地账本；
   *  消费冲突或结果未知时按真实状态补查（有原操作走操作查询，无原操作走历史查询）。 */
  withdraw = (roundId: string, steeringId: string): void => {
    const round = this.view.rounds.find((item) => item.id === roundId);
    const entry = round?.entries.find(
      (item) => item.id === steeringId && item.kind === "user",
    );
    const target = round?.run_id;
    if (
      round === undefined ||
      entry === undefined ||
      entry.kind !== "user" ||
      entry.steering?.status !== "pending" ||
      target === undefined
    )
      return;
    const operation =
      entry.operation_id === undefined
        ? undefined
        : this.view.operations.find(
            (item) => item.operation_id === entry.operation_id,
          );
    this.patch({ error: undefined });
    void withdrawSteering(
      target,
      steeringId,
      { session_id: this.sessionId },
      this.inquiry.signal,
    ).then(
      (result) => {
        if (this.removed) return;
        this.applySteering(roundId, operation, result);
      },
      (failure: unknown) => {
        if (this.removed) return;
        const conflict =
          failure instanceof ReActHttpError &&
          failure.code === "steering_consumption_conflict";
        if (conflict || failureOutcome(failure) === "unknown") {
          this.patch({
            error: conflict
              ? "Steering 消费已开始。"
              : "撤回结果未知，正在补查状态。",
          });
          this.querySteering(roundId, steeringId, operation);
          return;
        }
        this.patch({
          error: failure instanceof Error ? failure.message : "撤回失败。",
        });
      },
    );
  };

  /** 显式重试（§5.5）：先查询原操作，已受理只关联状态，未受理才沿用原身份与原正文重发 */
  retry = (roundId: string, operationId: string): void => {
    const operation = this.view.operations.find(
      (item) => item.operation_id === operationId,
    );
    if (operation === undefined || this.view.retrying !== undefined) return;
    this.patch({ error: undefined, retrying: operationId });
    void getOperation(this.sessionId, operationId, this.inquiry.signal).then(
      (result) => {
        if (this.removed) return;
        this.patch({ retrying: undefined });
        if (result.accepted) {
          if (result.kind === "steering") {
            if (result.steering !== null)
              this.applySteering(roundId, operation, result.steering);
            return;
          }
          if (result.run !== null)
            this.resolveRun(roundId, operation, result.run);
          return;
        }
        if (operation.kind === "steering")
          void this.dispatchSteering(roundId, operation);
        else if (operation.kind === "send") this.dispatchSend(operation);
        else this.beginExec(operation, EXEC_ENDPOINT[operation.kind]);
      },
      (failure: unknown) => {
        if (this.removed) return;
        this.patch({ retrying: undefined });
        if (isExpiredOperation(failure)) {
          this.expireOperation(roundId, operation);
          return;
        }
        this.patch({ error: "重试查询结果未知，保持未确认。" });
      },
    );
  };

  /** 用户停止（§9.1、§9.2）：关闭该会话活动运行的连接，保留运行身份与占用，由独立查询确认终态 */
  stop = (): void => {
    const run = this.exec;
    if (run === null) return;
    this.exec = null;
    run.controller.abort();
    this.patch({ busy: false, ready: false });
    if (run.run_id !== undefined) {
      this.watchRun(run.operation.operation_id, run.run_id);
      return;
    }
    this.keepUnknown(run.operation.operation_id, run.operation);
    void this.confirmOperation(run.operation.operation_id, run.operation);
  };

  /** 重新读取历史（§5.3）：读取失败保留已有展示，未成功加载前禁止向该会话发送 */
  reload = (): void => {
    if (
      !this.view.persistent ||
      this.view.history === "loading" ||
      this.exec !== null
    )
      return;
    this.stopWatch();
    this.patch({ error: undefined, ready: false });
    this.loadHistory(this.inquiry.signal, "initial");
  };

  /** 输入提交（§5、§6.1、附件契约 §3）：运行中作为 Steering 进入该运行，空闲时作为普通发送；
   *  两者均支持文字与附件及纯附件输入 */
  send = (
    request: string,
    attachments: AttachmentDraft[],
  ): Promise<boolean> => {
    if (
      this.steering !== null ||
      this.watch !== null ||
      !canSubmitChatInput(
        request,
        attachments,
        unknownSteeringKeys(this.view.operations),
      )
    )
      return Promise.resolve(false);
    this.patch({ error: undefined });
    const run = this.exec;
    if (run !== null) {
      if (run.run_id === undefined) return Promise.resolve(false);
      const operation: PendingOperation = {
        operation_id: crypto.randomUUID(),
        session_id: this.sessionId,
        kind: "steering",
        run_id: run.run_id,
        request,
        attachments,
        created_at: Date.now(),
      };
      this.setOperations(rememberOperation(this.sessionId, operation));
      return this.dispatchSteering(run.operation.operation_id, operation);
    }
    if (
      !this.view.ready ||
      this.view.confirming ||
      pendingExecOperation(this.view.operations) !== undefined ||
      !this.canStartRun()
    )
      return Promise.resolve(false);
    const operation: PendingOperation = {
      operation_id: crypto.randomUUID(),
      session_id: this.sessionId,
      kind: "send",
      run_id: null,
      request,
      attachments,
      created_at: Date.now(),
    };
    this.generation += 1;
    this.patch({ ready: false });
    this.setOperations(rememberOperation(this.sessionId, operation));
    this.setRounds([
      ...this.view.rounds,
      {
        id: operation.operation_id,
        entries: [
          {
            kind: "user",
            id: operation.operation_id,
            request,
            ...attachmentDisplay(operation.attachments),
          },
        ],
        status: "running",
      },
    ]);
    this.dispatchSend(operation);
    return Promise.resolve(true);
  };
}

/**
 * 应用级会话运行管理层（backend-http-sse-contract §9.1、session-history-contract §5.1）：
 * 按 session_id 持有运行记录，位于路由之外，生命周期覆盖当前浏览器文档中的全部站内导航。
 * 页面挂载与卸载只影响订阅；仅显式停止与删除会话关闭原执行流。
 */
class SessionRunRegistry {
  private readonly records = new Map<string, SessionRunRecord>();

  /** 取得或创建目标会话的运行记录（§5.1）：返回的记录与订阅函数跨导航保持稳定 */
  open(sessionId: string, isDraft: boolean): SessionRunRecord {
    const existing = this.records.get(sessionId);
    if (existing !== undefined) return existing;
    const created = new SessionRunRecord(sessionId, isDraft);
    this.records.set(sessionId, created);
    return created;
  }

  /** 服务端确认删除成功后的清理（session-delete-contract §5、§7.1）：仅针对目标会话 */
  discard(sessionId: string): void {
    const record = this.records.get(sessionId);
    if (record === undefined) return;
    this.records.delete(sessionId);
    record.dispose();
  }
}

const sessionRuns = new SessionRunRegistry();

/** 删除广播（session-delete-contract §5）：管理层随会话删除释放连接、状态、确认任务与订阅 */
if (typeof window !== "undefined") {
  window.addEventListener(SESSION_DELETED_EVENT, (event) =>
    sessionRuns.discard((event as CustomEvent<string>).detail),
  );
}

export { sessionRuns };
