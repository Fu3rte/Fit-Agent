/**
 * Fit-Agent 前端契约（Stage 1 表单写入 + Stage 2 统计只读）。
 *
 * 本文件是前端唯一的数据形状来源：UI 组件与 API 封装（src/lib/api.ts）一律从此处
 * 引用类型，不得另写第三份形状。对齐 ``backend/api/dto.py`` 与 routes_profile /
 * routes_records / routes_plans / routes_stats 的传输形状。
 *
 * 冻结口径（讨论总结 §7、§9；REFACTOR_PLAN §5/§6）：无 RIR、无 context_version、
 * 无通用草稿/修订链。Provider 传输形状见文末「模型配置」段（用户拍板：设置页可编辑）。
 */

/** 统一错误码：Stage 1 传输层只表达「请求或输入不合法」 */
export type ErrorCode = "invalid_request";

export interface ApiError {
  http_status: number;
  error_code: ErrorCode;
  message: string;
}

/** 目录记录口径恰三类（与 001_initial.sql exercises CHECK 同集合） */
export type CatalogRecordType = "reps_weight" | "reps_bodyweight" | "time";

/** 负重口径六种（自重／计时型为 null，不虚构口径）；
 *  external_added_weight = 外加重量（不含体重），用于独立负重引体 */
export type LoadConvention =
  | "barbell_includes_bar_total"
  | "dumbbell_per_hand"
  | "machine_pin_displayed_value"
  | "plate_loaded_total_excluding_empty"
  | "unilateral_setting_per_side"
  | "external_added_weight";

/** 动作目录一行（exercise_dto）：表单动作选择与负重口径来源 */
export interface ExerciseWire {
  id: string;
  standard_name_zh: string;
  equipment_variant: string;
  record_type: CatalogRecordType;
  load_convention: LoadConvention | null;
  min_load_increment_kg: number | null;
  recommendable: boolean;
  modes: string[];
  source_ref: string;
  attribution: string;
}

export interface ExerciseListWire {
  exercises: ExerciseWire[];
}

/** 画像单字段三态（无 context_version）：known 带值，unknown／denied 值必须为 null */
export type FactState = "unknown" | "denied" | "known";

export interface ProfileFactWire<T> {
  state: FactState;
  value: T | null;
}

/** GET／PUT /api/profile 的 profile 载荷：七字段逐字拼写 */
export interface ProfileFactsWire {
  training_goal: ProfileFactWire<string>;
  weekly_frequency: ProfileFactWire<number>;
  available_equipment: ProfileFactWire<string[]>;
  explicit_preferences: ProfileFactWire<string[]>;
  current_level: ProfileFactWire<string>;
  known_injuries: ProfileFactWire<string[]>;
  forbidden_exercise_ids: ProfileFactWire<string[]>;
}

export interface ProfileResponseWire {
  /** null = 未建档（不得显示成完整画像） */
  profile: ProfileFactsWire | null;
}

/** PUT /api/profile 请求体：整份覆盖，未填写用 unknown、明确为空用 denied */
export type ProfileWriteBody = ProfileFactsWire;

/** 组类型固定三态（与 workout_sets CHECK 同集合；没有「未申报」态） */
export type SetTypeWire = "work" | "warmup" | "assisted";

/** 提交的一组训练事实；set_no 由后端按提交顺序对同一动作分配 1,2,3…（前端不送）
 *  reps 与 duration_seconds 按所选动作的记录口径二选一：外加重量／自重回填 reps，计时回填秒数 */
export interface WorkoutSetInputWire {
  exercise_id: string;
  set_type: SetTypeWire;
  reps: number | null;
  /** 目录 load_convention：自重／计时型必须为 null（与目录不符后端拒绝） */
  load_convention: LoadConvention | null;
  weight_kg: number | null;
  /** 计时动作的单组秒数（不小于 1、无业务上限）；非计时动作为 null */
  duration_seconds: number | null;
}

/** POST／PUT /api/records 请求体；plan_session_id 为 null 即额外训练 */
export interface RecordWriteBody {
  performed_on: string;
  plan_session_id?: number | null;
  auto_link?: boolean;
  sets: WorkoutSetInputWire[];
}

/** 一次训练（record_dto） */
export interface RecordWire {
  id: number;
  performed_on: string;
  plan_session_id: number | null;
  sets: Array<{
    exercise_id: string;
    set_no: number;
    set_type: SetTypeWire;
    load_convention: LoadConvention | null;
    weight_kg: number | null;
    /** 计时组为 null（不补 0） */
    reps: number | null;
    /** 非计时组为 null（不补 0） */
    duration_seconds: number | null;
  }>;
}

export interface RecordListWire {
  records: RecordWire[];
}

export interface RecordItemWire {
  record: RecordWire;
}

/** 当天可关联的计划日程候选（未取消且未被其他训练关联） */
export interface PlanSessionCandidateWire {
  id: number;
  plan_id: number;
  scheduled_on: string;
}

export interface PlanSessionCandidatesWire {
  sessions: PlanSessionCandidateWire[];
}

/** 一条身体指标（body_metric_dto）；body_fat_pct 为 null 表示该次未记录体脂 */
export interface BodyMetricWire {
  id: number;
  measured_on: string;
  weight_kg: number;
  body_fat_pct: number | null;
}

/** POST／PUT /api/body-metrics 请求体；体脂留空即不记录（不补 0） */
export interface BodyMetricWriteBody {
  measured_on: string;
  weight_kg: number;
  body_fat_pct?: number | null;
}

export interface BodyMetricListWire {
  metrics: BodyMetricWire[];
}

export interface BodyMetricItemWire {
  metric: BodyMetricWire;
}

/** 一个计划版本（plan_dto） */
export interface PlanWire {
  id: number;
  version: number;
  status: "draft" | "active" | "archived" | "rejected";
  source_plan_id: number | null;
  /** 结构化计划内容：形状即 PlanDraftWire（与 domain/plans/schema.py 的 PlanDraft 同一 Schema） */
  structured_content: unknown;
  evaluator_result: unknown | null;
  created_at: string;
  confirmed_at: string | null;
  archived_at: string | null;
}

export interface PlanListWire {
  plans: PlanWire[];
}

/* 计划内容的判别联合（plan_draft_schema）：字段与 domain/plans/schema.py 逐字对应 */

/** 负荷判别联合：``status`` 是判别键；没有有效历史时为 needs_calibration，不带任何重量 */
export type LoadWire =
  | {
      status: "known";
      weight_kg: number;
      source_workout_session_id: number;
      source_set_no: number;
    }
  | { status: "needs_calibration" };

/** 三类处方：``type`` 是判别键，字段严格互斥（只有外加负重次数处方携带 load） */
export type PrescriptionWire =
  | {
      type: "weighted_reps";
      reps_min: number;
      reps_max: number;
      progression_note: string | null;
      load: LoadWire;
    }
  | {
      type: "bodyweight_reps";
      reps_min: number;
      reps_max: number;
      progression_note: string | null;
    }
  | {
      type: "timed";
      duration_seconds_min: number;
      duration_seconds_max: number;
      progression_note: string | null;
    };

/** 计划里的一个动作：稳定身份、组数与一个处方 */
export interface PlannedExerciseWire {
  exercise_id: string;
  sets: number;
  prescription: PrescriptionWire;
}

/** 一个训练日：窗口内的一个日期与它的动作（同日不重复同一动作） */
export interface TrainingDayWire {
  scheduled_on: string;
  exercises: PlannedExerciseWire[];
}

/** 统一计划草案：``weekly_frequency`` 与 ``training_days`` 数量由后端强约束相等 */
export interface PlanDraftWire {
  goal: string;
  starts_on: string;
  explanation: string;
  weekly_frequency: number;
  training_days: TrainingDayWire[];
}

export interface PlanItemWire {
  /** null = 没有正式启用（或未启用的）计划 */
  plan: PlanWire | null;
}

/** 一条计划日程（plan_session_dto） */
export interface PlanSessionWire {
  id: number;
  plan_id: number;
  scheduled_on: string;
  cancelled_at: string | null;
}

export interface PlanSessionListWire {
  sessions: PlanSessionWire[];
}

/** 三类 PB（与 dto.personal_best_dto 同集合）；没有容量 PB，也没有估算 1RM */
export type PersonalBestTypeWire = "weight_pb" | "reps_pb" | "duration_pb";

/** 一条现算 PB：数值、适用重量／负重口径，加来源训练、组序号与来源日期 */
export interface PersonalBestWire {
  exercise_id: string;
  exercise_name: string;
  pb_type: PersonalBestTypeWire;
  /** 单位随 pb_type：weight_pb 为 kg、reps_pb 为次数、duration_pb 为秒数 */
  value: number;
  load_convention: LoadConvention | null;
  /** weight_pb 时等于 value；纯自重次数 PB 与计时 PB 为 null */
  weight_kg: number | null;
  workout_session_id: number;
  set_no: number;
  performed_on: string;
}

/** 数据是否足够给出变化值：ok 有最近两条记录，insufficient_data 只有一条，no_data 无记录 */
export type TrendStatusWire = "ok" | "no_data" | "insufficient_data";

/** 最近两条记录的变化；status 不是 ok 时取值字段全为 null（不补 0，也不把单条记录当变化） */
export interface MetricChangeWire {
  status: TrendStatusWire;
  current: number | null;
  current_on: string | null;
  previous: number | null;
  previous_on: string | null;
  change: number | null;
}

/** 距上次训练天数；无训练历史时 status 为 no_data 且 days 为 null */
export interface WorkoutGapWire {
  status: TrendStatusWire;
  days: number | null;
  last_performed_on: string | null;
}

/** 趋势折线上的一个原始点：只在真实存在记录（体脂为非空）的日期出点，不按日补 0 */
export interface MetricPointWire {
  measured_on: string;
  value: number;
}

/** 力量趋势上的一点：value 为截至该日期的累计 PB（历史最好成绩，曲线不下降） */
export interface StrengthPointWire {
  performed_on: string;
  value: number;
}

/** 一个力量趋势系列；Stage 2 看板只透传不渲染该曲线，也不提供动作选择 UI */
export interface StrengthTrendWire {
  exercise_id: string;
  exercise_name: string;
  pb_type: PersonalBestTypeWire;
  load_convention: LoadConvention | null;
  /** 三类系列都不带分组重量：weight_pb 的累计值本身就是重量，此处恒为 null */
  weight_kg: number | null;
  points: StrengthPointWire[];
}

/** 确定性趋势摘要：只含体重变化、体脂变化与停训天数，不含容量／完成率／效果评价 */
export interface TrendSummaryWire {
  weight_change: MetricChangeWire;
  body_fat_change: MetricChangeWire;
  days_since_last_workout: WorkoutGapWire;
}

/** GET /api/stats/trends 的 trends 载荷：窗口、两类原始点、力量系列与摘要 */
export interface TrendsWire {
  window_days: number;
  from: string;
  to: string;
  weight: MetricPointWire[];
  /** 体脂未记录的日期不出点，不用 0 补线 */
  body_fat: MetricPointWire[];
  /** 后端按截至各日期的累计 PB 计算；看板不展示该字段 */
  strength: StrengthTrendWire[];
  trend_summary: TrendSummaryWire;
}

/** 单次日程状态（与后端 CalendarSessionStatus 同集合） */
export type CalendarSessionStatusWire = "cancelled" | "incomplete" | "complete";

/** 月历上的一条计划日程事实；落在 scheduled_on 当天，完成状态由关联训练现算 */
export interface CalendarPlanSessionWire {
  id: number;
  scheduled_on: string;
  status: CalendarSessionStatusWire;
  /** 非空即已完成该日程 */
  workout_session_id: number | null;
  /** 完成该日程的真实训练日期，跨月时仍返回 */
  actual_performed_on: string | null;
}

/** 月历上的一次实际训练事实；plan_session_id 为 null 即额外训练 */
export interface CalendarWorkoutWire {
  id: number;
  performed_on: string;
  plan_session_id: number | null;
}

/** 月历上的一天：只含当天真实发生的日程与训练，空白日期不出条目（不生成「休息日」） */
export interface CalendarDayWire {
  date: string;
  plan_sessions: CalendarPlanSessionWire[];
  workouts: CalendarWorkoutWire[];
}

/** GET /api/stats/calendar 的 calendar 载荷：只读当前 active 计划的日程 */
export interface CalendarMonthWire {
  month: string;
  from: string;
  to: string;
  days: CalendarDayWire[];
}

export interface PersonalBestListWire {
  personal_bests: PersonalBestWire[];
}

export interface TrendsResponseWire {
  trends: TrendsWire;
}

export interface CalendarResponseWire {
  calendar: CalendarMonthWire;
}

/** POST /api/agent/run 请求体：conversation_id 由前端生成 UUID，即 Checkpointer 的 thread_id */
export interface AgentRunBody {
  conversation_id: string;
  request: string;
  /** 缺省 false：已有同类 draft 时按 §3.4 直接复用，不调模型 */
  regenerate?: boolean;
}

/** POST /api/agent/confirm 与 /api/agent/reject 请求体：会话身份 + 目标计划身份 */
export interface AgentPlanBody {
  conversation_id: string;
  plan_id: number;
}

/** confirm／reject 的响应：落库后的计划行（激活成功或幂等返回既有行） */
export interface AgentPlanResponseWire {
  plan: PlanWire;
}

/**
 * POST /api/agent/confirm-workout 请求体（stage6.md §2.4.2）：用户修改后的完整确认载荷。
 *
 * 只提交 `waiting` 结构化字段，不从 `message.text` 反解（§2.5.2）；本端点不要求服务端证明该
 * conversation_id 此前完成过一次自然语言解析。
 */
export interface ConfirmWorkoutBody {
  conversation_id: string;
  performed_on: string;
  sets: WorkoutSetConfirmWire[];
  plan_session_id: number | null;
  auto_link: boolean;
}

/** confirm-workout 响应：落库训练事实 ＋ 重新现算的 PB（与表单写入的传输对象同一形状） */
export interface ConfirmWorkoutResponseWire {
  workout_session: RecordWire;
  personal_bests: PersonalBestWire[];
}

/** 五类 SSE 产品事件名（与后端 AgentEventName 同一封闭集合，不发送别的名字） */
export type AgentEventNameWire =
  "node" | "message" | "waiting" | "done" | "error";

/** 当前 Graph 节点／阶段名 */
export interface AgentNodeEventWire {
  event: "node";
  data: { name: string };
}

/** 面向用户的可见文本（安全提示、表单引导、统计解释、二次阻断说明） */
export interface AgentMessageEventWire {
  event: "message";
  data: { text: string };
}

/** 计划确认路径的 `waiting`（Stage 5 契约）：已持久化 draft，只带 draft 身份 */
export interface AgentWaitingEventWire {
  event: "waiting";
  data: { draft_plan_id: number };
}

/**
 * 自然语言打卡的一条组事实（stage6.md §2.2 的 `WorkoutSetBody`）：`waiting.workout.sets` 与
 * confirm-workout 请求体共用同一形状。
 *
 * 与表单 `WorkoutSetInputWire` 的差别只有 `set_no`：自然语言提取结果已给出组序号，确认 UI 提交的
 * 是用户修改后的完整值（表单路径的组序号由后端按提交顺序分配，前端不送）。
 */
export interface WorkoutSetConfirmWire {
  exercise_id: string;
  set_no: number;
  set_type: SetTypeWire;
  reps: number | null;
  load_convention: LoadConvention | null;
  weight_kg: number | null;
  duration_seconds: number | null;
}

/**
 * 自然语言打卡确认 UI 的编辑数据源 `waiting.workout`（stage6.md §2.4.3；后端 `_workout_payload`）：
 * 日期、组事实与日程关联默认值。
 */
export interface ConfirmWorkoutDraftWire {
  performed_on: string;
  sets: WorkoutSetConfirmWire[];
  /** 初始 null：用户可在确认 UI 改选具体日程；服务端不替用户选择候选 */
  plan_session_id: number | null;
  /**
   * 初始 true：恰一个未完成日程时由既有服务自动关联；零候选或多候选且未显式选择时由既有领域规则
   * 产生日程歧义错误，不写库
   */
  auto_link: boolean;
}

/** 自然语言打卡路径的 `waiting`（stage6.md §2.4.3）：结构化训练结果 ＋ 数据库候选日程 */
export interface AgentWaitingWorkoutEventWire {
  event: "waiting";
  data: {
    workout: ConfirmWorkoutDraftWire;
    /** 数据库查询结果；模型不得重新生成候选 ID 或候选集合 */
    candidate_plan_sessions: PlanSessionCandidateWire[];
  };
}

/** Run 正常结束；安全命中先于 Router 时没有分类结论，intent 因此可为 null */
export interface AgentDoneEventWire {
  event: "done";
  data: {
    ok: true;
    intent: string | null;
    termination_reason: string | null;
    draft_plan_id: number | null;
  };
}

/** 运行错误；message 是后端已脱敏的可见文本，不含密钥、Provider 配置或堆栈 */
export interface AgentErrorEventWire {
  event: "error";
  data: { message: string };
}

/** 五类事件的判别联合：data 键集合与后端逐字一致，不增不减 */
export type AgentEventWire =
  | AgentNodeEventWire
  | AgentMessageEventWire
  | AgentWaitingEventWire
  | AgentWaitingWorkoutEventWire
  | AgentDoneEventWire
  | AgentErrorEventWire;

/** GET /api/provider 响应：后端不回传 api_key 本体，只给是否已配置的布尔 */
export type ProviderStatusWire = {
  has_api_key: boolean;
  base_url: string;
  model: string;
};

/** PUT /api/provider 请求体：整份覆盖，未出现的字段由后端写空串 */
export type ProviderWriteBody = {
  api_key?: string;
  base_url?: string;
  model?: string;
};

/** POST /api/provider/test 响应：ok 为 false 时 latency_ms 为 null，message 为固定文案 */
export type ProviderTestWire = {
  ok: boolean;
  latency_ms: number | null;
  message: string;
};
