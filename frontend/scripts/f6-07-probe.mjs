/**
 * F6-07a 协议探针：复盘协议面（真实后端 uvicorn + 临时数据目录；假 Key、无真实模型、仅回环）。
 *
 * 覆盖 stage6 F6-07（无模型路径；真实模型闭环归 F6-07b）：
 * 1. GET /api/reviews → 200 空列表（空库不伪造正文）
 * 2. GET /api/reviews/nonexistent → 404 invalid_request
 * 3. POST /api/reviews 空 body → 400 invalid_request（缺 client_request_id）
 * 4. 无公开 PUT/PATCH /api/reviews → 404/405（对话是唯一变更入口）
 * 5. GET /api/reviews 不启动 Run（空库仍为空；读端点无副作用）
 * 6. 无 Key 时 POST /api/reviews → Run 终态 failed / error_code=model_request_failed（不外呼）
 * 7. 幂等：同一 client_request_id 再 POST → created=false 且不新建第二条 reviews
 *    （若 Run 已 failed 仍幂等返回同一 Run）
 * 8. 误操作后 GET /api/reviews 仍 []
 *
 * 运行：node scripts/f6-07-probe.mjs（cwd=frontend）
 * 归档：plans/stage6-evidence-assets/f6-07-probe.txt
 *
 * 红线：不向子进程传 MODEL_* 真实凭据；不用真实 Key；不改业务代码；不新增端点。
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
const evidencePath = path.join(evidenceDir, "f6-07-probe.txt");

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

const dataDir = mkdtempSync(path.join(os.tmpdir(), "fit-agent-f6-07-"));
const port = await freePort();
const base = `http://127.0.0.1:${port}`;

log(`# F6-07 probe — ${new Date().toISOString()}`);
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

  /* ---- 1. GET /api/reviews → 200 空列表（空库不伪造正文） ---- */
  {
    const r = await api("GET", "/api/reviews");
    check(
      "GET /api/reviews → 200 []",
      r.status === 200 && Array.isArray(r.body?.reviews) && r.body.reviews.length === 0,
      `status=${r.status} n=${Array.isArray(r.body?.reviews) ? r.body.reviews.length : "—"}`,
    );
  }

  /* ---- 2. GET /api/reviews/nonexistent → 404 invalid_request ---- */
  {
    const r = await api("GET", "/api/reviews/f6-07-nonexistent-review");
    check(
      "GET /api/reviews/nonexistent → 404 invalid_request",
      r.status === 404 && r.body?.error_code === "invalid_request",
      `status=${r.status} error_code=${r.body?.error_code ?? "—"}`,
    );
  }

  /* ---- 3. POST /api/reviews 空 body → 400 invalid_request（缺 client_request_id） ---- */
  {
    const r = await api("POST", "/api/reviews", {});
    check(
      "POST /api/reviews empty body → 400 invalid_request",
      r.status === 400 && r.body?.error_code === "invalid_request",
      `status=${r.status} error_code=${r.body?.error_code ?? "—"}`,
    );
  }

  /* ---- 4. 无公开 PUT/PATCH /api/reviews → 404/405 ---- */
  {
    const put = await api("PUT", "/api/reviews", { review: {} });
    check(
      "PUT /api/reviews → 404/405 (无公开写入口)",
      put.status === 404 || put.status === 405,
      `status=${put.status}`,
    );

    const patch = await api("PATCH", "/api/reviews", { review: {} });
    check(
      "PATCH /api/reviews → 404/405 (无公开写入口)",
      patch.status === 404 || patch.status === 405,
      `status=${patch.status}`,
    );
  }

  /* ---- 5. GET /api/reviews 不启动 Run（空库仍为空；读端点无副作用） ---- */
  {
    const before = await api("GET", "/api/reviews");
    const reviewsBefore = Array.isArray(before.body?.reviews) ? before.body.reviews.length : -1;
    const after = await api("GET", "/api/reviews");
    const reviewsAfter = Array.isArray(after.body?.reviews) ? after.body.reviews.length : -1;
    check(
      "GET /api/reviews has no side-effect (still [])",
      before.status === 200 &&
        after.status === 200 &&
        reviewsBefore === 0 &&
        reviewsAfter === 0,
      `before_n=${reviewsBefore} after_n=${reviewsAfter}`,
    );
  }

  /* ---- 6. 无 Key：POST /api/reviews → Run 终态 failed / model_request_failed ---- */
  let reviewRunId = null;
  let clientRequestId = null;
  {
    clientRequestId = `f6-07-probe-${Date.now()}`;
    const submit = await api("POST", "/api/reviews", {
      client_request_id: clientRequestId,
    });
    reviewRunId = submit.body?.run?.run_id ?? null;
    check(
      "POST /api/reviews accepted",
      submit.status === 200 && submit.body?.created === true && reviewRunId,
      `status=${submit.status} created=${submit.body?.created} run=${reviewRunId ?? "—"}`,
    );
    if (reviewRunId) {
      const run = await waitForRun(reviewRunId);
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
    // 失败的 Run 不产生正文
    const reviews = await api("GET", "/api/reviews");
    check(
      "after failed Run: GET /api/reviews still []",
      reviews.status === 200 &&
        Array.isArray(reviews.body?.reviews) &&
        reviews.body.reviews.length === 0,
      `status=${reviews.status} n=${Array.isArray(reviews.body?.reviews) ? reviews.body.reviews.length : "—"}`,
    );
  }

  /* ---- 7. 幂等：同一 client_request_id 再 POST → created=false 且不新建 ---- */
  {
    const second = await api("POST", "/api/reviews", {
      client_request_id: clientRequestId,
    });
    const secondRunId = second.body?.run?.run_id ?? null;
    check(
      "idempotent POST /api/reviews → created=false same run",
      second.status === 200 &&
        second.body?.created === false &&
        secondRunId === reviewRunId,
      `status=${second.status} created=${second.body?.created} run=${secondRunId ?? "—"}`,
    );
    const reviews = await api("GET", "/api/reviews");
    check(
      "idempotent: GET /api/reviews still [] (no second review row)",
      reviews.status === 200 &&
        Array.isArray(reviews.body?.reviews) &&
        reviews.body.reviews.length === 0,
      `status=${reviews.status} n=${Array.isArray(reviews.body?.reviews) ? reviews.body.reviews.length : "—"}`,
    );
  }

  /* ---- 8. 误操作后 GET /api/reviews 仍 [] ---- */
  {
    const r = await api("GET", "/api/reviews");
    check(
      "after mis-ops: GET /api/reviews still []",
      r.status === 200 &&
        Array.isArray(r.body?.reviews) &&
        r.body.reviews.length === 0,
      `status=${r.status} n=${Array.isArray(r.body?.reviews) ? r.body.reviews.length : "—"}`,
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
