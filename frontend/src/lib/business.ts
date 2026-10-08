/* ===== 共享 wire 校验原语与业务 schema（backend-http-sse-contract §11.8、§11.9，workout-http-sse-contract §2–§5）===== */

import type {
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
