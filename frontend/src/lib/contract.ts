/**
 * Fit-Agent 前端契约（Stage 1 表单写入）。
 *
 * 本文件是前端唯一的数据形状来源：UI 组件与 API 封装（src/lib/api.ts）一律从此处
 * 引用类型，不得另写第三份形状。对齐 ``backend/api/dto.py`` 与 routes_profile /
 * routes_records / routes_plans 的传输形状。
 *
 * 冻结口径（讨论总结 §7、§9；REFACTOR_PLAN §5/§6）：无 RIR、无 context_version、
 * 无通用草稿/修订链、无 Run/Provider 传输形状。
 */

/* ---------------------------------- 错误 ---------------------------------- */

/** 统一错误码：Stage 1 传输层只表达「请求或输入不合法」 */
export type ErrorCode = "invalid_request";

export interface ApiError {
  http_status: number;
  error_code: ErrorCode;
  message: string;
}

/* ------------------------------- 动作目录 -------------------------------- */

/** 目录记录口径恰三类（与 001_initial.sql exercises CHECK 同集合） */
export type CatalogRecordType = "reps_weight" | "reps_bodyweight" | "time";

/** 负重口径五种（自重／计时型为 null，不虚构口径） */
export type LoadConvention =
  | "barbell_includes_bar_total"
  | "dumbbell_per_hand"
  | "machine_pin_displayed_value"
  | "plate_loaded_total_excluding_empty"
  | "unilateral_setting_per_side";

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

/* --------------------------------- 用户画像 -------------------------------- */

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

/* -------------------------------- 训练记录 -------------------------------- */

/** 组类型固定三态（与 workout_sets CHECK 同集合；没有「未申报」态） */
export type SetTypeWire = "work" | "warmup" | "assisted";

/** 提交的一组训练事实；set_no 由后端按提交顺序对同一动作分配 1,2,3…（前端不送） */
export interface WorkoutSetInputWire {
  exercise_id: string;
  set_type: SetTypeWire;
  reps: number;
  /** 目录 load_convention：自重／计时型必须为 null（与目录不符后端拒绝） */
  load_convention: LoadConvention | null;
  weight_kg: number | null;
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
    reps: number;
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

/* -------------------------------- 身体指标 -------------------------------- */

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

/* --------------------------- 计划（只读，无写入入口） -------------------------- */

/** 一个计划版本（plan_dto） */
export interface PlanWire {
  id: number;
  version: number;
  status: "draft" | "active" | "archived";
  source_plan_id: number | null;
  /** 结构化计划内容：后端只保证是合法 JSON，形状不在此层解释 */
  structured_content: unknown;
  evaluator_result: unknown | null;
  created_at: string;
  confirmed_at: string | null;
  archived_at: string | null;
}

export interface PlanListWire {
  plans: PlanWire[];
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
