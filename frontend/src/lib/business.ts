/* ===== 共享 wire 校验原语与业务 schema（backend-http-sse-contract §11.8、§11.9，workout-http-sse-contract §2–§5，plan-generation-contract §2–§8）===== */

import type {
  AttachmentContentWire,
  AttachmentInputWire,
  AttachmentWire,
  CurrentPlanWire,
  PlanBusinessErrorWire,
  PlanContentWire,
  PlanDayWire,
  PlanExerciseWire,
  PlanGetArgumentsWire,
  PlanAdjustmentProposalWire,
  PlanImportArgumentsWire,
  PlanImportProposalWire,
  PlanListWire,
  PlanProposalArgumentsWire,
  PlanProposalWire,
  PlanRecordWire,
  PlanSaveArgumentsWire,
  PlanSaveResultWire,
  PlanStatusArgumentsWire,
  PlanStatusResultWire,
  ProfileContentWire,
  ProfileProposalArgumentsWire,
  ProfileProposalWire,
  ProfileResponseWire,
  ProfileSaveArgumentsWire,
  ProfileSaveResultWire,
  ProfileStatusArgumentsWire,
  ProfileStatusResultWire,
  WorkoutContentWire,
  WorkoutExerciseWire,
  WorkoutListWire,
  WorkoutProposalWire,
  WorkoutRecordWire,
  WorkoutSaveResultWire,
  WorkoutSetWire,
  WorkoutStatusResultWire,
} from "@/lib/contract";

/** 标准带连字符 UUID 字符串（§11.2） */
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
export function requireUuid(value: unknown, field: string): string {
  if (typeof value !== "string" || !UUID_PATTERN.test(value))
    throw new Error(`${field} 身份无效。`);
  return value;
}
export function nullableUuid(value: unknown, field: string): string | null {
  return value === null ? null : requireUuid(value, field);
}
export function requireString(value: unknown, field: string): string {
  if (typeof value !== "string" || value === "")
    throw new Error(`${field} 无效。`);
  return value;
}

/** 用户文本投影：保留原值，允许纯文件输入的缺省空文本 */
export function requireText(value: unknown, field: string): string {
  if (typeof value !== "string") throw new Error(`${field} 无效。`);
  return value;
}
export function nullableString(value: unknown, field: string): string | null {
  return value === null ? null : requireString(value, field);
}
export function requireMillis(value: unknown, field: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0)
    throw new Error(`${field} 无效。`);
  return value as number;
}
export function nullableMillis(value: unknown, field: string): number | null {
  return value === null ? null : requireMillis(value, field);
}
export function requireBoolean(value: unknown, field: string): boolean {
  if (typeof value !== "boolean") throw new Error(`${field} 无效。`);
  return value;
}
export function requireEnum<T extends string>(
  value: unknown,
  allowed: readonly T[],
  field: string,
): T {
  if (
    typeof value !== "string" ||
    !(allowed as readonly string[]).includes(value)
  )
    throw new Error(`${field} 无效。`);
  return value as T;
}
export function requireArray(value: unknown, field: string): unknown[] {
  if (!Array.isArray(value)) throw new Error(`${field} 无效。`);
  return value;
}

/** 必填字段集合完全一致：缺字段与额外字段均按协议异常处理 */
function exactKeys(
  value: Record<string, unknown>,
  keys: readonly string[],
  field: string,
): void {
  const known = new Set(keys);
  for (const key of Object.keys(value))
    if (!known.has(key)) throw new Error(`${field} 含未定义字段。`);
  for (const key of keys)
    if (!(key in value)) throw new Error(`${field} 缺少必填字段。`);
}

/** 正整数（画像版本号、快照依据版本）：§11.9 要求对 number 执行整数及取值范围检查 */
function requirePositiveInt(value: unknown, field: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 1)
    throw new Error(`${field} 无效。`);
  return value as number;
}

/** 非负安全整数（附件原始字节大小）：0 为合法的空文件 */
export function requireNonNegativeInt(value: unknown, field: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0)
    throw new Error(`${field} 无效。`);
  return value as number;
}

/** 标准 Base64 字符串：四位一组、字母表与填充位置固定，空串表示零字节原始内容 */
function requireBase64(value: unknown, field: string): string {
  if (typeof value !== "string" || !BASE64_PATTERN.test(value))
    throw new Error(`${field} 无效。`);
  return value;
}

const BASE64_PATTERN =
  /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;

/** 文本必须包含非空白内容并保留原文本；列表的 null 为未知，[] 为明确没有限制（§11.9） */
function requireNonBlank(value: unknown, field: string): string {
  const text = requireString(value, field);
  if (text.trim() === "") throw new Error(`${field} 无效。`);
  return text;
}

const TEXT_KEYS = [
  "goal",
  "experience",
  "environment",
  "availability",
  "health_notes",
  "movement_restrictions",
] as const;
const LIST_KEYS = ["unavailable_equipment", "forbidden_exercise_ids"] as const;

/** 完整画像：八个字段全部必填；限制列表拒绝空白项与重复项（§11.9、DATABASE §2） */
export function parseProfileContent(value: unknown): ProfileContentWire {
  if (!isObject(value)) throw new Error("画像内容无效。");
  exactKeys(value, [...TEXT_KEYS, ...LIST_KEYS], "画像");
  const content: Partial<ProfileContentWire> = {};
  for (const key of TEXT_KEYS)
    content[key] =
      value[key] === null ? null : requireNonBlank(value[key], key);
  for (const key of LIST_KEYS) {
    if (value[key] === null) {
      content[key] = null;
      continue;
    }
    const list = requireArray(value[key], key).map((item) =>
      requireNonBlank(item, key),
    );
    if (new Set(list).size !== list.length)
      throw new Error(`${key} 含重复项。`);
    content[key] = list;
  }
  return content as ProfileContentWire;
}

/** GET /api/profile：未建档时版本与内容同时为 null（§11.9） */
export function parseProfileResponse(body: unknown): ProfileResponseWire {
  if (!isObject(body)) throw new Error("画像响应无效。");
  exactKeys(body, ["version", "content"], "画像响应");
  const version =
    body.version === null ? null : requirePositiveInt(body.version, "version");
  const content =
    body.content === null ? null : parseProfileContent(body.content);
  if ((version === null) !== (content === null))
    throw new Error("画像版本与内容不同步。");
  return { version, content };
}

/** 画像与训练记录快照共用的保存状态集合（§11.9、workout-http-sse-contract §5） */
const PROPOSAL_STATUSES = [
  "pending",
  "processing",
  "saved",
  "invalidated",
  "conflicted",
] as const;

/** 单用户目标画像：严格整数且固定 1（§11.9） */
function requireTargetProfile(value: unknown, field: string): number {
  if (!Number.isSafeInteger(value) || (value as number) !== 1)
    throw new Error(`${field} 画像标识无效。`);
  return value as number;
}

/** 准备工具输入与输出共用的快照字段：目标画像、依据版本与完整画像内容（§11.9） */
function profileProposalFields(
  value: Record<string, unknown>,
  field: string,
): {
  profile_id: number;
  base_profile_version: number | null;
  payload: ProfileContentWire;
} {
  const baseProfileVersion =
    value.base_profile_version === null
      ? null
      : requirePositiveInt(value.base_profile_version, "base_profile_version");
  const payload = parseProfileContent(value.payload);
  if (
    baseProfileVersion === null &&
    Object.values(payload).every((item) => item === null)
  )
    throw new Error(`${field} 首次建档缺少任何已知信息。`);
  return {
    profile_id: requireTargetProfile(value.profile_id, `${field} 画像`),
    base_profile_version: baseProfileVersion,
    payload,
  };
}

export function parseProfileProposalArguments(
  value: unknown,
): ProfileProposalArgumentsWire {
  if (!isObject(value)) throw new Error("画像准备输入无效。");
  exactKeys(
    value,
    ["profile_id", "base_profile_version", "payload"],
    "画像准备输入",
  );
  return profileProposalFields(value, "画像准备输入");
}

export function parseProfileProposal(value: unknown): ProfileProposalWire {
  if (!isObject(value)) throw new Error("画像快照无效。");
  exactKeys(
    value,
    ["proposal_id", "profile_id", "base_profile_version", "payload"],
    "画像快照",
  );
  return {
    proposal_id: requireUuid(value.proposal_id, "proposal_id"),
    ...profileProposalFields(value, "画像快照"),
  };
}

/**
 * 准备工具的成功结果内容（§11.9）：单个 text 内容块里的完整快照 JSON，前端只展示其中的
 * 完整 ``payload``；内容不符合快照 schema 属协议异常，在解析位置报错。
 */
export function preparedProfilePayload(text: string): ProfileContentWire {
  return parseProfileProposal(JSON.parse(text) as unknown).payload;
}

export function parseProfileSaveArguments(
  value: unknown,
): ProfileSaveArgumentsWire {
  if (!isObject(value)) throw new Error("画像保存输入无效。");
  exactKeys(
    value,
    ["proposal_id", "display_entry_id", "confirmation_entry_id"],
    "画像保存输入",
  );
  return {
    proposal_id: requireUuid(value.proposal_id, "proposal_id"),
    display_entry_id: requireUuid(value.display_entry_id, "display_entry_id"),
    confirmation_entry_id: requireUuid(
      value.confirmation_entry_id,
      "confirmation_entry_id",
    ),
  };
}

/** 固定保存结果：正整数版本、完整画像与 UTC 毫秒保存时间（§11.8、§11.9） */
export function parseProfileSaveResult(value: unknown): ProfileSaveResultWire {
  if (!isObject(value)) throw new Error("画像保存结果无效。");
  exactKeys(
    value,
    ["proposal_id", "profile_id", "version", "content", "saved_at"],
    "画像保存结果",
  );
  return {
    proposal_id: requireUuid(value.proposal_id, "proposal_id"),
    profile_id: requireTargetProfile(value.profile_id, "画像保存结果"),
    version: requirePositiveInt(value.version, "version"),
    content: parseProfileContent(value.content),
    saved_at: requirePositiveInt(value.saved_at, "saved_at"),
  };
}

export function parseProfileStatusArguments(
  value: unknown,
): ProfileStatusArgumentsWire {
  if (!isObject(value)) throw new Error("画像状态查询输入无效。");
  exactKeys(value, ["proposal_id"], "画像状态查询输入");
  return { proposal_id: requireUuid(value.proposal_id, "proposal_id") };
}

/** 状态查询输出：仅 saved 携带完整固定结果，其他状态 result 为 null（§11.9） */
export function parseProfileStatusResult(
  value: unknown,
): ProfileStatusResultWire {
  if (!isObject(value)) throw new Error("画像保存状态无效。");
  exactKeys(value, ["proposal_id", "status", "result"], "画像保存状态");
  const status = requireEnum(value.status, PROPOSAL_STATUSES, "画像保存状态");
  if (status === "saved") {
    if (value.result === null) throw new Error("已保存快照缺少固定结果。");
    const result = parseProfileSaveResult(value.result);
    if (result.proposal_id !== value.proposal_id)
      throw new Error("固定结果的快照标识不一致。");
    return { proposal_id: result.proposal_id, status, result };
  }
  if (value.result !== null) throw new Error("未保存快照携带保存结果。");
  return {
    proposal_id: requireUuid(value.proposal_id, "proposal_id"),
    status,
    result: null,
  };
}

/* ===== 实际训练记录（workout-http-sse-contract §2、§3、§5）===== */

const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

/** 业务自然日 ``yyyy-mm-dd``：格式与真实日历日期同时校验，拒绝进位后的非法日期 */
function requireDate(value: unknown, field: string): string {
  if (typeof value !== "string" || !DATE_PATTERN.test(value))
    throw new Error(`${field} 无效。`);
  const [year, month, day] = value.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  if (
    date.getUTCFullYear() !== year ||
    date.getUTCMonth() !== month - 1 ||
    date.getUTCDate() !== day
  )
    throw new Error(`${field} 无效。`);
  return value;
}

/** 非负有限数值（重量 kg）：拒绝字符串数字、布尔值、NaN 与无穷 */
function requireNonNegative(value: unknown, field: string): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0)
    throw new Error(`${field} 无效。`);
  return value;
}

/** 正有限数值（时长秒）：允许小数秒，拒绝 0 与负值 */
function requirePositiveNumber(value: unknown, field: string): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0)
    throw new Error(`${field} 无效。`);
  return value;
}

/** 感受允许缺失（null），出现时必须包含非空白内容 */
function nullableNonBlank(value: unknown, field: string): string | null {
  return value === null ? null : requireNonBlank(value, field);
}

const WORKOUT_LOAD_CONVENTIONS = [
  "per_implement",
  "barbell_total",
  "machine_display",
  "plates_total",
  "per_side",
  "added_weight",
  "assistance_weight",
] as const;

/** 已知完成的一组：三个数值允许全部为 null，null 保持未知语义而不补 0 */
function parseWorkoutSet(value: unknown): WorkoutSetWire {
  if (!isObject(value)) throw new Error("训练组无效。");
  exactKeys(value, ["reps", "weight_kg", "duration_seconds"], "训练组");
  return {
    reps: value.reps === null ? null : requirePositiveInt(value.reps, "reps"),
    weight_kg:
      value.weight_kg === null
        ? null
        : requireNonNegative(value.weight_kg, "weight_kg"),
    duration_seconds:
      value.duration_seconds === null
        ? null
        : requirePositiveNumber(value.duration_seconds, "duration_seconds"),
  };
}

function parseWorkoutExercise(value: unknown): WorkoutExerciseWire {
  if (!isObject(value)) throw new Error("训练动作无效。");
  exactKeys(
    value,
    ["exercise_id", "name", "load_convention", "sets"],
    "训练动作",
  );
  const sets = requireArray(value.sets, "sets").map(parseWorkoutSet);
  const loadConvention =
    value.load_convention === null
      ? null
      : requireEnum(
          value.load_convention,
          WORKOUT_LOAD_CONVENTIONS,
          "load_convention",
        );
  // 重量有值必须明确口径（§3），重量为 0 同样适用
  if (loadConvention === null && sets.some((set) => set.weight_kg !== null))
    throw new Error("训练动作缺少重量口径。");
  return {
    // 目录原始 ID（四位数字字符串，§11.7），目录外动作为 null；真实存在性由后端核实
    exercise_id: nullableNonBlank(value.exercise_id, "exercise_id"),
    name: requireNonBlank(value.name, "name"),
    load_convention: loadConvention,
    sets,
  };
}

/** 完整训练内容：至少一个实际动作；``sets=[]`` 表示组数及逐组数据未知 */
export function parseWorkoutContent(value: unknown): WorkoutContentWire {
  if (!isObject(value)) throw new Error("训练记录内容无效。");
  exactKeys(value, ["exercises", "notes"], "训练记录内容");
  const exercises = requireArray(value.exercises, "exercises").map(
    parseWorkoutExercise,
  );
  if (exercises.length === 0) throw new Error("训练记录缺少实际动作。");
  return { exercises, notes: nullableNonBlank(value.notes, "notes") };
}

/** 完整记录（§3）：ID 为标准 UUID，版本与时间戳为正整数 UTC 毫秒 */
export function parseWorkoutRecord(value: unknown): WorkoutRecordWire {
  if (!isObject(value)) throw new Error("训练记录无效。");
  exactKeys(
    value,
    ["id", "performed_on", "version", "content", "created_at", "updated_at"],
    "训练记录",
  );
  return {
    id: requireUuid(value.id, "id"),
    performed_on: requireDate(value.performed_on, "performed_on"),
    version: requirePositiveInt(value.version, "version"),
    content: parseWorkoutContent(value.content),
    created_at: requirePositiveInt(value.created_at, "created_at"),
    updated_at: requirePositiveInt(value.updated_at, "updated_at"),
  };
}

/** GET /api/workouts（§2）：page 正整数、page_size 1–100、total 非负整数 */
export function parseWorkoutList(body: unknown): WorkoutListWire {
  if (!isObject(body)) throw new Error("训练记录列表响应无效。");
  exactKeys(body, ["items", "page", "page_size", "total"], "训练记录列表响应");
  const pageSize = requirePositiveInt(body.page_size, "page_size");
  if (pageSize > 100) throw new Error("page_size 无效。");
  return {
    items: requireArray(body.items, "items").map(parseWorkoutRecord),
    page: requirePositiveInt(body.page, "page"),
    page_size: pageSize,
    total: requireMillis(body.total, "total"),
  };
}

/** 准备快照（§5）：基础记录 ID 与版本同时为空或同时有值 */
export function parseWorkoutProposal(value: unknown): WorkoutProposalWire {
  if (!isObject(value)) throw new Error("训练记录快照无效。");
  exactKeys(
    value,
    [
      "proposal_id",
      "performed_on",
      "base_workout_id",
      "base_workout_version",
      "payload",
    ],
    "训练记录快照",
  );
  const baseId = nullableUuid(value.base_workout_id, "base_workout_id");
  const baseVersion =
    value.base_workout_version === null
      ? null
      : requirePositiveInt(value.base_workout_version, "base_workout_version");
  if ((baseId === null) !== (baseVersion === null))
    throw new Error("快照依据的记录身份不完整。");
  return {
    proposal_id: requireUuid(value.proposal_id, "proposal_id"),
    performed_on: requireDate(value.performed_on, "performed_on"),
    base_workout_id: baseId,
    base_workout_version: baseVersion,
    payload: parseWorkoutContent(value.payload),
  };
}

/** 准备工具的成功结果内容（§5）：单个 text 内容块里的完整快照 JSON，不合 schema 属协议异常 */
export function preparedWorkoutProposal(text: string): WorkoutProposalWire {
  return parseWorkoutProposal(JSON.parse(text) as unknown);
}

/** 固定保存结果（§5）：完整记录字段加本次保存时间，重复提交保持一致 */
export function parseWorkoutSaveResult(value: unknown): WorkoutSaveResultWire {
  if (!isObject(value)) throw new Error("训练记录保存结果无效。");
  exactKeys(
    value,
    [
      "proposal_id",
      "id",
      "performed_on",
      "version",
      "content",
      "created_at",
      "updated_at",
      "saved_at",
    ],
    "训练记录保存结果",
  );
  return {
    proposal_id: requireUuid(value.proposal_id, "proposal_id"),
    id: requireUuid(value.id, "id"),
    performed_on: requireDate(value.performed_on, "performed_on"),
    version: requirePositiveInt(value.version, "version"),
    content: parseWorkoutContent(value.content),
    created_at: requirePositiveInt(value.created_at, "created_at"),
    updated_at: requirePositiveInt(value.updated_at, "updated_at"),
    saved_at: requirePositiveInt(value.saved_at, "saved_at"),
  };
}

/** 状态查询输出：仅 saved 携带完整固定结果，其他状态 result 为 null（§5） */
export function parseWorkoutStatusResult(
  value: unknown,
): WorkoutStatusResultWire {
  if (!isObject(value)) throw new Error("训练记录保存状态无效。");
  exactKeys(value, ["proposal_id", "status", "result"], "训练记录保存状态");
  const status = requireEnum(
    value.status,
    PROPOSAL_STATUSES,
    "训练记录保存状态",
  );
  if (status === "saved") {
    if (value.result === null) throw new Error("已保存快照缺少固定结果。");
    const result = parseWorkoutSaveResult(value.result);
    if (result.proposal_id !== value.proposal_id)
      throw new Error("固定结果的快照标识不一致。");
    return { proposal_id: result.proposal_id, status, result };
  }
  if (value.result !== null) throw new Error("未保存快照携带保存结果。");
  return {
    proposal_id: requireUuid(value.proposal_id, "proposal_id"),
    status,
    result: null,
  };
}

/* ===== 训练计划（plan-generation-contract §2–§5、§8）===== */

const PLAN_DAY_KINDS = ["training", "rest"] as const;
const PLAN_ERROR_CODES = [
  "plan_not_found",
  "plan_proposal_not_found",
  "plan_proposal_invalidated",
  "plan_save_processing",
  "plan_version_conflict",
  "profile_required",
  "profile_version_conflict",
  "plan_confirmation_invalid",
  "plan_access_denied",
  "session_not_found",
] as const;

/** JSON Pointer 字段路径：以 ``/`` 开头的非空白字符串，前端按路径显示建议来源 */
function requireFieldPath(value: unknown, field: string): string {
  const text = requireNonBlank(value, field);
  if (!text.startsWith("/")) throw new Error(`${field} 无效。`);
  return text;
}

/** 计划动作（§2、§3.1）：目录 ID 是保留前导零的字符串；``rest_seconds`` 的 0 与 null 分别表示不安排休息与未结构化指定 */
function parsePlanExercise(value: unknown): PlanExerciseWire {
  if (!isObject(value)) throw new Error("计划动作无效。");
  exactKeys(
    value,
    [
      "exercise_id",
      "name",
      "sets",
      "reps",
      "duration_seconds",
      "weight_kg",
      "load_convention",
      "rest_seconds",
    ],
    "计划动作",
  );
  const loadConvention =
    value.load_convention === null
      ? null
      : requireEnum(
          value.load_convention,
          WORKOUT_LOAD_CONVENTIONS,
          "load_convention",
        );
  const weightKg =
    value.weight_kg === null
      ? null
      : requireNonNegative(value.weight_kg, "weight_kg");
  // 重量有值（含 0）必须明确口径，重量为空时允许保留已知口径（§3.1）
  if (loadConvention === null && weightKg !== null)
    throw new Error("计划动作缺少重量口径。");
  return {
    exercise_id: nullableNonBlank(value.exercise_id, "exercise_id"),
    name: requireNonBlank(value.name, "name"),
    sets: value.sets === null ? null : requirePositiveInt(value.sets, "sets"),
    reps: value.reps === null ? null : requirePositiveInt(value.reps, "reps"),
    duration_seconds:
      value.duration_seconds === null
        ? null
        : requirePositiveNumber(value.duration_seconds, "duration_seconds"),
    weight_kg: weightKg,
    load_convention: loadConvention,
    rest_seconds:
      value.rest_seconds === null
        ? null
        : requireNonNegative(value.rest_seconds, "rest_seconds"),
  };
}

/** 计划训练日（§3.1）：休息日动作列表必须为空，通用结构允许训练日 ``exercises=[]`` */
function parsePlanDay(value: unknown): PlanDayWire {
  if (!isObject(value)) throw new Error("计划训练日无效。");
  exactKeys(value, ["kind", "focus", "exercises", "notes"], "计划训练日");
  const kind = requireEnum(value.kind, PLAN_DAY_KINDS, "kind");
  const exercises = requireArray(value.exercises, "exercises").map(
    parsePlanExercise,
  );
  if (kind === "rest" && exercises.length > 0)
    throw new Error("休息日含动作。");
  return {
    kind,
    focus: nullableNonBlank(value.focus, "focus"),
    exercises,
    notes: nullableNonBlank(value.notes, "notes"),
  };
}

/** 通用计划内容（§3.1）：至少一个训练日，数组顺序即展示与保存顺序 */
export function parsePlanContent(value: unknown): PlanContentWire {
  if (!isObject(value)) throw new Error("计划内容无效。");
  exactKeys(
    value,
    ["repeat", "days", "notes", "suggested_fields"],
    "计划内容",
  );
  const days = requireArray(value.days, "days").map(parsePlanDay);
  if (days.length === 0) throw new Error("计划缺少训练日。");
  return {
    repeat:
      value.repeat === null ? null : requireBoolean(value.repeat, "repeat"),
    days,
    notes: nullableNonBlank(value.notes, "notes"),
    suggested_fields: requireArray(
      value.suggested_fields,
      "suggested_fields",
    ).map((item) => requireFieldPath(item, "suggested_fields")),
  };
}

/** 本次生成快照的附加完整度（§3.2）：训练日必须有动作，动作必须有组数且次数或时长至少一个有值 */
function requireGeneratedPlanContent(content: PlanContentWire): void {
  for (const day of content.days) {
    if (day.kind === "training" && day.exercises.length === 0)
      throw new Error("生成的计划训练日缺少具体动作。");
    for (const exercise of day.exercises) {
      if (exercise.sets === null) throw new Error("生成的计划动作缺少组数。");
      if (exercise.reps === null && exercise.duration_seconds === null)
        throw new Error("生成的计划动作缺少目标次数或时长。");
    }
  }
}

function parseGeneratedPlanPayload(value: unknown): PlanContentWire {
  const content = parsePlanContent(value);
  requireGeneratedPlanContent(content);
  return content;
}

/** GET /api/plans/current（§5）：``id`` 与 ``content`` 同时为空或同时有值 */
export function parseCurrentPlan(body: unknown): CurrentPlanWire {
  if (!isObject(body)) throw new Error("当前计划响应无效。");
  exactKeys(body, ["id", "content"], "当前计划响应");
  if (body.id === null && body.content === null)
    return { id: null, content: null };
  return {
    id: requireUuid(body.id, "id"),
    content: parsePlanContent(body.content),
  };
}

/** 已保存版本（§4）：固定内容加实时当前标记与保存时间 */
export function parsePlanRecord(value: unknown): PlanRecordWire {
  if (!isObject(value)) throw new Error("计划版本无效。");
  exactKeys(value, ["id", "is_current", "created_at", "content"], "计划版本");
  return {
    id: requireUuid(value.id, "id"),
    is_current: requireBoolean(value.is_current, "is_current"),
    created_at: requireMillis(value.created_at, "created_at"),
    content: parsePlanContent(value.content),
  };
}

/** GET /api/plans（§5）：直接数组，顺序由后端给出，无版本时为空数组 */
export function parsePlanList(body: unknown): PlanListWire {
  return requireArray(body, "计划列表响应").map(parsePlanRecord);
}

export function parsePlanGetArguments(value: unknown): PlanGetArgumentsWire {
  if (!isObject(value)) throw new Error("计划查询输入无效。");
  exactKeys(value, ["plan_id"], "计划查询输入");
  return { plan_id: requireUuid(value.plan_id, "plan_id") };
}

/** `prepare_plan` 输出与生成完整度（§4）：生成快照必须已建档，依据当前计划 ID 使用查询原值 */
function planProposalFields(
  value: Record<string, unknown>,
): PlanProposalArgumentsWire {
  return {
    base_profile_version: requirePositiveInt(
      value.base_profile_version,
      "base_profile_version",
    ),
    base_plan_id: nullableUuid(value.base_plan_id, "base_plan_id"),
    payload: parseGeneratedPlanPayload(value.payload),
  };
}

export function parsePlanProposalArguments(
  value: unknown,
): PlanProposalArgumentsWire {
  if (!isObject(value)) throw new Error("计划准备输入无效。");
  exactKeys(
    value,
    ["base_profile_version", "base_plan_id", "payload"],
    "计划准备输入",
  );
  return planProposalFields(value);
}

export function parsePlanProposal(value: unknown): PlanProposalWire {
  if (!isObject(value)) throw new Error("计划快照无效。");
  exactKeys(
    value,
    ["proposal_id", "base_profile_version", "base_plan_id", "payload"],
    "计划快照",
  );
  return {
    proposal_id: requireUuid(value.proposal_id, "proposal_id"),
    ...planProposalFields(value),
  };
}

/** 准备工具的成功结果内容（§4、§6）：单个 text 内容块里的完整快照 JSON，不合 schema 属协议异常 */
export function preparedPlanProposal(text: string): PlanProposalWire {
  return parsePlanProposal(JSON.parse(text) as unknown);
}

/** 录入与调整快照共用依据字段（录入契约 §7）：画像不存在时 ``base_profile_version`` 为 null，
 *  内容完整度沿用通用计划结构，训练日动作详情可以为空 */
function planImportProposalFields(
  value: Record<string, unknown>,
): PlanImportArgumentsWire {
  return {
    base_profile_version:
      value.base_profile_version === null
        ? null
        : requirePositiveInt(
            value.base_profile_version,
            "base_profile_version",
          ),
    base_plan_id: nullableUuid(value.base_plan_id, "base_plan_id"),
    payload: parsePlanContent(value.payload),
  };
}

/** `prepare_plan_import` 输出：准备类型由实际工具确定为 import，输入不接受该字段 */
export function parsePlanImportProposal(
  value: unknown,
): PlanImportProposalWire {
  if (!isObject(value)) throw new Error("计划录入快照无效。");
  exactKeys(
    value,
    [
      "proposal_id",
      "preparation_kind",
      "base_profile_version",
      "base_plan_id",
      "payload",
    ],
    "计划录入快照",
  );
  return {
    proposal_id: requireUuid(value.proposal_id, "proposal_id"),
    preparation_kind: requireEnum(
      value.preparation_kind,
      ["import"] as const,
      "preparation_kind",
    ),
    ...planImportProposalFields(value),
  };
}

/** `prepare_plan_adjustment` 输出：准备类型由实际工具确定为 adjustment */
export function parsePlanAdjustmentProposal(
  value: unknown,
): PlanAdjustmentProposalWire {
  if (!isObject(value)) throw new Error("计划调整快照无效。");
  exactKeys(
    value,
    [
      "proposal_id",
      "preparation_kind",
      "base_profile_version",
      "base_plan_id",
      "payload",
    ],
    "计划调整快照",
  );
  return {
    proposal_id: requireUuid(value.proposal_id, "proposal_id"),
    preparation_kind: requireEnum(
      value.preparation_kind,
      ["adjustment"] as const,
      "preparation_kind",
    ),
    ...planImportProposalFields(value),
  };
}

export function preparedPlanImportProposal(
  text: string,
): PlanImportProposalWire {
  return parsePlanImportProposal(JSON.parse(text) as unknown);
}

export function preparedPlanAdjustmentProposal(
  text: string,
): PlanAdjustmentProposalWire {
  return parsePlanAdjustmentProposal(JSON.parse(text) as unknown);
}

export function parsePlanSaveArguments(
  value: unknown,
): PlanSaveArgumentsWire {
  if (!isObject(value)) throw new Error("计划保存输入无效。");
  exactKeys(
    value,
    ["proposal_id", "display_entry_id", "confirmation_entry_id"],
    "计划保存输入",
  );
  return {
    proposal_id: requireUuid(value.proposal_id, "proposal_id"),
    display_entry_id: requireUuid(value.display_entry_id, "display_entry_id"),
    confirmation_entry_id: requireUuid(
      value.confirmation_entry_id,
      "confirmation_entry_id",
    ),
  };
}

/** 固定保存结果（§4）：不携带动态当前标记，``created_at`` 与 ``saved_at`` 为同一次保存时间 */
export function parsePlanSaveResult(value: unknown): PlanSaveResultWire {
  if (!isObject(value)) throw new Error("计划保存结果无效。");
  exactKeys(
    value,
    ["proposal_id", "id", "content", "created_at", "saved_at"],
    "计划保存结果",
  );
  const createdAt = requireMillis(value.created_at, "created_at");
  const savedAt = requireMillis(value.saved_at, "saved_at");
  if (createdAt !== savedAt) throw new Error("计划保存时间不一致。");
  return {
    proposal_id: requireUuid(value.proposal_id, "proposal_id"),
    id: requireUuid(value.id, "id"),
    content: parsePlanContent(value.content),
    created_at: createdAt,
    saved_at: savedAt,
  };
}

export function parsePlanStatusArguments(
  value: unknown,
): PlanStatusArgumentsWire {
  if (!isObject(value)) throw new Error("计划状态查询输入无效。");
  exactKeys(value, ["proposal_id"], "计划状态查询输入");
  return { proposal_id: requireUuid(value.proposal_id, "proposal_id") };
}

/** 状态查询输出（§4）：仅 saved 携带完整固定结果，其他状态 result 为 null */
export function parsePlanStatusResult(
  value: unknown,
): PlanStatusResultWire {
  if (!isObject(value)) throw new Error("计划保存状态无效。");
  exactKeys(value, ["proposal_id", "status", "result"], "计划保存状态");
  const status = requireEnum(value.status, PROPOSAL_STATUSES, "计划保存状态");
  if (status === "saved") {
    if (value.result === null) throw new Error("已保存快照缺少固定结果。");
    const result = parsePlanSaveResult(value.result);
    if (result.proposal_id !== value.proposal_id)
      throw new Error("固定结果的快照标识不一致。");
    return { proposal_id: result.proposal_id, status, result };
  }
  if (value.result !== null) throw new Error("未保存快照携带保存结果。");
  return {
    proposal_id: requireUuid(value.proposal_id, "proposal_id"),
    status,
    result: null,
  };
}

/** 工具失败结果内容（§8）：仅 invalid_business_payload 携带字段错误列表 */
export function parsePlanBusinessError(
  value: unknown,
): PlanBusinessErrorWire {
  if (!isObject(value)) throw new Error("计划业务错误无效。");
  const code = requireString(value.code, "code");
  const message = requireString(value.message, "message");
  if (code === "invalid_business_payload") {
    exactKeys(value, ["code", "message", "errors"], "计划业务错误");
    return {
      code,
      message,
      errors: requireArray(value.errors, "errors").map((item) => {
        if (!isObject(item)) throw new Error("计划字段错误无效。");
        exactKeys(item, ["path", "message"], "计划字段错误");
        return {
          path: requireString(item.path, "path"),
          message: requireString(item.message, "message"),
        };
      }),
    };
  }
  exactKeys(value, ["code", "message"], "计划业务错误");
  return { code: requireEnum(code, PLAN_ERROR_CODES, "code"), message };
}

/* ===== 附件（plan-import-adjustment-contract §1、§2、§4）===== */

/** 单条消息最终附件集合的原始字节合计上限（§1） */
export const ATTACHMENT_TOTAL_BYTES = 100_000;

/** 支持的文本附件扩展名，匹配忽略大小写（§1） */
const ATTACHMENT_EXTENSIONS = ["md", "txt"] as const;

/** 扩展名判定（§1）：仅按 ``.md``／``.txt`` 结尾识别，大小写均可 */
export function hasAttachmentExtension(fileName: string): boolean {
  const at = fileName.lastIndexOf(".");
  if (at <= 0) return false;
  const extension = fileName.slice(at + 1).toLowerCase();
  return (ATTACHMENT_EXTENSIONS as readonly string[]).includes(extension);
}

/** 文件名为非空文件名，拒绝路径分隔符、NUL、驱动器与路径形式（§2）；仅用于展示，服务器路径使用 UUID */
export function requireAttachmentName(value: unknown, field: string): string {
  const text = requireString(value, field);
  if (text.trim() === "") throw new Error(`${field} 无效。`);
  if (/[\\/\0:]|\.\./.test(text)) throw new Error(`${field} 含路径形式。`);
  return text;
}

const ATTACHMENT_WIRE_KEYS = [
  "attachment_id",
  "file_name",
  "size_bytes",
  "created_at",
] as const;

/** 附件元数据字段（§4）：``size_bytes`` 为原始字节数 */
function attachmentWireFields(value: Record<string, unknown>): AttachmentWire {
  return {
    attachment_id: requireUuid(value.attachment_id, "attachment_id"),
    file_name: requireAttachmentName(value.file_name, "file_name"),
    size_bytes: requireNonNegativeInt(value.size_bytes, "size_bytes"),
    created_at: requireMillis(value.created_at, "created_at"),
  };
}

/** 已受理附件的公开元数据：响应不返回存储引用等内部字段 */
export function parseAttachmentWire(value: unknown): AttachmentWire {
  if (!isObject(value)) throw new Error("附件元数据无效。");
  exactKeys(value, ATTACHMENT_WIRE_KEYS, "附件元数据");
  return attachmentWireFields(value);
}

/** 有序附件集合：无附件为 ``[]``，同一集合内附件 ID 不得重复（§2、§4） */
export function parseAttachmentWireList(
  value: unknown,
  field: string,
): AttachmentWire[] {
  const list = requireArray(value, field).map(parseAttachmentWire);
  if (new Set(list.map((item) => item.attachment_id)).size !== list.length)
    throw new Error(`${field} 含重复附件。`);
  return list;
}

/** 附件读取响应（§4）：元数据加严格 UTF-8 解码正文，不含存储引用 */
export function parseAttachmentContent(value: unknown): AttachmentContentWire {
  if (!isObject(value)) throw new Error("附件内容响应无效。");
  exactKeys(value, [...ATTACHMENT_WIRE_KEYS, "text"], "附件内容响应");
  return {
    ...attachmentWireFields(value),
    text: requireText(value.text, "text"),
  };
}

/** 附件请求输入（§2）：上传项带文件名与标准 Base64 原始字节，引用项只有同会话附件 ID */
export function parseAttachmentInputWire(value: unknown): AttachmentInputWire {
  if (!isObject(value)) throw new Error("附件输入无效。");
  if (value.kind === "upload") {
    exactKeys(
      value,
      ["kind", "attachment_id", "file_name", "data_base64"],
      "附件上传输入",
    );
    return {
      kind: "upload",
      attachment_id: requireUuid(value.attachment_id, "attachment_id"),
      file_name: requireAttachmentName(value.file_name, "file_name"),
      data_base64: requireBase64(value.data_base64, "data_base64"),
    };
  }
  if (value.kind === "reference") {
    exactKeys(value, ["kind", "attachment_id"], "附件引用输入");
    return {
      kind: "reference",
      attachment_id: requireUuid(value.attachment_id, "attachment_id"),
    };
  }
  throw new Error("附件输入类型无效。");
}

/** 请求附件集合：顺序即提交顺序，重复附件 ID 就地拒绝（§2） */
export function parseAttachmentInputList(
  value: unknown,
  field = "attachments",
): AttachmentInputWire[] {
  const list = requireArray(value, field).map(parseAttachmentInputWire);
  if (new Set(list.map((item) => item.attachment_id)).size !== list.length)
    throw new Error(`${field} 含重复附件。`);
  return list;
}
