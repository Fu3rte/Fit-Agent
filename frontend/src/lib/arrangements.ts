/**
 * 已接受安排的前端投影（F6-02c 已拍 A2）：
 * 生产路径不新增/不调用后端 `GET /api/arrangements`；从已有数据推导「该应训练日
 * 是否有已接受安排」，供档案页日程联表只读展示。
 *
 * 数据源（全部是既有只读面）：
 * 1. 计划日程（`GET /api/plan` → mapPlanView）——日程基底，cancelled/locked/scheduled；
 * 2. 训练记录（`GET /api/records` → mapRecordList）——仅当展示行显式携带
 *    scheduled_session_id + arrangement_revision_id 时作为联键证据
 *    （绝不从日期推断关联）。注意：生产传输契约（RecordStoragePayload）当前不携带
 *    scheduled_session_id，readModels 映射固定为 null——记录证据在生产路径联不到
 *    具体日程条目（缺项，见 02c 报告）；
 * 3. 会话安排草稿（`GET /api/sessions/{id}/drafts`，kind=arrangement、status=committed）
 *    ——携带完整 target 快照（target.scheduled_session_id 联日程），可细分
 *    「已调整 / 目标更保守 / 未调整」。真实后端无会话列表端点：跨会话枚举草稿
 *    不可行，仅调用方已知会话的草稿可进入投影。
 *
 * 投影局限（报告缺项，不静默补造）：
 * - 无会话列表端点 → 档案页无法枚举跨会话已提交安排草稿；
 * - 记录传输契约无 scheduled_session_id → 记录证据联不到日程条目；
 * - Draft 不携带 accepted_at → 草稿来源的接受时间显示为空，不伪造时间。
 */
import type {
  AcceptedArrangement,
  ArrangementTarget,
  Draft,
  TrainingRecord,
} from "./contract";

/** 单条投影：日程联键 + 可得证据 */
export interface ProjectedArrangement {
  /** 应训练日程联键（records.scheduled_session_id / target.scheduled_session_id） */
  scheduled_session_id: string;
  /** 接受时间（ISO；来源 comparison.accepted_at 或安排草稿缺失时为 undefined） */
  accepted_at?: string;
  /** 完整目标快照（仅已提交安排草稿可提供；否则细分状态不可用） */
  target?: ArrangementTarget;
  /** 安排修订身份（records.arrangement_revision_id；草稿来源为 draft.id） */
  arrangement_revision_id?: string;
  /** 证据来源标记（展示调试/报告用，不进 UI 文案） */
  source: "record" | "arrangement_draft";
}

export interface ArrangementProjectionInput {
  records?: TrainingRecord[];
  /** 已知会话的草稿（生产路径仅能按已知 session id 拉取；档案页通常为空，见缺项） */
  drafts?: Draft[];
}

/**
 * 投影为 Map<scheduled_session_id, ProjectedArrangement>：
 * - 记录证据与草稿证据按日程联键合并（草稿补 target，记录补 accepted_at/修订 id）；
 * - 仅显式联键，不按日期推断；
 * - 无任何证据的日程不在 Map 中（展示为「尚无安排」）。
 */
export function projectAcceptedArrangements(
  input: ArrangementProjectionInput,
): Map<string, ProjectedArrangement> {
  const out = new Map<string, ProjectedArrangement>();

  // 1) 记录证据：显式携带安排关联键的训练记录
  for (const record of input.records ?? []) {
    const sessionId = record.scheduled_session_id;
    const revisionId = record.arrangement_revision_id;
    if (!sessionId || !revisionId) continue;
    const prev = out.get(sessionId);
    if (prev) continue; // 同日程多条记录：先到保留，接受时间不覆盖
    out.set(sessionId, {
      scheduled_session_id: sessionId,
      arrangement_revision_id: revisionId,
      ...(record.comparison?.accepted_at
        ? { accepted_at: record.comparison.accepted_at }
        : {}),
      source: "record",
    });
  }

  // 2) 已提交安排草稿：完整 target 快照（细分状态仅此来源可用）
  for (const draft of input.drafts ?? []) {
    if (draft.kind !== "arrangement" || draft.status !== "committed") continue;
    if (!("target" in draft.payload)) continue;
    const target = draft.payload.target;
    if (!target?.scheduled_session_id) continue;
    const prev = out.get(target.scheduled_session_id);
    out.set(target.scheduled_session_id, {
      scheduled_session_id: target.scheduled_session_id,
      target,
      ...(prev?.accepted_at ? { accepted_at: prev.accepted_at } : {}),
      ...(prev?.arrangement_revision_id
        ? { arrangement_revision_id: prev.arrangement_revision_id }
        : {}),
      source: "arrangement_draft",
    });
  }

  return out;
}

/** 投影条目 → AcceptedArrangement 视图（有 target 才可调 classifyArrangementStatus） */
export function projectedAsAccepted(
  projected: ProjectedArrangement | undefined,
): AcceptedArrangement | undefined {
  if (!projected?.target) return undefined;
  return {
    id: projected.arrangement_revision_id ?? projected.scheduled_session_id,
    accepted_at: projected.accepted_at ?? "",
    target: projected.target,
  };
}
