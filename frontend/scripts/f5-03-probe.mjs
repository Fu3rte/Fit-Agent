/**
 * F5-03 验收探针（走真实 mock REST + 源码断言）：/review 展示、stale 徽章与重新生成入口
 * （plans/stage5.md F5-03 / §3.4 / §7 第 5、8 步）。
 *
 * 运行：node scripts/f5-03-probe.mjs
 * 覆盖：
 *  - 源码：ReviewPage 含 stale 徽章、生成按钮、预填文案命中 REVIEW_GENERATE
 *  - 源码：无 input/select/textarea；无历史列表 UI；无 RIR；Markdown 白名单沿用
 *  - 源码：basis 快照摘要消费（BasisSummary / 依据快照）
 *  - 源码：App 路由未新增（仍仅 /review 一条）
 *  - 运行时：GET /api/review 返回最新条 + basis
 *  - 运行时：更正后 stale=true 且 text/generated_at 逐字不变
 *  - 运行时：预填文案经 POST /api/runs 发送 → 命中 F5-02 生成路径
 */
import path from "node:path";
import { fileURLToPath } from "node:url";
import { readFileSync } from "node:fs";
import { createServer as createViteServer } from "vite";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
let failed = 0;
let passed = 0;
const check = (name, ok, detail = "") => {
  if (!ok) failed += 1;
  else passed += 1;
  console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? ` — ${detail}` : ""}`);
};

const vite = await createViteServer({
  root,
  configFile: path.join(root, "vite.config.ts"),
  logLevel: "error",
  server: { host: "127.0.0.1", port: 0, strictPort: false },
  optimizeDeps: { noDiscovery: true },
});
await vite.listen();
const port = vite.httpServer.address().port;
const base = `http://127.0.0.1:${port}`;

const api = async (method, p, body) => {
  const res = await fetch(base + p, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  return { status: res.status, body: await res.json().catch(() => null) };
};

const reset = (seed) => api("POST", "/api/dev/reset", { seed });
const confirm = (id, revision) =>
  api("POST", `/api/drafts/${id}/confirm`, { revision });
const stats = async () => (await api("GET", "/api/stats")).body;
const review = async () => (await api("GET", "/api/review")).body;
const reviewEntries = async () =>
  (await api("GET", "/api/dev/status")).body?.review_entries;

async function run(session, message, prefix) {
  const started = await api("POST", "/api/runs", {
    session_id: session,
    message,
    client_request_id: `${prefix}-${Date.now()}-${Math.random()}`,
  });
  if (started.status !== 200)
    throw new Error(
      `发起 Run 失败：${started.status} ${JSON.stringify(started.body)}`,
    );
  const runId = started.body.run_id;
  for (let i = 0; i < 120; i += 1) {
    await new Promise((r) => setTimeout(r, 300));
    const active = (await api("GET", "/api/runs/active")).body?.run;
    if (!active || active.run_id !== runId) continue;
    if (["completed", "failed", "cancelled"].includes(active.status))
      return {
        text: active.saved_text,
        status: active.status,
        draft: active.drafts?.[active.drafts.length - 1] ?? null,
      };
  }
  throw new Error("Run 未在预期时间内进入终态");
}

/* ========== 1. 源码断言：ReviewPage 展示与入口 ========== */
console.log("\n--- 源码断言 ReviewPage ---");
{
  const src = readFileSync(
    path.join(root, "src/features/review/ReviewPage.tsx"),
    "utf8",
  );
  const server = readFileSync(path.join(root, "src/mock/server.ts"), "utf8");

  check(
    "1 stale 徽章文案「依据已变更」「可重新生成」+ stale 消费",
    /依据已变更/.test(src) &&
      /可重新生成/.test(src) &&
      /review\.data\?\.stale|stale/.test(src),
  );
  check(
    "1 生成按钮「在对话中生成复盘」+ navigate prefill",
    /在对话中生成复盘/.test(src) &&
      /prefill/.test(src) &&
      /navigate\(["']\/["']/.test(src),
  );

  /* 预填文案必须命中 F5-02 REVIEW_GENERATE（源码字符串断言） */
  const prefillMatch = src.match(/prefill:\s*"([^"]+)"/);
  const prefillText = prefillMatch?.[1] ?? "";
  const reviewGenMatch = server.match(
    /const REVIEW_GENERATE\s*=\s*(\/.+\/[a-z]*)/,
  );
  let genHits = false;
  if (prefillText && reviewGenMatch) {
    // 安全还原正则字面量（server 内为单行）
    try {
      // eslint-disable-next-line no-new-func
      const re = new Function(`return ${reviewGenMatch[1]}`)();
      genHits = re.test(prefillText);
    } catch {
      genHits = false;
    }
  }
  check(
    "1 预填文案命中 REVIEW_GENERATE（F5-02 生成路径）",
    Boolean(prefillText) && genHits,
    JSON.stringify({ prefill: prefillText }),
  );

  check(
    "1 只读：无 input/select/textarea",
    !/<input|<select|<textarea/i.test(src),
  );
  check(
    "1 无历史列表 UI（不 map 历史复盘 / 无 review_entries 展示）",
    !/review_entries/.test(src) &&
      !/\.map\([^)]*review/i.test(src) &&
      !/历史复盘|复盘历史/.test(src),
  );
  check("1 主文案无 RIR 缩写", !/\bRIR\b/.test(src));
  check(
    "1 Markdown 白名单沿用（allowedElements + unwrapDisallowed）",
    /allowedElements/.test(src) &&
      /unwrapDisallowed/.test(src) &&
      /remarkGfm/.test(src),
  );
  check(
    "1 basis 快照摘要消费（BasisSummary / 依据快照 / data_updated_at）",
    /BasisSummary/.test(src) &&
      /依据快照/.test(src) &&
      /basis\.data_updated_at|data_updated_at/.test(src) &&
      /review\.data\?\.basis/.test(src),
  );
  check(
    "1 无 basis 空态说明（不编造）",
    /暂无依据快照|不编造/.test(src),
  );
}

/* ========== 2. 源码断言：App 路由未新增 ========== */
console.log("\n--- 源码断言 App 路由 ---");
{
  const src = readFileSync(path.join(root, "src/app/App.tsx"), "utf8");
  const reviewRoutes = src.match(/path="\/review"/g) ?? [];
  const detailRoutes = src.match(/path="\/(?:records|review)\/[^"]+"/g) ?? [];
  check(
    "2 App 路由未新增：/review 恰一条，无详情子路由",
    reviewRoutes.length === 1 && detailRoutes.length === 0,
    JSON.stringify({ review: reviewRoutes.length, detail: detailRoutes }),
  );
}

/* ========== 3. 运行时：GET /api/review 最新条 + basis ========== */
console.log("\n--- 运行时 最新条+basis ---");
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });
{
  const s0 = await stats();
  const rev = await review();
  check(
    "3 GET /api/review 返回最新条：text/generated_at/stale",
    typeof rev.text === "string" &&
      rev.text.length > 0 &&
      typeof rev.generated_at === "string" &&
      typeof rev.stale === "boolean",
  );
  check(
    "3 最新条含 basis：per_week/三桶/PR/data_updated_at",
    rev.basis != null &&
      Array.isArray(rev.basis.per_week) &&
      typeof rev.basis.buckets?.met === "number" &&
      Array.isArray(rev.basis.prs) &&
      typeof rev.basis.data_updated_at === "string",
    JSON.stringify({
      pw: rev.basis?.per_week?.length,
      b: rev.basis?.buckets,
      prs: rev.basis?.prs?.length,
    }),
  );
  check(
    "3 basis 与 /api/stats 对齐（default 种子冻结）",
    JSON.stringify(rev.basis?.per_week) === JSON.stringify(s0.per_week) &&
      JSON.stringify(rev.basis?.buckets) === JSON.stringify(s0.buckets) &&
      rev.basis?.data_updated_at === s0.data_updated_at,
  );
  // 空种子：空态投影不编造 basis
  await reset("empty");
  const revEmpty = await review();
  check(
    "3 empty 种子：basis 缺省（空态不编造）",
    revEmpty.basis == null && revEmpty.stale === false,
    JSON.stringify({ hasBasis: revEmpty.basis != null }),
  );
}

/* ========== 4. 运行时：更正后 stale=true 且正文/时间不变 ========== */
console.log("\n--- 运行时 更正后 stale ---");
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });
{
  // 先显式生成一条新复盘（stale=false），再更正业务数据断言 stale 翻转
  await run("s-f5-03-stale", "生成复盘", "f5-03-gen0");
  const rev0 = await review();
  check("4 前置：显式生成后 stale=false", rev0.stale === false);

  const res = await run(
    "s-f5-03-stale",
    "更正 2026-09-07 的训练记录：杠铃平板卧推 第2组改成7次",
    "f5-03-stale",
  );
  const draft = res.draft;
  check(
    "4 更正草稿就绪",
    draft?.id != null && draft?.revision != null,
    JSON.stringify({ id: draft?.id, revision: draft?.revision }),
  );
  const ok = draft ? await confirm(draft.id, draft.revision) : { status: 0 };
  check("4 确认更正成功", ok.status === 200, JSON.stringify(ok.body));

  const rev1 = await review();
  const entries = await reviewEntries();
  check(
    "4 更正后 stale=true 且 text/generated_at 逐字不变",
    rev1.stale === true &&
      rev1.text === rev0.text &&
      rev1.generated_at === rev0.generated_at &&
      entries.length >= 2,
    JSON.stringify({
      stale: rev1.stale,
      textSame: rev1.text === rev0.text,
      genSame: rev1.generated_at === rev0.generated_at,
      n: entries.length,
    }),
  );
}

/* ========== 5. 运行时：预填文案经 /api/runs 生成成功（F5-02 路径） ========== */
console.log("\n--- 运行时 预填文案生成 ---");
await reset("default");
await api("PUT", "/api/provider/api-key", { api_key: "probe" });
{
  const src = readFileSync(
    path.join(root, "src/features/review/ReviewPage.tsx"),
    "utf8",
  );
  const prefillText = src.match(/prefill:\s*"([^"]+)"/)?.[1] ?? "";
  check("5 预填文案可提取", Boolean(prefillText), prefillText);

  const revBefore = await review();
  const entriesBefore = await reviewEntries();
  const res = await run("s-f5-03-prefill", prefillText, "f5-03-prefill");
  const rev = await review();
  const entries = await reviewEntries();

  check(
    "5 预填发送后命中生成路径：回复含「已保存复盘」",
    /已保存复盘/.test(res.text ?? ""),
    (res.text ?? "").slice(-100),
  );
  check(
    "5 生成成功：条数+1、generated_at 更新、stale=false",
    entries.length === entriesBefore.length + 1 &&
      rev.generated_at !== revBefore.generated_at &&
      rev.stale === false,
    JSON.stringify({
      before: entriesBefore.length,
      after: entries.length,
      stale: rev.stale,
    }),
  );
  check(
    "5 新条 basis 与现算统计对齐（F5-02 冻结数字）",
    rev.basis != null &&
      /2\/3/.test(rev.text) &&
      /80kg/.test(rev.text),
  );
}

await vite.close();
console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
