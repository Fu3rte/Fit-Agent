/**
 * Fit-Agent 前端契约 v1（D1A 契约先行；stage0 F0-01 定型）
 *
 * 本文件是前端唯一的数据形状来源：UI 组件、mock 服务器（src/mock/server.ts）
 * 与 API 封装（src/lib/api.ts）一律从此处引用类型，不得另写第三份形状。
 *
 * v1 基线对齐架构正本：SSE 事件全集与断线/刷新恢复规则（08 8.7）、Run 状态与
 * 显示映射（08 8.1/8.8）、草稿生命周期与确认事务（01 1.2–1.6）；
 * 后端对齐时改动收敛在本文件与少数渲染分支。
 */

/* ---------------------------------- 错误 ---------------------------------- */

/**
 * 机器可读错误码（409 语义对齐正本）：
 * - draft_stale：确认时业务基线冲突（base_business_version != context_version，01 1.4/1.6）；
 * - draft_modified：确认时草稿修订版本不匹配（01 1.4：拒绝确认已被修改的草稿）；
 * - conversation_busy：全局已有活跃 Run（08 8.2）；
 * - interrupted_by_restart：服务重启中断遗留 Run（08 8.4：pending/running 统一改 failed 并附此原因）；
 * - not_configured / invalid_request：未配置模型 / 请求无效。
 */
export type ErrorCode =
 | "draft_stale"
 | "draft_modified"
 | "conversation_busy"
 | "not_configured"
 | "invalid_request"
 | "interrupted_by_restart";

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

/**
 * 依据与说明（08 8.7「依据与说明结果」/8.8；stage0 已拍 D2）：有事实支持的来源、引用
 * 和简短说明，随消息携带、不独立流式化，可折叠；无内容时字段缺省，不展示空区域。
 * 渲染 UI 随阶段 5 交付，本阶段仅契约预留形状。
 */
export interface MessageEvidence {
 /** 来源/引用列表 */
 sources: string[];
 /** 简短说明 */
 summary: string;
}

export interface ChatMessage {
 id: string;
 role: "user" | "assistant";
 content: string;
 /** 关联草稿卡 */
 draft_id?: string;
 /** 依据与说明（08 8.7/8.8；可选：无内容不展示空区域） */
 evidence?: MessageEvidence;
}

/* --------------------------------- 草稿 ---------------------------------- */

export type DraftKind = "training_record" | "plan_adjust" | "profile_update";

/**
 * 草稿生命周期 Pending / Committed / Discarded（01 1.3）；Discarded 不得再提交。
 * stale 非生命周期状态（01 1.3：过期是业务基线冲突，不替代已丢弃状态），仅作为
 * 一键重算后旧草稿卡的过渡标记（01 1.6）；确认时的基线冲突以 409 draft_stale 表达。
 */
export type DraftStatus = "pending" | "committed" | "discarded" | "stale";

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
 /**
  * 草稿修订版本（01 1.3）：标识用户所见并准备确认的内容版本，防止提交已被修改的草稿；
  * 内联纠错经业务接口 revise 使其 +1（01 1.2/1.3）；确认请求携带所见修订，不匹配
  * 返回 409 draft_modified（01 1.4）。
  */
 revision: number;
 /** 生成时的统一业务版本；确认事务内校验 draft.base_business_version == context_version（01 1.3/1.4） */
 base_business_version: number;
 /** 一键重算产生的新草稿关联旧草稿（01 1.6） */
 parent_draft_id?: string;
 payload: DraftPayload;
 diff: FieldDiff[];
}

/** 幂等确认结果：重复确认返回原结果 */
export interface ConfirmResult {
 draft_id: string;
 status: "committed";
 /** 本次确认是否新提交（false = 已提交过，幂等返回原结果） */
 newly_committed: boolean;
 context_version: number;
 summary: string;
}

/**
 * 确认请求体（01 1.4 确认顺序：已提交幂等返回 → 已丢弃拒绝 → 业务版本检查 → 修订版本检查）。
 * 仅携带用户所见草稿修订版本，与服务端不一致返回 409 draft_modified（01 1.4）；
 * 确认针对服务端存储的草稿内容复查领域规则（01 1.4）。
 * 内联纠错不在确认内提交：纠错经 revise 业务接口修订待确认草稿（revision+1、展示与
 * Diff 随之更新、不自动提交），修订后再确认（01 1.2；前端接入随 stage0 F0-05）。
 */
export interface ConfirmRequest {
 /** 用户所见草稿修订版本（01 1.3） */
 revision: number;
}

/** 重算结果：新草稿 + 新旧草稿 diff */
export interface RecalcResult {
 new_draft: Draft;
 old_draft: Draft;
 draft_vs_draft_diff: FieldDiff[];
}

/* ----------------------------- Run 状态与恢复 ------------------------------ */

/**
 * Run 状态五值（08 8.1）：pending/running 为活跃态，completed/failed/cancelled 为终态，
 * 终态不可恢复；cancelled 为独立终态（用户主动取消不计执行失败）。
 * 08 8.8 显示映射（前端仅做显示映射，不新增或替换后端状态）：
 * pending|running → 处理中；completed → 已完成；cancelled → 已取消；failed → 未完成
 * （重启中断归 failed，附 interrupted_by_restart 可理解中断原因）。
 */
export type RunStatus =
 | "pending"
 | "running"
 | "completed"
 | "failed"
 | "cancelled";

/** GET /api/runs/active 中的全局 Run 当前状态（08 8.7 断线/刷新恢复规则 2/3/8） */
export interface ActiveRunInfo {
 run_id: string;
 session_id: string;
 status: RunStatus;
 /** 已保存的部分回答（stage0 已拍 D1：mock 简化「已流出整段文本 = 已保存」，分批落盘不建模） */
 saved_text: string;
 /** status = failed 时的失败原因（08 8.4/8.7 规则 8：前端显示「未完成」及中断原因） */
 error_code?: ErrorCode;
 /** 该 Run 已提出草稿的当前状态（01 1.2：当前状态经业务接口查询，不依赖历史通知） */
 drafts: Draft[];
}

/**
 * GET /api/runs/active：全局 Run 槽位当前状态查询（状态 + 已保存部分回答 + 关联草稿当前状态）。
 * run 返回全局最近一个 Run 的当前状态：活跃 pending/running，或已进入终态者——供断线/刷新后
 * 恢复「未完成/已取消」与已保存部分（08 8.7 规则 2/3、规则 8）；null = 当前无可查询 Run。
 */
export interface ActiveRunResponse {
 run: ActiveRunInfo | null;
}

/** 纠错请求：内联纠错后的完整草稿内容（01 1.2：纠错只修改待确认草稿，不自动提交） */
export interface ReviseRequest {
 payload: DraftPayload;
}

/** 纠错结果：修订后的草稿（revision + 1，payload 与 diff 随之更新；01 1.2/1.3） */
export interface ReviseResult {
 draft: Draft;
}

/** 丢弃结果（01 1.3：用户拒绝则丢弃草稿，正式数据及业务版本不变；Discarded 不得再提交） */
export interface DiscardResult {
 draft_id: string;
 status: "discarded";
}

/* --------------------------------- REST 端点 -------------------------------- */

/**
 * REST 端点清单（契约 v1）：
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
 * - GET    /api/sessions/:id/drafts     -> Draft[]（会话草稿当前状态列表；01 1.2 草稿纠错/确认/丢弃走业务接口，恢复时经此查询当前状态——08 8.7 规则 2）
 * - POST   /api/runs                    body {session_id, message, client_request_id} -> {run_id}
 *          活跃 run 存在 -> 409 conversation_busy；未配置模型 -> 409 not_configured
 * - GET    /api/runs/active             -> ActiveRunResponse（全局 Run 当前状态：状态 + 已保存部分回答 + 关联草稿当前状态；08 8.7 规则 2/3）
 * - POST   /api/runs/:id/cancel         -> {run_id, status: "cancelled"}
 * - POST   /api/drafts/:id/revise       body ReviseRequest -> ReviseResult（内联纠错走业务接口：revision+1，不自动提交——01 1.2）
 * - POST   /api/drafts/:id/confirm      body ConfirmRequest -> ConfirmResult
 *          409 draft_stale 当业务版本不一致（01 1.6）；409 draft_modified 当修订版本不匹配（01 1.4）
 * - POST   /api/drafts/:id/recalc       -> RecalcResult
 * - POST   /api/drafts/:id/discard      -> DiscardResult（丢弃待确认草稿；Discarded 不得再提交——01 1.3）
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
 * SSE 订阅：GET /api/events（原生 EventSource，A2）。
 * 事件全集按 08 8.7 收敛；SSE 只负责实时展示，断线或刷新后经业务接口查询当前状态，
 * 不依赖浏览器自动重连，不使用 Last-Event-ID 补读或事件重放（08 8.7 替代旧约定）。
 */
export type SseEvent =
 | { event: "run.started"; run_id: string }
 | { event: "message.delta"; run_id: string; text: string }
 | { event: "draft.proposed"; run_id: string; draft: Draft }
 | { event: "context.compacting"; run_id: string }
 | { event: "context.compacted"; run_id: string; note: string }
 | { event: "run.completed"; run_id: string }
 | { event: "run.cancelled"; run_id: string }
 | { event: "run.failed"; run_id: string; error_code: ErrorCode }
 /** 连接保活（08 8.7）：无业务事件 15 秒时发送；不入库、不分配 ID、不显示在聊天里 */
 | { event: "heartbeat" };
