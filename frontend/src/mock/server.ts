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
  PrEntry,
  ProviderConfig,
  RecalcResult,
  Restriction,
  ReviewDoc,
  RunStatus,
  SessionSummary,
  StatsSummary,
  TrainingRecord,
} from "@/lib/contract";

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

interface MockState {
  context_version: number;
  provider: ProviderConfig;
  profile: {
    goal: string;
    experience: string;
    weekly_frequency: number;
    session_minutes: number;
    equipment: string[];
    body_weight_kg: number;
  };
  restrictions: Restriction[];
  plan: PlanState;
  records: TrainingRecord[];
  stats: StatsSummary;
  review: ReviewDoc;
  sessions: SessionSummary[];
  messages: Map<string, ChatMessage[]>;
  drafts: Map<string, Draft>;
  /** 草稿归属会话（mock 内部索引；契约 Draft 本身无会话字段） */
  draft_sessions: Map<string, string>;
  /** 首次确认凭据（owner 决策 B；01 1.3 提交凭据）：draft_id → 原始 context_version 与 summary；
   *  重复确认返回持久化凭据，不从当前状态重建，不再写入或递增版本 */
  confirm_receipts: Map<string, { context_version: number; summary: string }>;
  runs: Map<string, RunState>;
  /** 唯一执行名额（08 8.3）：由正在执行的 Run 持有，runScript 实际退出（finally）后才释放；
   *  与 Run 状态解耦——取消立即置 cancelled，名额不提前释放。非权威状态，仅作并发互斥。 */
  execution_slot_run_id: string | null;
}

interface PlanState {
  version: string;
  start_date: string;
  review_date: string;
  status: "active" | "archived";
  blocks: {
    name: string;
    weekday: number;
    exercises: {
      name: string;
      variant: string;
      sets: number;
      rep_range: string;
      target_rir: string;
      progression: string;
    }[];
  }[];
}

function seedState(): MockState {
  const plan: PlanState = {
    version: "v2",
    start_date: "2026-08-31",
    review_date: "2026-10-12",
    status: "active",
    blocks: [
      {
        name: "推日",
        weekday: 2,
        exercises: [
          {
            name: "杠铃卧推",
            variant: "杠铃",
            sets: 4,
            rep_range: "6-8",
            target_rir: "1-3",
            progression: "双重渐进",
          },
          {
            name: "哑铃肩推",
            variant: "哑铃",
            sets: 3,
            rep_range: "8-12",
            target_rir: "1-3",
            progression: "双重渐进",
          },
          {
            name: "双杠臂屈伸",
            variant: "自重",
            sets: 3,
            rep_range: "8-12",
            target_rir: "1-3",
            progression: "次数递增",
          },
        ],
      },
      {
        name: "拉日",
        weekday: 4,
        exercises: [
          {
            name: "引体向上",
            variant: "自重",
            sets: 3,
            rep_range: "6-10",
            target_rir: "1-3",
            progression: "次数递增",
          },
          {
            name: "杠铃划船",
            variant: "杠铃",
            sets: 4,
            rep_range: "8-10",
            target_rir: "1-3",
            progression: "双重渐进",
          },
          {
            name: "面拉",
            variant: "绳索",
            sets: 3,
            rep_range: "12-15",
            target_rir: "2-3",
            progression: "次数递增",
          },
        ],
      },
      {
        name: "腿日",
        weekday: 6,
        exercises: [
          {
            name: "杠铃深蹲",
            variant: "杠铃",
            sets: 4,
            rep_range: "6-8",
            target_rir: "1-3",
            progression: "双重渐进",
          },
          {
            name: "罗马尼亚硬拉",
            variant: "杠铃",
            sets: 3,
            rep_range: "8-10",
            target_rir: "1-3",
            progression: "双重渐进",
          },
          {
            name: "腿屈伸",
            variant: "器械",
            sets: 3,
            rep_range: "12-15",
            target_rir: "1-3",
            progression: "次数递增",
          },
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
      exercise: "杠铃卧推",
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
      exercise: "杠铃划船",
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
      exercise: "杠铃卧推",
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
      exercise: "哑铃肩推",
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
        exercise: "杠铃卧推",
        variant: "杠铃",
        best_weight_kg: 80,
        best_reps_at_weight: 8,
      },
      {
        exercise: "杠铃划船",
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
    },
    restrictions: [
      { name: "杠铃颈后推举", restricted: true, note: "肩部不适史，暂禁" },
      {
        name: "颈前深蹲模式",
        restricted: false,
        note: "观察中，可用颈后深蹲替代",
      },
    ],
    plan,
    records,
    stats,
    review,
    sessions,
    messages,
    drafts: new Map(),
    draft_sessions: new Map(),
    confirm_receipts: new Map(),
    runs: new Map(),
    execution_slot_run_id: null,
  };
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
    base_business_version: 0, // 创建时以当时 context_version 填充
    payload: {
      date: MOCK_TODAY,
      exercise: "杠铃卧推",
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
        field: "杠铃卧推 · 工作组",
        new_value: "4 组 x 8 次 @ 80kg，目标 RIR 1-3",
      },
      { field: "第 4 组 · RIR", new_value: "未报告（待确认）" },
      { field: "热身", new_value: "递增至 60kg（摘要）" },
    ],
  };
  return {
    text: "已将打卡内容整理为训练记录草稿：**杠铃卧推 4 组 x 8 次 @ 80kg**，第 4 组 RIR 未报告（保持为空，不自行补造）。\n\n请核对下方草稿卡，可内联纠错关键字段，确认采纳后才写入正式记录。",
    draft,
  };
}

function planScriptReply(): { text: string; draft: Draft } {
  const draft: Draft = {
    id: nextId("draft"),
    kind: "plan_adjust",
    status: "pending",
    revision: 1, // 初版修订（01 1.3：标识用户所见内容版本）
    base_business_version: 0,
    payload: {
      title: "推日减量（PPL v2 → 拟议调整）",
      diff: [
        { field: "杠铃卧推 · 组数", old_value: "4 组", new_value: "3 组" },
        { field: "杠铃卧推 · 目标 RIR", old_value: "1-3", new_value: "2-3" },
        { field: "哑铃肩推 · 组数", old_value: "3 组", new_value: "2 组" },
      ],
    },
    diff: [
      { field: "杠铃卧推 · 组数", old_value: "4", new_value: "3" },
      { field: "杠铃卧推 · 目标 RIR", old_value: "1-3", new_value: "2-3" },
      { field: "哑铃肩推 · 组数", old_value: "3", new_value: "2" },
    ],
  };
  return {
    text: "根据近期表现，建议对推日做如下调整（作为计划新版本草稿，不静默覆盖正式计划）：\n\n- 卧推 4 组 → **3 组**\n- 卧推目标 RIR 1-3 → **2-3**\n- 哑铃肩推 3 组 → **2 组**\n\n请确认后生成新版本；旧版本保留历史。",
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

const REVIEW_REPLY = [
  "## 本阶段复盘（基于最新有效记录重算）",
  "",
  "- 完成率：W1 **2/3（66.7%）**，W2 **1/1（100%）**；漏练保留，不做补课。",
  "- 组级判定：符合目标 8 组 / 未符合 1 组 / 待补全 1 组。",
  "- PR：杠铃卧推 **80kg x8**；杠铃划船 **60kg x10**。",
  "",
  "| 计划周 | 应训练 | 已完成 | 完成率 |",
  "| --- | --- | --- | --- |",
  "| W1 | 3 | 2 | 66.7% |",
  "| W2 | 1 | 1 | 100% |",
  "",
  "> 建议下一步：腿日恢复训练前先做接回评估；卧推维持 80kg 观察 RIR。",
].join("\n");

const GENERIC_REPLY = [
  "收到。当前处于 **mock 演示模式**，我可以：",
  "",
  "- 训练打卡：如「今天卧推 80kg 4组x8」",
  "- 调整计划：如「最近很累，帮我调整计划」",
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

  const isRecord = /打卡|卧推|深蹲|硬拉|划船/.test(message);
  const isPlan = /计划|调整|哑铃/.test(message);
  const isReview = /复盘/.test(message);

  let text: string;
  let draft: Draft | undefined;

  if (isRecord) {
    const r = recordScriptReply();
    text = r.text;
    draft = r.draft;
  } else if (isPlan) {
    const r = planScriptReply();
    text = r.text;
    draft = r.draft;
  } else if (isReview) {
    text = REVIEW_REPLY;
  } else {
    text = GENERIC_REPLY;
  }

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
    draft.base_business_version = state.context_version;
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

  /* 1) 重置种子场景：整份内存态回到 seedState() 初始值 */
  if (path === "/api/dev/reset" && method === "POST") {
    await readBody(req); // 消费请求体，保持 keep-alive 连接干净
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
    Object.assign(state, seedState());
    return json(res, 200, {
      ok: true,
      runs_stopped: runsStopped,
      context_version: state.context_version,
      plan_version: state.plan.version,
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
    const body = JSON.parse((await readBody(req)) || "{}") as {
      run_id?: string;
      error_code?: string;
    };
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
    const body = JSON.parse((await readBody(req)) || "{}") as {
      run_id?: string;
    };
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
    const body = JSON.parse((await readBody(req)) || "{}") as {
      seconds?: number;
      heartbeat?: boolean;
    };
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
      plan_version: state.plan.version,
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
    const ex = state.plan.blocks
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

      /* 档案 */
      if (path === "/api/profile" && method === "GET") {
        return json(res, 200, {
          profile: state.profile,
          restrictions: state.restrictions,
          context_version: state.context_version,
          /* STAGED-SHARED-EDIT（lane 3b，supervisor 批准）：/profile 当前计划卡数据源 */
          plan: state.plan,
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

        draft.payload = body.payload;
        // SAFETY: payload 形状由草稿 kind 决定；record 分支按 RecordDraftPayload 派生展示 diff，
        // plan/profile 分支的 diff 内嵌于 payload 本体，直接取用保持两者一致
        draft.diff =
          draft.kind === "training_record"
            ? recordDraftDiff(
                body.payload as Extract<Draft["payload"], { date: string }>,
              )
            : (
                body.payload as Extract<
                  Draft["payload"],
                  { title: string; diff: FieldDiff[] }
                >
              ).diff;
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

        // 原子提交（mock：顺序内存写入）
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
          const p = draft.payload as Extract<
            Draft["payload"],
            { title: string; diff: FieldDiff[] }
          >;
          for (const d of p.diff) {
            const sets = Number(d.new_value.match(/(\d+)/)?.[1]);
            const ex = state.plan.blocks
              .flatMap((b) => b.exercises)
              .find((e) => d.field.startsWith(e.name));
            if (ex && d.field.includes("组数") && Number.isFinite(sets))
              ex.sets = sets;
          }
          // 计划调整生成新版本（PRD 5.3：不静默覆盖、版本化启用）。
          // mock 简化：旧版本不归档展示（当前无版本历史 UI 消费方，不建版本列表）。
          state.plan.version = `v${Number(state.plan.version.slice(1)) + 1}`;
        }
        draft.status = "committed";
        state.context_version += 1;
        recomputeStats(state);

        const result: ConfirmResult = {
          draft_id: draft.id,
          status: "committed",
          newly_committed: true,
          context_version: state.context_version,
          summary:
            draft.kind === "training_record"
              ? "训练记录已写入正式数据"
              : draft.kind === "plan_adjust"
                ? "计划新版本已启用"
                : "档案已更新",
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
          // 计划草稿：从当前计划状态派生最小提案——旧值取当前计划实际值，
          // 取第一个可减量动作（sets >= 2）提议组数 -1；确认事务按同字段规则应用
          const ex = state.plan.blocks
            .flatMap((b) => b.exercises)
            .find((e) => e.sets >= 2);
          const rows: FieldDiff[] = ex
            ? [
                {
                  field: `${ex.name} · 组数`,
                  old_value: String(ex.sets),
                  new_value: String(ex.sets - 1),
                },
              ]
            : [];
          // F0-02B1：最新计划已派生不出可调整动作时，重算明确失败（409 invalid_request），
          // 不创建草稿、不把父草稿置 stale、不推进任何版本
          if (!rows.length)
            return apiError(res, {
              http_status: 409,
              error_code: "invalid_request",
              message: "当前计划没有组数 ≥ 2 的动作，重算无法生成新提案",
            });
          fresh = {
            id: nextId("draft"),
            kind: "plan_adjust",
            status: "pending",
            revision: 1,
            base_business_version: state.context_version,
            parent_draft_id: old.id,
            payload: {
              title: `计划调整重算（基于当前计划 ${state.plan.version}）`,
              diff: rows,
            },
            diff: rows,
          };
        } else {
          // mock Stage 0 场景不生成档案草稿；如出现则明确拒绝而非误派生
          return apiError(res, {
            http_status: 409,
            error_code: "invalid_request",
            message: "当前 mock 场景不支持档案草稿重算",
          });
        }
        state.drafts.set(fresh.id, fresh);
        // 重算新草稿归属同一会话（sessions/:id/drafts 可恢复）
        const oldSession = state.draft_sessions.get(old.id);
        if (oldSession) state.draft_sessions.set(fresh.id, oldSession);
        old.status = "stale";

        const diff: FieldDiff[] = [];
        // SAFETY: payload 只做字段级序列化对比，不当成可变记录使用；DraftPayload 联合类型的键在此处按 JSON 视图遍历
        const oldPayload = old.payload as unknown as Record<string, unknown>;
        // SAFETY: 同上，fresh.payload 为刚生成的 DraftPayload，仅用于与旧草稿做 JSON 字段对比
        const newPayload = fresh.payload as unknown as Record<string, unknown>;
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
