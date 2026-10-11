// 运行：node scripts/session-history-merge-check.mjs
// 纯函数检查：历史重建、账本合并与去重、HISTORY 展示状态（不触达后端）
import assert from "node:assert/strict";
import { createServer } from "vite";

// 被测模块按项目别名引用 @/lib，须经 vite 解析加载
const server = await createServer({
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});
const { historyToRounds, mergeHistoryRounds, reconcileLedger } =
  await server.ssrLoadModule("/src/features/chat/utils/sessionHistory.ts");
await server.close();

const u = (n) => `${n.toString(16).padStart(8, "0")}-0000-4000-8000-000000000000`;
const sessionId = u(1);
const systemEntry = u(2);
const requestEntry = u(3);
const assistantEntry = u(4);
const toolEntry = u(5);
const steerConsumedEntry = u(6);
const steerConsumedId = u(7);
const failRequest = u(8);
const intrRequest = u(9);
const runPendingRequest = u(10);
const pendingSteerId = u(11);
const runOkId = u(20);
const failRunId = u(21);
const intrRunId = u(22);
const runPendingId = u(23);

const run = (overrides) => ({
  session_id: sessionId,
  run_id: u(99),
  request_entry_id: u(98),
  last_entry_id: null,
  status: "completed",
  started_at: 1,
  finished_at: 2,
  error_code: null,
  error_message: null,
  ...overrides,
});

const history = {
  session: { session_id: sessionId, title: "会话", active_leaf_id: runPendingRequest, created_at: 1, updated_at: 2 },
  entries: [
    { type: "message", entry_id: systemEntry, parent_id: null, run_id: null, created_at: 1, message: { role: "system" } },
    { type: "message", entry_id: requestEntry, parent_id: systemEntry, run_id: null, created_at: 2, message: { role: "user", text: "第一条请求。", timestamp: 2, attachments: [] } },
    {
      type: "message",
      entry_id: assistantEntry,
      parent_id: requestEntry,
      run_id: runOkId,
      created_at: 3,
      message: {
        role: "assistant",
        content: [{ content_index: 0, type: "text", text: "调用工具。" }, { content_index: 1, type: "tool_call", tool_call_id: "tc1", name: "bash", arguments: { command: "x" } }],
        stop_reason: "toolUse",
        timestamp: 3,
      },
    },
    { type: "message", entry_id: toolEntry, parent_id: assistantEntry, run_id: runOkId, created_at: 4, message: { role: "toolResult", tool_call_id: "tc1", tool_name: "bash", content: "ok", is_error: false, timestamp: 4 } },
    { type: "message", entry_id: steerConsumedEntry, parent_id: toolEntry, run_id: runOkId, created_at: 5, message: { role: "user", text: "追加A。", timestamp: 5, attachments: [] } },
    { type: "message", entry_id: failRequest, parent_id: steerConsumedEntry, run_id: null, created_at: 6, message: { role: "user", text: "失败请求。", timestamp: 6, attachments: [] } },
    { type: "message", entry_id: intrRequest, parent_id: failRequest, run_id: null, created_at: 7, message: { role: "user", text: "中断请求。", timestamp: 7, attachments: [] } },
    { type: "message", entry_id: runPendingRequest, parent_id: intrRequest, run_id: null, created_at: 8, message: { role: "user", text: "运行中请求。", timestamp: 8, attachments: [] } },
  ],
  runs: [
    run({ run_id: runOkId, request_entry_id: requestEntry, last_entry_id: steerConsumedEntry, status: "completed" }),
    run({ run_id: failRunId, request_entry_id: failRequest, last_entry_id: null, status: "failed", error_code: "execution_failed", error_message: "执行失败。" }),
    run({ run_id: intrRunId, request_entry_id: intrRequest, last_entry_id: null, status: "interrupted", finished_at: null }),
    run({ run_id: runPendingId, request_entry_id: runPendingRequest, last_entry_id: null, status: "running", finished_at: null }),
  ],
  steering: [
    { session_id: sessionId, run_id: runOkId, steering_id: steerConsumedId, text: "追加A。", attachments: [], timestamp: 5, status: "consumed", entry_id: steerConsumedEntry, reason: null, created_at: 5, updated_at: 5 },
    { session_id: sessionId, run_id: runPendingId, steering_id: pendingSteerId, text: "待消费。", attachments: [], timestamp: 9, status: "pending", entry_id: null, reason: null, created_at: 9, updated_at: 9 },
  ],
};

const rounds = historyToRounds(history);
assert.deepEqual(rounds.map((r) => r.run_id), [runOkId, failRunId, intrRunId, runPendingId], "轮次按祖先链顺序按运行分组");
assert.deepEqual(rounds.map((r) => r.status), ["completed", "failed", "interrupted", "running"], "运行状态映射为展示状态");

const ok = rounds[0];
assert.deepEqual(ok.entries.map((e) => e.kind), ["user", "assistant", "tool", "user"], "已提交节点顺序保持");
assert.equal(ok.entries[0].id, requestEntry, "初始用户节点通过 request_entry_id 关联");
const tool = ok.entries[2];
assert.equal(tool.name, "bash");
assert.deepEqual(tool.arguments, { command: "x" }, "工具结果通过 tool_call_id 关联助手工具调用参数");
assert.equal(tool.status, "completed");
const consumed = ok.entries[3];
assert.equal(consumed.id, steerConsumedEntry, "consumed 输入展示关联用户节点");
assert.equal(consumed.steering.status, "consumed");
assert.equal(consumed.steering.entry_id, steerConsumedEntry);
assert.equal(ok.entries.filter((e) => e.kind === "user" && e.request === "追加A。").length, 1, "已消费 Steering 只展示一次");
assert.equal(rounds.some((r) => r.entries.some((e) => e.id === systemEntry)), false, "系统节点不产生可见条目");

assert.equal(rounds[1].status, "failed");
assert.deepEqual(rounds[1].entries.map((e) => e.id), [failRequest], "无助手消息的失败运行仍展示请求节点");
assert.equal(rounds[1].error, "执行失败。");
assert.equal(rounds[2].status, "interrupted");
assert.deepEqual(rounds[2].entries.map((e) => e.id), [intrRequest], "无助手消息的中断运行仍展示请求节点");
assert.equal(rounds[3].status, "running");
assert.deepEqual(rounds[3].entries.map((e) => e.id), [runPendingRequest, pendingSteerId], "pending 输入按原运行展示");
assert.equal(rounds[3].entries[1].steering.status, "pending");

// 账本合并：已提交运行/输入不再进入未确认集合，按 run_id/steering_id 去重
const op = (overrides) => ({ operation_id: u(50), session_id: sessionId, kind: "send", run_id: null, request: "正文。", created_at: 1, ...overrides });
const committed = reconcileLedger(history, [op({ operation_id: u(51), run_id: runOkId })]);
assert.deepEqual(committed.accepted, [u(51)], "历史已覆盖的终态运行操作移出账本");
assert.equal(committed.rounds.length, 4, "已提交运行不重复生成占位轮次");
const running = reconcileLedger(history, [op({ operation_id: u(52), run_id: runPendingId })]);
assert.deepEqual(running.watch, [{ roundId: runPendingId, runId: runPendingId }], "running 运行保持占用并转入运行查询");
assert.deepEqual(running.accepted, [], "running 操作不移出账本");
const unknown = reconcileLedger(history, [op({ operation_id: u(53) })]);
assert.equal(unknown.rounds.length, 5, "未受理发送生成占位轮次");
assert.deepEqual(unknown.query, [{ roundId: u(53), operation: op({ operation_id: u(53) }) }], "未受理发送按原 operation_id 查询");
const steerConsumed = reconcileLedger(history, [{ ...op({ operation_id: u(54), kind: "steering", run_id: runOkId }), steering_id: steerConsumedId, request: "追加A。" }]);
assert.deepEqual(steerConsumed.accepted, [u(54)], "已消费输入操作移出账本");
const steerPending = reconcileLedger(history, [{ ...op({ operation_id: u(55), kind: "steering", run_id: runPendingId }), steering_id: pendingSteerId, request: "待消费。" }]);
assert.deepEqual(steerPending.accepted, [], "pending 输入保留账本供撤回");
assert.equal(steerPending.rounds[3].entries[1].operation_id, u(55), "pending 输入补上原操作身份");
assert.equal(steerPending.rounds.filter((r) => r.entries.some((e) => e.id === pendingSteerId)).length, 1, "pending 输入不重复生成节点");

// 历史刷新并入展示：终态不被较早 running 快照覆盖，在途快照保留
const stale = [{ id: runOkId, run_id: runOkId, entries: [], status: "running" }];
assert.equal(mergeHistoryRounds(stale, rounds)[0].status, "completed", "较早 running 快照不覆盖当前终态");
const live = [{ id: u(60), run_id: u(60), entries: [], status: "running" }];
const merged = mergeHistoryRounds(live, rounds);
assert.equal(merged.length, 5, "历史未覆盖的在途轮次保留");
assert.equal(merged.at(-1).run_id, u(60));

// 修复：终态轮次按稳定节点身份合并条目，补齐历史中的已提交工具结果与最终回答
const partial = historyToRounds({
  ...history,
  entries: history.entries.slice(0, 3),
  runs: [run({ run_id: runOkId, request_entry_id: requestEntry, last_entry_id: assistantEntry, status: "completed" })],
  steering: [],
});
assert.deepEqual(partial[0].entries.map((e) => e.kind), ["user", "assistant"], "局部快照仅含已提交节点");
const healed = mergeHistoryRounds(partial.map((round) => ({ ...round, status: "completed" })), rounds);
assert.deepEqual(healed[0].entries.map((e) => e.id), rounds[0].entries.map((e) => e.id), "终态轮次补齐工具结果与最终回答");
assert.equal(healed[0].status, "completed", "补齐后保持终态");
assert.equal(healed[0].entries.filter((e) => e.kind === "tool").length, 1, "工具结果只展示一次");

// 修复：Steering 跨状态统一身份，旧 pending 快照不得与已消费输入重复展示，并保持分支内顺序
const stalePending = [{
  id: runOkId,
  run_id: runOkId,
  request_entry_id: requestEntry,
  entries: [
    { kind: "user", id: requestEntry, request: "第一条请求。" },
    { kind: "user", id: steerConsumedId, steering_id: steerConsumedId, request: "追加A。", steering: { status: "pending", entry_id: null, reason: null } },
  ],
  status: "running",
}];
const deduped = mergeHistoryRounds(rounds, stalePending);
const steerShown = deduped[0].entries.filter((e) => e.kind === "user" && e.request === "追加A。");
assert.equal(steerShown.length, 1, "旧 pending 快照与已消费输入合并为一条");
assert.equal(steerShown[0].steering.status, "consumed", "保持已确认终态");
assert.deepEqual(deduped[0].entries.map((e) => e.id), rounds[0].entries.map((e) => e.id), "合并保持分支内顺序");

// 回归：历史已确认受理的操作必须移出展示的 pending，否则残留“提交结果未知 / 重试”且重试失效
const steerOperation = { operation_id: u(30), session_id: sessionId, kind: "steering", run_id: runOkId, request: "追加A。", created_at: 1, steering_id: steerConsumedId, request_entry_id: null };
const localUnknown = [{ ...stalePending[0], status: "completed", pending: [steerOperation] }];
const confirmed = mergeHistoryRounds(localUnknown, rounds, new Set([steerOperation.operation_id]));
assert.equal(confirmed[0].pending, undefined, "已确认操作不再保留未知/重试");
const unresolved = mergeHistoryRounds(localUnknown, rounds, new Set());
assert.equal(unresolved[0].pending?.length, 1, "未解决操作保留重试入口");

console.log("PASS: 历史重建、账本合并去重、终态条目补齐、Steering 跨状态归并、已确认 pending 清理与终态保护");
