// 运行：node scripts/business-context-stream-check.mjs
// 纯解析／状态检查：custom_message 事件只登记隐藏结构节点与运行归属，不生成可见聊天条目；
// 相同身份字段冲突与运行身份不一致按协议错误处理，历史读取与合并保持同一口径。
import assert from "node:assert/strict";
import { createServer } from "vite";

const server = await createServer({
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});
const api = await server.ssrLoadModule("/src/lib/api.ts");
const react = await server.ssrLoadModule(
  "/src/features/chat/utils/reactAgent.ts",
);
const historyMod = await server.ssrLoadModule(
  "/src/features/chat/utils/sessionHistory.ts",
);
await server.close();

const u = (n) => `${n.toString(16).padStart(8, "0")}-0000-4000-8000-000000000000`;

/* ===== SSE 事件解析（契约 §8）===== */
const R = u(20);
const C1 = u(21);
const U1 = u(22);
const C2 = u(23);

function feedEvents(events, runId) {
  const received = [];
  const parser = react.createReActParser((event) => received.push(event), runId);
  for (const { event, data } of events)
    parser.feed(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  parser.finish();
  return received;
}

const customEvent = (data) => ({
  event: "custom_message",
  data: { run_id: R, ...data },
});
const doneEvent = { event: "done", data: { run_id: R, status: "completed", stop_reason: "stop" } };

const received = feedEvents(
  [
    customEvent({
      type: "custom_message",
      entry_id: C1,
      parent_id: U1,
      created_at: 1786766400000,
    }),
    doneEvent,
  ],
  R,
);
assert.equal(received.length, 2, "收到资料事件与终止事件");
assert.equal(received[0].event, "custom_message");
assert.deepEqual(received[0].data, {
  run_id: R,
  type: "custom_message",
  entry_id: C1,
  parent_id: U1,
  created_at: 1786766400000,
});

const badEvent = (data) => () =>
  feedEvents([customEvent(data), doneEvent], R);
assert.throws(
  badEvent({ type: "custom_message", entry_id: "not-a-uuid", parent_id: U1, created_at: 5 }),
  /资料节点事件无效/,
  "非法 entry_id 报错",
);
assert.throws(
  badEvent({ type: "custom_message", entry_id: C1, parent_id: "not-a-uuid", created_at: 5 }),
  /资料节点事件无效/,
  "非法 parent_id 报错",
);
assert.throws(
  badEvent({ type: "custom_message", entry_id: C1, parent_id: U1 }),
  /资料节点事件无效/,
  "缺少 created_at 报错",
);
assert.throws(
  badEvent({ type: "custom_message", entry_id: C1, parent_id: U1, created_at: -1 }),
  /资料节点事件无效/,
  "负数 created_at 报错",
);
assert.throws(
  badEvent({ type: "business_context", entry_id: C1, parent_id: U1, created_at: 5 }),
  /资料节点事件无效/,
  "错误 type 报错",
);
assert.throws(
  () =>
    feedEvents(
      [
        { event: "custom_message", data: { run_id: u(90), type: "custom_message", entry_id: C1, parent_id: U1, created_at: 5 } },
        doneEvent,
      ],
      R,
    ),
  /事件运行身份无效/,
  "运行身份不一致报错",
);

/* ===== 运行中隐藏节点落地（契约 §8.3）===== */
const base = (runId) => ({ id: runId, run_id: runId, entries: [], status: "running" });
let round = base(R);
round = react.applyReActEvent(round, {
  event: "custom_message",
  data: { run_id: R, type: "custom_message", entry_id: C1, parent_id: U1, created_at: 5 },
});
assert.deepEqual(
  round.hidden,
  { [C1]: { parent_id: U1, run_id: R } },
  "隐藏节点只登记父节点与运行归属",
);
assert.deepEqual(round.entries, [], "隐藏节点不生成可见聊天条目");

// 相同身份重复通知幂等。
round = react.applyReActEvent(round, {
  event: "custom_message",
  data: { run_id: R, type: "custom_message", entry_id: C1, parent_id: U1, created_at: 5 },
});
assert.deepEqual(round.entries, []);

// 相同身份字段冲突按协议错误处理。
assert.throws(
  () =>
    react.applyReActEvent(round, {
      event: "custom_message",
      data: { run_id: R, type: "custom_message", entry_id: C1, parent_id: u(30), created_at: 5 },
    }),
  /资料节点身份冲突/,
  "同身份不同父节点报错",
);
assert.throws(
  () =>
    react.applyReActEvent(round, {
      event: "custom_message",
      data: { run_id: u(31), type: "custom_message", entry_id: C1, parent_id: U1, created_at: 5 },
    }),
  /运行身份不匹配/,
  "运行归属不一致报错",
);

// 后续助手节点引用隐藏父节点。
const A1 = u(40);
round = react.applyReActEvent(round, {
  event: "message_start",
  data: { run_id: R, message_id: A1, content: [] },
});
round = react.applyReActEvent(round, {
  event: "message_end",
  data: { run_id: R, message_id: A1, content: [], stop_reason: "stop", entry_id: A1, parent_id: C1 },
});
assert.equal(round.entries.length, 1, "隐藏节点不挤占可见条目");
assert.equal(round.entries[0].parent_id, C1, "助手节点可引用隐藏父节点");
assert.equal(round.entries[0].entry_id, A1);

/* ===== 历史读取与合并（契约 §3、§8.3）===== */
const SESSION = u(1);
const S = u(2);
const U2 = u(3);
const A2 = u(4);
const C3 = u(5);
const R1 = u(6);

const message = (entry_id, parent_id, run_id, role) => ({
  type: "message",
  entry_id,
  parent_id,
  run_id,
  created_at: 1,
  message:
    role === "user"
      ? { role, text: "请求。", timestamp: 1, attachments: [] }
      : role === "assistant"
        ? { role, content: [{ content_index: 0, type: "text", text: "回答。" }], stop_reason: "stop", timestamp: 1 }
        : { role },
});
const custom = (entry_id, parent_id, run_id) => ({
  type: "custom_message",
  entry_id,
  parent_id,
  run_id,
  created_at: 1,
});
const run = (run_id, request_entry_id, last_entry_id) => ({
  session_id: SESSION,
  run_id,
  request_entry_id,
  last_entry_id,
  status: "completed",
  started_at: 1,
  finished_at: 2,
  error_code: null,
  error_message: null,
  stop_reason: null,
});

const parsed = api.parseSessionHistory(SESSION, {
  session: { session_id: SESSION, title: "会话", active_leaf_id: C3, created_at: 1, updated_at: 2 },
  entries: [
    message(S, null, null, "system"),
    message(U2, S, R1, "user"),
    message(A2, U2, R1, "assistant"),
    custom(C3, A2, R1),
  ],
  runs: [run(R1, U2, C3)],
  steering: [],
  confirmations: [],
  pending_proposals: [],
});
const rounds = historyMod.historyToRounds(parsed);
assert.equal(rounds.length, 1, "资料节点不产生额外轮次");
assert.deepEqual(
  rounds[0].hidden,
  { [C3]: { parent_id: A2, run_id: R1 } },
  "历史读取登记同一隐藏身份",
);
assert.deepEqual(
  rounds[0].entries.map((entry) => entry.kind),
  ["user", "assistant"],
  "资料节点不生成可见条目",
);

// 同身份字段冲突按协议错误处理。
assert.throws(
  () =>
    api.parseSessionHistory(SESSION, {
      session: { session_id: SESSION, title: "会话", active_leaf_id: C3, created_at: 1, updated_at: 2 },
      entries: [
        message(S, null, null, "system"),
        message(U2, S, R1, "user"),
        message(A2, U2, R1, "assistant"),
        custom(C3, U2, R1),
      ],
      runs: [run(R1, U2, C3)],
      steering: [],
      confirmations: [],
      pending_proposals: [],
    }),
  /父节点|分支链/,
  "历史链非法父子关系报错",
);

/* ===== 历史刷新合并隐藏节点（契约 §8.3）===== */
const current = [
  { id: R1, run_id: R1, entries: [], status: "completed", hidden: { [C1]: { parent_id: U1, run_id: R1 } } },
];
const incoming = [
  { id: R1, run_id: R1, entries: [], status: "completed", hidden: { [C2]: { parent_id: C1, run_id: R1 } } },
];
const merged = historyMod.mergeHistoryRounds(current, incoming);
assert.deepEqual(
  Object.keys(merged[0].hidden).sort(),
  [C1, C2].sort(),
  "历史刷新合并两侧隐藏身份",
);
assert.throws(
  () =>
    historyMod.mergeHistoryRounds(current, [
      { id: R1, run_id: R1, entries: [], status: "completed", hidden: { [C1]: { parent_id: u(70), run_id: R1 } } },
    ]),
  /资料节点身份冲突/,
  "合并时同身份冲突报错",
);

console.log(
  "PASS: custom_message 事件解析与校验、隐藏节点登记与冲突、后续父子关系、历史读取与刷新合并",
);
