import assert from "node:assert/strict";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout } from "node:timers/promises";

const root = path.resolve(import.meta.dirname, "../temp/chat-contract-probe");
mkdirSync(root, { recursive: true });
const origin = "http://127.0.0.1:8000";
const log = [];
const json = { "Content-Type": "application/json" };
const stream = { "Content-Type": "application/json", Accept: "text/event-stream" };
const call = async (label, route, init) => {
  const response = await fetch(origin + route, init);
  log.push(`${label} -> ${response.status} ${response.headers.get("content-type") ?? ""}`);
  return response;
};
const readAll = async (response) => {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let text = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    text += decoder.decode(value, { stream: true });
  }
  return text;
};

const controller = new AbortController();
try {
  // 发送：首次 SSE、重复 JSON 受理、同键冲突、操作/运行查询身份
  const session = crypto.randomUUID();
  const operation = crypto.randomUUID();
  const request = "只回复 DUPLICATE_PROBE，不调用工具。";
  const create = await call("POST /api/sessions", "/api/sessions", { method: "POST", headers: json, body: JSON.stringify({ session_id: session, title: request }) });
  assert.equal(create.status, 201);
  const first = await call("POST /api/agent/run #1", "/api/agent/run", { method: "POST", headers: stream, body: JSON.stringify({ session_id: session, operation_id: operation, request }) });
  assert.ok((first.headers.get("content-type") ?? "").includes("text/event-stream"), "首次受理返回 SSE");
  const runId = first.headers.get("x-run-id");
  assert.ok((await readAll(first)).includes("event: done"), "首次流收到终止事件");
  const repeat = await call("POST /api/agent/run #2 同键同正文", "/api/agent/run", { method: "POST", headers: stream, body: JSON.stringify({ session_id: session, operation_id: operation, request }) });
  assert.ok((repeat.headers.get("content-type") ?? "").includes("application/json"), "重复受理返回 JSON");
  const repeatBody = await repeat.json();
  assert.equal(repeatBody.run_id, runId, "重复受理返回原运行");
  assert.equal(repeatBody.operation_id, operation);
  const conflict = await call("POST /api/agent/run #3 同键不同正文", "/api/agent/run", { method: "POST", headers: stream, body: JSON.stringify({ session_id: session, operation_id: operation, request: "不同正文。" }) });
  assert.equal(conflict.status, 409);
  assert.equal((await conflict.json()).detail.code, "operation_conflict");
  const query = await call("GET 操作查询", `/api/sessions/${session}/operations/${operation}`, { headers: json });
  const queryBody = await query.json();
  assert.equal(queryBody.accepted, true);
  assert.ok(queryBody.run && queryBody.run.run_id === runId && queryBody.run.request_entry_id, "已受理查询返回完整运行对象");
  const run = await call("GET 运行查询", `/api/sessions/${session}/runs/${runId}`, { headers: json });
  assert.ok(["completed", "failed", "cancelled"].includes((await run.json()).status), "运行终态可查询");

  // Steering：接受/重复/同键冲突/撤回/消费冲突
  const steerSession = crypto.randomUUID();
  const sendOperation = crypto.randomUUID();
  const steerRequest = "逐行输出从 1 到 100000 的整数，不要省略，最后一行只写 STEER_PROBE。无需使用工具。";
  await call("POST /api/sessions (steering)", "/api/sessions", { method: "POST", headers: json, body: JSON.stringify({ session_id: steerSession, title: steerRequest }) });
  const steerRun = await call("POST /api/agent/run (steering)", "/api/agent/run", { method: "POST", headers: stream, body: JSON.stringify({ session_id: steerSession, operation_id: sendOperation, request: steerRequest }), signal: controller.signal });
  const steerRunId = steerRun.headers.get("x-run-id");
  assert.ok(steerRunId, "Steering 目标运行返回 run_id");
  const pump = (async () => { const reader = steerRun.body.getReader(); try { for (;;) { const { done } = await reader.read(); if (done) break; } } catch { /* 收尾中止 */ } })();
  const steer = (operationId, message) => call(`POST steering ${operationId.slice(0, 8)}`, `/api/agent/runs/${steerRunId}/steering`, { method: "POST", headers: json, body: JSON.stringify({ session_id: steerSession, operation_id: operationId, message }) });

  const steeringOperation = crypto.randomUUID();
  const accepted = await steer(steeringOperation, "追加 A。");
  const acceptedBody = await accepted.json();
  assert.equal(acceptedBody.created, true);
  assert.equal(acceptedBody.status, "accepted");
  assert.equal(acceptedBody.entry_id, null);
  assert.equal(acceptedBody.reason, null);
  assert.equal(acceptedBody.run_id, steerRunId);
  const repeated = await (await steer(steeringOperation, "追加 A。")).json();
  assert.equal(repeated.created, false, "重复接收 created=false");
  assert.equal(repeated.steering_id, acceptedBody.steering_id);
  assert.ok(["pending", "consumed", "withdrawn", "discarded"].includes(repeated.status));
  const conflictSteer = await steer(steeringOperation, "不同的追加文本。");
  assert.equal(conflictSteer.status, 409);
  assert.equal((await conflictSteer.json()).detail.code, "operation_conflict");
  const operationBody = await (await call("GET 操作查询(steering)", `/api/sessions/${steerSession}/operations/${steeringOperation}`, { headers: json })).json();
  assert.equal(operationBody.kind, "steering");
  assert.equal(operationBody.steering.steering_id, acceptedBody.steering_id);
  assert.equal(operationBody.run.run_id, steerRunId, "steering 关联同一运行");
  const withdrawn = await (await call("POST withdraw", `/api/agent/runs/${steerRunId}/steering/${acceptedBody.steering_id}/withdraw`, { method: "POST", headers: json, body: JSON.stringify({ session_id: steerSession }) })).json();
  assert.equal(withdrawn.status, "withdrawn");
  assert.equal(withdrawn.entry_id, null);
  assert.equal(withdrawn.reason, null);
  const withdrawnAgain = await (await call("POST withdraw 重复", `/api/agent/runs/${steerRunId}/steering/${acceptedBody.steering_id}/withdraw`, { method: "POST", headers: json, body: JSON.stringify({ session_id: steerSession }) })).json();
  assert.equal(withdrawnAgain.status, "withdrawn", "重复撤回返回同一状态");

  const secondOperation = crypto.randomUUID();
  await steer(secondOperation, "追加 B。");
  let terminal = null;
  for (let index = 0; index < 80; index += 1) {
    const polled = await (await call("GET 操作查询(steering B)", `/api/sessions/${steerSession}/operations/${secondOperation}`, { headers: json })).json();
    terminal = polled.steering?.status ?? null;
    if (terminal !== null && terminal !== "pending") break;
    await setTimeout(250);
  }
  if (terminal === "consumed") {
    const polled = await (await call("GET 操作查询(steering B 终态)", `/api/sessions/${steerSession}/operations/${secondOperation}`, { headers: json })).json();
    const conflictWithdraw = await call("POST withdraw 已消费", `/api/agent/runs/${steerRunId}/steering/${polled.steering.steering_id}/withdraw`, { method: "POST", headers: json, body: JSON.stringify({ session_id: steerSession }) });
    assert.equal(conflictWithdraw.status, 409);
    assert.equal((await conflictWithdraw.json()).detail.code, "steering_consumption_conflict");
    log.push("消费冲突：已消费输入撤回返回 409 steering_consumption_conflict");
  } else {
    log.push(`消费冲突未触发（输入终态=${terminal}）`);
  }
  controller.abort();
  await pump;

  log.push("PASS: 重复 JSON 受理/同键冲突/操作与运行查询；Steering 接受/重复/冲突/撤回/消费冲突");
  console.log(log.join("\n"));
} finally {
  controller.abort();
  writeFileSync(path.join(root, "probe.log"), `${log.join("\n")}\n`);
}
