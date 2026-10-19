# 动作检索评测

## 当前口径

- 指标引擎：MTEB 2.26.7；版本由 `backend/uv.lock` 锁定，每次运行记录实际版本。
- 评分入口：`mteb._evaluators.retrieval_metrics.calculate_retrieval_scores` 与 `mrr`；依赖上界为 `<2.27`。
- 金标：新数据使用 `backend/dataset/evaluation/<dataset_version>/queries.jsonl` 与 `qrels.jsonl`，逐条用户审批后发布；历史记录继续使用 `backend/dataset/action-retrieval-queries.json` 与已批准用途适配表。
- 相关性：grade >= 1。候选阶段 Recall@20 对应 `recall_20`；最终输入阶段 Hit 对应 `success_5`，Precision 对应 `P_5`，nDCG 对应 `ndcg_cut_5`，MRR 由 `mrr()` 返回。
- nDCG 增益：`2^grade−1`，等级 1/2/3 映射为 1/3/7。
- 阶段：Recall@20 只消费门槛前候选池（最多 20 条）；四项 @5 只消费实际公开的最终输入（最多 5 条）。两阶段分别调用 MTEB。
- Precision@5 固定以 5 为分母，短结果保留未填满位置。
- 排序指标对有金标查询做宏平均；有答案查询最终输入为空时四项 @5 计零分，Recall@20 保持候选阶段真实值。
- 无答案误返回率只按最终模型输入是否非空统计，分母为 `expect_empty` 查询数。
- 缺少结果、重复 ID、目录外 ID、预算或硬约束违规直接阻止记录有效结果。
- 实际返回顺序编码为严格递减 rank score 交给 MTEB；原检索分数独立保留。
- 新数据两阶段证据 Schema 为 `fit-agent-retrieval-mteb-2`，历史单阶段证据继续使用 `fit-agent-retrieval-mteb-1`；输出目录为 `backend/temp/retrieval-mteb/<run_id>/`。
- `historical_rescore` 表示对历史实际候选重算指标；不表示当前系统运行或新的留出验收。
- `live` 使用真实 Milvus 查询和已有查询向量缓存；耗时不包含在线 Embedding。
- 历史留出集已经被观察，后续运行标记为回归评测。

## 当前实现状态

检索评分迁移、依赖清理、数据加载、证据、报告与上传适配已完成开发验证。当前环境固定 MTEB 2.26.7 与 jsonlines 4.0.0，不包含 ranx。新数据 `backend/dataset/evaluation/rebuild-v1/` 已冻结为 corpus 1321、queries 150、qrels 1079、scenarios 80、附件 8；全部 query、qrel、scenario 的 review 与 manifest.release 为 approved，`load_for_review` 与 `load_for_evaluation` 通过。取消检查点按用户授权修订：消息回合验证 `pending→invalidated`，后置取消事件验证 `invalidated→invalidated` 并保留真实取消证据。正式 150 条检索、80 个业务场景、云端上传、Ragas 与裁判调用尚未执行，本页不追加开发测试成绩。

## 工具职责与可信度

MTEB 负责标准排序指标计算，LangSmith 负责实验管理和逐例分析，Ragas 负责后续生成指标。检索金标仍需人工复核。工具接入不证明标注正确、覆盖完整或评测集独立。

现有功能回归测试继续覆盖器械、禁用动作、JSON、索引、取消和保存授权。评测适配器保留输入校验和边界测试。

使用说明见 `optimization/retrieval-evaluation-guide.md`。有效评测通过入口写入 `backend/temp/retrieval-mteb/`；历史 ranx 原件继续保留在 `backend/temp/retrieval-ranx/`。

## 执行验证

以下执行记录、得分、时间和实验 ID 为 ranx 迁移前的历史原件，保持原值。

- worker 执行 `uv run --directory backend python -m pytest test/test_retrieval_ranx.py -q`：退出码 0，32 passed。
- worker 执行 `uv run --directory backend python -m ruff check evaluation test/test_retrieval_ranx.py`：退出码 0。日志：`tmp/retrieval-final-checks/01-ruff.log`。
- worker 执行响应预算与条件功能检查：`test.check_retrieval_response`、`test.check_retrieval_conditions`，均退出码 0。
- 历史重评分：66 条查询、四组；上传 payload 本地构造、白名单、候选绑定及严格 JSON 检查通过。结合开发集，共检查 7 组、441 行。证据：`tmp/retrieval-revision-validation/05-payload-summary.json`。
- 最终检索源码对应的真实开发集：59 条查询，三组成功，退出码 0。证据：`backend/temp/retrieval-ranx/20261017T081150Z-8a6d899e/evaluation.json`。
- worker 执行功能准备检查：退出码 1，8 项通过、1 项失败。唯一失败为 `historical_ledgers_preserved`：`backend/temp/retrieval-evaluation-requalification/authorization.json`、`executions.json` 缺失。现有冻结参数及留出执行账本仍在。未重建或替代历史原件。证据：`tmp/retrieval-final-checks/eval-prep-utf8/eval-prep.json`；日志无 `UnicodeDecodeError`。
- LangSmith 云端上传与读取核验通过：数据集 59 examples，BM25、dense、hybrid 各 59 runs、59 个不同 reference examples。每组 699 项反馈均按 run 逐项核对 key 与四位小数评分，无缺失、重复、多余或数值不符。证据：`tmp/langsmith-upload-validation/three-groups-report.json`。
- 云端反馈分数保留 4 位小数，本地原始 ranx 指标保持完整精度。

## 20261017T081150Z-8a6d899e

- 类型：`live`；拆分：`dev`。
- 数据集 SHA-256：`8260d967a5c7a17e36f792e3d5a003cee41454f8ef09fdf7df4caeb55b835458`。
- 证据：`backend/temp/retrieval-ranx/20261017T081150Z-8a6d899e/evaluation.json`。
- 评分：ranx `0.3.21`；相关性 grade>=1；nDCG 为 ndcg_burges。
- 暂缓样例：`q-cond-002, q-cond-006, q-cond-015`。
- 用途：`development`。

| group | 有标注 / 无答案 | Hit@5 | Recall@5 | Precision@5 | MRR@5 | nDCG@5 | Hit@10 | Recall@10 | Precision@10 | MRR@10 | nDCG@10 | 无答案误返回率 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| bm25 | 58 / 1 | 0.603448 | 0.331034 | 0.220690 | 0.439368 | 0.330167 | 0.689655 | 0.450144 | 0.153448 | 0.449521 | 0.371821 | 1.000000 |
| dense | 58 / 1 | 0.879310 | 0.410201 | 0.255172 | 0.690230 | 0.506889 | 0.931034 | 0.492549 | 0.158621 | 0.698440 | 0.539286 | 1.000000 |
| hybrid | 58 / 1 | 0.879310 | 0.428017 | 0.265517 | 0.682759 | 0.514316 | 0.913793 | 0.528756 | 0.168966 | 0.687787 | 0.553514 | 1.000000 |

- LangSmith：`20261017T081150Z-8a6d899e/bm25`；experiment ID `d4a5d450-004f-4880-8967-b1d4e1b3061d`；dataset ID `b2ffed00-465f-473a-9f57-1201eede7de4`。

- LangSmith：`20261017T081150Z-8a6d899e/dense`；experiment ID `7d5378f3-821e-4f43-9f3d-f4471108aa08`；dataset ID `b2ffed00-465f-473a-9f57-1201eede7de4`。

- LangSmith：`20261017T081150Z-8a6d899e/hybrid`；experiment ID `a57b797b-e9a1-4d6c-996b-cefb0f422c8b`；dataset ID `b2ffed00-465f-473a-9f57-1201eede7de4`。
