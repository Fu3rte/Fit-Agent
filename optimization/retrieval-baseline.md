---
title: 检索 Baseline 定义
lang: zh
template: doc
---

## A 已确定的基线配置

主 baseline：Hybrid RRF。对照组：BM25、dense。

- 融合算法：Milvus 内置 `RRFRanker(k=60)`，配置身份为 `fusion=rrf`、`rrf_k=60`。
- Hybrid 两路内部召回深度：100/100。
- 每组最终候选池：最多 20 条。
- 提供给模型的候选动作：最多 5 条。
- Hybrid 分数门槛：0.0。
- 排序：按 RRF 融合分数排序，基线不增加独立 reranker。
- 沿用现有硬约束求值及响应字符预算。

本文件记录用户确定的 RRF 基线。RRF 已接入真实检索与评测运行入口，20→5 两阶段管线已在生产业务检索与评测入口共用同一阶段构造；正式基线运行仍待完成。RRF 的 k=60 为排名平滑参数，候选数量与指标 k 分别配置。

## B 指标的阶段与 k

**候选阶段：Recall@20。**

在硬约束过滤后，取实际检索排名前 20 条；位置在后续重排、门槛筛选及 Prompt 预算裁剪之前。

分母为冻结金标中全部 grade≥1 的相关动作数。

**最终输入阶段：Hit@5、MRR@5、nDCG@5、Precision@5。**

按实际提供给模型的候选动作及可见顺序评分，最多 5 条。Precision@5 固定以 5 为分母。

有金标空返回记 0。预期无答案查询独立统计误返回，并明确其输入阶段。

候选池、最终输入和各项得分分别保存真实证据。模型可见元信息需与 Top-5 约定一致。

## C 后续优化的比较规则

不同算法、融合策略或 reranker 均可与此 RRF baseline 比较。

对照实验固定同一冻结数据、金标、阶段定义、k 和约束规则。各实验记录真实代码、参数、模型、索引及缓存身份。

仅重排同一候选池时，Recall@20 保持不变；最终 Top-5 的质量可以变化。

数据修改或补标须经用户批准，统一比较实验的数据版本。

当前不设统一 Recall 数值及格线。解释分数时同时查看任务类型、相关集合规模及理论上限。

## D 实现与正式结果状态

当前源码核对结果：

- `retrieval_frozen.py` 的 Hybrid 参数为 `fusion=rrf`、`rrf_k=60`、深度 100/100、门槛 0.0，并独立记录候选池上限 20 与最终输入上限 5。
- `search_vectors` 使用 `RRFRanker`；生产 runtime 与评测入口共用 RRF 配置。
- `build_search_stage` 在同一候选池上构造两阶段：门槛与预算裁剪前的前 20 条为候选池，最终公开搜索响应最多 5 条。
- 生产 runtime 不再提前应用门槛；门槛属于阶段构造，调用方保留门槛前的真实候选池。
- 搜索公开响应仅包含 `items` 与 `has_more`，不再暴露内部剩余候选 ID、名称或排名；详情响应继续保留 `remaining_ids`。
- `retrieval_metrics.py` 的正式入口消费 `RetrievalInput`：Recall@20 只消费候选池，Hit@5、MRR@5、nDCG@5、Precision@5 只消费最终模型输入。
- 历史入口继续输出单阶段 @5 指标身份，不推算或宣称 Recall@20。

按用户确认，评测集已冻结，当前正式 baseline 以 RRF 为准。用户决定跳过加权融合 baseline 的重新评测。历史量化结果及算法身份保持完整。

RRF 正式成绩需要后续实际运行并绑定对应数据、配置及 run_id。此次算法切换不修改冻结数据或提前执行全量正式评测。

## E RRF 接入验证

验证由 subagent 实际执行，以下命令退出码均为 0：

- `uv run --locked --directory backend python -m pytest test/test_retrieval_rrf.py test/test_retrieval_mteb.py -q --basetemp=D:/Repository/Fit-Agent/backend/temp/rrf-pytest`：74 项通过，耗时 63.56 秒。
- `uv run --locked --directory backend python -m ruff check app/application/business/retrieval_search.py app/application/business/retrieval_runtime.py app/application/business/retrieval_frozen.py evaluation/retrieval.py test/test_retrieval_rrf.py test/test_retrieval_mteb.py`：通过。
- `git diff --check`：通过，存在 Git LF→CRLF 提示。
- `uv run --locked --directory backend --env-file D:/Repository/Fit-Agent/backend/.env python -m test.test_retrieval_rrf`：真实 Milvus 只读核验通过。

真实核验复用已发布集合中的一个动作向量，覆盖无过滤、器械过滤并排除源动作、空器械集合三种条件。分别读取 BM25、dense 的实际排名，以 `Σ 1/(60+rank)` 验算 RRF 候选分数及排序，绝对容差为 `1e-6`；Embedding 请求为 0，未写集合。

本次未执行 150 条正式检索、80 个业务场景、生成裁判或 LangSmith 上传。验证证明本次 RRF 接入及上述范围正确，正式质量成绩保持待运行状态。

20→5 两阶段接入新增的契约、指标、阶段构造与证据测试代码尚未执行；功能测试待整体补齐后的统一验证阶段执行，未执行的测试不得视为通过。
