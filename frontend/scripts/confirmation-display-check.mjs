// 运行：node scripts/confirmation-display-check.mjs
// 纯前端展示检查：确认按钮与快照标识及展示节点的绑定、待确认状态与占用下的可用性、
// 确认节点与真实结果的独立展示、确认结果不提供模型重新生成入口。
// 渲染树夹具 frontend/temp/confirmation-display-tree.tsx 与被测组件同一模块图。
import assert from "node:assert/strict";
import { createServer } from "vite";

const server = await createServer({
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});
const api = await server.ssrLoadModule("/src/lib/api.ts");
const history = await server.ssrLoadModule(
  "/src/features/chat/utils/sessionHistory.ts",
);
const confirmations = await server.ssrLoadModule(
  "/src/features/chat/utils/confirmations.ts",
);
const { renderTranscript, primeEquipment } = await server.ssrLoadModule(
  "/temp/confirmation-display-tree.tsx",
);
await server.close();
// 目录查询无数据时看板不渲染，先落到应用级缓存再展示
primeEquipment({});

const u = (n) =>
  `${n.toString(16).padStart(8, "0")}-0000-4000-8000-000000000000`;
const sessionId = u(1);
const runId = u(2);
const ids = {
  request: u(3),
  planSource: u(4),
  planDisplay: u(5),
  workoutAddSource: u(6),
  workoutAddDisplay: u(7),
  workoutUpdateSource: u(8),
  workoutUpdateDisplay: u(9),
  confirmation: u(10),
};
const proposalIds = {
  plan: u(20),
  workoutAdd: u(21),
  workoutUpdate: u(22),
};

const userNode = (entryId, parentId, text) => ({
  type: "message",
  entry_id: entryId,
  parent_id: parentId,
  run_id: null,
  created_at: 1,
  message: { role: "user", text, timestamp: 1, attachments: [] },
});
const callNode = (entryId, parentId, toolCallId, name, argumentsValue) => ({
  type: "message",
  entry_id: entryId,
  parent_id: parentId,
  run_id: runId,
  created_at: 1,
  message: {
    role: "assistant",
    content: [
      {
        content_index: 0,
        type: "tool_call",
        tool_call_id: toolCallId,
        name,
        arguments: argumentsValue,
      },
    ],
    stop_reason: "toolUse",
    timestamp: 1,
  },
});
const resultNode = (entryId, parentId, toolCallId, name, payload) => ({
  type: "message",
  entry_id: entryId,
  parent_id: parentId,
  run_id: runId,
  created_at: 1,
  message: {
    role: "toolResult",
    tool_call_id: toolCallId,
    tool_name: name,
    content: JSON.stringify(payload),
    is_error: false,
    timestamp: 1,
  },
});

const planPayload = {
  proposal_id: proposalIds.plan,
  base_profile_version: 4,
  base_plan_id: null,
  payload: {
    repeat: null,
    days: [
      {
        kind: "training",
        focus: " PLAN_MARKER ",
        exercises: [
          {
            exercise_id: null,
            name: "PLAN_EXERCISE",
            sets: 4,
            reps: 8,
            duration_seconds: null,
            weight_kg: null,
            load_convention: null,
            rest_seconds: null,
          },
        ],
        notes: null,
      },
    ],
    notes: null,
    suggested_fields: [],
  },
};
const workoutAddPayload = {
  proposal_id: proposalIds.workoutAdd,
  performed_on: "2026-10-11",
  base_workout_id: null,
  base_workout_version: null,
  payload: {
    exercises: [
      {
        exercise_id: null,
        name: "WORKOUT_ADD_EXERCISE",
        load_convention: null,
        sets: [{ reps: 5, weight_kg: null, duration_seconds: null }],
      },
    ],
    notes: null,
  },
};
const workoutUpdatePayload = {
  ...workoutAddPayload,
  proposal_id: proposalIds.workoutUpdate,
  performed_on: "2026-10-12",
  base_workout_id: u(30),
  base_workout_version: 2,
  payload: {
    exercises: [
      {
        exercise_id: null,
        name: "WORKOUT_UPDATE_EXERCISE",
        load_convention: null,
        sets: [{ reps: 6, weight_kg: null, duration_seconds: null }],
      },
    ],
    notes: null,
  },
};

const entries = [
  userNode(ids.request, null, "整理计划与训练记录"),
  callNode(ids.planSource, ids.request, "tc-plan", "prepare_plan", planPayload),
  resultNode(
    ids.planDisplay,
    ids.planSource,
    "tc-plan",
    "prepare_plan",
    planPayload,
  ),
  callNode(
    ids.workoutAddSource,
    ids.planDisplay,
    "tc-add",
    "prepare_workout",
    workoutAddPayload,
  ),
  resultNode(
    ids.workoutAddDisplay,
    ids.workoutAddSource,
    "tc-add",
    "prepare_workout",
    workoutAddPayload,
  ),
  callNode(
    ids.workoutUpdateSource,
    ids.workoutAddDisplay,
    "tc-update",
    "prepare_workout",
    workoutUpdatePayload,
  ),
  resultNode(
    ids.workoutUpdateDisplay,
    ids.workoutUpdateSource,
    "tc-update",
    "prepare_workout",
    workoutUpdatePayload,
  ),
];
const reference = (kind, proposalId, displayEntryId, requestEntryId, overrides) => ({
  business_kind: kind,
  proposal_id: proposalId,
  status: "pending",
  request_entry_id: requestEntryId ?? ids.request,
  source_entry_id: overrides?.source_entry_id ?? ids.planSource,
  display_entry_id: displayEntryId,
  confirmation_entry_id: null,
  ...overrides,
});
const baseWire = (overrides) => ({
  session: {
    session_id: sessionId,
    title: "会话",
    active_leaf_id: overrides?.active_leaf_id ?? ids.workoutUpdateDisplay,
    created_at: 1,
    updated_at: 2,
  },
  entries: overrides?.entries ?? entries,
  runs: [
    {
      session_id: sessionId,
      run_id: runId,
      request_entry_id: ids.request,
      last_entry_id: overrides?.last_entry_id ?? ids.workoutUpdateDisplay,
      status: "completed",
      started_at: 1,
      finished_at: 2,
      error_code: null,
      error_message: null,
    },
  ],
  steering: [],
  confirmations: overrides?.confirmations ?? [],
  pending_proposals:
    overrides?.pending_proposals ??
    [
      reference("plan", proposalIds.plan, ids.planDisplay),
      reference("workout", proposalIds.workoutAdd, ids.workoutAddDisplay, null, {
        source_entry_id: ids.workoutAddSource,
      }),
      reference(
        "workout",
        proposalIds.workoutUpdate,
        ids.workoutUpdateDisplay,
        null,
        { source_entry_id: ids.workoutUpdateSource },
      ),
    ],
});
const workoutSaveResult = (payload, id, version) => ({
  proposal_id: payload.proposal_id,
  id,
  performed_on: payload.performed_on,
  version,
  content: payload.payload,
  created_at: 1_700_000_000_000,
  updated_at: 1_700_000_000_000,
  saved_at: 1_700_000_000_000,
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
const count = (markup, needle) => markup.split(needle).length - 1;

/* ===== 1. 三类快照各自绑定确认入口 ===== */
{
  const parsed = api.parseSessionHistory(sessionId, baseWire());
  const rounds = history.historyToRounds(parsed);
  const markup = render(rounds, { proposals: parsed.pending_proposals });
  assert.equal(count(markup, "确认保存计划"), 1, "计划快照有一个绑定入口");
  assert.equal(count(markup, "确认保存训练记录"), 2, "两份训练记录各自有入口");
  assert.equal(markup.includes("确认保存画像"), false, "无画像快照则无画像入口");
  const planAt = markup.indexOf("PLAN_EXERCISE");
  const addAt = markup.indexOf("WORKOUT_ADD_EXERCISE");
  const updateAt = markup.indexOf("WORKOUT_UPDATE_EXERCISE");
  const planButton = markup.indexOf("确认保存计划");
  const buttons = [
    markup.indexOf("确认保存训练记录"),
    markup.lastIndexOf("确认保存训练记录"),
  ];
  assert.ok(
    planAt >= 0 && addAt >= 0 && updateAt >= 0,
    "三份完整内容均已展示",
  );
  assert.ok(planAt < planButton, "入口位于该份完整内容之后");
  assert.ok(
    planButton < addAt && addAt < buttons[0] && buttons[0] < updateAt,
    "新增记录入口绑定其自身展示节点",
  );
  assert.ok(updateAt < buttons[1], "更新记录入口绑定其自身展示节点");
  assert.equal(
    markup.includes("<button") && markup.includes("确认保存计划</button>"),
    true,
    "待确认快照的入口可用",
  );
}

/* ===== 2. 占用与失效状态不开放新的提交 ===== */
{
  const parsed = api.parseSessionHistory(sessionId, baseWire());
  const rounds = history.historyToRounds(parsed);
  const blocked = render(rounds, {
    proposals: parsed.pending_proposals,
    canConfirm: false,
  });
  assert.equal(count(blocked, 'disabled=""'), 3, "执行期间或历史未就绪时全部禁用");
  const processing = api.parseSessionHistory(
    sessionId,
    baseWire({
      pending_proposals: [
        reference("plan", proposalIds.plan, ids.planDisplay, null, {
          status: "processing",
        }),
      ],
    }),
  );
  const processingMarkup = render(history.historyToRounds(processing), {
    proposals: processing.pending_proposals,
  });
  assert.equal(
    count(processingMarkup, "确认保存"),
    0,
    "processing 与失效快照不提供新的提交入口",
  );
  assert.equal(
    processingMarkup.includes("PLAN_EXERCISE"),
    true,
    "处理中快照的完整内容继续展示",
  );
  const saved = api.parseSessionHistory(
    sessionId,
    baseWire({ pending_proposals: [] }),
  );
  const savedMarkup = render(history.historyToRounds(saved), {
    proposals: saved.pending_proposals,
  });
  assert.equal(
    count(savedMarkup, "确认保存"),
    0,
    "已保存与失效快照不提供新的提交",
  );
}

/* ===== 3. 确认节点与真实结果独立展示，且不提供模型重新生成 ===== */
{
  const confirmed = [
    ...entries,
    userNode(ids.confirmation, ids.workoutUpdateDisplay, "确认保存训练记录"),
  ];
  const operation = {
    session_id: sessionId,
    operation_id: u(40),
    business_kind: "workout",
    proposal_id: proposalIds.workoutUpdate,
    display_entry_id: ids.workoutUpdateDisplay,
    confirmation_entry_id: ids.confirmation,
    status: "saved",
    result: workoutSaveResult(workoutUpdatePayload, u(41), 3),
    error: null,
    created_at: 1,
    updated_at: 2,
  };
  const parsed = api.parseSessionHistory(
    sessionId,
    baseWire({
      entries: confirmed,
      active_leaf_id: ids.confirmation,
      last_entry_id: ids.workoutUpdateDisplay,
      confirmations: [operation, { ...operation, operation_id: u(42) }],
      pending_proposals: [
        reference("plan", proposalIds.plan, ids.planDisplay),
        reference("workout", proposalIds.workoutAdd, ids.workoutAddDisplay, null, {
          source_entry_id: ids.workoutAddSource,
        }),
      ],
    }),
  );
  const rounds = history.historyToRounds(parsed);
  const markup = render(rounds, { proposals: parsed.pending_proposals });
  const round = rounds.find((item) => item.confirmation !== undefined);
  assert.equal(round.id, `confirmation:${ids.confirmation}`);
  assert.equal(round.run_id, undefined, "确认结果无对应 Agent 运行");
  assert.equal(round.confirmation.text, "已保存");
  assert.equal(count(markup, "已保存"), 1, "重复结果按确认节点去重展示");
  assert.equal(
    count(markup, "确认保存训练记录 · 已保存"),
    1,
    "确认结果行只出现一次",
  );
  assert.equal(
    count(markup, "确认保存训练记录"),
    3,
    "另一份待确认入口、确认节点正文与结果行各一次",
  );
  assert.equal(
    count(markup, 'aria-label="重新生成"'),
    count(markup, 'aria-label="编辑消息"') - 1,
    "确认节点不提供模型重新生成入口，普通用户节点保持原有入口",
  );
  assert.equal(markup.includes("WORKOUT_UPDATE_EXERCISE"), true);
}

/* ===== 4. 失败结果展示实际原因 ===== */
{
  const failed = {
    session_id: sessionId,
    operation_id: u(50),
    business_kind: "plan",
    proposal_id: proposalIds.plan,
    display_entry_id: ids.planDisplay,
    confirmation_entry_id: ids.confirmation,
    status: "failed",
    result: null,
    error: {
      code: "invalid_business_payload",
      message: "内容校验失败。",
      errors: [{ path: "/days/0", message: "训练日缺少动作" }],
    },
    created_at: 1,
    updated_at: 2,
  };
  const parsed = api.parseSessionHistory(
    sessionId,
    baseWire({
      entries: [
        ...entries,
        userNode(ids.confirmation, ids.workoutUpdateDisplay, "确认保存计划"),
      ],
      active_leaf_id: ids.confirmation,
      last_entry_id: ids.workoutUpdateDisplay,
      confirmations: [failed],
      pending_proposals: [],
    }),
  );
  const markup = render(history.historyToRounds(parsed), {
    proposals: parsed.pending_proposals,
  });
  assert.ok(markup.includes("保存失败"));
  assert.ok(markup.includes("训练日缺少动作"), "展示实际字段错误");
  assert.equal(markup.includes("已保存"), false);
}

/* ===== 5. 多份待确认内容分别确认：空闲时各自独立，任一处理中全部占用 ===== */
{
  const attempts = [
    {
      session_id: sessionId,
      request: {
        operation_id: u(60),
        business_kind: "plan",
        proposal_id: proposalIds.plan,
        display_entry_id: ids.planDisplay,
      },
      created_at: 1,
      phase: "processing",
      operation: null,
    },
  ];
  const proposals = [
    reference("plan", proposalIds.plan, ids.planDisplay),
    reference("workout", proposalIds.workoutAdd, ids.workoutAddDisplay, null, {
      source_entry_id: ids.workoutAddSource,
    }),
  ];
  assert.deepEqual(
    [
      confirmations.confirmationButtonState(
        "plan",
        proposalIds.plan,
        ids.planDisplay,
        proposals,
        [],
        true,
      ),
      confirmations.confirmationButtonState(
        "workout",
        proposalIds.workoutAdd,
        ids.workoutAddDisplay,
        proposals,
        [],
        true,
      ),
    ],
    ["available", "available"],
    "空闲时两份快照分别可确认",
  );
  assert.deepEqual(
    [
      confirmations.confirmationButtonState(
        "plan",
        proposalIds.plan,
        ids.planDisplay,
        proposals,
        attempts,
        true,
      ),
      confirmations.confirmationButtonState(
        "workout",
        proposalIds.workoutAdd,
        ids.workoutAddDisplay,
        proposals,
        attempts,
        true,
      ),
    ],
    ["busy", "busy"],
    "一份处理中时全部入口保持占用（§7.1）",
  );
  assert.equal(
    confirmations.confirmationButtonState(
      "workout",
      proposalIds.workoutAdd,
      u(99),
      proposals,
      [],
      true,
    ),
    "unavailable",
    "展示节点与引用不匹配时不提供入口",
  );
  assert.equal(
    confirmations.confirmationButtonState(
      "workout",
      proposalIds.workoutAdd,
      ids.workoutAddDisplay,
      [],
      [],
      true,
    ),
    "unavailable",
    "失效与已保存快照不开放提交",
  );
  assert.equal(
    confirmations.confirmationButtonState(
      "workout",
      proposalIds.workoutAdd,
      ids.workoutAddDisplay,
      proposals,
      [],
      false,
    ),
    "busy",
    "Agent 执行期间禁用确认按钮",
  );
}

console.log(
  "PASS: 三类快照入口绑定与顺序、占用与失效不开放提交、确认节点独立展示与去重、失败原因展示、多份内容分别确认",
);
