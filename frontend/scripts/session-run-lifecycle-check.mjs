// 运行：node scripts/session-run-lifecycle-check.mjs
// 纯状态检查：实时运行、未知账本、历史终态与 Steering 按稳定身份归并。
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { createServer } from "vite";

const server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
const { reconcileLedger, mergeHistoryRounds } = await server.ssrLoadModule("/src/features/chat/utils/sessionHistory.ts");
await server.close();

const operation = {
  operation_id: randomUUID(), session_id: randomUUID(), kind: "send",
  run_id: randomUUID(), request_entry_id: randomUUID(), request: "继续", created_at: Date.now(),
};
const live = {
  id: operation.operation_id, run_id: operation.run_id, request_entry_id: operation.request_entry_id,
  status: "running", entries: [
    { kind: "user", id: operation.operation_id, request: operation.request },
    { kind: "assistant", id: randomUUID(), content: [{ content_index: 0, type: "text", text: "正在执行" }] },
  ],
};
const recovery = reconcileLedger(null, [operation]);
assert.equal(recovery.watch[0].runId, operation.run_id);
const merged = mergeHistoryRounds([live], recovery.rounds, new Set(recovery.accepted));
assert.equal(merged[0].status, "running");
assert.deepEqual(merged[0].entries, live.entries);
assert.equal(merged[0].pending, undefined);

const beforeHeaders = { ...live, run_id: undefined, request_entry_id: undefined };
const unknown = reconcileLedger(null, [{ ...operation, run_id: null, request_entry_id: null }]);
assert.equal(unknown.query[0].operation.operation_id, operation.operation_id);
assert.equal(mergeHistoryRounds([beforeHeaders], unknown.rounds)[0].status, "running");

for (const status of ["completed", "failed", "cancelled", "interrupted"]) {
  const terminal = { ...live, status };
  assert.equal(mergeHistoryRounds([live], [terminal])[0].status, status);
  assert.equal(mergeHistoryRounds([terminal], [live])[0].status, status);
  assert.equal(mergeHistoryRounds([terminal], recovery.rounds)[0].status, status);
}

const steeringId = randomUUID();
const consumed = { kind: "user", id: steeringId, steering_id: steeringId, request: "追加", steering: { status: "consumed", entry_id: randomUUID(), reason: null } };
const current = { ...live, entries: [...live.entries, consumed] };
const stale = { ...current, entries: [{ ...consumed, steering: { status: "pending", entry_id: null, reason: null } }] };
const shown = mergeHistoryRounds([current], [stale])[0].entries;
assert.equal(shown.filter((entry) => entry.id === steeringId).length, 1);
assert.equal(shown.find((entry) => entry.id === steeringId).steering.status, "consumed");

console.log("PASS: 受理前后实时状态、未提交消息、历史终态与 Steering 去重保持正确");
