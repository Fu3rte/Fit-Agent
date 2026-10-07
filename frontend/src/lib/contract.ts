/* ===== 会话持久化请求层（backend-http-sse-contract §11）===== */

/** 会话对象（§11.2）：``active_leaf_id`` 为 UUID 或 null，其余字段非空 */
export interface SessionWire {
  session_id: string;
  title: string;
  active_leaf_id: string | null;
  created_at: number;
  updated_at: number;
}

/** 运行状态（§11.2） */
export type RunStatusWire =
  "running" | "completed" | "failed" | "cancelled" | "interrupted";

/** 运行对象（§11.2）：``last_entry_id``／``finished_at`` 与错误字段允许 null */
export interface SessionRunWire {
  session_id: string;
  run_id: string;
  request_entry_id: string;
  last_entry_id: string | null;
  status: RunStatusWire;
  started_at: number;
  finished_at: number | null;
  error_code: string | null;
  error_message: string | null;
}

/** 输入状态（§11.2） */
export type SteeringStatusWire =
  "pending" | "consumed" | "withdrawn" | "discarded";

/** 丢弃原因（§11.2） */
export type DiscardReasonWire =
  "completed" | "failed" | "cancelled" | "interrupted";

/** 输入对象（§11.2）：``entry_id`` 仅 consumed 有值，``reason`` 仅 discarded 有值 */
export interface SteeringInputWire {
  session_id: string;
  run_id: string;
  steering_id: string;
  status: SteeringStatusWire;
  entry_id: string | null;
  reason: DiscardReasonWire | null;
  created_at: number;
  updated_at: number;
}

/** 操作类型（§11.2） */
export type OperationKindWire = "send" | "edit" | "regenerate" | "steering";

/** 创建会话请求体（§4）：``session_id`` 标准 UUID，``title`` 为首条请求原文 */
export interface SessionCreateBody {
  session_id: string;
  title: string;
}

/** 发送请求体（§5）：``operation_id`` 为同一次操作的幂等键，显式重试沿用原值 */
export interface ReActRunBody {
  session_id: string;
  operation_id: string;
  request: string;
}

/** 编辑请求体（session-edit-regenerate-contract §3）：``target_entry_id`` 为被编辑的用户节点 */
export interface EditRunBody {
  session_id: string;
  operation_id: string;
  target_entry_id: string;
  request: string;
}

/** 重新生成请求体（session-edit-regenerate-contract §4）：``target_entry_id`` 为执行起点的用户节点 */
export interface RegenerateRunBody {
  session_id: string;
  operation_id: string;
  target_entry_id: string;
}

/** 重复发送返回的 JSON（§11.3）：原运行的关联身份与当前持久化状态 */
export interface SendRunRepeatWire {
  operation_id: string;
  session_id: string;
  run_id: string;
  request_entry_id: string;
  status: RunStatusWire;
}

/** 接收 Steering 请求体（§6.1） */
export interface SteeringBody {
  session_id: string;
  operation_id: string;
  message: string;
}

/** 接收 Steering 响应状态（§11.3）：首次为 accepted，重复为原输入当前持久化状态 */
export type SteeringReceiveStatusWire = "accepted" | SteeringStatusWire;

/** 接收 Steering 响应（§11.3） */
export interface SteeringReceiveWire {
  operation_id: string;
  session_id: string;
  run_id: string;
  steering_id: string;
  created: boolean;
  status: SteeringReceiveStatusWire;
  entry_id: string | null;
  reason: DiscardReasonWire | null;
}

/** 撤回 Steering 请求体（§6.2） */
export interface SteeringWithdrawBody {
  session_id: string;
}

/** 撤回 Steering 响应（§11.3）：``status`` 为 withdrawn 或已有 discarded */
export interface SteeringWithdrawWire {
  session_id: string;
  run_id: string;
  steering_id: string;
  status: "withdrawn" | "discarded";
  entry_id: string | null;
  reason: DiscardReasonWire | null;
}

/** 操作查询响应（§11.3）：未受理时 kind／run／steering 全为 null */
export interface OperationQueryWire {
  operation_id: string;
  session_id: string;
  accepted: boolean;
  kind: OperationKindWire | null;
  run: SessionRunWire | null;
  steering: SteeringInputWire | null;
}

/** SSE 响应头（§11.4）：四个身份均为标准 UUID 字符串 */
export interface AgentStreamHeaders {
  session_id: string;
  operation_id: string;
  run_id: string;
  request_entry_id: string;
}

/* ===== 会话列表与当前分支历史（session-history-contract §2、§3）===== */

/** GET /api/sessions：全量会话头（§2） */
export interface SessionListWire {
  sessions: SessionWire[];
}

/** DELETE /api/sessions/{session_id}（session-delete-contract §4.2）：``deleted`` 固定为 true */
export interface SessionDeleteWire {
  session_id: string;
  deleted: true;
}

/** 助手公开内容块（§3.2）：历史与 SSE 使用同一公开快照规则 */
export type PublicAssistantContent = ReActContent;

/** 历史消息公开投影（§3.2）：系统消息不含可见字段，前端不产生可见消息 */
export type PublicMessageWire =
  | { role: "system" }
  | { role: "user"; text: string; timestamp: number }
  | {
      role: "assistant";
      content: PublicAssistantContent[];
      stop_reason: ReActStopReason;
      timestamp: number;
    }
  | {
      role: "toolResult";
      tool_call_id: string;
      tool_name: string;
      content: string;
      is_error: boolean;
      timestamp: number;
    };

/** 一个已提交节点（§3.1）：entries 为 active_leaf_id 的祖先链，按根到叶排列 */
export interface HistoryEntryWire {
  entry_id: string;
  parent_id: string | null;
  run_id: string | null;
  created_at: number;
  message: PublicMessageWire;
}

/** 历史运行对象（§3.3）：与运行接口契约的运行对象同形 */
export type HistoryRunWire = SessionRunWire;

/** 历史输入状态（§3.4）：consumed 关联用户节点，其余状态不伪造节点 */
export interface HistorySteeringWire {
  session_id: string;
  run_id: string;
  steering_id: string;
  text: string;
  timestamp: number;
  status: SteeringStatusWire;
  entry_id: string | null;
  reason: DiscardReasonWire | null;
  created_at: number;
  updated_at: number;
}

/* ===== 画像查询（backend-http-sse-contract §11.9）===== */

/** 画像八个必填字段：文本字段 null 表示未知；限制列表 null 表示未知，[] 表示明确没有限制 */
export interface ProfileContentWire {
  goal: string | null;
  experience: string | null;
  environment: string | null;
  availability: string | null;
  health_notes: string | null;
  movement_restrictions: string | null;
  unavailable_equipment: string[] | null;
  forbidden_exercise_ids: string[] | null;
}

/** GET /api/profile：未建档时 ``version`` 与 ``content`` 同时为 null */
export interface ProfileResponseWire {
  version: number | null;
  content: ProfileContentWire | null;
}

/* ===== 画像自然语言确认与保存（backend-http-sse-contract §11.8、§11.9）===== */

/** 快照保存状态：仅 saved 携带完整保存结果 */
export type ProfileProposalStatusWire =
  "pending" | "processing" | "saved" | "invalidated" | "conflicted";

/** `prepare_profile_update` 输入：单用户目标画像、查询得到的依据版本与完整待确认内容 */
export interface ProfileProposalArgumentsWire {
  /** 目标画像：严格整数且固定 1（单用户本地画像 id） */
  profile_id: number;
  base_profile_version: number | null;
  payload: ProfileContentWire;
}

/** `prepare_profile_update` 输出：后端生成的快照标识与固定内容，前端完整展示 `payload` */
export interface ProfileProposalWire {
  proposal_id: string;
  profile_id: number;
  base_profile_version: number | null;
  payload: ProfileContentWire;
}

/** `save_profile_update` 输入：快照标识、完整画像展示节点与用户确认节点 */
export interface ProfileSaveArgumentsWire {
  proposal_id: string;
  display_entry_id: string;
  confirmation_entry_id: string;
}

/** `save_profile_update` 成功输出，同时是 saved 状态的固定保存结果与幂等记录内容 */
export interface ProfileSaveResultWire {
  proposal_id: string;
  profile_id: number;
  version: number;
  content: ProfileContentWire;
  /** UTC 毫秒；重复保存保持原值 */
  saved_at: number;
}

/** `get_profile_update_status` 输入 */
export interface ProfileStatusArgumentsWire {
  proposal_id: string;
}

/** `get_profile_update_status` 输出：`saved` 的 `result` 为完整固定结果，其他状态为 null */
export interface ProfileStatusResultWire {
  proposal_id: string;
  status: ProfileProposalStatusWire;
  result: ProfileSaveResultWire | null;
}

/** GET /api/sessions/{session_id}/history：会话头 ＋ 当前分支节点 ＋ 关联运行与输入（§3） */
export interface SessionHistoryWire {
  session: SessionWire;
  entries: HistoryEntryWire[];
  runs: HistoryRunWire[];
  steering: HistorySteeringWire[];
}

/* ===== 会话运行及 Steering 独立列表（session-list-contract §2、§3）===== */

/** GET /api/sessions/{session_id}/runs：该会话仍保存的全部运行，按 started_at、run_id 升序（§2） */
export interface SessionRunListWire {
  session_id: string;
  runs: SessionRunWire[];
}

/** GET /api/sessions/{session_id}/runs/{run_id}/steering：指定运行的全部保留输入，按 created_at、steering_id 升序（§3） */
export interface RunSteeringListWire {
  session_id: string;
  run_id: string;
  steering: HistorySteeringWire[];
}

/* ===== Agent SSE 事件（backend-http-sse-contract §8、§11.6）===== */

export type ReActStopReason =
  "stop" | "toolUse" | "length" | "error" | "aborted";

export type ReActContent = { content_index: number } & (
  | { type: "text"; text: string }
  | { type: "thinking"; thinking: string }
  | {
      type: "tool_call";
      tool_call_id: string;
      name: string;
      arguments: Record<string, unknown>;
    }
);

export type ReActUpdateType =
  | "text_start"
  | "text_delta"
  | "text_end"
  | "thinking_start"
  | "thinking_delta"
  | "thinking_end"
  | "toolcall_start"
  | "toolcall_delta"
  | "toolcall_end";

/** 一条 Steering 状态通知（§11.6）：三种状态的字段组合固定 */
export type SteeringStatus =
  | { status: "consumed"; entry_id: string; reason: null }
  | { status: "withdrawn"; entry_id: null; reason: null }
  | { status: "discarded"; entry_id: null; reason: DiscardReasonWire };

type ReActMessageData = { message_id: string; content: ReActContent[] };

/** 已提交节点身份：首节点的 ``parent_id`` 为 null（§8） */
interface ReActCommittedEntry {
  entry_id: string;
  parent_id: string | null;
}

/** 单工具进度／完成事件共用的公开快照（§8） */
interface ReActToolSnapshot {
  tool_call_id: string;
  tool_name: string;
  content: string;
  is_error: boolean;
}

export type ReActEvent = { data: { run_id: string } } & (
  | { event: "message_start"; data: ReActMessageData }
  | {
      event: "message_update";
      data: ReActMessageData & {
        content_index: number;
        update_type: ReActUpdateType;
      };
    }
  | {
      event: "message_end";
      data: ReActMessageData &
        ReActCommittedEntry & { stop_reason: ReActStopReason };
    }
  | {
      event: "tool_start";
      data: {
        tool_call_id: string;
        name: string;
        arguments: Record<string, unknown>;
      };
    }
  | { event: "tool_execution_update"; data: ReActToolSnapshot }
  | { event: "tool_execution_end"; data: ReActToolSnapshot }
  | {
      event: "tool_result";
      data: {
        tool_call_id: string;
        content: string;
        is_error: boolean;
      } & ReActCommittedEntry;
    }
  | { event: "steering_status"; data: { steering_id: string } & SteeringStatus }
  | {
      event: "done";
      data: { status: "completed"; stop_reason: "stop" | "length" };
    }
  | {
      event: "error";
      data: {
        status: "failed" | "cancelled";
        code: "execution_failed" | "cancelled" | "credential_detected";
        message: string;
        tool_call_id: string | null;
      };
    }
);

/** 已注册业务接口错误码（§11.5，编辑与重新生成新增 ``entry_not_found`` / ``invalid_target_entry``，
 *  画像自然语言确认新增 §11.9 的八项业务码） */
export type ErrorCode =
  | "host_forbidden"
  | "origin_forbidden"
  | "session_not_found"
  | "run_not_found"
  | "steering_not_found"
  | "entry_not_found"
  | "invalid_target_entry"
  | "session_conflict"
  | "session_mismatch"
  | "run_busy"
  | "run_closed"
  | "operation_conflict"
  | "steering_consumption_conflict"
  | "incomplete_tool_chain"
  | "profile_proposal_not_found"
  | "profile_proposal_invalidated"
  | "profile_update_processing"
  | "profile_version_conflict"
  | "profile_confirmation_invalid"
  | "profile_access_denied"
  | "invalid_request"
  | "invalid_business_payload"
  | "credential_detected"
  | "internal_error";

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
  /** 常见别名：中文变式名与英文原名；自然语言匹配与搜索都命中它们 */
  aliases: string[];
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

/** 一个力量趋势系列：看板只透传不渲染，不提供动作选择 UI */
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

/** POST /api/agent/confirm 与 /api/agent/reject 请求体：会话身份 + 目标计划身份 */
export interface AgentPlanBody {
  chat_id: string;
  conversation_id: string;
  plan_id: number;
}

/** confirm／reject 的响应：落库后的计划行（激活成功或幂等返回既有行） */
export interface AgentPlanResponseWire {
  plan: PlanWire;
}

/**
 * POST /api/agent/confirm-workout 请求体：用户修改后的完整确认载荷。
 *
 * 只提交 `waiting` 结构化字段，不从 `message.text` 反解；本端点不要求服务端证明该
 * conversation_id 此前完成过一次自然语言解析。
 */
export interface ConfirmWorkoutBody {
  chat_id: string;
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

/* 会话历史 REST：GET 列表／POST 新建／GET 详情／DELETE 删除（conversation_dto 系列） */

/** 一条会话头（conversation_dto）：稳定身份、展示标题与两个时间戳 */
export interface ConversationWire {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

/** GET /api/conversations：服务端已按 ``updated_at`` 降序排列 */
export interface ConversationListWire {
  conversations: ConversationWire[];
}

/** POST /api/conversations 请求体：标题由客户端提供，空白标题由后端按 400 拒绝 */
export interface ConversationCreateBody {
  title: string;
}

/** Run 状态（conversation_runs.status 同集合）：``waiting`` 即该轮在等用户确认 */
export type ConversationRunStatusWire =
  "pending" | "running" | "waiting" | "completed" | "failed" | "cancelled";

/** 服务端投影的一条 Assistant 文本与状态（message.status 同集合） */
export interface ConversationAssistantWire {
  entry_id: string;
  content: string;
  /** 不是 ``complete`` 时只用于展示，不进入后续模型上下文 */
  status: "complete" | "partial" | "failed" | "aborted";
}

/** 一条确认投影：动作与面向用户的稳定文本（已确认／已拒绝／已打卡） */
export interface ConversationConfirmationWire {
  entry_id: string;
  action: "plan_confirmed" | "plan_rejected" | "workout_confirmed";
  text: string;
}

/** 一条 Run Event：``sequence`` 升序，``event``／``data`` 与 SSE 事件逐字同形 */
export type ConversationRunEventWire = { sequence: number } & AgentEventWire;

/** GET /api/conversations/{id} 详情里的一条轮次（与前端 ChatRound 同形，另带服务端身份与状态） */
export interface ConversationRoundWire {
  run_id: string;
  /** 该轮的 LangGraph thread 身份，等于发起该轮时的 conversation_id */
  conversation_id: string;
  status: ConversationRunStatusWire;
  request: string;
  assistants: ConversationAssistantWire[];
  confirmations: ConversationConfirmationWire[];
  events: ConversationRunEventWire[];
}

/** 单条受限运行轨迹：只含阶段与工具诊断元数据 */
export interface ConversationRunTraceEntryWire {
  sequence: number;
  created_at: string;
  stage: string;
  tool_call_id: string | null;
  tool_name: string | null;
  status: string;
  error_code: string | null;
}

/** GET /api/conversations/{chat_id}/runs/{thread_id}/trace */
export interface ConversationRunTraceWire {
  run_id: string;
  entries: ConversationRunTraceEntryWire[];
}

/** 一次压缩的展示分隔：摘要与保留起点（原始 Entry 不受影响） */
export interface ConversationCompactionWire {
  entry_id: string;
  summary: string;
  first_kept_entry_id: string;
}

/** GET /api/conversations/{id}：会话头 ＋ 会话全部 Entry 重建的轮次与压缩分隔 */
export interface ConversationDetailWire {
  conversation: ConversationWire;
  rounds: ConversationRoundWire[];
  compactions: ConversationCompactionWire[];
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

/** 计划确认路径的 `waiting`：已持久化 draft，只带 draft 身份 */
export interface AgentWaitingEventWire {
  event: "waiting";
  data: { draft_plan_id: number };
}

/**
 * 自然语言打卡的一条组事实：`waiting.workout.sets` 与 confirm-workout 请求体共用同一形状。
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
 * 自然语言打卡确认 UI 的编辑数据源 `waiting.workout`：日期、组事实与日程关联默认值。
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

/** 自然语言打卡路径的 `waiting`：结构化训练结果 ＋ 数据库候选日程 */
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

/** 客户端 transport：只决定后端用哪个 Chat 客户端（与后端 APIS 同集合） */
export type ProviderApiWire = "openai_compatible" | "anthropic_messages";

/** 结构化输出机制：只决定 with_structured_output 的原生 kwargs（与后端 STRUCTURED_OUTPUTS 同集合） */
export type ProviderStructuredOutputWire =
  "json_schema" | "function_calling_strict";

/** GET /api/provider 响应：后端不回传 api_key 本体，只给是否已配置的布尔 */
export type ProviderStatusWire = {
  has_api_key: boolean;
  base_url: string;
  model: string;
  api: ProviderApiWire;
  structured_output: ProviderStructuredOutputWire;
};

/** PUT /api/provider 请求体：api_key 空串或省略时后端沿用已存值，api／structured_output 空串回落默认 */
export type ProviderWriteBody = {
  api_key?: string;
  base_url?: string;
  model?: string;
  api?: ProviderApiWire;
  structured_output?: ProviderStructuredOutputWire;
};

/** POST /api/provider/test 响应：ok 为 false 时 latency_ms 为 null，message 为固定文案 */
export type ProviderTestWire = {
  ok: boolean;
  latency_ms: number | null;
  message: string;
};
