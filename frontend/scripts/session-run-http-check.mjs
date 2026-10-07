// 运行：node --experimental-webstorage --localstorage-file=temp/run-http-storage scripts/session-run-http-check.mjs <隔离后端地址>
// 使用真实 HTTP、SQLite 与查询缓存；延迟真实响应交付以检查异步交错，不调用模型。
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";
import { QueryClientProvider, QueryObserver } from "@tanstack/react-query";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { createServer } from "vite";

const origin = process.argv[2];
assert.ok(origin, "必须提供隔离后端地址");
localStorage.clear();
const server = await createServer({
  appType: "custom", logLevel: "silent",
  server: { host: "127.0.0.1", port: 0, proxy: {
    "/api": { target: origin, changeOrigin: false, headers: { host: "127.0.0.1:8000", origin: "http://localhost:5173" } },
  } },
});
await server.listen();
const frontendOrigin = `http://127.0.0.1:${server.httpServer.address().port}`;
const nativeFetch = globalThis.fetch;
let delayed = null;
const holdNext = (pathname, method = "GET") => {
  assert.equal(delayed, null);
  const gate = { pathname, method, received: Promise.withResolvers(), release: Promise.withResolvers(), delivered: Promise.withResolvers() };
  delayed = gate;
  return gate;
};
globalThis.fetch = async (input, init) => {
  const url = new URL(input, frontendOrigin);
  const gate = delayed?.pathname === url.pathname && delayed.method === (init?.method ?? "GET") ? delayed : null;
  if (gate !== null) delayed = null;
  const response = await nativeFetch(url, init);
  if (gate !== null) {
    gate.received.resolve();
    await gate.release.promise;
    gate.delivered.resolve();
  }
  return response;
};
const until = async (condition) => {
  for (let i = 0; i < 200; i += 1) {
    if (condition()) return;
    await delay(10);
  }
  assert.fail("异步状态未收敛");
};
const { sessionRuns } = await server.ssrLoadModule("/src/features/chat/utils/sessionRunManager.ts");
const { queryClient } = await server.ssrLoadModule("/src/lib/query.ts");
const { createSession, deleteSession, listSessions } = await server.ssrLoadModule("/src/lib/api.ts");
const { startNewSession, loadChatStore } = await server.ssrLoadModule("/src/features/chat/utils/reactAgent.ts");
const { default: SessionList } = await server.ssrLoadModule("/src/features/chat/components/SessionList.tsx");
const { TooltipProvider } = await server.ssrLoadModule("/src/components/ui/tooltip.tsx");
const { SidebarContent } = await server.ssrLoadModule("/src/components/ui/sidebar.tsx");

// 较旧成功响应不得覆盖后续查询失败。
const id = randomUUID();
await createSession({ session_id: id, title: "历史查询" });
const oldSuccess = holdNext(`/api/sessions/${id}/history`);
const record = sessionRuns.open(id, false);
record.mount(false);
await oldSuccess.received.promise;
await deleteSession(id);
record.loadHistory(new AbortController().signal, "refresh");
await until(() => record.snapshot().history === "failed");
oldSuccess.release.resolve();
await oldSuccess.delivered.promise;
await delay(30);
assert.equal(record.snapshot().history, "failed");
assert.equal(record.snapshot().ready, false);

// 较旧失败响应不得覆盖后续查询成功。
const nextId = randomUUID();
const oldFailure = holdNext(`/api/sessions/${nextId}/history`);
const next = sessionRuns.open(nextId, false);
next.mount(false);
await oldFailure.received.promise;
await createSession({ session_id: nextId, title: "后续历史" });
next.loadHistory(new AbortController().signal, "refresh");
await until(() => next.snapshot().history === "loaded");
oldFailure.release.resolve();
await oldFailure.delivered.promise;
await delay(30);
assert.equal(next.snapshot().history, "loaded");
assert.equal(next.snapshot().ready, true);
assert.equal(next.snapshot().error, undefined);

// 创建响应交付前解除订阅并新建草稿，缓存仍更新且新选择保持。
const oldList = holdNext("/api/sessions");
const observer = new QueryObserver(queryClient, { queryKey: ["sessions"], queryFn: ({ signal }) => listSessions(signal) });
const unsubscribeList = observer.subscribe(() => {});
await oldList.received.promise;
const draftId = startNewSession().draft.session_id;
const draft = sessionRuns.open(draftId, true);
draft.mount(true);
let notifications = 0;
const unsubscribe = draft.subscribe(() => { notifications += 1; });
const creation = holdNext("/api/sessions", "POST");
const fullTitle = "后台创建的完整会话标题".repeat(8);
const creating = draft.ensureSession(fullTitle);
await creation.received.promise;
unsubscribe();
const newDraft = startNewSession().draft;
creation.release.resolve();
assert.equal((await creating).state, "created");
await until(() => observer.getCurrentResult().data?.sessions.some((session) => session.session_id === draftId));
oldList.release.resolve();
await oldList.delivered.promise;
await delay(30);
assert.equal(notifications, 0);
assert.equal(draft.snapshot().persistent, true);
assert.equal(loadChatStore().draft.session_id, newDraft.session_id);
assert.equal(loadChatStore().selected_session_id, null);
assert.ok(observer.getCurrentResult().data.sessions.some((session) => session.session_id === draftId));

// 真实列表数据在各路由渲染为同一顺序，完整标题与 Tooltip 触发器保持。
const listed = observer.getCurrentResult().data.sessions;
assert.equal(listed.find((session) => session.session_id === draftId).title, fullTitle);
assert.ok(listed.length >= 2);
for (let index = 1; index < listed.length; index += 1) {
  const previous = listed[index - 1];
  const current = listed[index];
  assert.ok(previous.updated_at > current.updated_at || (previous.updated_at === current.updated_at && previous.session_id > current.session_id));
}
for (const route of [`/sessions/${nextId}`, "/profile", `/sessions/${draftId}`, "/"]) {
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient },
    createElement(MemoryRouter, { initialEntries: [route] },
      createElement(TooltipProvider, null, createElement(SessionList)),
    ),
  ));
  let previousIndex = -1;
  for (const session of listed) {
    const index = html.indexOf(`href="/sessions/${session.session_id}"`);
    assert.ok(index > previousIndex, `路由 ${route} 的顺序与共享列表不一致`);
    assert.ok(html.includes(session.title));
    previousIndex = index;
  }
  assert.ok(html.includes('data-state="closed"'), "会话链接带有 Tooltip 触发器");
  assert.ok(html.includes('class="peer/menu-button flex w-full'), "链接保留菜单样式");
  assert.ok(html.includes('class="min-w-0 flex-1 truncate"'), "标题使用单行 ellipsis");
  assert.ok(html.includes("aria-[current=page]:bg-sidebar-accent"), "选中样式绑定 aria-current");
  assert.equal(html.includes('aria-current="page"'), route.startsWith("/sessions/"));
}
const sidebar = renderToStaticMarkup(createElement(SidebarContent));
assert.ok(sidebar.includes("overflow-x-hidden overflow-y-auto"));
assert.equal(queryClient.getQueryCache().findAll({ queryKey: ["sessions"] }).length, 1);

unsubscribeList();
queryClient.clear();
sessionRuns.discard(id);
sessionRuns.discard(nextId);
sessionRuns.discard(draftId);
await server.close();
console.log("PASS: 真实历史响应交错、列表缓存、跨路由排序、菜单样式、标题 ellipsis 及 Tooltip 触发器");
