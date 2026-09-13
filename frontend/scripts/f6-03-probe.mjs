/**
 * F6-03 协议探针：Provider 配置闭环（真实存储 + 兼容端点；假 Key、无真实模型、仅回环）。
 *
 * 覆盖 stage6 F6-03 验收（协议面）：
 * 1. GET /api/provider 无 Key：has_api_key=false；响应无 Key/掩码/data_dir；有只读展示字段
 * 2. PUT /api/provider/api-key 录入假 Key → has_api_key=true
 * 3. 再 GET：仍只安全投影；响应全文不含假 Key 明文
 * 4. 同数据目录重启 uvicorn → GET 仍 has_api_key=true（跨重启持久）
 * 5. DELETE → false；再 DELETE 幂等 false
 * 6. 无 Key 时 Run 终态 failed + error_code=model_request_failed（不外呼）
 * 7. 后端 stdout/stderr 无假 Key 明文
 *
 * 运行：node scripts/f6-03-probe.mjs
 * 归档：plans/stage6-evidence-assets/f6-03-probe.txt
 *
 * 红线：假 Key 仅用于断言「响应不含该字符串」；不读取、不打印真实 MODEL_API_KEY。
 */
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, existsSync, writeFileSync, mkdirSync } from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const frontendRoot = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const backendRoot = path.join(frontendRoot, "..", "backend");
const evidenceDir = path.join(frontendRoot, "plans", "stage6-evidence-assets");
const evidencePath = path.join(evidenceDir, "f6-03-probe.txt");

/** 辨识度明确的假 Key：出现在任何响应或后端日志里即视为泄漏。 */
const FAKE_KEY = "probe-fake-key-not-real";

let failed = 0;
let passed = 0;
const lines = [];
const log = (s = "") => {
  lines.push(s);
  console.log(s);
};
const check = (name, ok, detail = "") => {
  if (!ok) failed += 1;
  else passed += 1;
  const line = `${ok ? "PASS" : "FAIL"} ${name}${detail ? ` — ${detail}` : ""}`;
  log(line);
};

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.once("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

async function waitHealthz(base, deadlineMs = 15000) {
  const t0 = Date.now();
  let lastErr = "";
  while (Date.now() - t0 < deadlineMs) {
    try {
      const res = await fetch(`${base}/healthz`);
      if (res.ok) return true;
      lastErr = `HTTP ${res.status}`;
    } catch (e) {
      lastErr = String(e);
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error(`healthz 未就绪: ${lastErr}`);
}

const pythonExe = path.join(backendRoot, ".venv", "Scripts", "python.exe");
const usePython = existsSync(pythonExe) ? pythonExe : "python";

const dataDir = mkdtempSync(path.join(os.tmpdir(), "fit-agent-f6-03-"));
const port = await freePort();
const base = `http://127.0.0.1:${port}`;

log(`# F6-03 probe — ${new Date().toISOString()}`);
log(`# backend=${usePython} data_dir=${dataDir} base=${base}`);
log(`# loopback only; fake key only; no real model / no real API key`);
log("");

/** 启动一个 uvicorn 进程（同 dataDir / 同端口；显式剥离真实凭据环境变量）。 */
function spawnBackend() {
  const env = { ...process.env, FIT_AGENT_DATA_DIR: dataDir, PYTHONPATH: backendRoot };
  // 红线：不向探针子进程传真实凭据（即使宿主 shell 里有）。
  delete env.MODEL_API_KEY;
  delete env.MODEL_BASE_URL;
  delete env.MODEL_NAME;
  const proc = spawn(
    usePython,
    [
      "-m",
      "uvicorn",
      "api.app:create_app",
      "--factory",
      "--host",
      "127.0.0.1",
      "--port",
      String(port),
      "--log-level",
      "warning",
    ],
    {
      cwd: backendRoot,
      env,
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    },
  );
  const out = { text: "" };
  proc.stdout?.on("data", (d) => {
    out.text += d.toString();
  });
  proc.stderr?.on("data", (d) => {
    out.text += d.toString();
  });
  return { proc, out };
}

async function killBackend(proc) {
  try {
    proc.kill();
    await new Promise((r) => setTimeout(r, 300));
    if (!proc.killed) proc.kill("SIGKILL");
  } catch {
    /* ignore */
  }
  // 等端口释放，避免重启 bind 冲突
  await new Promise((r) => setTimeout(r, 300));
}

let { proc, out } = spawnBackend();
let procOut = out;

const api = async (method, p, body) => {
  const res = await fetch(base + p, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  let json = null;
  try {
    json = JSON.parse(text);
  } catch {
    /* non-JSON ok */
  }
  return { status: res.status, body: json, text };
};

/** GET /api/provider 安全投影断言：无 Key/掩码/data_dir；有只读展示。 */
function assertSafeProjection(label, r) {
  const body = r.body ?? {};
  const text = r.text ?? "";
  const hasNoDataDir = body.data_dir === undefined && !("data_dir" in body);
  const hasNoKeyLeak =
    !text.includes(FAKE_KEY) &&
    !JSON.stringify(body).toLowerCase().includes("api_key_secret") &&
    typeof body.api_key === "undefined" &&
    typeof body.api_key_masked === "undefined";
  const hasReadOnly =
    typeof body.protocol === "string" &&
    body.protocol.length > 0 &&
    typeof body.base_url === "string" &&
    body.base_url.length > 0 &&
    body.model != null &&
    typeof body.model.name === "string" &&
    body.model.name.length > 0;
  check(
    `${label} status=200 has_api_key=${body.has_api_key} safe projection`,
    r.status === 200 &&
      typeof body.has_api_key === "boolean" &&
      hasNoDataDir &&
      hasNoKeyLeak &&
      hasReadOnly,
    `keys=${Object.keys(body).join(",") || "—"}`,
  );
  return body;
}

async function waitForRun(runId, deadlineMs = 20000) {
  const t0 = Date.now();
  while (Date.now() - t0 < deadlineMs) {
    const { status, body } = await api("GET", `/api/runs/${runId}`);
    if (status === 200 && body?.run) {
      const run = body.run;
      if (["completed", "failed", "cancelled"].includes(run.status)) return run;
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  return null;
}

try {
  await waitHealthz(base);
  check("backend_ready", true, `uvicorn ${base}`);

  /* ---- 1. 无 Key 时 GET /api/provider ---- */
  {
    const r = await api("GET", "/api/provider");
    const body = assertSafeProjection("GET /api/provider (no key)", r);
    check(
      "GET /api/provider has_api_key=false",
      r.status === 200 && body.has_api_key === false,
      `has_api_key=${body.has_api_key}`,
    );
  }

  /* ---- 2. PUT 录入假 Key ---- */
  {
    const r = await api("PUT", "/api/provider/api-key", { api_key: FAKE_KEY });
    const body = r.body ?? {};
    check(
      "PUT /api/provider/api-key → has_api_key=true",
      r.status === 200 &&
        body.has_api_key === true &&
        typeof body.provider === "string" &&
        !r.text.includes(FAKE_KEY),
      `status=${r.status} keys=${Object.keys(body).join(",")}`,
    );
  }

  /* ---- 3. 再 GET：仍安全投影，全文无假 Key 明文 ---- */
  {
    const r = await api("GET", "/api/provider");
    const body = assertSafeProjection("GET /api/provider (after put)", r);
    check(
      "GET after put has_api_key=true no key plaintext",
      r.status === 200 && body.has_api_key === true && !r.text.includes(FAKE_KEY),
      `has_api_key=${body.has_api_key} leak=${r.text.includes(FAKE_KEY)}`,
    );
  }

  /* ---- 4. 同数据目录重启 → has_api_key 仍 true ---- */
  {
    await killBackend(proc);
    ({ proc, out } = spawnBackend());
    procOut = out;
    await waitHealthz(base);
    check("backend_restarted same data_dir", true, `uvicorn ${base} data_dir=${dataDir}`);
    const r = await api("GET", "/api/provider");
    const body = r.body ?? {};
    check(
      "GET after restart has_api_key=true (persisted)",
      r.status === 200 &&
        body.has_api_key === true &&
        !r.text.includes(FAKE_KEY) &&
        body.data_dir === undefined,
      `status=${r.status} has_api_key=${body.has_api_key}`,
    );
  }

  /* ---- 5. DELETE → false；再 DELETE 幂等 false ---- */
  {
    const first = await api("DELETE", "/api/provider/api-key");
    check(
      "DELETE /api/provider/api-key → has_api_key=false",
      first.status === 200 && first.body?.has_api_key === false,
      `status=${first.status}`,
    );
    const second = await api("DELETE", "/api/provider/api-key");
    check(
      "DELETE idempotent second call → has_api_key=false",
      second.status === 200 && second.body?.has_api_key === false,
      `status=${second.status}`,
    );
  }

  /* ---- 6. 无 Key：session + request → Run 终态 failed / model_request_failed ---- */
  {
    const create = await api("POST", "/api/sessions", {});
    check(
      "POST /api/sessions",
      create.status === 200 && create.body?.session_id,
      `status=${create.status}`,
    );
    const sessionId = create.body?.session_id ?? null;
    if (sessionId) {
      const submit = await api("POST", `/api/sessions/${sessionId}/requests`, {
        client_request_id: `f6-03-probe-${Date.now()}`,
        text: "你好",
      });
      const runId = submit.body?.run?.run_id;
      check(
        "POST /api/sessions/{id}/requests accepted",
        submit.status === 200 && runId,
        `status=${submit.status} run=${runId ?? "—"}`,
      );
      if (runId) {
        const run = await waitForRun(runId);
        check(
          "Run terminal failed error_code=model_request_failed (no key)",
          run != null &&
            run.status === "failed" &&
            run.error_code === "model_request_failed",
          run ? `status=${run.status} error_code=${run.error_code}` : "timeout waiting terminal",
        );
      } else {
        check(
          "Run terminal failed error_code=model_request_failed (no key)",
          false,
          "no run_id",
        );
      }
    } else {
      check("POST /api/sessions/{id}/requests accepted", false, "no session_id");
      check(
        "Run terminal failed error_code=model_request_failed (no key)",
        false,
        "no session_id",
      );
    }
  }

  /* ---- 7. 后端 stdout/stderr 无假 Key 明文 ---- */
  {
    const backendText = procOut.text;
    check(
      "backend stdout/stderr contains no fake key",
      !backendText.includes(FAKE_KEY),
      `len=${backendText.length}`,
    );
  }
} catch (e) {
  check("probe_unexpected_error", false, String(e));
  if (procOut?.text) log(`# backend output (truncated):\n${procOut.text.slice(-2000)}`);
} finally {
  await killBackend(proc);
  try {
    rmSync(dataDir, { recursive: true, force: true });
  } catch {
    /* ignore */
  }
}

log("");
log(`# summary: passed=${passed} failed=${failed}`);
// 归档头带 PASS/FAIL 计数（便于 grep / evidence 引用）
if (!existsSync(evidenceDir)) mkdirSync(evidenceDir, { recursive: true });
const header = `# F6-03 probe result: passed=${passed} failed=${failed}\n`;
writeFileSync(evidencePath, header + lines.join("\n") + "\n", "utf8");
log(`# evidence: ${evidencePath}`);
process.exit(failed === 0 ? 0 : 1);
