/**
 * 契约 API 封装：每个端点一个函数，形状一律来自 src/lib/contract.ts。
 * 错误统一解析为 ApiError 形状抛出；SSE 用原生 EventSource（A2）。
 */
import type {
  ActiveRunResponse,
  ApiError,
  ChatMessage,
  ConfirmRequest,
  ConfirmResult,
  DiscardResult,
  Draft,
  DraftPayload,
  ProfileResponse,
  ProviderConfig,
  RecalcResult,
  ReviewDoc,
  ReviseRequest,
  ReviseResult,
  RunHandle,
  RunRequest,
  SessionSummary,
  StatsSummary,
  TrainingRecord,
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
    body: body === undefined ? undefined : JSON.stringify(body),
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

export const getProfile = () => request<ProfileResponse>("/api/profile");

export const getRecords = () =>
  request<{ records: TrainingRecord[] }>("/api/records");

export const getStats = () => request<StatsSummary>("/api/stats");

export const getReview = () => request<ReviewDoc>("/api/review");

export const getSessions = () => request<SessionSummary[]>("/api/sessions");

export const createSession = (title?: string) =>
  post<SessionSummary>("/api/sessions", { title });

export const getMessages = (sessionId: string) =>
  request<ChatMessage[]>(`/api/sessions/${sessionId}/messages`);

/** 会话草稿当前状态列表（08 8.7 规则 2：草稿状态经业务接口查询，不依赖历史通知） */
export const getSessionDrafts = (sessionId: string) =>
  request<Draft[]>(`/api/sessions/${sessionId}/drafts`);

/* ------------------------------- Run / 草稿 -------------------------------- */

export const createRun = (req: RunRequest) => post<RunHandle>("/api/runs", req);

export const cancelRun = (runId: string) =>
  post<{ run_id: string; status: string }>(`/api/runs/${runId}/cancel`);

/**
 * 全局 Run 槽位当前状态（状态 + 已保存部分回答 + 关联草稿当前状态）；
 * run = null 表示当前无可查询 Run。断线/刷新后的查询恢复入口（08 8.7 规则 2/3）。
 */
export const getActiveRun = () =>
  request<ActiveRunResponse>("/api/runs/active");

/** 确认采纳；仅携带所见修订版本（契约 ConfirmRequest，01 1.4）；内联纠错经 revise 业务接口另行修订（01 1.2），不在确认内提交 */
export const confirmDraft = (draftId: string, revision: number) => {
  const body: ConfirmRequest = { revision };
  return post<ConfirmResult>(`/api/drafts/${draftId}/confirm`, body);
};

/**
 * 内联纠错（01 1.2/1.3）：提交纠错后的完整草稿内容，服务端整份替换 payload 并 revision+1，
 * 返回修订后的草稿（含随内容更新的 diff）；只改待确认草稿，不自动提交。
 */
export const reviseDraft = (draftId: string, payload: DraftPayload) => {
  const body: ReviseRequest = { payload };
  return post<ReviseResult>(`/api/drafts/${draftId}/revise`, body);
};

/** 丢弃待确认草稿（01 1.3）：正式数据与业务版本不变；Discarded 不得再提交 */
export const discardDraft = (draftId: string) =>
  post<DiscardResult>(`/api/drafts/${draftId}/discard`);

export const recalcDraft = (draftId: string) =>
  post<RecalcResult>(`/api/drafts/${draftId}/recalc`);

/* --------------------------------- SSE ------------------------------------ */

/**
 * 原生 EventSource 订阅（GET /api/events，A2）。
 * 不依赖浏览器自动重连，不使用 Last-Event-ID 补读或事件重放；
 * 断线/刷新经业务接口查询恢复（08 8.7）。
 */
export function createEventSource(): EventSource {
  return new EventSource("/api/events");
}
