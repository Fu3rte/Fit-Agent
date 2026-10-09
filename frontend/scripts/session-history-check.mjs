// 运行：node scripts/session-history-check.mjs
// 前置：后端（新会话历史接口）与 vite dev server 已在 http://127.0.0.1:5173 可用。
// 覆盖：首页草稿、创建后 URL 切换、侧栏会话列表、刷新/直接 URL 历史恢复、运行中恢复、快速切换。
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { closeSync, mkdirSync, openSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout } from "node:timers/promises";

const root = path.resolve(import.meta.dirname, "../temp/session-history-check");
mkdirSync(root, { recursive: true });
const executable = path.join(process.env.APPDATA, "npm/node_modules/agent-browser/bin/agent-browser-win32-x64.exe");

function browser(...args) {
  const outPath = path.join(root, "command.json");
  const errPath = path.join(root, "command-error.log");
  const out = openSync(outPath, "w");
  const err = openSync(errPath, "w");
  let result;
  try {
    result = spawnSync(executable, ["--session", "fit-session-history", "--profile", path.join(root, "profile"), "--json", ...args], { stdio: ["ignore", out, err], timeout: 60000 });
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
const transcriptText = "document.querySelector('[data-slot=message-scroller-content]').innerText";
const stop = "button[aria-label='停止']";
const sendButton = "button[aria-label='发送']";
const send = (text) => { browser("fill", message, text); browser("press", "Enter"); };
const RAW = 'JSON.parse(localStorage.getItem("fit-agent:chat-client")||"null")';
const ACTIVE_ID = `(() => { const s = ${RAW}; return s ? (s.draft ? s.draft.session_id : s.selected_session_id) : null; })()`;
const LEDGER = `(() => { const s = ${RAW}; const id = ${ACTIVE_ID}; return id && s.ledgers ? (s.ledgers[id] || []) : []; })()`;
async function until(expression, timeout = 90000, label = expression) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (evaluate(expression)) return;
    await setTimeout(200);
  }
  assert.fail(`浏览器条件超时：${label}\n${JSON.stringify(browser("snapshot")).slice(0, 1500)}`);
}

const report = { steps: [], blockers: [] };
const note = (text) => { report.steps.push(text); console.log("STEP:", text); };

try {
  // 1. 首页草稿：无选择时进入空白会话流程，localStorage 保留草稿 UUID
  browser("open", "http://127.0.0.1:5173/");
  await until("!!document.querySelector('textarea')", 30000, "页面加载");
  evaluate("localStorage.clear();true");
  browser("reload");
  await until("!!document.querySelector('textarea')", 30000, "清空存储后加载");
  assert.equal(await evaluate("location.pathname"), "/", "无选择时停留在首页");
  assert.equal(await evaluate(transcriptText), "", "草稿展示空白");
  const draftId = await evaluate(ACTIVE_ID);
  assert.ok(draftId, "首页保留本地草稿 UUID");
  note("首页草稿空白且保留 UUID");

  // 2. 创建后打开会话 URL；侧栏出现该会话
  send("只回复 HISTORY_ONE，不调用工具。");
  await until(`!document.querySelector(${JSON.stringify(stop)}) && ${transcriptText}.includes('HISTORY_ONE')`, 90000, "首条发送完成");
  await until(`location.pathname.startsWith('/sessions/')`, 15000, "创建后跳转会话 URL");
  const firstId = await evaluate("location.pathname.split('/').pop()");
  assert.equal(firstId, draftId, "创建沿用草稿 UUID 打开会话 URL");
  await until(`[...document.querySelectorAll('a')].some((el) => el.getAttribute('href') === '/sessions/${firstId}')`, 15000, "侧栏出现该会话");
  note("创建后 URL 切换且侧栏出现会话");

  // 3. 刷新恢复：历史接口重建展示
  browser("reload");
  await until("!!document.querySelector('textarea')", 30000, "刷新后加载");
  await until(`${transcriptText}.includes('HISTORY_ONE')`, 30000, "刷新后恢复用户请求");
  await until(`${transcriptText}.includes('HISTORY_ONE') && !document.querySelector(${JSON.stringify(stop)})`, 30000, "刷新后收敛");
  assert.equal(await evaluate("location.pathname"), `/sessions/${firstId}`, "刷新保持 URL 身份");
  note("刷新恢复历史展示");

  // 4. 直接 URL：新开一个会话后回访旧 URL 恢复其历史
  browser("find", "role", "button", "click", "--name", "新建会话");
  await until(`location.pathname === "/"`, 15000, "回到首页草稿");
  send("只回复 HISTORY_TWO，不调用工具。");
  await until(`!document.querySelector(${JSON.stringify(stop)}) && ${transcriptText}.includes('HISTORY_TWO')`, 90000, "第二条发送完成");
  const secondId = await evaluate("location.pathname.split('/').pop()");
  assert.notEqual(secondId, firstId, "新建会话使用新 UUID");
  await until(`[...document.querySelectorAll('a')].filter((el) => el.getAttribute('href')?.startsWith('/sessions/')).length === 2`, 15000, "侧栏出现两条会话");
  browser("open", `http://127.0.0.1:5173/sessions/${firstId}`);
  await until("!!document.querySelector('textarea')", 30000, "直接 URL 加载");
  await until(`${transcriptText}.includes('HISTORY_ONE') && !${transcriptText}.includes('HISTORY_TWO')`, 30000, "直接 URL 恢复指定会话历史");
  note("直接 URL 恢复指定会话");

  // 5. 快速切换：旧会话响应不得覆盖当前选择
  browser("open", `http://127.0.0.1:5173/sessions/${secondId}`);
  await until("!!document.querySelector('textarea')", 30000, "切换加载");
  await until(`${transcriptText}.includes('HISTORY_TWO') && !${transcriptText}.includes('HISTORY_ONE')`, 30000, "切换后展示目标会话");
  assert.equal(await evaluate("location.pathname"), `/sessions/${secondId}`, "切换后 URL 与展示一致");
  note("快速切换展示与 URL 一致");

  // 6. 运行中刷新恢复：保持占用，终态后经历史刷新释放
  send("必须调用 calculate_date 传 days_offset=-1，然后逐行输出从 1 到 100000 的整数，不要省略，最后一行只写 HISTORY_REFRESH。");
  await until(`!!document.querySelector(${JSON.stringify(stop)})`, 60000, "运行开始");
  await until(`(${LEDGER}).some((item) => item.kind === 'send' && item.run_id)`, 60000, "运行受理并持久化");
  await until(`!!document.querySelector("button[aria-label='calculate_date 工具调用详情']")`, 60000, "工具执行中");
  browser("reload");
  await until("!!document.querySelector('textarea')", 30000, "运行中刷新加载");
  browser("fill", message, "占用探测");
  assert.equal(await evaluate(`document.querySelector(${JSON.stringify(sendButton)}).disabled`), true, "恢复运行期间保持占用");
  browser("fill", message, "");
  await until(`(${LEDGER}).length === 0`, 90000, "恢复运行进入终态");
  await until(`${transcriptText}.includes('HISTORY_REFRESH')`, 30000, "恢复后历史含助手回复");
  note("运行中刷新恢复并释放占用");

  // 7. 首页恢复上次选择
  browser("open", "http://127.0.0.1:5173/");
  await until(`location.pathname === '/sessions/${secondId}'`, 15000, "首页恢复上次选择");
  assert.equal(await evaluate(`${transcriptText}.includes('HISTORY_TWO')`), true, "恢复会话展示其历史");
  note("首页恢复上次选择");

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
