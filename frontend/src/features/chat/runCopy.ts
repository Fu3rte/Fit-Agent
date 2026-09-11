import type { ErrorCode, RunStatus } from "@/lib/contract";

/* ------------------- 08 8.8 显示映射（仅显示，不新增后端状态） ------------------- */

/** RunStatus → 展示文案；pending/running 统一「处理中」 */
export const RUN_STATUS_COPY: Record<RunStatus, string> = {
  pending: "处理中",
  running: "处理中",
  completed: "已完成",
  cancelled: "已取消",
  failed: "未完成",
};

/** 「处理中」的次要文案：pending = 受理中，running = 执行中 */
export const runPhaseCopy = (started: boolean): string =>
  started ? "执行中" : "受理中";

/** run.failed 原因码 → 可理解文案（未列出者走兜底，不暴露错误码给用户） */
const FAILURE_REASON_COPY: Partial<Record<ErrorCode, string>> = {
  interrupted_by_restart: "服务重启导致执行中断",
  not_configured: "模型未配置或不可用",
  conversation_busy: "已有正在进行的对话",
  invalid_request: "请求无效",
  draft_stale: "依据的业务数据已变更",
  draft_modified: "草稿已被修改",
};

export const failureReasonCopy = (code: ErrorCode): string =>
  FAILURE_REASON_COPY[code] ?? "执行过程中断";

/** 断线/刷新恢复期的常驻提示（08 8.7 规则 1：只提示，不取消、不重跑、不判失败） */
export const RECOVERY_HINT = "连接中断，正在恢复";

/**
 * 查询不到终态时的兜底原因（规则 1：连接不可用本身不判 Run 失败，
 * 但服务端已无此 Run 可查时停止等待，不自动恢复执行）。
 */
export const RECOVERY_UNKNOWN_CAUSE = "无法查询到该任务的最终状态，已停止等待";
