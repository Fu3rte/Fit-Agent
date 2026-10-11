// 运行：node scripts/compaction-error-contract-check.mjs
import assert from "node:assert/strict";
import { createServer } from "vite";

const server = await createServer({
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});
const { parsePlanBusinessError } = await server.ssrLoadModule("/src/lib/business.ts");
const { createReActParser, applyReActEvent } = await server.ssrLoadModule(
  "/src/features/chat/utils/reactAgent.ts",
);
await server.close();

const saved = {
  code: "proposal_already_saved",
  message: "该提案已保存，无法作为待确认内容读取。请查询原保存操作状态。",
};
assert.deepEqual(parsePlanBusinessError(saved), saved);
assert.throws(() => parsePlanBusinessError({ ...saved, errors: [] }));
assert.throws(() => parsePlanBusinessError({ ...saved, code: "unknown_error" }));

const runId = "11111111-1111-4111-8111-111111111111";
const round = { id: runId, run_id: runId, status: "running", entries: [] };
const data = (code, status = "failed") => ({
  run_id: runId, code, status, message: "运行结束。", tool_call_id: null,
});
function parse(value) {
  const events = [];
  const parser = createReActParser((event) => events.push(event), runId);
  parser.feed(`event: error\ndata: ${JSON.stringify(value)}\n\n`);
  parser.finish();
  assert.equal(parser.terminal, true);
  assert.equal(events.length, 1);
  return events[0];
}

for (const code of [
  "execution_failed", "credential_detected", "compaction_failed",
  "context_budget_exceeded", "context_overflow",
]) {
  const value = data(code);
  const event = parse(value);
  assert.deepEqual(event, { event: "error", data: value });
  const next = applyReActEvent(round, event);
  assert.equal(next.status, "failed");
  assert.equal(next.error, value.message);
  assert.throws(() => parse(data(code, "cancelled")));
}
assert.equal(applyReActEvent(round, parse(data("cancelled", "cancelled"))).status, "cancelled");
for (const value of [
  data("unknown_error"), data("proposal_already_saved"), data("cancelled"),
  data("compaction_failed", "completed"),
  { ...data("compaction_failed"), run_id: "22222222-2222-4222-8222-222222222222" },
]) assert.throws(() => parse(value));

console.log("PASS: 已保存提案错误、压缩失败码、终态转换及非法错误组合拒绝");
