import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { closeSync, mkdirSync, openSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout } from "node:timers/promises";

const root = path.resolve(import.meta.dirname, "../temp/chat-persistence-check");
mkdirSync(root, { recursive: true });
const executable = path.join(process.env.APPDATA, "npm/node_modules/agent-browser/bin/agent-browser-win32-x64.exe");

function browser(...args) {
  const outPath = path.join(root, "command.json");
  const errPath = path.join(root, "command-error.log");
  const out = openSync(outPath, "w");
  const err = openSync(errPath, "w");
  let result;
  try {
    result = spawnSync(executable, ["--session", "fit-chat-persist", "--profile", path.join(root, "profile"), "--json", ...args], { stdio: ["ignore", out, err], timeout: 60000 });
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
const RAW = 'JSON.parse(localStorage.getItem("fit-agent:chat-client")||"null")';
const ACTIVE_ID = `(() => { const s = ${RAW}; return s ? (s.draft ? s.draft.session_id : s.selected_session_id) : null; })()`;
const LEDGER = `(() => { const s = ${RAW}; const id = ${ACTIVE_ID}; return id && s.ledgers ? (s.ledgers[id] || []) : []; })()`;
/* 客户端持久化状态 → 检查用视图（当前会话 id、创建状态、当前账本、全部账本） */
const store = () => evaluate(`(() => { const s = ${RAW}; const id = ${ACTIVE_ID}; return { session_id: id, session_created: !!(s && s.draft === null && s.selected_session_id !== null), operations: ${LEDGER}, ledgers: (s && s.ledgers) || {} }; })()`);
const message = "textarea[aria-label='消息']";
const transcriptText = "document.querySelector('[data-slot=message-scroller-content]').innerText";
const stop = "button[aria-label='停止']";
const sendButton = "button[aria-label='发送']";
// 真实工具卡片提供可见调用详情，后续长输出保持运行中窗口用于刷新、steering 与竞争断言。
const toolCard = "button[aria-label='calculate_date 工具调用详情']";
const toolCount = `document.querySelectorAll("${toolCard}").length`;
const toolRequest = (reply) =>
  `必须调用 calculate_date 传 days_offset=-1，然后逐行输出从 1 到 100000 的整数，不要省略，最后一行只写 ${reply}。`;
const send = (text) => { browser("fill", message, text); browser("press", "Enter"); };
async function until(expression, timeout = 90000, label = expression) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (evaluate(expression)) return;
    await setTimeout(250);
  }
  assert.fail(`浏览器条件超时：${label}\n${JSON.stringify(browser("snapshot")).slice(0, 1500)}`);
}

const report = { steps: [], blockers: [] };
const note = (text) => { report.steps.push(text); console.log("STEP:", text); };

try {
  browser("open", "http://localhost:5173");
  await until("!!document.querySelector('textarea')", 30000, "页面加载");
  evaluate("localStorage.clear();true");
  browser("reload");
  await until("!!document.querySelector('textarea')", 30000, "清空存储后加载");

  // 1. 新建会话：身份变化、独立空白、账本隔离
  send("只回复 IDENTITY_ONE，不调用工具。");
  await until(`!document.querySelector(${JSON.stringify(stop)}) && ${transcriptText}.includes('IDENTITY_ONE')`, 90000, "首条发送完成");
  await until(`(${RAW}).draft === null && (${RAW}).selected_session_id !== null`, 15000, "会话创建确认");
  const beforeNew = await store();
  assert.equal(beforeNew.operations.length, 0, "受理完成后当前会话账本应为空");
  browser("find", "role", "button", "click", "--name", "新建会话");
  await setTimeout(500);
  const afterNew = await store();
  assert.notEqual(afterNew.session_id, beforeNew.session_id, "新建会话必须更换 session_id");
  assert.equal(afterNew.session_created, false, "新会话未创建");
  assert.equal(afterNew.operations.length, 0, "新会话账本为空");
  assert.equal(await evaluate(transcriptText), "", "新会话展示独立空白聊天");
  await setTimeout(1500);
  assert.equal((await store()).session_id, afterNew.session_id, "旧请求回调不得覆盖新会话状态");
  report.newSession = { before: beforeNew.session_id, after: afterNew.session_id, changed: true };
  note("新建会话身份变化并保持稳定");

  // 2. 运行中刷新：保持占用，终态后释放
  send(toolRequest("REFRESH_DONE"));
  await until(`!!document.querySelector(${JSON.stringify(stop)})`, 60000, "运行开始");
  await (async () => {
    for (let index = 0; index < 120; index += 1) {
      const current = await store();
      const operation = current.operations.find((item) => item.kind === "send" && item.run_id);
      if (operation) return operation;
      await setTimeout(250);
    }
    assert.fail("运行未受理");
  })();
  const persistedRun = (await store()).operations.find((item) => item.kind === "send");
  assert.ok(persistedRun.run_id, "运行身份已持久化");
  report.refreshRunId = persistedRun.run_id;
  await until(`!!document.querySelector("${toolCard}")`, 60000, "工具执行中");
  browser("reload");
  await until("!!document.querySelector('textarea')", 30000, "刷新后加载");
  assert.equal((await evaluate(transcriptText)).includes("必须调用 calculate_date"), true, "恢复原用户请求");
  browser("fill", message, "占用探测");
  assert.equal(await evaluate(`document.querySelector(${JSON.stringify(sendButton)}).disabled`), true, "恢复运行期间保持占用");
  browser("fill", message, "");
  await until(`(${LEDGER}).length === 0`, 60000, "恢复运行进入终态");
  send("只回复 AFTER_REFRESH，不调用工具。");
  await until(`!document.querySelector(${JSON.stringify(stop)}) && ${transcriptText}.includes('AFTER_REFRESH')`, 90000, "终态后可再次发送");
  note("运行中刷新恢复并保持占用");

  // 3. Steering 撤回（pending 转 withdrawn 并清理操作）
  const toolBeforeSteer = evaluate(toolCount);
  send(toolRequest("STEER_RUN"));
  await until(`!!document.querySelector(${JSON.stringify(stop)})`, 60000, "Steering 运行开始");
  await until(`(${LEDGER}).some((item) => item.kind === 'send' && item.run_id)`, 60000, "Steering 运行受理");
  await until(`${toolCount} > ${toolBeforeSteer}`, 60000, "工具执行中");
  send("STEER_WITHDRAW_ONLY。");
  await until(`!!document.querySelector("button[aria-label='撤回']")`, 30000, "出现撤回按钮");
  const steerStore = await store();
  assert.equal(steerStore.operations.some((item) => item.kind === "steering" && item.steering_id), true, "已受理 Steering 持久化身份");
  browser("find", "role", "button", "click", "--name", "撤回");
  await until(`!document.querySelector("button[aria-label='撤回']")`, 30000, "撤回后按钮消失");
  await until(`(${LEDGER}).every((item) => item.kind !== 'steering')`, 30000, "撤回终态清理操作");
  assert.equal((await evaluate(transcriptText)).includes("STEER_WITHDRAW_ONLY"), true, "撤回后保留输入原文");
  browser("click", stop);
  await until(`!document.querySelector(${JSON.stringify(stop)})`, 60000, "停止收尾");
  await until(`(${LEDGER}).length === 0`, 60000, "停止收尾进入终态");
  report.steeringWithdrawn = true;
  note("Steering 撤回终态清理");

  // 4. 原键重试：查询失败不重发，未受理沿用原键重发
  browser("set", "offline", "on");
  send("离线未知请求 RETRY_KEY，不调用工具。");
  await until(`${transcriptText}.includes('结果未知')`, 30000, "出现结果未知");
  const unknownStore = await store();
  const pending = unknownStore.operations.find((item) => item.kind === "send");
  assert.ok(pending, "未确认操作保留");
  report.retryOperationId = pending.operation_id;
  browser("find", "role", "button", "click", "--name", "重试");
  await setTimeout(1500);
  const retriedOffline = await store();
  assert.equal(retriedOffline.operations.filter((item) => item.kind === "send").length, 1, "查询失败不重发");
  assert.equal(retriedOffline.operations[0].operation_id, report.retryOperationId, "查询失败沿用原 operation_id");
  browser("set", "offline", "off");
  browser("find", "role", "button", "click", "--name", "重试");
  await until(`!document.querySelector(${JSON.stringify(stop)}) && (${LEDGER}).length === 0`, 120000, "原键重发完成");
  note("原键重试（查询优先）");

  // 5. SSE consumed 先到、Steering JSON 响应后到：只推迟真实响应的到达顺序
  const toolBefore = evaluate(toolCount);
  send(toolRequest("RACE_RUN"));
  await until(`(${LEDGER}).some((item) => item.kind === 'send' && item.run_id)`, 60000, "竞争运行已受理");
  await until(`${toolCount} > ${toolBefore}`, 90000, "竞争工具执行中");
  evaluate(`(() => {
    const original = window.fetch;
    window.__steerGate = { held: false, release: null };
    window.fetch = async (...args) => {
      const response = await original(...args);
      const target = typeof args[0] === 'string' ? args[0] : args[0].url;
      const method = ((args[1] && args[1].method) || 'GET').toUpperCase();
      if (method === 'POST' && target.includes('/steering') && !target.includes('/withdraw')) {
        window.__steerGate.held = true;
        await new Promise((resolve) => { window.__steerGate.release = resolve; });
      }
      return response;
    };
    return true;
  })()`);
  send("STEER_RACE_ONLY。");
  await until(`(${LEDGER}).some((item) => item.kind === 'steering')`, 30000, "Steering 操作已登记");
  await until("!!window.__steerGate.held", 30000, "Steering JSON 已被暂缓");
  const raceStore = await store();
  const raceOperation = raceStore.operations.find((item) => item.kind === "steering");
  assert.ok(raceOperation, "Steering 操作已登记");
  let raceStatus = null;
  for (let index = 0; index < 120; index += 1) {
    const polled = await (await fetch(`http://127.0.0.1:8000/api/sessions/${raceStore.session_id}/operations/${raceOperation.operation_id}`, { headers: { "Content-Type": "application/json" } })).json();
    raceStatus = polled.steering?.status ?? null;
    if (raceStatus === "consumed") break;
    await setTimeout(250);
  }
  assert.equal(raceStatus, "consumed", "后端已确认消费");
  await setTimeout(600);
  evaluate("window.__steerGate.release && window.__steerGate.release(); true");
  await until(`(${LEDGER}).every((item) => item.kind !== 'steering')`, 30000, "后到 JSON 合并并清理操作");
  assert.equal(await evaluate("[...document.querySelectorAll('[data-slot=message-scroller-item]')].filter((el) => el.innerText === 'STEER_RACE_ONLY。').length"), 1, "输入只展示一次");
  assert.equal(await evaluate(`!!document.querySelector("button[aria-label='撤回']")`), false, "状态保持 consumed");
  assert.equal(await evaluate("[...document.querySelectorAll('[role=alert]')].filter((el) => /协议|不匹配|标识冲突|重复|无效/.test(el.textContent)).length"), 0, "无协议异常");
  report.steeringRace = { status: raceStatus, shownOnce: true };
  note("SSE consumed 先到、JSON 后到：幂等合并");
  if (evaluate(`!!document.querySelector(${JSON.stringify(stop)})`)) browser("click", stop);
  await until(`(${LEDGER}).length === 0`, 90000, "竞争运行终态");

  // 6. 旧会话未确认请求归档保留，不得直接删除
  browser("set", "offline", "on");
  send("待确认归档 RETENTION_KEY，不调用工具。");
  await until(`${transcriptText}.includes('结果未知')`, 30000, "归档前出现结果未知");
  const beforeArchive = await store();
  assert.equal(beforeArchive.operations.some((item) => item.kind === "send"), true, "未确认操作存在");
  browser("set", "offline", "off");
  browser("find", "role", "button", "click", "--name", "新建会话");
  await setTimeout(600);
  const archived = evaluate(`${RAW}.ledgers`);
  assert.ok(archived && Array.isArray(archived[beforeArchive.session_id]), "旧会话未确认账本保留");
  assert.equal(archived[beforeArchive.session_id].length, 1, "账本保留未确认操作");
  const archivedNew = await store();
  assert.notEqual(archivedNew.session_id, beforeArchive.session_id, "归档后启用新会话身份");
  assert.equal(archivedNew.operations.length, 0);
  report.archivedRetained = true;
  note("旧会话未确认请求归档保留");

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
