/**
 * Mock 服务器（mock 阶段专用，后端 spike 落地后整个 src/mock/ 可删）。
 *
 * 以 Vite 插件形式把 mock REST + SSE 挂在 dev/preview 服务器上：
 * 客户端代码使用真实 fetch 与原生 EventSource 走本进程 /api/*，
 * 数据形状一律来自 src/lib/contract.ts。纯内存态，不落盘。
 */
import type { IncomingMessage, ServerResponse } from "node:http";
import type { Plugin } from "vite";
import type {
  AcceptedArrangement,
  ApiError,
  ArrangementDraftPayload,
  ArrangementItemDisposition,
  ArrangementTarget,
  Buckets,
  ChatMessage,
  ConfirmResult,
  Draft,
  DraftExerciseLog,
  ErrorCode,
  FieldDiff,
  PlanDraftPayload,
  PlanExerciseItem,
  PlanScheduleEntry,
  PlanVersion,
  PrEntry,
  Profile,
  ProfileDraftPayload,
  ProviderConfig,
  RawLoad,
  RecordDraftPayload,
  RecordRevisionStatus,
  RecalcResult,
  Restriction,
  ReviewDoc,
  RunStatus,
  SessionSummary,
  SetFacts,
  StatsSummary,
  TrainingRecord,
  WeekCompletion,
} from "@/lib/contract";
import {
  derivePlanBlocks,
  dayDiff,
  deriveRangeLabel,
  effortPlainLabel,
  findWorkout,
  prescriptionLabel,
  progressionLabel,
  projectSchedules,
  weekdayLabel,
} from "../lib/planView";
import { CATALOG } from "./catalog";
import {
  profileFieldRows,
  profilePayloadDiff,
  restrictionLabel,
} from "../lib/profile";
import {
  PLAN_CANDIDATE,
  PPL_CALENDAR_SLOTS,
  buildPplDraft,
  classifyBodyConditions,
  normalizePlanPayload,
  planCandidates,
  planDraftDiff,
  planExercise,
  planPayloadError,
  reviewPlanSafety,
} from "./plan";

/* ---------------------------------- 状态 ---------------------------------- */

const MOCK_TODAY = "2026-09-11";
const MOCK_UPDATED_AT = "2026-09-11T09:00:00+08:00";
/** mock 时钟：不依赖真实 Date.now，保证统计/接受时间的确定性可断言 */
let mockClockMs = Date.parse(MOCK_UPDATED_AT);
function mockNowIso(): string {
  return new Date(mockClockMs).toISOString();
}
function advanceMockClock(): void {
  mockClockMs += 60_000;
}

interface RunState {
  id: string;
  session_id: string;
  client_request_id: string;
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  cancelled: boolean;
  /** 已保存的部分回答 = 已流出的整段文本（stage0 已拍 D1）；取消/失败后保留（08 8.7 规则 5/8） */
  saved_text: string;
  /** status = failed 时的失败原因（08 8.4） */
  error_code?: ErrorCode;
  /** 该 Run 已提出草稿的 id；当前状态以 state.drafts 为准（01 1.2） */
  draft_ids: string[];
}

/**
 * 建档会话状态（stage1 F1-02 多轮收集）。每个事实三态：缺省 = 尚未收集（未知）、
 * 显式值 = 用户已给出、显式空数组 = 用户明确否认；不把未知写成默认值（PRD §5.2）。
 */
interface OnboardingState {
  goal?: string;
  experience?: string;
  weekly_frequency?: number;
  session_minutes?: number;
  /** 缺省 = 未收集；[] = 用户明确说明无器械 */
  equipment?: string[];
  body_weight_kg?: number;
  /** 缺省 = 未收集；[] = 用户明确说明无限制（只含当前有效限制，无状态语义）
   *  2026-09-10 拍板：首次建档不单独追问，结论由身体情况推出（未提议限制时为 []） */
  restrictions?: Restriction[];
  /** 缺省 = 未收集；已收集时为用户报告原文（[] = 用户明确否认；存储不做医学分类） */
  body_conditions?: string[];
  /** 最近一次追问的事实：用于把「没有」解释为对该项的明确否认 */
  last_asked?: OnboardingFact;
  /** 读取时分类未命中六类清单的原文（不判定安全）：只用于澄清追问，不单独入档 */
  pending_symptoms: string[];
  /** 最近一次生成的档案草稿 id 与当时的载荷快照：避免同基线重复出稿 */
  draft_id?: string;
  drafted_snapshot?: string;
}

interface MockState {
  context_version: number;
  provider: ProviderConfig;
  /** 未建档（空种子）= null：GET /api/profile 据此表达未建档态（stage1 F1-01） */
  profile: Profile | null;
  restrictions: Restriction[];
  /** 未生成/未启用计划（空种子）= null；形状 = 契约 PlanVersion（F2-01：不再是 mock 私有形状） */
  plan: PlanVersion | null;
  /** 历史计划版本（F2-04；PRD 5.3：正式版本只追加不原地改）：替换时旧版以 archived 归档保留 */
  plan_history: PlanVersion[];
  /** 具体日程（04 4.2/4.4）：多版本共存，stored_status/locked_effective 由启用事务与日期规则给出 */
  schedules: PlanScheduleEntry[];
  records: TrainingRecord[];
  /** 当次安排修订（S3-08）：确认写入的完整目标快照；不改 plan_versions */
  arrangement_revisions: {
    id: string;
    target: ArrangementTarget;
    accepted_at: string;
  }[];
  stats: StatsSummary;
  review: ReviewDoc;
  sessions: SessionSummary[];
  messages: Map<string, ChatMessage[]>;
  drafts: Map<string, Draft>;
  /** 草稿归属会话（mock 内部索引；契约 Draft 本身无会话字段） */
  draft_sessions: Map<string, string>;
  /** 建档对话状态（按会话；stage1 F1-02） */
  onboarding: Map<string, OnboardingState>;
  /** 首次确认凭据（owner 决策 B；01 1.3 提交凭据）：draft_id → 完整 ConfirmResult 凭据；
   *  重复确认返回持久化凭据，不从当前状态重建，不再写入或递增版本 */
  confirm_receipts: Map<string, ConfirmResult>;
  runs: Map<string, RunState>;
  /** 唯一执行名额（08 8.3）：由正在执行的 Run 持有，runScript 实际退出（finally）后才释放；
   *  与 Run 状态解耦——取消立即置 cancelled，名额不提前释放。非权威状态，仅作并发互斥。 */
  execution_slot_run_id: string | null;
  /** dev-only 故障注入位（F2-04）：置位后下一次计划确认事务在写入中途失败，用于验证整份回滚 */
  dev_confirm_failure: boolean;
}

/**
 * 校准口径、目录身份构造器（`planExercise`）与计划生成／校验统一在 ./plan
 * （F2-01 种子与 F2-02 生成共用同一实现，避免第二份身份与器械文案映射）。
 */

/** D9 种子计划：PPL v2，锚点 2026-08-31（周一），标准 push/rest/pull/rest/legs/rest/rest 循环 → 周一/三/五 */
function seedPlanV2(): PlanVersion {
  const exercises = (
    workout_key: string,
    rows: {
      id: string;
      work_sets: number;
      reps: { min: number; max: number };
      target_rir?: { min: number; max: number };
      progression_method:
        | "double_progression"
        | "repetition_progression"
        | "duration_progression"
        | "custom";
    }[],
  ): PlanExerciseItem[] =>
    rows.map((r, i) =>
      planExercise(`${workout_key}-${i + 1}`, r.id, {
        work_sets: r.work_sets,
        reps: r.reps,
        target_rir: r.target_rir,
        progression_method: r.progression_method,
      }),
    );
  return {
    version: "v2",
    starts_on: "2026-08-31",
    review_on: "2026-10-12",
    mode: "regular",
    status: "active",
    payload: {
      schema_version: 1,
      template_key: "ppl",
      plan_workouts: [
        {
          workout_key: "push",
          name: "推日",
          estimated_minutes: 60,
          exercises: exercises("push", [
            {
              id: "barbell-bench-press",
              work_sets: 4,
              reps: { min: 6, max: 8 },
              target_rir: { min: 1, max: 3 },
              progression_method: "double_progression",
            },
            {
              id: "seated-dumbbell-shoulder-press",
              work_sets: 3,
              reps: { min: 8, max: 12 },
              target_rir: { min: 1, max: 3 },
              progression_method: "double_progression",
            },
            {
              id: "parallel-bar-dip",
              work_sets: 3,
              reps: { min: 8, max: 12 },
              target_rir: { min: 1, max: 3 },
              progression_method: "repetition_progression",
            },
          ]),
        },
        {
          workout_key: "pull",
          name: "拉日",
          estimated_minutes: 60,
          exercises: exercises("pull", [
            {
              id: "pull-up",
              work_sets: 3,
              reps: { min: 6, max: 10 },
              target_rir: { min: 1, max: 3 },
              progression_method: "repetition_progression",
            },
            {
              id: "barbell-bent-over-row",
              work_sets: 4,
              reps: { min: 8, max: 10 },
              target_rir: { min: 1, max: 3 },
              progression_method: "double_progression",
            },
            {
              id: "dumbbell-reverse-fly",
              work_sets: 3,
              reps: { min: 12, max: 15 },
              target_rir: { min: 2, max: 3 },
              progression_method: "repetition_progression",
            },
          ]),
        },
        {
          workout_key: "legs",
          name: "腿日",
          estimated_minutes: 60,
          exercises: exercises("legs", [
            {
              id: "barbell-back-squat",
              work_sets: 4,
              reps: { min: 6, max: 8 },
              target_rir: { min: 1, max: 3 },
              progression_method: "double_progression",
            },
            {
              id: "barbell-romanian-deadlift",
              work_sets: 3,
              reps: { min: 8, max: 10 },
              target_rir: { min: 1, max: 3 },
              progression_method: "double_progression",
            },
            {
              id: "leg-extension",
              work_sets: 3,
              reps: { min: 12, max: 15 },
              target_rir: { min: 1, max: 3 },
              progression_method: "repetition_progression",
            },
          ]),
        },
      ],
      calendar_cycle: {
        anchor_date: "2026-08-31",
        slots: PPL_CALENDAR_SLOTS.map((s) => ({ ...s })),
      },
    },
  };
}

function seedState(): MockState {
  const plan: PlanVersion = seedPlanV2();
  const schedules = seedSchedules(plan);

  /** 按绑定训练日构造已接受安排快照（stage3 §3.5 种子） */
  const seedArrangement = (
    id: string,
    date: string,
    acceptedAt: string,
    opts: {
      /** deload：卧推 work_sets 减 1（4→3）并标 disposition */
      deloadBench?: boolean;
      reason?: string;
    },
  ): MockState["arrangement_revisions"][number] => {
    const sched = schedules.find((s) => s.date === date);
    const workout = sched
      ? findWorkout(plan.payload, sched.plan_workout_key)
      : undefined;
    if (!sched || !workout)
      throw new Error(`种子安排绑定的日程不存在：${date}`);
    const exercises = workout.exercises.map((e) => {
      if (
        opts.deloadBench &&
        e.exercise_id === "barbell-bench-press" &&
        e.prescription.kind === "reps"
      ) {
        return {
          ...e,
          prescription: {
            ...e.prescription,
            work_sets: e.prescription.work_sets - 1,
          },
          disposition: "deload" as const,
        };
      }
      return { ...e, disposition: "keep" as const };
    });
    const target: ArrangementTarget = {
      schema_version: 1,
      scheduled_session_id: sched.id,
      plan_version: plan.version,
      plan_workout_key: workout.workout_key,
      scheduled_on: date,
      exercises,
      ...(opts.reason ? { adjustment_reason: opts.reason } : {}),
    };
    return { id, target, accepted_at: acceptedAt };
  };

  const arrangement_revisions: MockState["arrangement_revisions"] = [
    seedArrangement("arr-seed-0831", "2026-08-31", "2026-08-31T08:05:00.000Z", {}),
    seedArrangement("arr-seed-0902", "2026-09-02", "2026-09-02T08:05:00.000Z", {}),
    seedArrangement("arr-seed-0907", "2026-09-07", "2026-09-07T08:10:00.000Z", {
      deloadBench: true,
      reason: "当日状态不佳：卧推减 1 组（方案 1）",
    }),
  ];

  /* 种子事实（stage3 §3.5）：
   * W1 2/3（08-31✓ / 09-02✓ / 09-04 漏）、W2 1/3（09-07✓ / 09-09 漏 / 09-11 今日待办）、
   * 三桶 1/1/1 按 09-07 对照安排、PR 卧推 80kg×8；09-05 无安排 incomplete。
   * 只有 09-07 记录显式携带 arrangement_revision_id（三桶只按对照安排判定）。 */
  const records: TrainingRecord[] = [
    {
      id: "rec-seed-0831",
      date: "2026-08-31",
      kind: "new",
      status: "valid",
      exercise: "杠铃平板卧推",
      variant: "杠铃",
      sets: [
        { weight_kg: 80, reps: 8, rir: 2, set_type: "working" },
        { weight_kg: 80, reps: 8, rir: 2, set_type: "working" },
        { weight_kg: 80, reps: 8, rir: 1, set_type: "working" },
        { weight_kg: 80, reps: 8, rir: 1, set_type: "working" },
      ],
      warmup_summary: "递增至 60kg",
      schedule_snapshot: "W1 · 推日 · PPL v2",
      scheduled_session_id: "sched-v2-2026-08-31",
      arrangement_revision_id: null,
    },
    {
      id: "rec-seed-0902",
      date: "2026-09-02",
      kind: "new",
      status: "valid",
      exercise: "自重引体向上",
      variant: "自重",
      sets: [
        { reps: 8, rir: 2, set_type: "working" },
        { reps: 7, rir: 2, set_type: "working" },
        { reps: 6, rir: 1, set_type: "working" },
      ],
      schedule_snapshot: "W1 · 拉日 · PPL v2",
      scheduled_session_id: "sched-v2-2026-09-02",
      arrangement_revision_id: null,
    },
    {
      id: "rec-seed-0907",
      date: "2026-09-07",
      kind: "new",
      status: "valid",
      exercise: "杠铃平板卧推",
      variant: "杠铃",
      sets: [
        // 对照安排（deload）：卧推 3 组、次数区间 6-8（RIR 不参与判定）
        { weight_kg: 75, reps: 7, rir: 2, set_type: "working" }, // met
        { weight_kg: 75, reps: 5, rir: 1, set_type: "working" }, // unmet（低于下限）
        { weight_kg: 75, set_type: "working" }, // pending（缺次数）
      ],
      warmup_summary: "递增至 60kg",
      schedule_snapshot: "W2 · 推日 · PPL v2 · 当次安排 deload",
      scheduled_session_id: "sched-v2-2026-09-07",
      arrangement_revision_id: "arr-seed-0907",
    },
    {
      id: "rec-seed-0905",
      date: "2026-09-05",
      kind: "new",
      status: "incomplete",
      exercise: "哑铃弯举",
      variant: "哑铃",
      sets: [{ weight_kg: 12, reps: 10, set_type: "working" }],
      schedule_snapshot: null,
      scheduled_session_id: null,
      arrangement_revision_id: null,
      revision_note: "无安排加练，待补全",
    },
  ];

  const stats: StatsSummary = {
    per_week: [],
    buckets: { met: 0, unmet: 0, pending: 0 },
    prs: [],
    data_updated_at: MOCK_UPDATED_AT,
  };

  const review: ReviewDoc = {
    text: [
      "## 阶段复盘（截至 2026-09-11）",
      "",
      "按 PPL v2 执行：W1 完成率 **2/3（66.7%）**（09-04 腿日漏练），W2 截至今日 **1/3（33.3%）**（09-09 拉日漏练、09-11 腿日待办）。",
      "",
      "- 08-31 卧推 80kg×8 全部符合；PR：卧推 80kg × 8。",
      "- 09-07 按当次安排（卧推减 1 组）：符合 1 / 未符合 1 / 待补全 1。",
      "- 自重引体不进重量 PR；09-05 无安排 incomplete 不进 PR 与完成率分子。",
      "",
      "> 复盘仅解释确定性统计结果；不修改数值，也不补充没有数据支持的因果结论。",
    ].join("\n"),
    stale: true,
    generated_at: "2026-09-08T21:00:00+08:00",
  };

  const provider: ProviderConfig = {
    protocol: "openai-compatible",
    base_url: "https://api.deepseek.com/v1",
    has_api_key: false,
    model: { name: "deepseek-chat", deployment: "cloud" },
    data_dir: "/home/user/.local/share/Fit-Agent",
  };

  const sessions: SessionSummary[] = [
    {
      id: "s1",
      title: "初始建档与计划生成",
      updated_at: "2026-08-31T19:20:00+08:00",
    },
    { id: "s2", title: "W1 打卡", updated_at: MOCK_UPDATED_AT },
  ];

  const messages = new Map<string, ChatMessage[]>([
    [
      "s1",
      [
        {
          id: "m1",
          role: "user",
          content:
            "你好，我想开始系统训练。目标增肌，有一点基础，每周能练 3 次，每次 60 分钟左右，家里有杠铃、哑铃和卧推架。",
        },
        {
          id: "m2",
          role: "assistant",
          content:
            "已为你建立档案并生成三分化 PPL 计划草稿，请到对话中的草稿卡确认后启用（正式写入以确认为准）。",
        },
        { id: "m3", role: "user", content: "确认采纳。" },
        {
          id: "m4",
          role: "assistant",
          content:
            "计划 PPL v2 已启用，开始日期 2026-08-31，复核日期 2026-10-12。",
        },
      ],
    ],
    [
      "s2",
      [
        {
          id: "m5",
          role: "user",
          content:
            "今天卧推 80kg 4组 每组8次，最后一组 RIR 1。热身递增到 60kg。",
        },
        {
          id: "m6",
          role: "assistant",
          content:
            "已整理为训练记录草稿，请核对每组重量、次数与 RIR 后确认采纳。",
        },
      ],
    ],
  ]);

  const state: MockState = {
    context_version: 3,
    provider,
    profile: {
      goal: "增肌（肌肥大）",
      experience: "初级（有少量训练经验）",
      weekly_frequency: 3,
      session_minutes: 60,
      equipment: ["杠铃", "哑铃", "卧推架", "引体架", "绳索"],
      body_weight_kg: 72.5,
      body_conditions: ["肩部偶有不适（颈后推举时明显）"],
    },
    // 只列当前有效的已确认限制（02 2.2）：两种粒度各一例，不携带状态语义
    restrictions: [
      { name: "杠铃颈后推举", scope: "specific_action", note: "肩部不适史" },
      {
        name: "颈前深蹲",
        scope: "movement_pattern",
        note: "膝部不适，改用颈后深蹲",
      },
    ],
    plan,
    plan_history: [],
    /** 种子计划的日程：锁定双态——date <= MOCK_TODAY → locked_by_date_rule + stored locked */
    schedules,
    records,
    arrangement_revisions,
    stats,
    review,
    sessions,
    messages,
    drafts: new Map(),
    draft_sessions: new Map(),
    onboarding: new Map(),
    confirm_receipts: new Map(),
    runs: new Map(),
    execution_slot_run_id: null,
    dev_confirm_failure: false,
  };
  // 启动即全量现算（stage3 §3.4：种子不再写字面 per_week/三桶/PR）
  recomputeStats(state);
  return state;
}

/**
 * 种子计划的日程（锁定双态）：按 D9 projectSchedules 投影；
 * date < MOCK_TODAY → locked_by_date_rule=true、stored_status=locked、locked_effective=true。
 * 不需要后台任务：存储标记与日期规则取并集。
 */
function seedSchedules(plan: PlanVersion): PlanScheduleEntry[] {
  return projectSchedules(plan.version, plan.payload, {
    starts_on: plan.starts_on,
    review_on: plan.review_on,
    today: MOCK_TODAY,
  });
}

/**
 * 全新空种子（stage1 F1-01）：无档案/限制/计划/记录，供建档闭环从零走查。
 * 复用 Stage 0 控制端点切换（POST /api/dev/reset {"seed":"empty"}），不是第二个种子机制。
 * provider 与默认种子一致（未配置 Key，走设置页配置链路）；context_version 从 0 起。
 */
function emptySeedState(): MockState {
  return {
    context_version: 0,
    provider: {
      protocol: "openai-compatible",
      base_url: "https://api.deepseek.com/v1",
      has_api_key: false,
      model: { name: "deepseek-chat", deployment: "cloud" },
      data_dir: "/home/user/.local/share/Fit-Agent",
    },
    profile: null,
    restrictions: [],
    plan: null,
    plan_history: [],
    schedules: [],
    records: [],
    arrangement_revisions: [],
    stats: {
      per_week: [],
      buckets: { met: 0, unmet: 0, pending: 0 },
      prs: [],
      data_updated_at: MOCK_UPDATED_AT,
    },
    review: {
      text: "当前没有可复盘的训练记录；完成打卡并确认后可生成复盘。",
      stale: false,
      generated_at: MOCK_UPDATED_AT,
    },
    sessions: [],
    messages: new Map(),
    drafts: new Map(),
    draft_sessions: new Map(),
    onboarding: new Map(),
    confirm_receipts: new Map(),
    runs: new Map(),
    execution_slot_run_id: null,
    dev_confirm_failure: false,
  };
}

/**
 * Stage 2 计划生成种子（plans/stage2.md §7 前置）：已配置、已建档、无红旗、尚无计划、
 * 无可信训练记录。provider 只置 has_api_key 徽章位，不存或返回任何 Key 明文；
 * context_version = 1（档案与限制经一次确认写入）。复用同一控制端点切换
 * （POST /api/dev/reset {"seed":"noplan"}），不是第二个种子机制。
 */
function noPlanSeedState(): MockState {
  const base = emptySeedState();
  base.context_version = 1;
  base.provider.has_api_key = true;
  base.profile = {
    goal: "增肌（肌肥大）",
    experience: "初级（有少量训练经验）",
    weekly_frequency: 3,
    session_minutes: 60,
    equipment: ["杠铃", "哑铃", "卧推架", "引体架", "绳索"],
    body_weight_kg: 72.5,
    body_conditions: [],
  };
  base.sessions = [
    { id: "s1", title: "计划生成", updated_at: MOCK_UPDATED_AT },
  ];
  base.messages.set("s1", []);
  return base;
}

/* ------------------------------- SSE 总线 --------------------------------- */

/**
 * SSE 总线（08 8.7）：SSE 只负责实时展示，无事件缓冲、无事件 ID、无 Last-Event-ID
 * 补读或重放——断线/刷新后经业务接口查询当前状态（GET /api/runs/active、会话消息
 * 与草稿），不依赖浏览器自动重连。
 */
class SseHub {
  /** 连接 → 心跳空闲窗口重置器（08 8.7：业务事件重置该连接的 15s 空闲计时） */
  private clients = new Map<ServerResponse, () => void>();

  /**
   * dev-only 控制位（演练 45s 无事件转查询）：不进 contract.ts、不进 SseEvent 联合类型，
   * 挂起期间业务事件整段丢弃——不缓冲、不补发、不重放（08 8.7 无事件缓冲规则）。
   */
  readonly dev: DevSuspendControl = { until: 0, heartbeat: false, dropped: 0 };

  /** 广播业务事件；返回值仅用于 dev 诊断（挂起窗口内为 false） */
  emit(event: string, data: unknown): boolean {
    if (this.suspended()) {
      this.dev.dropped += 1;
      return false;
    }
    const payload = `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
    for (const [client, resetIdle] of this.clients) {
      client.write(payload);
      resetIdle();
    }
    return true;
  }

  /** dev-only：当前是否处于业务事件挂起窗口 */
  suspended(): boolean {
    return this.dev.until > Date.now();
  }

  /**
   * dev-only：挂起业务事件 seconds 秒。
   * heartbeat=false 时同时静默心跳（完全静默，前端连续 45s 无事件即关闭连接转查询）；
   * heartbeat=true 时连接保活、只丢业务事件（前端不得转查询）。
   */
  devSuspend(seconds: number, heartbeat: boolean): void {
    this.dev.until = Date.now() + seconds * 1000;
    this.dev.heartbeat = heartbeat;
    this.dev.dropped = 0;
  }

  /** dev-only：解除挂起（心跳在下一个 15s 窗口内自行恢复；不补发任何已丢弃事件） */
  devResume(): void {
    this.dev.until = 0;
    this.dev.heartbeat = false;
    this.dev.dropped = 0;
  }

  /** dev-only：当前 SSE 连接数 */
  get clientCount(): number {
    return this.clients.size;
  }

  open(req: IncomingMessage, res: ServerResponse): void {
    res.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
    });
    // 心跳（08 8.7）：连续 15s 无业务事件时发命名 heartbeat（不入库、不分配 ID、
    // 无 UI 载荷）；业务事件重置空闲窗口，心跳发出后重新武装 15s
    let timer: ReturnType<typeof setTimeout> | undefined;
    const armHeartbeat = (): void => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        // dev-only 完全静默演练：挂起窗口内且 heartbeat=false 时只重武装计时、不写字节
        if (!(this.suspended() && !this.dev.heartbeat)) {
          res.write("event: heartbeat\ndata: {}\n\n");
        }
        armHeartbeat();
      }, 15000);
    };
    armHeartbeat();
    this.clients.set(res, armHeartbeat);
    req.on("close", () => {
      clearTimeout(timer);
      this.clients.delete(res);
    });
  }
}

/* ------------------------------ dev 控制元数据 ----------------------------- */

/**
 * dev-only：挂起控制形状（见 SseHub.dev）。
 */
interface DevSuspendControl {
  /** 挂起截止时刻（Date.now() 毫秒）；0 = 未挂起 */
  until: number;
  /** 挂起期间是否照发 heartbeat：false = 完全静默；true = 保活但无业务事件 */
  heartbeat: boolean;
  /** 挂起期间被丢弃的业务事件计数（仅供人工核对确实静默，不参与任何业务状态） */
  dropped: number;
}

/**
 * dev-only：允许注入的失败原因，取值全部来自契约 ErrorCode（不新增形状、不扩枚举）。
 * 默认注入 `interrupted_by_restart` 之外的通用失败；重启中断见 /api/dev/restart。
 */
const DEV_ERROR_CODES: readonly ErrorCode[] = [
  "invalid_request",
  "not_configured",
  "conversation_busy",
  "draft_stale",
  "draft_modified",
  "interrupted_by_restart",
];

function isInjectableErrorCode(value: string): value is ErrorCode {
  return (DEV_ERROR_CODES as readonly string[]).includes(value);
}

/** 默认挂起时长（>45s 转查询阈值）与上限（防止忘开忘关） */
const DEV_SUSPEND_DEFAULT_SECONDS = 60;
const DEV_SUSPEND_MAX_SECONDS = 300;

/**
 * dev-only 总开关（默认开启）：置 FIT_MOCK_DEV_CONTROLS=off 时整组控制端点返回 409。
 * mock 中间件只存在于 vite dev/preview 进程，本仓库产物由后端静态托管、不含 /api/dev/*；
 * 该开关用于把 mock 演示放到非本机环境时彻底关闭故障注入面。
 */
const DEV_CONTROLS_ENABLED = process.env.FIT_MOCK_DEV_CONTROLS !== "off";

/** 每个 dev 响应携带的边界声明，避免被误当作契约端点 */
const DEV_NOTE = [
  "dev-only mock 控制端点：不属于 src/lib/contract.ts 契约、无 SSE 事件类型变更，",
  "UI/api.ts 不消费；真实后端落地后随 src/mock/ 一并删除",
].join("");

/* --------------------------------- 工具 ----------------------------------- */

function json(res: ServerResponse, status: number, body: unknown): void {
  res.writeHead(status, { "Content-Type": "application/json; charset=utf-8" });
  res.end(JSON.stringify(body));
}

function apiError(res: ServerResponse, err: ApiError): void {
  json(res, err.http_status, err);
}

function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", (chunk: Buffer) => (data += chunk.toString()));
    req.on("end", () => resolve(data));
    req.on("error", reject);
  });
}

/**
 * dev 控制端点请求体：非法/空 JSON 一律视为空对象（mock 开发端点，不因请求体格式中断；
 * 各端点随后自行校验字段）。
 */
async function readJsonBody<T>(req: IncomingMessage): Promise<T> {
  try {
    return JSON.parse((await readBody(req)) || "{}") as T;
  } catch {
    return {} as T;
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

let idSeq = 100;
const nextId = (prefix: string) => `${prefix}-${(idSeq += 1)}`;

/* ------------------------------ 运行剧本 ---------------------------------- */

/**
 * 记录草稿状态派生（05 5.3）：由事实完整性派生 incomplete/valid，不单独存第二份状态。
 * incomplete 不进 PR/完成率分子；必填次数/时长缺失（后端 D8）= incomplete。
 */
export function deriveRecordDraftStatus(
  payload: RecordDraftPayload,
): RecordRevisionStatus {
  if (payload.exercises.length === 0) return "incomplete";
  for (const ex of payload.exercises) {
    if (ex.sets.length === 0) return "incomplete";
    for (const s of ex.sets) {
      // 组类型未明确 = incomplete（不默认 work）
      if (s.set_type === undefined || s.set_type === null) return "incomplete";
      // 缺次数且缺时长 = incomplete（不进 PR 与完成率分子）
      if (
        (s.reps === undefined || s.reps === null) &&
        (s.duration_seconds === undefined || s.duration_seconds === null)
      )
        return "incomplete";
    }
  }
  return "valid";
}

/** 记录载荷领域校验（stage3 §3.2：负数/非有限 RIR 不通过校验；revise 与 confirm 共用） */
function recordPayloadError(p: RecordDraftPayload): string | undefined {
  for (const ex of p.exercises) {
    for (const s of ex.sets) {
      if (
        s.rir !== undefined &&
        s.rir !== null &&
        (!Number.isFinite(s.rir) || s.rir < 0)
      )
        return `第 ${s.set_no} 组 RIR 不得为负数或非有限值`;
      if (
        s.reps !== undefined &&
        s.reps !== null &&
        (!Number.isFinite(s.reps) || s.reps < 0)
      )
        return `第 ${s.set_no} 组次数无效`;
      if (s.load && !Number.isFinite(Number(s.load.value_text)))
        return `第 ${s.set_no} 组负重不是有效数值`;
    }
  }
  return undefined;
}

/** RawLoad → 展示 kg（仅 unit=kg 时返回数值；否则 null） */
function rawLoadKg(load?: RawLoad | null): number | undefined {
  if (!load || load.unit !== "kg") return undefined;
  const n = Number(load.value_text);
  return Number.isFinite(n) ? n : undefined;
}

/** SetFacts → 展示 RecordSet（映射关系：rir null→缺省、assistance→assisted） */
function setFactsToRecordSet(s: SetFacts): {
  weight_kg?: number;
  reps?: number;
  rir?: number;
  set_type: "working" | "warmup";
  assisted?: boolean;
} {
  const weight_kg = rawLoadKg(s.load);
  return {
    ...(weight_kg !== undefined ? { weight_kg } : {}),
    ...(s.reps !== undefined && s.reps !== null ? { reps: s.reps } : {}),
    ...(s.rir !== undefined && s.rir !== null ? { rir: s.rir } : {}),
    set_type: s.set_type === "warmup" ? "warmup" : "working",
    ...(s.assistance === "assisted" ? { assisted: true } : {}),
  };
}

/** 打卡剧本的动作短语表（确定性 mock 解析，不扩目录、不是通用 NLU） */
const RECORD_EXERCISES: readonly { pattern: RegExp; id: string }[] = [
  { pattern: /卧推(?!架)/, id: "barbell-bench-press" },
  { pattern: /深蹲|背蹲/, id: "barbell-back-squat" },
  { pattern: /哑铃弯举|弯举/, id: "dumbbell-biceps-curl" },
  { pattern: /引体/, id: "pull-up" },
  // 罗马尼亚必须先于泛化「硬拉」，否则被误匹配为传统硬拉
  { pattern: /罗马尼亚/, id: "barbell-romanian-deadlift" },
  { pattern: /硬拉/, id: "barbell-deadlift" },
  { pattern: /划船/, id: "barbell-bent-over-row" },
];

/** 同日补充歧义（stage3 §3.2/§7 第 11 步）：草稿前先询问，不自动选拆 */
const SUPPLEMENT_CHOICE = /补充上一练|补充上一次|加到上一练/;
const NEW_SESSION_CHOICE = /新增一练|新练一次|另起一练/;
const SUPPLEMENT_AMBIGUOUS = /再补一组|补一组|补充/;

/** 相对日期 → 具体日期（展示层不用相对词；「今天」= 业务日 MOCK_TODAY） */
function resolveRecordDate(message: string): string {
  const explicit = message.match(/(\d{4}-\d{2}-\d{2})/);
  if (explicit) return explicit[1];
  if (/昨天/.test(message)) {
    const d = new Date(`${MOCK_TODAY}T00:00:00Z`);
    d.setUTCDate(d.getUTCDate() - 1);
    return d.toISOString().slice(0, 10);
  }
  return MOCK_TODAY;
}

interface ParsedRecordFacts {
  exercise_id: string;
  /** 负重原文数值；未提负重 = undefined（自重等） */
  weightText?: string;
  unit: "kg" | "lb";
  reps?: number;
  sets: SetFacts[];
  warmup?: string;
}

/**
 * 打卡反馈 → 结构化事实（stage3 §3.2）：逐组负重/次数；
 * RIR 不解析、不落库（已拍 2026-09-12：记录侧隐藏）；辅助/热身仍解析；无法解析时不编造。
 */
function parseRecordMessage(message: string): ParsedRecordFacts | undefined {
  let exercise_id: string | undefined;
  for (const { pattern, id } of RECORD_EXERCISES) {
    if (pattern.test(message)) {
      exercise_id = id;
      break;
    }
  }
  if (!exercise_id) return undefined;

  // 负重优先取与「N 组」同句的重量（避免热身「递增至 60kg」抢在工作组「100kg 4组」前）
  const weight = message.match(
    /(\d+(?:\.\d+)?)\s*(kg|公斤|磅|lb)\s*\D{0,6}\d+\s*组/i,
  );
  const setCount = message.match(/(\d+)\s*组/);
  const perSetReps = message.match(/每组\s*(\d+)\s*次/);
  const pairReps =
    message.match(/(\d+)\s*组\s*[x×]\s*(\d+)\s*次/i) ??
    message.match(/(\d+)\s*[x×]\s*(\d+)\s*次/i);
  const setN = setCount
    ? Number(setCount[1])
    : perSetReps || pairReps
      ? 1
      : undefined;
  if (setN === undefined || setN <= 0) return undefined;
  const reps = perSetReps
    ? Number(perSetReps[1])
    : pairReps
      ? Number(pairReps[2])
      : undefined;
  const unit: "kg" | "lb" = weight && /lb|磅/i.test(weight[2]) ? "lb" : "kg";

  /** 逐组覆盖：辅助（RIR 已拍不解析、不落库） */
  const assisted = new Set<number>();
  let assistedLast = false;
  for (const clause of message.split(/[，。；;、\n]/)) {
    if (
      !/朋友帮忙|有人辅助|辅助抬起|被辅助|让人辅助|辅助才|帮忙才|请人辅助|辅助完成/.test(
        clause,
      )
    )
      continue;
    const sm = clause.match(/第\s*(\d+)\s*组/);
    if (sm) assisted.add(Number(sm[1]));
    else if (/最后一组/.test(clause)) assistedLast = true;
  }

  // 热身摘要：原文保留（如「递增至 60kg」）
  const warmupM = message.match(
    /[^。；;\n]*?递增[至到]\s*\d+(?:\.\d+)?\s*(?:kg|公斤|磅|lb)/i,
  );
  const warmup = warmupM
    ? warmupM[0].replace(/^热身\s*/, "").trim()
    : undefined;

  const sets: SetFacts[] = Array.from({ length: setN }, (_, i) => {
    const no = i + 1;
    const isLast = no === setN;
    const isAssisted = assisted.has(no) || (isLast && assistedLast);
    return {
      set_no: no,
      set_type: "work" as const,
      ...(weight ? { load: { value_text: weight[1], unit } } : {}),
      ...(reps !== undefined ? { reps } : {}),
      rir: null,
      assistance: isAssisted ? ("assisted" as const) : null,
    };
  });

  return {
    exercise_id,
    ...(weight ? { weightText: weight[1] } : {}),
    unit,
    ...(reps !== undefined ? { reps } : {}),
    sets,
    ...(warmup ? { warmup } : {}),
  };
}

/**
 * 打卡剧本（stage3 F3-04）：对话反馈 → RecordDraftPayload（确定性 mock 解析）。
 * - 「今天/昨天/具体日期」→ 具体日期；未明确字段保持空、不编造；
 * - 同日补充歧义先询问（补充上一练 / 新增一练），未澄清不生成草稿；
 * - 当天已接受安排且动作在安排目标内时显式携带 arrangement_revision_id（绝不从日期推断）；
 * - 无法解析时回询问文案、不生成草稿。
 */
function recordScriptReply(
  state: MockState,
  message: string,
): { text: string; draft?: Draft } {
  const occurred_on = resolveRecordDate(message);
  const facts = parseRecordMessage(message);
  const sameDayRecords = state.records.filter((r) => r.date === occurred_on);

  // 归属歧义：先询问，不自动选拆（草稿前）
  if (
    !SUPPLEMENT_CHOICE.test(message) &&
    !NEW_SESSION_CHOICE.test(message) &&
    SUPPLEMENT_AMBIGUOUS.test(message) &&
    sameDayRecords.length > 0
  ) {
    return {
      text: [
        `${occurred_on} 已有训练记录。「再补一组」还不清楚归属，请先选择：`,
        "",
        "- **补充上一练**：并入既有训练身份（复用同一 training_session_id，不新增身份）；",
        "- **新增一练**：作为同日新的一练（新身份）。",
        "",
        "请直接回复「补充上一练」或「新增一练」，我再整理草稿。",
      ].join("\n"),
    };
  }

  if (!facts)
    return {
      text: [
        "我没能从这句话里整理出完整的结构化训练事实，因此**不生成草稿、也不编造数值**。",
        "",
        "请按此格式反馈：「今天卧推 80kg 4组 每组8次」；",
        "辅助如「第3组朋友帮忙抬起」；热身如「热身递增至 60kg」。",
      ].join("\n"),
    };

  // 稳定身份：补充上一练 → 复用当日最近一次身份；其余（含新增一练）= null 新身份
  let training_session_id: string | null = null;
  if (SUPPLEMENT_CHOICE.test(message)) {
    const last = [...sameDayRecords]
      .reverse()
      .find((r) => r.training_session_id);
    training_session_id = last?.training_session_id ?? null;
  }

  // 当次安排：当天已接受且该动作在安排目标内时显式携带（不从日期静默推断）
  const arrangement = [...state.arrangement_revisions]
    .reverse()
    .find(
      (a) =>
        a.target.scheduled_on === occurred_on &&
        a.target.exercises.some((e) => e.exercise_id === facts.exercise_id),
    );

  const cat = CATALOG.find((c) => c.id === facts.exercise_id);
  const exercises: DraftExerciseLog[] = [
    {
      position: 1,
      exercise_id: facts.exercise_id,
      record_type: cat?.record_type ?? "reps_weight",
      load_notation: cat?.load_convention ?? null,
      ...(facts.warmup ? { warmup_summary_text: facts.warmup } : {}),
      sets: facts.sets,
    },
  ];
  const payload: RecordDraftPayload = {
    occurred_on,
    training_session_id,
    ...(arrangement ? { arrangement_revision_id: arrangement.id } : {}),
    exercises,
  };
  const draft: Draft = {
    id: nextId("draft"),
    kind: "training_record",
    status: "pending",
    revision: 1,
    base_business_version: 0,
    payload,
    diff: recordDraftDiff(state, payload),
  };

  const assistedCount = facts.sets.filter(
    (s) => s.assistance === "assisted",
  ).length;
  const plainCount = facts.sets.length - assistedCount;
  const name = cat?.standard_name_zh ?? facts.exercise_id;
  return {
    text: [
      `已将打卡内容整理为训练记录草稿：**${name} ${facts.sets.length} 组**${
        facts.reps !== undefined ? ` × ${facts.reps} 次` : ""
      }${facts.weightText ? ` @ ${facts.weightText}${facts.unit}` : ""}（${occurred_on}）。`,
      facts.warmup ? `- 热身：${facts.warmup}（保留原文摘要）` : "",
      assistedCount > 0
        ? `- ${assistedCount} 组标注人工辅助；其余 ${plainCount} 组未提及辅助，按「无辅助」待确认展示（不默认 assisted）。`
        : "- 全部组未提及辅助，按「无辅助」待确认展示（不默认 assisted）。",
      arrangement
        ? `- 已关联当次安排 ${arrangement.id}（草稿卡展示「原计划 X 组 · 当次安排 Y 组」对照摘要）。`
        : "- 当天该动作无已接受的当次安排：不携带 arrangement_revision_id（无对照，不从日期推断）。",
      "",
      "请核对下方草稿卡，可内联纠错关键字段，确认采纳后才写入正式记录。",
    ]
      .filter((line) => line !== "")
      .join("\n"),
    draft,
  };
}

/**
 * 计划版本号只追加（PRD 5.3：正式版本不原地改）
 */
function nextPlanVersion(version: string): string {
  return `v${Number(version.slice(1)) + 1}`;
}

/**
 * 待确认计划草稿（01 1.2/1.3）：revision 从 1 起；kind = "plan"（对齐后端 DraftKind）
 */
function pendingPlanDraft(payload: PlanDraftPayload): Draft {
  return {
    id: nextId("draft"),
    kind: "plan",
    status: "pending",
    revision: 1,
    base_business_version: 0,
    payload,
    diff: payload.diff,
  };
}

/**
 * 结构化替换草稿载荷（D9）：拟议 payload 来自计划生成或处方改写；版本取当前计划下一版，
 * 生效范围取候选日期，日程按新 payload 重投影，旧版未来未锁定日程进取消清单。
 * 产出后仍按 planPayloadError 复检（fail-closed）。
 */
function replacementPayload(
  state: MockState,
  input: {
    profile: Profile;
    plan_workouts: PlanVersion["payload"]["plan_workouts"];
    title: string;
    extra_diff?: FieldDiff[];
  },
): PlanDraftPayload | undefined {
  const { profile, plan_workouts } = input;
  const previous = state.plan;
  const version = previous ? nextPlanVersion(previous.version) : "v1";
  const payloadBody: PlanVersion["payload"] = {
    schema_version: 1,
    template_key: "ppl",
    plan_workouts,
    calendar_cycle: {
      anchor_date: PLAN_CANDIDATE.starts_on,
      slots: PPL_CALENDAR_SLOTS.map((s) => ({ ...s })),
    },
  };
  const schedules = projectSchedules(version, payloadBody, {
    starts_on: PLAN_CANDIDATE.starts_on,
    review_on: PLAN_CANDIDATE.review_on,
  });
  const cancellations = previous
    ? state.schedules
        .filter(
          (s) =>
            s.plan_version === previous.version &&
            !s.locked_effective &&
            s.stored_status !== "cancelled",
        )
        .map((s) => ({
          scheduled_session_id: s.id,
          plan_version: s.plan_version,
          plan_workout_key: s.plan_workout_key,
          scheduled_on: s.date,
        }))
    : [];
  const plan: PlanVersion = {
    version,
    starts_on: PLAN_CANDIDATE.starts_on,
    review_on: PLAN_CANDIDATE.review_on,
    mode: "regular",
    status: "active",
    payload: payloadBody,
  };
  const payload: PlanDraftPayload = {
    title: input.title,
    diff: [
      ...planDraftDiff(
        plan,
        schedules,
        previous ? { previous_version: previous.version } : undefined,
      ),
      ...(input.extra_diff ?? []),
    ],
    plan,
    schedules,
    cancellations,
    candidates: planCandidates(profile, state.restrictions),
  };
  const invalid = planPayloadError(payload, {
    profile,
    restrictions: state.restrictions,
    today: MOCK_TODAY,
  });
  return invalid ? undefined : payload;
}

/**
 * 档案长期补丁 → 拟议完整档案（F2-04；01 1.4「受限组合计划草稿」）：补丁随计划一次确认写入，
 * 确认前不改正式档案。无正式档案时返回 null（由 `planPayloadError` 以「尚未建档」拒绝）。
 */
function profileWithPatch(
  current: Profile | null,
  patch: ProfileDraftPayload,
): Profile | null {
  if (!current) return null;
  const p = patch.profile;
  return {
    goal: p.goal ?? current.goal,
    experience: p.experience ?? current.experience,
    weekly_frequency: p.weekly_frequency ?? current.weekly_frequency,
    session_minutes: p.session_minutes ?? current.session_minutes,
    equipment: [...(p.equipment ?? current.equipment)],
    body_weight_kg: p.body_weight_kg ?? current.body_weight_kg,
    body_conditions: [...(p.body_conditions ?? current.body_conditions)],
  };
}

/**
 * 计划启用／替换事务（F2-04；01 1.4 原子确认、04 4.4 版本与日程）：
 * 领域复查由调用方完成，本函数只做一次确认内的全部写入——档案长期补丁、新版本启用、
 * 旧版归档保留、旧版未来未锁定日程取消（已锁定日程不动）、新日程写入、草稿提交、
 * context_version 一次递增；任一步失败按快照整份回滚（档案、计划与历史、全部日程、
 * 草稿状态、context_version），不留半写状态。mock 以顺序内存写入 + 快照回滚模拟同一事务，
 * 不建第二套事务机制。dev 故障注入位（POST /api/dev/confirm/fail-next）用于验证中途失败的回滚。
 */
function commitPlanDraft(
  state: MockState,
  draft: Draft,
  payload: PlanDraftPayload,
  ctx: { profile: Profile; restrictions: Restriction[] },
): { version: string; created: number; cancelled: number } {
  const plan = payload.plan as PlanVersion;
  const schedules = payload.schedules ?? [];
  const previous = state.plan;
  const snapshot = {
    context_version: state.context_version,
    profile: state.profile,
    restrictions: state.restrictions,
    plan: state.plan,
    plan_history: state.plan_history,
    schedules: state.schedules,
    draft_status: draft.status,
  };
  try {
    // 1) 长期档案补丁（与计划同一次确认、一次版本递增）
    if (payload.profile_patch) {
      state.profile = ctx.profile;
      state.restrictions = ctx.restrictions;
    }
    // 2) 旧版只归档不原地改（PRD 5.3）
    if (previous) {
      state.plan_history = [
        ...state.plan_history,
        { ...previous, status: "archived" },
      ];
    }
    // 3) 旧版未来未锁定日程取消，已锁定日程不动（04 4.4；effective 锁定并集）
    const cancellable = new Set(
      state.schedules
        .filter(
          (s) =>
            s.plan_version === previous?.version &&
            !s.locked_effective &&
            s.stored_status !== "cancelled",
        )
        .map((s) => s.id),
    );
    state.schedules = [
      ...state.schedules.map((s) =>
        cancellable.has(s.id)
          ? { ...s, stored_status: "cancelled" as const }
          : s,
      ),
      // 4) 新版本日程（草稿生成时已投影，这里归属到新版本；未来日一律 scheduled）
      ...schedules.map((s) => ({
        ...s,
        plan_version: plan.version,
        stored_status: "scheduled" as const,
        locked_by_date_rule: false,
        locked_effective: false,
      })),
    ];
    // 5) 新版本启用
    state.plan = { ...plan, status: "active" };
    // dev-only 故障注入：正式写入已完成、尚未提交草稿与推进版本
    if (state.dev_confirm_failure) {
      state.dev_confirm_failure = false; // 一次性注入
      throw new Error("dev 注入失败：计划启用事务在正式写入后失败");
    }
    // 6) 草稿提交与 context_version 递增同属这次提交（01 1.4）
    draft.status = "committed";
    state.context_version += 1;
    return {
      version: plan.version,
      created: schedules.length,
      cancelled: cancellable.size,
    };
  } catch (e) {
    state.context_version = snapshot.context_version;
    state.profile = snapshot.profile;
    state.restrictions = snapshot.restrictions;
    state.plan = snapshot.plan;
    state.plan_history = snapshot.plan_history;
    state.schedules = snapshot.schedules;
    draft.status = snapshot.draft_status;
    throw e;
  }
}

/**
 * 长期器械范围组合草稿的载荷（F2-04）：「以后只能用哑铃」= 档案器械补丁 + 新计划版本 +
 * 新日程 + 旧版未来未锁定日程取消，一次确认、一次 context_version 递增。
 * 当日临时限制（「今天只能用哑铃」）不走本路径，也不写长期档案。
 * 生成与重算共用本函数（01 1.6：重算以最新业务上下文重新派生同一类提案）。
 */
function dumbbellPayload(state: MockState): PlanDraftPayload | undefined {
  const profile = state.profile;
  if (!profile) return undefined;
  const proposed: Profile = { ...profile, equipment: ["哑铃"] };
  const built = buildPplDraft({
    profile: proposed,
    restrictions: state.restrictions,
  });
  if (!built.ok) return undefined;
  const base = state.plan
    ? replacementPayload(state, {
        profile: proposed,
        plan_workouts: built.plan.payload.plan_workouts,
        title: `器械范围调整（${state.plan.version} → 拟议替换）`,
      })
    : built.payload;
  if (!base) return undefined;
  const patch: ProfileDraftPayload = {
    profile: proposed,
    restrictions: state.restrictions.map((r) => ({ ...r })),
  };
  const diff: FieldDiff[] = [
    ...profilePayloadDiff({ profile, restrictions: state.restrictions }, patch),
    ...base.diff,
  ];
  return {
    ...base,
    title: "器械范围调整（以后只能用哑铃）",
    profile_patch: patch,
    diff,
  };
}

/**
 * 长期器械范围的组合草稿回复（F2-04；plans/stage2.md §7 第 7 步）
 */
function dumbbellOnlyReply(state: MockState): { text: string; draft?: Draft } {
  const profile = state.profile;
  if (!profile) return { text: NO_PROFILE_PLAN_REPLY };
  const payload = dumbbellPayload(state);
  if (!payload)
    return {
      text: "按当前档案与限制无法生成仅用哑铃的计划（未通过安全前置校验）；未生成草稿，正式档案、计划与日程保持不变。",
    };
  return {
    text: [
      "已按「以后只能用哑铃」生成一张组合草稿（一次确认、一次版本递增）：",
      "",
      `- 档案器械补丁：${profile.equipment.join("、")} → **哑铃**（长期生效）`,
      `- 拟议计划版本：${payload.plan?.version ?? "—"}${state.plan ? `（旧版 ${state.plan.version} 归档保留）` : "（新建）"}`,
      `- 具体日程：${payload.schedules?.length ?? 0} 个应训练日`,
      "",
      "确认前正式档案、计划与日程均不变；确认后档案补丁与新计划版本同时生效。",
    ].join("\n"),
    draft: pendingPlanDraft(payload),
  };
}

/**
 * 处方改写提案（存量演示剧本：推日减量）：按 D9 重建 plan_workouts，只改组数与目标 RIR；
 * 版本、日程与取消清单由 replacementPayload 按当前正式计划派生。
 */
function adjustPlanProposal(
  state: MockState,
): { payload: PlanDraftPayload; rows: FieldDiff[] } | undefined {
  const profile = state.profile;
  if (!profile || !state.plan) return undefined;
  const built = buildPplDraft({
    profile,
    restrictions: state.restrictions,
  });
  if (!built.ok) return undefined;
  const edits: Record<
    string,
    { work_sets?: number; target_rir?: { min: number; max: number } }
  > = {
    "barbell-bench-press": { work_sets: 3, target_rir: { min: 2, max: 3 } },
    "seated-dumbbell-shoulder-press": { work_sets: 2 },
  };
  const rows: FieldDiff[] = [];
  const plan_workouts = built.plan.payload.plan_workouts.map((w) => ({
    ...w,
    exercises: w.exercises.map((e) => {
      const edit = edits[e.exercise_id];
      if (!edit || e.prescription.kind !== "reps") return e;
      const oldSets = e.prescription.work_sets;
      const oldRir = e.prescription.target_rir;
      const nextPrescription = {
        ...e.prescription,
        work_sets: edit.work_sets ?? oldSets,
        ...(edit.target_rir ? { target_rir: edit.target_rir } : {}),
      };
      if (edit.work_sets !== undefined && edit.work_sets !== oldSets)
        rows.push({
          field: `${e.display_snapshot.name} · 组数`,
          old_value: `${oldSets} 组`,
          new_value: `${edit.work_sets} 组`,
        });
      if (
        edit.target_rir &&
        JSON.stringify(edit.target_rir) !== JSON.stringify(oldRir)
      )
        rows.push({
          field: `${e.display_snapshot.name} · 目标 RIR`,
          old_value: deriveRangeLabel(oldRir),
          new_value: deriveRangeLabel(edit.target_rir),
        });
      return { ...e, prescription: nextPrescription };
    }),
  }));
  const payload = replacementPayload(state, {
    profile,
    plan_workouts,
    title: `推日减量（${state.plan.version} → 拟议替换）`,
    extra_diff: rows,
  });
  return payload ? { payload, rows } : undefined;
}

/**
 * 计划类回复（F2-02/F2-04）：尚无正式计划时从正式档案生成 PPL 草稿；已有计划时给结构化
 * 替换草稿（处方改写 + 只追加新版本 + 旧版未来未锁定日程取消）。
 */
function planScriptReply(state: MockState): { text: string; draft?: Draft } {
  if (!state.plan) return newPlanReply(state);
  if (!state.profile) return { text: NO_PROFILE_PLAN_REPLY };
  const proposal = adjustPlanProposal(state);
  if (!proposal)
    return {
      text: "按当前档案与限制无法生成这次计划调整（未通过安全前置校验）；未生成草稿，正式计划保持不变。",
    };
  const { payload, rows } = proposal;
  return {
    text: [
      `根据近期表现，建议对计划做如下调整（只追加新版本 ${payload.plan?.version}，不静默覆盖 ${state.plan.version}）：`,
      "",
      ...rows.map(
        (row) => `- ${row.field}：${row.old_value} → **${row.new_value}**`,
      ),
      "",
      "请确认后启用；确认前正式计划与日程不变。",
    ].join("\n"),
    draft: pendingPlanDraft(payload),
  };
}

/**
 * F2-05：请求基于当前计划的训练日指导（只读，不生成任何草稿——计划修改仍须经对话草稿确认）。
 * 给出处方前先用最新身体情况分类与限制复核整份计划（04 4.5）：
 * - 命中六类安全症状时独立阻断：建议线下专业评估，不给任何常规处方；
 * - 任一动作命中具体动作／动作模式限制：整份阻断，**不**只跳过冲突动作继续给其余动作的处方。
 * 两种阻断都只影响「使用时」：当前计划与其当前日程仍可在 /profile 查看，修订从对话发起。
 */
/**
 * 指导用参考工作组重量：取该动作最近一条 valid 记录的非辅助工作组重量。
 * 首次（无记录）仍走校准文案；有记录后直接标重量（用户走查反馈 2026-09-12）。
 */
function referenceWorkKg(
  state: MockState,
  exerciseId: string,
): number | undefined {
  const cat = CATALOG.find((c) => c.id === exerciseId);
  if (!cat) return undefined;
  for (let i = state.records.length - 1; i >= 0; i--) {
    const rec = state.records[i];
    if (rec.status !== "valid") continue;
    if (rec.exercise !== cat.standard_name_zh) continue;
    for (let j = rec.sets.length - 1; j >= 0; j--) {
      const s = rec.sets[j];
      if (s.set_type === "working" && !s.assisted && s.weight_kg !== undefined)
        return s.weight_kg;
    }
  }
  return undefined;
}

/** 指导单行动作：有参考重量 → 直接标 kg；无 → 组次 + 目标用力大白话 */
function guidanceExerciseLine(
  state: MockState,
  e: PlanExerciseItem | ArrangementTarget["exercises"][number],
): string {
  const name = e.display_snapshot.name;
  const kg = referenceWorkKg(state, e.exercise_id);
  if (kg !== undefined)
    return `- ${name} ${kg}kg ${prescriptionLabel(e.prescription)}（${progressionLabel(e.progression.method)}）`;
  const effort =
    e.prescription.kind === "reps" && e.prescription.target_rir
      ? `，${effortPlainLabel(e.prescription.target_rir)}`
      : "";
  return `- ${name} ${prescriptionLabel(e.prescription)}${effort}（${progressionLabel(e.progression.method)}）`;
}

function planGuidanceReply(state: MockState): string {
  /* 安全症状独立阻断（02 2.3；读取时分类）：与有没有计划无关，先于计划检查——不生成、也不给出任何常规处方 */
  const redFlags = classifyBodyConditions(
    state.profile?.body_conditions,
  ).confirmed;
  if (redFlags.length > 0)
    return [
      professionalEvalBlock(redFlags),
      "当前身体情况命中需要专业评估的症状，本次不给出任何基于计划的训练指导。请先完成线下专业评估；经专业人员确认可以恢复训练后，再从对话调整档案与计划。",
    ].join("\n\n");

  const plan = state.plan;
  if (!plan) return NO_PLAN_GUIDANCE_REPLY;
  /* 普通身体情况非空但未命中六类：不阻断，只提示需澄清（不判定安全） */
  const unlisted = classifyBodyConditions(
    state.profile?.body_conditions,
  ).unlisted;
  const clarification =
    unlisted.length > 0 ? [unlistedSymptomBlock(unlisted)] : [];
  const safety = reviewPlanSafety({
    plan,
    profile: state.profile,
    restrictions: state.restrictions,
    context_version: state.context_version,
  });

  /* 安全症状已在上面拦截，这里的不可用只剩限制冲突/目录缺失（仍按整份阻断，不降级为逐动作跳过） */
  if (!safety.usable) {
    return [
      `按最新限制复核当前计划 ${plan.version}：**整份计划指导已阻断**——任一动作命中限制时，我不会给出任何基于该计划或已接受安排的处方，也不会只跳过冲突动作、继续给其余「未冲突」动作的处方。`,
      ...(safety.block_code === "plan_action_unavailable"
        ? [
            "计划引用的动作目录身份缺失（plan_action_unavailable）：无法安全给出基于该计划或已接受安排的处方。",
          ]
        : []),
      "",
      ...safety.conflicts.map(
        (c) =>
          `- 冲突：${c.exercise_name} 命中限制「${c.restriction.name}」（${c.restriction.scope === "specific_action" ? "具体动作" : "动作模式"}）`,
      ),
      "",
      `正式计划 ${plan.version} 与它的当前日程仍可在「档案与限制」页查看（不隐藏、不改写，也不标为「部分可用」）。`,
      "修改计划只能从对话发起：说明你要调整的内容，我给出修订草稿，确认后生成新版本。",
    ]
      .filter((line) => line !== "")
      .join("\n");
  }

  /* 指导日：今日仍应训练则优先今日（日期规则锁定 ≠ 处方锁定），否则下一个未到期应训练日 */
  const upcoming = state.schedules
    .filter(
      (s) =>
        s.plan_version === plan.version &&
        s.date >= MOCK_TODAY &&
        s.stored_status !== "cancelled",
    )
    .sort((a, b) => a.date.localeCompare(b.date))[0];
  const workout = upcoming
    ? findWorkout(plan.payload, upcoming.plan_workout_key)
    : undefined;
  if (!upcoming || !workout)
    return [
      `当前计划 ${plan.version} 通过最新身体情况与限制复核，但区间内没有未到期的应训练日（${plan.starts_on} ~ ${plan.review_on}）。`,
      "复核日后的续期与跨周期切换不在本阶段范围内；如需新计划请从对话发起。",
      ...clarification,
    ].join("\n");

  /* F3-03：存在已接受安排的应训练日时按安排目标（最新快照优先），否则按原计划处方 */
  const arrangement = [...state.arrangement_revisions]
    .reverse()
    .find(
      (a) =>
        a.target.scheduled_session_id === upcoming.id ||
        a.target.scheduled_on === upcoming.date,
    );
  const exercises = arrangement ? arrangement.target.exercises : workout.exercises;
  const calibration = exercises.find(
    (e) => e.load?.kind === "needs_calibration",
  )?.load;
  return [
    `下一个应训练日：${upcoming.date}（${weekdayLabel(upcoming.weekday)}）· ${workout.name} · 预计 ${workout.estimated_minutes} 分钟（计划 ${plan.version}${arrangement ? " · **按已接受安排**" : ""}）`,
    ...(arrangement?.target.adjustment_reason
      ? [`安排原因：${arrangement.target.adjustment_reason}`]
      : []),
    "",
    ...exercises.map((e) => guidanceExerciseLine(state, e)),
    "",
    exercises.every((e) => referenceWorkKg(state, e.exercise_id) !== undefined)
      ? "负荷：已按近期有效工作组记录直接标明参考重量；加重仍须逐级试重。"
      : [
          "负荷：首次或尚无该动作有效记录时，不给出具体起始重量，按「需要校准」逐级试重；有记录后将直接标明重量。",
          "目标用力示例：卧推 60kg 一组能做 8 个、再推 2 个就起不来 = 每一组结束还能再做 2 次的重量。",
          calibration && calibration.kind === "needs_calibration"
            ? `- 通过标准：${calibration.pass_criteria}；停止条件：${calibration.stop_criteria}`
            : "",
        ]
          .filter(Boolean)
          .join("\n"),
    "",
    arrangement
      ? "该指导依据当前计划的已接受安排与最新安全复核；如出现疼痛或需专业评估的情况请立即停止并按线下专业评估处理。"
      : "该指导依据当前计划与最新安全复核；如出现疼痛或需专业评估的情况请立即停止并按线下专业评估处理。",
    ...clarification,
  ]
    .filter((line) => line !== "")
    .join("\n");
}

/**
 * F2-02：从正式档案与当前有效限制生成 PPL 计划草稿（新建 v1）。
 * 缺档案、身体情况命中六类安全症状或排不进档案约束时不给任何处方，只说明原因与下一步。
 */
function newPlanReply(state: MockState): { text: string; draft?: Draft } {
  const built = buildPplDraft({
    profile: state.profile,
    restrictions: state.restrictions,
  });
  if (!built.ok) {
    if (built.code === "red_flag")
      return {
        text: [
          professionalEvalBlock(built.red_flags),
          "当前身体情况命中需要专业评估的症状，因此我不会生成任何计划处方。请先完成线下专业评估；经专业人员确认可以恢复训练后，再回来调整档案与计划。",
        ].join("\n\n"),
      };
    if (built.code === "no_profile")
      return {
        text: [
          "还没有正式档案，我不会凭空生成计划处方。",
          "请在对话中补齐建档信息（目标、经验、每周频率、单次时长、可用器械、体重、动作限制与身体情况），确认后再生成计划。",
        ].join("\n\n"),
      };
    return {
      text: [
        `当前条件排不出符合档案约束的 PPL 计划：${built.reason}。`,
        "未生成任何计划草稿；请调整档案或说明可用日期后重试。",
      ].join("\n\n"),
    };
  }

  const { plan, schedules } = built;
  const first = schedules[0];
  const last = schedules[schedules.length - 1];
  const blocks = derivePlanBlocks(plan.payload);
  const rows = blocks.map((b) => {
    const wdays = b.weekday !== undefined ? weekdayLabel(b.weekday) : "—";
    return `- ${b.name}（${wdays}，预计 ${b.estimated_minutes} 分钟）：${b.exercises
      .map((e) => {
        const effort =
          e.prescription.kind === "reps" && e.prescription.target_rir
            ? `，${effortPlainLabel(e.prescription.target_rir)}`
            : "";
        return `${e.display_snapshot.name} ${prescriptionLabel(e.prescription)}${effort}`;
      })
      .join("；")}`;
  });
  const draft: Draft = {
    id: nextId("draft"),
    kind: "plan",
    status: "pending",
    revision: 1,
    base_business_version: 0,
    payload: built.payload,
    diff: built.payload.diff,
  };
  const weekdays = blocks
    .map((b) => b.weekday)
    .filter((w): w is number => w !== undefined)
    .sort((a, b) => a - b);
  return {
    text: [
      `已按正式档案生成 PPL 计划草稿（新建 ${plan.version}）：`,
      "",
      `- 生效范围：${plan.starts_on} 起，复核日期 ${plan.review_on}，每周 ${[...new Set(weekdays)].map(weekdayLabel).join(" / ")}`,
      `- 具体日程：${first?.date} ~ ${last?.date} 共 ${schedules.length} 个应训练日（复核日当天不排日程）`,
      ...rows,
      "",
      "负荷：当前没有可信训练记录，我不会给出任何起始重量；每个动作按「需要校准」处理（逐级试重，稳定完成处方次数下限才算通过；RIR 仅作展示参考）。",
      "",
      "请在草稿卡核对后确认；确认前正式计划与日程不变。",
    ].join("\n"),
    draft,
  };
}

/** 纠错后训练记录草稿的 diff 再生成（01 1.2；从纠错后 payload 派生；含安排对照摘要） */
function recordDraftDiff(
  state: MockState,
  p: RecordDraftPayload,
): FieldDiff[] {
  const rows: FieldDiff[] = [
    { field: "训练记录 · 日期", new_value: p.occurred_on },
    {
      field: "归属训练身份",
      new_value: p.training_session_id
        ? `补充/更正既有 ${p.training_session_id}`
        : "新增（确认时建立）",
    },
  ];
  // 对照摘要（有安排时；从 arrangement target vs 计划派生；无安排不显示）
  const arr = p.arrangement_revision_id
    ? state.arrangement_revisions.find(
        (r) => r.id === p.arrangement_revision_id,
      )
    : undefined;
  if (arr) {
    const plannedWorkout =
      state.plan && state.plan.version === arr.target.plan_version
        ? findWorkout(state.plan.payload, arr.target.plan_workout_key)
        : undefined;
    for (const ex of p.exercises) {
      const item = arr.target.exercises.find(
        (e) => e.exercise_id === ex.exercise_id,
      );
      const planned = plannedWorkout?.exercises.find(
        (e) => e.exercise_id === ex.exercise_id,
      );
      if (
        item?.prescription.kind === "reps" &&
        planned?.prescription.kind === "reps"
      )
        rows.push({
          field: `对照 · ${item.display_snapshot.name}`,
          new_value: `原计划 ${planned.prescription.work_sets} 组 · 当次安排 ${item.prescription.work_sets} 组`,
        });
    }
  }
  for (const ex of p.exercises) {
    const working = ex.sets.filter((s) => s.set_type === "work");
    const first = working[0];
    rows.push({
      field: `动作 ${ex.position} · 工作组`,
      new_value: `${working.length} 组 x ${first?.reps ?? "?"} 次 @ ${first?.load?.value_text ?? "?"}${first?.load?.unit ?? ""}`,
    });
    if (ex.warmup_summary_text)
      rows.push({
        field: "热身",
        new_value: `${ex.warmup_summary_text}（摘要）`,
      });
  }
  const status = deriveRecordDraftStatus(p);
  if (status === "incomplete")
    rows.push({ field: "修订状态", new_value: "待补全（incomplete）" });
  return rows;
}

/* ---------------------- 当次安排（S3-08）辅助 ---------------------- */

/** 处置中文标签（mock 内展示；页面主词不用英文缩写） */
const DISPOSITION_LABEL: Record<
  ArrangementItemDisposition,
  string
> = {
  keep: "保留（目标更保守）",
  deload: "减载",
  equivalent_replace: "同等刺激替换",
  local_skip: "局部跳过",
};

/**
 * 安排草稿校验（mock 内，复刻 S4-04 四种处置 + legacy 兼容路径的语义）：
 * - 身份/绑定/展示快照/递增不得改；不得增删动作；
 * - keep：组次次数负荷全等，只可升目标用力（目标 RIR 只增不减）；
 * - deload：组次次数只减、负荷只降且在已验证重量 50–70%、至少一项真的降低；
 *   需校准动作不得改负荷；不改目标用力（属 keep 范畴）；
 * - equivalent_replace：处方与负荷照抄计划；replacement_exercise_id 必填且目录身份合法；
 * - local_skip：目标保持计划值，仅标注 disposition；
 * - legacy（disposition 缺省）：只减组 / 只升目标用力；
 * - 有实质差异必须非空白 adjustment_reason（仅标注处置不算差异）。
 * 返回 undefined = 通过。
 */
function arrangementPayloadError(
  state: MockState,
  p: ArrangementDraftPayload,
): string | undefined {
  const t = p.target;
  if (!t || t.schema_version !== 1) return "安排目标缺 schema_version=1";
  if (!t.scheduled_session_id.trim()) return "安排目标缺 scheduled_session_id";
  if (!t.plan_version.trim()) return "安排目标缺 plan_version";
  if (!t.plan_workout_key.trim()) return "安排目标缺 plan_workout_key";
  if (!t.scheduled_on) return "安排目标缺 scheduled_on";
  const plan = state.plan;
  if (!plan || plan.version !== t.plan_version)
    return `安排目标绑定的计划版本 ${t.plan_version} 不是当前正式计划`;
  // 绑定校验：session_id 必须命中当前计划下真实日程；scheduled_on / plan_workout_key 与该日程一致
  const boundSched = state.schedules.find(
    (s) => s.id === t.scheduled_session_id && s.plan_version === t.plan_version,
  );
  if (!boundSched)
    return `安排目标绑定的 scheduled_session_id ${t.scheduled_session_id} 不在当前计划日程内`;
  if (boundSched.date !== t.scheduled_on)
    return `安排目标 scheduled_on ${t.scheduled_on} 与日程 ${t.scheduled_session_id} 的日期 ${boundSched.date} 不一致`;
  if (boundSched.plan_workout_key !== t.plan_workout_key)
    return `安排目标 plan_workout_key ${t.plan_workout_key} 与日程 ${t.scheduled_session_id} 的训练日 ${boundSched.plan_workout_key} 不一致`;
  const workout = findWorkout(plan.payload, t.plan_workout_key);
  if (!workout) return `安排目标绑定的训练日 ${t.plan_workout_key} 不在计划内`;
  if (t.exercises.length !== workout.exercises.length)
    return "安排目标必须与绑定训练日逐条对应";
  const catalogIds = new Set(CATALOG.map((c) => c.id));
  let hasDiff = false;

  for (let i = 0; i < workout.exercises.length; i++) {
    const planned = workout.exercises[i];
    const item = t.exercises[i];
    if (!planned || !item) return "安排目标必须与绑定训练日逐条对应";
    if (item.item_key !== planned.item_key)
      return "安排目标不得增删、重排或改名动作条目";
    if (
      item.exercise_id !== planned.exercise_id ||
      item.record_type !== planned.record_type ||
      JSON.stringify(item.display_snapshot) !==
        JSON.stringify(planned.display_snapshot) ||
      JSON.stringify(item.progression) !== JSON.stringify(planned.progression)
    )
      return `安排目标不得改动作身份／展示快照／递增：${planned.item_key}`;
    if (item.prescription.kind !== planned.prescription.kind)
      return `安排目标不得改处方类型：${planned.item_key}`;

    const d = item.disposition;
    if (d !== undefined && !(d in DISPOSITION_LABEL))
      return `处置不在已拍四类内：${String(d)}`;

    // 处方结构：两边都 reps 或都 timed（上面 kind 已比对）
    if (
      planned.prescription.kind === "reps" &&
      item.prescription.kind === "reps"
    ) {
      const prx = planned.prescription;
      const irx = item.prescription;
      const sameReps =
        JSON.stringify(irx.reps_range) === JSON.stringify(prx.reps_range);
      const sameSets = irx.work_sets === prx.work_sets;
      const pr = prx.target_rir;
      const ir = irx.target_rir;
      const rirEqual = JSON.stringify(ir) === JSON.stringify(pr);
      const rirRaised =
        pr !== undefined &&
        ir !== undefined &&
        ir.min >= pr.min &&
        ir.max >= pr.max &&
        !rirEqual;
      const rirLowered =
        pr !== undefined &&
        (ir === undefined || ir.min < pr.min || ir.max < pr.max);
      const loadSame =
        JSON.stringify(item.load ?? null) === JSON.stringify(planned.load ?? null);

      if (d === "local_skip") {
        if (!sameReps || !sameSets || !rirEqual || !loadSame)
          return `局部跳过的目标必须保持计划值，仅标注处置：${planned.item_key}`;
        if (item.replacement_exercise_id != null)
          return `局部跳过不得携带替代动作身份：${planned.item_key}`;
        continue; // 仅标注不算实质差异
      }
      if (d === "equivalent_replace") {
        if (!sameReps || !sameSets || !rirEqual || !loadSame)
          return `同等刺激替换必须照抄计划的处方与负荷，不得自行造处方或重量：${planned.item_key}`;
        if (!item.replacement_exercise_id)
          return `同等刺激替换缺少 replacement_exercise_id：${planned.item_key}`;
        if (item.replacement_exercise_id === item.exercise_id)
          return `替代动作身份与原动作相同，不构成替换：${planned.item_key}`;
        if (!catalogIds.has(item.replacement_exercise_id))
          return `替代动作身份不在目录内：${item.replacement_exercise_id}`;
        hasDiff = true;
        continue;
      }
      if (d === "keep") {
        if (item.replacement_exercise_id != null)
          return `保留项不得携带替代动作身份：${planned.item_key}`;
        if (!sameReps || !sameSets || !loadSame)
          return `保留项的组数、次数与负荷必须与计划相同：${planned.item_key}`;
        if (pr === undefined) {
          if (ir !== undefined)
            return `计划目标没有目标用力，不得凭当次调整新造一个：${planned.item_key}`;
        } else if (ir === undefined) {
          return `当次目标不得删除计划已有的目标用力：${planned.item_key}`;
        } else if (rirLowered) {
          return `保留项只能提高目标用力、不得降低：${planned.item_key}`;
        } else if (rirRaised) hasDiff = true;
        continue;
      }
      if (d === "deload") {
        if (item.replacement_exercise_id != null)
          return `减载项不得携带替代动作身份：${planned.item_key}`;
        if (!rirEqual)
          return `减载方案 1–3 不改目标用力（提高用力属于保留）：${planned.item_key}`;
        if (!sameSets && irx.work_sets > prx.work_sets)
          return `减载只能减组、不得增组：${planned.item_key}`;
        if (!sameReps) {
          if (irx.reps_range.min > prx.reps_range.min || irx.reps_range.max > prx.reps_range.max)
            return `减载只能减少每组次数：${planned.item_key}`;
        }
        if (!loadSame) {
          const plannedLoad = planned.load;
          const itemLoad = item.load;
          if (
            !plannedLoad ||
            plannedLoad.kind !== "verified" ||
            !itemLoad ||
            itemLoad.kind !== "verified"
          )
            return `减载改负荷只适用于已有已验证重量的动作（需校准动作回落方案 1）：${planned.item_key}`;
          if (
            plannedLoad.unit !== itemLoad.unit ||
            plannedLoad.load_notation !== itemLoad.load_notation
          )
            return `减载不得改负荷单位或负重口径：${planned.item_key}`;
          const ratio = itemLoad.value / plannedLoad.value;
          if (!(ratio >= 0.5 && ratio <= 0.7))
            return `减载只能降到已验证重量的 50–70%：${planned.item_key}`;
        }
        const reduced =
          irx.work_sets < prx.work_sets ||
          (!sameReps &&
            (irx.reps_range.min < prx.reps_range.min ||
              irx.reps_range.max < prx.reps_range.max)) ||
          !loadSame;
        if (!reduced)
          return `减载必须真的减少组数、次数或负荷之一：${planned.item_key}`;
        hasDiff = true;
        continue;
      }

      // legacy：disposition 缺省 —— 只减组 / 只升目标用力
      if (item.replacement_exercise_id != null)
        return `未标注处置的条目不得携带替代动作身份：${planned.item_key}`;
      if (!sameReps || !loadSame)
        return `当次目标不得改次数区间或负荷：${planned.item_key}`;
      if (!sameSets && irx.work_sets > prx.work_sets)
        return `当次调整只能减组、不得增组：${planned.item_key}`;
      if (!sameSets) hasDiff = true;
      if (pr === undefined) {
        if (ir !== undefined)
          return `计划目标没有目标用力，不得凭当次调整新造一个：${planned.item_key}`;
      } else if (ir === undefined) {
        return `当次目标不得删除计划已有的目标用力：${planned.item_key}`;
      } else if (rirLowered) {
        return `当次调整只能提高目标用力、不得降低：${planned.item_key}`;
      } else if (rirRaised) hasDiff = true;
    } else if (
      planned.prescription.kind === "timed" &&
      item.prescription.kind === "timed"
    ) {
      const sameDuration =
        JSON.stringify(item.prescription.duration_seconds_range) ===
        JSON.stringify(planned.prescription.duration_seconds_range);
      const sameSets =
        item.prescription.work_sets === planned.prescription.work_sets;
      const loadSame =
        JSON.stringify(item.load ?? null) ===
        JSON.stringify(planned.load ?? null);
      if (d === "equivalent_replace") {
        if (!sameDuration || !sameSets || !loadSame)
          return `同等刺激替换必须照抄计划的处方与负荷：${planned.item_key}`;
        if (!item.replacement_exercise_id)
          return `同等刺激替换缺少 replacement_exercise_id：${planned.item_key}`;
        if (item.replacement_exercise_id === item.exercise_id)
          return `替代动作身份与原动作相同，不构成替换：${planned.item_key}`;
        if (!catalogIds.has(item.replacement_exercise_id))
          return `替代动作身份不在目录内：${item.replacement_exercise_id}`;
        hasDiff = true;
        continue;
      }
      if (d === "local_skip") {
        if (!sameDuration || !sameSets || !loadSame)
          return `局部跳过的目标必须保持计划值，仅标注处置：${planned.item_key}`;
        if (item.replacement_exercise_id != null)
          return `局部跳过不得携带替代动作身份：${planned.item_key}`;
        continue;
      }
      if (d === "keep") {
        if (item.replacement_exercise_id != null)
          return `保留项不得携带替代动作身份：${planned.item_key}`;
        if (!sameDuration || !sameSets || !loadSame)
          return `计时型保留项的组数与负荷必须与计划相同：${planned.item_key}`;
        continue;
      }
      // deload / legacy（timed）：只允许减组
      if (item.replacement_exercise_id != null)
        return `计时型条目不得携带替代动作身份：${planned.item_key}`;
      if (!sameDuration || !loadSame)
        return `安排目标不得改时长处方或负荷：${planned.item_key}`;
      if (!sameSets && item.prescription.work_sets > planned.prescription.work_sets)
        return `当次调整只能减组、不得增组：${planned.item_key}`;
      if (!sameSets) hasDiff = true;
      if (d === "deload") {
        if (sameSets)
          return `减载必须真的减少组数（计时型只允许减组）：${planned.item_key}`;
        hasDiff = true;
      }
    }
  }
  if (hasDiff) {
    const reason = t.adjustment_reason?.trim();
    if (!reason)
      return "当次目标与计划不同却没有 adjustment_reason：普通调整必须说明原因";
  }
  return undefined;
}

/** 安排草稿 diff：相对绑定计划的差异行（服务端派生；目标用力用大白话） */
function arrangementDiff(
  state: MockState,
  p: ArrangementDraftPayload,
): FieldDiff[] {
  const t = p.target;
  const rows: FieldDiff[] = [
    {
      field: "当次安排 · 训练日",
      new_value: `${t.scheduled_on} · ${t.plan_workout_key}（计划 ${t.plan_version}）`,
    },
  ];
  const plan = state.plan;
  const workout =
    plan && plan.version === t.plan_version
      ? findWorkout(plan.payload, t.plan_workout_key)
      : undefined;
  if (workout) {
    for (let i = 0; i < workout.exercises.length; i++) {
      const planned = workout.exercises[i];
      const item = t.exercises[i];
      if (!planned || !item) continue;
      if (item.disposition && planned.disposition !== item.disposition)
        rows.push({
          field: `${item.display_snapshot.name} · 处置`,
          new_value: DISPOSITION_LABEL[item.disposition],
        });
      if (
        item.prescription.kind === "reps" &&
        planned.prescription.kind === "reps"
      ) {
        if (item.prescription.work_sets !== planned.prescription.work_sets)
          rows.push({
            field: `${item.display_snapshot.name} · 组数`,
            old_value: `${planned.prescription.work_sets} 组`,
            new_value: `${item.prescription.work_sets} 组`,
          });
        if (
          JSON.stringify(item.prescription.reps_range) !==
          JSON.stringify(planned.prescription.reps_range)
        )
          rows.push({
            field: `${item.display_snapshot.name} · 每组次数`,
            old_value: deriveRangeLabel(planned.prescription.reps_range),
            new_value: deriveRangeLabel(item.prescription.reps_range),
          });
        if (
          JSON.stringify(item.prescription.target_rir) !==
          JSON.stringify(planned.prescription.target_rir)
        )
          rows.push({
            field: `${item.display_snapshot.name} · 目标用力`,
            old_value: effortPlainLabel(planned.prescription.target_rir),
            new_value: effortPlainLabel(item.prescription.target_rir),
          });
      }
      if (JSON.stringify(item.load ?? null) !== JSON.stringify(planned.load ?? null))
        rows.push({
          field: `${item.display_snapshot.name} · 负荷`,
          old_value:
            planned.load?.kind === "verified"
              ? `${planned.load.value}${planned.load.unit}`
              : "—",
          new_value:
            item.load?.kind === "verified"
              ? `${item.load.value}${item.load.unit}`
              : "—",
        });
      if (
        item.replacement_exercise_id &&
        item.replacement_exercise_id !== planned.exercise_id
      )
        rows.push({
          field: `${item.display_snapshot.name} · 替换为`,
          new_value: item.replacement_exercise_id,
        });
    }
  }
  if (t.adjustment_reason)
    rows.push({ field: "调整原因", new_value: t.adjustment_reason });
  return rows;
}

/** 当次安排触发短语（stage3 §7 第 2–5 步）：含状态档位关键词，便于档位分流入口 */
const ARRANGEMENT_PATTERN =
  /安排|减组|状态不好|今天轻一点|少做几组|状态不佳|状态差|状态一般|状态正常|非常差|不想练|不太想练|只改今天|升目标|更保守|局部跳过/;
/** 明确指向长期计划（本阶段不生成 plan 修订，先澄清） */
const LONG_TERM_SCOPE = /长期|以后|从下周|往后|未来/;
/** 明确只改当次（今天/这次） */
const TODAY_SCOPE = /今天|只改今天|就今天|这次|当次|本次/;
/** 明显状态差：本阶段仅建议休息文案、不生成任何结构化草稿 */
const SEVERE_STATE = /非常差|很差|极度|不想练|完全不想|动不了|太累.*不想|状态非常|彻底不想/;
/** 正常：按原计划回复、无草稿 */
const NORMAL_STATE = /状态正常|今天正常|状态不错|状态很好|状态还行|状态没问题/;
/** 一般状态差：出 deload / keep 安排草稿（演示主路径） */
const MILD_STATE =
  /状态(一般|有点|不太好|不好|不佳|差)|有点累|轻一点|少做几组|减组|没力气|疲劳|状态不佳/;

/**
 * 当次安排剧本（stage3 F3-02）：
 * - 先澄清「只改今天 / 是否动长期计划」——未说改长期时只出当次安排草稿；
 *   明确要改长期时本阶段不生成 plan 修订，先澄清；
 * - 状态档位：正常 → 原计划回复无草稿；一般状态差 → deload/keep 草稿；
 *   明显状态差 → 仅建议休息文案、无结构化草稿；
 * - 目标日 = 今日 MOCK_TODAY（日程按日期规则锁定 ≠ 处方锁定，仍可安排）。
 */
function arrangementScriptReply(
  state: MockState,
  message: string,
): {
  text: string;
  draft?: Draft;
} {
  const plan = state.plan;
  if (!plan || !state.profile)
    return { text: "当前没有可用的正式计划与档案，无法生成当次安排调整。" };

  const longTerm = LONG_TERM_SCOPE.test(message);
  const todayScope = TODAY_SCOPE.test(message);
  // 明确要改长期计划：本阶段不生成 plan 修订，先澄清（08：先问是否改长期）
  if (longTerm && !/只改今天|就今天|这次|当次|本次/.test(message))
    return {
      text: [
        "你提到了长期计划调整。",
        "",
        "本阶段只处理**当次安排**（只影响某一次训练，不修改长期计划与其余日程）。",
        "长期修订需要单独确认。请先澄清：",
        "- 只改今天这一练（生成当次安排草稿）；还是",
        "- 需要动长期计划（本阶段先不生成计划修订草稿）。",
      ].join("\n"),
    };
  // 状态档位：明显状态差 → 仅建议休息、无草稿
  if (SEVERE_STATE.test(message))
    return {
      text: [
        "今天状态非常差，建议休息，不生成训练安排。",
        "",
        "恢复后再按原计划训练；若长期状态持续低迷，可再讨论是否调整长期计划。",
      ].join("\n"),
    };
  // 状态档位：正常 → 按原计划回复、无草稿
  if (NORMAL_STATE.test(message) && !MILD_STATE.test(message))
    return {
      text: [
        "今天状态正常，按原计划执行，无需当次安排调整。",
        "",
        `今日应训练：${planVersionLabel(state)}`,
      ].join("\n"),
    };

  // 目标日：今日（日期规则锁定 ≠ 处方锁定）
  const upcoming =
    state.schedules.find(
      (s) =>
        s.plan_version === plan.version &&
        s.date === MOCK_TODAY &&
        s.stored_status !== "cancelled",
    ) ??
    // 兜底：无今日日程时取下一未锁定日（仍可演示，但默认路径走今日）
    state.schedules
      .filter(
        (s) =>
          s.plan_version === plan.version &&
          !s.locked_effective &&
          s.stored_status !== "cancelled",
      )
      .sort((a, b) => a.date.localeCompare(b.date))[0];
  if (!upcoming)
    return { text: "当前计划没有可安排的应训练日，无法生成当次安排调整。" };
  const workout = findWorkout(plan.payload, upcoming.plan_workout_key);
  if (!workout) return { text: "目标应训练日不在当前计划内。" };

  // 未明确「只改今天」且也未明确长期：先澄清范围，不出草稿
  if (!todayScope && !MILD_STATE.test(message)) {
    return {
      text: [
        "请先澄清：这次是**只改今天**这一练，还是也要动长期计划？",
        "",
        "只改今天 → 生成当次安排草稿（不修改长期计划）。",
        "动长期计划 → 本阶段先不生成计划修订草稿。",
      ].join("\n"),
    };
  }

  // 演示主路径：一般状态差 → 按消息偏好生成 deload（只减组）或 keep（只升目标用力）
  const preferKeep = /只升|保守|不减组|不减次数|目标用力|升目标/.test(message);
  const exercises = workout.exercises.map((e) => {
    if (e.prescription.kind !== "reps") return e;
    if (preferKeep) {
      const rir = e.prescription.target_rir;
      const nextRir = rir
        ? { min: Math.min(rir.min + 1, rir.max + 1), max: rir.max + 1 }
        : undefined;
      return {
        ...e,
        disposition: "keep" as const,
        prescription: {
          ...e.prescription,
          ...(nextRir ? { target_rir: nextRir } : {}),
        },
      };
    }
    // deload 方案 1：各动作减 1 组（不改次数与目标用力）
    return {
      ...e,
      disposition: "deload" as const,
      prescription: {
        ...e.prescription,
        work_sets: Math.max(1, e.prescription.work_sets - 1),
      },
    };
  });
  const target: ArrangementTarget = {
    schema_version: 1,
    scheduled_session_id: upcoming.id,
    plan_version: plan.version,
    plan_workout_key: workout.workout_key,
    scheduled_on: upcoming.date,
    exercises,
    adjustment_reason: preferKeep
      ? "当日状态一般：各动作目标用力上调一档（更保守），组次不变"
      : "当日状态一般：各动作减 1 组（方案 1），其余目标保持计划",
  };
  const payload: ArrangementDraftPayload = { target };
  const invalid = arrangementPayloadError(state, payload);
  if (invalid) return { text: `当次安排草稿未通过校验：${invalid}` };
  const draft: Draft = {
    id: nextId("draft"),
    kind: "arrangement",
    status: "pending",
    revision: 1,
    base_business_version: 0,
    payload,
    diff: arrangementDiff(state, payload),
  };
  const lockNote =
    upcoming.locked_effective
      ? "（今日日程已按日期规则锁定，但处方仍可当次调整——日程锁定 ≠ 处方锁定）"
      : "";
  return {
    text: [
      `已按「${upcoming.date} 状态一般」生成**当次安排草稿**（只影响这一练，不修改长期计划）${lockNote}：`,
      "",
      `- 绑定：${upcoming.date} · ${workout.name} · 计划 ${plan.version}`,
      preferKeep
        ? "- 调整：保留原组次，目标用力只升不降（更保守）"
        : "- 调整：方案 1 减组（组数只减不增），次数与目标用力保持计划",
      `- 原因：${target.adjustment_reason}`,
      "",
      "确认后写入当次安排修订（arrangement_revisions）；长期计划与其余日程不变。",
    ].join("\n"),
    draft,
  };
}

/** 当日应训练文案（正常档无草稿时的原计划回复） */
function planVersionLabel(state: MockState): string {
  const plan = state.plan;
  if (!plan) return "—";
  const sched = state.schedules.find(
    (s) => s.plan_version === plan.version && s.date === MOCK_TODAY,
  );
  const workout = sched
    ? findWorkout(plan.payload, sched.plan_workout_key)
    : undefined;
  if (!workout) return `${MOCK_TODAY}（当前计划无安排）`;
  return `${MOCK_TODAY} · ${workout.name} · 计划 ${plan.version}`;
}

/* ---------------------- 建档对话剧本（stage1 F1-02） -----------------------
 *
 * plans/stage1.md F1-02：从空种子开始的多轮建档剧本。确定性解析只覆盖阶段 1 演示
 * 短语（目标/经验/频率/时长/器械/体重/动作限制/当前身体状态），不是通用 NLP；
 * 未收集的事实保持未知，不写默认值（PRD §5.2 六类事实，缺失继续追问）。
 * ------------------------------------------------------------------------- */

/** 建档事实键（顺序 = 追问顺序） */
type OnboardingFact =
  | "goal"
  | "experience"
  | "weekly_frequency"
  | "session_minutes"
  | "equipment"
  | "body_weight_kg"
  | "restrictions"
  | "body_conditions";

/** 建档事实顺序（八项）；动作限制不单独追问：结论由身体情况一次推出（2026-09-10 拍板） */
const ONBOARDING_FACTS: readonly OnboardingFact[] = [
  "goal",
  "experience",
  "weekly_frequency",
  "session_minutes",
  "equipment",
  "body_weight_kg",
  "restrictions",
  "body_conditions",
];

/** 可单独追问的事实；`restrictions` 无独立追问文案（结论随 `body_conditions` 一次给出） */
type AskedFact = Exclude<OnboardingFact, "restrictions">;

const FACT_QUESTIONS: Record<AskedFact, string> = {
  goal: "你的训练目标是什么？（增肌 / 力量 / 整体健康）",
  experience:
    "你的训练经验大概到什么程度？（零基础 / 有一点基础 / 稳定训练过一段时间）",
  weekly_frequency: "每周计划训练几次？",
  session_minutes: "每次训练大约能安排多少分钟？",
  equipment:
    "可用器械有哪些？（如杠铃、哑铃、卧推架、引体架、绳索；没有器械也请直接说明）",
  body_weight_kg: "当前体重是多少公斤？（建档必填，我不会替你填默认值）",
  body_conditions:
    "当前身体情况如何？一次说明就好：有没有哪里不适、是否影响某个动作（如「深蹲时膝盖锐痛」）、是否出现胸部异常不适、晕厥、异常气短、锐痛、麻木、放射痛这类需要线下专业评估的情况；都没有也请明确说「没有」。",
};

/** 打卡类请求（阶段 1 仅识别这些演示短语，不解析自然语言意图；F3-04 增同日补充歧义短语） */
const CHECKIN_PATTERN =
  /打卡|训练记录|记录一下|记一下|帮我记|今天练|昨天练|今天做|昨天做|再补一组|补一组|补充上一练|新增一练|\d+\s*(?:kg|公斤)\D{0,6}\d+\s*组|\d+\s*组\s*[x×]?\s*\d+\s*次/;

/** 用户明确否认的表达（仅用于把「没有」解释为对上一问的明确否认） */
const DENIAL_PATTERN = /没有|没|无|不用|不需要|一切正常|都正常|没问题/;

/** 症状标记（识别身体情况报告的原文子句；不做分类，不判定安全） */
const SYMPTOM_MARKER =
  /不适|不舒服|疼痛|疼|痛|发麻|麻木|发酸|酸胀|发紧|头晕|晕|气短|胸闷|受伤|肿/;

/** 已知限制对象（固定提议剧本的短语表）：只用于模拟「限制提议」，不在写入阶段做医学分类 */
const RESTRICTION_SUBJECTS: readonly {
  pattern: RegExp;
  name: string;
  scope: Restriction["scope"];
}[] = [
  { pattern: /颈后推举/, name: "杠铃颈后推举", scope: "specific_action" },
  { pattern: /卧推/, name: "杠铃平板卧推", scope: "specific_action" },
  { pattern: /肩推/, name: "坐姿哑铃肩推", scope: "specific_action" },
  { pattern: /引体向上/, name: "自重引体向上", scope: "specific_action" },
  { pattern: /双杠臂屈伸/, name: "自重双杠臂屈伸", scope: "specific_action" },
  { pattern: /划船/, name: "杠铃俯身划船", scope: "specific_action" },
  { pattern: /弯举/, name: "哑铃弯举", scope: "specific_action" },
  { pattern: /深蹲/, name: "深蹲", scope: "movement_pattern" },
  { pattern: /硬拉/, name: "硬拉", scope: "movement_pattern" },
  { pattern: /髋铰链/, name: "髋铰链", scope: "movement_pattern" },
  { pattern: /水平推/, name: "水平推", scope: "movement_pattern" },
  { pattern: /水平拉/, name: "水平拉", scope: "movement_pattern" },
  { pattern: /垂直推/, name: "垂直推", scope: "movement_pattern" },
  { pattern: /垂直拉/, name: "垂直拉", scope: "movement_pattern" },
];

const RESTRICTION_MARKERS = /不适|不舒服|疼|痛|受伤|受限|不能|避免|做不了/;

/** 子句分隔符：限制标记只与同一子句内的限制对象绑定 */
const CLAUSE_BREAK = /[，。；;,、\n！!？?：]/;

/** 器械关键词（阶段 1 演示短语表） */
const EQUIPMENT_NAMES: readonly string[] = [
  "杠铃",
  "哑铃凳",
  "哑铃",
  "卧推架",
  "引体架",
  "单杠",
  "绳索",
  "龙门架",
  "固定器械",
  "器械",
  "壶铃",
  "弹力带",
  "自重",
  "徒手",
];

interface ParsedFacts {
  goal?: string;
  experience?: string;
  weekly_frequency?: number;
  session_minutes?: number;
  equipment?: string[];
  /** 用户明确说明无器械（显式空值，区别于未知） */
  equipment_none: boolean;
  body_weight_kg?: number;
  restrictions: Restriction[];
  /** 用户明确说明无动作限制 */
  restrictions_none: boolean;
  /** 用户报告的身体情况原文子句（只存原文，分类在读取时进行） */
  body_conditions: string[];
  /** 用户明确说明无身体情况（不入档的否定不算报告） */
  physical_none: boolean;
  /** 本条消息是否提供了任何建档事实（含明确否认） */
  touched: boolean;
}

function onboardingOf(state: MockState, sessionId: string): OnboardingState {
  let ob = state.onboarding.get(sessionId);
  if (!ob) {
    ob = { pending_symptoms: [] };
    state.onboarding.set(sessionId, ob);
  }
  return ob;
}

/**
 * 身体情况报告原文：逐子句保存原文（如「深蹲时膝盖锐痛」整句入档，不只存「锐痛」）。
 * 不做医学分类，也不在写入阶段判定红旗；命中与未命中清单的区分放到读取时（classifyBodyConditions）。
 * 否认子句（「没有其他不适」）不算报告。
 */
function reportedBodyConditions(message: string): string[] {
  return message
    .split(/[，。；;,、\n！!？?]/)
    .map((c) => c.trim())
    .filter(
      (c) => c !== "" && SYMPTOM_MARKER.test(c) && !DENIAL_PATTERN.test(c),
    );
}

/** 器械词在原文中的下标区间（用于排除嵌在器械词里的动作名，如「卧推架」中的「卧推」） */
function equipmentSpans(message: string): { start: number; end: number }[] {
  const re = new RegExp(EQUIPMENT_NAMES.join("|"), "g");
  return [...message.matchAll(re)].map((m) => ({
    start: m.index,
    end: m.index + m[0].length,
  }));
}

/** index 所在的标点子句原文 */
function clauseOf(message: string, index: number): string {
  let start = 0;
  for (let i = 0; i < index; i++) {
    if (CLAUSE_BREAK.test(message[i])) start = i + 1;
  }
  for (let end = index; end < message.length; end++) {
    if (CLAUSE_BREAK.test(message[end])) return message.slice(start, end);
  }
  return message.slice(start);
}

/**
 * 限制提议（固定剧本，不做通用医学推断器）：已知限制对象 + 同一子句内不适／受限标记。
 * 提议只进草稿（用户确认前不写正式限制）；理由链由草稿卡按「身体情况 → 提议限制」派生展示。
 */
function parseRestrictions(message: string): Restriction[] {
  const out: Restriction[] = [];
  const equipment = equipmentSpans(message);
  for (const { pattern, name, scope } of RESTRICTION_SUBJECTS) {
    for (const m of message.matchAll(new RegExp(pattern.source, "g"))) {
      const end = m.index + m[0].length;
      // 嵌在器械词内的动作名不是限制对象（「卧推架」不产生「杠铃平板卧推」限制）
      if (equipment.some((s) => m.index < s.end && end > s.start)) continue;
      if (!RESTRICTION_MARKERS.test(clauseOf(message, m.index))) continue;
      out.push({ name, scope, note: message.trim() });
      break;
    }
  }
  return out;
}

function parseFacts(message: string): ParsedFacts {
  const text = message.trim();
  const out: ParsedFacts = {
    equipment_none: false,
    restrictions: [],
    restrictions_none: false,
    body_conditions: [],
    physical_none: false,
    touched: false,
  };

  const goal = /增肌|肌肥大/.test(text)
    ? "增肌（肌肥大）"
    : /力量/.test(text)
      ? "力量"
      : /健康|体能/.test(text)
        ? "整体健康"
        : undefined;
  if (goal) {
    out.goal = goal;
    out.touched = true;
  }

  const experience =
    /零基础|没有练过|没练过|没有经验|没有基础|没练|新手|从没练/.test(text)
      ? "零基础"
      : /初级|一点基础|有点基础|刚入门|入门|不到一年|半年/.test(text)
        ? "初级（有少量训练经验）"
        : /中级|一年多|两年|三年|稳定训练/.test(text)
          ? "中级"
          : undefined;
  if (experience) {
    out.experience = experience;
    out.touched = true;
  }

  const freq =
    text.match(/(?:每周|一周|周)[^\d]{0,4}(\d+)\s*次/) ??
    text.match(/(\d+)\s*次\s*\/?\s*(?:每周|周)/);
  if (freq) {
    out.weekly_frequency = Number(freq[1]);
    out.touched = true;
  }

  const durMin =
    text.match(/(?:每次|一次|单次)[^\d]{0,4}(\d+)\s*分钟/) ??
    text.match(/(\d+)\s*分钟/);
  const durHour = text.match(/(\d+(?:\.\d+)?)\s*(?:小时|钟头)/);
  if (durMin) {
    out.session_minutes = Number(durMin[1]);
    out.touched = true;
  } else if (durHour) {
    out.session_minutes = Math.round(Number(durHour[1]) * 60);
    out.touched = true;
  }

  // 体重：优先取带「体重」标记的表述；无标记时仅在非打卡类语句里取 kg 数值
  const weightMarked = text.match(/体重[^\d]{0,6}(\d+(?:\.\d+)?)/);
  const weightLoose = /[组次]/.test(text)
    ? null
    : text.match(/(\d+(?:\.\d+)?)\s*(?:kg|公斤|千克)/i);
  const weight = weightMarked ?? weightLoose;
  if (weight) {
    out.body_weight_kg = Number(weight[1]);
    out.touched = true;
  }

  // 明确「无器械」优先：先剔除否定短语内的器械词，避免把「没有器械」当成可用器械
  const noneEquipment = /(?:没有|没|无)[^，。；;]{0,4}器械|自重|徒手/.exec(
    text,
  );
  const eqMatched = EQUIPMENT_NAMES.filter((n) => text.includes(n)).filter(
    (n) => {
      if (!noneEquipment) return true;
      const at = text.indexOf(n);
      return !(
        at >= noneEquipment.index &&
        at < noneEquipment.index + noneEquipment[0].length
      );
    },
  );
  const eqList = eqMatched.filter(
    (n) =>
      n !== "自重" &&
      n !== "徒手" &&
      !eqMatched.some((m) => m !== n && m.includes(n)),
  );
  if (eqList.length > 0) {
    out.equipment = eqList;
    out.touched = true;
  } else if (noneEquipment) {
    out.equipment_none = true;
    out.touched = true;
  }

  // 身体情况：只保存用户报告原文（逐子句；不做分类，分类在读取时进行）
  out.body_conditions = reportedBodyConditions(text);
  if (out.body_conditions.length > 0) out.touched = true;
  // 限制提议：基于同一批报告原文（固定剧本），进草稿而非正式限制
  out.restrictions =
    out.body_conditions.length > 0 ? parseRestrictions(text) : [];
  if (out.restrictions.length > 0) out.touched = true;
  if (
    /没有(?:任何)?(?:动作)?限制|无(?:动作)?限制|没有(?:动作)?受限|都能练|都可以练|没有不能做/.test(
      text,
    )
  ) {
    out.restrictions_none = true;
    out.touched = true;
  }

  if (
    out.body_conditions.length === 0 &&
    /没有(?:这些|其他)?(?:情况|问题|症状|不适|异常)|无不适|无异常|一切正常|都正常|没有问题/.test(
      text,
    )
  ) {
    out.physical_none = true;
    out.touched = true;
  }

  return out;
}

function professionalEvalBlock(flags: string[]): string {
  return [
    `> ⚠️ 你报告的「${flags.join("、")}」属于需要专业评估的情况：建议尽快线下就医并由专业人员评估。`,
    "> 我不会在此基础上给出任何训练建议；该描述已按原文记入身体情况，分类在读取时进行。Agent 不诊断，也不解除这类阻断。",
  ].join("\n");
}

function unlistedSymptomBlock(symptoms: string[]): string {
  return [
    `你提到的「${symptoms.join("；")}」不在我能判定的安全症状清单内（本阶段只识别正本明确列出的症状），因此我不会判断它是否安全，也不会把它写成「无」。`,
    "请补充：具体部位、在什么动作或场景下出现、持续多久、是否影响日常；如持续或加重，建议线下专业评估。",
  ].join("\n");
}

function missingFacts(ob: OnboardingState): AskedFact[] {
  return ONBOARDING_FACTS.filter((f): f is AskedFact => {
    // 不单独追问动作限制：结论随身体情况一次推出（2026-09-10 拍板）
    if (f === "restrictions") return false;
    switch (f) {
      case "goal":
        return ob.goal === undefined;
      case "experience":
        return ob.experience === undefined;
      case "weekly_frequency":
        return ob.weekly_frequency === undefined;
      case "session_minutes":
        return ob.session_minutes === undefined;
      case "equipment":
        return ob.equipment === undefined;
      case "body_weight_kg":
        return ob.body_weight_kg === undefined;
      case "body_conditions":
        return ob.body_conditions === undefined;
    }
  });
}

/** 回答上一问时的裸否定（如「没有」）：只对可表达为「无」的事实生效 */
function applyDenial(
  ob: OnboardingState,
  fact: OnboardingFact,
  ack: string[],
): void {
  if (fact === "equipment" && ob.equipment === undefined) {
    ob.equipment = [];
    ack.push("可用器械：无（用户明确说明）");
    return;
  }
  if (
    fact === "body_conditions" &&
    (ob.body_conditions === undefined || ob.body_conditions.length === 0)
  ) {
    ob.body_conditions = [];
    ob.pending_symptoms = [];
    /* 身体情况结论 = 无 → 动作限制结论同次给出（不单独追问） */
    ob.restrictions = ob.restrictions ?? [];
    ack.push("当前身体情况：无（用户确认）");
    return;
  }
  if (fact === "experience" && ob.experience === undefined) {
    ob.experience = "零基础";
    ack.push("训练经验：零基础（用户确认）");
  }
}

function applyFacts(
  ob: OnboardingState,
  parsed: ParsedFacts,
  message: string,
  ack: string[],
): void {
  if (parsed.goal !== undefined && parsed.goal !== ob.goal) {
    ob.goal = parsed.goal;
    ack.push(`训练目标：${parsed.goal}`);
  }
  if (parsed.experience !== undefined && parsed.experience !== ob.experience) {
    ob.experience = parsed.experience;
    ack.push(`训练经验：${parsed.experience}`);
  }
  if (
    parsed.weekly_frequency !== undefined &&
    parsed.weekly_frequency !== ob.weekly_frequency
  ) {
    ob.weekly_frequency = parsed.weekly_frequency;
    ack.push(`每周频率：${parsed.weekly_frequency} 次`);
  }
  if (
    parsed.session_minutes !== undefined &&
    parsed.session_minutes !== ob.session_minutes
  ) {
    ob.session_minutes = parsed.session_minutes;
    ack.push(`单次时长：${parsed.session_minutes} 分钟`);
  }
  if (
    parsed.equipment !== undefined &&
    JSON.stringify(parsed.equipment) !== JSON.stringify(ob.equipment)
  ) {
    ob.equipment = parsed.equipment;
    ack.push(`可用器械：${parsed.equipment.join("、")}`);
  }
  if (
    parsed.body_weight_kg !== undefined &&
    parsed.body_weight_kg !== ob.body_weight_kg
  ) {
    ob.body_weight_kg = parsed.body_weight_kg;
    ack.push(`体重：${parsed.body_weight_kg} kg`);
  }
  if (parsed.restrictions.length > 0) {
    const merged = [...(ob.restrictions ?? [])];
    for (const r of parsed.restrictions)
      if (!merged.some((m) => m.name === r.name)) merged.push(r);
    ob.restrictions = merged;
    ack.push(`动作限制：${restrictionLabel(merged)}`);
  }
  // 未知不等于无：只有用户明确否认才写显式空值（PRD §5.2）
  if (parsed.equipment_none && ob.equipment?.length !== 0) {
    ob.equipment = [];
    ack.push("可用器械：无（用户明确说明）");
  }
  if (parsed.restrictions_none && ob.restrictions?.length !== 0) {
    ob.restrictions = [];
    ack.push("动作限制：无（用户明确说明）");
  }
  // 身体情况：只保存用户报告原文（逐条去重合并，不做分类）
  if (parsed.body_conditions.length > 0) {
    ob.body_conditions = [
      ...new Set([...(ob.body_conditions ?? []), ...parsed.body_conditions]),
    ];
    ack.push(`当前身体情况：${ob.body_conditions.join("；")}`);
    /* 动作限制结论随身体情况一次推出：本次提议的限制进草稿，用户确认前不写正式限制 */
    ob.restrictions = ob.restrictions ?? [];
  }
  // 未知不等于无：只有用户明确否认（且本条未报告任何身体情况）才写显式空值（PRD §5.2）
  if (
    parsed.physical_none &&
    parsed.body_conditions.length === 0 &&
    (ob.body_conditions === undefined || ob.body_conditions.length === 0)
  ) {
    ob.body_conditions = [];
    ob.pending_symptoms = [];
    ob.restrictions = ob.restrictions ?? [];
    ack.push("当前身体情况：无（用户确认）");
  }
  if (DENIAL_PATTERN.test(message) && !parsed.touched && ob.last_asked)
    applyDenial(ob, ob.last_asked, ack);
}

function profileDraftPayload(
  ob: OnboardingState,
): ProfileDraftPayload | undefined {
  if (
    ob.goal === undefined ||
    ob.experience === undefined ||
    ob.weekly_frequency === undefined ||
    ob.session_minutes === undefined ||
    ob.equipment === undefined ||
    ob.body_weight_kg === undefined ||
    ob.restrictions === undefined ||
    ob.body_conditions === undefined
  )
    return undefined;
  return {
    profile: {
      goal: ob.goal,
      experience: ob.experience,
      weekly_frequency: ob.weekly_frequency,
      session_minutes: ob.session_minutes,
      equipment: ob.equipment,
      body_weight_kg: ob.body_weight_kg,
      body_conditions: ob.body_conditions,
    },
    restrictions: ob.restrictions,
  };
}

/**
 * 档案草稿字段级 Diff（A4：由服务端对比新旧档案派生「旧值→新值」，不信任客户端提交值）。
 * 新值口径与草稿卡一致（src/lib/profile.ts profileFieldRows）；未建档（old = null）时
 * 全部为新增，渲染层显示「新增」徽章。
 */
function profileDraftDiff(
  oldProfile: Profile | null,
  oldRestrictions: Restriction[],
  payload: ProfileDraftPayload,
): FieldDiff[] {
  // 未建档（old = null）：全部为新增，渲染层按缺省 old_value 显示「新增」徽章
  if (oldProfile === null)
    return profileFieldRows(payload).map(({ field, value }) => ({
      field,
      new_value: value,
    }));
  // 已建档：与草稿卡实时 Diff 同一口径（profilePayloadDiff）对比新旧档案，
  // 只列出真正变化的字段，避免把未变化字段渲染成伪变更
  return profilePayloadDiff(
    { profile: oldProfile, restrictions: oldRestrictions },
    payload,
  );
}

/**
 * 档案载荷的确定性校验（纠错与确认提交共用；只认服务端可校验的结构，不信任客户端值）。
 * 返回 undefined = 载荷有效；否则返回具体缺项/非法项说明。完整档案须含六类事实且
 * 体重必填（plans/stage1.md §8 已拍）；字段缺省一律拒绝，不当作「无」（未知 ≠ 显式 none）。
 */
function profilePayloadError(payload: unknown): string | undefined {
  if (
    typeof payload !== "object" ||
    payload === null ||
    !("profile" in payload)
  )
    return "缺少 profile 字段";
  const profile = (payload as { profile?: unknown }).profile;
  if (typeof profile !== "object" || profile === null)
    return "profile 字段无效";
  const prof = profile as Record<string, unknown>;
  const missingText = (key: string, label: string): string | undefined => {
    const v = prof[key];
    return typeof v === "string" && v.trim() !== ""
      ? undefined
      : `缺少${label}`;
  };
  const invalidNumber = (key: string, label: string): string | undefined => {
    const v = prof[key];
    return typeof v === "number" && Number.isFinite(v) && v > 0
      ? undefined
      : `${label}须为正数`;
  };
  const invalid =
    missingText("goal", "训练目标") ??
    missingText("experience", "训练经验") ??
    invalidNumber("weekly_frequency", "每周频率") ??
    invalidNumber("session_minutes", "单次时长") ??
    invalidNumber("body_weight_kg", "体重");
  if (invalid) return invalid;
  const equipment = prof.equipment;
  if (
    !Array.isArray(equipment) ||
    equipment.some((e) => typeof e !== "string" || e.trim() === "")
  )
    return "可用器械须为非空字符串列表（无器械请用空列表）";
  // 身体情况只存用户报告原文（逐条）；空列表 = 用户明确表示无，缺省则是尚未收集
  const conditions = prof.body_conditions;
  if (
    !Array.isArray(conditions) ||
    conditions.some((s) => typeof s !== "string" || s.trim() === "")
  )
    return "当前身体情况须为非空字符串列表（无身体情况请用空列表）";
  const restrictions = (payload as { restrictions?: unknown }).restrictions;
  // 完整档案草稿必须携带当前有效限制列表：缺省 = 尚未收集，不得在确认时写成「无限制」
  if (restrictions === undefined)
    return "缺少动作限制（无限制请用空列表明确表达）";
  if (!Array.isArray(restrictions)) return "动作限制须为列表";
  for (const r of restrictions) {
    if (typeof r !== "object" || r === null) return "动作限制条目无效";
    const item = r as Record<string, unknown>;
    if (typeof item.name !== "string" || item.name.trim() === "")
      return "动作限制缺少名称";
    if (item.scope !== "specific_action" && item.scope !== "movement_pattern")
      return "动作限制粒度须为 specific_action 或 movement_pattern";
    if (item.note !== undefined && typeof item.note !== "string")
      return "动作限制说明须为字符串";
  }
  return undefined;
}

/** 按会话最新收集事实重建档案载荷（重算用；拿不到时由调用方保留旧草稿内容） */
function latestProfilePayload(
  state: MockState,
  draftId: string,
): ProfileDraftPayload | undefined {
  const sessionId = state.draft_sessions.get(draftId);
  if (!sessionId) return undefined;
  const ob = state.onboarding.get(sessionId);
  if (!ob) return undefined;
  return profileDraftPayload(ob);
}

/** 本会话是否已有待确认的档案草稿（避免同一基线并存多份竞争草稿） */
function pendingProfileDraft(
  state: MockState,
  sessionId: string,
): Draft | undefined {
  for (const [draftId, sid] of state.draft_sessions) {
    if (sid !== sessionId) continue;
    const d = state.drafts.get(draftId);
    if (d && d.kind === "profile_update" && d.status === "pending") return d;
  }
  return undefined;
}

function buildProfileDraft(
  state: MockState,
  payload: ProfileDraftPayload,
): Draft {
  return {
    id: nextId("draft"),
    kind: "profile_update",
    status: "pending",
    revision: 1,
    base_business_version: 0, // 占位；由 runScript 在生成处按当时读到的 context_version 填充（01 1.3）
    payload,
    diff: profileDraftDiff(state.profile, state.restrictions, payload),
  };
}

function composeOnboardingReply(
  ack: string[],
  blocks: string[],
  tail: string,
): string {
  const parts: string[] = [];
  if (ack.length > 0) parts.push(`已记录：${ack.join("；")}。`);
  parts.push(...blocks);
  parts.push(tail);
  return parts.join("\n\n");
}

/**
 * 一轮建档回复：记录本轮事实 → 安全提示／需澄清分支 → 追问下一项缺失事实或出稿。
 * 缺失事实继续追问、不编造；事实齐备时只生成一份 profile_update 草稿（结构化载荷 +
 * 服务端派生 Diff），同一基线不并存竞争草稿（plans/stage1.md F1-02）。
 */
function onboardingTurn(
  state: MockState,
  sessionId: string,
  message: string,
  parsed: ParsedFacts,
  safetyHits: string[],
): { text: string; draft?: Draft } {
  const ob = onboardingOf(state, sessionId);
  const ack: string[] = [];
  const blocks: string[] = [];

  if (safetyHits.length > 0) {
    ack.push(`当前身体情况 · 需专业评估：${safetyHits.join("、")}`);
    blocks.push(professionalEvalBlock(safetyHits));
  }

  applyFacts(ob, parsed, message, ack);

  // 未命中六类清单的报告只做澄清，不判定安全（读取时分类；原文照常入档，不写成「无」）
  ob.pending_symptoms = classifyBodyConditions(parsed.body_conditions).unlisted;
  if (ob.pending_symptoms.length > 0)
    blocks.push(unlistedSymptomBlock(ob.pending_symptoms));

  const missing = missingFacts(ob);
  if (missing.length > 0) {
    const next = missing[0];
    ob.last_asked = next;
    const tail =
      ack.length > 0 || blocks.length > 0
        ? `接下来：${FACT_QUESTIONS[next]}`
        : `我没能从这句话里提取到建档信息。请补充：${FACT_QUESTIONS[next]}`;
    return { text: composeOnboardingReply(ack, blocks, tail) };
  }

  const pending = pendingProfileDraft(state, sessionId);
  if (pending) {
    const tail =
      pending.base_business_version === state.context_version
        ? "档案草稿已生成且仍待确认：请在草稿卡中核对或纠错后确认；确认前正式档案不变。"
        : "已有档案草稿因业务数据变更而过期：请在草稿卡中按最新数据一键重算后再确认。";
    return { text: composeOnboardingReply(ack, blocks, tail) };
  }

  const payload = profileDraftPayload(ob);
  if (!payload)
    return {
      text: composeOnboardingReply(
        ack,
        blocks,
        `请继续补充：${FACT_QUESTIONS[missingFacts(ob)[0] ?? "goal"]}`,
      ),
    };

  const snapshot = JSON.stringify(payload);
  if (ob.draft_id !== undefined && ob.drafted_snapshot === snapshot)
    return {
      text: composeOnboardingReply(
        ack,
        blocks,
        "档案内容与上一份草稿一致，未生成新草稿。",
      ),
    };

  const draft = buildProfileDraft(state, payload);
  ob.draft_id = draft.id;
  ob.drafted_snapshot = snapshot;
  ob.last_asked = undefined;
  return {
    text: composeOnboardingReply(
      ack,
      blocks,
      "已收集齐建档所需事实，生成 1 份档案草稿（结构化载荷；字段级 Diff 由服务端对比新旧档案派生）。请在草稿卡核对后确认；确认前正式档案不变。",
    ),
    draft,
  };
}

const REVIEW_REPLY = [
  "## 本阶段复盘（基于最新有效记录重算）",
  "",
  "- 完成率：W1 **2/3（66.7%）**，W2 截至今日 **1/3（33.3%）**；漏练保留，不做补课。",
  "- 组级判定（按当次安排）：符合 1 组 / 未符合 1 组 / 待补全 1 组。",
  "- PR：杠铃平板卧推 **80kg x8**；自重引体不进重量 PR。",
  "",
  "| 计划周 | 应训练 | 已完成 | 完成率 |",
  "| --- | --- | --- | --- |",
  "| W1 | 3 | 2 | 66.7% |",
  "| W2 | 3 | 1 | 33.3% |",
  "",
  "> 建议下一步：今日腿日尚未完成；按已接受安排优先于原计划处方。",
].join("\n");

/**
 * 训练日指导意图（F2-05；04 4.5）：请求基于当前计划的训练指导。
 * 只读分支——不生成任何草稿，计划修改仍须经对话草稿确认；与「计划调整」区分开。
 */
const GUIDANCE_PATTERN = /指导|练什么|训练安排/;

/** 尚无正式计划时的指导口径：不凭空给处方，先经对话生成并确认计划 */
const NO_PLAN_GUIDANCE_REPLY = [
  "当前没有正式计划，我不会凭档案直接给出训练日指导。",
  "请在对话里说明训练目标与可用日期，由我提出计划草稿；确认启用后，这里给出基于当周应训练日的处方与校准说明。",
].join("\n");

/** 无正式档案时的统一口径（计划类回复；建档才收集档案事实，不凭空生成计划处方） */
const NO_PROFILE_PLAN_REPLY = [
  "还没有正式档案，我不会凭空生成计划处方。",
  "请在对话中补齐建档信息（目标、经验、每周频率、单次时长、可用器械、体重、动作限制与身体情况），确认后再生成计划。",
].join("\n\n");

/**
 * 当日临时器械限制（02 章：不得污染长期档案；plans/stage2.md §7 第 7 步）：只说明不写入，
 * 不生成任何草稿、不改正式档案、计划与日程。
 */
const TODAY_ONLY_EQUIPMENT_REPLY = [
  "「今天只能用哑铃」是当日临时情况：**不写入长期档案**，也不修改正式计划与日程。",
  "",
  "按次安排（临时改变器械或当次训练内容）属于后续阶段能力；本阶段不会把当日限制沉淀成长期器械范围。",
  "如果你说的是**以后**只能用哑铃，请照这个说法再说一次，我会给出「档案器械补丁 + 新计划 + 新日程」的组合草稿，确认后一次生效。",
].join("\n");

const GENERIC_REPLY = [
  "收到。当前处于 **mock 演示模式**，我可以：",
  "",
  "- 建档：直接给出目标、经验、每周频率、单次时长、可用器械、体重、动作限制与身体情况",
  "- 请求训练指导：如「给我周三的训练指导」（按最新身体情况与限制复核整份计划）",
  "- 调整计划：如「最近很累，帮我调整计划」",
  "- 当次安排：如「今天轻一点，减一组」",
  "- 训练打卡：如「今天卧推 80kg 4组 每组8次」",
  "- 生成复盘：如「给我看一下复盘」",
  "",
  "所有业务变更都会先以草稿卡展示，确认后才写入。",
].join("\n");

async function runScript(
  state: MockState,
  hub: SseHub,
  run: RunState,
  message: string,
): Promise<void> {
  const emit = (event: string, data: unknown) => hub.emit(event, data);

  // pending 阶段（08 8.1：pending → running）；短暂受理窗口后转 running 并发 run.started
  await sleep(300);
  if (run.cancelled) return finishCancelled(emit, run);
  run.status = "running";
  emit("run.started", { run_id: run.id });

  // 上下文压缩轻提示（B3：仅界面轻提示）；长会话触发一次（08 8.7 压缩两态）
  const history = state.messages.get(run.session_id) ?? [];
  if (history.length >= 5) {
    await sleep(300);
    if (run.cancelled) return finishCancelled(emit, run);
    emit("context.compacting", { run_id: run.id });
    await sleep(300);
    if (run.cancelled) return finishCancelled(emit, run);
    emit("context.compacted", {
      run_id: run.id,
      note: "已发生上下文压缩：核心事实（档案、计划、限制、待确认事项）已保留。",
    });
  }

  // 身体情况读取时分类：任何意图分支都不得吞掉需专业评估的报告（plans/stage1.md F1-02），
  // 但分类不写档案、不阻断普通身体情况
  const parsed = parseFacts(message);
  const safetyHits = classifyBodyConditions(parsed.body_conditions).confirmed;
  const isGuidance = GUIDANCE_PATTERN.test(message);
  // 「今天练什么」这类问句会同时命中打卡短语：指导意图优先
  const isCheckIn = !isGuidance && CHECKIN_PATTERN.test(message);
  const isArrangement =
    !isGuidance &&
    !isCheckIn &&
    state.profile !== null &&
    ARRANGEMENT_PATTERN.test(message);
  const isPlan = /计划|调整|哑铃/.test(message);
  const isReview = /复盘/.test(message);
  // 器械范围短语（plans/stage2.md §7 第 7 步，02 章：当日限制不得污染长期档案）：
  // 「以后只能用哑铃」= 长期档案补丁 + 新计划与新日程的组合草稿；「今天只能用哑铃」不写长期档案。
  // 尚未建档时仍走建档收集（无正式档案无法生成计划处方）。
  const equipmentScope =
    state.profile === null
      ? undefined
      : /今天/.test(message) && message.includes("哑铃")
        ? "today_only"
        : /以后/.test(message) && message.includes("哑铃")
          ? "long_term"
          : undefined;
  // 未建档（对话是唯一建档入口）、本会话建档进行中、或消息含档案事实 → 走建档剧本
  const isOnboarding =
    state.profile === null ||
    parsed.touched ||
    state.onboarding.get(run.session_id)?.last_asked !== undefined;

  let text: string;
  let draft: Draft | undefined;
  // 01 1.3/1.4：base_business_version 绑定「生成草稿时实际读到的业务版本」。提案就在下面这段
  // 同步分支内按当前业务状态派生，所以先在生成处取版本；不得等流式输出结束后再用最新
  // context_version 回填（否则流式期间被别的确认推进后，旧载荷会带着新版本通过 stale 检查）。
  const base_business_version = state.context_version;

  if (isCheckIn) {
    const r = recordScriptReply(state, message);
    text = r.text;
    draft = r.draft;
  } else if (equipmentScope === "today_only") {
    text = TODAY_ONLY_EQUIPMENT_REPLY;
  } else if (equipmentScope === "long_term") {
    const r = dumbbellOnlyReply(state);
    text = r.text;
    draft = r.draft;
  } else if (isOnboarding) {
    const r = onboardingTurn(
      state,
      run.session_id,
      message,
      parsed,
      safetyHits,
    );
    text = r.text;
    draft = r.draft;
  } else if (isGuidance) {
    text = planGuidanceReply(state);
  } else if (isArrangement) {
    const r = arrangementScriptReply(state, message);
    text = r.text;
    draft = r.draft;
  } else if (isPlan) {
    const r = planScriptReply(state);
    text = r.text;
    draft = r.draft;
  } else if (isReview) {
    text = REVIEW_REPLY;
  } else {
    text = GENERIC_REPLY;
  }

  if (safetyHits.length > 0 && !isOnboarding) {
    // 身体情况命中六类安全症状：立即给出专业评估措辞，且不生成训练类草稿（plans/stage1.md F1-02）
    if (draft && draft.kind !== "profile_update") {
      draft = undefined;
      text =
        "你报告的情况需要先线下专业评估；本次不生成训练建议或计划调整草稿。";
    }
    text = [professionalEvalBlock(safetyHits), text].join("\n\n");
  }

  // 生成时版本先落到草稿上，再开始流式输出（01 1.3：确认时的 stale 检查按这个版本比对）
  if (draft) draft.base_business_version = base_business_version;

  // 流式输出：按段落切块；节奏放缓保证流式观感与 conversation_busy 演示窗口
  await sleep(600);
  if (run.cancelled) return finishCancelled(emit, run);
  const parts = text.split(/\n(?=.)/);
  for (const part of parts) {
    await sleep(450);
    if (run.cancelled) return finishCancelled(emit, run);
    emit("message.delta", { run_id: run.id, text: part + "\n" });
    // 已流出整段记为已保存（stage0 已拍 D1）；取消/失败后经 /api/runs/active 恢复
    run.saved_text += part + "\n";
  }

  if (draft) {
    await sleep(500);
    if (run.cancelled) return finishCancelled(emit, run);
    state.drafts.set(draft.id, draft);
    state.draft_sessions.set(draft.id, run.session_id);
    run.draft_ids.push(draft.id);
    emit("draft.proposed", { run_id: run.id, draft });
  }

  run.status = "completed";
  emit("run.completed", { run_id: run.id });

  const full = text;
  const msgs = state.messages.get(run.session_id) ?? [];
  msgs.push({
    id: nextId("m"),
    role: "assistant",
    content: full,
    draft_id: draft?.id,
  });
  state.messages.set(run.session_id, msgs);
  const session = state.sessions.find((s) => s.id === run.session_id);
  if (session) session.updated_at = new Date().toISOString();
}

function finishCancelled(
  emit: (event: string, data: unknown) => void,
  run: RunState,
): void {
  // 取消端点已置 status 并发过 run.cancelled；此处仅在剧本仍处活跃态时补发，
  // 避免双发。已进入终态者（completed/cancelled，以及 dev 注入的 failed）一律不覆写
  // （08 8.1：终态不可恢复、不得互相流转）。
  if (run.status === "pending" || run.status === "running") {
    run.status = "cancelled";
    emit("run.cancelled", { run_id: run.id });
  }
}

/* --------------------------- dev-only 控制端点实现 --------------------------
 *
 * /api/dev/* 为 mock 演示与故障注入专用路由组（plans/stage0.md F0-02、第 7 节剧本
 * 第 4/6/8 步）。它们不是契约端点：请求/响应形状一律不写进 src/lib/contract.ts，
 * 不新增 SSE 事件类型，前端 UI 与 src/lib/api.ts 不消费。
 * ------------------------------------------------------------------------- */

/** Run 的 dev 诊断快照（形状仅用于人读与脚本断言，含 saved_text_chars 以核对保留） */
interface DevRunSnapshot {
  run_id: string;
  session_id: string;
  status: RunStatus;
  error_code?: ErrorCode;
  /** 已保存部分回答字符数（08 8.7 规则 5/8、已拍 D1：失败/中断后必须原样保留） */
  saved_text_chars: number;
  /** 该 Run 已提出草稿的 id：注入失败不丢弃、不改动草稿（01 1.2） */
  draft_ids: string[];
}

function devRunSnapshot(run: RunState): DevRunSnapshot {
  return {
    run_id: run.id,
    session_id: run.session_id,
    status: run.status,
    ...(run.error_code ? { error_code: run.error_code } : {}),
    saved_text_chars: run.saved_text.length,
    draft_ids: [...run.draft_ids],
  };
}

/** 全局最近一个 Run（Map 插入序），与 GET /api/runs/active 的取法一致 */
function latestRun(state: MockState): RunState | undefined {
  let latest: RunState | undefined;
  for (const r of state.runs.values()) latest = r;
  return latest;
}

/** 最近一个仍在活跃态（pending/running）的 Run；mock 为全局单 Run（08 8.2/8.3） */
function activeRun(state: MockState): RunState | undefined {
  for (const r of [...state.runs.values()].reverse()) {
    if (r.status === "pending" || r.status === "running") return r;
  }
  return undefined;
}

/** 活跃态可被注入失败的 Run（08 8.1：终态不得再流转） */
function isInjectable(run: RunState): boolean {
  return run.status === "pending" || run.status === "running";
}

/**
 * dev-only：把 pending/running Run 转 failed 并发 run.failed（08 8.1 唯一合法入口）。
 * 先置 cancelled 标志让在途剧本在下一个检查点静默退出——不覆写 failed、不补发
 * run.cancelled、不追加完整助手消息；saved_text 与 draft_ids 一律不动。
 */
function failRun(hub: SseHub, run: RunState, code: ErrorCode): DevRunSnapshot {
  run.cancelled = true;
  run.status = "failed";
  run.error_code = code;
  hub.emit("run.failed", { run_id: run.id, error_code: code });
  return devRunSnapshot(run);
}

function devUnknownRun(res: ServerResponse, runId: string | undefined): void {
  apiError(res, {
    http_status: 404,
    error_code: "invalid_request",
    message: `Run 不存在${runId ? `：${runId}` : ""}`,
  });
}

function devTerminal(res: ServerResponse, run: RunState): void {
  // 终态合法性（08 8.1）：completed/failed/cancelled 不得被注入失败或重启中断覆写
  apiError(res, {
    http_status: 409,
    error_code: "invalid_request",
    message: `Run ${run.id} 已处于终态 ${run.status}，不可注入失败（08 8.1：终态不可恢复）`,
  });
}

async function handleDevControls(
  req: IncomingMessage,
  res: ServerResponse,
  path: string,
  state: MockState,
  hub: SseHub,
): Promise<void> {
  const method = (req.method ?? "GET").toUpperCase();

  if (!DEV_CONTROLS_ENABLED) {
    return apiError(res, {
      http_status: 409,
      error_code: "invalid_request",
      message: "mock dev 控制端点已关闭（FIT_MOCK_DEV_CONTROLS=off）",
    });
  }

  /* 1) 重置种子场景：整份内存态回到初始值（默认种子 / 空种子 / 计划生成种子） */
  if (path === "/api/dev/reset" && method === "POST") {
    const body = await readJsonBody<{ seed?: string }>(req);
    const seed = body.seed ?? "default";
    if (seed !== "default" && seed !== "empty" && seed !== "noplan")
      return apiError(res, {
        http_status: 400,
        error_code: "invalid_request",
        message: `未知种子场景：${seed}`,
        detail: "default | empty | noplan",
      });
    const runsStopped: string[] = [];
    for (const run of state.runs.values()) {
      if (!isInjectable(run)) continue;
      // 先置终态再置标志：剧本在下一检查点静默退出，不向已重置的场景补发事件
      run.status = "cancelled";
      run.cancelled = true;
      runsStopped.push(run.id);
    }
    hub.devResume();
    // 原地覆盖字段：保持 state 对象身份不变（在途剧本与中间件闭包仍指向同一对象）
    Object.assign(
      state,
      seed === "empty"
        ? emptySeedState()
        : seed === "noplan"
          ? noPlanSeedState()
          : seedState(),
    );
    return json(res, 200, {
      ok: true,
      seed,
      runs_stopped: runsStopped,
      context_version: state.context_version,
      has_profile: state.profile !== null,
      plan_version: state.plan?.version ?? null,
      sessions: state.sessions.length,
      records: state.records.length,
      drafts: state.drafts.size,
      execution_slot_run_id: state.execution_slot_run_id,
      sse_clients: hub.clientCount,
      note: "既有 SSE 连接不关闭（不重放、不补发），前端按 08 8.7 经查询取回新场景",
      dev_note: DEV_NOTE,
    });
  }

  /* 2) 注入 run.failed：活跃 Run 或指定 Run，错误码取自契约 ErrorCode */
  if (path === "/api/dev/runs/fail" && method === "POST") {
    const body = await readJsonBody<{ run_id?: string; error_code?: string }>(
      req,
    );
    const code = body.error_code ?? "invalid_request";
    if (!isInjectableErrorCode(code))
      return apiError(res, {
        http_status: 400,
        error_code: "invalid_request",
        message: `error_code 不在契约 ErrorCode 之内：${code}`,
        detail: DEV_ERROR_CODES.join(" | "),
      });
    let target: RunState | undefined;
    if (body.run_id) {
      target = state.runs.get(body.run_id);
      if (!target) return devUnknownRun(res, body.run_id);
      if (!isInjectable(target)) return devTerminal(res, target);
    } else {
      target = activeRun(state);
      if (!target)
        return apiError(res, {
          http_status: 409,
          error_code: "invalid_request",
          message:
            "当前没有 pending/running Run 可注入失败；run_id 可指定历史 Run（终态仍受 08 8.1 约束）",
        });
    }
    const suppressed = hub.suspended();
    const run = failRun(hub, target, code);
    return json(res, 200, {
      ok: true,
      run,
      event_suppressed_by_suspend: suppressed,
      execution_slot_run_id: state.execution_slot_run_id,
      note: "run.failed 已发出；执行名额在剧本实际退出后释放（08 8.3）",
      dev_note: DEV_NOTE,
    });
  }

  /* 3) 模拟服务重启中断（08 8.4：遗留 pending/running 统一 failed + interrupted_by_restart） */
  if (path === "/api/dev/restart" && method === "POST") {
    const body = await readJsonBody<{ run_id?: string }>(req);
    const legacy = [...state.runs.values()].filter(isInjectable);
    if (body.run_id) {
      const only = state.runs.get(body.run_id);
      if (!only) return devUnknownRun(res, body.run_id);
      if (!isInjectable(only)) return devTerminal(res, only);
      legacy.length = 0;
      legacy.push(only);
    }
    const suppressed = hub.suspended();
    const affected = legacy.map((run) =>
      failRun(hub, run, "interrupted_by_restart"),
    );
    // 进程重启隐含执行名额一并释放；已保存文本与草稿原样保留（saved_text_chars/draft_ids 可核对）
    state.execution_slot_run_id = null;
    return json(res, 200, {
      ok: true,
      affected,
      skipped_terminal: [...state.runs.values()]
        .filter((run) => !isInjectable(run))
        .map((run) => `${run.id}:${run.status}`),
      event_suppressed_by_suspend: suppressed,
      execution_slot_run_id: null,
      note: "仅投影 08 8.4 的 Run 状态：mock 不重启进程，SSE 连接与内存态仍在，saved_text 与草稿不丢",
      dev_note: DEV_NOTE,
    });
  }

  /* 4) 挂起 SSE 业务事件（演练 08 8.7「连续 45s 无事件 → 转查询」） */
  if (path === "/api/dev/events/suspend" && method === "POST") {
    const body = await readJsonBody<{ seconds?: number; heartbeat?: boolean }>(
      req,
    );
    if (
      body.seconds !== undefined &&
      (typeof body.seconds !== "number" ||
        !Number.isFinite(body.seconds) ||
        body.seconds <= 0)
    )
      return apiError(res, {
        http_status: 400,
        error_code: "invalid_request",
        message: "seconds 须为正数（≤300 会被截到上限）",
      });
    const seconds = Math.min(
      body.seconds ?? DEV_SUSPEND_DEFAULT_SECONDS,
      DEV_SUSPEND_MAX_SECONDS,
    );
    const heartbeat = body.heartbeat === true;
    hub.devSuspend(seconds, heartbeat);
    return json(res, 200, {
      ok: true,
      suspended_seconds: seconds,
      heartbeat_during_suspend: heartbeat,
      business_events: "dropped",
      active_run_id: activeRun(state)?.id ?? null,
      sse_clients: hub.clientCount,
      client_effect: heartbeat
        ? "连接保活（heartbeat 每 15s）但无任何业务事件：前端不得转查询"
        : "完全静默（无业务事件、无 heartbeat）：前端连续 45s 无事件即关闭连接转 GET /api/runs/active 轮询（08 8.7）",
      note: `默认 ${DEV_SUSPEND_DEFAULT_SECONDS}s（>45s 阈值）；seconds 可缩短以便快速演练，上限 ${DEV_SUSPEND_MAX_SECONDS}s，可用 /api/dev/events/resume 提前恢复`,
      dev_note: DEV_NOTE,
    });
  }

  if (path === "/api/dev/events/resume" && method === "POST") {
    await readBody(req);
    const dropped = hub.dev.dropped;
    hub.devResume();
    return json(res, 200, {
      ok: true,
      dropped_business_events: dropped,
      business_events: "resumed",
      note: "不补发挂起期间丢弃的事件（08 8.7 无重放）；当前状态经业务接口查询",
      dev_note: DEV_NOTE,
    });
  }

  /* 5) 只读诊断快照（便于走查取证，不改任何状态） */
  if (path === "/api/dev/status" && method === "GET") {
    return json(res, 200, {
      ok: true,
      context_version: state.context_version,
      has_api_key: state.provider.has_api_key,
      has_profile: state.profile !== null,
      plan_version: state.plan?.version ?? null,
      /** 历史计划版本（F2-04：替换时旧版以 archived 归档保留，PRD 5.3） */
      plan_history: state.plan_history.map((p) => ({
        version: p.version,
        status: p.status,
      })),
      /** 具体日程全量快照（锁定双态可核对） */
      schedules: state.schedules.map((s) => ({
        id: s.id,
        plan_version: s.plan_version,
        date: s.date,
        plan_workout_key: s.plan_workout_key,
        stored_status: s.stored_status,
        locked_by_date_rule: s.locked_by_date_rule,
        locked_effective: s.locked_effective,
        status: s.stored_status,
      })),
      arrangement_revisions: state.arrangement_revisions.map((r) => ({
        id: r.id,
        scheduled_on: r.target.scheduled_on,
        accepted_at: r.accepted_at,
      })),
      execution_slot_run_id: state.execution_slot_run_id,
      runs: [...state.runs.values()].map(devRunSnapshot),
      drafts: [...state.drafts.values()].map((d) => ({
        id: d.id,
        kind: d.kind,
        status: d.status,
        revision: d.revision,
        base_business_version: d.base_business_version,
      })),
      dev_control: {
        suspended_seconds_remaining: Math.max(
          0,
          Math.round((hub.dev.until - Date.now()) / 1000),
        ),
        heartbeat_while_suspended: hub.dev.heartbeat,
        dropped_business_events: hub.dev.dropped,
        sse_clients: hub.clientCount,
      },
      dev_note: DEV_NOTE,
    });
  }

  /* 6) 注入下一次计划确认事务中途失败（F2-04；plans/stage2.md §7 第 8 步回滚验证）
        确认事务在正式写入（档案补丁／计划／日程）之后、草稿提交与版本递增之前失败，
        用于验证任一步失败时整份回滚；一次性，命中后自动解除 */
  if (path === "/api/dev/confirm/fail-next" && method === "POST") {
    await readBody(req);
    state.dev_confirm_failure = true;
    return json(res, 200, {
      ok: true,
      armed: true,
      note: "下一次计划草稿确认将在正式写入后失败，并整份回滚（档案、计划与历史、全部日程、草稿状态、context_version）",
      dev_note: DEV_NOTE,
    });
  }

  return apiError(res, {
    http_status: 404,
    error_code: "invalid_request",
    message: `未知 dev 控制端点 ${method} ${path}`,
  });
}
/* ------------------------------ 统计重算 ---------------------------------- */

/**
 * 全量现算（stage3 §3.3/§3.4）：
 * - 完成率：分母 = 该版本 [starts_on, review_on) 内 scheduled_on <= 业务日且未取消的应训练日；
 *   分子 = 关联该日程的 valid 记录且至少一个工作组（同一日程最多计一次）；分母 0 → rate null。
 * - 三桶：基准是当次安排快照（非计划处方）；只看次数；缺次数 → pending；无对照安排不进三桶。
 * - PR：排除 incomplete、辅助组、热身组；同重量最高次数按单组；自重/无重量不进重量 PR。
 * - data_updated_at 用 mock 时钟（不依赖真实 Date.now）。
 */
function recomputeStats(state: MockState): void {
  advanceMockClock();
  const buckets: Buckets = { met: 0, unmet: 0, pending: 0 };
  const prMap = new Map<string, PrEntry>();
  const scheduleById = new Map(state.schedules.map((s) => [s.id, s]));
  const arrangementById = new Map(
    state.arrangement_revisions.map((r) => [r.id, r]),
  );
  /** 完成率分子：已出现 valid 工作组记录的日程 id（同一安排/日程最多一次） */
  const completedSessionIds = new Set<string>();

  for (const rec of state.records) {
    rec.judgement = null;
    delete rec.comparison;
    for (const s of rec.sets) delete s.judgement;
    if (rec.status !== "valid") continue; // incomplete 不进 PR / 完成率 / 三桶
    const hasWork = rec.sets.some((s) => s.set_type === "working");

    // 完成率分子（显式关联；不从日期推断）
    if (rec.scheduled_session_id && hasWork) {
      const sched = scheduleById.get(rec.scheduled_session_id);
      if (sched && sched.stored_status !== "cancelled")
        completedSessionIds.add(rec.scheduled_session_id);
    }

    // 三桶 + 对照摘要：仅显式携带 arrangement_revision_id 的记录
    const arr = rec.arrangement_revision_id
      ? arrangementById.get(rec.arrangement_revision_id)
      : undefined;
    if (arr && hasWork) {
      const item = arr.target.exercises.find(
        (e) => e.display_snapshot.name === rec.exercise,
      );
      if (item && item.prescription.kind === "reps") {
        const { min, max } = item.prescription.reps_range;
        const local: Buckets = { met: 0, unmet: 0, pending: 0 };
        for (const set of rec.sets) {
          if (set.set_type !== "working") continue;
          // 判定只看次数：缺次数 → pending；落在显式区间（含端点）→ met；否则 unmet
          if (set.reps === undefined || set.reps === null) {
            local.pending += 1;
            set.judgement = "pending";
          } else if (set.reps >= min && set.reps <= max) {
            local.met += 1;
            set.judgement = "met";
          } else {
            local.unmet += 1;
            set.judgement = "unmet";
          }
        }
        rec.judgement = local;
        buckets.met += local.met;
        buckets.unmet += local.unmet;
        buckets.pending += local.pending;
        const plannedItem =
          state.plan && state.plan.version === arr.target.plan_version
            ? findWorkout(
                state.plan.payload,
                arr.target.plan_workout_key,
              )?.exercises.find((e) => e.item_key === item.item_key)
            : undefined;
        rec.comparison = {
          ...(plannedItem?.prescription.kind === "reps"
            ? { planned_sets: plannedItem.prescription.work_sets }
            : {}),
          arranged_sets: item.prescription.work_sets,
          accepted_at: arr.accepted_at,
        };
      }
    }

    // PR：valid + 工作组 + 非辅助 + 有重量与次数（自重不进重量 PR）
    for (const set of rec.sets) {
      if (
        set.set_type !== "working" ||
        set.assisted ||
        set.weight_kg === undefined ||
        set.reps === undefined
      )
        continue;
      const key = `${rec.exercise}·${rec.variant}`;
      const cur = prMap.get(key);
      if (!cur || set.weight_kg > cur.best_weight_kg) {
        prMap.set(key, {
          exercise: rec.exercise,
          variant: rec.variant,
          best_weight_kg: set.weight_kg,
          best_reps_at_weight: set.reps,
        });
      } else if (
        set.weight_kg === cur.best_weight_kg &&
        set.reps > cur.best_reps_at_weight
      ) {
        cur.best_reps_at_weight = set.reps;
      }
    }
  }

  // 完成率 per_week：按计划周 Wn 现算（只含截至业务日已到的应训练日）
  const per_week: WeekCompletion[] = [];
  if (state.plan) {
    const plan = state.plan;
    const due = state.schedules.filter(
      (s) =>
        s.plan_version === plan.version &&
        s.stored_status !== "cancelled" &&
        s.date >= plan.starts_on &&
        s.date < plan.review_on &&
        s.date <= MOCK_TODAY,
    );
    if (due.length > 0) {
      const maxWeek = Math.max(
        ...due.map((s) => Math.floor(dayDiff(plan.starts_on, s.date) / 7)),
      );
      for (let w = 0; w <= maxWeek; w += 1) {
        const weekSchedules = due.filter(
          (s) => Math.floor(dayDiff(plan.starts_on, s.date) / 7) === w,
        );
        const planned = weekSchedules.length;
        const completed = weekSchedules.filter((s) =>
          completedSessionIds.has(s.id),
        ).length;
        per_week.push({
          week: `W${w + 1}`,
          planned,
          completed,
          rate:
            planned === 0
              ? null
              : Math.round((completed / planned) * 1000) / 10,
        });
      }
    }
  }

  state.stats = {
    per_week,
    buckets,
    prs: [...prMap.values()].sort((a, b) =>
      a.exercise.localeCompare(b.exercise),
    ),
    data_updated_at: mockNowIso(),
  };
  state.review.stale = true;
}

/* ------------------------------ 中间件主体 --------------------------------- */

function createHandler(state: MockState, hub: SseHub) {
  return async (
    req: IncomingMessage,
    res: ServerResponse,
    next: () => void,
  ) => {
    const url = new URL(req.url ?? "/", "http://localhost");
    const path = url.pathname;
    const method = (req.method ?? "GET").toUpperCase();
    if (!path.startsWith("/api/")) return next();

    try {
      /* SSE 订阅 */
      if (path === "/api/events" && method === "GET") {
        hub.open(req, res);
        return;
      }

      /* dev-only 控制端点组（/api/dev/*）：非契约、不进 contract.ts、不进 SseEvent；
         见文件内「dev-only 控制端点实现」段 */
      if (path.startsWith("/api/dev/")) {
        return await handleDevControls(req, res, path, state, hub);
      }

      /* Provider */
      if (path === "/api/provider" && method === "GET")
        return json(res, 200, state.provider);

      if (
        path === "/api/provider/api-key" &&
        (method === "PUT" || method === "DELETE")
      ) {
        if (method === "PUT") {
          const body = JSON.parse((await readBody(req)) || "{}") as {
            api_key?: string;
          };
          if (!body.api_key || typeof body.api_key !== "string") {
            return apiError(res, {
              http_status: 400,
              error_code: "invalid_request",
              message: "缺少 api_key",
            });
          }
          // 明文只置位，绝不存储或返回
          state.provider.has_api_key = true;
          return json(res, 200, { has_api_key: true });
        }
        state.provider.has_api_key = false;
        return json(res, 200, { has_api_key: false });
      }

      /* 档案（PRD §5.2；02 2.1/2.2）：profile = null = 尚未建档；plan 缺省 = 未生成/未启用 */
      if (path === "/api/profile" && method === "GET") {
        /* F2-05：安全复核在请求时按最新红旗与限制重算，不缓存结果——限制/红旗一经确认即在此反映 */
        const safety = state.plan
          ? reviewPlanSafety({
              plan: state.plan,
              profile: state.profile,
              restrictions: state.restrictions,
              context_version: state.context_version,
            })
          : undefined;
        return json(res, 200, {
          profile: state.profile,
          restrictions: state.restrictions,
          context_version: state.context_version,
          /* STAGED-SHARED-EDIT（lane 3b，supervisor 批准）：/profile 当前计划卡数据源 */
          ...(state.plan ? { plan: state.plan } : {}),
          /* 具体日程含历史版本条目（旧版已取消、已到期锁定）：计划卡展示取消/锁定并保留历史 */
          ...(state.plan ? { schedules: state.schedules } : {}),
          ...(safety ? { plan_safety: safety } : {}),
        });
      }

      /* 记录 */
      if (path === "/api/records" && method === "GET")
        return json(res, 200, { records: state.records });

      /* 已接受安排读回（stage3 拍板的 mock 端点；镜像 arrangement_revisions） */
      if (path === "/api/arrangements" && method === "GET") {
        const arrangements: AcceptedArrangement[] =
          state.arrangement_revisions.map((r) => ({
            id: r.id,
            accepted_at: r.accepted_at,
            target: r.target,
          }));
        return json(res, 200, { arrangements });
      }

      /* 统计与复盘 */
      if (path === "/api/stats" && method === "GET")
        return json(res, 200, state.stats);
      if (path === "/api/review" && method === "GET")
        return json(res, 200, state.review);

      /* 会话 */
      if (path === "/api/sessions" && method === "GET")
        return json(res, 200, state.sessions);
      if (path === "/api/sessions" && method === "POST") {
        const body = JSON.parse((await readBody(req)) || "{}") as {
          title?: string;
        };
        const s: SessionSummary = {
          id: nextId("s"),
          title: body.title || "新对话",
          updated_at: new Date().toISOString(),
        };
        state.sessions.unshift(s);
        state.messages.set(s.id, []);
        return json(res, 200, s);
      }

      const messagesMatch = path.match(/^\/api\/sessions\/([^/]+)\/messages$/);
      if (messagesMatch && method === "GET") {
        return json(res, 200, state.messages.get(messagesMatch[1]) ?? []);
      }

      /* 会话草稿当前状态列表（08 8.7 规则 2、01 1.2：当前状态经业务接口查询） */
      const sessionDraftsMatch = path.match(
        /^\/api\/sessions\/([^/]+)\/drafts$/,
      );
      if (sessionDraftsMatch && method === "GET") {
        const drafts: Draft[] = [];
        for (const [draftId, sessionId] of state.draft_sessions) {
          if (sessionId !== sessionDraftsMatch[1]) continue;
          const d = state.drafts.get(draftId);
          if (d) drafts.push(d);
        }
        return json(res, 200, drafts);
      }

      /* Run：发起 / 取消 */
      if (path === "/api/runs" && method === "POST") {
        const body = JSON.parse((await readBody(req)) || "{}") as {
          session_id?: string;
          message?: string;
          client_request_id?: string;
        };
        if (!body.session_id || !body.message || !body.client_request_id) {
          return apiError(res, {
            http_status: 400,
            error_code: "invalid_request",
            message: "缺少 session_id / message / client_request_id",
          });
        }

        // 相同 client_request_id 幂等返回已有 Run
        for (const r of state.runs.values()) {
          if (r.client_request_id === body.client_request_id)
            return json(res, 200, { run_id: r.id });
        }
        // 全局单 Run（08 8.2/8.3）：唯一执行名额被持有（含取消后的收尾窗口）时 409
        if (state.execution_slot_run_id !== null) {
          return apiError(res, {
            http_status: 409,
            error_code: "conversation_busy",
            message: "已有正在进行的对话，请稍候或取消当前任务。",
          });
        }
        if (!state.provider.has_api_key) {
          return apiError(res, {
            http_status: 409,
            error_code: "not_configured",
            message: "尚未配置可用模型，请先到设置页完成配置。",
          });
        }

        const run: RunState = {
          id: nextId("run"),
          session_id: body.session_id,
          client_request_id: body.client_request_id,
          status: "pending",
          cancelled: false,
          saved_text: "",
          draft_ids: [],
        };
        state.runs.set(run.id, run);
        // 执行名额归本次 Run（08 8.3）：runScript 实际退出（finally）后才释放
        state.execution_slot_run_id = run.id;

        const msgs = state.messages.get(body.session_id) ?? [];
        msgs.push({ id: nextId("m"), role: "user", content: body.message });
        state.messages.set(body.session_id, msgs);

        // 剧本异步推进，不阻塞响应；名额在剧本实际退出后释放（08 8.3）
        void runScript(state, hub, run, body.message)
          .catch(() => {
            if (run.status === "running" || run.status === "pending") {
              run.status = "failed";
              run.error_code = "invalid_request";
              hub.emit("run.failed", {
                run_id: run.id,
                error_code: "invalid_request",
              });
            }
          })
          .finally(() => {
            if (state.execution_slot_run_id === run.id)
              state.execution_slot_run_id = null;
          });
        return json(res, 200, { run_id: run.id });
      }

      const cancelMatch = path.match(/^\/api\/runs\/([^/]+)\/cancel$/);
      if (cancelMatch && method === "POST") {
        const run = state.runs.get(cancelMatch[1]);
        if (!run)
          return apiError(res, {
            http_status: 404,
            error_code: "invalid_request",
            message: "Run 不存在",
          });
        if (run.status === "pending" || run.status === "running") {
          run.cancelled = true;
          run.status = "cancelled";
          hub.emit("run.cancelled", { run_id: run.id });
        }
        return json(res, 200, { run_id: run.id, status: run.status });
      }

      /* Run 查询（08 8.7 断线/刷新恢复规则 2/3）：全局最近一个 Run 的当前状态
         （状态 + 已保存部分回答 + 关联草稿当前状态）；null = 当前无可查询 Run */
      if (path === "/api/runs/active" && method === "GET") {
        const latest = latestRun(state);
        if (!latest) return json(res, 200, { run: null });
        return json(res, 200, {
          run: {
            run_id: latest.id,
            session_id: latest.session_id,
            status: latest.status,
            saved_text: latest.saved_text,
            ...(latest.error_code ? { error_code: latest.error_code } : {}),
            drafts: latest.draft_ids.flatMap((id) => {
              const d = state.drafts.get(id);
              return d ? [d] : [];
            }),
          },
        });
      }

      /* 草稿：确认（幂等）/ 重算 */
      /* 草稿纠错（01 1.2/1.3）：仅待确认草稿可纠错；整份 payload 替换，revision+1，
         展示与 Diff 随之更新；不触碰正式数据与 context_version，不自动提交 */
      const reviseMatch = path.match(/^\/api\/drafts\/([^/]+)\/revise$/);
      if (reviseMatch && method === "POST") {
        const body = JSON.parse((await readBody(req)) || "{}") as {
          payload?: Draft["payload"];
          revision?: number;
        };
        const draft = state.drafts.get(reviseMatch[1]);
        if (!draft)
          return apiError(res, {
            http_status: 404,
            error_code: "invalid_request",
            message: "草稿不存在",
          });
        if (draft.status !== "pending")
          return apiError(res, {
            http_status: 409,
            error_code: "invalid_request",
            message: "仅待确认草稿可纠错",
          });
        if (!body.payload)
          return apiError(res, {
            http_status: 400,
            error_code: "invalid_request",
            message: "缺少 payload",
          });
        // 交接 F3：纠错必须携带所见 revision；不匹配按 draft_modified 拒绝
        if (typeof body.revision !== "number")
          return apiError(res, {
            http_status: 400,
            error_code: "invalid_request",
            message: "缺少 revision（用户所见草稿修订版本）",
          });
        if (body.revision !== draft.revision)
          return apiError(res, {
            http_status: 409,
            error_code: "draft_modified",
            message: "草稿已被修改（所见修订版本不一致），请刷新草稿后重试。",
          });
        // payload 形状由草稿 kind 决定（D1A 单一形状源）：不匹配则拒绝
        const payloadMatchesKind =
          draft.kind === "training_record"
            ? "occurred_on" in body.payload
            : draft.kind === "plan"
              ? "title" in body.payload && "diff" in body.payload
              : draft.kind === "arrangement"
                ? "target" in body.payload
                : "profile" in body.payload;
        if (!payloadMatchesKind)
          return apiError(res, {
            http_status: 400,
            error_code: "invalid_request",
            message: `payload 形状与草稿类型不匹配：${draft.kind}`,
          });

        // 展示与 Diff 随纠错更新（01 1.2）：diff 一律由服务端派生
        if (draft.kind === "training_record") {
          const rp = body.payload as RecordDraftPayload;
          const recInvalid = recordPayloadError(rp);
          if (recInvalid)
            return apiError(res, {
              http_status: 400,
              error_code: "invalid_request",
              message: `训练记录载荷无效，纠错未生效：${recInvalid}`,
            });
          draft.payload = rp;
          draft.diff = recordDraftDiff(state, rp);
        } else if (draft.kind === "plan") {
          const storedPatch = (draft.payload as PlanDraftPayload).profile_patch;
          const normalized = normalizePlanPayload(
            draft.payload as PlanDraftPayload,
            body.payload as PlanDraftPayload,
            {
              profile: storedPatch
                ? profileWithPatch(state.profile, storedPatch)
                : state.profile,
              restrictions: storedPatch
                ? (storedPatch.restrictions ?? [])
                : state.restrictions,
              today: MOCK_TODAY,
            },
          );
          if (!normalized.ok)
            return apiError(res, {
              http_status: 400,
              error_code: "invalid_request",
              message: `计划载荷无效，纠错未生效：${normalized.error}`,
            });
          draft.payload = normalized.payload;
          draft.diff = normalized.payload.diff;
        } else if (draft.kind === "arrangement") {
          // 安排草稿：整份替换目标快照，diff 由服务端对比当前计划派生
          const p = body.payload as ArrangementDraftPayload;
          const invalid = arrangementPayloadError(state, p);
          if (invalid)
            return apiError(res, {
              http_status: 400,
              error_code: "invalid_request",
              message: `安排载荷无效，纠错未生效：${invalid}`,
            });
          draft.payload = p;
          draft.diff = arrangementDiff(state, p);
        } else {
          const invalid = profilePayloadError(body.payload);
          if (invalid)
            return apiError(res, {
              http_status: 400,
              error_code: "invalid_request",
              message: `档案载荷无效：${invalid}`,
            });
          const p = body.payload as ProfileDraftPayload;
          draft.payload = p;
          draft.diff = profileDraftDiff(state.profile, state.restrictions, p);
        }
        draft.revision += 1;
        return json(res, 200, { draft });
      }

      const confirmMatch = path.match(/^\/api\/drafts\/([^/]+)\/confirm$/);
      if (confirmMatch && method === "POST") {
        // 先读完请求体再分支，避免 keep-alive 连接上残留未消费的 body
        const confirmBody = JSON.parse((await readBody(req)) || "{}") as {
          revision?: number;
        };
        const draft = state.drafts.get(confirmMatch[1]);
        if (!draft)
          return apiError(res, {
            http_status: 404,
            error_code: "invalid_request",
            message: "草稿不存在",
          });
        // 确认顺序（01 1.4）：1) 已提交草稿幂等返回原结果——不做 revision 要求，不受后续
        // 业务版本/修订变化影响；返回首次提交时持久化的原始凭据（owner 决策 B），不从当前状态重建
        if (draft.status === "committed") {
          const receipt = state.confirm_receipts.get(draft.id);
          const result: ConfirmResult = {
            draft_id: draft.id,
            status: "committed",
            newly_committed: false,
            context_version: receipt?.context_version ?? state.context_version,
            summary: receipt?.summary ?? "该草稿已提交过（幂等返回原结果）",
            committed_revision: receipt?.committed_revision ?? draft.revision,
            committed_business_version:
              receipt?.committed_business_version ?? state.context_version,
            ...(receipt?.plan_version
              ? { plan_version: receipt.plan_version }
              : {}),
            ...(receipt?.arrangement_revision_id
              ? { arrangement_revision_id: receipt.arrangement_revision_id }
              : {}),
            ...(receipt?.training_session_id
              ? { training_session_id: receipt.training_session_id }
              : {}),
          };
          return json(res, 200, result);
        }

        // 2) 已丢弃拒绝：Discarded 不得再提交（01 1.3）；不做 revision 要求；先于基线/修订检查（01 1.4 顺序）
        if (draft.status === "discarded")
          return apiError(res, {
            http_status: 409,
            error_code: "invalid_request",
            message: "草稿已丢弃，不可确认",
          });

        // 3) 首次确认才要求携带所见修订版本（01 1.4）
        if (typeof confirmBody.revision !== "number")
          return apiError(res, {
            http_status: 400,
            error_code: "invalid_request",
            message: "缺少 revision（用户所见草稿修订版本）",
          });

        // 4) 业务基线检查（01 1.4/1.6）：context-version 先于修订检查
        if (draft.base_business_version !== state.context_version) {
          return apiError(res, {
            http_status: 409,
            error_code: "draft_stale",
            message:
              "业务数据已变更（如：新增训练记录），请按最新数据一键重算后再次确认。",
            detail: "context_version 已从生成时的版本向前推进",
          });
        }

        // 5) 修订版本检查：所见修订与当前不一致 → 409 draft_modified（01 1.4）
        if (confirmBody.revision !== draft.revision)
          return apiError(res, {
            http_status: 409,
            error_code: "draft_modified",
            message: "草稿已被修改（所见修订版本不一致），请刷新草稿后重试。",
          });

        // 6) 以服务端存储的草稿内容复查领域规则并原子提交（01 1.4）；
        //    内联纠错不经确认提交——纠错走 revise 业务接口（01 1.2，不建第二编辑入口）
        //    profile_update 的复查 = profilePayloadError（确定性字段校验）

        // 原子提交（mock：顺序内存写入）；计划草稿的写入在 commitPlanDraft 事务内完成
        let planCommit:
          | {
              version: string;
              created: number;
              cancelled: number;
              summary: string;
            }
          | undefined;
        let arrangementSummary: string | undefined;
        let arrangementRevisionId: string | undefined;
        let trainingSessionId: string | undefined;
        let planVersionResult: string | undefined;
        if (draft.kind === "training_record") {
          const p = draft.payload as RecordDraftPayload;
          const recInvalid = recordPayloadError(p);
          if (recInvalid)
            return apiError(res, {
              http_status: 400,
              error_code: "invalid_request",
              message: `训练记录载荷无效，无法确认：${recInvalid}`,
            });
          const status = deriveRecordDraftStatus(p);
          // 日程关联：仅当草稿显式携带已接受安排时，从安排 target 取（不从日期推断）
          const linkedArr = p.arrangement_revision_id
            ? state.arrangement_revisions.find(
                (r) => r.id === p.arrangement_revision_id,
              )
            : undefined;
          const linkedSessionId =
            linkedArr?.target.scheduled_session_id ?? null;
          // 稳定训练身份：null = 新建；有 id = 补充/更正（同日多练各自身份）
          trainingSessionId = p.training_session_id ?? nextId("ts");
          // dev 故障注入：记录确认在写入后、提交前可注入失败并整份回滚
          if (state.dev_confirm_failure) {
            state.dev_confirm_failure = false;
            throw new Error("dev 注入失败：训练记录确认事务在写入中途失败");
          }
          // 多动作：逐动作写入展示友好 TrainingRecord（同日多练以 training_session_id 显式区分）
          for (const ex of p.exercises) {
            const cat = CATALOG.find((c) => c.id === ex.exercise_id);
            state.records.push({
              id: nextId("rec"),
              date: p.occurred_on,
              kind: p.training_session_id ? "correction" : "new",
              status,
              exercise: cat?.standard_name_zh ?? ex.exercise_id,
              variant: ex.load_notation ?? "",
              sets: ex.sets.map(setFactsToRecordSet),
              ...(ex.warmup_summary_text
                ? { warmup_summary: ex.warmup_summary_text }
                : {}),
              schedule_snapshot: null,
              scheduled_session_id: linkedSessionId,
              arrangement_revision_id: p.arrangement_revision_id ?? null,
              training_session_id: trainingSessionId,
            });
          }
        } else if (draft.kind === "plan") {
          const p = draft.payload as PlanDraftPayload;
          if (!p.plan || !p.schedules)
            return apiError(res, {
              http_status: 400,
              error_code: "invalid_request",
              message:
                "计划草稿缺少结构化载荷（计划版本／具体日程），无法确认",
            });
          const patch = p.profile_patch;
          if (patch) {
            const patchError = profilePayloadError(patch);
            if (patchError)
              return apiError(res, {
                http_status: 400,
                error_code: "invalid_request",
                message: `档案补丁无效，无法确认：${patchError}`,
              });
          }
          const nextProfile = patch
            ? profileWithPatch(state.profile, patch)
            : state.profile;
          const nextRestrictions = patch
            ? (patch.restrictions ?? []).map((r) => ({ ...r }))
            : state.restrictions;
          const invalid = planPayloadError(p, {
            profile: nextProfile,
            restrictions: nextRestrictions,
            today: MOCK_TODAY,
          });
          if (invalid)
            return apiError(res, {
              http_status: 400,
              error_code: "invalid_request",
              message: `计划草稿内容无效，无法确认：${invalid}`,
            });
          if (!nextProfile)
            return apiError(res, {
              http_status: 400,
              error_code: "invalid_request",
              message: "尚未建立正式档案，无法启用计划",
            });
          const expected = state.plan
            ? nextPlanVersion(state.plan.version)
            : "v1";
          if (p.plan.version !== expected)
            return apiError(res, {
              http_status: 400,
              error_code: "invalid_request",
              message: `计划版本须为 ${expected}（版本只追加、不原地改），收到 ${p.plan.version}`,
            });
          const committed = commitPlanDraft(state, draft, p, {
            profile: nextProfile,
            restrictions: nextRestrictions,
          });
          planVersionResult = committed.version;
          planCommit = {
            ...committed,
            summary: [
              `计划 ${committed.version} 已启用`,
              state.plan_history.length > 0
                ? `（${state.plan_history[state.plan_history.length - 1]?.version} 归档保留）`
                : "（新建）",
              `：新建日程 ${committed.created} 个`,
              patch ? "，长期档案补丁同次写入" : "",
            ].join(""),
          };
        } else if (draft.kind === "arrangement") {
          const p = draft.payload as ArrangementDraftPayload;
          const invalid = arrangementPayloadError(state, p);
          if (invalid)
            return apiError(res, {
              http_status: 400,
              error_code: "invalid_request",
              message: `安排草稿内容无效，无法确认：${invalid}`,
            });
          // dev 故障注入：安排/记录确认同样可整份回滚
          if (state.dev_confirm_failure) {
            state.dev_confirm_failure = false;
            throw new Error("dev 注入失败：安排确认事务在写入中途失败");
          }
          // 写入 arrangement_revisions（完整目标快照）；不改 plan_versions
          const revisionId = nextId("arr");
          advanceMockClock();
          state.arrangement_revisions.push({
            id: revisionId,
            target: p.target,
            accepted_at: mockNowIso(),
          });
          arrangementRevisionId = revisionId;
          // 仅 keep 升目标用力未改组数/次数时，摘要标「目标更保守」（须至少一项真的升 RIR）
          let onlyConservative = p.target.exercises.length > 0;
          let anyEffortRaised = false;
          for (const e of p.target.exercises) {
            if (e.disposition !== "keep") {
              onlyConservative = false;
              break;
            }
            const plannedEx = state.plan
              ? findWorkout(state.plan.payload, p.target.plan_workout_key)?.exercises.find(
                  (x) => x.item_key === e.item_key,
                )
              : undefined;
            if (
              !plannedEx ||
              plannedEx.prescription.kind !== "reps" ||
              e.prescription.kind !== "reps"
            ) {
              onlyConservative = false;
              break;
            }
            const pr = plannedEx.prescription;
            const ir = e.prescription;
            if (
              ir.work_sets !== pr.work_sets ||
              JSON.stringify(ir.reps_range) !== JSON.stringify(pr.reps_range)
            ) {
              onlyConservative = false;
              break;
            }
            if (
              pr.target_rir &&
              ir.target_rir &&
              (ir.target_rir.min > pr.target_rir.min ||
                ir.target_rir.max > pr.target_rir.max)
            )
              anyEffortRaised = true;
          }
          arrangementSummary =
            onlyConservative && anyEffortRaised
              ? `当次安排已确认（修订 ${revisionId}）：目标更保守，不修改长期计划，仅影响 ${p.target.scheduled_on}`
              : `当次安排已确认（修订 ${revisionId}）：不修改长期计划，仅影响 ${p.target.scheduled_on}`;
        } else {
          // 档案草稿：提交前对服务端存储的载荷做确定性复查（六类事实 + 必填体重、
          // 限制粒度）；不通过则不写任何正式数据、不递增版本
          const p = draft.payload as ProfileDraftPayload;
          const invalid = profilePayloadError(p);
          if (invalid)
            return apiError(res, {
              http_status: 400,
              error_code: "invalid_request",
              message: `档案草稿内容无效，无法确认：${invalid}`,
            });
          if (state.dev_confirm_failure) {
            state.dev_confirm_failure = false;
            throw new Error("dev 注入失败：档案确认事务在写入中途失败");
          }
          // 原子写入完整档案与当前有效限制（01 1.4：版本检查、领域复查、正式写入、
          // 版本递增、草稿状态变更同属一次提交）
          const prof = p.profile as Profile;
          const nextRestrictions = p.restrictions;
          if (nextRestrictions === undefined)
            return apiError(res, {
              http_status: 400,
              error_code: "invalid_request",
              message:
                "档案草稿内容无效，无法确认：缺少动作限制（无限制须为空列表）",
            });
          state.profile = {
            goal: prof.goal,
            experience: prof.experience,
            weekly_frequency: prof.weekly_frequency,
            session_minutes: prof.session_minutes,
            equipment: [...prof.equipment],
            body_weight_kg: prof.body_weight_kg,
            body_conditions: [...prof.body_conditions],
          };
          state.restrictions = nextRestrictions.map((r) => ({ ...r }));
        }
        // 计划草稿的正式写入、草稿提交与 context_version 递增已在 commitPlanDraft 事务内
        // 完成（失败即整份回滚）；其余草稿按 01 1.4 在同一提交内顺序写入
        if (!planCommit) {
          draft.status = "committed";
          state.context_version += 1;
        }
        recomputeStats(state);

        const committedRevision = draft.revision;
        const result: ConfirmResult = {
          draft_id: draft.id,
          status: "committed",
          newly_committed: true,
          context_version: state.context_version,
          summary:
            planCommit?.summary ??
            arrangementSummary ??
            (draft.kind === "training_record"
              ? "训练记录已写入正式数据"
              : "档案与动作限制已写入正式数据"),
          committed_revision: committedRevision,
          committed_business_version: state.context_version,
          ...(planVersionResult ? { plan_version: planVersionResult } : {}),
          ...(arrangementRevisionId
            ? { arrangement_revision_id: arrangementRevisionId }
            : {}),
          ...(trainingSessionId ? { training_session_id: trainingSessionId } : {}),
        };
        // 持久化首次确认凭据（owner 决策 B；01 1.3 提交凭据）：重复确认返回原始凭据，
        // 不再写入正式数据或递增版本
        state.confirm_receipts.set(draft.id, result);
        return json(res, 200, result);
      }

      const recalcMatch = path.match(/^\/api\/drafts\/([^/]+)\/recalc$/);
      if (recalcMatch && method === "POST") {
        const old = state.drafts.get(recalcMatch[1]);
        if (!old)
          return apiError(res, {
            http_status: 404,
            error_code: "invalid_request",
            message: "草稿不存在",
          });

        // 重算仅适用于被拦截的待确认/已过期草稿（01 1.6）；已提交/已丢弃不可重算
        if (old.status === "committed" || old.status === "discarded")
          return apiError(res, {
            http_status: 409,
            error_code: "invalid_request",
            message: "该草稿已提交或已丢弃，不可重算",
          });

        // F0-02B1：重算仅是陈旧冲突恢复——只接受「pending 且 base 已落后于当前上下文」的草稿。
        // 已重算过的 stale 草稿不可重复重算（重算动作在其新草稿上进行）；
        // 仍基于最新上下文的 pending 草稿没有重算必要，直接走确认即可
        if (old.status === "stale")
          return apiError(res, {
            http_status: 409,
            error_code: "invalid_request",
            message: "该草稿已重算过，请在新草稿上确认或丢弃",
          });
        if (
          old.status !== "pending" ||
          old.base_business_version === state.context_version
        )
          return apiError(res, {
            http_status: 409,
            error_code: "invalid_request",
            message: "该草稿仍基于最新业务上下文，无需重算",
          });

        // 以当前 MockState 派生重算草稿（01 1.6：以最新业务上下文生成；新草稿 revision 从 1 起，
        // 须再次确认）：不调用 canned 剧本回复，避免把已过期的旧提案当作最新上下文的产物
        let fresh: Draft;
        if (old.kind === "training_record") {
          const p = old.payload as RecordDraftPayload;
          fresh = {
            id: nextId("draft"),
            kind: "training_record",
            status: "pending",
            revision: 1,
            base_business_version: state.context_version,
            parent_draft_id: old.id,
            payload: { ...p },
            diff: recordDraftDiff(state, p),
          };
        } else if (old.kind === "plan") {
          const oldPayload = old.payload as PlanDraftPayload;
          const proposal = oldPayload.profile_patch
            ? dumbbellPayload(state)
            : adjustPlanProposal(state)?.payload;
          if (!proposal)
            return apiError(res, {
              http_status: 409,
              error_code: "invalid_request",
              message: "当前档案与限制下无法重算计划草稿（未通过安全前置校验）",
            });
          fresh = {
            ...pendingPlanDraft(proposal),
            base_business_version: state.context_version,
            parent_draft_id: old.id,
          };
        } else if (old.kind === "arrangement") {
          // 安排草稿：保留原目标（用户意图），diff 对比当前计划重建
          const p = old.payload as ArrangementDraftPayload;
          fresh = {
            id: nextId("draft"),
            kind: "arrangement",
            status: "pending",
            revision: 1,
            base_business_version: state.context_version,
            parent_draft_id: old.id,
            payload: { ...p },
            diff: arrangementDiff(state, p),
          };
        } else {
          const oldPayload = old.payload as ProfileDraftPayload;
          const payload = latestProfilePayload(state, old.id) ?? oldPayload;
          fresh = {
            id: nextId("draft"),
            kind: "profile_update",
            status: "pending",
            revision: 1,
            base_business_version: state.context_version,
            parent_draft_id: old.id,
            payload,
            diff: profileDraftDiff(state.profile, state.restrictions, payload),
          };
        }
        state.drafts.set(fresh.id, fresh);
        // 重算新草稿归属同一会话（sessions/:id/drafts 可恢复）
        const oldSession = state.draft_sessions.get(old.id);
        if (oldSession) state.draft_sessions.set(fresh.id, oldSession);
        old.status = "stale";

        // 新旧草稿 Diff：档案草稿按字段级展示口径对比两份载荷；其余草稿保持原 payload 键级对比
        let diff: FieldDiff[] = [];
        if (old.kind === "profile_update" && fresh.kind === "profile_update") {
          diff = profilePayloadDiff(
            old.payload as ProfileDraftPayload,
            fresh.payload as ProfileDraftPayload,
          );
        } else {
          // SAFETY: payload 只做字段级序列化对比，不当成可变记录使用；DraftPayload 联合类型的键在此处按 JSON 视图遍历
          const oldPayload = old.payload as unknown as Record<string, unknown>;
          // SAFETY: 同上，fresh.payload 为刚生成的 DraftPayload，仅用于与旧草稿做 JSON 字段对比
          const newPayload = fresh.payload as unknown as Record<
            string,
            unknown
          >;
          for (const key of Object.keys(newPayload)) {
            const a = JSON.stringify(oldPayload[key]);
            const b = JSON.stringify(newPayload[key]);
            if (a !== b)
              diff.push({
                field: `payload.${key}`,
                old_value: a ?? "—",
                new_value: b ?? "—",
              });
          }
        }

        const result: RecalcResult = {
          new_draft: fresh,
          old_draft: old,
          draft_vs_draft_diff: diff,
        };
        return json(res, 200, result);
      }

      /* 丢弃（01 1.3）：用户拒绝则丢弃待确认草稿，正式数据及业务版本不变；
         Discarded 不得再提交；仅 pending 可转 discarded（重复丢弃幂等返回） */
      const discardMatch = path.match(/^\/api\/drafts\/([^/]+)\/discard$/);
      if (discardMatch && method === "POST") {
        const draft = state.drafts.get(discardMatch[1]);
        if (!draft)
          return apiError(res, {
            http_status: 404,
            error_code: "invalid_request",
            message: "草稿不存在",
          });
        if (draft.status === "discarded")
          return json(res, 200, { draft_id: draft.id, status: "discarded" });
        if (draft.status !== "pending")
          return apiError(res, {
            http_status: 409,
            error_code: "invalid_request",
            message: "仅待确认草稿可丢弃",
          });
        draft.status = "discarded";
        return json(res, 200, { draft_id: draft.id, status: "discarded" });
      }

      return apiError(res, {
        http_status: 404,
        error_code: "invalid_request",
        message: `未知端点 ${method} ${path}`,
      });
    } catch (e) {
      return apiError(res, {
        http_status: 500,
        error_code: "invalid_request",
        message: e instanceof Error ? e.message : "mock 内部错误",
      });
    }
  };
}

/* -------------------------------- Vite 插件 -------------------------------- */

export function mockPlugin(): Plugin {
  const state = seedState();
  const hub = new SseHub();
  const handler = createHandler(state, hub);

  const attach = (middlewares: { use: (fn: typeof handler) => void }) => {
    middlewares.use(handler);
  };

  return {
    name: "fit-agent-mock-server",
    configureServer(server) {
      attach(server.middlewares);
    },
    configurePreviewServer(server) {
      attach(server.middlewares);
    },
  };
}
