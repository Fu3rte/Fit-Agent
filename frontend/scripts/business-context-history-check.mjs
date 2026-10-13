// 运行：node scripts/business-context-history-check.mjs
// 纯解析／状态检查：内部资料节点参与祖先链、叶节点与运行归属，聊天不生成可见条目且不暴露业务资料。
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
await server.close();

const u = (n) => `${n.toString(16).padStart(8, "0")}-0000-4000-8000-000000000000`;
const SESSION = u(1);
const S = u(2);
const U1 = u(3);
const A1 = u(4);
const C1 = u(5);
const U2 = u(6);
const A2 = u(7);
const C2 = u(8);
const R1 = u(9);
const R2 = u(10);

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
        ? {
            role,
            content: [{ content_index: 0, type: "text", text: "回答。" }],
            stop_reason: "stop",
            timestamp: 1,
          }
        : { role },
});

/** 内部资料节点只公开身份与父子关系（业务上下文契约 §3）：公开载荷不得携带 message、content 或 details。 */
const custom = (entry_id, parent_id, run_id, created_at = 1) => ({
  type: "custom_message",
  entry_id,
  parent_id,
  run_id,
  created_at,
});

const run = (run_id, request_entry_id, last_entry_id, status) => ({
  session_id: SESSION,
  run_id,
  request_entry_id,
  last_entry_id,
  status,
  started_at: 1,
  finished_at: status === "running" ? null : 2,
  error_code: null,
  error_message: null,
});

/** 合法历史：资料节点位于链中（C1）与叶节点（C2），last_entry_id 指向资料节点 */
const baseHistory = () => ({
  session: {
    session_id: SESSION,
    title: "会话",
    active_leaf_id: C2,
    created_at: 1,
    updated_at: 2,
  },
  entries: [
    message(S, null, null, "system"),
    message(U1, S, R1, "user"),
    message(A1, U1, R1, "assistant"),
    custom(C1, A1, R1),
    message(U2, C1, R2, "user"),
    message(A2, U2, R2, "assistant"),
    custom(C2, A2, R2),
  ],
  runs: [run(R1, U1, C1, "completed"), run(R2, U2, C2, "running")],
  steering: [],
  confirmations: [],
  pending_proposals: [],
});

const parsed = api.parseSessionHistory(SESSION, baseHistory());
assert.deepEqual(
  parsed.entries.map((entry) => entry.type),
  [
    "message",
    "message",
    "message",
    "custom_message",
    "message",
    "message",
    "custom_message",
  ],
  "祖先链保留资料隐藏节点且顺序不变",
);
assert.equal(parsed.session.active_leaf_id, C2, "叶节点可以指向资料节点");
assert.equal(
  parsed.runs.find((item) => item.run_id === R1).last_entry_id,
  C1,
  "运行末节点可以指向资料节点",
);
assert.deepEqual(
  Object.keys(parsed.entries[3]).sort(),
  ["created_at", "entry_id", "parent_id", "run_id", "type"],
  "资料节点只保留五个身份字段",
);
assert.equal(parsed.entries[3].message, undefined, "资料节点不暴露 message");
assert.equal(parsed.entries[6].parent_id, A2, "资料节点保留真实父节点");

const rounds = history.historyToRounds(parsed);
assert.deepEqual(
  rounds.map((round) => round.run_id),
  [R1, R2],
  "资料节点不产生额外轮次",
);
assert.deepEqual(
  rounds[0].entries.map((entry) => entry.kind),
  ["user", "assistant"],
  "链中的资料节点被隐藏且不断链",
);
assert.deepEqual(
  rounds[1].entries.map((entry) => entry.kind),
  ["user", "assistant"],
  "叶资料节点不生成可见用户聊天条目",
);
assert.equal(
  rounds
    .flatMap((round) => round.entries)
    .some((entry) => entry.id === C1 || entry.id === C2),
  false,
  "资料节点不作为普通消息访问 message",
);

// 非法节点与非法归属一律报错
const reject = (mutate, expected) => {
  const wire = baseHistory();
  mutate(wire);
  assert.throws(() => api.parseSessionHistory(SESSION, wire), expected);
};

assert.throws(
  () =>
    api.parseSessionHistory(SESSION, {
      ...baseHistory(),
      entries: [
        ...baseHistory().entries,
        { type: "business_context", entry_id: u(90), parent_id: C2, run_id: R2, created_at: 1 },
      ],
    }),
  /type 无效|叶节点/,
  "未知节点类型报错",
);
reject((wire) => {
  wire.entries[3].message = { role: "custom" };
}, /含未定义字段/);
reject((wire) => {
  wire.entries[3].content = "后端业务资料";
}, /含未定义字段/);
reject((wire) => {
  wire.entries[3].details = { mode: "snapshot" };
}, /含未定义字段/);
reject((wire) => {
  delete wire.entries[3].run_id;
}, /run_id 身份无效/);
reject((wire) => {
  delete wire.entries[3].created_at;
}, /无效/);
reject((wire) => {
  wire.entries[3].entry_id = "not-a-uuid";
}, /身份无效/);
reject((wire) => {
  wire.entries[3].created_at = -1;
}, /无效/);
reject((wire) => {
  wire.entries[3].parent_id = u(91);
}, /分支链无效/);
reject((wire) => {
  wire.session.active_leaf_id = A2;
}, /叶节点不匹配/);
reject((wire) => {
  wire.runs[0].last_entry_id = A1;
  wire.entries[3].run_id = u(92);
}, /运行归属无效/);

console.log(
  "PASS: 资料隐藏节点参与祖先链、叶节点与运行归属，不生成可见条目且不暴露业务资料",
);
