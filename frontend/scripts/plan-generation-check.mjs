// 运行：node scripts/plan-generation-check.mjs
// 计划事件的展示绑定与刷新口径（plan-generation-contract §4、§6；structured-save-confirmation §7.3、§7.6）：
// 只有持久化成功的 prepare_plan 结果形成待确认展示，同一调用的进度／执行完成／持久化结果合并为一项，实时与历史一致；
// 已保存事实只来自确认操作对象，核实为 saved 才刷新计划查询（不触达后端）。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createServer } from "vite";

const server = await createServer({
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});
const { applyReActEvent } = await server.ssrLoadModule(
  "/src/features/chat/utils/reactAgent.ts",
);
const { historyToRounds, mergeHistoryRounds } = await server.ssrLoadModule(
  "/src/features/chat/utils/sessionHistory.ts",
);
const { parseSessionHistory } = await server.ssrLoadModule("/src/lib/api.ts");
const { PlanBoard: PlanFields } = await server.ssrLoadModule(
  "/src/features/plans/PlanFields.tsx",
);
const { PLAN_QUERY_KEY, queryClient } = await server.ssrLoadModule("/src/lib/query.ts");
await server.close();

const u = (n) => `${n.toString(16).padStart(8, "0")}-0000-4000-8000-000000000000`;
const runId = u(1);
const sessionId = u(2);
const userEntry = u(3);
const assistantId = u(4);
const prepareNode = u(5);
const proposalId = u(6);
const confirmationEntry = u(7);
const savedPlanId = u(8);
const prepareCall = "tc-prepare";
const queryCall = "tc-query";

/** 两条同名动作按顺序保留（重复动作不靠 exercise_id 区分），休息日动作列表为空 */
const payload = {
  repeat: true,
  days: [
    {
      kind: "training",
      focus: "推",
      exercises: [
        {
          exercise_id: "0409",
          name: "俯卧撑",
          sets: 3,
          reps: 12,
          duration_seconds: null,
          weight_kg: null,
          load_convention: null,
          rest_seconds: null,
        },
        {
          exercise_id: null,
          name: "俯卧撑",
          sets: 3,
          reps: null,
          duration_seconds: 45,
          weight_kg: 0,
          load_convention: "per_side",
          rest_seconds: 0,
        },
      ],
      notes: "组间可休息3–5分钟，结合心率恢复和自身状态判断。",
    },
    { kind: "rest", focus: null, exercises: [], notes: null },
  ],
  notes: "依据已保存画像安排。",
  suggested_fields: ["/days/0/exercises", "/days/1", "/notes"],
};
const proposal = {
  proposal_id: proposalId,
  base_profile_version: 2,
  base_plan_id: null,
  payload,
};
const prepared = JSON.stringify(proposal);
const saveResult = {
  proposal_id: proposalId,
  id: savedPlanId,
  content: payload,
  created_at: 1780272000000,
  saved_at: 1780272000000,
};
const currentPlanText = JSON.stringify({ id: savedPlanId, content: payload });

const event = (name, data) => ({ event: name, data: { run_id: runId, ...data } });
const callBlock = (index, callId, name, args) => ({
  content_index: index,
  type: "tool_call",
  tool_call_id: callId,
  name,
  arguments: args,
});

/** 助手节点提交工具调用后，逐个建立调用身份与工具名称关联 */
function roundWithCalls(calls) {
  const content = calls.map((call, index) =>
    callBlock(index, call.id, call.name, call.args),
  );
  let round = { id: runId, run_id: runId, entries: [], status: "running" };
  round = applyReActEvent(
    round,
    event("message_start", { message_id: assistantId, content: [] }),
  );
  round = applyReActEvent(
    round,
    event("message_end", {
      message_id: assistantId,
      content,
      stop_reason: "toolUse",
      entry_id: assistantId,
      parent_id: userEntry,
    }),
  );
  for (const call of calls)
    round = applyReActEvent(
      round,
      event("tool_start", {
        tool_call_id: call.id,
        name: call.name,
        arguments: call.args,
      }),
    );
  return round;
}

const prepareCallSpec = { id: prepareCall, name: "prepare_plan", args: { base_profile_version: 2, base_plan_id: null, payload } };
const preparedRound = () => roundWithCalls([prepareCallSpec]);
const entryOf = (round, callId) =>
  round.entries.find((item) => item.kind === "tool" && item.id === callId);

/** 1. 进度与执行完成不形成已持久化的待确认展示，同一调用不新增条目 */
let round = preparedRound();
for (const name of ["tool_execution_update", "tool_execution_end"])
  round = applyReActEvent(
    round,
    event(name, {
      tool_call_id: prepareCall,
      tool_name: "prepare_plan",
      content: prepared,
      is_error: false,
    }),
  );
assert.equal(entryOf(round, prepareCall).plan, undefined, "执行事件不冒充待确认展示");
assert.equal(round.entries.length, 2, "执行事件不产生额外展示条目");

/** 2. 持久化成功后完整绑定快照内容与结果节点身份 */
round = applyReActEvent(
  round,
  event("tool_result", {
    tool_call_id: prepareCall,
    content: prepared,
    is_error: false,
    entry_id: prepareNode,
    parent_id: assistantId,
  }),
);
const liveTool = entryOf(round, prepareCall);
assert.deepEqual(liveTool.plan, proposal, "待确认展示即快照的依据与完整 payload");
assert.equal(liveTool.entry_id, prepareNode, "结果节点身份即展示节点，前端不猜测 display_entry_id");
assert.equal(liveTool.id, prepareCall, "工具调用身份保留");
assert.equal(round.entries.length, 2, "同一调用合并为一项，无重复计划展示");
assert.deepEqual(
  liveTool.plan.payload.days[0].exercises.map((item) => item.name),
  ["俯卧撑", "俯卧撑"],
  "重复动作按实际顺序保留",
);

/** 3. 并行查询的不同完成顺序保持调用身份 */
let parallel = roundWithCalls([
  prepareCallSpec,
  { id: queryCall, name: "get_current_plan", args: {} },
]);
parallel = applyReActEvent(
  parallel,
  event("tool_result", {
    tool_call_id: queryCall,
    content: currentPlanText,
    is_error: false,
    entry_id: u(30),
    parent_id: assistantId,
  }),
);
assert.equal(entryOf(parallel, queryCall).plan, undefined, "查询结果不形成待确认展示");
parallel = applyReActEvent(
  parallel,
  event("tool_result", {
    tool_call_id: prepareCall,
    content: prepared,
    is_error: false,
    entry_id: prepareNode,
    parent_id: assistantId,
  }),
);
assert.deepEqual(entryOf(parallel, prepareCall).plan, proposal, "后到的准备结果仍绑定原调用");
assert.equal(entryOf(parallel, queryCall).plan, undefined, "查询调用身份不被准备结果改写");

/** 4. 失败结果按业务错误校验且不展示待确认计划；框架级纯文本失败保持通用展示 */
const failedRound = applyReActEvent(
  preparedRound(),
  event("tool_result", {
    tool_call_id: prepareCall,
    content: JSON.stringify({
      code: "invalid_business_payload",
      message: "字段无效。",
      errors: [{ path: "payload.days.0.exercises.0.sets", message: "组数无效。" }],
    }),
    is_error: true,
    entry_id: prepareNode,
    parent_id: assistantId,
  }),
);
assert.equal(entryOf(failedRound, prepareCall).plan, undefined, "失败结果不展示待确认计划");
assert.equal(entryOf(failedRound, prepareCall).status, "failed", "失败结果按失败展示");
const frameworkRound = applyReActEvent(
  preparedRound(),
  event("tool_result", {
    tool_call_id: prepareCall,
    content: "工具执行失败：参数缺少 payload。",
    is_error: true,
    entry_id: prepareNode,
    parent_id: assistantId,
  }),
);
assert.equal(entryOf(frameworkRound, prepareCall).plan, undefined, "框架级失败不展示待确认计划");

/** 4.1 失败内容两种形态的分流：JSON 对象严格校验，harness 说明文本原样保留 */
const failedResult = (content) =>
  applyReActEvent(
    preparedRound(),
    event("tool_result", {
      tool_call_id: prepareCall,
      content,
      is_error: true,
      entry_id: prepareNode,
      parent_id: assistantId,
    }),
  );
assert.equal(
  failedResult(
    JSON.stringify({ code: "plan_version_conflict", message: "当前计划已变化。" }),
  ).entries.at(-1).status,
  "failed",
  "业务错误对象通过校验并按失败展示",
);
for (const [label, content] of [
  ["缺 code 的对象", JSON.stringify({ detail: { code: "plan_not_found" } })],
  ["非计划错误码", JSON.stringify({ code: "workout_not_found", message: "错误码集合不一致。" })],
  ["内容校验错误缺字段列表", JSON.stringify({ code: "invalid_business_payload", message: "字段无效。" })],
  ["业务错误含未定义字段", JSON.stringify({ code: "plan_not_found", message: "缺少字段。", errors: [] })],
])
  assert.throws(
    () => failedResult(content),
    /无效|缺少|含未定义字段/,
    `${label} 在解析位置报错`,
  );
for (const content of [
  "工具执行失败：参数缺少 payload。",
  "参数预处理失败：base_plan_id 不是合法 UUID。",
  "权限检查失败：该工具未授权。",
  '{"code":"plan_not_found","message":"计划版本不存在。"}\n\n工具后处理失败：超时。',
])
  assert.equal(failedResult(content).entries.at(-1).content, content, "说明文本原样保留");

/** 5. 协议异常在解析位置报错 */
for (const [name, broken] of [
  ["含未定义字段", { ...proposal, extra: 1 }],
  ["训练日缺少动作", { ...proposal, payload: { ...payload, days: [{ ...payload.days[0], exercises: [] }] } }],
  ["动作缺少组数", { ...proposal, payload: { ...payload, days: [{ ...payload.days[0], exercises: [{ ...payload.days[0].exercises[0], sets: null }] }] } }],
  ["次数与时长同时未知", { ...proposal, payload: { ...payload, days: [{ ...payload.days[0], exercises: [{ ...payload.days[0].exercises[0], reps: null, duration_seconds: null }] }] } }],
  ["休息日含动作", { ...proposal, payload: { ...payload, days: [...payload.days, { ...payload.days[1], exercises: [payload.days[0].exercises[0]] }] } }],
  ["依据画像版本缺失", { ...proposal, base_profile_version: null }],
  ["非法快照标识", { ...proposal, proposal_id: "nope" }],
])
  assert.throws(
    () =>
      applyReActEvent(
        preparedRound(),
        event("tool_result", {
          tool_call_id: prepareCall,
          content: JSON.stringify(broken),
          is_error: false,
          entry_id: prepareNode,
          parent_id: assistantId,
        }),
      ),
    /无效|缺少|含未定义字段|含动作|为空|身份/,
    `${name} 在解析位置报错`,
  );

/** 6. 历史恢复与实时同一口径，合并后不重复展示；待确认引用与快照展示绑定同一结果节点 */
const pendingReference = {
  business_kind: "plan",
  proposal_id: proposalId,
  status: "pending",
  request_entry_id: userEntry,
  source_entry_id: assistantId,
  display_entry_id: prepareNode,
  confirmation_entry_id: null,
};
const historyBody = (confirmations, pending_proposals, extraEntries) => ({
  session: {
    session_id: sessionId,
    title: "会话",
    active_leaf_id: (extraEntries.at(-1) ?? { entry_id: prepareNode }).entry_id,
    created_at: 1,
    updated_at: 2,
  },
  entries: [
    { type: "message", entry_id: userEntry, parent_id: null, run_id: null, created_at: 1, message: { role: "user", text: "给我一份训练计划。", timestamp: 1, attachments: [] } },
    { type: "message", entry_id: assistantId, parent_id: userEntry, run_id: runId, created_at: 2, message: { role: "assistant", content: [callBlock(0, prepareCall, "prepare_plan", prepareCallSpec.args)], stop_reason: "toolUse", timestamp: 2 } },
    { type: "message", entry_id: prepareNode, parent_id: assistantId, run_id: runId, created_at: 3, message: { role: "toolResult", tool_call_id: prepareCall, tool_name: "prepare_plan", content: prepared, is_error: false, timestamp: 3 } },
    ...extraEntries,
  ],
  runs: [{ session_id: sessionId, run_id: runId, request_entry_id: userEntry, last_entry_id: prepareNode, status: "completed", started_at: 1, finished_at: 4, error_code: null, error_message: null }],
  steering: [],
  confirmations,
  pending_proposals,
});
const pendingHistory = parseSessionHistory(
  sessionId,
  historyBody([], [pendingReference], []),
);
assert.deepEqual(
  pendingHistory.pending_proposals,
  [pendingReference],
  "待确认快照引用逐字段完整恢复",
);
const historyRounds = historyToRounds(pendingHistory);
const historyTool = historyRounds[0].entries.at(-1);
assert.deepEqual(historyTool.plan, liveTool.plan, "实时与历史计划展示一致");
assert.equal(historyTool.id, liveTool.id, "计划展示按工具调用身份去重");
const merged = mergeHistoryRounds(
  [{ id: runId, run_id: runId, request_entry_id: userEntry, entries: round.entries, status: "running" }],
  historyRounds,
);
assert.equal(
  merged[0].entries.filter((item) => item.kind === "tool" && item.plan !== undefined).length,
  1,
  "历史刷新不重复待确认计划展示",
);

/** 7. 已保存事实只来自确认操作对象（确认契约 §7.3、§7.6）：saved 的固定结果完整校验，状态配对与 schema 异常就地失败 */
const savedOperation = {
  session_id: sessionId,
  operation_id: u(60),
  business_kind: "plan",
  proposal_id: proposalId,
  display_entry_id: prepareNode,
  confirmation_entry_id: confirmationEntry,
  status: "saved",
  result: saveResult,
  error: null,
  created_at: 1780272000000,
  updated_at: 1780272000000,
};
const confirmationNodeEntry = {
  type: "message",
  entry_id: confirmationEntry,
  parent_id: prepareNode,
  run_id: null,
  created_at: 4,
  message: { role: "user", text: "确认保存计划", timestamp: 4, attachments: [] },
};
const confirmedHistory = (operations) =>
  parseSessionHistory(sessionId, historyBody(operations, [], [confirmationNodeEntry]));
const savedHistory = confirmedHistory([savedOperation]);
assert.deepEqual(
  savedHistory.confirmations[0].result,
  saveResult,
  "saved 的计划固定保存结果逐字段完整恢复",
);
assert.deepEqual(
  savedHistory.confirmations[0].result.content,
  payload,
  "保存结果内容与待确认快照 payload 一致",
);
const confirmationRound = historyToRounds(savedHistory).at(-1);
assert.equal(
  confirmationRound.id,
  `confirmation:${confirmationEntry}`,
  "确认节点独立成轮，不属于任何 Agent 运行",
);
assert.equal(confirmationRound.entries.length, 1, "确认轮次不伪造助手消息");
assert.equal(confirmationRound.entries[0].kind, "user", "确认轮次仅含真实确认用户节点");
assert.deepEqual(
  confirmationRound.confirmation,
  { business_kind: "plan", proposal_id: proposalId, status: "saved", text: "已保存" },
  "确认展示只陈述真实操作状态",
);
for (const [status, extra, display] of [
  ["processing", { result: null, error: null }, { status: "processing", text: "确认处理中" }],
  [
    "failed",
    { result: null, error: { code: "plan_version_conflict", message: "当前计划已变化。" } },
    { status: "failed", text: "保存失败", detail: "当前计划已变化。" },
  ],
])
  assert.deepEqual(
    historyToRounds(confirmedHistory([{ ...savedOperation, status, ...extra }])).at(-1)
      .confirmation,
    { business_kind: "plan", proposal_id: proposalId, ...display },
    `${status} 状态按真实操作对象展示`,
  );

/** 7.1 历史响应的确认数组为必填，配对与节点绑定异常在解析位置失败 */
for (const field of ["confirmations", "pending_proposals"]) {
  const missing = historyBody([], [pendingReference], []);
  delete missing[field];
  assert.throws(
    () => parseSessionHistory(sessionId, missing),
    /无效/,
    `历史响应缺少 ${field} 属协议异常`,
  );
}
const rejectedOperation = (values, message) =>
  assert.throws(
    () => confirmedHistory([{ ...savedOperation, ...values }]),
    /无效|缺少|含未定义字段|不一致|携带|配对|晚于/,
    message,
  );
rejectedOperation({ status: "processing" }, "处理中确认操作携带结果属协议异常");
rejectedOperation({ status: "saved", result: null }, "已保存确认操作缺少结果属协议异常");
rejectedOperation({ status: "failed" }, "失败确认操作缺少错误对象属协议异常");
rejectedOperation(
  { result: { ...saveResult, is_current: true } },
  "固定结果携带动态当前标记属协议异常",
);
rejectedOperation(
  { result: { ...saveResult, saved_at: saveResult.saved_at + 1 } },
  "保存时间与创建时间必须一致",
);
rejectedOperation(
  { result: { ...saveResult, proposal_id: u(61) } },
  "结果快照标识与操作标识必须一致",
);
rejectedOperation({ confirmation_entry_id: userEntry }, "确认节点未晚于完整展示属协议异常");
assert.throws(
  () =>
    parseSessionHistory(
      sessionId,
      historyBody([], [{ ...pendingReference, display_entry_id: assistantId }], []),
    ),
  /无效/,
  "待确认引用的展示节点必须是工具结果节点",
);

/** 8. 确认 saved 后按统一计划查询前缀失效，当前计划、版本列表与已加载详情同时覆盖，画像与训练记录查询保持 */
const planKeys = [
  [...PLAN_QUERY_KEY, "current"],
  [...PLAN_QUERY_KEY, "list"],
  [...PLAN_QUERY_KEY, "record", savedPlanId],
];
const otherKeys = [["profile"], ["workout", "list"], ["profile_saved"]];
for (const key of [...planKeys, ...otherKeys])
  queryClient.setQueryData(key, { seeded: true });
await queryClient.invalidateQueries({ queryKey: PLAN_QUERY_KEY });
for (const key of planKeys)
  assert.equal(
    queryClient.getQueryCache().find({ queryKey: key }).isStale(),
    true,
    `${key.join("/")} 随确认 saved 失效`,
  );
for (const key of otherKeys)
  assert.equal(
    queryClient.getQueryCache().find({ queryKey: key }).isStale(),
    false,
    `${key.join("/")} 不受计划刷新影响`,
  );
queryClient.clear();

// 已知训练参数与原始说明在统一看板中展示。
const { renderToStaticMarkup } = await import("react-dom/server");
const { createElement } = await import("react");
const render = (suggestedFields) =>
  renderToStaticMarkup(
    createElement(PlanFields, {
      content: { ...payload, suggested_fields: suggestedFields },
      equipment: Object.fromEntries(
        JSON.parse(readFileSync(new URL("../../backend/dataset/actions.agent.json", import.meta.url), "utf8"))
          .map((exercise) => [exercise.id, exercise.equipment]),
      ),
    }),
  );

const shown = render(payload.suggested_fields);
for (const text of [
  "Day 1 · 推",
  "Day 2",
  "训练日",
  "休息日",
  "3×12",
  "3×45s",
  "0kg（单侧加载重量）",
  "组间可休息3–5分钟，结合心率恢复和自身状态判断。",
  "依据已保存画像安排。",
  "未在动作数据集内核实",
])
  assert.ok(shown.includes(text), `展示缺少业务内容：${text}`);
assert.ok(!shown.includes("未知") && !shown.includes("循环执行"), "空值占位与循环方式不展示");
assert.equal(render([]), shown, "来源元数据不改变展示");
assert.equal(render(["/days/0/exercises/0/sets", "/notes"]), shown);
assert.ok(!shown.includes(">建议<") && !shown.includes("不安排休息"));

console.log(
  "PASS: 待确认计划仅由持久化结果绑定、并行与历史同一口径且无重复展示、已保存事实仅来自确认操作对象且 saved 刷新计划查询、已知字段与原始说明完整渲染",
);
