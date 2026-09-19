/**
 * Stage 6 前端可执行验证（Node 原生，无第三方依赖、无测试框架）：T2.3 契约 ＋ T2.4 对话页。
 *
 * 两类断言，脚本内逐条标注：
 *
 * - **真实调用**（REAL）：直接 ``import`` ``../src/lib/api.ts`` 与
 *   ``../src/features/chat/utils/workoutDraft.ts``（Node 24 原生 TS type stripping；两者对 ``@/lib/contract`` 的导入
 *   都是 type-only，会被剥离），用桩 ``fetch`` 驱动 ``confirmWorkout`` 的 URL／方法／请求体逐字段与响应形状、
 *   错误形状，``runAgentStream`` 对 ``waiting`` 双路径载荷的解析，以及对话页打卡载荷的三种日程关联形状。
 * - **静态断言**（STATIC）：``ChatPage.tsx``／``PlansPage.tsx``／``App.tsx`` 是 React 组件，无 DOM 的 Node
 *   不能渲染，因此用源码文本断言；``contract.ts``／``api.ts`` 的字段逐字一致同时与后端
 *   ``dto.py``／``workflow.py``／``routes_agent.py`` 源码交叉核对。
 *
 * 断言失败即进程非零退出；不使用 console 打印代替断言。
 */
import assert from "node:assert/strict";
import { readFile as readFileRaw } from "node:fs/promises";

/** 源码断言按 LF 口径比对；``core.autocrlf`` 检出的 CRLF 工作树在此归一化 */
async function readFile(url) {
  return (await readFileRaw(url, "utf8")).replace(/\r\n/g, "\n");
}

const api = await import("../src/lib/api.ts");

const encoder = new TextEncoder();
const CONVERSATION_ID = "6a1c0f3e-2b47-4d90-8e5a-0c3f7b1d9a24";

/** ``waiting.workout``：后端 ``_workout_payload`` 的载荷（stage6.md §2.4.3），确认 UI 的编辑数据源 */
const WAITING_WORKOUT = {
  performed_on: "2026-09-18",
  sets: [
    {
      exercise_id: "barbell-bench-press",
      set_no: 1,
      set_type: "work",
      load_convention: "barbell_includes_bar_total",
      weight_kg: 60,
      reps: 5,
      duration_seconds: null,
    },
    {
      exercise_id: "plank",
      set_no: 1,
      set_type: "work",
      load_convention: null,
      weight_kg: null,
      reps: null,
      duration_seconds: 60,
    },
  ],
  plan_session_id: null,
  auto_link: true,
};

/** ``waiting.candidate_plan_sessions``：数据库候选日程（stage6.md §2.4.3） */
const CANDIDATES = [
  { id: 11, plan_id: 3, scheduled_on: "2026-09-18" },
  { id: 12, plan_id: 3, scheduled_on: "2026-09-18" },
];

/** ``record_dto`` 的传输形状（T2.2 响应 ``workout_session``） */
const WORKOUT_SESSION = {
  id: 41,
  performed_on: "2026-09-18",
  plan_session_id: 12,
  sets: WAITING_WORKOUT.sets,
};

/** ``personal_best_dto`` 的传输形状（T2.2 响应 ``personal_bests``） */
const PERSONAL_BESTS = [
  {
    exercise_id: "barbell-bench-press",
    exercise_name: "杠铃卧推",
    pb_type: "weight_pb",
    value: 60,
    load_convention: "barbell_includes_bar_total",
    weight_kg: 60,
    workout_session_id: 41,
    set_no: 1,
    performed_on: "2026-09-18",
  },
];

const CONFIRM_BODY_KEYS = [
  "auto_link",
  "conversation_id",
  "performed_on",
  "plan_session_id",
  "sets",
];

const CONFIRM_SET_KEYS = [
  "duration_seconds",
  "exercise_id",
  "load_convention",
  "reps",
  "set_no",
  "set_type",
  "weight_kg",
];

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

/** 一次请求的错误结果（用例注入错误响应时捕获；不断言「不该抛」的路径） */
async function errorOf(run) {
  try {
    await run();
  } catch (error) {
    return error;
  }
  throw new Error("用例要求接口失败，但请求成功返回");
}

const passed = [];
function pass(name) {
  passed.push(name);
}

/* --- REAL 1：confirm-workout 的 URL／方法／请求体逐字段（waiting.workout 原样提交，auto_link=true） --- */

const autoLinkBody = { conversation_id: CONVERSATION_ID, ...WAITING_WORKOUT };
calls.length = 0;
respond = () =>
  jsonResponse({
    workout_session: { ...WORKOUT_SESSION, plan_session_id: 11 },
    personal_bests: PERSONAL_BESTS,
  });
const autoLinked = await api.confirmWorkout(autoLinkBody);

assert.equal(calls.length, 1);
assert.equal(calls[0].path, "/api/agent/confirm-workout");
assert.equal(calls[0].init.method, "POST");
assert.equal(calls[0].init.headers["Content-Type"], "application/json");
// 提交键集合恰为契约的五字段：不带 draft_plan_id，也不把 waiting 的其他字段塞进来
assert.deepEqual(Object.keys(calls[0].body).sort(), CONFIRM_BODY_KEYS);
for (const set of calls[0].body.sets) {
  assert.deepEqual(Object.keys(set).sort(), CONFIRM_SET_KEYS);
}
assert.deepEqual(calls[0].body, {
  conversation_id: CONVERSATION_ID,
  performed_on: "2026-09-18",
  sets: WAITING_WORKOUT.sets,
  plan_session_id: null,
  auto_link: true,
});
assert.deepEqual(autoLinked, {
  workout_session: { ...WORKOUT_SESSION, plan_session_id: 11 },
  personal_bests: PERSONAL_BESTS,
});
pass("REAL confirm-workout 请求体五字段 ＋ 每组七字段（含 set_no）逐字段，响应 {workout_session, personal_bests}");

/* --- REAL 2：用户修改后的完整载荷原样提交（显式选日程／显式额外训练） --- */

const editedSet = {
  ...WAITING_WORKOUT.sets[0],
  set_no: 2,
  weight_kg: 62.5,
  reps: 6,
};
calls.length = 0;
respond = () => jsonResponse({ workout_session: WORKOUT_SESSION, personal_bests: [] });
await api.confirmWorkout({
  conversation_id: CONVERSATION_ID,
  performed_on: "2026-09-19",
  sets: [editedSet],
  plan_session_id: 12,
  auto_link: false,
});
assert.deepEqual(calls[0].body, {
  conversation_id: CONVERSATION_ID,
  performed_on: "2026-09-19",
  sets: [editedSet],
  plan_session_id: 12,
  auto_link: false,
});

calls.length = 0;
respond = () => jsonResponse({ workout_session: WORKOUT_SESSION, personal_bests: [] });
await api.confirmWorkout({
  conversation_id: CONVERSATION_ID,
  performed_on: "2026-09-19",
  sets: [editedSet],
  plan_session_id: null,
  auto_link: false,
});
assert.equal(calls[0].body.plan_session_id, null);
assert.equal(calls[0].body.auto_link, false);
pass("REAL 用户修改日期／组序号／重量／次数／关联日程后提交修改后的完整载荷（不重算、不补默认值）");

/* --- REAL 3：错误形状（400 形状／409 日程歧义／422 领域校验）原样透出 --- */

const failures = [
  [400, "请求体不是接口约定的 JSON 形状"],
  [409, "当天存在多个未完成的计划日程，无法自动关联：11, 12"],
  [422, "同一动作内的组序号不得重复：2"],
];
for (const [status, message] of failures) {
  calls.length = 0;
  respond = () =>
    jsonResponse(
      { http_status: status, error_code: "invalid_request", message },
      status,
    );
  const error = await errorOf(() => api.confirmWorkout(autoLinkBody));
  assert.equal(error.http_status, status);
  assert.equal(error.error_code, "invalid_request");
  assert.equal(error.message, message);
}
pass("REAL confirm-workout 的 400／409／422 都是既有 JSON 错误形状（http_status／error_code／message）");

/* --- REAL 4：SSE 五类事件不扩 ＋ waiting 双路径载荷解析（复用既有 parser） --- */

const naturalLanguageFrames =
  'event: node\ndata: {"name":"natural_language_record"}\n\n' +
  'event: message\ndata: {"text":"9 月 18 日：杠铃卧推 60kg，1 组 × 5 次。"}\n\n' +
  `event: waiting\ndata: ${JSON.stringify({
    workout: WAITING_WORKOUT,
    candidate_plan_sessions: CANDIDATES,
  })}\n\n` +
  'event: done\ndata: {"ok":true,"intent":"natural_language_record","termination_reason":null,"draft_plan_id":null}';

calls.length = 0;
respond = () =>
  streamResponse([
    encoder.encode(naturalLanguageFrames.slice(0, 60)),
    encoder.encode(naturalLanguageFrames.slice(60)),
  ]);
const naturalLanguageEvents = [];
await api.runAgentStream(
  { conversation_id: CONVERSATION_ID, request: "记录今天杠铃卧推60kg5次" },
  (event) => naturalLanguageEvents.push(event),
);
assert.deepEqual(
  naturalLanguageEvents.map((event) => event.event),
  ["node", "message", "waiting", "done"],
);
assert.deepEqual(naturalLanguageEvents[2], {
  event: "waiting",
  data: { workout: WAITING_WORKOUT, candidate_plan_sessions: CANDIDATES },
});
// 自然语言打卡路径不带计划身份
assert.equal(naturalLanguageEvents[3].data.draft_plan_id, null);

// 计划路径的 waiting 仍是 Stage 5 形状（双路径同一事件名、不同载荷）
calls.length = 0;
respond = () =>
  streamResponse([
    encoder.encode('event: waiting\ndata: {"draft_plan_id":123}\n\n'),
  ]);
const planWaitingEvents = [];
await api.runAgentStream(
  { conversation_id: CONVERSATION_ID, request: "生成计划" },
  (event) => planWaitingEvents.push(event),
);
assert.deepEqual(planWaitingEvents, [
  { event: "waiting", data: { draft_plan_id: 123 } },
]);
pass("REAL SSE 五类事件内 waiting 双路径载荷（自然语言打卡 workout＋候选日程／计划 draft_plan_id）");

calls.length = 0;
respond = () =>
  streamResponse([
    encoder.encode('event: confirm\ndata: {"workout_session":{}}\n\n'),
  ]);
const sixthNameError = await errorOf(() =>
  api.runAgentStream(
    { conversation_id: CONVERSATION_ID, request: "记录今天杠铃卧推60kg5次" },
    () => {},
  ),
);
assert.match(sixthNameError.message, /未知的 SSE 事件名/);
pass("REAL 第六类事件名（confirm）立即抛错，事件名集合不扩");

/* --- STATIC 5：contract.ts 五类事件名与 waiting 双路径载荷 --- */

const contract = await readFile(
  new URL("../src/lib/contract.ts", import.meta.url),
  "utf8",
);
const plansPageSource = await readFile(
  new URL("../src/features/plans/PlansPage.tsx", import.meta.url),
  "utf8",
);
const apiSource = await readFile(
  new URL("../src/lib/api.ts", import.meta.url),
  "utf8",
);

assert.match(
  contract,
  /export type AgentEventNameWire =\s*\n\s*"node" \| "message" \| "waiting" \| "done" \| "error";/,
);
// 文件里出现的事件名字面量恰为五类，不新增第六类
const declaredEventNames = [
  ...contract.matchAll(/^ {2}event: "([a-z_]+)";$/gm),
].map((match) => match[1]);
assert.deepEqual([...new Set(declaredEventNames)].sort(), [
  "done",
  "error",
  "message",
  "node",
  "waiting",
]);
// 计划路径（Stage 5 契约）与自然语言打卡路径（§2.4.3）各一个 waiting 事件形状
assert.match(contract, /data: \{ draft_plan_id: number \};/);
const waitingWorkoutBlock = contract.slice(
  contract.indexOf("export interface AgentWaitingWorkoutEventWire"),
  contract.indexOf("export interface AgentDoneEventWire"),
);
assert.match(waitingWorkoutBlock, /event: "waiting";/);
assert.match(waitingWorkoutBlock, /workout: ConfirmWorkoutDraftWire;/);
assert.match(
  waitingWorkoutBlock,
  /candidate_plan_sessions: PlanSessionCandidateWire\[\];/,
);
pass("STATIC contract.ts 事件名恰五类；waiting 双路径为 {draft_plan_id} 与 {workout, candidate_plan_sessions}");

/* --- STATIC 6：confirm-workout 请求／响应逐字段，组形状与表单形状的差别只有 set_no --- */

const draftBlock = contract.slice(
  contract.indexOf("export interface ConfirmWorkoutDraftWire"),
  contract.indexOf("export interface AgentWaitingWorkoutEventWire"),
);
for (const field of ["performed_on", "sets", "plan_session_id", "auto_link"]) {
  assert.match(draftBlock, new RegExp(`\\n  ${field}[:(]`), `waiting.workout 缺 ${field}`);
}

const confirmBodyBlock = contract.slice(
  contract.indexOf("export interface ConfirmWorkoutBody"),
  contract.indexOf("export interface ConfirmWorkoutResponseWire"),
);
for (const field of [
  "conversation_id",
  "performed_on",
  "sets",
  "plan_session_id",
  "auto_link",
]) {
  assert.match(
    confirmBodyBlock,
    new RegExp(`\\n  ${field}[:(]`),
    `confirm-workout 请求体缺 ${field}`,
  );
}
assert.match(confirmBodyBlock, /sets: WorkoutSetConfirmWire\[\];/);
assert.match(confirmBodyBlock, /plan_session_id: number \| null;/);
assert.match(confirmBodyBlock, /auto_link: boolean;/);

const confirmSetBlock = contract.slice(
  contract.indexOf("export interface WorkoutSetConfirmWire"),
  contract.indexOf("export interface ConfirmWorkoutDraftWire"),
);
for (const field of [
  "exercise_id",
  "set_no",
  "set_type",
  "reps",
  "load_convention",
  "weight_kg",
  "duration_seconds",
]) {
  assert.match(
    confirmSetBlock,
    new RegExp(`\\n  ${field}:`),
    `confirm-workout 组缺 ${field}`,
  );
}
// 表单形状没有 set_no（后端按提交顺序分配），差别就在这一个字段
const formSetBlock = contract.slice(
  contract.indexOf("export interface WorkoutSetInputWire"),
  contract.indexOf("/** POST／PUT /api/records 请求体"),
);
assert.ok(
  !formSetBlock.includes("set_no:"),
  "WorkoutSetInputWire 不得带 set_no（表单路径的组序号由后端分配）",
);

const confirmResponseBlock = contract.slice(
  contract.indexOf("export interface ConfirmWorkoutResponseWire"),
  contract.length,
);
assert.match(confirmResponseBlock, /workout_session: RecordWire;/);
assert.match(confirmResponseBlock, /personal_bests: PersonalBestWire\[\];/);
pass("STATIC confirm-workout 请求体五字段、组七字段（比表单形状多 set_no）、响应两字段");

/* --- STATIC 7：api.ts 复用既有 post 与唯一 SSE parser，不为 message.text 增解析入口 --- */

assert.match(
  apiSource,
  /export const confirmWorkout = \(body: ConfirmWorkoutBody\) =>\n {2}post<ConfirmWorkoutResponseWire>\("\/api\/agent\/confirm-workout", body\);/,
);
assert.equal(
  (apiSource.match(/function parseAgentFrame\(/g) ?? []).length,
  1,
  "不得写第二个 SSE parser",
);
assert.equal(
  (apiSource.match(/export async function runAgentStream\(/g) ?? []).length,
  1,
);
assert.ok(
  apiSource.includes('data: JSON.parse(data.join("\\n"))'),
  "SSE 帧的 JSON 解析必须只作用于 data 行",
);
// 唯一的运行时解析是 SSE 帧的 data 行：没有任何入口去解析 message 的可见文本（§2.5.2）
assert.equal(
  (apiSource.match(/JSON\.parse\(/g) ?? []).length,
  1,
  "api.ts 不得出现第二个 JSON 解析入口（含从 message.text 反解确认数据）",
);
assert.ok(
  !/JSON\.parse\([^)]*text/.test(apiSource) &&
    !contract.includes("JSON.parse"),
  "确认提交数据的唯一来源是 waiting 结构化字段，不得解析可见文本",
);
pass("STATIC confirmWorkout 复用既有 post；SSE parser 仍唯一；无 message.text 解析入口");

/* --- STATIC 8：对话页的 waiting 双路径判别与打卡确认 UI；计划页不再有运行入口 --- */

const chatPageSource = await readFile(
  new URL("../src/features/chat/ChatPage.tsx", import.meta.url),
  "utf8",
);
const chatRoundSource = await readFile(
  new URL("../src/features/chat/utils/chatRound.ts", import.meta.url),
  "utf8",
);
const workoutConfirmSource = await readFile(
  new URL(
    "../src/features/chat/components/WorkoutConfirmCard.tsx",
    import.meta.url,
  ),
  "utf8",
);
const planWaitingSource = await readFile(
  new URL(
    "../src/features/chat/components/PlanWaitingCard.tsx",
    import.meta.url,
  ),
  "utf8",
);
const chatDraftSource = await readFile(
  new URL("../src/features/chat/utils/workoutDraft.ts", import.meta.url),
  "utf8",
);
const appSource = await readFile(
  new URL("../src/app/App.tsx", import.meta.url),
  "utf8",
);

// 计划页不再承载生成／调整与确认／拒绝入口（stage6.md §2.5.4，本轮裁决）：整页只读
for (const forbidden of [
  "runAgentStream",
  "startRun",
  "draftPlanIdOf",
  "runConversations",
  "本次运行进度",
  "confirmPlan",
  "rejectPlan",
  "Textarea",
]) {
  assert.ok(
    !plansPageSource.includes(forbidden),
    `计划页不得再有写入入口：${forbidden}`,
  );
}
// 计划确认／拒绝的按钮与调用都在对话页（计划路径 waiting，D1-③ 裁决）
assert.match(
  planWaitingSource,
  /<CardTitle>待确认计划 #\{planId\}<\/CardTitle>/,
);
assert.match(planWaitingSource, />\s*\n\s*确认启用\s*\n\s*<\/Button>/);
assert.match(planWaitingSource, />\s*\n\s*拒绝\s*\n\s*<\/Button>/);
for (const name of ["confirmPlan", "rejectPlan"]) {
  assert.ok(chatPageSource.includes(name), `对话页未使用 ${name}`);
}

// 对话页 waiting 判别：计划路径 → 计划确认卡片；打卡路径 → 打卡确认表单（§2.4.3 双载荷）
assert.match(
  chatPageSource,
  /if \("draft_plan_id" in event\.data\) \{[\s\S]{0,80}?setPlanDraft\(/,
  "计划路径 waiting 必须驱动计划确认 UI",
);
assert.match(
  chatPageSource,
  /\} else \{[\s\S]{0,80}?setWorkoutDraft\(/,
  "打卡路径 waiting 必须驱动打卡确认 UI",
);
assert.match(
  chatPageSource,
  /candidates: event\.data\.candidate_plan_sessions,/,
  "候选日程只能取 waiting.candidate_plan_sessions",
);

// 对话页不复用也不新增第二个 SSE parser（parser 唯一在 api.ts，由 STATIC 7 断言）
for (const forbidden of [
  "parseAgentFrame",
  "TextDecoder",
  "ReadableStream",
  "AGENT_EVENT_NAMES",
  "JSON.parse",
]) {
  assert.ok(
    !chatPageSource.includes(forbidden),
    `对话页不得自建 SSE 解析或文本解析：${forbidden}`,
  );
}
// message 的可见文本只被原样透传渲染一次，没有任何反解入口（§2.5.2）
assert.equal(
  (chatRoundSource.match(/event\.data\.text/g) ?? []).length,
  1,
  "event.data.text 只能出现一次（原样渲染）",
);
assert.match(
  chatRoundSource,
  /case "message":\s*\n\s*return event\.data\.text;/,
  "message 事件只能原样返回可见文本",
);

// 多候选必须展示并要求用户选择（§2.1）：三种选项与候选列表都在确认表单里
for (const option of ['"auto"', '"extra"']) {
  assert.ok(
    workoutConfirmSource.includes(`<SelectItem value=${option}>`),
    `确认表单缺 ${option}`,
  );
}
assert.match(
  workoutConfirmSource,
  /: `当天有 \$\{available\.length\} 个未完成日程：请选择本次训练对应的那个/,
  "多候选必须提示用户显式选择日程或额外训练",
);
// 取消确认只清本地状态（§4.1）：具名函数或 onCancel 内联，二者都不发请求
assert.match(
  chatPageSource,
  /const cancelWorkoutDraft = \(\) => setWorkoutDraft\(null\);|onCancel=\{\(\) => setWorkoutDraft\(null\)\}/,
  "取消确认只能清本地状态",
);
// 改日期后候选按新日期重查，且关联选择回到 waiting 的初始默认值（不沿用旧日期的日程 id）
assert.match(
  workoutConfirmSource,
  /const changePerformedOn = \(value: string\) => \{[\s\S]{0,200}?initialSessionChoice\(/,
  "改日期后关联选择必须回到初始默认值",
);
// 写入成功后沿用记录页的既有全量失效口径（不新造第二套 key 命名）
assert.match(
  workoutConfirmSource,
  /await queryClient\.invalidateQueries\(\);/,
  "打卡确认成功后必须全量失效记录派生 Query",
);
assert.ok(
  chatPageSource.includes("invalidatePlanAndCalendarQueries"),
  "对话页未使用 invalidatePlanAndCalendarQueries",
);
assert.match(
  chatPageSource,
  /invalidatePlanAndCalendarQueries\(queryClient\)/,
  "计划确认／拒绝后必须失效计划与日历 Query",
);

// App.tsx 路由与导航（D2 裁决：/chat ＋ 对话 ＋ MessageSquare）；既有四条 nav 不变
assert.match(appSource, /<Route path="\/chat" element=\{<ChatPage \/>\} \/>/);
assert.match(
  appSource,
  /\{ to: "\/chat", label: "对话", icon: MessageSquare, end: false \}/,
);
pass(
  "STATIC 对话页 waiting 双路径判别、无第二套 SSE／文本解析、多候选必须选择、取消不请求、失效口径、App 路由与导航",
);

/* --- STATIC 9：契约字段与后端源码逐字一致（对照索引见 stage6.md §2.4.2／§2.4.3） --- */

const {
  dtoSource,
  routesAgentSource,
  workflowSource,
} = {
  dtoSource: await readFile(
    new URL("../../backend/api/dto.py", import.meta.url),
    "utf8",
  ),
  routesAgentSource: await readFile(
    new URL("../../backend/api/routes_agent.py", import.meta.url),
    "utf8",
  ),
  workflowSource: await readFile(
    new URL("../../backend/graph/workflow.py", import.meta.url),
    "utf8",
  ),
};

// backend/api/dto.py::WorkoutSetBody（:190-222）与 ConfirmWorkoutBody 的字段名
for (const line of [
  "    exercise_id: str\n    set_no: int\n    set_type: str\n",
  "    reps: int | None = None\n",
  "    load_convention: str | None = None\n",
  "    weight_kg: float | None = None\n",
  "    duration_seconds: int | None = None\n",
  "    conversation_id: UUID\n    performed_on: date\n    sets: list[WorkoutSetBody]\n",
  "    plan_session_id: int | None = None\n    auto_link: bool = False\n",
]) {
  assert.ok(dtoSource.includes(line), `dto.py 缺契约字段：${line.trim()}`);
}
// backend/graph/workflow.py::_workout_payload／_candidate_plan_session_payload（:817-848）
for (const line of [
  '        "performed_on": day.isoformat(),\n',
  '                "exercise_id": fact.exercise_id,\n                "set_no": fact.set_no,\n                "set_type": fact.set_type,\n',
  '                "load_convention": fact.load_convention,\n                "weight_kg": fact.weight_kg,\n                "reps": fact.reps,\n                "duration_seconds": fact.duration_seconds,\n',
  '        "plan_session_id": None,\n        "auto_link": True,\n',
  '        "id": session.id,\n        "plan_id": session.plan_id,\n        "scheduled_on": session.scheduled_on.isoformat(),\n',
]) {
  assert.ok(
    workflowSource.includes(line),
    `workflow.py 缺 waiting 载荷字段：${line.trim()}`,
  );
}
// backend/api/routes_agent.py::confirm_workout（:128-150）的响应键
assert.match(routesAgentSource, /@router\.post\("\/api\/agent\/confirm-workout"\)/);
assert.match(routesAgentSource, /"workout_session": record_dto\(session\),/);
assert.match(
  routesAgentSource,
  /"personal_bests": \[personal_best_dto\(best\) for best in bests\],/,
);
pass("STATIC 契约字段与后端 dto.py／workflow.py／routes_agent.py 源码逐字一致");

/* --- REAL 10：对话页打卡载荷 → 确认请求：三种日程关联形状与组字段逐字往返（T2.4） --- */

const chatDraft = await import("../src/features/chat/utils/workoutDraft.ts");

// 三种提交形状（stage6.md §2.1 硬边界表／§2.4.2）
assert.deepEqual(chatDraft.sessionLink("auto"), {
  plan_session_id: null,
  auto_link: true,
});
assert.deepEqual(chatDraft.sessionLink("extra"), {
  plan_session_id: null,
  auto_link: false,
});
assert.deepEqual(chatDraft.sessionLink(12), {
  plan_session_id: 12,
  auto_link: false,
});

// waiting.workout 的关联默认值 → 选择项：初始 null/true 即「未手动选择」
assert.equal(chatDraft.initialSessionChoice(null, true), "auto");
assert.equal(chatDraft.initialSessionChoice(null, false), "extra");
assert.equal(chatDraft.initialSessionChoice(12, true), 12);

// 候选日程的初始值只在 waiting 自己的日期上生效；改日期后必须按新日期重查（§2.5.1）
assert.deepEqual(
  chatDraft.initialCandidates("2026-09-18", "2026-09-18", CANDIDATES),
  { sessions: CANDIDATES },
);
assert.equal(
  chatDraft.initialCandidates("2026-09-19", "2026-09-18", CANDIDATES),
  undefined,
);

// 编辑行 ↔ 载荷的七字段逐字往返：set_no 原样携带，计时组只送秒数、其余只送次数
const rows = chatDraft.rowsFromWorkout(WAITING_WORKOUT.sets);
assert.deepEqual(
  rows.map((row) => [row.exercise_id, row.set_no, row.timed]),
  [
    [WAITING_WORKOUT.sets[0].exercise_id, "1", false],
    [WAITING_WORKOUT.sets[1].exercise_id, "1", true],
  ],
);
assert.deepEqual(chatDraft.setsFromRows(rows), WAITING_WORKOUT.sets);

// 未手动选择（含恰一个候选）：plan_session_id=null 且 auto_link=true
calls.length = 0;
respond = () => jsonResponse({ workout_session: WORKOUT_SESSION, personal_bests: [] });
const autoBody = chatDraft.confirmBodyOf({
  conversation_id: CONVERSATION_ID,
  performed_on: WAITING_WORKOUT.performed_on,
  rows,
  choice: "auto",
});
assert.deepEqual(Object.keys(autoBody).sort(), CONFIRM_BODY_KEYS);
for (const set of autoBody.sets) {
  assert.deepEqual(Object.keys(set).sort(), CONFIRM_SET_KEYS);
}
assert.deepEqual(autoBody, {
  conversation_id: CONVERSATION_ID,
  performed_on: WAITING_WORKOUT.performed_on,
  sets: WAITING_WORKOUT.sets,
  plan_session_id: null,
  auto_link: true,
});
// 对话页构造的载荷原样走既有 confirmWorkout 封装（同一个端点与错误形状）
await api.confirmWorkout(autoBody);
assert.equal(calls[0].path, "/api/agent/confirm-workout");
assert.equal(calls[0].init.method, "POST");
assert.deepEqual(calls[0].body, autoBody);
pass(
  "REAL 对话页未手动选择 → plan_session_id=null 且 auto_link=true；组七字段（含 set_no）逐字往返；候选初始值只在原日期生效",
);

/* --- REAL 11：用户在行内编辑日期／组序号／重量／次数后的完整载荷与增删组行 --- */

const edited = rows.map((row, index) =>
  index === 0
    ? { ...row, set_no: "2", weight_kg: "62.5", reps: "6" }
    : { ...row, duration_seconds: "75" },
);
assert.deepEqual(
  chatDraft.confirmBodyOf({
    conversation_id: CONVERSATION_ID,
    performed_on: "2026-09-19",
    rows: edited,
    choice: 12,
  }),
  {
    conversation_id: CONVERSATION_ID,
    performed_on: "2026-09-19",
    sets: [
      {
        exercise_id: WAITING_WORKOUT.sets[0].exercise_id,
        set_no: 2,
        set_type: WAITING_WORKOUT.sets[0].set_type,
        reps: 6,
        load_convention: WAITING_WORKOUT.sets[0].load_convention,
        weight_kg: 62.5,
        duration_seconds: null,
      },
      {
        exercise_id: WAITING_WORKOUT.sets[1].exercise_id,
        set_no: 1,
        set_type: WAITING_WORKOUT.sets[1].set_type,
        reps: null,
        load_convention: null,
        weight_kg: null,
        duration_seconds: 75,
      },
    ],
    plan_session_id: 12,
    auto_link: false,
  },
);

// 增删组行（D4 裁决 c）：新增行沿用最后一行的动作并给同动作 max+1 的组序号初值，删除不重排
const added = chatDraft.addSetRow(rows);
assert.equal(added.length, 3);
const appended = added[2];
assert.equal(appended.exercise_id, rows[1].exercise_id);
assert.equal(appended.set_no, "2");
assert.equal(appended.set_type, rows[1].set_type);
assert.equal(appended.load_convention, rows[1].load_convention);
assert.equal(appended.timed, true);
assert.equal(appended.reps, "");
assert.equal(appended.weight_kg, "");
assert.equal(appended.duration_seconds, "");
assert.deepEqual(
  chatDraft.removeSetRow(rows, 1).map((row) => row.set_no),
  ["1"],
);
// 组序号在行内手填：空值就地失败，不静默补默认值
assert.throws(
  () => chatDraft.setsFromRows([{ ...rows[0], set_no: "" }]),
  /请填写组序号/,
);
pass(
  "REAL 行内改日期／组序号／重量／次数后提交完整载荷；增删组行；空组序号就地失败",
);

/* --- REAL 12：对话页打卡路径的 400／409／422 仍是既有 JSON 错误形状 --- */

for (const [status, message] of failures) {
  calls.length = 0;
  respond = () =>
    jsonResponse(
      { http_status: status, error_code: "invalid_request", message },
      status,
    );
  const error = await errorOf(() => api.confirmWorkout(autoBody));
  assert.equal(error.http_status, status);
  assert.equal(error.error_code, "invalid_request");
  assert.equal(error.message, message);
}
pass("REAL 对话页提交的同一载荷在 400／409／422 下原样透出后端产品错误文本");

/* --- STATIC 13：对话页纯映射模块的边界（无 React／无 DOM；三种形状集中在工作模块） --- */

assert.ok(
  !chatDraftSource.includes('from "react"'),
  "纯映射模块不得依赖 React",
);
for (const name of [
  "export function rowsFromWorkout",
  "export function setsFromRows",
  "export function sessionLink",
  "export function initialSessionChoice",
  "export function initialCandidates",
  "export function addSetRow",
  "export function removeSetRow",
  "export function confirmBodyOf",
]) {
  assert.ok(chatDraftSource.includes(name), `workoutDraft.ts 缺 ${name}`);
}
pass(
  "STATIC 对话页确认载荷由无 React 的纯映射模块给出（REAL 11–12 直接调用它）",
);

console.log(`通过 ${passed.length} 组断言：`);
for (const name of passed) console.log(`  - ${name}`);
