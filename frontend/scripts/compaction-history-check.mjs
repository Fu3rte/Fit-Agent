// 运行：node scripts/compaction-history-check.mjs
// 纯解析／状态检查：压缩节点参与祖先链、叶节点与运行归属，聊天仅从 message 节点提取消息。
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
  // 确认契约 §7.6：确认操作与待确认引用为历史响应必填数组，本场景两份均为空
  confirmations: [],
  pending_proposals: [],
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
reject((wire) => {
  delete wire.confirmations;
}, /confirmations 无效/);
reject((wire) => {
  delete wire.pending_proposals;
}, /pending_proposals 无效/);

/* 确认契约 §7.6：确认操作与待确认引用按真实节点角色、归属与时序核验 */
const P3 = u(93);
const A3 = u(94);
const T3 = u(95);
const K1 = u(96);
const R3 = u(97);
const PROPOSAL = u(98);
/** 完整待确认快照：展示节点携带 profile-plan §3.2 的完整快照对象（reactAgent 工具条目 profile 即该对象） */
const proposalContent = JSON.stringify({
  proposal_id: PROPOSAL,
  profile_id: 1,
  base_profile_version: 1,
  payload: {
    goal: "增肌",
    experience: "一年训练经历",
    environment: "健身房",
    availability: "每周三次",
    health_notes: null,
    movement_restrictions: null,
    unavailable_equipment: [],
    forbidden_exercise_ids: [],
  },
});
const confirmationWire = (overrides) => ({
  session_id: SESSION,
  operation_id: u(100),
  business_kind: "profile",
  proposal_id: PROPOSAL,
  display_entry_id: T3,
  confirmation_entry_id: K1,
  status: "processing",
  result: null,
  error: null,
  created_at: 1,
  updated_at: 1,
  ...overrides,
});
const proposalWire = (overrides) => ({
  business_kind: "profile",
  proposal_id: PROPOSAL,
  status: "processing",
  request_entry_id: P3,
  source_entry_id: A3,
  display_entry_id: T3,
  confirmation_entry_id: K1,
  ...overrides,
});
const confirmHistory = () => {
  const wire = baseHistory();
  wire.entries.push(
    message(P3, C2, null, "user"),
    message(A3, P3, R3, "assistant"),
    {
      type: "message",
      entry_id: T3,
      parent_id: A3,
      run_id: R3,
      created_at: 1,
      message: { role: "toolResult", tool_call_id: "tc-prepare", tool_name: "prepare_profile_update", content: proposalContent, is_error: false, timestamp: 1 },
    },
    message(K1, T3, null, "user"),
  );
  wire.session.active_leaf_id = K1;
  wire.runs.push(run(R3, P3, T3, "completed"));
  wire.confirmations = [confirmationWire({})];
  wire.pending_proposals = [proposalWire({})];
  return wire;
};
const confirmed = api.parseSessionHistory(SESSION, confirmHistory());
assert.equal(confirmed.confirmations.length, 1, "确认操作随历史返回");
assert.equal(confirmed.pending_proposals.length, 1, "待确认引用随历史返回");
const confirmedRounds = history.historyToRounds(confirmed);
assert.deepEqual(
  confirmedRounds.map((round) => round.id),
  [R1, R2, R3, `confirmation:${K1}`],
  "确认节点独立成轮且不改动既有运行轮次",
);
assert.deepEqual(
  confirmedRounds[2].entries.map((entry) => entry.kind),
  ["user", "assistant", "tool"],
  "确认所在运行按链顺序展示完整内容",
);
assert.deepEqual(
  confirmedRounds[2].entries[2].profile,
  JSON.parse(proposalContent),
  "展示工具条目携带完整快照对象而非仅 payload",
);
assert.equal(
  confirmedRounds[3].confirmation.status,
  "processing",
  "确认轮次携带真实操作状态",
);

const rejectConfirm = (mutate, expected) => {
  const wire = confirmHistory();
  mutate(wire);
  assert.throws(() => api.parseSessionHistory(SESSION, wire), expected);
};
// 压缩节点只参与祖先链与运行归属，不能作为确认展示、确认或待确认引用节点
rejectConfirm((wire) => {
  wire.confirmations = [confirmationWire({ display_entry_id: C2 })];
}, /确认展示节点 无效/);
rejectConfirm((wire) => {
  wire.confirmations = [confirmationWire({ confirmation_entry_id: C2 })];
}, /确认节点 无效/);
rejectConfirm((wire) => {
  wire.pending_proposals = [proposalWire({ confirmation_entry_id: C1 })];
}, /确认节点 无效/);

// length 保留已输出内容，并以 model_output_limit_exceeded 收尾。
const RUN = u(70);
const MSG = u(71);
const apply = (state, event, data) =>
  reactAgent.applyReActEvent(state, { event, data });
const text = [{ content_index: 0, type: "text", text: "已输出的截断内容" }];
let live = apply({ id: RUN, run_id: RUN, status: "running", entries: [] }, "message_start", { run_id: RUN, message_id: MSG, content: [] });
live = apply(live, "message_update", { run_id: RUN, message_id: MSG, content: text, content_index: 0, update_type: "text_delta" });
live = apply(live, "message_end", { run_id: RUN, message_id: MSG, content: text, stop_reason: "length", entry_id: MSG, parent_id: null });
assert.equal(live.entries[0].stop_reason, "length", "length 终态保留在助手消息");
const outputLimit = { run_id: RUN, status: "failed", code: "model_output_limit_exceeded", message: "回答达到输出上限，内容未完成", tool_call_id: null };
live = apply(live, "error", outputLimit);
assert.equal(live.status, "failed", "length 以 failed 结束运行");
assert.equal(live.error, outputLimit.message);
assert.equal(live.entries[0].content[0].text, "已输出的截断内容", "length 保留已输出内容");

// 真实解析器保留 length 消息，并接收输出截断错误。
const events = [];
const parser = reactAgent.createReActParser((event) => events.push(event), RUN);
parser.feed(`event: message_end\ndata: ${JSON.stringify({ run_id: RUN, message_id: MSG, content: text, stop_reason: "length", entry_id: MSG, parent_id: null })}\n\n`);
parser.feed(`event: error\ndata: ${JSON.stringify(outputLimit)}\n\n`);
parser.finish();
assert.deepEqual(events.map((event) => event.event), ["message_end", "error"], "解析器接受截断失败事件序列");

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
  "PASS: 压缩隐藏节点祖先链与叶节点、非法节点归属拒绝、length 截断失败终态、确认扩展必填数组与独立成轮，以及压缩阶段事件解析、保持 running、严格拒绝与失败/取消收尾",
);
