# LangSmith + MTEB + Ragas 评测方案

## 已确定的基线目标

主 baseline 为 Hybrid，保留 BM25、dense 对照。候选池上限 20，使用 Recall@20；实际模型输入最多 5 个候选，使用 Hit@5、MRR@5、nDCG@5 和 Precision@5。Hybrid 使用 Milvus 内置 RRF，参数为 `rrf_k=60`、两路深度 100/100、门槛 0.0，基线按 RRF 融合分数排序。

完整配置见 [检索 Baseline 定义](retrieval-baseline.md)。20→5 两阶段管线及指标接入已实现，正式基线成绩待实际运行。下节记录当前源码的实现状态。

## 当前实现与迁移范围

检索评分已迁移到 MTEB 2.26.7。`backend/uv.lock` 固定 `mteb==2.26.7` 与 `jsonlines==4.0.0`，dev 依赖已移除 ranx。评分适配、数据加载、证据、报告和 LangSmith 上传适配已完成开发验证；正式 150 条检索、80 个业务场景执行、云端上传、Ragas 与裁判调用尚未执行。

- LangSmith 负责统一看板、实验管理、对照和逐例分析，MTEB 负责检索指标，Ragas 负责生成指标。
- 候选阶段只计算 Recall@20：门槛与预算裁剪前的前 20 条候选。最终输入阶段计算 Hit@5、MRR@5、nDCG@5、Precision@5，按实际公开顺序最多 5 条评分。两阶段分别调用 MTEB。
- 无答案误返回率、硬约束、候选数、字符预算和 JSON 结构检查继续保留；搜索公开响应最多 5 条且不暴露内部剩余候选身份。
- 二元相关性沿用 grade≥1；Precision@5 固定分母为 5；nDCG@5 使用 `2^grade−1` 增益。
- 新数据包含 150 条检索请求与 80 个端到端业务场景，逐条用户审批后发布。详见 [全新评测数据准备方案](evaluation-dataset-rebuild-plan.md)。
- 历史量化指标、逐例结果、旧数据版本及实验身份完整存档；历史 holdout 保留 `previously_observed_holdout_regression` 身份。
- Ragas 生成评测及 LLM 裁判作为后续需求；本次检索迁移不新增生成运行或裁判调用。
- 上传沿用现有字段白名单与外部实验接口，保持生产 Trace 隐私契约。

MTEB 接入真实 BM25、dense、hybrid 检索结果及业务金标。候选池与最终输入分别调用评分入口：`mteb._evaluators.retrieval_metrics.calculate_retrieval_scores`（候选池 k=20、最终输入 k=5）与 `mrr`（最终输入 k=5）。模块属于 MTEB 私有实现，依赖上界为 `<2.27`。固定分母 Precision@5、nDCG 增益、阶段独立空返回口径及宏平均由测试代码覆盖；功能测试待统一验证阶段执行。

检索与生成指标分别命名，并通过样例 ID、数据版本及运行证据关联。项目验收使用动作目录与业务查询集；公开基准结果单独标明其任务及数据来源。

## 文件与职责

- `backend/evaluation/retrieval.py`：新数据全量检索、历史重评分与 LangSmith 上传入口。
- `backend/evaluation/retrieval_metrics.py`：`RetrievalInput` 到 `RetrievalResult` 的适配；候选池 Recall@20 与最终输入四项 @5 分别计算，历史入口保留单阶段 @5 身份。
- `backend/evaluation/retrieval_support.py`：复用目录、用途适配、条件求值、索引验证与查询向量缓存。
- `backend/evaluation/dataset.py`：新数据加载、Schema 与来源校验、审批门禁、评分输入适配与未审阅候选检测。
- `backend/dataset/evaluation/schemas/`：`defs`、`manifest`、`corpus`、`queries`、`qrels`、`scenarios` 六个 Draft 2020-12 Schema。
- `backend/test/test_retrieval_mteb.py`：MTEB 两阶段评分、证据、报告与上传回归测试。
- `backend/test/test_retrieval_stages.py`：生产 20→5 阶段构造、预算裁剪、隐藏身份与取消语义测试。
- `backend/test/test_evaluation_dataset.py`：数据加载、审批、配额、引用、路径边界与发布门禁测试。
- `backend/test/evaluation_fixtures.py`：使用真实动作目录、已批准补充数据与真实 Schema 构造的开发测试数据。
- `quantitive-index/evaluation.md`：保留历史实际评测指标与实验身份；新数据版本的结果独立记录。
- `backend/temp/retrieval-mteb/<run_id>/`：每轮 `evaluation.json`、`report.md` 与逐组 LangSmith 回执；历史 `backend/temp/retrieval-evaluation/`、`backend/temp/retrieval-ranx/` 原件保持原值。
- `backend/pyproject.toml`、`backend/uv.lock`：固定 MTEB、jsonlines 及实际指标计算依赖；Ragas 依赖在生成评测实现时锁定。

MTEB 负责检索评分，Ragas 负责生成评分。数据适配和业务规则由项目代码执行，并保留必要测试。

## 1. 理解数据集和金标

新数据版本目录为 `backend/dataset/evaluation/<dataset_version>/`，包含 `corpus.jsonl`、`queries.jsonl`、`qrels.jsonl`、`scenarios.jsonl`、`manifest.json` 与 `assets/`。Schema 位于 `backend/dataset/evaluation/schemas/`，版本 manifest 绑定实际 Schema 哈希。

`queries.jsonl` 每行字段：

- `query_id`、`scenario_id`、`query`、`purpose`、`conditions`、`tags`、`family`、`expect_empty`、`judgement_scope`、`review`。
- `purpose` 限定为 `lookup/generation/replacement`。
- `conditions` 保存四个精确筛选字段、环境、声明可用器械、本次不可用器械、本次排除 ID 与已保存画像限制。
- `tags` 为唯一字符串集合，评分按标签生成 `by_tag` 分组；历史查询集仍使用 `by_intent`。
- `judgement_scope.complete` 表示完整相关集合已复核。

`qrels.jsonl` 每行字段：

- `query_id`、`corpus_id`、`grade`、`rationale`、`evidence`、`review`。
- `grade` 为 0..3：3 高度相关、2 相关、1 弱相关、0 已审阅且不相关。
- `(query_id, corpus_id)` 组合唯一。
- `evidence` 使用冻结来源文件与 JSON Pointer，加载器实际解析指针。

等级为 1、2、3 的动作进入二元相关集合，供 Recall@20、Hit、Precision 和 MRR 使用；nDCG 按等级使用 7、3、1 增益。`grade=0` 是已审阅负例，不进入相关集合，可作为未审阅候选检查的已知候选。参与 Recall@20 的整个候选池及最终公开列表中的每个动作 ID 必须具有该请求的 qrel 记录，未审阅候选按未知身份处理并阻止发布。

数据版本统一全量评测：150 条 query（`lookup=60`、`generation=45`、`replacement=45`，各含 10 条 `expect_empty`）、80 个 scenario（10 个主要类别各 8 个）。corpus 与真实目录一致，当前为 1321 条；正式 qrels 数量以实际版本 manifest 为准，`rebuild-v1` 为 1079 条。

两种加载入口：

```python
from evaluation.dataset import load_for_review, load_for_evaluation

load_for_review(version_dir)      # 结构、来源、引用、语义与审阅状态
load_for_evaluation(version_dir)  # 追加全部配额、逐条审批与发布批准
```

正式发布要求全部 query、qrel、scenario 的 `review.status=approved`、`reviewer_type=user`、审阅者与用户决定引用非空、内容哈希一致；全部 query 的 `judgement_scope.complete=true`；没有 `draft` 或 `deferred`；`manifest.release.status=approved`、审阅者为用户且内容摘要与当前 manifest 一致。

query 批准覆盖自身正文与关联 qrels，qrel 批准覆盖自身正文，scenario 批准覆盖场景正文与引用附件；manifest release 摘要覆盖除 `release` 外的全部字段。内容、Schema、来源或附件变化会使对应批准与 release 摘要失效。加载器只读本地文件，不自动补批准或改写条件。

未审阅候选会阻止正式汇总发布。运行结果保留实际 `projections`、`timings` 与未审阅候选列表，组状态记为 `blocked_unreviewed_candidates`，上传入口拒绝该证据。补标并由用户批准新版本后，对同一新版本重算全部对照组；已有证据与旧身份保持原值。

`backend/dataset/evaluation/rebuild-v1/` 已冻结：corpus 1321、queries 150、qrels 1079、scenarios 80、附件 8。全部 query、qrel、scenario 与 release 均为 `approved`；`load_for_review()` 完成结构、来源、引用、语义与配额检查并返回 `ready=true`，`load_for_evaluation()` 通过。场景消息事件只保留实际待发送的用户输入，保存类断言在消息检查点验证零提交、在确认事件检查点验证保存成功，`saved_state` 表达截至检查点的已保存状态。取消场景在消息检查点验证 `pending→invalidated`，在后置取消事件检查点验证 `invalidated→invalidated` 并保留真实取消证据。

批准状态及内容哈希只能证明文件与批准记录一致。标注是否符合业务，需要人工复核。可以在 LangSmith 查看失分样例，并回到动作目录核实。

历史 `action-retrieval-queries.json` 与 `action-retrieval-purpose-adaptation.json` 保留为旧数据，绑定原始批准与暂缓记录。新数据入口不会自行解释尚未批准的条件。

## 2. 安装与本地核验

在仓库根目录执行：

```powershell
uv sync --locked --directory backend
uv lock --check --directory backend
uv run --locked --directory backend python -m evaluation.retrieval --help
uv run --locked --directory backend python -m pytest test/test_retrieval_mteb.py test/test_evaluation_dataset.py -q
uv run --locked --directory backend python -m ruff check evaluation test/test_retrieval_mteb.py test/test_evaluation_dataset.py test/evaluation_fixtures.py
```

`--directory backend` 把后续路径解析基准切换到 `backend/`，因此测试路径和数据参数均相对于后端目录。测试运行超时上限为 300 秒。

`uv.lock` 固定 `mteb==2.26.7` 与 `jsonlines==4.0.0`，当前环境不包含 ranx。数学边界测试使用已知金标与排名，通过真实 MTEB 检索评分入口验证指标和适配层；数据测试使用真实动作目录、已批准补充数据与真实 Schema 构造开发夹具，不替代正式数据批准。

本地核验覆盖：有金标空返回记 0、短返回 Precision@5 固定分母为 5、仅前 5 条参与排序评分、分级 nDCG、重复和非法 ID 拒绝、无答案样例单独统计、150/80 配额、逐条审批、未审阅候选门禁、状态迁移、Unicode 分行、路径边界、证据 Schema、报告和上传 payload。数学边界测试不代表业务检索质量。

测试文件版本单独记录在验证交接中，运行身份覆盖实际影响运行的实现、Schema、锁文件与数据源。依赖或数据变化后，既有缓存与冻结身份按真实规则核验；历史证据身份不会被自动改写。

## 3. 运行统一检索评测

保证现有 Milvus 服务和 `backend/.env` 检索配置可用。新数据入口先执行 `load_for_evaluation()`，任一审批、配额、来源或引用缺口都会在检索前失败。

在仓库根目录执行：

```powershell
uv run --locked --directory backend python -m evaluation.retrieval run --dataset dataset/evaluation/<dataset_version>
```

`--dataset` 相对于 `backend/` 解析。入口执行该版本全部 150 条请求，复用与数据版本及编码配置一致的真实查询向量缓存；缓存缺失或身份不符时明确失败，新缓存需要真实编码并校验。

`run` 必须显式提供 `--dataset` 或 `--legacy-split dev|holdout`。`--legacy-split` 是历史入口，使用旧查询集、旧金标与旧用途适配身份；新数据运行不接受 `--frozen`。`--groups` 默认同时执行 `bm25 dense hybrid`，三组消费相同查询、条件与最终响应预算。

每轮结果写入 `backend/temp/retrieval-mteb/<run_id>/`：

- `evaluation.json`：新数据 Schema 为 `fit-agent-retrieval-mteb-2`，包含数据身份、参数、身份哈希、逐组指标、逐查询候选池、最终模型输入、原分数、公开响应与证据、耗时和未审阅候选。历史入口继续使用 `fit-agent-retrieval-mteb-1` 单阶段身份。
- `report.md`：逐组 Recall@20、Hit@5、MRR@5、nDCG@5、Precision@5、有标注/无答案数量与无答案误返回率。
- `langsmith-<group>.json`：上传成功后写入的回执。

结果包括：

- Recall@20、Hit@5、MRR@5、nDCG@5、Precision@5。
- 无答案误返回率。
- 按 `tags` 分组的指标；历史入口保留 `by_intent`。
- 每条查询的有序候选池、最终模型输入、原始融合分数、完整公开响应、SearchResponseEvidence、逐项指标与真实搜索时间。
- 条件、门槛、预算、RRF 参数及索引、模型、代码、数据身份。

数据身份由 `dataset.version`、`version_dir`、`manifest_sha256`、`content_sha256`、`corpus_sha256`、Schema 哈希、来源与文件哈希组成。实验数据集名称为 `fit-agent-retrieval-<version>-<content_sha256[:12]>`，样例行 ID 使用 `uuid5(dataset_name + ":" + query_id)`，同一数据身份的 ID 稳定，不同版本相互隔离。

出现未审阅候选时，组状态为 `blocked_unreviewed_candidates`，本轮证据与报告仍写入运行目录，`evaluation.json` 的文档状态为 `blocked`，退出码非零。补标并批准新版本后重算全部对照组。

当前耗时范围是“缓存向量搜索及响应构造”。它不包含在线 Embedding，也不等于完整 Agent 响应时间。

## 4. 配置 LangSmith

登录 LangSmith，在设置中创建 API Key，将其保存到已被 gitignore 排除的 `backend/.env`，变量名为 `LANGSMITH_API_KEY`。

使用对应区域或自建服务时，配置正确的 `LANGSMITH_ENDPOINT`。工作区要求时配置 `LANGSMITH_WORKSPACE_ID`。密钥保持本地，不粘贴到聊天、报告或版本库。

本次授权上传范围为现有评测查询、合成条件、金标和候选 ID。上传入口按字段白名单构造内容，不上传真实个人画像、缓存向量、服务凭据或查询端点配置。

上传不会开启生产 Trace，也不会改变原有 Trace 隐私契约。已授权生成评测将请求、回答、实际上下文及参考答案发送至 StepFun，并将样例内容、模型输出及评分结果上传 LangSmith。现有检索上传白名单保持不变；生成评测实现时按上述授权范围构造独立字段白名单，服务凭据保持本地。

## 5. 上传实验并在界面分析

使用第 3 步全量运行产生的 artifact，通过上传入口提交。`--source` 使用实际绝对路径，避免工作目录歧义：

```powershell
uv run --locked --directory backend python -m evaluation.retrieval upload --source D:/repository/fit-agent/backend/temp/retrieval-mteb/<run_id>/evaluation.json
```

上传采用 LangSmith 官方外部实验接口。每个检索组成为一个 experiment，共用同一版本的数据集。上传前重新执行真实两阶段评分与 `validate_projections()`，核验候选池、最终输入、公开响应与 SearchResponseEvidence 一致，并核对数据版本、manifest、内容摘要与语料摘要；按生产序列化重算完整公开响应字符数，从已批准查询条件重做整个候选池的硬约束，并用共享阶段构造核对门槛、裁剪与最终响应。投影自报的预算与硬约束标记不作为合规依据；存储评分与实际候选、金标或引擎身份不一致时拒绝上传。

上传反馈分数按接口要求保留 4 位小数；本地原始指标保持完整精度。上传字段白名单包含查询、条件、用途、标签、家族、有序候选池、最终模型输入、候选原分数、公开搜索响应与证据、契约指标、硬约束、预算结果与 RRF 参数；不上传真实个人画像、缓存向量、服务凭据或查询端点配置。

新数据的数据集名称为 `fit-agent-retrieval-<version>-<content_sha256[:12]>`；历史入口使用拆分、数据集哈希与用途适配哈希生成名称。每条查询的行 ID 使用 `uuid5(dataset_name + ":" + query_id)`，同一数据身份稳定，不同版本相互隔离。

上传回执记录在实验目录，实验与数据集 ID 写入对应运行目录。再次执行时，入口核对已有回执的源文件哈希及远端实验身份，仅提交尚未上传的组。无答案样例只附加 `false_return` 与确定性规则字段，汇总分母仅包含有金标样例。

各实验使用本组真实查询的最早开始时间和最晚结束时间，逐查询的原始耗时保持不变。

在 LangSmith 的 Datasets & Experiments 中：

1. 打开本轮绑定的新版本全量检索数据集。
2. 选择本轮 BM25、dense、hybrid 实验。
3. 查看平均得分及每条样例的输入和候选。
4. 筛选 `hit@5=0`，检查遗漏的目标动作。
5. 查看低 nDCG 样例，检查弱相关动作的排名。
6. 查看 `false_return=1`，检查无答案误返回。
7. 按 tags、intent 和 family 定位集中退化的请求。

无答案样例只附加误返回评分，不填充不存在的排序分数。排名指标的汇总分母仅包含有金标样例。

## 6. 比较两轮配置

在同一版本的全部 150 条检索请求上比较 RRF 平滑参数与召回深度，每轮固定其余条件并记录参数。每轮使用独立运行目录：

```powershell
uv run --locked --directory backend python -m evaluation.retrieval run --dataset dataset/evaluation/<dataset_version> --rrf-k 60 --depth 100
```

新数据入口从 `retrieval_frozen` 读取未显式覆盖的参数，`--rrf-k` 与 `--depth` 只覆盖本轮运行值；运行结果绑定参数与数据身份，不自动改写 runtime 配置。

两轮配置的结果均记录在对应新数据版本的指标文件。在 LangSmith 选择对应实验对照：

- Recall 提升覆盖了哪些新动作？
- MRR 提升是否来自核心查询？
- nDCG 变化是否反映更合理的变式顺序？
- 哪些 query 的得分下降？
- 无答案误返回和硬约束是否变化？

同一指标必须保持相同金标、相关性规则、k 和响应预算。当前查询量有限，小幅差异需要逐例与统计证据支持。

参数选择由人工基于用途判断。入口不自动决定上线参数，也不改写 runtime 配置。

## 7. 版本冻结与全量回归

选定实验后，以实验原件绑定参数、代码、数据和索引身份。在同一版本的全部检索请求与业务场景上运行回归，分别报告检索、生成和确定性业务规则结果。

统一评测集可以用于调参与回归。报告记录配置选择使用的实验，说明结果对应固定评测集合；反复调优存在对集合过拟合的风险。

历史数据继续保留原拆分身份与 `previously_observed_holdout_regression` 等记录，绑定原数据版本存档。

## 8. 用 MTEB 重算历史候选

历史候选可以用于学习和验证迁移口径，不需要运行 Milvus 或编码模型：

```powershell
$history = uv run --locked --directory backend python -m evaluation.retrieval rescore --source D:/repository/fit-agent/backend/temp/retrieval-evaluation/<run_id>/compare.json | ConvertFrom-Json
$history.groups
uv run --locked --directory backend python -m evaluation.retrieval upload --source $history.artifact
```

`rescore` 是历史入口，继续使用旧查询集与已批准用途适配，要求历史 `compare.json` 的查询集与适配哈希一致。结果标记为 `historical_rescore`，使用历史返回 ID 及顺序，按单阶段 @5 口径重新交给 MTEB 评分；历史证据缺少 20 候选，不推算或宣称 Recall@20。历史原始分数仅保留为证据，历史证据身份与成绩保持原值，不追加开发测试结果。

历史证据缺少逐查询检索耗时。上传时间范围表示本次批量重评分，LangSmith 展示的这类延迟不能用于评价检索速度。

## 9. Ragas 生成评测边界

生成评测采集真实请求、真实回答及该次生成实际获得的上下文。Agent 读取的动作详情及其他工具事实须纳入对应回答的证据范围，并绑定数据、代码、生成模型及提示版本。

- **Faithfulness**：检查回答中的事实能否由实际上下文支持，使用回答和上下文评分。
- **FactualCorrectness**：检查回答中的事实与经审阅的参考答案是否一致，需要配套参考答案。

检索动作 ID 金标继续用于 MTEB；生成事实正确性评测使用独立审阅的参考答案及标准。参考答案和审阅标准在构建生成测评集及语料库时编写。

裁判模型确定为 StepFun `step-5-preview`。已在本地 `backend/.env` 预置 `FIT_AGENT_EVAL_JUDGE_MODEL=step-5-preview`；生成评测实现时读取该配置并通过 Ragas 调用模型服务 API，评分结果上传 LangSmith，并记录裁判模型、提示及指标版本。费用不设上限。凭据保留在本地受保护配置中，服务地址、凭据接入、模型可用性及接口兼容性需在接入时核验。

生成评分与检索评分分别展示。器械限制、禁用动作、响应预算、无答案误返回和保存授权继续通过项目确定性规则检查。生成评测须使用实际产物验证证据关联、指标输入、裁判调用及上传白名单。

生成评测已实现独立入口：

```powershell
uv run --locked --directory backend python -m evaluation.summary report --source backend/temp/evaluation-scenarios/<run_id>
uv run --locked --directory backend python -m evaluation.generation_upload check --source backend/temp/evaluation-scenarios/<run_id>
uv run --locked --directory backend python -m evaluation.generation_upload upload --source backend/temp/evaluation-scenarios/<run_id>
```

`summary` 与 `generation_upload check` 均只读本地产物，不加载密钥、不发起网络请求。生成指标逐检查点保留状态，场景内按成功检查点取均值，跨场景并列输出“含部分成功”与“仅完整成功”两套分母；有效发布只认完整成功口径。`upload` 仅消费通过正式全量门禁的运行，按独立字段白名单逐字段构造 payload，排除裁判地址、裁判调用原文、prompt 指纹原文、原始模型调用与本地路径，并保存源摘要和 payload 摘要回执。详细口径见 [共享契约](evaluation-contracts.md) 第 9 节。

## 检索评分口径核对

- **Recall@20**：候选池前 20 条命中的相关 ID 数除以金标全部正相关 ID 总数。候选池为空时记 0。
- **Hit@5**：最终模型输入前 5 条至少命中一个 grade≥1 的相关 ID 记 1，否则记 0。
- **Precision@5**：最终模型输入前 5 条命中的相关 ID 数除以 5，短返回保留空位。
- **MRR@5**：最终模型输入前 5 条中第一个相关动作的排名倒数；未命中记 0。
- **nDCG@5**：以 `2^grade−1` 为增益、`log2(rank+1)` 为折损计算 DCG@5，除以完整金标按等级降序排列的理想 DCG@5。
- **无答案误返回率**：预期空结果样例的最终模型输入非空记 1，按这类样例平均。

有金标样例的最终输入为空时，四项 @5 指标为 0；Recall@20 保持候选阶段的真实值。无答案样例的排序指标不填值。汇总按对应样例逐条宏平均。

自定义布尔业务规则可由人工直接核对。平台和指标库不会自动消除金标遗漏、用途偏差或数据泄漏。

## 官方依据

- [MTEB 运行评测](https://docs.mteb.org/get_started/usage/running_the_evaluation/)
- [MTEB 检索指标实现](https://github.com/embeddings-benchmark/mteb/blob/cd182883/mteb/_evaluators/retrieval_metrics.py)
- [Ragas Faithfulness](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/)
- [Ragas FactualCorrectness](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/factual_correctness/)
- [LangSmith 外部实验上传与对照](https://docs.langchain.com/langsmith/upload-existing-experiments)
