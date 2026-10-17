// 运行：node scripts/confirmation-ledger-check.mjs（package scripts 已附 --experimental-webstorage）
// 前端行为检查：确认提交、占用与阻断、结果未知与显式重试、拒绝与失效、业务失败边界、刷新恢复、删除隔离、业务查询刷新。
// 说明：任务 2 尚未提供真实确认接口，本脚本在测试进程内用 Response 馈入生产请求层与解析层，
// 只验证前端账本与状态机行为，不代表任何后端成功结果。
import assert from "node:assert/strict";
import { mkdirSync, rmSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { QueryObserver } from "@tanstack/react-query";
import { createServer } from "vite";

const root = path.resolve(import.meta.dirname, "../temp/confirmation-ledger");
rmSync(root, { recursive: true, force: true });
mkdirSync(root, { recursive: true });
localStorage.clear();

const server = await createServer({
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});
const api = await server.ssrLoadModule("/src/lib/api.ts");
const manager = await server.ssrLoadModule(
  "/src/features/chat/utils/sessionRunManager.ts",
);
const confirmations = await server.ssrLoadModule(
  "/src/features/chat/utils/confirmations.ts",
);
const { queryClient } = await server.ssrLoadModule("/src/lib/query.ts");
await server.close();

const u = (n) =>
  `${n.toString(16).padStart(8, "0")}-0000-4000-8000-000000000000`;
const profileContent = {
  goal: "增肌",
  experience: null,
  environment: null,
  availability: null,
  health_notes: null,
  movement_restrictions: null,
  unavailable_equipment: null,
  forbidden_exercise_ids: null,
};
const proposalId = u(8);
const profileResult = {
  proposal_id: proposalId,
  profile_id: 1,
  version: 3,
  content: profileContent,
  saved_at: 1_700_000_000_000,
};

/* ===== 传输馈入：按 method 与去身份化路径安排响应，一次性优先、粘滞兜底 ===== */
const requests = [];
let oneShot = new Map();
let sticky = new Map();
const routeKey = (method, url) =>
  `${method} ${url.replace(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi, ":id")}`;
const setRoute = (name, reply) => {
  const list = oneShot.get(name) ?? [];
  list.push(reply);
  oneShot.set(name, list);
};
const setSticky = (name, reply) => sticky.set(name, reply);
const takeRoute = (name) => {
  const list = oneShot.get(name);
  if (list !== undefined && list.length > 0) return list.shift();
  if (sticky.has(name)) return sticky.get(name);
  throw new Error(`未安排的请求：${name}`);
};
globalThis.fetch = async (input, init) => {
  const url = String(input);
  const method = init?.method ?? "GET";
  const body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
  requests.push({ method, url, body });
  const handler = takeRoute(routeKey(method, url));
  const value =
    typeof handler === "function" ? await handler({ method, url, body }) : handler;
  if (value instanceof Error) throw value;
  if (value.defer !== undefined) await value.defer;
  return new Response(JSON.stringify(value.payload), {
    status: value.status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
};
const ok = (payload) => ({ status: 200, payload });
const http = (status, payload) => ({ status, payload });
const rejected = (status, code, message, extra) =>
  http(status, { detail: { code, message, ...extra } });
const HISTORY = "GET /api/sessions/:id/history";
const POST = "POST /api/sessions/:id/confirmations";
const GET = "GET /api/sessions/:id/confirmations/:id";
const RUN = "POST /api/agent/run";
const PROVIDER = "GET /api/provider";
/** 每个用例开始前的路由复位：确认提交与查询一律重新安排，避免跨用例串扰 */
const resetRoutes = () => {
  oneShot = new Map();
  sticky = new Map();
  requests.length = 0;
  installBusinessRoutes();
};

/* ===== 历史投影：请求节点 → 助手准备调用 → 完整展示节点，可追加确认节点 ===== */
const ids = {
  requestEntry: u(2),
  sourceEntry: u(3),
  displayEntry: u(4),
  confirmationEntry: u(5),
  runId: u(20),
};
/** 一条快照链：用户请求 → 助手准备调用 → 准备结果展示，各自绑定独立运行与快照标识 */
const defaultChain = {
  request: ids.requestEntry,
  source: ids.sourceEntry,
  display: ids.displayEntry,
  proposal: proposalId,
  run: ids.runId,
  tool: "tc1",
};
const secondChain = {
  request: u(85),
  source: u(86),
  display: u(87),
  proposal: u(88),
  run: u(89),
  tool: "tc2",
};
const node = (entryId, parentId, role, chain = defaultChain) => ({
  type: "message",
  entry_id: entryId,
  parent_id: parentId,
  run_id: role === "user" ? null : chain.run,
  created_at: 1,
  message:
    role === "user"
      ? { role: "user", text: "整理画像", timestamp: 1, attachments: [] }
      : role === "assistant"
        ? {
            role: "assistant",
            content: [
              {
                content_index: 0,
                type: "tool_call",
                tool_call_id: chain.tool,
                name: "prepare_profile_update",
                arguments: {
                  profile_id: 1,
                  base_profile_version: 2,
                  payload: profileContent,
                },
              },
            ],
            stop_reason: "toolUse",
            timestamp: 1,
          }
        : {
            role: "toolResult",
            tool_call_id: chain.tool,
            tool_name: "prepare_profile_update",
            content: JSON.stringify({
              proposal_id: chain.proposal,
              profile_id: 1,
              base_profile_version: 2,
              payload: profileContent,
            }),
            is_error: false,
            timestamp: 1,
          },
});
const referenceFor = (chain, overrides) => ({
  business_kind: "profile",
  proposal_id: chain.proposal,
  status: "pending",
  request_entry_id: chain.request,
  source_entry_id: chain.source,
  display_entry_id: chain.display,
  confirmation_entry_id: null,
  ...overrides,
});
const pendingReference = (overrides) => referenceFor(defaultChain, overrides);
const operation = (sessionId, overrides) => ({
  session_id: sessionId,
  operation_id: u(7),
  business_kind: "profile",
  proposal_id: proposalId,
  display_entry_id: ids.displayEntry,
  confirmation_entry_id: ids.confirmationEntry,
  status: "saved",
  result: profileResult,
  error: null,
  created_at: 1,
  updated_at: 2,
  ...overrides,
});
const posts = () =>
  requests.filter(
    (item) => item.method === "POST" && item.url.endsWith("/confirmations"),
  );
/** 最近一次提交的操作身份：响应对象必须与之逐字一致 */
const lastPostId = () => posts().at(-1).body.operation_id;
const agentRuns = () => requests.filter((item) => item.url === RUN.slice(5));
const agentEdits = () => requests.filter((item) => item.url === "/api/agent/edit");
const queries = (operationId) =>
  requests.filter((item) => item.url.includes(`/confirmations/${operationId}`));
const resolve = (value) => (typeof value === "function" ? value() : value);
const historyPayload = (sessionId, current) => {
  const chains = resolve(current.chains);
  const confirmations = resolve(current.confirmations);
  const entries = [];
  let parent = null;
  for (const chain of chains) {
    for (const [entryId, role] of [
      [chain.request, "user"],
      [chain.source, "assistant"],
      [chain.display, "toolResult"],
    ]) {
      entries.push(node(entryId, parent, role, chain));
      parent = entryId;
    }
  }
  const emitted = new Set();
  for (const item of confirmations) {
    // 同一确认节点上的重复操作只对应一个节点（§7.6）
    if (emitted.has(item.confirmation_entry_id)) continue;
    emitted.add(item.confirmation_entry_id);
    entries.push(node(item.confirmation_entry_id, parent, "user"));
    parent = item.confirmation_entry_id;
  }
  return {
    session: {
      session_id: sessionId,
      title: "会话",
      active_leaf_id: parent,
      created_at: 1,
      updated_at: 2,
    },
    entries,
    runs: chains.map((chain) => ({
      session_id: sessionId,
      run_id: chain.run,
      request_entry_id: chain.request,
      last_entry_id: chain.display,
      status: "completed",
      started_at: 1,
      finished_at: 2,
      error_code: null,
      error_message: null,
      stop_reason: null,
    })),
    steering: [],
    confirmations,
    pending_proposals: resolve(current.pending_proposals),
  };
};

/** 挂载一个会话记录：历史由可变对象提供，反映确认后的最新投影 */
const mount = (sessionId, initial = {}) => {
  const chains = initial.chains ?? [defaultChain];
  const current = {
    chains,
    confirmations: initial.confirmations ?? [],
    pending_proposals:
      initial.pending_proposals === undefined
        ? chains.map((chain) => referenceFor(chain, {}))
        : initial.pending_proposals,
  };
  setSticky(HISTORY, () => ok(historyPayload(sessionId, current)));
  const record = manager.sessionRuns.open(sessionId, false);
  record.mount(false);
  const view = () => record.snapshot();
  return {
    record,
    view,
    loaded: () => view().history === "loaded",
    set: (patch) => Object.assign(current, patch),
  };
};

let sequence = 30;
const newSession = () => {
  sequence += 1;
  return u(sequence);
};
const settle = async (predicate, label, timeout = 6000) => {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await delay(20);
  }
  assert.fail(
    `等待超时：${label}\n${JSON.stringify(requests.map((item) => `${item.method} ${item.url}`))}`,
  );
};

/* ===== 业务查询观察：saved 后对应查询与会话列表重取，失败与未知不重取 ===== */
const fetches = { profile: 0, plan: 0, workout: 0, sessions: 0 };
function installBusinessRoutes() {
  setSticky("GET /api/profile", ok({ version: 3, content: profileContent }));
  setSticky("GET /api/plans/current", ok({ id: null, content: null }));
  setSticky(
    "GET /api/workouts",
    ok({ items: [], page: 1, page_size: 10, total: 0 }),
  );
  setSticky("GET /api/sessions", ok({ sessions: [] }));
}
installBusinessRoutes();
for (const [name, key, queryFn] of [
  ["profile", ["profile"], api.getProfile],
  ["plan", ["plan", "current"], api.getCurrentPlan],
  ["workout", ["workout", "list"], () => api.listWorkouts({})],
  ["sessions", ["sessions"], api.listSessions],
]) {
  const observer = new QueryObserver(queryClient, {
    queryKey: key,
    queryFn: ({ signal }) => {
      fetches[name] += 1;
      return queryFn(signal);
    },
    staleTime: 0,
  });
  observer.subscribe(() => {});
}
const snapshotFetches = async () => {
  await delay(60);
  return { ...fetches };
};

/* ===== A. 无模型配置也能确认；处理中占用；saved 后刷新历史、列表与业务查询 ===== */
{
  resetRoutes();
  queryClient.removeQueries({ queryKey: ["provider"] });
  const sessionId = newSession();
  const handle = mount(sessionId);
  await settle(handle.loaded, "历史加载完成");
  assert.equal(handle.view().proposals.length, 1, "待确认引用决定按钮可用性");

  const before = await snapshotFetches();
  setRoute(
    POST,
    ({ body }) =>
      http(
        202,
        operation(sessionId, {
          operation_id: body.operation_id,
          status: "processing",
          result: null,
        }),
      ),
  );
  handle.record.confirmProposal("profile", proposalId, ids.displayEntry);
  await settle(() => posts().length === 1, "确认请求已发出");
  const posted = posts()[0].body;
  assert.deepEqual(
    Object.keys(posted).sort(),
    ["business_kind", "display_entry_id", "operation_id", "proposal_id"],
    "正文恰好四字段",
  );
  assert.equal(posted.display_entry_id, ids.displayEntry, "目标绑定被点击的展示节点");
  assert.equal(posted.proposal_id, proposalId, "目标绑定被点击的快照");
  assert.ok(
    !requests.some((item) => item.url.includes("/api/agent")),
    "确认不调用模型，无模型配置同样受理",
  );
  await settle(
    () => handle.view().confirmations[0]?.phase === "processing",
    "202 落地为处理中同一操作",
  );
  assert.equal(handle.view().confirming, true, "确认处理中保持占用");

  const executed = requests.length;
  assert.equal(await handle.record.send("确认。", []), false, "处理中禁止新的发送");
  assert.equal(
    await handle.record.editMessage(ids.requestEntry, "修改内容", []),
    false,
    "处理中禁止编辑",
  );
  handle.record.regenerateMessage(ids.requestEntry);
  assert.ok(
    !requests.slice(executed).some((item) => item.url.includes("/api/agent")),
    "占用期间不产生任何执行请求",
  );
  handle.record.confirmProposal("profile", u(60), ids.displayEntry);
  assert.equal(posts().length, 1, "处理中不开放其它确认提交");

  setSticky(GET, () =>
    ok(operation(sessionId, { operation_id: lastPostId() })),
  );
  handle.set({
    confirmations: () => [
      operation(sessionId, { operation_id: lastPostId() }),
      operation(sessionId, {
        operation_id: u(61),
        confirmation_entry_id: u(62),
      }),
    ],
    pending_proposals: [],
  });
  await settle(() => !handle.view().confirming, "saved 后释放占用");
  await settle(
    () => queries(posted.operation_id).length >= 1,
    "处理中通过专用查询核实原操作",
  );
  assert.equal(
    confirmations.readConfirmationLedger(sessionId).length,
    0,
    "saved 后原操作离开账本",
  );
  await settle(
    () => handle.view().rounds.some((round) => round.confirmation?.status === "saved"),
    "确认节点与真实结果独立展示",
  );
  await delay(120);
  assert.deepEqual(
    {
      profile: fetches.profile > before.profile,
      plan: fetches.plan > before.plan,
      workout: fetches.workout > before.workout,
      sessions: fetches.sessions > before.sessions,
    },
    { profile: true, plan: false, workout: false, sessions: true },
    "saved 刷新会话列表与对应业务查询，其余查询保持原值",
  );
  const shown = handle.view().rounds.filter((round) => round.confirmation !== undefined);
  assert.equal(shown.length, 2, "每个确认节点各展示一次");
  assert.equal(shown[0].entries.length, 1, "确认结果独立于 Agent round");
  assert.equal(shown[0].run_id, undefined, "确认节点无对应运行");
  assert.equal(shown[0].confirmation.text, "已保存");
  assert.equal(handle.view().ready, true, "确认终态后恢复发送与编辑");
  assert.equal(
    handle.view().confirmations.length,
    0,
    "历史覆盖后本地尝试不再重复展示",
  );
  handle.record.dispose();
  manager.sessionRuns.discard(sessionId);
}

/* ===== B. 同一确认节点上的重复结果按确认节点去重 ===== */
{
  resetRoutes();
  const sessionId = newSession();
  const handle = mount(sessionId, {
    confirmations: [
      operation(sessionId, { operation_id: u(41) }),
      operation(sessionId, { operation_id: u(42) }),
    ],
    pending_proposals: [],
  });
  await settle(handle.loaded, "重复结果历史加载");
  const shown = handle.view().rounds.filter((round) => round.confirmation !== undefined);
  assert.equal(shown.length, 1, "重复结果按确认节点去重展示");
  assert.equal(shown[0].id, `confirmation:${ids.confirmationEntry}`);
  assert.equal(shown[0].confirmation.proposal_id, proposalId);
  handle.record.dispose();
  manager.sessionRuns.discard(sessionId);
}

/* ===== C. 断连结果未知：查询未命中时保持未知与占用，显式重试沿用原请求 ===== */
{
  resetRoutes();
  const sessionId = newSession();
  const handle = mount(sessionId);
  await settle(handle.loaded, "历史加载完成");
  setRoute(POST, () => new Error("连接已中断"));
  setSticky(GET, () => rejected(404, "confirmation_not_found", "确认操作不存在。"));
  handle.record.confirmProposal("profile", proposalId, ids.displayEntry);
  await settle(
    () => handle.view().confirmations[0]?.notice !== undefined,
    "断连后保持占用并记录真实原因",
  );
  const attempt = handle.view().confirmations[0];
  assert.equal(attempt.operation, null, "未核实到操作时不伪造结果");
  assert.equal(confirmations.confirmationAttemptDisplay(attempt).text, "确认结果未知");
  assert.match(attempt.notice, /连接已中断/, "保留真实失败原因");
  const stored = confirmations.readConfirmationLedger(sessionId);
  assert.equal(stored.length, 1, "账本保留原会话与四字段请求");
  assert.equal(stored[0].session_id, sessionId);
  assert.deepEqual(stored[0].request, attempt.request);
  await settle(
    () => queries(attempt.request.operation_id).length >= 1,
    "结果未知走专用 GET",
  );
  assert.equal(handle.view().confirmations.length, 1, "未查到操作不视为原请求已结束");
  assert.equal(handle.view().confirming, true, "核实前不创建新的保存操作");

  setRoute(
    POST,
    ({ body }) =>
      http(
        202,
        operation(sessionId, {
          operation_id: body.operation_id,
          status: "processing",
          result: null,
        }),
      ),
  );
  handle.record.retryConfirmation(attempt.request.operation_id);
  await settle(() => posts().length >= 2, "显式重试已发出");
  assert.deepEqual(
    posts().at(-1).body,
    attempt.request,
    "显式重试沿用原操作 ID 与逐字相同的四字段请求",
  );
  assert.equal(posts().length, 2, "重试不换 ID 重发");
  handle.record.dispose();
  manager.sessionRuns.discard(sessionId);
  assert.equal(
    confirmations.readConfirmationLedger(sessionId).length,
    0,
    "会话删除清理确认账本",
  );
}

/* ===== D. 占用拒绝：核实未登记后移出账本、解除占用并允许重新点击 ===== */
{
  resetRoutes();
  const sessionId = newSession();
  const handle = mount(sessionId);
  await settle(handle.loaded, "历史加载完成");
  setRoute(POST, () => rejected(409, "run_busy", "已有任务正在执行。"));
  setSticky(GET, () => rejected(404, "confirmation_not_found", "确认操作不存在。"));
  handle.record.confirmProposal("profile", proposalId, ids.displayEntry);
  await settle(() => !handle.view().confirming, "拒绝后解除占用");
  assert.equal(handle.view().confirmations.length, 0, "未登记操作移出展示");
  assert.equal(confirmations.readConfirmationLedger(sessionId).length, 0);
  assert.match(handle.view().error ?? "", /已有任务/, "展示后端真实拒绝原因");
  setRoute(
    POST,
    ({ body }) =>
      http(
        202,
        operation(sessionId, {
          operation_id: body.operation_id,
          status: "processing",
          result: null,
        }),
      ),
  );
  const first = posts().at(-1).body.operation_id;
  handle.record.confirmProposal("profile", proposalId, ids.displayEntry);
  await settle(() => posts().at(-1).body.operation_id !== first, "重新点击生成新操作");
  assert.equal(
    confirmations.readConfirmationLedger(sessionId).length,
    1,
    "新操作重新入账本",
  );
  handle.record.dispose();
  manager.sessionRuns.discard(sessionId);
}

/* ===== E. 授权登记后的业务冲突：展示真实失败，保留业务页面并刷新待确认 ===== */
{
  resetRoutes();
  const sessionId = newSession();
  const handle = mount(sessionId);
  await settle(handle.loaded, "历史加载完成");
  const before = await snapshotFetches();
  setRoute(POST, () => rejected(409, "profile_version_conflict", "画像版本已变化。"));
  handle.record.confirmProposal("profile", proposalId, ids.displayEntry);
  await settle(() => posts().length === 1, "确认请求已发出");
  const failed = () =>
    operation(sessionId, {
      operation_id: lastPostId(),
      status: "failed",
      result: null,
      error: { code: "profile_version_conflict", message: "画像版本已变化。" },
    });
  setSticky(GET, () => ok(failed()));
  handle.set({
    confirmations: () => [failed()],
    pending_proposals: () => [
      pendingReference({
        status: "processing",
        confirmation_entry_id: ids.confirmationEntry,
      }),
    ],
  });
  await settle(
    () => handle.view().rounds.some((round) => round.confirmation?.status === "failed"),
    "失败结果落地展示",
  );
  const shown = handle.view().rounds.find((round) => round.confirmation !== undefined);
  assert.equal(shown.confirmation.detail, "画像版本已变化。", "展示实际失败原因");
  assert.equal(
    shown.confirmation.text,
    "保存失败",
  );
  await delay(120);
  assert.equal(
    fetches.profile,
    before.profile,
    "失败不刷新已保存业务页面内容",
  );
  assert.equal(fetches.plan, before.plan);
  assert.equal(fetches.workout, before.workout);
  assert.ok(
    requests.filter((item) => item.url.includes("/history")).length >= 2,
    "失败后刷新待确认状态",
  );
  assert.equal(
    confirmations.readConfirmationLedger(sessionId).length,
    0,
    "已核实失败的操作离开账本",
  );
  assert.equal(
    handle.view().proposals[0].status,
    "processing",
    "待确认引用按最新历史刷新",
  );
  handle.record.dispose();
  manager.sessionRuns.discard(sessionId);
}

/* ===== F. 失效操作冲突：静默移出账本并重读历史，不弹错也不重发 ===== */
{
  resetRoutes();
  const sessionId = newSession();
  const handle = mount(sessionId);
  await settle(handle.loaded, "历史加载完成");
  const histories = () => requests.filter((item) => item.url.includes("/history")).length;
  const before = histories();
  setRoute(
    POST,
    () =>
      rejected(409, "operation_conflict", "操作请求冲突。", {
        reason: "operation_expired",
      }),
  );
  handle.record.confirmProposal("profile", proposalId, ids.displayEntry);
  await settle(() => handle.view().confirmations.length === 0, "失效操作静默移出");
  assert.equal(handle.view().error ?? "", "", "失效操作不弹错误提示");
  assert.equal(confirmations.readConfirmationLedger(sessionId).length, 0, "失效账本清除");
  assert.ok(histories() > before, "失效后重读当前历史");
  assert.equal(posts().length, 1, "失效操作不自动重发");
  handle.record.dispose();
  manager.sessionRuns.discard(sessionId);
}

/* ===== G. 刷新恢复：账本按原操作 ID 核实；历史已覆盖时移出账本且不查询 ===== */
{
  resetRoutes();
  const request = {
    operation_id: u(70),
    business_kind: "profile",
    proposal_id: proposalId,
    display_entry_id: ids.displayEntry,
  };
  const sessionId = newSession();
  confirmations.updateConfirmationLedger(sessionId, () => [
    { session_id: sessionId, request },
  ]);
  setSticky(GET, () => ok(operation(sessionId, { operation_id: request.operation_id })));
  const handle = mount(sessionId);
  await settle(
    () => queries(request.operation_id).length >= 1,
    "刷新后按原操作 ID 核实",
  );
  await settle(
    () => confirmations.readConfirmationLedger(sessionId).length === 0,
    "核实后账本收敛",
  );
  assert.equal(
    handle.view().confirmations[0].phase,
    "saved",
    "核实到真实终态后按操作对象展示",
  );
  assert.equal(
    handle.view().confirmations[0].operation.result.version,
    profileResult.version,
    "展示使用完整固定保存结果",
  );
  assert.equal(handle.view().confirming, false, "终态后释放占用");
  handle.record.dispose();
  manager.sessionRuns.discard(sessionId);

  const covered = newSession();
  confirmations.updateConfirmationLedger(covered, () => [
    { session_id: covered, request },
  ]);
  const mark = requests.length;
  const second = mount(covered, {
    confirmations: [operation(covered, { operation_id: request.operation_id })],
    pending_proposals: [],
  });
  await settle(second.loaded, "覆盖历史加载");
  assert.equal(
    confirmations.readConfirmationLedger(covered).length,
    0,
    "历史覆盖后账本移出",
  );
  assert.ok(
    !requests
      .slice(mark)
      .some((item) => item.url.includes(`/confirmations/${request.operation_id}`)),
    "覆盖后不再查询该操作",
  );
  assert.equal(
    second.view().rounds.filter((round) => round.confirmation !== undefined).length,
    1,
    "覆盖后由确认节点展示",
  );
  second.record.dispose();
  manager.sessionRuns.discard(covered);
}

/* ===== H. 删除会话：迟到响应不得恢复已删除身份的确认状态 ===== */
{
  resetRoutes();
  const sessionId = newSession();
  const handle = mount(sessionId);
  await settle(handle.loaded, "历史加载完成");
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  setRoute(
    POST,
    ({ body }) => ({
      defer: gate,
      ...http(200, operation(sessionId, { operation_id: body.operation_id })),
    }),
  );
  handle.record.confirmProposal("profile", proposalId, ids.displayEntry);
  await settle(() => posts().length >= 1, "确认在途");
  assert.equal(confirmations.readConfirmationLedger(sessionId).length, 1, "提交期间账本持有原请求");
  handle.record.dispose();
  manager.sessionRuns.discard(sessionId);
  const histories = requests.filter((item) => item.url.includes("/history")).length;
  const saved = requests.length;
  release();
  await delay(200);
  assert.equal(requests.length, saved, "迟到响应不产生任何后续请求");
  assert.equal(
    requests.filter((item) => item.url.includes("/history")).length,
    histories,
    "迟到响应不刷新已删除会话的历史",
  );
  assert.equal(
    confirmations.readConfirmationLedger(sessionId).length,
    0,
    "删除后账本清空",
  );
  assert.equal(
    manager.sessionRuns.open(sessionId, false).snapshot().confirmations.length,
    0,
    "重新打开不恢复已删除会话的确认状态",
  );
  manager.sessionRuns.discard(sessionId);
}

/* ===== I. 两份待确认快照：普通文字零提交、执行期间禁用确认、一次只提交一份 ===== */
{
  resetRoutes();
  const sessionId = newSession();
  setSticky(
    PROVIDER,
    ok({
      api: "openai-completions",
      base_url: "https://models.example.test/v1",
      model: "example-model",
      api_key: "local-test-key",
      provider: "example",
    }),
  );
  queryClient.setQueryData(["provider"], await api.getProvider());
  const handle = mount(sessionId, { chains: [defaultChain, secondChain] });
  await settle(handle.loaded, "两份快照历史加载");
  assert.deepEqual(
    handle.view().proposals.map((item) => item.proposal_id),
    [proposalId, secondChain.proposal],
    "两份快照同时保持待确认",
  );
  assert.equal(posts().length, 0, "加载与展示不产生保存请求");

  let release;
  const gate = new Promise((awaiting) => {
    release = awaiting;
  });
  setRoute(RUN, () => ({ defer: gate, status: 202, payload: null }));
  assert.equal(
    await handle.record.send("确认。", []),
    true,
    "普通文字进入 Agent 运行路径",
  );
  await settle(() => agentRuns().length === 1, "运行请求已发出");
  assert.equal(agentRuns()[0].body.request, "确认。", "文字只作为运行输入");
  assert.deepEqual(
    Object.keys(agentRuns()[0].body).sort(),
    ["attachments", "operation_id", "request", "session_id"],
    "运行正文不携带任何保存授权",
  );
  assert.equal(posts().length, 0, "普通聊天文字不产生保存授权");
  assert.equal(
    confirmations.readConfirmationLedger(sessionId).length,
    0,
    "普通聊天文字不写入确认账本",
  );
  handle.record.confirmProposal("profile", proposalId, ids.displayEntry);
  handle.record.confirmProposal(
    "profile",
    secondChain.proposal,
    secondChain.display,
  );
  assert.equal(posts().length, 0, "Agent 执行期间不开放确认提交");
  handle.record.dispose();
  manager.sessionRuns.discard(sessionId);
  release();
  await delay(50);

  resetRoutes();
  const other = newSession();
  const occupied = mount(other, { chains: [defaultChain, secondChain] });
  await settle(occupied.loaded, "两份快照重新加载");
  setRoute(
    POST,
    ({ body }) =>
      http(
        202,
        operation(other, {
          operation_id: body.operation_id,
          business_kind: body.business_kind,
          proposal_id: body.proposal_id,
          display_entry_id: body.display_entry_id,
          status: "processing",
          result: null,
        }),
      ),
  );
  setSticky(GET, () =>
    ok(
      operation(other, {
        operation_id: lastPostId(),
        status: "processing",
        result: null,
      }),
    ),
  );
  occupied.record.confirmProposal("profile", proposalId, ids.displayEntry);
  await settle(() => posts().length === 1, "第一份快照已提交");
  assert.equal(occupied.view().confirming, true, "确认处理中保持占用");
  occupied.record.confirmProposal(
    "profile",
    secondChain.proposal,
    secondChain.display,
  );
  assert.equal(
    posts().length,
    1,
    "一次只提交一份：占用期间另一份有效快照不提交",
  );
  assert.equal(
    confirmations.readConfirmationLedger(other).length,
    1,
    "占用期间只有被点击的快照入账本",
  );
  assert.deepEqual(
    confirmations.readConfirmationLedger(other)[0].request,
    posts()[0].body,
    "账本持有已提交快照的四字段请求",
  );
  occupied.record.dispose();
  manager.sessionRuns.discard(other);

  resetRoutes();
  const edited = newSession();
  const source = mount(edited, {
    confirmations: [operation(edited, { operation_id: u(90) })],
    pending_proposals: [],
  });
  await settle(source.loaded, "确认节点历史加载");
  let hold;
  const holdGate = new Promise((awaiting) => {
    hold = awaiting;
  });
  setRoute("POST /api/agent/edit", () => ({
    defer: holdGate,
    status: 202,
    payload: null,
  }));
  /* 编辑的受理 promise 在运行终态才落地，此处只核对已发出的请求正文 */
  source.record.editMessage(ids.confirmationEntry, "改后的普通消息", []);
  await settle(() => agentEdits().length === 1, "编辑请求已发出");
  assert.deepEqual(
    Object.keys(agentEdits()[0].body).sort(),
    ["attachments", "operation_id", "request", "session_id", "target_entry_id"],
    "编辑正文不携带任何保存授权字段",
  );
  assert.equal(agentEdits()[0].body.target_entry_id, ids.confirmationEntry);
  assert.equal(posts().length, 0, "编辑确认节点后的普通消息不产生保存提交");
  assert.equal(
    confirmations.readConfirmationLedger(edited).length,
    0,
    "编辑不写入确认账本",
  );
  source.record.dispose();
  manager.sessionRuns.discard(edited);
  hold();
  await delay(50);
}

console.log(
  "PASS: 无模型配置确认、占用与阻断、四字段绑定、202/未知/显式重试、拒绝与失效、失败边界、刷新恢复与删除隔离、普通文字零提交、多快照占用与编辑不继承授权",
);