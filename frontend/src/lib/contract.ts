/**
 * Fit-Agent 前端契约草案（D1A 契约先行）
 *
 * 本文件是前端唯一的数据形状来源：UI 组件、mock 服务器（src/mock/server.ts）
 * 与 API 封装（src/lib/api.ts）一律从此处引用类型，不得另写第三份形状。
 *
 * 契约草案：SSE 事件全集与部分字段未定，后端 spike 落地后以此文件对齐；
 * 届时改动收敛在本文件与少数渲染分支。
 */

/* ---------------------------------- 错误 ---------------------------------- */

/** 机器可读错误码（与 architecture-decisions 的 409 语义对齐） */
export type ErrorCode =
 | "draft_stale"
 | "conversation_busy"
 | "not_configured"
 | "invalid_request";

export interface ApiError {
 http_status: number;
 error_code: ErrorCode;
 message: string;
 /** 409 draft_stale 时指出冲突变更项，如「新增训练记录」 */
 detail?: string;
}

/* --------------------------------- 领域类型 -------------------------------- */

/** 训练目标 */
export type TrainingGoal = "hypertrophy" | "strength" | "general";

/** 经验水平 */
export type ExperienceLevel = "beginner" | "novice" | "intermediate";

/** 模型部署位置：本地 / 云端（PRD 5.1 界面须明确展示） */
export type ModelDeployment = "local" | "cloud";

export interface ProviderConfig {
 protocol: string;
 base_url: string;
 /** 只暴露 has_api_key，任何接口不得返回明文 Key */
 has_api_key: boolean;
 model: { name: string; deployment: ModelDeployment };
 /** 用户数据目录（演示用常量，真实值由后端 platformdirs 解析） */
 data_dir: string;
}

export interface Profile {
 goal: string;
 experience: string;
 /** 每周训练频率 */
 weekly_frequency: number;
 /** 单次可用时长（分钟） */
 session_minutes: number;
 equipment: string[];
 body_weight_kg: number;
}

/** 动作限制；restricted = 暂禁（红色徽章） */
export interface Restriction {
 name: string;
 restricted: boolean;
 note?: string;
}

/** 计划中的一个板块：推 / 拉 / 腿 */
export interface PlanBlock {
 name: string;
 /** 每周第几天（1-7），用于展示每周安排 */
 weekday: number;
 exercises: PlanExercise[];
}

export interface PlanExercise {
 name: string;
 /** 器械变式，如 杠铃/哑铃/自重 */
 variant: string;
 sets: number;
 rep_range: string;
 /** 目标 RIR，显式区间含端点（如 "1-3"） */
 target_rir: string;
 progression: string;
}

export interface PlanVersion {
 version: string;
 start_date: string;
 review_date: string;
 status: "active" | "archived";
 blocks: PlanBlock[];
}

/** 一次训练中的一组 */
export interface RecordSet {
 weight_kg?: number;
 reps?: number;
 /** 未报告保持为空（PRD 5.6：不得补造） */
 rir?: number;
 /** 工作组 / 热身组 */
 set_type: "working" | "warmup";
 /** 人工辅助标记；未申报 = false（异常申报制） */
 assisted?: boolean;
}

/** 组级三桶判定（统一用语：符合目标/未符合/待补全，不另设同义状态） */
export type SetJudgement = "met" | "unmet" | "pending";

export interface TrainingRecord {
 id: string;
 date: string;
 /** 归属：新增 / 更正 */
 kind: "new" | "correction";
 /** 正式 / 待补全；待补全不参与 PR 与完成率 */
 status: "formal" | "pending_completion";
 exercise: string;
 variant: string;
 sets: RecordSet[];
 /** 热身摘要保留原文，如「递增至 60kg」 */
 warmup_summary?: string;
 /** 关联的当次安排快照；无对照安排为空（仅作历史表现数据） */
 schedule_snapshot?: string | null;
 /** 历史更正：修订说明 */
 revision_note?: string;
}

/** 计划周完成率；rate 为 null 时显示「暂无」（不得显示 0%/100%） */
export interface WeekCompletion {
 week: string;
 planned: number;
 completed: number;
 rate: number | null;
}

/** 三桶组数：符合目标 / 未符合 / 待补全 */
export interface Buckets {
 met: number;
 unmet: number;
 pending: number;
}

export interface PrEntry {
 exercise: string;
 variant: string;
 best_weight_kg: number;
 /** 同重量下单组最高次数（不累计多组） */
 best_reps_at_weight: number;
}

export interface StatsSummary {
 per_week: WeekCompletion[];
 buckets: Buckets;
 prs: PrEntry[];
 data_updated_at: string;
}

/** 复盘沉淀；stale = 依据已变更·可重新生成（不静默改写） */
export interface ReviewDoc {
 text: string;
 stale: boolean;
 generated_at: string;
}

export interface SessionSummary {
 id: string;
 title: string;
 updated_at: string;
}

export interface ChatMessage {
 id: string;
 role: "user" | "assistant";
 content: string;
 /** 关联草稿卡 */
 draft_id?: string;
}

/* --------------------------------- 草稿 ---------------------------------- */

export type DraftKind = "training_record" | "plan_adjust" | "profile_update";

export type DraftStatus = "pending" | "committed" | "stale";

/** 结构化字段级 Diff（A4：业务 Diff 是「旧值→新值」字段对，不是文本 diff） */
export interface FieldDiff {
 /** 字段路径，如「卧推 · 组数」 */
 field: string;
 old_value?: string;
 new_value: string;
}

/** 训练记录草稿载荷 */
export interface RecordDraftPayload {
 date: string;
 exercise: string;
 variant: string;
 warmup_summary?: string;
 sets: RecordSet[];
}

/** 计划调整草稿载荷 */
export interface PlanDraftPayload {
 title: string;
 diff: FieldDiff[];
}

/** 档案变更草稿载荷 */
export interface ProfileDraftPayload {
 title: string;
 diff: FieldDiff[];
}

export type DraftPayload =
 | RecordDraftPayload
 | PlanDraftPayload
 | ProfileDraftPayload;

export interface Draft {
 id: string;
 kind: DraftKind;
 status: DraftStatus;
 /** 生成时的统一业务版本；确认事务内校验 draft.base_business_version == context_version */
 base_business_version: number;
 /** 一键重算产生的新草稿关联旧草稿 */
 parent_draft_id?: string;
 payload: DraftPayload;
 diff: FieldDiff[];
}

/** 幂等确认结果：重复确认返回原结果 */
export interface ConfirmResult {
 draft_id: string;
 status: "committed";
 /** 本次确认是否新提交（false = 幂等重放） */
 newly_committed: boolean;
 context_version: number;
 summary: string;
}

/**
 * 确认请求体：payload 为内联纠错后的最终草稿内容。
 * 前端纠错不绕过后端校验：后端确认事务内以最终内容复查领域规则（architecture-decisions 已拍）。
 */
export interface ConfirmRequest {
 /** 可选；缺省时按服务端存储的草稿内容提交 */
 payload?: DraftPayload;
}

/** 重算结果：新草稿 + 新旧草稿 diff */
export interface RecalcResult {
 new_draft: Draft;
 old_draft: Draft;
 draft_vs_draft_diff: FieldDiff[];
}

/* --------------------------------- REST 端点 -------------------------------- */

/**
 * REST 端点清单（契约草案）：
 * - GET    /api/provider                -> ProviderConfig
 * - PUT    /api/provider/api-key        body {api_key} -> {has_api_key: true}
 * - DELETE /api/provider/api-key        -> {has_api_key: false}
 * - GET    /api/profile                 -> {profile: Profile, restrictions: Restriction[], context_version: number}
 * - GET    /api/records                 -> {records: TrainingRecord[]}
 * - GET    /api/stats                   -> StatsSummary
 * - GET    /api/review                  -> ReviewDoc
 * - GET    /api/sessions                -> SessionSummary[]
 * - POST   /api/sessions                -> SessionSummary
 * - GET    /api/sessions/:id/messages   -> ChatMessage[]
 * - POST   /api/runs                    body {session_id, message, client_request_id} -> {run_id}
 *          活跃 run 存在 -> 409 conversation_busy；未配置模型 -> 409 not_configured
 * - POST   /api/runs/:id/cancel         -> {run_id, status: "cancelled"}
 * - POST   /api/drafts/:id/confirm      body ConfirmRequest -> ConfirmResult；409 draft_stale 当版本不一致
 * - POST   /api/drafts/:id/recalc       -> RecalcResult
 */

export interface RunRequest {
 session_id: string;
 message: string;
 client_request_id: string;
}

export interface RunHandle {
 run_id: string;
}

export interface ProfileResponse {
 profile: Profile;
 restrictions: Restriction[];
 context_version: number;
 /** STAGED-SHARED-EDIT（lane 3b，supervisor 批准）：契约遗漏；/profile 当前计划卡所需 */
 plan?: PlanVersion;
}

/* --------------------------------- SSE 事件 -------------------------------- */

/**
 * SSE 订阅：GET /api/events（原生 EventSource，浏览器重连自动携带 Last-Event-ID）。
 * 事件全集为草案，未定项以后端 spike 为准对齐。
 */
export type SseEvent =
 | { event: "run.started"; run_id: string }
 | { event: "message.delta"; run_id: string; text: string }
 | { event: "draft.proposed"; run_id: string; draft: Draft }
 | { event: "context.compacted"; run_id: string; note: string }
 | { event: "run.completed"; run_id: string }
 | { event: "run.cancelled"; run_id: string }
 | { event: "run.failed"; run_id: string; error_code: ErrorCode }
 | { event: "error"; error_code: ErrorCode; message: string };
