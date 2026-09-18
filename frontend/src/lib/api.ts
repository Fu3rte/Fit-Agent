/**
 * 契约 API 封装：每个端点一个函数，形状一律来自 src/lib/contract.ts。
 * 错误统一解析为 ApiError 形状抛出（后端固定
 * ``{http_status, error_code:"invalid_request", message}``）。
 *
 * 覆盖 Stage 1 交付端点：画像、训练记录与组、身体指标、动作目录、计划只读；
 * 以及 Stage 2 的 Stats 只读端点（三类 PB、趋势、月历）。
 * 表单写入直连业务端点，不经 Run／草稿。
 */
import type {
  AgentEventNameWire,
  AgentEventWire,
  AgentPlanBody,
  AgentPlanResponseWire,
  AgentRunBody,
  ApiError,
  BodyMetricItemWire,
  BodyMetricListWire,
  BodyMetricWriteBody,
  CalendarResponseWire,
  ConfirmWorkoutBody,
  ConfirmWorkoutResponseWire,
  ExerciseListWire,
  PersonalBestListWire,
  PlanItemWire,
  PlanListWire,
  PlanSessionCandidatesWire,
  PlanSessionListWire,
  ProfileResponseWire,
  ProfileWriteBody,
  RecordItemWire,
  RecordListWire,
  RecordWriteBody,
  TrendsResponseWire,
} from "@/lib/contract";

export type { ApiError };

/** 后端统一 JSON 错误形状 → Error（形状不变；SSE 与普通请求共用同一份错误处理） */
function apiError(body: unknown, status: number): Error & Partial<ApiError> {
  const err = body as Partial<ApiError> | null;
  const error = new Error(
    err?.message ?? `请求失败（${status}）`,
  ) as Error & Partial<ApiError>;
  error.http_status = err?.http_status ?? status;
  error.error_code = err?.error_code ?? "invalid_request";
  return error;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  const body = (await res.json().catch(() => null)) as unknown;
  if (!res.ok) throw apiError(body, res.status);
  return body as T;
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, {
    method: "POST",
    body: body === undefined ? "{}" : JSON.stringify(body),
  });

/* --------------------------------- 画像 ----------------------------------- */

/** 画像（七字段三态事实）；profile: null = 未建档 */
export const getProfile = () => request<ProfileResponseWire>("/api/profile");

/** 整份覆盖写入画像：未填写用 unknown、明确为空用 denied */
export const putProfile = (body: ProfileWriteBody) =>
  request<ProfileResponseWire>("/api/profile", {
    method: "PUT",
    body: JSON.stringify(body),
  });

/* -------------------------------- 动作目录 --------------------------------- */

/** 动作目录全量：表单的动作选择与负重口径来源 */
export const listExercises = () => request<ExerciseListWire>("/api/exercises");

/* -------------------------------- 训练记录 --------------------------------- */

/** 训练记录列表（稳定身份 + 发生日期 + 关联日程 + 全部组） */
export const listRecords = () => request<RecordListWire>("/api/records");

export const getRecordById = (recordId: number) =>
  request<RecordItemWire>(`/api/records/${recordId}`);

export const createRecord = (body: RecordWriteBody) =>
  post<RecordItemWire>("/api/records", body);

export const updateRecord = (recordId: number, body: RecordWriteBody) =>
  request<RecordItemWire>(`/api/records/${recordId}`, {
    method: "PUT",
    body: JSON.stringify(body),
  });

export const deleteRecord = (recordId: number) =>
  request<void>(`/api/records/${recordId}`, { method: "DELETE" });

/**
 * 当天可关联的计划日程候选；省略 date 用服务端业务自然日。
 * 零个或多个候选时必须由用户显式选择或标记额外训练（不猜）。
 */
export const listPlanSessionCandidates = (date?: string) =>
  request<PlanSessionCandidatesWire>(
    date
      ? `/api/records/plan-session-candidates?date=${encodeURIComponent(date)}`
      : "/api/records/plan-session-candidates",
  );

/* -------------------------------- 身体指标 --------------------------------- */

/** 身体指标列表；体脂未记录为 null，不补 0 */
export const listBodyMetrics = () => request<BodyMetricListWire>("/api/body-metrics");

export const createBodyMetric = (body: BodyMetricWriteBody) =>
  post<BodyMetricItemWire>("/api/body-metrics", body);

export const updateBodyMetric = (metricId: number, body: BodyMetricWriteBody) =>
  request<BodyMetricItemWire>(`/api/body-metrics/${metricId}`, {
    method: "PUT",
    body: JSON.stringify(body),
  });

export const deleteBodyMetric = (metricId: number) =>
  request<void>(`/api/body-metrics/${metricId}`, { method: "DELETE" });

/* ------------------------------ 计划（只读） ------------------------------- */

/** 计划只读：全部版本（升序）、当前 active、按身份、计划日程 */
export const listPlans = () => request<PlanListWire>("/api/plans");

export const getActivePlan = () => request<PlanItemWire>("/api/plans/active");

export const getPlanById = (planId: number) =>
  request<PlanItemWire>(`/api/plans/${planId}`);

export const listPlanSessions = (planId: number) =>
  request<PlanSessionListWire>(`/api/plans/${planId}/sessions`);

/* ------------------------------ 统计（只读现算） ------------------------------ */

/** 三类 PB（最大重量、最大次数、最长时长）及来源训练、组序号与日期 */
export const listPersonalBests = () =>
  request<PersonalBestListWire>("/api/stats/personal-bests");

/** 最近 30 天体重／体脂原始点与趋势摘要；力量系列由后端计算，看板不渲染 */
export const getTrends = () => request<TrendsResponseWire>("/api/stats/trends");

/** 一个自然月的计划日程状态与实际训练事实；month 为严格的 ``YYYY-MM`` */
export const getCalendarMonth = (month: string) =>
  request<CalendarResponseWire>(
    `/api/stats/calendar?month=${encodeURIComponent(month)}`,
  );

/* -------------------------------- Agent（run 流 ＋ 确认／拒绝） -------------------------------- */

/** 五类 SSE 产品事件名（与 contract.ts::AgentEventNameWire 同一集合） */
const AGENT_EVENT_NAMES: readonly AgentEventNameWire[] = [
  "node",
  "message",
  "waiting",
  "done",
  "error",
];

/** 帧内行终止符：CRLF、LF 与单独的 CR 都算一行结束 */
const SSE_LINE_BREAK = /\r\n|\n|\r/;

/**
 * 位置 ``index`` 处的行终止符长度：``\r\n`` 算**一个**终止符（2），单独的 LF／CR 为 1。
 *
 * 缓冲区末尾孤立的 ``\r`` 可能正是被 chunk 切开的 ``\r\n`` 的前半，返回 -1 表示「等下一个 chunk 再判定」。
 */
function lineTerminatorLength(buffer: string, index: number): number {
  const char = buffer[index];
  if (char === "\n") return 1;
  if (char !== "\r") return 0;
  if (index + 1 >= buffer.length) return -1;
  return buffer[index + 1] === "\n" ? 2 : 1;
}

/**
 * 一条 SSE 帧的边界：两个相邻的行终止符构成空行（CR／LF／CRLF 可以混用，因为 ``\r\n`` 只算一个）。
 *
 * 返回帧文本的结束位置与下一帧的起始位置；缓冲区里凑不出完整空行时返回 ``null``。切帧只看终止符
 * 计数，因此落在 CRLF 内部的 chunk 切点不会被误当成空行。
 */
function agentFrameBoundary(
  buffer: string,
): { end: number; next: number } | null {
  let index = 0;
  while (index < buffer.length) {
    const terminator = lineTerminatorLength(buffer, index);
    if (terminator === 0) {
      index += 1;
      continue;
    }
    if (terminator < 0) return null;
    const afterFirst = index + terminator;
    const second = lineTerminatorLength(buffer, afterFirst);
    if (second > 0) return { end: index, next: afterFirst + second };
    if (second < 0) return null;
    index = afterFirst;
  }
  return null;
}

/**
 * 一条 SSE 帧 → 事件对象（``event:`` 与 ``data:`` 行）。
 *
 * 帧分隔（空行）由调用方切分；``:`` 开头是注释行；``data:`` 多行按 SSE 规范用换行拼接。事件名只认
 * 契约的五类：未知事件名立即抛错，不猜默认值，也不静默丢弃。
 */
function parseAgentFrame(frame: string): AgentEventWire | null {
  let name = "";
  const data: string[] = [];
  for (const line of frame.split(SSE_LINE_BREAK)) {
    if (line === "" || line.startsWith(":")) continue;
    if (line.startsWith("event:")) name = line.slice("event:".length).trim();
    else if (line.startsWith("data:"))
      data.push(line.slice("data:".length).replace(/^ /, ""));
  }
  if (name === "") {
    if (data.length === 0) return null; // 空行或纯注释帧不是事件
    throw new Error("SSE 帧缺少事件名");
  }
  if (!AGENT_EVENT_NAMES.includes(name as AgentEventNameWire))
    throw new Error(`未知的 SSE 事件名：${name}`);
  if (data.length === 0) throw new Error(`SSE 事件 ${name} 缺少 data`);
  return { event: name, data: JSON.parse(data.join("\n")) } as AgentEventWire;
}

/**
 * 消费 ``POST /api/agent/run`` 的 SSE 流（五类产品事件），每条事件先交给 ``onEvent``。
 *
 * - 原生 ``fetch`` ＋ ``ReadableStream``：帧可能被任意切分，用 ``TextDecoder`` 以 ``{ stream: true }``
 *   累积、结束时 flush，并按空行切帧（最后一帧可以没有结尾空行）；CR／LF／CRLF 都是 SSE 行终止符且可
 *   混用（``\r\n`` 只算一个终止符，``agentFrameBoundary`` 逐字符扫描两个相邻终止符），跨 chunk 拆开的
 *   ``\r\n`` 也不能漏掉帧边界：缓冲区保留原始字节，末尾孤立 ``\r`` 留到下一个 chunk 判定，因此落在
 *   CRLF 之间的 chunk 切点不会被误当成空行；
 * - 流内 ``error`` 事件在回调之后仍会抛错（message 即后端已脱敏的可见文本），调用方能提示失败；
 * - 非 2xx（请求形状或 UUID 非法等流建立前的错误）复用既有 JSON 错误形状，不进入帧解析。
 */
export async function runAgentStream(
  body: AgentRunBody,
  onEvent: (event: AgentEventWire) => void,
): Promise<void> {
  const res = await fetch("/api/agent/run", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify(body),
  });
  if (!res.ok || res.body === null) {
    throw apiError(await res.json().catch(() => null), res.status);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let failure: string | null = null;

  const emit = (frame: string) => {
    const event = parseAgentFrame(frame);
    if (event === null) return;
    if (event.event === "error") failure = event.data.message;
    onEvent(event);
  };
  /** 切出缓冲区里所有完整帧（跨 chunk 的半条边界留到下一块） */
  const consumeFrames = () => {
    for (;;) {
      const boundary = agentFrameBoundary(buffer);
      if (boundary === null) return;
      emit(buffer.slice(0, boundary.end));
      buffer = buffer.slice(boundary.next);
    }
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    consumeFrames();
  }
  buffer += decoder.decode();
  consumeFrames();
  // 最后一帧可以没有结尾空行；流已结束，此处孤立的 CR 只能按行终止符解释
  emit(buffer.replace(/\r\n|\r/g, "\n"));

  if (failure !== null) throw new Error(failure);
}

/** 用户确认：激活 draft（幂等已 active 时返回既有行），返回落库后的计划行 */
export const confirmPlan = (body: AgentPlanBody) =>
  post<AgentPlanResponseWire>("/api/agent/confirm", body);

/** 用户拒绝：把 draft 归档（原 active 不变，永不写 rejected），返回落库后的计划行 */
export const rejectPlan = (body: AgentPlanBody) =>
  post<AgentPlanResponseWire>("/api/agent/reject", body);

/**
 * 自然语言打卡确认写入：提交 `waiting` 载荷（含用户修改后的完整值），返回落库训练事实与重新现算的 PB。
 *
 * 服务端重新执行 DTO／领域／目录／日程关联校验，并复用表单的同一写入服务；错误仍是既有 JSON
 * 错误形状（400／409／422）。
 */
export const confirmWorkout = (body: ConfirmWorkoutBody) =>
  post<ConfirmWorkoutResponseWire>("/api/agent/confirm-workout", body);
