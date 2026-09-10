/**
 * 档案载荷的字段渲染与字段级对比（展示口径唯一来源）。
 *
 * 数据形状来自 src/lib/contract.ts（D1A）；本文件只做「已收集事实 → 展示行」的
 * 渲染与两份载荷的字段级对比，不承载业务语义、不补造缺省值。mock（派生 Diff、
 * 重算新旧草稿 Diff）与草稿卡（内联纠错的实时 Diff）共用同一口径。
 */
import type { FieldDiff, ProfileDraftPayload, Restriction } from "./contract";

/** 限制列表展示文案：空数组 = 用户明确说明无限制（与「尚未收集」区分） */
export function restrictionLabel(restrictions: Restriction[]): string {
  if (restrictions.length === 0) return "无（用户明确说明）";
  return restrictions
    .map(
      (r) =>
        `${r.name}（${r.scope === "specific_action" ? "具体动作" : "动作模式"}）`,
    )
    .join("、");
}

export interface ProfileFieldRow {
  /** 字段名（与档案草稿卡六类事实一致） */
  field: string;
  /** 展示值；字段缺省 = 尚未收集，不渲染默认值（PRD §5.2） */
  value: string;
}

/** 档案载荷 → 展示行（只渲染已收集事实，字段顺序 = 追问顺序） */
export function profileFieldRows(
  payload: ProfileDraftPayload,
): ProfileFieldRow[] {
  const p = payload.profile;
  const rows: ProfileFieldRow[] = [];
  const push = (field: string, value: string) => rows.push({ field, value });
  if (p.goal !== undefined) push("档案 · 训练目标", p.goal);
  if (p.experience !== undefined) push("档案 · 训练经验", p.experience);
  if (p.weekly_frequency !== undefined)
    push("档案 · 每周频率", `${p.weekly_frequency} 次`);
  if (p.session_minutes !== undefined)
    push("档案 · 单次时长", `${p.session_minutes} 分钟`);
  if (p.equipment !== undefined)
    push(
      "档案 · 可用器械",
      p.equipment.length > 0 ? p.equipment.join("、") : "无（用户确认）",
    );
  if (p.body_weight_kg !== undefined)
    push("档案 · 体重", `${p.body_weight_kg} kg`);
  if (p.physical_state !== undefined) {
    const ps = p.physical_state;
    push(
      "档案 · 当前身体状态 · 红旗症状",
      ps.red_flags.length > 0
        ? ps.red_flags.join("、")
        : "无明确红旗（用户确认）",
    );
    push(
      "档案 · 当前身体状态 · 其他",
      ps.notes.length > 0 ? ps.notes.join("、") : "无",
    );
  }
  if (payload.restrictions !== undefined)
    push("档案 · 当前有效限制", restrictionLabel(payload.restrictions));
  return rows;
}

/**
 * 两份档案载荷的字段级对比（重算的新旧草稿 Diff、草稿卡内联纠错的实时 Diff）。
 * 只列出发生变化的字段；字段从「有」变「未收集」同样列出，不静默丢弃。
 */
export function profilePayloadDiff(
  oldPayload: ProfileDraftPayload,
  newPayload: ProfileDraftPayload,
): FieldDiff[] {
  const before = new Map(
    profileFieldRows(oldPayload).map((r) => [r.field, r.value]),
  );
  const after = profileFieldRows(newPayload);
  const rows: FieldDiff[] = [];
  for (const { field, value } of after) {
    const prev = before.get(field);
    if (prev === value) continue;
    rows.push(
      prev === undefined
        ? { field, old_value: "未收集", new_value: value }
        : { field, old_value: prev, new_value: value },
    );
  }
  const afterFields = new Set(after.map((r) => r.field));
  for (const [field, value] of before)
    if (!afterFields.has(field))
      rows.push({ field, old_value: value, new_value: "未收集" });
  return rows;
}
