/**
 * F3-05 验收探针（走真实 mock REST + 源码断言）：记录对照展示与统计页消费
 * （plans/stage3.md §3.3/§3.4 / F3-05）。
 *
 * 运行：node scripts/f3-05-probe.mjs
 * 覆盖：
 *  - 种子 09-07：comparison（4→3 deload）+ record.judgement 非 null
 *  - 组级 judgement：met/unmet/pending 按次数区间（含端点）
 *  - 09-05 incomplete：judgement=null、无 comparison、不进三桶（buckets 1/1/1）
 *  - RecordsPage：原计划/当次安排；不展示用力余量（已拍隐藏）
 *  - ReviewPage：只读、消费 getStats、分母零「暂无」逻辑仍在
 */
import path from "node:path";
import { fileURLToPath } from "node:url";
import { readFileSync } from "node:fs";
import { createServer as createViteServer } from "vite";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
let failed = 0;
const check = (name, ok, detail = "") => {
  if (!ok) failed += 1;
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

/* ---------- 0. 源码断言 ---------- */
{
  const recordsSrc = readFileSync(
    path.join(root, "src/features/records/RecordsPage.tsx"),
    "utf8",
  );
  check(
    "RecordsPage 含「原计划」「当次安排」（三份事实文案）",
    /原计划/.test(recordsSrc) && /当次安排/.test(recordsSrc),
  );
  check(
    "RecordsPage 不展示用力余量/RIR（已拍隐藏且不落库）",
    !/用力余量/.test(recordsSrc) && !/RIR/.test(recordsSrc),
  );
  check(
    "RecordsPage 组级判定徽章用语（符合/未符合/待补全）",
    /符合/.test(recordsSrc) &&
      /未符合/.test(recordsSrc) &&
      /待补全/.test(recordsSrc),
  );
  check(
    "RecordsPage 辅助 Badge 保留（有辅助）",
    /有辅助/.test(recordsSrc),
  );

  const reviewSrc = readFileSync(
    path.join(root, "src/features/review/ReviewPage.tsx"),
    "utf8",
  );
  check(
    "ReviewPage 仍消费 getStats（现算结果）",
    /getStats/.test(reviewSrc) && /queryKey: \["stats"\]/.test(reviewSrc),
  );
  check(
    "ReviewPage 分母零「暂无」逻辑仍在（WeekRow rate null）",
    /rate === null/.test(reviewSrc) && /暂无/.test(reviewSrc),
  );
  check(
    "ReviewPage 保持只读（无 input/select/textarea）",
    !/<input|<select|<textarea/i.test(reviewSrc),
  );
  check(
    "RecordsPage 保持只读（无表单输入控件；更正经对话）",
    !/<input|<select|<textarea/i.test(recordsSrc),
  );
}

/* ---------- 1. REST：种子 09-07 对照与组级判定 ---------- */
await api("POST", "/api/dev/reset", { seed: "default" });

{
  const records = (await api("GET", "/api/records")).body.records;
  const rec = records.find((x) => x.date === "2026-09-07");
  check(
    "09-07 记录有 comparison：planned 4 → arranged 3（deload）",
    rec?.comparison?.planned_sets === 4 &&
      rec?.comparison?.arranged_sets === 3,
    JSON.stringify(rec?.comparison),
  );
  check(
    "09-07 record.judgement 非 null 且 1/1/1",
    rec?.judgement != null &&
      rec.judgement.met === 1 &&
      rec.judgement.unmet === 1 &&
      rec.judgement.pending === 1,
    JSON.stringify(rec?.judgement),
  );
  const working = rec?.sets?.filter((s) => s.set_type === "working") ?? [];
  check(
    "09-07 组级 judgement：met（7 次落在 6–8 含端点）",
    working[0]?.judgement === "met" && working[0]?.reps === 7,
    JSON.stringify(working[0]),
  );
  check(
    "09-07 组级 judgement：unmet（5 次低于下限）",
    working[1]?.judgement === "unmet" && working[1]?.reps === 5,
    JSON.stringify(working[1]),
  );
  check(
    "09-07 组级 judgement：pending（缺次数）",
    working[2]?.judgement === "pending" &&
      (working[2]?.reps == null || working[2]?.reps === undefined),
    JSON.stringify(working[2]),
  );

  // 无安排 incomplete（09-05）：不判定、不进三桶
  const inc = records.find((x) => x.date === "2026-09-05");
  check(
    "09-05 incomplete：judgement=null、无 comparison",
    (inc?.judgement ?? null) === null && inc?.comparison === undefined,
    JSON.stringify({ j: inc?.judgement, c: inc?.comparison }),
  );
  check(
    "09-05 incomplete 工作组不写 set.judgement",
    (inc?.sets ?? []).every((s) => s.judgement === undefined),
    JSON.stringify(inc?.sets),
  );

  // 无对照安排的 valid 记录（08-31）：单独展示实际表现
  const free = records.find((x) => x.date === "2026-08-31");
  check(
    "08-31 无对照安排：judgement=null、无 comparison、组级无 judgement",
    (free?.judgement ?? null) === null &&
      free?.comparison === undefined &&
      free.sets.every((s) => s.judgement === undefined),
    JSON.stringify({ j: free?.judgement, sets: free?.sets }),
  );

  const stats = (await api("GET", "/api/stats")).body;
  check(
    "incomplete 不进三桶：stats.buckets 仍 1/1/1（种子）",
    stats.buckets.met === 1 &&
      stats.buckets.unmet === 1 &&
      stats.buckets.pending === 1,
    JSON.stringify(stats.buckets),
  );
  check(
    "统计现算包含 Wn/PR/更新时间（ReviewPage 消费的字段）",
    Array.isArray(stats.per_week) &&
      stats.per_week.length > 0 &&
      Array.isArray(stats.prs) &&
      typeof stats.data_updated_at === "string",
    JSON.stringify({
      weeks: stats.per_week.map((w) => `${w.week} ${w.completed}/${w.planned}`),
      prs: stats.prs.length,
    }),
  );
}

await vite.close();
console.log(failed === 0 ? "\n全部通过" : `\n${failed} 项失败`);
process.exit(failed === 0 ? 0 : 1);
