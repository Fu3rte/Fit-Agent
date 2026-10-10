import { createServer } from "node:net";
import { spawn } from "node:child_process";
import path from "node:path";

const repoRoot = path.resolve(import.meta.dirname, "..");
const concurrentlyBin = path.join(repoRoot, "node_modules", "concurrently", "dist", "bin", "concurrently.js");
const probeAttempts = 20;

// 开发期端口探测必须覆盖双栈：Vite 监听 localhost 时可能只绑 ::1，
// 仅查 127.0.0.1 会把已被占用的端口判为空闲，strictPort 随后崩溃。
function probe(host, port) {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.once("error", (error) => {
      if (error.code === "EADDRINUSE" || error.code === "EACCES" || error.code === "EPERM") {
        resolve(true);
        return;
      }
      // 本机缺该协议栈时 listen 报地址不可用，此地址不参与判定。
      if (error.code === "EADDRNOTAVAIL" || error.code === "EAFNOSUPPORT") {
        resolve(false);
        return;
      }
      reject(error);
    });
    server.once("listening", () => {
      server.close(() => resolve(false));
    });
    server.listen(port, host);
  });
}

async function isAvailable(port) {
  const busy = await Promise.all(["127.0.0.1", "::1"].map((host) => probe(host, port)));
  return busy.every((value) => value === false);
}

function readRequestedPort() {
  const raw = process.env.FIT_AGENT_FRONTEND_PORT ?? "5173";
  if (!/^[0-9]+$/.test(raw) || Number(raw) < 1 || Number(raw) > 65535) {
    throw new Error("FIT_AGENT_FRONTEND_PORT 必须是 1–65535 的整数");
  }
  return Number(raw);
}

async function pickPort(requested) {
  const lastCandidate = Math.min(requested + probeAttempts - 1, 65535);
  for (let candidate = requested; candidate <= lastCandidate; candidate += 1) {
    if (await isAvailable(candidate)) {
      return candidate;
    }
  }
  throw new Error(`端口 ${requested}–${lastCandidate} 全部被占用`);
}

const requestedPort = readRequestedPort();
const frontendPort = await pickPort(requestedPort);

if (frontendPort !== requestedPort) {
  console.log(`端口 ${requestedPort} 已被占用，前端改用 ${frontendPort}`);
}
console.log(`前端 http://localhost:${frontendPort} ，后端 http://127.0.0.1:8000`);

const child = spawn(
  process.execPath,
  [
    concurrentlyBin,
    "-n",
    "backend,frontend",
    "-c",
    "blue,green",
    "npm run dev:backend",
    "npm run dev:frontend",
  ],
  {
    cwd: repoRoot,
    env: { ...process.env, FIT_AGENT_FRONTEND_PORT: String(frontendPort) },
    stdio: "inherit",
  }
);

child.on("error", (error) => {
  throw error;
});
child.on("close", (code, signal) => {
  process.exit(signal ? 1 : (code ?? 1));
});
