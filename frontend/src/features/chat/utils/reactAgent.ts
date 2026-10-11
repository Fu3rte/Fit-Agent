import { createParser } from "eventsource-parser";
import type {
  AttachmentInputWire,
  DiscardReasonWire,
  PreparedPlanProposalWire,
  ProfileContentWire,
  ReActContent,
  ReActEvent,
  ReActStopReason,
  RunStatusWire,
  SteeringReceiveStatusWire,
  SteeringStatus,
  SteeringStatusWire,
  WorkoutProposalWire,
} from "@/lib/contract";
import {
  isObject,
  parsePlanBusinessError,
  parsePlanSaveResult,
  parsePlanStatusResult,
  parseProfileSaveResult,
  parseProfileStatusResult,
  parseWorkoutSaveResult,
  parseWorkoutStatusResult,
  preparedPlanAdjustmentProposal,
  preparedPlanImportProposal,
  preparedPlanProposal,
  preparedProfilePayload,
  preparedWorkoutProposal,
} from "@/lib/business";
import type { ToolCallCardProps } from "../components/ToolCallCard";
import {
  attachmentInputs,
  fromAttachmentDraft,
  type AttachmentDraft,
} from "./attachments";

/** 已提交节点身份（§8）：message_end（助手消息）与 tool_result（工具结果）确认后写入 */
interface CommittedNode {
  entry_id: string;
  parent_id: string | null;
}

/** Steering 状态（§6.1、§11.6）：终态来自 SSE 通知、接收、撤回或操作查询，pending 表示受理后尚未取得终态 */
export type SteeringState =
  { status: "pending"; entry_id: null; reason: null } | SteeringStatus;

export type ReActEntry =
  | {
      kind: "user";
      id: string;
      request?: string;
      /** 有序附件（附件契约 §4）：无附件为 `[]`，纯文件消息 `request` 可为空；编辑默认按此集合载入 */
      attachments?: AttachmentDraft[];
      steering?: SteeringState;
      /** 受理该输入的操作身份（§7.1）：状态补查沿用 */
      operation_id?: string;
      /** Steering 跨状态统一身份（§6.1）：pending 快照与已消费节点据此归并 */
      steering_id?: string | null;
      /** 已提交的真实用户节点 ID（§3.2、session-edit-regenerate-contract §2）：编辑与重新生成的可用性依据 */
      entry_id?: string;
    }
  | ({
      kind: "assistant";
      id: string;
      content: ReActContent[];
      stop_reason?: ReActStopReason;
    } & Partial<CommittedNode>)
  /** 业务待确认内容的完整展示（profile-plan §3.2、workout-http-sse-contract §5、plan-generation-contract §6、
   *  plan-import-adjustment-contract §7）：仅在对应准备工具的结果节点提交后携带，实时与历史同一口径 */
  | ({
      kind: "tool";
      id: string;
      profile?: ProfileContentWire;
      workout?: WorkoutProposalWire;
      plan?: PreparedPlanProposalWire;
    } & ToolCallCardProps &
      Partial<CommittedNode>);

/** 结果未知或执行中的操作（§5.5、session-edit-regenerate-contract §8、附件契约 §2）：
 *  保留原 operation_id、所属会话、目标运行、原始正文与完整有序附件集合，显式重试逐字沿用 */
export interface PendingOperation {
  operation_id: string;
  session_id: string;
  kind: "send" | "edit" | "regenerate" | "steering";
  /** 发送未取得 run_id 时为 null，Steering 为目标运行 */
  run_id: string | null;
  request: string;
  attachments: AttachmentDraft[];
  legacy_edit?: true;
  created_at: number;
  /** 受理后服务端确认的请求节点（§11.4）：刷新后据此恢复运行身份 */
  request_entry_id?: string | null;
  /** 已受理 Steering 的稳定输入身份（§6.1）：撤回与状态补查沿用 */
  steering_id?: string | null;
  /** 编辑／重新生成的目标用户节点（session-edit-regenerate-contract §6）：受理删除与重试定位沿用 */
  target_entry_id?: string | null;
}

/** 本地草稿（§5.1）：尚未成功创建的会话，使用保留 UUID 与首条请求原文 */
export interface ChatDraft {
  session_id: string;
  pending_title: string | null;
}

/** 客户端持久化状态（§5.1）：当前选择、本地草稿与按会话归属的未确认操作账本 */
export interface ChatStore {
  selected_session_id: string | null;
  draft: ChatDraft | null;
  ledgers: Record<string, PendingOperation[]>;
}

export interface ReActRound {
  id: string;
  run_id?: string;
  /** 本次运行依据的用户节点（§8）：由 SSE 响应头或重复受理结果确认 */
  request_entry_id?: string;
  entries: ReActEntry[];
  error?: string;
  status:
    | "running"
    | "completed"
    | "failed"
    | "cancelled"
    | "interrupted"
    | "unknown";
  /** 进行中的压缩阶段身份（compaction-contract-decisions §B）：仅由当前连接的 compaction_start 得知，
   *  compaction_end 或运行终态清除；刷新后不猜测此前阶段 */
  compaction_id?: string;
  /** 结果未知、等待确认或显式重试的操作（§5.5） */
  pending?: PendingOperation[];
}

export function finishReActRound(
  round: ReActRound,
  status: "failed" | "cancelled" | "interrupted",
  error?: string,
  toolId?: string | null,
): ReActRound {
  if (round.status !== "running") return round;
  return {
    ...round,
    status,
    error,
    // 终态收尾清除压缩阶段：失败或取消允许没有 compaction_end（compaction-contract-decisions §B）
    compaction_id: undefined,
    entries: round.entries.map((entry) => {
      if (entry.kind !== "tool" || entry.status !== "running") return entry;
      const failed =
        status === "failed" && (toolId == null || entry.id === toolId);
      return {
        ...entry,
        status: failed ? "failed" : "cancelled",
        ...(failed ? { error } : {}),
      };
    }),
  };
}

/** 已取得终态（§11.6）：pending 可被终态覆盖，终态不再改写 */
function settledSteering(state: SteeringState | undefined): boolean {
  return state !== undefined && state.status !== "pending";
}

/** 一次 Steering 输入的状态快照（§6.1、§6.2、§7.1）：接收、撤回与操作查询共用同一形状 */
interface SteeringSnapshot {
  run_id: string;
  steering_id: string;
  status: SteeringReceiveStatusWire;
  entry_id: string | null;
  reason: DiscardReasonWire | null;
  /** 仅接收响应提供；撤回与查询结果沿用条目已保存的原操作身份 */
  operation_id?: string;
}

/**
 * Steering 状态落地（§11.6）：按 steering_id 去重，首次 accepted 与重复 pending 收敛为待确认；
 * SSE 状态先到时按 steering_id 缓存，后到的 accepted／pending 保持已确认的终态。
 */
export function applySteeringStatus(
  round: ReActRound,
  received: SteeringSnapshot,
  request?: string,
  attachments?: AttachmentDraft[],
): ReActRound {
  if (round.run_id !== received.run_id)
    throw new Error("Steering 运行身份不匹配。");
  const index = round.entries.findIndex(
    (entry) => entry.id === received.steering_id,
  );
  const found: ReActEntry | undefined = round.entries[index];
  if (found !== undefined && found.kind !== "user")
    throw new Error("Steering 标识冲突。");
  if (
    found !== undefined &&
    found.request !== undefined &&
    request !== undefined &&
    found.request !== request
  )
    throw new Error("重复 Steering 接受记录。");
  const next =
    received.status === "accepted"
      ? steeringStatus("pending", null, null)
      : steeringStatus(received.status, received.entry_id, received.reason);
  const state: SteeringState =
    found !== undefined &&
    found.steering !== undefined &&
    settledSteering(found.steering)
      ? found.steering
      : next;
  const nodeEntry =
    state.status === "consumed" ? { entry_id: state.entry_id } : {};
  const displayed = attachmentDisplay(attachments ?? found?.attachments);
  if (found === undefined)
    return {
      ...round,
      entries: [
        ...round.entries,
        {
          kind: "user",
          id: received.steering_id,
          request,
          ...displayed,
          steering: state,
          ...nodeEntry,
          operation_id: received.operation_id,
          steering_id: received.steering_id,
        },
      ],
    };
  return {
    ...round,
    entries: round.entries.map((item, at) =>
      at !== index
        ? item
        : {
            ...found,
            request: request ?? found.request,
            ...displayed,
            steering: state,
            ...nodeEntry,
            operation_id: received.operation_id ?? found.operation_id,
            steering_id: received.steering_id,
          },
    ),
  };
}

/** 附件展示字段：无附件的文本输入保持字段缺省，避免与已受理集合混淆 */
export function attachmentDisplay(drafts: AttachmentDraft[] | undefined) {
  return drafts === undefined || drafts.length === 0
    ? {}
    : { attachments: drafts };
}

/* ===== 会话选择、本地草稿与操作账本（session-history-contract §5.1、§5.5）===== */

const CHAT_CLIENT_KEY = "fit-agent:chat-client";

function createDraft(): ChatDraft {
  return { session_id: crypto.randomUUID(), pending_title: null };
}

/** 失败分类（§11.5）：4xx 为服务端明确拒绝；网络中断、5xx 与协议解析失败结果未知，
 * 保留操作身份并通过操作查询确认。 */
export function failureOutcome(failure: unknown): "rejected" | "unknown" {
  return failure instanceof ReActHttpError &&
    failure.http_status !== null &&
    failure.http_status >= 400 &&
    failure.http_status < 500
    ? "rejected"
    : "unknown";
}

function rememberPending(
  pending: PendingOperation[] | undefined,
  operation: PendingOperation,
): PendingOperation[] {
  const current = pending ?? [];
  return current.some((item) => item.operation_id === operation.operation_id)
    ? current
    : [...current, operation];
}

/** 登记未确认操作（§3、§6.1）：保留原请求与操作身份，等待查询确认或显式重试 */
export function withPending(
  round: ReActRound,
  operation: PendingOperation,
): ReActRound {
  return { ...round, pending: rememberPending(round.pending, operation) };
}

/** 受理已确认（§5.5）：移除对应未确认操作 */
export function resolveReActPending(
  round: ReActRound,
  operationId: string,
): ReActRound {
  const pending = (round.pending ?? []).filter(
    (item) => item.operation_id !== operationId,
  );
  return { ...round, pending: pending.length > 0 ? pending : undefined };
}

/** 显式重试（§5.5）：沿用原操作身份回到执行中，禁止生成新键 */
export function resumeReActRound(round: ReActRound): ReActRound {
  return { ...round, status: "running", error: undefined, pending: undefined };
}

/** 运行状态收敛（§7.2、§5.3）：running 保持未确认；中断形成独立终态，其余按运行状态展示 */
export function concludeReActRound(
  round: ReActRound,
  status: RunStatusWire,
  message?: string,
): ReActRound {
  if (round.status !== "running" && round.status !== "unknown") return round;
  if (status === "running") return round;
  if (status === "completed") return { ...round, status: "completed" };
  if (status === "interrupted")
    return finishReActRound(
      { ...round, status: "running" },
      "interrupted",
      message ?? "运行已中断。",
    );
  return finishReActRound(
    { ...round, status: "running" },
    status === "cancelled" ? "cancelled" : "failed",
    message,
  );
}

/** Steering 状态字段组合（§6.1、§11.6）：接收、撤回、SSE 通知与操作查询共用同一判别 */
export function steeringStatus(
  status: SteeringStatusWire,
  entryId: string | null,
  reason: DiscardReasonWire | null,
): SteeringState {
  if (status === "pending") {
    if (entryId !== null || reason !== null)
      throw new Error("Steering 待确认状态无效。");
    return { status: "pending", entry_id: null, reason: null };
  }
  if (status === "consumed") {
    if (entryId === null || reason !== null)
      throw new Error("Steering 消费状态无效。");
    return { status: "consumed", entry_id: entryId, reason: null };
  }
  if (status === "withdrawn") {
    if (entryId !== null || reason !== null)
      throw new Error("Steering 撤回状态无效。");
    return { status: "withdrawn", entry_id: null, reason: null };
  }
  if (entryId !== null || reason === null)
    throw new Error("Steering 丢弃状态无效。");
  return { status: "discarded", entry_id: null, reason };
}

function parsePendingOperation(value: unknown): PendingOperation {
  if (!object(value) || !uuid(value.operation_id) || !uuid(value.session_id))
    throw new Error("操作账本身份无效。");
  if (
    value.kind !== "send" &&
    value.kind !== "edit" &&
    value.kind !== "regenerate" &&
    value.kind !== "steering"
  )
    throw new Error("操作账本类型无效。");
  if (!(value.run_id === null || uuid(value.run_id)))
    throw new Error("操作账本运行身份无效。");
  if (!(
    value.request_entry_id === undefined ||
    value.request_entry_id === null ||
    uuid(value.request_entry_id)
  ))
    throw new Error("操作账本请求节点无效。");
  if (!(
    value.steering_id === undefined ||
    value.steering_id === null ||
    uuid(value.steering_id)
  ))
    throw new Error("操作账本输入身份无效。");
  if (!(
    value.target_entry_id === undefined ||
    value.target_entry_id === null ||
    uuid(value.target_entry_id)
  ))
    throw new Error("操作账本目标节点无效。");
  if (
    typeof value.request !== "string" ||
    !Number.isSafeInteger(value.created_at)
  )
    throw new Error("操作账本正文无效。");
  // 旧版纯文本账本迁移为空附件；旧编辑重试保留原请求的缺省字段语义。
  const legacy = !Object.hasOwn(value, "attachments");
  if (legacy && value.request.trim() === "") throw new Error("操作账本正文无效。");
  const attachments = legacy ? [] : value.attachments;
  if (!Array.isArray(attachments)) throw new Error("操作账本附件无效。");
  if (value.legacy_edit !== undefined &&
      (value.legacy_edit !== true || value.kind !== "edit" || attachments.length !== 0))
    throw new Error("操作账本编辑版本无效。");
  return {
    operation_id: value.operation_id,
    session_id: value.session_id,
    kind: value.kind,
    run_id: value.run_id as string | null,
    request: value.request,
    attachments: attachments.map(fromAttachmentDraft),
    ...((legacy && value.kind === "edit") || value.legacy_edit === true
      ? { legacy_edit: true as const } : {}),
    created_at: value.created_at as number,
    request_entry_id: (value.request_entry_id ?? null) as string | null,
    steering_id: (value.steering_id ?? null) as string | null,
    target_entry_id: (value.target_entry_id ?? null) as string | null,
  };
}

function parseDraft(value: unknown): ChatDraft {
  if (
    !object(value) ||
    !uuid(value.session_id) ||
    !(value.pending_title === null || typeof value.pending_title === "string")
  )
    throw new Error("本地草稿无效。");
  return {
    session_id: value.session_id,
    pending_title: value.pending_title as string | null,
  };
}

function parseLedgers(value: unknown): Record<string, PendingOperation[]> {
  if (!object(value)) throw new Error("会话账本无效。");
  const ledgers: Record<string, PendingOperation[]> = {};
  for (const [sessionId, list] of Object.entries(value)) {
    if (!uuid(sessionId) || !Array.isArray(list))
      throw new Error("会话账本无效。");
    ledgers[sessionId] = list.map(parsePendingOperation);
  }
  return ledgers;
}

function parseChatStore(value: unknown): ChatStore {
  if (
    !object(value) ||
    !(value.selected_session_id === null || uuid(value.selected_session_id))
  )
    throw new Error("会话状态无效。");
  return {
    selected_session_id: value.selected_session_id as string | null,
    draft: value.draft === null ? null : parseDraft(value.draft),
    ledgers: parseLedgers(value.ledgers),
  };
}

function writeChatStore(store: ChatStore): void {
  if (typeof localStorage === "undefined") return;
  localStorage.setItem(CHAT_CLIENT_KEY, JSON.stringify(store));
}

/** 读取持久化客户端状态（§5.1）：无存储时新建草稿并落盘 */
export function loadChatStore(): ChatStore {
  if (typeof localStorage === "undefined")
    return { selected_session_id: null, draft: createDraft(), ledgers: {} };
  const raw = localStorage.getItem(CHAT_CLIENT_KEY);
  if (raw === null) {
    const fresh: ChatStore = {
      selected_session_id: null,
      draft: createDraft(),
      ledgers: {},
    };
    writeChatStore(fresh);
    return fresh;
  }
  const store = parseChatStore(JSON.parse(raw));
  writeChatStore(store);
  return store;
}

/** 读取指定会话的未确认账本（§5.2） */
export function readLedger(sessionId: string): PendingOperation[] {
  return loadChatStore().ledgers[sessionId] ?? [];
}

/**
 * 变更指定会话的未确认账本并落盘（§5.3、§5.5）：只影响该会话账本，
 * 当前选择与其它会话账本（含尚未创建的草稿）保持原值。
 */
export function updateLedger(
  sessionId: string,
  change: (operations: PendingOperation[]) => PendingOperation[],
): PendingOperation[] {
  const store = loadChatStore();
  const next = change(store.ledgers[sessionId] ?? []);
  const ledgers = { ...store.ledgers };
  if (next.length === 0) delete ledgers[sessionId];
  else ledgers[sessionId] = next;
  writeChatStore({ ...store, ledgers });
  return next;
}

/** 生成并登记一次操作（§5.5）：同 operation_id 已登记时保持原记录 */
export function rememberOperation(
  sessionId: string,
  operation: PendingOperation,
): PendingOperation[] {
  return updateLedger(sessionId, (operations) =>
    operations.some((item) => item.operation_id === operation.operation_id)
      ? operations
      : [...operations, operation],
  );
}

/** 受理结果已确认（§5.5）：操作离开未确认集合 */
export function forgetOperation(
  sessionId: string,
  operationId: string,
): PendingOperation[] {
  return updateLedger(sessionId, (operations) =>
    operations.filter((item) => item.operation_id !== operationId),
  );
}

/** 运行终态确认（§7.2、§9.2）：清理该运行关联的全部未确认操作 */
export function forgetRunOperations(
  sessionId: string,
  runId: string,
): PendingOperation[] {
  return updateLedger(sessionId, (operations) =>
    operations.filter((item) => item.run_id !== runId),
  );
}

/** 记录确认后的所属运行与请求节点（§7.1、§11.4）：刷新后据此恢复运行身份 */
export function attachOperationRun(
  sessionId: string,
  operationId: string,
  runId: string,
  requestEntryId?: string | null,
): PendingOperation[] {
  return updateLedger(sessionId, (operations) =>
    operations.map((item) =>
      item.operation_id === operationId
        ? {
            ...item,
            run_id: runId,
            request_entry_id: requestEntryId ?? item.request_entry_id ?? null,
          }
        : item,
    ),
  );
}

/** 文本与附件的受理条件（附件契约 §1、§3.1、§3.4）：文本最多 32000 个 Unicode 字符并保留原值，
 *  空白或缺省文本要求附件集合非空 */
export function validMessageInput(
  text: string,
  attachments: readonly unknown[],
): boolean {
  return (
    Array.from(text).length <= 32000 &&
    (!!text.trim() || attachments.length > 0)
  );
}

/** 未确认输入的身份（§5.5）：同一文本与同一有序附件集合视为同一输入，只能显式重试原操作 */
export function messageInputKey(
  text: string,
  attachments: readonly AttachmentInputWire[],
): string {
  return JSON.stringify([
    text,
    attachments.map((item) => [item.kind, item.attachment_id]),
  ]);
}

/** 未确认的 Steering 输入身份（§5.5）：同身份输入必须经显式重试，不得作为新操作重发 */
export function unknownSteeringKeys(operations: PendingOperation[]): string[] {
  return operations
    .filter((item) => item.kind === "steering")
    .map((item) =>
      messageInputKey(item.request, attachmentInputs(item.attachments)),
    );
}

export function pendingExecOperation(
  operations: PendingOperation[],
): PendingOperation | undefined {
  return operations.find((item) => item.kind !== "steering");
}

/** 记录当前选择（§5.1）：URL 身份优先，首页据此恢复上次选择 */
export function selectSession(sessionId: string): void {
  writeChatStore({ ...loadChatStore(), selected_session_id: sessionId });
}

export const SESSION_DELETED_EVENT = "session-deleted";

/** 服务端确认删除后的本地清理（session-delete-contract §5）：移除目标账本与重试请求，选择指向目标时清除，防止首页恢复已删除身份 */
export function forgetSession(sessionId: string): void {
  window.dispatchEvent(
    new CustomEvent(SESSION_DELETED_EVENT, { detail: sessionId }),
  );
  const store = loadChatStore();
  delete store.ledgers[sessionId];
  if (store.selected_session_id === sessionId) store.selected_session_id = null;
  writeChatStore(store);
}

/** 新建会话（§5.1）：生成新 UUID 草稿并清空选择；旧会话账本按原 session_id 保留 */
export function startNewSession(): ChatStore {
  const store = loadChatStore();
  const next: ChatStore = {
    ...store,
    selected_session_id: null,
    draft: createDraft(),
  };
  writeChatStore(next);
  return next;
}

/** 首页空白流程（§5.1）：无草稿时补一个保留 UUID */
export function ensureDraft(): ChatStore {
  const store = loadChatStore();
  if (store.draft !== null) return store;
  const next: ChatStore = { ...store, draft: createDraft() };
  writeChatStore(next);
  return next;
}

/** 保留首条请求原文作为草稿标题（§4）：仅归属当前草稿，已保留时保持原值 */
export function retainDraftTitle(sessionId: string, title: string): ChatStore {
  const store = loadChatStore();
  if (
    store.draft === null ||
    store.draft.session_id !== sessionId ||
    store.draft.pending_title !== null
  )
    return store;
  const next: ChatStore = {
    ...store,
    draft: { ...store.draft, pending_title: title },
  };
  writeChatStore(next);
  return next;
}

/** 草稿创建成功（§4）：清除该草稿身份；仅在首页未显式选择其他会话时推进当前选择 */
export function confirmDraftCreated(sessionId: string): ChatStore {
  const store = loadChatStore();
  if (store.draft === null || store.draft.session_id !== sessionId)
    return store;
  const next: ChatStore = {
    ...store,
    draft: null,
    selected_session_id: store.selected_session_id ?? sessionId,
  };
  writeChatStore(next);
  return next;
}

export function applyReActEvent(
  round: ReActRound,
  event: ReActEvent,
): ReActRound {
  if (round.status !== "running") throw new Error("终止后收到事件。");
  if (round.run_id !== event.data.run_id) throw new Error("运行身份不匹配。");
  const entries = [...round.entries];
  const data = event.data;
  switch (event.event) {
    case "message_start":
      if (
        entries.some((entry) => entry.id === event.data.message_id) ||
        entries.some(
          (entry) => entry.kind === "assistant" && !entry.stop_reason,
        )
      )
        throw new Error("重复或重叠的助手消息。");
      entries.push({
        kind: "assistant",
        id: event.data.message_id,
        content: event.data.content,
      });
      break;
    case "message_update":
    case "message_end": {
      const index = entries.findIndex(
        (entry) => entry.id === event.data.message_id,
      );
      const entry = entries[index];
      if (!entry || entry.kind !== "assistant" || entry.stop_reason)
        throw new Error("助手消息生命周期无效。");
      if (event.event === "message_update") {
        const { content_index, update_type, content } = event.data;
        const kind = update_type.split("_")[0];
        const block = content.find(
          (item) => item.content_index === content_index,
        );
        if (!block || block.type !== (kind === "toolcall" ? "tool_call" : kind))
          throw new Error("内容块类型不匹配。");
        for (const old of entry.content) {
          const next = content.find(
            (item) => item.content_index === old.content_index,
          );
          if (
            !next ||
            next.type !== old.type ||
            (old.type === "tool_call" &&
              next.type === "tool_call" &&
              next.tool_call_id !== old.tool_call_id)
          )
            throw new Error("内容块身份变化。");
        }
      } else {
        const reason = event.data.stop_reason;
        if (
          (reason === "length" || reason === "aborted") &&
          event.data.content.some((block) => block.type === "tool_call")
        )
          throw new Error("截断或取消消息包含工具调用。");
      }
      entries[index] = {
        ...entry,
        content: event.data.content,
        ...(event.event === "message_end"
          ? {
              stop_reason: event.data.stop_reason,
              entry_id: event.data.entry_id,
              parent_id: event.data.parent_id,
            }
          : {}),
      };
      break;
    }
    case "tool_start": {
      const { tool_call_id, name, arguments: args } = event.data;
      if (entries.some((entry) => entry.id === tool_call_id))
        throw new Error("重复工具执行。");
      const assistant = entries
        .filter((entry) => entry.kind === "assistant")
        .at(-1);
      if (
        assistant?.kind !== "assistant" ||
        assistant.stop_reason !== "toolUse" ||
        !assistant.content.some(
          (block) =>
            block.type === "tool_call" &&
            block.tool_call_id === tool_call_id &&
            block.name === name,
        )
      )
        throw new Error("工具执行缺少最终模型调用。");
      entries.push({
        kind: "tool",
        id: tool_call_id,
        name,
        arguments: args,
        status: "running",
      });
      break;
    }
    case "tool_execution_update": {
      const index = entries.findIndex(
        (entry) => entry.id === event.data.tool_call_id,
      );
      const entry = entries[index];
      // 中间态快照：完整替换内容；执行结束后的迟到或无关更新忽略，最终结果由完成/保存事件覆盖。
      if (
        !entry ||
        entry.kind !== "tool" ||
        entry.status !== "running" ||
        entry.name !== event.data.tool_name
      )
        break;
      entries[index] = { ...entry, content: event.data.content };
      break;
    }
    case "tool_execution_end": {
      const index = entries.findIndex(
        (entry) => entry.id === event.data.tool_call_id,
      );
      const entry = entries[index];
      if (
        !entry ||
        entry.kind !== "tool" ||
        entry.status !== "running" ||
        entry.name !== event.data.tool_name
      )
        throw new Error("工具执行完成缺少调用或重复返回。");
      entries[index] = {
        ...entry,
        content: event.data.content,
        status: event.data.is_error ? "failed" : "completed",
      };
      break;
    }
    case "tool_result": {
      const index = entries.findIndex(
        (entry) => entry.id === event.data.tool_call_id,
      );
      const entry = entries[index];
      if (!entry || entry.kind !== "tool" || entry.entry_id !== undefined)
        throw new Error("工具结果缺少调用或重复返回。");
      if (event.data.is_error)
        assertPlanBusinessError(entry.name, event.data.content);
      const displayed =
        !event.data.is_error && entry.name === PREPARE_PROFILE
          ? { profile: preparedProfilePayload(event.data.content) }
          : !event.data.is_error && entry.name === PREPARE_WORKOUT
            ? { workout: preparedWorkoutProposal(event.data.content) }
            : !event.data.is_error
              ? (preparedPlanDisplay(entry.name, event.data.content) ?? {})
              : {};
      entries[index] = {
        ...entry,
        content: event.data.content,
        status: event.data.is_error ? "failed" : "completed",
        entry_id: event.data.entry_id,
        parent_id: event.data.parent_id,
        ...displayed,
      };
      break;
    }
    /* 压缩阶段（compaction-contract-decisions §B）：开始事件登记阶段并保持 running，完成事件在检查点提交后清除；
     * 重复开始、缺失开始的完成、compaction_id 与进行中阶段不一致均按协议异常处理。 */
    case "compaction_start": {
      if (round.compaction_id !== undefined)
        throw new Error("重复的压缩开始事件。");
      return { ...round, compaction_id: event.data.compaction_id };
    }
    case "compaction_end": {
      if (round.compaction_id !== event.data.compaction_id)
        throw new Error("压缩完成事件与进行中的阶段不匹配。");
      return { ...round, compaction_id: undefined };
    }
    case "steering_status": {
      const existing: ReActEntry | undefined = entries.find(
        (entry) => entry.id === event.data.steering_id,
      );
      if (existing !== undefined && existing.kind !== "user")
        throw new Error("Steering 标识冲突。");
      return applySteeringStatus(round, event.data);
    }
    case "done": {
      // 压缩进行中收到完成事件视为协议异常：关闭连接保留已有内容，由运行查询确认真实终态
      if (round.compaction_id !== undefined)
        throw new Error("压缩阶段未结束时收到完成事件。");
      const assistants = entries.filter((entry) => entry.kind === "assistant");
      if (
        !assistants.length ||
        assistants.some((entry) => !entry.stop_reason) ||
        assistants.at(-1)?.stop_reason !== event.data.stop_reason ||
        entries.some(
          (entry) => entry.kind === "tool" && entry.entry_id === undefined,
        ) ||
        entries.some(
          (entry) =>
            entry.kind === "user" &&
            entry.steering !== undefined &&
            !settledSteering(entry.steering),
        )
      )
        throw new Error("运行未完成。");
      return { ...round, status: "completed" };
    }
    case "error":
      if (
        event.data.tool_call_id !== null &&
        !entries.some(
          (entry) =>
            entry.kind === "tool" &&
            entry.id === event.data.tool_call_id &&
            entry.entry_id === undefined,
        )
      )
        throw new Error("错误关联工具无效。");
      return finishReActRound(
        round,
        event.data.status,
        event.data.message,
        event.data.tool_call_id,
      );
  }
  return { ...round, entries, run_id: data.run_id };
}

/* ===== 业务快照的展示与保存落定（profile-natural-confirmation-plan §3.2、§9；workout-record-contract §1；plan-generation-contract §4、§6）===== */

/** 三个画像业务工具的固定名称（profile-plan §4） */
export const PREPARE_PROFILE = "prepare_profile_update";
const SAVE_PROFILE = "save_profile_update";
const PROFILE_UPDATE_STATUS = "get_profile_update_status";

/** 训练记录工具的固定名称（workout-record-contract §5） */
export const PREPARE_WORKOUT = "prepare_workout";
const SAVE_WORKOUT = "save_workout";
const UPDATE_WORKOUT = "update_workout";
const WORKOUT_SAVE_STATUS = "get_workout_save_status";

/** 计划工具的固定名称（plan-generation-contract §4、plan-import-adjustment-contract §7） */
export const PREPARE_PLAN = "prepare_plan";
export const PREPARE_PLAN_IMPORT = "prepare_plan_import";
export const PREPARE_PLAN_ADJUSTMENT = "prepare_plan_adjustment";
const SAVE_PLAN = "save_plan";
const PLAN_SAVE_STATUS = "get_plan_save_status";
const PLAN_BUSINESS_TOOLS = [
  "get_current_plan",
  "get_plan",
  "list_plans",
  PREPARE_PLAN,
  PREPARE_PLAN_IMPORT,
  PREPARE_PLAN_ADJUSTMENT,
  SAVE_PLAN,
  PLAN_SAVE_STATUS,
];

/**
 * 计划准备结果的展示投影（附件契约 §7、plan-generation-contract §6）：按实际工具名选择对应 schema，
 * 生成快照要求生成完整度，录入与调整快照使用通用内容完整度并携带后端确定的准备类型；
 * 实时 `tool_result` 与历史 `toolResult` 共用本口径，非准备工具返回 null 走通用工具展示。
 */
export function preparedPlanDisplay(
  name: string,
  content: string,
): { plan: PreparedPlanProposalWire } | null {
  if (name === PREPARE_PLAN) return { plan: preparedPlanProposal(content) };
  if (name === PREPARE_PLAN_IMPORT)
    return { plan: preparedPlanImportProposal(content) };
  if (name === PREPARE_PLAN_ADJUSTMENT)
    return { plan: preparedPlanAdjustmentProposal(content) };
  return null;
}

/**
 * 计划工具的失败结果（契约 §8）：业务失败与内容校验错误以 JSON 对象返回，在解析位置严格校验；
 * 参数预处理、参数校验、权限拒绝、执行与后处理失败是 harness 的说明文本，原样进入通用工具展示。
 */
export function assertPlanBusinessError(name: string, content: string): void {
  if (!PLAN_BUSINESS_TOOLS.includes(name)) return;
  let value: unknown;
  try {
    value = JSON.parse(content) as unknown;
  } catch {
    return;
  }
  if (isObject(value)) parsePlanBusinessError(value);
}

/** 保存落定的判定口径：保存工具名集合、状态查询工具名与两类结果的 schema 解析 */
interface SaveVerification {
  saveTools: readonly string[];
  statusTool: string;
  saveProposalId: (value: unknown) => string;
  statusResult: (value: unknown) => { proposal_id: string; saved: boolean };
}

const PROFILE_SAVE: SaveVerification = {
  saveTools: [SAVE_PROFILE],
  statusTool: PROFILE_UPDATE_STATUS,
  saveProposalId: (value) => parseProfileSaveResult(value).proposal_id,
  statusResult: (value) => {
    const status = parseProfileStatusResult(value);
    return { proposal_id: status.proposal_id, saved: status.status === "saved" };
  },
};

const WORKOUT_SAVE: SaveVerification = {
  saveTools: [SAVE_WORKOUT, UPDATE_WORKOUT],
  statusTool: WORKOUT_SAVE_STATUS,
  saveProposalId: (value) => parseWorkoutSaveResult(value).proposal_id,
  statusResult: (value) => {
    const status = parseWorkoutStatusResult(value);
    return { proposal_id: status.proposal_id, saved: status.status === "saved" };
  },
};

const PLAN_SAVE: SaveVerification = {
  saveTools: [SAVE_PLAN],
  statusTool: PLAN_SAVE_STATUS,
  saveProposalId: (value) => parsePlanSaveResult(value).proposal_id,
  statusResult: (value) => {
    const status = parsePlanStatusResult(value);
    return { proposal_id: status.proposal_id, saved: status.status === "saved" };
  },
};

/**
 * 保存落定（§6、§9.1）：仅保存工具的成功结果或状态查询核实 saved，且快照标识与本次调用参数一致。
 * 结果不合 schema 在解析位置报错；进度与执行完成事件不作为凭据，由 sessionRunManager 据此刷新查询缓存。
 */
function proposalSaved(
  event: ReActEvent,
  round: ReActRound,
  verification: SaveVerification,
): boolean {
  if (event.event !== "tool_result" || event.data.is_error) return false;
  const entry = round.entries.find(
    (item) => item.kind === "tool" && item.id === event.data.tool_call_id,
  );
  if (
    entry?.kind !== "tool" ||
    (!verification.saveTools.includes(entry.name) &&
      entry.name !== verification.statusTool)
  )
    return false;
  const value = JSON.parse(event.data.content) as unknown;
  const proposalId = entry.arguments.proposal_id;
  if (verification.saveTools.includes(entry.name))
    return verification.saveProposalId(value) === proposalId;
  const status = verification.statusResult(value);
  return status.saved && status.proposal_id === proposalId;
}

export function profileSaved(event: ReActEvent, round: ReActRound): boolean {
  return proposalSaved(event, round, PROFILE_SAVE);
}

export function workoutSaved(event: ReActEvent, round: ReActRound): boolean {
  return proposalSaved(event, round, WORKOUT_SAVE);
}

export function planSaved(event: ReActEvent, round: ReActRound): boolean {
  return proposalSaved(event, round, PLAN_SAVE);
}

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
const uuid = (value: unknown): value is string =>
  typeof value === "string" &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
const stopReasons = ["stop", "toolUse", "length", "error", "aborted"];
const discardReasons: readonly DiscardReasonWire[] = [
  "completed",
  "failed",
  "cancelled",
  "interrupted",
];
const updateTypes = [
  "text_start",
  "text_delta",
  "text_end",
  "thinking_start",
  "thinking_delta",
  "thinking_end",
  "toolcall_start",
  "toolcall_delta",
  "toolcall_end",
];

function validateContent(value: unknown): void {
  if (!Array.isArray(value)) throw new Error("消息内容无效。");
  let previous = -1;
  for (const block of value) {
    if (
      !object(block) ||
      !Number.isSafeInteger(block.content_index) ||
      (block.content_index as number) <= previous
    )
      throw new Error("内容块索引无效。");
    previous = block.content_index as number;
    const keys = ["content_index", "type"];
    if (block.type === "text" && typeof block.text === "string")
      keys.push("text");
    else if (block.type === "thinking" && typeof block.thinking === "string")
      keys.push("thinking");
    else if (
      block.type === "tool_call" &&
      typeof block.tool_call_id === "string" &&
      block.tool_call_id &&
      typeof block.name === "string" &&
      block.name &&
      object(block.arguments)
    )
      keys.push("tool_call_id", "name", "arguments");
    else throw new Error("内容块字段无效。");
    if (Object.keys(block).some((key) => !keys.includes(key)))
      throw new Error("未知内容块字段。");
  }
}

export function createReActParser(
  onEvent: (event: ReActEvent) => void,
  runId: string,
) {
  if (!uuid(runId)) throw new Error("运行身份无效。");
  let terminal = false;
  const parser = createParser({
    onError(error) {
      throw error;
    },
    onEvent(message) {
      if (terminal) throw new Error("终止后收到事件。");
      const data: unknown = JSON.parse(message.data);
      if (!object(data) || data.run_id !== runId)
        throw new Error("事件运行身份无效。");
      const string = (key: string) => typeof data[key] === "string";
      switch (message.event) {
        case "message_start":
        case "message_update":
        case "message_end":
          if (!uuid(data.message_id)) throw new Error("消息身份无效。");
          validateContent(data.content);
          if (
            message.event === "message_update" &&
            (!Number.isSafeInteger(data.content_index) ||
              !updateTypes.includes(data.update_type as string) ||
              !(data.content as ReActContent[]).some(
                (block) => block.content_index === data.content_index,
              ))
          )
            throw new Error("消息更新无效。");
          if (
            message.event === "message_end" &&
            (!stopReasons.includes(data.stop_reason as string) ||
              !uuid(data.entry_id) ||
              data.entry_id !== data.message_id ||
              !(data.parent_id === null || uuid(data.parent_id)))
          )
            throw new Error("消息结束事件无效。");
          break;
        case "tool_start":
          if (
            !string("tool_call_id") ||
            !data.tool_call_id ||
            !string("name") ||
            !data.name ||
            !object(data.arguments)
          )
            throw new Error("工具调用无效。");
          break;
        case "tool_execution_update":
          if (
            !string("tool_call_id") ||
            !data.tool_call_id ||
            !string("tool_name") ||
            !data.tool_name ||
            typeof data.content !== "string" ||
            typeof data.is_error !== "boolean"
          )
            throw new Error("工具进度事件无效。");
          break;
        case "tool_execution_end":
          if (
            !string("tool_call_id") ||
            !data.tool_call_id ||
            !string("tool_name") ||
            !data.tool_name ||
            typeof data.content !== "string" ||
            typeof data.is_error !== "boolean"
          )
            throw new Error("工具执行完成事件无效。");
          break;
        case "tool_result":
          if (
            !string("tool_call_id") ||
            !string("content") ||
            typeof data.is_error !== "boolean" ||
            !uuid(data.entry_id) ||
            !(data.parent_id === null || uuid(data.parent_id))
          )
            throw new Error("工具结果无效。");
          break;
        /* 压缩事件（§B）：compaction_id 为该次压缩提交节点的 UUID，reason 为触发原因；
         * compaction_end 的 compaction_id 与 entry_id 一致，parent_id 为必填非 null UUID。 */
        case "compaction_start":
          if (
            !uuid(data.compaction_id) ||
            !(data.reason === "threshold" || data.reason === "overflow")
          )
            throw new Error("压缩开始事件无效。");
          break;
        case "compaction_end":
          if (
            !uuid(data.compaction_id) ||
            !uuid(data.entry_id) ||
            data.entry_id !== data.compaction_id ||
            !uuid(data.parent_id)
          )
            throw new Error("压缩完成事件无效。");
          break;
        case "steering_status":
          if (!uuid(data.steering_id)) throw new Error("Steering 状态无效。");
          else if (data.status === "consumed") {
            if (!uuid(data.entry_id) || data.reason !== null)
              throw new Error("Steering 消费状态无效。");
          } else if (data.status === "withdrawn") {
            if (data.entry_id !== null || data.reason !== null)
              throw new Error("Steering 撤回状态无效。");
          } else if (data.status === "discarded") {
            if (
              data.entry_id !== null ||
              !discardReasons.includes(data.reason as DiscardReasonWire)
            )
              throw new Error("Steering 丢弃状态无效。");
          } else throw new Error("Steering 状态无效。");
          break;
        case "done":
          if (
            data.status !== "completed" ||
            !["stop", "length"].includes(data.stop_reason as string)
          )
            throw new Error("终止状态无效。");
          terminal = true;
          break;
        case "error":
          if (
            !string("message") ||
            !(data.tool_call_id === null || string("tool_call_id")) ||
            !(
              (data.status === "failed" &&
                (data.code === "execution_failed" ||
                  data.code === "credential_detected" ||
                  data.code === "compaction_failed" ||
                  data.code === "context_budget_exceeded" ||
                  data.code === "context_overflow")) ||
              (data.status === "cancelled" && data.code === "cancelled")
            )
          )
            throw new Error("错误事件无效。");
          terminal = true;
          break;
        default:
          throw new Error("未知事件。");
      }
      onEvent({ event: message.event, data } as ReActEvent);
    },
  });
  return {
    feed: (chunk: string) => parser.feed(chunk),
    finish() {
      if (!terminal) throw new Error("连接中断，未收到终止事件。");
    },
    get terminal() {
      return terminal;
    },
  };
}

export function canSubmitChatInput(
  text: string,
  attachments: readonly AttachmentDraft[],
  unknownKeys: string[],
): boolean {
  return (
    validMessageInput(text, attachments) &&
    !unknownKeys.includes(messageInputKey(text, attachmentInputs(attachments)))
  );
}

export class ReActHttpError extends Error {
  constructor(
    message: string,
    readonly code: string | null = null,
    readonly http_status: number | null = null,
    /** 冲突细分原因（operation_conflict 的 reason）：失效操作按 operation_expired 静默处理 */
    readonly reason: string | null = null,
  ) {
    super(message);
  }
}

/** 失效操作冲突（用户约定）：operation_conflict 且 reason=operation_expired，前端删除本地账本并重读历史 */
export function isExpiredOperation(failure: unknown): boolean {
  return (
    failure instanceof ReActHttpError &&
    failure.code === "operation_conflict" &&
    failure.reason === "operation_expired"
  );
}
