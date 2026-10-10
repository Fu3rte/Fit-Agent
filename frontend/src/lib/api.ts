import type {
  AgentStreamHeaders,
  AttachmentContentWire,
  CurrentPlanWire,
  EditRunBody,
  HistoryEntryWire,
  HistoryRunWire,
  HistorySteeringWire,
  OperationQueryWire,
  PlanListWire,
  PlanRecordWire,
  ProfileResponseWire,
  ProviderStatusWire,
  ProviderTestWire,
  ProviderWriteBody,
  PublicAssistantContent,
  PublicMessageWire,
  ReActEvent,
  ReActRunBody,
  RegenerateRunBody,
  RunSteeringItemWire,
  RunSteeringListWire,
  SendRunRepeatWire,
  SessionCreateBody,
  SessionDeleteWire,
  SessionHistoryWire,
  SessionListWire,
  SessionRunListWire,
  SessionRunWire,
  SessionWire,
  DiscardReasonWire,
  SteeringBody,
  SteeringInputWire,
  SteeringReceiveWire,
  SteeringStatusWire,
  SteeringWithdrawBody,
  SteeringWithdrawWire,
  WorkoutListWire,
  WorkoutRecordWire,
} from "@/lib/contract";
import {
  isObject,
  nullableMillis,
  nullableString,
  nullableUuid,
  parseAttachmentContent,
  parseAttachmentWireList,
  parseCurrentPlan,
  parsePlanList,
  parsePlanRecord,
  parseProfileResponse,
  parseProviderStatus,
  parseProviderTest,
  parseWorkoutList,
  parseWorkoutRecord,
  requireArray,
  requireBoolean,
  requireEnum,
  requireMillis,
  requireString,
  requireText,
  requireUuid,
} from "@/lib/business";
import {
  ReActHttpError,
  createReActParser,
  validMessageInput,
} from "@/features/chat/utils/reactAgent";

/* ===== 会话持久化请求层（backend-http-sse-contract §11）===== */

const RUN_STATUSES = [
  "running",
  "completed",
  "failed",
  "cancelled",
  "interrupted",
] as const;
const STEERING_STATUSES = [
  "pending",
  "consumed",
  "withdrawn",
  "discarded",
] as const;
const STEERING_RECEIVE_STATUSES = [
  "accepted",
  "pending",
  "consumed",
  "withdrawn",
  "discarded",
] as const;
const DISCARD_REASONS = [
  "completed",
  "failed",
  "cancelled",
  "interrupted",
] as const;
const OPERATION_KINDS = ["send", "edit", "regenerate", "steering"] as const;

/** 会话持久化接口错误（§11.5）：body 形如 ``{"detail":{"code":"...","message":"..."}}`` */
async function contractError(response: Response): Promise<ReActHttpError> {
  const body: unknown = await response.json().catch(() => null);
  const detail = isObject(body) && isObject(body.detail) ? body.detail : null;
  if (
    detail === null ||
    typeof detail.code !== "string" ||
    typeof detail.message !== "string"
  )
    throw new Error(`响应错误结构无效（${response.status}）。`);
  return new ReActHttpError(
    detail.message,
    detail.code,
    response.status,
    typeof detail.reason === "string" ? detail.reason : null,
  );
}

function parseSession(body: unknown): SessionWire {
  if (!isObject(body)) throw new Error("会话响应无效。");
  return {
    session_id: requireUuid(body.session_id, "session_id"),
    title: requireString(body.title, "title"),
    active_leaf_id: nullableUuid(body.active_leaf_id, "active_leaf_id"),
    created_at: requireMillis(body.created_at, "created_at"),
    updated_at: requireMillis(body.updated_at, "updated_at"),
  };
}

function parseRun(body: unknown): SessionRunWire {
  if (!isObject(body)) throw new Error("运行响应无效。");
  return {
    session_id: requireUuid(body.session_id, "session_id"),
    run_id: requireUuid(body.run_id, "run_id"),
    request_entry_id: requireUuid(body.request_entry_id, "request_entry_id"),
    last_entry_id: nullableUuid(body.last_entry_id, "last_entry_id"),
    status: requireEnum(body.status, RUN_STATUSES, "status"),
    started_at: requireMillis(body.started_at, "started_at"),
    finished_at: nullableMillis(body.finished_at, "finished_at"),
    error_code: nullableString(body.error_code, "error_code"),
    error_message: nullableString(body.error_message, "error_message"),
  };
}

/** 输入状态的字段组合（§11.2）：consumed 必须有 entry_id，discarded 必须有丢弃原因，其余两者为 null */
function requireSteeringShape(
  status: SteeringStatusWire,
  entryId: string | null,
  reason: DiscardReasonWire | null,
): void {
  if (status === "consumed") {
    if (entryId === null || reason !== null)
      throw new Error("Steering 消费状态无效。");
    return;
  }
  if (status === "discarded") {
    if (entryId !== null || reason === null)
      throw new Error("Steering 丢弃状态无效。");
    return;
  }
  if (entryId !== null || reason !== null)
    throw new Error("Steering 状态无效。");
}

/** 输入状态对象的共有身份字段（§11.2）：consumed 必须有 entry_id，discarded 必须有丢弃原因，其余两者为 null */
function parseSteeringIdentity(
  body: unknown,
): Omit<SteeringInputWire, "attachments"> {
  if (!isObject(body)) throw new Error("输入响应无效。");
  const status = requireEnum(body.status, STEERING_STATUSES, "status");
  const entry_id = nullableUuid(body.entry_id, "entry_id");
  const reason =
    body.reason === null
      ? null
      : requireEnum(body.reason, DISCARD_REASONS, "reason");
  requireSteeringShape(status, entry_id, reason);
  return {
    session_id: requireUuid(body.session_id, "session_id"),
    run_id: requireUuid(body.run_id, "run_id"),
    steering_id: requireUuid(body.steering_id, "steering_id"),
    status,
    entry_id,
    reason,
    created_at: requireMillis(body.created_at, "created_at"),
    updated_at: requireMillis(body.updated_at, "updated_at"),
  };
}

/** 操作查询的输入对象（§11.2、附件契约 §4）：身份字段加按受理顺序返回的附件集合 */
function parseSteeringInput(body: unknown): SteeringInputWire {
  const identity = parseSteeringIdentity(body);
  if (!isObject(body)) throw new Error("输入响应无效。");
  return {
    ...identity,
    attachments: parseAttachmentWireList(body.attachments, "attachments"),
  };
}

function parseSendRunRepeat(body: unknown): SendRunRepeatWire {
  if (!isObject(body)) throw new Error("重复受理响应无效。");
  return {
    operation_id: requireUuid(body.operation_id, "operation_id"),
    session_id: requireUuid(body.session_id, "session_id"),
    run_id: requireUuid(body.run_id, "run_id"),
    request_entry_id: requireUuid(body.request_entry_id, "request_entry_id"),
    status: requireEnum(body.status, RUN_STATUSES, "status"),
  };
}

function parseSteeringReceive(
  body: unknown,
  runId: string,
  sessionId: string,
): SteeringReceiveWire {
  if (!isObject(body)) throw new Error("Steering 接受响应无效。");
  const received: SteeringReceiveWire = {
    operation_id: requireUuid(body.operation_id, "operation_id"),
    session_id: requireUuid(body.session_id, "session_id"),
    run_id: requireUuid(body.run_id, "run_id"),
    steering_id: requireUuid(body.steering_id, "steering_id"),
    created: requireBoolean(body.created, "created"),
    status: requireEnum(body.status, STEERING_RECEIVE_STATUSES, "status"),
    entry_id: nullableUuid(body.entry_id, "entry_id"),
    reason:
      body.reason === null
        ? null
        : requireEnum(body.reason, DISCARD_REASONS, "reason"),
  };
  if (received.run_id !== runId || received.session_id !== sessionId)
    throw new Error("Steering 接受响应身份不匹配。");
  if (received.status === "accepted") {
    if (
      !received.created ||
      received.entry_id !== null ||
      received.reason !== null
    )
      throw new Error("Steering 首次受理响应无效。");
  } else {
    if (received.created) throw new Error("Steering 重复受理响应无效。");
    requireSteeringShape(received.status, received.entry_id, received.reason);
  }
  return received;
}

function parseSteeringWithdraw(body: unknown): SteeringWithdrawWire {
  if (!isObject(body)) throw new Error("撤回响应无效。");
  const result: SteeringWithdrawWire = {
    session_id: requireUuid(body.session_id, "session_id"),
    run_id: requireUuid(body.run_id, "run_id"),
    steering_id: requireUuid(body.steering_id, "steering_id"),
    status: requireEnum(
      body.status,
      ["withdrawn", "discarded"] as const,
      "status",
    ),
    entry_id: nullableUuid(body.entry_id, "entry_id"),
    reason:
      body.reason === null
        ? null
        : requireEnum(body.reason, DISCARD_REASONS, "reason"),
  };
  requireSteeringShape(result.status, result.entry_id, result.reason);
  return result;
}

function parseOperationQuery(body: unknown): OperationQueryWire {
  if (!isObject(body)) throw new Error("操作查询响应无效。");
  const operation_id = requireUuid(body.operation_id, "operation_id");
  const session_id = requireUuid(body.session_id, "session_id");
  const accepted = requireBoolean(body.accepted, "accepted");
  if (!accepted) {
    if (body.kind !== null || body.run !== null || body.steering !== null)
      throw new Error("未受理操作响应字段无效。");
    return {
      operation_id,
      session_id,
      accepted: false,
      kind: null,
      run: null,
      steering: null,
    };
  }
  const kind = requireEnum(body.kind, OPERATION_KINDS, "kind");
  if (body.run === null) throw new Error("已受理操作缺少运行对象。");
  const run = parseRun(body.run);
  const steering =
    kind === "steering" ? parseSteeringInput(body.steering) : null;
  if (kind !== "steering" && body.steering !== null)
    throw new Error("操作查询响应 steering 字段无效。");
  if (run.session_id !== session_id)
    throw new Error("操作查询会话身份不匹配。");
  if (
    steering !== null &&
    (steering.session_id !== session_id || steering.run_id !== run.run_id)
  )
    throw new Error("操作查询输入归属无效。");
  return { operation_id, session_id, accepted: true, kind, run, steering };
}

/** 读取并校验四个 SSE 响应头（§11.4），并核对与会话／操作的身份一致 */
function parseAgentStreamHeaders(
  response: Response,
  body: { session_id: string; operation_id: string },
): AgentStreamHeaders {
  const headers: AgentStreamHeaders = {
    session_id: requireUuid(
      response.headers.get("X-Session-ID"),
      "X-Session-ID",
    ),
    operation_id: requireUuid(
      response.headers.get("X-Operation-ID"),
      "X-Operation-ID",
    ),
    run_id: requireUuid(response.headers.get("X-Run-ID"), "X-Run-ID"),
    request_entry_id: requireUuid(
      response.headers.get("X-Request-Entry-ID"),
      "X-Request-Entry-ID",
    ),
  };
  if (
    headers.session_id !== body.session_id ||
    headers.operation_id !== body.operation_id
  )
    throw new Error("SSE 响应头与请求身份不一致。");
  return headers;
}

async function sessionRequest<T>(
  path: string,
  init: RequestInit,
  parse: (body: unknown) => T,
): Promise<T> {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!response.ok) throw await contractError(response);
  return parse(await response.json());
}

/** 创建会话（§4）：首次 201、同 ID 同标题重试 200，均返回会话对象 */
export const createSession = (body: SessionCreateBody, signal?: AbortSignal) =>
  sessionRequest<SessionWire>(
    "/api/sessions",
    { method: "POST", body: JSON.stringify(body), signal },
    parseSession,
  );

/**
 * 发送／编辑／重新生成（§5、session-edit-regenerate-contract §3、§4）：三者共用同一响应形状，
 * 按 Content-Type 走 SSE 或重复受理 JSON；SSE 分支先校验四个响应头再喂
 * eventsource-parser（UTF-8 跨 chunk 由 TextDecoder 与 parser 共同保证）（§11.4）。
 * 流式受理返回 null，重复受理返回原运行关联结果。
 */
export async function runReActStream(
  body: ReActRunBody | EditRunBody | RegenerateRunBody,
  onEvent: (event: ReActEvent) => void,
  signal: AbortSignal,
  onOpen: (headers: AgentStreamHeaders) => void,
  endpoint = "/api/agent/run",
): Promise<SendRunRepeatWire | null> {
  if ("request" in body && !validMessageInput(body.request,
    body.attachments === undefined ? [] : body.attachments))
    throw new Error(
      "消息必须包含非空白文本或至少一个附件，文本最多 32000 个字符。",
    );
  const response = await fetch(endpoint, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) throw await contractError(response);
  const contentType = (
    response.headers.get("content-type") ?? ""
  ).toLowerCase();
  if (contentType.includes("application/json"))
    return parseSendRunRepeat(await response.json());
  if (!contentType.includes("text/event-stream"))
    throw new Error("响应 Content-Type 无效。");
  if (response.body === null) throw new Error("响应缺少流。");
  const headers = parseAgentStreamHeaders(response, body);
  const reader = response.body.getReader();
  try {
    const decoder = new TextDecoder("utf-8", { fatal: true });
    const parser = createReActParser(onEvent, headers.run_id);
    onOpen(headers);
    while (!parser.terminal) {
      const { done, value } = await reader.read();
      if (done) break;
      parser.feed(decoder.decode(value, { stream: true }));
    }
    if (!parser.terminal) parser.feed(decoder.decode());
    parser.finish();
  } finally {
    try {
      await reader.cancel();
    } finally {
      reader.releaseLock();
    }
  }
  return null;
}

/** 接收 Steering（§6.1）：首次 accepted，重复返回原输入当前持久化状态 */
export async function submitSteering(
  runId: string,
  body: SteeringBody,
  signal: AbortSignal,
): Promise<SteeringReceiveWire> {
  if (!validMessageInput(body.message, body.attachments))
    throw new Error(
      "Steering 必须包含非空白文本或至少一个附件，文本最多 32000 个字符。",
    );
  const response = await fetch(
    `/api/agent/runs/${encodeURIComponent(runId)}/steering`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    },
  );
  if (!response.ok) throw await contractError(response);
  return parseSteeringReceive(await response.json(), runId, body.session_id);
}

/** 撤回 Steering（§6.2）：pending 转 withdrawn，已消费返回冲突 */
export const withdrawSteering = (
  runId: string,
  steeringId: string,
  body: SteeringWithdrawBody,
  signal?: AbortSignal,
) =>
  sessionRequest<SteeringWithdrawWire>(
    `/api/agent/runs/${encodeURIComponent(runId)}/steering/${encodeURIComponent(steeringId)}/withdraw`,
    { method: "POST", body: JSON.stringify(body), signal },
    parseSteeringWithdraw,
  ).then((result) => {
    if (
      result.session_id !== body.session_id ||
      result.run_id !== runId ||
      result.steering_id !== steeringId
    )
      throw new Error("撤回响应身份不匹配。");
    return result;
  });

/** 操作查询（§7.1）：已受理返回关联身份与当前状态，未受理 accepted=false */
export const getOperation = (
  sessionId: string,
  operationId: string,
  signal?: AbortSignal,
) =>
  sessionRequest<OperationQueryWire>(
    `/api/sessions/${encodeURIComponent(sessionId)}/operations/${encodeURIComponent(operationId)}`,
    { method: "GET", signal },
    parseOperationQuery,
  ).then((result) => {
    if (result.session_id !== sessionId || result.operation_id !== operationId)
      throw new Error("操作查询身份不匹配。");
    return result;
  });

/** 运行查询（§7.2）：断连或停止后据此确认终态，不推断运行已结束 */
export const getRun = (
  sessionId: string,
  runId: string,
  signal?: AbortSignal,
) =>
  sessionRequest<SessionRunWire>(
    `/api/sessions/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(runId)}`,
    { method: "GET", signal },
    parseRun,
  ).then((result) => {
    if (result.session_id !== sessionId || result.run_id !== runId)
      throw new Error("运行查询身份不匹配。");
    return result;
  });

/* ===== 会话列表与历史读取（session-history-contract §1、§2、§3）===== */

const STOP_REASONS = ["stop", "length", "toolUse", "error", "aborted"] as const;

/** 消息 timestamp（§1）：有限数值，保留存储精度 */
function requireTimestamp(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value))
    throw new Error("timestamp 无效。");
  return value;
}

/** 助手公开内容块（§3.2）：content_index 唯一且严格递增，隐藏块过滤后允许不连续 */
function parseAssistantContent(value: unknown): PublicAssistantContent {
  if (
    !isObject(value) ||
    !Number.isSafeInteger(value.content_index) ||
    (value.content_index as number) < 0
  )
    throw new Error("助手内容块无效。");
  const content_index = value.content_index as number;
  if (value.type === "text" && typeof value.text === "string")
    return { content_index, type: "text", text: value.text };
  if (value.type === "thinking" && typeof value.thinking === "string")
    return { content_index, type: "thinking", thinking: value.thinking };
  if (value.type === "tool_call") {
    if (
      typeof value.tool_call_id === "string" &&
      value.tool_call_id !== "" &&
      typeof value.name === "string" &&
      value.name !== "" &&
      isObject(value.arguments)
    )
      return {
        content_index,
        type: "tool_call",
        tool_call_id: value.tool_call_id,
        name: value.name,
        arguments: value.arguments,
      };
  }
  throw new Error("助手内容块字段无效。");
}

function parseAssistantContentList(value: unknown): PublicAssistantContent[] {
  const list = requireArray(value, "content").map(parseAssistantContent);
  let previous = -1;
  for (const block of list) {
    if (block.content_index <= previous)
      throw new Error("助手内容块下标无效。");
    previous = block.content_index;
  }
  return list;
}

/** 历史消息投影（§3.2）：角色与字段一一对应，系统消息只含 role */
function parsePublicMessage(value: unknown): PublicMessageWire {
  if (!isObject(value)) throw new Error("历史消息无效。");
  if (value.role === "system") return { role: "system" };
  if (value.role === "user") {
    return {
      role: "user",
      text: requireText(value.text, "text"),
      timestamp: requireTimestamp(value.timestamp),
      attachments: parseAttachmentWireList(value.attachments, "attachments"),
    };
  }
  if (value.role === "assistant") {
    return {
      role: "assistant",
      content: parseAssistantContentList(value.content),
      stop_reason: requireEnum(value.stop_reason, STOP_REASONS, "stop_reason"),
      timestamp: requireTimestamp(value.timestamp),
    };
  }
  if (value.role === "toolResult") {
    if (typeof value.content !== "string")
      throw new Error("历史工具结果无效。");
    return {
      role: "toolResult",
      tool_call_id: requireString(value.tool_call_id, "tool_call_id"),
      tool_name: requireString(value.tool_name, "tool_name"),
      content: value.content,
      is_error: requireBoolean(value.is_error, "is_error"),
      timestamp: requireTimestamp(value.timestamp),
    };
  }
  throw new Error("历史消息角色无效。");
}

function parseHistoryEntry(value: unknown): HistoryEntryWire {
  if (!isObject(value)) throw new Error("历史节点无效。");
  return {
    entry_id: requireUuid(value.entry_id, "entry_id"),
    parent_id: nullableUuid(value.parent_id, "parent_id"),
    run_id: nullableUuid(value.run_id, "run_id"),
    created_at: requireMillis(value.created_at, "created_at"),
    message: parsePublicMessage(value.message),
  };
}

/** 输入项的文本投影（session-history-contract §3.4、session-list-contract §3）：文本与时间戳保持接收时原值 */
function parseSteeringText(value: unknown): RunSteeringItemWire {
  const identity = parseSteeringIdentity(value);
  if (!isObject(value)) throw new Error("输入响应无效。");
  return {
    ...identity,
    text: requireText(value.text, "text"),
    timestamp: requireTimestamp(value.timestamp),
  };
}

/** 历史输入项（§3.4、附件契约 §4）：文本投影加受理时的有序附件集合 */
function parseHistorySteering(value: unknown): HistorySteeringWire {
  const text = parseSteeringText(value);
  if (!isObject(value)) throw new Error("输入响应无效。");
  return {
    ...text,
    attachments: parseAttachmentWireList(value.attachments, "attachments"),
  };
}

/** 会话列表响应（§2）：身份唯一，服务端已按 updated_at 降序 */
function parseSessionList(body: unknown): SessionListWire {
  if (!isObject(body)) throw new Error("会话列表响应无效。");
  const sessions = requireArray(body.sessions, "sessions").map(parseSession);
  const ids = new Set<string>();
  for (const session of sessions) {
    if (ids.has(session.session_id)) throw new Error("会话列表身份重复。");
    ids.add(session.session_id);
  }
  return { sessions };
}

/**
 * 历史响应（§3）：校验会话归属、祖先链顺序、运行请求节点、末节点归属与输入关联；
 * 协议异常就地报错，不转换为空历史。
 */
export function parseSessionHistory(
  sessionId: string,
  body: unknown,
): SessionHistoryWire {
  if (!isObject(body)) throw new Error("会话历史响应无效。");
  const session = parseSession(body.session);
  if (session.session_id !== sessionId) throw new Error("会话历史身份不匹配。");
  const entries = requireArray(body.entries, "entries").map(parseHistoryEntry);
  const runs = requireArray(body.runs, "runs").map(parseRun);
  const steering = requireArray(body.steering, "steering").map(
    parseHistorySteering,
  );

  const nodes = new Map<string, HistoryEntryWire>();
  entries.forEach((entry, index) => {
    if (nodes.has(entry.entry_id)) throw new Error("历史节点身份重复。");
    const expected = index === 0 ? null : entries[index - 1].entry_id;
    if (entry.parent_id !== expected) throw new Error("历史分支链无效。");
    nodes.set(entry.entry_id, entry);
  });
  if (session.active_leaf_id === null) {
    if (entries.length > 0 || runs.length > 0 || steering.length > 0)
      throw new Error("空分支历史无效。");
  } else if (entries.at(-1)?.entry_id !== session.active_leaf_id) {
    throw new Error("历史叶节点不匹配。");
  }

  const runsById = new Map<string, HistoryRunWire>();
  for (const run of runs) {
    if (run.session_id !== sessionId || runsById.has(run.run_id))
      throw new Error("历史运行归属无效。");
    runsById.set(run.run_id, run);
    const request = nodes.get(run.request_entry_id);
    if (request === undefined || request.message.role !== "user")
      throw new Error("历史运行请求节点无效。");
    if (run.last_entry_id !== null) {
      const last = nodes.get(run.last_entry_id);
      if (last === undefined || last.run_id !== run.run_id)
        throw new Error("历史运行末节点无效。");
    }
  }
  for (const entry of entries) {
    if (entry.run_id !== null && !runsById.has(entry.run_id))
      throw new Error("历史节点运行归属无效。");
  }

  const steeringIds = new Set<string>();
  for (const input of steering) {
    if (input.session_id !== sessionId || steeringIds.has(input.steering_id))
      throw new Error("历史输入归属无效。");
    steeringIds.add(input.steering_id);
    if (!runsById.has(input.run_id)) throw new Error("历史输入目标运行无效。");
    if (input.status === "consumed") {
      const node = nodes.get(input.entry_id as string);
      if (
        node === undefined ||
        node.message.role !== "user" ||
        node.run_id !== input.run_id
      )
        throw new Error("历史消费节点无效。");
    }
  }

  return { session, entries, runs, steering };
}

/** 会话列表（§2）：全量会话头，无分页与业务查询参数 */
export const listSessions = (signal?: AbortSignal) =>
  sessionRequest<SessionListWire>(
    "/api/sessions",
    { method: "GET", signal },
    parseSessionList,
  );

/** 当前分支历史（§3）：刷新与直接 URL 恢复展示的唯一来源 */
export const readSessionHistory = (sessionId: string, signal?: AbortSignal) =>
  sessionRequest<SessionHistoryWire>(
    `/api/sessions/${encodeURIComponent(sessionId)}/history`,
    { method: "GET", signal },
    (body) => parseSessionHistory(sessionId, body),
  );

/**
 * 删除会话（session-delete-contract §4）：``DELETE /api/sessions/{session_id}``，无请求正文与 operation_id；
 * 重复删除与目标不存在返回同一成功结构。结构与身份校验失败即结果未知，调用方不得执行成功清理。
 */
export const deleteSession = (sessionId: string) =>
  sessionRequest<SessionDeleteWire>(
    `/api/sessions/${encodeURIComponent(sessionId)}`,
    { method: "DELETE" },
    (body) => {
      if (!isObject(body) || body.deleted !== true)
        throw new Error("会话删除响应无效。");
      const session_id = requireUuid(body.session_id, "session_id");
      if (session_id !== sessionId) throw new Error("会话删除响应身份不匹配。");
      return { session_id, deleted: true };
    },
  );

/* ===== 画像查询（backend-http-sse-contract §11.9）===== */

/** 已保存画像（§11.9）：未建档同样返回 200，版本与内容同时为 null */
export const getProfile = (signal?: AbortSignal) =>
  sessionRequest<ProfileResponseWire>(
    "/api/profile",
    { method: "GET", signal },
    parseProfileResponse,
  );

/* ===== 训练记录查询（workout-http-sse-contract §2、§3）===== */

/** GET /api/workouts 查询参数：省略日期即对应方向不限日期，page 默认 1，page_size 默认 10 且范围 1–100 */
export interface WorkoutListQuery {
  date_from?: string;
  date_to?: string;
  page?: number;
  page_size?: number;
}

/** 训练记录列表：完整记录按 performed_on、id 降序，无结果或超出总页数时 items 为空数组 */
export const listWorkouts = (
  query: WorkoutListQuery = {},
  signal?: AbortSignal,
) => {
  // 空日期为该方向不限；其余取值原样送出，非法参数由后端按协议拒绝
  const params = new URLSearchParams();
  if (query.date_from) params.set("date_from", query.date_from);
  if (query.date_to) params.set("date_to", query.date_to);
  if (query.page !== undefined) params.set("page", String(query.page));
  if (query.page_size !== undefined)
    params.set("page_size", String(query.page_size));
  const search = params.toString();
  return sessionRequest<WorkoutListWire>(
    search === "" ? "/api/workouts" : `/api/workouts?${search}`,
    { method: "GET", signal },
    parseWorkoutList,
  );
};

/** 按 ID 查询完整训练记录：不存在时返回 404 workout_not_found */
export const getWorkout = (workoutId: string, signal?: AbortSignal) =>
  sessionRequest<WorkoutRecordWire>(
    `/api/workouts/${encodeURIComponent(workoutId)}`,
    { method: "GET", signal },
    parseWorkoutRecord,
  );

/* ===== 附件原文读取（plan-import-adjustment-contract §4）===== */

/** GET /api/sessions/{session_id}/attachments/{attachment_id}：元数据加严格 UTF-8 解码正文，无副作用 */
export const getAttachmentContent = (
  sessionId: string,
  attachmentId: string,
  signal?: AbortSignal,
) =>
  sessionRequest<AttachmentContentWire>(
    `/api/sessions/${encodeURIComponent(sessionId)}/attachments/${encodeURIComponent(attachmentId)}`,
    { method: "GET", signal },
    parseAttachmentContent,
  ).then((result) => {
    if (result.attachment_id !== attachmentId)
      throw new Error("附件内容身份不匹配。");
    return result;
  });

/* ===== 会话运行及 Steering 独立列表（session-list-contract §2、§3）===== */

/**
 * 运行列表响应（§2）：顶层及每项 ``session_id`` 必须与路径会话一致；
 * 列表含分支外节点对应的运行，父子链由历史接口校验，此处不复用历史链检查。
 */
export function parseSessionRunList(
  sessionId: string,
  body: unknown,
): SessionRunListWire {
  if (!isObject(body)) throw new Error("运行列表响应无效。");
  const session_id = requireUuid(body.session_id, "session_id");
  if (session_id !== sessionId) throw new Error("运行列表会话身份不匹配。");
  const runs = requireArray(body.runs, "runs").map(parseRun);
  if (runs.some((run) => run.session_id !== sessionId))
    throw new Error("运行列表项会话身份不匹配。");
  return { session_id, runs };
}

/** 输入列表响应（§3）：顶层及每项同时校验会话与运行身份，文本与时间戳保留接收原值 */
export function parseRunSteeringList(
  sessionId: string,
  runId: string,
  body: unknown,
): RunSteeringListWire {
  if (!isObject(body)) throw new Error("输入列表响应无效。");
  const session_id = requireUuid(body.session_id, "session_id");
  const run_id = requireUuid(body.run_id, "run_id");
  if (session_id !== sessionId || run_id !== runId)
    throw new Error("输入列表运行身份不匹配。");
  const steering = requireArray(body.steering, "steering").map(
    parseSteeringText,
  );
  if (
    steering.some(
      (item) => item.session_id !== sessionId || item.run_id !== runId,
    )
  )
    throw new Error("输入列表项身份不匹配。");
  // 列表契约 §3、§4：timestamp 为 UTC Unix 毫秒整数，历史接口保留存储精度的有限数值规则在列表侧收紧
  for (const item of steering) requireMillis(item.timestamp, "timestamp");
  return { session_id, run_id, steering };
}

/** 会话运行列表（§2）：返回仍保存的全部运行，状态为读取时的持久化快照 */
export const listSessionRuns = (sessionId: string, signal?: AbortSignal) =>
  sessionRequest<SessionRunListWire>(
    `/api/sessions/${encodeURIComponent(sessionId)}/runs`,
    { method: "GET", signal },
    (body) => parseSessionRunList(sessionId, body),
  );

/** 指定运行的 Steering 列表（§3）：四种输入状态全部返回，查询保持队列与消费状态原样 */
export const listRunSteering = (
  sessionId: string,
  runId: string,
  signal?: AbortSignal,
) =>
  sessionRequest<RunSteeringListWire>(
    `/api/sessions/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(runId)}/steering`,
    { method: "GET", signal },
    (body) => parseRunSteeringList(sessionId, runId, body),
  );

/* ===== 训练计划查询（plan-generation-contract §5）===== */

/** GET /api/plans/current（§5）：没有当前计划时 ``id`` 与 ``content`` 同时为 null，不接受查询参数 */
export const getCurrentPlan = (signal?: AbortSignal) =>
  sessionRequest<CurrentPlanWire>(
    "/api/plans/current",
    { method: "GET", signal },
    parseCurrentPlan,
  );

/** GET /api/plans（§5）：直接数组，按 created_at 降序、同时间按 id 降序，无版本时为空数组 */
export const listPlans = (signal?: AbortSignal) =>
  sessionRequest<PlanListWire>(
    "/api/plans",
    { method: "GET", signal },
    parsePlanList,
  );

/** GET /api/plans/{plan_id}（§5）：完整版本；不存在时 404 plan_not_found */
export const getPlan = (planId: string, signal?: AbortSignal) =>
  sessionRequest<PlanRecordWire>(
    `/api/plans/${encodeURIComponent(planId)}`,
    { method: "GET", signal },
    parsePlanRecord,
  );

/* ===== 模型配置（PRODUCT.md §3.4）===== */

export const getProvider = (signal?: AbortSignal) =>
  sessionRequest<ProviderStatusWire>(
    "/api/provider",
    { method: "GET", cache: "no-store", signal },
    parseProviderStatus,
  );

export const putProvider = (body: ProviderWriteBody) =>
  sessionRequest<ProviderStatusWire>(
    "/api/provider",
    { method: "PUT", cache: "no-store", body: JSON.stringify(body) },
    parseProviderStatus,
  );

export const deleteProvider = () =>
  sessionRequest<ProviderStatusWire>(
    "/api/provider",
    { method: "DELETE", cache: "no-store" },
    parseProviderStatus,
  );

export const testProvider = (body: ProviderWriteBody) =>
  sessionRequest<ProviderTestWire>(
    "/api/provider/test",
    { method: "POST", cache: "no-store", body: JSON.stringify(body) },
    parseProviderTest,
  );
