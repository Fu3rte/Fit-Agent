import assert from "node:assert/strict";
import { readFile as readFileRaw } from "node:fs/promises";

/** 源码断言按 LF 口径比对；``core.autocrlf`` 检出的 CRLF 工作树在此归一化 */
async function readFile(url) {
  return (await readFileRaw(url, "utf8")).replace(/\r\n/g, "\n");
}

const api = await import("../src/lib/api.ts");

const encoder = new TextEncoder();
const CONVERSATION_ID = "6a1c0f3e-2b47-4d90-8e5a-0c3f7b1d9a24";
/** 稳定会话身份（conversations.id）；thread 身份与它不同，两者一起构成确认动作的定位 */
const CHAT_ID = "8d5a4c21-7e36-4b90-9f13-2c6a0d8b5e47";

/** 一轮 Run 的请求体：稳定会话身份 ＋ 本轮 thread 与幂等键，与后端 DTO 逐字段一致 */
const runBody = (request) => ({
  chat_id: CHAT_ID,
  conversation_id: CONVERSATION_ID,
  client_request_id: crypto.randomUUID(),
  request,
});

/** ``waiting.workout``：后端 ``_workout_payload`` 的载荷，确认 UI 的编辑数据源 */
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

/** ``waiting.candidate_plan_sessions``：数据库候选日程 */
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
  "chat_id",
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

const autoLinkBody = {
  chat_id: CHAT_ID,
  conversation_id: CONVERSATION_ID,
  ...WAITING_WORKOUT,
};
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
// 提交键集合恰为契约的六字段：不带 draft_plan_id，也不把 waiting 的其他字段塞进来
assert.deepEqual(Object.keys(calls[0].body).sort(), CONFIRM_BODY_KEYS);
for (const set of calls[0].body.sets) {
  assert.deepEqual(Object.keys(set).sort(), CONFIRM_SET_KEYS);
}
assert.deepEqual(calls[0].body, {
  chat_id: CHAT_ID,
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
pass("REAL confirm-workout 请求体六字段 ＋ 每组七字段（含 set_no）逐字段，响应 {workout_session, personal_bests}");

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
  chat_id: CHAT_ID,
  conversation_id: CONVERSATION_ID,
  performed_on: "2026-09-19",
  sets: [editedSet],
  plan_session_id: 12,
  auto_link: false,
});
assert.deepEqual(calls[0].body, {
  chat_id: CHAT_ID,
  conversation_id: CONVERSATION_ID,
  performed_on: "2026-09-19",
  sets: [editedSet],
  plan_session_id: 12,
  auto_link: false,
});

calls.length = 0;
respond = () => jsonResponse({ workout_session: WORKOUT_SESSION, personal_bests: [] });
await api.confirmWorkout({
  chat_id: CHAT_ID,
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
  runBody("记录今天杠铃卧推60kg5次"),
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

// 计划路径的 waiting 形状（双路径同一事件名、不同载荷）
calls.length = 0;
respond = () =>
  streamResponse([
    encoder.encode('event: waiting\ndata: {"draft_plan_id":123}\n\n'),
  ]);
const planWaitingEvents = [];
await api.runAgentStream(runBody("生成计划"), (event) =>
  planWaitingEvents.push(event),
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
    runBody("记录今天杠铃卧推60kg5次"),
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
// 计划路径与自然语言打卡路径各一个 waiting 事件形状
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
pass("STATIC confirm-workout 请求体六字段（含 chat_id）、组七字段（比表单形状多 set_no）、响应两字段");

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
// 唯一的运行时解析是 SSE 帧的 data 行：没有任何入口去解析 message 的可见文本
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
const chatTranscriptSource = await readFile(
  new URL(
    "../src/features/chat/components/ChatTranscript.tsx",
    import.meta.url,
  ),
  "utf8",
);

// 计划页不再承载生成／调整与确认／拒绝入口：整页只读
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
// 计划确认／拒绝的按钮与调用都在对话页（计划路径 waiting）
assert.match(
  planWaitingSource,
  /<CardTitle>待确认计划 #\{planId\}<\/CardTitle>/,
);
assert.match(planWaitingSource, />\s*\n\s*确认启用\s*\n\s*<\/Button>/);
assert.match(planWaitingSource, />\s*\n\s*拒绝\s*\n\s*<\/Button>/);
// 确认卡必须拉取并展示 draft 正文，供用户判断是否启用
assert.match(
  planWaitingSource,
  /getPlan\(planId\)/,
  "确认卡必须按 plan_id 读取计划版本",
);
assert.match(
  planWaitingSource,
  /structured_content as PlanDraftWire/,
  "确认卡必须解析 structured_content",
);
for (const field of [
  "draft.starts_on",
  "draft.goal",
  "draft.weekly_frequency",
  "draft.training_days",
  "draft.explanation",
]) {
  assert.ok(
    planWaitingSource.includes(field),
    `确认卡未展示计划字段：${field}`,
  );
}
assert.match(
  apiSource,
  /export const getPlan = \(planId: number\) =>\s*\n\s*request<PlanItemWire>\(`\/api\/plans\/\$\{planId\}`\)/,
  "api.ts 必须导出 getPlan",
);
for (const name of ["confirmPlan", "rejectPlan"]) {
  assert.ok(chatPageSource.includes(name), `对话页未使用 ${name}`);
}

// 对话页 waiting 判别：计划路径 → 计划确认卡片；打卡路径 → 打卡确认表单
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
// message 的可见文本只被原样透传渲染一次，没有任何反解入口
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

// 多候选必须展示并要求用户选择：三种选项与候选列表都在确认表单里
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
// 取消确认只清本地状态：具名函数或 onCancel 内联，二者都不发请求
assert.match(
  chatPageSource,
  /const cancelWorkoutDraft = \(\) => setWorkoutDraft\(null\);|onCancel=\{\(\) => setWorkoutDraft\(null\)\}/,
  "取消确认只能清本地状态",
);
// 改日期后候选按新日期重查，且关联选择回到 waiting 的初始默认值（不沿用旧日期的日程 id）
assert.match(
  workoutConfirmSource,
  /const changePerformedOn = \(value: string\) => \{[\s\S]{0,200}?plan_session_id \?\? "extra"/,
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

// App.tsx 路由与导航（根路径为新会话状态 ＋ /chat/:chatId；nav 只剩看板与计划，会话历史由 ConversationList 承载）
assert.match(appSource, /<Route path="\/" element=\{<ChatPage \/>\} \/>/);
assert.ok(
  !appSource.includes('path="/chat"'),
  "根路径即新会话状态，不得再有独立 /chat 路由",
);
assert.match(
  appSource,
  /<Route path="\/chat\/:chatId" element=\{<ChatPage \/>\} \/>/,
);
assert.match(
  appSource,
  /\{ to: "\/dashboard", label: "数据看板", icon: LayoutDashboard, end: false \}/,
);
assert.match(
  appSource,
  /\{ to: "\/plans", label: "训练计划", icon: ClipboardList, end: false \}/,
);
assert.match(appSource, /<ConversationList \/>/);
assert.ok(
  !appSource.includes('label: "对话"'),
  "会话入口已由 ConversationList 承载，nav 不得再有对话项",
);
pass(
  "STATIC 对话页 waiting 双路径判别、无第二套 SSE／文本解析、多候选必须选择、取消不请求、失效口径、App 路由与导航",
);

/* --- STATIC 9：契约字段与后端源码逐字一致 --- */

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

// 三种提交形状
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

// 未显式给出日程时默认「额外训练」（WorkoutConfirmCard 内联 `plan_session_id ?? "extra"`）
assert.ok(
  workoutConfirmSource.includes('plan_session_id ?? "extra"'),
  "确认卡默认日程选择必须是额外训练",
);

// 候选日程的初始值只在 waiting 自己的日期上生效；改日期后必须按新日期重查
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
  chat_id: CHAT_ID,
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
  chat_id: CHAT_ID,
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
    chat_id: CHAT_ID,
    conversation_id: CONVERSATION_ID,
    performed_on: "2026-09-19",
    rows: edited,
    choice: 12,
  }),
  {
    chat_id: CHAT_ID,
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

// 增删组行：新增行沿用最后一行的动作并给同动作 max+1 的组序号初值，删除不重排
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

/* --- REAL 13：会话详情 → 消息列轮次的纯转换、在途轮次收敛与等待卡恢复 --- */

const {
  conversationTitle,
  detailToRounds,
  mergeRounds,
  mergeWaitingDrafts,
  waitingDrafts,
} = await import("../src/features/chat/utils/conversationHistory.ts");

const PENDING_THREAD = "1b9d5c74-3e0f-4a2b-8c61-5d0e2f7a9b34";
const DETAIL = {
  conversation: {
    id: CHAT_ID,
    title: "记录今天杠铃卧推60kg5次",
    created_at: "2026-09-18T10:00:00+00:00",
    updated_at: "2026-09-18T10:00:05+00:00",
  },
  rounds: [
    {
      run_id: "run-1",
      conversation_id: CONVERSATION_ID,
      status: "completed",
      request: "记录今天杠铃卧推60kg5次",
      assistants: [{ entry_id: "a-1", content: "已记录。", status: "complete" }],
      confirmations: [],
      events: [
        { sequence: 1, event: "message", data: { text: "已记录。" } },
        {
          sequence: 2,
          event: "done",
          data: {
            ok: true,
            intent: "natural_language_record",
            termination_reason: null,
            draft_plan_id: null,
          },
        },
      ],
    },
    {
      run_id: "run-2",
      conversation_id: PENDING_THREAD,
      status: "waiting",
      request: "生成计划",
      assistants: [{ entry_id: "a-2", content: "解析中…", status: "partial" }],
      confirmations: [],
      events: [{ sequence: 1, event: "waiting", data: { draft_plan_id: 123 } }],
    },
  ],
  compactions: [],
};

const restored = detailToRounds(DETAIL);
assert.equal(restored.length, 2);
assert.deepEqual(
  restored[0].events.map((event) => event.event),
  ["message", "done"],
);
// 落库事件进消息列时只保留 SSE 形状：sequence 是存储顺序，不是产品事件字段
assert.deepEqual(Object.keys(restored[0].events[0]).sort(), ["data", "event"]);
assert.equal(restored[0].request, DETAIL.rounds[0].request);
assert.equal(restored[0].run_status, "completed");
assert.deepEqual(Object.keys(restored[0].assistants[0]).sort(), [
  "content",
  "entry_id",
  "status",
]);

const inFlight = {
  chat_id: CHAT_ID,
  conversation_id: "pending-thread-2",
  request: "改成周三",
  events: [],
  assistants: [],
  confirmations: [],
  run_status: null,
};
// 服务端已有同一 thread 时以服务端投影为准，未落库的在途轮次接在末尾
assert.deepEqual(
  mergeRounds(restored, [
    { ...inFlight, conversation_id: CONVERSATION_ID },
  ]).map((round) => round.conversation_id),
  [CONVERSATION_ID, PENDING_THREAD],
);
assert.deepEqual(
  mergeRounds(restored, [inFlight]).map((round) => round.conversation_id),
  [CONVERSATION_ID, PENDING_THREAD, "pending-thread-2"],
);

// waiting 恢复：未确认的计划等待恢复卡片，打卡路径不误恢复成另一条路径
assert.deepEqual(waitingDrafts(restored, CHAT_ID), {
  plan: { chat_id: CHAT_ID, conversation_id: PENDING_THREAD, plan_id: 123 },
  workout: null,
});
const confirmedRound = restored.map((round) =>
  round.conversation_id === PENDING_THREAD
    ? {
        ...round,
        confirmations: [
          { entry_id: "c-1", action: "plan_confirmed", text: "已确认" },
        ],
      }
    : round,
);
assert.deepEqual(waitingDrafts(confirmedRound, CHAT_ID), {
  plan: null,
  workout: null,
});

// 详情空快照不得抹掉 SSE 已展示的 waiting；服务端确认投影出现后才清掉
const localWorkout = {
  chat_id: CHAT_ID,
  conversation_id: "sse-thread",
  workout: { performed_on: "2026-09-20", sets: [], plan_session_id: null, auto_link: false },
  candidates: [],
};
assert.deepEqual(
  mergeWaitingDrafts(localWorkout, null, restored),
  localWorkout,
);
const serverConfirmed = detailToRounds({
  ...DETAIL,
  rounds: [
    {
      run_id: "run-sse",
      conversation_id: "sse-thread",
      status: "waiting",
      request: "今天额外练了引体向上",
      assistants: [],
      confirmations: [
        { entry_id: "c-w", action: "workout_confirmed", text: "已确认" },
      ],
      events: [
        {
          sequence: 1,
          event: "waiting",
          data: {
            workout: localWorkout.workout,
            candidate_plan_sessions: [],
          },
        },
      ],
    },
  ],
});
assert.equal(
  mergeWaitingDrafts(
    localWorkout,
    waitingDrafts(serverConfirmed, CHAT_ID).workout,
    serverConfirmed,
  ),
  null,
);

// 标题按 Unicode 码点截断且不空白；根路径（``chatId`` 缺失）即新会话状态
assert.equal(
  conversationTitle("  记录今天杠铃卧推60kg5次  "),
  "记录今天杠铃卧推60kg5次",
);
assert.equal(Array.from(conversationTitle("句".repeat(40))).length, 30);
pass(
  "REAL 会话详情→轮次（事件去 sequence）、在途轮次收敛、waiting 恢复与已确认不复活、空快照不抹本地卡、标题纯函数",
);

/* --- REAL 14：会话 REST 的 URL／方法与请求体 --- */

calls.length = 0;
respond = () => jsonResponse({ conversations: [DETAIL.conversation] });
const conversations = await api.listConversations();
assert.equal(calls.length, 1);
assert.equal(calls[0].path, "/api/conversations");
assert.equal(calls[0].init.method, undefined);
assert.deepEqual(
  conversations.conversations.map((conversation) => conversation.id),
  [CHAT_ID],
);

calls.length = 0;
respond = () => jsonResponse(DETAIL.conversation, 201);
await api.createConversation({ title: "记录今天杠铃卧推60kg5次" });
assert.equal(calls[0].path, "/api/conversations");
assert.equal(calls[0].init.method, "POST");
assert.deepEqual(calls[0].body, { title: "记录今天杠铃卧推60kg5次" });

calls.length = 0;
respond = () => jsonResponse(DETAIL);
await api.readConversation(CHAT_ID);
assert.equal(calls[0].path, `/api/conversations/${CHAT_ID}`);
assert.equal(calls[0].init.method, undefined);

calls.length = 0;
respond = () => jsonResponse({ deleted: true });
await api.deleteConversation(CHAT_ID);
assert.equal(calls[0].path, `/api/conversations/${CHAT_ID}`);
assert.equal(calls[0].init.method, "DELETE");
pass("REAL 会话列表／新建／详情／删除的 URL、方法与请求体（删除用方法而非路径动词）");

console.log(`通过 ${passed.length} 组断言：`);
for (const name of passed) console.log(`  - ${name}`);
