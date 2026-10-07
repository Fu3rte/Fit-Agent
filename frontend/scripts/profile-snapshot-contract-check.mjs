// 运行：后端先执行 `uv run python -m test.check_profile_snapshot_store`，再执行 `node scripts/profile-snapshot-contract-check.mjs`
// 跨语言一致性：把后端 Pydantic 判定过的同一组 fixture 逐条交给前端校验器，要求接受/拒绝结论与归一化结果完全一致。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createServer } from "vite";

const server = await createServer({
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});
const business = await server.ssrLoadModule("/src/lib/business.ts");
await server.close();

const PARSERS = {
  prepare_arguments: business.parseProfileProposalArguments,
  prepare_result: business.parseProfileProposal,
  save_arguments: business.parseProfileSaveArguments,
  save_result: business.parseProfileSaveResult,
  status_arguments: business.parseProfileStatusArguments,
  status_result: business.parseProfileStatusResult,
};

const EVIDENCE = new URL(
  "../../backend/temp/profile-snapshot/profile-snapshot.json",
  import.meta.url,
);

let evidence;
try {
  evidence = JSON.parse(readFileSync(EVIDENCE, "utf8"));
} catch {
  throw new Error(
    "缺少后端判定结果，请先运行 uv run python -m test.check_profile_snapshot_store",
  );
}

assert.deepEqual(
  evidence.models.codes,
  [
    "invalid_business_payload",
    "profile_access_denied",
    "profile_confirmation_invalid",
    "profile_proposal_invalidated",
    "profile_proposal_not_found",
    "profile_update_processing",
    "profile_version_conflict",
    "session_not_found",
  ],
  "八项业务错误码集合不一致",
);

const counts = { accepted: 0, rejected: 0 };
for (const item of evidence.models.cases) {
  const parse = PARSERS[item.parser];
  assert.ok(parse, `前端缺少 ${item.parser} 校验器`);
  let accepted = true;
  let parsed;
  try {
    parsed = parse(structuredClone(item.payload));
  } catch {
    accepted = false;
  }
  assert.equal(
    accepted,
    item.accepted,
    `${item.parser} 判定不一致：${JSON.stringify(item.payload)}`,
  );
  if (item.accepted) {
    assert.deepEqual(parsed, item.payload, `${item.parser} 归一化结果丢失字段`);
    counts.accepted += 1;
  } else {
    counts.rejected += 1;
  }
}

console.log(
  `PASS: 前后端 schema 判定一致（接受 ${counts.accepted} 条，拒绝 ${counts.rejected} 条），八项错误码集合一致`,
);
