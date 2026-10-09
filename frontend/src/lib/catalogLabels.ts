import type { WorkoutLoadConvention } from "@/lib/contract";

/** 训练记录与训练计划共用的七种重量口径标签（取值域与 §11.7 口径说明及 ``domain/business`` 同集合） */
export const WORKOUT_LOAD_CONVENTION_LABELS: Record<
  WorkoutLoadConvention,
  string
> = {
  per_implement: "单只器械重量",
  barbell_total: "杠铃含杆总重",
  machine_display: "器械显示重量",
  plates_total: "加载片总重（不含器械自重）",
  per_side: "单侧加载重量",
  added_weight: "外加负重（不含体重）",
  assistance_weight: "辅助减重",
};
