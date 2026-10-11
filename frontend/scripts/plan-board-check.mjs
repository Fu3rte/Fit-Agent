import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const catalog = JSON.parse(readFileSync(new URL("../../backend/dataset/actions.agent.json", import.meta.url), "utf8"));
const equipment = Object.fromEntries(catalog.map((exercise) => [exercise.id, exercise.equipment]));
const bodyWeight = catalog.find((exercise) => exercise.equipment === "body weight");
const dumbbell = catalog.find((exercise) => exercise.equipment === "dumbbell" && exercise.load_convention === "per_implement");
assert.ok(bodyWeight && dumbbell);
const server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
const { PlanBoard } = await server.ssrLoadModule("/src/features/plans/PlanFields.tsx");
const { parsePlanContent } = await server.ssrLoadModule("/src/lib/business.ts");
await server.close();

const exercise = (record, values = {}) => ({
  exercise_id: record.id,
  name: record.name,
  sets: null,
  reps: null,
  duration_seconds: null,
  weight_kg: null,
  load_convention: record.load_convention,
  rest_seconds: null,
  ...values,
});
const plan = (item, values = {}) => parsePlanContent({
  repeat: null,
  days: [{ kind: "training", focus: null, exercises: [item], notes: null }],
  notes: null,
  suggested_fields: [],
  ...values,
});
const render = (content) => renderToStaticMarkup(createElement(PlanBoard, { content, equipment }));
const show = (values) => render(plan(exercise(dumbbell, values)));
for (const [values, texts] of [
  [{}, ["自由安排"]],
  [{ sets: 4 }, ["4组"]],
  [{ reps: 8 }, ["8次"]],
  [{ duration_seconds: 30.5 }, ["30.5s"]],
  [{ sets: 4, reps: 8 }, ["4×8"]],
  [{ sets: 4, duration_seconds: 30 }, ["4×30s"]],
  [{ sets: 4, reps: 8, duration_seconds: 30 }, ["4×8", "4×30s"]],
  [{ rest_seconds: 0 }, ["自由安排"]],
  [{ rest_seconds: 90.5, weight_kg: 12.5 }, ["自由安排", "12.5kg（单只器械重量）"]],
  [{ weight_kg: 0 }, ["0kg（单只器械重量）"]],
]) {
  const markup = show(values);
  for (const text of texts) assert.ok(markup.includes(text), `${JSON.stringify(values)} 缺少 ${text}`);
  assert.ok(!markup.includes("未知") && !markup.includes("循环"));
}
for (const rest_seconds of [null, 0, 45, 60, 90.5])
  assert.equal(show({ rest_seconds }), show({}), "组间休息参数不展示");
assert.equal(
  render(plan(exercise(dumbbell, { sets: 4, reps: 8 }), { suggested_fields: ["/days/0", "/notes"] })),
  show({ sets: 4, reps: 8 }),
  "来源元数据不生成标签",
);
assert.ok(!show({}).includes("单只器械重量"), "空重量隐藏已知口径");
const ordinary = render(plan(exercise(bodyWeight)));
assert.ok(!ordinary.includes("kg") && !ordinary.includes("重量"), "普通自重动作省略重量");
for (const [load_convention, label] of [
  ["added_weight", "外加负重（不含体重）"],
  ["assistance_weight", "辅助减重"],
]) {
  const markup = render(plan(exercise(bodyWeight, { weight_kg: 12.5, load_convention })));
  assert.ok(markup.includes(`12.5kg（${label}）`));
}
for (const weight_kg of [null, 0, 5.5]) {
  const markup = render(plan(exercise(dumbbell, { exercise_id: null, name: "用户指定动作", weight_kg })));
  assert.ok(markup.includes("未在动作数据集内核实"));
  assert.equal(markup.includes("kg"), weight_kg !== null);
}
const text = "恢复说明\n保持自然活动";
const notes = "依据保持完整，包括原文中的未知信息。";
const content = plan(exercise(dumbbell), {
  days: [
    { kind: "training", focus: "全身", exercises: [], notes: null },
    { kind: "rest", focus: null, exercises: [], notes: text },
  ],
  notes,
});
const markup = render(content);
assert.equal(markup.split(text).length - 1, 1, "休息日说明只展示一次");
assert.ok(markup.includes(notes), "正文保持原文");
assert.ok(markup.includes("Day 1 · 全身") && markup.includes("Day 2"));
assert.ok(!markup.includes("<ul") && !markup.includes("button"), "空动作容器省略且卡片零交互");
assert.ok(markup.includes("grid-cols-1") && markup.includes("lg:grid-cols-3"));
assert.throws(() => renderToStaticMarkup(createElement(PlanBoard, { content: plan(exercise(dumbbell)), equipment: {} })), /目录缺少/);
console.log("PASS: 计划看板空值、组合参数、小数、零值、自重负荷、目录外动作、休息说明、原始正文与零交互");
