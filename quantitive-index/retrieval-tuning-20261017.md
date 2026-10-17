# 动作检索调参报告：alpha 0 / 0.1 / 0.25，k=5

## 1. 实验身份

| 项 | 值 |
|---|---|
| git HEAD | `6f12eb9a7c81881ba31fba0bc8d1c11e3f8edb11` |
| 分支 | `test/v2-full-evaluation` |
| 日期 | 2026-10-17 |
| 依赖版本 | `pymilvus 3.0.2`、`openai 3.13.0` |
| grid 运行（主实验） | `20261017T032514Z-c44be847` |
| grid 运行（alpha=0 补充） | `20261017T040232Z-c44be847` |
| freeze 运行 | `20261017T032601Z-604ab661` |
| holdout 运行 | `20261017T032643Z-10e40f95` |
| selfcheck 运行 | `20261017T032718Z-b585e168` |
| 索引 | `fit_agent_actions_build_df2e2f6042934c79b626c29fe8562bb3`；manifest `21a147252af0b02c9599132888dda269d535749694e6aace2cf6a438181eb3cb` |
| Embedding | `qwen3.7-text-embedding-flash`，1024 维；缓存身份 `381c33aa4a7a9c6bea4b`；128 条查询 |
| 新增 Embedding 请求 | 0 |
| 冻结接线 | `ALPHA=0.1`、`BM25_DEPTH=100`、`DENSE_DEPTH=100`、`THRESHOLD=0.0`；`frozen_config_identity.sha256=bb51d6b7f536b9eab0bf29fc80b9d5734b99b27e2d9e6311c8ef1241c745ba67` |

## 2. 协议与口径

- dev：59 条查询（58 条有标注、1 条 `expect_empty`），`conditions=adapted`，`threshold=0.0`；查询向量全部来自缓存。
- 主实验 grid：`alpha ∈ {0.1, 0.25}` × `depth ∈ {20, 50, 100}`，每个单元 20 个门槛候选；共 6 个单元、354 次 hybrid RPC。
- 补充 grid：`alpha ∈ {0.0, 0.1}` × `depth ∈ {20, 50, 100}`，只跑 dev，不执行 freeze 或 holdout；共 6 个单元、354 次 hybrid RPC。
- 选参：阶段一按 `Recall@5 desc → nDCG@5 desc → MRR@5 desc → false_return_rate asc → depth asc → alpha asc`；阶段二在不丢弃任何已标注相关候选的门槛中取 `false_return_rate` 最低者，并列取最小门槛。
- holdout：66 条查询，冻结参数 `alpha=0.1`、`bm25_depth=100`、`dense_depth=100`、`threshold=0.0`；四组同轮执行。
- 指标：`grade > 0` 视为相关；`Recall@k` 分母为该查询的相关集合；`MRR@k` 取首个相关项名次的倒数；`nDCG@k` 以 grade 为增益并按理想排序归一；`Hit@k` 为 top-k 内存在任一相关项。宏平均分母为有标注查询；`expect_empty` 单独统计 `false_return_rate`。
- `k` 由 `--k` 参数控制，默认 `5,10`；同一次运行原生输出 @5 与 @10，不再通过 top10 截断推导。
- gate：硬约束违反数为 0，预算不合规为空。

## 3. dev 结果

### 3.1 k=5

| alpha | depth | Recall@5 | MRR@5 | Hit@5 | nDCG@5 |
|---|---|---|---|---|---|
| 0.1 | 20 | 0.3675 | 0.6172 | 0.7931 | 0.4566 |
| 0.1 | 50 | 0.4151 | 0.6672 | 0.8276 | 0.5042 |
| 0.1 | 100 | **0.4280** | **0.6828** | **0.8793** | **0.5143** |
| 0.25 | 20 | 0.3593 | 0.6190 | 0.7586 | 0.4541 |
| 0.25 | 50 | 0.4007 | 0.6592 | 0.7931 | 0.4924 |
| 0.25 | 100 | 0.4007 | 0.6681 | 0.7931 | 0.4976 |

### 3.2 k=10

| alpha | depth | Recall@10 | Hit@10 |
|---|---|---|---|
| 0.1 | 20 | 0.5012 | 0.9310 |
| 0.1 | 50 | 0.5230 | 0.9138 |
| 0.1 | 100 | 0.5288 | 0.9138 |
| 0.25 | 20 | 0.5012 | 0.9310 |
| 0.25 | 50 | 0.5210 | 0.9138 |
| 0.25 | 100 | 0.5325 | 0.9138 |

选中单元 `(alpha=0.1, depth=100)` 的完整 @10：Recall 0.5288、MRR 0.6878、nDCG 0.5535、Hit 0.9138。

### 3.3 与 v2 alpha=0.5 的对照

v2 dev 检索运行使用 `alpha=0.5`、`depth=50`、无门槛、同一 59 条 dev 查询，可作为同 depth 的对照。

| 配置 | Recall@5 | MRR@5 | Hit@5 |
|---|---|---|---|
| v2 alpha=0.5, depth=50 | 0.4037 | 0.6121 | 0.7414 |
| 本次 alpha=0.1, depth=50 | 0.4151 | 0.6672 | 0.8276 |
| 本次 alpha=0.25, depth=50 | 0.4007 | 0.6592 | 0.7931 |
| 本次 alpha=0.1, depth=100 | 0.4280 | 0.6828 | 0.8793 |

相对 v2 的 `alpha=0.5, depth=50`：`alpha=0.1, depth=50` 的 Recall@5 +1.14 pp、MRR@5 +5.51 pp、Hit@5 +8.62 pp；`alpha=0.1, depth=100` 的 Recall@5 +2.43 pp、MRR@5 +7.07 pp、Hit@5 +13.79 pp。

### 3.4 选参结果

`select_frozen` 选中 `alpha=0.1`、`bm25_depth=100`、`dense_depth=100`、`threshold=0.0`，未 blocked。runner-up 依次为 `(0.1, 50)`、`(0.25, 100)`、`(0.25, 50)`。

选中门槛 `0.0` 的 dev 指标：Recall@5 0.4280、MRR@5 0.6828、nDCG@5 0.5143、Hit@5 0.8793；Recall@10 0.5288、MRR@10 0.6878、nDCG@10 0.5535、Hit@10 0.9138；`false_return_rate=1.0`、`relevant_candidates_dropped=0`、`empty_queries=0`。

### 3.5 补充实验：alpha=0 极端值（dev-only）

补充实验把候选集改为 `alpha ∈ {0.0, 0.1}`，只跑 dev grid，不执行 freeze 或 holdout；当前冻结配置保持 `alpha=0.1`。

| alpha | depth | Recall@5 | MRR@5 | nDCG@5 | Hit@5 | Recall@10 | MRR@10 | nDCG@10 | Hit@10 |
|---|---|---|---|---|---|---|---|---|---|
| 0.0 | 20 | 0.4102 | 0.6902 | 0.5069 | 0.8793 | 0.4911 | 0.6977 | 0.5384 | 0.9310 |
| 0.0 | 50 | 0.4102 | 0.6902 | 0.5066 | 0.8793 | 0.4868 | 0.6977 | 0.5371 | 0.9310 |
| 0.0 | 100 | 0.4102 | 0.6902 | 0.5066 | 0.8793 | 0.4868 | 0.6977 | 0.5371 | 0.9310 |
| 0.1 | 20 | 0.3675 | 0.6172 | 0.4566 | 0.7931 | 0.5012 | 0.6373 | 0.5081 | 0.9310 |
| 0.1 | 50 | 0.4151 | 0.6672 | 0.5042 | 0.8276 | 0.5230 | 0.6801 | 0.5483 | 0.9138 |
| 0.1 | 100 | 0.4280 | 0.6828 | 0.5143 | 0.8793 | 0.5288 | 0.6878 | 0.5535 | 0.9138 |

`alpha=0` 的 @5 三项与 v2 dev dense 单路一致（Recall 0.4102、MRR 0.6902、Hit 0.8793），验证该权重端点的排序语义与 dense 单路一致。

`select_frozen` 在新候选集下仍选中 `(0.1, 100, 100, 0.0)`；runner-up 依次为 `(0.1, 50)`、`(0.0, 20)`、`(0.0, 50)`。相对 `(0.1, 100)`，`alpha=0` 在 @5 的 Recall 低 1.78 pp、nDCG 低 0.77 pp、MRR 高 0.74 pp、Hit 持平；在 @10 的 Recall 低 4.20 pp、nDCG 低 1.64 pp、MRR 高 0.99 pp、Hit 高 1.72 pp。按 nDCG@5 优先，`0.1` 保持；按 MRR@5 优先，`0.0` 胜出。

## 4. holdout 结果

### 4.1 hybrid

| k | Recall | MRR | Hit | nDCG |
|---|---|---|---|---|
| 5 | 0.5023 | 0.6205 | 0.7368 | 0.5193 |
| 10 | 0.6132 | 0.6313 | 0.8070 | 0.5572 |

### 4.2 四组对照

| group | Recall@5 | Hit@5 | Recall@10 | Hit@10 |
|---|---|---|---|---|
| hybrid | 0.5023 | 0.7368 | 0.6132 | 0.8070 |
| dense | 0.4523 | 0.7018 | 0.5614 | 0.8070 |
| bm25 | 0.4249 | 0.6316 | 0.5462 | 0.7018 |
| name | 0.1874 | 0.4035 | 0.2626 | 0.4386 |

holdout 共 66 条查询，其中 57 条有标注、9 条 `expect_empty`。`false_return_rate`：hybrid、dense、bm25 均为 1.0，name 为 0.0；`threshold=0.0` 不对无答案查询做过滤。`name` 组为参考基线，不参与向量检索结论。

## 5. 代码与评测能力变更

- `backend/test/check_retrieval_baseline.py`：新增 `DEFAULT_KS=(5, 10)`；`per_query_record(item, ids, ks)`、`aggregate(rows, ks)`、`evaluate(queries, results, ks)` 支持多个 k；原生输出 `recall@k`、`mrr@k`、`ndcg@k`、`hit@k`；保留 `top10`。
- `backend/test/retrieval_evaluation_support.py`：`EVAL_ROOT=backend/temp/retrieval-evaluation`、`REQUAL_ROOT=backend/temp/retrieval-evaluation-requalification`；`ALPHA_CANDIDATES=(0.0, 0.1)`（主实验使用 `(0.1, 0.25)`，补充实验改为当前值）；`metrics(queries, projections, ks)` 透传 k；`select_frozen` 阶段一改为 @5；`validate_grid_cells` 检查 `recall@5`；`PARENT_HOLDOUT_RUN_ID=20261017T032643Z-10e40f95`。
- `backend/test/check_retrieval_evaluation.py`：新增 `--k`（默认 `5,10`，支持逗号分隔多值）；grid 强制包含 5 与 10；控制台摘要按 k 输出 recall/hit；selfcheck 增加 hit@k、自定义 k、宏平均与单调性检查。
- `backend/app/application/business/retrieval_frozen.py`：`ALPHA=0.1`；注释同步为新持久路径与 `alpha=0.1、两路深度=100、门槛=0.0`。
- selfcheck：118 项全部通过，退出码 0；父证据指向本次 holdout，`historical_holdout_query_ids()` 返回 66 个排序唯一 ID。

## 6. 证据路径

- grid（主实验）：`backend/temp/retrieval-evaluation/20261017T032514Z-c44be847/grid.json`
- grid（alpha=0 补充）：`backend/temp/retrieval-evaluation/20261017T040232Z-c44be847/grid.json`
- 冻结参数：`backend/temp/retrieval-evaluation/frozen-parameters.json`
- holdout：`backend/temp/retrieval-evaluation/20261017T032643Z-10e40f95/compare.json`
- holdout 账本：`backend/temp/retrieval-evaluation/holdout-executions.json`
- selfcheck：`backend/temp/retrieval-evaluation/20261017T032718Z-b585e168/selfcheck.json`
- 查询向量缓存：`backend/temp/retrieval-evaluation/query-vectors/query-vectors-381c33aa4a7a9c6bea4b.json`

## 7. 结果边界

- 本次为单轮运行，未做重复采样；dev 59 条、holdout 66 条。
- alpha=0 补充实验只覆盖 dev 59 条，未执行 freeze/holdout；当前冻结配置仍为 `alpha=0.1`。
- `threshold=0.0`，`false_return_rate` 仍为 1.0；无答案误返回控制不在本次调参范围内。
- `hit@k` 为有标注查询上的宏平均，`expect_empty` 查询不计入排序指标。
- holdout 账本保留 2 条 completed 记录，当前父证据为 `20261017T032643Z-10e40f95`。
- 全链路未新增 Embedding 请求，查询向量来自缓存重放。
- 本报告只覆盖检索排序质量；端到端任务成功率、延迟与 token 消耗不在本次运行范围内。
