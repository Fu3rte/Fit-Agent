/**
 * 契约 API 封装：每个端点一个函数，形状一律来自 src/lib/contract.ts。
 * 错误统一解析为 ApiError 形状抛出（后端固定
 * ``{http_status, error_code:"invalid_request", message}``）。
 *
 * 覆盖 Stage 1 交付端点：画像、训练记录与组、身体指标、动作目录、计划只读。
 * 表单写入直连业务端点，不经 Run／草稿。
 */
import type {
  ApiError,
  BodyMetricItemWire,
  BodyMetricListWire,
  BodyMetricWriteBody,
  ExerciseListWire,
  PlanItemWire,
  PlanListWire,
  PlanSessionCandidatesWire,
  PlanSessionListWire,
  ProfileResponseWire,
  ProfileWriteBody,
  RecordItemWire,
  RecordListWire,
  RecordWriteBody,
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
    throw error;
  }
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
