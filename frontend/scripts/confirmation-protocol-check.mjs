// 运行：node scripts/confirmation-protocol-check.mjs
// 纯函数检查：确认请求／操作／引用与历史扩展的严格解析，以及确认 POST 与专用 GET 的状态码处理。
// 说明：任务 2 尚未提供真实确认接口，本脚本用测试进程内的 Response 馈入生产解析与状态处理代码，
// 只验证客户端协议实现，不代表任何后端成功结果。
import assert from "node:assert/strict";
import { createServer } from "vite";

const server = await createServer({
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});
const business = await server.ssrLoadModule("/src/lib/business.ts");
const api = await server.ssrLoadModule("/src/lib/api.ts");
const { ReActHttpError } = await server.ssrLoadModule(
  "/src/features/chat/utils/reactAgent.ts",
);
await server.close();

const u = (n) =>
  `${n.toString(16).padStart(8, "0")}-0000-4000-8000-000000000000`;
const sessionId = u(1);
const requestEntry = u(2);
const sourceEntry = u(3);
const displayEntry = u(4);
const confirmationEntry = u(5);
const otherConfirmationEntry = u(6);
const operationId = u(7);
const profileProposal = u(8);
const planProposal = u(9);
const workoutProposal = u(10);

const profileContent = {
  goal: "增肌",
  experience: "训练一年",
  environment: "健身房",
  availability: "每周三次",
  health_notes: null,
  movement_restrictions: null,
  unavailable_equipment: [],
  forbidden_exercise_ids: null,
};
const profileResult = {
  proposal_id: profileProposal,
  profile_id: 1,
  version: 3,
  content: profileContent,
  saved_at: 1_700_000_000_000,
};
const planContent = {
  repeat: null,
  days: [
    {
      kind: "training",
      focus: "推",
      exercises: [
        {
          exercise_id: "0001",
          name: "卧推",
          sets: 4,
          reps: 8,
          duration_seconds: null,
          weight_kg: 60,
          load_convention: "barbell_total",
          rest_seconds: null,
        },
      ],
      notes: null,
    },
  ],
  notes: null,
  suggested_fields: [],
};
const planResult = {
  proposal_id: planProposal,
  id: u(11),
  content: planContent,
  created_at: 1_700_000_000_000,
  saved_at: 1_700_000_000_000,
};
const workoutResult = {
  proposal_id: workoutProposal,
  id: u(12),
  performed_on: "2026-10-12",
  version: 2,
  content: {
    exercises: [
      {
        exercise_id: "0002",
        name: "深蹲",
        load_convention: "barbell_total",
        sets: [{ reps: 5, weight_kg: 100, duration_seconds: null }],
      },
    ],
    notes: null,
  },
  created_at: 1_700_000_000_000,
  updated_at: 1_700_000_000_000,
  saved_at: 1_700_000_000_000,
};

const operation = (overrides) => ({
  session_id: sessionId,
  operation_id: operationId,
  business_kind: "profile",
  proposal_id: profileProposal,
  display_entry_id: displayEntry,
  confirmation_entry_id: confirmationEntry,
  status: "saved",
  result: profileResult,
  error: null,
  created_at: 1,
  updated_at: 2,
  ...overrides,
});

const reference = (overrides) => ({
  business_kind: "profile",
  proposal_id: profileProposal,
  status: "pending",
  request_entry_id: requestEntry,
  source_entry_id: sourceEntry,
  display_entry_id: displayEntry,
  confirmation_entry_id: null,
  ...overrides,
});

/* ===== 1. 确认请求体：恰好四字段（§7.2） ===== */
assert.deepEqual(
  business.parseSaveConfirmationRequest({
    operation_id: operationId,
    business_kind: "workout",
    proposal_id: workoutProposal,
    display_entry_id: displayEntry,
  }),
  {
    operation_id: operationId,
    business_kind: "workout",
    proposal_id: workoutProposal,
    display_entry_id: displayEntry,
  },
  "四字段请求逐字保留",
);
for (const bad of [
  {
    operation_id: operationId,
    business_kind: "profile",
    proposal_id: profileProposal,
  },
  {
    operation_id: operationId,
    business_kind: "profile",
    proposal_id: profileProposal,
    display_entry_id: displayEntry,
    confirmation_entry_id: confirmationEntry,
  },
  {
    operation_id: operationId,
    business_kind: "diet",
    proposal_id: profileProposal,
    display_entry_id: displayEntry,
  },
  {
    operation_id: "not-a-uuid",
    business_kind: "profile",
    proposal_id: profileProposal,
    display_entry_id: displayEntry,
  },
])
  assert.throws(() => business.parseSaveConfirmationRequest(bad));

/* ===== 2. 操作对象与状态配对（§7.3） ===== */
assert.deepEqual(business.parseSaveConfirmationOperation(operation()), {
  ...operation(),
  status: "saved",
  result: profileResult,
  error: null,
});
assert.equal(
  business.parseSaveConfirmationOperation(
    operation({
      business_kind: "plan",
      proposal_id: planProposal,
      result: planResult,
    }),
  ).result.id,
  planResult.id,
  "计划完整固定结果按业务类型校验",
);
assert.equal(
  business.parseSaveConfirmationOperation(
    operation({
      business_kind: "workout",
      proposal_id: workoutProposal,
      result: workoutResult,
    }),
  ).result.performed_on,
  "2026-10-12",
  "训练记录完整固定结果按业务类型校验",
);
assert.equal(
  business.parseSaveConfirmationOperation(
    operation({ status: "processing", result: null, error: null }),
  ).status,
  "processing",
);
assert.equal(
  business.parseSaveConfirmationOperation(
    operation({
      status: "failed",
      result: null,
      error: { code: "profile_version_conflict", message: "画像版本已变化。" },
    }),
  ).error.code,
  "profile_version_conflict",
);
assert.deepEqual(
  business.parseSaveConfirmationOperation(
    operation({
      status: "failed",
      result: null,
      error: {
        code: "invalid_business_payload",
        message: "内容校验失败。",
        errors: [{ path: "payload.days.0", message: "缺少训练日" }],
      },
    }),
  ).error.errors,
  [{ path: "payload.days.0", message: "缺少训练日" }],
  "内容校验错误携带字段定位",
);
/** errors 的输入形状：缺省即未携带，出现时逐项严格校验（§7.4） */
const failedWith = (error) =>
  business.parseSaveConfirmationOperation(
    operation({ status: "failed", result: null, error }),
  ).error;
assert.deepEqual(
  Object.keys(
    failedWith({ code: "profile_version_conflict", message: "画像版本已变化。" }),
  ).sort(),
  ["code", "message"],
  "缺省时不携带 errors",
);
assert.deepEqual(
  failedWith({
    code: "invalid_business_payload",
    message: "内容校验失败。",
    errors: [],
  }).errors,
  [],
  "空明细数组原样保留",
);
for (const bad of [
  { code: "x", message: "y", errors: null },
  { code: "x", message: "y", errors: 0 },
  { code: "x", message: "y", errors: "days" },
  { code: "x", message: "y", errors: {} },
  { code: "x", message: "y", errors: [{}] },
  { code: "x", message: "y", errors: [{ path: "payload.days" }] },
  { code: "x", message: "y", errors: [{ path: "a", message: "b", extra: 1 }] },
  { code: "x", message: "y", errors: [{ path: 1, message: "b" }] },
])
  assert.throws(() => failedWith(bad), "非法 errors 形状就地报错");
for (const bad of [
  operation({ status: "processing", result: profileResult }),
  operation({
    status: "processing",
    error: { code: "run_busy", message: "已有任务正在执行。" },
  }),
  operation({ status: "saved", result: null }),
  operation({
    status: "saved",
    error: { code: "internal_error", message: "内部错误。" },
  }),
  operation({
    status: "saved",
    result: { ...profileResult, proposal_id: planProposal },
  }),
  operation({
    status: "saved",
    result: { ...profileResult, profile_id: 2 },
  }),
  operation({ status: "failed", error: null }),
  operation({
    status: "failed",
    result: profileResult,
    error: { code: "x", message: "y" },
  }),
  operation({ status: "done" }),
  { ...operation(), extra: 1 },
  operation({ created_at: -1 }),
  operation({ confirmation_entry_id: null }),
])
  assert.throws(() => business.parseSaveConfirmationOperation(bad));

/* ===== 3. 待确认引用（§7.6） ===== */
assert.deepEqual(business.parsePendingProposalReference(reference()), {
  ...reference(),
  confirmation_entry_id: null,
});
assert.equal(
  business.parsePendingProposalReference(
    reference({
      status: "processing",
      confirmation_entry_id: confirmationEntry,
    }),
  ).confirmation_entry_id,
  confirmationEntry,
);
for (const bad of [
  reference({ display_entry_id: null }),
  reference({ status: "saved" }),
  reference({ business_kind: "unknown" }),
  { ...reference(), extra: 1 },
])
  assert.throws(() => business.parsePendingProposalReference(bad));

/* ===== 4. 历史扩展必填与关联校验（§7.6） ===== */
const historyNode = (entryId, parentId, role, runId) => ({
  type: "message",
  entry_id: entryId,
  parent_id: parentId,
  run_id: runId,
  created_at: 1,
  message:
    role === "user"
      ? { role: "user", text: "正文", timestamp: 1, attachments: [] }
      : role === "assistant"
        ? {
            role: "assistant",
            content: [{ content_index: 0, type: "text", text: "助手" }],
            stop_reason: "stop",
            timestamp: 1,
          }
        : {
            role: "toolResult",
            tool_call_id: "tc1",
            tool_name: "prepare_profile_update",
            content: "{}",
            is_error: false,
            timestamp: 1,
          },
});
const historyBase = (overrides) => ({
  session: {
    session_id: sessionId,
    title: "会话",
    active_leaf_id: otherConfirmationEntry,
    created_at: 1,
    updated_at: 2,
  },
  entries: [
    historyNode(requestEntry, null, "user", null),
    historyNode(sourceEntry, requestEntry, "assistant", u(20)),
    historyNode(displayEntry, sourceEntry, "toolResult", u(20)),
    historyNode(confirmationEntry, displayEntry, "user", null),
    historyNode(otherConfirmationEntry, confirmationEntry, "user", null),
  ],
  runs: [
    {
      session_id: sessionId,
      run_id: u(20),
      request_entry_id: requestEntry,
      last_entry_id: displayEntry,
      status: "completed",
      started_at: 1,
      finished_at: 2,
      error_code: null,
      error_message: null,
      stop_reason: null,
    },
  ],
  steering: [],
  confirmations: [],
  pending_proposals: [],
  ...overrides,
});
assert.deepEqual(
  Object.keys(api.parseSessionHistory(sessionId, historyBase())).sort(),
  [
    "confirmations",
    "entries",
    "pending_proposals",
    "runs",
    "session",
    "steering",
  ],
  "历史响应包含两个必填扩展数组",
);
assert.equal(
  api.parseSessionHistory(sessionId, historyBase()).pending_proposals.length,
  0,
);
assert.equal(
  api.parseSessionHistory(sessionId, {
    ...historyBase(),
    confirmations: [operation()],
  }).confirmations.length,
  1,
  "确认操作与确认节点归属校验通过",
);
assert.equal(
  api.parseSessionHistory(sessionId, {
    ...historyBase(),
    pending_proposals: [reference()],
  }).pending_proposals[0].proposal_id,
  profileProposal,
  "待确认引用的节点角色与顺序校验通过",
);
assert.throws(
  () => api.parseSessionHistory(sessionId, { ...historyBase(), confirmations: undefined }),
  /confirmations/,
  "缺少 confirmations 属协议异常",
);
assert.throws(
  () =>
    api.parseSessionHistory(sessionId, {
      ...historyBase(),
      pending_proposals: undefined,
    }),
  /pending_proposals/,
  "缺少 pending_proposals 属协议异常",
);
assert.throws(
  () =>
    api.parseSessionHistory(sessionId, {
      ...historyBase(),
      confirmations: [operation({ confirmation_entry_id: requestEntry })],
    }),
  /确认/,
  "确认节点必须晚于展示节点",
);
assert.throws(
  () =>
    api.parseSessionHistory(sessionId, {
      ...historyBase(),
      confirmations: [operation({ display_entry_id: requestEntry })],
    }),
  /确认/,
  "展示节点必须属于当前分支",
);
assert.throws(
  () =>
    api.parseSessionHistory(sessionId, {
      ...historyBase(),
      confirmations: [operation(), operation({ operation_id: operationId })],
    }),
  /重复/,
  "同一历史内操作身份唯一",
);
assert.throws(
  () =>
    api.parseSessionHistory(sessionId, {
      ...historyBase(),
      confirmations: [operation({ session_id: u(99) })],
    }),
  /归属/,
  "确认操作归属当前会话",
);
assert.throws(
  () =>
    api.parseSessionHistory(sessionId, {
      ...historyBase(),
      pending_proposals: [reference(), reference()],
    }),
  /重复/,
  "待确认引用的快照标识唯一",
);
assert.throws(
  () =>
    api.parseSessionHistory(sessionId, {
      ...historyBase(),
      pending_proposals: [reference({ source_entry_id: displayEntry })],
    }),
  /来源节点/,
  "来源节点必须是助手节点",
);
assert.throws(
  () =>
    api.parseSessionHistory(sessionId, {
      ...historyBase(),
      pending_proposals: [reference({ confirmation_entry_id: requestEntry })],
    }),
  /确认/,
  "确认节点必须晚于完整展示",
);
assert.throws(
  () =>
    api.parseSessionHistory(sessionId, {
      ...historyBase({
        session: {
          session_id: sessionId,
          title: "会话",
          active_leaf_id: null,
          created_at: 1,
          updated_at: 2,
        },
        entries: [],
        runs: [],
        confirmations: [operation()],
      }),
    }),
  /空分支/,
  "空分支历史不得携带确认操作",
);

/* ===== 5. 确认 HTTP 客户端状态处理（§7.2、§7.3、§7.4） ===== */
const nativeFetch = globalThis.fetch;
const requests = [];
let nextReply = null;
const reply = (status, body) => {
  nextReply = { status, body };
};
globalThis.fetch = async (input, init) => {
  requests.push({
    url: String(input),
    method: init?.method ?? "GET",
    body: init?.body === undefined ? null : JSON.parse(init.body),
  });
  if (nextReply === null) throw new Error("测试未安排响应");
  const { status, body } = nextReply;
  nextReply = null;
  return new Response(typeof body === "string" ? body : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
};
const requestBody = {
  operation_id: operationId,
  business_kind: "profile",
  proposal_id: profileProposal,
  display_entry_id: displayEntry,
};

reply(200, operation());
const saved = await api.postSaveConfirmation(sessionId, requestBody);
assert.equal(saved.status, "saved", "HTTP 200 落地为已保存终态");
assert.deepEqual(requests[0], {
  url: `/api/sessions/${sessionId}/confirmations`,
  method: "POST",
  body: requestBody,
});
assert.equal(
  Object.keys(requests[0].body).sort().join(","),
  "business_kind,display_entry_id,operation_id,proposal_id",
  "正文恰好四字段",
);

requests.length = 0;
reply(200, operation({ status: "processing", result: null }));
const processing = await api.getSaveConfirmation(sessionId, operationId);
assert.equal(processing.status, "processing", "专用查询返回处理中同一操作");
assert.equal(
  requests[0].url,
  `/api/sessions/${sessionId}/confirmations/${operationId}`,
  "查询无正文与查询参数",
);
assert.equal(requests[0].method, "GET");
reply(202, operation({ status: "processing", result: null }));
await assert.rejects(
  () => api.getSaveConfirmation(sessionId, operationId),
  /状态码无效/,
  "专用查询只返回 200",
);

reply(200, operation({ status: "processing", result: null }));
await assert.rejects(
  () => api.postSaveConfirmation(sessionId, requestBody),
  /HTTP 200/,
  "200 与 processing 配对不合契约",
);
reply(202, operation());
await assert.rejects(
  () => api.postSaveConfirmation(sessionId, requestBody),
  /HTTP 202/,
  "202 与 saved 配对不合契约",
);
reply(201, operation());
await assert.rejects(
  () => api.postSaveConfirmation(sessionId, requestBody),
  /状态码无效/,
);
reply(200, operation({ operation_id: u(31) }));
await assert.rejects(
  () => api.postSaveConfirmation(sessionId, requestBody),
  /身份不匹配/,
  "响应身份与请求身份一致",
);

const rejected = {
  detail: { code: "run_busy", message: "已有任务正在执行。" },
};
reply(409, rejected);
let failure = null;
try {
  await api.postSaveConfirmation(sessionId, requestBody);
} catch (cause) {
  failure = cause;
}
assert.ok(failure instanceof ReActHttpError);
assert.equal(failure.code, "run_busy");
assert.equal(failure.http_status, 409);
assert.equal(failure.reason, null, "占用拒绝无细分原因");

reply(409, {
  detail: {
    code: "operation_conflict",
    message: "操作请求冲突。",
    reason: "operation_expired",
  },
});
await assert.rejects(
  () => api.postSaveConfirmation(sessionId, requestBody),
  (cause) =>
    cause.code === "operation_conflict" && cause.reason === "operation_expired",
  "失效操作冲突按 reason 分类",
);

reply(422, {
  detail: {
    code: "invalid_business_payload",
    message: "内容校验失败。",
    errors: [{ path: "payload.days.0", message: "缺少训练日" }],
  },
});
await assert.rejects(
  () => api.postSaveConfirmation(sessionId, requestBody),
  (cause) =>
    cause.code === "invalid_business_payload" && cause.http_status === 422,
  "内容校验错误按明确拒绝分类",
);

reply(404, {
  detail: { code: "confirmation_not_found", message: "确认操作不存在。" },
});
await assert.rejects(
  () => api.getSaveConfirmation(sessionId, operationId),
  (cause) =>
    cause.code === "confirmation_not_found" && cause.http_status === 404,
  "未登记操作按专用错误码返回",
);

reply(500, "not json");
await assert.rejects(
  () => api.getSaveConfirmation(sessionId, operationId),
  /响应错误结构无效/,
  "错误结构不合契约不转换为成功",
);

reply(200, { ...operation(), status: "failed", result: null, error: null });
await assert.rejects(
  () => api.getSaveConfirmation(sessionId, operationId),
  /失败/,
  "失败状态缺少错误对象属协议异常",
);
globalThis.fetch = nativeFetch;

console.log(
  "PASS: 确认请求与操作严格解析、状态配对、历史扩展必填与关联校验、200/202/错误与未知分类",
);
