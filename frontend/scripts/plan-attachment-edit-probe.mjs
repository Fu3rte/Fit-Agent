// 运行：node scripts/plan-attachment-edit-probe.mjs
// 真实附件受理链路探针：以真实后端与真实 SessionRunService 受理一次发送与一次编辑，断言输入门禁、
// 发送受理后按身份清空、取消使旧编辑实例的受理回调失效。受理与消息替换由真实结果完成，不使用人工
// Promise、伪造 File 或 Mock。需要本机 127.0.0.1:8000 已有已配置模型凭据的后端在运行（会发起真实
// 模型调用）。
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import {
  closeSync,
  mkdirSync,
  openSync,
  readFileSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { createServer } from "vite";

const root = path.resolve(import.meta.dirname, "..");
const temp = path.join(root, "temp");
mkdirSync(temp, { recursive: true });
const backendOrigin = "http://127.0.0.1:8000";
const frontendOrigin = "http://localhost:5173";
const port = 5317;
const backendHeaders = { Host: "127.0.0.1:8000", Origin: frontendOrigin };

// 探针页：挂载真实 ChatComposer 与 ChatTranscript，onSend/onEdit 接真实 SessionRunRecord；
// 读取与编辑响应仅在原生实现上包一层并原样委托，用于制造可控的在途窗口。
const probePath = path.join(temp, "attachment-edit-probe.tsx");
writeFileSync(
  probePath,
  `import { createElement, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "@/components/ui/tooltip";
import ChatComposer from "../src/features/chat/components/ChatComposer";
import ChatTranscript from "../src/features/chat/components/ChatTranscript";
import { queryClient, PROVIDER_QUERY_KEY } from "../src/lib/query";
import { getProvider } from "../src/lib/api";
import { sessionRuns } from "../src/features/chat/utils/sessionRunManager";

const w = window as any;
const nativeFetch = window.fetch.bind(window);
w.__held = null;
w.releaseEdit = () => {
  const release = w.__held;
  w.__held = null;
  if (release) release();
};
window.fetch = async (input: any, init?: any) => {
  const url = typeof input === "string" ? input : input.url;
  const response = await nativeFetch(input, init);
  if (String(url).indexOf("/api/agent/edit") !== -1) {
    await new Promise<void>((resolve) => {
      w.__held = resolve;
    });
  }
  return response;
};

const nativeArrayBuffer = File.prototype.arrayBuffer;
File.prototype.arrayBuffer = function (this: File, ...args: any[]) {
  const milliseconds = w.__readDelay;
  if (!milliseconds) return nativeArrayBuffer.apply(this, args as []);
  return new Promise<ArrayBuffer>((resolve, reject) => {
    setTimeout(
      () => void nativeArrayBuffer.apply(this, args as []).then(resolve, reject),
      milliseconds,
    );
  });
};

function App() {
  const [ready, setReady] = useState(false);
  useEffect(() => {
    let active = true;
    void (async () => {
      const { sendSessionId, editSessionId, entryId, request } = w.__probe;
      const sendRecord = sessionRuns.open(sendSessionId, false);
      const editRecord = sessionRuns.open(editSessionId, false);
      sendRecord.mount(false);
      editRecord.mount(false);
      const status = await getProvider();
      queryClient.setQueryData(PROVIDER_QUERY_KEY, status);
      if (!active) return;
      w.__sendRecord = sendRecord;
      w.__editRecord = editRecord;
      w.__probeEntry = {
        kind: "user",
        id: entryId,
        entry_id: entryId,
        request,
        attachments: [],
      };
      setReady(true);
    })();
    return () => {
      active = false;
    };
  }, []);
  if (!ready) return null;
  return createElement(
    "div",
    null,
    createElement(
      "div",
      { id: "composer-root" },
      createElement(ChatComposer, {
        busy: false,
        ready: true,
        unknownKeys: [],
        centered: false,
        onSend: w.__sendRecord.send,
        onStop: () => {},
      }),
    ),
    createElement(
      "div",
      { id: "transcript-root" },
      createElement(ChatTranscript, {
        sessionId: w.__probe.editSessionId,
        rounds: [{ id: "probe", status: "completed", entries: [w.__probeEntry] }],
        onRetry: () => {},
        onWithdraw: () => {},
        canEdit: true,
        onEdit: w.__editRecord.editMessage,
        onRegenerate: () => {},
      }),
    ),
  );
}

const host = document.createElement("div");
host.id = "probe-host";
document.body.append(host);
createRoot(host).render(
  createElement(
    TooltipProvider,
    null,
    createElement(
      QueryClientProvider,
      { client: queryClient },
      createElement(App),
    ),
  ),
);
`,
);

const profile = path.join(temp, "attachment-edit-browser");
mkdirSync(profile, { recursive: true });
const executable = path.join(
  process.env.APPDATA,
  "npm/node_modules/agent-browser/bin/agent-browser-win32-x64.exe",
);
const browserSession = "attachment-edit";

async function browser(...args) {
  const outputPath = path.join(profile, "command.json");
  const errorPath = path.join(profile, "error.log");
  const output = openSync(outputPath, "w");
  const error = openSync(errorPath, "w");
  const child = spawn(
    executable,
    ["--session", browserSession, "--profile", profile, "--json", ...args],
    { stdio: ["ignore", output, error], timeout: 120000 },
  );
  const status = await new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("exit", resolve);
  });
  closeSync(output);
  closeSync(error);
  const content = readFileSync(outputPath, "utf8");
  assert.equal(status, 0, readFileSync(errorPath, "utf8") || content);
  const reply = JSON.parse(content);
  assert.equal(reply.success, true, content);
  return reply.data;
}

const evaluate = async (source) => (await browser("eval", source)).result;
async function until(source, label) {
  for (let attempt = 0; attempt < 400; attempt += 1) {
    let satisfied = false;
    try {
      satisfied = Boolean((await browser("eval", source)).result);
    } catch {
      satisfied = false;
    }
    if (satisfied) return;
    await delay(50);
  }
  assert.fail(`Timed out: ${label ?? source}`);
}

// 后端全局同一时刻只接受一个运行；真实运行收尾前提交下一次运行会被拒为 run_busy。
async function waitRunsSettled(sessionId, label) {
  for (let attempt = 0; attempt < 600; attempt += 1) {
    const runs = (await api.listSessionRuns(sessionId)).runs;
    if (runs.every((run) => run.status !== "running")) return;
    await delay(200);
  }
  assert.fail(`运行未收敛：${label ?? sessionId}`);
}

// Node 侧以真实前端 api 客户端驱动真实后端；相对路径指向后端并补上边界头。
const nativeFetch = globalThis.fetch;
globalThis.fetch = (input, init = {}) =>
  nativeFetch(
    typeof input === "string" && input.startsWith("/")
      ? backendOrigin + input
      : input,
    {
      ...init,
      headers: { ...(init.headers ?? {}), ...backendHeaders },
    },
  );

const vite = await createServer({
  root,
  logLevel: "error",
  server: {
    port,
    strictPort: true,
    proxy: {
      "/api": {
        target: backendOrigin,
        changeOrigin: true,
        headers: { origin: frontendOrigin },
      },
    },
  },
});
await vite.listen();
const api = await vite.ssrLoadModule("/src/lib/api.ts");

let sendSessionId = null;
let editSessionId = null;
try {
  const ready = await nativeFetch(`${backendOrigin}/api/provider`, {
    headers: backendHeaders,
  }).catch(() => null);
  assert.ok(
    ready !== null && ready.status === 200,
    "需要本机 127.0.0.1:8000 已有已配置凭据的后端在运行",
  );

  // 1. 发送目标：空会话；编辑目标：真实运行产出的用户节点
  sendSessionId = crypto.randomUUID();
  editSessionId = crypto.randomUUID();
  await api.createSession({ session_id: sendSessionId, title: "发送受理探针" });
  await api.createSession({ session_id: editSessionId, title: "编辑受理探针" });
  await api.runReActStream(
    {
      session_id: editSessionId,
      operation_id: crypto.randomUUID(),
      request: "只回复 EDIT_PROBE OK，不要调用工具。",
    },
    () => {},
    new AbortController().signal,
    () => {},
  );
  const history = await api.readSessionHistory(editSessionId);
  const userEntry = history.entries.find(
    (entry) => entry.type === "message" && entry.message.role === "user",
  );
  assert.ok(userEntry, "真实运行产出用户节点");
  await waitRunsSettled(editSessionId, "编辑目标种子运行");

  await browser("open", `http://localhost:${port}`);
  await evaluate(
    `window.__probe = ${JSON.stringify({
      sendSessionId,
      editSessionId,
      entryId: userEntry.entry_id,
      request: userEntry.message.text,
    })}; true`,
  );
  await evaluate(
    "(async () => { await import('/temp/attachment-edit-probe.tsx'); return true; })()",
  );
  await until(
    "!!document.querySelector('#composer-root textarea[aria-label=\\\"消息\\\"]')",
    "probe mounted",
  );

  // 2. 发送：读取在途时发送禁用（真实读取窗口）
  await evaluate(
    `(() => {
      const element = document.querySelector('#composer-root textarea[aria-label=\\\"消息\\\"]');
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
      setter.call(element, "发送编辑探针 EDIT_PROBE_SEND");
      element.dispatchEvent(new Event("input", { bubbles: true }));
      return true;
    })()`,
  );
  await until(
    "document.querySelector('#composer-root textarea[aria-label=\\\"消息\\\"]').value === '发送编辑探针 EDIT_PROBE_SEND'",
    "composer text set",
  );
  await evaluate(
    `(() => {
      window.__readDelay = 500;
      const input = document.querySelector('#composer-root input[type=file]');
      const transfer = new DataTransfer();
      transfer.items.add(new File(["# 发送附件"], "send.md"));
      input.files = transfer.files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    })()`,
  );
  await until(
    "document.querySelector('#composer-root button[aria-label=\\\"发送\\\"]')?.disabled === true",
    "send disabled while reading",
  );
  await evaluate("window.__readDelay = 0; true");
  await until(
    "!!document.querySelector('#composer-root span.truncate')",
    "send attachment merged",
  );
  await until(
    "document.querySelector('#composer-root button[aria-label=\\\"发送\\\"]')?.disabled === false",
    "send enabled after merge",
  );

  // 3. 发送受理后按身份清空：提交的真实附件被移除，正文清空
  await evaluate(
    "document.querySelector('#composer-root button[aria-label=\\\"发送\\\"]').click(); true",
  );
  await until(
    "!document.querySelector('#composer-root span.truncate')",
    "submitted attachment discarded after real acceptance",
  );
  await until(
    "document.querySelector('#composer-root textarea[aria-label=\\\"消息\\\"]').value === ''",
    "text cleared after real acceptance",
  );
  await waitRunsSettled(sendSessionId, "发送受理运行");

  // 4. 编辑：打开并挂入真实文件附件，提交后真实响应交付被挂起，断言在途门禁
  await evaluate(
    "document.querySelector('#transcript-root button[aria-label=\\\"编辑消息\\\"]').click(); true",
  );
  await until(
    "!!document.querySelector('#transcript-root textarea[aria-label=\\\"编辑消息\\\"]')",
    "edit open",
  );
  await evaluate(
    `(() => {
      const input = document.querySelector('#transcript-root input[type=file]');
      const transfer = new DataTransfer();
      transfer.items.add(new File(["# 编辑附件"], "edit.md"));
      input.files = transfer.files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    })()`,
  );
  await until(
    "!!document.querySelector('#transcript-root button[aria-label^=\\\"移除 \\\"]')",
    "edit attachment chip present",
  );
  await evaluate(
    `(() => {
      const element = document.querySelector('#transcript-root textarea[aria-label=\\\"编辑消息\\\"]');
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
      setter.call(element, "编辑后的内容 EDIT_PROBE_EDIT");
      element.dispatchEvent(new Event("input", { bubbles: true }));
      return true;
    })()`,
  );
  await until(
    "document.querySelector('#transcript-root textarea[aria-label=\\\"编辑消息\\\"]').value === '编辑后的内容 EDIT_PROBE_EDIT'",
    "edit text set",
  );
  await evaluate(
    "[...document.querySelectorAll('#transcript-root button')].find((node) => node.textContent.trim() === '提交编辑').click(); true",
  );
  await until("typeof window.__held === 'function'", "real edit response held");
  assert.equal(
    await evaluate(
      "document.querySelector('#transcript-root textarea[aria-label=\\\"编辑消息\\\"]').disabled",
    ),
    true,
    "编辑在途期间文本输入禁用",
  );
  assert.equal(
    await evaluate(
      "document.querySelector('#transcript-root button[aria-label=\\\"导入训练计划文件\\\"]').disabled",
    ),
    true,
    "编辑在途期间导入入口禁用",
  );
  assert.equal(
    await evaluate(
      "document.querySelector('#transcript-root button[aria-label^=\\\"移除 \\\"]').disabled",
    ),
    true,
    "编辑在途期间移除入口禁用",
  );

  // 5. 在途取消并重新打开同一消息，随后交付真实受理结果
  await evaluate(
    "[...document.querySelectorAll('#transcript-root button')].find((node) => node.textContent.trim() === '取消').click(); true",
  );
  await until(
    "!document.querySelector('#transcript-root textarea[aria-label=\\\"编辑消息\\\"]')",
    "edit cancelled",
  );
  await evaluate(
    "document.querySelector('#transcript-root button[aria-label=\\\"编辑消息\\\"]').click(); true",
  );
  await until(
    "!!document.querySelector('#transcript-root textarea[aria-label=\\\"编辑消息\\\"]')",
    "edit reopened",
  );
  await evaluate("window.releaseEdit(); true");
  try {
    await until(
      "window.__editRecord.snapshot().rounds.some((round) => round.entries.some((entry) => entry.request === '编辑后的内容 EDIT_PROBE_EDIT'))",
      "real acceptance replaced the message",
    );
  } catch (failure) {
    console.log(
      "DIAG:",
      await evaluate(
        "JSON.stringify({ error: window.__editRecord.snapshot().error, busy: window.__editRecord.snapshot().busy, ready: window.__editRecord.snapshot().ready, rounds: window.__editRecord.snapshot().rounds.map((r) => ({ id: r.id, status: r.status, entries: r.entries.map((e) => e.request) })) })",
      ),
    );
    throw failure;
  }
  await delay(300);
  assert.equal(
    await evaluate(
      "!!document.querySelector('#transcript-root textarea[aria-label=\\\"编辑消息\\\"]')",
    ),
    true,
    "取消后重新打开时，真实受理的迟到回调不关闭编辑框",
  );

  console.log(
    "PASS: 真实受理链路下的发送读取门禁、发送受理按身份清空、编辑在途门禁、取消使旧编辑实例受理回调失效与真实消息替换",
  );
} finally {
  await browser("close");
  if (sendSessionId !== null)
    await api.deleteSession(sendSessionId).catch(() => {});
  if (editSessionId !== null)
    await api.deleteSession(editSessionId).catch(() => {});
  await vite.close();
}
