// 运行：node --experimental-transform-types scripts/chat-steering-merge-check.mjs
import assert from "node:assert/strict";
import { applyReActEvent, applySteeringStatus } from "../src/features/chat/utils/reactAgent.ts";

const RUN = "11111111-1111-4111-8111-111111111111";
const OTHER = "22222222-2222-4222-8222-222222222222";
const STEER = "33333333-3333-4333-8333-333333333333";
const ENTRY = "44444444-4444-4444-8444-444444444444";
const request = "追加输入。";

const base = () => ({ id: "op", run_id: RUN, entries: [], status: "running" });

// 首次 accepted 收敛为 pending；随后同终态重复到达幂等合并且不抛协议异常
const accepted = applySteeringStatus(base(), { run_id: RUN, steering_id: STEER, status: "accepted", entry_id: null, reason: null, operation_id: "op" }, request);
assert.equal(accepted.entries[0].steering.status, "pending");
const consumed = applySteeringStatus(accepted, { run_id: RUN, steering_id: STEER, status: "consumed", entry_id: ENTRY, reason: null }, request);
assert.equal(consumed.entries[0].steering.status, "consumed");
assert.equal(consumed.entries[0].steering.entry_id, ENTRY);

// 相同终态重复到达（JSON 响应 / SSE 通知 / 查询）不抛异常，保持首次终态
const again = applySteeringStatus(consumed, { run_id: RUN, steering_id: STEER, status: "consumed", entry_id: ENTRY, reason: null }, request);
assert.equal(again.entries[0].steering.status, "consumed");
// 后到的 accepted/pending 只补齐原文与身份，保持已确认终态
const late = applySteeringStatus(consumed, { run_id: RUN, steering_id: STEER, status: "accepted", entry_id: null, reason: null, operation_id: "op" }, request);
assert.equal(late.entries[0].steering.status, "consumed");
assert.equal(late.entries[0].steering.entry_id, ENTRY);

// SSE steering_status 重复同终态不抛异常
const round = { ...base(), entries: [] };
const first = applyReActEvent(round, { event: "steering_status", data: { run_id: RUN, steering_id: STEER, status: "consumed", entry_id: ENTRY, reason: null } });
assert.equal(first.entries[0].steering.status, "consumed");
const second = applyReActEvent(first, { event: "steering_status", data: { run_id: RUN, steering_id: STEER, status: "consumed", entry_id: ENTRY, reason: null } });
assert.equal(second.entries[0].steering.status, "consumed");

// 真实矛盾仍按协议异常处理：运行身份不符 / 标识冲突 / 原文不符
assert.throws(() => applySteeringStatus(base(), { run_id: OTHER, steering_id: STEER, status: "consumed", entry_id: ENTRY, reason: null }, request), /运行身份/);
assert.throws(() => applyReActEvent({ ...base(), entries: [{ kind: "assistant", id: STEER, content: [] }] }, { event: "steering_status", data: { run_id: RUN, steering_id: STEER, status: "consumed", entry_id: ENTRY, reason: null } }), /标识冲突/);
assert.throws(() => applySteeringStatus(consumed, { run_id: RUN, steering_id: STEER, status: "consumed", entry_id: ENTRY, reason: null }, "不同的原文。"), /重复/);

console.log("PASS: steering 状态幂等合并与真实矛盾协议异常");
