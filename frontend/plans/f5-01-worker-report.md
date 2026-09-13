# F5-01 Worker 报告：契约与 mock 复盘模型收口

- 日期：2026-09-13
- 任务：F5-01（仅此一项；未触碰 F5-02/03/04）
- 状态：**完成**（tsc 零错误；f5-01/f4-01/f4-05 探针全 PASS）

## 改了哪些文件

- `frontend/src/lib/contract.ts` — 新增 `ReviewBasis`、`ReviewEntry`；`ReviewDoc` 增加可选 `basis`；REST 注释标注 `/api/review` 为最新条投影
- `frontend/src/mock/server.ts` — `state.review` → append-only `review_entries: ReviewEntry[]`；新增 `freezeReviewBasis` / `saveReviewEntry` / `projectReviewDoc`；stale 置位改写到最新条；种子 basis 回填；dev 桥 `/api/dev/review/save` + `/api/dev/review/fail-next`（一次性注入 `dev_review_save_failure`）；`/api/dev/status` 增加 `review_entries` 只读摘要
- `frontend/scripts/f5-01-probe.mjs` — 新建（27 项断言）
- `frontend/plans/f5-01-worker-report.md` — 本报告

## 跑过的命令与结果

| 命令（frontend/ 目录） | 结果 |
| --- | --- |
| `node node_modules/typescript/bin/tsc -b --pretty false` | 零错误（注：`npx tsc` 会命中 npm 占位包，须用本地 bin） |
| `node scripts/f5-01-probe.mjs` | 27 passed / 0 failed |
| `node scripts/f4-01-probe.mjs` | 30 passed / 0 failed |
| `node scripts/f4-05-probe.mjs` | 31 passed / 0 failed |

f5-01 探针覆盖：default 种子统计基线不回归（W1 2/3、W2 1/3、三桶 1/1/1、PR 卧推 80×8）；GET /api/review 最新条投影（text/stale/generated_at + basis）；种子旧稿 stale=true 且 basis 与当时 stats 一致；内部保存路径追加（最新条可读回、旧条仍在存储、cv 不推进、重生成=再追加）；冻结快照与保存前后 stats 逐项一致 + source_revision_ids 非空；保存失败注入整份不落且一次性解除；业务变更（作废 09-07）后最新条 stale=true 而正文/generated_at 逐字不变；empty 空态（stale=false、无 basis、不编造完成率）；源码断言（contract 唯一形状来源、无新业务端点、保存/注入仅走 /api/dev/*）。

## 形状决定

- `ReviewBasis`：`per_week` / `buckets` / `prs` / `data_updated_at` / `source_revision_ids`（生成时刻当前训练修订 id + 当次安排修订 id）——语义对齐 06 6.4 / S4-08 review_basis。
- `ReviewEntry`（内部存储，append-only）：`id` / `text` / `generated_at` / `stale` / `basis`。
- `ReviewDoc`（GET /api/review 最新条投影）：`text` / `stale` / `generated_at` 必选（ReviewPage 消费字段不变）+ `basis?` 可选；无条目时空态 = 说明文案 + `stale:false` + `generated_at`（无 basis）。
- stale 口径：沿用 4385 逻辑——`recomputeStats`（业务数据变更）时把**最新条**置 `stale=true`，正文与 `generated_at` 不改写。
- 保存失败注入：`dev_review_save_failure` 一次性位，命中即整份不落并自动解除；保存函数不推进 `context_version`。
- 新增端点仅 dev-only：`POST /api/dev/review/save`（探针走内部保存路径的桥，F5-02 对话生成入口将调用同一 `saveReviewEntry`）、`POST /api/dev/review/fail-next`；契约 REST 清单未扩（仍仅 GET /api/review）。

## 剩余缺口（留给 F5-02）

- 对话显式/重新生成意图识别与确定性 Markdown 正文渲染（数字只能来自 `basis`）——本任务只收口存储/冻结/投影。
- dev 保存桥 `/api/dev/review/save` 将由 F5-02 正式对话路径取代消费方（桥保留供探针）。
- 空数据下生成路径的「拒绝或空态说明稿」策略归 F5-02（存储口已可追加）。

## 是否碰到待拍事项

无。未新增业务端点/依赖，未与已拍语义冲突。
