// 运行：node scripts/session-history-refresh-failure-check.mjs
// 前置：隔离数据库后端与 vite dev server 已在 http://127.0.0.1:5173 可用。
// 覆盖：终态确认后历史刷新失败进入明确失败状态、保留展示与账本、显式重载后释放占用，且不自动重发。
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { closeSync, mkdirSync, openSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout } from "node:timers/promises";

const root = path.resolve(import.meta.dirname, "../temp/session-history-refresh-failure");
rmSync(root, { recursive: true, force: true });
mkdirSync(root, { recursive: true });
const executable = path.join(process.env.APPDATA, "npm/node_modules/agent-browser/bin/agent-browser-win32-x64.exe");

function browser(...args) {
  const outPath = path.join(root, "command.json");
  const errPath = path.join(root, "command-error.log");
  const out = openSync(outPath, "w");
  const err = openSync(errPath, "w");
  let result;
  try {
    result = spawnSync(executable, ["--session", "fit-refresh-failure", "--profile", path.join(root, "profile"), "--json", ...args], { stdio: ["ignore", out, err], timeout: 60000 });
  } finally {
    closeSync(out);
    closeSync(err);
  }
  assert.equal(result.status, 0, readFileSync(errPath, "utf8"));
  const reply = JSON.parse(readFileSync(outPath, "utf8"));
  assert.equal(reply.success, true, JSON.stringify(reply));
  return reply.data;
}

const evaluate = (expression) => browser("eval", expression).result;
const message = "textarea[aria-label='消息']";
const stop = "button[aria-label='停止']";
const sendButton = "button[aria-label='发送']";
const transcriptText = "document.querySelector('[data-slot=message-scroller-content]').innerText";
const RELOAD = "[...document.querySelectorAll('button')].some((el) => el.textContent.trim() === '重新加载历史')";
const ledger = "(() => { const s = JSON.parse(localStorage.getItem('fit-agent:chat-client')||'null'); const id = s ? s.selected_session_id : null; return id && s.ledgers ? (s.ledgers[id] || []) : []; })()";
const send = (text) => { browser("fill", message, text); browser("press", "Enter"); };
async function until(expression, timeout, label) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (evaluate(expression)) return;
    await setTimeout(200);
  }
  assert.fail(`浏览器条件超时：${label}`);
}

const report = { steps: [], blockers: [] };
const note = (text) => { report.steps.push(text); console.log("STEP:", text); };

try {
  browser("open", "http://127.0.0.1:5173/");
  await until("!!document.querySelector('textarea')", 30000, "页面加载");
  if (evaluate("localStorage.getItem('fit-agent:chat-client')") !== null) evaluate("localStorage.clear();true");
  browser("reload");
  await until("!!document.querySelector('textarea')", 30000, "清空后加载");

  send("必须调用 bash，command 精确为 sleep 10，timeout 为 20；结束后只回复 REFRESH_FAILURE。");
  await until(`!!document.querySelector(${JSON.stringify(stop)})`, 90000, "运行开始");
  await until(`(${ledger}).some((item) => item.kind === 'send' && item.run_id)`, 30000, "运行受理并持久化操作");
  const operationId = evaluate(`(${ledger}).find((item) => item.kind === 'send').operation_id`);

  // 仅让历史读取失败：在浏览器网络层中断 /history 请求（真实网络故障，非 JS 兜底）。
  browser("network", "route", "*/history", "--abort");
  assert.equal(evaluate("window.__failHistory === undefined"), true, "不注入 fetch 兜底");

  browser("click", stop);
  await until(RELOAD, 60000, "历史刷新失败出现重载入口");
  assert.equal(evaluate(`${transcriptText}.includes('必须调用 bash')`), true, "保留已展示内容");
  assert.ok((evaluate(ledger)).some((item) => item.kind === "send" && item.operation_id === operationId), "保留未确认账本");
  assert.equal(evaluate(`!!document.querySelector(${JSON.stringify(stop)})`), false, "失败不自动重发原操作");
  browser("fill", message, "占用探测");
  assert.equal(evaluate(`document.querySelector(${JSON.stringify(sendButton)}).disabled`), true, "历史成功读取前保持占用");
  browser("fill", message, "");
  report.failureState = { reloadEntry: true, occupancyRetained: true, ledgerRetained: true };
  note("终态后历史刷新失败进入明确失败状态并保留占用");

  browser("network", "unroute", "*/history");
  browser("find", "role", "button", "click", "--name", "重新加载历史");
  await until(`!${RELOAD}`, 30000, "重载后失败入口消失");
  await until(`(${ledger}).length === 0`, 60000, "重载后释放占用并清理账本");
  browser("fill", message, "只回复 AFTER_REFRESH_FAILURE，不调用工具。");
  assert.equal(evaluate(`document.querySelector(${JSON.stringify(sendButton)}).disabled`), false, "重载后可再次发送");
  browser("press", "Enter");
  await until(`!document.querySelector(${JSON.stringify(stop)}) && ${transcriptText}.includes('AFTER_REFRESH_FAILURE')`, 90000, "重载后继续发送成功");
  await until(`(${ledger}).length === 0`, 30000, "续聊收尾");
  report.recovered = { sendRetried: false, resumed: true };
  note("显式重载后释放占用并恢复续聊");

  report.browserErrors = browser("errors");
  assert.deepEqual(report.browserErrors.errors, []);
  report.result = "PASS";
} catch (failure) {
  report.result = "FAIL";
  report.error = String(failure && failure.stack ? failure.stack : failure);
  throw failure;
} finally {
  try { browser("close"); } catch {}
  writeFileSync(path.join(root, "result.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
}
