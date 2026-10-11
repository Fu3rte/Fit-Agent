import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createServer } from "vite";

const evidence = JSON.parse(readFileSync(
  new URL("../../tmp/design/model-request-budget-live.json", import.meta.url), "utf8",
));
const server = await createServer({
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});
const api = await server.ssrLoadModule("/src/lib/api.ts");
const history = await server.ssrLoadModule("/src/features/chat/utils/sessionHistory.ts");
const agent = await server.ssrLoadModule("/src/features/chat/utils/reactAgent.ts");
await server.close();

let live = {
  id: evidence.run_id,
  run_id: evidence.run_id,
  status: "running",
  entries: [{
    id: evidence.request_entry_id,
    entry_id: evidence.request_entry_id,
    kind: "user",
    request: evidence.request,
  }],
};
const parser = agent.createReActParser((event) => {
  live = agent.applyReActEvent(live, event);
}, evidence.run_id);
for (const { event, data } of evidence.events) {
  parser.feed(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
}
parser.finish();
const stored = api.parseSessionHistory(evidence.session_id, evidence.history);
const replayed = history.historyToRounds(stored).find((round) => round.run_id === evidence.run_id);
assert.ok(replayed);
assert.equal(live.status, "completed");
assert.equal(replayed.status, live.status);
assert.deepEqual(live.entries, replayed.entries);
assert.ok(live.entries.some((entry) => entry.kind === "tool" && entry.name === "calculate_date"));
console.log("PASS: 真实模型 SSE 经前端解析和状态处理，与重新读取的持久化历史条目一致");
