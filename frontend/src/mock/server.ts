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
  FieldDiff,
  PrEntry,
  ProviderConfig,
  RecalcResult,
  Restriction,
  ReviewDoc,
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
  runs: Map<string, RunState>;
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
    runs: new Map(),
  };
}

/* ------------------------------- SSE 总线 --------------------------------- */

interface BufferedEvent {
  id: number;
  event: string;
  data: unknown;
}

class SseHub {
  private seq = 0;
  private buffer: BufferedEvent[] = [];
  private clients = new Set<ServerResponse>();

  emit(event: string, data: unknown): void {
    this.seq += 1;
    const item = { id: this.seq, event, data };
    this.buffer.push(item);
    if (this.buffer.length > 200) this.buffer.shift();
    const payload = `id: ${item.id}\nevent: ${item.event}\ndata: ${JSON.stringify(data)}\n\n`;
    for (const client of this.clients) client.write(payload);
  }

  open(req: IncomingMessage, res: ServerResponse): void {
    res.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
    });

    const url = new URL(req.url ?? "/", "http://localhost");
    const headerId = Number(req.headers["last-event-id"] ?? Number.NaN);
    const queryId = Number(url.searchParams.get("last_event_id") ?? Number.NaN);
    const lastId =
      Number.isFinite(headerId) && headerId > 0
        ? headerId
        : Number.isFinite(queryId)
          ? queryId
          : 0;

    for (const item of this.buffer) {
      if (item.id > lastId) {
        res.write(
          `id: ${item.id}\nevent: ${item.event}\ndata: ${JSON.stringify(item.data)}\n\n`,
        );
      }
    }

    this.clients.add(res);
    const heartbeat = setInterval(() => res.write(": ping\n\n"), 15000);
    req.on("close", () => {
      clearInterval(heartbeat);
      this.clients.delete(res);
    });
  }
}

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
  emit("run.started", { run_id: run.id });

  // 上下文压缩轻提示（B3：仅界面轻提示）；长会话触发一次
  const history = state.messages.get(run.session_id) ?? [];
  if (history.length >= 5) {
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
  }

  if (draft) {
    await sleep(500);
    if (run.cancelled) return finishCancelled(emit, run);
    draft.base_business_version = state.context_version;
    state.drafts.set(draft.id, draft);
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
  // 取消端点已置 status 并发过 run.cancelled；此处仅在尚未标记时补发，避免双发
  if (run.status !== "cancelled") {
    run.status = "cancelled";
    emit("run.cancelled", { run_id: run.id });
  }
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
        // 全局单 Run：活跃 Run 存在时 409
        for (const r of state.runs.values()) {
          if (r.status === "pending" || r.status === "running") {
            return apiError(res, {
              http_status: 409,
              error_code: "conversation_busy",
              message: "已有正在进行的对话，请稍候或取消当前任务。",
            });
          }
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
          status: "running",
          cancelled: false,
        };
        state.runs.set(run.id, run);

        const msgs = state.messages.get(body.session_id) ?? [];
        msgs.push({ id: nextId("m"), role: "user", content: body.message });
        state.messages.set(body.session_id, msgs);

        // 剧本异步推进，不阻塞响应
        void runScript(state, hub, run, body.message).catch(() => {
          if (run.status === "running" || run.status === "pending") {
            run.status = "failed";
            hub.emit("run.failed", {
              run_id: run.id,
              error_code: "invalid_request",
            });
          }
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

      /* 草稿：确认（幂等）/ 重算 */
      const confirmMatch = path.match(/^\/api\/drafts\/([^/]+)\/confirm$/);
      if (confirmMatch && method === "POST") {
        // 先读完请求体再分支，避免 keep-alive 连接上残留未消费的 body
        const confirmBody = JSON.parse((await readBody(req)) || "{}") as {
          payload?: Draft["payload"];
        };
        const draft = state.drafts.get(confirmMatch[1]);
        if (!draft)
          return apiError(res, {
            http_status: 404,
            error_code: "invalid_request",
            message: "草稿不存在",
          });

        if (draft.status === "committed") {
          const result: ConfirmResult = {
            draft_id: draft.id,
            status: "committed",
            newly_committed: false,
            context_version: state.context_version,
            summary: "该草稿已提交过（幂等返回原结果）",
          };
          return json(res, 200, result);
        }

        if (draft.base_business_version !== state.context_version) {
          return apiError(res, {
            http_status: 409,
            error_code: "draft_stale",
            message:
              "业务数据已变更（如：新增训练记录），请按最新数据一键重算后再次确认。",
            detail: "context_version 已从生成时的版本向前推进",
          });
        }

        // 内联纠错（阶段三拍板 A）：confirm 携带最终纠错后的草稿内容，以其提交并同步更新存储草稿；
        // 幂等与 draft_stale 语义不变；真实后端仍以最终内容在事务内复查领域规则，前端纠错不绕过后端校验。
        if (confirmBody.payload) draft.payload = confirmBody.payload;

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

        // 以最新业务上下文重算：重新生成同 kind 草稿
        const regenerated =
          old.kind === "training_record"
            ? recordScriptReply().draft
            : planScriptReply().draft;
        const fresh: Draft = {
          ...regenerated,
          id: nextId("draft"),
          parent_draft_id: old.id,
          base_business_version: state.context_version,
        };
        state.drafts.set(fresh.id, fresh);
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
