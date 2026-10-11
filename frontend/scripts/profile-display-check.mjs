// 运行：node scripts/profile-display-check.mjs
// 画像展示口径（contract §11.9、PRODUCT.md §3.1–3.2）：个人画像页与对话画像快照共用 ProfileFields，
// 展示文本字段与禁用动作列表，不展示不可用器械字段；数据模型与 wire 仍保留该字段。
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
await server.close();

const render = (content) =>
  renderToStaticMarkup(createElement(ProfileFields, { content }));

/** 完整画像：不可用器械有明确取值，用于证明展示层忽略该字段（§11.9）。 */
const content = {
  goal: "增肌",
  experience: "力量训练新手",
  environment: "户外",
  availability: "每周三次",
  health_notes: "无伤病",
  movement_restrictions: null,
  unavailable_equipment: ["barbell"],
  forbidden_exercise_ids: ["0001"],
};
const markup = render(content);

for (const label of [
  "训练目标",
  "训练经验",
  "训练环境",
  "可用时间与频率",
  "伤病与不适",
  "动作限制",
  "禁用动作",
])
  assert.ok(markup.includes(label), `画像缺少字段 ${label}`);

// 不可用器械仅在数据与 wire 中保留，展示层不生成标签与取值。
assert.ok(!markup.includes("不可用器械"), "画像不展示不可用器械标签");
assert.ok(!markup.includes("barbell"), "画像不渲染不可用器械取值");

// 文本字段 null 保持未知语义；限制列表 null 与 [] 分别展示。
assert.ok(render({ ...content, movement_restrictions: null }).includes("未知"), "文本字段 null 展示未知");
assert.ok(render({ ...content, forbidden_exercise_ids: null }).includes("未知"), "限制列表 null 展示未知");
assert.ok(render({ ...content, forbidden_exercise_ids: [] }).includes("无"), "空限制列表展示无");
for (const item of ["0001", "0002"])
  assert.ok(
    render({ ...content, forbidden_exercise_ids: [item] }).includes(item),
    `禁用动作展示真实引用 ${item}`,
  );

// 两个展示位点（个人画像页、对话画像快照）共用同一字段集合。
const source = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const profilePage = source("../src/features/profile/ProfilePage.tsx");
const transcript = source("../src/features/chat/components/ChatTranscript.tsx");
assert.match(profilePage, /import ProfileFields from ["'][^"']*ProfileFields["']/);
assert.match(profilePage, /<ProfileFields content=/);
assert.match(transcript, /import ProfileFields from ["'][^"']*ProfileFields["']/);
assert.match(transcript, /<ProfileFields content=/);

console.log("PASS: 画像展示字段集合、不可用器械隐藏、null/空列表语义与两处展示位点共用");
