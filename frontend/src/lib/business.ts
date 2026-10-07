/* ===== 共享 wire 校验原语与业务 schema（backend-http-sse-contract §11.8、§11.9）===== */

import type {
  ProfileContentWire,
  ProfileProposalArgumentsWire,
  ProfileProposalWire,
  ProfileResponseWire,
  ProfileSaveArgumentsWire,
  ProfileSaveResultWire,
  ProfileStatusArgumentsWire,
  ProfileStatusResultWire,
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

const PROFILE_PROPOSAL_STATUSES = [
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
  const status = requireEnum(
    value.status,
    PROFILE_PROPOSAL_STATUSES,
    "画像保存状态",
  );
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
