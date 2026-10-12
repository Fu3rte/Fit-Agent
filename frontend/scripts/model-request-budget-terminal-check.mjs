// 运行：node scripts/model-request-budget-terminal-check.mjs
// 模型请求预算终态契约：新增三个运行错误码、done 成功契约、length 截断失败终态与历史回放一致性。
import assert from "node:assert/strict";
import { createServer } from "vite";

const server = await createServer({
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});
const api = await server.ssrLoadModule("/src/lib/api.ts");
const history = await server.ssrLoadModule(
  "/src/features/chat/utils/sessionHistory.ts",
);
const reactAgent = await server.ssrLoadModule(
  "/src/features/chat/utils/reactAgent.ts",
);
await server.close();

const RUN = "11111111-1111-4111-8111-111111111111";
const OTHER = "99999999-9999-4999-8999-999999999999";
const SESSION = "22222222-2222-4222-8222-222222222222";
const USER = "33333333-3333-4333-8333-333333333333";
const ASSIST = "44444444-4444-4444-8444-444444444444";

const REQUEST_LIMIT_MESSAGE = "本次运行已达到 64 次模型请求上限";
const OUTPUT_LIMIT_MESSAGE = "回答达到输出上限，内容未完成";
const QUOTA_MESSAGE = "模型服务账户额度不足，无法继续执行。";

const sse = (event, data) =>
  `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

/** 真实解析器：喂入单条事件并返回解析结果；terminal 事件同时校验终止标记 */
function feed(event, data, { runId = RUN, terminal = false } = {}) {
  const events = [];
  const parser = reactAgent.createReActParser(
    (item) => events.push(item),
    runId,
  );
  parser.feed(sse(event, { run_id: runId, ...data }));
  if (terminal) {
    parser.finish();
    assert.equal(parser.terminal, true);
  }
  assert.equal(events.length, 1);
  return events[0];
}

/** 断言单条事件被真实解析器拒绝 */
function reject(event, data, runId = RUN) {
  assert.throws(() =>
    reactAgent
      .createReActParser(() => {}, runId)
      .feed(sse(event, { run_id: runId, ...data })),
  );
}

const apply = (state, event) => reactAgent.applyReActEvent(state, event);
const round = () => ({ id: RUN, run_id: RUN, status: "running", entries: [] });
const errorData = (code, message, status = "failed") => ({
  status,
  code,
  message,
  tool_call_id: null,
});

/* ===== 1. 三个新增运行错误码被真实解析器接收并形成失败终态 ===== */
for (const [code, message] of [
  ["model_request_limit_exceeded", REQUEST_LIMIT_MESSAGE],
  ["model_output_limit_exceeded", OUTPUT_LIMIT_MESSAGE],
  ["provider_quota_exceeded", QUOTA_MESSAGE],
]) {
  const data = errorData(code, message);
  const event = feed("error", data, { terminal: true });
  assert.deepEqual(event, { event: "error", data: { run_id: RUN, ...data } });
  const ended = apply(round(), event);
  assert.equal(ended.status, "failed", `${code} 形成失败终态`);
  assert.equal(ended.error, message, `${code} 展示后端安全文案`);
}

/* ===== 2. 未知错误码与非法状态组合仍被拒绝 ===== */
reject("error", errorData("unknown_error", "未知。"));
reject(
  "error",
  errorData("proposal_already_saved", "业务错误码不得作为运行错误。"),
);
reject(
  "error",
  errorData("model_output_limit_exceeded", OUTPUT_LIMIT_MESSAGE, "cancelled"),
);
reject("error", errorData("execution_failed", "执行失败。", "completed"));
reject("error", errorData("execution_failed", "执行失败。", "cancelled"));
reject("error", {
  status: "failed",
  code: "provider_quota_exceeded",
  tool_call_id: null,
});
reject("error", {
  status: "failed",
  code: "provider_quota_exceeded",
  message: "额度不足。",
  tool_call_id: 7,
});
assert.throws(() =>
  reactAgent
    .createReActParser(() => {}, RUN)
    .feed(
      sse("error", {
        run_id: OTHER,
        ...errorData("execution_failed", "执行失败。"),
      }),
    ),
);

/* ===== 3. 普通 stop 完成路径保持正常，done 只接受 stop ===== */
const textBlocks = (text) => [{ content_index: 0, type: "text", text }];
let done = round();
done = apply(done, feed("message_start", { message_id: ASSIST, content: [] }));
done = apply(
  done,
  feed("message_update", {
    message_id: ASSIST,
    content: textBlocks("最终回答。"),
    content_index: 0,
    update_type: "text_delta",
  }),
);
done = apply(
  done,
  feed("message_end", {
    message_id: ASSIST,
    content: textBlocks("最终回答。"),
    stop_reason: "stop",
    entry_id: ASSIST,
    parent_id: null,
  }),
);
assert.equal(done.status, "running");
done = apply(
  done,
  feed(
    "done",
    { status: "completed", stop_reason: "stop" },
    { terminal: true },
  ),
);
assert.equal(done.status, "completed");
assert.equal(done.error, undefined);
reject("done", { status: "completed", stop_reason: "length" });
reject("done", { status: "completed", stop_reason: "toolUse" });
reject("done", { status: "failed", stop_reason: "stop" });

/* ===== 4. length 助手内容加 error 形成失败终态，截断内容保留 ===== */
let trunc = round();
trunc = apply(
  trunc,
  feed("message_start", { message_id: ASSIST, content: [] }),
);
trunc = apply(
  trunc,
  feed("message_end", {
    message_id: ASSIST,
    content: textBlocks("已生成的部分内容"),
    stop_reason: "length",
    entry_id: ASSIST,
    parent_id: null,
  }),
);
assert.equal(trunc.status, "running", "截断事件后运行保持 running");
assert.equal(trunc.entries[0].stop_reason, "length");
const truncEnded = apply(
  trunc,
  feed(
    "error",
    errorData("model_output_limit_exceeded", OUTPUT_LIMIT_MESSAGE),
    {
      terminal: true,
    },
  ),
);
assert.equal(truncEnded.status, "failed", "length 以失败终止");
assert.equal(truncEnded.error, OUTPUT_LIMIT_MESSAGE);
assert.equal(
  truncEnded.entries[0].content[0].text,
  "已生成的部分内容",
  "截断已生成文本保留",
);

/* ===== 5. 截断工具调用被接收，且不产生已执行工具条目 ===== */
const truncatedCall = [
  {
    content_index: 0,
    type: "tool_call",
    tool_call_id: "tc-truncated",
    name: "save_plan",
    arguments: {},
  },
];
let toolTrunc = round();
toolTrunc = apply(
  toolTrunc,
  feed("message_start", { message_id: ASSIST, content: [] }),
);
toolTrunc = apply(
  toolTrunc,
  feed("message_end", {
    message_id: ASSIST,
    content: truncatedCall,
    stop_reason: "length",
    entry_id: ASSIST,
    parent_id: null,
  }),
);
assert.equal(
  toolTrunc.entries.filter((entry) => entry.kind === "tool").length,
  0,
  "未执行的截断工具调用不产生工具条目",
);
assert.equal(toolTrunc.entries[0].stop_reason, "length");
assert.throws(
  () => {
    let aborted = round();
    aborted = apply(
      aborted,
      feed("message_start", { message_id: ASSIST, content: [] }),
    );
    apply(
      aborted,
      feed("message_end", {
        message_id: ASSIST,
        content: truncatedCall,
        stop_reason: "aborted",
        entry_id: ASSIST,
        parent_id: null,
      }),
    );
  },
  /取消消息包含工具调用/,
  "取消消息含工具调用保持既有拒绝语义",
);

/* ===== 6. 已有取消与上下文类错误保持语义 ===== */
const cancelled = apply(
  round(),
  feed("error", errorData("cancelled", "执行已取消。", "cancelled"), {
    terminal: true,
  }),
);
assert.equal(cancelled.status, "cancelled");
for (const code of [
  "execution_failed",
  "credential_detected",
  "compaction_failed",
  "context_budget_exceeded",
  "context_overflow",
]) {
  const ended = apply(
    round(),
    feed("error", errorData(code, "失败。"), { terminal: true }),
  );
  assert.equal(ended.status, "failed", `${code} 保持失败语义`);
  assert.equal(ended.error, "失败。");
}

/* ===== 7. 历史回放与即时事件终态一致 ===== */
const truncationHistory = {
  session: {
    session_id: SESSION,
    title: "会话",
    active_leaf_id: ASSIST,
    created_at: 1,
    updated_at: 2,
  },
  entries: [
    {
      type: "message",
      entry_id: USER,
      parent_id: null,
      run_id: RUN,
      created_at: 1,
      message: { role: "user", text: "写一段", timestamp: 1, attachments: [] },
    },
    {
      type: "message",
      entry_id: ASSIST,
      parent_id: USER,
      run_id: RUN,
      created_at: 2,
      message: {
        role: "assistant",
        content: textBlocks("已生成的部分内容"),
        stop_reason: "length",
        timestamp: 2,
      },
    },
  ],
  runs: [
    {
      session_id: SESSION,
      run_id: RUN,
      request_entry_id: USER,
      last_entry_id: ASSIST,
      status: "failed",
      started_at: 1,
      finished_at: 2,
      error_code: "model_output_limit_exceeded",
      error_message: OUTPUT_LIMIT_MESSAGE,
    },
  ],
  steering: [],
  // 确认契约 §7.6：确认操作与待确认引用为历史必填数组，本场景两份均为空
  confirmations: [],
  pending_proposals: [],
};
const parsedHistory = api.parseSessionHistory(SESSION, truncationHistory);
const replay = history.historyToRounds(parsedHistory);
assert.equal(replay.length, 1);
assert.equal(replay[0].status, "failed");
assert.equal(replay[0].error, OUTPUT_LIMIT_MESSAGE);
const historyAssistant = replay[0].entries.find(
  (entry) => entry.kind === "assistant",
);
assert.equal(
  historyAssistant.stop_reason,
  "length",
  "历史助手 length 内容继续可读",
);
assert.equal(historyAssistant.content[0].text, "已生成的部分内容");

let live = round();
live = apply(live, feed("message_start", { message_id: ASSIST, content: [] }));
live = apply(
  live,
  feed("message_end", {
    message_id: ASSIST,
    content: textBlocks("已生成的部分内容"),
    stop_reason: "length",
    entry_id: ASSIST,
    parent_id: USER,
  }),
);
live = apply(
  live,
  feed(
    "error",
    errorData("model_output_limit_exceeded", OUTPUT_LIMIT_MESSAGE),
    {
      terminal: true,
    },
  ),
);
assert.equal(live.status, replay[0].status, "即时与历史运行状态一致");
assert.equal(live.error, replay[0].error, "即时与历史失败文案一致");
assert.equal(
  live.entries.find((entry) => entry.kind === "assistant").stop_reason,
  historyAssistant.stop_reason,
  "即时与历史助手终态一致",
);

console.log(
  "PASS: 三个新增运行错误码、done 成功契约、length 截断失败终态、未执行工具调用及历史回放一致性",
);
