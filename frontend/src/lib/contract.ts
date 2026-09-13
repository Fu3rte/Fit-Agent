/**
 * Fit-Agent 前端契约 v1（D1A 契约先行；stage0 F0-01 定型；stage3 D9 全面对齐）
 *
 * 本文件是前端唯一的数据形状来源：UI 组件与 API 封装（src/lib/api.ts）
 * 一律从此处引用类型，不得另写第三份形状。
 * F6-02d 起 src/mock/ 已删除；类型仅对齐真实后端传输契约。
 *
 * v1 基线对齐架构正本：SSE 事件全集与断线/刷新恢复规则（08 8.7）、Run 状态与
 * 显示映射（08 8.1/8.8）、草稿生命周期与确认事务（01 1.2–1.6）。
 * Stage3 对齐：D9 计划 payload（backend/domain/plan/schema.py）、D3 校准口径、
 * S3-08 安排目标、S3-10 记录草稿、锁定双态（04 4.2）。
 */

/* ---------------------------------- 错误 ---------------------------------- */

/**
 * 机器可读错误码（409 语义 + Run 终态失败原因，对齐 backend/runtime/error_codes.py 封闭集合）：
 * - draft_stale：确认时业务基线冲突（base_business_version != context_version，01 1.4/1.6）；
 * - draft_modified：确认时草稿修订版本不匹配（01 1.4：拒绝确认已被修改的草稿）；
 * - conversation_busy：全局已有活跃 Run（08 8.2；HTTP 409，不创建 Run）；
 * - interrupted_by_restart / model_request_timeout / run_timeout /
 *   context_budget_exceeded / model_request_failed：Run 终态失败原因（随 Run 查询与
 *   status 事件给出，不是 HTTP 错误码；08 8.1/8.4/8.5/8.8）；
 * - not_configured / invalid_request：未配置模型 / 请求无效；
 * - plan_action_unavailable：计划引用动作目录缺失时的具体安全阻断（S3-07）。
 */
export type ErrorCode =
  | "draft_stale"
  | "draft_modified"
  | "conversation_busy"
  | "not_configured"
  | "invalid_request"
  | "interrupted_by_restart"
  | "model_request_timeout"
  | "run_timeout"
  | "context_budget_exceeded"
  | "model_request_failed"
  | "plan_action_unavailable";

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
  // F6-02d：无 data_dir——freeze 契约不投影数据目录（见 settings 页「见数据目录配置」）
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

/** 目录记录口径恰三类（03 章已拍；与计划侧处方口径的映射在 plan/rules） */
export type CatalogRecordType = "reps_weight" | "reps_bodyweight" | "time";

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
 */
export interface CatalogExercise {
  /** 稳定动作身份（03 3.1：停用后仍可按 ID 读） */
  id: string;
  /** 中文标准名（一个身份一个标准名） */
  standard_name_zh: string;
  /** 器械变式，如 barbell / dumbbell / bodyweight / cable / leverage_machine */
  equipment_variant: string;
  record_type: CatalogRecordType;
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

/* ---------------------------------- 计划（D9） ---------------------------------- */

/** 处方 record_type（计划侧三类，与目录三类经 rules 映射） */
export type PrescriptionRecordType =
  | "external_load_reps"
  | "bodyweight_reps"
  | "timed";

/** 渐进方式（D9）：method 与记录口径匹配表见 plan/rules；custom 也必须有明确 rule */
export type ProgressionMethod =
  | "double_progression"
  | "repetition_progression"
  | "duration_progression"
  | "custom";

/** 显式闭区间：min ≤ max，不表达「约」「左右」等模糊口径 */
export interface IntRange {
  min: number;
  max: number;
}

/** 次数型处方（D9）：work_sets + reps_range + 可选 target_rir（仅展示参考，D3） */
export interface RepsPrescription {
  kind: "reps";
  work_sets: number;
  reps_range: IntRange;
  /** 目标 RIR 展示参考（D3：RIR 不是校准硬性条件） */
  target_rir?: IntRange;
}

/** 计时型处方（D9）：work_sets + duration_seconds_range（秒）；首版不强制 RIR */
export interface TimedPrescription {
  kind: "timed";
  work_sets: number;
  duration_seconds_range: IntRange;
}

export type PlanPrescription = RepsPrescription | TimedPrescription;

/** 可信历史给出的外加负重（D9）；无可信记录时生成侧不得构造 */
export interface VerifiedLoad {
  kind: "verified";
  value: number;
  unit: string;
  load_notation: string;
  basis_record_revision_id?: string;
}

/**
 * 需要校准（D3）：只给逐级试重步骤与通过／停止标准，结构上不携带任何重量。
 * pass_criteria = 稳定完成该组处方次数下限；stop_criteria = 疼痛/失稳/完不成下限——
 * RIR 不作通过硬性条件。
 */
export interface NeedsCalibration {
  kind: "needs_calibration";
  steps: string[];
  pass_criteria: string;
  stop_criteria: string;
}

/** 负荷互斥：verified 与 needs_calibration 两态；仅 external_load_reps 携带 */
export type PlanLoad = VerifiedLoad | NeedsCalibration;

/** 渐进方式 + 明确规则文本（D9：method + rule，custom 也必须有 rule） */
export interface Progression {
  method: ProgressionMethod;
  rule: string;
}

/** 确认时冻结的展示副本（D9）：仅供展示；校验仍以目录与最新限制为准 */
export interface DisplaySnapshot {
  name: string;
  equipment_variant: string;
  load_convention: string | null;
}

/**
 * 当次安排的逐项处置恰四类（04 4.3 / S4-04）：保留、减载、同等刺激替换、局部跳过。
 * 快照始终保存该项完整最终目标，处置只是标注；计划 payload 与既有快照可缺省（None）。
 */
export type ArrangementItemDisposition =
  | "keep"
  | "deload"
  | "equivalent_replace"
  | "local_skip";

/** 一个计划动作：目录稳定身份 + 处方 + 负荷 + 渐进（D9） */
export interface PlanExerciseItem {
  /** workout 内唯一 */
  item_key: string;
  exercise_id: string;
  display_snapshot: DisplaySnapshot;
  record_type: PrescriptionRecordType;
  prescription: PlanPrescription;
  /** 仅 external_load_reps 携带；verified | needs_calibration 互斥 */
  load?: PlanLoad;
  progression: Progression;
  /** 当次安排快照的处置标注；计划 payload 与既有快照缺省（S4-04） */
  disposition?: ArrangementItemDisposition;
  /** 同等刺激替换的替代动作身份；仅 disposition=equivalent_replace 时出现 */
  replacement_exercise_id?: string | null;
}

/** 可被 calendar_cycle 引用的训练处方（D9）：workout_key 版本内唯一 */
export interface PlanWorkout {
  workout_key: string;
  name: string;
  estimated_minutes: number;
  exercises: PlanExerciseItem[];
}

/** 日历循环中的训练槽：引用同版本存在的 workout_key；投影时生成一条应训练名额 */
export type CycleSlot =
  | { kind: "workout"; workout_key: string }
  | { kind: "rest" };

/** 日历循环（D9）：anchor_date 定相位，slots 长度即循环长度；rest 不生成名额 */
export interface CalendarCycle {
  anchor_date: string; // YYYY-MM-DD
  slots: CycleSlot[];
}

/** D9 plan_versions.payload_json（schema_version=1） */
export interface PlanPayload {
  schema_version: 1;
  /** 如 "ppl" */
  template_key?: string;
  plan_workouts: PlanWorkout[];
  calendar_cycle: CalendarCycle;
}

/**
 * 计划版本行（行字段不进 payload）：
 * starts_on/review_on/mode 在 plan_versions 行上；payload 保持 D9 纯净。
 */
export interface PlanVersion {
  version: string;
  starts_on: string;
  review_on: string;
  mode: "regular" | "return";
  status: "active" | "archived";
  payload: PlanPayload;
}

/** 拟议投影出的一条应训练名额（草稿展示用） */
export interface ProjectedSession {
  plan_workout_key: string;
  scheduled_on: string;
}

/**
 * 具体日程（04 4.2/4.4）：锁定双态。
 * - stored_status：业务表写入的标记；未写标记时可能是 scheduled；
 * - locked_by_date_rule：按固定业务时区日期规则 business_date >= date 或已确认完成/漏练；
 * - locked_effective = stored locked ∪ locked_by_date_rule；UI 与「能否改期」用这个。
 * 到期即锁不需要后台任务：停机跨过训练日后重开，仍按日期规则判锁定。
 */
export interface PlanScheduleEntry {
  id: string;
  plan_version: string;
  /** scheduled_on */
  date: string;
  plan_workout_key: string;
  /** 由 date 按业务时区派生，展示用 */
  weekday: number;
  /** 存储状态：业务表写入的标记；未写标记时可能是 scheduled */
  stored_status: "scheduled" | "locked" | "cancelled";
  /** 按固定业务时区日期规则：business_date(today) >= date 或已确认完成/漏练 */
  locked_by_date_rule: boolean;
  /** effective = stored locked ∪ locked_by_date_rule；UI 与「能否改期」用这个 */
  locked_effective: boolean;
}

/** 替换计划时拟议取消的旧版日程（S3-04：仅拟议预览，不落正式取消） */
export interface ProposedSessionCancellation {
  scheduled_session_id: string;
  plan_version: string;
  plan_workout_key: string;
  scheduled_on: string;
}

/**
 * 计划安全复核投影（04 4.5；02 2.2/2.3）：任一动作或模式冲突即整份阻断；
 * 目录身份读不到时 usable=false 且 block_code=plan_action_unavailable（S3-07）。
 * 展示形状（mock / 本地投影）；真实后端传输形状见 PlanSafetyWire。
 */
export interface PlanSafetyReview {
  context_version: number;
  reviewed_at: string;
  /** true = 可给出基于该计划的处方；false = 整份阻断 */
  usable: boolean;
  red_flag_blocked: boolean;
  conflicts: PlanSafetyConflict[];
  /** 可选具体阻断码（目录缺失等）；限制冲突/红旗时缺省 */
  block_code?: "plan_action_unavailable";
}

export interface PlanSafetyConflict {
  exercise_id: string;
  exercise_name: string;
  restriction: Restriction;
}

/* -------------------------- 只读传输形状（stage6 F6-02b） ------------------- */
/* 对齐 backend api/dto.py + stage6-transport-freeze §1.2；页面经 lib/readModels 映射。 */

export type FactState = "unknown" | "denied" | "known";

/** S2-07 三态事实：unknown/denied 不携带值；known 带值 */
export interface FactDto<T> {
  state: FactState;
  value: T | null;
}

/** 限制传输身份（02 2.2）：scope + target，不是展示名 */
export interface ActionRestrictionWire {
  scope: "specific_action" | "movement_pattern";
  target: string;
}

/** GET /api/profile 的 profile 载荷（八项三态事实；无 plan/safety/restrictions 外置数组） */
export interface ProfileFactsDto {
  training_goal: FactDto<string>;
  training_experience: FactDto<string>;
  weekly_frequency: FactDto<number>;
  session_duration_minutes: FactDto<number>;
  available_equipment: FactDto<string[]>;
  action_restrictions: FactDto<ActionRestrictionWire[]>;
  body_conditions: FactDto<string[]>;
  body_weight_kg: FactDto<number>;
}

/** 一条日程传输行（schedule_dto） */
export interface PlanScheduleWire {
  id: string;
  plan_version_id: string;
  plan_workout_key: string;
  scheduled_on: string;
  /** 1=周一 */
  weekday: number;
  cancelled: boolean;
  cancelled_at: string | null;
  locked_at: string | null;
  lock: {
    stored: boolean;
    by_business_date: boolean;
    effective: boolean;
  };
  status: "cancelled" | "locked" | "scheduled";
}

/** GET /api/plan 与 /api/plans/{id} 的 plan 视图（plan_view_dto） */
export interface PlanViewWire {
  id: string;
  version: string;
  source_plan_version_id: string | null;
  starts_on: string;
  review_on: string;
  mode: "regular" | "return";
  is_current: boolean;
  confirmed_at: string;
  template_key: string | null;
  /** D9 payload JSON（schema_version=1） */
  plan: PlanPayload;
  schedules: PlanScheduleWire[];
}

export interface PlanResponseWire {
  plan: PlanViewWire | null;
}

/** 安全复核传输形状（plan_safety_dto）：conflict.restriction 为 scope/target */
export interface PlanSafetyWire {
  context_version: number;
  reviewed_at: string;
  usable: boolean;
  red_flag_blocked: boolean;
  conflicts: Array<{
    exercise_id: string;
    exercise_name: string;
    restriction: ActionRestrictionWire;
    matched_modes: string[];
  }>;
  action_unavailable: boolean;
  block_code: "plan_action_unavailable" | null;
  unknown_exercise_ids: string[];
  reasons: string[];
  /** clarifications ≠ 安全放行 */
  clarifications: string[];
}

/** GET /api/plan/guidance（guidance_dto） */
export interface PlanGuidanceWire {
  plan: PlanViewWire;
  safety: PlanSafetyWire;
}

export interface PlanGuidanceResponseWire {
  guidance: PlanGuidanceWire | null;
}

/** 记录列表条目（record_dto）：稳定身份 + 当前修订摘要 + 存储契约 payload */
export interface RecordListItemWire {
  id: string;
  created_at: string;
  revision: {
    id: string;
    revision_no: number;
    status: "incomplete" | "valid" | "voided";
    occurred_on: string;
  } | null;
  record: RecordStoragePayload;
}

/** GET /api/records 响应 */
export interface RecordListResponseWire {
  records: RecordListItemWire[];
}

/** GET /api/records/{id} 响应 */
export interface RecordItemResponseWire {
  record: RecordListItemWire;
}

/** 组级三桶判定传输（target_judgement_dto） */
export interface RecordJudgementWire {
  session_revision_id: string;
  has_comparison: boolean;
  is_return_phase: boolean;
  buckets: Buckets;
}

export interface RecordJudgementResponseWire {
  judgement: RecordJudgementWire | null;
}

/** 完成率传输（week_completion_dto）；rate 为 0–1 比率，null=「暂无」 */
export interface WeekCompletionWire {
  plan_version_id: string;
  week_no: number;
  week_start: string;
  week_end: string;
  planned: number;
  completed: number;
  rate: number | null;
}

export interface StatsCompletionResponseWire {
  completion: WeekCompletionWire | null;
}

/** PR 传输（/api/stats/pr）；load_kg_key 为 kg×1000 整数键 */
export interface PrWire {
  exercise_id: string;
  load_notation: string;
  load_kg_key: number | null;
  max_load_kg_key: number | null;
  best_reps: number | null;
}

export interface StatsPrResponseWire {
  pr: PrWire;
}

/** 一条复盘传输（review_dto）：正文字段 body_markdown */
export interface ReviewWire {
  id: string;
  body_markdown: string;
  stale: boolean;
  generated_at: string;
  source_revision_ids: string[];
  basis: ReviewBasisWire | null;
}

/** 复盘依据快照传输（review_basis_to_json）：冻结 per_week/prs，无 buckets */
export interface ReviewBasisWire {
  schema_version: number;
  per_week: Array<{
    plan_version_id: string;
    week_no: number;
    week_start: string;
    week_end: string;
    numerator: number;
    denominator: number;
  }>;
  prs: Array<{
    exercise_id: string;
    load_notation: string;
    load_kg_key: number;
    best_reps: number;
  }>;
}

export interface ReviewListResponseWire {
  reviews: ReviewWire[];
}

export interface ReviewItemResponseWire {
  review: ReviewWire;
}

/** 记录存储契约（record_draft_to_json / exercises 展平行） */
export interface RecordStoragePayload {
  schema_version: number;
  occurred_on: string;
  training_session_id?: string | null;
  arrangement_revision_id?: string | null;
  started_at?: string | null;
  time_precision?: string | null;
  completion_declared?: boolean;
  is_return_phase?: boolean;
  feedback?: Record<string, unknown> | null;
  exercises: Array<{
    position: number;
    exercise_id: string;
    record_type: CatalogRecordType;
    load_notation?: string | null;
    target_item_key?: string | null;
    warmup_summary_text?: string | null;
    sets: SetFacts[];
  }>;
}

/* --------------------------------- 记录事实 --------------------------------- */

/** 组类型：热身／工作（05 5.2）。未明确保持 null/undefined，不默认 work */
export type SetType = "warmup" | "work";

/** 人工辅助（05 5.5）：未申报 = null（不默认 none） */
export type Assistance = "none" | "spotter_only" | "assisted";

/** 负重原始值：十进制原文 + 单位（不归一单位） */
export interface RawLoad {
  value_text: string;
  unit: "kg" | "lb";
}

/** 一组实际训练事实（05 5.2）：可空字段一律表示「未明确」，不得用默认值补造 */
export interface SetFacts {
  set_no: number;
  set_type?: SetType | null;
  load?: RawLoad | null;
  reps?: number | null;
  duration_seconds?: number | null;
  /** 不补 0 */
  rir?: number | null;
  assistance?: Assistance | null;
  assisted_reps?: number | null;
  quality_text?: string | null;
}

/** 展示用组（旧 RecordSet 兼容视图；优先直接用 SetFacts） */
export interface RecordSet {
  weight_kg?: number;
  reps?: number;
  rir?: number;
  set_type: "working" | "warmup";
  assisted?: boolean;
  /** 服务端派生：按当次安排、只看次数；无对照安排或热身组不写（F3-05） */
  judgement?: SetJudgement;
}

/** 组级三桶判定（统一用语：符合目标/未符合/待补全，不另设同义状态） */
export type SetJudgement = "met" | "unmet" | "pending";

/**
 * 训练记录对外列表（展示友好形状）：status 对齐当前修订状态
 * valid / incomplete / voided（05 5.3）：
 * - incomplete 不进 PR 与完成率分子；
 * - voided = 当前修订为作废：整次退出统计（完成率分子／三桶／PR 均不计该次）、
 *   不物理删除、不回退旧有效版本，且该训练身份为终态（2026-09-13 拍 A）。
 * scheduled_session_id / arrangement_revision_id 为关联键（显式携带，不从日期推断）；
 * judgement / comparison 为服务端派生的展示扩展（stage3 F3-01）。
 */
export interface TrainingRecord {
  id: string;
  date: string;
  kind: "new" | "correction";
  /** 当前修订状态（对齐 RecordRevisionStatus + voided；替换 formal/pending_completion） */
  status: "valid" | "incomplete" | "voided";
  exercise: string;
  variant: string;
  sets: RecordSet[];
  warmup_summary?: string;
  schedule_snapshot?: string | null;
  revision_note?: string;
  /** 关联的应训练日程 id；无对照计划的加练为 null/缺省 */
  scheduled_session_id?: string | null;
  /** 关联的已接受安排修订 id；只由调用方显式携带，绝不从日期推断 */
  arrangement_revision_id?: string | null;
  /** 稳定训练身份 id（确认时建立或复用；同日多练各自身份、补充复用既有身份） */
  training_session_id?: string | null;
  /** 服务端派生：按当次安排快照的组级三桶；无对照安排为 null */
  judgement?: Buckets | null;
  /** 服务端派生：对照摘要（原计划组数 / 当次安排组数 / 接受时间） */
  comparison?: {
    planned_sets?: number;
    arranged_sets?: number;
    accepted_at?: string;
  };
  /** 旧修订只读追溯摘要（F4-01，供 /records 内联展示）；缺省 = 无旧修订或未提供 */
  revisions?: TrainingRevision[];
  /**
   * 回归期标签（F5-05；04 4.6）：接回版本（mode=return）生效后确认的训练记录标 "return"。
   * 缺省 / "normal" = 常规口径。回归期不进 PR；工作组判定仍展示。恢复常规=新常规版本（不重激活旧版）。
   */
  period?: "normal" | "return";
}

/**
 * 旧修订只读追溯摘要（05 5.3；F4-01，/records 内联只读追溯，不新增页面概念）：
 * 训练身份维度的一条旧修订展示行——状态、日期、组事实摘要、修订说明与确认时间；
 * 只读展示，不可编辑、不可再提交；当前修订仍由 TrainingRecord 表达。
 */
export interface TrainingRevision {
  id: string;
  /** 该修订当时的状态（05 5.3：incomplete / valid / voided） */
  status: "valid" | "incomplete" | "voided";
  /** 该修订的训练发生日期（YYYY-MM-DD） */
  occurred_on: string;
  /** 该修订确认／追加时间（ISO 8601） */
  confirmed_at: string;
  exercise: string;
  variant: string;
  /** 该修订的组事实摘要（展示兼容形状，同 RecordSet） */
  sets: RecordSet[];
  warmup_summary?: string;
  revision_note?: string;
}

/** 修订状态：由事实完整性派生，不单独存第二份与事实冲突的状态（05 5.3） */
export type RecordRevisionStatus = "incomplete" | "valid";

/** 一条修订里的一个动作事实 + 其全部组（05 5.3） */
export interface DraftExerciseLog {
  /** 修订内动作顺序（1 起、连续） */
  position: number;
  exercise_id: string;
  /** 目录词表 */
  record_type: CatalogRecordType;
  load_notation?: string | null;
  target_item_key?: string | null;
  warmup_summary_text?: string | null;
  sets: SetFacts[];
}

/**
 * 训练记录草稿的完整拟议载荷（S3-10）：
 * - training_session_id：null = 新增；有 id = 补充/更正既有身份（同日多练各自身份）；
 * - arrangement_revision_id：可空且只由调用方显式给出，绝不从日期推断；
 * - 修订状态由 deriveRecordDraftStatus 按事实完整性派生。
 */
export interface RecordDraftPayload {
  occurred_on: string;
  training_session_id: string | null;
  arrangement_revision_id?: string | null;
  exercises: DraftExerciseLog[];
}

/**
 * 作废草稿载荷（05 5.3；F4-01）：只承载「作废哪一次训练身份」，不承载任何可编辑事实；
 * 确认后追加 voided 修订并切换当前修订指针，整次退出统计、不物理删除、不回退旧有效版本。
 * 独立 kind（不扩展 RecordDraftPayload）：作废与更正语义互斥——作废无事实可编辑。
 */
export interface TrainingVoidPayload {
  training_session_id: string;
}

/* --------------------------------- 统计/复盘 -------------------------------- */

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
  best_reps_at_weight: number;
}

export interface StatsSummary {
  per_week: WeekCompletion[];
  buckets: Buckets;
  prs: PrEntry[];
  data_updated_at: string;
}

/** 复盘依据快照（F5-01；语义对齐 06 6.4 / S4-08 review_basis）：生成时冻结的统计与来源修订 */
export interface ReviewBasis {
  per_week: WeekCompletion[];
  buckets: Buckets;
  prs: PrEntry[];
  data_updated_at: string;
  /** 生成时刻相关来源修订 id（当前训练修订 + 当次安排修订） */
  source_revision_ids: string[];
}

/** 复盘存储条目（F5-01；append-only，重生成追加不覆盖；UI 不展示历史，探针可读回） */
export interface ReviewEntry {
  id: string;
  text: string;
  generated_at: string;
  /** stale = 来源相对生成时是否变化（不静默改写正文与 generated_at） */
  stale: boolean;
  basis: ReviewBasis;
}

/** 复盘沉淀投影：GET /api/review 返回最新一条；无条目时为空态；stale = 依据已变更·可重新生成（不静默改写） */
export interface ReviewDoc {
  text: string;
  stale: boolean;
  generated_at: string;
  /** 最新条的依据快照（空态无） */
  basis?: ReviewBasis;
}

/**
 * 会话摘要（仅 mock 列表端点仍在用；真实后端暂无 `GET /api/sessions` 列表，
 * 会话身份经 URL `?s=` 与 `POST /api/sessions` 获得）。
 */
export interface SessionSummary {
  id: string;
  title: string;
  updated_at: string;
}

/**
 * 会话查询内嵌消息（stage4 §6 / dto.session_messages_dto）：
 * 按 Run 分组投影——用户请求 + 可见回答（完整）或未完成的部分回答（complete=false）。
 */
export interface SessionMessage {
  seq: number;
  run_id: string;
  role: "user" | "assistant";
  kind: "user_request" | "answer" | "partial";
  text: string;
  complete: boolean;
}

/* --------------------------------- 当次安排（S3-08） ------------------------ */

/**
 * 当次临时调整的可变字段（对齐 S4-04 / backend ArrangementAdjustment）：
 * 处置四类 + 减载参数 + 替换身份；disposition 缺省时沿用 legacy（只减组 / 只升 RIR）。
 */
export interface ArrangementAdjustment {
  item_key: string;
  work_sets?: number;
  target_rir?: IntRange;
  disposition?: ArrangementItemDisposition;
  reps_range?: IntRange;
  load_value?: number;
  replacement_exercise_id?: string | null;
}

/**
 * 一次接受的当次目标快照（04 4.3、S3-08）：绑定 + 该次完整目标，不是差异补丁。
 * exercises 是完整目标（照抄绑定版本该训练日，只允许已拍临时调整改组次/目标 RIR）。
 * accepted_at 是 arrangement_revisions 行字段，不进本结构。
 */
export interface ArrangementTarget {
  schema_version: 1;
  scheduled_session_id: string;
  plan_version: string;
  plan_workout_key: string;
  scheduled_on: string;
  exercises: PlanExerciseItem[];
  /** 有差异时必填非空白 */
  adjustment_reason?: string;
}

/** 当次安排草稿载荷（展示完整目标；与绑定计划的差异可由服务端派生放 draft.diff） */
export interface ArrangementDraftPayload {
  target: ArrangementTarget;
}

/* --------------------------------- 草稿 ---------------------------------- */

/**
 * 草稿 kind 封闭集（交接 F2）：真实后端恰四种
 * `profile_update | plan | training_record | arrangement`（backend app/draft_repo.py
 * DRAFT_KINDS）。作废经 `POST /api/drafts/{id}/void` 对 training_record 追加
 * voided 修订，不产生独立 kind。F6-02d：`training_void` 已随 mock 删除。
 */
export type DraftKind =
  | "profile_update"
  | "plan"
  | "training_record"
  | "arrangement";

/**
 * 草稿生命周期 Pending / Committed / Discarded（01 1.3）；Discarded 不得再提交。
 * stale 非生命周期状态，仅作为一键重算后旧草稿卡的过渡标记（01 1.6）。
 */
export type DraftStatus = "pending" | "committed" | "discarded" | "stale";

/**
 * 结构化字段级 Diff 的展示形状（A4：业务 Diff 是「旧值→新值」字段对，不是文本 diff）。
 * 后端传输形状是 WireFieldDiff（{field, before, after, changed}，F6-02c 已拍）；
 * 前端展示层仍用 old_value/new_value，网络边界经 normalizeFieldDiffRows 映射
 * （src/lib/diffNormalize.ts），页面与 mock 内部构造沿用展示形状。
 */
export interface FieldDiff {
  /** 字段路径，如「卧推 · 组数」 */
  field: string;
  /** 无旧值 = 新增（展示「新增」徽章） */
  old_value?: string;
  new_value: string;
}

/**
 * 后端 Diff 传输形状（F6-02c 已拍：{field, before, after, changed}）。
 * before/after 可能是字符串，也可能是结构化值（档案 Fact {state,value}、
 * 计划/安排结构化字段）——展示前必须经 normalizeFieldDiffRows 映射为 FieldDiff。
 */
export interface WireFieldDiff {
  field: string;
  before?: unknown;
  after?: unknown;
  /** false 的行不进展示（基线派生时含未变字段） */
  changed?: boolean;
}

/**
 * 计划草稿的可替换动作候选（stage2 F2-03）：服务端按当前正式档案器械与有效限制过滤后的
 * 目录动作，供草稿卡轻量纠错选择。
 */
export interface PlanCandidate {
  exercise_id: string;
  name: string;
  variant: string;
}

/**
 * 计划调整草稿载荷（对齐 D9）：拟议完整计划版本（行字段 + payload）、具体日程与
 * 旧日程取消清单是结构化字段；diff 仍是服务端派生的字段级展示 Diff（A4）。
 * 结构字段缺省 = 该草稿尚未带结构化载荷，不伪造空日程。
 */
export interface PlanDraftPayload {
  title: string;
  diff: FieldDiff[];
  /** 拟议完整计划版本（确认前正式计划与日程不变） */
  plan?: PlanVersion;
  /** 拟议具体日程（[starts_on, review_on) 内应训练日） */
  schedules?: PlanScheduleEntry[];
  /** 替换计划时旧版未来未锁定日程的取消清单预览 */
  cancellations?: ProposedSessionCancellation[];
  candidates?: PlanCandidate[];
  /** 可选长期档案补丁（如「以后只能用哑铃」）：与计划、日程一次确认、一次版本递增 */
  profile_patch?: ProfileDraftPayload;
}

/**
 * 档案变更草稿载荷（结构化档案字段；PRD §5.2 六类事实）。
 * 只承载已收集事实：字段缺省 = 尚未收集（未知），与显式空值可区分。
 */
export interface ProfileDraftPayload {
  profile: Partial<Profile>;
  restrictions?: Restriction[];
}

export type DraftPayload =
  | RecordDraftPayload
  | PlanDraftPayload
  | ProfileDraftPayload
  | ArrangementDraftPayload
  | TrainingVoidPayload;

export interface Draft {
  id: string;
  kind: DraftKind;
  status: DraftStatus;
  revision: number;
  base_business_version: number;
  parent_draft_id?: string;
  payload: DraftPayload;
  diff: FieldDiff[];
  /** 一键重算的新旧草稿 Diff（后端 draft_dto.parent_diff；仅重算子草稿携带） */
  parent_diff?: FieldDiff[] | null;
  /** 已提交草稿的提交凭据（持久化，不随后续业务版本改写；draft_dto） */
  committed_revision?: number | null;
  committed_business_version?: number | null;
}

/**
 * 幂等确认结果（交接 F4 / dto.any_commit_result_dto）：
 * 公共凭据 draft_id + status="committed" + committed_revision + committed_business_version；
 * 首次确认按 kind 附带正式事实身份（plan_version(_id) / arrangement_revision* /
 * training_session_id 等）。重复确认返回同一份持久化凭据，不重复写、不递增业务版本。
 */
export interface ConfirmResult {
  draft_id: string;
  status: "committed";
  committed_revision: number;
  committed_business_version: number;
  /** kind=plan：计划版本身份 */
  plan_version_id?: string;
  plan_version?: string;
  /** kind=arrangement：当次安排修订身份 */
  arrangement_revision_id?: string;
  arrangement_revision_no?: number;
  scheduled_session_id?: string;
  accepted_at?: string;
  /** kind=training_record：稳定训练身份与修订 */
  training_session_id?: string;
  session_revision_id?: string;
  revision_no?: number;
  revision_status?: string;
}

export interface ConfirmRequest {
  revision: number;
}

// F6-02d：RecalcResult 仅 mock 同步旧链路使用，已随 mock 删除；
// 真实后端一键重算经 `{created, run}` + draft 事件到达。

/**
 * 一键重算请求（01 1.6 / S4-08）：携带幂等 client_request_id；
 * 同一旧草稿已有 Pending 子草稿时服务端按幂等规则返回同一子草稿（01 1.6）。
 */
export interface RecalcRequest {
  client_request_id: string;
}

/* ----------------------------- Run 状态与恢复 ------------------------------ */

export type RunStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

/** Run 行传输投影（dto.run_dto，stage4 §6）：五态权威 + 可理解失败原因 */
export interface Run {
  run_id: string;
  conversation_id: string;
  status: RunStatus;
  error_code?: ErrorCode | null;
  retry_of_run_id?: string | null;
  created_at?: string;
  updated_at?: string;
}

/** 提交对话请求 / 重试 / 重算的统一响应（routes_chat：{created, run}） */
export interface SubmitRequestResult {
  /** false = client_request_id 幂等命中已有 Run，不重启执行 */
  created: boolean;
  run: Run;
}

/**
 * 会话查询投影（dto.session_dto；08 8.7 断线/刷新/重启后的恢复入口）：
 * 消息内嵌 + 全部 Run 状态；不重放 SSE、不重复拼接回答。草稿当前状态另走
 * `GET /api/sessions/{id}/drafts`（本结构不复制草稿语义）。
 */
export interface SessionDetail {
  session_id: string;
  created_at?: string;
  runs: Run[];
  messages: SessionMessage[];
}

/**
 * 纠错请求（交接 F3）：revision 为用户所见草稿修订版本；不匹配按 409 draft_modified 拒绝。
 */
export interface ReviseRequest {
  payload: DraftPayload;
  revision: number;
}

export interface ReviseResult {
  draft: Draft;
}

export interface DiscardResult {
  draft_id: string;
  status: "discarded";
}

/* --------------------------------- REST 端点 -------------------------------- */

/**
 * 已接受安排（一次接受的当次目标快照投影）：由前端从 records / 会话安排草稿
 * 投影（src/lib/arrangements.ts），无独立后端端点。id 为安排修订身份
 * （投影来源可得时携带）。F6-02d：mock GET /api/arrangements 已随 mock 删除。
 */
export interface AcceptedArrangement {
  id: string;
  accepted_at: string;
  target: ArrangementTarget;
}

/**
 * REST 端点清单（契约 v1，stage6 F6-02b 只读看板对齐；F6-02d 删 mock 后仅真实后端）：
 * - GET    /api/provider                -> ProviderConfig
 * - PUT    /api/provider/api-key        body {api_key} -> {has_api_key: true}
 * - DELETE /api/provider/api-key        -> {has_api_key: false}
 * - GET    /api/profile                 -> ProfileResponse（S2-07：无 plan/safety）
 * - GET    /api/plan                    -> PlanResponseWire（{plan|null}）
 * - GET    /api/plan/guidance           -> PlanGuidanceResponseWire
 * - GET    /api/records                 -> RecordListResponseWire（存储契约，页面映射）
 * - GET    /api/records/:id             -> RecordItemResponseWire
 * - GET    /api/records/:id/judgement   -> RecordJudgementResponseWire
 * - GET    /api/stats/completion        -> StatsCompletionResponseWire（必填 plan_version_id+week_no）
 * - GET    /api/stats/pr                -> StatsPrResponseWire（必填 exercise_id+load_notation）
 * - GET    /api/reviews                 -> ReviewListResponseWire（最新条=当前复盘）
 * - GET    /api/reviews/:id             -> ReviewItemResponseWire
 * - POST   /api/sessions                body {} -> SessionDetail
 * - GET    /api/sessions/:id            -> SessionDetail
 * - POST   /api/sessions/:id/requests   body {client_request_id, text} -> SubmitRequestResult
 * - GET    /api/sessions/:id/drafts     -> Draft[]
 * - GET    /api/runs/:id                -> {run: Run}
 * - GET    /api/runs/:id/events         -> SSE
 * - POST   /api/runs/:id/retry|/cancel
 * - POST   /api/drafts/:id/revise|confirm|recalc|discard|void
 *
 * 已废弃（前端不得再调用）：GET /api/stats（聚合）、GET /api/review（单文档）、
 * GET /api/arrangements（仅 mock 遗留，02d 随 mock 删除）、POST /api/runs、
 * GET /api/events、GET /api/runs/active、GET /api/sessions/:id/messages。
 */

export interface ProfileResponse {
  /** null = 未建档；已建档为 S2-07 三态事实（不再内嵌 plan/schedules/safety，F1） */
  profile: ProfileFactsDto | null;
  context_version: number;
}

/* --------------------------------- SSE 事件 -------------------------------- */

/**
 * Run 级 SSE 产品事件（backend runtime/events.py EVENT_KINDS 封闭集合）：
 * 订阅 `GET /api/runs/{run_id}/events`；订阅即先发当前 status；终态 status 后流结束；
 * 断线不取消、不重跑、不重放——恢复一律走会话查询（08 8.7）。
 * draft 事件只是已持久化草稿的引用（身份 + 修订），当前状态以业务查询为准。
 */
export type SseEvent =
  | {
      event: "status";
      run_id: string;
      status: RunStatus;
      error_code?: ErrorCode | null;
    }
  | { event: "answer"; run_id: string; text: string }
  | { event: "rationale"; run_id: string }
  | {
      event: "draft";
      run_id: string;
      draft_id: string;
      kind: DraftKind;
      revision: number;
      status: DraftStatus;
    }
  | { event: "compression"; run_id: string; state: "started" | "finished" }
  | { event: "heartbeat" };
