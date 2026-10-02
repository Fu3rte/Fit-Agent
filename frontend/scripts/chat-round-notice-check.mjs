import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { createServer } from "vite";
import { realpathSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(realpathSync.native(import.meta.filename));
const { createElement } = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

const server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
try {
  const { applyReActEvent: apply, acceptSteering, finishReActRound: finish, createReActParser, validChatInput, canSubmitChatInput, markUnknownSteering, runReActStream } = await server.ssrLoadModule("/src/features/chat/utils/reactAgent.ts");
  const { default: Transcript } = await server.ssrLoadModule("/src/features/chat/components/ChatTranscript.tsx");
  const run_id = randomUUID(), first = randomUUID(), second = randomUUID(), steering_id = randomUUID();
  const event = (event, data) => ({ event, data: { run_id, ...data } });
  const start = (id) => event("message_start", { message_id: id, content: [] });
  const update = (id, content, update_type, content_index = content.at(-1).content_index) => event("message_update", { message_id: id, content, update_type, content_index });
  const end = (id, content, stop_reason) => event("message_end", { message_id: id, content, stop_reason });
  const text = (text, content_index = 0) => ({ content_index, type: "text", text });
  const thinking = (thinking, content_index = 1) => ({ content_index, type: "thinking", thinking });
  const call = { content_index: 2, type: "tool_call", tool_call_id: "call-1", name: "bash", arguments: { command: "exit 7" } };
  const fresh = () => ({ id: "round", run_id, status: "running", entries: [{ kind: "user", id: "round", request: "用户 **纯文本**" }] });
  let round = fresh();
  const sequence = [
    start(first), update(first, [text("前文")], "text_start"),
    update(first, [text("前文增量")], "text_delta"), update(first, [text("前文增量")], "text_end"),
    update(first, [text("前文增量"), thinking("思考中文🙂")], "thinking_start"),
    update(first, [text("前文增量"), thinking("思考中文🙂继续")], "thinking_delta"),
    update(first, [text("前文增量"), thinking("思考中文🙂继续")], "thinking_end"),
    update(first, [text("前文增量"), thinking("思考中文🙂继续"), call], "toolcall_start"),
    update(first, [text("前文增量"), thinking("思考中文🙂继续"), call], "toolcall_end"),
    end(first, [text("前文校准"), thinking("思考中文🙂继续"), call], "toolUse"),
    event("tool_start", { tool_call_id: call.tool_call_id, name: call.name, arguments: call.arguments }),
    event("tool_result", { tool_call_id: call.tool_call_id, content: "exit 7", is_error: true }),
    event("steering_status", { steering_id, status: "consumed" }), start(second),
    update(second, [text("**流式重点**")], "text_start"), update(second, [text("**流式重点**")], "text_end"),
    end(second, [text("**加粗**\n\n<script>alert(1)</script> [危险](javascript:alert(1))")], "stop"),
    event("done", { status: "completed", stop_reason: "stop" }),
  ];
  const parser = createReActParser((item) => { round = apply(round, item); }, run_id);
  const wire = sequence.map((item) => `event: ${item.event}\r\ndata: ${JSON.stringify(item.data, null, 2).split("\n").join("\r\ndata: ")}\r\n\r\n`).join("");
  for (const character of wire) parser.feed(character);
  parser.finish();
  round = acceptSteering(round, { run_id, steering_id, status: "accepted" }, "继续");
  assert.equal(round.status, "completed");
  assert.deepEqual(round.entries.map((item) => item.kind), ["user", "assistant", "tool", "user", "assistant"]);
  assert.equal(round.entries[1].content[0].text, "前文校准");
  assert.equal(round.entries[2].status, "failed");
  assert.equal(round.entries[3].request, "继续");
  assert.throws(() => acceptSteering(round, { run_id, steering_id, status: "accepted" }, "继续"));
  assert.throws(() => apply(round, start(randomUUID())));
  assert.throws(() => parser.feed("event: done\ndata: {}\n\n"));
  assert.equal(finish(round, "failed", "cleanup"), round);

  const rendered = renderToStaticMarkup(createElement(Transcript, { rounds: [round] }));
  assert.match(rendered, /<strong>加粗<\/strong>/);
  assert.match(rendered, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.doesNotMatch(rendered, /<script|href="javascript:|thinking_signature|redacted_thinking|生成中/i);
  assert.match(rendered, /用户 \*\*纯文本\*\*/);
  assert.match(rendered, /aria-label="思考内容"[^>]*data-state="closed"|data-state="closed"[^>]*aria-label="思考内容"/);
  assert.doesNotMatch(rendered, /思考中文/);

  const partial = sequence.slice(0, 11).reduce(apply, fresh());
  assert.equal(partial.status, "running");
  const cancelled = finish(partial, "cancelled");
  assert.equal(cancelled.entries.at(-1).status, "cancelled");
  assert.equal(cancelled.entries[1].content[0].text, "前文校准");
  assert.equal(finish(cancelled, "cancelled"), cancelled);
  const failed = apply(partial, event("error", { status: "failed", code: "execution_failed", message: "公开错误", tool_call_id: "call-1" }));
  assert.equal(failed.entries.at(-1).status, "failed");
  assert.match(renderToStaticMarkup(createElement(Transcript, { rounds: [failed] })), /公开错误/);
  assert.throws(() => apply(sequence.slice(0, 12).reduce(apply, fresh()), sequence[11]));
  assert.throws(() => apply(fresh(), end(first, [], "stop")));
  assert.equal(apply(apply(fresh(), start(first)), update(first, [text("x")], "text_delta")).entries.at(-1).content[0].text, "x");
  let nonempty = apply(fresh(), event("message_start", { message_id: first, content: [thinking("初始思考", 2), text("初始文本", 4)] }));
  nonempty = apply(nonempty, update(first, [thinking("初始思考", 2), text("累计文本", 4)], "text_delta", 4));
  assert.equal(nonempty.entries.at(-1).content[1].text, "累计文本");
  assert.throws(() => apply(nonempty, update(first, [thinking("初始思考", 2), thinking("身份变化", 4)], "thinking_delta", 4)));
  for (const reason of ["length", "aborted"]) {
    const partialCall = { ...call, content_index: 0, arguments: {} };
    let truncated = apply(fresh(), event("message_start", { message_id: first, content: [partialCall, text("保留文本", 1), thinking("保留思考", 3)] }));
    truncated = apply(truncated, end(first, [text("保留文本", 0), thinking("保留思考", 2)], reason));
    assert.deepEqual(truncated.entries.at(-1).content.map((block) => block.content_index), [0, 2]);
    assert.ok(truncated.entries.at(-1).content.every((block) => block.type !== "tool_call"));
  }
  const unknown = markUnknownSteering(round, "未知提交文本");
  assert.deepEqual(unknown.unknown_steering, ["未知提交文本"]);
  assert.equal(canSubmitChatInput("未知提交文本", unknown.unknown_steering), false);
  assert.equal(canSubmitChatInput("其他文本", unknown.unknown_steering), true);
  assert.match(renderToStaticMarkup(createElement(Transcript, { rounds: [unknown] })), /Steering 提交结果未知。/);
  assert.throws(() => apply(fresh(), { ...start(first), data: { ...start(first).data, run_id: randomUUID() } }));

  for (const stop_reason of ["stop", "length", "error", "aborted"]) {
    let current = apply(fresh(), start(first));
    current = apply(current, end(first, stop_reason === "aborted" ? [] : [text("保留")], stop_reason));
    assert.equal(current.status, "running");
    current = ["stop", "length"].includes(stop_reason)
      ? apply(current, event("done", { status: "completed", stop_reason }))
      : apply(current, event("error", { status: stop_reason === "aborted" ? "cancelled" : "failed", code: stop_reason === "aborted" ? "cancelled" : "execution_failed", message: "公开错误", tool_call_id: null }));
    assert.equal(current.status, stop_reason === "aborted" ? "cancelled" : stop_reason === "error" ? "failed" : "completed");
    if (stop_reason === "length") assert.match(renderToStaticMarkup(createElement(Transcript, { rounds: [current] })), /输出已达到上限/);
  }
  let accepted = acceptSteering(fresh(), { run_id, steering_id, status: "accepted" }, "待消费");
  accepted = apply(accepted, event("steering_status", { steering_id, status: "discarded", reason: "run_failed" }));
  assert.equal(accepted.entries.at(-1).steering.status, "discarded");
  assert.throws(() => apply(accepted, event("steering_status", { steering_id, status: "consumed" })));
  assert.throws(() => createReActParser(() => {}, run_id).finish());
  for (const invalid of [
    { event: "unknown", data: { run_id } },
    event("message_end", { message_id: first, content: [text("x"), text("y")], stop_reason: "stop" }),
    event("message_end", { message_id: first, content: [{ ...thinking("x"), thinking_signature: "secret" }], stop_reason: "stop" }),
    event("message_end", { message_id: first, content: [{ content_index: 0, type: "redacted_thinking", data: "secret" }], stop_reason: "stop" }),
    event("tool_result", { tool_call_id: "x", content: "x", is_error: "false" }),
    event("done", { status: "completed", stop_reason: "toolUse" }),
  ]) assert.throws(() => createReActParser(() => {}, run_id).feed(`event: ${invalid.event}\ndata: ${JSON.stringify(invalid.data)}\n\n`));
  assert.throws(() => createReActParser(() => {}, run_id).feed("event: done\ndata: {broken}\n\n"));
  assert.equal(validChatInput("🙂".repeat(32000)), true);
  assert.equal(validChatInput("🙂".repeat(32001)), false);
  assert.equal(validChatInput(" \n"), false);
  assert.equal(validChatInput("x".repeat(32000) + " "), false);

  if (process.env.CHAT_REAL_HTTP === "1") {
    let current = { ...fresh(), run_id: undefined };
    await runReActStream({ session_id: randomUUID(), request: "必须调用 bash，command 精确为 exit 7，然后只回复 REAL_DONE。" }, (item) => { current = apply(current, item); }, new AbortController().signal, (id) => { current = { ...current, run_id: id }; }, "http://127.0.0.1:8000/api/agent/run");
    assert.equal(current.status, "completed");
    assert.ok(current.entries.some((entry) => entry.kind === "tool" && entry.status === "failed"));
    assert.ok(current.entries.some((entry) => entry.kind === "assistant" && entry.content.some((block) => block.type === "text" && block.text.includes("REAL_DONE"))));
    const base = "http://127.0.0.1:8000";
    const closed = await fetch(`${base}/api/agent/runs/${current.run_id}/steering`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session_id: randomUUID(), message: "late" }) });
    assert.equal(closed.status, 409);
    assert.deepEqual(await closed.json(), { detail: { code: "session_mismatch", message: "运行与会话不匹配。" } });
    console.log("PASS: real backend SSE, tool business failure and HTTP rejection");
  }
  console.log("PASS: SSE chunks/multiline/Unicode, snapshots, ordered identities, steering races, tools, stop reasons, cancellation, protocol rejection, safe Markdown and collapsed thinking");
} finally { await server.close(); }
