// 运行：node scripts/plan-contract-check.mjs
// 计划 wire 严格校验（plan-generation-contract §2、§3、§4、§5、§8）：契约示例逐字通过、字段集合精确一致、
// 数值与 null 语义、生成快照完整度、当前计划空状态、固定保存结果不变量、业务错误形态（不触达后端）。
import assert from "node:assert/strict";
import { createServer } from "vite";

const server = await createServer({
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});
const business = await server.ssrLoadModule("/src/lib/business.ts");
await server.close();

const clone = (value) => structuredClone(value);
const rejected = (parse, value, message) =>
  assert.throws(
    () => parse(clone(value)),
    /无效|缺少|含未定义字段|不一致|为空|含动作|未知|携带/,
    message,
  );

const PLAN_ID = "22222222-2222-4222-8222-222222222222";
const PROPOSAL_ID = "11111111-1111-4111-8111-111111111111";
const SAVED_AT = 1780272000000;

/** 契约 §9 示例：一个训练日含目录外徒手动作 ＋ 一个休息日，全部字段为建议 */
const example = {
  proposal_id: PROPOSAL_ID,
  base_profile_version: 1,
  base_plan_id: null,
  payload: {
    repeat: true,
    days: [
      {
        kind: "training",
        focus: "全身",
        exercises: [
          {
            exercise_id: null,
            name: "用户指定的徒手动作",
            sets: 3,
            reps: 10,
            duration_seconds: null,
            weight_kg: null,
            load_convention: null,
            rest_seconds: null,
          },
        ],
        notes:
          "动作未在动作数据集内核实。负重动作选择能保持动作规范并完成目标次数的重量。组间可休息3–5分钟，结合心率恢复和自身状态判断。",
      },
      { kind: "rest", focus: null, exercises: [], notes: null },
    ],
    notes: "依据已保存画像安排全身训练与休息交替。最近一周无训练记录。训练中出现不适时停止相关动作。",
    suggested_fields: [
      "/repeat",
      "/days/0/focus",
      "/days/0/exercises/0/sets",
      "/days/0/exercises/0/reps",
      "/days/0/notes",
      "/days/1",
      "/notes",
    ],
  },
};
const content = example.payload;
const record = {
  id: PLAN_ID,
  is_current: true,
  created_at: SAVED_AT,
  content,
};
const saveResult = {
  proposal_id: PROPOSAL_ID,
  id: PLAN_ID,
  content,
  created_at: SAVED_AT,
  saved_at: SAVED_AT,
};

/** 1. 契约示例逐字通过并保持字段与顺序 */
assert.deepEqual(business.parsePlanProposal(example), example, "准备快照示例丢失字段");
assert.deepEqual(business.parsePlanRecord(record), record, "计划版本示例丢失字段");
assert.deepEqual(business.parsePlanList([record]), [record], "计划列表为直接数组");
assert.deepEqual(business.parsePlanList([]), [], "无版本返回空数组");
assert.deepEqual(business.parsePlanSaveResult(saveResult), saveResult, "固定保存结果示例丢失字段");
assert.deepEqual(
  business.parseCurrentPlan({ id: null, content: null }),
  { id: null, content: null },
  "没有当前计划返回 id 与 content 同时为空",
);
assert.deepEqual(business.parseCurrentPlan({ id: PLAN_ID, content }), { id: PLAN_ID, content });

/** 2. 通用结构允许不完整内容，生成快照按完整度拒绝 */
const incomplete = {
  ...content,
  days: [{ kind: "training", focus: "推", exercises: [], notes: null }],
};
assert.deepEqual(business.parsePlanContent(incomplete), incomplete, "通用计划允许训练日动作未知");
assert.deepEqual(
  business.parsePlanContent({ ...content, days: [{ kind: "rest", focus: null, exercises: [], notes: null }] }),
  { ...content, days: [{ kind: "rest", focus: null, exercises: [], notes: null }] },
  "休息日动作列表为空",
);
rejected(business.parsePlanProposal, { ...example, payload: incomplete }, "生成快照拒绝空训练日");
rejected(
  business.parsePlanProposal,
  {
    ...example,
    payload: {
      ...content,
      days: [
        {
          ...content.days[0],
          exercises: [{ ...content.days[0].exercises[0], sets: null }],
        },
      ],
    },
  },
  "生成快照拒绝未知组数",
);
rejected(
  business.parsePlanProposal,
  {
    ...example,
    payload: {
      ...content,
      days: [
        {
          ...content.days[0],
          exercises: [
            { ...content.days[0].exercises[0], reps: null, duration_seconds: null },
          ],
        },
      ],
    },
  },
  "生成快照拒绝次数与时长同时未知",
);

/** 3. 数值口径：正整数、0 与 null 分别处理、重量口径依赖重量 */
const exercise = content.days[0].exercises[0];
const withWeight = {
  exercise_id: "0409",
  name: "杠铃深蹲",
  sets: 5,
  reps: 5,
  duration_seconds: null,
  weight_kg: 100,
  load_convention: "barbell_total",
  rest_seconds: 0,
};
assert.deepEqual(business.parsePlanContent({
  ...content,
  days: [{ kind: "training", focus: null, exercises: [withWeight], notes: null }],
}).days[0].exercises[0], withWeight, "目录 ID 保留前导零，rest_seconds=0 表示不安排组间休息");
assert.deepEqual(business.parsePlanContent({
  ...content,
  days: [{ kind: "training", focus: null, exercises: [{ ...withWeight, weight_kg: 0 }], notes: null }],
}).days[0].exercises[0].weight_kg, 0, "重量 0 保持已知值");
assert.deepEqual(business.parsePlanContent({
  ...content,
  days: [{ kind: "training", focus: null, exercises: [{ ...withWeight, reps: null, duration_seconds: 45, weight_kg: null, load_convention: null }], notes: null }],
}).days[0].exercises[0].duration_seconds, 45, "计时动作用时长表达目标");
assert.deepEqual(business.parsePlanContent({
  ...content,
  days: [{ kind: "training", focus: null, exercises: [{ ...withWeight, weight_kg: null }], notes: null }],
}).days[0].exercises[0].weight_kg, null, "重量为空时允许保留已知口径");

for (const field of ["sets", "reps"])
  for (const bad of ["3", true, 3.5, 0, -1, Number.NaN, Infinity])
    rejected(
      business.parsePlanContent,
      { ...content, days: [{ kind: "training", focus: null, exercises: [{ ...exercise, [field]: bad }], notes: null }] },
      `${field} 拒绝非正整数 ${String(bad)}`,
    );
for (const bad of ["100", true, Number.NaN, -0.5])
  rejected(
    business.parsePlanContent,
    { ...content, days: [{ kind: "training", focus: null, exercises: [{ ...exercise, weight_kg: bad, load_convention: "per_implement" }], notes: null }] },
    `weight_kg 拒绝非数值 ${String(bad)}`,
  );
for (const bad of ["30", -1, Number.NaN])
  rejected(
    business.parsePlanContent,
    { ...content, days: [{ kind: "training", focus: null, exercises: [{ ...exercise, rest_seconds: bad }], notes: null }] },
    `rest_seconds 拒绝非法值 ${String(bad)}`,
  );
rejected(
  business.parsePlanContent,
  { ...content, days: [{ kind: "training", focus: null, exercises: [{ ...exercise, duration_seconds: 0 }], notes: null }] },
  "duration_seconds 拒绝 0",
);
rejected(
  business.parsePlanContent,
  { ...content, days: [{ kind: "training", focus: null, exercises: [{ ...exercise, weight_kg: 60, load_convention: null }], notes: null }] },
  "重量有值缺口径被拒绝",
);
rejected(
  business.parsePlanContent,
  { ...content, days: [{ kind: "training", focus: null, exercises: [{ ...exercise, load_convention: "barbell" }], notes: null }] },
  "未知重量口径被拒绝",
);
rejected(
  business.parsePlanContent,
  { ...content, days: [{ kind: "rest", focus: null, exercises: [exercise], notes: null }] },
  "休息日含动作被拒绝",
);
rejected(business.parsePlanContent, { ...content, days: [] }, "缺少训练日被拒绝");
rejected(business.parsePlanContent, { ...content, repeat: "true" }, "循环方式拒绝字符串");
rejected(business.parsePlanContent, { ...content, suggested_fields: ["days/0"] }, "建议路径非 JSON Pointer");
rejected(business.parsePlanContent, { ...content, suggested_fields: ["/days/0", "  "] }, "建议路径拒绝空白");
rejected(business.parsePlanContent, { ...content, notes: "   " }, "整体提示拒绝空白文本");
rejected(
  business.parsePlanContent,
  { ...content, days: [{ kind: "workout", focus: null, exercises: [], notes: null }] },
  "未知训练日类型被拒绝",
);

/** 4. 字段集合精确一致：缺字段与额外字段均按协议异常 */
rejected(business.parsePlanContent, { days: content.days, notes: null, suggested_fields: [] }, "内容缺 repeat 字段");
rejected(business.parsePlanContent, { ...content, extra: 1 }, "内容含未定义字段");
rejected(business.parsePlanContent, { ...content, days: [{ kind: "rest", focus: null, exercises: [], notes: null, scheduled_on: "2026-06-01" }] }, "训练日沿用旧排程字段");
rejected(business.parsePlanContent, { ...content, days: [{ kind: "rest", focus: null, notes: null }] }, "训练日缺动作列表");
rejected(business.parsePlanProposal, { ...example, payload: { ...content, days: [{ ...content.days[0], exercises: [{ ...exercise, set_no: 1 }] }] } }, "动作含未定义字段");
rejected(business.parsePlanProposal, { ...example, proposal_id: "11111111111111111111111111111111" }, "非法 UUID");
rejected(business.parsePlanProposal, { ...example, base_profile_version: 0 }, "依据画像版本非正整数");
rejected(business.parsePlanProposal, { ...example, base_profile_version: null }, "生成快照必须已建档");
rejected(business.parsePlanProposal, { ...example, base_plan_id: "not-a-uuid" }, "依据计划 ID 非法");
assert.deepEqual(
  business.parsePlanProposal({ ...example, base_plan_id: PLAN_ID }),
  { ...example, base_plan_id: PLAN_ID },
  "已有当前计划时依据计划 ID 原值保留",
);

/** 5. 入参解析与保存不变量 */
assert.deepEqual(
  business.parsePlanProposalArguments({
    base_profile_version: 1,
    base_plan_id: null,
    payload: content,
  }),
  { base_profile_version: 1, base_plan_id: null, payload: content },
);
assert.deepEqual(business.parsePlanGetArguments({ plan_id: PLAN_ID }), { plan_id: PLAN_ID });
assert.deepEqual(
  business.parsePlanSaveArguments({
    proposal_id: PROPOSAL_ID,
    display_entry_id: "33333333-3333-4333-8333-333333333333",
    confirmation_entry_id: "44444444-4444-4444-8444-444444444444",
  }).proposal_id,
  PROPOSAL_ID,
);
assert.deepEqual(business.parsePlanStatusArguments({ proposal_id: PROPOSAL_ID }), { proposal_id: PROPOSAL_ID });
rejected(business.parsePlanStatusArguments, { proposal_id: PROPOSAL_ID, extra: 1 }, "状态查询输入含未定义字段");
rejected(business.parsePlanSaveResult, { ...saveResult, saved_at: SAVED_AT + 1 }, "保存时间不一致");
rejected(business.parsePlanSaveResult, { ...saveResult, is_current: true }, "固定结果不携带动态当前标记");
rejected(business.parsePlanRecord, { ...record, is_current: 1 }, "当前标记拒绝非布尔值");
rejected(business.parsePlanRecord, { ...record, created_at: "1780272000000" }, "时间戳拒绝字符串");
rejected(business.parsePlanList, { plans: [record] }, "列表沿用旧包装对象");
rejected(business.parseCurrentPlan, { id: null, content }, "当前计划身份与内容不同步");
rejected(business.parseCurrentPlan, { id: PLAN_ID, content: null }, "当前计划身份与内容不同步");

/** 6. 状态查询：仅 saved 携带完整固定结果，快照标识必须一致 */
assert.deepEqual(
  business.parsePlanStatusResult({ proposal_id: PROPOSAL_ID, status: "pending", result: null }),
  { proposal_id: PROPOSAL_ID, status: "pending", result: null },
);
assert.deepEqual(business.parsePlanStatusResult({ proposal_id: PROPOSAL_ID, status: "saved", result: saveResult }).result, saveResult);
for (const status of ["pending", "processing", "invalidated", "conflicted"])
  rejected(
    business.parsePlanStatusResult,
    { proposal_id: PROPOSAL_ID, status, result: saveResult },
    `${status} 状态携带保存结果被拒绝`,
  );
rejected(business.parsePlanStatusResult, { proposal_id: PROPOSAL_ID, status: "saved", result: null }, "saved 缺少固定结果");
rejected(
  business.parsePlanStatusResult,
  { proposal_id: PROPOSAL_ID, status: "saved", result: { ...saveResult, proposal_id: "55555555-5555-4555-8555-555555555555" } },
  "固定结果快照标识不一致",
);
rejected(business.parsePlanStatusResult, { proposal_id: PROPOSAL_ID, status: "done", result: null }, "未知状态被拒绝");

/** 7. 业务错误：仅 invalid_business_payload 携带字段错误 */
assert.deepEqual(
  business.parsePlanBusinessError({ code: "plan_not_found", message: "计划版本不存在。" }),
  { code: "plan_not_found", message: "计划版本不存在。" },
);
assert.deepEqual(
  business.parsePlanBusinessError({
    code: "invalid_business_payload",
    message: "字段无效。",
    errors: [{ path: "payload.days.0.exercises.0.sets", message: "组数无效。" }],
  }).errors[0].path,
  "payload.days.0.exercises.0.sets",
);
for (const code of [
  "plan_proposal_not_found",
  "plan_proposal_invalidated",
  "plan_save_processing",
  "plan_version_conflict",
  "profile_required",
  "profile_version_conflict",
  "plan_confirmation_invalid",
  "plan_access_denied",
  "session_not_found",
])
  assert.equal(business.parsePlanBusinessError({ code, message: "业务失败。" }).code, code, `${code} 未被接受`);
rejected(business.parsePlanBusinessError, { code: "workout_not_found", message: "错误码集合不一致。" }, "非计划错误码被拒绝");
rejected(business.parsePlanBusinessError, { code: "plan_not_found", message: "缺少字段。", errors: [] }, "非内容校验错误携带字段列表");
rejected(business.parsePlanBusinessError, { code: "invalid_business_payload", message: "字段无效。" }, "内容校验错误缺少字段列表");
rejected(business.parsePlanBusinessError, { code: "plan_not_found", message: "字段无效。", detail: {} }, "业务错误含未定义字段");

console.log(
  "PASS: 计划 wire 字段集合、数值与 null 语义、生成完整度、空状态、保存不变量与业务错误形态全部符合契约",
);
