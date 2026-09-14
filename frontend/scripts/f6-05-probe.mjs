/**
 * F6-05 协议探针：计划启用闭环的无模型协议面（真实后端 uvicorn + 临时数据目录；假 Key、无真实模型、仅回环）。
 *
 * 覆盖 stage6 F6-05（协议级；空库 + 无计划）：
 * 1. GET /api/plan → 200 且 plan === null（尚无正式计划）
 * 2. GET /api/plan/guidance → 200 且 guidance === null（无计划不给可执行处方）
 * 3. PUT/POST /api/plan → 404/405（对话是唯一变更入口，无公开写计划端点）
 * 4. GET /api/plans/nonexistent → 404 invalid_request
 * 5. GET /api/plan/guidance?arrangement_revision_id=missing → 404 invalid_request（不伪造）
 * 6. 无 plan 草稿时 GET /api/sessions/{id}/drafts → 空数组（新建 session 后）
 * 7. POST /api/drafts/nonexistent/confirm {revision:1} → 404（不半写计划）
 * 8. 空库任何误操作后 GET /api/plan 仍为 null
 *
 * 运行：node scripts/f6-05-probe.mjs
 * 归档：plans/stage6-evidence-assets/f6-05-probe.txt
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
const evidencePath = path.join(evidenceDir, "f6-05-probe.txt");

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

const dataDir = mkdtempSync(path.join(os.tmpdir(), "fit-agent-f6-05-"));
const port = await freePort();
const base = `http://127.0.0.1:${port}`;

log(`# F6-05 probe — ${new Date().toISOString()}`);
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

  /* ---- 1. GET /api/plan → 200 且 plan === null（尚无正式计划） ---- */
  {
    const r = await api("GET", "/api/plan");
    check(
      "GET /api/plan → 200 plan=null (尚无正式计划)",
      r.status === 200 && r.body?.plan === null,
      `status=${r.status} plan=${r.body?.plan === null ? "null" : "obj"}`,
    );
  }

  /* ---- 2. GET /api/plan/guidance → 200 且 guidance === null（无计划不给可执行处方） ---- */
  {
    const r = await api("GET", "/api/plan/guidance");
    check(
      "GET /api/plan/guidance → 200 guidance=null (无计划不给处方)",
      r.status === 200 && r.body?.guidance === null,
      `status=${r.status} guidance=${r.body?.guidance === null ? "null" : "obj"}`,
    );
  }

  /* ---- 3. PUT/POST /api/plan → 404/405（对话是唯一变更入口） ---- */
  {
    const put = await api("PUT", "/api/plan", { plan: {} });
    check(
      "PUT /api/plan → 404/405 (无公开写计划入口)",
      put.status === 404 || put.status === 405,
      `status=${put.status}`,
    );

    const post = await api("POST", "/api/plan", { plan: {} });
    check(
      "POST /api/plan → 404/405 (无公开写计划入口)",
      post.status === 404 || post.status === 405,
      `status=${post.status}`,
    );
  }

  /* ---- 4. GET /api/plans/nonexistent → 404 invalid_request ---- */
  {
    const r = await api("GET", "/api/plans/f6-05-nonexistent-plan");
    check(
      "GET /api/plans/nonexistent → 404 invalid_request",
      r.status === 404 && r.body?.error_code === "invalid_request",
      `status=${r.status} error_code=${r.body?.error_code ?? "—"}`,
    );
  }

  /* ---- 5. GET /api/plan/guidance?arrangement_revision_id=missing → 404 invalid_request（不伪造） ---- */
  {
    const r = await api(
      "GET",
      "/api/plan/guidance?arrangement_revision_id=f6-05-missing-arr",
    );
    check(
      "GET /api/plan/guidance?arrangement_revision_id=missing → 404 invalid_request",
      r.status === 404 && r.body?.error_code === "invalid_request",
      `status=${r.status} error_code=${r.body?.error_code ?? "—"}`,
    );
  }

  /* ---- 6. 新会话 + GET /api/sessions/{id}/drafts → 空数组（无 plan 草稿） ---- */
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
        "GET /api/sessions/{id}/drafts → 200 [] (无 plan 草稿)",
        drafts.status === 200 && Array.isArray(drafts.body) && drafts.body.length === 0,
        `status=${drafts.status} n=${Array.isArray(drafts.body) ? drafts.body.length : "—"}`,
      );
    } else {
      check("GET /api/sessions/{id}/drafts → 200 [] (无 plan 草稿)", false, "no session_id");
    }
  }

  /* ---- 7. POST /api/drafts/nonexistent/confirm {revision:1} → 404（不半写计划） ---- */
  {
    const r = await api("POST", "/api/drafts/f6-05-nonexistent-draft/confirm", {
      revision: 1,
    });
    check(
      "POST /api/drafts/nonexistent/confirm → 404 invalid_request",
      r.status === 404 && r.body?.error_code === "invalid_request",
      `status=${r.status} error_code=${r.body?.error_code ?? "—"}`,
    );
  }

  /* ---- 8. 空库任何误操作后 GET /api/plan 仍为 null ---- */
  {
    const r = await api("GET", "/api/plan");
    check(
      "GET /api/plan after mis-ops → plan still null",
      r.status === 200 && r.body?.plan === null,
      `status=${r.status} plan=${r.body?.plan === null ? "null" : "obj"}`,
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
