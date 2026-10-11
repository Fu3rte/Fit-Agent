// 运行：node scripts/plan-generation-check.mjs
// 计划事件的展示绑定与刷新口径（plan-generation-contract §4、§6）：只有持久化成功的 prepare_plan 结果形成待确认展示，
// 同一调用的进度／执行完成／持久化结果合并为一项，实时与历史一致，保存落定与 saved 核实才刷新计划查询（不触达后端）。
import assert from "node:assert/strict";
import { createServer } from "vite";

const server = await createServer({
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});
const { applyReActEvent, planSaved } = await server.ssrLoadModule(
  "/src/features/chat/utils/reactAgent.ts",
);
const { historyToRounds, mergeHistoryRounds } = await server.ssrLoadModule(
  "/src/features/chat/utils/sessionHistory.ts",
);
const { default: PlanFields } = await server.ssrLoadModule(
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
const displayEntry = prepareNode;
const prepareCall = "tc-prepare";
const saveCall = "tc-save";
const statusCall = "tc-status";
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
const saveArguments = {
  proposal_id: proposalId,
  display_entry_id: displayEntry,
  confirmation_entry_id: confirmationEntry,
};
const saveResult = {
  proposal_id: proposalId,
  id: savedPlanId,
  content: payload,
  created_at: 1780272000000,
  saved_at: 1780272000000,
};
const saveResultText = JSON.stringify(saveResult);
const statusResult = (status, result) =>
  JSON.stringify({ proposal_id: proposalId, status, result });
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

/** 6. 历史恢复与实时同一口径，合并后不重复展示 */
const historyRounds = historyToRounds({
  session: { session_id: sessionId, title: "会话", active_leaf_id: prepareNode, created_at: 1, updated_at: 2 },
  entries: [
    { type: "message", entry_id: userEntry, parent_id: null, run_id: null, created_at: 1, message: { role: "user", text: "给我一份训练计划。", timestamp: 1, attachments: [] } },
    { type: "message", entry_id: assistantId, parent_id: userEntry, run_id: runId, created_at: 2, message: { role: "assistant", content: [callBlock(0, prepareCall, "prepare_plan", prepareCallSpec.args)], stop_reason: "toolUse", timestamp: 2 } },
    { type: "message", entry_id: prepareNode, parent_id: assistantId, run_id: runId, created_at: 3, message: { role: "toolResult", tool_call_id: prepareCall, tool_name: "prepare_plan", content: prepared, is_error: false, timestamp: 3 } },
  ],
  runs: [{ session_id: sessionId, run_id: runId, request_entry_id: userEntry, last_entry_id: prepareNode, status: "completed", started_at: 1, finished_at: 4, error_code: null, error_message: null }],
  steering: [],
});
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

/** 7. 保存落定凭据：仅保存成功结果与 saved 状态核实，且快照标识与调用参数一致 */
const resultOf = (callId, name, args, content, is_error = false) =>
  planSaved(
    event("tool_result", {
      tool_call_id: callId,
      content,
      is_error,
      entry_id: u(40),
      parent_id: assistantId,
    }),
    roundWithCalls([{ id: callId, name, args }]),
  );
assert.equal(resultOf(saveCall, "save_plan", saveArguments, saveResultText), true, "保存成功刷新计划查询");
assert.equal(resultOf(saveCall, "save_plan", saveArguments, saveResultText, true), false, "保存失败不刷新计划查询");
assert.equal(
  planSaved(
    event("tool_execution_end", {
      tool_call_id: saveCall,
      tool_name: "save_plan",
      content: saveResultText,
      is_error: false,
    }),
    roundWithCalls([{ id: saveCall, name: "save_plan", args: saveArguments }]),
  ),
  false,
  "执行完成通知不作为保存凭据",
);
assert.equal(resultOf(statusCall, "get_plan_save_status", { proposal_id: proposalId }, statusResult("saved", saveResult)), true, "核实原操作已保存刷新计划查询");
for (const status of ["pending", "processing", "invalidated", "conflicted"])
  assert.equal(
    resultOf(statusCall, "get_plan_save_status", { proposal_id: proposalId }, statusResult(status, null)),
    false,
    `${status} 状态不刷新计划查询`,
  );
assert.equal(resultOf(prepareCall, "prepare_plan", prepareCallSpec.args, prepared), false, "准备完成不刷新计划查询");
assert.equal(resultOf(queryCall, "get_current_plan", {}, currentPlanText), false, "当前计划查询不作为保存凭据");
const planRecord = {
  id: savedPlanId,
  is_current: true,
  created_at: 1780272000000,
  content: payload,
};
assert.equal(resultOf(queryCall, "list_plans", {}, JSON.stringify([planRecord])), false, "版本列表查询不作为保存凭据");
assert.equal(
  resultOf(statusCall, "get_plan_save_status", { proposal_id: u(50) }, statusResult("saved", saveResult)),
  false,
  "快照标识与调用参数不一致不作凭据",
);
assert.throws(
  () => resultOf(saveCall, "save_plan", saveArguments, statusResult("saved", null)),
  /计划保存结果/,
  "保存结果不合 schema 在解析位置报错",
);
assert.throws(
  () => resultOf(saveCall, "save_plan", saveArguments, JSON.stringify({ ...saveResult, is_current: true })),
  /含未定义字段/,
  "固定保存结果携带动态当前标记属协议异常",
);

/** 8. 保存落定按统一计划查询前缀失效，当前计划、版本列表与已加载详情同时覆盖，画像与训练记录查询保持 */
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
    `${key.join("/")} 随保存落定失效`,
  );
for (const key of otherKeys)
  assert.equal(
    queryClient.getQueryCache().find({ queryKey: key }).isStale(),
    false,
    `${key.join("/")} 不受计划刷新影响`,
  );
queryClient.clear();

/** 9. 业务展示渲染：全部字段可核对，建议标记只落在 suggested_fields 命中的字段上 */
const { renderToStaticMarkup } = await import("react-dom/server");
const { createElement } = await import("react");
const render = (suggestedFields) =>
  renderToStaticMarkup(
    createElement(PlanFields, {
      content: { ...payload, suggested_fields: suggestedFields },
    }),
  );
const badges = (html) => html.split(">建议<").length - 1;

const shown = render(payload.suggested_fields);
for (const text of [
  "循环执行",
  "第 1 天 · 训练日",
  "第 2 天 · 休息日",
  "推",
  "3 组",
  "目标 12 次",
  "时长未知",
  "重量未知",
  "目标次数未知",
  "时长 45 秒",
  "重量 0 kg",
  "单侧加载重量",
  "组间不安排休息",
  "组间休息未结构化指定",
  "组间可休息3–5分钟，结合心率恢复和自身状态判断。",
  "依据已保存画像安排。",
  "未在动作数据集内核实",
])
  assert.ok(shown.includes(text), `展示缺少业务内容：${text}`);
assert.equal(badges(render([])), 0, "没有建议字段时不出现建议标记");
assert.equal(
  badges(render(["/days/0/exercises/0/sets"])),
  1,
  "建议标记只落在命中的字段行",
);
assert.equal(badges(render(["/notes"])), 1, "整体依据的建议标记落在整体提示行");

console.log(
  "PASS: 待确认计划仅由持久化结果绑定、并行与历史同一口径且无重复展示、保存落定刷新计划查询、非 saved 不刷新、字段与建议来源完整渲染",
);
