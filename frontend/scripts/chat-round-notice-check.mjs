import assert from "node:assert/strict";
import { createServer } from "vite";

// 消息列的失败提示行为：页面在途轮次与恢复历史都渲染同一条已脱敏的 error Event 文本，
// 没有 error Event 的取消／部分输出退回固定文案，正常轮次不出现提示。
// ChatTranscript 直接消费 chatRound.ts::interruptedNotice，本脚本验证该唯一来源。

const CHAT_ID = "8d5a4c21-7e36-4b90-9f13-2c6a0d8b5e47";
const THREAD_ID = "6a1c0f3e-2b47-4d90-8e5a-0c3f7b1d9a24";
/** 后端 agent_run_error_message 的可见文本：已脱敏、不含密钥与堆栈 */
const SANITIZED = "模型调用失败：本次运行未产生计划写入，请稍后重试";
const INTERRUPTED = "本次运行被中断";
const PARTIAL = "输出未完成";

const messageEvent = (text) => ({ event: "message", data: { text } });
const errorEvent = (message) => ({ event: "error", data: { message } });

/** 一轮对话的最小形状：在途轮次 assistants 为空、run_status 为 null */
const round = (overrides) => ({
  conversation_id: THREAD_ID,
  request: "帮我生成一份训练计划",
  events: [],
  assistants: [],
  confirmations: [],
  run_status: null,
  ...overrides,
});

const server = await createServer({
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});

try {
  const { interruptedNotice } = await server.ssrLoadModule(
    "/src/features/chat/utils/chatRound.ts",
  );

  const half = messageEvent("半截输出");
  const err = errorEvent(SANITIZED);
  const entry = (n, status, content = "半截输出") => [
    { entry_id: `entry-${n}`, content, status },
  ];
  const cases = [
    ["在途轮次显示 error 事件的脱敏文本", { events: [half, err] }, SANITIZED],
    [
      "零可见输出的失败轮次同样显示 error 事件文本",
      { events: [err] },
      SANITIZED,
    ],
    [
      "恢复历史与在途轮次显示同一条 error 事件文本",
      {
        events: [half, err],
        assistants: entry(1, "failed"),
        run_status: "failed",
      },
      SANITIZED,
    ],
    [
      "取消轮次没有 error 事件时显示中断文案",
      {
        events: [half],
        assistants: entry(2, "aborted"),
        run_status: "cancelled",
      },
      INTERRUPTED,
    ],
    [
      "助手状态优先于 Run 状态给出固定文案",
      { assistants: entry(3, "partial"), run_status: "failed" },
      PARTIAL,
    ],
    ...["completed", "waiting", "running"].map((run_status) => [
      `${run_status} 轮次不得出现失败提示`,
      {
        events: [messageEvent("计划已生成"), { event: "done", data: {} }],
        assistants: entry(4, "complete", "计划已生成"),
        run_status,
      },
      undefined,
    ]),
  ];
  for (const [name, overrides, expected] of cases) {
    assert.equal(interruptedNotice(round(overrides)), expected, name);
  }

  console.log(
    `消息列失败提示验证通过：在途／恢复共用 error 事件文本 · 取消 ${INTERRUPTED} · ` +
      `部分 ${PARTIAL} · 正常轮次无提示（chat=${CHAT_ID} thread=${THREAD_ID}）`,
  );
} finally {
  await server.close();
}
