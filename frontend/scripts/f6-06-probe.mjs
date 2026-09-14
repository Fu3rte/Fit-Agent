/**
 * F6-06a 协议探针：训练/统计/复盘只读协议面（真实后端 uvicorn + 临时数据目录；假 Key、无真实模型、仅回环）。
 *
 * 覆盖 stage6 F6-06 空库协议面（无模型路径；对话产生记录草稿→确认归 F6-06b 真实闭环）：
 * 1. GET /api/records → 200 空列表（尚无记录）
 * 2. GET /api/stats/completion → 200 completion=null（空数据不伪造完成率）
 * 3. GET /api/stats/pr → 200 max_load_kg_key/best_reps=null（空数据不伪造 PR）
 * 4. GET /api/reviews → 200 空列表
 * 5. 无公开写记录端点：PUT/POST /api/records → 404/405（对话是唯一变更入口）
 * 6. GET /api/records/nonexistent-session → 404 invalid_request
 * 7. POST /api/drafts/nonexistent/confirm {revision:1} → 404（不半写记录）
 * 8. POST /api/drafts/nonexistent/void → 404
 * 9. 空库误操作后统计仍为空/零/null，不出现编造数字
 *
 * 运行：node scripts/f6-06-probe.mjs（cwd=frontend）
 * 归档：plans/stage6-evidence-assets/f6-06-probe.txt
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
const evidencePath = path.join(evidenceDir, "f6-06-probe.txt");

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

const dataDir = mkdtempSync(path.join(os.tmpdir(), "fit-agent-f6-06-"));
const port = await freePort();
const base = `http://127.0.0.1:${port}`;

log(`# F6-06 probe — ${new Date().toISOString()}`);
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

  /* ---- 1. GET /api/records → 200 空列表 ---- */
  {
    const r = await api("GET", "/api/records");
    check(
      "GET /api/records → 200 []",
      r.status === 200 && Array.isArray(r.body?.records) && r.body.records.length === 0,
      `status=${r.status} n=${Array.isArray(r.body?.records) ? r.body.records.length : "—"}`,
    );
  }

  /* ---- 2. GET /api/stats/completion → 200 completion=null（空库不伪造完成率） ---- */
  {
    const r = await api(
      "GET",
      "/api/stats/completion?plan_version_id=f6-06-nonexistent-pv&week_no=1",
    );
    check(
      "GET /api/stats/completion → 200 completion=null (空数据不伪造)",
      r.status === 200 && r.body?.completion === null,
      `status=${r.status} completion=${r.body?.completion === null ? "null" : JSON.stringify(r.body?.completion)}`,
    );
  }

  /* ---- 3. GET /api/stats/pr → 200 空投影（null 不是 0） ---- */
  {
    const r = await api(
      "GET",
      "/api/stats/pr?exercise_id=barbell-back-squat&load_notation=barbell_includes_bar_total",
    );
    const pr = r.body?.pr;
    check(
      "GET /api/stats/pr → 200 max_load_kg_key/best_reps=null (空数据不伪造)",
      r.status === 200 &&
        pr != null &&
        pr.max_load_kg_key === null &&
        pr.best_reps === null,
      `status=${r.status} max_load_kg_key=${pr?.max_load_kg_key} best_reps=${pr?.best_reps}`,
    );
  }

  /* ---- 4. GET /api/reviews → 200 空列表 ---- */
  {
    const r = await api("GET", "/api/reviews");
    check(
      "GET /api/reviews → 200 []",
      r.status === 200 && Array.isArray(r.body?.reviews) && r.body.reviews.length === 0,
      `status=${r.status} n=${Array.isArray(r.body?.reviews) ? r.body.reviews.length : "—"}`,
    );
  }

  /* ---- 5. 无公开写记录端点：PUT/POST /api/records → 404/405 ---- */
  {
    const put = await api("PUT", "/api/records", { record: {} });
    check(
      "PUT /api/records → 404/405 (无公开写入口)",
      put.status === 404 || put.status === 405,
      `status=${put.status}`,
    );

    const post = await api("POST", "/api/records", { record: {} });
    check(
      "POST /api/records → 404/405 (无公开写入口)",
      post.status === 404 || post.status === 405,
      `status=${post.status}`,
    );

    // 会话级写路径同样不存在
    const putSession = await api("PUT", "/api/records/f6-06-nonexistent-session", {
      record: {},
    });
    check(
      "PUT /api/records/{id} → 404/405 (无公开写入口)",
      putSession.status === 404 || putSession.status === 405,
      `status=${putSession.status}`,
    );
  }

  /* ---- 6. GET /api/records/nonexistent-session → 404 ---- */
  {
    const r = await api("GET", "/api/records/f6-06-nonexistent-session");
    check(
      "GET /api/records/nonexistent-session → 404 invalid_request",
      r.status === 404 && r.body?.error_code === "invalid_request",
      `status=${r.status} error_code=${r.body?.error_code ?? "—"}`,
    );
  }

  /* ---- 7. POST /api/drafts/nonexistent/confirm {revision:1} → 404 ---- */
  {
    const r = await api("POST", "/api/drafts/f6-06-nonexistent-draft/confirm", {
      revision: 1,
    });
    check(
      "POST /api/drafts/nonexistent/confirm → 404 invalid_request",
      r.status === 404 && r.body?.error_code === "invalid_request",
      `status=${r.status} error_code=${r.body?.error_code ?? "—"}`,
    );
  }

  /* ---- 8. POST /api/drafts/nonexistent/void → 404 ---- */
  {
    // void 传输形状为 {revision}；身份查找先于其他校验，可占位 revision。
    const r = await api("POST", "/api/drafts/f6-06-nonexistent-draft/void", {
      revision: 1,
    });
    check(
      "POST /api/drafts/nonexistent/void → 404 invalid_request",
      r.status === 404 && r.body?.error_code === "invalid_request",
      `status=${r.status} error_code=${r.body?.error_code ?? "—"}`,
    );
  }

  /* ---- 9. 误操作后统计仍为空/null，不出现编造数字 ---- */
  {
    const records = await api("GET", "/api/records");
    check(
      "after mis-ops: GET /api/records still []",
      records.status === 200 &&
        Array.isArray(records.body?.records) &&
        records.body.records.length === 0,
      `status=${records.status} n=${Array.isArray(records.body?.records) ? records.body.records.length : "—"}`,
    );

    const completion = await api(
      "GET",
      "/api/stats/completion?plan_version_id=f6-06-nonexistent-pv&week_no=1",
    );
    check(
      "after mis-ops: completion still null (no fabricated rate)",
      completion.status === 200 && completion.body?.completion === null,
      `status=${completion.status} completion=${completion.body?.completion === null ? "null" : JSON.stringify(completion.body?.completion)}`,
    );

    const pr = await api(
      "GET",
      "/api/stats/pr?exercise_id=barbell-back-squat&load_notation=barbell_includes_bar_total",
    );
    check(
      "after mis-ops: PR still null (no fabricated numbers)",
      pr.status === 200 &&
        pr.body?.pr?.max_load_kg_key === null &&
        pr.body?.pr?.best_reps === null,
      `status=${pr.status} max_load_kg_key=${pr.body?.pr?.max_load_kg_key} best_reps=${pr.body?.pr?.best_reps}`,
    );

    const reviews = await api("GET", "/api/reviews");
    check(
      "after mis-ops: GET /api/reviews still []",
      reviews.status === 200 &&
        Array.isArray(reviews.body?.reviews) &&
        reviews.body.reviews.length === 0,
      `status=${reviews.status} n=${Array.isArray(reviews.body?.reviews) ? reviews.body.reviews.length : "—"}`,
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
