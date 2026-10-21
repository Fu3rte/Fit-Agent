# rebuild-v2 候选相关性补标批准记录

## 用户授权

用户在本次对话审阅了《25 条候选的相关性评分草案》，回复：

> 我看了，都没有问题

批准对象为草案列出的 25 个查询与动作组合，以及对应 0–3 级相关性评分。草案列明的目录事实疑点及其 0 分建议一并获得确认。

按现有评测版本规则，将本次补标保存为 `backend/dataset/evaluation/rebuild-v2/`。继承 `rebuild-v1` 的既有批准；更新本次新增 qrels、三个关联查询的审阅范围与内容摘要，以及新版本 manifest 的发布摘要。

## 批准分级

| 查询 | 动作 ID | grade |
|---|---|---|
| q-lookup-001 | 0022 | 0 |
| q-lookup-001 | 0589 | 0 |
| q-lookup-001 | 0574 | 0 |
| q-lookup-001 | 0055 | 1 |
| q-lookup-001 | 0788 | 0 |
| q-generation-001 | 1297 | 2 |
| q-generation-001 | 1689 | 2 |
| q-generation-001 | 1167 | 1 |
| q-generation-001 | 2363 | 0 |
| q-generation-001 | 0500 | 0 |
| q-generation-001 | 1271 | 1 |
| q-replacement-001 | 0343 | 2 |
| q-replacement-001 | 0342 | 2 |
| q-replacement-001 | 1622 | 0 |
| q-replacement-001 | 0303 | 2 |
| q-replacement-001 | 3545 | 2 |
| q-replacement-001 | 1293 | 1 |
| q-replacement-001 | 0433 | 1 |
| q-replacement-001 | 1282 | 1 |
| q-replacement-001 | 1281 | 1 |
| q-replacement-001 | 1283 | 1 |
| q-replacement-001 | 1295 | 1 |
| q-replacement-001 | 0375 | 1 |
| q-replacement-001 | 1290 | 0 |
| q-replacement-001 | 1291 | 1 |

分布：0 分 8 条、1 分 11 条、2 分 6 条、3 分 0 条。每条事实依据与目录 JSON Pointer 保存于新版本 `qrels.jsonl`。

## 版本与修改范围

- `rebuild-v1` 的文件、审批和历史运行证据保持原值。
- 新版本 corpus 1321、queries 150、qrels 1104、scenarios 80、附件 8。
- 原有 1079 条 qrels 原样继承，仅追加本次批准的 25 条。
- 三个关联查询的检索文本、用途、有效条件、标签与场景引用保持原值。
- 三个查询更新已审阅范围说明及覆盖关联 qrels 的审批摘要。
- 其余 147 条查询、全部场景、corpus 与附件保持原有字节内容。
- 目录动作事实、Schema、索引、生产配置与模型参数均不修改。

## 评测与证据边界

批准本次补标不包含正式 150 条检索、80 个业务场景执行或任何云端上传授权。

已有 blocked 运行保持其原始数据版本、指标和阻断状态。新版本的评测需使用自身的数据身份，历史 evidence 不自动转为 complete。

本次 25 条补标覆盖运行 `20261020T140331Z-92009dc8` 中已发现的未知候选。其他查询或后续真实召回仍可能出现新的未审阅候选，继续适用现有门禁。

## 验证记录

subagent 实际验证结果：

- 两版均通过 `load_for_evaluation()`，`ready=true`；新版本所有审批摘要有效。
- `rebuild-v1` 全部 13 个文件的路径与 SHA-256 保持不变。
- 新版本前 1079 条 qrels 字节不变，追加恰 25 条，分级与本次批准一致。
- 三条关联查询仅变更审阅范围说明与审批；其余 147 条查询字节不变。
- 原验证运行两组完整候选池在新版本均无未审阅候选；旧版本保留 5/6/14 的缺标状态。
- 旧 artifact 的文件哈希、版本、blocked 状态及 `metrics=null` 均保持不变。
- 门禁回归 `13 passed in 12.13s`，退出码 0；`git diff --check` 退出码 0。

新版本身份：

- manifest SHA-256：`f2a1e3b57ef014612c3a18412d7191a3e5070921fdbac6df8d1bee0a97414fb3`
- content SHA-256：`336061b215ffd2aad0f9c567e9efcbe96fd531fa612d368de61a6bc003d6f449`
- corpus SHA-256：`2eb59eb570a796ace0a1778c2ad5f198d49cd60142205e31f00fdd28a3f3fcf4`

命令、退出码与可重跑 selfcheck 见 `tmp/rerank-qrels-amendment/verification.md`。本次未执行正式全量评测、真实模型调用或云端上传。
