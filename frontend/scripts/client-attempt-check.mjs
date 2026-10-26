// 运行：node scripts/client-attempt-check.mjs
// 客户端发送观测检查（Trace 契约 §6.2、§6.3）：真实解析器与业务投影下的首正文、终态、
// 停止、EOF、解析失败、查询恢复分类，缺失测量省略、真实零值与一次结算，
// 以及管理层在途回执与终态补报触发的交错。
import assert from "node:assert/strict";
import { createServer } from "vite";

const server = await createServer({
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});
const {
  startClientAttempt,
  observeFirstText,
  settleClientAttempt,
  clientAttemptBody,
  streamFailureAction,
  isPermanentAttemptRejection,
} = await server.ssrLoadModule("/src/features/chat/utils/clientAttempt.ts");
const { createReActParser, applyReActEvent, ReActHttpError, ReActStreamError } =
  await server.ssrLoadModule("/src/features/chat/utils/reactAgent.ts");
const { sessionRuns } = await server.ssrLoadModule(
  "/src/features/chat/utils/sessionRunManager.ts",
);
await server.close();

const RUN = "11111111-1111-4111-8111-111111111111";
const SESSION = "22222222-2222-4222-8222-222222222222";
const OPERATION = "33333333-3333-4333-8333-333333333333";
const MESSAGE = "44444444-4444-4444-8444-444444444444";
const UUID4 =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

const frame = (event, data) =>
  `event: ${event}\ndata: ${JSON.stringify({ run_id: RUN, ...data })}\n\n`;

function parse(eventName, data) {
  const events = [];
  const parser = createReActParser((item) => events.push(item), RUN);
  parser.feed(frame(eventName, data));
  assert.equal(events.length, 1);
  return events[0];
}

function roundWith(text) {
  const round = { id: OPERATION, run_id: RUN, status: "running", entries: [] };
  const start = parse("message_start", {
    message_id: MESSAGE,
    content: [{ content_index: 0, type: "text", text }],
  });
  const started = applyReActEvent(round, start);
  const end = parse("message_end", {
    message_id: MESSAGE,
    content: [{ content_index: 0, type: "text", text }],
    stop_reason: "stop",
    entry_id: MESSAGE,
    parent_id: null,
  });
  return { round: applyReActEvent(started, end), start };
}

// 有效发送：通过门禁后的起点被冻结；未结算不上报，身份为 UUIDv4。
const attempt = startClientAttempt(SESSION, OPERATION, 1000);
assert.match(attempt.client_attempt_id, UUID4);
assert.equal(attempt.run_id, null);
assert.equal(attempt.first_text_ms, null);
assert.equal(clientAttemptBody(attempt), null);

// 首正文：真实解析 + 业务投影后的首个非空 text；thinking、空正文与工具事件不计。
const { round, start } = roundWith("你好");
observeFirstText(attempt, start, 1250);
assert.equal(attempt.first_text_ms, 250);
observeFirstText(attempt, start, 1300);
assert.equal(attempt.first_text_ms, 250);

const thinking = startClientAttempt(SESSION, OPERATION, 0);
observeFirstText(
  thinking,
  parse("message_start", {
    message_id: MESSAGE,
    content: [{ content_index: 0, type: "thinking", thinking: "内部思考" }],
  }),
  10,
);
assert.equal(thinking.first_text_ms, null);
observeFirstText(
  thinking,
  parse("message_start", {
    message_id: MESSAGE,
    content: [{ content_index: 0, type: "text", text: "   " }],
  }),
  10,
);
assert.equal(thinking.first_text_ms, null);
observeFirstText(
  thinking,
  parse("tool_execution_update", {
    tool_call_id: "call-1",
    tool_name: "read_file",
    content: "工具输出",
    is_error: false,
  }),
  10,
);
assert.equal(thinking.first_text_ms, null);

// 终态：真实 done 通过业务校验后结算 completed；error 按 status 结算 failed/cancelled。
const done = parse("done", { status: "completed", stop_reason: "stop" });
assert.equal(applyReActEvent(round, done).status, "completed");
assert.equal(settleClientAttempt(attempt, "completed", 1450), true);
assert.equal(attempt.duration_ms, 450);
assert.equal(attempt.first_text_ms, 250);
const doneBody = clientAttemptBody(attempt);
assert.deepEqual(doneBody, {
  session_id: SESSION,
  operation_id: OPERATION,
  run_id: null,
  client_attempt_id: attempt.client_attempt_id,
  attempt_kind: "send",
  client_terminal_kind: "completed",
  client_duration_ms: 450,
  client_first_text_ms: 250,
});

const failed = startClientAttempt(SESSION, OPERATION, 100);
const errorEvent = parse("error", {
  status: "failed",
  code: "execution_failed",
  message: "运行失败。",
  tool_call_id: null,
});
assert.equal(
  applyReActEvent(
    { id: OPERATION, run_id: RUN, status: "running", entries: [] },
    errorEvent,
  ).status,
  "failed",
);
assert.equal(settleClientAttempt(failed, errorEvent.data.status, 400), true);
assert.equal(failed.terminal_kind, "failed");

// 停止：在 stop 入口结算 cancelled；后续中止异常与查询不得改写分类或耗时。
const stopped = startClientAttempt(SESSION, OPERATION, 2000);
assert.equal(settleClientAttempt(stopped, "cancelled", 2250), true);
assert.equal(settleClientAttempt(stopped, "disconnected", 9999), false);
assert.equal(settleClientAttempt(stopped, "unknown", 9999, false), false);
assert.equal(stopped.terminal_kind, "cancelled");
assert.equal(stopped.duration_ms, 250);

// EOF：真实解析器未收到终止事件时给出断连分类。
const ended = (() => {
  try {
    createReActParser(() => {}, RUN).finish();
  } catch (error) {
    return error;
  }
  assert.fail("未收到终止事件应抛出错误");
})();
assert.ok(ended instanceof ReActStreamError);
assert.equal(ended.kind, "ended");
assert.deepEqual(streamFailureAction(ended), {
  report: true,
  kind: "disconnected",
});

// 解析失败：协议事件非法时归入 unknown；解码失败同样为协议错误。
const protocol = (() => {
  try {
    parse("done", { status: "failed", stop_reason: "stop" });
  } catch (error) {
    return error;
  }
  assert.fail("非法终止事件应抛出错误");
})();
assert.deepEqual(streamFailureAction(protocol), { report: true, kind: "unknown" });
assert.deepEqual(
  streamFailureAction(new ReActStreamError("protocol", "响应流解码失败。")),
  { report: true, kind: "unknown" },
);

// 网络中断归入 disconnected；明确拒绝与主动中止不生成样本。
assert.deepEqual(streamFailureAction(new TypeError("fetch failed")), {
  report: true,
  kind: "disconnected",
});
assert.deepEqual(
  streamFailureAction(new ReActHttpError("请求不合法。", "invalid_request", 422)),
  { report: false, kind: null },
);
assert.deepEqual(
  streamFailureAction(new DOMException("aborted", "AbortError")),
  { report: false, kind: null },
);

// 查询恢复：分类 unknown，完成时长省略，已有真实首正文保留。
const recovered = startClientAttempt(SESSION, OPERATION, 3000);
observeFirstText(recovered, start, 3100);
assert.equal(settleClientAttempt(recovered, "unknown", 4000, false), true);
assert.equal(recovered.duration_ms, null);
const recoveredBody = clientAttemptBody(recovered);
assert.equal("client_duration_ms" in recoveredBody, false);
assert.equal(recoveredBody.client_first_text_ms, 100);
assert.equal(recoveredBody.client_terminal_kind, "unknown");

// 缺失测量直接省略；真实零值保留。
const noMeasures = startClientAttempt(SESSION, OPERATION, 5000);
settleClientAttempt(noMeasures, "unknown", 6000, false);
assert.deepEqual(clientAttemptBody(noMeasures), {
  session_id: SESSION,
  operation_id: OPERATION,
  run_id: null,
  client_attempt_id: noMeasures.client_attempt_id,
  attempt_kind: "send",
  client_terminal_kind: "unknown",
});
const zeros = startClientAttempt(SESSION, OPERATION, 7000);
observeFirstText(zeros, start, 7000);
settleClientAttempt(zeros, "completed", 7000);
assert.equal(zeros.first_text_ms, 0);
assert.equal(zeros.duration_ms, 0);
const zeroBody = clientAttemptBody(zeros);
assert.equal(zeroBody.client_duration_ms, 0);
assert.equal(zeroBody.client_first_text_ms, 0);

// 永久拒收：4xx 结束补报，run_not_terminal 等待既有查询确认。
assert.equal(
  isPermanentAttemptRejection(
    new ReActHttpError("内容冲突。", "attempt_conflict", 409),
  ),
  true,
);
assert.equal(
  isPermanentAttemptRejection(
    new ReActHttpError("运行未结束。", "run_not_terminal", 409),
  ),
  false,
);
assert.equal(
  isPermanentAttemptRejection(
    new ReActHttpError("观测服务暂不可用。", "observation_unavailable", 503),
  ),
  false,
);
assert.equal(isPermanentAttemptRejection(new TypeError("fetch failed")), false);

// 回执交错：在途 run_not_terminal 回执期间终态查询已确认并触发补报；失败结束后立即消费该
// 触发，使用原拿到的 run_id 与原测量重报，期间不产生并发请求。
const requests = [];
const responses = [];
globalThis.fetch = (_input, init) => {
  requests.push(JSON.parse(String(init?.body)));
  return new Promise((resolve) => responses.push(resolve));
};
const respond = (status, body) =>
  responses.shift()(
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
const settle = async (check, label) => {
  for (let turn = 0; turn < 50; turn += 1) {
    if (check()) return;
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.fail(`等待失败：${label}`);
};

const record = sessionRuns.open(SESSION, true);
const raced = startClientAttempt(SESSION, OPERATION, 1000);
settleClientAttempt(raced, "cancelled", 1250);
record.trackAttempt(raced);
record.reportAttempt(raced);
assert.equal(requests.length, 1);
record.confirmAttempts(RUN, OPERATION);
assert.equal(requests.length, 1);
respond(409, {
  detail: { code: "run_not_terminal", message: "运行尚未取得终态。" },
});
await settle(() => requests.length === 2, "在途失败后补报");
assert.deepEqual(requests[1], {
  session_id: SESSION,
  operation_id: OPERATION,
  run_id: RUN,
  client_attempt_id: raced.client_attempt_id,
  attempt_kind: "send",
  client_terminal_kind: "cancelled",
  client_duration_ms: 250,
});
respond(202, { status: "recorded", observation_id: "0123456789abcdef" });
await settle(
  () => record.attempts.has(raced.client_attempt_id) === false,
  "回执释放",
);
record.dispose();
delete globalThis.fetch;

console.log(
  "PASS: 发送计时、首正文、五类终态、停止、EOF、解析失败、查询恢复分类与回执补报交错正确",
);
