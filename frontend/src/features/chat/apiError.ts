import type { ApiError } from "@/lib/contract";

/** api.ts 抛出的是 Error & Partial<ApiError>；安全取回机器错误信息 */
export const toApiError = (error: unknown): ApiError => {
  const err = error as Partial<ApiError> | null;
  return {
    http_status: err?.http_status ?? 0,
    error_code: err?.error_code ?? "invalid_request",
    message: err?.message ?? "请求失败",
    detail: err?.detail,
  };
};
