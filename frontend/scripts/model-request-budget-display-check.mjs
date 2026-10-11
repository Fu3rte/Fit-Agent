import assert from "node:assert/strict";
import { realpathSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const root = realpathSync.native(fileURLToPath(new URL("../", import.meta.url)));
const server = await createServer({
  configFile: false,
  root,
  appType: "custom",
  logLevel: "silent",
  resolve: { alias: { "@": join(root, "src") }, dedupe: ["react", "react-dom"] },
  server: { middlewareMode: true },
});
const require = createRequire(join(root, "package.json"));
const { createElement } = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const { default: ChatTranscript } = await server.ssrLoadModule(
  "/src/features/chat/components/ChatTranscript.tsx",
);
await server.close();

const outputLimit = "回答达到输出上限，内容未完成";
const entry = {
  id: "assistant",
  kind: "assistant",
  stop_reason: "length",
  content: [{ type: "text", content_index: 0, text: "已生成内容" }],
};
const unexpectedAction = () => {
  throw new Error("静态展示不应发起操作");
};
function render(status, error) {
  return renderToStaticMarkup(createElement(ChatTranscript, {
    sessionId: "session",
    rounds: [{ id: "run", status, error, entries: [entry] }],
    onRetry: unexpectedAction,
    onWithdraw: unexpectedAction,
    retrying: null,
    canEdit: false,
    onEdit: unexpectedAction,
    onRegenerate: unexpectedAction,
  }));
}

const truncated = render("failed", outputLimit);
assert.equal(truncated.split(outputLimit).length - 1, 1);
assert.ok(truncated.includes("已生成内容"));
const quotaMessage = "模型服务账户额度不足，无法继续执行。";
assert.ok(render("failed", quotaMessage).includes(quotaMessage));
assert.ok(render("cancelled", "执行已取消。").includes("执行已取消。"));
assert.ok(render("interrupted").includes("已中断"));
assert.ok(render("running").includes("执行中"));
console.log("PASS: 真实 ChatTranscript 截断提示去重，独立错误、取消、中断和运行状态保留");
