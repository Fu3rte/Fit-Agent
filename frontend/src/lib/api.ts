/**
 * 契约 API 封装：每个端点一个函数，形状一律来自 src/lib/contract.ts。
 * 错误统一解析为 ApiError 形状抛出；SSE 用原生 EventSource（A2）。
 */
import type {
  ApiError,
  ChatMessage,
  ConfirmRequest,
  ConfirmResult,
  DraftPayload,
  ProfileResponse,
  ProviderConfig,
  RecalcResult,
  ReviewDoc,
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

/* ------------------------------- Run / 草稿 -------------------------------- */

export const createRun = (req: RunRequest) => post<RunHandle>("/api/runs", req);

export const cancelRun = (runId: string) =>
  post<{ run_id: string; status: string }>(`/api/runs/${runId}/cancel`);

/** 确认采纳；payload 为内联纠错后的最终草稿内容（契约 ConfirmRequest，后端以最终内容复查） */
export const confirmDraft = (draftId: string, payload?: DraftPayload) => {
  const body: ConfirmRequest | undefined =
    payload === undefined ? undefined : { payload };
  return post<ConfirmResult>(`/api/drafts/${draftId}/confirm`, body);
};

export const recalcDraft = (draftId: string) =>
  post<RecalcResult>(`/api/drafts/${draftId}/recalc`);

/* --------------------------------- SSE ------------------------------------ */

/**
 * 原生 EventSource 订阅（GET + Last-Event-ID 补读）。
 * 首连通过查询参数声明补读位点；浏览器重连自动携带 Last-Event-ID 头。
 */
export function createEventSource(lastEventId?: number): EventSource {
  const url = lastEventId
    ? `/api/events?last_event_id=${lastEventId}`
    : "/api/events";
  return new EventSource(url);
}
