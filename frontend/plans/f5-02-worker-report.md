# F5-02 Worker 报告：对话显式生成／重新生成复盘

- 日期：2026-09-13
- 任务：F5-02（仅此一项；未触碰 F5-03/04/07）
- 状态：**完成**（tsc 零错误；f5-02/f5-01/f4-05 探针全 PASS）

## 改了哪些文件

- `frontend/src/mock/server.ts`
  - 删除写死 `REVIEW_REPLY` 常量
  - 新增 `REVIEW_GENERATE` 显式意图正则、`REVIEW_EXPLAIN` 模糊解释口径、`REVIEW_EMPTY_REFUSE` 空数据拒绝
  - 新增 `renderReviewText(basis)`：从冻结 `ReviewBasis` 确定性渲染 Markdown（完成率/三桶/PR/`data_updated_at` 逐字来自 basis）
  - 新增 `reviewGenerateReply(state)`：冻结 basis → 空数据拒绝（不落库）→ `saveReviewEntry` → 成功回复附 `generated_at`/`data_updated_at`；失败注入命中则说明未落库
  - `runScript` 意图：`isReviewGenerate`（显式）优先于建档分支（空种子也能走拒绝路径）；`isReviewExplain`（模糊）只解释不落库；均不建草稿
- `frontend/scripts/f5-02-probe.mjs` — 新建（29 项断言）
- `frontend/plans/f5-02-worker-report.md` — 本报告

## 跑过的命令与结果

| 命令（frontend/ 目录） | 结果 |
| --- | --- |
| `node node_modules/typescript/bin/tsc -b --pretty false` | 零错误 |
| `node scripts/f5-02-probe.mjs` | 29 passed / 0 failed |
| `node scripts/f5-01-probe.mjs` | 27 passed / 0 failed |
| `node scripts/f4-05-probe.mjs` | 31 passed / 0 failed |

f5-02 探针覆盖：显式「生成复盘」→ 最新条含冻结数字（W1 2/3、W2 1/3、三桶 1/1/1、80kg×8）、`generated_at` 更新、旧条仍在 `dev/status review_entries`、cv 不变、`plan_version`/schedules 快照不变、不建草稿、正文与 `/api/stats` 及 basis 一致；「重新生成复盘」再追加条数+1、旧条保留；模糊「复盘是什么」「最近怎么样」不追加且最新条逐字不变；empty 显式生成可读拒绝且无 2/3、80kg 等编造数字、条目数不变；fail-next 后对话回复说明失败且不落库、重做成功；源码断言写死分支已替换、意图正则存在、无新业务端点。

## 意图规则

- **显式生成**（`REVIEW_GENERATE`，最简正则）：`(?:生成|重新生成|更新).{0,6}复盘|复盘.{0,4}(?:生成|更新)|给我看一下复盘|看一下复盘|看复盘|展示复盘|显示复盘` → 冻结 + 渲染 + `saveReviewEntry` append。
- **模糊**：命中「复盘」但不满足显式（如「复盘是什么」）→ 只回 `REVIEW_EXPLAIN`，不落库。
- 非复盘消息 → 走既有分支/`GENERIC_REPLY`，不生成。
- 分支顺序：显式生成优先于 `isOnboarding`（空种子 profile=null 时仍能给出可读拒绝）；打卡/器械范围优先级不变。

## 空数据与失败路径

- **空数据**：`per_week` 无 planned>0 且无 PR → 回复可读拒绝（`REVIEW_EMPTY_REFUSE`），**不调用** `saveReviewEntry`，不编造数字。
- **失败注入**：对话路径调用同一 `saveReviewEntry`，命中 `dev_review_save_failure` 时整份不落并回复说明；重做一次成功（一次性位自动解除）。

## 剩余缺口（留给后续）

- 剧本第 3–7、9 步（建议不自动建草稿、后续调整草稿、stale 徽章 UI、empty 浏览器切换、刷新恢复）归 F5-03/04/F5-07。
- 对话回复未做流式分段优化（沿用既有 `text.split` 流式；正文一次生成）。
- 「给我看一下复盘」按显式生成处理（对齐 GENERIC_REPLY 示例文案与 §3.1 入口 1 的「明确请求」）；若 owner 希望「看」只读最新条不追加，属口径微调，可在 F5-03 预填跳转时一并拍。

## 是否碰到待拍事项

无条件性待拍阻塞项。上述「看复盘」口径已在剩余缺口注明，不阻塞本任务验收。

## 剧本步对应

| 剧本步 | 本任务覆盖 |
| --- | --- |
| 1 统计只读 | 回归 f4-05 / f5-01 已过 |
| 2 显式生成 | f5-02 §1 全覆盖 |
| 8 失败注入 | f5-02 §5 全覆盖 |
