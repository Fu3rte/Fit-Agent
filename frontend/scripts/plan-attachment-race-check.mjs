// 运行：node scripts/plan-attachment-race-check.mjs
// 附件导入竞态回归：真实 File、真实 React hook 与浏览器环境，验证修复后的多文件导入与移除状态一致性。
// 覆盖：连续导入保持发起顺序、读取期间移除即刻生效、clear／reset 使旧任务失效、旧失败不污染新草稿、
//      并发合计超限整批拒绝、并发失败错误保留、超限文件读取前拒绝、恰好达上限受理、批次原子性、
//      发送受理按身份清空。真实发送／编辑受理链路见 scripts/plan-attachment-edit-probe.mjs。
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

// 探针页：挂载真实的 useAttachmentDrafts，并暴露可控的读取延迟（仅原生 read 上包一层并原样委托）。
const hooksPath = path.join(temp, "attachment-race-hooks.tsx");
writeFileSync(
  hooksPath,
  `import { StrictMode, createElement } from "react";
import { createRoot } from "react-dom/client";
import { useAttachmentDrafts } from "../src/features/chat/components/Attachments";

const w = window as any;

w.raceFile = (name: string, size = 0) =>
  new File([size === 0 ? "" : "a".repeat(size)], name);
w.raceBadFile = (name: string) =>
  new File([new Uint8Array([0xff, 0xfe, 0x41])], name);
w.raceRetained = (name: string, size: number) => {
  const attachment_id = crypto.randomUUID();
  return {
    attachment_id,
    file_name: name,
    size_bytes: size,
    input: { kind: "reference", attachment_id },
  };
};
w.raceDelays = (map: Record<string, number>) => {
  w.fileReadDelays = map;
};

const originalArrayBuffer = File.prototype.arrayBuffer;
w.raceReads = 0;
File.prototype.arrayBuffer = function (this: File) {
  w.raceReads += 1;
  const milliseconds = w.fileReadDelays?.[this.name] ?? 0;
  if (milliseconds <= 0) return originalArrayBuffer.call(this);
  return new Promise<ArrayBuffer>((resolve, reject) => {
    setTimeout(
      () => void originalArrayBuffer.call(this).then(resolve, reject),
      milliseconds,
    );
  });
};

function HookProbe() {
  const files = useAttachmentDrafts();
  w.attachmentRace = files;
  return createElement(
    "div",
    { id: "attachment-race" },
    files.drafts.map((item) => item.file_name).join(","),
  );
}

for (const mount of [{ id: "hook-root", render: HookProbe }]) {
  const host = document.createElement("div");
  host.id = mount.id;
  document.body.append(host);
  createRoot(host).render(
    createElement(StrictMode, null, createElement(mount.render)),
  );
}
`,
);

const profile = path.join(temp, "attachment-race-browser");
mkdirSync(profile, { recursive: true });
const executable = path.join(
  process.env.APPDATA,
  "npm/node_modules/agent-browser/bin/agent-browser-win32-x64.exe",
);
const session = "attachment-race";

async function browser(...args) {
  const outputPath = path.join(profile, "command.json");
  const errorPath = path.join(profile, "error.log");
  const output = openSync(outputPath, "w");
  const error = openSync(errorPath, "w");
  const child = spawn(
    executable,
    ["--session", session, "--profile", profile, "--json", ...args],
    { stdio: ["ignore", output, error], timeout: 45000 },
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
  for (let attempt = 0; attempt < 200; attempt++) {
    // 元素在重渲染瞬间可能短暂缺失，求值失败按未满足继续轮询
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

const names = () =>
  evaluate("window.attachmentRace.drafts.map((item) => item.file_name)");
const error = () => evaluate("window.attachmentRace.error");
const pending = () => evaluate("window.attachmentRace.pending");

async function load() {
  await browser("open", "http://localhost:5311");
  await evaluate(
    "(async () => { await import('/temp/attachment-race-hooks.tsx'); return true; })()",
  );
  await until("!!window.attachmentRace", "hook mounted");
}

const server = await createServer({
  root,
  logLevel: "error",
  server: { port: 5311, strictPort: true },
});
await server.listen();

try {
  await load();

  /* 1. 读取期间 pending 为真，完成合入后回落 */
  const reading = await evaluate(`(async () => {
    window.raceDelays({'slow.md': 800});
    window.attachmentRace.pick([window.raceFile('slow.md', 5)]);
    await new Promise((resolve) => setTimeout(resolve, 120));
    return window.attachmentRace.pending;
  })()`);
  assert.equal(reading, true, "读取期间 pending 为真");
  await until(
    "window.attachmentRace.pending === false && window.attachmentRace.drafts.length === 1",
    "pending false after merge",
  );
  assert.deepEqual(await names(), ["slow.md"]);

  /* 2. 连续导入按发起顺序合入，短读取不越过长读取 */
  await evaluate("window.attachmentRace.clear(); true");
  await until("window.attachmentRace.drafts.length === 0", "cleared");
  await evaluate(
    "window.raceDelays({'a.md': 300, 'b.md': 0}); window.attachmentRace.pick([window.raceFile('a.md', 5)]); window.attachmentRace.pick([window.raceFile('b.md', 5)]); true",
  );
  await until("window.attachmentRace.drafts.length === 2", "both merged");
  assert.deepEqual(await names(), ["a.md", "b.md"]);

  /* 3. 读取期间移除已有附件，读取完成后保持移除状态 */
  await evaluate("window.attachmentRace.clear(); true");
  await until("window.attachmentRace.drafts.length === 0", "cleared");
  await evaluate("window.attachmentRace.pick([window.raceFile('a.md', 5)]); true");
  await until("window.attachmentRace.drafts.length === 1", "a merged");
  await evaluate(
    "window.raceDelays({'b.md': 300}); window.attachmentRace.pick([window.raceFile('b.md', 5)]); window.attachmentRace.remove(0); true",
  );
  await until(
    "window.attachmentRace.drafts.length === 1 && window.attachmentRace.drafts[0].file_name === 'b.md'",
    "removal persisted",
  );
  assert.deepEqual(await names(), ["b.md"]);

  /* 4. 读取期间 clear，旧任务完成后草稿仍为空且不报错 */
  await evaluate(
    "window.attachmentRace.clear(); window.raceDelays({'a.md': 300}); window.attachmentRace.pick([window.raceFile('a.md', 5)]); window.attachmentRace.clear(); true",
  );
  await delay(500);
  assert.deepEqual(await names(), []);
  assert.equal(await pending(), false);

  /* 5. 读取期间 reset 到另一条消息的附件，旧任务完成后保持新集合 */
  await evaluate(
    "window.attachmentRace.clear(); window.raceDelays({'a.md': 300}); window.attachmentRace.pick([window.raceFile('a.md', 5)]); window.attachmentRace.reset([window.raceRetained('msg.md', 3)]); true",
  );
  await delay(500);
  assert.deepEqual(await names(), ["msg.md"]);

  /* 6. 旧任务失败不污染新草稿的错误状态（clear 使失败任务失效） */
  await evaluate(
    "window.attachmentRace.clear(); window.raceDelays({'bad.txt': 300}); window.attachmentRace.pick([window.raceBadFile('bad.txt')]); window.attachmentRace.clear(); true",
  );
  await delay(500);
  assert.deepEqual(await names(), []);
  assert.equal(await error(), null);
  // 未被失效的非法批次必须如实报错
  await evaluate(
    "window.attachmentRace.clear(); window.raceDelays({'bad.txt': 0}); window.attachmentRace.pick([window.raceBadFile('bad.txt')]); true",
  );
  await until("window.attachmentRace.error !== null", "invalid batch error surfaced");
  assert.match(await error(), /UTF-8/);
  await evaluate("window.attachmentRace.clear(); true");

  /* 7. 并发导入合计超限：拒绝超限批次，保留已接受附件 */
  await evaluate(
    "window.raceDelays({}); window.attachmentRace.pick([window.raceFile('big.md', 60000)]); true",
  );
  await until("window.attachmentRace.drafts.length === 1", "big merged");
  await evaluate(
    "window.raceDelays({'mid.md': 60}); window.attachmentRace.pick([window.raceFile('mid.md', 30000)]); window.attachmentRace.pick([window.raceFile('small.md', 20000)]); true",
  );
  await until("window.attachmentRace.pending === false", "over-limit batch settled");
  assert.deepEqual(await names(), ["big.md", "mid.md"]);
  assert.match(await error(), /超过 100000 字节/);

  /* 8. 原附件与新附件合计恰好达到上限时成功 */
  await evaluate("window.attachmentRace.clear(); window.raceDelays({}); true");
  await until("window.attachmentRace.drafts.length === 0", "cleared");
  await evaluate(
    "window.attachmentRace.pick([window.raceFile('big.md', 60000)]); true",
  );
  await until("window.attachmentRace.drafts.length === 1", "big merged");
  await evaluate(
    "window.attachmentRace.pick([window.raceFile('rest.md', 40000)]); true",
  );
  await until("window.attachmentRace.drafts.length === 2", "rest merged");
  assert.equal(await error(), null);
  assert.deepEqual(await names(), ["big.md", "rest.md"]);

  /* 9. 批次含非法文件时整批拒绝，此前的移除操作保持生效 */
  await evaluate("window.attachmentRace.clear(); true");
  await until("window.attachmentRace.drafts.length === 0", "cleared");
  await evaluate("window.attachmentRace.pick([window.raceFile('keep.md', 5)]); true");
  await until("window.attachmentRace.drafts.length === 1", "keep merged");
  await evaluate(
    "window.attachmentRace.remove(0); window.attachmentRace.pick([window.raceFile('g.txt', 3), new File(['x'], 'x.docx')]); true",
  );
  await until("window.attachmentRace.error !== null", "atomic batch rejected");
  assert.deepEqual(await names(), []);

  /* 10. 发送成功后按身份清空，提交期间新挂入的附件保留 */
  await evaluate("window.attachmentRace.clear(); window.raceDelays({}); true");
  await until("window.attachmentRace.drafts.length === 0", "cleared");
  await evaluate(
    "window.attachmentRace.pick([window.raceFile('sent.md', 5)]); true",
  );
  await until("window.attachmentRace.drafts.length === 1", "sent merged");
  await evaluate(
    "window.__submitted = window.attachmentRace.drafts; window.attachmentRace.pick([window.raceFile('late.md', 5)]); true",
  );
  await until("window.attachmentRace.drafts.length === 2", "late merged");
  await evaluate("window.attachmentRace.discard(window.__submitted); true");
  assert.deepEqual(await names(), ["late.md"]);

  /* 11. 并发导入：前序失败批次的错误不被后序成功批次吞掉，独立后续操作才清除 */
  await evaluate("window.attachmentRace.clear(); true");
  await until("window.attachmentRace.drafts.length === 0", "cleared");
  await evaluate(
    "window.raceDelays({'bad.txt': 200, 'good.txt': 0}); window.attachmentRace.pick([window.raceBadFile('bad.txt')]); window.attachmentRace.pick([window.raceFile('good.txt', 5)]); true",
  );
  await until(
    "window.attachmentRace.pending === false && window.attachmentRace.drafts.length === 1",
    "concurrent failure and success settled",
  );
  assert.deepEqual(await names(), ["good.txt"]);
  assert.match(await error(), /UTF-8/, "失败批次错误在并发成功后被保留");
  await evaluate(
    "window.raceDelays({}); window.attachmentRace.pick([window.raceFile('next.md', 5)]); true",
  );
  await until("window.attachmentRace.drafts.length === 2", "independent import merged");
  assert.equal(await error(), null, "独立后续操作清除上一轮错误");

  /* 12. 超限文件在读取前拒绝：不触发任何 arrayBuffer，也不改变草稿 */
  await evaluate("window.attachmentRace.clear(); window.raceDelays({}); true");
  await until("window.attachmentRace.drafts.length === 0", "cleared");
  await evaluate(
    "window.raceReads = 0; window.attachmentRace.pick([window.raceFile('huge.md', 2000000)]); true",
  );
  await until("window.attachmentRace.error !== null", "oversized rejected");
  assert.match(await error(), /超过 100000 字节/);
  assert.equal(await evaluate("window.raceReads"), 0, "超限批次未被读取字节");
  assert.deepEqual(await names(), []);

  console.log(
    "PASS: 附件导入竞态回归全部通过（发起顺序、移除保持、clear／reset 失效、旧失败不污染、并发超限拒绝、并发失败错误保留、超限不读取、恰好达上限、批次原子性、发送受理按身份清空）",
  );
} finally {
  await browser("close");
  await server.close();
}
