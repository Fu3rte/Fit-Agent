// 运行：node scripts/profile-display-check.mjs
// 画像展示口径（contract §11.9、PRODUCT.md §3.1–3.2）：个人画像页与对话画像快照共用 ProfileFields，
// 展示文本字段、不可用器械与禁用动作列表。取值按真实组件节点读取：字段以 Field 节点为单位，
// FieldTitle 节点整段文本相等才算命中，取值取该字段其余节点的文本内容，条目按 Badge 节点核对。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const server = await createServer({
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});
const { default: ProfileFields } = await server.ssrLoadModule(
  "/src/features/profile/ProfileFields.tsx",
);
const { Field, FieldTitle } = await server.ssrLoadModule(
  "/src/components/ui/field.tsx",
);
const { Badge } = await server.ssrLoadModule("/src/components/ui/badge.tsx");
await server.close();

/** 展开 map 产生的嵌套数组并保持真实文档顺序；``false``/``null``/``undefined`` 是未渲染的空槽 */
const flatten = (nodes) =>
  nodes.flatMap((item) => (Array.isArray(item) ? flatten(item) : [item]));
const childNodes = (node) =>
  flatten([node.props?.children]).filter(
    (item) => item !== null && item !== undefined && item !== false && item !== "",
  );
/** 节点子树的文本内容，与 DOM ``textContent`` 同口径 */
const textOf = (node) =>
  typeof node === "string" ? node : childNodes(node).map(textOf).join("");

const fieldsOf = (content) =>
  childNodes(ProfileFields({ content }))
    .filter((node) => node.type === Field)
    .map((node) => ({
      node,
      title: childNodes(node).find((item) => item.type === FieldTitle),
    }));
/** 按标题节点命中的唯一字段；取值节点即该字段除标题外的子节点 */
const fieldOf = (content, label) => {
  const found = fieldsOf(content).filter(
    (item) => item.title !== undefined && textOf(item.title) === label,
  );
  assert.equal(found.length, 1, `字段 ${label} 按标题节点唯一命中`);
  return found[0];
};
const valueNodes = (content, label) => {
  const { node, title } = fieldOf(content, label);
  return childNodes(node).filter((item) => item !== title);
};
/** 字段取值的文本内容 */
const fieldValue = (content, label) =>
  valueNodes(content, label).map(textOf).join("");
/** 列表字段的 Badge 条目文本，按节点顺序 */
const badgeItems = (content, label) => {
  const value = valueNodes(content, label);
  assert.equal(value.length, 1, `${label} 的条目由单一节点承载`);
  return childNodes(value[0]).map((item) => {
    assert.equal(item.type, Badge, `${label} 的每个条目渲染为 Badge 节点`);
    return textOf(item);
  });
};

// 展示字段与顺序按节点读取：六个文本字段、不可用器械、禁用动作。
const LABELS = [
  "训练目标",
  "训练经验",
  "训练环境",
  "可用时间与频率",
  "伤病与不适",
  "动作限制",
  "不可用器械",
  "禁用动作",
];

/** 完整画像：文本字段与两个限制列表都有明确取值（§11.9） */
const content = {
  goal: "增肌",
  experience: "力量训练新手",
  environment: "户外",
  availability: "每周三次",
  health_notes: "右肩旧伤",
  movement_restrictions: "避免过顶推举",
  unavailable_equipment: ["barbell"],
  forbidden_exercise_ids: ["0001"],
};

assert.deepEqual(
  fieldsOf(content).map((item) => textOf(item.title)),
  LABELS,
  "画像字段集合与顺序固定为八",
);

// 文本字段有值时展示自身取值，null 保持未知语义。
for (const [label, value] of [
  ["训练目标", "增肌"],
  ["训练经验", "力量训练新手"],
  ["训练环境", "户外"],
  ["可用时间与频率", "每周三次"],
  ["伤病与不适", "右肩旧伤"],
  ["动作限制", "避免过顶推举"],
])
  assert.equal(fieldValue(content, label), value, `${label} 未展示取值`);
assert.equal(
  fieldValue({ ...content, movement_restrictions: null }, "动作限制"),
  "未知",
  "文本字段 null 展示未知",
);

// 不可用器械三种状态各自成立：null→未知、[]→无、非空→按原顺序完整展示实际条目。
assert.equal(
  fieldValue({ ...content, unavailable_equipment: null }, "不可用器械"),
  "未知",
  "不可用器械 null 展示未知",
);
assert.equal(
  fieldValue({ ...content, unavailable_equipment: [] }, "不可用器械"),
  "无",
  "不可用器械空列表展示无",
);
assert.deepEqual(
  badgeItems({ ...content, unavailable_equipment: ["barbell"] }, "不可用器械"),
  ["barbell"],
  "不可用器械单项完整展示",
);
assert.deepEqual(
  badgeItems(
    { ...content, unavailable_equipment: ["barbell", "dumbbell"] },
    "不可用器械",
  ),
  ["barbell", "dumbbell"],
  "不可用器械多项按原顺序完整展示",
);
assert.equal(
  fieldValue(
    { ...content, unavailable_equipment: ["barbell", "dumbbell"] },
    "不可用器械",
  ),
  "barbelldumbbell",
  "不可用器械条目无附加说明",
);
// 条目文本与字段标签同字时仍按标题节点定位，取值不受影响。
assert.deepEqual(
  badgeItems(
    { ...content, unavailable_equipment: ["不可用器械", "barbell"] },
    "不可用器械",
  ),
  ["不可用器械", "barbell"],
  "条目文本含字段标签不影响节点定位",
);

// 禁用动作列表语义未退化。
assert.equal(
  fieldValue({ ...content, forbidden_exercise_ids: null }, "禁用动作"),
  "未知",
  "禁用动作 null 展示未知",
);
assert.equal(
  fieldValue({ ...content, forbidden_exercise_ids: [] }, "禁用动作"),
  "无",
  "禁用动作空列表展示无",
);
assert.deepEqual(
  badgeItems(
    { ...content, forbidden_exercise_ids: ["0001", "0002"] },
    "禁用动作",
  ),
  ["0001", "0002"],
  "禁用动作按原顺序完整展示",
);

// 两份快照各按自身 payload 展示：旧快照保留原有取值，修改后的新快照展示新值。
assert.deepEqual(
  badgeItems(content, "不可用器械"),
  ["barbell"],
  "旧快照保留原有取值",
);
assert.deepEqual(
  badgeItems({ ...content, unavailable_equipment: ["dumbbell"] }, "不可用器械"),
  ["dumbbell"],
  "修改后的新快照展示新值",
);

// 真实组件经 react-dom/server 完成一次服务端渲染。
assert.ok(
  renderToStaticMarkup(createElement(ProfileFields, { content })).length > 0,
  "真实组件完成 SSR 渲染",
);

// 两个展示位点（个人画像页、对话画像快照）共用同一字段集合。
const source = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const profilePage = source("../src/features/profile/ProfilePage.tsx");
const transcript = source("../src/features/chat/components/ChatTranscript.tsx");
assert.match(profilePage, /import ProfileFields from ["'][^"']*ProfileFields["']/);
assert.match(profilePage, /<ProfileFields content=/);
assert.match(transcript, /import ProfileFields from ["'][^"']*ProfileFields["']/);
assert.match(transcript, /<ProfileFields content=/);

console.log(
  "PASS: 画像字段集合与顺序、不可用器械三种状态（null/空/非空）、禁用动作未退化、两处展示位点共用",
);
