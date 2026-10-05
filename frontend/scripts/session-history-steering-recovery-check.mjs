// 运行：node scripts/session-history-steering-recovery-check.mjs
// 前置：隔离数据库后端与 vite dev server 已在 http://127.0.0.1:5173 可用。
// 覆盖：无原操作账本的浏览器对历史 pending Steering 的撤回、消费冲突补查与结果未知恢复。
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { closeSync, mkdirSync, openSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout } from "node:timers/promises";

const root = path.resolve(import.meta.dirname, "../temp/session-history-steering-recovery");
rmSync(root, { recursive: true, force: true });
mkdirSync(root, { recursive: true });
const executable = path.join(process.env.APPDATA, "npm/node_modules/agent-browser/bin/agent-browser-win32-x64.exe");

function run(session, ...args) {
  const outPath = path.join(root, `command-${session}.json`);
  const errPath = path.join(root, `error-${session}.log`);
  const out = openSync(outPath, "w");
  const err = openSync(errPath, "w");
  let result;
  try {
    result = spawnSync(executable, ["--session", session, "--profile", path.join(root, `profile-${session}`), "--json", ...args], { stdio: ["ignore", out, err], timeout: 60000 });
  } finally {
    closeSync(out);
    closeSync(err);
  }
  assert.equal(result.status, 0, readFileSync(errPath, "utf8"));
  const reply = JSON.parse(readFileSync(outPath, "utf8"));
  assert.equal(reply.success, true, JSON.stringify(reply));
  return reply.data;
}

const owner = (...args) => run("fit-steer-owner", ...args);
const observer = (...args) => run("fit-steer-observer", ...args);
const evaluate = (driver, expression) => driver("eval", expression).result;
const raw = (driver) => JSON.parse(evaluate(driver, "localStorage.getItem('fit-agent:chat-client')") || "null");
const until = async (driver, expression, timeout, label) => {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (evaluate(driver, expression)) return;
    await setTimeout(200);
  }
  assert.fail(`浏览器条件超时：${label}`);
};

const message = "textarea[aria-label='消息']";
const stop = "button[aria-label='停止']";
const transcriptText = "document.querySelector('[data-slot=message-scroller-content]').innerText";
const WITHDRAW = "[...document.querySelectorAll('button')].some((el) => el.textContent.trim() === '撤回')";
const ALERT = "!!document.querySelector('[role=alert]')";
const activeId = "(JSON.parse(localStorage.getItem('fit-agent:chat-client')||'null')||{}).selected_session_id";
const send = (driver, text) => { driver("fill", message, text); driver("press", "Enter"); };

async function history(sessionId) {
  const response = await fetch(`http://127.0.0.1:8000/api/sessions/${sessionId}/history`);
  if (response.status !== 200) assert.fail(`历史读取失败：${response.status} ${await response.text()}`);
  return response.json();
}

async function steeringStatus(sessionId, steeringId, timeout) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const body = await history(sessionId);
    const record = body.steering.find((item) => item.steering_id === steeringId);
    if (record !== undefined && record.status !== "pending") return record.status;
    await setTimeout(500);
  }
  assert.fail(`Steering 未进入终态：${steeringId}`);
}

const report = { steps: [], blockers: [] };
const note = (text) => { report.steps.push(text); console.log("STEP:", text); };

try {
  // 两个浏览器均无历史存储：owner 通过真实发送建立运行，observer 仅读历史、无原操作账本。
  owner("open", "http://127.0.0.1:5173/");
  await until(owner, "!!document.querySelector('textarea')", 30000, "owner 页面加载");
  if (raw(owner) !== null) owner("eval", "localStorage.clear();true");
  owner("reload");
  await until(owner, "!!document.querySelector('textarea')", 30000, "owner 清空后加载");

  // 1. 撤回：observer 无原账本，用历史的 session_id/run_id/steering_id 成功撤回
  send(owner, "必须调用 bash，command 精确为 sleep 20，timeout 为 30；结束后只回复 STEER_RUN_ONE。");
  await until(owner, `!!document.querySelector(${JSON.stringify(stop)})`, 90000, "运行一开始");
  await until(owner, "location.pathname.startsWith('/sessions/')", 20000, "运行一会话 URL");
  const sessionId = evaluate(owner, "location.pathname.split('/').pop()");
  await until(owner, `(() => { const s = localStorage.getItem('fit-agent:chat-client'); const id = ${activeId}; const l = s && id ? (JSON.parse(s).ledgers[id] || []) : []; return l.some((item) => item.kind === 'send' && item.run_id); })()`, 30000, "运行一受理");

  send(owner, "STEER_WITHDRAW_ONLY。");
  await until(owner, `(() => { const s = localStorage.getItem('fit-agent:chat-client'); const id = ${activeId}; const l = s && id ? (JSON.parse(s).ledgers[id] || []) : []; return l.some((item) => item.kind === 'steering' && item.steering_id); })()`, 30000, "撤回 Steering 受理");

  observer("open", `http://127.0.0.1:5173/sessions/${sessionId}`);
  await until(observer, "!!document.querySelector('textarea')", 30000, "observer 加载");
  await until(observer, `${transcriptText}.includes('STEER_WITHDRAW_ONLY')`, 30000, "observer 恢复 pending 输入");
  await until(observer, WITHDRAW, 30000, "observer 出现撤回按钮");
  assert.ok(!raw(observer).ledgers?.[sessionId], "observer 无原操作账本");
  observer("find", "role", "button", "click", "--name", "撤回");
  await until(observer, `!${WITHDRAW}`, 30000, "撤回后按钮消失");
  assert.equal(evaluate(observer, `${transcriptText}.includes('STEER_WITHDRAW_ONLY')`), true, "撤回后保留输入原文");
  assert.equal(evaluate(observer, ALERT), false, "撤回成功无错误");
  report.noLedgerWithdraw = { sessionId, observedEmptyLedger: true };
  note("无原账本浏览器撤回历史 pending 输入成功");

  owner("click", stop);
  await until(owner, `(() => { const s = localStorage.getItem('fit-agent:chat-client'); const id = ${activeId}; const l = s && id ? (JSON.parse(s).ledgers[id] || []) : []; return l.length === 0; })()`, 90000, "运行一收尾");

  // 2. 消费冲突：observer 点击撤回时后端已消费，改查真实状态并展示 consumed
  send(owner, "必须调用 bash，command 精确为 sleep 12，timeout 为 60；bash 完成后用不少于 500 字逐段描述刚才执行的每一步，最后一行只写 STEER_RUN_TWO。");
  await until(owner, `!!document.querySelector(${JSON.stringify(stop)})`, 90000, "运行二开始");
  await until(owner, `(() => { const s = localStorage.getItem('fit-agent:chat-client'); const id = ${activeId}; const l = s && id ? (JSON.parse(s).ledgers[id] || []) : []; return l.some((item) => item.kind === 'send' && item.run_id); })()`, 30000, "运行二受理");
  send(owner, "STEER_CONFLICT_ONLY。");
  await until(owner, `(() => { const s = localStorage.getItem('fit-agent:chat-client'); const id = ${activeId}; const l = s && id ? (JSON.parse(s).ledgers[id] || []) : []; return l.some((item) => item.kind === 'steering' && item.steering_id); })()`, 30000, "冲突 Steering 受理");
  const conflictId = raw(owner).ledgers[sessionId].find((item) => item.kind === "steering").steering_id;

  observer("open", `http://127.0.0.1:5173/sessions/${sessionId}`);
  await until(observer, "!!document.querySelector('textarea')", 30000, "observer 二次加载");
  await until(observer, `${transcriptText}.includes('STEER_CONFLICT_ONLY')`, 30000, "observer 恢复冲突输入");
  await until(observer, WITHDRAW, 30000, "observer 出现冲突撤回按钮");
  const consumed = await steeringStatus(sessionId, conflictId, 60000);
  assert.equal(consumed, "consumed", "后端已消费");
  observer("find", "role", "button", "click", "--name", "撤回");
  await until(observer, `!${WITHDRAW}`, 30000, "冲突后按钮消失");
  await setTimeout(1000);
  assert.equal(evaluate(observer, "[...document.querySelectorAll('[data-slot=message-scroller-item]')].filter((el) => el.innerText === 'STEER_CONFLICT_ONLY。').length"), 1, "冲突输入只展示一次");
  assert.equal(evaluate(observer, WITHDRAW), false, "冲突后展示真实 consumed 状态且不再提供撤回");
  report.consumptionConflict = { steeringId: conflictId, status: consumed };
  note("消费冲突后查询并展示真实 consumed 状态");

  await until(owner, `(() => { const s = localStorage.getItem('fit-agent:chat-client'); const id = ${activeId}; const l = s && id ? (JSON.parse(s).ledgers[id] || []) : []; return l.length === 0; })()`, 90000, "运行二收尾");

  // 3. 结果未知：真实网络故障下保留未确认状态，恢复后再次撤回完成
  send(owner, "必须调用 bash，command 精确为 sleep 25，timeout 为 35；结束后只回复 STEER_RUN_THREE。");
  await until(owner, `!!document.querySelector(${JSON.stringify(stop)})`, 90000, "运行三开始");
  await until(owner, `(() => { const s = localStorage.getItem('fit-agent:chat-client'); const id = ${activeId}; const l = s && id ? (JSON.parse(s).ledgers[id] || []) : []; return l.some((item) => item.kind === 'send' && item.run_id); })()`, 30000, "运行三受理");
  send(owner, "STEER_UNKNOWN_ONLY。");
  await until(owner, `(() => { const s = localStorage.getItem('fit-agent:chat-client'); const id = ${activeId}; const l = s && id ? (JSON.parse(s).ledgers[id] || []) : []; return l.some((item) => item.kind === 'steering' && item.steering_id); })()`, 30000, "未知 Steering 受理");

  observer("open", `http://127.0.0.1:5173/sessions/${sessionId}`);
  await until(observer, "!!document.querySelector('textarea')", 30000, "observer 三次加载");
  await until(observer, `${transcriptText}.includes('STEER_UNKNOWN_ONLY')`, 30000, "observer 恢复未知输入");
  await until(observer, WITHDRAW, 30000, "observer 出现未知撤回按钮");
  observer("set", "offline", "on");
  observer("find", "role", "button", "click", "--name", "撤回");
  await until(observer, ALERT, 30000, "离线撤回进入结果未知");
  assert.equal(evaluate(observer, WITHDRAW), true, "结果未知保留撤回入口");
  assert.equal(evaluate(observer, `${transcriptText}.includes('未确认') || document.body.innerText.includes('状态查询失败')`), true, "展示未确认状态");
  observer("set", "offline", "off");
  observer("find", "role", "button", "click", "--name", "撤回");
  await until(observer, `!${WITHDRAW}`, 30000, "恢复后撤回完成");
  assert.equal(evaluate(observer, `${transcriptText}.includes('STEER_UNKNOWN_ONLY')`), true, "撤回后保留输入原文");
  report.unknownRecovery = { retained: true, recovered: true };
  note("结果未知保留未确认并可补查完成");

  owner("click", stop);
  await until(owner, `(() => { const s = localStorage.getItem('fit-agent:chat-client'); const id = ${activeId}; const l = s && id ? (JSON.parse(s).ledgers[id] || []) : []; return l.length === 0; })()`, 90000, "运行三收尾");

  report.browserErrors = observer("errors");
  assert.deepEqual(report.browserErrors.errors, []);
  report.result = "PASS";
} catch (failure) {
  report.result = "FAIL";
  report.error = String(failure && failure.stack ? failure.stack : failure);
  throw failure;
} finally {
  try { owner("close"); } catch {}
  try { observer("close"); } catch {}
  writeFileSync(path.join(root, "result.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
}
