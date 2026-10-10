import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";

export async function checkProvider({ business, provider, manager, queryClient, queryKey }) {
  const empty = { api: null, base_url: null, model: null, api_key: null, provider: null };
  const body = {
    api: "openai-completions",
    base_url: "http://192.168.1.20:8000/v1",
    model: "vendor/model-name",
    api_key: "sk-parser-check",
  };
  const status = { ...body, provider: body.api };
  assert.deepEqual(business.parseProviderStatus(empty), empty);
  for (const api of ["openai-completions", "anthropic-messages"])
    assert.deepEqual(business.parseProviderStatus({ ...status, api }), { ...status, api });
  for (const value of [null, 1, "x", [],
    { ...status, api: "openai_compatible" },
    { ...status, api: "" },
    { ...status, api: 1 },
    { ...status, base_url: "127.0.0.1:11434/v1" },
    { ...status, base_url: "ftp://localhost/v1" },
    { ...status, base_url: "http://" },
    { ...status, base_url: " " },
    { ...status, model: "" },
    { ...status, model: " " },
    { ...status, api_key: "" },
    { ...status, provider: "" },
    { ...status, provider: " " },
    { ...status, provider: 1 },
    { ...status, structured_output: "json_schema" },
    { ...status, has_api_key: true },
    body,
  ]) assert.throws(() => business.parseProviderStatus(value));
  assert.deepEqual(business.parseProviderStatus({ ...status, provider: null }), { ...status, provider: null });

  assert.deepEqual(business.parseProviderWriteBody(body), body);
  for (const value of ["", "   ", undefined])
    assert.deepEqual(business.parseProviderWriteBody({ ...body, provider: value }), body);
  assert.deepEqual(business.parseProviderWriteBody({ ...body, provider: "  stepfun  " }), { ...body, provider: "stepfun" });
  for (const base_url of ["https://[::1]:8443/v1", "https://api.example.com"])
    assert.equal(business.parseProviderWriteBody({ ...body, api: "anthropic-messages", base_url }).base_url, base_url);
  for (const value of [
    { ...body, api: null },
    { ...body, api: "openai_compatible" },
    { ...body, api_key: "" },
    { ...body, api_key: " " },
    { ...body, model: "" },
    { ...body, model: " " },
    { ...body, base_url: "192.168.1.20:8000/v1" },
    { ...body, base_url: "ftp://localhost/v1" },
    { ...body, provider: null },
    { ...body, provider: 1 },
    { ...body, provider: true },
    { ...body, structured_output: "function_calling_strict" },
    { ...body, has_api_key: true },
    { api: body.api, base_url: body.base_url, model: body.model },
  ]) assert.throws(() => business.parseProviderWriteBody(value));
  assert.throws(() => business.parseProviderWriteBody({ ...body, provider: null }), /服务标识/);

  for (const value of [
    { ok: true, latency_ms: 0, message: "诊断通过" },
    { ok: false, latency_ms: 7, message: "诊断未通过" },
  ]) assert.deepEqual(business.parseProviderTest(value), value);
  for (const value of [
    { ok: true, latency_ms: null, message: "x" },
    { ok: true, latency_ms: -1, message: "x" },
    { ok: true, latency_ms: 12.5, message: "x" },
    { ok: true, latency_ms: "12", message: "x" },
    { ok: true, message: "x" },
    { ok: "true", latency_ms: 1, message: "x" },
    { ok: true, latency_ms: 1, message: "" },
    { ok: true, latency_ms: 1, message: "x", structured_output: "json_schema" },
  ]) assert.throws(() => business.parseProviderTest(value));

  assert.deepEqual(provider.providerFormState(empty), { api: null, base_url: "", model: "", api_key: "", provider: "" });
  assert.deepEqual(provider.providerFormState(undefined), provider.providerFormState(empty));
  assert.deepEqual(provider.providerFormState(status), status);
  for (const value of [empty, undefined, { ...status, api_key: null }, { ...status, provider: null }]) {
    assert.equal(provider.hasValidProviderConfig(value), false);
    assert.equal(provider.providerAllowsInput(value, false), false);
    assert.equal(provider.providerAllowsInput(value, true), true);
  }
  assert.equal(provider.hasValidProviderConfig(status), true);
  assert.equal(provider.providerAllowsInput(status, false), true);

  queryClient.setQueryData(queryKey, empty);
  const sessionId = randomUUID();
  const session = manager.sessionRuns.open(sessionId, true);
  session.mount(true);
  assert.equal(session.snapshot().ready, true);
  assert.equal(await session.send("未配置时的发送", []), false);
  assert.equal(await session.editMessage(randomUUID(), "未配置时的编辑", []), false);
  session.regenerateMessage(randomUUID());
  assert.equal(session.snapshot().error, provider.NO_VALID_MODEL_CONFIG);
  assert.deepEqual(session.snapshot().operations, []);
  manager.sessionRuns.discard(sessionId);
}
