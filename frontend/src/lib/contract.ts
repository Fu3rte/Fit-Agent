/**
 * Fit-Agent 前端契约 v1（D1A 契约先行；stage0 F0-01 定型；stage3 D9 全面对齐）
 *
 * 本文件是前端唯一的数据形状来源：UI 组件、mock 服务器（src/mock/server.ts）
 * 与 API 封装（src/lib/api.ts）一律从此处引用类型，不得另写第三份形状。
 *
 * v1 基线对齐架构正本：SSE 事件全集与断线/刷新恢复规则（08 8.7）、Run 状态与
 * 显示映射（08 8.1/8.8）、草稿生命周期与确认事务（01 1.2–1.6）。
 * Stage3 对齐：D9 计划 payload（backend/domain/plan/schema.py）、D3 校准口径、
 * S3-08 安排目标、S3-10 记录草稿、锁定双态（04 4.2）。
 */

/* ---------------------------------- 错误 ---------------------------------- */

/**
 * 机器可读错误码（409 语义对齐正本）：
 * - draft_stale：确认时业务基线冲突（base_business_version != context_version，01 1.4/1.6）；
 * - draft_modified：确认时草稿修订版本不匹配（01 1.4：拒绝确认已被修改的草稿）；
 * - conversation_busy：全局已有活跃 Run（08 8.2）；
 * - interrupted_by_restart：服务重启中断遗留 Run（08 8.4：pending/running 统一改 failed 并附此原因）；
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

export interface SessionSummary {
  id: string;
  title: string;
  updated_at: string;
}

export interface MessageEvidence {
  sources: string[];
  summary: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  draft_id?: string;
  evidence?: MessageEvidence;
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

export type DraftKind =
  | "training_record"
  | "plan"
  | "profile_update"
  | "arrangement"
  | "training_void";

/**
 * 草稿生命周期 Pending / Committed / Discarded（01 1.3）；Discarded 不得再提交。
 * stale 非生命周期状态，仅作为一键重算后旧草稿卡的过渡标记（01 1.6）。
 */
export type DraftStatus = "pending" | "committed" | "discarded" | "stale";

/** 结构化字段级 Diff（A4：业务 Diff 是「旧值→新值」字段对，不是文本 diff） */
export interface FieldDiff {
  /** 字段路径，如「卧推 · 组数」 */
  field: string;
  old_value?: string;
  new_value: string;
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
}

/**
 * 幂等确认结果（交接 F4 提交凭据 + mock 展示扩展）：
 * 重复确认返回原凭据（committed_* 与 kind 结果 id），不重复写、不 cv+1；
 * newly_committed / summary 保留为 mock 展示扩展。
 */
export interface ConfirmResult {
  draft_id: string;
  status: "committed";
  newly_committed: boolean;
  context_version: number;
  summary: string;
  /** 提交时的草稿修订版本 */
  committed_revision: number;
  /** 提交后的业务版本（context_version） */
  committed_business_version: number;
  /** kind=plan：启用的计划版本 */
  plan_version?: string;
  /** kind=arrangement：写入的安排修订 id */
  arrangement_revision_id?: string;
  /** kind=training_record：稳定训练身份 id（handover F4） */
  training_session_id?: string;
}

export interface ConfirmRequest {
  revision: number;
}

/** 重算结果：新草稿 + 新旧草稿 diff */
export interface RecalcResult {
  new_draft: Draft;
  old_draft: Draft;
  draft_vs_draft_diff: FieldDiff[];
}

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

export interface ActiveRunInfo {
  run_id: string;
  session_id: string;
  status: RunStatus;
  saved_text: string;
  error_code?: ErrorCode;
  drafts: Draft[];
}

export interface ActiveRunResponse {
  run: ActiveRunInfo | null;
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

/** 已接受安排读回（stage3 拍板的 mock 端点；镜像 arrangement_revisions 行） */
export interface AcceptedArrangement {
  id: string;
  accepted_at: string;
  target: ArrangementTarget;
}

/**
 * REST 端点清单（契约 v1）：
 * - GET    /api/provider                -> ProviderConfig
 * - PUT    /api/provider/api-key        body {api_key} -> {has_api_key: true}
 * - DELETE /api/provider/api-key        -> {has_api_key: false}
 * - GET    /api/profile                 -> ProfileResponse
 * - GET    /api/records                 -> {records: TrainingRecord[]}
 * - GET    /api/arrangements            -> {arrangements: AcceptedArrangement[]}
 * - GET    /api/stats                   -> StatsSummary
 * - GET    /api/review                  -> ReviewDoc（最新条投影；无条目时空态）
 * - GET    /api/sessions                -> SessionSummary[]
 * - POST   /api/sessions                -> SessionSummary
 * - GET    /api/sessions/:id/messages   -> ChatMessage[]
 * - GET    /api/sessions/:id/drafts     -> Draft[]
 * - POST   /api/runs                    body RunRequest -> RunHandle
 * - GET    /api/runs/active             -> ActiveRunResponse
 * - POST   /api/runs/:id/cancel         -> {run_id, status}
 * - POST   /api/drafts/:id/revise       body ReviseRequest -> ReviseResult
 * - POST   /api/drafts/:id/confirm      body ConfirmRequest -> ConfirmResult
 * - POST   /api/drafts/:id/recalc       body RecalcRequest -> RecalcResult
 * - POST   /api/drafts/:id/discard      -> DiscardResult
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
  profile: Profile | null;
  restrictions: Restriction[];
  context_version: number;
  plan?: PlanVersion;
  schedules?: PlanScheduleEntry[];
  plan_safety?: PlanSafetyReview;
}

/* --------------------------------- SSE 事件 -------------------------------- */

export type SseEvent =
  | { event: "run.started"; run_id: string }
  | { event: "message.delta"; run_id: string; text: string }
  | { event: "draft.proposed"; run_id: string; draft: Draft }
  | { event: "context.compacting"; run_id: string }
  | { event: "context.compacted"; run_id: string; note: string }
  | { event: "run.completed"; run_id: string }
  | { event: "run.cancelled"; run_id: string }
  | { event: "run.failed"; run_id: string; error_code: ErrorCode }
  | { event: "heartbeat" };
