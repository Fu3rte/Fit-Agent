// 运行 npm run verify:confirm:http；隔离后端监听 8793，转发器使用随机端口。
// 真实链路验证：Python 服务用真实准备流程在隔离 SQLite 中种下画像与训练记录两份待确认快照，
// 前端生产代码（严格解析、账本、状态机、查询刷新）只通过真实 HTTP 完成确认、幂等与拒绝核验。
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer as createHttpServer, request as upstreamRequest } from "node:http";
import {
  createWriteStream,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";
import { QueryObserver } from "@tanstack/react-query";
import { createServer } from "vite";

const PORT = "8793";
const work = path.resolve(import.meta.dirname, "../../tmp/frontend-confirmation");
rmSync(work, { recursive: true, force: true });
mkdirSync(work, { recursive: true });
const backend = path.resolve(import.meta.dirname, "../../backend");
const log = createWriteStream(path.join(work, "server.log"));
const child = spawn(
  path.join(backend, ".venv/Scripts/python.exe"),
  [path.resolve(import.meta.dirname, "confirmation-http-server.py")],
  {
    cwd: backend,
    env: {
      ...process.env,
      CONFIRM_PROBE_DATA: path.join(work, "data"),
      CONFIRM_PROBE_PORT: PORT,
      CONFIRM_PROBE_OUT: path.join(work, "ids.json"),
    },
    stdio: ["ignore", "pipe", "pipe"],
  },
);
child.stdout.pipe(log, { end: false });
child.stderr.pipe(log, { end: false });
const ready = new Promise((resolve, reject) => {
  const deadline = setTimeout(
    () => reject(new Error("隔离后端启动超时")),
    60_000,
  );
  let output = "";
  child.stderr.on("data", (chunk) => {
    output += chunk.toString();
    if (output.includes("Uvicorn running on")) {
      clearTimeout(deadline);
      resolve();
    }
  });
  child.once("error", (error) => {
    clearTimeout(deadline);
    reject(error);
  });
  child.once("exit", (code) => {
    clearTimeout(deadline);
    reject(new Error(`隔离后端退出：${code}\n${log.path}`));
  });
});

const traffic = [];
const forwarder = createHttpServer(async (req, res) => {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const body = Buffer.concat(chunks);
  const record = {
    method: req.method,
    path: req.url,
    body: body.toString(),
    status: 0,
    cache: "",
  };
  traffic.push(record);
  const outgoing = upstreamRequest(
    {
      host: "127.0.0.1",
      port: Number(PORT),
      path: req.url,
      method: req.method,
      headers: { ...req.headers, host: "127.0.0.1:8000" },
    },
    (reply) => {
      record.status = reply.statusCode ?? 0;
      record.cache = String(reply.headers["cache-control"] ?? "");
      if ((req.url ?? "").includes("/confirmations")) {
        const content = [];
        reply.on("data", (chunk) => content.push(chunk));
        reply.on("end", () => {
          record.response = Buffer.concat(content).toString();
          res.writeHead(reply.statusCode ?? 500, reply.headers);
          res.end(Buffer.concat(content));
        });
        return;
      }
      res.writeHead(reply.statusCode ?? 500, reply.headers);
      reply.pipe(res);
    },
  );
  outgoing.on("error", (error) => res.destroy(error));
  outgoing.end(body);
});

const confirmationsPath = (sessionId) =>
  `/api/sessions/${sessionId}/confirmations`;
/** 画像版本是全局状态：主会话保存完成后再准备冲突会话，避免早期确认被后续准备视为过期 */
const stageConflict = () =>
  new Promise((resolve, reject) => {
    const proc = spawn(
      path.join(backend, ".venv/Scripts/python.exe"),
      [path.resolve(import.meta.dirname, "confirmation-http-server.py")],
      {
        cwd: backend,
        env: {
          ...process.env,
          CONFIRM_PROBE_ACTION: "stage",
          CONFIRM_PROBE_DATA: path.join(work, "data"),
        },
        stdio: ["ignore", "pipe", "pipe"],
      },
    );
    let out = "";
    let err = "";
    proc.stdout.on("data", (chunk) => {
      out += chunk;
    });
    proc.stderr.on("data", (chunk) => {
      err += chunk;
    });
    proc.once("error", reject);
    proc.once("exit", (code) =>
      code === 0
        ? resolve(JSON.parse(out))
        : reject(new Error(`冲突场景准备失败：${code}\n${err}`)),
    );
  });
const confirmationsOf = (sessionId) =>
  traffic.filter(
    (item) => item.method === "POST" && item.path === confirmationsPath(sessionId),
  );
const settle = async (predicate, label, timeout = 30_000) => {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await delay(20);
  }
  assert.fail(
    `等待超时：${label}\n${JSON.stringify(
      traffic
        .filter((item) => item.path.includes("/confirmations") && item.response !== undefined)
        .slice(-3)
        .map((item) => [item.method, item.status, item.response]),
    )}`,
  );
};

const nativeFetch = globalThis.fetch;
let server;
let queryClient;
let record;
try {
  await ready;
  await new Promise((resolve) => forwarder.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${forwarder.address().port}`;
  globalThis.fetch = (input, init) =>
    nativeFetch(new URL(typeof input === "string" ? input : input.url, origin), init);

  server = await createServer({ appType: "custom", logLevel: "silent" });
  const api = await server.ssrLoadModule("/src/lib/api.ts");
  const manager = await server.ssrLoadModule(
    "/src/features/chat/utils/sessionRunManager.ts",
  );
  const confirmations = await server.ssrLoadModule(
    "/src/features/chat/utils/confirmations.ts",
  );
  const query = await server.ssrLoadModule("/src/lib/query.ts");
  await server.close();
  queryClient = query.queryClient;
  localStorage.clear();

  const seeded = JSON.parse(
    readFileSync(path.join(work, "ids.json"), "utf8"),
  );
  const sessionId = seeded.session;
  traffic.length = 0;

  /* ===== 1. 无模型配置下的真实历史投影 ===== */
  assert.deepEqual(await api.getProvider(), {
    api: null,
    base_url: null,
    model: null,
    api_key: null,
    provider: null,
  });
  const profileBefore = await api.getProfile();
  const workoutsBefore = await api.listWorkouts({});
  const initial = await api.readSessionHistory(sessionId);
  assert.equal(initial.confirmations.length, 0, "未确认时会话无确认操作");
  assert.deepEqual(
    initial.pending_proposals.map((item) => item.business_kind).sort(),
    ["profile", "workout"],
    "两份快照同时待确认",
  );
  const profileRef = initial.pending_proposals.find(
    (item) => item.business_kind === "profile",
  );
  const workoutRef = initial.pending_proposals.find(
    (item) => item.business_kind === "workout",
  );
  assert.equal(profileRef.display_entry_id, seeded.profile.display);
  assert.equal(profileRef.proposal_id, seeded.profile.proposal);
  assert.equal(workoutRef.display_entry_id, seeded.workout.display);
  for (const item of initial.pending_proposals)
    assert.equal(item.status, "pending", "真实引用为 pending");

  /* ===== 2. 业务查询观察 ===== */
  const fetches = { profile: 0, plan: 0, workout: 0, sessions: 0 };
  for (const [name, key, queryFn] of [
    ["profile", ["profile"], api.getProfile],
    ["plan", ["plan", "current"], api.getCurrentPlan],
    ["workout", ["workout", "list"], () => api.listWorkouts({})],
    ["sessions", ["sessions"], api.listSessions],
  ]) {
    new QueryObserver(queryClient, {
      queryKey: key,
      queryFn: ({ signal }) => {
        fetches[name] += 1;
        return queryFn(signal);
      },
      staleTime: 0,
    }).subscribe(() => {});
  }

  /* ===== 3. 点击画像快照确认：真实 200 saved、账本收敛、确认节点独立展示 ===== */
  record = manager.sessionRuns.open(sessionId, false);
  record.mount(false);
  const view = () => record.snapshot();
  await settle(() => view().history === "loaded", "真实历史加载");
  assert.equal(view().proposals.length, 2, "两份快照各自开放确认入口");
  const beforeClick = { ...fetches };
  record.confirmProposal(
    "profile",
    profileRef.proposal_id,
    profileRef.display_entry_id,
  );
  await settle(
    () => confirmationsOf(sessionId).some((item) => item.status > 0),
    "确认请求已收到响应",
  );
  const posted = JSON.parse(confirmationsOf(sessionId)[0].body);
  assert.deepEqual(
    Object.keys(posted).sort(),
    ["business_kind", "display_entry_id", "operation_id", "proposal_id"],
    "真实请求正文恰好四字段",
  );
  assert.equal(confirmationsOf(sessionId)[0].status, 200, "真实保存返回 200");
  await settle(
    () => view().proposals.length === 1 && view().confirmations.length === 0,
    "saved 后仅画像收敛、训练记录保持待确认",
  );
  assert.equal(view().proposals[0].business_kind, "workout");
  assert.equal(
    confirmations.readConfirmationLedger(sessionId).length,
    0,
    "真实终态后账本清空",
  );
  assert.equal(view().confirming, false, "终态后释放占用");
  assert.equal(view().ready, true, "终态后恢复发送");
  const shown = view().rounds.filter((round) => round.confirmation !== undefined);
  assert.equal(shown.length, 1, "确认节点独立于 Agent round 展示");
  assert.equal(shown[0].run_id, undefined);
  assert.equal(shown[0].confirmation.status, "saved");
  assert.equal(shown[0].confirmation.text, "已保存");
  assert.equal(
    shown[0].entries[0].request,
    "确认保存画像",
    "确认节点正文来自后端投影",
  );
  assert.ok(
    !traffic.some((item) => item.path.startsWith("/api/agent")),
    "确认全程不调用模型",
  );
  const profileAfter = await api.getProfile();
  assert.equal(profileBefore.version, null, "确认前画像尚未保存");
  assert.equal(profileAfter.version, 1, "确认按真实快照保存首个版本");
  assert.equal(profileAfter.content.goal, "增肌");
  assert.equal((await api.listWorkouts({})).total, workoutsBefore.total);
  await settle(
    () => fetches.profile > beforeClick.profile && fetches.sessions > beforeClick.sessions,
    "saved 刷新画像查询与会话列表",
  );
  assert.equal(fetches.workout, beforeClick.workout, "训练记录查询未被误刷新");

  /* ===== 4. 同键同请求与同键异请求 ===== */
  const stored = await api.getSaveConfirmation(sessionId, posted.operation_id);
  const repeat = await api.postSaveConfirmation(sessionId, posted);
  assert.deepEqual(repeat, stored, "同键同请求复用原操作与固定结果");
  assert.equal(repeat.status, "saved");
  await assert.rejects(
    () =>
      api.postSaveConfirmation(sessionId, {
        ...posted,
        business_kind: "workout",
        proposal_id: workoutRef.proposal_id,
        display_entry_id: workoutRef.display_entry_id,
      }),
    (error) => error.http_status === 409 && error.code === "operation_conflict",
    "同键异请求始终冲突",
  );
  const conflict = JSON.parse(confirmationsOf(sessionId).at(-1).response);
  assert.deepEqual(Object.keys(conflict), ["detail"], "失败响应保持既有 detail 结构");
  assert.deepEqual(
    Object.keys(conflict.detail).sort(),
    ["code", "message"],
    "冲突响应无多余字段",
  );
  assert.equal(conflict.detail.code, "operation_conflict");
  assert.ok(conflict.detail.message.length > 0, "冲突响应携带真实说明");
  const reconfirm = await api.postSaveConfirmation(sessionId, {
    ...posted,
    operation_id: randomUUID(),
  });
  assert.equal(reconfirm.status, "saved", "新操作重复确认返回原结果");
  assert.deepEqual(reconfirm.result, stored.result, "重复确认不新增业务版本");
  assert.equal(
    reconfirm.confirmation_entry_id,
    stored.confirmation_entry_id,
    "重复确认关联原确认节点",
  );
  assert.deepEqual(await api.getProfile(), profileAfter, "重复确认后画像保持原值");
  const deduped = await api.readSessionHistory(sessionId);
  assert.equal(
    deduped.confirmations.length,
    2,
    "两个操作各自登记并关联同一确认节点",
  );
  assert.equal(
    new Set(deduped.confirmations.map((item) => item.confirmation_entry_id)).size,
    1,
    "两次操作共用同一确认节点",
  );
  assert.ok(
    deduped.confirmations.every(
      (item) => item.status === "saved" && item.result.version === profileAfter.version,
    ),
    "重复确认不新增业务版本",
  );

  /* ===== 5. 未登记操作与不存在的快照：真实拒绝零提交 ===== */
  await assert.rejects(
    () => api.getSaveConfirmation(sessionId, randomUUID()),
    (error) =>
      error.http_status === 404 && error.code === "confirmation_not_found",
    "未登记操作按专用错误码返回",
  );
  const beforeReject = await api.readSessionHistory(sessionId);
  await assert.rejects(
    () =>
      api.postSaveConfirmation(sessionId, {
        operation_id: randomUUID(),
        business_kind: "profile",
        proposal_id: randomUUID(),
        display_entry_id: profileRef.display_entry_id,
      }),
    (error) =>
      error.http_status === 404 &&
      error.code === "profile_proposal_not_found",
    "不存在的快照返回真实业务错误",
  );
  const missing = JSON.parse(confirmationsOf(sessionId).at(-1).response);
  assert.deepEqual(Object.keys(missing), ["detail"], "业务错误沿用 detail 结构");
  assert.equal(missing.detail.code, "profile_proposal_not_found");
  assert.ok(missing.detail.message.length > 0, "业务错误响应携带真实说明");
  const afterReject = await api.readSessionHistory(sessionId);
  assert.equal(
    afterReject.confirmations.length,
    beforeReject.confirmations.length,
    "授权拒绝不创建确认操作",
  );
  assert.equal(afterReject.pending_proposals.length, 1, "拒绝后训练记录仍待确认");

  /* ===== 6. 训练记录独立确认并刷新业务查询 ===== */
  record.confirmProposal(
    "workout",
    workoutRef.proposal_id,
    workoutRef.display_entry_id,
  );
  await settle(() => view().proposals.length === 0, "全部快照已落定");
  const workoutPost = confirmationsOf(sessionId).at(-1);
  assert.equal(
    JSON.parse(workoutPost.body).proposal_id,
    workoutRef.proposal_id,
    "第二份提交目标为训练记录快照",
  );
  assert.equal(workoutPost.status, 200, "训练记录按自身快照保存");
  assert.equal(
    view().rounds.filter((round) => round.confirmation !== undefined).length,
    2,
    "两份确认各展示一次",
  );
  assert.equal(
    view().rounds
      .filter((round) => round.confirmation !== undefined)
      .map((round) => round.confirmation.business_kind)
      .sort()
      .join(","),
    "profile,workout",
  );
  assert.equal((await api.listWorkouts({})).total, workoutsBefore.total + 1);
  assert.equal((await api.readSessionHistory(sessionId)).confirmations.length, 3);
  assert.equal(
    confirmations.readConfirmationLedger(sessionId).length,
    0,
    "全部终态后账本为空",
  );

  /* ===== 7. 刷新恢复：重复操作按确认节点合并展示 ===== */
  record.dispose();
  manager.sessionRuns.discard(sessionId);
  record = manager.sessionRuns.open(sessionId, false);
  record.mount(false);
  await settle(() => view().history === "loaded", "刷新后真实历史加载");
  const rounds = view().rounds.filter((round) => round.confirmation !== undefined);
  assert.equal(rounds.length, 2, "同一确认节点上的重复操作只展示一次");
  assert.deepEqual(
    rounds.map((round) => round.confirmation.business_kind),
    ["profile", "workout"],
    "确认结果按后端节点顺序展示",
  );
  assert.equal(view().proposals.length, 0, "刷新后无剩余待确认引用");
  assert.equal(view().confirmations.length, 0, "刷新后本地无未决确认");
  assert.equal(view().confirming, false, "刷新后释放占用");
  assert.equal(confirmations.readConfirmationLedger(sessionId).length, 0);

  /* ===== 8. 确认节点不作为模型重新生成目标 ===== */
  await api.putProvider({
    api: "openai-completions",
    base_url: "http://127.0.0.1:11434/v1",
    model: "vendor/model-name",
    api_key: "sk-integration-placeholder",
    provider: "example",
  });
  await assert.rejects(
    () =>
      api.runReActStream(
        {
          session_id: sessionId,
          operation_id: randomUUID(),
          target_entry_id: deduped.confirmations[0].confirmation_entry_id,
        },
        () => {},
        new AbortController().signal,
        () => {},
        "/api/agent/regenerate",
      ),
    (error) =>
      error.http_status === 409 && error.code === "invalid_target_entry",
    "后端拒绝把确认节点作为重新生成目标",
  );
  assert.equal(
    traffic.some(
      (item) => item.path.startsWith("/api/agent") && item.status < 400,
    ),
    false,
    "拒绝未启动任何运行",
  );

  /* ===== 9. 真实业务冲突：失败终态持久化、展示实际原因、释放占用 ===== */
  const conflictCase = await stageConflict();
  const conflictBefore = await api.readSessionHistory(conflictCase.session);
  assert.equal(conflictBefore.confirmations.length, 0, "冲突会话自身没有确认操作");
  assert.deepEqual(
    conflictBefore.pending_proposals.map((item) => item.proposal_id),
    [conflictCase.stale.proposal],
    "过期快照仍是当前有效引用",
  );
  const conflictRecord = manager.sessionRuns.open(conflictCase.session, false);
  conflictRecord.mount(false);
  await settle(
    () => conflictRecord.snapshot().history === "loaded",
    "冲突会话历史加载",
  );
  assert.equal(conflictRecord.snapshot().proposals.length, 1);
  conflictRecord.confirmProposal(
    "profile",
    conflictCase.stale.proposal,
    conflictCase.stale.display,
  );
  await settle(
    () =>
      conflictRecord
        .snapshot()
        .rounds.some((round) => round.confirmation?.status === "failed"),
    "真实失败操作按确认节点展示",
  );
  const failed = conflictRecord
    .snapshot()
    .rounds.find((round) => round.confirmation?.status === "failed");
  assert.equal(failed.confirmation.text, "保存失败");
  assert.match(failed.confirmation.detail, /画像版本已变化/, "展示实际失败原因");
  assert.equal(failed.run_id, undefined, "失败确认同样独立于 Agent 运行");
  assert.equal(conflictRecord.snapshot().confirming, false, "失败终态释放占用");
  assert.equal(conflictRecord.snapshot().confirmations.length, 0, "历史覆盖后移出本地展示");
  assert.equal(
    confirmations.readConfirmationLedger(conflictCase.session).length,
    0,
    "终态后账本清空",
  );
  const afterConflict = await api.readSessionHistory(conflictCase.session);
  assert.equal(
    afterConflict.confirmations.length,
    1,
    "授权登记后的业务冲突持久化为失败操作",
  );
  assert.equal(afterConflict.confirmations[0].status, "failed");
  assert.equal(
    afterConflict.confirmations[0].error.code,
    "profile_version_conflict",
    "失败操作携带真实业务错误码",
  );
  assert.equal(
    afterConflict.confirmations[0].error.errors,
    undefined,
    "后端序列化的 null 明细按缺省解析",
  );
  assert.equal(
    confirmations.confirmationOperationDisplay(afterConflict.confirmations[0])
      .detail,
    afterConflict.confirmations[0].error.message,
    "失败展示使用操作对象内的实际说明",
  );
  conflictRecord.dispose();
  manager.sessionRuns.discard(conflictCase.session);

  /* ===== 10. 确认相关响应一律 no-store ===== */
  assert.equal(
    traffic.every((item) => item.cache === "no-store"),
    true,
    "确认相关响应一律 no-store",
  );

  record.dispose();
  manager.sessionRuns.discard(sessionId);
  writeFileSync(
    path.join(work, "evidence.json"),
    JSON.stringify({ seeded, traffic }, null, 2),
    "utf8",
  );
  console.log(
    "PASS: 真实后端确认保存、无模型配置确认、四字段请求、200/幂等/冲突/未登记、拒绝零提交、确认节点独立展示与查询刷新",
  );
} finally {
  globalThis.fetch = nativeFetch;
  record?.dispose();
  queryClient?.clear();
  forwarder.closeAllConnections();
  await new Promise((resolve) => forwarder.close(resolve));
  child.kill();
  await new Promise((resolve) =>
    child.exitCode !== null ? resolve() : child.once("exit", resolve),
  );
  log.end();
}
