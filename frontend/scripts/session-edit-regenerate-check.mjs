// 运行：node scripts/session-edit-regenerate-check.mjs
// 纯函数检查：编辑/重新生成的删除范围、账本恢复、占位轮次、done 校验与失效操作识别（不触达后端）
import assert from "node:assert/strict";
import { createServer } from "vite";

// 被测模块按项目别名引用 @/lib，须经 vite 解析加载
const server = await createServer({
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});
const {
  applyReActEvent,
  applySteeringStatus,
  isExpiredOperation,
  ReActHttpError,
} = await server.ssrLoadModule("/src/features/chat/utils/reactAgent.ts");
const {
  historyToRounds,
  operationRound,
  pruneFrom,
  reconcileLedger,
} = await server.ssrLoadModule("/src/features/chat/utils/sessionHistory.ts");
await server.close();

const u = (n) => `${n.toString(16).padStart(8, "0")}-0000-4000-8000-000000000000`;
const sessionId = u(1);
const A = u(2);
const B = u(3);
const C = u(4);
const ASSIST = u(5);
const RUN = u(6);

const round = (id, entries) => ({ id, run_id: id, entries, status: "completed" });
const user = (id, text) => ({ kind: "user", id, entry_id: id, request: text });
const rounds = [
  round(RUN, [user(A, "第一条。"), { kind: "assistant", id: ASSIST, content: [{ content_index: 0, type: "text", text: "回答。" }], stop_reason: "stop", entry_id: ASSIST, parent_id: A }]),
  round(u(7), [user(B, "第二条。")]),
  round(u(8), [user(C, "第三条。")]),
];

// 1. 编辑删除范围：保留目标之前的全部展示，移除目标及其后（session-position-contract §2）
const editPrune = pruneFrom(rounds, B);
assert.deepEqual(editPrune.kept.map((item) => item.id), [RUN], "编辑保留目标之前的轮次");
assert.equal(editPrune.kept[0].entries.map((entry) => entry.id).join(","), `${A},${ASSIST}`, "编辑保留目标之前的条目");
assert.equal(editPrune.target.id, B, "编辑返回被移除的目标节点");

// 2. 重新生成删除范围：同样移除目标及其后，目标节点由调用方放回新轮次（session-position-contract §3）
const regenPrune = pruneFrom(rounds, A);
assert.deepEqual(regenPrune.kept, [], "重新生成首条时移除全部后续展示");
assert.equal(regenPrune.target.id, A, "重新生成返回保留的目标节点");

// 3. 未确认占位轮次（session-edit-regenerate-contract §8）：编辑保留修改后正文，重新生成不伪造用户消息
const editOperation = { operation_id: u(10), session_id: sessionId, kind: "edit", run_id: null, request: "修改后。", created_at: 1, target_entry_id: B };
const editRound = operationRound(editOperation);
assert.deepEqual(editRound.entries.map((entry) => entry.id), [editOperation.operation_id], "编辑占位展示修改后正文");
assert.equal(editRound.entries[0].request, "修改后。");
assert.deepEqual(editRound.pending, [editOperation], "编辑占位保留重试入口");
const regenOperation = { operation_id: u(11), session_id: sessionId, kind: "regenerate", run_id: null, request: "", created_at: 1, target_entry_id: B };
const regenRound = operationRound(regenOperation);
assert.deepEqual(regenRound.entries, [], "重新生成占位不伪造用户消息");

// 4. 账本恢复（session-edit-regenerate-contract §8）：已受理终态移出账本，未受理按原 operation_id 查询
const history = {
  session: { session_id: sessionId, title: "会话", active_leaf_id: C, created_at: 1, updated_at: 2 },
  entries: [
    { entry_id: A, parent_id: null, run_id: null, created_at: 1, message: { role: "user", text: "第一条。", timestamp: 1, attachments: [] } },
    { entry_id: ASSIST, parent_id: A, run_id: RUN, created_at: 2, message: { role: "assistant", content: [], stop_reason: "stop", timestamp: 2 } },
  ],
  runs: [{ session_id: sessionId, run_id: RUN, request_entry_id: A, last_entry_id: ASSIST, status: "completed", started_at: 1, finished_at: 2, error_code: null, error_message: null }],
  steering: [],
};
const accepted = reconcileLedger(history, [{ ...editOperation, run_id: RUN }]);
assert.deepEqual(accepted.accepted, [editOperation.operation_id], "历史覆盖的终态编辑移出账本");
const unresolved = reconcileLedger(history, [editOperation]);
assert.deepEqual(unresolved.query, [{ roundId: editOperation.operation_id, operation: editOperation }], "未受理编辑按原 operation_id 查询");
assert.equal(unresolved.rounds.some((item) => item.id === editOperation.operation_id), true, "未受理编辑生成占位轮次");
const regenUnresolved = reconcileLedger(history, [regenOperation]);
assert.deepEqual(regenUnresolved.rounds.find((item) => item.id === regenOperation.operation_id).entries, [], "未受理重新生成占位无条目");

// 5. done 校验：重新生成轮次以目标用户节点为 request_entry_id 时正常终止（不再误判未完成）
const regenRoundLive = { id: regenOperation.operation_id, run_id: RUN, request_entry_id: B, entries: [user(B, "第二条。"), { kind: "assistant", id: ASSIST, content: [], stop_reason: "stop", entry_id: ASSIST, parent_id: B }], status: "running" };
const done = applyReActEvent(regenRoundLive, { event: "done", data: { run_id: RUN, status: "completed", stop_reason: "stop" } });
assert.equal(done.status, "completed", "重新生成运行正常收敛为完成");

// 6. 失效操作识别（用户约定）：仅 operation_conflict + reason=operation_expired 命中
assert.equal(isExpiredOperation(new ReActHttpError("冲突。", "operation_conflict", 409, "operation_expired")), true, "失效操作被识别");
assert.equal(isExpiredOperation(new ReActHttpError("冲突。", "operation_conflict", 409, null)), false, "无 reason 的冲突不静默");
assert.equal(isExpiredOperation(new ReActHttpError("冲突。", "operation_conflict", 409, "other")), false, "其他 reason 不静默");

// 7. 重新生成复用已消费 Steering 节点：该节点创建自旧运行，仍归属以它为 request_entry_id 的新运行
const U0 = u(40);
const A0 = u(41);
const S = u(42);
const A2 = u(43);
const RUN0 = u(44);
const RUN2 = u(45);
const steerId = u(46);
const historyRound = historyToRounds({
  session: { session_id: sessionId, title: "会话", active_leaf_id: A2, created_at: 1, updated_at: 2 },
  entries: [
    { entry_id: U0, parent_id: null, run_id: null, created_at: 1, message: { role: "user", text: "原始请求。", timestamp: 1, attachments: [] } },
    { entry_id: A0, parent_id: U0, run_id: RUN0, created_at: 2, message: { role: "assistant", content: [], stop_reason: "stop", timestamp: 2 } },
    { entry_id: S, parent_id: A0, run_id: RUN0, created_at: 3, message: { role: "user", text: "追加。", timestamp: 3, attachments: [] } },
    { entry_id: A2, parent_id: S, run_id: RUN2, created_at: 4, message: { role: "assistant", content: [], stop_reason: "stop", timestamp: 4 } },
  ],
  runs: [
    { session_id: sessionId, run_id: RUN0, request_entry_id: U0, last_entry_id: A0, status: "completed", started_at: 1, finished_at: 2, error_code: null, error_message: null },
    { session_id: sessionId, run_id: RUN2, request_entry_id: S, last_entry_id: A2, status: "completed", started_at: 3, finished_at: 4, error_code: null, error_message: null },
  ],
  steering: [{ session_id: sessionId, run_id: RUN0, steering_id: steerId, text: "追加。", attachments: [], timestamp: 3, status: "consumed", entry_id: S, reason: null, created_at: 3, updated_at: 3 }],
});
const byRun = new Map(historyRound.map((item) => [item.run_id, item]));
assert.deepEqual(byRun.get(RUN0).entries.map((entry) => entry.id), [U0, A0], "目标节点不再留在旧运行轮次");
assert.deepEqual(byRun.get(RUN2).entries.map((entry) => entry.id), [S, A2], "目标节点归属以它为请求节点的新运行");
assert.equal(byRun.get(RUN2).entries[0].steering.status, "consumed", "已消费状态保留");

// 8. 对已消费 Steering 目标重新生成：保留其祖先，移除目标及其后，目标由调用方放入新轮次
const preRegen = [{ id: RUN0, run_id: RUN0, request_entry_id: U0, entries: [user(U0, "原始请求。"), { kind: "assistant", id: A0, content: [], stop_reason: "stop", entry_id: A0, parent_id: U0 }, { kind: "user", id: S, entry_id: S, request: "追加。", steering: { status: "consumed", entry_id: S, reason: null }, steering_id: steerId }], status: "completed" }];
const regenFromSteer = pruneFrom(preRegen, S);
assert.deepEqual(regenFromSteer.kept[0].entries.map((entry) => entry.id), [U0, A0], "重新生成保留目标之前的祖先与助手");
assert.equal(regenFromSteer.target.id, S, "重新生成保留目标用户节点");

// 9. 实时已消费 Steering 节点按真实节点 ID 定位（session-position-contract §2）：
//    展示 ID 为 steering_id，真实用户节点 ID 是 entry_id，两者不同。
const liveSteer = [{ id: RUN0, run_id: RUN0, request_entry_id: U0, entries: [user(U0, "原始请求。"), { kind: "user", id: steerId, entry_id: S, steering_id: steerId, request: "追加。", steering: { status: "consumed", entry_id: S, reason: null } }], status: "completed" }];
const livePrune = pruneFrom(liveSteer, S);
assert.equal(livePrune.target.id, steerId, "目标按真实 entry_id 命中展示 ID 不同的实时节点");

// 10. 实时已消费 Steering 落地真实节点 ID（§6.1）：查询恢复与删除定位据此一致
const liveRound = { id: RUN0, run_id: RUN0, request_entry_id: U0, entries: [user(U0, "原始请求。")], status: "running" };
const consumed = applySteeringStatus(liveRound, { run_id: RUN0, steering_id: steerId, status: "consumed", entry_id: S, reason: null }, "追加。");
assert.equal(consumed.entries.find((entry) => entry.steering_id === steerId).entry_id, S, "已消费 Steering 节点携带真实 entry_id");

console.log("PASS: 编辑/重新生成删除范围、账本恢复、占位轮次、done 校验、失效操作识别、已消费 Steering 归属与真实节点定位");
