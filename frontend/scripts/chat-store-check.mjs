import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { createServer } from "vite";

const server = await createServer({
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});
const { loadChatStore, readLedger } = await server.ssrLoadModule(
  "/src/features/chat/utils/reactAgent.ts",
);
await server.close();

const key = "fit-agent:chat-client";
const session = randomUUID();
const operations = ["send", "edit", "regenerate", "steering"].map((kind) => ({
  operation_id: randomUUID(),
  session_id: session,
  kind,
  run_id: randomUUID(),
  request: "  原请求文本  ",
  created_at: 123,
  request_entry_id: randomUUID(),
  steering_id: kind === "steering" ? randomUUID() : null,
  target_entry_id: ["edit", "regenerate"].includes(kind) ? randomUUID() : null,
}));
const stored = {
  selected_session_id: session,
  draft: { session_id: randomUUID(), pending_title: "草稿" },
  ledgers: { [session]: operations },
};
localStorage.setItem(key, JSON.stringify(stored));
const loaded = loadChatStore();
assert.equal(loaded.selected_session_id, session);
assert.deepEqual(loaded.draft, stored.draft);
for (let index = 0; index < operations.length; index++) {
  assert.deepEqual(loaded.ledgers[session][index], {
    ...operations[index],
    attachments: [],
    ...(operations[index].kind === "edit" ? { legacy_edit: true } : {}),
  });
}
assert.deepEqual(JSON.parse(localStorage.getItem(key)), loaded);
assert.deepEqual(loadChatStore(), loaded);
assert.deepEqual(readLedger(session), loaded.ledgers[session]);

const identity = randomUUID();
const attachment = {
  attachment_id: identity,
  file_name: "plan.md",
  size_bytes: 4,
  input: { kind: "upload", attachment_id: identity, file_name: "plan.md", data_base64: "cGxhbg==" },
};
const withFile = { ...loaded, ledgers: { [session]: [{ ...operations[0], request: "", attachments: [attachment] }] } };
localStorage.setItem(key, JSON.stringify(withFile));
assert.deepEqual(loadChatStore(), withFile);
assert.deepEqual(JSON.parse(localStorage.getItem(key)), withFile);

for (const attachments of [null, {}, "", [null]]) {
  const broken = JSON.stringify({ ...stored, ledgers: { [session]: [{ ...operations[0], attachments }] } });
  localStorage.setItem(key, broken);
  assert.throws(loadChatStore);
  assert.equal(localStorage.getItem(key), broken);
}
const emptyLegacy = JSON.stringify({ ...stored, ledgers: { [session]: [{ ...operations[0], request: " " }] } });
localStorage.setItem(key, emptyLegacy);
assert.throws(loadChatStore, /正文无效/);
assert.equal(localStorage.getItem(key), emptyLegacy);
localStorage.removeItem(key);
console.log("PASS: real localStorage legacy ledger migration; request identities/draft retained; edit omission retained; file bytes retained; corruption fails without overwriting");
