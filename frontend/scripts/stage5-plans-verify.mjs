import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const api = await import("../src/lib/api.ts");

const encoder = new TextEncoder();
const CONVERSATION_ID = "6f1c0b7e-2a4d-4f0c-9d13-7a1f6c2b8e55";
const PLAN = {
  id: 7,
  version: 2,
  status: "active",
  source_plan_id: null,
  structured_content: { goal: "深蹲为主" },
  evaluator_result: null,
  created_at: "2026-06-01T09:00:00+00:00",
  confirmed_at: "2026-06-01T10:00:00+00:00",
  archived_at: null,
};

/** 桩 fetch：记录每次调用的路径／方法／解析后的请求体，按用例注入响应 */
const calls = [];
let respond = () => {
  throw new Error("用例未注入响应");
};
globalThis.fetch = async (path, init) => {
  calls.push({
    path,
    init,
    body: init?.body === undefined ? undefined : JSON.parse(init.body),
  });
  return respond(path, init);
};

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** 把字节块按给定顺序推入流；块边界即 fetch 读到的 chunk 边界 */
function streamResponse(chunks) {
  return new Response(
    new ReadableStream({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(chunk);
        controller.close();
      },
    }),
    { status: 200, headers: { "Content-Type": "text/event-stream" } },
  );
}

const passed = [];
function pass(name) {
  passed.push(name);
}

/* --- REAL 1：/api/agent/run 的请求形状 + 五类中的 node／message／waiting／done 消费 --- */

const payload =
  'event: node\ndata: {"name":"safety_check"}\n\n' +
  'event: message\ndata: {"text":"中文提示：安全"}\n\n' +
  'event: waiting\ndata: {"draft_plan_id": 7}\n\n' +
  'event: done\ndata: {"ok":true,"intent":"generate_plan","termination_reason":null,"draft_plan_id":7}';
const bytes = encoder.encode(payload);
// 切点落在「安」的 UTF-8 三字节序列中间：验证多字节字符被 chunk 切开仍能正确解码
const cut =
  encoder.encode(
    'event: node\ndata: {"name":"safety_check"}\n\n' +
      'event: message\ndata: {"text":"中文提示：',
  ).length + 1;
assert.equal(bytes[cut - 1], 0xe5, "切点必须落在「安」（E5 AE 89）的第一个字节内");
respond = () => streamResponse([bytes.slice(0, cut), bytes.slice(cut)]);

const events = [];
await api.runAgentStream(
  { conversation_id: CONVERSATION_ID, request: "生成计划" },
  (event) => events.push(event),
);
assert.equal(calls.length, 1);
assert.equal(calls[0].path, "/api/agent/run");
assert.equal(calls[0].init.method, "POST");
assert.equal(calls[0].init.headers["Content-Type"], "application/json");
assert.deepEqual(calls[0].body, {
  conversation_id: CONVERSATION_ID,
  request: "生成计划",
});
// 第一个 chunk 含一帧 + 半帧，第二个 chunk 含剩余 + 两帧且没有结尾空行
assert.deepEqual(events, [
  { event: "node", data: { name: "safety_check" } },
  { event: "message", data: { text: "中文提示：安全" } },
  { event: "waiting", data: { draft_plan_id: 7 } },
  {
    event: "done",
    data: {
      ok: true,
      intent: "generate_plan",
      termination_reason: null,
      draft_plan_id: 7,
    },
  },
]);
pass("REAL /api/agent/run 请求形状 + node／message／waiting／done（跨 chunk、多字节、无结尾空行）");

/* --- REAL 2：CRLF 行终止符与空行；切点落在 CRLF 对之间 --- */

calls.length = 0;
// 两帧全用 CRLF 终止，且 chunk 边界刚好落在帧间 CRLF 对的\r 与\n 之间
const crlfNode = 'event: node\r\ndata: {"name":"planner"}\r\n\r\n\r';
const crlfDone =
  '\nevent: done\r\ndata: {"ok":true,"intent":null,"termination_reason":"safety_stop","draft_plan_id":null}\r\n\r\n';
respond = () =>
  streamResponse([encoder.encode(crlfNode), encoder.encode(crlfDone)]);
const crlfEvents = [];
await api.runAgentStream(
  { conversation_id: CONVERSATION_ID, request: "查看进步" },
  (event) => crlfEvents.push(event),
);
assert.deepEqual(crlfEvents, [
  { event: "node", data: { name: "planner" } },
  {
    event: "done",
    data: {
      ok: true,
      intent: null,
      termination_reason: "safety_stop",
      draft_plan_id: null,
    },
  },
]);
pass("REAL CRLF 行终止符（含跨 chunk 的 CRLF 对）与空行不产生多余事件");

/* --- REAL 2b：chunk 切点落在帧内 CRLF 对之间，不得被误判成帧分隔的空行 --- */

calls.length = 0;
respond = () =>
  streamResponse([
    encoder.encode('event: node\r'),
    encoder.encode('\ndata: {"name":"planner"}\r\n\r\n'),
  ]);
const splitTerminatorEvents = [];
await api.runAgentStream(
  { conversation_id: CONVERSATION_ID, request: "生成计划" },
  (event) => splitTerminatorEvents.push(event),
);
assert.deepEqual(splitTerminatorEvents, [
  { event: "node", data: { name: "planner" } },
]);
pass("REAL 切点在帧内 CRLF 对之间不被当成空行（缓冲区保留原始字节）");

/* --- REAL 2c：LF／CRLF 混型的行终止符与空行（旧的正则边界会把两帧拼成一帧） --- */

calls.length = 0;
respond = () =>
  streamResponse([
    encoder.encode('event: node\ndata: {"name":"planner"}\n\r'),
    encoder.encode(
      '\nevent: message\r\ndata: {"text":"混型终止符"}\r\n\n' +
        'event: done\ndata: {"ok":true,"intent":"generate_plan","termination_reason":null,"draft_plan_id":null}',
    ),
  ]);
const mixedTerminatorEvents = [];
await api.runAgentStream(
  { conversation_id: CONVERSATION_ID, request: "生成计划" },
  (event) => mixedTerminatorEvents.push(event),
);
assert.deepEqual(mixedTerminatorEvents, [
  { event: "node", data: { name: "planner" } },
  { event: "message", data: { text: "混型终止符" } },
  {
    event: "done",
    data: {
      ok: true,
      intent: "generate_plan",
      termination_reason: null,
      draft_plan_id: null,
    },
  },
]);
pass("REAL LF／CRLF 混型的行终止符与空行逐帧切分正确");

/* --- REAL 3：流内 error 先回调，再抛错（五类中的 error） --- */

calls.length = 0;
const errorText = "模型调用失败：本次运行未产生计划写入，请稍后重试";
respond = () =>
  streamResponse([
    encoder.encode(
      'event: node\ndata: {"name":"planner"}\n\n' +
        `event: error\ndata: {"message":"${errorText}"}\n\n`,
    ),
  ]);
const errorEvents = [];
let streamError = null;
try {
  await api.runAgentStream(
    { conversation_id: CONVERSATION_ID, request: "生成计划" },
    (event) => errorEvents.push(event),
  );
} catch (error) {
  streamError = error;
}
assert.equal(streamError?.message, errorText);
assert.deepEqual(errorEvents, [
  { event: "node", data: { name: "planner" } },
  { event: "error", data: { message: errorText } },
]);
pass("REAL 流内 error 既交给回调又抛错（不把 error 当成功）");

/* --- REAL 4：未知事件名立即抛错，不猜默认值 --- */

calls.length = 0;
respond = () =>
  streamResponse([encoder.encode('event: progress\ndata: {"name":"x"}\n\n')]);
let unknownError = null;
try {
  await api.runAgentStream(
    { conversation_id: CONVERSATION_ID, request: "生成计划" },
    () => {},
  );
} catch (error) {
  unknownError = error;
}
assert.match(unknownError?.message ?? "", /未知的 SSE 事件名/);
pass("REAL 未知 SSE 事件名立即抛错");

/* --- REAL 5：流建立前的非 2xx 复用既有 JSON 错误形状 --- */

calls.length = 0;
respond = () =>
  jsonResponse(
    {
      http_status: 400,
      error_code: "invalid_request",
      message: "请求体不是接口约定的 JSON 形状",
    },
    400,
  );
let shapeError = null;
try {
  await api.runAgentStream(
    { conversation_id: "not-a-uuid", request: "生成计划" },
    () => {},
  );
} catch (error) {
  shapeError = error;
}
assert.equal(shapeError?.http_status, 400);
assert.equal(shapeError?.error_code, "invalid_request");
assert.equal(shapeError?.message, "请求体不是接口约定的 JSON 形状");
pass("REAL /api/agent/run 非 2xx（UUID／形状非法）为既有 JSON 错误形状");

/* --- REAL 6：confirm／reject 端点与 409 领域错误 --- */

calls.length = 0;
respond = () => jsonResponse({ plan: PLAN });
const confirmed = await api.confirmPlan({
  conversation_id: CONVERSATION_ID,
  plan_id: 7,
});
assert.equal(calls[0].path, "/api/agent/confirm");
assert.equal(calls[0].init.method, "POST");
assert.deepEqual(calls[0].body, {
  conversation_id: CONVERSATION_ID,
  plan_id: 7,
});
assert.deepEqual(confirmed, { plan: PLAN });

calls.length = 0;
respond = () => jsonResponse({ plan: { ...PLAN, status: "archived" } });
const rejected = await api.rejectPlan({
  conversation_id: CONVERSATION_ID,
  plan_id: 7,
});
assert.equal(calls[0].path, "/api/agent/reject");
assert.equal(calls[0].init.method, "POST");
assert.deepEqual(calls[0].body, {
  conversation_id: CONVERSATION_ID,
  plan_id: 7,
});
assert.equal(rejected.plan.status, "archived");

calls.length = 0;
respond = () =>
  jsonResponse(
    {
      http_status: 409,
      error_code: "invalid_request",
      message: "请求的 plan_id 与等待确认的 draft 不一致：7",
    },
    409,
  );
let conflictError = null;
try {
  await api.confirmPlan({ conversation_id: CONVERSATION_ID, plan_id: 7 });
} catch (error) {
  conflictError = error;
}
assert.equal(conflictError?.http_status, 409);
assert.equal(conflictError?.error_code, "invalid_request");
pass("REAL confirm／reject 端点、响应形状与 409 领域错误");

/* --- STATIC 7：页面源码断言（React 组件无法在无 DOM 的 Node 里渲染） --- */

const plansPage = await readFile(
  new URL("../src/features/plans/PlansPage.tsx", import.meta.url),
  "utf8",
);
const app = await readFile(
  new URL("../src/app/App.tsx", import.meta.url),
  "utf8",
);

// STATIC 计划页不再有任何计划写入入口（生成／调整与确认／拒绝都已移至对话页）
for (const forbidden of ["confirmPlan", "rejectPlan", "runAgentStream", "Textarea"]) {
  assert.ok(
    !plansPage.includes(forbidden),
    `计划页不得再有计划写入入口：${forbidden}`,
  );
}
// STATIC 路由与导航
assert.match(app, /<Route path="\/plans" element=\{<PlansPage \/>\} \/>/);
assert.match(app, /\{ to: "\/plans", label: "训练计划"/);
// STATIC 唯一 draft 取自计划版本列表的 draft 状态（后端单 draft 索引保证至多一条）
assert.match(
  plansPage,
  /plans\.data\?\.plans\.find\(\(plan\) => plan\.status === "draft"\)/,
  "计划页必须从计划版本列表按 draft 状态取唯一 draft",
);
pass("STATIC 计划页无计划写入入口、路由注册、唯一 draft 取自版本列表");

/* --- STATIC 8：契约与后端字段一致（五类事件名与 data 键） --- */

const contract = await readFile(
  new URL("../src/lib/contract.ts", import.meta.url),
  "utf8",
);
assert.match(
  contract,
  /export type AgentEventNameWire =\s*"node" \| "message" \| "waiting" \| "done" \| "error";/,
);
assert.match(
  contract,
  /data: \{ name: string \};[\s\S]*?data: \{ text: string \};[\s\S]*?data: \{ draft_plan_id: number \};/,
);
assert.match(
  contract,
  /ok: true;[\s\S]*?intent: string \| null;[\s\S]*?termination_reason: string \| null;[\s\S]*?draft_plan_id: number \| null;/,
);
pass("STATIC 五类事件名与 data 键同后端一致（done 四个键、intent 可为 null）");

console.log(`通过 ${passed.length} 组断言：`);
for (const name of passed) console.log(`  - ${name}`);
