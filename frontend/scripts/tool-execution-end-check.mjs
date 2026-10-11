// 运行：node scripts/tool-execution-end-check.mjs
// 纯状态检查：完成/进度事件解析、执行完成与保存确认状态转换、调用位置稳定、去重、错误与取消、历史衔接（不触达后端）。
import assert from "node:assert/strict";
import { createServer } from "vite";

const server = await createServer({
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});
const { applyReActEvent, createReActParser } = await server.ssrLoadModule(
  "/src/features/chat/utils/reactAgent.ts",
);
const { historyToRounds, mergeHistoryRounds } = await server.ssrLoadModule(
  "/src/features/chat/utils/sessionHistory.ts",
);

const u = (n) => `${n.toString(16).padStart(8, "0")}-0000-4000-8000-000000000000`;
const runId = u(1);
const assistantId = u(2);
const userEntry = u(3);
const nodeSlow = u(4);
const nodeFast = u(5);
const requestEntry = u(6);

const event = (name, data) => ({ event: name, data: { run_id: runId, ...data } });
const apply = (round, name, data) => applyReActEvent(round, event(name, data));

const callBlock = (index, id, name, args) => ({ content_index: index, type: "tool_call", tool_call_id: id, name, arguments: args });
const content = [
  { content_index: 0, type: "text", text: "调用工具。" },
  callBlock(1, "tc-slow", "read", { path: "slow.txt" }),
  callBlock(2, "tc-fast", "ls", { path: "." }),
];
const toolNames = { "tc-slow": "read", "tc-fast": "ls" };

function startBatch() {
  let round = { id: runId, run_id: runId, entries: [], status: "running" };
  round = apply(round, "message_start", { message_id: assistantId, content });
  round = apply(round, "message_end", { message_id: assistantId, content, stop_reason: "toolUse", entry_id: assistantId, parent_id: null });
  round = apply(round, "tool_start", { tool_call_id: "tc-slow", name: "read", arguments: { path: "slow.txt" } });
  round = apply(round, "tool_start", { tool_call_id: "tc-fast", name: "ls", arguments: { path: "." } });
  return round;
}

const toolOf = (round, id) => round.entries.find((entry) => entry.kind === "tool" && entry.id === id);
const toolIds = (round) => round.entries.filter((entry) => entry.kind === "tool").map((entry) => entry.id);

// 1. 完成事件按实际完成顺序更新各自调用，展示位置保持创建顺序，未保存不写入节点身份
let round = startBatch();
assert.deepEqual(toolIds(round), ["tc-slow", "tc-fast"], "工具展示位置按调用创建顺序");
round = apply(round, "tool_execution_end", { tool_call_id: "tc-fast", tool_name: toolNames["tc-fast"], content: "fast-result", is_error: false });
let fast = toolOf(round, "tc-fast");
assert.equal(fast.status, "completed", "完成事件置执行结果");
assert.equal(fast.content, "fast-result");
assert.equal(fast.entry_id, undefined, "完成事件保留未确认保存状态");
assert.deepEqual(toolIds(round), ["tc-slow", "tc-fast"], "完成顺序不改变展示位置");
assert.equal(round.entries.filter((entry) => entry.id === "tc-fast").length, 1, "完成事件不重复创建展示项");
assert.equal(toolOf(round, "tc-slow").status, "running", "未完成的调用保持执行中");

round = apply(round, "tool_execution_end", { tool_call_id: "tc-slow", tool_name: toolNames["tc-slow"], content: "slow-result", is_error: false });
assert.equal(toolOf(round, "tc-slow").status, "completed");

// 2. 保存确认更新同一调用并绑定真实节点身份，不新增展示项
round = apply(round, "tool_result", { tool_call_id: "tc-fast", content: "fast-result", is_error: false, entry_id: nodeFast, parent_id: assistantId });
fast = toolOf(round, "tc-fast");
assert.equal(fast.entry_id, nodeFast, "保存确认绑定节点身份");
assert.equal(fast.parent_id, assistantId);
assert.equal(fast.status, "completed");
round = apply(round, "tool_result", { tool_call_id: "tc-slow", content: "slow-result", is_error: false, entry_id: nodeSlow, parent_id: nodeFast });
assert.equal(toolOf(round, "tc-slow").entry_id, nodeSlow, "按调用顺序确认保存");
assert.deepEqual(toolIds(round), ["tc-slow", "tc-fast"], "两个事件后仍各一条展示项");
assert.equal(round.entries.filter((entry) => entry.kind === "tool").length, 2);

// 3. 无完成事件直连保存确认（旧路径）仍成立
let direct = startBatch();
direct = apply(direct, "tool_result", { tool_call_id: "tc-slow", content: "slow-result", is_error: false, entry_id: nodeSlow, parent_id: assistantId });
assert.equal(toolOf(direct, "tc-slow").status, "completed");
assert.equal(toolOf(direct, "tc-slow").entry_id, nodeSlow);

// 4. 错误完成事件的展示：执行失败置 failed，并在运行失败时保留真实执行结果
let failed = startBatch();
failed = apply(failed, "tool_execution_end", { tool_call_id: "tc-slow", tool_name: "read", content: "读取失败：路径不存在", is_error: true });
assert.equal(toolOf(failed, "tc-slow").status, "failed");
assert.equal(toolOf(failed, "tc-slow").content, "读取失败：路径不存在");
failed = apply(failed, "error", { status: "failed", code: "execution_failed", message: "执行失败。", tool_call_id: "tc-slow" });
assert.equal(failed.status, "failed");
assert.equal(toolOf(failed, "tc-slow").entry_id, undefined, "执行失败仍为未确认保存");

// 5. 取消：已执行完成的调用保持真实结果，仍在执行的调用转为已中断
let cancelled = startBatch();
cancelled = apply(cancelled, "tool_execution_end", { tool_call_id: "tc-slow", tool_name: "read", content: "slow-result", is_error: false });
cancelled = apply(cancelled, "error", { status: "cancelled", code: "cancelled", message: "执行已取消。", tool_call_id: null });
assert.equal(cancelled.status, "cancelled");
assert.equal(toolOf(cancelled, "tc-slow").status, "completed", "已执行完成的调用不因取消改写");
assert.equal(toolOf(cancelled, "tc-fast").status, "cancelled", "未完成的调用标记已中断");
assert.equal(toolOf(cancelled, "tc-slow").entry_id, undefined);

// 6. 迟到与重复事件就地报错，不产生重复展示
assert.throws(() => apply(round, "tool_execution_end", { tool_call_id: "tc-fast", tool_name: "ls", content: "x", is_error: false }), /重复返回/, "保存确认后重复完成事件报错");
assert.throws(() => apply(round, "tool_result", { tool_call_id: "tc-fast", content: "x", is_error: false, entry_id: nodeFast, parent_id: assistantId }), /重复返回/, "重复保存确认报错");
assert.throws(() => apply(round, "tool_execution_end", { tool_call_id: "unknown", tool_name: "ls", content: "x", is_error: false }), /缺少调用/, "未知调用完成事件报错");
assert.throws(() => apply(round, "tool_result", { tool_call_id: "unknown", content: "x", is_error: false, entry_id: nodeFast, parent_id: assistantId }), /缺少调用/, "未知调用保存确认报错");

// 7. 完成事件解析：必填字段与身份校验
const parser = (data) => {
  const seen = [];
  const sink = createReActParser((parsed) => seen.push(parsed), runId);
  sink.feed(`event: tool_execution_end\ndata: ${JSON.stringify(data)}\n\n`);
  return seen;
};
const parsed = parser({ run_id: runId, tool_call_id: "tc-1", tool_name: "read", content: "ok", is_error: true });
assert.equal(parsed.length, 1);
assert.equal(parsed[0].event, "tool_execution_end");
assert.equal(parsed[0].data.tool_name, "read");
assert.equal(parsed[0].data.is_error, true);
assert.throws(() => parser({ run_id: runId, tool_call_id: "tc-1", tool_name: "", content: "ok", is_error: false }), /工具执行完成事件无效/, "空工具名报错");
assert.throws(() => parser({ run_id: runId, tool_call_id: "tc-1", tool_name: "read", content: 3, is_error: false }), /工具执行完成事件无效/, "content 类型错误报错");
assert.throws(() => parser({ run_id: runId, tool_call_id: "tc-1", tool_name: "read", content: "ok", is_error: "no" }), /工具执行完成事件无效/, "is_error 类型错误报错");
assert.throws(() => parser({ run_id: runId, tool_call_id: "", tool_name: "read", content: "ok", is_error: false }), /工具执行完成事件无效/, "空调用身份报错");
assert.throws(() => parser({ run_id: u(99), tool_call_id: "tc-1", tool_name: "read", content: "ok", is_error: false }), /事件运行身份无效/, "运行身份不匹配报错");

// 8. 历史衔接：未确认保存的执行结果按 tool_call_id 与已提交结果合并为一条，绑定真实身份
const history = {
  session: { session_id: u(50), title: "会话", active_leaf_id: nodeSlow, created_at: 1, updated_at: 2 },
  entries: [
    { type: "message", entry_id: userEntry, parent_id: null, run_id: null, created_at: 1, message: { role: "user", text: "读取文件。", timestamp: 1, attachments: [] } },
    {
      type: "message",
      entry_id: assistantId,
      parent_id: userEntry,
      run_id: runId,
      created_at: 2,
      message: { role: "assistant", content, stop_reason: "toolUse", timestamp: 2 },
    },
    { type: "message", entry_id: nodeSlow, parent_id: assistantId, run_id: runId, created_at: 3, message: { role: "toolResult", tool_call_id: "tc-slow", tool_name: "read", content: "slow-result", is_error: false, timestamp: 3 } },
    { type: "message", entry_id: nodeFast, parent_id: nodeSlow, run_id: runId, created_at: 4, message: { role: "toolResult", tool_call_id: "tc-fast", tool_name: "ls", content: "fast-result", is_error: false, timestamp: 4 } },
  ],
  runs: [{ session_id: u(50), run_id: runId, request_entry_id: userEntry, last_entry_id: nodeFast, status: "completed", started_at: 1, finished_at: 5, error_code: null, error_message: null }],
  steering: [],
};
const historyRounds = historyToRounds(history);
let local = startBatch();
local = apply(local, "tool_execution_end", { tool_call_id: "tc-slow", tool_name: "read", content: "slow-result", is_error: false });
local = apply(local, "tool_execution_end", { tool_call_id: "tc-fast", tool_name: "ls", content: "fast-result", is_error: false });
const merged = mergeHistoryRounds([local], historyRounds);
const mergedTools = merged[0].entries.filter((entry) => entry.kind === "tool");
assert.equal(mergedTools.length, 2, "未确认执行结果与已提交结果不重复");
assert.equal(mergedTools.find((entry) => entry.id === "tc-slow").entry_id, nodeSlow, "合并后绑定真实节点身份");
assert.equal(mergedTools.find((entry) => entry.id === "tc-fast").entry_id, nodeFast);
assert.deepEqual(mergedTools.map((entry) => entry.id), ["tc-slow", "tc-fast"], "合并保持提交顺序");
assert.equal(merged[0].status, "completed", "历史终态优先");

// 9. 定稿约束与终态完整性：重复完成事件、工具名不符、未保存确认不得接受 done
const finalId = u(7);
const finalContent = [{ content_index: 0, type: "text", text: "完成。" }];
let guard = startBatch();
guard = apply(guard, "tool_execution_end", { tool_call_id: "tc-slow", tool_name: "read", content: "slow-result", is_error: false });
assert.throws(() => apply(guard, "tool_execution_end", { tool_call_id: "tc-slow", tool_name: "read", content: "覆盖", is_error: false }), /重复返回/, "保存确认前重复完成事件报错");
assert.throws(() => apply(guard, "tool_execution_end", { tool_call_id: "tc-fast", tool_name: "read", content: "x", is_error: false }), /重复返回/, "工具名不符报错");
guard = apply(guard, "tool_execution_end", { tool_call_id: "tc-fast", tool_name: "ls", content: "fast-result", is_error: false });
guard = apply(guard, "message_start", { message_id: finalId, content: finalContent });
guard = apply(guard, "message_end", { message_id: finalId, content: finalContent, stop_reason: "stop", entry_id: finalId, parent_id: nodeSlow });
assert.throws(() => apply(guard, "done", { status: "completed", stop_reason: "stop" }), /运行未完成/, "执行完成但未保存确认不得接受 done");
guard = apply(guard, "tool_result", { tool_call_id: "tc-slow", content: "slow-result", is_error: false, entry_id: nodeSlow, parent_id: assistantId });
guard = apply(guard, "tool_result", { tool_call_id: "tc-fast", content: "fast-result", is_error: false, entry_id: nodeFast, parent_id: nodeSlow });
assert.equal(apply(guard, "done", { status: "completed", stop_reason: "stop" }).status, "completed", "全部保存确认后可接受 done");

// 10. 进度快照：替换内容、不新增展示项、迟到与无关更新忽略、最终结果覆盖
let progress = startBatch();
progress = apply(progress, "tool_execution_update", { tool_call_id: "tc-fast", tool_name: "ls", content: "快照一", is_error: false });
assert.equal(toolOf(progress, "tc-fast").content, "快照一", "进度快照替换内容");
assert.equal(toolOf(progress, "tc-fast").status, "running", "进度不改变执行状态");
assert.equal(progress.entries.filter((entry) => entry.id === "tc-fast").length, 1, "进度不新增展示项");
progress = apply(progress, "tool_execution_update", { tool_call_id: "tc-fast", tool_name: "ls", content: "快照二", is_error: false });
assert.equal(toolOf(progress, "tc-fast").content, "快照二", "进度按当前完整快照替换");
progress = apply(progress, "tool_execution_end", { tool_call_id: "tc-fast", tool_name: "ls", content: "最终", is_error: false });
assert.equal(toolOf(progress, "tc-fast").content, "最终", "完成事件覆盖进度");
progress = apply(progress, "tool_execution_update", { tool_call_id: "tc-fast", tool_name: "ls", content: "迟到", is_error: false });
assert.equal(toolOf(progress, "tc-fast").content, "最终", "执行结束后的迟到更新忽略");
assert.equal(toolOf(progress, "tc-fast").status, "completed");
progress = apply(progress, "tool_execution_update", { tool_call_id: "unknown", tool_name: "ls", content: "x", is_error: false });
progress = apply(progress, "tool_execution_update", { tool_call_id: "tc-slow", tool_name: "wrong", content: "x", is_error: false });
assert.equal(toolOf(progress, "tc-slow").content, undefined, "无关或工具名不符的进度忽略");

const updateParser = (data) => {
  const seen = [];
  const sink = createReActParser((parsed) => seen.push(parsed), runId);
  sink.feed(`event: tool_execution_update\ndata: ${JSON.stringify(data)}\n\n`);
  return seen;
};
const parsedUpdate = updateParser({ run_id: runId, tool_call_id: "tc-1", tool_name: "read", content: "快照", is_error: false });
assert.equal(parsedUpdate.length, 1);
assert.equal(parsedUpdate[0].event, "tool_execution_update");
assert.throws(() => updateParser({ run_id: runId, tool_call_id: "tc-1", tool_name: "", content: "快照", is_error: false }), /工具进度事件无效/, "空工具名报错");
assert.throws(() => updateParser({ run_id: runId, tool_call_id: "tc-1", tool_name: "read", content: 1, is_error: false }), /工具进度事件无效/, "content 类型错误报错");
assert.throws(() => updateParser({ run_id: runId, tool_call_id: "tc-1", tool_name: "read", content: "快照", is_error: "no" }), /工具进度事件无效/, "is_error 类型错误报错");

await server.close();
console.log("PASS: 完成事件与进度快照解析、执行完成与保存确认状态转换、调用位置稳定与去重、错误/取消保留、定稿约束与终态完整性、历史衔接");
