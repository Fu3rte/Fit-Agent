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

/**
 * 正式档案：建档事实的唯一来源（PRD §5.2 六类事实；02 2.1）。
 * 完整档案须含全部事实且 body_weight_kg 必填（stage1 已拍 2026-09-09：缺失时继续追问，
 * 不生成完整档案草稿）；未收集的字段在草稿侧缺省表达，不写默认值。
 */
export interface Profile {
 goal: string;
 experience: string;
 /** 每周训练频率 */
 weekly_frequency: number;
 /** 单次可用时长（分钟） */
 session_minutes: number;
 equipment: string[];
 /** 体重（kg）；建档必填 */
 body_weight_kg: number;
 /**
  * 当前身体情况（PRD §5.2；02 2.1/2.3）：用户报告原文逐条保存（如「深蹲时膝盖锐痛」），
  * 存储层只保存事实、不做医学分类、不诊断、不自动增删禁忌；六类安全症状的匹配在读取时进行。
  * 空数组 = 用户明确表示无身体情况，与字段缺省（尚未收集）可区分。
  */
 body_conditions: string[];
}

/**
 * 动作限制（PRD §5.2/§5.7；02 2.2）。只保存当前有效的已确认限制，不承载生命周期/状态
 * 语义——不新增观察中／暂禁／永久等状态；「暂禁」仅为 /profile 红色徽章展示文案，
 * 解除 = 经对话草稿确认后删除该条（02 2.2）。增删只能经对话草稿确认（02 2.1/2.2）。
 */
export interface Restriction {
 /** 限制对象名称，如具体动作「杠铃颈后推举」或动作模式「颈前深蹲」 */
 name: string;
 /** 粒度（02 2.2）：具体动作 / 动作模式（如深蹲、髋铰链、水平推） */
 scope: "specific_action" | "movement_pattern";
 /** 说明：用户报告原文或确认备注 */
 note?: string;
}

/* ------------------------------ 动作目录（参考） ----------------------------- */

/** 记录口径恰三类（03 章已拍；不新增辅助负重型、不建第四类） */
export type ExerciseRecordType = "reps_weight" | "reps_bodyweight" | "time";

/** 负重口径（stage1 S1-03 已拍五种；自重／计时型为 null，不虚构口径） */
export type LoadConvention =
 | "barbell_includes_bar_total"
 | "dumbbell_per_hand"
 | "machine_pin_displayed_value"
 | "plate_loaded_total_excluding_empty"
 | "unilateral_setting_per_side";

/**
 * 目录动作（03 3.1–3.2）：镜像后端 `exercises` 表中做计划候选判定所需的字段，
 * 不另造第二份动作身份；媒体、attribution 等展示字段不入镜像。
 * 后端 `recommendable` 列不在镜像内：stage1 全为 0 且本阶段不修改后端值，
 * 候选资格由 mock 按已拍最小标准（来源已核对 + active + 器械／记录／负重口径／模式完整）判定。
 */
export interface CatalogExercise {
 /** 稳定动作身份（03 3.1：停用后仍可按 ID 读） */
 id: string;
 /** 中文标准名（一个身份一个标准名） */
 standard_name_zh: string;
 /** 器械变式，如 barbell / dumbbell / bodyweight / cable / leverage_machine */
 equipment_variant: string;
 record_type: ExerciseRecordType;
 /** 仅记录口径 reps_weight 需要；其余为 null */
 load_convention: LoadConvention | null;
 /** 单侧动作：左右分别计数 */
 unilateral: boolean;
 /** 停用不删除：false = 已停用，不得成为候选 */
 active: boolean;
 /** 动作模式（13 项已拍词表的子集，多归属仅跨界高复合型动作） */
 modes: string[];
 /** 来源与许可：核不上的条目不导入；空 = 未经来源核对 */
 source_ref: string;
}

/* ---------------------------------- 计划 ---------------------------------- */

/**
 * 校准（3.2 已拍 2026-09-10）：无可信训练记录时不给具体起始重量，也不按体重、
 * 估算 1RM 或默认杠重猜测，只展示逐级试重步骤与通过／停止标准。
 * 基于可信历史的负荷与渐进建议留待记录数据接入后的阶段（plans/stage2.md §3.2）。
 */
export interface Calibration {
 status: "needs_calibration" | "calibrated";
 /** 逐级试重步骤（从轻到重，不含具体起始重量） */
 steps: string[];
 /** 通过标准：稳定完成处方次数下限，且落在目标 RIR 区间 */
 pass_criteria: string;
 /** 停止条件：疼痛／已报告的安全症状、动作明显失稳、无法满足目标 RIR（停止后不继续加重） */
 stop_criteria: string;
}

/**
 * 计划动作（3.4）：目录身份 + 处方 + 校准状态。
 * `name`／`variant` 为展示用文案，身份以 `exercise_id` 为准（不按名称猜测身份）。
 */
export interface PlanExercise {
 /** 目录动作身份（CatalogExercise.id） */
 exercise_id: string;
 /** 展示名（取自目录 standard_name_zh） */
 name: string;
 /** 器械变式展示文案，如 杠铃/哑铃/自重 */
 variant: string;
 /** 所需器械（取自目录 equipment_variant；候选筛选按此与档案器械比对） */
 equipment: string;
 /** 动作模式（取自目录 modes；限制判定按模式集合交集） */
 modes: string[];
 sets: number;
 rep_range: string;
 /** 目标 RIR，显式区间含端点（如 "1-3"） */
 target_rir: string;
 progression: string;
 calibration: Calibration;
}

/** 计划中的一个板块：推 / 拉 / 腿 */
export interface PlanBlock {
 name: string;
 /** 每周第几天（1-7），用于展示每周安排 */
 weekday: number;
 /** 该训练日预计时长（分钟）；须不超过档案单次可用时长 */
 estimated_minutes: number;
 exercises: PlanExercise[];
}

/** 计划生效范围（3.4）：应生效区间与每周训练日；不是覆盖任意频率的通用排程算法 */
export interface PlanScope {
 start_date: string;
 review_date: string;
 /** 每周训练日（1-7，周一起） */
 weekdays: number[];
}

export interface PlanVersion {
 version: string;
 start_date: string;
 review_date: string;
 status: "active" | "archived";
 blocks: PlanBlock[];
}

/**
 * 具体日程（04 4.4）：`[开始日期, 复核日期)` 内逐个应训练日；日历休息日不写成应训练日。
 * 到期即锁（locked）；替换计划只取消旧版未来未锁定日程（cancelled），已锁定日程不动。
 */
export interface PlanScheduleEntry {
 id: string;
 /** 归属计划版本 */
 plan_version: string;
 /** 每周第几天（1-7，周一起） */
 weekday: number;
 date: string;
 status: "scheduled" | "locked" | "cancelled";
}

/**
 * 计划安全复核投影（04 4.5；02 2.2/2.3）：请求基于当前计划的指导时按最新限制与身体情况
 * 复核整份计划。任一动作或模式冲突即整份阻断（不输出其余「未冲突」动作的处方），
 * 命中安全症状独立阻断；不新增「部分可用」计划状态。
 */
export interface PlanSafetyReview {
 /** 复核所依据的业务版本（限制／身体情况变更后须重新复核） */
 context_version: number;
 reviewed_at: string;
 /** true = 可给出基于该计划的处方；false = 整份阻断 */
 usable: boolean;
 /** 身体情况命中安全症状而独立阻断（02 2.3：建议线下专业评估） */
 red_flag_blocked: boolean;
 /** 命中的限制冲突（空 = 无冲突） */
 conflicts: PlanSafetyConflict[];
}

export interface PlanSafetyConflict {
 /** 计划中被命中的动作身份（CatalogExercise.id） */
 exercise_id: string;
 exercise_name: string;
 /** 命中的限制：具体动作或动作模式（02 2.2） */
 restriction: Restriction;
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

/**
 * 计划草稿的可替换动作候选（stage2 F2-03）：服务端按当前正式档案器械与有效限制过滤后的
 * 目录动作，供草稿卡轻量纠错选择；不是完整目录浏览器，也不扩目录。
 */
export interface PlanCandidate {
 /** 目录动作身份（CatalogExercise.id） */
 exercise_id: string;
 /** 展示名（取自目录 standard_name_zh） */
 name: string;
 /** 器械变式展示文案，如 杠铃/哑铃/自重 */
 variant: string;
}

/**
 * 计划调整草稿载荷（stage2 F2-01：从「标题 + 文本 Diff」升级为结构化载荷）。
 * 拟议计划版本、生效范围、具体日程与旧日程取消清单是结构化字段，不再塞进文本 Diff；
 * `diff` 仍是服务端派生的字段级展示 Diff（A4）。
 * 结构字段由计划生成（F2-02）填写：缺省 = 该草稿尚未带结构化载荷，不伪造空日程。
 */
export interface PlanDraftPayload {
 title: string;
 diff: FieldDiff[];
 /** 拟议完整计划版本（确认前正式计划与日程不变） */
 plan?: PlanVersion;
 /** 生效范围（开始／复核日期与每周训练日） */
 scope?: PlanScope;
 /** 拟议具体日程（`[开始日期, 复核日期)` 内应训练日） */
 schedules?: PlanScheduleEntry[];
 /** 替换计划时旧版未来未锁定日程的取消清单（已锁定日程不动） */
 cancellations?: PlanScheduleEntry[];
 /** 可替换动作候选（F2-03；服务端按当前档案与限制给出，纠错时刷新） */
 candidates?: PlanCandidate[];
 /** 可选长期档案补丁（如「以后只能用哑铃」）：与计划、日程一次确认、一次版本递增 */
 profile_patch?: ProfileDraftPayload;
}

/**
 * 档案变更草稿载荷（结构化档案字段；PRD §5.2 六类事实）。
 * 只承载已收集事实：字段缺省 = 尚未收集（未知），与显式空值（equipment: [] 无器械、
 * body_conditions: [] 明确无身体情况）可区分；不得以默认值补造（PRD §5.2）。
 * diff 不在载荷内：由服务端对比新旧档案派生字段级「旧值→新值」（A4；Draft.diff）。
 */
export interface ProfileDraftPayload {
 /** 已收集的档案事实（目标与经验、频率、时长、器械、体重、身体情况） */
 profile: Partial<Profile>;
 /** 拟议的限制集合（只含当前有效限制）；缺省 = 尚未收集，与空数组（明确无限制）可区分 */
 restrictions?: Restriction[];
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
 * - GET    /api/profile                 -> {profile: Profile | null, restrictions: Restriction[], context_version: number,
 *                                            plan?: PlanVersion, schedules?: PlanScheduleEntry[], plan_safety?: PlanSafetyReview}
 *          profile = null 表示尚未建档（不返回占位档案，避免把未知写成默认值）；
 *          计划、日程与安全复核只读投影（计划变更仍只能从对话发起）
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

/**
 * GET /api/profile（PRD §5.2；02 2.1/2.2）：正式档案 + 当前有效限制 + 业务版本。
 * profile = null 表示尚未建档：此时 restrictions 为空数组、plan 缺省；未建档引导的呈现
 * 随阶段 1 F1-04。
 */
export interface ProfileResponse {
 profile: Profile | null;
 restrictions: Restriction[];
 context_version: number;
 /** STAGED-SHARED-EDIT（lane 3b，supervisor 批准）：契约遗漏；/profile 当前计划卡所需 */
 plan?: PlanVersion;
 /** 具体日程（stage2 F2-01/F2-05；plan 缺省时同样缺省）：含历史版本条目（旧版取消、已到期锁定），状态语义见 PlanScheduleEntry */
 schedules?: PlanScheduleEntry[];
 /** 当前计划的安全复核投影（stage2 F2-01/F2-05）：按最新身体情况与限制复核，与计划状态分开表达，不新增「部分可用」状态 */
 plan_safety?: PlanSafetyReview;
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
