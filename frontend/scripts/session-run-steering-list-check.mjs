// 运行：node scripts/session-run-steering-list-check.mjs
// 纯解析检查：会话运行列表与指定运行 Steering 列表的必填字段、UUID、枚举、时间、null 组合与身份归属（不触达后端）。
import assert from "node:assert/strict";
import { createServer } from "vite";

const u = (n) => `${n.toString(16).padStart(8, "0")}-0000-4000-8000-000000000000`;
const sessionId = u(1);
const runId = u(2);
const otherSession = u(3);
const otherRun = u(4);

const runItem = (overrides = {}) => ({
  session_id: sessionId,
  run_id: runId,
  request_entry_id: u(10),
  last_entry_id: u(11),
  status: "completed",
  started_at: 1_000,
  finished_at: 2_000,
  error_code: null,
  error_message: null,
  ...overrides,
});

const steeringItem = (overrides = {}) => ({
  session_id: sessionId,
  run_id: runId,
  steering_id: u(20),
  text: "追加输入。",
  timestamp: 1_500,
  status: "pending",
  entry_id: null,
  reason: null,
  created_at: 1_600,
  updated_at: 1_600,
  ...overrides,
});

/** 协议异常必须就地抛出错误，不得转换为空列表 */
const invalid = (label, pattern, parse) => assert.throws(parse, pattern, label);

const server = await createServer({
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});

try {
  const api = await server.ssrLoadModule("/src/lib/api.ts");
  const { parseSessionRunList, parseRunSteeringList } = api;
  const runs = (items) => () => parseSessionRunList(sessionId, { session_id: sessionId, runs: items });
  const steering = (items) => () =>
    parseRunSteeringList(sessionId, runId, { session_id: sessionId, run_id: runId, steering: items });

  // ===== 会话运行列表（session-list-contract §2）=====

  // 1. 会话存在且没有运行：空数组正常返回
  assert.deepEqual(parseSessionRunList(sessionId, { session_id: sessionId, runs: [] }), {
    session_id: sessionId,
    runs: [],
  }, "空运行列表返回空数组");

  // 2. 五种运行状态全部解析，字段原值保留；interrupted 的未知结束时间保持 null
  const statuses = ["running", "completed", "failed", "cancelled", "interrupted"];
  const allStatuses = statuses.map((status, index) =>
    runItem({
      run_id: u(100 + index),
      status,
      finished_at: status === "running" || status === "interrupted" ? null : 2_000 + index,
      error_code: status === "failed" ? "execution_failed" : null,
      error_message: status === "failed" ? "执行失败。" : null,
    }),
  );
  assert.deepEqual(runs(allStatuses)(), { session_id: sessionId, runs: allStatuses }, "全部运行状态与 null 字段原样返回");

  // 3. 独立列表不要求节点位于当前历史链：请求节点与末节点均可为分支外身份
  const detached = runItem({ request_entry_id: u(50), last_entry_id: u(51), status: "failed", finished_at: 3_000, error_code: "credential_detected", error_message: "安全失败。" });
  assert.deepEqual(runs([detached])().runs[0], detached, "分支外节点身份不影响列表解析");

  // 4. 可空字段与必填字段：last_entry_id/finished_at/error_code/error_message 允许 null，缺失即报错
  assert.equal(runs([runItem({ last_entry_id: null, finished_at: null })])().runs[0].last_entry_id, null, "可空字段接受 JSON null");
  invalid("缺少 error_code 报错", /无效/, runs([{ ...runItem(), error_code: undefined }]));
  invalid("缺少 run_id 报错", /无效/, runs([{ ...runItem(), run_id: undefined }]));

  // 5. 身份校验：顶层及每项 session_id 必须等于路径会话
  invalid("顶层会话身份不一致报错", /不匹配/, () => parseSessionRunList(sessionId, { session_id: otherSession, runs: [] }));
  invalid("顶层会话身份非 UUID 报错", /无效/, () => parseSessionRunList(sessionId, { session_id: "session-1", runs: [] }));
  invalid("列表项会话身份不一致报错", /不匹配/, runs([runItem({ session_id: otherSession })]));
  invalid("列表项 run_id 非标准 UUID 报错", /无效/, runs([runItem({ run_id: "00000000000040008000000000000009" })]));

  // 6. 枚举与时间校验：状态枚举、毫秒整数
  invalid("未知运行状态报错", /无效/, runs([runItem({ status: "queued" })]));
  invalid("started_at 非整数报错", /无效/, runs([runItem({ started_at: 1_000.5 })]));
  invalid("started_at 为负报错", /无效/, runs([runItem({ started_at: -1 })]));
  invalid("started_at 为字符串报错", /无效/, runs([runItem({ started_at: "1000" })]));
  invalid("finished_at 非空字符串报错", /无效/, runs([runItem({ finished_at: "2000" })]));
  invalid("error_message 非空空串报错", /无效/, runs([runItem({ error_message: "" })]));

  // 7. 响应结构：runs 必须为数组，正文必须为对象
  invalid("runs 非数组报错", /无效/, () => parseSessionRunList(sessionId, { session_id: sessionId, runs: {} }));
  invalid("缺少 runs 字段报错", /无效/, () => parseSessionRunList(sessionId, { session_id: sessionId }));
  invalid("响应正文非对象报错", /无效/, () => parseSessionRunList(sessionId, []));

  // ===== 指定运行的 Steering 列表（§3）=====

  // 8. 运行存在且没有输入：空数组正常返回，顶层身份完整
  assert.deepEqual(parseRunSteeringList(sessionId, runId, { session_id: sessionId, run_id: runId, steering: [] }), {
    session_id: sessionId,
    run_id: runId,
    steering: [],
  }, "空输入列表返回空数组");

  // 9. 四种输入状态及字段组合全部解析；consumed 关联节点、discarded 覆盖四种丢弃原因
  const allSteeringStatuses = [
    steeringItem({ steering_id: u(200), status: "pending" }),
    steeringItem({ steering_id: u(201), status: "withdrawn" }),
    steeringItem({ steering_id: u(202), status: "consumed", entry_id: u(210), reason: null }),
    ...["completed", "failed", "cancelled", "interrupted"].map((reason, index) =>
      steeringItem({ steering_id: u(220 + index), status: "discarded", entry_id: null, reason }),
    ),
  ];
  assert.deepEqual(steering(allSteeringStatuses)(), {
    session_id: sessionId,
    run_id: runId,
    steering: allSteeringStatuses,
  }, "全部输入状态与字段组合原样返回");

  // 10. 文本与消息时间戳保留接收原值：首尾空白、换行与原文长度不改动
  const verbatim = steeringItem({ text: "  第一行\n第二行　带全角空格  ", timestamp: 1_725_000_000_123 });
  assert.equal(steering([verbatim])().steering[0].text, verbatim.text, "输入正文保留原文");
  assert.equal(steering([verbatim])().steering[0].timestamp, verbatim.timestamp, "消息时间戳保留原值");

  // 11. 身份校验：顶层及每项同时校验会话与运行身份
  invalid("顶层运行身份不一致报错", /不匹配/, () =>
    parseRunSteeringList(sessionId, runId, { session_id: sessionId, run_id: otherRun, steering: [] }),
  );
  invalid("顶层会话身份不一致报错", /不匹配/, () =>
    parseRunSteeringList(sessionId, runId, { session_id: otherSession, run_id: runId, steering: [] }),
  );
  invalid("列表项运行归属不一致报错", /不匹配/, steering([steeringItem({ run_id: otherRun })]));
  invalid("列表项会话归属不一致报错", /不匹配/, steering([steeringItem({ session_id: otherSession })]));

  // 12. 状态字段组合违规：consumed 必须有节点，discarded 必须有原因，其余两者为 null
  invalid("consumed 缺少节点报错", /无效/, steering([steeringItem({ status: "consumed", entry_id: null })]));
  invalid("consumed 带丢弃原因报错", /无效/, steering([steeringItem({ status: "consumed", entry_id: u(230), reason: "completed" })]));
  invalid("discarded 缺少原因报错", /无效/, steering([steeringItem({ status: "discarded", reason: null })]));
  invalid("discarded 带节点报错", /无效/, steering([steeringItem({ status: "discarded", entry_id: u(231), reason: "failed" })]));
  invalid("pending 带节点报错", /无效/, steering([steeringItem({ status: "pending", entry_id: u(232) })]));
  invalid("withdrawn 带原因报错", /无效/, steering([steeringItem({ status: "withdrawn", reason: "cancelled" })]));
  invalid("未知丢弃原因报错", /无效/, steering([steeringItem({ status: "discarded", reason: "timeout" })]));

  // 13. 枚举、时间与必填字段校验：列表 timestamp 为 UTC Unix 毫秒整数（§3、§4）
  invalid("未知输入状态报错", /无效/, steering([steeringItem({ status: "accepted" })]));
  invalid("text 空串报错", /无效/, steering([steeringItem({ text: "" })]));
  invalid("timestamp 非有限数值报错", /无效/, steering([steeringItem({ timestamp: Number.POSITIVE_INFINITY })]));
  invalid("timestamp 为字符串报错", /无效/, steering([steeringItem({ timestamp: "1500" })]));
  invalid("timestamp 为负报错", /无效/, steering([steeringItem({ timestamp: -1 })]));
  invalid("timestamp 非整数报错", /无效/, steering([steeringItem({ timestamp: 1_500.5 })]));
  invalid("created_at 非整数报错", /无效/, steering([steeringItem({ created_at: 1_600.5 })]));
  invalid("updated_at 为 null 报错", /无效/, steering([steeringItem({ updated_at: null })]));
  invalid("缺少 steering_id 报错", /无效/, steering([{ ...steeringItem(), steering_id: undefined }]));
  invalid("steering 非数组报错", /无效/, () =>
    parseRunSteeringList(sessionId, runId, { session_id: sessionId, run_id: runId, steering: {} }),
  );
  invalid("响应正文非对象报错", /无效/, () => parseRunSteeringList(sessionId, runId, null));

  console.log("PASS: 运行列表与 Steering 列表的空数组、五种运行状态、四种输入状态及丢弃原因、原文与时间保留、身份归属、枚举/null 组合与协议异常报错");
} finally {
  await server.close();
}
