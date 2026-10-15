// 运行：node scripts/run-duration-check.mjs
// 前端运行用时检查（Trace 采集方案 §5）：历史起止时间的归属与合并、运行查询与重复受理的时间归属（helper 级）、
// 刷新与重开会话的一致恢复、多运行隔离、执行中轮次与未知轮次的时间边界，
// 以及真实 ChatTranscript 渲染的用时时文本与既有展示回归。
// 输入均为本文件编写的契约 JSON，一律经生产解析函数 api.parseSessionHistory 进入 historyToRounds；
// 展示断言全部来自真实 React 渲染（scripts/confirmation-display-tree.tsx 的 renderTranscript）。
// 覆盖口径：块 7 只调用 mergeRunTimes 与 mergeHistoryRounds 两个合并 helper，属 helper 级覆盖；
// 运行管理层 sessionRunManager 的真实运行查询轮询与重复受理链路本文件未执行，归属真实浏览器 + 隔离服务验收。
import assert from "node:assert/strict";
import { createServer } from "vite";

const server = await createServer({
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});
const api = await server.ssrLoadModule("/src/lib/api.ts");
const sessionHistory = await server.ssrLoadModule(
  "/src/features/chat/utils/sessionHistory.ts",
);
// 运行态切换 helper：显式重试与运行查询终态收敛都由真实生产模块提供
const reactAgent = await server.ssrLoadModule(
  "/src/features/chat/utils/reactAgent.ts",
);
const { renderTranscript, primeEquipment } = await server.ssrLoadModule(
  "/scripts/confirmation-display-tree.tsx",
);
await server.close();
// 目录查询无数据时看板不渲染，先落到应用级缓存再展示
primeEquipment();

const u = (n) =>
  `${n.toString(16).padStart(8, "0")}-0000-4000-8000-000000000000`;
const sessionId = u(1);
const {
  historyToRounds,
  mergeHistoryRounds,
  mergeRunTimes,
  operationRound,
  reconcileLedger,
} = sessionHistory;
const { resumeReActRound, concludeReActRound } = reactAgent;

/* ===== 契约夹具构造（UTC 毫秒为业务记录的真实测量值，期望文本逐条独立书写） ===== */
const userNode = (entryId, text, runId, at) => ({
  type: "message",
  entry_id: entryId,
  parent_id: null,
  run_id: runId ?? null,
  created_at: at,
  message: { role: "user", text, timestamp: at, attachments: [] },
});
const assistantNode = (entryId, runId, at, content, stopReason) => ({
  type: "message",
  entry_id: entryId,
  parent_id: null,
  run_id: runId,
  created_at: at,
  message: {
    role: "assistant",
    content,
    stop_reason: stopReason,
    timestamp: at,
  },
});
const toolNode = (entryId, runId, at, toolCallId, name, content) => ({
  type: "message",
  entry_id: entryId,
  parent_id: null,
  run_id: runId,
  created_at: at,
  message: {
    role: "toolResult",
    tool_call_id: toolCallId,
    tool_name: name,
    content,
    is_error: false,
    timestamp: at,
  },
});
const hiddenNode = (entryId, runId, at) => ({
  type: "custom_message",
  entry_id: entryId,
  parent_id: null,
  run_id: runId,
  created_at: at,
});
const textBlock = (index, text) => ({
  content_index: index,
  type: "text",
  text,
});
const thinkingBlock = (index, thinking) => ({
  content_index: index,
  type: "thinking",
  thinking,
});
const callBlock = (index, toolCallId, name, args) => ({
  content_index: index,
  type: "tool_call",
  tool_call_id: toolCallId,
  name,
  arguments: args,
});
const runWire = (
  runId,
  requestEntryId,
  lastEntryId,
  status,
  startedAt,
  finishedAt,
  overrides = {},
) => ({
  session_id: sessionId,
  run_id: runId,
  request_entry_id: requestEntryId,
  last_entry_id: lastEntryId,
  status,
  started_at: startedAt,
  finished_at: finishedAt,
  error_code: null,
  error_message: null,
  ...overrides,
});
// 祖先链顺序即分支时序：parent_id 按数组顺序补齐
const historyBody = (nodes, runs, extra = {}) => {
  let previous = null;
  const entries = nodes.map((node) => {
    const item = { ...node, parent_id: previous };
    previous = node.entry_id;
    return item;
  });
  return {
    session: {
      session_id: sessionId,
      title: "会话",
      active_leaf_id: nodes.at(-1).entry_id,
      created_at: 1,
      updated_at: 2,
    },
    entries,
    runs,
    steering: extra.steering ?? [],
    confirmations: extra.confirmations ?? [],
    pending_proposals: extra.pending_proposals ?? [],
  };
};
const roundsOf = (value) => historyToRounds(api.parseSessionHistory(sessionId, value));
const steeringRecord = (runId, steeringId, text, status, at) => ({
  session_id: sessionId,
  run_id: runId,
  steering_id: steeringId,
  text,
  attachments: [],
  timestamp: at,
  status,
  entry_id: null,
  reason: null,
  created_at: at,
  updated_at: at,
});
const confirmationRecord = (
  operationId,
  proposalId,
  displayEntryId,
  confirmationEntryId,
  at,
) => ({
  session_id: sessionId,
  operation_id: operationId,
  business_kind: "profile",
  proposal_id: proposalId,
  display_entry_id: displayEntryId,
  confirmation_entry_id: confirmationEntryId,
  status: "processing",
  result: null,
  error: null,
  created_at: at,
  updated_at: at,
});
const pendingOperation = (operationId, overrides = {}) => ({
  operation_id: operationId,
  session_id: sessionId,
  kind: "send",
  run_id: null,
  request: "补充提问。",
  attachments: [],
  created_at: 1,
  request_entry_id: null,
  steering_id: null,
  target_entry_id: null,
  ...overrides,
});

const actions = () => {
  throw new Error("静态展示不发起操作");
};
const render = (rounds, props) =>
  renderTranscript({
    sessionId,
    rounds,
    onRetry: actions,
    onWithdraw: actions,
    retrying: null,
    canEdit: true,
    canConfirm: true,
    proposals: [],
    confirmations: [],
    onConfirm: () => {},
    onRetryConfirmation: actions,
    onEdit: () => Promise.resolve(true),
    onRegenerate: actions,
    ...props,
  });
// 渲染文本独立提取：与组件文案格式一致的字面模式，用于逐条比对期望值
const durations = (markup) => markup.match(/用时 [0-9]+\.[0-9] 秒/g) ?? [];
const count = (markup, needle) => markup.split(needle).length - 1;
const durationItem = (runId) => `data-message-id="${runId}:duration"`;

/* ===== 1. 正常历史：轮次保留运行真实起止时间，渲染文本等于精确毫秒换算的秒值 ===== */
const sharedRun = u(12);
const sharedRequest = u(10);
const sharedAssistant = u(11);
const SHARED_STARTED = 1_700_000_000_000;
const SHARED_FINISHED = 1_700_000_002_300;
const sharedBody = () =>
  historyBody(
    [
      userNode(sharedRequest, "本周训练安排。", null, 2),
      assistantNode(
        sharedAssistant,
        sharedRun,
        3,
        [textBlock(0, "已安排三天训练。")],
        "stop",
      ),
    ],
    [
      runWire(
        sharedRun,
        sharedRequest,
        sharedAssistant,
        "completed",
        SHARED_STARTED,
        SHARED_FINISHED,
      ),
    ],
  );
{
  const rounds = roundsOf(sharedBody());
  assert.equal(rounds.length, 1, "一份历史只含该运行的轮次");
  assert.equal(
    rounds[0].started_at,
    1_700_000_000_000,
    "轮次保留运行的真实起始时间（UTC 毫秒）",
  );
  assert.equal(
    rounds[0].finished_at,
    1_700_000_002_300,
    "轮次保留运行的真实结束时间（UTC 毫秒）",
  );
  const markup = render(rounds);
  assert.deepEqual(durations(markup), ["用时 2.3 秒"], "渲染文本等于毫秒差换算的秒值");
  assert.equal(
    count(markup, durationItem(sharedRun)),
    1,
    "该运行渲染出唯一一条用时条目",
  );
  assert.equal(markup.includes("已安排三天训练。"), true, "回答正文继续展示");
}

/* ===== 2. 四种终态在起止时间齐备时都渲染用时 ===== */
{
  const cases = [
    {
      status: "completed",
      started_at: 1_700_000_100_000,
      finished_at: 1_700_000_102_300,
      text: "用时 2.3 秒",
    },
    {
      status: "failed",
      started_at: 1_700_000_200_000,
      finished_at: 1_700_000_201_500,
      text: "用时 1.5 秒",
      error_code: "execution_failed",
      error_message: "执行失败。",
    },
    {
      status: "cancelled",
      started_at: 1_700_000_300_000,
      finished_at: 1_700_000_300_600,
      text: "用时 0.6 秒",
    },
    {
      status: "interrupted",
      started_at: 1_700_000_400_000,
      finished_at: 1_700_000_490_000,
      text: "用时 90.0 秒",
    },
  ];
  cases.forEach((item, index) => {
    const request = u(200 + index * 3);
    const assistant = u(201 + index * 3);
    const run = u(202 + index * 3);
    const rounds = roundsOf(
      historyBody(
        [
          userNode(request, "状态用例。", null, 2),
          assistantNode(
            assistant,
            run,
            3,
            [textBlock(0, "状态用例回答。")],
            "stop",
          ),
        ],
        [
          runWire(
            run,
            request,
            assistant,
            item.status,
            item.started_at,
            item.finished_at,
            {
              error_code: item.error_code ?? null,
              error_message: item.error_message ?? null,
            },
          ),
        ],
      ),
    );
    assert.equal(rounds[0].status, item.status, "运行状态映射为展示状态");
    assert.equal(
      rounds[0].started_at,
      item.started_at,
      `${item.status} 轮次保留起始时间`,
    );
    assert.equal(
      rounds[0].finished_at,
      item.finished_at,
      `${item.status} 轮次保留结束时间`,
    );
    const markup = render(rounds);
    assert.deepEqual(
      durations(markup),
      [item.text],
      `${item.status} 渲染该用例自身的用时时文本`,
    );
    assert.equal(count(markup, durationItem(run)), 1, `${item.status} 用时条目唯一`);
  });
}

/* ===== 3. 测量不完整时不新增任何用时文本 ===== */
const sseRound = (id, status, times) => ({
  id,
  run_id: id,
  entries: [
    { kind: "user", id: u(300), entry_id: u(300), request: "本地快照提问。" },
    {
      kind: "assistant",
      id: u(301),
      entry_id: u(301),
      parent_id: u(300),
      content: [textBlock(0, "本地快照回答。")],
      stop_reason: "stop",
    },
  ],
  status,
  ...times,
});
{
  const running = roundsOf(
    historyBody(
      [
        userNode(u(310), "仍在执行的提问。", null, 2),
        assistantNode(u(311), u(312), 3, [textBlock(0, "执行中的回答。")], "stop"),
      ],
      [
        runWire(
          u(312),
          u(310),
          u(311),
          "running",
          1_700_000_500_000,
          null,
        ),
      ],
    ),
  );
  assert.equal(running[0].status, "running", "历史运行中的轮次保持执行中");
  assert.equal(
    running[0].finished_at,
    null,
    "结束时间尚未测量时为 null",
  );
  assert.deepEqual(durations(render(running)), [], "running 轮次不渲染用时");

  // 本地 SSE 与账本状态：起止时间字段整体缺省（unknown 语义），与历史 null 同样不渲染
  const baseline = render([sseRound(u(320), "completed", {})]);
  assert.equal(count(baseline, "用时"), 0, "完成但无测量的轮次零用时文本基线");
  assert.deepEqual(durations(baseline), [], "完成但无测量的轮次不渲染用时");
  assert.deepEqual(
    durations(render([sseRound(u(321), "completed", { finished_at: null })])),
    [],
    "finished_at 为 null 不渲染用时",
  );
  assert.deepEqual(
    durations(
      render([
        sseRound(u(322), "completed", { started_at: 1_700_000_500_000 }),
      ]),
    ),
    [],
    "finished_at 缺省不渲染用时",
  );
  assert.deepEqual(
    durations(
      render([
        sseRound(u(323), "completed", { finished_at: 1_700_000_502_300 }),
      ]),
    ),
    [],
    "started_at 缺省不渲染用时",
  );
  assert.deepEqual(
    durations(render([sseRound(u(324), "running", { started_at: 1_700_000_500_000 })])),
    [],
    "running 且仅有起始时间不渲染用时",
  );
  assert.equal(baseline.includes("NaN"), false, "无测量时不出现无效数值");
  assert.equal(
    count(baseline, `data-message-id`),
    2,
    "无测量时该轮只有原有两条条目，没有占位条目",
  );
  // 同一轮次补上完整测量后仅多出一条用时文本，其它展示不变
  const withTimes = render([
    sseRound(u(320), "completed", {
      started_at: SHARED_STARTED,
      finished_at: SHARED_FINISHED,
    }),
  ]);
  assert.deepEqual(durations(withTimes), ["用时 2.3 秒"], "补齐测量后渲染用时");
  assert.equal(
    count(withTimes, "本地快照回答。"),
    count(baseline, "本地快照回答。"),
    "补齐测量不改变正文展示",
  );
}

/* ===== 4. 零测量（起止相同）是有效展示：用时 0.0 秒必须保留 ===== */
{
  const request = u(330);
  const assistant = u(331);
  const run = u(332);
  const rounds = roundsOf(
    historyBody(
      [
        userNode(request, "极短运行提问。", null, 2),
        assistantNode(assistant, run, 3, [textBlock(0, "极短运行回答。")], "stop"),
      ],
      [runWire(run, request, assistant, "completed", 1_700_000_500_000, 1_700_000_500_000)],
    ),
  );
  assert.equal(rounds[0].started_at, 1_700_000_500_000, "零测量保留起始时间");
  assert.equal(rounds[0].finished_at, 1_700_000_500_000, "零测量保留结束时间");
  const markup = render(rounds);
  assert.deepEqual(durations(markup), ["用时 0.0 秒"], "零测量渲染为 用时 0.0 秒");
  assert.equal(count(markup, "用时 0.0 秒"), 1, "零测量文本不被真值判断丢弃");
}

/* ===== 5. SSE 结算后同终态的历史刷新补上真实测量 ===== */
{
  const local = [
    {
      id: sharedRun,
      run_id: sharedRun,
      request_entry_id: sharedRequest,
      entries: [
        {
          kind: "user",
          id: sharedRequest,
          entry_id: sharedRequest,
          request: "本周训练安排。",
        },
        {
          kind: "assistant",
          id: sharedAssistant,
          entry_id: sharedAssistant,
          parent_id: sharedRequest,
          content: [textBlock(0, "已安排三天训练。")],
          stop_reason: "stop",
        },
      ],
      status: "completed",
    },
  ];
  assert.equal(local[0].started_at, undefined, "SSE 结算的轮次尚未取得时间");
  assert.deepEqual(durations(render(local)), [], "刷新前不渲染用时");
  const refreshed = roundsOf(sharedBody());
  const merged = mergeHistoryRounds(local, refreshed);
  assert.equal(merged.length, 1, "同运行刷新不新增轮次");
  assert.equal(merged[0].status, "completed", "同终态刷新保持完成状态");
  assert.equal(
    merged[0].started_at,
    SHARED_STARTED,
    "同终态刷新补齐真实起始时间",
  );
  assert.equal(
    merged[0].finished_at,
    SHARED_FINISHED,
    "同终态刷新补齐真实结束时间",
  );
  assert.deepEqual(durations(render(merged)), ["用时 2.3 秒"], "刷新后渲染出用时");
  assert.equal(count(render(merged), "已安排三天训练。"), 1, "刷新后正文仍只展示一次");
}

/* ===== 6. 较早快照与未知占位轮次不得清除已知时间；既有终态与条目合并保持 ===== */
{
  const KNOWN_STARTED = 1_700_000_600_000;
  const KNOWN_FINISHED = 1_700_000_603_400;
  const run = u(340);
  const known = {
    id: run,
    run_id: run,
    entries: [],
    status: "running",
    started_at: KNOWN_STARTED,
    finished_at: KNOWN_FINISHED,
  };
  const stale = { ...known, finished_at: null };
  const noTimes = { id: run, run_id: run, entries: [], status: "running" };
  assert.equal(
    mergeHistoryRounds([stale], [known])[0].finished_at,
    KNOWN_FINISHED,
    "较新的结束测量覆盖较早的 null 快照",
  );
  assert.equal(
    mergeHistoryRounds([known], [stale])[0].finished_at,
    KNOWN_FINISHED,
    "较早的 null 快照不清除已知结束时间",
  );
  assert.equal(
    mergeHistoryRounds([noTimes], [known])[0].started_at,
    KNOWN_STARTED,
    "无时间快照并入已知测量",
  );
  const unknownRound = { id: run, run_id: run, entries: [], status: "unknown" };
  assert.equal(
    mergeHistoryRounds([unknownRound], [known])[0].finished_at,
    KNOWN_FINISHED,
    "未知占位轮次不遮挡历史测量",
  );

  // 既有合并行为保持：历史终态优先于旧 running 快照，同身份条目不重复
  const healed = mergeHistoryRounds(
    [
      {
        id: sharedRun,
        run_id: sharedRun,
        entries: [
          {
            kind: "user",
            id: sharedRequest,
            entry_id: sharedRequest,
            request: "本周训练安排。",
          },
        ],
        status: "running",
      },
    ],
    roundsOf(sharedBody()),
  );
  assert.equal(healed[0].status, "completed", "历史终态优先于较早 running 快照");
  assert.deepEqual(
    healed[0].entries.map((entry) => entry.kind),
    ["user", "assistant"],
    "合并后按稳定身份补齐条目且不重复",
  );
  assert.equal(
    healed[0].finished_at,
    SHARED_FINISHED,
    "终态优先的合并同样带上真实测量",
  );
  assert.deepEqual(durations(render(healed)), ["用时 2.3 秒"], "终态合并后渲染用时");
}

/* ===== 7. 运行查询与重复受理的时间归属（helper 级覆盖）===== */
/* 能力范围标注：本块把运行查询响应与重复受理响应的 JSON 形状手工展开成轮次，再交给
 * mergeRunTimes 与 mergeHistoryRounds 两个合并 helper 断言时间归属；它验证的是这两个 helper
 * 的行为口径，不代表运行管理层状态机：sessionRunManager 的真实 HTTP 轮询、重复受理与
 * 状态收敛链路属未执行项，归属真实浏览器 + 隔离服务验收。 */
{
  const QUERY_STARTED = 1_700_000_700_000;
  const QUERY_FINISHED = 1_700_000_701_200;
  const run = u(350);
  const request = u(351);
  const operationId = u(352);
  // 本地执行中的轮次（SSE 事件不写入时间）
  const live = {
    id: operationId,
    run_id: run,
    entries: [
      { kind: "user", id: request, entry_id: request, request: "运行查询提问。" },
    ],
    status: "running",
  };
  // GET /api/agent/runs/{run_id} 的响应形状（SessionRunWire，含起止时间）：本文件手工构造，未经真实 HTTP
  const runQuery = {
    session_id: sessionId,
    run_id: run,
    request_entry_id: request,
    last_entry_id: null,
    status: "completed",
    started_at: QUERY_STARTED,
    finished_at: QUERY_FINISHED,
    error_code: null,
    error_message: null,
  };
  const polled = {
    ...live,
    run_id: runQuery.run_id,
    request_entry_id: runQuery.request_entry_id,
    ...mergeRunTimes(live, runQuery),
  };
  assert.equal(polled.started_at, QUERY_STARTED, "helper 合并把查询响应的起始时间并入执行中快照");
  assert.equal(polled.finished_at, QUERY_FINISHED, "helper 合并把查询响应的结束时间并入执行中快照");
  const settled = { ...polled, status: "completed" };
  assert.deepEqual(
    durations(render([settled])),
    ["用时 1.2 秒"],
    "helper 合并出的终态轮次渲染用时",
  );
  // 重复受理响应形状（SendRunRepeatWire，契约不含时间字段）：该轮仍无测量，本文件手工构造，未经真实 HTTP
  const repeat = {
    operation_id: operationId,
    session_id: sessionId,
    run_id: run,
    request_entry_id: request,
    status: "completed",
  };
  const repeatOnly = {
    ...live,
    status: "completed",
    ...mergeRunTimes(live, repeat),
  };
  assert.deepEqual(durations(render([repeatOnly])), [], "重复受理的完成轮次不渲染用时");
  // 历史刷新路径同样供给时间（补齐重复受理遗留的未知测量）
  const historyRound = roundsOf(
    historyBody(
      [
        userNode(request, "运行查询提问。", null, 2),
        assistantNode(u(353), run, 3, [textBlock(0, "运行查询回答。")], "stop"),
      ],
      [
        runWire(
          run,
          request,
          u(353),
          "completed",
          QUERY_STARTED,
          QUERY_FINISHED,
        ),
      ],
    ),
  );
  assert.equal(
    mergeHistoryRounds([repeatOnly], historyRound)[0].finished_at,
    QUERY_FINISHED,
    "历史刷新补齐重复受理遗留的未知测量",
  );
  assert.deepEqual(durations(render([{ ...live, status: "failed" }, ])), [], "失败但无测量的轮次不渲染用时");
}

/* ===== 8. 重开会话与刷新页面：同一份 wire 两次解析的用时展示完全一致 ===== */
{
  const first = roundsOf(sharedBody());
  const second = roundsOf(sharedBody());
  assert.equal(second[0].started_at, SHARED_STARTED, "再次解析保持同一时间来源");
  assert.deepEqual(durations(render(first)), ["用时 2.3 秒"], "重开会话渲染用时");
  assert.equal(render(second), render(first), "同一历史两次解析的渲染完全一致");
  assert.deepEqual(
    durations(render(mergeHistoryRounds(first, second))),
    ["用时 2.3 秒"],
    "刷新合并后用时文本保持一致",
  );
}

/* ===== 9. 编辑/重新生成与多运行隔离：各运行只渲染自己的用时 ===== */
{
  const reqA = u(360);
  const asA = u(361);
  const runA = u(362);
  const hidden = u(363);
  const reqB = u(364);
  const asB = u(365);
  const runB = u(366);
  const steeringId = u(367);
  const rounds = roundsOf(
    historyBody(
      [
        userNode(reqA, "第一次提问。", null, 2),
        assistantNode(
          asA,
          runA,
          3,
          [textBlock(0, "第一次回答。")],
          "stop",
        ),
        hiddenNode(hidden, runB, 4),
        userNode(reqB, "第二次提问。", null, 5),
        assistantNode(
          asB,
          runB,
          6,
          [textBlock(0, "第二次回答。")],
          "stop",
        ),
      ],
      [
        runWire(runA, reqA, asA, "completed", 1_700_000_900_000, 1_700_000_902_300),
        runWire(runB, reqB, hidden, "completed", 1_700_001_000_000, 1_700_001_004_500),
      ],
      {
        steering: [
          steeringRecord(runA, steeringId, "运行中的追加。", "pending", 7),
        ],
      },
    ),
  );
  assert.equal(rounds.length, 2, "两次运行各自成轮");
  assert.equal(rounds[0].started_at, 1_700_000_900_000, "第一个轮次归属自己的起始时间");
  assert.equal(rounds[0].finished_at, 1_700_000_902_300, "第一个轮次归属自己的结束时间");
  assert.equal(rounds[1].started_at, 1_700_001_000_000, "第二个轮次归属自己的起始时间");
  assert.equal(rounds[1].finished_at, 1_700_001_004_500, "第二个轮次归属自己的结束时间");
  const markup = render(rounds);
  assert.deepEqual(
    durations(markup),
    ["用时 2.3 秒", "用时 4.5 秒"],
    "两条用时按运行顺序各自取值",
  );
  assert.equal(count(markup, durationItem(runA)), 1, "第一个运行只有一条用时条目");
  assert.equal(count(markup, durationItem(runB)), 1, "第二个运行只有一条用时条目");
  assert.equal(
    count(markup, ":duration"),
    2,
    "运行内追加输入不产生额外用时条目",
  );
  assert.equal(
    rounds[1].hidden?.[hidden]?.run_id,
    runB,
    "资料节点登记在第二个运行的轮次上",
  );
  assert.equal(
    rounds[1].finished_at,
    1_700_001_004_500,
    "带隐藏资料节点的轮次保持自身运行时间",
  );
  assert.equal(rounds[1].entries.length, 2, "资料节点不生成可见条目");
  // 重新生成复用请求节点：新一轮次只带自己的时间，旧轮次不被改写
  const regenerated = roundsOf(
    historyBody(
      [
        userNode(reqA, "第一次提问。", null, 2),
        assistantNode(asA, runA, 3, [textBlock(0, "第一次回答。")], "stop"),
        assistantNode(asB, runB, 4, [textBlock(0, "重新生成的回答。")], "stop"),
      ],
      [
        runWire(runA, reqA, asA, "completed", 1_700_000_900_000, 1_700_000_902_300),
        runWire(runB, reqA, asB, "completed", 1_700_001_000_000, 1_700_001_004_500),
      ],
    ),
  );
  assert.equal(regenerated.length, 2, "同一请求节点的两次运行各自成轮");
  assert.deepEqual(
    durations(render(regenerated)),
    ["用时 2.3 秒", "用时 4.5 秒"],
    "重新生成运行的用时与原运行互不干扰",
  );
}

/* ===== 10. 一次运行内多条助手消息与工具续跑只有一条用时条目 ===== */
{
  const req = u(370);
  const first = u(371);
  const result = u(372);
  const second = u(373);
  const run = u(374);
  const rounds = roundsOf(
    historyBody(
      [
        userNode(req, "查一下本周训练。", null, 2),
        assistantNode(
          first,
          run,
          3,
          [textBlock(0, "先查一下。"), callBlock(1, "tc-1", "get_workouts", {})],
          "toolUse",
        ),
        toolNode(result, run, 4, "tc-1", "get_workouts", "周一、周三、周五。"),
        assistantNode(
          second,
          run,
          5,
          [textBlock(0, "本周共三次训练。")],
          "stop",
        ),
      ],
      [runWire(run, req, second, "completed", 1_700_001_100_000, 1_700_001_107_800)],
    ),
  );
  assert.equal(rounds.length, 1, "一次运行只成一轮");
  const markup = render(rounds);
  assert.deepEqual(durations(markup), ["用时 7.8 秒"], "整轮只有一条用时取值");
  assert.equal(count(markup, durationItem(run)), 1, "用时条目身份为该轮一条");
  assert.equal(count(markup, "先查一下。"), 1, "第一段回答不重复");
  assert.equal(count(markup, "本周共三次训练。"), 1, "最终回答不重复");
  assert.equal(count(markup, "周一、周三、周五。"), 1, "工具结果不重复");
  assert.equal(
    markup.indexOf(durationItem(run)) > markup.indexOf(`data-message-id="${second}"`),
    true,
    "用时条目位于该轮全部条目之后",
  );
  // 条目身份稳定：刷新合并与再次渲染完全一致
  const merged = mergeHistoryRounds(rounds, rounds);
  assert.equal(merged.length, 1, "同轮合并不新增轮次");
  assert.equal(
    merged[0].finished_at,
    1_700_001_107_800,
    "同轮合并保持自身结束时间",
  );
  assert.equal(render(merged), markup, "刷新合并后用时条目身份与渲染保持稳定");
}

/* ===== 11. 确认独立轮次、用户气泡、工具卡片与未受理占位轮次都不携带 Agent 用时 ===== */
{
  const req = u(380);
  const call = u(381);
  const display = u(382);
  const confirm = u(383);
  const run = u(384);
  const proposal = u(385);
  const operation = u(386);
  const rounds = roundsOf(
    historyBody(
      [
        userNode(req, "记录今天的训练。", null, 2),
        assistantNode(
          call,
          run,
          3,
          [callBlock(0, "tc-9", "save_note", { text: "今天练背" })],
          "toolUse",
        ),
        toolNode(display, run, 4, "tc-9", "save_note", "已记录。"),
        userNode(confirm, "确认保存画像", null, 5),
      ],
      [runWire(run, req, display, "completed", SHARED_STARTED, SHARED_FINISHED)],
      { confirmations: [confirmationRecord(operation, proposal, display, confirm, 6)] },
    ),
  );
  assert.equal(rounds.length, 2, "运行轮次与确认独立轮次各一条");
  const standalone = rounds.find((round) => round.confirmation !== undefined);
  assert.equal(standalone.id, `confirmation:${confirm}`, "确认轮次使用保留身份");
  assert.equal(standalone.run_id, undefined, "确认轮次不归属任何 Agent 运行");
  assert.equal(
    Object.hasOwn(standalone, "started_at"),
    false,
    "确认轮次不携带运行起始时间",
  );
  assert.equal(
    Object.hasOwn(standalone, "finished_at"),
    false,
    "确认轮次不携带运行结束时间",
  );
  const markup = render(rounds);
  assert.deepEqual(
    durations(markup),
    ["用时 2.3 秒"],
    "确认轮次与用户气泡、工具卡片不产生额外用时",
  );
  assert.equal(count(markup, ":duration"), 1, "整页只有 Agent 运行的用时条目");
  assert.equal(
    markup.includes(`data-message-id="confirmation:${confirm}:duration"`),
    false,
    "确认轮次没有用时条目身份",
  );
  assert.equal(markup.includes("确认保存画像 · 确认处理中"), true, "确认结果行继续展示");
  assert.equal(markup.includes("已记录。"), true, "工具卡片继续展示");

  // 未受理操作的占位轮次：无时间归属、无用时时文本
  const recovery = reconcileLedger(null, [pendingOperation(u(387))]);
  assert.equal(recovery.rounds.length, 1, "未受理发送生成占位轮次");
  assert.equal(
    Object.hasOwn(recovery.rounds[0], "started_at"),
    false,
    "占位轮次不携带运行时间",
  );
  const placeholderMarkup = render(recovery.rounds);
  assert.deepEqual(durations(placeholderMarkup), [], "占位轮次不渲染用时");
  assert.equal(count(placeholderMarkup, "提交结果未知"), 1, "占位轮次保留未知提示");
}

/* ===== 12. 真实渲染回归：用时与既有条目、折叠、状态行与操作入口共存 ===== */
{
  const req = u(390);
  const call = u(391);
  const result = u(392);
  const confirm = u(393);
  const run = u(394);
  const steeringId = u(395);
  const pendingId = u(396);
  const rounds = roundsOf(
    historyBody(
      [
        userNode(req, "取消的一次运行。", null, 2),
        assistantNode(
          call,
          run,
          3,
          [
            thinkingBlock(0, "先确认日期。"),
            textBlock(1, "已取消的回答。"),
            callBlock(2, "tc-3", "get_workouts", { day: "mon" }),
          ],
          "toolUse",
        ),
        toolNode(result, run, 4, "tc-3", "get_workouts", "训练结果。"),
        userNode(confirm, "确认保存画像", null, 5),
      ],
      [
        runWire(
          run,
          req,
          result,
          "cancelled",
          1_700_001_200_000,
          1_700_001_202_300,
        ),
      ],
      {
        steering: [
          steeringRecord(run, steeringId, "撤回的一条输入。", "pending", 6),
        ],
        confirmations: [
          confirmationRecord(u(397), u(398), result, confirm, 7),
        ],
      },
    ),
  );
  // 本页仍有结果未知的发送操作（与历史轮次并存）
  const withPending = [
    ...rounds,
    {
      id: pendingId,
      entries: [{ kind: "user", id: pendingId, request: "补充提问。" }],
      status: "unknown",
      pending: [pendingOperation(pendingId)],
    },
  ];
  const markup = render(withPending);
  assert.deepEqual(durations(markup), ["用时 2.3 秒"], "共存场景只渲染运行用时");
  assert.equal(markup.includes("已取消的回答。"), true, "回答正文继续展示");
  assert.equal(markup.includes("训练结果。"), true, "工具结果继续展示");
  assert.equal(markup.includes("思考"), true, "思考折叠入口继续展示");
  assert.equal(count(markup, 'aria-label="思考内容"'), 1, "思考折叠面板入口身份唯一");
  assert.equal(markup.includes("已取消"), true, "取消状态行继续展示");
  assert.equal(markup.includes("确认保存画像 · 确认处理中"), true, "确认结果行继续展示");
  assert.equal(markup.includes("提交结果未知"), true, "未知操作提示继续展示");
  assert.equal(count(markup, "重试"), 1, "显式重试入口保留");
  assert.equal(count(markup, 'aria-label="撤回"'), 1, "待消费输入的撤回入口保留");
  assert.equal(count(markup, 'aria-label="编辑消息"'), 2, "两个用户节点的编辑入口保留");
  assert.equal(count(markup, 'aria-label="重新生成"'), 1, "重新生成入口保留（确认节点不提供）");
  assert.equal(
    markup.indexOf(durationItem(run)) <
      markup.indexOf(`data-message-id="confirmation:${confirm}:confirmation"`),
    true,
    "用时条目位于该轮条目之后、其它状态条目之前",
  );

  // 无错误说明的失败与中断状态行同样与用时共存
  const failedRun = u(399);
  const failedMarkup = render(
    roundsOf(
      historyBody(
        [
          userNode(u(400), "失败的一次运行。", null, 2),
          assistantNode(u(401), failedRun, 3, [textBlock(0, "失败的回答。")], "stop"),
        ],
        [
          runWire(
            failedRun,
            u(400),
            u(401),
            "failed",
            1_700_001_300_000,
            1_700_001_305_000,
          ),
        ],
      ),
    ),
  );
  assert.equal(failedMarkup.includes("本次运行失败"), true, "失败状态行继续展示");
  assert.deepEqual(durations(failedMarkup), ["用时 5.0 秒"], "失败状态行与用时共存");

  const interruptedRun = u(402);
  const interruptedMarkup = render(
    roundsOf(
      historyBody(
        [
          userNode(u(403), "中断的一次运行。", null, 2),
          assistantNode(
            u(404),
            interruptedRun,
            3,
            [textBlock(0, "中断的回答。")],
            "stop",
          ),
        ],
        [
          runWire(
            interruptedRun,
            u(403),
            u(404),
            "interrupted",
            1_700_001_400_000,
            1_700_001_400_100,
          ),
        ],
      ),
    ),
  );
  assert.equal(interruptedMarkup.includes("已中断"), true, "中断状态行继续展示");
  assert.deepEqual(
    durations(interruptedMarkup),
    ["用时 0.1 秒"],
    "不足一秒的测量按既有约定保留一位小数",
  );
}

/* ===== 13. 形状约束：只有归属运行的轮次携带起止时间 ===== */
{
  const req = u(410);
  const display = u(411);
  const call = u(412);
  const confirm = u(413);
  const run = u(414);
  const rounds = roundsOf(
    historyBody(
      [
        userNode(req, "形状检查提问。", null, 2),
        assistantNode(
          call,
          run,
          3,
          [callBlock(0, "tc-7", "get_workouts", {})],
          "toolUse",
        ),
        toolNode(display, run, 4, "tc-7", "get_workouts", "形状检查结果。"),
        userNode(confirm, "确认保存画像", null, 5),
      ],
      [runWire(run, req, display, "completed", SHARED_STARTED, SHARED_FINISHED)],
      {
        confirmations: [
          confirmationRecord(u(415), u(416), display, confirm, 6),
        ],
        steering: [
          steeringRecord(run, u(417), "形状检查追加。", "pending", 7),
        ],
      },
    ),
  );
  for (const round of rounds) {
    const hasTimes = Object.hasOwn(round, "started_at");
    assert.equal(
      hasTimes,
      round.run_id !== undefined,
      "起止时间只出现在归属运行的轮次上",
    );
    if (round.run_id === undefined) continue;
    assert.equal(round.started_at, SHARED_STARTED, "运行轮次起始时间来自其自身运行");
    assert.equal(round.finished_at, SHARED_FINISHED, "运行轮次结束时间来自其自身运行");
  }
  assert.deepEqual(durations(render(rounds)), ["用时 2.3 秒"], "形状检查场景用时唯一");
  const ledger = reconcileLedger(
    api.parseSessionHistory(sessionId, sharedBody()),
    [pendingOperation(u(418)), pendingOperation(u(419), { kind: "steering", run_id: sharedRun })],
  );
  const placeholders = ledger.rounds.filter((round) => round.pending !== undefined);
  assert.equal(placeholders.length, 1, "未受理发送保留一条占位轮次");
  assert.equal(
    Object.hasOwn(placeholders[0], "started_at"),
    false,
    "账本占位轮次不携带运行时间",
  );
  assert.equal(
    ledger.rounds.filter((round) => round.run_id === sharedRun)[0].finished_at,
    SHARED_FINISHED,
    "账本合并后已提交运行的时间保持",
  );
  assert.deepEqual(
    durations(render(ledger.rounds)),
    ["用时 2.3 秒"],
    "账本合并场景只渲染已提交运行的用时",
  );
}

/* ===== 14. 边界：轮次处于 running 时即使携带完整真实测量也零用时文本，正文与条目全部保留 ===== */
{
  const [completed] = roundsOf(sharedBody());
  assert.equal(completed.status, "completed", "夹具轮次为完成终态");
  assert.equal(completed.started_at, SHARED_STARTED, "夹具轮次携带真实起始测量");
  assert.equal(
    completed.finished_at,
    SHARED_FINISHED,
    "夹具轮次携带真实结束测量",
  );
  assert.deepEqual(
    durations(render([completed])),
    ["用时 2.3 秒"],
    "同一轮次终态时渲染用时（与下方 running 断言构成唯一差异对照）",
  );

  const resumed = resumeReActRound(completed);
  assert.equal(resumed.status, "running", "显式重试把轮次恢复为执行中");
  assert.equal(
    resumed.started_at,
    1_700_000_000_000,
    "执行中轮次完整保留原始起始测量",
  );
  assert.equal(
    resumed.finished_at,
    1_700_000_002_300,
    "执行中轮次完整保留原始结束测量",
  );
  const markup = render([resumed]);
  assert.deepEqual(durations(markup), [], "执行中轮次零用时文本");
  assert.equal(count(markup, "用时"), 0, "执行中轮次不出现用时时字样");
  assert.equal(
    count(markup, durationItem(sharedRun)),
    0,
    "执行中轮次不产生 round.id 加 :duration 的条目",
  );
  assert.equal(count(markup, ":duration"), 0, "执行中轮次不存在任何用时条目身份");
  assert.equal(markup.includes("已安排三天训练。"), true, "执行中轮次的助手正文仍然可见");
  assert.equal(markup.includes("本周训练安排。"), true, "执行中轮次的用户正文仍然可见");
  assert.equal(
    count(markup, `data-message-id="${sharedRequest}"`),
    1,
    "执行中轮次保留原有用户条目",
  );
  assert.equal(
    count(markup, `data-message-id="${sharedAssistant}"`),
    1,
    "执行中轮次保留原有助手条目",
  );

  // 恢复终态后沿用同一份真实测量继续展示（真实收敛 helper）
  const concluded = concludeReActRound(resumed, "completed");
  assert.equal(concluded.status, "completed", "运行查询终态收敛把执行中轮次收敛为完成");
  assert.equal(concluded.started_at, SHARED_STARTED, "收敛后的轮次保持同一份起始测量");
  assert.equal(
    concluded.finished_at,
    SHARED_FINISHED,
    "收敛后的轮次保持同一份结束测量",
  );
  assert.deepEqual(
    durations(render([concluded])),
    ["用时 2.3 秒"],
    "恢复终态后沿用同一份真实测量渲染用时",
  );
}

/* ===== 15. 边界：existing 为 unknown 且携带完整测量时，较早的 running 历史不清除结束测量（mergeHistoryRounds helper 级） ===== */
{
  const operation = pendingOperation(u(430), {
    run_id: sharedRun,
    request: "本周训练安排。",
    request_entry_id: sharedRequest,
  });
  const [known] = roundsOf(sharedBody());
  const ledgerRound = operationRound(operation);
  const existing = { ...ledgerRound, ...mergeRunTimes(ledgerRound, known) };
  assert.equal(existing.status, "unknown", "真实 operationRound 生成未知状态轮次");
  assert.equal(existing.id, operation.operation_id, "未知轮次以操作身份为 id");
  assert.equal(
    existing.started_at,
    SHARED_STARTED,
    "未知轮次并入已知起始测量",
  );
  assert.equal(
    existing.finished_at,
    SHARED_FINISHED,
    "未知轮次并入已知结束测量",
  );

  const incoming = roundsOf(
    historyBody(
      [
        userNode(sharedRequest, "本周训练安排。", null, 2),
        assistantNode(
          sharedAssistant,
          sharedRun,
          3,
          [textBlock(0, "已安排三天训练。")],
          "stop",
        ),
      ],
      [
        runWire(
          sharedRun,
          sharedRequest,
          sharedAssistant,
          "running",
          SHARED_STARTED,
          null,
        ),
      ],
    ),
  );
  assert.equal(incoming.length, 1, "较早历史解析出同一运行的轮次");
  assert.equal(incoming[0].status, "running", "较早历史仍为执行中");
  assert.equal(incoming[0].finished_at, null, "较早历史的结束测量尚未取得");

  const merged = mergeHistoryRounds([existing], incoming);
  assert.equal(merged.length, 1, "同 run_id 合并不新增轮次");
  assert.equal(
    merged[0].finished_at,
    1_700_000_002_300,
    "未知侧的已知结束测量不被较早 running 历史清除",
  );
  assert.equal(
    merged[0].started_at,
    1_700_000_000_000,
    "未知侧的已知起始测量同样保留",
  );
  assert.equal(merged[0].status, "running", "状态跟随历史侧的较早执行中快照");
  assert.equal(merged[0].id, sharedRun, "轮次 id 来自历史轮次，未知轮次的操作 id 不再残留");
  assert.equal(merged[0].run_id, sharedRun, "运行身份取自历史轮次");
  assert.equal(
    merged[0].request_entry_id,
    sharedRequest,
    "请求节点身份取自历史轮次",
  );
  assert.equal(merged[0].entries.length, 2, "条目以历史为事实来源，共两条");
  assert.deepEqual(
    merged[0].entries.map((entry) => entry.kind),
    ["user", "assistant"],
    "合并后条目顺序与内容仍来自历史",
  );
  assert.equal(
    merged[0].pending,
    undefined,
    "历史已受理的操作不再残留未确认提示",
  );
  assert.deepEqual(durations(render(merged)), [], "合并后的执行中轮次同样零用时文本");
}

/* ===== 16. 边界：existing 为 unknown 且携带完整测量时，reconcileLedger 的未知账本轮次（不含任何时间字段）保留两个测量 ===== */
{
  const operation = pendingOperation(u(440), {
    run_id: sharedRun,
    request: "本周训练安排。",
    request_entry_id: sharedRequest,
  });
  const [known] = roundsOf(sharedBody());
  const ledgerRound = operationRound(operation);
  const existing = { ...ledgerRound, ...mergeRunTimes(ledgerRound, known) };
  assert.equal(existing.status, "unknown", "existing 为真实 unknown 轮次");
  assert.equal(existing.started_at, SHARED_STARTED, "existing 已知起始测量");
  assert.equal(existing.finished_at, SHARED_FINISHED, "existing 已知结束测量");

  const incoming = reconcileLedger(null, [operation]).rounds;
  assert.equal(incoming.length, 1, "未受理的发送只生成一条账本轮次");
  assert.equal(incoming[0].status, "unknown", "账本轮次为未知状态");
  assert.equal(
    Object.hasOwn(incoming[0], "started_at"),
    false,
    "账本占位轮次自身不含起始时间字段",
  );
  assert.equal(
    Object.hasOwn(incoming[0], "finished_at"),
    false,
    "账本占位轮次自身不含结束时间字段",
  );

  const merged = mergeHistoryRounds([existing], incoming);
  assert.equal(merged.length, 1, "同 run_id 账本合并不新增轮次");
  assert.equal(merged[0].status, "unknown", "状态取账本侧的未知");
  assert.equal(
    merged[0].started_at,
    1_700_000_000_000,
    "账本侧缺省的时间字段不清除已知起始测量",
  );
  assert.equal(
    merged[0].finished_at,
    1_700_000_002_300,
    "账本侧缺省的时间字段不清除已知结束测量",
  );
  assert.equal(
    Object.hasOwn(merged[0], "started_at"),
    true,
    "合并结果显式携带起始时间字段",
  );
  assert.equal(
    Object.hasOwn(merged[0], "finished_at"),
    true,
    "合并结果显式携带结束时间字段",
  );
  assert.equal(merged[0].id, operation.operation_id, "轮次 id 仍为账本操作身份");
  assert.equal(merged[0].run_id, sharedRun, "运行身份取自账本侧");
  assert.equal(merged[0].entries.length, 1, "条目仍来自账本侧的单条用户输入");
  assert.equal(
    merged[0].pending?.length,
    1,
    "未确认操作仍然保留，等待运行查询或受理结果确认",
  );
  assert.equal(
    merged[0].pending?.[0].operation_id,
    operation.operation_id,
    "保留的未确认操作身份不变",
  );
  const markup = render(merged);
  assert.deepEqual(
    durations(markup),
    ["用时 2.3 秒"],
    "未知状态且测量齐备的合并轮次渲染用时",
  );
  assert.equal(count(markup, "提交结果未知"), 1, "未知操作的显式提示继续展示");
  assert.equal(count(markup, "重试"), 1, "未知操作的重试入口继续保留");
}

console.log(
  "SCOPE: 覆盖为契约解析（api.parseSessionHistory）、historyToRounds/reconcileLedger/mergeHistoryRounds/mergeRunTimes/operationRound 与 reactAgent 的 resumeReActRound/concludeReActRound 等真实生产模块，加上真实 ChatTranscript 服务端渲染；运行查询与重复受理两项为 mergeRunTimes/mergeHistoryRounds 的 helper 级覆盖。未执行：sessionRunManager 的真实运行查询轮询、重复受理与状态收敛链路（需隔离服务与真实 HTTP），归属真实浏览器 + 隔离服务验收。",
);
console.log(
  "PASS: 历史起止时间归属与渲染、四种终态用时、测量不完整零文本、零测量保留、同终态刷新补齐、快照与未知轮次不清除测量、运行查询与重复受理的时间归属（helper 级）、重开一致、多运行与资料节点隔离、单轮一条用时、确认与占位轮次零用时、既有展示回归与形状约束、执行中轮次携带完整测量仍零用时、未知轮次对较早 running 历史与未知账本的测量保留",
);
