import type {
  CatalogRecordType,
  LoadConvention,
  SetTypeWire,
} from "@/lib/contract";

/** 六种负重口径的中文标签（取值域与 ``domain/actions/rules.py`` 的 LOAD_CONVENTIONS 同集合） */
export const LOAD_CONVENTION_LABELS: Record<LoadConvention, string> = {
  barbell_includes_bar_total: "杠铃含杠总重",
  dumbbell_per_hand: "哑铃每手重量",
  machine_pin_displayed_value: "器械插销显示值",
  plate_loaded_total_excluding_empty: "挂片总重（不含空杆）",
  unilateral_setting_per_side: "单侧设置重量",
  external_added_weight: "外加重量（不含体重）",
};

/** 三类记录口径的中文标签（与目录 CHECK 的三类同集合） */
export const RECORD_TYPE_LABELS: Record<CatalogRecordType, string> = {
  reps_weight: "负重次数",
  reps_bodyweight: "自重次数",
  time: "计时",
};

/** 三类组类型的中文标签（与后端 workout_sets CHECK 同集合） */
export const SET_TYPE_LABELS: Record<SetTypeWire, string> = {
  work: "正式组",
  warmup: "热身组",
  assisted: "辅助组",
};
