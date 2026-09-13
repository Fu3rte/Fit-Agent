/**
 * 契约 API 封装：每个端点一个函数，形状一律来自 src/lib/contract.ts。
 * 错误统一解析为 ApiError 形状抛出；SSE 用原生 EventSource（A2）。
 *
 * 传输面按 stage4 §6 冻结拼写（F6-02a）：会话查询恢复、按 Run 订阅 SSE；
 * 废弃 POST /api/runs、GET /api/events、GET /api/runs/active、GET /api/sessions/:id/messages。
 */
import { normalizeDraft } from "@/lib/diffNormalize";
import type {
  ApiError,
  ConfirmRequest,
  ConfirmResult,
  DiscardResult,
  Draft,
  DraftPayload,
  PlanGuidanceResponseWire,
  PlanResponseWire,
  ProfileResponse,
  ProviderConfig,
  RecordItemResponseWire,
  RecordJudgementResponseWire,
  RecordListResponseWire,
  RecalcRequest,
  ReviewItemResponseWire,
  ReviewListResponseWire,
  ReviseRequest,
  ReviseResult,
  Run,
  SessionDetail,
  StatsCompletionResponseWire,
  StatsPrResponseWire,
  SubmitRequestResult,
} from "@/lib/contract";

export type { ApiError };

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  const body = (await res.json().catch(() => null)) as unknown;
  if (!res.ok) {
    const err = body as Partial<ApiError> | null;
    const error = new Error(
      err?.message ?? `请求失败（${res.status}）`,
    ) as Error & Partial<ApiError>;
    error.http_status = err?.http_status ?? res.status;
    error.error_code = err?.error_code ?? "invalid_request";
    error.detail = err?.detail;
    throw error;
  }
  return body as T;
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, {
    method: "POST",
    body: body === undefined ? "{}" : JSON.stringify(body),
  });

/* ------------------------------- 查询端点 --------------------------------- */

export const getProvider = () => request<ProviderConfig>("/api/provider");

export const putApiKey = (api_key: string) =>
  request<{ has_api_key: boolean }>("/api/provider/api-key", {
    method: "PUT",
    body: JSON.stringify({ api_key }),
  });

export const deleteApiKey = () =>
  request<{ has_api_key: boolean }>("/api/provider/api-key", {
    method: "DELETE",
  });

/** 正式档案（S2-07：无 plan/safety；F1 计划改走 getPlan/getPlanGuidance） */
export const getProfile = () => request<ProfileResponse>("/api/profile");

/** 当前计划 + 全部日程；无计划时 plan:null（stage6 §1.2） */
export const getPlan = () => request<PlanResponseWire>("/api/plan");

/** 计划历史版本；不存在 404 invalid_request */
export const getPlanVersion = (planVersionId: string) =>
  request<PlanResponseWire>(`/api/plans/${planVersionId}`);

/** 计划安全复核；可选按已接受安排绑定版本复核；无计划时 guidance:null */
export const getPlanGuidance = (opts?: { arrangement_revision_id?: string }) => {
  const qs = opts?.arrangement_revision_id
    ? `?arrangement_revision_id=${encodeURIComponent(opts.arrangement_revision_id)}`
    : "";
  return request<PlanGuidanceResponseWire>(`/api/plan/guidance${qs}`);
};

/** 记录列表（存储契约；展示映射见 lib/readModels） */
export const getRecords = () =>
  request<RecordListResponseWire>("/api/records");

export const getRecord = (sessionId: string) =>
  request<RecordItemResponseWire>(`/api/records/${sessionId}`);

/** 组级三桶；无对照/已作废/无当前修订时 judgement:null */
export const getRecordJudgement = (sessionId: string) =>
  request<RecordJudgementResponseWire>(`/api/records/${sessionId}/judgement`);

// F6-02c（已拍 A2）：无 getArrangements / GET /api/arrangements——生产路径不依赖
// 该 mock 端点；已接受安排由前端从 records / 会话安排草稿投影（src/lib/arrangements.ts）。

/**
 * 完成率（F7）：必填 plan_version_id + week_no；无到期名额 completion:null（「暂无」）。
 * rate 为 0–1 比率（null=暂无），展示百分比在 readModels 转换。
 */
export const getStatsCompletion = (planVersionId: string, weekNo: number) => {
  const qs = new URLSearchParams({
    plan_version_id: planVersionId,
    week_no: String(weekNo),
  });
  return request<StatsCompletionResponseWire>(`/api/stats/completion?${qs}`);
};

/**
 * PR（F7）：必填 exercise_id + load_notation；可选 load_kg_key（该重量下单组最高次数）。
 * max_load_kg_key 为 kg×1000 整数键；无候选时相关数值字段为 null。
 */
export const getStatsPr = (
  exerciseId: string,
  loadNotation: string,
  loadKgKey?: number,
) => {
  const qs = new URLSearchParams({
    exercise_id: exerciseId,
    load_notation: loadNotation,
  });
  if (loadKgKey !== undefined) qs.set("load_kg_key", String(loadKgKey));
  return request<StatsPrResponseWire>(`/api/stats/pr?${qs}`);
};

/** 复盘列表（F8：追加语义；最新条 = 前端「当前复盘」） */
export const listReviews = () =>
  request<ReviewListResponseWire>("/api/reviews");

/** 复盘单条；不存在 404 invalid_request */
export const getReviewById = (reviewId: string) =>
  request<ReviewItemResponseWire>(`/api/reviews/${reviewId}`);

/**
 * 新建空会话（stage4 §6：请求体必须为空对象 `{}`，不接受 title）：
 * 返回会话查询投影（消息 + 全部 Run）。
 */
export const createSession = () => post<SessionDetail>("/api/sessions", {});

/**
 * 会话查询（08 8.7 断线/刷新/重启后的恢复入口）：内嵌消息 + 全部 Run 状态；
 * 不重放 SSE，草稿当前状态另走 getSessionDrafts（消息不内嵌草稿，08 8.7）。
 */
export const getSession = (sessionId: string) =>
  request<SessionDetail>(`/api/sessions/${sessionId}`);

/** 会话草稿当前状态列表（08 8.7 规则 2：草稿状态经业务接口查询，不依赖历史通知）。
 * 草稿 diff 经 normalizeDraft 做网络边界映射（wire {before,after,changed} → 展示形状）。 */
export const getSessionDrafts = async (sessionId: string): Promise<Draft[]> => {
  const drafts = await request<Draft[]>(`/api/sessions/${sessionId}/drafts`);
  return drafts.map(normalizeDraft);
};

/* ------------------------------- Run / 草稿 -------------------------------- */

/**
 * 提交一次用户请求（stage4 §6）：`{client_request_id, text}` → `{created, run}`。
 * 幂等：相同 client_request_id 返回已有 Run 且不重启；全局已有活跃 Run 时 409
 * conversation_busy（不创建 Run 或消息）。
 */
export const submitRequest = (
  sessionId: string,
  client_request_id: string,
  text: string,
) =>
  post<SubmitRequestResult>(`/api/sessions/${sessionId}/requests`, {
    client_request_id,
    text,
  });

/** 按身份查询 Run：五态权威状态与可理解原因（SQLite 是事实源，08 8.7） */
export const getRun = (runId: string) =>
  request<{ run: Run }>(`/api/runs/${runId}`);

/**
 * 手动重试：用旧 Run 的同一请求事实创建新 Run（`retry_of_run_id` 指向旧 Run）；
 * 不复活旧记录、不做断点续跑（08 8.1/8.4）。body `{client_request_id}` 幂等。
 */
export const retryRun = (runId: string, client_request_id: string) =>
  post<SubmitRequestResult>(`/api/runs/${runId}/retry`, { client_request_id });

/**
 * 显式取消（08 8.3）：只有本入口触发取消；SSE 断开与刷新都不取消。
 * 终态 Run 重复取消返回 409 invalid_request。
 */
export const cancelRun = (runId: string) =>
  post<{ run: Run }>(`/api/runs/${runId}/cancel`, {});

/** 确认采纳；仅携带所见修订版本（契约 ConfirmRequest，01 1.4）；幂等返回原提交凭据 */
export const confirmDraft = (draftId: string, revision: number) => {
  const body: ConfirmRequest = { revision };
  return post<ConfirmResult>(`/api/drafts/${draftId}/confirm`, body);
};

/**
 * 内联纠错（01 1.2/1.3）：提交纠错后的完整草稿内容与所见 revision，服务端整份替换
 * payload 并 revision+1；revision 不匹配按 409 draft_modified 拒绝（交接 F3）。
 */
export const reviseDraft = async (
  draftId: string,
  payload: DraftPayload,
  revision: number,
): Promise<ReviseResult> => {
  const body: ReviseRequest = { payload, revision };
  const result = await post<ReviseResult>(`/api/drafts/${draftId}/revise`, body);
  return { ...result, draft: normalizeDraft(result.draft) };
};

/** 丢弃待确认草稿（01 1.3）：正式数据与业务版本不变；Discarded 不得再提交 */
export const discardDraft = (draftId: string) =>
  post<DiscardResult>(`/api/drafts/${draftId}/discard`, {});

/**
 * 作废整次训练（F6-02c 已拍；S3-11）：`POST /api/drafts/{id}/void` body `{revision}`，
 * 向既有训练身份追加 voided 修订并切换当前指针；仅适用于绑定既有身份的
 * training_record 草稿（无独立 training_void kind）。幂等返回原提交凭据（ConfirmResult）。
 */
export const voidDraft = (draftId: string, revision: number) => {
  const body: ConfirmRequest = { revision };
  return post<ConfirmResult>(`/api/drafts/${draftId}/void`, body);
};

/**
 * 一键重算（01 1.6 / S4-08 Q1=C）：真实后端创建 Agent Run 并返回 `{created, run}`；
 * 新草稿经 draft 事件 + 会话草稿查询到达，不是同步重算结果对象。
 */
export const recalcDraft = (
  draftId: string,
  client_request_id: string = crypto.randomUUID(),
) => {
  const body: RecalcRequest = { client_request_id };
  return post<SubmitRequestResult>(`/api/drafts/${draftId}/recalc`, body);
};

/* --------------------------------- SSE ------------------------------------ */

/**
 * 按 Run 订阅的原生 EventSource（stage4 §6：`GET /api/runs/{run_id}/events`）。
 * 订阅即先发当前 status；15 秒无业务事件由传输层补 heartbeat；终态即结束。
 * 不依赖浏览器自动重连，不使用 Last-Event-ID 补读或事件重放；
 * 断线/刷新经会话查询恢复（08 8.7）。
 */
export function createRunEventsSource(runId: string): EventSource {
  return new EventSource(`/api/runs/${runId}/events`);
}
