import assert from "node:assert/strict";
import net from "node:net";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { createServer, resolveConfig } from "vite";

if (process.argv.includes("--probe")) {
  const config = await resolveConfig({}, "serve");
  assert.equal(config.server.port, Number(process.env.FIT_AGENT_FRONTEND_PORT ?? "5173"));
  assert.equal(config.server.strictPort, true);
  assert.equal(config.server.proxy["/api"].target, "http://127.0.0.1:8000");
  const occupied = net.createServer();
  await new Promise((resolve) => occupied.listen(0, "127.0.0.1", resolve));
  const port = occupied.address().port;
  const vite = await createServer({
    logLevel: "silent",
    server: { host: "127.0.0.1", port },
  });
  try {
    await assert.rejects(vite.listen(), /already in use/);
    assert.equal(vite.httpServer.listening, false);
  } finally {
    await vite.close();
    await new Promise((resolve) => occupied.close(resolve));
  }
} else {
  for (const value of [undefined, "5176", "1", "65535", "0", "65536", "abc", "5176.0", "", " 5176"]) {
    const env = { ...process.env };
    delete env.FIT_AGENT_FRONTEND_PORT;
    if (value !== undefined) env.FIT_AGENT_FRONTEND_PORT = value;
    const result = spawnSync(process.execPath, [fileURLToPath(import.meta.url), "--probe"], {
      env,
      encoding: "utf8",
      timeout: 20_000,
    });
    assert.equal(result.error, undefined);
    if ([undefined, "5176", "1", "65535"].includes(value)) {
      assert.equal(result.status, 0, result.stderr);
    } else {
      assert.notEqual(result.status, 0);
      assert.match(result.stderr, /FIT_AGENT_FRONTEND_PORT/);
    }
  }
  console.log("PASS: Vite shared frontend port; default/custom/range; proxy; real occupied port fails without switching");
}
