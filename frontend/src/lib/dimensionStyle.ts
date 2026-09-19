import { Dumbbell, Timer, Zap } from "lucide-react";
import type { ComponentType } from "react";

/**
 * 训练维度的统一呈现口径（看板 PB 面板与计划卡的展台、列表行共用同一份三色表）。
 *
 * 三键只描述「负重／静态／次数」这三个维度本身；PB 的 ``weight_pb``／``duration_pb``／``reps_pb``
 * 与计划处方的 ``weighted_reps``／``timed``／``bodyweight_reps`` 各自映射到这里的键，不各建一份配色。
 */
export type DimensionKey = "weight" | "duration" | "reps";

export interface DimensionStyle {
  label: string;
  /** 单位随维度：负重 kg、静态 秒、次数 次 */
  unit: string;
  Icon: ComponentType<{ className?: string }>;
  text: string;
  border: string;
  bg: string;
}

export const DIMENSION_STYLE: Record<DimensionKey, DimensionStyle> = {
  weight: {
    label: "负重",
    unit: "kg",
    Icon: Dumbbell,
    text: "text-amber-600 dark:text-amber-400",
    border: "border-amber-500/30",
    bg: "bg-amber-500/10",
  },
  duration: {
    label: "静态",
    unit: "秒",
    Icon: Timer,
    text: "text-emerald-600 dark:text-emerald-400",
    border: "border-emerald-500/30",
    bg: "bg-emerald-500/10",
  },
  reps: {
    label: "次数",
    unit: "次",
    Icon: Zap,
    text: "text-sky-600 dark:text-sky-400",
    border: "border-sky-500/30",
    bg: "bg-sky-500/10",
  },
};
