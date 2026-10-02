import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { closeSync, mkdirSync, openSync, readFileSync } from "node:fs";
import path from "node:path";
import { setTimeout } from "node:timers/promises";

const executable = path.join(process.env.APPDATA, "npm/node_modules/agent-browser/bin/agent-browser-win32-x64.exe");
const profile = path.resolve(import.meta.dirname, "../../tmp/frontend-chat-contract-browser");
mkdirSync(profile, { recursive: true });
function browser(...args) {
  const outputPath = path.join(profile, "command.json");
  const errorPath = path.join(profile, "command-error.log");
  const output = openSync(outputPath, "w");
  const error = openSync(errorPath, "w");
  let result;
  try {
    result = spawnSync(executable, ["--session", "fit-chat-contract", "--profile", profile, "--json", ...args], { stdio: ["ignore", output, error], timeout: 30000 });
  } finally { closeSync(output); closeSync(error); }
  const content = readFileSync(outputPath, "utf8");
  assert.equal(result.status, 0, readFileSync(errorPath, "utf8") || content);
  const reply = JSON.parse(content);
  assert.equal(reply.success, true, content);
  return reply.data;
}
const evaluate = (expression) => browser("eval", expression).result;
async function until(expression) {
  const deadline = Date.now() + 60000;
  while (Date.now() < deadline) {
    if (evaluate(expression)) return;
    await setTimeout(200);
  }
  assert.fail(`浏览器条件超时：${expression}\n${JSON.stringify(browser("snapshot"))}`);
}
const message = "textarea[aria-label='消息']";
const transcriptText = "document.querySelector('[data-slot=message-scroller-content]').innerText";
const stop = "button[aria-label='停止']";
function send(text) { browser("fill", message, text); browser("press", "Enter"); }

try {
  browser("tab", "list");
  browser("open", "http://127.0.0.1:5173");
  await until("!!document.querySelector('textarea')");
  browser("fill", message, "输入法测试");
  evaluate("document.querySelector('textarea').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,isComposing:true}));true");
  assert.equal(evaluate("document.querySelector('textarea').value"), "输入法测试");
  assert.equal(evaluate(`${transcriptText} === ''`), true);
  browser("press", "Shift+Enter");
  assert.match(evaluate("document.querySelector('textarea').value"), /\n/);

  send("必须调用 bash，command 精确为 sleep 5，timeout 为 10；完成后只回复 FIRST_BROWSER。");
  await until("!!document.querySelector(\"button[aria-label='bash 工具调用详情']\")");
  assert.equal(evaluate("document.querySelector('textarea').disabled"), false);
  assert.equal(evaluate(`!!document.querySelector(${JSON.stringify(stop)})`), true);
  send("只回复 STEER_BROWSER。");
  await until("document.querySelector('textarea').value === ''");
  await until(`!document.querySelector(${JSON.stringify(stop)})`);
  assert.equal(evaluate(`${transcriptText}.includes('STEER_BROWSER')`), true);
  assert.equal(evaluate("document.querySelectorAll('[role=alert]').length"), 0);
  assert.equal(evaluate("[...document.querySelectorAll('[data-slot=message-scroller-item]')].filter(el=>el.innerText==='只回复 STEER_BROWSER。').length"), 1);
  browser("click", "button[aria-label='bash 工具调用详情']");
  assert.equal(evaluate(`${transcriptText}.includes('sleep 5')`), true);

  send("必须调用 bash，command 精确为 sleep 20，timeout 为 30；结束后只回复 STOP_BROWSER。");
  await until("document.querySelectorAll(\"button[aria-label='bash 工具调用详情']\").length === 2");
  browser("fill", message, "保留的后续输入");
  browser("click", stop);
  assert.equal(evaluate("document.querySelector('textarea').value"), "保留的后续输入");
  assert.equal(evaluate(`${transcriptText}.includes('已中断')`), true);
  assert.equal(evaluate(`!!document.querySelector(${JSON.stringify(stop)})`), false);
  browser("focus", message);
  browser("press", "Enter");
  await until("!!document.querySelector('[role=alert]')");
  assert.equal(evaluate("document.querySelector('textarea').value"), "保留的后续输入");
  assert.equal(evaluate(`${transcriptText}.includes('已有任务正在执行')`), true);
  await setTimeout(21000);

  browser("find", "role", "button", "click", "--name", "新建会话");
  assert.equal(evaluate(`${transcriptText} === ''`), true);
  assert.equal(evaluate("document.querySelector('textarea').value"), "");
  send("只回复 NEW_BROWSER。");
  await until(`!document.querySelector(${JSON.stringify(stop)}) && ${transcriptText}.includes('NEW_BROWSER')`);
  assert.equal(evaluate("document.querySelectorAll('[role=alert]').length"), 0, evaluate(transcriptText));
  const previousTools = evaluate("document.querySelectorAll(\"button[aria-label='bash 工具调用详情']\").length");
  send("必须调用 bash，command 精确为 sleep 10，timeout 为 20；完成后只回复 UNKNOWN_BROWSER。");
  await until(`document.querySelectorAll("button[aria-label='bash 工具调用详情']").length > ${previousTools}`);
  browser("set", "offline", "on");
  send("未知的 steering 内容");
  await until(`${transcriptText}.includes('Steering 提交结果未知。')`);
  assert.equal(evaluate("document.querySelector('textarea').value"), "未知的 steering 内容");
  assert.equal(evaluate("document.querySelector(\"button[aria-label='发送']\").disabled"), true);
  browser("press", "Enter");
  assert.equal(evaluate("document.querySelector('textarea').value"), "未知的 steering 内容");
  browser("set", "offline", "off");
  browser("click", stop);
  assert.equal(evaluate(`${transcriptText}.includes('Steering 提交结果未知。')`), true);
  browser("find", "role", "button", "click", "--name", "新建会话");
  assert.equal(evaluate(`${transcriptText} === ''`), true);
  browser("reload");
  await until("!!document.querySelector('textarea')");
  assert.equal(evaluate(`${transcriptText} === ''`), true);
  const errors = browser("errors");
  assert.deepEqual(errors.errors, []);
  console.log("PASS: real browser steering, tool order/details, editable input, IME/Shift+Enter, stop, conflict/unknown steering input retention, duplicate prevention, new session and refresh");
} finally { browser("close"); }
