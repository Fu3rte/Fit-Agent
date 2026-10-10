// 运行 npm run verify:provider；隔离后端监听 8791，转发器使用随机端口。
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer as createHttpServer, request as upstreamRequest } from "node:http";
import { createWriteStream, existsSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";
import { QueryObserver } from "@tanstack/react-query";
import { createServer } from "vite";
import { checkProvider } from "./model-config-check.mjs";

const work = path.resolve(import.meta.dirname, "../../tmp/frontend-model-config/integration");
rmSync(work, { recursive: true, force: true });
mkdirSync(work, { recursive: true });
const backend = path.resolve(import.meta.dirname, "../../backend");
const dataDir = path.join(work, "data");
const log = createWriteStream(path.join(work, "backend.log"));
const child = spawn(path.join(backend, ".venv/Scripts/python.exe"), [path.resolve(import.meta.dirname, "model-config-probe-server.py")], {
  cwd: backend,
  env: { ...process.env, MODEL_PROBE_DATA: dataDir, MODEL_PROBE_PORT: "8791" },
  stdio: ["ignore", "pipe", "pipe"],
});
child.stdout.pipe(log, { end: false });
child.stderr.pipe(log, { end: false });
const ready = new Promise((resolve, reject) => {
  const deadline = setTimeout(() => reject(new Error("隔离后端启动超时")), 10000);
  let output = "";
  child.stderr.on("data", (chunk) => {
    output += chunk.toString();
    if (output.includes("Uvicorn running on")) {
      clearTimeout(deadline);
      resolve();
    }
  });
  child.once("error", (error) => { clearTimeout(deadline); reject(error); });
  child.once("exit", (code) => { clearTimeout(deadline); reject(new Error(`隔离后端退出：${code}`)); });
});

const requests = [];
let delayedRead = null;
const forwarder = createHttpServer(async (req, res) => {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const body = Buffer.concat(chunks);
  requests.push({ method: req.method, path: req.url, body: body.toString() });
  const outgoing = upstreamRequest({
    host: "127.0.0.1", port: 8791, path: req.url, method: req.method,
    headers: { ...req.headers, host: "127.0.0.1:8000" },
  }, async (reply) => {
    // 延迟真实 GET 响应，验证变更期间的读取竞态。
    if (delayedRead !== null && req.method === "GET" && req.url === "/api/provider") {
      const pending = delayedRead;
      delayedRead = null;
      const content = [];
      for await (const chunk of reply) content.push(chunk);
      pending.arrived();
      await pending.released;
      res.writeHead(reply.statusCode, reply.headers);
      res.end(Buffer.concat(content));
      return;
    }
    res.writeHead(reply.statusCode, reply.headers);
    reply.pipe(res);
  });
  outgoing.on("error", (error) => res.destroy(error));
  outgoing.end(body);
});
const nativeFetch = globalThis.fetch;
let server;
let queryClient;
try {
  await ready;
  await new Promise((resolve) => forwarder.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${forwarder.address().port}`;
  const issued = [];
  globalThis.fetch = (input, init) => {
    issued.push({ path: typeof input === "string" ? input : input.url, method: init?.method ?? "GET", cache: init?.cache, body: init?.body });
    return nativeFetch(new URL(typeof input === "string" ? input : input.url, origin), init);
  };
  server = await createServer({ appType: "custom", logLevel: "silent" });
  const api = await server.ssrLoadModule("/src/lib/api.ts");
  const business = await server.ssrLoadModule("/src/lib/business.ts");
  const provider = await server.ssrLoadModule("/src/features/provider/providerConfig.ts");
  const manager = await server.ssrLoadModule("/src/features/chat/utils/sessionRunManager.ts");
  const { ReActHttpError } = await server.ssrLoadModule("/src/features/chat/utils/reactAgent.ts");
  const query = await server.ssrLoadModule("/src/lib/query.ts");
  queryClient = query.queryClient;
  const queryKey = query.PROVIDER_QUERY_KEY;
  localStorage.clear();
  sessionStorage.clear();
  await checkProvider({ business, provider, manager, queryClient, queryKey });
  assert.equal(requests.length, 0, "无配置时发送、编辑与重新生成不发起请求");

  const empty = { api: null, base_url: null, model: null, api_key: null, provider: null };
  assert.deepEqual(await api.getProvider(), empty);
  const body = business.parseProviderWriteBody({
    api: "openai-completions", base_url: "http://127.0.0.1:11434/v1",
    model: "vendor/model-name", api_key: "sk-integration-2b7d", provider: "  stepfun  ",
  });
  const saved = await provider.saveProviderConfig(body);
  assert.deepEqual(saved, { ...body, provider: "stepfun" });
  assert.deepEqual(queryClient.getQueryData(queryKey), saved);
  assert.deepEqual(provider.providerFormState(saved), saved);
  assert.deepEqual(JSON.parse(requests.at(-1).body), body);
  const defaulted = await provider.saveProviderConfig({ ...body, provider: undefined });
  assert.equal(defaulted.provider, body.api);
  assert.equal("provider" in JSON.parse(requests.at(-1).body), false);
  for (const invalid of [
    { ...body, api_key: "" }, { ...body, model: " " },
    { ...body, api: "openai_compatible" }, { ...body, base_url: "127.0.0.1:11434/v1" },
    { ...body, provider: null }, { ...body, provider: 1 }, { ...body, extra: true },
  ]) {
    await assert.rejects(() => api.putProvider(invalid), (error) =>
      error instanceof ReActHttpError && error.http_status === 422 && error.code === "invalid_request" && !error.message.includes(body.api_key));
  }
  assert.deepEqual(await api.getProvider(), defaulted);

  const observer = new QueryObserver(queryClient, provider.providerQueryOptions());
  const unsubscribe = observer.subscribe(() => {});
  await observer.refetch();
  assert.deepEqual(observer.getCurrentResult().data, defaulted);
  const reads = issued.length;
  await observer.refetch();
  assert.equal(issued.length, reads + 1);
  assert.equal(provider.providerQueryOptions().staleTime, 0);
  unsubscribe();

  for (const mutation of [() => provider.saveProviderConfig(body), provider.clearProviderConfig]) {
    await provider.saveProviderConfig({ ...body, api_key: "sk-stale-read" });
    let arrived;
    let release;
    const received = new Promise((resolve) => { arrived = resolve; });
    const released = new Promise((resolve) => { release = resolve; });
    delayedRead = { arrived, released };
    const read = queryClient.fetchQuery(provider.providerQueryOptions());
    const settled = Promise.allSettled([read]);
    await received;
    const status = await mutation();
    release();
    const [result] = await settled;
    assert.equal(result.status, "rejected", "配置变更取消在途读取");
    await delay(20);
    assert.deepEqual(queryClient.getQueryData(queryKey), status);
    assert.deepEqual(await api.getProvider(), status);
  }
  assert.deepEqual(await provider.clearProviderConfig(), empty);
  assert.equal(issued.at(-1).method, "DELETE");
  assert.equal(issued.at(-1).body, undefined);
  assert.deepEqual(await api.deleteProvider(), empty);
  assert.equal(existsSync(path.join(dataDir, "models.json")), false);
  assert.equal(existsSync(path.join(dataDir, "auth.json")), false);

  // 客户端缓存有效、服务端未配置时，验证真实拒绝后的会话状态。
  queryClient.setQueryData(queryKey, saved);
  const sessionId = randomUUID();
  const session = manager.sessionRuns.open(sessionId, true);
  session.mount(true);
  assert.equal(await session.send("未配置时的发送", []), true);
  for (let at = 0; at < 200 && session.snapshot().operations.length !== 0; at++) await delay(10);
  assert.deepEqual(session.snapshot().operations, []);
  assert.equal(session.snapshot().rounds.at(-1).status, "failed");
  assert.match(session.snapshot().error, /未配置|尚未配置/);
  assert.deepEqual((await api.readSessionHistory(sessionId)).entries, []);
  manager.sessionRuns.discard(sessionId);

  await provider.saveProviderConfig(body);
  writeFileSync(path.join(dataDir, "models.json"), "{invalid", "utf8");
  await assert.rejects(() => api.getProvider(), (error) =>
    error instanceof ReActHttpError && error.http_status === 500 && error.code === "internal_error");
  await provider.saveProviderConfig(body);

  assert.ok(localStorage.length + sessionStorage.length > 0, "扫描实际会话持久化数据");
  for (const area of [localStorage, sessionStorage])
    for (let at = 0; at < area.length; at++) {
      const value = area.getItem(area.key(at));
      assert.ok(!value.includes(body.api_key) && !value.includes("sk-stale-read") && !value.includes("sk-parser-check"));
    }
  assert.ok(issued.filter((request) => request.path.startsWith("/api/provider")).every((request) => request.cache === "no-store"));
  writeFileSync(path.join(work, "evidence.json"), JSON.stringify({ status: "passed", requests }, null, 2), "utf8");
  console.log("PASS: 配置解析、真实 HTTP、缓存变更竞态、发送限制、错误处理与凭据存储检查");
} finally {
  globalThis.fetch = nativeFetch;
  queryClient?.clear();
  await server?.close();
  forwarder.closeAllConnections();
  await new Promise((resolve) => forwarder.close(resolve));
  child.kill();
  await new Promise((resolve) => child.exitCode !== null ? resolve() : child.once("exit", resolve));
  log.end();
}
