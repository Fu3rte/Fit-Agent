// 运行：node scripts/plan-attachment-check.mjs
// 附件录入与调整前端专项：扩展名与严格 UTF-8、100000 原始字节合计边界、纯文件输入与标题、
// 上传/引用序列化、编辑保留移除替换、账本原值重放、Steering 附件、历史附件投影、
// prepare_plan_import 与 prepare_plan_adjustment 结果校验（不触达后端）。
import assert from "node:assert/strict";
import { createServer } from "vite";

const server = await createServer({
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});
const business = await server.ssrLoadModule("/src/lib/business.ts");
const api = await server.ssrLoadModule("/src/lib/api.ts");
const files = await server.ssrLoadModule("/src/features/chat/utils/attachments.ts");
const reactAgent = await server.ssrLoadModule(
  "/src/features/chat/utils/reactAgent.ts",
);
const history = await server.ssrLoadModule(
  "/src/features/chat/utils/sessionHistory.ts",
);
await server.close();

const LIMIT = business.ATTACHMENT_TOTAL_BYTES;
assert.equal(LIMIT, 100_000, "原始字节合计上限为契约取值");

const u = (n) =>
  `${n.toString(16).padStart(8, "0")}-0000-4000-8000-000000000000`;
const sessionId = u(1);
const file = (name, content) => new File([content], name);
const body = (draft) => draft.input;
const decodeBase64 = (value) =>
  Uint8Array.from(atob(value), (character) => character.charCodeAt(0));

/** 1. 格式识别与严格 UTF-8：扩展名忽略大小写，非法编码与路径形式在读取位置报错 */
assert.equal(business.hasAttachmentExtension("推腿计划.MD"), true);
assert.equal(business.hasAttachmentExtension("plan.Txt"), true);
assert.equal(business.hasAttachmentExtension("plan.markdown"), false);
assert.equal(business.hasAttachmentExtension("no-extension"), false);
assert.throws(() => business.requireAttachmentName("a/b.md", "文件名"), /路径/);
assert.throws(() => business.requireAttachmentName("a\\b.md", "文件名"), /路径/);
assert.throws(() => business.requireAttachmentName("..\\", "文件名"), /路径/);
assert.throws(() => business.requireAttachmentName("C:plan.md", "文件名"), /路径/);
assert.throws(() => business.requireAttachmentName("  ", "文件名"), /无效/);
assert.throws(
  () => business.requireAttachmentName(null, "文件名"),
  /无效/,
);
assert.throws(
  () => business.requireAttachmentName("a\0b.md", "文件名"),
  /路径/,
);

const accepted = await files.appendAttachmentFiles([], [
  file("push.md", "# 推\n\n卧推 3×8"),
  file("PULL.TXT", "硬拉 5×5"),
]);
assert.equal(accepted.length, 2, "一条消息支持多个文件");
assert.deepEqual(
  [...decodeBase64(body(accepted[0]).data_base64)],
  [...new TextEncoder().encode("# 推\n\n卧推 3×8")],
  "原始字节保持完整，包括多字节字符与换行",
);
assert.equal(accepted[1].size_bytes, 11, "size_bytes 为原始字节数而非字符数");
await assert.rejects(
  files.appendAttachmentFiles([], [file("plan.docx", "内容")]),
  /\.md.*\.txt/,
  "扩展名不符明确报错",
);
await assert.rejects(
  files.appendAttachmentFiles([], [file("bad.txt", new Uint8Array([0xff, 0xfe, 0x41]))]),
  /UTF-8/,
  "非法 UTF-8 明确报错",
);
// BOM 属于合法 UTF-8，保持原字节不做静默转换
const withBom = await files.appendAttachmentFiles([], [
  file("bom.md", new Uint8Array([0xef, 0xbb, 0xbf, 0x41])),
]);
assert.deepEqual(
  [...decodeBase64(body(withBom[0]).data_base64)],
  [0xef, 0xbb, 0xbf, 0x41],
  "BOM 原样保留",
);

/** 2. 合计字节边界：恰好达上限受理，超限拒绝且原草稿保持不变，禁止截断 */
const exactlyLimit = await files.appendAttachmentFiles([], [
  file("big.md", "a".repeat(LIMIT)),
]);
assert.equal(exactlyLimit[0].size_bytes, LIMIT, "合计等于上限受理");
await assert.rejects(
  files.appendAttachmentFiles([], [file("big.md", "a".repeat(LIMIT + 1))]),
  /超过 100000 字节/,
  "超限拒绝",
);
const firstHalf = await files.appendAttachmentFiles([], [
  file("a.md", "a".repeat(60_000)),
]);
await assert.rejects(
  files.appendAttachmentFiles(firstHalf, [file("b.md", "b".repeat(50_000))]),
  /超过 100000 字节/,
  "多文件合计超限拒绝",
);
assert.equal(firstHalf.length, 1, "拒绝后原附件集合不变");
const secondHalf = await files.appendAttachmentFiles(firstHalf, [
  file("b.md", "b".repeat(40_000)),
]);
assert.equal(
  files.attachmentBytes(secondHalf),
  LIMIT,
  "追加后合计按保留项与新增项共同计算",
);

/** 3. 纯文件输入与序列化：空文本要求附件非空，集合为空时拒绝 */
assert.equal(reactAgent.validMessageInput("", accepted), true);
assert.equal(reactAgent.validMessageInput("   ", accepted), true);
assert.equal(reactAgent.validMessageInput("普通文字", accepted), true);
assert.equal(reactAgent.validMessageInput("", []), false);
assert.equal(reactAgent.validMessageInput("   ", []), false);
assert.equal(reactAgent.validMessageInput("字".repeat(32_000), accepted), true);
assert.equal(reactAgent.validMessageInput("字".repeat(32_001), accepted), false);
assert.deepEqual(files.attachmentInputs([]), [], "无附件提交空集合");
assert.deepEqual(
  files.attachmentInputs(accepted).map((item) => item.kind),
  ["upload", "upload"],
  "顺序即提交顺序",
);
assert.deepEqual(
  files.attachmentInputs(accepted).map((item) => item.attachment_id),
  accepted.map((item) => item.attachment_id),
  "新附件 ID 稳定沿用",
);
for (const input of files.attachmentInputs(accepted))
  assert.deepEqual(business.parseAttachmentInputWire(input), input, "上传输入符合附件 wire");

/** 4. 会话标题：文本有非空白内容时沿用原文，纯文件消息按附件顺序以换行连接文件名 */
assert.equal(reactAgent.canSubmitChatInput("录入这份计划", accepted, []), true);
assert.equal(
  files.draftSessionTitle("  带空白原文  ", accepted),
  "  带空白原文  ",
  "标题沿用原文本并保留首尾空白",
);
assert.equal(
  files.draftSessionTitle("", accepted),
  "push.md\nPULL.TXT",
  "纯文件消息标题按附件顺序连接文件名",
);

/** 5. 编辑默认保留、移除与替换：保留项引用、替换项上传、全部移除为空集合 */
const stored = [
  {
    attachment_id: accepted[0].attachment_id,
    file_name: accepted[0].file_name,
    size_bytes: accepted[0].size_bytes,
    created_at: 1_780_000_000_000,
  },
  {
    attachment_id: accepted[1].attachment_id,
    file_name: accepted[1].file_name,
    size_bytes: accepted[1].size_bytes,
    created_at: 1_780_000_000_001,
  },
];
for (const wire of stored)
  assert.deepEqual(business.parseAttachmentWire(wire), wire, "附件元数据完整解析");
const retained = files.retainedAttachmentDrafts(stored);
assert.deepEqual(
  files.attachmentInputs(retained),
  stored.map((wire) => ({ kind: "reference", attachment_id: wire.attachment_id })),
  "保留项提交引用输入",
);
assert.equal(
  files.attachmentBytes(retained),
  accepted[0].size_bytes + accepted[1].size_bytes,
  "保留项计入合计字节",
);
const removed = retained.filter((_, at) => at !== 0);
assert.deepEqual(files.attachmentInputs(removed), [
  { kind: "reference", attachment_id: stored[1].attachment_id },
]);
const replaced = await files.appendAttachmentFiles(removed, [
  file("pull-v2.md", "引体向上 4×6"),
]);
assert.deepEqual(
  files.attachmentInputs(replaced),
  [
    { kind: "reference", attachment_id: stored[1].attachment_id },
    {
      kind: "upload",
      attachment_id: replaced[1].attachment_id,
      file_name: "pull-v2.md",
      data_base64: body(replaced[1]).data_base64,
    },
  ],
  "替换项使用新附件 ID 上传，原文件保持引用",
);
assert.equal(files.attachmentInputs([]).length, 0, "全部移除提交空集合");
assert.throws(
  () =>
    business.parseAttachmentInputList([
      { kind: "reference", attachment_id: stored[0].attachment_id },
      { kind: "reference", attachment_id: stored[0].attachment_id },
    ]),
  /重复/,
  "同一消息附件 ID 不得重复",
);

/** 6. 账本保留完整请求：重试序列与原值一致，字段异常就地报错 */
const replayed = replaced.map((draft) =>
  files.fromAttachmentDraft(JSON.parse(JSON.stringify(draft))),
);
assert.deepEqual(
  files.attachmentInputs(replayed),
  files.attachmentInputs(replaced),
  "显式重试沿用原附件 ID、顺序与字节内容",
);
assert.throws(
  () => files.fromAttachmentDraft({ ...replayed[0], attachment_id: u(9) }),
  /身份不一致/,
);
assert.throws(
  () =>
    files.fromAttachmentDraft({
      attachment_id: "not-a-uuid",
      file_name: "a.md",
      size_bytes: 1,
      input: { kind: "reference", attachment_id: "not-a-uuid" },
    }),
  /身份/,
);
assert.throws(
  () =>
    files.fromAttachmentDraft({
      attachment_id: stored[0].attachment_id,
      file_name: "a.md",
      size_bytes: 1,
      input: {
        kind: "upload",
        attachment_id: stored[0].attachment_id,
        file_name: "a.md",
        data_base64: "###",
      },
    }),
  /无效/,
  "非法 Base64 在账本解析位置拒绝",
);

/** 7. 历史与运行查询的附件投影：所有用户消息与输入返回有序集合，缺字段属协议异常 */
const attachment = {
  attachment_id: stored[0].attachment_id,
  file_name: "push.md",
  size_bytes: 16,
  created_at: 1_780_000_000_000,
};
const userEntry = u(2);
const assistantEntry = u(3);
const steerId = u(4);
const steerEntry = u(5);
const runId = u(6);
const baseHistory = () => ({
  session: {
    session_id: sessionId,
    title: "会话",
    active_leaf_id: steerEntry,
    created_at: 1,
    updated_at: 2,
  },
  entries: [
    {
      entry_id: userEntry,
      parent_id: null,
      run_id: null,
      created_at: 1,
      message: {
        role: "user",
        text: "",
        timestamp: 1,
        attachments: [attachment],
      },
    },
    {
      entry_id: assistantEntry,
      parent_id: userEntry,
      run_id: runId,
      created_at: 2,
      message: {
        role: "assistant",
        content: [{ content_index: 0, type: "text", text: "已读取。" }],
        stop_reason: "stop",
        timestamp: 2,
      },
    },
    {
      entry_id: steerEntry,
      parent_id: assistantEntry,
      run_id: runId,
      created_at: 3,
      message: { role: "user", text: "再调整一次", timestamp: 3, attachments: [] },
    },
  ],
  runs: [
    {
      session_id: sessionId,
      run_id: runId,
      request_entry_id: userEntry,
      last_entry_id: steerEntry,
      status: "completed",
      started_at: 2,
      finished_at: 3,
      error_code: null,
      error_message: null,
    },
  ],
  steering: [
    {
      session_id: sessionId,
      run_id: runId,
      steering_id: steerId,
      text: "",
      attachments: [attachment],
      timestamp: 3,
      status: "withdrawn",
      entry_id: null,
      reason: null,
      created_at: 3,
      updated_at: 3,
    },
  ],
});
const parsedHistory = api.parseSessionHistory(sessionId, baseHistory());
assert.deepEqual(
  parsedHistory.entries[0].message.attachments,
  [attachment],
  "纯文件用户消息的有序附件恢复",
);
assert.deepEqual(parsedHistory.entries[2].message.attachments, [], "无附件返回空集合");
assert.deepEqual(parsedHistory.steering[0].attachments, [attachment], "输入按受理顺序恢复附件");
const missingAttachments = structuredClone(baseHistory());
delete missingAttachments.entries[0].message.attachments;
assert.throws(() => api.parseSessionHistory(sessionId, missingAttachments), /无效/);
const extraAttachmentField = structuredClone(baseHistory());
extraAttachmentField.entries[0].message.attachments[0].storage_ref = "tmp/x.md";
assert.throws(
  () => api.parseSessionHistory(sessionId, extraAttachmentField),
  /未定义字段/,
  "响应泄露存储引用属协议异常",
);
assert.deepEqual(
  business.parseAttachmentContent({ ...attachment, text: "# 推\n\n卧推 3×8" }),
  { ...attachment, text: "# 推\n\n卧推 3×8" },
  "附件原文读取响应包含严格解码正文",
);
assert.throws(
  () => business.parseAttachmentContent({ ...attachment }),
  /缺少必填字段/,
);

const rounds = history.historyToRounds(parsedHistory);
assert.deepEqual(
  rounds.flatMap((round) => round.entries.map((entry) => entry.attachments?.length ?? 0)),
  [1, 0, 0, 1],
  "历史转换为展示条目携带有序附件",
);
assert.deepEqual(
  rounds[0].entries[0].attachments,
  files.retainedAttachmentDrafts([attachment]),
  "历史附件作为引用项供编辑默认保留",
);

/** 8. Steering 附件：pending 快照与消费节点同一集合，撤回保持原值，终态不被覆盖 */
const roundBase = () => ({ id: "op", run_id: runId, entries: [], status: "running" });
const drafts = files.retainedAttachmentDrafts([attachment]);
const received = apply("accepted", null, null);
function apply(status, entryId, reason) {
  return {
    run_id: runId,
    steering_id: steerId,
    status,
    entry_id: entryId,
    reason,
    operation_id: "op",
  };
}
const pendingRound = reactAgent.applySteeringStatus(roundBase(), received, "", drafts);
assert.deepEqual(pendingRound.entries[0].attachments, drafts, "纯文件 Steering 携带附件");
const textOnly = reactAgent.applySteeringStatus(roundBase(), received, "文字输入");
assert.equal(textOnly.entries[0].attachments, undefined, "文本输入不伪造附件字段");
const consumedRound = reactAgent.applySteeringStatus(
  pendingRound,
  apply("consumed", steerEntry, null),
  "",
  files.retainedAttachmentDrafts([attachment, stored[1]]),
);
assert.equal(consumedRound.entries.length, 1, "消费后按真实用户节点归并，不重复展示");
assert.equal(consumedRound.entries[0].entry_id, steerEntry, "消费绑定真实用户节点");
assert.deepEqual(
  consumedRound.entries[0].attachments,
  files.retainedAttachmentDrafts([attachment, stored[1]]),
  "consumed 集合与其用户节点一致",
);
const withdrawnRound = reactAgent.applySteeringStatus(
  reactAgent.applySteeringStatus(roundBase(), received, "", drafts),
  apply("withdrawn", null, null),
);
assert.deepEqual(
  withdrawnRound.entries[0].attachments,
  drafts,
  "撤回保持受理时附件集合",
);
assert.equal(withdrawnRound.entries[0].steering.status, "withdrawn");

/** 9. 未确认输入身份：同文本不同附件集合视为不同输入 */
const key = reactAgent.messageInputKey("同一文本", files.attachmentInputs(drafts));
assert.equal(
  key,
  reactAgent.messageInputKey("同一文本", files.attachmentInputs(drafts)),
  "同请求身份稳定",
);
assert.notEqual(
  key,
  reactAgent.messageInputKey("同一文本", []),
  "附件集合不同则请求不同",
);
assert.deepEqual(
  reactAgent.unknownSteeringKeys([
    {
      operation_id: "op",
      session_id: sessionId,
      kind: "steering",
      run_id: runId,
      request: "同一文本",
      attachments: drafts,
      created_at: 1,
    },
    {
      operation_id: "op2",
      session_id: sessionId,
      kind: "send",
      run_id: null,
      request: "同一文本",
      attachments: [],
      created_at: 1,
    },
  ]),
  [key],
  "只有未确认 Steering 参与输入身份比较",
);
assert.equal(
  reactAgent.canSubmitChatInput("同一文本", drafts, [key]),
  false,
  "同身份未确认输入必须显式重试",
);

/** 10. 录入与调整准备结果：通用完整度、允许无画像依据、准备类型由实际工具确定 */
const incompleteContent = {
  repeat: null,
  days: [
    { kind: "training", focus: "推", exercises: [], notes: null },
    { kind: "rest", focus: null, exercises: [], notes: "休一天" },
  ],
  notes: null,
  suggested_fields: [],
};
const importProposal = {
  proposal_id: u(7),
  preparation_kind: "import",
  base_profile_version: null,
  base_plan_id: null,
  payload: incompleteContent,
};
assert.deepEqual(
  business.parsePlanImportProposal(importProposal),
  importProposal,
  "录入快照允许无画像依据与不完整计划",
);
assert.deepEqual(
  business.preparedPlanImportProposal(JSON.stringify(importProposal)),
  importProposal,
);
const adjustmentProposal = {
  ...importProposal,
  proposal_id: u(8),
  preparation_kind: "adjustment",
  base_profile_version: 2,
  base_plan_id: u(9),
};
assert.deepEqual(
  business.preparedPlanAdjustmentProposal(JSON.stringify(adjustmentProposal)),
  adjustmentProposal,
  "调整快照保留依据身份",
);
assert.throws(
  () => business.parsePlanImportProposal({ ...adjustmentProposal }),
  /preparation_kind|无效/,
  "录入结果携带 adjustment 类型属协议异常",
);
assert.throws(
  () => business.parsePlanAdjustmentProposal(importProposal),
  /无效/,
  "调整结果携带 import 类型属协议异常",
);
assert.throws(
  () => business.parsePlanImportProposal({ ...importProposal, preparation_kind: "generation" }),
  /无效/,
);
assert.throws(
  () => business.parsePlanImportProposal({ ...importProposal, attachments: [] }),
  /未定义字段/,
  "原文件信息不写入计划快照",
);
assert.throws(
  () => business.parsePlanImportProposal({ ...importProposal, base_profile_version: 0 }),
  /无效/,
  "画像版本为正整数或 null",
);
assert.throws(
  () => business.parsePlanImportProposal({ ...importProposal, base_plan_id: "not-a-uuid" }),
  /无效/,
);
const generatedIncomplete = {
  proposal_id: u(10),
  base_profile_version: 1,
  base_plan_id: null,
  payload: incompleteContent,
};
assert.throws(
  () => business.parsePlanProposal(generatedIncomplete),
  /训练日缺少具体动作/,
  "生成快照继续执行生成完整度",
);
assert.throws(
  () =>
    business.parsePlanProposal({
      ...generatedIncomplete,
      base_profile_version: null,
    }),
  /无效/,
  "生成快照要求已建档画像",
);
const completeContent = {
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
      notes: null,
    },
    { kind: "rest", focus: null, exercises: [], notes: null },
  ],
  notes: null,
  suggested_fields: ["/days/0/exercises/0/sets"],
};
const generated = { ...generatedIncomplete, payload: completeContent };

/** 11. 准备工具按工具名选择 schema，实时与历史同一口径 */
assert.deepEqual(
  reactAgent.preparedPlanDisplay("prepare_plan_import", JSON.stringify(importProposal)),
  { plan: importProposal },
);
assert.deepEqual(
  reactAgent.preparedPlanDisplay(
    "prepare_plan_adjustment",
    JSON.stringify(adjustmentProposal),
  ),
  { plan: adjustmentProposal },
);
assert.deepEqual(
  reactAgent.preparedPlanDisplay("prepare_plan", JSON.stringify(generated)),
  { plan: generated },
  "现有 prepare_plan 展示保持生成契约原值",
);
assert.equal(reactAgent.preparedPlanDisplay("get_current_plan", "{}"), null);
assert.throws(
  () =>
    reactAgent.preparedPlanDisplay(
      "prepare_plan_import",
      JSON.stringify({ ...importProposal, proposal_id: "x" }),
    ),
  /身份无效/,
);

console.log(
  "PASS: 附件格式与 UTF-8、100000 字节边界、纯文件输入与标题、上传引用序列化、编辑保留移除替换、账本原值重放、历史与 Steering 附件投影、录入与调整准备结果全部符合契约",
);
