import type { WorkoutContentWire, WorkoutRecordWire } from "@/lib/contract";

/**
 * 日期筛选边界（workout-http-sse-contract §2）：起止日期均有值且起始晚于结束时后端返回 422，
 * ``YYYY-MM-DD`` 按字典序比较即日期比较，页面就地阻断查询。
 */
export function isRejectedDateRange(dateFrom: string, dateTo: string): boolean {
  return dateFrom !== "" && dateTo !== "" && dateFrom > dateTo;
}

/**
 * 选中项在当前查询结果内保持有效：``selectedId`` 命中本页记录时返回该记录，
 * 否则回到本页第一条（首次查询成功即默认选中第一条），无记录时为 null。
 * 保存落定重取列表后 ID 仍命中，展示的即该记录的最新内容。
 */
export function resolveSelectedRecord(
  items: readonly WorkoutRecordWire[],
  selectedId: string | null,
): WorkoutRecordWire | null {
  const matched = items.find((item) => item.id === selectedId);
  if (matched !== undefined) return matched;
  return items[0] ?? null;
}

/**
 * 训练内容摘要口径（§3）：``sets=[]`` 的动作组数未知且不计入已知组数；
 * 存在组数未知动作时明确标注已知组数。
 */
export function describeWorkoutContent(content: WorkoutContentWire): string {
  const unknownSetCount = content.exercises.filter(
    (exercise) => exercise.sets.length === 0,
  ).length;
  const knownSetCount = content.exercises.flatMap(
    (exercise) => exercise.sets,
  ).length;
  const parts = [`${content.exercises.length} 个动作`];
  if (unknownSetCount === 0) parts.push(`${knownSetCount} 组`);
  else if (knownSetCount === 0) parts.push("组数未知");
  else
    parts.push(`已知 ${knownSetCount} 组`, `${unknownSetCount} 个动作组数未知`);
  return parts.join(" · ");
}
