/**
 * F6-09a 协议探针：安全、失败与重启恢复——无模型协议面（真实后端 uvicorn + 临时数据目录；
 * 假 Key 仅用于泄漏断言、无真实模型、仅回环）。
 *
 * 覆盖 stage6 F6-09（协议面；完整重启剧本与 Windows 人工归 F6-09b/F6-10）：
 * A. 密钥不回显
 *   1. PUT /api/provider/api-key 写入假 Key → has_api_key=true
 *   2. GET /api/provider 响应全文不含假 Key
 *   3. 假 Key 后 POST /api/reviews：Run error 字段不含 Key 明文
 *   4. 捕获 uvicorn stdout/stderr：不含假 Key
 * B. 失败可理解（错误码 + 有 message）
 *   5. 无 Key Run → failed + model_request_failed（Run DTO 无 message 字段，
 *      可读文案由前端 failureReasonCopy 映射 error_code；此处断言 error_code 非空）
 *   6. 缺 client_request_id POST /api/reviews → 400 invalid_request + message 非空
 *   7. GET /api/drafts/nonexistent → 404 invalid_request + message 非空
 *   8. stale/invalid：confirm 错 revision → 409 draft_modified（离线 pytest 已覆盖；
 *      本探针无模型难稳定造 draft 行，SKIP）
 * C. 回环边界
 *   9. Host=127.0.0.1:port → 200
 *  10. Host=evil.example.com → 403
 *  11. Host=127.0.0.1:port 且 Origin=http://evil.example.com → 403
 *  12. Host=localhost:port → 200
 *  13. spawn 参数含 --host 127.0.0.1
 * D. 重启恢复（探针内最小版）
 *  14. sqlite 向 data_dir 的 app.db 插入 running Run → kill → 同 data_dir 重启
 *      → GET run → failed + interrupted_by_restart
 *
 * 运行：cd frontend; node scripts/f6-09-probe.mjs
 * 归档：plans/stage6-evidence-assets/f6-09-probe.txt
 *
 * 红线：不向子进程传 MODEL_* 真实凭据；假 Key 仅用于断言「不含该字符串」；
 * 不改业务代码；不新增端点。
 */
import { spawn, spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, existsSync, writeFileSync, mkdirSync } from "node:fs";
import http from "node:http";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const frontendRoot = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const backendRoot = path.join(frontendRoot, "..", "backend");
const evidenceDir = path.join(frontendRoot, "plans", "stage6-evidence-assets");
const evidencePath = path.join(evidenceDir, "f6-09-probe.txt");

/** 辨识度明确的假 Key：出现在任何响应、Run 字段或后端日志里即视为泄漏。 */
const FAKE_KEY = "probe-fake-key-not-real-f609";

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

const dataDir = mkdtempSync(path.join(os.tmpdir(), "fit-agent-f6-09-"));
const port = await freePort();
const base = `http://127.0.0.1:${port}`;

log(`# F6-09 probe — ${new Date().toISOString()}`);
log(`# backend=${usePython} data_dir=${dataDir} base=${base}`);
log("# loopback only; fake key only for leak assertions; no real model");
log("");

/** uvicorn CLI 参数（供 C13 断言 bind 仅回环；与 spawnBackend 保持一致）。 */
const uvicornArgs = [
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
];

function spawnBackend() {
  const env = { ...process.env, FIT_AGENT_DATA_DIR: dataDir, PYTHONPATH: backendRoot };
  // 红线：不向探针子进程传真实凭据（即使宿主 shell 里有）。
  delete env.MODEL_API_KEY;
  delete env.MODEL_BASE_URL;
  delete env.MODEL_NAME;
  const proc = spawn(usePython, uvicornArgs, {
    cwd: backendRoot,
    env,
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });
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
  await new Promise((r) => setTimeout(r, 400));
}

/** fetch 便捷封装（同 base、JSON body）。 */
const api = async (method, p, body, headers = {}) => {
  const res = await fetch(base + p, {
    method,
    headers: { "Content-Type": "application/json", ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  let json = null;
  try {
    json = JSON.parse(text);
  } catch {
    /* non-JSON ok */
  }
  return { status: res.status, body: json, text, headers: res.headers };
};

/**
 * 用底层 http.request 覆盖 Host 头（undici fetch 的 Host 可能被忽略或禁止）。
 * 返回 {status, text, body}。
 */
function rawHttp(method, pathName, { hostHeader, originHeader, body } = {}) {
  return new Promise((resolve, reject) => {
    const payload = body === undefined ? undefined : JSON.stringify(body);
    const headers = {
      Host: hostHeader,
      Accept: "application/json",
      "Content-Length": payload ? Buffer.byteLength(payload) : 0,
    };
    if (originHeader) headers.Origin = originHeader;
    if (payload) headers["Content-Type"] = "application/json";
    const req = http.request(
      {
        host: "127.0.0.1",
        port,
        method,
        path: pathName,
        headers,
      },
      (res) => {
        const chunks = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () => {
          const text = Buffer.concat(chunks).toString("utf8");
          let json = null;
          try {
            json = JSON.parse(text);
          } catch {
            /* non-JSON ok */
          }
          resolve({ status: res.statusCode, text, body: json });
        });
      },
    );
    req.on("error", reject);
    if (payload) req.write(payload);
    req.end();
  });
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

/** 响应全文 / 结构化字段是否出现假 Key 明文。 */
function leakIn(...blobs) {
  return blobs.some((b) => {
    if (b == null) return false;
    const s = typeof b === "string" ? b : JSON.stringify(b);
    return s.includes(FAKE_KEY);
  });
}

let { proc, out } = spawnBackend();
let procOut = out;

try {
  await waitHealthz(base);
  check("backend_ready", true, `uvicorn ${base}`);

  /* ================= A. 密钥不回显 ================= */

  /* ---- A1. PUT 录入假 Key → has_api_key=true ---- */
  {
    const r = await api("PUT", "/api/provider/api-key", { api_key: FAKE_KEY });
    check(
      "A1 PUT /api/provider/api-key → has_api_key=true",
      r.status === 200 && r.body?.has_api_key === true,
      `status=${r.status} has_api_key=${r.body?.has_api_key}`,
    );
  }

  /* ---- A2. GET /api/provider 响应全文不含假 Key ---- */
  {
    const r = await api("GET", "/api/provider");
    check(
      "A2 GET /api/provider 200 has_api_key=true no key plaintext",
      r.status === 200 && r.body?.has_api_key === true && !r.text.includes(FAKE_KEY),
      `status=${r.status} leak=${r.text.includes(FAKE_KEY)}`,
    );
  }

  /* ---- A3. 假 Key 时 POST /api/reviews：Run error 字段不含 Key 明文 ---- */
  let fakeKeyRunId = null;
  {
    const clientRequestId = `f6-09-fake-key-${Date.now()}`;
    const submit = await api("POST", "/api/reviews", { client_request_id: clientRequestId });
    fakeKeyRunId = submit.body?.run?.run_id ?? null;
    check(
      "A3a POST /api/reviews accepted (fake key present)",
      submit.status === 200 && fakeKeyRunId,
      `status=${submit.status} run=${fakeKeyRunId ?? "—"}`,
    );
    if (fakeKeyRunId) {
      const run = await waitForRun(fakeKeyRunId);
      const leak =
        leakIn(run, submit.text) ||
        (run != null && leakIn(JSON.stringify(run)));
      check(
        "A3b Run payload no fake key plaintext",
        run != null && !leak,
        run
          ? `status=${run.status} error_code=${run.error_code} leak=${leak}`
          : "timeout waiting terminal",
      );
    } else {
      check("A3b Run payload no fake key plaintext", false, "no run_id");
    }
  }

  /* ---- A4. 后端 stdout/stderr 无假 Key 明文 ---- */
  {
    const backendText = procOut.text;
    check(
      "A4 backend stdout/stderr contains no fake key",
      !backendText.includes(FAKE_KEY),
      `len=${backendText.length}`,
    );
  }

  /* ================= B. 失败可理解 ================= */

  /* ---- B5. 无 Key：DELETE Key 后 Run → failed + model_request_failed ---- */
  {
    await api("DELETE", "/api/provider/api-key");
    const clientRequestId = `f6-09-no-key-${Date.now()}`;
    const submit = await api("POST", "/api/reviews", { client_request_id: clientRequestId });
    const runId = submit.body?.run?.run_id ?? null;
    check(
      "B5a POST /api/reviews accepted (no key)",
      submit.status === 200 && runId,
      `status=${submit.status} run=${runId ?? "—"}`,
    );
    if (runId) {
      const run = await waitForRun(runId);
      // Run DTO 无独立 message 字段（stage4 §6）：可读文案由前端 failureReasonCopy
      // 按 error_code 映射；此处断言 error_code 非空即可理解（机器可读原因）。
      check(
        "B5b Run failed error_code=model_request_failed (non-empty)",
        run != null &&
          run.status === "failed" &&
          run.error_code === "model_request_failed" &&
          typeof run.error_code === "string" &&
          run.error_code.length > 0,
        run ? `status=${run.status} error_code=${run.error_code}` : "timeout",
      );
      // 无 Key 失败响应也不得泄漏（此处无 Key，仍扫一遍）
      check(
        "B5c no-key Run payload has no key-like leak",
        !leakIn(run, submit.text),
        `leak=${leakIn(run, submit.text)}`,
      );
    } else {
      check("B5b Run failed error_code=model_request_failed (non-empty)", false, "no run_id");
      check("B5c no-key Run payload has no key-like leak", false, "no run_id");
    }
  }

  /* ---- B6. 缺 client_request_id POST /api/reviews → 400 invalid_request + message ---- */
  {
    const r = await api("POST", "/api/reviews", {});
    check(
      "B6 POST /api/reviews empty body → 400 invalid_request + message",
      r.status === 400 &&
        r.body?.error_code === "invalid_request" &&
        typeof r.body?.message === "string" &&
        r.body.message.length > 0,
      `status=${r.status} error_code=${r.body?.error_code} message=${JSON.stringify(r.body?.message)}`,
    );
  }

  /* ---- B7. GET /api/drafts/nonexistent → 404 invalid_request + message ---- */
  {
    const r = await api("GET", "/api/drafts/f6-09-nonexistent-draft");
    check(
      "B7 GET /api/drafts/nonexistent → 404 invalid_request + message",
      r.status === 404 &&
        r.body?.error_code === "invalid_request" &&
        typeof r.body?.message === "string" &&
        r.body.message.length > 0,
      `status=${r.status} error_code=${r.body?.error_code} message=${JSON.stringify(r.body?.message)}`,
    );
  }

  /* ---- B8. stale/invalid：confirm 错 revision → 409 draft_modified ---- */
  {
    // 离线 pytest（test_stage2/3）已覆盖 draft_stale/draft_modified；
    // 本探针空库+无模型难稳定造 draft 行，SKIP（不计入 PASS/FAIL）。
    log("SKIP B8 draft_modified 409 — offline pytest covers; hard to seed draft without model");
  }

  /* ================= C. 回环边界 ================= */

  /* ---- C9. Host=127.0.0.1:port → 200 ---- */
  {
    const r = await rawHttp("GET", "/api/provider", {
      hostHeader: `127.0.0.1:${port}`,
    });
    check("C9 Host=127.0.0.1:port → 200", r.status === 200, `status=${r.status}`);
  }

  /* ---- C10. Host=evil.example.com → 403 ---- */
  {
    const r = await rawHttp("GET", "/api/provider", {
      hostHeader: "evil.example.com",
    });
    check("C10 Host=evil.example.com → 403", r.status === 403, `status=${r.status}`);
  }

  /* ---- C11. Host=127.0.0.1:port + Origin=evil → 403 ---- */
  {
    const r = await rawHttp("GET", "/api/provider", {
      hostHeader: `127.0.0.1:${port}`,
      originHeader: "http://evil.example.com",
    });
    check(
      "C11 Host=loopback + Origin=http://evil.example.com → 403",
      r.status === 403,
      `status=${r.status}`,
    );
  }

  /* ---- C12. Host=localhost:port → 200 ---- */
  {
    const r = await rawHttp("GET", "/api/provider", {
      hostHeader: `localhost:${port}`,
    });
    check("C12 Host=localhost:port → 200", r.status === 200, `status=${r.status}`);
  }

  /* ---- C13. spawn 参数含 --host 127.0.0.1（仅监听回环） ---- */
  {
    const hasLoopbackBind =
      uvicornArgs.includes("--host") &&
      uvicornArgs[uvicornArgs.indexOf("--host") + 1] === "127.0.0.1";
    check(
      "C13 spawn args contain --host 127.0.0.1",
      hasLoopbackBind,
      `args=${uvicornArgs.join(" ")}`,
    );
  }

  /* ================= D. 重启恢复（最小版） ================= */

  /* ---- D14. sqlite 插入 running Run → kill → 重启 → failed + interrupted_by_restart ---- */
  {
    // 先确认库已迁移（healthz 已过）；停止后端后用 sqlite 直接插一条 running Run。
    const dbPath = path.join(dataDir, "app.db");
    if (!existsSync(dbPath)) {
      check("D14 restart recovery → interrupted_by_restart", false, `no db at ${dbPath}`);
    } else {
      await killBackend(proc);
      const runId = `f6-09-restart-${Date.now()}`;
      const convId = `f6-09-conv-${Date.now()}`;
      const now = new Date().toISOString();
      const py = [
        "-c",
        [
          "import sqlite3,sys",
          "conn=sqlite3.connect(sys.argv[1])",
          "conn.execute('INSERT INTO conversations (id,created_at) VALUES (?,?)',(sys.argv[2],sys.argv[4]))",
          "conn.execute(",
          "  \"INSERT INTO runs (id,conversation_id,client_request_id,status,created_at,updated_at)\"",
          "  \" VALUES (?,?,?,?,?,?)\",",
          "  (sys.argv[3],sys.argv[2],'f6-09-restart-cri','running',sys.argv[4],sys.argv[4]),",
          ")",
          "conn.commit()",
          "conn.close()",
        ].join("\n"),
        dbPath,
        convId,
        runId,
        now,
      ];
      const ins = spawnSync(usePython, py, { encoding: "utf8", cwd: backendRoot });
      if (ins.status !== 0) {
        check(
          "D14 restart recovery → interrupted_by_restart",
          false,
          `insert failed status=${ins.status} stderr=${(ins.stderr || "").slice(0, 200)}`,
        );
      } else {
        ({ proc, out } = spawnBackend());
        procOut = out;
        await waitHealthz(base);
        const r = await api("GET", `/api/runs/${runId}`);
        const run = r.body?.run;
        check(
          "D14 restart recovery → failed + interrupted_by_restart",
          r.status === 200 &&
            run?.status === "failed" &&
            run?.error_code === "interrupted_by_restart",
          r.status === 200
            ? `status=${run?.status} error_code=${run?.error_code}`
            : `http=${r.status} text=${r.text.slice(0, 120)}`,
        );
      }
    }
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
writeFileSync(
  evidencePath,
  `passed=${passed} failed=${failed}\n` + lines.join("\n") + "\n",
  "utf8",
);
log(`# evidence: ${evidencePath}`);
process.exit(failed === 0 ? 0 : 1);
