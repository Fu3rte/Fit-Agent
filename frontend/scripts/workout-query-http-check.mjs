// 运行：node scripts/workout-query-http-check.mjs <后端地址>
// 真实 HTTP 查询验收（workout-http-sse-contract §2、§3、§7）：只执行 GET，
// 覆盖查询参数序列化、列表与单条记录的严格校验、404 workout_not_found 与 422 非法参数。
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { createServer } from "vite";

const origin = process.argv[2];
assert.ok(origin, "必须提供后端地址");

const server = await createServer({
  appType: "custom",
  logLevel: "silent",
  server: {
    host: "127.0.0.1",
    port: 0,
    proxy: {
      "/api": {
        target: origin,
        changeOrigin: false,
        headers: { host: "127.0.0.1:8000", origin: "http://localhost:5173" },
      },
    },
  },
});
await server.listen();
const frontendOrigin = `http://127.0.0.1:${server.httpServer.address().port}`;
const nativeFetch = globalThis.fetch;
globalThis.fetch = (input, init) =>
  nativeFetch(new URL(input, frontendOrigin), init);

const api = await server.ssrLoadModule("/src/lib/api.ts");
const { ReActHttpError } = await server.ssrLoadModule(
  "/src/features/chat/utils/reactAgent.ts",
);

/** 只读查询：默认分页与显式分页均由后端回显实际生效参数 */
const defaults = await api.listWorkouts();
assert.deepEqual(Object.keys(defaults).sort(), ["items", "page", "page_size", "total"]);
assert.equal(defaults.page, 1, "默认第一页");
assert.equal(defaults.page_size, 10, "默认每页 10 条");
assert.ok(Array.isArray(defaults.items), "items 为数组");
for (const record of defaults.items)
  assert.ok(record.content.exercises.length >= 1, "每项为含至少一个动作的完整记录");

const paged = await api.listWorkouts({ page: 1, page_size: 20 });
assert.equal(paged.page_size, 20, "page_size 随查询参数生效");

/** 同一日期作起止即查询当天：每个日期最多一条记录 */
const today = new Date().toISOString().slice(0, 10);
const singleDay = await api.listWorkouts({ date_from: today, date_to: today });
assert.ok(singleDay.items.length <= 1, "同一天最多一条记录");
assert.ok(
  singleDay.items.every((item) => item.performed_on === today),
  "日期范围只返回范围内的记录",
);
assert.ok(
  singleDay.items.every(
    (item) => item.created_at > 0 && item.updated_at > 0 && item.version >= 1,
  ),
  "记录身份、版本与时间戳通过严格校验",
);

/** 排序由后端提供：performed_on 降序、id 降序 */
const ordered = [...paged.items].map((item) => item.performed_on);
assert.deepEqual(ordered, [...ordered].sort().reverse(), "按 performed_on 降序排列");

/** 超出总页数返回空数组 */
const beyond = await api.listWorkouts({ page: defaults.total + 1000 });
assert.deepEqual(beyond.items, [], "超出总页数为空结果");

/** 按 ID 查询不存在时 404 workout_not_found */
await assert.rejects(
  () => api.getWorkout(randomUUID()),
  (error) =>
    error instanceof ReActHttpError &&
    error.http_status === 404 &&
    error.code === "workout_not_found",
  "不存在的记录返回 404 workout_not_found",
);

/** 非法日期与分页参数在 422 拒绝，前端不转换为空结果 */
for (const query of [
  { page_size: 101 },
  { page: 0 },
  { date_from: "2026-02-30" },
  { date_from: "2026-06-10", date_to: "2026-06-01" },
  { date_from: "2026/06/01" },
])
  await assert.rejects(
    () => api.listWorkouts(query),
    (error) => error instanceof ReActHttpError && error.http_status === 422,
    `非法参数按 422 拒绝：${JSON.stringify(query)}`,
  );

await server.close();
console.log("PASS: 训练记录查询参数序列化、严格校验、404 与 422 错误码全部符合协议");
