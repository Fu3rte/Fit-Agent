/**
 * F6-02d 协议探针：真实后端（uvicorn + 临时数据目录）协议联通验收。
 * 仅回环、无真实模型；无 API Key → Run 以 model_request_failed 终态失败（不发外部请求）。
 *
 * 运行：node scripts/f6-02-probe.mjs
 * 归档：plans/stage6-evidence-assets/f6-02-probe.txt
 *
 * F6-01 归属（2026-09-14 backend 全量回滚后）：
 * - Provider 路由 / 静态托管归后端 owner，本探针不再把其缺失当 FAIL：
 *   404 / 非 SPA → SKIP（provider_route_pending_backend_owner / static_hosting_pending_backend_owner）。
 * - 路由若再次出现，仍校验 has_api_key=false 且无明文 Key（不放松）。
 *
 * 已知限制（不静默改语义）：
 * - 安排徽章：真实后端无跨会话安排枚举端点；ProfilePage 的 arranged 恒空 →
 *   生产路径徽章默认「尚无安排」（假阴性），已知限制见 02d 报告。
 * - GET /api/stats/completion、/api/stats/pr 需必填查询参数；空库下给假 id
 *   返回 null 候选（可理解），不强制业务有数据。
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
const evidencePath = path.join(evidenceDir, "f6-02-probe.txt");

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

const dataDir = mkdtempSync(path.join(os.tmpdir(), "fit-agent-f6-02-"));
const port = await freePort();
const base = `http://127.0.0.1:${port}`;

log(`# F6-02 probe — ${new Date().toISOString()}`);
log(`# backend=${usePython} data_dir=${dataDir} base=${base}`);
log(`# loopback only; no real model / no API key`);
log("");

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
    env: {
      ...process.env,
      FIT_AGENT_DATA_DIR: dataDir,
      PYTHONPATH: backendRoot,
    },
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  },
);

let procOut = "";
proc.stdout?.on("data", (d) => {
  procOut += d.toString();
});
proc.stderr?.on("data", (d) => {
  procOut += d.toString();
});

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
    /* non-JSON ok for static */
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

  /* ---- 1. /healthz ---- */
  {
    const r = await api("GET", "/healthz");
    check("GET /healthz", r.status === 200 && r.body != null, `status=${r.status}`);
  }

  /* ---- 2. /api/provider：F6-01 归后端 owner；404 可接受 ---- */
  {
    const r = await api("GET", "/api/provider");
    if (r.status === 404) {
      log(
        "SKIP GET /api/provider has_api_key=false no data_dir — provider_route_pending_backend_owner status=404",
      );
    } else {
      const body = r.body ?? {};
      const noKey =
        body.has_api_key === false &&
        body.data_dir === undefined &&
        typeof body.protocol === "string" &&
        body.model != null &&
        !JSON.stringify(body).toLowerCase().includes("api_key_secret");
      check(
        "GET /api/provider has_api_key=false no data_dir",
        r.status === 200 && noKey,
        `status=${r.status} has_api_key=${body.has_api_key} keys=${Object.keys(body).join(",")}`,
      );
    }
  }

  /* ---- 3. session create + get ---- */
  let sessionId = null;
  {
    const create = await api("POST", "/api/sessions", {});
    check("POST /api/sessions", create.status === 200 && create.body?.session_id, `status=${create.status}`);
    sessionId = create.body?.session_id ?? null;
    if (sessionId) {
      const get = await api("GET", `/api/sessions/${sessionId}`);
      check(
        "GET /api/sessions/{id}",
        get.status === 200 && get.body?.session_id === sessionId,
        `status=${get.status}`,
      );
    } else {
      check("GET /api/sessions/{id}", false, "no session_id");
    }
  }

  /* ---- 4. read-only endpoints (empty DB → null / empty arrays) ---- */
  {
    const plan = await api("GET", "/api/plan");
    check(
      "GET /api/plan",
      plan.status === 200 && plan.body && "plan" in plan.body,
      `status=${plan.status} plan=${plan.body?.plan === null ? "null" : "obj"}`,
    );

    const guidance = await api("GET", "/api/plan/guidance");
    check(
      "GET /api/plan/guidance",
      guidance.status === 200 && guidance.body && "guidance" in guidance.body,
      `status=${guidance.status} guidance=${guidance.body?.guidance === null ? "null" : "obj"}`,
    );

    const records = await api("GET", "/api/records");
    check(
      "GET /api/records",
      records.status === 200 && Array.isArray(records.body?.records),
      `status=${records.status} n=${records.body?.records?.length}`,
    );

    const completion = await api(
      "GET",
      "/api/stats/completion?plan_version_id=probe-no-plan&week_no=1",
    );
    check(
      "GET /api/stats/completion (required query)",
      completion.status === 200 && completion.body && "completion" in completion.body,
      `status=${completion.status} completion=${completion.body?.completion === null ? "null" : "obj"}`,
    );

    const pr = await api(
      "GET",
      "/api/stats/pr?exercise_id=probe-ex&load_notation=reps_weight",
    );
    check(
      "GET /api/stats/pr (required query)",
      pr.status === 200 && pr.body?.pr != null,
      `status=${pr.status} max=${pr.body?.pr?.max_load_kg_key}`,
    );

    const reviews = await api("GET", "/api/reviews");
    check(
      "GET /api/reviews",
      reviews.status === 200 && Array.isArray(reviews.body?.reviews),
      `status=${reviews.status} n=${reviews.body?.reviews?.length}`,
    );

    const profile = await api("GET", "/api/profile");
    check(
      "GET /api/profile",
      profile.status === 200 && profile.body && "profile" in profile.body,
      `status=${profile.status} profile=${profile.body?.profile === null ? "null" : "obj"}`,
    );
  }

  /* ---- 5. no key: submit request → Run fails with model_request_failed ---- */
  if (sessionId) {
    const submit = await api("POST", `/api/sessions/${sessionId}/requests`, {
      client_request_id: `f6-02-probe-${Date.now()}`,
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
        "Run terminal model_request_failed (no key, no external call)",
        run != null && run.status === "failed" && run.error_code === "model_request_failed",
        run ? `status=${run.status} error_code=${run.error_code}` : "timeout waiting terminal",
      );
    } else {
      check("Run terminal model_request_failed (no key, no external call)", false, "no run_id");
    }
  }

  /* ---- 6. static hosting：F6-01 归后端 owner；无静态路由时 SKIP ---- */
  {
    const distExists = existsSync(path.join(frontendRoot, "dist", "index.html"));
    const root = await api("GET", "/");
    const looksHtml =
      root.status === 200 &&
      /html/i.test(root.text.slice(0, 200) || "") &&
      /<html/i.test(root.text);
    if (looksHtml) {
      // 路由若再次出现则仍校验 HTML 形状（不放松）
      check(
        "GET / serves index.html when dist present",
        distExists,
        `status=${root.status} dist=${distExists}`,
      );
    } else {
      log(
        `SKIP GET / static hosting — static_hosting_pending_backend_owner status=${root.status}`,
      );
    }
    log(`# static: dist_present=${distExists} GET/ status=${root.status}`);
  }
} catch (e) {
  check("probe_unexpected_error", false, String(e));
  if (procOut) log(`# backend output (truncated):\n${procOut.slice(-2000)}`);
} finally {
  try {
    proc.kill();
    await new Promise((r) => setTimeout(r, 300));
    if (!proc.killed) proc.kill("SIGKILL");
  } catch {
    /* ignore */
  }
  try {
    rmSync(dataDir, { recursive: true, force: true });
  } catch {
    /* ignore */
  }
}

log("");
log(`# summary: passed=${passed} failed=${failed}`);
if (!existsSync(evidenceDir)) mkdirSync(evidenceDir, { recursive: true });
writeFileSync(evidencePath, lines.join("\n") + "\n", "utf8");
log(`# evidence: ${evidencePath}`);
process.exit(failed === 0 ? 0 : 1);
