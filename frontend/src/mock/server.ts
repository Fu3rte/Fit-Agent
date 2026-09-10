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
  ApiError,
  ChatMessage,
  ConfirmResult,
  Draft,
  ErrorCode,
  FieldDiff,
  PhysicalState,
  PlanBlock,
  PlanDraftPayload,
  PlanScheduleEntry,
  PlanScope,
  PlanVersion,
  PrEntry,
  Profile,
  ProfileDraftPayload,
  ProviderConfig,
  RecalcResult,
  Restriction,
  ReviewDoc,
  RunStatus,
  SessionSummary,
  StatsSummary,
  TrainingRecord,
} from "@/lib/contract";
import {
  profileFieldRows,
  profilePayloadDiff,
  restrictionLabel,
} from "../lib/profile";
import {
  PLAN_CANDIDATE,
  buildPplDraft,
  buildSchedules,
  normalizePlanPayload,
  planCandidates,
  planDraftDiff,
  planExercise,
  planPayloadError,
  reviewPlanSafety,
  weekdayLabel,
} from "./plan";

/* ---------------------------------- 状态 ---------------------------------- */

const MOCK_TODAY = "2026-09-10";
const MOCK_UPDATED_AT = "2026-09-10T08:30:00+08:00";

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
  /** 缺省 = 未收集；[] = 用户明确说明无限制（只含当前有效限制，无状态语义） */
  restrictions?: Restriction[];
  /** 缺省 = 未收集；已收集时 red_flags/notes 为明确值（[] = 用户确认无） */
  physical_state?: PhysicalState;
  /** 最近一次追问的事实：用于把「没有」解释为对该项的明确否认 */
  last_asked?: OnboardingFact;
  /** 清单外症状（未判定安全）：用户明确否认前不写入档案，也不判定为无 */
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
  /** 具体日程（04 4.4）：多版本共存，scheduled/locked/cancelled 由启用事务写入 */
  schedules: PlanScheduleEntry[];
  records: TrainingRecord[];
  stats: StatsSummary;
  review: ReviewDoc;
  sessions: SessionSummary[];
  messages: Map<string, ChatMessage[]>;
  drafts: Map<string, Draft>;
  /** 草稿归属会话（mock 内部索引；契约 Draft 本身无会话字段） */
  draft_sessions: Map<string, string>;
  /** 建档对话状态（按会话；stage1 F1-02） */
  onboarding: Map<string, OnboardingState>;
  /** 首次确认凭据（owner 决策 B；01 1.3 提交凭据）：draft_id → 原始 context_version 与 summary；
   *  重复确认返回持久化凭据，不从当前状态重建，不再写入或递增版本 */
  confirm_receipts: Map<string, { context_version: number; summary: string }>;
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

function seedState(): MockState {
  const plan: PlanVersion = {
    version: "v2",
    start_date: "2026-08-31",
    review_date: "2026-10-12",
    status: "active",
    blocks: [
      {
        name: "推日",
        weekday: 2,
        estimated_minutes: 60,
        exercises: [
          planExercise("barbell-bench-press", {
            sets: 4,
            rep_range: "6-8",
            target_rir: "1-3",
            progression: "双重渐进",
          }),
          planExercise("seated-dumbbell-shoulder-press", {
            sets: 3,
            rep_range: "8-12",
            target_rir: "1-3",
            progression: "双重渐进",
          }),
          planExercise("parallel-bar-dip", {
            sets: 3,
            rep_range: "8-12",
            target_rir: "1-3",
            progression: "次数递增",
          }),
        ],
      },
      {
        name: "拉日",
        weekday: 4,
        estimated_minutes: 60,
        exercises: [
          planExercise("pull-up", {
            sets: 3,
            rep_range: "6-10",
            target_rir: "1-3",
            progression: "次数递增",
          }),
          planExercise("barbell-bent-over-row", {
            sets: 4,
            rep_range: "8-10",
            target_rir: "1-3",
            progression: "双重渐进",
          }),
          // 原「面拉」不在已拍 24 项内，改同后束部位的目录动作（12-15 次、RIR 2-3 原样保留）
          planExercise("dumbbell-reverse-fly", {
            sets: 3,
            rep_range: "12-15",
            target_rir: "2-3",
            progression: "次数递增",
          }),
        ],
      },
      {
        name: "腿日",
        weekday: 6,
        estimated_minutes: 60,
        exercises: [
          planExercise("barbell-back-squat", {
            sets: 4,
            rep_range: "6-8",
            target_rir: "1-3",
            progression: "双重渐进",
          }),
          planExercise("barbell-romanian-deadlift", {
            sets: 3,
            rep_range: "8-10",
            target_rir: "1-3",
            progression: "双重渐进",
          }),
          planExercise("leg-extension", {
            sets: 3,
            rep_range: "12-15",
            target_rir: "1-3",
            progression: "次数递增",
          }),
        ],
      },
    ],
  };

  const records: TrainingRecord[] = [
    {
      id: "rec-001",
      date: "2026-09-01",
      kind: "correction",
      status: "formal",
      exercise: "杠铃平板卧推",
      variant: "杠铃",
      sets: [
        { weight_kg: 80, reps: 8, rir: 1, set_type: "working" },
        { weight_kg: 80, reps: 8, rir: 1, set_type: "working" },
        { weight_kg: 80, reps: 8, rir: 1, set_type: "working" },
        { weight_kg: 80, reps: 8, rir: 0, set_type: "working" },
      ],
      warmup_summary: "递增至 60kg",
      schedule_snapshot: "W1 · 推日 · PPL v2",
      revision_note: "更正：4组x8@82.5kg → 4组x8@80kg（2026-09-02 确认）",
    },
    {
      id: "rec-002",
      date: "2026-09-03",
      kind: "new",
      status: "formal",
      exercise: "杠铃俯身划船",
      variant: "杠铃",
      sets: [
        { weight_kg: 60, reps: 10, rir: 2, set_type: "working" },
        { weight_kg: 60, reps: 10, rir: 2, set_type: "working" },
        { weight_kg: 60, reps: 10, rir: 2, set_type: "working" },
      ],
      schedule_snapshot: "W1 · 拉日 · PPL v2",
    },
    {
      id: "rec-003",
      date: "2026-09-08",
      kind: "new",
      status: "formal",
      exercise: "杠铃平板卧推",
      variant: "杠铃",
      sets: [
        { weight_kg: 77.5, reps: 8, rir: 2, set_type: "working" },
        { weight_kg: 77.5, reps: 8, rir: 2, set_type: "working" },
        { weight_kg: 77.5, reps: 8, set_type: "working" },
      ],
      warmup_summary: "递增至 60kg",
      schedule_snapshot: "W2 · 推日 · PPL v2",
    },
    {
      id: "rec-004",
      date: "2026-09-09",
      kind: "new",
      status: "pending_completion",
      exercise: "坐姿哑铃肩推",
      variant: "哑铃",
      sets: [{ weight_kg: 15, reps: 10, set_type: "working" }],
      schedule_snapshot: null,
      revision_note: "额外训练，无对照安排；缺组类型确认，待补全",
    },
  ];

  const stats: StatsSummary = {
    per_week: [
      { week: "W1", planned: 3, completed: 2, rate: 66.7 },
      { week: "W2", planned: 1, completed: 1, rate: 100 },
    ],
    buckets: { met: 8, unmet: 1, pending: 1 },
    prs: [
      {
        exercise: "杠铃平板卧推",
        variant: "杠铃",
        best_weight_kg: 80,
        best_reps_at_weight: 8,
      },
      {
        exercise: "杠铃俯身划船",
        variant: "杠铃",
        best_weight_kg: 60,
        best_reps_at_weight: 10,
      },
    ],
    data_updated_at: MOCK_UPDATED_AT,
  };

  const review: ReviewDoc = {
    text: [
      "## W1 复盘（2026-08-31 ~ 2026-09-06）",
      "",
      "本周按 PPL v2 执行，完成率 **2/3（66.7%）**，腿日（09-05）漏练。",
      "",
      "- 卧推两次训练均完成 8 次目标，RIR 1，符合渐进预期。",
      "- 09-01 第 4 组 RIR 0，未符合目标区间（1-3），建议下周维持 80kg 观察一组。",
      "- 划船 60kg x10 全部符合，下周可尝试 62.5kg。",
      "",
      "| 组级判定 | 组数 |",
      "| --- | --- |",
      "| 符合目标 | 8 |",
      "| 未符合 | 1 |",
      "| 待补全 | 1 |",
      "",
      "> 复盘仅解释确定性统计结果；不修改数值，也不补充没有数据支持的因果结论。",
    ].join("\n"),
    stale: true,
    generated_at: "2026-09-07T21:00:00+08:00",
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

  return {
    context_version: 3,
    provider,
    profile: {
      goal: "增肌（肌肥大）",
      experience: "初级（有少量训练经验）",
      weekly_frequency: 3,
      session_minutes: 60,
      equipment: ["杠铃", "哑铃", "卧推架", "引体架", "绳索"],
      body_weight_kg: 72.5,
      physical_state: {
        red_flags: [],
        notes: ["肩部偶有不适（颈后推举时明显）"],
      },
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
    /** 种子计划的日程（F2-04）：mock 当前日期之前到期即锁（04 4.4），当日及之后为 scheduled */
    schedules: seedSchedules(plan),
    records,
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
}

/**
 * 种子计划的日程（F2-04；04 4.4 到期即锁）：mock 当前日期之前到期即锁（locked），
 * 当日及之后为应训练（scheduled）。种子只给状态，排程仍由 ./plan 给出（不写第二份算法）。
 */
function seedSchedules(plan: PlanVersion): PlanScheduleEntry[] {
  const scope: PlanScope = {
    start_date: plan.start_date,
    review_date: plan.review_date,
    weekdays: [...new Set(plan.blocks.map((b) => b.weekday))].sort(
      (a, b) => a - b,
    ),
  };
  return buildSchedules(plan.version, scope).map((s) =>
    s.date < MOCK_TODAY ? { ...s, status: "locked" as const } : s,
  );
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
    physical_state: { red_flags: [], notes: [] },
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

function recordScriptReply(): { text: string; draft: Draft } {
  const draft: Draft = {
    id: nextId("draft"),
    kind: "training_record",
    status: "pending",
    revision: 1, // 初版修订（01 1.3：标识用户所见内容版本）
    base_business_version: 0, // 占位；由 runScript 在生成处按当时读到的 context_version 填充（01 1.3）
    payload: {
      date: MOCK_TODAY,
      exercise: "杠铃平板卧推",
      variant: "杠铃",
      warmup_summary: "递增至 60kg",
      sets: [
        { weight_kg: 80, reps: 8, rir: 1, set_type: "working" },
        { weight_kg: 80, reps: 8, rir: 1, set_type: "working" },
        { weight_kg: 80, reps: 8, rir: 1, set_type: "working" },
        { weight_kg: 80, reps: 8, set_type: "working" },
      ],
    },
    diff: [
      { field: "训练记录 · 日期", new_value: MOCK_TODAY },
      {
        field: "杠铃平板卧推 · 工作组",
        new_value: "4 组 x 8 次 @ 80kg，目标 RIR 1-3",
      },
      { field: "第 4 组 · RIR", new_value: "未报告（待确认）" },
      { field: "热身", new_value: "递增至 60kg（摘要）" },
    ],
  };
  return {
    text: "已将打卡内容整理为训练记录草稿：**杠铃平板卧推 4 组 x 8 次 @ 80kg**，第 4 组 RIR 未报告（保持为空，不自行补造）。\n\n请核对下方草稿卡，可内联纠错关键字段，确认采纳后才写入正式记录。",
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
 * 待确认计划草稿（01 1.2/1.3）：revision 从 1 起（标识用户所见内容版本），
 * base_business_version 由 runScript 在生成处填充为当时读到的 context_version
 */
function pendingPlanDraft(payload: PlanDraftPayload): Draft {
  return {
    id: nextId("draft"),
    kind: "plan_adjust",
    status: "pending",
    revision: 1,
    base_business_version: 0,
    payload,
    diff: payload.diff,
  };
}

/**
 * 结构化替换草稿载荷（F2-04；PRD 5.3 只追加不原地改、04 4.4 旧版未来未锁定日程取消）：
 * 拟议板块来自计划生成或处方改写；版本取当前计划下一版（无正式计划时 v1），生效范围取
 * 3.3 已拍候选日期，新日程按新版本重建，旧版未来未锁定日程进取消清单（已锁定日程不动）。
 * 产出后仍按 `planPayloadError` 复检，不通过即不产出草稿（fail-closed）。
 */
function replacementPayload(
  state: MockState,
  input: {
    profile: Profile;
    blocks: PlanBlock[];
    title: string;
    extra_diff?: FieldDiff[];
  },
): PlanDraftPayload | undefined {
  const { profile, blocks } = input;
  const previous = state.plan;
  const scope: PlanScope = {
    start_date: PLAN_CANDIDATE.start_date,
    review_date: PLAN_CANDIDATE.review_date,
    weekdays: [...new Set(blocks.map((b) => b.weekday))].sort((a, b) => a - b),
  };
  const version = previous ? nextPlanVersion(previous.version) : "v1";
  const schedules = buildSchedules(version, scope);
  const cancellations = previous
    ? state.schedules.filter(
        (s) =>
          s.plan_version === previous.version &&
          s.status === "scheduled" &&
          s.date >= MOCK_TODAY,
      )
    : [];
  const plan: PlanVersion = {
    version,
    start_date: scope.start_date,
    review_date: scope.review_date,
    status: "active",
    blocks,
  };
  const payload: PlanDraftPayload = {
    title: input.title,
    diff: [
      ...planDraftDiff(
        plan,
        scope,
        schedules,
        previous ? { previous_version: previous.version } : undefined,
      ),
      ...(input.extra_diff ?? []),
    ],
    plan,
    scope,
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
    physical_state: p.physical_state
      ? {
          red_flags: [...p.physical_state.red_flags],
          notes: [...p.physical_state.notes],
        }
      : {
          red_flags: [...current.physical_state.red_flags],
          notes: [...current.physical_state.notes],
        },
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
    // 3) 旧版未来未锁定日程取消，已锁定日程不动（04 4.4）
    const cancellable = new Set(
      state.schedules
        .filter(
          (s) =>
            s.plan_version === previous?.version &&
            s.status === "scheduled" &&
            s.date >= MOCK_TODAY,
        )
        .map((s) => s.id),
    );
    state.schedules = [
      ...state.schedules.map((s) =>
        cancellable.has(s.id) ? { ...s, status: "cancelled" as const } : s,
      ),
      // 4) 新版本日程（服务端在草稿生成时已按生效范围算好，这里只归属到新版本）
      ...schedules.map((s) => ({
        ...s,
        plan_version: plan.version,
        status: "scheduled" as const,
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
        blocks: built.plan.blocks,
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
 * 处方改写提案（存量演示剧本：推日减量）：按 3.1 可推荐目录从当前档案重建板块，只改组数与
 * 目标 RIR；版本、日程与取消清单由 `replacementPayload` 按当前正式计划派生。
 * `rows` = 实际落地的处方改写展示行（动作不在拟议计划内时不虚报变更）。
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
  const edits: Record<string, { sets?: number; target_rir?: string }> = {
    "barbell-bench-press": { sets: 3, target_rir: "2-3" },
    "seated-dumbbell-shoulder-press": { sets: 2 },
  };
  const rows: FieldDiff[] = [];
  const blocks: PlanBlock[] = built.plan.blocks.map((b) => ({
    ...b,
    exercises: b.exercises.map((e) => {
      const edit = edits[e.exercise_id];
      if (!edit) return e;
      if (edit.sets !== undefined && edit.sets !== e.sets)
        rows.push({
          field: `${e.name} · 组数`,
          old_value: `${e.sets} 组`,
          new_value: `${edit.sets} 组`,
        });
      if (edit.target_rir !== undefined && edit.target_rir !== e.target_rir)
        rows.push({
          field: `${e.name} · 目标 RIR`,
          old_value: e.target_rir,
          new_value: edit.target_rir,
        });
      return {
        ...e,
        sets: edit.sets ?? e.sets,
        target_rir: edit.target_rir ?? e.target_rir,
      };
    }),
  }));
  const payload = replacementPayload(state, {
    profile,
    blocks,
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
 * 给出处方前先用最新红旗与限制复核整份计划（04 4.5）：
 * - 红旗症状独立阻断：建议线下专业评估，不给任何常规处方；
 * - 任一动作命中具体动作／动作模式限制：整份阻断，**不**只跳过冲突动作继续给其余动作的处方。
 * 两种阻断都只影响「使用时」：当前计划与其当前日程仍可在 /profile 查看，修订从对话发起。
 */
function planGuidanceReply(state: MockState): string {
  /* 红旗独立阻断（02 2.3）：与有没有计划无关，先于计划检查——不生成、也不给出任何常规处方 */
  const redFlags = state.profile?.physical_state.red_flags ?? [];
  if (redFlags.length > 0)
    return [
      professionalEvalBlock(redFlags),
      "当前档案含红旗症状，本次不给出任何基于计划的训练指导。请先完成线下专业评估；经专业人员确认可以恢复训练后，再从对话调整档案与计划。",
    ].join("\n\n");

  const plan = state.plan;
  if (!plan) return NO_PLAN_GUIDANCE_REPLY;
  const safety = reviewPlanSafety({
    plan,
    profile: state.profile,
    restrictions: state.restrictions,
    context_version: state.context_version,
  });

  /* 红旗已在上面拦截，这里的不可用只剩限制冲突（仍按整份阻断，不降级为逐动作跳过） */
  if (!safety.usable) {
    return [
      `按最新限制复核当前计划 ${plan.version}：**整份计划指导已阻断**——任一动作命中限制时，我不会给出任何基于该计划的处方，也不会只跳过冲突动作、继续给其余「未冲突」动作的处方。`,
      "",
      ...safety.conflicts.map(
        (c) =>
          `- 冲突：${c.exercise_name} 命中限制「${c.restriction.name}」（${c.restriction.scope === "specific_action" ? "具体动作" : "动作模式"}）`,
      ),
      "",
      `正式计划 ${plan.version} 与它的当前日程仍可在「档案与限制」页查看（不隐藏、不改写，也不标为「部分可用」）。`,
      "修改计划只能从对话发起：说明你要调整的内容，我给出修订草稿，确认后生成新版本。",
    ].join("\n");
  }

  const upcoming = state.schedules
    .filter(
      (s) =>
        s.plan_version === plan.version &&
        s.status === "scheduled" &&
        s.date >= MOCK_TODAY,
    )
    .sort((a, b) => a.date.localeCompare(b.date))[0];
  const block = plan.blocks.find((b) => b.weekday === upcoming?.weekday);
  if (!upcoming || !block)
    return [
      `当前计划 ${plan.version} 通过最新红旗与限制复核，但区间内没有未到期的应训练日（${plan.start_date} ~ ${plan.review_date}）。`,
      "复核日后的续期与跨周期切换不在本阶段范围内；如需新计划请从对话发起。",
    ].join("\n");

  const calibration = block.exercises[0]?.calibration;
  return [
    `下一个应训练日：${upcoming.date}（${weekdayLabel(block.weekday)}）· ${block.name} · 预计 ${block.estimated_minutes} 分钟（计划 ${plan.version}）`,
    "",
    ...block.exercises.map(
      (e) =>
        `- ${e.name} ${e.sets} 组 x ${e.rep_range} 次，目标 RIR ${e.target_rir}（${e.progression}）`,
    ),
    "",
    "负荷：没有可信训练记录，不给出具体起始重量，按各动作的「需要校准」步骤逐级试重。",
    calibration
      ? `- 通过标准：${calibration.pass_criteria}；停止条件：${calibration.stop_criteria}`
      : "",
    "",
    "该指导依据当前计划与最新安全复核；如出现疼痛或红旗症状请立即停止并按线下专业评估处理。",
  ]
    .filter((line) => line !== "")
    .join("\n");
}

/**
 * F2-02：从正式档案与当前有效限制生成 PPL 计划草稿（新建 v1）。
 * 缺档案、档案含红旗症状或排不进档案约束时不给任何处方，只说明原因与下一步。
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
          "当前档案已记录红旗症状，因此我不会生成任何计划处方。请先完成线下专业评估；经专业人员确认可以恢复训练后，再回来调整档案与计划。",
        ].join("\n\n"),
      };
    if (built.code === "no_profile")
      return {
        text: [
          "还没有正式档案，我不会凭空生成计划处方。",
          "请在对话中补齐建档信息（目标、经验、每周频率、单次时长、可用器械、体重、动作限制与当前身体状态），确认后再生成计划。",
        ].join("\n\n"),
      };
    return {
      text: [
        `当前条件排不出符合档案约束的 PPL 计划：${built.reason}。`,
        "未生成任何计划草稿；请调整档案或说明可用日期后重试。",
      ].join("\n\n"),
    };
  }

  const { plan, scope, schedules } = built;
  const first = schedules[0];
  const last = schedules[schedules.length - 1];
  const rows = plan.blocks.map(
    (b) =>
      `- ${b.name}（${weekdayLabel(b.weekday)}，预计 ${b.estimated_minutes} 分钟）：${b.exercises
        .map(
          (e) =>
            `${e.name} ${e.sets} 组 x ${e.rep_range} 次，目标 RIR ${e.target_rir}`,
        )
        .join("；")}`,
  );
  const draft: Draft = {
    id: nextId("draft"),
    kind: "plan_adjust",
    status: "pending",
    revision: 1, // 初版修订（01 1.3：标识用户所见内容版本）
    base_business_version: 0, // 占位；由 runScript 在生成处按当时读到的 context_version 填充（01 1.3）
    payload: built.payload,
    diff: built.payload.diff,
  };
  return {
    text: [
      `已按正式档案生成 PPL 计划草稿（新建 ${plan.version}）：`,
      "",
      `- 生效范围：${scope.start_date} 起，复核日期 ${scope.review_date}，每周 ${scope.weekdays.map(weekdayLabel).join(" / ")}`,
      `- 具体日程：${first.date} ~ ${last.date} 共 ${schedules.length} 个应训练日（复核日当天不排日程）`,
      ...rows,
      "",
      "负荷：当前没有可信训练记录，我不会给出任何起始重量；每个动作按「需要校准」处理（逐级试重，稳定完成处方次数下限且落在目标 RIR 区间才算通过）。",
      "",
      "请在草稿卡核对后确认；确认前正式计划与日程不变。",
    ].join("\n"),
    draft,
  };
}

/** 纠错后训练记录草稿的 diff 再生成（01 1.2：展示结果和 Diff 随纠错更新；从纠错后 payload 派生） */
function recordDraftDiff(
  p: Extract<Draft["payload"], { date: string }>,
): FieldDiff[] {
  const working = p.sets.filter((s) => s.set_type === "working");
  const first = working[0];
  const rows: FieldDiff[] = [
    { field: "训练记录 · 日期", new_value: p.date },
    {
      field: `${p.exercise} · 工作组`,
      new_value: `${working.length} 组 x ${first?.reps ?? "?"} 次 @ ${first?.weight_kg ?? "?"}kg`,
    },
  ];
  working.forEach((s, i) => {
    rows.push({
      field: `第 ${i + 1} 组 · RIR`,
      new_value: s.rir === undefined ? "未报告（待确认）" : String(s.rir),
    });
  });
  if (p.warmup_summary)
    rows.push({ field: "热身", new_value: `${p.warmup_summary}（摘要）` });
  return rows;
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
  | "physical_state";

const ONBOARDING_FACTS: readonly OnboardingFact[] = [
  "goal",
  "experience",
  "weekly_frequency",
  "session_minutes",
  "equipment",
  "body_weight_kg",
  "restrictions",
  "physical_state",
];

const FACT_QUESTIONS: Record<OnboardingFact, string> = {
  goal: "你的训练目标是什么？（增肌 / 力量 / 整体健康）",
  experience:
    "你的训练经验大概到什么程度？（零基础 / 有一点基础 / 稳定训练过一段时间）",
  weekly_frequency: "每周计划训练几次？",
  session_minutes: "每次训练大约能安排多少分钟？",
  equipment:
    "可用器械有哪些？（如杠铃、哑铃、卧推架、引体架、绳索；没有器械也请直接说明）",
  body_weight_kg: "当前体重是多少公斤？（建档必填，我不会替你填默认值）",
  restrictions:
    "有没有已知的动作限制？可以说具体动作（如「颈后推举肩部不适」）或动作模式（如「深蹲膝部不适」）；没有请明确说「没有」。",
  physical_state:
    "当前身体状态如何？有没有胸部异常不适、晕厥、异常气短、锐痛、麻木、放射痛这类需要线下专业评估的情况？没有也请明确说明。",
};

/**
 * 阶段能力开关：训练打卡（记录写入）属阶段 3（plans/business-roadmap.md）。
 * 阶段 1 的 mock 不开放打卡：plans/stage1.md F1-02 要求仅说明该能力不在本阶段开放、
 * 不生成训练记录草稿，也不宣称建立计划是记录训练的业务前提。Stage 0 记录剧本保留在
 * 开关之后，待阶段 3 接线时替换为真实记录流程。
 */
const CHECKIN_ENABLED = false;

const CHECKIN_UNAVAILABLE_REPLY = [
  "训练打卡（写入训练记录）不在本阶段开放，我不会生成训练记录草稿。",
  "",
  "本阶段对话只用于建档；训练记录与统计在后续阶段接入，建档本身不受影响——需要的话我们继续把档案补齐。",
].join("\n");

/** 打卡类请求（阶段 1 仅识别这些演示短语，不解析自然语言意图） */
const CHECKIN_PATTERN =
  /打卡|训练记录|记录一下|记一下|帮我记|今天练|昨天练|今天做|昨天做|\d+\s*(?:kg|公斤)\D{0,6}\d+\s*组|\d+\s*组\s*[x×]?\s*\d+\s*次/;

/** 用户明确否认的表达（仅用于把「没有」解释为对上一问的明确否认） */
const DENIAL_PATTERN = /没有|没|无|不用|不需要|一切正常|都正常|没问题/;

/**
 * 明确红旗症状（02 2.3 清单）。plans/stage1.md §8 已拍：清单外症状继续澄清、
 * 不判定安全，不自行扩充医学规则；Agent 不诊断、不解除红旗。
 */
const RED_FLAG_PATTERNS: readonly { label: string; pattern: RegExp }[] = [
  { label: "胸部异常不适", pattern: /胸部(?:异常|明显|持续)(?:不适|闷|痛)/ },
  { label: "晕厥", pattern: /晕厥|昏厥|晕倒/ },
  { label: "异常气短", pattern: /气短|喘不上气|呼吸困难/ },
  { label: "锐痛", pattern: /锐痛|刺痛/ },
  { label: "麻木", pattern: /麻木|发麻/ },
  { label: "放射痛", pattern: /放射痛|放射到|放射至/ },
];

/** 症状标记（用于识别清单外症状 → 继续澄清，不判定安全） */
const SYMPTOM_MARKER =
  /不适|不舒服|疼痛|疼|痛|发麻|麻木|发酸|酸胀|发紧|头晕|晕|气短|胸闷|受伤|肿/;

/** 已知限制对象（阶段 1 演示短语表；不在表内不做限制解析） */
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
  red_flags: string[];
  /** 用户明确说明无红旗/无不适 */
  physical_none: boolean;
  unlisted_symptoms: string[];
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

/** 明确红旗识别：否定前缀（如「没有胸部异常不适」）不算报告红旗 */
function detectRedFlags(message: string): string[] {
  const found: string[] = [];
  for (const { label, pattern } of RED_FLAG_PATTERNS) {
    const re = new RegExp(pattern.source, "g");
    let m: RegExpExecArray | null;
    while ((m = re.exec(message)) !== null) {
      const before = message.slice(Math.max(0, m.index - 6), m.index);
      if (!/(没有|没|无|不|未)/.test(before)) {
        if (!found.includes(label)) found.push(label);
        break;
      }
    }
  }
  return found;
}

/** 清单外症状：只做澄清，不判定安全（plans/stage1.md §8） */
function unlistedSymptoms(message: string): string[] {
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

/** 限制解析：已知限制对象 + 同一子句内不适/受限标记（两种粒度；不引入限制状态语义） */
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
    red_flags: [],
    physical_none: false,
    unlisted_symptoms: [],
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

  out.red_flags = detectRedFlags(text);
  if (out.red_flags.length > 0) out.touched = true;
  // 明确红旗消息里的不适归入红旗，不再重复解析为动作限制（避免把症状误报成限制）
  if (out.red_flags.length === 0) {
    out.restrictions = parseRestrictions(text);
    if (out.restrictions.length > 0) out.touched = true;
  }
  if (
    /没有(?:任何)?(?:动作)?限制|无(?:动作)?限制|没有(?:动作)?受限|都能练|都可以练|没有不能做/.test(
      text,
    )
  ) {
    out.restrictions_none = true;
    out.touched = true;
  }

  if (
    out.red_flags.length === 0 &&
    /没有(?:这些|其他)?(?:情况|问题|症状|不适|异常)|无不适|无异常|一切正常|都正常|没有问题/.test(
      text,
    )
  ) {
    out.physical_none = true;
    out.touched = true;
  }
  // 限制相关不适归入限制，不重复当清单外症状；其余症状只做澄清，不判定安全
  if (
    out.red_flags.length === 0 &&
    out.restrictions.length === 0 &&
    !out.physical_none
  ) {
    out.unlisted_symptoms = unlistedSymptoms(text);
    if (out.unlisted_symptoms.length > 0) out.touched = true;
  }

  return out;
}

function professionalEvalBlock(flags: string[]): string {
  return [
    `> ⚠️ 你报告的「${flags.join("、")}」属于需要专业评估的情况：建议尽快线下就医并由专业人员评估。`,
    "> 我不会在此基础上给出任何训练建议；该症状已记入待生成的档案内容（红旗症状字段）。Agent 不诊断，也不解除红旗。",
  ].join("\n");
}

function unlistedSymptomBlock(symptoms: string[]): string {
  return [
    `你提到的「${symptoms.join("；")}」不在我能判定的明确红旗清单内（本阶段只识别正本明确列出的症状），因此我不会判断它是否安全，也不会把它写成「无」。`,
    "请补充：具体部位、在什么动作或场景下出现、持续多久、是否影响日常；如持续或加重，建议线下专业评估。",
  ].join("\n");
}

/** 扫描并在会话建档状态中记录明确红旗（不覆盖已记录的红旗）；返回本次报告的红旗 */
function scanRedFlags(
  state: MockState,
  sessionId: string,
  message: string,
): string[] {
  const flags = detectRedFlags(message);
  if (flags.length === 0) return [];
  const ob = onboardingOf(state, sessionId);
  ob.physical_state = {
    red_flags: [
      ...new Set([...(ob.physical_state?.red_flags ?? []), ...flags]),
    ],
    notes: ob.physical_state?.notes ?? [],
  };
  ob.pending_symptoms = [];
  return flags;
}

function missingFacts(ob: OnboardingState): OnboardingFact[] {
  return ONBOARDING_FACTS.filter((f) => {
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
      case "restrictions":
        return ob.restrictions === undefined;
      case "physical_state":
        return ob.physical_state === undefined;
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
  if (fact === "restrictions" && ob.restrictions === undefined) {
    ob.restrictions = [];
    ack.push("动作限制：无（用户明确说明）");
    return;
  }
  if (
    fact === "physical_state" &&
    (ob.physical_state?.red_flags.length ?? 0) === 0
  ) {
    ob.physical_state = { red_flags: [], notes: [] };
    ob.pending_symptoms = [];
    ack.push("当前身体状态：无明确红旗（用户确认）");
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
  // 已明确红旗不被后到的「无红旗」覆盖（02 2.3；backend S1-05 同规则）
  if (
    parsed.physical_none &&
    (ob.physical_state?.red_flags.length ?? 0) === 0
  ) {
    ob.physical_state = { red_flags: [], notes: [] };
    ob.pending_symptoms = [];
    ack.push("当前身体状态：无明确红旗（用户确认）");
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
    ob.physical_state === undefined
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
      physical_state: ob.physical_state,
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
  const physical = prof.physical_state;
  if (typeof physical !== "object" || physical === null)
    return "缺少当前身体状态";
  const ps = physical as Record<string, unknown>;
  for (const [key, label] of [
    ["red_flags", "红旗症状"],
    ["notes", "其他描述"],
  ] as const) {
    const list = ps[key];
    if (
      !Array.isArray(list) ||
      list.some((s) => typeof s !== "string" || s.trim() === "")
    )
      return `当前身体状态 · ${label}须为非空字符串列表`;
  }
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
 * 一轮建档回复：记录本轮事实 → 红旗/清单外症状分支 → 追问下一项缺失事实或出稿。
 * 缺失事实继续追问、不编造；事实齐备时只生成一份 profile_update 草稿（结构化载荷 +
 * 服务端派生 Diff），同一基线不并存竞争草稿（plans/stage1.md F1-02）。
 */
function onboardingTurn(
  state: MockState,
  sessionId: string,
  message: string,
  parsed: ParsedFacts,
  newRedFlags: string[],
): { text: string; draft?: Draft } {
  const ob = onboardingOf(state, sessionId);
  const ack: string[] = [];
  const blocks: string[] = [];

  if (newRedFlags.length > 0) {
    ack.push(`当前身体状态 · 红旗症状：${newRedFlags.join("、")}`);
    blocks.push(professionalEvalBlock(newRedFlags));
  }

  applyFacts(ob, parsed, message, ack);

  if (parsed.unlisted_symptoms.length > 0) {
    ob.pending_symptoms = [
      ...new Set([...ob.pending_symptoms, ...parsed.unlisted_symptoms]),
    ];
    // 未判定安全的症状不写成「无」；但已记录的红旗事实不得被后到的症状清空
    // （02 2.3；与 applyFacts 中「已明确红旗不被后到的无红旗覆盖」同规则）
    if ((ob.physical_state?.red_flags.length ?? 0) === 0)
      ob.physical_state = undefined;
    blocks.push(unlistedSymptomBlock(parsed.unlisted_symptoms));
  }

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
  "- 完成率：W1 **2/3（66.7%）**，W2 **1/1（100%）**；漏练保留，不做补课。",
  "- 组级判定：符合目标 8 组 / 未符合 1 组 / 待补全 1 组。",
  "- PR：杠铃平板卧推 **80kg x8**；杠铃俯身划船 **60kg x10**。",
  "",
  "| 计划周 | 应训练 | 已完成 | 完成率 |",
  "| --- | --- | --- | --- |",
  "| W1 | 3 | 2 | 66.7% |",
  "| W2 | 1 | 1 | 100% |",
  "",
  "> 建议下一步：腿日恢复训练前先做接回评估；卧推维持 80kg 观察 RIR。",
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
  "请在对话中补齐建档信息（目标、经验、每周频率、单次时长、可用器械、体重、动作限制与当前身体状态），确认后再生成计划。",
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
  "- 建档：直接给出目标、经验、每周频率、单次时长、可用器械、体重、动作限制与当前身体状态",
  "- 请求训练指导：如「给我周三的训练指导」（按最新红旗与限制复核整份计划）",
  "- 调整计划：如「最近很累，帮我调整计划」",
  "- 生成复盘：如「给我看一下复盘」",
  "",
  "训练打卡不在本阶段开放；所有业务变更都会先以草稿卡展示，确认后才写入。",
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

  // 明确红旗症状优先扫描：任何意图分支都不得吞掉它（plans/stage1.md F1-02）
  const newRedFlags = scanRedFlags(state, run.session_id, message);
  const parsed = parseFacts(message);
  const isGuidance = GUIDANCE_PATTERN.test(message);
  // 「今天练什么」这类问句会同时命中打卡短语：指导意图优先，否则会把计划指导请求当成打卡
  // （打卡类短语仍归记录分支，如「今天练了卧推 80kg 3 组」）
  const isCheckIn = !isGuidance && CHECKIN_PATTERN.test(message);
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
    if (CHECKIN_ENABLED) {
      const r = recordScriptReply();
      text = r.text;
      draft = r.draft;
    } else {
      text = CHECKIN_UNAVAILABLE_REPLY;
    }
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
      newRedFlags,
    );
    text = r.text;
    draft = r.draft;
  } else if (isGuidance) {
    text = planGuidanceReply(state);
  } else if (isPlan) {
    const r = planScriptReply(state);
    text = r.text;
    draft = r.draft;
  } else if (isReview) {
    text = REVIEW_REPLY;
  } else {
    text = GENERIC_REPLY;
  }

  if (newRedFlags.length > 0 && !isOnboarding) {
    // 明确红旗：立即给出专业评估措辞，且不生成训练类草稿（plans/stage1.md F1-02）
    if (draft && draft.kind !== "profile_update") {
      draft = undefined;
      text =
        "你报告的症状需要先线下专业评估；本次不生成训练建议或计划调整草稿。";
    }
    text = [professionalEvalBlock(newRedFlags), text].join("\n\n");
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
      /** 具体日程全量快照（F2-04：原子切换后可核对取消／锁定与新日程） */
      schedules: state.schedules.map((s) => ({
        id: s.id,
        plan_version: s.plan_version,
        date: s.date,
        status: s.status,
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

function recomputeStats(state: MockState): void {
  const buckets = { met: 0, unmet: 0, pending: 0 };
  const prMap = new Map<string, PrEntry>();

  // 处方区间以计划版本中该动作的为准（不另设隐藏容差）；找不到时退回通用区间
  const prescriptionOf = (exercise: string) => {
    const ex = (state.plan?.blocks ?? [])
      .flatMap((b) => b.exercises)
      .find((e) => e.name === exercise);
    const rir = ex?.target_rir.match(/(\d+)-(\d+)/);
    const reps = ex?.rep_range.match(/(\d+)-(\d+)/);
    return {
      rirMin: rir ? Number(rir[1]) : 1,
      rirMax: rir ? Number(rir[2]) : 3,
      repMin: reps ? Number(reps[1]) : 6,
      repMax: reps ? Number(reps[2]) : 8,
    };
  };

  for (const rec of state.records) {
    if (rec.status !== "formal" || !rec.schedule_snapshot) continue;
    const p = prescriptionOf(rec.exercise);
    for (const set of rec.sets) {
      if (set.set_type !== "working") continue;
      if (set.rir === undefined) buckets.pending += 1;
      else if (
        set.rir >= p.rirMin &&
        set.rir <= p.rirMax &&
        set.reps !== undefined &&
        set.reps >= p.repMin &&
        set.reps <= p.repMax
      )
        buckets.met += 1;
      else buckets.unmet += 1;
    }
    // PR：正式有效记录中可比较的独立完成工作组（辅助组不参与；mock 数据无辅助组）
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

  state.stats.buckets = buckets;
  state.stats.prs = [...prMap.values()].sort((a, b) =>
    a.exercise.localeCompare(b.exercise),
  );
  state.stats.data_updated_at = new Date().toISOString();
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
        // payload 形状由草稿 kind 决定（D1A 单一形状源）：不匹配则拒绝，
        // 避免整份替换后 draft.diff 派生到错误形状上
        const payloadMatchesKind =
          draft.kind === "training_record"
            ? "sets" in body.payload
            : draft.kind === "plan_adjust"
              ? "title" in body.payload && "diff" in body.payload
              : "profile" in body.payload;
        if (!payloadMatchesKind)
          return apiError(res, {
            http_status: 400,
            error_code: "invalid_request",
            message: `payload 形状与草稿类型不匹配：${draft.kind}`,
          });

        // 展示与 Diff 随纠错更新（01 1.2）：diff 一律由服务端派生，不信任客户端提交值
        // （记录：从 payload 派生；计划：diff 属于计划提案本体；档案：对比当前正式档案派生）
        if (draft.kind === "training_record") {
          draft.payload = body.payload;
          draft.diff = recordDraftDiff(
            body.payload as Extract<Draft["payload"], { date: string }>,
          );
        } else if (draft.kind === "plan_adjust") {
          // 计划草稿：以服务端存储的草稿为基准，只取客户端提交的允许纠错字段；身份／时长／
          // 日程／Diff／版本／状态／取消清单一律由服务端重建或保留（F2-03：结构化内容与 Diff 同步）
          // 复合草稿（带长期档案补丁）的纠错同样按「补丁 + 当前正式档案」复检，与确认事务同一上下文
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
        } else {
          // 档案草稿：确定性校验结构（六类事实 + 必填体重、限制粒度）后整份替换，
          // Diff 由服务端对比当前正式档案重新派生（不信任客户端提交值）
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
        if (draft.kind === "training_record") {
          const p = draft.payload as Extract<
            Draft["payload"],
            { date: string }
          >;
          state.records.push({
            id: nextId("rec"),
            date: p.date,
            kind: "new",
            status: "formal",
            exercise: p.exercise,
            variant: p.variant,
            sets: p.sets,
            warmup_summary: p.warmup_summary,
            schedule_snapshot: null,
          });
        } else if (draft.kind === "plan_adjust") {
          const p = draft.payload as PlanDraftPayload;
          if (!p.plan || !p.scope || !p.schedules)
            return apiError(res, {
              http_status: 400,
              error_code: "invalid_request",
              message:
                "计划草稿缺少结构化载荷（计划版本／生效范围／具体日程），无法确认",
            });
          // 长期档案补丁：与计划同属一次确认（01 1.4 受限组合草稿）；补丁形状先用档案载荷口径把关
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
          // 领域复查（01 1.4）：以草稿载荷与长期档案补丁合并后的正式上下文复检整份计划
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
          // 版本只追加不原地改（PRD 5.3）：无正式计划时新建 v1，替换时为当前版本的下一版
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
            physical_state: {
              red_flags: [...prof.physical_state.red_flags],
              notes: [...prof.physical_state.notes],
            },
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

        const result: ConfirmResult = {
          draft_id: draft.id,
          status: "committed",
          newly_committed: true,
          context_version: state.context_version,
          summary:
            planCommit?.summary ??
            (draft.kind === "training_record"
              ? "训练记录已写入正式数据"
              : "档案与动作限制已写入正式数据"),
        };
        // 持久化首次确认凭据（owner 决策 B；01 1.3 提交凭据）：重复确认返回原始 context_version 与 summary，
        // 不再写入正式数据或递增版本
        state.confirm_receipts.set(draft.id, {
          context_version: result.context_version,
          summary: result.summary,
        });
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
          // 记录草稿：保留原草稿内容（用户意图，不虚构无关正式状态），
          // 展示 diff 从 payload 派生（与 revise 同一渲染）
          const p = old.payload as Extract<Draft["payload"], { date: string }>;
          fresh = {
            id: nextId("draft"),
            kind: "training_record",
            status: "pending",
            revision: 1,
            base_business_version: state.context_version,
            parent_draft_id: old.id,
            payload: { ...p },
            diff: recordDraftDiff(p),
          };
        } else if (old.kind === "plan_adjust") {
          // 计划草稿：以最新业务上下文重新派生同一类提案（01 1.6）——器械范围调整草稿按
          // 补丁重算，其余按当前计划生成结构化替换提案；不把已过期的旧提案当最新上下文的产物
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
        } else {
          // 档案草稿：按会话最新收集事实重建提案（01 1.6「以最新业务上下文生成新草稿」）；
          // 拿不到最新事实时保留旧草稿内容（用户意图，不虚构），Diff 一律对比当前正式档案派生
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
