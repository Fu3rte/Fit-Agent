// 运行：node scripts/compaction-history-check.mjs
// 纯解析／状态检查：压缩隐藏结构节点参与祖先链、叶节点与运行归属，聊天仅从 message 节点提取消息。
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
const reactAgent = await server.ssrLoadModule(
  "/src/features/chat/utils/reactAgent.ts",
);
await server.close();

const u = (n) => `${n.toString(16).padStart(8, "0")}-0000-4000-8000-000000000000`;
const SESSION = u(1);
const S = u(2);
const U1 = u(3);
const C1 = u(4);
const A1 = u(5);
const U2 = u(6);
const C2 = u(7);
const R1 = u(8);
const R2 = u(9);
const W1 = u(10);

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

/** 合法历史：压缩节点位于链中（C1）与叶节点（C2），last_entry_id 指向压缩节点 */
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
    message(U1, S, null, "user"),
    { type: "compaction", entry_id: C1, parent_id: U1, run_id: R1, created_at: 1 },
    message(A1, C1, R1, "assistant"),
    message(U2, A1, R2, "user"),
    { type: "compaction", entry_id: C2, parent_id: U2, run_id: R2, created_at: 1 },
  ],
  runs: [
    run(R1, U1, A1, "completed"),
    run(R2, U2, C2, "running"),
  ],
  steering: [
    {
      session_id: SESSION,
      run_id: R2,
      steering_id: W1,
      text: "追加。",
      attachments: [],
      timestamp: 1,
      status: "consumed",
      entry_id: U2,
      reason: null,
      created_at: 1,
      updated_at: 1,
    },
  ],
});

const parsed = api.parseSessionHistory(SESSION, baseHistory());
assert.deepEqual(
  parsed.entries.map((entry) => entry.type),
  ["message", "message", "compaction", "message", "message", "compaction"],
  "祖先链保留压缩隐藏节点且顺序不变",
);
assert.equal(parsed.session.active_leaf_id, C2, "叶节点可以指向压缩节点");
assert.equal(parsed.runs.find((item) => item.run_id === R2).last_entry_id, C2, "运行末节点可以指向压缩节点");

const rounds = history.historyToRounds(parsed);
assert.deepEqual(
  rounds.map((round) => round.run_id),
  [R1, R2],
  "压缩节点不产生额外轮次",
);
assert.deepEqual(
  rounds[0].entries.map((entry) => entry.kind),
  ["user", "assistant"],
  "链中的压缩节点被隐藏且不断链",
);
assert.deepEqual(
  rounds[1].entries.map((entry) => entry.kind),
  ["user"],
  "叶压缩节点不产生可见消息",
);
assert.equal(
  rounds.flatMap((round) => round.entries).some((entry) => entry.id === C1 || entry.id === C2),
  false,
  "压缩节点不作为普通消息访问 message",
);
assert.equal(rounds[1].entries[0].steering.status, "consumed", "consumed 输入仍关联真实用户节点");

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
      entries: [...baseHistory().entries, { type: "summary", entry_id: u(90), parent_id: C2, run_id: R2, created_at: 1 }],
    }),
  /type 无效|叶节点/,
  "未知节点类型报错",
);
reject((wire) => {
  wire.entries[2].message = { role: "system" };
}, /含未定义字段/);
reject((wire) => {
  delete wire.entries[0].message;
}, /缺少必填字段/);
reject((wire) => {
  wire.entries[0].summary = "摘要";
}, /含未定义字段/);
reject((wire) => {
  wire.entries[2].entry_id = "not-a-uuid";
}, /身份无效/);
reject((wire) => {
  wire.entries[2].created_at = -1;
}, /无效/);
reject((wire) => {
  wire.entries[2].parent_id = u(91);
}, /分支链无效/);
reject((wire) => {
  wire.session.active_leaf_id = A1;
}, /叶节点不匹配/);
reject((wire) => {
  wire.entries[4].entry_id = U1;
}, /身份重复/);
reject((wire) => {
  wire.runs[1].last_entry_id = A1;
}, /末节点无效/);
reject((wire) => {
  wire.runs[1].request_entry_id = C2;
}, /请求节点无效/);
reject((wire) => {
  wire.steering[0].entry_id = C1;
}, /消费节点无效/);
reject((wire) => {
  wire.entries[4].run_id = u(92);
}, /运行归属无效/);
reject((wire) => {
  wire.session.active_leaf_id = null;
}, /空分支历史无效/);

// 任务三：普通最终回答 stop_reason=length 保存已输出内容并以 completed 收尾（既有行为）
const RUN = u(70);
const MSG = u(71);
const apply = (state, event, data) =>
  reactAgent.applyReActEvent(state, { event, data });
const text = [{ content_index: 0, type: "text", text: "已输出的截断内容" }];
let live = apply({ id: RUN, run_id: RUN, status: "running", entries: [] }, "message_start", { run_id: RUN, message_id: MSG, content: [] });
live = apply(live, "message_update", { run_id: RUN, message_id: MSG, content: text, content_index: 0, update_type: "text_delta" });
live = apply(live, "message_end", { run_id: RUN, message_id: MSG, content: text, stop_reason: "length", entry_id: MSG, parent_id: null });
assert.equal(live.entries[0].stop_reason, "length", "length 终态保留在助手消息");
live = apply(live, "done", { run_id: RUN, status: "completed", stop_reason: "length" });
assert.equal(live.status, "completed", "length 以 completed 结束运行");
assert.equal(live.entries[0].content[0].text, "已输出的截断内容", "length 保留已输出内容");

// 长度事件真实解析：message_end 的 stop_reason 接受 length，done 接受 length
const events = [];
const parser = reactAgent.createReActParser((event) => events.push(event), RUN);
parser.feed(`event: message_end\ndata: ${JSON.stringify({ run_id: RUN, message_id: MSG, content: text, stop_reason: "length", entry_id: MSG, parent_id: null })}\n\n`);
parser.feed(`event: done\ndata: ${JSON.stringify({ run_id: RUN, status: "completed", stop_reason: "length" })}\n\n`);
parser.finish();
assert.deepEqual(events.map((event) => event.event), ["message_end", "done"], "解析器接受 length 的既有事件序列");

/* ===== 任务二：压缩阶段事件解析与运行 reducer ===== */
const CRUN = u(80);
const CID = u(81);
const CPARENT = u(82);
const runRound = () => ({ id: CRUN, run_id: CRUN, status: "running", entries: [] });

/** 单事件真实解析：返回解析后的事件对象；结构非法时在 feed 处抛错 */
function one(event, data) {
  const seen = [];
  const parser = reactAgent.createReActParser((item) => seen.push(item), CRUN);
  parser.feed(`event: ${event}\ndata: ${JSON.stringify({ run_id: CRUN, ...data })}\n\n`);
  assert.equal(seen.length, 1);
  return seen[0];
}
const rejectEvent = (event, data, expected) =>
  assert.throws(
    () =>
      reactAgent.createReActParser(() => {}, CRUN).feed(
        `event: ${event}\ndata: ${JSON.stringify({ run_id: CRUN, ...data })}\n\n`,
      ),
    expected,
  );

/** 经真实解析器解析后应用到运行 reducer */
const step = (state, event, data) => reactAgent.applyReActEvent(state, one(event, data));

// 结构校验：compaction_id 为 UUID，reason 为 threshold/overflow；end 的 entry_id 等于 compaction_id，parent_id 非 null
one("compaction_start", { compaction_id: CID, reason: "threshold" });
one("compaction_start", { compaction_id: CID, reason: "overflow" });
one("compaction_end", { compaction_id: CID, entry_id: CID, parent_id: CPARENT });
rejectEvent("compaction_start", { compaction_id: "not-a-uuid", reason: "threshold" }, /压缩开始事件无效/);
rejectEvent("compaction_start", { compaction_id: CID, reason: "manual" }, /压缩开始事件无效/);
rejectEvent("compaction_end", { compaction_id: CID, entry_id: u(84), parent_id: CPARENT }, /压缩完成事件无效/);
rejectEvent("compaction_end", { compaction_id: CID, entry_id: CID, parent_id: null }, /压缩完成事件无效/);

// 阶段状态：开始登记并保持 running，完成在检查点提交后清除且不结束运行
const started = step(runRound(), "compaction_start", { compaction_id: CID, reason: "overflow" });
assert.equal(started.status, "running", "压缩期间保持 running");
assert.equal(started.compaction_id, CID);
assert.deepEqual(started.entries, [], "压缩开始不产生可见条目");
const ended = step(started, "compaction_end", { compaction_id: CID, entry_id: CID, parent_id: CPARENT });
assert.equal(ended.compaction_id, undefined, "compaction_end 清除阶段");
assert.equal(ended.status, "running", "compaction_end 不结束运行");

// 重复开始、缺失开始的完成、身份不匹配的完成按协议异常拒绝
assert.throws(() => step(started, "compaction_start", { compaction_id: u(83), reason: "threshold" }), /重复/);
assert.throws(() => step(runRound(), "compaction_end", { compaction_id: CID, entry_id: CID, parent_id: CPARENT }), /不匹配/);
assert.throws(() => step(started, "compaction_end", { compaction_id: u(83), entry_id: u(83), parent_id: CPARENT }), /不匹配/);

// 压缩中收到 done：协议异常；收到 error：按失败或取消收尾并清除阶段（允许缺少 compaction_end）
assert.throws(() => step(started, "done", { status: "completed", stop_reason: "stop" }), /压缩阶段未结束/);
const failed = step(started, "error", { status: "failed", code: "compaction_failed", message: "压缩失败。", tool_call_id: null });
assert.equal(failed.status, "failed", "压缩失败收尾为 failed");
assert.equal(failed.compaction_id, undefined, "失败收尾清除压缩阶段");
const cancelled = step(started, "error", { status: "cancelled", code: "cancelled", message: "已取消。", tool_call_id: null });
assert.equal(cancelled.status, "cancelled", "压缩取消收尾为 cancelled");
assert.equal(cancelled.compaction_id, undefined, "取消收尾清除压缩阶段");

// 完整运行：压缩发生在单次运行内部，结束后继续执行并以 done 收尾
const FASSIST = u(85);
const FTOOL = u(86);
const FANSWER = u(87);
const TC = "tc-compact";
const call = [{ content_index: 0, type: "tool_call", tool_call_id: TC, name: "read", arguments: {} }];
const answer = [{ content_index: 0, type: "text", text: "最终回答。" }];
let flow = runRound();
flow = step(flow, "message_start", { message_id: FASSIST, content: [] });
flow = step(flow, "message_update", { message_id: FASSIST, content: call, content_index: 0, update_type: "toolcall_delta" });
flow = step(flow, "message_end", { message_id: FASSIST, content: call, stop_reason: "toolUse", entry_id: FASSIST, parent_id: null });
flow = step(flow, "tool_start", { tool_call_id: TC, name: "read", arguments: {} });
flow = step(flow, "tool_result", { tool_call_id: TC, content: "ok", is_error: false, entry_id: FTOOL, parent_id: FASSIST });
flow = step(flow, "compaction_start", { compaction_id: CID, reason: "threshold" });
assert.equal(flow.compaction_id, CID);
flow = step(flow, "compaction_end", { compaction_id: CID, entry_id: CID, parent_id: FTOOL });
flow = step(flow, "message_start", { message_id: FANSWER, content: [] });
flow = step(flow, "message_update", { message_id: FANSWER, content: answer, content_index: 0, update_type: "text_delta" });
flow = step(flow, "message_end", { message_id: FANSWER, content: answer, stop_reason: "stop", entry_id: FANSWER, parent_id: CID });
flow = step(flow, "done", { status: "completed", stop_reason: "stop" });
assert.equal(flow.status, "completed", "压缩后继续执行并正常收尾");
assert.equal(flow.compaction_id, undefined, "收尾后无残留阶段");
assert.deepEqual(flow.entries.map((entry) => entry.kind), ["assistant", "tool", "assistant"], "压缩节点不作为可见条目");
assert.equal(flow.entries[2].content[0].text, "最终回答。", "压缩后的真实助手内容完整");

console.log(
  "PASS: 压缩隐藏节点祖先链与叶节点、非法节点归属拒绝、length 原行为，以及压缩阶段事件解析、保持 running、严格拒绝与失败/取消收尾",
);
