/**
 * FieldDiff 网络边界归一化（F6-02c 已拍）：
 * 后端传输形状是 WireFieldDiff（{field, before, after, changed}）；前端展示层
 * 沿用 FieldDiff（{field, old_value?, new_value}）。本文件是二者之间唯一映射点——
 * 页面、mock 内部构造与内联纠错 Diff 都直接产出展示形状，不经此处。
 *
 * 兼容两路来源：
 * - 真实后端：before/after 可能是字符串，也可能是结构化值（档案 Fact {state,value}、
 *   计划/安排结构化字段）；changed=false 的行不进展示。
 * - mock（F6-02d 前保留）：直接产出 old_value/new_value，原样透传。
 */
import type { Draft, FieldDiff } from "./contract";

type DiffRow = Partial<FieldDiff> & { field: string };

function isWireRow(row: Record<string, unknown>): boolean {
  return "before" in row || "after" in row || "changed" in row;
}

/** 结构化/三态值 → 展示字符串；无法安全压成文本时给占位（不静默丢行） */
function wireValueToText(value: unknown): string | undefined {
  if (value === undefined || value === null) return undefined;
  if (typeof value === "string") return value === "" ? "—" : value;
  if (typeof value === "number" || typeof value === "boolean")
    return String(value);
  if (typeof value === "object") {
    // 档案 Fact 三态：{state: known|unknown|denied, value?}
    const fact = value as { state?: string; value?: unknown };
    if (typeof fact.state === "string") {
      if (fact.state === "known")
        return wireValueToText(fact.value) ?? "已知";
      if (fact.state === "denied") return "明确无";
      if (fact.state === "unknown") return "未收集";
    }
    try {
      return JSON.stringify(value);
    } catch {
      return "（结构化变更）";
    }
  }
  return String(value);
}

/** 单行映射：wire → 展示；非 wire（mock 旧形状）原样透传 */
function normalizeFieldDiffRow(raw: unknown): DiffRow | null {
  if (!raw || typeof raw !== "object") return null;
  const row = raw as Record<string, unknown>;
  const field = typeof row.field === "string" ? row.field : undefined;
  if (field === undefined) return null;

  if (!isWireRow(row)) {
    // mock/内联旧形状：{field, old_value?, new_value}
    const newValue = row.new_value;
    return {
      field,
      ...(row.old_value !== undefined ? { old_value: String(row.old_value) } : {}),
      new_value: newValue === undefined ? "" : String(newValue),
    };
  }

  // 真实后端 wire：changed=false 的行不进展示
  if (row.changed === false) return null;
  const before = wireValueToText(row.before);
  const after = wireValueToText(row.after) ?? "";
  return {
    field,
    ...(before !== undefined ? { old_value: before } : {}),
    new_value: after,
  };
}

/** Diff 行列表归一化（网络边界唯一入口） */
export function normalizeFieldDiffRows(rows: unknown): FieldDiff[] {
  if (!Array.isArray(rows)) return [];
  return rows
    .map(normalizeFieldDiffRow)
    .filter((r): r is FieldDiff => r !== null);
}

/** 草稿归一化：diff / parent_diff 走网络边界映射；其余字段原样保留 */
export function normalizeDraft(draft: Draft): Draft {
  return {
    ...draft,
    diff: normalizeFieldDiffRows(draft.diff),
    ...(draft.parent_diff !== undefined && draft.parent_diff !== null
      ? { parent_diff: normalizeFieldDiffRows(draft.parent_diff) }
      : {}),
  };
}
