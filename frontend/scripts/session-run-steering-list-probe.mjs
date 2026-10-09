// 运行：node scripts/session-run-steering-list-probe.mjs
// 真实后端联调探针：自行以隔离数据库启动现有 HTTP 入口（凭据取自 backend/.env，不改动任何后端文件）。
// 覆盖：空列表、running/cancelled/interrupted/completed、pending/withdrawn/consumed/discarded、
//       身份与毫秒时间、null 字段、404/422 错误传递及 AbortSignal。
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdirSync, openSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { createServer } from "vite";

const root = path.resolve(import.meta.dirname, "../temp/session-run-steering-list-probe");
rmSync(root, { recursive: true, force: true });
mkdirSync(root, { recursive: true });
const backend = path.resolve(import.meta.dirname, "../../backend");
const database = path.join(root, "isolated.db");
const origin = "http://127.0.0.1:8000";
const log = [];
const note = (text) => {
  log.push(text);
  console.log("NOTE:", text);
};

const nodeFetch = globalThis.fetch;
// 请求层沿用浏览器的相对路径；探针把同一相对路径指向真实后端 origin。
globalThis.fetch = (input, init) =>
  nodeFetch(typeof input === "string" && input.startsWith("/") ? origin + input : input, init);

let child = null;
function startBackend(reset) {
  const out = openSync(path.join(root, `backend-${reset ? "fresh" : "restart"}.log`), "w");
  child = spawn(
    path.join(backend, ".venv/Scripts/python.exe"),
    [path.resolve(import.meta.dirname, "session-list-probe-server.py")],
    {
      cwd: backend,
      env: { ...process.env, LIST_PROBE_DB: database, LIST_PROBE_RESET: reset ? "1" : "0" },
      stdio: ["ignore", out, out],
    },
  );
  return new Promise((resolve) => child.once("exit", resolve));
}

async function waitBackend() {
  const deadline = Date.now() + 60000;
  for (;;) {
    const response = await nodeFetch(`${origin}/api/sessions`).catch(() => null);
    if (response !== null && response.status === 200) return;
    if (Date.now() > deadline) assert.fail("隔离后端未就绪。");
    await delay(500);
  }
}

async function until(predicate, timeout, label) {
  const deadline = Date.now() + timeout;
  for (;;) {
    const value = await predicate();
    if (value) return value;
    if (Date.now() > deadline) assert.fail(`条件超时：${label}`);
    await delay(500);
  }
}

async function rejects(action, label) {
  let failure = null;
  try {
    await action();
  } catch (error) {
    failure = error;
  }
  assert.ok(failure instanceof Error, `${label} 必须抛出真实错误`);
  return failure;
}

const server = await createServer({
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});

const probe = new AbortController();
let result = "FAIL";
try {
  const api = await server.ssrLoadModule("/src/lib/api.ts");
  const freshExit = startBackend(true);
  await waitBackend();
  const session = crypto.randomUUID();
  await api.createSession({ session_id: session, title: "运行与 Steering 独立列表联调。" }, probe.signal);

  // 1. 会话存在且没有运行：真实空数组返回
  assert.deepEqual(await api.listSessionRuns(session, probe.signal), { session_id: session, runs: [] }, "空运行列表");
  note("运行列表空数组返回成功");

  // 2. 错误传递：不存在会话、不存在运行、非法路径 UUID 均抛真实错误，不返回伪造空列表
  const missingSession = await rejects(() => api.listSessionRuns(crypto.randomUUID(), probe.signal), "会话不存在");
  assert.equal(missingSession.code, "session_not_found");
  assert.equal(missingSession.http_status, 404);
  assert.equal(missingSession.message.includes("会话不存在"), true, "错误说明沿用后端原文");
  const missingRun = await rejects(() => api.listRunSteering(session, crypto.randomUUID(), probe.signal), "运行不存在");
  assert.equal(missingRun.code, "run_not_found");
  assert.equal(missingRun.http_status, 404);
  const invalidPath = await rejects(() => api.listSessionRuns("session-list-probe", probe.signal), "非法路径 UUID");
  assert.equal(invalidPath.code, "invalid_request");
  assert.equal(invalidPath.http_status, 422);
  const invalidSteering = await rejects(() => api.listRunSteering(session, "not-a-uuid", probe.signal), "非法运行身份");
  assert.equal(invalidSteering.code, "invalid_request");
  note("404/422 错误按 detail.code 原样抛出");

  // 3. AbortSignal：已中止信号的查询直接失败
  const aborted = new AbortController();
  aborted.abort();
  assert.equal((await rejects(() => api.listSessionRuns(session, aborted.signal), "中止信号查询")).name, "AbortError");
  note("AbortSignal 中止在请求层原样抛出");

  // 4. 执行期间查询：running 快照与空输入列表
  const longRequest = "逐行输出从 1 到 100000 的整数，不要省略，最后一行只写 LIST_PROBE_LONG。无需使用工具。";
  let cancelRunId = null;
  const cancelStream = new AbortController();
  api
    .runReActStream(
      { session_id: session, operation_id: crypto.randomUUID(), request: longRequest },
      () => {},
      cancelStream.signal,
      (headers) => {
        cancelRunId = headers.run_id;
      },
    )
    .catch(() => null);
  await until(() => cancelRunId !== null, 90000, "长运行受理");
  const during = await api.listSessionRuns(session, probe.signal);
  assert.equal(during.runs.length, 1, "执行期间返回该运行");
  const running = during.runs[0];
  assert.equal(running.run_id, cancelRunId);
  assert.equal(running.status, "running");
  assert.equal(running.finished_at, null, "运行中结束时间为 null");
  assert.equal(running.error_code, null);
  assert.equal(running.error_message, null);
  assert.deepEqual(await api.listRunSteering(session, cancelRunId, probe.signal), {
    session_id: session,
    run_id: cancelRunId,
    steering: [],
  }, "运行存在且没有输入时返回空数组");
  note("running 快照与空输入列表字段校验通过");

  // 5. 输入正文保留原文，pending 的 entry_id/reason 为 null
  const first = await api.submitSteering(
    cancelRunId,
    { session_id: session, operation_id: crypto.randomUUID(), message: "  追加 A。 " },
    probe.signal,
  );
  const afterFirst = (await api.listRunSteering(session, cancelRunId, probe.signal)).steering;
  assert.equal(afterFirst.length, 1);
  assert.equal(afterFirst[0].steering_id, first.steering_id);
  assert.equal(afterFirst[0].text, "  追加 A。 ", "输入正文保留首尾空白");
  assert.equal(afterFirst[0].status, "pending");
  assert.equal(afterFirst[0].entry_id, null);
  assert.equal(afterFirst[0].reason, null);

  const second = await api.submitSteering(
    cancelRunId,
    { session_id: session, operation_id: crypto.randomUUID(), message: "追加 B。" },
    probe.signal,
  );
  await api.withdrawSteering(cancelRunId, first.steering_id, { session_id: session }, probe.signal);
  const withdrawn = (await api.listRunSteering(session, cancelRunId, probe.signal)).steering.find(
    (item) => item.steering_id === first.steering_id,
  );
  assert.equal(withdrawn.status, "withdrawn");
  assert.equal(withdrawn.entry_id, null);
  assert.equal(withdrawn.reason, null);

  const consumed = await until(async () => {
    const list = await api.listRunSteering(session, cancelRunId, probe.signal);
    return list.steering.find((item) => item.steering_id === second.steering_id && item.status === "consumed") ?? null;
  }, 150000, "追加 B 被消费");
  assert.notEqual(consumed.entry_id, null, "consumed 携带真实用户节点");
  assert.equal(consumed.reason, null);

  // 6. 取消收尾：运行 cancelled，未消费输入进入丢弃或已消费
  const third = await api.submitSteering(
    cancelRunId,
    { session_id: session, operation_id: crypto.randomUUID(), message: "追加 C。" },
    probe.signal,
  );
  cancelStream.abort();
  const cancelled = await until(async () => {
    const list = await api.listSessionRuns(session, probe.signal);
    const item = list.runs.find((run) => run.run_id === cancelRunId);
    return item && item.status !== "running" ? item : null;
  }, 120000, "取消运行进入终态");
  assert.equal(cancelled.status, "cancelled");
  assert.notEqual(cancelled.finished_at, null, "取消终态写入结束时间");
  const cancelledSteering = (await api.listRunSteering(session, cancelRunId, probe.signal)).steering;
  assert.equal(cancelledSteering.length, 3, "三条输入全部保留");
  assert.deepEqual(cancelledSteering.map((item) => item.steering_id), [first.steering_id, second.steering_id, third.steering_id], "输入按接收顺序排列");
  assert.equal(cancelledSteering.filter((item) => item.status === "pending").length, 0, "终态后不再保留 pending");
  const left = cancelledSteering.find((item) => item.steering_id === third.steering_id);
  assert.ok(["discarded", "consumed"].includes(left.status), "取消时未消费输入进入丢弃，已消费输入保留节点");
  note(`取消收尾：运行 cancelled，第三条输入 ${left.status}${left.reason === null ? "" : `/${left.reason}`}`);

  // 7. 运行中断：写入 pending 输入后硬杀进程，重启后按中断恢复投影
  const interruptRequest = "逐行输出从 1 到 100000 的整数，不要省略，最后一行只写 LIST_PROBE_INTERRUPT。无需使用工具。";
  let interruptRunId = null;
  const interruptStream = new AbortController();
  api
    .runReActStream(
      { session_id: session, operation_id: crypto.randomUUID(), request: interruptRequest },
      () => {},
      interruptStream.signal,
      (headers) => {
        interruptRunId = headers.run_id;
      },
    )
    .catch(() => null);
  await until(() => interruptRunId !== null, 90000, "中断目标运行受理");
  const pendingInput = await api.submitSteering(
    interruptRunId,
    { session_id: session, operation_id: crypto.randomUUID(), message: "追加 D。" },
    probe.signal,
  );
  assert.equal((await api.listRunSteering(session, interruptRunId, probe.signal)).steering[0].status, "pending", "中断前输入保持 pending");
  await until(async () => {
    const list = await api.listSessionRuns(session, probe.signal);
    return list.runs.some((run) => run.run_id === interruptRunId && run.status === "running");
  }, 60000, "中断目标运行处于 running");
  const beforeKill = (await api.listSessionRuns(session, probe.signal)).runs.length;
  child.kill();
  await freshExit;
  // 端口在进程退出后仍可能短暂占用，留出一个重启缓冲窗口。
  await delay(2000);
  note(`硬杀后端时保存 ${beforeKill} 条运行，遗留 running 与 pending 输入`);

  startBackend(false);
  await waitBackend();
  const afterRestart = await api.listSessionRuns(session, probe.signal);
  assert.equal(afterRestart.runs.length, beforeKill, "重启保留全部运行记录");
  const interrupted = afterRestart.runs.find((run) => run.run_id === interruptRunId);
  assert.equal(interrupted.status, "interrupted");
  assert.equal(interrupted.finished_at, null, "中断运行的未知结束时间保持 null");
  const recovered = (await api.listRunSteering(session, interruptRunId, probe.signal)).steering;
  assert.equal(recovered.length, 1);
  assert.equal(recovered[0].steering_id, pendingInput.steering_id);
  assert.equal(recovered[0].status, "discarded");
  assert.equal(recovered[0].reason, "interrupted");
  assert.equal(recovered[0].entry_id, null);
  assert.equal(recovered[0].text, "追加 D。", "恢复后输入正文保持接收原值");
  note("重启恢复投影为 interrupted 与 discarded/interrupted");

  // 8. 续聊形成 completed 运行：取消与中断运行继续保留在列表中
  let doneEvent = null;
  let shortRunId = null;
  await api.runReActStream(
    { session_id: session, operation_id: crypto.randomUUID(), request: "只回复 LIST_PROBE_DONE，不调用工具。" },
    (event) => {
      if (event.event === "done") doneEvent = event;
    },
    probe.signal,
    (headers) => {
      shortRunId = headers.run_id;
    },
  );
  assert.notEqual(doneEvent, null, "短运行收到终止事件");
  const completedList = await api.listSessionRuns(session, probe.signal);
  assert.deepEqual(completedList.runs.map((run) => run.run_id), [cancelRunId, interruptRunId, shortRunId], "取消、中断与完成运行按开始时间一次返回");
  const completed = completedList.runs.find((run) => run.run_id === shortRunId);
  assert.equal(completed.status, "completed");
  assert.notEqual(completed.finished_at, null);
  assert.equal(completed.error_code, null);
  assert.equal(completed.error_message, null);
  assert.notEqual(completed.last_entry_id, null, "完成运行带最后提交节点");
  assert.deepEqual(await api.listRunSteering(session, shortRunId, probe.signal), {
    session_id: session,
    run_id: shortRunId,
    steering: [],
  }, "短运行无输入时返回空数组");
  note(`completed 运行加入列表，共 ${completedList.runs.length} 条运行`);

  result = "PASS";
  note("PASS: 真实后端下的空列表、running/cancelled/interrupted/completed、pending/withdrawn/consumed/discarded、身份、时间、null 与错误传递");
} finally {
  probe.abort();
  if (child !== null) child.kill();
  await delay(500);
  await server.close();
  writeFileSync(path.join(root, "result.json"), `${JSON.stringify({ result, notes: log }, null, 2)}\n`);
  console.log(JSON.stringify({ result }, null, 2));
  assert.equal(result, "PASS");
}
