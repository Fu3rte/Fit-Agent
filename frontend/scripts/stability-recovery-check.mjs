// 运行：node --experimental-webstorage --localstorage-file=temp/stability-task4-storage scripts/stability-recovery-check.mjs
// 任务四定向验证：准入错误、取消等待、SSE 断流恢复、原操作查询、终态展示、快照确认门槛与确认结果恢复。
// 全部 HTTP/SSE 请求由本进程内的随机 loopback 端口服务处理，使用真实 fetch 与生产状态管理层。
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { mkdirSync, rmSync } from "node:fs";
import http from "node:http";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { createServer } from "vite";

const root = path.resolve(import.meta.dirname, "../temp/stability-task4");
rmSync(root, { recursive: true, force: true });
mkdirSync(root, { recursive: true });
localStorage.clear();

const vite = await createServer({
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});
const api = await vite.ssrLoadModule("/src/lib/api.ts");
const reactAgent = await vite.ssrLoadModule(
  "/src/features/chat/utils/reactAgent.ts",
);
const sessionHistory = await vite.ssrLoadModule(
  "/src/features/chat/utils/sessionHistory.ts",
);
const confirmations = await vite.ssrLoadModule(
  "/src/features/chat/utils/confirmations.ts",
);
const manager = await vite.ssrLoadModule(
  "/src/features/chat/utils/sessionRunManager.ts",
);
const { queryClient, PROVIDER_QUERY_KEY } = await vite.ssrLoadModule(
  "/src/lib/query.ts",
);
const { renderTranscript, primeEquipment } = await vite.ssrLoadModule(
  "/scripts/confirmation-display-tree.tsx",
);
await vite.close();
primeEquipment();

queryClient.setQueryData(PROVIDER_QUERY_KEY, {
  api: "openai-completions",
  base_url: "http://127.0.0.1",
  model: "stability-check",
  api_key: "test-provider-key",
  provider: "stability-check",
});

const u = (n) =>
  `${n.toString(16).padStart(8, "0")}-0000-4000-8000-000000000000`;
const frame = (event, data) =>
  `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
const textBlock = (text) => [{ content_index: 0, type: "text", text }];

const profilePayload = {
  goal: "增肌",
  experience: null,
  environment: null,
  availability: null,
  health_notes: null,
  movement_restrictions: null,
  unavailable_equipment: null,
  forbidden_exercise_ids: null,
};
const proposalId = u(0x801);
const displayEntryId = u(0x802);
const requestEntryId = u(0x803);
const sourceEntryId = u(0x804);
const confirmationEntryId = u(0x805);
const proposal = {
  proposal_id: proposalId,
  profile_id: 1,
  base_profile_version: null,
  payload: profilePayload,
};
const profileResult = {
  proposal_id: proposalId,
  profile_id: 1,
  version: 1,
  content: profilePayload,
  saved_at: 1_700_000_000_000,
};

function runObject(sessionId, runId, overrides = {}) {
  const status = overrides.status ?? "running";
  return {
    session_id: sessionId,
    run_id: runId,
    request_entry_id: overrides.request_entry_id ?? requestEntryId,
    last_entry_id: overrides.last_entry_id ?? null,
    status,
    started_at: overrides.started_at ?? 1_700_000_000_000,
    finished_at:
      overrides.finished_at ??
      (status === "running" || status === "interrupted"
        ? null
        : 1_700_000_001_000),
    error_code: overrides.error_code ?? null,
    error_message: overrides.error_message ?? null,
    stop_reason: overrides.stop_reason ?? null,
  };
}

function emptyHistory(sessionId) {
  return {
    session: {
      session_id: sessionId,
      title: "stability",
      active_leaf_id: null,
      created_at: 1_700_000_000_000,
      updated_at: 1_700_000_000_000,
    },
    entries: [],
    runs: [],
    steering: [],
    confirmations: [],
    pending_proposals: [],
  };
}

function proposalHistory(sessionId, savedOperation) {
  const completedRunId = u(0x901);
  const entries = [
    {
      type: "message",
      entry_id: requestEntryId,
      parent_id: null,
      run_id: null,
      created_at: 1,
      message: {
        role: "user",
        text: "更新我的画像",
        timestamp: 1,
        attachments: [],
      },
    },
    {
      type: "message",
      entry_id: sourceEntryId,
      parent_id: requestEntryId,
      run_id: completedRunId,
      created_at: 2,
      message: {
        role: "assistant",
        content: [
          {
            content_index: 0,
            type: "tool_call",
            tool_call_id: u(0x902),
            name: reactAgent.PREPARE_PROFILE,
            arguments: {},
          },
        ],
        stop_reason: "toolUse",
        timestamp: 2,
      },
    },
    {
      type: "message",
      entry_id: displayEntryId,
      parent_id: sourceEntryId,
      run_id: completedRunId,
      created_at: 3,
      message: {
        role: "toolResult",
        tool_call_id: u(0x902),
        tool_name: reactAgent.PREPARE_PROFILE,
        content: JSON.stringify(proposal),
        is_error: false,
        timestamp: 3,
      },
    },
  ];
  if (savedOperation !== null)
    entries.push({
      type: "message",
      entry_id: confirmationEntryId,
      parent_id: displayEntryId,
      run_id: null,
      created_at: 4,
      message: {
        role: "user",
        text: "确认保存画像",
        timestamp: 4,
        attachments: [],
      },
    });
  return {
    session: {
      session_id: sessionId,
      title: "stability",
      active_leaf_id:
        savedOperation === null ? displayEntryId : confirmationEntryId,
      created_at: 1_700_000_000_000,
      updated_at: 1_700_000_000_001,
    },
    entries,
    runs: [
      runObject(sessionId, completedRunId, {
        status: "completed",
        request_entry_id: requestEntryId,
        last_entry_id: displayEntryId,
        finished_at: 1_700_000_000_999,
      }),
    ],
    steering: [],
    confirmations: savedOperation === null ? [] : [savedOperation],
    pending_proposals:
      savedOperation === null
        ? [
            {
              business_kind: "profile",
              proposal_id: proposalId,
              status: "pending",
              request_entry_id: requestEntryId,
              source_entry_id: sourceEntryId,
              display_entry_id: displayEntryId,
              confirmation_entry_id: null,
            },
          ]
        : [],
  };
}

function savedConfirmation(sessionId, operationId) {
  return {
    session_id: sessionId,
    operation_id: operationId,
    business_kind: "profile",
    proposal_id: proposalId,
    display_entry_id: displayEntryId,
    confirmation_entry_id: confirmationEntryId,
    status: "saved",
    result: profileResult,
    error: null,
    created_at: 1_700_000_000_010,
    updated_at: 1_700_000_000_011,
  };
}

/* ===== 随机 loopback HTTP/SSE 服务 ===== */
let current = null;
const server = http.createServer(async (request, response) => {
  const url = new URL(request.url, "http://127.0.0.1");
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  let body = null;
  if (chunks.length > 0) body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  assert.notEqual(current, null, "请求没有对应的测试场景");
  const record = {
    method: request.method,
    path: url.pathname,
    headers: request.headers,
    body,
  };
  current.requests.push(record);
  try {
    await current.handle(request, response, url, body, record);
  } catch (failure) {
    response.destroy(failure);
  }
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const base = `http://127.0.0.1:${server.address().port}`;
const nativeFetch = globalThis.fetch;
globalThis.fetch = (input, init) =>
  nativeFetch(
    typeof input === "string" || input instanceof URL
      ? new URL(String(input), base)
      : input,
    init,
  );

const json = (response, status, body, headers = {}) => {
  response.writeHead(status, {
    "Content-Type": "application/json",
    ...headers,
  });
  response.end(JSON.stringify(body));
};
const detail = (code, message, extra = {}) =>
  JSON.stringify({ detail: { code, message, ...extra } });
const sseHeaders = (sessionId, operationId, runId, requestEntry) => ({
  "Content-Type": "text/event-stream",
  "Cache-Control": "no-cache",
  "X-Session-ID": sessionId,
  "X-Operation-ID": operationId,
  "X-Run-ID": runId,
  "X-Request-Entry-ID": requestEntry,
});

function useScenario(handler) {
  const scenario = { requests: [], handle: handler };
  current = scenario;
  return scenario;
}

let debugView = null;
async function waitFor(predicate, label, timeout = 10_000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await delay(20);
  }
  assert.fail(
    `等待超时：${label}\n请求：${JSON.stringify(current?.requests ?? [])}\n视图：${JSON.stringify(debugView?.() ?? null)}`,
  );
}

function openRecord(sessionId) {
  const record = manager.sessionRuns.open(sessionId, false);
  debugView = () => record.snapshot();
  record.mount(false);
  return record;
}

const passed = [];
const verify = (name) => {
  passed.push(name);
  console.log(`PASS: ${name}`);
};

try {
  /* ===== 1. 429 / 503 准入错误与安全展示 ===== */
  {
    const sessionId = randomUUID();
    let admission = "rate_limited";
    const scenario = useScenario((request, response, url, body) => {
      if (url.pathname.endsWith("/history"))
        return json(response, 200, emptyHistory(sessionId));
      if (url.pathname === "/api/agent/run") {
        if (admission === "rate_limited")
          return json(
            response,
            429,
            {
              detail: {
                code: "rate_limited",
                message: "请求过于频繁，请稍后重试。",
              },
              api_key: "sk-live-secret",
              upstream: "UPSTREAM_SECRET_BODY",
            },
            { "Retry-After": "7" },
          );
        if (admission === "capacity_exceeded")
          return json(
            response,
            503,
            JSON.parse(
              detail(
                "capacity_exceeded",
                "当前已有任务执行中，请稍后重试。",
              ),
            ),
          );
        return json(
          response,
          503,
          JSON.parse(
            detail(
              "service_unavailable",
              "服务正在恢复，暂不接受新运行。",
            ),
          ),
        );
      }
      throw new Error(`未安排：${request.method} ${url.pathname}`);
    });
    const record = openRecord(sessionId);
    await waitFor(() => record.snapshot().history === "loaded", "历史加载");
    assert.equal(record.snapshot().ready, true);

    await record.send("第一次", []);
    await waitFor(() => record.snapshot().ready, "429 拒绝后释放占用");
    const view429 = record.snapshot();
    assert.match(view429.error, /7 秒后可重试/);
    assert.doesNotMatch(view429.error, /sk-live-secret|UPSTREAM_SECRET_BODY/);
    assert.equal(view429.operations.length, 0);
    assert.equal(
      scenario.requests.filter((item) => item.path === "/api/agent/run").length,
      1,
    );

    admission = "capacity_exceeded";
    await record.send("第二次", []);
    await waitFor(() => record.snapshot().ready, "容量拒绝后释放占用");
    assert.equal(record.snapshot().error, "当前已有任务执行中，请稍后重试。");
    assert.equal(record.snapshot().operations.length, 0);

    admission = "service_unavailable";
    await record.send("第三次", []);
    await waitFor(() => record.snapshot().ready, "故障封闭反馈");
    assert.equal(
      record.snapshot().error,
      "服务正在恢复，暂不接受新运行。",
    );
    assert.equal(record.snapshot().operations.length, 0);
    assert.equal(
      scenario.requests.filter((item) => item.path === "/api/agent/run").length,
      3,
    );
    manager.sessionRuns.discard(sessionId);
    verify("429 Retry-After、503 capacity_exceeded 与 service_unavailable 安全展示");
  }

  /* ===== 2. 取消后保持“正在停止”，查询到终态才结束 ===== */
  {
    const sessionId = randomUUID();
    const operationId = randomUUID();
    const runId = randomUUID();
    let runPolls = 0;
    const scenario = useScenario((request, response, url, body) => {
      if (url.pathname.endsWith("/history"))
        return json(response, 200, emptyHistory(sessionId));
      if (url.pathname === "/api/agent/run") {
        response.writeHead(
          200,
          sseHeaders(sessionId, body.operation_id, runId, requestEntryId),
        );
        response.write(": keep-alive\n\n");
        return;
      }
      if (url.pathname === `/api/sessions/${sessionId}/runs/${runId}`) {
        runPolls += 1;
        return json(
          response,
          200,
          runPolls === 1
            ? runObject(sessionId, runId)
            : runObject(sessionId, runId, {
                status: "cancelled",
                stop_reason: "cancelled",
                error_code: "cancelled",
                error_message: "执行已取消。",
              }),
        );
      }
      throw new Error(`未安排：${request.method} ${url.pathname}`);
    });
    const record = openRecord(sessionId);
    await waitFor(() => record.snapshot().history === "loaded", "历史加载");
    assert.equal(record.snapshot().ready, true, "历史加载后允许发送");
    assert.equal(await record.send("开始执行", []), true, "发送进入执行流");
    await waitFor(
      () =>
        record.snapshot().operations.some((item) => item.run_id === runId),
      "运行身份绑定",
    );
    record.stop();
    const stopping = record.snapshot();
    assert.equal(stopping.stoppingRoundId, operationsRoundId(stopping));
    assert.equal(stopping.busy, false);
    assert.equal(stopping.ready, false);
    assert.equal(stopping.rounds.at(-1).status, "running");
    assert.equal(
      await record.send("停止中的追加", []),
      false,
      "停止期间关闭 Steering 新输入",
    );

    await waitFor(() => runPolls >= 2, "运行查询到终态");
    await waitFor(
      () =>
        record.snapshot().stoppingRoundId === null &&
        record.snapshot().rounds.at(-1).status === "cancelled",
      "取消终态结束等待",
    );
    assert.equal(record.snapshot().rounds.at(-1).error, "执行已取消。");
    assert.equal(
      scenario.requests.filter((item) => item.path === "/api/agent/run").length,
      1,
      "取消不重发新运行",
    );
    assert.equal(
      scenario.requests.filter((item) => item.path.includes("/steering")).length,
      0,
    );
    manager.sessionRuns.discard(sessionId);
    verify("取消后显示正在停止，持久化 cancelled 终态后才恢复交互");
  }

  /* ===== 2b. 停止等待期间迟到的 Steering 拒绝不解除取消等待 ===== */
  {
    const sessionId = randomUUID();
    const runId = randomUUID();
    let runPolls = 0;
    let runFinished = false;
    let pendingSteering = null;
    const cancelledHistory = {
      session: {
        session_id: sessionId,
        title: "stability",
        active_leaf_id: requestEntryId,
        created_at: 1_700_000_000_000,
        updated_at: 1_700_000_000_001,
      },
      entries: [
        {
          type: "message",
          entry_id: requestEntryId,
          parent_id: null,
          run_id: null,
          created_at: 1,
          message: {
            role: "user",
            text: "开始执行",
            timestamp: 1,
            attachments: [],
          },
        },
      ],
      runs: [
        runObject(sessionId, runId, {
          status: "cancelled",
          stop_reason: "cancelled",
          error_code: "cancelled",
          error_message: "执行已取消。",
        }),
      ],
      steering: [],
      confirmations: [],
      pending_proposals: [],
    };
    const scenario = useScenario((request, response, url, body) => {
      if (url.pathname.endsWith("/history"))
        return json(
          response,
          200,
          runFinished ? cancelledHistory : emptyHistory(sessionId),
        );
      if (url.pathname === "/api/agent/run") {
        response.writeHead(
          200,
          sseHeaders(sessionId, body.operation_id, runId, requestEntryId),
        );
        response.write(": keep-alive\n\n");
        return;
      }
      if (url.pathname === `/api/agent/runs/${runId}/steering`) {
        pendingSteering = response;
        return;
      }
      if (url.pathname === `/api/sessions/${sessionId}/runs/${runId}`) {
        runPolls += 1;
        return json(
          response,
          200,
          runFinished
            ? runObject(sessionId, runId, {
                status: "cancelled",
                stop_reason: "cancelled",
                error_code: "cancelled",
                error_message: "执行已取消。",
              })
            : runObject(sessionId, runId),
        );
      }
      throw new Error(`未安排：${request.method} ${url.pathname}`);
    });
    const record = openRecord(sessionId);
    await waitFor(() => record.snapshot().history === "loaded", "历史加载");
    assert.equal(await record.send("开始执行", []), true, "发送进入执行流");
    await waitFor(
      () =>
        record.snapshot().operations.some((item) => item.run_id === runId),
      "运行身份绑定",
    );
    const steeringResult = record.send("迟到的追加", []);
    await waitFor(() => pendingSteering !== null, "Steering 请求到达");
    record.stop();
    const stopping = record.snapshot();
    const stoppingRoundId = operationsRoundId(stopping);
    assert.equal(stopping.stoppingRoundId, stoppingRoundId);
    assert.equal(stopping.ready, false);
    assert.equal(stopping.rounds.at(-1).status, "running");

    await waitFor(() => runPolls >= 1, "停止后运行查询仍在 running");
    await delay(100);
    assert.equal(record.snapshot().rounds.at(-1).status, "running");

    json(
      pendingSteering,
      409,
      JSON.parse(detail("run_closed", "运行已关闭。")),
    );
    pendingSteering = null;
    assert.equal(await steeringResult, false, "Steering 被明确拒绝");
    const afterReject = record.snapshot();
    assert.equal(
      afterReject.stoppingRoundId,
      stoppingRoundId,
      "迟到的 Steering 拒绝保留正在停止",
    );
    assert.equal(afterReject.ready, false, "迟到的 Steering 拒绝保留交互门禁");
    assert.equal(
      afterReject.rounds.at(-1).status,
      "running",
      "原运行仍待持久化终态",
    );
    assert.equal(
      afterReject.operations.some((item) => item.kind === "steering"),
      false,
      "只结算被拒绝的输入",
    );

    runFinished = true;
    await waitFor(
      () =>
        record.snapshot().stoppingRoundId === null &&
        record.snapshot().rounds.at(-1).status === "cancelled" &&
        record.snapshot().ready,
      "持久化终态结束后恢复交互",
      10_000,
    );
    assert.equal(record.snapshot().rounds.at(-1).error, "执行已取消。");
    assert.equal(
      scenario.requests.filter((item) => item.path.includes("/steering")).length,
      1,
      "不重发被拒绝的 Steering",
    );
    manager.sessionRuns.discard(sessionId);
    verify("停止等待中迟到的 Steering 拒绝保留取消等待，持久化终态后恢复交互");
  }

  /* ===== 3. SSE 无终态 EOF，按已知 run_id 找回结果 ===== */
  {
    const sessionId = randomUUID();
    const operationId = randomUUID();
    const runId = randomUUID();
    const messageId = randomUUID();
    let runPolls = 0;
    const scenario = useScenario((request, response, url, body) => {
      if (url.pathname.endsWith("/history"))
        return json(response, 200, emptyHistory(sessionId));
      if (url.pathname === "/api/agent/run") {
        response.writeHead(
          200,
          sseHeaders(sessionId, body.operation_id, runId, requestEntryId),
        );
        response.write(
          frame("message_start", { run_id: runId, message_id: messageId, content: [] }),
        );
        response.end();
        return;
      }
      if (url.pathname === `/api/sessions/${sessionId}/runs/${runId}`) {
        runPolls += 1;
        return json(
          response,
          200,
          runObject(sessionId, runId, {
            status: "completed",
            last_entry_id: messageId,
          }),
        );
      }
      throw new Error(`未安排：${request.method} ${url.pathname}`);
    });
    const record = openRecord(sessionId);
    await waitFor(() => record.snapshot().history === "loaded", "历史加载");
    await record.send("断流检查", []);
    await waitFor(
      () => record.snapshot().rounds.at(-1)?.status === "completed",
      "按 run_id 找回完成终态",
    );
    assert.ok(runPolls >= 1);
    assert.equal(
      scenario.requests.filter((item) => item.path.includes("/operations/")).length,
      0,
      "已知 run_id 不退回操作查询",
    );
    manager.sessionRuns.discard(sessionId);
    verify("SSE 无 done/error EOF 时按原 run_id 查询持久化终态");
  }

  /* ===== 4. 响应头丢失、accepted=false 后迟到受理、查询失败恢复 ===== */
  {
    const sessionId = randomUUID();
    const runId = randomUUID();
    let operationPolls = 0;
    const scenario = useScenario((request, response, url, body) => {
      if (url.pathname.endsWith("/history"))
        return json(response, 200, emptyHistory(sessionId));
      if (url.pathname === "/api/agent/run") {
        response.writeHead(200, {
          "Content-Type": "text/event-stream",
          "X-Session-ID": sessionId,
          "X-Operation-ID": body.operation_id,
          "X-Request-Entry-ID": requestEntryId,
        });
        response.end();
        return;
      }
      if (url.pathname.startsWith(`/api/sessions/${sessionId}/operations/`)) {
        const pathOperationId = url.pathname.split("/").at(-1);
        operationPolls += 1;
        if (operationPolls === 1)
          return json(
            response,
            500,
            JSON.parse(detail("internal_error", "查询暂时失败。")),
          );
        if (operationPolls === 2)
          return json(response, 200, {
            operation_id: pathOperationId,
            session_id: sessionId,
            accepted: false,
            kind: null,
            run: null,
            steering: null,
          });
        return json(response, 200, {
          operation_id: pathOperationId,
          session_id: sessionId,
          accepted: true,
          kind: "send",
          run: runObject(sessionId, runId),
          steering: null,
        });
      }
      if (url.pathname === `/api/sessions/${sessionId}/runs/${runId}`)
        return json(
          response,
          200,
          runObject(sessionId, runId, { status: "completed" }),
        );
      throw new Error(`未安排：${request.method} ${url.pathname}`);
    });
    const record = openRecord(sessionId);
    await waitFor(() => record.snapshot().history === "loaded", "历史加载");
    await record.send("响应头丢失", []);
    await waitFor(
      () => record.snapshot().operations.length === 1,
      "保留原操作身份",
    );
    const operation = record.snapshot().operations[0];
    const roundId = record.snapshot().rounds.at(-1).id;
    for (let index = 0; index < 4; index += 1)
      record.confirmOperation(roundId, operation);

    await waitFor(
      () => record.snapshot().rounds.at(-1)?.status === "completed",
      "迟到受理后收敛终态",
      20_000,
    );
    assert.equal(operationPolls, 3, "重复恢复触发不创建并行查询");
    assert.equal(
      scenario.requests.filter((item) => item.path === "/api/agent/run").length,
      1,
      "不生成新键自动重发",
    );
    assert.equal(
      scenario.requests.filter((item) => item.path.includes("/confirmations")).length,
      0,
      "恢复查询不触发保存",
    );
    const operationPaths = scenario.requests
      .filter((item) => item.path.includes("/operations/"))
      .map((item) => item.path);
    assert.ok(
      operationPaths.every((item) => item.endsWith(operation.operation_id)),
    );
    await delay(2_500);
    assert.equal(operationPolls, 3, "终态后清理原操作查询链路");
    manager.sessionRuns.discard(sessionId);
    verify("响应头丢失、accepted=false 与查询失败均按原 operation_id 有界恢复");
  }

  /* ===== 4b. 运行查询 404 且原操作已受理仍 running 时保留退避 ===== */
  {
    const sessionId = randomUUID();
    const runId = randomUUID();
    let runPolls = 0;
    let operationPolls = 0;
    let firstRunAt = null;
    let secondRunAt = null;
    const scenario = useScenario((request, response, url, body) => {
      if (url.pathname.endsWith("/history"))
        return json(response, 200, emptyHistory(sessionId));
      if (url.pathname === "/api/agent/run") {
        response.writeHead(
          200,
          sseHeaders(sessionId, body.operation_id, runId, requestEntryId),
        );
        response.end(": heartbeat\n\n");
        return;
      }
      if (url.pathname === `/api/sessions/${sessionId}/runs/${runId}`) {
        runPolls += 1;
        if (runPolls === 1) {
          firstRunAt = performance.now();
          return json(
            response,
            404,
            JSON.parse(detail("not_found", "运行不存在。")),
          );
        }
        secondRunAt = performance.now();
        return json(
          response,
          200,
          runObject(sessionId, runId, { status: "completed" }),
        );
      }
      if (url.pathname.startsWith(`/api/sessions/${sessionId}/operations/`)) {
        operationPolls += 1;
        return json(response, 200, {
          operation_id: url.pathname.split("/").at(-1),
          session_id: sessionId,
          accepted: true,
          kind: "send",
          run: runObject(sessionId, runId),
          steering: null,
        });
      }
      throw new Error(`未安排：${request.method} ${url.pathname}`);
    });
    const record = openRecord(sessionId);
    await waitFor(() => record.snapshot().history === "loaded", "历史加载");
    await record.send("运行查询 404 退避", []);
    await waitFor(
      () => record.snapshot().rounds.at(-1)?.status === "completed",
      "404 后按退避收敛终态",
      10_000,
    );
    assert.equal(runPolls, 2, "运行查询按退避执行两次");
    assert.equal(operationPolls, 1, "同一运行 running 时只核对一次原操作");
    assert.ok(firstRunAt !== null && secondRunAt !== null);
    assert.ok(
      secondRunAt - firstRunAt >= 1_500,
      `运行查询间隔不足：${Math.round(secondRunAt - firstRunAt)}ms`,
    );
    assert.equal(
      scenario.requests.filter((item) => item.path === "/api/agent/run").length,
      1,
      "不重发新运行",
    );
    assert.equal(
      scenario.requests.filter((item) => item.path.includes("/confirmations"))
        .length,
      0,
    );
    manager.sessionRuns.discard(sessionId);
    verify("运行查询 404 时保留退避，不放大恢复查询");
  }

  /* ===== 5. SSE 注释、UTF-8 分块与跨块事件 ===== */
  {
    const sessionId = randomUUID();
    const operationId = randomUUID();
    const runId = randomUUID();
    const messageId = randomUUID();
    const text = "中文分块完成";
    const scenario = useScenario((request, response, url, body) => {
      if (url.pathname.endsWith("/history"))
        return json(response, 200, emptyHistory(sessionId));
      if (url.pathname === "/api/agent/run") {
        response.writeHead(
          200,
          sseHeaders(sessionId, body.operation_id, runId, requestEntryId),
        );
        const payload = Buffer.from(
          [
            ": keep-alive\n\n",
            frame("message_start", {
              run_id: runId,
              message_id: messageId,
              content: [],
            }),
            ": keep-alive\n\n",
            frame("message_update", {
              run_id: runId,
              message_id: messageId,
              content: textBlock(text),
              content_index: 0,
              update_type: "text_delta",
            }),
            frame("message_end", {
              run_id: runId,
              message_id: messageId,
              content: textBlock(text),
              stop_reason: "stop",
              entry_id: messageId,
              parent_id: null,
            }),
            frame("done", {
              run_id: runId,
              status: "completed",
              stop_reason: "stop",
            }),
          ].join(""),
          "utf8",
        );
        const middle = payload.indexOf(Buffer.from("中")) + 1;
        const cuts = [0, 7, middle, middle + 3, payload.length];
        void (async () => {
          for (let index = 1; index < cuts.length; index += 1) {
            response.write(payload.subarray(cuts[index - 1], cuts[index]));
            await delay(10);
          }
          response.end();
        })();
        return;
      }
      throw new Error(`未安排：${request.method} ${url.pathname}`);
    });
    const record = openRecord(sessionId);
    await waitFor(() => record.snapshot().history === "loaded", "历史加载");
    await record.send("分块检查", []);
    await waitFor(
      () => record.snapshot().rounds.at(-1)?.status === "completed",
      "跨块 SSE 完成",
    );
    const round = record.snapshot().rounds.at(-1);
    const assistant = round.entries.find((entry) => entry.kind === "assistant");
    assert.equal(assistant.content[0].text, text);
    assert.equal(record.snapshot().error, undefined);
    manager.sessionRuns.discard(sessionId);
    verify("SSE 注释心跳、UTF-8 分块与跨块事件解析");
  }

  /* ===== 6. 五类停止错误码的解析与终态展示 ===== */
  {
    const codes = [
      ["model_timeout", "failed", "模型响应超时，请重新发起请求。"],
      ["tool_timeout", "failed", "工具执行超时，已结束本次运行。"],
      ["run_timeout", "failed", "本次运行已超时结束。"],
      ["slow_consumer", "cancelled", "客户端读取过慢，已结束本次运行。"],
      ["output_too_large", "failed", "输出内容超过单事件上限，已结束本次运行。"],
    ];
    for (const [code, status, message] of codes) {
      const runId = randomUUID();
      const events = [];
      const parser = reactAgent.createReActParser(
        (event) => events.push(event),
        runId,
      );
      parser.feed(
        frame("error", {
          run_id: runId,
          status,
          code,
          message,
          tool_call_id: null,
        }),
      );
      parser.finish();
      const ended = reactAgent.applyReActEvent(
        { id: runId, run_id: runId, status: "running", entries: [] },
        events[0],
      );
      assert.equal(ended.status, status);
      assert.equal(ended.error, message);
    }
    assert.equal(
      reactAgent.runErrorMessage({
        status: "failed",
        error_message: null,
        stop_reason: "run_timeout",
      }),
      "本次运行已超时结束。",
    );
    const interrupted = reactAgent.concludeReActRound(
      { id: randomUUID(), status: "running", entries: [] },
      "interrupted",
    );
    assert.equal(interrupted.status, "interrupted");
    assert.equal(
      reactAgent.concludeReActRound(interrupted, "cancelled").status,
      "interrupted",
      "已取得终态不被迟到状态覆盖",
    );
    verify("run_timeout、model_timeout、tool_timeout、slow_consumer、output_too_large 全链路");
  }

  /* ===== 7. 快照完整性、确认门槛与 output_too_large ===== */
  {
    const runId = randomUUID();
    const messageId = randomUUID();
    const toolCallId = randomUUID();
    let round = { id: runId, run_id: runId, status: "running", entries: [] };
    const feed = (event, data) => {
      const events = [];
      const parser = reactAgent.createReActParser(
        (item) => events.push(item),
        runId,
      );
      parser.feed(frame(event, { run_id: runId, ...data }));
      round = reactAgent.applyReActEvent(round, events[0]);
    };
    feed("message_start", { message_id: messageId, content: [] });
    feed("message_end", {
      message_id: messageId,
      content: [
        {
          content_index: 0,
          type: "tool_call",
          tool_call_id: toolCallId,
          name: reactAgent.PREPARE_PROFILE,
          arguments: {},
        },
      ],
      stop_reason: "toolUse",
      entry_id: messageId,
      parent_id: null,
    });
    feed("tool_start", {
      tool_call_id: toolCallId,
      name: reactAgent.PREPARE_PROFILE,
      arguments: {},
    });
    const partial = '{"proposal_id":"' + proposalId + '","payload":';
    feed("tool_execution_update", {
      tool_call_id: toolCallId,
      tool_name: reactAgent.PREPARE_PROFILE,
      content: partial,
      is_error: false,
    });
    const partialEntry = round.entries.find((entry) => entry.id === toolCallId);
    assert.equal(partialEntry.profile, undefined);
    assert.equal(partialEntry.content, partial);

    assert.throws(
      () =>
        feed("tool_result", {
          tool_call_id: toolCallId,
          content: partial,
          is_error: false,
          entry_id: displayEntryId,
          parent_id: messageId,
        }),
      /Unexpected end|JSON/,
      "不完整业务快照触发协议错误",
    );
    feed("error", {
      status: "failed",
      code: "output_too_large",
      message: "输出内容超过单事件上限，已结束本次运行。",
      tool_call_id: null,
    });
    assert.equal(round.status, "failed");
    assert.equal(
      confirmations.confirmationButtonState(
        "profile",
        proposalId,
        displayEntryId,
        [],
        [],
        true,
      ),
      "unavailable",
      "超限与部分快照不可确认",
    );

    let complete = { id: runId, run_id: runId, status: "running", entries: [] };
    const feedComplete = (event, data) => {
      const events = [];
      const parser = reactAgent.createReActParser(
        (item) => events.push(item),
        runId,
      );
      parser.feed(frame(event, { run_id: runId, ...data }));
      complete = reactAgent.applyReActEvent(complete, events[0]);
    };
    feedComplete("message_start", { message_id: messageId, content: [] });
    feedComplete("message_end", {
      message_id: messageId,
      content: [
        {
          content_index: 0,
          type: "tool_call",
          tool_call_id: toolCallId,
          name: reactAgent.PREPARE_PROFILE,
          arguments: {},
        },
      ],
      stop_reason: "toolUse",
      entry_id: messageId,
      parent_id: null,
    });
    feedComplete("tool_start", {
      tool_call_id: toolCallId,
      name: reactAgent.PREPARE_PROFILE,
      arguments: {},
    });
    feedComplete("tool_result", {
      tool_call_id: toolCallId,
      content: JSON.stringify(proposal),
      is_error: false,
      entry_id: displayEntryId,
      parent_id: messageId,
    });
    const completeEntry = complete.entries.find(
      (entry) => entry.kind === "tool",
    );
    assert.equal(completeEntry.profile.proposal_id, proposalId);
    const reference = {
      business_kind: "profile",
      proposal_id: proposalId,
      status: "pending",
      request_entry_id: requestEntryId,
      source_entry_id: sourceEntryId,
      display_entry_id: displayEntryId,
      confirmation_entry_id: null,
    };
    assert.equal(
      confirmations.confirmationButtonState(
        "profile",
        proposalId,
        displayEntryId,
        [reference],
        [],
        true,
      ),
      "available",
      "完整快照与有效引用开放确认",
    );
    assert.equal(
      confirmations.confirmationButtonState(
        "profile",
        proposalId,
        displayEntryId,
        [reference],
        [],
        false,
      ),
      "busy",
      "执行期间禁用确认",
    );
    verify("不完整帧与 output_too_large 不可确认，完整有效快照保持确认资格");
  }

  /* ===== 8. 确认结果未知按原操作 ID 恢复；失效/不完整目标不提交 ===== */
  {
    const sessionId = randomUUID();
    let posted = null;
    let postedOperation = null;
    let historyCalls = 0;
    const scenario = useScenario((request, response, url, body) => {
      if (url.pathname.endsWith("/history")) {
        historyCalls += 1;
        return json(
          response,
          200,
          proposalHistory(sessionId, postedOperation),
        );
      }
      if (url.pathname === `/api/sessions/${sessionId}/confirmations`) {
        posted = body;
        postedOperation = savedConfirmation(sessionId, body.operation_id);
        response.destroy();
        return;
      }
      if (
        url.pathname ===
        `/api/sessions/${sessionId}/confirmations/${posted?.operation_id}`
      )
        return json(response, 200, postedOperation);
      throw new Error(`未安排：${request.method} ${url.pathname}`);
    });
    const record = openRecord(sessionId);
    await waitFor(
      () => record.snapshot().history === "loaded" && record.snapshot().ready,
      "历史加载",
    );
    assert.equal(record.snapshot().proposals.length, 1);
    assert.equal(
      record.snapshot().rounds.some((round) =>
        round.entries.some(
          (entry) => entry.kind === "tool" && entry.profile !== undefined,
        ),
      ),
      true,
    );

    record.confirmProposal("profile", proposalId, randomUUID());
    await delay(100);
    assert.equal(posted, null, "展示节点不匹配不提交");
    record.confirmProposal("profile", randomUUID(), displayEntryId);
    await delay(100);
    assert.equal(posted, null, "无有效引用不提交");

    record.confirmProposal("profile", proposalId, displayEntryId);
    await waitFor(() => posted !== null, "确认请求提交");
    await waitFor(
      () =>
        record.snapshot().rounds.some(
          (round) => round.confirmation?.status === "saved",
        ),
      "原确认操作恢复为已保存",
      10_000,
    );
    assert.equal(
      record.snapshot().confirmations.length,
      0,
      "已核实终态移出未决确认",
    );
    assert.equal(
      scenario.requests.filter(
        (item) =>
          item.method === "POST" &&
          item.path === `/api/sessions/${sessionId}/confirmations`,
      ).length,
      1,
      "不换新键重复保存",
    );
    const queries = scenario.requests.filter(
      (item) =>
        item.method === "GET" && item.path.includes("/confirmations/"),
    );
    assert.ok(queries.length >= 1);
    assert.ok(
      queries.every((item) => item.path.endsWith(posted.operation_id)),
      "按该次确认的原操作 ID 查询",
    );
    assert.equal(
      confirmations.readConfirmationLedger(sessionId).length,
      0,
      "恢复后清理确认账本",
    );
    assert.ok(historyCalls >= 2, "保存终态后刷新历史");
    manager.sessionRuns.discard(sessionId);
    verify("确认结果未知按原 operation_id 恢复，已保存结果保留");
  }

  /* ===== 8b. 确认首次未登记，迟到保存后自动收敛 ===== */
  {
    const sessionId = randomUUID();
    let posted = null;
    let postedOperation = null;
    let historyCalls = 0;
    let gets = 0;
    const getTimes = [];
    const scenario = useScenario((request, response, url, body) => {
      if (url.pathname.endsWith("/history")) {
        historyCalls += 1;
        return json(
          response,
          200,
          proposalHistory(sessionId, postedOperation),
        );
      }
      if (url.pathname === `/api/sessions/${sessionId}/confirmations`) {
        posted = body;
        postedOperation = savedConfirmation(sessionId, body.operation_id);
        response.destroy();
        return;
      }
      if (
        url.pathname ===
        `/api/sessions/${sessionId}/confirmations/${posted?.operation_id}`
      ) {
        gets += 1;
        getTimes.push(performance.now());
        if (gets === 1)
          return json(
            response,
            404,
            JSON.parse(detail("confirmation_not_found", "确认操作不存在。")),
          );
        return json(response, 200, postedOperation);
      }
      throw new Error(`未安排：${request.method} ${url.pathname}`);
    });
    const record = openRecord(sessionId);
    await waitFor(
      () => record.snapshot().history === "loaded" && record.snapshot().ready,
      "历史加载",
    );
    assert.equal(record.snapshot().proposals.length, 1);
    record.confirmProposal("profile", proposalId, displayEntryId);
    await waitFor(() => posted !== null, "确认请求提交");
    await waitFor(
      () =>
        record.snapshot().rounds.some(
          (round) => round.confirmation?.status === "saved",
        ),
      "迟到保存自动收敛",
      10_000,
    );
    assert.equal(
      scenario.requests.filter(
        (item) =>
          item.method === "POST" &&
          item.path === `/api/sessions/${sessionId}/confirmations`,
      ).length,
      1,
      "不自动重放保存",
    );
    assert.ok(gets >= 2, "首次未登记后继续核对");
    assert.ok(
      getTimes[1] - getTimes[0] >= 800,
      `确认核对间隔不足：${Math.round(getTimes[1] - getTimes[0])}ms`,
    );
    const queries = scenario.requests.filter(
      (item) =>
        item.method === "GET" && item.path.includes("/confirmations/"),
    );
    assert.ok(
      queries.every((item) => item.path.endsWith(posted.operation_id)),
      "保持原确认操作 ID",
    );
    assert.equal(record.snapshot().confirmations.length, 0);
    assert.equal(
      confirmations.readConfirmationLedger(sessionId).length,
      0,
    );
    assert.ok(historyCalls >= 2, "保存终态后刷新历史");
    manager.sessionRuns.discard(sessionId);
    verify("确认首次未登记后按原操作 ID 有界核对，迟到保存自动收敛");
  }

  /* ===== 9. 真实组件渲染：正在停止与已中断 ===== */
  {
    const sessionId = randomUUID();
    const runId = randomUUID();
    const render = (rounds, stoppingRoundId) =>
      renderTranscript({
        sessionId,
        rounds,
        stoppingRoundId,
        onRetry: () => {},
        onWithdraw: () => {},
        canEdit: false,
        canConfirm: false,
        proposals: [],
        confirmations: [],
        onConfirm: () => {},
        onRetryConfirmation: () => {},
        onEdit: () => Promise.resolve(true),
        onRegenerate: () => {},
      });
    const running = {
      id: runId,
      run_id: runId,
      status: "running",
      entries: [{ kind: "user", id: requestEntryId, request: "执行中的请求。" }],
    };
    const stoppingMarkup = render([running], runId);
    assert.equal(stoppingMarkup.includes("正在停止"), true);
    assert.equal(stoppingMarkup.includes('aria-label="正在停止"'), true);
    assert.equal(stoppingMarkup.includes('aria-label="执行中"'), false);
    const runningMarkup = render([running], null);
    assert.equal(runningMarkup.includes("正在停止"), false);
    assert.equal(runningMarkup.includes('aria-label="执行中"'), true);
    const interruptedMarkup = render(
      [{ id: runId, run_id: runId, status: "interrupted", entries: [] }],
      null,
    );
    assert.equal(interruptedMarkup.includes("已中断"), true);
    verify("真实组件渲染正在停止与已中断状态");
  }

  /* ===== 10. interrupted 展示与已保存结果保留 ===== */
  {
    const sessionId = randomUUID();
    const interruptedRun = u(0xa01);
    const rounds = sessionHistory.historyToRounds({
      session: {
        session_id: sessionId,
        title: "stability",
        active_leaf_id: null,
        created_at: 1,
        updated_at: 2,
      },
      entries: [],
      runs: [
        runObject(sessionId, interruptedRun, {
          status: "interrupted",
          request_entry_id: requestEntryId,
        }),
      ],
      steering: [],
      confirmations: [],
      pending_proposals: [],
    });
    assert.equal(rounds.length, 1);
    assert.equal(rounds[0].status, "interrupted");
    assert.equal(rounds[0].error, undefined);
    const saved = confirmations.confirmationOperationDisplay(
      savedConfirmation(sessionId, randomUUID()),
    );
    assert.equal(saved.status, "saved");
    assert.equal(saved.text, "已保存");
    verify("interrupted 展示与已保存结果保留");
  }
} finally {
  current = null;
  await new Promise((resolve) => server.close(resolve));
}

function operationsRoundId(view) {
  const operation = view.operations.find((item) => item.kind !== "steering");
  return operation?.operation_id ?? null;
}

console.log(`PASS: 任务四定向验证完成（${passed.length} 组）`);
