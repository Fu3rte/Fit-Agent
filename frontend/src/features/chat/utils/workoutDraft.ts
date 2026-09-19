/**
 * 对话页打卡确认的纯映射（无 React、无 DOM）：Node 验证脚本可直接 import 本文件做 REAL 断言。
 *
 * 边界（stage6.md §2.1／§2.2／§2.4.2／§2.5.1）：
 * - 只做「``waiting.workout`` ↔ 编辑行 ↔ 确认载荷」的形状转换与三种日程关联形状；
 * - 不复制领域校验：范围、负重口径、必填／互斥字段、组序号唯一性一律由后端复验（§2.2 第二层）；
 * - 组序号由用户在行内填写（本轮裁决 c），新增行给出同动作 ``max+1`` 的可编辑初值；
 * - 数值输入框的空串按 ``null`` 提交（不补 0、不猜默认值）。
 */
import type {
  ConfirmWorkoutBody,
  LoadConvention,
  PlanSessionCandidateWire,
  PlanSessionCandidatesWire,
  SetTypeWire,
  WorkoutSetConfirmWire,
} from "@/lib/contract";

/** 一行可编辑组事实：数字字段用字符串承载，空串表示 ``null`` */
export interface WorkoutDraftRow {
  exercise_id: string;
  set_no: string;
  set_type: SetTypeWire;
  reps: string;
  weight_kg: string;
  duration_seconds: string;
  load_convention: LoadConvention | null;
  /** 计时动作（``duration_seconds`` 非 null）：只提交秒数，不提交次数 */
  timed: boolean;
}

/** 日程关联选择：未手动选择／显式额外训练／显式某个日程 */
export type SessionChoice = "auto" | "extra" | number;

/** 空串即 null；其余原样交给 ``Number``（非法输入由后端拒绝并原样展示产品错误） */
function numberOrNull(text: string): number | null {
  return text.trim() === "" ? null : Number(text);
}

function numberOrFail(text: string, message: string): number {
  const value = numberOrNull(text);
  if (value === null || Number.isNaN(value)) throw new Error(message);
  return value;
}

/** ``waiting.workout.sets`` → 可编辑行：七个字段逐字回填，不补默认值 */
export function rowsFromWorkout(
  sets: WorkoutSetConfirmWire[],
): WorkoutDraftRow[] {
  return sets.map((set) => ({
    exercise_id: set.exercise_id,
    set_no: String(set.set_no),
    set_type: set.set_type,
    reps: set.reps === null ? "" : String(set.reps),
    weight_kg: set.weight_kg === null ? "" : String(set.weight_kg),
    duration_seconds:
      set.duration_seconds === null ? "" : String(set.duration_seconds),
    load_convention: set.load_convention,
    timed: set.duration_seconds !== null,
  }));
}

/** 编辑行 → 确认载荷的组：按行的记录口径只送适用字段（计时送秒数，其余送次数） */
export function setsFromRows(
  rows: WorkoutDraftRow[],
): WorkoutSetConfirmWire[] {
  return rows.map((row, index) => {
    const position = index + 1;
    if (row.exercise_id.trim() === "")
      throw new Error(`第 ${position} 组：动作身份为空`);
    return {
      exercise_id: row.exercise_id,
      set_no: numberOrFail(row.set_no, `第 ${position} 组：请填写组序号`),
      set_type: row.set_type,
      reps: row.timed ? null : numberOrNull(row.reps),
      load_convention: row.load_convention,
      weight_kg: numberOrNull(row.weight_kg),
      duration_seconds: row.timed ? numberOrNull(row.duration_seconds) : null,
    };
  });
}

/** 新增行的组序号初值：同动作现有组序号最大值 + 1 */
function nextSetNo(rows: WorkoutDraftRow[], exerciseId: string): string {
  const numbers = rows
    .filter((row) => row.exercise_id === exerciseId)
    .map((row) => Number(row.set_no))
    .filter((value) => Number.isInteger(value));
  return String(numbers.length === 0 ? 1 : Math.max(...numbers) + 1);
}

/** 新增一组：沿用最后一行的动作、组类型与负重口径（本表单不提供动作切换）；数值字段留空 */
export function addSetRow(rows: WorkoutDraftRow[]): WorkoutDraftRow[] {
  const last = rows[rows.length - 1];
  return [
    ...rows,
    {
      exercise_id: last.exercise_id,
      set_no: nextSetNo(rows, last.exercise_id),
      set_type: last.set_type,
      reps: "",
      weight_kg: "",
      duration_seconds: "",
      load_convention: last.load_convention,
      timed: last.timed,
    },
  ];
}

/** 删除一行；剩余行不重排组序号（``domain/records/rules.py`` 只要求 1–50 且同动作内不重复） */
export function removeSetRow(
  rows: WorkoutDraftRow[],
  index: number,
): WorkoutDraftRow[] {
  return rows.filter((_, position) => position !== index);
}

/** ``waiting.workout`` 的关联默认值 → 选择项（§2.4.3 初始 ``plan_session_id=null``、``auto_link=true``） */
export function initialSessionChoice(
  planSessionId: number | null,
  autoLink: boolean,
): SessionChoice {
  return planSessionId ?? (autoLink ? "auto" : "extra");
}

/**
 * 候选日程的初始值：只在 ``waiting.workout`` 自己的日期上生效。
 *
 * 用户改日期后返回 ``undefined``，让查询按新日期重新结果（§2.5.1），旧日期的候选与旧日程 id
 * 都不得出现在新日期的选项里。
 */
export function initialCandidates(
  performedOn: string,
  workoutPerformedOn: string,
  candidates: PlanSessionCandidateWire[],
): PlanSessionCandidatesWire | undefined {
  return performedOn === workoutPerformedOn ? { sessions: candidates } : undefined;
}

/**
 * 三种提交形状（stage6.md §2.1 硬边界表／§2.4.2）：
 * - 未手动选择（含恰一个候选）：``plan_session_id=null`` 且 ``auto_link=true``，由既有服务解析唯一候选；
 * - 显式日程：``plan_session_id=<id>`` 且 ``auto_link=false``；
 * - 显式额外训练：``plan_session_id=null`` 且 ``auto_link=false``。
 */
export function sessionLink(choice: SessionChoice): {
  plan_session_id: number | null;
  auto_link: boolean;
} {
  if (choice === "auto") return { plan_session_id: null, auto_link: true };
  if (choice === "extra") return { plan_session_id: null, auto_link: false };
  return { plan_session_id: choice, auto_link: false };
}

/** 完整确认载荷：五字段由 ``waiting`` 结构化字段与用户编辑值构成，不从 ``message.text`` 反解 */
export function confirmBodyOf(input: {
  conversation_id: string;
  performed_on: string;
  rows: WorkoutDraftRow[];
  choice: SessionChoice;
}): ConfirmWorkoutBody {
  return {
    conversation_id: input.conversation_id,
    performed_on: input.performed_on,
    sets: setsFromRows(input.rows),
    ...sessionLink(input.choice),
  };
}
