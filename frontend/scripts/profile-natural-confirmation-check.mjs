// 运行：node scripts/profile-natural-confirmation-check.mjs
// 纯函数检查（profile-natural-confirmation-plan §3.2、§4、§9）：准备结果的完整画像展示仅在结果节点提交后出现、
// 实时与历史同一解析口径且无重复展示、保存与状态查询核实落定后才刷新个人页、失败与协议异常不视为保存成功（不触达后端）。
import assert from "node:assert/strict";
import { createServer } from "vite";

const server = await createServer({
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});
const { applyReActEvent, profileSaved } = await server.ssrLoadModule(
  "/src/features/chat/utils/reactAgent.ts",
);
const { historyToRounds, mergeHistoryRounds } = await server.ssrLoadModule(
  "/src/features/chat/utils/sessionHistory.ts",
);
await server.close();

const u = (n) => `${n.toString(16).padStart(8, "0")}-0000-4000-8000-000000000000`;
const runId = u(1);
const sessionId = u(2);
const userEntry = u(3);
const assistantId = u(4);
const prepareNode = u(5);
const proposalId = u(6);
const confirmationEntry = u(7);
const prepareCall = "tc-prepare";
const saveCall = "tc-save";
const statusCall = "tc-status";
const otherCall = "tc-profile";

/** 完整画像：文本未知用 null，器械清单未知用 null，禁用动作明确为空（§11.9） */
const payload = {
  goal: "增肌",
  experience: null,
  environment: "家庭",
  availability: "每周三晚",
  health_notes: null,
  movement_restrictions: null,
  unavailable_equipment: null,
  forbidden_exercise_ids: [],
};
const prepareArguments = { profile_id: 1, base_profile_version: 2, payload };
const saveArguments = {
  proposal_id: proposalId,
  display_entry_id: prepareNode,
  confirmation_entry_id: confirmationEntry,
};
const statusArguments = { proposal_id: proposalId };
const prepared = JSON.stringify({
  proposal_id: proposalId,
  profile_id: 1,
  base_profile_version: 2,
  payload,
});
const saveResult = JSON.stringify({
  proposal_id: proposalId,
  profile_id: 1,
  version: 3,
  content: payload,
  saved_at: 1780000000000,
});
const statusResult = (status, result) =>
  JSON.stringify({ proposal_id: proposalId, status, result });

const event = (name, data) => ({ event: name, data: { run_id: runId, ...data } });
const callBlock = (callId, name, args) => ({
  content_index: 0,
  type: "tool_call",
  tool_call_id: callId,
  name,
  arguments: args,
});

/** 一次工具调用：助手节点已提交工具调用块，工具条目处于执行中 */
function roundWithTool(callId, name, args) {
  const content = [callBlock(callId, name, args)];
  let round = { id: runId, run_id: runId, entries: [], status: "running" };
  round = applyReActEvent(round, event("message_start", { message_id: assistantId, content: [] }));
  round = applyReActEvent(round, event("message_end", {
    message_id: assistantId,
    content,
    stop_reason: "toolUse",
    entry_id: assistantId,
    parent_id: userEntry,
  }));
  return applyReActEvent(round, event("tool_start", { tool_call_id: callId, name, arguments: args }));
}

const preparedRound = () => roundWithTool(prepareCall, "prepare_profile_update", prepareArguments);

/** 1. 执行通知不形成画像展示：tool_execution_end 没有结果节点身份 */
let round = preparedRound();
assert.equal(round.entries.at(-1).profile, undefined, "调用中无画像展示");
round = applyReActEvent(round, event("tool_execution_end", {
  tool_call_id: prepareCall,
  tool_name: "prepare_profile_update",
  content: prepared,
  is_error: false,
}));
assert.equal(round.entries.at(-1).profile, undefined, "执行完成通知不冒充已持久化节点");
assert.equal(round.entries.length, 2, "执行通知不产生额外展示条目");

/** 2. 结果节点提交后才展示，展示内容即快照完整 payload（未知值与空列表保持原样） */
round = applyReActEvent(round, event("tool_result", {
  tool_call_id: prepareCall,
  content: prepared,
  is_error: false,
  entry_id: prepareNode,
  parent_id: assistantId,
}));
const liveTool = round.entries.at(-1);
assert.equal(liveTool.kind, "tool");
assert.deepEqual(liveTool.profile, payload, "展示内容即快照完整 payload");
assert.equal(liveTool.entry_id, prepareNode, "结果节点身份保留为展示节点");
assert.equal(liveTool.id, prepareCall, "工具调用身份保留");
assert.equal(round.entries.length, 2, "实时展示不新增条目，无重复画像");

/** 3. 历史恢复与实时同一口径、同一去重身份 */
const historyRounds = historyToRounds({
  session: { session_id: sessionId, title: "会话", active_leaf_id: prepareNode, created_at: 1, updated_at: 2 },
  entries: [
    { entry_id: userEntry, parent_id: null, run_id: null, created_at: 1, message: { role: "user", text: "整理画像。", timestamp: 1 } },
    { entry_id: assistantId, parent_id: userEntry, run_id: runId, created_at: 2, message: { role: "assistant", content: [callBlock(prepareCall, "prepare_profile_update", prepareArguments)], stop_reason: "toolUse", timestamp: 2 } },
    { entry_id: prepareNode, parent_id: assistantId, run_id: runId, created_at: 3, message: { role: "toolResult", tool_call_id: prepareCall, tool_name: "prepare_profile_update", content: prepared, is_error: false, timestamp: 3 } },
  ],
  runs: [{ session_id: sessionId, run_id: runId, request_entry_id: userEntry, last_entry_id: prepareNode, status: "completed", started_at: 1, finished_at: 4, error_code: null, error_message: null }],
  steering: [],
});
assert.equal(historyRounds.length, 1, "历史重建为单一轮次");
const historyTool = historyRounds[0].entries.at(-1);
assert.deepEqual(historyTool.profile, liveTool.profile, "实时与历史画像展示一致");
assert.equal(historyTool.id, liveTool.id, "画像展示按工具调用身份去重");

/** 3.1 历史刷新并入本页仍在执行的轮次时，画像仍只出现一次 */
const merged = mergeHistoryRounds(
  [{ id: runId, run_id: runId, request_entry_id: userEntry, entries: round.entries, status: "running" }],
  historyRounds,
);
assert.equal(merged.length, 1, "同运行合并为单一轮次");
assert.equal(
  merged[0].entries.filter((item) => item.kind === "tool" && item.profile !== undefined).length,
  1,
  "历史刷新不重复画像展示",
);

/** 4. 快照内容不合 schema 在解析位置报错；失败结果不形成画像展示 */
assert.throws(
  () => applyReActEvent(preparedRound(), event("tool_result", {
    tool_call_id: prepareCall,
    content: JSON.stringify({ ...JSON.parse(prepared), extra: 1 }),
    is_error: false,
    entry_id: prepareNode,
    parent_id: assistantId,
  })),
  /含未定义字段/,
  "快照含未定义字段在解析位置报错",
);
const failedRound = applyReActEvent(preparedRound(), event("tool_result", {
  tool_call_id: prepareCall,
  content: JSON.stringify({ code: "invalid_business_payload", message: "字段无效。", errors: [{ path: "/goal", message: "缺少内容。" }] }),
  is_error: true,
  entry_id: prepareNode,
  parent_id: assistantId,
}));
assert.equal(failedRound.entries.at(-1).profile, undefined, "失败结果不展示画像");
assert.equal(failedRound.entries.at(-1).status, "failed", "失败结果按失败展示");

/** 5. 保存落定：仅保存工具成功结果与状态查询核实 saved 作为刷新个人页的凭据 */
const resultOf = (callId, name, args, content, is_error = false) => profileSaved(
  event("tool_result", { tool_call_id: callId, content, is_error, entry_id: u(20), parent_id: assistantId }),
  roundWithTool(callId, name, args),
);
assert.equal(resultOf(saveCall, "save_profile_update", saveArguments, saveResult), true, "保存成功刷新个人页");
assert.equal(resultOf(saveCall, "save_profile_update", saveArguments, saveResult, true), false, "保存失败不刷新个人页");
assert.equal(
  profileSaved(event("tool_execution_end", { tool_call_id: saveCall, tool_name: "save_profile_update", content: saveResult, is_error: false }), roundWithTool(saveCall, "save_profile_update", saveArguments)),
  false,
  "执行完成通知不作为保存凭据",
);
assert.equal(resultOf(statusCall, "get_profile_update_status", statusArguments, statusResult("saved", JSON.parse(saveResult))), true, "核实原操作已保存刷新个人页");
for (const status of ["pending", "processing", "invalidated", "conflicted"])
  assert.equal(resultOf(statusCall, "get_profile_update_status", statusArguments, statusResult(status, null)), false, `${status} 状态不刷新个人页`);
assert.equal(resultOf(statusCall, "get_profile_update_status", { proposal_id: u(30) }, statusResult("saved", JSON.parse(saveResult))), false, "快照标识与调用参数不一致不作凭据");
assert.equal(resultOf(otherCall, "get_profile", {}, JSON.stringify({ version: 3, content: payload })), false, "画像查询不作为保存凭据");
assert.throws(
  () => resultOf(saveCall, "save_profile_update", saveArguments, statusResult("saved", null)),
  /画像保存结果/,
  "保存结果不合 schema 在解析位置报错",
);

console.log("PASS: 画像完整展示、实时与历史一致、保存与状态查询落定刷新");
