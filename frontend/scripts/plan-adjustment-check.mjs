// 运行：node scripts/plan-adjustment-check.mjs
// 调整计划前端专项（plan-adjustment-contract §2–§9、plan-import-adjustment-contract §6–§10、plan-generation-contract §6）：
// 直接调用生产解析函数、事件归约、历史投影与展示组件，核验调整结果解析、完整计划与 notes 展示、
// 实时与历史一致、保存落定后的计划查询刷新。输入均为契约样例，不代表后端实际运行结果；
// 计划页查询使用本地契约样例传输层，真实后端 HTTP 与模型流程由后端专项及联调覆盖。
import assert from "node:assert/strict";
import { createServer as createHttpServer } from "node:http";
import { QueryClientProvider, QueryObserver } from "@tanstack/react-query";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const server = await createServer({
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});
const business = await server.ssrLoadModule("/src/lib/business.ts");
const reactAgent = await server.ssrLoadModule(
  "/src/features/chat/utils/reactAgent.ts",
);
const history = await server.ssrLoadModule(
  "/src/features/chat/utils/sessionHistory.ts",
);
const api = await server.ssrLoadModule("/src/lib/api.ts");
const { default: PlanFields } = await server.ssrLoadModule(
  "/src/features/plans/PlanFields.tsx",
);
const { default: PlansPage } = await server.ssrLoadModule(
  "/src/features/plans/PlansPage.tsx",
);
const { PLAN_QUERY_KEY, queryClient } = await server.ssrLoadModule(
  "/src/lib/query.ts",
);
const { formatTimestamp } = await server.ssrLoadModule("/src/lib/utils.ts");
await server.close();

const clone = (value) => structuredClone(value);
const u = (n) => `${n.toString(16).padStart(8, "0")}-0000-4000-8000-000000000000`;

const PROPOSAL_ID = u(0x11111111);
const BASE_PLAN_ID = u(0x22222222);
const SAVED_PLAN_ID = u(0x33333333);
const OTHER_PROPOSAL_ID = u(0x44444444);
const CONFIRM_ENTRY = u(0x55555555);
const RUN_ID = u(0x66666666);
const SESSION_ID = u(0x77777777);
const USER_ENTRY = u(0x88888888);
const ASSISTANT_ENTRY = u(0x99999999);
const RESULT_NODE = u(0xaaaaaaaa);
const STATUS_NODE = u(0xbbbbbbbb);
const ADJUSTMENT = "prepare_plan_adjustment";
const ADJUST_CALL = "tc-adjust-prepare";
const SAVE_CALL = "tc-adjust-save";
const STATUS_CALL = "tc-adjust-status";
const WORKOUTS_CALL = "tc-recent-workouts";
const CREATED_AT = 1780272000000;

/** 调整场景契约样例：同名同目录动作出现在两个位置、目录外动作全部未知、不完整训练日与休息日并存 */
const dayNotes = [
  "第 1 个杠铃卧推：重量由 75 kg 调整为 80 kg，口径为杠铃含杆总重。",
  "依据 2026-09-28 与 2026-10-05 两次记录，5 组 5 次全部完成且末组仍有储备。",
  "组间休息由 150 秒延长为 180 秒。",
].join("\n");
const planNotes = [
  "本次仅修改推日第 1 个动作的重量与组间休息，其余动作与训练日保持原值。",
  "原计划第 3 天的凳上臂屈伸已删除：该动作依赖可调哑铃，用户本次说明家中无该器械。",
  "肩部不适时立即停止卧推并就医评估。",
].join("\n");
const adjustmentPayload = {
  repeat: false,
  days: [
    {
      kind: "training",
      focus: "推",
      exercises: [
        {
          exercise_id: "0409",
          name: "杠铃卧推",
          sets: 5,
          reps: 5,
          duration_seconds: null,
          weight_kg: 80,
          load_convention: "barbell_total",
          rest_seconds: 180,
        },
        {
          exercise_id: "0409",
          name: "杠铃卧推",
          sets: 3,
          reps: 8,
          duration_seconds: null,
          weight_kg: 0,
          load_convention: "barbell_total",
          rest_seconds: 0,
        },
        {
          exercise_id: null,
          name: "弹力带夹胸",
          sets: null,
          reps: null,
          duration_seconds: 45,
          weight_kg: null,
          load_convention: null,
          rest_seconds: null,
        },
      ],
      notes: dayNotes,
    },
    { kind: "rest", focus: null, exercises: [], notes: null },
    { kind: "training", focus: "腿", exercises: [], notes: null },
  ],
  notes: planNotes,
  suggested_fields: [
    "/days/0/exercises/0/weight_kg",
    "/days/0/exercises/0/rest_seconds",
    "/days/0/notes",
    "/days/2",
    "/notes",
  ],
};
const adjustmentProposal = {
  proposal_id: PROPOSAL_ID,
  preparation_kind: "adjustment",
  base_profile_version: 4,
  base_plan_id: BASE_PLAN_ID,
  payload: adjustmentPayload,
};
const adjustmentText = JSON.stringify(adjustmentProposal);
const dayAt = (index, exercises) => ({
  ...adjustmentPayload,
  days: adjustmentPayload.days.map((day, at) =>
    at === index ? { ...day, exercises } : day,
  ),
});

/** 全部未知的不完整循环：两个依据字段同时为空 */
const unknownPayload = {
  repeat: null,
  days: [
    { kind: "training", focus: null, exercises: [], notes: null },
    { kind: "rest", focus: null, exercises: [], notes: null },
  ],
  notes: null,
  suggested_fields: [],
};
const nullBasisProposal = {
  proposal_id: PROPOSAL_ID,
  preparation_kind: "adjustment",
  base_profile_version: null,
  base_plan_id: null,
  payload: unknownPayload,
};

/** 1. 调整结果解析：完整字段、依据与数组顺序逐字保留 */
assert.deepEqual(
  business.parsePlanAdjustmentProposal(clone(adjustmentProposal)),
  adjustmentProposal,
  "调整快照丢失字段或数组顺序",
);
assert.deepEqual(
  business.preparedPlanAdjustmentProposal(adjustmentText),
  adjustmentProposal,
  "标准 JSON 解析后的调整结果一致",
);
assert.deepEqual(
  business.parsePlanAdjustmentProposal(clone(nullBasisProposal)),
  nullBasisProposal,
  "无画像且无当前计划时两个依据字段同时为 null 且完整 payload 保持",
);

/** 2. 严格校验：字段集合、准备类型、依据类型与数值口径全部就地失败 */
const rejected = (value, message) =>
  assert.throws(
    () => business.parsePlanAdjustmentProposal(clone(value)),
    /无效|缺少|含未定义字段|不一致|为空|含动作|携带/,
    message,
  );
for (const field of [
  "proposal_id",
  "preparation_kind",
  "base_profile_version",
  "base_plan_id",
  "payload",
]) {
  const missing = { ...adjustmentProposal };
  delete missing[field];
  rejected(missing, `调整快照缺少 ${field}`);
}
rejected({ ...adjustmentProposal, extra: 1 }, "调整快照含未定义字段");
rejected({ ...adjustmentProposal, attachments: [] }, "原文件信息不进入调整快照");
for (const kind of ["import", "generation", "ADJUSTMENT", "adjustment ", 1, null])
  rejected(
    { ...adjustmentProposal, preparation_kind: kind },
    `准备类型 ${String(kind)} 被拒绝`,
  );
for (const id of ["11111111111111111111111111111111", "nope", "", 1, undefined])
  rejected({ ...adjustmentProposal, proposal_id: id }, `非法 proposal_id ${String(id)}`);
for (const id of ["None", "not-a-uuid", "", 1, true])
  rejected({ ...adjustmentProposal, base_plan_id: id }, `非法 base_plan_id ${String(id)}`);
for (const version of ["None", "4", 4.5, 0, -1, true, false, Number.NaN, Infinity, ""])
  rejected(
    { ...adjustmentProposal, base_profile_version: version },
    `依据画像版本 ${String(version)} 既非正整数也非 null`,
  );
for (const [field, values] of [
  ["sets", ["5", true, 5.5, 0, -1, Number.NaN, Infinity]],
  ["reps", ["5", true, 0, -1, Number.NaN]],
  ["duration_seconds", ["45", true, 0, -1, Number.NaN, Infinity]],
  ["weight_kg", ["80", true, -0.5, Number.NaN, Infinity]],
  ["rest_seconds", ["180", true, -1, Number.NaN, Infinity]],
])
  for (const bad of values)
    rejected(
      {
        ...adjustmentProposal,
        payload: dayAt(0, [
          { ...adjustmentPayload.days[0].exercises[0], [field]: bad },
          ...adjustmentPayload.days[0].exercises.slice(1),
        ]),
      },
      `动作 ${field} 拒绝非法值 ${String(bad)}`,
    );
rejected(
  {
    ...adjustmentProposal,
    payload: dayAt(0, [
      { ...adjustmentPayload.days[0].exercises[0], weight_kg: 0, load_convention: null },
      ...adjustmentPayload.days[0].exercises.slice(1),
    ]),
  },
  "重量 0 缺少口径被拒绝",
);
rejected(
  {
    ...adjustmentProposal,
    payload: dayAt(0, [
      { ...adjustmentPayload.days[0].exercises[0], load_convention: "heavy" },
      ...adjustmentPayload.days[0].exercises.slice(1),
    ]),
  },
  "未知重量口径被拒绝",
);
assert.deepEqual(
  business.parsePlanAdjustmentProposal({
    ...adjustmentProposal,
    payload: dayAt(0, [
      {
        ...adjustmentPayload.days[0].exercises[0],
        weight_kg: null,
        load_convention: "barbell_total",
      },
      ...adjustmentPayload.days[0].exercises.slice(1),
    ]),
  }).payload.days[0].exercises[0].load_convention,
  "barbell_total",
  "重量未知时保留已知口径",
);
rejected({ ...adjustmentProposal, payload: { ...adjustmentPayload, days: [] } }, "调整快照要求至少一个训练日");
rejected(
  {
    ...adjustmentProposal,
    payload: {
      ...adjustmentPayload,
      days: [
        { ...adjustmentPayload.days[0] },
        { ...adjustmentPayload.days[1], exercises: [adjustmentPayload.days[0].exercises[0]] },
        adjustmentPayload.days[2],
      ],
    },
  },
  "休息日含动作被拒绝",
);
rejected({ ...adjustmentProposal, payload: { ...adjustmentPayload, repeat: "false" } }, "循环方式拒绝字符串");
rejected({ ...adjustmentProposal, payload: { ...adjustmentPayload, repeat: 0 } }, "循环方式拒绝布尔数值");
rejected(
  { ...adjustmentProposal, payload: { ...adjustmentPayload, suggested_fields: ["days/0"] } },
  "建议来源非 JSON Pointer",
);
rejected(
  { ...adjustmentProposal, payload: { ...adjustmentPayload, notes: "   " } },
  "整体说明拒绝空白文本",
);
assert.throws(
  () => business.preparedPlanAdjustmentProposal("{adjustment"),
  SyntaxError,
  "使用标准 JSON 解析器，非法 JSON 就地失败",
);

/** 3. 展示分派：按实际工具名选择 schema，查询与保存工具不形成待确认展示 */
assert.deepEqual(
  reactAgent.preparedPlanDisplay(ADJUSTMENT, adjustmentText),
  { plan: adjustmentProposal },
  "调整结果按 adjustment schema 投影",
);
assert.throws(
  () => reactAgent.preparedPlanDisplay("prepare_plan_import", adjustmentText),
  /无效/,
  "录入 schema 不接纳 adjustment",
);
assert.throws(
  () => reactAgent.preparedPlanDisplay("prepare_plan", adjustmentText),
  /含未定义字段/,
  "生成 schema 不接纳 preparation_kind",
);
assert.throws(
  () =>
    reactAgent.preparedPlanDisplay(
      ADJUSTMENT,
      JSON.stringify({ ...nullBasisProposal, payload: { ...adjustmentPayload, days: [] } }),
    ),
  /缺少/,
  "调整结果违反 schema 在解析位置失败",
);
for (const name of [
  "get_current_plan",
  "get_plan",
  "list_plans",
  "save_plan",
  "get_plan_save_status",
  "list_workouts",
  "search_exercises",
])
  assert.equal(
    reactAgent.preparedPlanDisplay(name, adjustmentText),
    null,
    `${name} 走通用工具展示`,
  );

/* ===== 展示组件 ===== */
const renderPlan = (content) =>
  renderToStaticMarkup(createElement(PlanFields, { content }));
const withSuggested = (fields) =>
  renderPlan({ ...adjustmentPayload, suggested_fields: fields });
const rowOf = (markup, text) => {
  const at = markup.indexOf(text);
  assert.ok(at >= 0, `展示缺少内容：${text}`);
  return markup.slice(markup.lastIndexOf("<li", at), markup.indexOf("</li>", at));
};
const shown = renderPlan(adjustmentPayload);

/** 4. 完整计划展示：循环方式、全部训练日与休息日、主题、动作顺序与身份、逐字段数值 */
for (const text of [
  "按序列执行",
  "第 1 天 · 训练日",
  "第 2 天 · 休息日",
  "第 3 天 · 训练日",
  ">推<",
  ">腿<",
  ">5 组<",
  ">目标 5 次<",
  ">重量 80 kg<",
  ">重量口径：杠铃含杆总重<",
  ">组间休息 180 秒<",
  ">3 组<",
  ">目标 8 次<",
  ">重量 0 kg<",
  ">组间不安排休息<",
  ">组数未知<",
  ">目标次数未知<",
  ">时长 45 秒<",
  ">重量未知<",
  ">重量口径未知<",
  ">组间休息未结构化指定<",
  ">时长未知<",
  "未在动作数据集内核实",
])
  assert.ok(shown.includes(text), `展示缺少业务内容：${text}`);
assert.equal(shown.split(">未在动作数据集内核实<").length - 1, 1, "仅目录外动作标注核实状态");
assert.equal(shown.split(">杠铃卧推</div>").length - 1, 2, "同名动作按位置各展示一次");
assert.equal(shown.split(">重量未知<").length - 1, 1, "未知重量与已知 0 kg 分别呈现");
assert.equal(shown.split(">组间休息未结构化指定<").length - 1, 1, "未结构化休息与不安排休息分别呈现");
assert.ok(
  shown.indexOf("第 1 天 · 训练日") < shown.indexOf("第 2 天 · 休息日") &&
    shown.indexOf("第 2 天 · 休息日") < shown.indexOf("第 3 天 · 训练日"),
  "训练日顺序沿用 payload 数组顺序",
);
assert.ok(
  shown.indexOf(">重量 80 kg<") < shown.indexOf(">重量 0 kg<") &&
    shown.indexOf(">重量 0 kg<") < shown.indexOf(">组数未知<"),
  "动作顺序沿用 payload 数组顺序",
);
const unknownShown = renderPlan(unknownPayload);
assert.ok(unknownShown.includes(">未知<"), "repeat 与 focus 的 null 保持未知语义");

/** 5. notes 完整性：修改位置、原值、新值、理由、训练事实来源、删除说明与特殊字符 */
for (const text of [
  "重量由 75 kg 调整为 80 kg",
  "依据 2026-09-28 与 2026-10-05 两次记录",
  "组间休息由 150 秒延长为 180 秒",
  "本次仅修改推日第 1 个动作的重量与组间休息，其余动作与训练日保持原值。",
  "凳上臂屈伸已删除",
  "家中无该器械",
  "肩部不适时立即停止卧推并就医评估。",
])
  assert.ok(shown.includes(text), `notes 展示缺少：${text}`);
assert.ok(shown.includes(dayNotes) && shown.includes(planNotes), "多行 notes 按原文逐行展示");
assert.equal(
  shown.split("whitespace-pre-wrap").length - 1,
  4,
  "三个训练日与整体说明段落保留换行排版",
);
const special = renderPlan({
  ...adjustmentPayload,
  notes: '删除 "凳上臂屈伸" <arc_press> 并用 5 & 3 组说明。',
});
assert.ok(
  special.includes("&lt;arc_press&gt;") &&
    special.includes("&amp;") &&
    !special.includes("<arc_press>"),
  "特殊字符按组件既有安全方式转义",
);

/** 6. 字段级建议来源：精确路径、祖先路径、位置区分与用户现状保持原来源 */
const fieldBadges = (fields) =>
  withSuggested(fields).split(">建议<").length - 1;
assert.equal(fieldBadges([]), 0, "没有建议字段时不出现建议标记");
assert.equal(fieldBadges(["/days/0/exercises/0/name"]), 1, "动作名的建议来源按位置命中");
assert.equal(fieldBadges(["/days/0/exercises/1/exercise_id"]), 1, "目录 ID 的建议来源按位置命中");
assert.equal(fieldBadges(["/days/0/exercises/1/weight_kg"]), 1, "同名动作的重量来源按位置区分");
assert.equal(fieldBadges(["/days/0/exercises/1"]), 7, "动作对象级来源覆盖其全部数值行");
assert.equal(fieldBadges(["/days/0"]), 24, "训练日祖先路径覆盖其下全部字段");
assert.equal(fieldBadges(["/days/2"]), 3, "不完整训练日的祖先路径覆盖主题与提示");
assert.equal(fieldBadges(["/notes"]), 1, "整体说明的建议来源落在整体提示行");
assert.equal(fieldBadges(["/repeat"]), 1, "循环方式的建议来源命中");
assert.equal(
  fieldBadges(adjustmentPayload.suggested_fields),
  7,
  "调整保留原有建议来源并叠加本次修改",
);
assert.equal(fieldBadges(["/days/0/exercises/1/name"]), 1, "同名动作的另一位置独立承载来源标记");
for (const [text, path] of [
  [">5 组<", "/days/0/exercises/0/sets"],
  [">目标 5 次<", "/days/0/exercises/0/reps"],
  [">时长未知<", "/days/0/exercises/0/duration_seconds"],
  [">重量 80 kg<", "/days/0/exercises/0/weight_kg"],
  [">组间休息 180 秒<", "/days/0/exercises/0/rest_seconds"],
  [">重量口径：杠铃含杆总重<", "/days/0/exercises/0/load_convention"],
])
  assert.ok(
    rowOf(withSuggested([path]), text).includes(">建议<"),
    `${path} 未标记对应字段行`,
  );
assert.ok(!rowOf(shown, ">5 组<").includes(">建议<"), "用户现状字段保持原来源语义");
assert.ok(!rowOf(shown, ">目标 5 次<").includes(">建议<"), "未涉及内容继续保留原来源状态");
assert.ok(!rowOf(shown, ">重量口径：杠铃含杆总重<").includes(">建议<"), "已知口径未修改时不新增建议标记");
const moved = renderPlan({
  ...adjustmentPayload,
  days: [adjustmentPayload.days[1], adjustmentPayload.days[0], adjustmentPayload.days[2]],
  suggested_fields: ["/days/1/exercises/1/rest_seconds"],
});
assert.ok(
  rowOf(moved, ">组间不安排休息<").includes(">建议<"),
  "训练日与动作位置变化后按最终 payload 路径展示来源",
);

/* ===== 实时事件与历史投影 ===== */
const event = (name, data) => ({ event: name, data: { run_id: RUN_ID, ...data } });
const callBlock = (index, callId, name, args) => ({
  content_index: index,
  type: "tool_call",
  tool_call_id: callId,
  name,
  arguments: args,
});
const adjustArguments = {
  base_profile_version: 4,
  base_plan_id: BASE_PLAN_ID,
  payload: adjustmentPayload,
};
const saveArguments = {
  proposal_id: PROPOSAL_ID,
  display_entry_id: RESULT_NODE,
  confirmation_entry_id: CONFIRM_ENTRY,
};
const saveResult = {
  proposal_id: PROPOSAL_ID,
  id: SAVED_PLAN_ID,
  content: adjustmentPayload,
  created_at: CREATED_AT,
  saved_at: CREATED_AT,
};
const saveResultText = JSON.stringify(saveResult);
const statusText = (status, result) =>
  JSON.stringify({ proposal_id: PROPOSAL_ID, status, result });

function roundWithCalls(calls) {
  const content = calls.map((call, index) =>
    callBlock(index, call.id, call.name, call.args),
  );
  let round = { id: RUN_ID, run_id: RUN_ID, entries: [], status: "running" };
  round = reactAgent.applyReActEvent(
    round,
    event("message_start", { message_id: ASSISTANT_ENTRY, content: [] }),
  );
  round = reactAgent.applyReActEvent(
    round,
    event("message_end", {
      message_id: ASSISTANT_ENTRY,
      content,
      stop_reason: "toolUse",
      entry_id: ASSISTANT_ENTRY,
      parent_id: USER_ENTRY,
    }),
  );
  for (const call of calls)
    round = reactAgent.applyReActEvent(
      round,
      event("tool_start", {
        tool_call_id: call.id,
        name: call.name,
        arguments: call.args,
      }),
    );
  return round;
}
const entryOf = (round, callId) =>
  round.entries.find((item) => item.kind === "tool" && item.id === callId);
const adjustCall = () => ({ id: ADJUST_CALL, name: ADJUSTMENT, args: adjustArguments });

/** 7. 实时事件：进度与执行完成不形成展示，持久化结果才绑定完整调整内容 */
let round = roundWithCalls([adjustCall()]);
const started = entryOf(round, ADJUST_CALL);
assert.equal(started.name, ADJUSTMENT, "tool_start 建立工具名称与调用身份");
assert.deepEqual(started.arguments, adjustArguments, "调用参数保留");
assert.equal(started.status, "running");
assert.equal(started.plan, undefined);
round = reactAgent.applyReActEvent(
  round,
  event("tool_execution_update", {
    tool_call_id: ADJUST_CALL,
    tool_name: ADJUSTMENT,
    content: "正在核对最近 10 条训练记录。",
    is_error: false,
  }),
);
assert.equal(entryOf(round, ADJUST_CALL).content, "正在核对最近 10 条训练记录。", "中间进度快照完整替换");
assert.equal(entryOf(round, ADJUST_CALL).plan, undefined, "进度不形成待确认展示");
assert.equal(entryOf(round, ADJUST_CALL).status, "running", "进度阶段保持执行中");
round = reactAgent.applyReActEvent(
  round,
  event("tool_execution_end", {
    tool_call_id: ADJUST_CALL,
    tool_name: ADJUSTMENT,
    content: adjustmentText,
    is_error: false,
  }),
);
assert.equal(entryOf(round, ADJUST_CALL).status, "completed", "执行完成定稿结果");
assert.equal(entryOf(round, ADJUST_CALL).plan, undefined, "执行完成尚未持久化不形成展示");
assert.equal(round.entries.length, 2, "同一调用不新增条目");
round = reactAgent.applyReActEvent(
  round,
  event("tool_result", {
    tool_call_id: ADJUST_CALL,
    content: adjustmentText,
    is_error: false,
    entry_id: RESULT_NODE,
    parent_id: ASSISTANT_ENTRY,
  }),
);
const liveTool = entryOf(round, ADJUST_CALL);
assert.deepEqual(liveTool.plan, adjustmentProposal, "准备结果完整恢复为调整计划展示对象");
assert.deepEqual(liveTool.plan.payload, adjustmentPayload, "完整 payload 与数组顺序保留");
assert.deepEqual(liveTool.plan.payload.suggested_fields, adjustmentPayload.suggested_fields);
assert.equal(liveTool.entry_id, RESULT_NODE, "结果节点身份即展示节点身份");
assert.equal(liveTool.parent_id, ASSISTANT_ENTRY, "结果节点的父节点身份保留");
assert.equal(round.entries.length, 2, "调整展示合并进同一调用");
assert.throws(
  () =>
    reactAgent.applyReActEvent(
      round,
      event("tool_result", {
        tool_call_id: ADJUST_CALL,
        content: adjustmentText,
        is_error: false,
        entry_id: u(0xcccccccc),
        parent_id: ASSISTANT_ENTRY,
      }),
    ),
  /重复返回/,
  "同一调用不重复持久化",
);

/** 8. 并行调用各自绑定身份，仅调整准备结果形成展示 */
const recentWorkouts = JSON.stringify({ items: [], page: 1, page_size: 10, total: 0 });
let parallel = roundWithCalls([
  adjustCall(),
  {
    id: WORKOUTS_CALL,
    name: "list_workouts",
    args: { date_from: null, date_to: "2026-10-09", page: 1, page_size: 10 },
  },
]);
parallel = reactAgent.applyReActEvent(
  parallel,
  event("tool_result", {
    tool_call_id: WORKOUTS_CALL,
    content: recentWorkouts,
    is_error: false,
    entry_id: u(0xdddddddd),
    parent_id: ASSISTANT_ENTRY,
  }),
);
assert.equal(entryOf(parallel, WORKOUTS_CALL).plan, undefined, "训练记录查询不形成计划展示");
parallel = reactAgent.applyReActEvent(
  parallel,
  event("tool_result", {
    tool_call_id: ADJUST_CALL,
    content: adjustmentText,
    is_error: false,
    entry_id: RESULT_NODE,
    parent_id: ASSISTANT_ENTRY,
  }),
);
assert.deepEqual(entryOf(parallel, ADJUST_CALL).plan, adjustmentProposal, "后到的调整结果仍绑定原调用");
assert.equal(entryOf(parallel, WORKOUTS_CALL).plan, undefined, "查询调用身份不被调整结果改写");

/** 9. 错误结果：业务错误对象严格校验，harness 文本原样保留 */
const withResult = (value, content, isError) =>
  reactAgent.applyReActEvent(
    value,
    event("tool_result", {
      tool_call_id: ADJUST_CALL,
      content,
      is_error: isError,
      entry_id: RESULT_NODE,
      parent_id: ASSISTANT_ENTRY,
    }),
  );
for (const [label, body] of [
  ["快照失效", { code: "plan_proposal_invalidated", message: "待确认快照已失效。" }],
  ["版本冲突", { code: "plan_version_conflict", message: "当前计划已变化。" }],
  ["画像版本冲突", { code: "profile_version_conflict", message: "画像版本已变化。" }],
  ["确认非法", { code: "plan_confirmation_invalid", message: "确认绑定非法。" }],
  [
    "内容校验失败",
    {
      code: "invalid_business_payload",
      message: "字段无效。",
      errors: [{ path: "/payload/days/0/exercises/0/sets", message: "组数无效。" }],
    },
  ],
]) {
  const item = entryOf(withResult(roundWithCalls([adjustCall()]), JSON.stringify(body), true), ADJUST_CALL);
  assert.equal(item.status, "failed", `${label} 保持失败状态`);
  assert.equal(item.plan, undefined, `${label} 不展示待确认计划`);
  assert.equal(item.content, JSON.stringify(body), `${label} 保留原始内容`);
  assert.equal(item.entry_id, RESULT_NODE, `${label} 仍绑定真实节点身份`);
}
for (const content of [
  "工具执行失败：payload 缺少 days 字段。",
  "参数预处理失败：base_plan_id 不是合法 UUID。",
  "权限检查失败：该工具未授权。",
  '{"code":"plan_not_found","message":"计划版本不存在。"}\n\n工具后处理失败：超时。',
])
  assert.equal(
    entryOf(withResult(roundWithCalls([adjustCall()]), content, true), ADJUST_CALL).content,
    content,
    "harness 说明文本保持原文进入通用展示",
  );
for (const [label, body] of [
  ["缺 code", { detail: { code: "plan_not_found" } }],
  ["非计划错误码", { code: "workout_not_found", message: "错误码集合不一致。" }],
  ["内容校验错误缺字段列表", { code: "invalid_business_payload", message: "字段无效。" }],
  ["业务错误含未定义字段", { code: "plan_not_found", message: "缺少字段。", errors: [] }],
])
  assert.throws(
    () => withResult(roundWithCalls([adjustCall()]), JSON.stringify(body), true),
    /无效|缺少|含未定义字段/,
    `${label} 在实时解析位置报错`,
  );
for (const [label, broken] of [
  ["含未定义字段", { ...adjustmentProposal, extra: 1 }],
  ["准备类型错误", { ...adjustmentProposal, preparation_kind: "import" }],
  ["依据字符串", { ...adjustmentProposal, base_profile_version: "None" }],
  [
    "休息日含动作",
    {
      ...adjustmentProposal,
      payload: {
        ...adjustmentPayload,
        days: [
          adjustmentPayload.days[0],
          { ...adjustmentPayload.days[1], exercises: [adjustmentPayload.days[0].exercises[0]] },
          adjustmentPayload.days[2],
        ],
      },
    },
  ],
  [
    "重量缺口径",
    {
      ...adjustmentProposal,
      payload: dayAt(0, [
        { ...adjustmentPayload.days[0].exercises[0], load_convention: null },
        ...adjustmentPayload.days[0].exercises.slice(1),
      ]),
    },
  ],
])
  assert.throws(
    () => withResult(roundWithCalls([adjustCall()]), JSON.stringify(broken), false),
    /无效|缺少|含未定义字段|含动作|为空/,
    `${label} 的成功结果在解析位置报错`,
  );

/** 10. 历史恢复：同一 schema 与展示投影，身份与完整内容保留 */
const historyWire = (toolName, content, isError) => ({
  session: {
    session_id: SESSION_ID,
    title: "调整会话",
    active_leaf_id: RESULT_NODE,
    created_at: 1,
    updated_at: 2,
  },
  entries: [
    {
      entry_id: USER_ENTRY,
      parent_id: null,
      run_id: null,
      created_at: 1,
      message: { role: "user", text: "把卧推重量提到 80 kg。", timestamp: 1, attachments: [] },
    },
    {
      entry_id: ASSISTANT_ENTRY,
      parent_id: USER_ENTRY,
      run_id: RUN_ID,
      created_at: 2,
      message: {
        role: "assistant",
        content: [callBlock(0, ADJUST_CALL, ADJUSTMENT, adjustArguments)],
        stop_reason: "toolUse",
        timestamp: 2,
      },
    },
    {
      entry_id: RESULT_NODE,
      parent_id: ASSISTANT_ENTRY,
      run_id: RUN_ID,
      created_at: 3,
      message: {
        role: "toolResult",
        tool_call_id: ADJUST_CALL,
        tool_name: toolName,
        content,
        is_error: isError,
        timestamp: 3,
      },
    },
  ],
  runs: [
    {
      session_id: SESSION_ID,
      run_id: RUN_ID,
      request_entry_id: USER_ENTRY,
      last_entry_id: RESULT_NODE,
      status: "completed",
      started_at: 1,
      finished_at: 4,
      error_code: null,
      error_message: null,
    },
  ],
  steering: [],
});
const historyTool = history.historyToRounds(historyWire(ADJUSTMENT, adjustmentText, false))[0]
  .entries.at(-1);
assert.deepEqual(historyTool.plan, liveTool.plan, "历史与实时使用相同 schema 与展示对象");
assert.equal(historyTool.name, ADJUSTMENT, "历史保留原工具身份");
assert.equal(historyTool.id, ADJUST_CALL, "历史保留调用身份");
assert.deepEqual(historyTool.arguments, adjustArguments, "历史恢复调用依据");
assert.equal(historyTool.entry_id, RESULT_NODE, "历史恢复真实 entry_id");
assert.equal(historyTool.parent_id, ASSISTANT_ENTRY, "历史恢复真实 parent_id");
assert.equal(historyTool.status, "completed");
assert.deepEqual(historyTool.plan.payload.days[0].exercises.map((item) => item.weight_kg), [80, 0, null]);
const invalidatedText = JSON.stringify({
  code: "plan_proposal_invalidated",
  message: "待确认快照已失效。",
});
const historyFailed = history.historyToRounds(historyWire(ADJUSTMENT, invalidatedText, true))[0]
  .entries.at(-1);
assert.equal(historyFailed.status, "failed", "历史错误结果保持失败状态");
assert.equal(historyFailed.plan, undefined, "历史错误结果不形成待确认展示");
assert.equal(historyFailed.content, invalidatedText, "历史保留业务错误原文");
assert.equal(
  history.historyToRounds(historyWire(ADJUSTMENT, "工具执行失败：参数缺少 payload。", true))[0]
    .entries.at(-1).content,
  "工具执行失败：参数缺少 payload。",
  "历史 harness 文本保持原文",
);
for (const [label, body] of [
  ["缺 code", { detail: { code: "plan_not_found" } }],
  ["非计划错误码", { code: "workout_not_found", message: "错误码集合不一致。" }],
  ["内容校验错误缺字段列表", { code: "invalid_business_payload", message: "字段无效。" }],
])
  assert.throws(
    () => history.historyToRounds(historyWire(ADJUSTMENT, JSON.stringify(body), true)),
    /无效|缺少/,
    `历史 ${label} 与实时同在解析位置失败`,
  );
for (const [label, broken] of [
  ["非法 proposal_id", { ...adjustmentProposal, proposal_id: "nope" }],
  ["准备类型 import", { ...adjustmentProposal, preparation_kind: "import" }],
  ["依据 None 字符串", { ...adjustmentProposal, base_profile_version: "None" }],
])
  assert.throws(
    () => history.historyToRounds(historyWire(ADJUSTMENT, JSON.stringify(broken), false)),
    /无效|缺少/,
    `历史 ${label} 属协议异常`,
  );
const merged = history.mergeHistoryRounds(
  [{ id: RUN_ID, run_id: RUN_ID, request_entry_id: USER_ENTRY, entries: round.entries, status: "running" }],
  history.historyToRounds(historyWire(ADJUSTMENT, adjustmentText, false)),
);
const plans = merged[0].entries.filter((item) => item.kind === "tool" && item.plan !== undefined);
assert.equal(plans.length, 1, "历史刷新不重复调整展示");
assert.deepEqual(plans[0].plan, adjustmentProposal, "合并保持完整调整内容");
assert.equal(plans[0].id, ADJUST_CALL, "合并保持原工具调用身份");
assert.equal(plans[0].entry_id, RESULT_NODE, "合并后仍为真实结果节点身份");

/* ===== 保存落定与刷新 ===== */
const resultEvent = (callId, content, isError = false) =>
  event("tool_result", {
    tool_call_id: callId,
    content,
    is_error: isError,
    entry_id: STATUS_NODE,
    parent_id: ASSISTANT_ENTRY,
  });
const savedFor = (callId, name, args, content, isError) =>
  reactAgent.planSaved(resultEvent(callId, content, isError), roundWithCalls([{ id: callId, name, args }]));

/** 11. 保存凭据：仅成功保存或 saved 核实，且快照标识与本次调用参数一致 */
assert.equal(savedFor(SAVE_CALL, "save_plan", saveArguments, saveResultText), true, "调整快照保存成功识别为落定");
assert.equal(savedFor(SAVE_CALL, "save_plan", saveArguments, saveResultText, true), false, "保存失败不落定");
assert.equal(
  reactAgent.planSaved(
    event("tool_execution_end", { tool_call_id: SAVE_CALL, tool_name: "save_plan", content: saveResultText, is_error: false }),
    roundWithCalls([{ id: SAVE_CALL, name: "save_plan", args: saveArguments }]),
  ),
  false,
  "执行完成事件不作为保存凭据",
);
assert.equal(
  reactAgent.planSaved(
    event("tool_execution_update", { tool_call_id: SAVE_CALL, tool_name: "save_plan", content: saveResultText, is_error: false }),
    roundWithCalls([{ id: SAVE_CALL, name: "save_plan", args: saveArguments }]),
  ),
  false,
  "进度事件不作为保存凭据",
);
assert.equal(savedFor(STATUS_CALL, "get_plan_save_status", { proposal_id: PROPOSAL_ID }, statusText("saved", saveResult)), true, "saved 状态核实支持恢复保存状态");
for (const status of ["pending", "processing", "invalidated", "conflicted"])
  assert.equal(
    savedFor(STATUS_CALL, "get_plan_save_status", { proposal_id: PROPOSAL_ID }, statusText(status, null)),
    false,
    `${status} 状态不落定`,
  );
assert.equal(savedFor(ADJUST_CALL, ADJUSTMENT, adjustArguments, adjustmentText), false, "调整准备完成不触发保存");
assert.equal(savedFor(WORKOUTS_CALL, "list_workouts", {}, recentWorkouts), false, "查询类工具结果不误判为已保存");
assert.equal(
  savedFor("tc-query", "get_current_plan", {}, JSON.stringify({ id: SAVED_PLAN_ID, content: adjustmentPayload })),
  false,
  "当前计划查询不作保存凭据",
);
assert.equal(
  savedFor(STATUS_CALL, "get_plan_save_status", { proposal_id: OTHER_PROPOSAL_ID }, statusText("saved", saveResult)),
  false,
  "旧快照标识与当前调用参数不一致不作凭据",
);
assert.equal(
  reactAgent.planSaved(
    resultEvent(SAVE_CALL, saveResultText),
    roundWithCalls([
      adjustCall(),
      { id: SAVE_CALL, name: "save_plan", args: { ...saveArguments, proposal_id: OTHER_PROPOSAL_ID } },
    ]),
  ),
  false,
  "另一快照的保存结果不误关联为当前调用的保存",
);
assert.equal(
  reactAgent.planSaved(resultEvent(SAVE_CALL, saveResultText), roundWithCalls([adjustCall()])),
  false,
  "未知调用身份不作保存凭据",
);
assert.throws(() => savedFor(SAVE_CALL, "save_plan", saveArguments, statusText("saved", null)), /计划保存结果/, "保存结果不合 schema 就地失败");
assert.throws(
  () => savedFor(SAVE_CALL, "save_plan", saveArguments, JSON.stringify({ ...saveResult, is_current: true })),
  /含未定义字段/,
  "固定结果携带动态当前标记属协议异常",
);
assert.throws(
  () => savedFor(SAVE_CALL, "save_plan", saveArguments, JSON.stringify({ ...saveResult, created_at: CREATED_AT + 1 })),
  /不一致/,
  "保存时间与创建时间必须一致",
);
assert.deepEqual(business.parsePlanSaveResult(saveResult).content, adjustmentPayload, "固定保存结果与调整 payload 完整一致");

/** 12. 真实查询缓存：仅保存落定使计划查询失效，其它业务查询保持 */
const planKeys = [
  [...PLAN_QUERY_KEY, "current"],
  [...PLAN_QUERY_KEY, "list"],
  [...PLAN_QUERY_KEY, "record", SAVED_PLAN_ID],
];
const otherKeys = [["profile"], ["workout", "list"], ["plan_saved"]];
const seed = () => {
  queryClient.getQueryCache().clear();
  for (const key of [...planKeys, ...otherKeys]) queryClient.setQueryData(key, { seeded: true });
};
const staleOf = (key) =>
  queryClient.getQueryCache().find({ queryKey: key }).isStale();
const applySaveRefresh = (callId, name, args, content, isError) => {
  const executed = roundWithCalls([{ id: callId, name, args }]);
  if (reactAgent.planSaved(resultEvent(callId, content, isError), executed))
    queryClient.invalidateQueries({ queryKey: PLAN_QUERY_KEY });
};
seed();
applySaveRefresh(SAVE_CALL, "save_plan", saveArguments, saveResultText, true);
for (const key of [...planKeys, ...otherKeys])
  assert.equal(staleOf(key), false, `${key.join("/")} 在保存失败后保持原状态`);
seed();
applySaveRefresh(STATUS_CALL, "get_plan_save_status", { proposal_id: PROPOSAL_ID }, statusText("processing", null));
for (const key of planKeys)
  assert.equal(staleOf(key), false, `${key.join("/")} 在结果未落定时不失效`);
seed();
applySaveRefresh(SAVE_CALL, "save_plan", saveArguments, saveResultText);
for (const key of planKeys)
  assert.equal(staleOf(key), true, `${key.join("/")} 随保存落定失效`);
for (const key of otherKeys)
  assert.equal(staleOf(key), false, `${key.join("/")} 不受计划刷新影响`);
queryClient.clear();

/** 13. 计划页查询与展示：失败态、空状态、版本顺序与当前标记、保存后刷新（契约样例传输层） */
const previousContent = { ...adjustmentPayload, notes: "上一版本的整体说明。" };
const previousRecord = {
  id: BASE_PLAN_ID,
  is_current: true,
  created_at: 1780000000000,
  content: previousContent,
};
const savedRecord = {
  id: SAVED_PLAN_ID,
  is_current: true,
  created_at: CREATED_AT,
  content: adjustmentPayload,
};
const scenario = {
  current: { status: 200, body: { id: BASE_PLAN_ID, content: previousContent } },
  list: [previousRecord],
  records: new Map([
    [BASE_PLAN_ID, previousRecord],
    [SAVED_PLAN_ID, savedRecord],
  ]),
};
const stub = createHttpServer((request, response) => {
  const path = new URL(request.url, "http://127.0.0.1").pathname;
  const send = (status, body) => {
    response.writeHead(status, {
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
    });
    response.end(JSON.stringify(body));
  };
  if (path === "/api/plans/current") return send(scenario.current.status, scenario.current.body);
  if (path === "/api/plans") return send(200, scenario.list);
  const record = scenario.records.get(path.slice("/api/plans/".length));
  return record
    ? send(200, record)
    : send(404, { detail: { code: "plan_not_found", message: "计划版本不存在。" } });
});
await new Promise((resolve) => stub.listen(0, "127.0.0.1", resolve));
const stubOrigin = `http://127.0.0.1:${stub.address().port}`;
const nativeFetch = globalThis.fetch;
globalThis.fetch = (input, init) => nativeFetch(new URL(String(input), stubOrigin), init);

const observers = [
  { key: [...PLAN_QUERY_KEY, "current"], queryFn: ({ signal }) => api.getCurrentPlan(signal) },
  { key: [...PLAN_QUERY_KEY, "list"], queryFn: ({ signal }) => api.listPlans(signal) },
  { key: [...PLAN_QUERY_KEY, "record", SAVED_PLAN_ID], queryFn: ({ signal }) => api.getPlan(SAVED_PLAN_ID, signal) },
].map((spec) => ({
  key: spec.key,
  observer: new QueryObserver(queryClient, { queryKey: spec.key, queryFn: spec.queryFn }),
}));
const unsubscribe = observers.map((item) => item.observer.subscribe(() => {}));
const settle = async () => {
  for (let attempt = 0; attempt < 800; attempt += 1) {
    const done = observers.every(({ observer }) => {
      const result = observer.getCurrentResult();
      return !result.isFetching && (result.data !== undefined || result.error !== null);
    });
    if (done) return;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.fail("计划查询未收敛");
};
const renderPage = () =>
  renderToStaticMarkup(
    createElement(QueryClientProvider, { client: queryClient }, createElement(PlansPage)),
  );
const refreshPlans = async () => {
  await queryClient.invalidateQueries({ queryKey: PLAN_QUERY_KEY });
  await settle();
};

scenario.current = { status: 200, body: { id: BASE_PLAN_ID, content: previousContent } };
scenario.list = [previousRecord];
await refreshPlans();
let page = renderPage();
assert.ok(page.includes(">推<") && page.includes("上一版本的整体说明。"), "当前计划按实时查询完整展示");
assert.ok(page.includes(formatTimestamp(previousRecord.created_at)), "版本列表沿用后端顺序");
assert.equal(page.split(">当前<").length - 1, 1, "当前标记来自实时 is_current");
assert.ok(page.includes("查看完整内容"), "已保存版本提供完整内容入口");
assert.equal(page.split(">第 1 天 · 训练日<").length - 1, 1, "未展开版本不重复渲染计划字段");

scenario.current = { status: 200, body: { id: null, content: null } };
scenario.list = [];
await refreshPlans();
page = renderPage();
assert.ok(page.includes("没有当前计划。"), "空状态按既有逻辑呈现");
assert.ok(page.includes("没有已保存的计划版本。"), "无版本时列表为空状态");

/** 重取失败：查询失败态与空状态分别呈现，已加载内容按既有逻辑保留 */
scenario.current = { status: 200, body: { id: BASE_PLAN_ID, content: previousContent } };
scenario.list = [previousRecord];
await refreshPlans();
scenario.current = { status: 500, body: { detail: { code: "internal_error", message: "当前计划查询失败。" } } };
await refreshPlans();
page = renderPage();
assert.ok(page.includes('role="alert"') && page.includes("当前计划查询失败。"), "查询失败态按既有逻辑呈现");
assert.ok(page.includes("上一版本的整体说明。"), "失败态保留已加载的当前计划内容");
assert.ok(!page.includes("没有当前计划。"), "查询失败不误显示为空状态");
assert.equal(observers[0].observer.getCurrentResult().isError, true, "计划页读取的查询结果保持错误态");

/** 保存落定驱动的刷新：与 sessionRunManager 相同的判定与失效调用 */
const persistedAdjust = reactAgent.applyReActEvent(
  roundWithCalls([adjustCall()]),
  resultEvent(ADJUST_CALL, adjustmentText),
);
assert.deepEqual(entryOf(persistedAdjust, ADJUST_CALL).plan, adjustmentProposal, "待保存调整内容来自真实持久化结果");
scenario.current = { status: 200, body: { id: SAVED_PLAN_ID, content: adjustmentPayload } };
scenario.list = [savedRecord, { ...previousRecord, is_current: false }];
assert.equal(
  reactAgent.planSaved(
    resultEvent(SAVE_CALL, saveResultText),
    roundWithCalls([adjustCall(), { id: SAVE_CALL, name: "save_plan", args: saveArguments }]),
  ),
  true,
  "保存结果与调整快照标识一致",
);
await queryClient.invalidateQueries({ queryKey: PLAN_QUERY_KEY });
await settle();
page = renderPage();
assert.ok(page.includes(planNotes.split("\n")[0]), "保存后当前计划重取为实时新版本");
assert.ok(!page.includes("上一版本的整体说明。"), "旧当前计划内容不再呈现");
assert.equal(page.split(">当前<").length - 1, 1, "刷新后仅一个当前版本");
assert.ok(
  page.indexOf(formatTimestamp(savedRecord.created_at)) <
    page.indexOf(formatTimestamp(previousRecord.created_at)),
  "版本顺序沿用后端返回顺序",
);
assert.ok(
  page.indexOf(formatTimestamp(savedRecord.created_at)) < page.indexOf(">当前<") &&
    page.indexOf(">当前<") < page.indexOf(formatTimestamp(previousRecord.created_at)),
  "当前标记落在实时标记为当前的版本上",
);
const recordData = observers[2].observer.getCurrentResult().data;
assert.deepEqual(recordData.content, adjustmentPayload, "版本详情查询解析完整调整内容");
assert.ok(
  page.includes(renderToStaticMarkup(createElement(PlanFields, { content: recordData.content }))),
  "当前计划、版本详情与对话待确认展示使用同一组件与同一口径",
);
await assert.rejects(
  () => api.getPlan(u(0xf0f0f0f0)),
  (error) =>
    error instanceof reactAgent.ReActHttpError &&
    error.http_status === 404 &&
    error.code === "plan_not_found",
  "不存在的版本返回 404 plan_not_found",
);
scenario.list = [{ ...previousRecord, is_current: 1 }];
await assert.rejects(() => api.listPlans(), /无效/, "列表响应违反 schema 在解析位置失败");

unsubscribe.forEach((dispose) => dispose());
globalThis.fetch = nativeFetch;
queryClient.clear();
await new Promise((resolve) => stub.close(resolve));

console.log(
  "PASS: 调整结果严格解析、完整计划与 notes 及字段来源渲染、实时与历史同一 schema 与身份、保存落定判定与计划查询刷新全部符合契约",
);
