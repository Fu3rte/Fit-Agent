/**
 * F6-04a 协议探针：空库建档协议面（真实后端 uvicorn + 临时数据目录；假 Key、无真实模型、仅回环）。
 *
 * 覆盖 stage6 F6-04（无模型路径；对话产生草稿→确认后 profile 非 null 归 F6-04b）：
 * 1. GET /api/profile → 200 且 profile === null（未建档）
 * 2. 无公开建档案端点：PUT/POST /api/profile → 404/405（对话是唯一入口）
 * 3. GET /api/sessions/{id}/drafts → 空数组
 * 4. GET /api/drafts/nonexistent → 404 invalid_request
 * 5. POST /api/drafts/nonexistent/confirm {revision:1} → 404（不半写档案）
 * 6. POST /api/drafts/nonexistent/revise → 404
 * 7. 确认前后档案保持 null（无草稿时任何误操作不写正式档案）
 *
 * 运行：node scripts/f6-04-probe.mjs
 * 归档：plans/stage6-evidence-assets/f6-04-probe.txt
 *
 * 红线：不向子进程传 MODEL_* 真实凭据；不用真实 Key；不改业务代码。
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
const evidencePath = path.join(evidenceDir, "f6-04-probe.txt");

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

const dataDir = mkdtempSync(path.join(os.tmpdir(), "fit-agent-f6-04-"));
const port = await freePort();
const base = `http://127.0.0.1:${port}`;

log(`# F6-04 probe — ${new Date().toISOString()}`);
log(`# backend=${usePython} data_dir=${dataDir} base=${base}`);
log("# loopback only; no real model / no real API key / empty DB");
log("");

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

try {
  await waitHealthz(base);
  check("backend_ready", true, `uvicorn ${base}`);

  /* ---- 1. GET /api/profile → 200 且 profile === null（未建档） ---- */
  {
    const r = await api("GET", "/api/profile");
    check(
      "GET /api/profile → 200 profile=null (未建档)",
      r.status === 200 && r.body?.profile === null && "context_version" in (r.body ?? {}),
      `status=${r.status} profile=${r.body?.profile === null ? "null" : "obj"} keys=${Object.keys(r.body ?? {}).join(",")}`,
    );
  }

  /* ---- 2. 无公开建档案端点：PUT/POST /api/profile 应 404/405 ---- */
  {
    const put = await api("PUT", "/api/profile", { profile: {} });
    const putOk = put.status === 404 || put.status === 405;
    check(
      "PUT /api/profile → 404/405 (无公开建档入口)",
      putOk,
      `status=${put.status}`,
    );

    const post = await api("POST", "/api/profile", { profile: {} });
    const postOk = post.status === 404 || post.status === 405;
    check(
      "POST /api/profile → 404/405 (无公开建档入口)",
      postOk,
      `status=${post.status}`,
    );
  }

  /* ---- 3. 新会话 + GET /api/sessions/{id}/drafts → 空数组 ---- */
  let sessionId = null;
  {
    const create = await api("POST", "/api/sessions", {});
    check(
      "POST /api/sessions",
      create.status === 200 && create.body?.session_id,
      `status=${create.status}`,
    );
    sessionId = create.body?.session_id ?? null;
    if (sessionId) {
      const drafts = await api("GET", `/api/sessions/${sessionId}/drafts`);
      check(
        "GET /api/sessions/{id}/drafts → 200 []",
        drafts.status === 200 && Array.isArray(drafts.body) && drafts.body.length === 0,
        `status=${drafts.status} n=${Array.isArray(drafts.body) ? drafts.body.length : "—"}`,
      );
    } else {
      check("GET /api/sessions/{id}/drafts → 200 []", false, "no session_id");
    }
  }

  /* ---- 4. GET /api/drafts/nonexistent → 404 invalid_request ---- */
  {
    const r = await api("GET", "/api/drafts/f6-04-nonexistent-draft");
    check(
      "GET /api/drafts/nonexistent → 404 invalid_request",
      r.status === 404 && r.body?.error_code === "invalid_request",
      `status=${r.status} error_code=${r.body?.error_code ?? "—"}`,
    );
  }

  /* ---- 5. POST /api/drafts/nonexistent/confirm {revision:1} → 404 ---- */
  {
    const r = await api("POST", "/api/drafts/f6-04-nonexistent-draft/confirm", {
      revision: 1,
    });
    check(
      "POST /api/drafts/nonexistent/confirm → 404 invalid_request",
      r.status === 404 && r.body?.error_code === "invalid_request",
      `status=${r.status} error_code=${r.body?.error_code ?? "—"}`,
    );
  }

  /* ---- 6. POST /api/drafts/nonexistent/revise → 404 ---- */
  {
    // revise 传输形状恰为 {revision, payload}；身份查找先于 payload 解码，载荷可占位。
    const r = await api("POST", "/api/drafts/f6-04-nonexistent-draft/revise", {
      revision: 1,
      payload: {},
    });
    check(
      "POST /api/drafts/nonexistent/revise → 404 invalid_request",
      r.status === 404 && r.body?.error_code === "invalid_request",
      `status=${r.status} error_code=${r.body?.error_code ?? "—"}`,
    );
  }

  /* ---- 7. 确认前后档案保持 null（无草稿时误操作不写正式档案） ---- */
  {
    const r = await api("GET", "/api/profile");
    check(
      "GET /api/profile after mis-ops → profile still null",
      r.status === 200 && r.body?.profile === null,
      `status=${r.status} profile=${r.body?.profile === null ? "null" : "obj"}`,
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
if (!existsSync(evidenceDir)) mkdirSync(evidenceDir, { recursive: true });
// 头行：`passed=N failed=M`（便于 grep / evidence 引用）
writeFileSync(
  evidencePath,
  `passed=${passed} failed=${failed}\n` + lines.join("\n") + "\n",
  "utf8",
);
log(`# evidence: ${evidencePath}`);
process.exit(failed === 0 ? 0 : 1);
