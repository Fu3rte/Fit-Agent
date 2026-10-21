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
- `backend/evaluation/langsmith_retrieval.py`：LangSmith SDK 检索实验入口，包含 `hybrid-rerank` 同池重排实验。
- `backend/test/test_retrieval_rerank_evaluation.py`：重排证据、同池对照、离线回放、上传白名单与场景记录回归测试。
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

数据版本统一全量评测：150 条 query（`lookup=60`、`generation=45`、`replacement=45`，各含 10 条 `expect_empty`）。场景数量与类别配额按版本固定：`rebuild-v1`、`rebuild-v2` 为 80 个 scenario（10 个主要类别各 8 个），默认版本 `rebuild-v3` 为 40 个 scenario（类别配额 8/5/4/2/4/3/3/3/5/3）。corpus 与真实目录一致，当前为 1321 条；正式 qrels 数量以实际版本 manifest 为准，`rebuild-v1` 为 1079 条，`rebuild-v2` 与 `rebuild-v3` 为 1104 条。

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

`backend/dataset/evaluation/rebuild-v2/` 继承 `rebuild-v1`，追加用户审阅批准的 25 条相关性标注：qrels 共 1104 条，新增分布为 0 分 8 条、1 分 11 条、2 分 6 条。三个关联查询更新审阅范围与审批摘要；原有 qrels、其余查询、corpus、场景和附件保持继承内容。批准记录见 [rebuild-v2 补标批准记录](evaluation-rebuild-v2-qrels-approval.md)。使用新版本时显式指定 `--dataset dataset/evaluation/rebuild-v2`；旧版本及其 blocked 运行身份保持原值。正式全量评测与云端上传仍需对应授权，后续出现的新未审阅候选继续阻断发布。

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

本地核验覆盖：有金标空返回记 0、短返回 Precision@5 固定分母为 5、仅前 5 条参与排序评分、分级 nDCG、重复和非法 ID 拒绝、无答案样例单独统计、150 条查询配额与版本化场景配额、逐条审批、未审阅候选门禁、状态迁移、Unicode 分行、路径边界、证据 Schema、报告和上传 payload。数学边界测试不代表业务检索质量。

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

## 3.1 LangSmith SDK 检索实验

`backend/evaluation/langsmith_retrieval.py` 使用 LangSmith 公开 SDK 执行真实检索实验，复用 `search_vectors`、`project_response`、`RetrievalInput` 与 `retrieval_metrics.evaluate`。

```powershell
uv run --locked --env-file .env --directory backend python -m evaluation.langsmith_retrieval sync --dataset dataset/evaluation/rebuild-v1
uv run --locked --env-file .env --directory backend python -m evaluation.langsmith_retrieval run --dataset dataset/evaluation/rebuild-v1 --groups bm25 dense hybrid --purpose validation
uv run --locked --env-file .env --directory backend python -m evaluation.langsmith_retrieval run --dataset dataset/evaluation/rebuild-v1 --groups bm25 dense hybrid --purpose formal
```

数据集名称为 `fit-agent-retrieval-sdk-<version>-<content_sha256[:12]>`。每个 example 的 ID 由数据集名称与 `query_id` 稳定派生；`inputs` 只包含查询、用途与合成条件，`reference_outputs` 包含 `query_id`、`scenario_id`、`expect_empty` 与 `labels`，`metadata` 记录用途、标签、样本摘要与数据摘要。同步入口按名称与内容核验远端样例，缺失样例补齐，未知字段或内容冲突直接阻断。本地回执位于 `backend/temp/retrieval-langsmith/datasets/`。

每个检索组独立创建实验，固定 `max_concurrency=6`、`num_repetitions=1`、`blocking=True`，三组依次执行。`purpose=validation` 按用途各取第一条有答案与第一条无答案查询，共 6 条；`purpose=formal` 覆盖 150 条。实验 metadata 记录本地 `run_id`、组名、用途、数据与 manifest 摘要、代码身份、冻结参数、物理索引身份、向量缓存身份、并发与样本范围。

`run.json`、`<group>/result.json` 与 `<group>/samples/<query_id>.json` 位于 `backend/temp/retrieval-langsmith/<run_id>/`。覆盖核验读取远端 run 与 feedback，逐样本比对本地评分；远端落库延迟时有界重试。SDK feedback 保留 4 位小数，本地一致性容差为 `5e-5`。并发证据同时记录 target 在途数与底层 Milvus RPC 调用数；现有 `MilvusConnection` 仍按内部锁串行执行 RPC。

候选审阅门禁沿用 `EvaluationDataset.reviewed_corpus_ids()` 与 `unreviewed_candidates` 规则，覆盖门槛与预算裁剪前的完整候选池。target 返回候选证据时写入 `review` 状态；evaluator 与 `expected_feedback` 在构造 `RetrievalInput` 前阻断；`parity_report`、`verify_group` 与 `sdk_summary` 在评分前再次核验。缺标组的 `run.json` 保留候选、`unknown_candidates` 与错误信息，组状态记为 `blocked_unreviewed_candidates`，实验文档状态保持 `incomplete`。空候选池继续按既有 `false_return` 口径处理。

dense 与 hybrid 复用 `backend/temp/retrieval-evaluation/query-vectors/` 下的查询向量缓存。缓存缺失或身份不符时该组直接阻断并报告所需路径，不自动生成全量查询 Embedding。

### 3.2 同一候选池上的 RRF 与重排对照

`run --rerank` 在 `hybrid` 组内执行同池对照，并写入两个独立实验身份：

```powershell
uv run --locked --env-file .env --directory backend python -m evaluation.retrieval run --dataset dataset/evaluation/rebuild-v1 --groups hybrid --rerank
```

每个查询只调用一次真实召回，冻结门槛前 RRF Top-20；先构造显式 `hybrid` baseline 响应，再对该池执行一次 `RerankClient.rerank()`，用同一 `build_search_stage` 构造 `hybrid-rerank` 响应。`evaluation.json` 的 `groups.hybrid` 与 `groups.hybrid-rerank` 共享候选 ID、顺序、RRF 分数与门槛；`hybrid-rerank` 额外保存 `experiment=rerank` 与 `rerank` 证据：

- `identity`：配置模型、完整请求地址、文本版本。
- `scored`：按重排分数降序的 `id`、`rerank_score` 与从 0 开始的 `rrf_rank`。
- `usage` 与 `request_id`；空池记录零请求与空 `request_id`。
- 公开响应、`SearchResponseEvidence`、实际门槛、数量上限、预算与字符数。

本地小规模核验使用与 LangSmith 验证一致的 6 条已批准查询：

```powershell
uv run --locked --env-file .env --directory backend python -m evaluation.retrieval run --dataset dataset/evaluation/rebuild-v1 --groups hybrid --rerank --sample validation
```

`--sample validation` 与 `--limit N` 写入 `sample_scope`，状态仅供本地验证；上传入口拒绝子集证据。正式全量仍使用不带子集参数的同一条命令。

Recall@20 两个组完全相同；Hit@5、MRR@5、nDCG@5、Precision@5 只按重排后的公开顺序评分。发布门禁会拒绝两组候选或 RRF 分数不一致的证据。

LangSmith SDK 入口使用独立组名：

```powershell
uv run --locked --env-file .env --directory backend python -m evaluation.langsmith_retrieval run --dataset dataset/evaluation/rebuild-v1 --groups hybrid hybrid-rerank --purpose validation
```

`hybrid-rerank` target 的每个样例同时返回重排证据与同池 baseline 的候选、最终输入与响应。实验 metadata 记录 `experiment`、`rerank_identity`（模型、文本版本、地址 SHA-256），`result.json` 的 `comparison` 保存逐查询换序统计。

### 3.3 仅用保存证据离线回放

```powershell
uv run --locked --directory backend python -m evaluation.retrieval replay --source D:/repository/fit-agent/backend/temp/retrieval-mteb/<run_id>/evaluation.json
```

回放读取已保存的候选 ID、RRF 分数、重排结果、身份、用量与请求身份，重建 `RerankResult`、`RerankIdentity` 与共享阶段，逐项核对候选池、最终输入、公开条目、`has_more`、预算与逐查询指标。回放不调用重排服务、Embedding 或 Milvus。缺少重排结果或身份、ID 集合或原排名不符、越界、重复、非降序、同分次序错误与最终输入不一致均明确失败。历史缺少 `experiment` 与 `rerank` 字段的证据按显式 RRF baseline 回放；REBUILD 之前的单阶段证据继续使用 `rescore`。

## 4. 配置 LangSmith

登录 LangSmith，在设置中创建 API Key，将其保存到已被 gitignore 排除的 `backend/.env`，变量名为 `LANGSMITH_API_KEY`。

使用对应区域或自建服务时，配置正确的 `LANGSMITH_ENDPOINT`。工作区要求时配置 `LANGSMITH_WORKSPACE_ID`。密钥保持本地，不粘贴到聊天、报告或版本库。

本次授权上传范围为现有评测查询、合成条件、金标和候选 ID。上传入口按字段白名单构造内容，不上传真实个人画像、缓存向量、服务凭据或查询端点配置。

上传不会开启生产 Trace，也不会改变原有 Trace 隐私契约。已授权生成评测将请求、回答、实际上下文及参考答案发送至用户当前配置的裁判服务，并将样例内容、模型输出及评分结果上传 LangSmith。现有检索上传白名单保持不变；生成评测实现时按上述授权范围构造独立字段白名单，服务凭据保持本地。

## 5. 上传实验并在界面分析

使用第 3 步全量运行产生的 artifact，通过上传入口提交。`--source` 使用实际绝对路径，避免工作目录歧义：

```powershell
uv run --locked --directory backend python -m evaluation.retrieval upload --source D:/repository/fit-agent/backend/temp/retrieval-mteb/<run_id>/evaluation.json
```

上传采用 LangSmith 官方外部实验接口。每个检索组成为一个 experiment，共用同一版本的数据集。上传前重新执行真实两阶段评分与 `validate_projections()`，核验候选池、最终输入、公开响应与 SearchResponseEvidence 一致，并核对数据版本、manifest、内容摘要与语料摘要；按生产序列化重算完整公开响应字符数，从已批准查询条件重做整个候选池的硬约束，并用共享阶段构造核对门槛、裁剪与最终响应。投影自报的预算与硬约束标记不作为合规依据；存储评分与实际候选、金标或引擎身份不一致时拒绝上传。

上传反馈分数按接口要求保留 4 位小数；本地原始指标保持完整精度。上传字段白名单包含查询、条件、用途、标签、家族、有序候选池、最终模型输入、候选原分数、公开搜索响应与证据、契约指标、硬约束、预算结果与 RRF 参数；重排实验额外包含 `experiment`、重排模型、文本版本、地址 SHA-256、重排排序、用量、`request_id` 与同池基线候选/最终输入/响应。完整请求地址、API Key、完整客户端配置、原始 HTTP 响应、真实个人画像、缓存向量与服务凭据保持本地。

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

重排实验在同一数据、金标、冻结参数与候选池上运行。对照时固定 `hybrid` 同池 baseline 与 `hybrid-rerank`，只观察最终 @5 的变化；Recall@20 必须一致。重排模型、完整请求地址、文本版本、用量与 `request_id` 进入独立实验身份，向量缓存目录与索引清单保持不变。

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

裁判模型从运行启动时的受保护配置 `FIT_AGENT_EVAL_JUDGE_MODEL` 读取，服务地址与密钥分别使用 `FIT_AGENT_EVAL_JUDGE_BASE_URL`、`FIT_AGENT_EVAL_JUDGE_API_KEY`。通过 Ragas 调用配置指定的服务，评分结果记录到 LangSmith，并绑定实际裁判模型、提示及指标版本。费用不设上限，凭据保持本地。每次更换配置均需核验当前实现支持范围、模型可用性及接口兼容性；禁止失败后静默换模型。

生成评分与检索评分分别展示。器械限制、禁用动作、响应预算、无答案误返回和保存授权继续通过项目确定性规则检查。生成评测须使用实际产物验证证据关联、指标输入、裁判调用及上传白名单。

生成评测已实现独立入口：

```powershell
uv run --locked --directory backend python -m evaluation.summary report --source backend/temp/evaluation-scenarios/<run_id>
uv run --locked --directory backend python -m evaluation.generation_upload check --source backend/temp/evaluation-scenarios/<run_id>
uv run --locked --directory backend python -m evaluation.generation_upload upload --source backend/temp/evaluation-scenarios/<run_id>
```

`summary` 与 `generation_upload check` 均只读本地产物，不加载密钥、不发起网络请求。生成指标逐检查点保留状态，场景内按成功检查点取均值，跨场景并列输出“含部分成功”与“仅完整成功”两套分母；有效发布只认完整成功口径。`upload` 仅消费通过正式全量门禁的运行，按独立字段白名单逐字段构造 payload，排除裁判地址、裁判调用原文、prompt 指纹原文、原始模型调用与本地路径，并保存源摘要和 payload 摘要回执。详细口径见 [共享契约](evaluation-contracts.md) 第 9 节。

## 10. 任务五验收

运行方法（真实服务入口，逐条顺序执行，配置由 `uv run --env-file .env` 加载。执行前需本机 Milvus 栈可用，判据是 `127.0.0.1:19530` 处于 LISTENING 且容器 `fit-agent-milvus-standalone` 状态为 healthy；服务缺失时脚本在建立连接处即失败，早于任何断言。`replay` 入口只读本地产物，不需要环境文件）：

```powershell
uv run --locked --env-file .env --directory backend python -m test.check_rerank_client_live
uv run --locked --env-file .env --directory backend python -m test.check_retrieval_rerank_production
uv run --locked --env-file .env --directory backend python -m test.check_retrieval_runtime_lifecycle
uv run --locked --env-file .env --directory backend python -m test.check_retrieval_runtime_integration
uv run --locked --env-file .env --directory backend python -m test.check_retrieval_toolchain
uv run --locked --env-file .env --directory backend python -m evaluation.retrieval run --dataset dataset/evaluation/rebuild-v1 --groups hybrid --rerank --sample validation
uv run --locked --directory backend python -m evaluation.retrieval replay --source <evaluation.json> --output <run_dir>/replay.json
```

- 实验身份：`hybrid` 为 `experiment=rrf_baseline`；`hybrid-rerank` 为 `experiment=rerank`。本地输出目录分别为 `backend/temp/retrieval-mteb/<run_id>/` 与 `backend/temp/retrieval-langsmith/<run_id>/<group>/`。
- 证据字段：`experiment`、`rerank.identity{model,endpoint,text_version}`、`rerank.scored[{id,rerank_score,rrf_rank}]`、`rerank.usage`、`rerank.request_id`、`candidate_pool`、`scores`、`final_model_input`、`response`、`search_evidence`、`threshold`、`budget_chars`、`item_limit`、`serialized_chars`。
- 上传字段：模型、文本版本、地址 SHA-256、排序、用量、`request_id`、同池基线证据；完整地址、密钥与原始 HTTP 响应不上传。新增字段未超出既有授权范围。
- 场景证据：`SearchStageRecord` 增加 `experiment` 与 `rerank`；`executed_index_identity` 聚合实际重排身份并拒绝混合运行。
- 开发验证运行：`backend/temp/retrieval-mteb/20261020T120245Z-c7542e56/evaluation.json`，6 条已批准验证查询（3 条非空、3 条空池）。`hybrid` 与 `hybrid-rerank` 的候选池、顺序、RRF 分数与门槛逐查询一致；该运行两组均为 `blocked_unreviewed_candidates`，`_validate_same_pool` 在算分分支之前返回，逐查询 Recall@20 数值未计算、两组间未执行数值比较，同池 Recall@20 相等的口径由 `test_retrieval_rerank_evaluation.py::test_same_pool_keeps_recall_and_rerank_changes_at_five` 在离线数据上断言；3 条非空查询发生真实重排，`usage.requests=3`、`prompt_tokens=14454`、`total_tokens=14454`，3 个唯一 `request_id`，空池均为零请求与空 `request_id`；3/6 查询最终 @5 顺序变化。两组预算、硬约束与字符数全部合规。
- 任务五独立复验运行：`backend/temp/retrieval-mteb/20261020T140331Z-92009dc8/evaluation.json`，同一数据版本与同一 6 条验证查询。两组 `candidate_pool` 与 `scores` 逐查询字面相等，`requests=3`、`prompt_tokens=14454`、`total_tokens=14454`、3 个唯一 `request_id`、3 条空池零请求；两组均 `blocked_unreviewed_candidates`，缺标为 `q-lookup-001` 5 个、`q-generation-001` 6 个、`q-replacement-001` 14 个。逐查询耗时窗口：非空 406.1/167.3/164.1 毫秒、空池 23.9/16.2/16.3 毫秒，`hybrid` 与 `hybrid-rerank` 记录同一时间窗。
- 回放验证：对新 artifact 执行 `replay --source … --output …` 退出码 0，`status=blocked`、`blocked_groups=["hybrid","hybrid-rerank"]`、两组 `metrics=null`，零模型零 Milvus 调用；对开发验证 artifact 的只读回放输出与新 artifact 逐字段相同。
- 真实链路验证：客户端脚本完成 1 条与 20 条真实评分（`{1,220,220}` 与 `{1,4110,4110}`），在途与外层取消均传播且取消后可复用、关闭后 `post_close_tasks=0`；生产脚本经 `BusinessService.search_exercises` 验证 20 条候选池、公开顺序与 `final_input` 一致、器械与排除硬约束在全池成立、空池零请求零用量；生命周期脚本验证前置取消不初始化、初始化中取消释放资源、异循环关闭保留资源、所属循环关闭与重复/并发关闭正确；工具链脚本验证接入重排后工具封套仍为 `{items, has_more}` 且 `json.dumps` 字符数不超过 4000。
- 门禁入口清单：审阅规则唯一落点为 `EvaluationDataset.reviewed_corpus_ids()` 与 `unreviewed_candidates`；生效位置为 `evaluation/retrieval.py:473`（本地算分）、`:615-628`（本地回放）、`:1122`（上传要求 `status=complete`）、`evaluation/langsmith_retrieval.py:376`（`retrieval_input`，即 evaluator 与 `expected_feedback`）、`:640`（target 审阅证据）、`:779`（远端覆盖核验）、`:911`（`parity_report`）、`evaluation/sdk_summary.py:481`（缺标集合）与 `:508`（跳过缺标样本）。
- P1 独立复验：`q-lookup-001` 的 5 个真实未审阅候选 `0022, 0589, 0574, 0055, 0788` 在 SDK `expected_feedback`（重排组与同池基线组）、`retrieval_input`、`make_evaluator`、`parity_report` 与本地 `validate_group` 五处入口全部阻断，缺标集合与开发验证运行逐字一致；空池查询返回 `{"false_return": 0.0}`，含已审阅 `grade=0` 候选的完整审阅查询照常产出五项指标。
- 已接受限制：离线入口的文档级 `rerank_identity` 与逐投影重排身份不做交叉比对；`run --rerank` 的 `hybrid` 组耗时窗口包含同池重排调用。两项按用户决定保持现状。
- 测试与 lint：第一轮按批执行，清单内 12 个回归文件为批次 A `133 passed`、审阅门禁 `13 passed`、MTEB `72 passed`、LangSmith 检索 `17 passed`、SDK 汇总 `22 passed`、场景与汇总消费方 `76 passed`，合计 333 项、退出码全为 0；附加批次实测 `68 passed`、`28 passed` 与 `64 passed, 1 failed`。第二轮按放行补齐重排证据回归断言并修正陈旧断言后，`test_input_audit.py` 单文件 `8 passed`、`test_evaluation_summary.py` `28 passed`、`test_retrieval_rerank_evaluation.py` `20 passed`，`python -m test.check_retrieval_runtime_integration` 连续两次退出码 0；全量为 501 项通过、零失败（第一轮 493 项加失败节点修正 1 项与本轮新增 7 项）。`ruff` 对 16 个业务/评测文件与 14 个测试与脚本均 `All checks passed!`；`git diff --check` 零输出。逐条命令、退出码与证据位置见 `tmp/action-rerank-t5/acceptance.md` 的 D 节与 M 节。
- 尚未执行：150 条正式检索、40 个业务场景（`rebuild-v3` 默认基线）、批量云端上传、LangSmith 提交与正式成绩。解除数据审阅阻塞需要用户扩充并批准 qrels；真实重排调用数与用量以当次运行入口输出为准，单价未经账单核验。

## 11. LangSmith 外部裁判检查点（任务 3）

外部裁判只从 `scoring_mode=external` 的 LangSmith 场景运行读取证据。运行目录为 `backend/temp/scenario-langsmith/runs/<run_id>/`，每个场景必须有 `execution.json` 与 `external-scoring.json`；旧 Ragas 运行、`scoring.json`、`generation.jsonl` 不进入此流程。完整契约见 [评测契约第 13 节](evaluation-contracts.md#13-langsmith-外部裁判迁移任务-3检查点投影与上传)。

### 11.1 准备运行

外部运行仍会真实调用生成模型与检索，但不调用本地 Ragas 裁判：

```powershell
uv run --locked --directory backend python -m evaluation.langsmith_scenarios run --purpose validation --scoring-mode external
```

运行结束后 `sdk-run.json` 的状态应为 `awaiting_external_scoring` 或 `incomplete`；记录输出的 `run_id`。

### 11.2 审阅与批准适用性

```powershell
uv run --locked --directory backend python -m evaluation.generation_upload external-review --source D:/Repository/Fit-Agent/backend/temp/scenario-langsmith/runs/<run_id>
```

该命令校验运行、数据身份、执行摘要、检查点与 `external-scoring.json` 一致性，写出：

- `external-judge/applicability.review.json`：逐检查点列出场景意图、证据是否存在、参考要求、旧 Ragas 适用性；两项新指标留空等待判断。
- `external-judge/coverage-ledger.json`：expected/uploadable/missing/not_applicable/pending_review 覆盖账本。

用户逐检查点决定两项指标是否适用；不适用必须填写原因。把决定写入 `external-judge/applicability.json`：

```json
{
  "schema_version": "fit-agent-external-judge-applicability-1",
  "run_id": "<run_id>",
  "dataset": {
    "version": "rebuild-v1",
    "manifest_sha256": "<review 中的值>",
    "content_sha256": "<review 中的值>",
    "corpus_sha256": "<review 中的值>"
  },
  "entries": [
    {
      "execution_sha256": "<review 条目中的值>",
      "checkpoint": {
        "run_id": "<run_id>",
        "scenario_id": "<review 条目中的值>",
        "event_id": "<review 条目中的值>",
        "checkpoint_id": "<review 条目中的值>",
        "tool_call_id": null
      },
      "applicability": [
        {"metric": "evidence_supported_v1", "applicable": true, "reason": null},
        {"metric": "reference_requirements_met_v1", "applicable": false, "reason": "<原因>"}
      ]
    }
  ],
  "approval": {
    "status": "approved",
    "reviewer": "<用户>",
    "reviewer_type": "user",
    "reviewed_at": "<ISO 时间>",
    "comment": "<可选>",
    "content_sha256": "<见下>",
    "decision_ref": "<审阅记录路径或编号>"
  }
}
```

`entries` 必须覆盖运行内全部预期检查点，每项恰好包含两项指标；缺失、重复、未知、事件或执行摘要冲突都会被拒绝。`approval.content_sha256` 是 `{schema_version, run_id, dataset, entries(按 scenario/event/checkpoint 排序)}` 的 canonical JSON SHA-256。若不确定，先填入任意 64 位值运行 `external-project`，命令会打印期望摘要；填入后重跑。审阅清单文件本身不能直接作为批准输入。

### 11.3 离线投影与数据集同步

```powershell
uv run --locked --directory backend python -m evaluation.generation_upload external-project --source D:/Repository/Fit-Agent/backend/temp/scenario-langsmith/runs/<run_id>
uv run --locked --directory backend python -m evaluation.generation_upload external-sync --source D:/Repository/Fit-Agent/backend/temp/scenario-langsmith/runs/<run_id>
```

`external-project` 不发起网络请求，写出 `checkpoints-payload.json` 与覆盖账本，并校验：回答/上下文/请求边界、检索工具绑定、重排地址摘要、脱敏、白名单、`projection_sha256`、`row_id`。`external-sync` 需要 `backend/.env` 中的 `LANGSMITH_API_KEY`，只创建/核验检查点数据集，不运行 Evaluator；输出真实数据集名称、ID、页面地址与 `checkpoint-dataset.json` 回执。数据集名称为 `fit-agent-external-judge-<run_id>-<applicability_sha256[:12]>`。

### 11.4 配置网页 Evaluator

1. LangSmith `Settings > Integrations > Provider secrets` 选择 `TypeSafe`，保存 `TYPESAFE_API_KEY`。
2. 打开同步返回的数据集 → `Evaluators` → `+ Evaluator` → `LLM-as-a-Judge Evaluator`。
3. 模型配置选择 `TypeSafe` + Jev（`jev-latest`）；决策模型 Evaluator 只能在网页创建。
4. State 映射四个变量（网页来源分别为 Input、Output、Output、Reference output；路径为 SDK JSON 实际路径）：
   - `user_request` ← `inputs.user_request`
   - `answer` ← `outputs.answer`
   - `context` ← `outputs.context.items`
   - `reference` ← `reference_outputs.reference`
   `outputs.context.request_boundary` 与 `outputs.context.retrieval` 只用于离线审计与复核，不映射到评分 State；裁判只能看到 `outputs.context.items` 中模型实际可见的正文。
5. 两项 Noul Question 的名称使用 `evidence_supported_v1` 与 `reference_requirements_met_v1`，Instructions 使用 [评测契约 13.7](evaluation-contracts.md#137-网页-evaluator-配置) 的原文。
6. 在 Preview 中确认 `inputs` 与 `reference_outputs` 是真实数据；若面板提供 Run 预览，再确认 `answer` 与 `context` 映射非空，且 `context` 只包含上下文条目本身。数据集尚未同步或适用性尚未批准时不要启用。

### 11.5 提交实验

```powershell
uv run --locked --directory backend python -m evaluation.generation_upload external-submit --source D:/Repository/Fit-Agent/backend/temp/scenario-langsmith/runs/<run_id> --confirm-external-judge-cost
```

该命令必须已有 `checkpoint-dataset.json` 回执，并使用固定实验名提交 SDK 实验。target 只返回冻结的 answer/context，不执行业务、不调用生成模型或本地裁判。提交后校验 Example、Run inputs/outputs 与元数据，写出 `checkpoint-experiment.json`（实验 ID/名称/页面地址、逐 row 的 run_id、checkpoint、execution_sha256、projection_sha256），并保持 `external_scoring_status=pending_collection`。

`--confirm-external-judge-cost` 是费用闸门：提交后数据集 Evaluator 会对每行运行两项 Question，触发真实 Jev 调用与费用。离线检查与数据集同步不会触发评分。

同一身份重复执行只核验已有数据集/实验；回执或内容不一致直接拒绝。中断后使用 pending 标记核验并补齐缺失运行，不自动重试失败运行。反馈回收、分数聚合与发布门禁属于任务 4、5，本轮不做。

### 11.6 当前状态

2026-10-21 定向验证运行 `20261021T034031Z-b5fb95de` 已通过审阅与批准，并同步出检查点数据集：

- 名称：`fit-agent-external-judge-20261021T034031Z-b5fb95de-6cfd1f851629`
- ID：`b342ad9f-365a-4512-adab-758cf1ebd09e`
- 页面：`https://smith.langchain.com/o/9b70e395-fb97-4ba4-922f-b6246212e102/datasets/b342ad9f-365a-4512-adab-758cf1ebd09e`
- 行数：8；覆盖账本 expected 15、uploadable 8、not_applicable 7；运行 `incomplete`，覆盖不完整，只用于诊断。
- 回执：`backend/temp/scenario-langsmith/runs/20261021T034031Z-b5fb95de/external-judge/checkpoint-dataset.json`

实验已提交：`fit-agent-external-judge-20261021T034031Z-b5fb95de-1cdf82ccc3a4`，ID `c26774c1-7ba9-4afe-8bed-266590d5a53b`，页面 `https://smith.langchain.com/o/9b70e395-fb97-4ba4-922f-b6246212e102/projects/p/c26774c1-7ba9-4afe-8bed-266590d5a53b`，8 个 run 全部与 Example 绑定。首个实验已按授权删除，旧回执保留为 `checkpoint-experiment.context-limit.json`。

网页 Evaluator 已将 `context` 改为 `outputs.context.items`：5 个 run 产生真实分数（`evidence_supported_v1` 0.58–0.82，`reference_requirements_met_v1` 0.38–0.59），3 个仍报 `The model's context limit was exceeded`，对应 `s-confirmation-cancellation-state-07/cp4`、`state-05/cp2`、`state-05/cp1`，上下文 69k–77k 字符。下一步尝试把 `context` 收窄为 `outputs.context.items[*].content`（61k–66k 字符）；网页路径不支持数组通配时，需要新增紧凑评分子段。

正式 80 场景运行 `20261021T031501Z-e91b6334` 因 6 个失败样本缺少 `external-scoring.json` 被 `external-review` 拒绝，需先处理 `s-profile-update-06`、`s-plan-import-file-02`、`s-performance-adjustment-04`、`s-clarification-authorization-medical-risk-01/03/07` 的证据缺陷后重跑。

任务 4 的反馈回收入口见第 12 节。上方已提交实验可直接用于离线 `check`；真实 `collect` 尚未执行，需要用户独立授权。

## 12. LangSmith 外部裁判反馈回收（任务 4）

完整契约、绑定字段与状态口径见 [评测契约第 14 节](evaluation-contracts.md#14-langsmith-外部裁判迁移任务-4反馈回收与评分证据落盘)。本节给出可直接执行的操作顺序。

### 12.1 写入显式反馈绑定

在源运行目录创建 `external-judge/decision-feedback/feedback-binding.json`。最小结构：

```json
{
  "schema_version": "fit-agent-external-judge-feedback-binding-1",
  "run_id": "<run_id>",
  "source_sha256": "<源证据摘要>",
  "applicability_sha256": "<已批准适用性摘要>",
  "experiment": {
    "experiment_id": "<checkpoint-experiment.json 的 experiment_id>",
    "experiment_name": "<checkpoint-experiment.json 的 experiment_name>",
    "dataset_id": "<checkpoint-dataset.json 的 dataset_id>",
    "dataset_name": "<checkpoint-dataset.json 的 dataset_name>",
    "payload_sha256": "<checkpoints-payload.json 的 payload 摘要>",
    "rows_sha256": "<payload 的 rows_sha256>",
    "projections_sha256": "<payload 的 projections_sha256>"
  },
  "evaluator": {
    "provider": "TypeSafe",
    "evaluator": "<网页 Evaluator 名称>",
    "evaluator_id": "<人工声明的 Evaluator 标识>",
    "model_alias": "jev-latest",
    "config": {
      "model": "jev-latest",
      "state": {
        "user_request": "inputs.user_request",
        "answer": "outputs.answer",
        "context": "outputs.context.items",
        "reference": "reference_outputs.reference"
      }
    },
    "config_sha256": "<sha256(canonical_json(config))>"
  },
  "state_mapping": {
    "user_request": "inputs.user_request",
    "answer": "outputs.answer",
    "context": "outputs.context.items",
    "reference": "reference_outputs.reference"
  },
  "metrics": [
    {
      "metric": "evidence_supported_v1",
      "feedback_key": "evidence_supported_v1",
      "question_id": "evidence_supported_v1",
      "question": "<网页 Question 原文>",
      "question_sha256": "<sha256(canonical_json({question_id, question}))>",
      "rubric_id": null,
      "rubric": "<判断口径原文>",
      "rubric_sha256": "<sha256(canonical_json({rubric_id, rubric}))>"
    },
    {
      "metric": "reference_requirements_met_v1",
      "feedback_key": "reference_requirements_met_v1",
      "question_id": "reference_requirements_met_v1",
      "question": "<网页 Question 原文>",
      "question_sha256": "<sha256(canonical_json({question_id, question}))>",
      "rubric_id": null,
      "rubric": "<判断口径原文>",
      "rubric_sha256": "<sha256(canonical_json({rubric_id, rubric}))>"
    }
  ]
}
```

全不适用且没有上传行的运行把 `experiment` 写成 `null`，不提供 payload/rows/projections；`applicability_sha256` 仍然必填。`config` 不写入认证头、API Key、Cookie、Token 或本地路径。

来源保证口径为“显式绑定 + `feedback_source.metadata.typesafe`”软链：成功分数要求来源类型为 `model`/`evaluator` 且 `typesafe` 对象存在；云端失败要求来源类型为 `model`/`evaluator` 且 `extra.error=true`。归档固定记录 `source_assurance=declared_binding`，不宣称服务已唯一证明 Evaluator 身份。

### 12.2 离线检查

```powershell
uv run --locked --directory backend python -m evaluation.decision_feedback check --source D:/Repository/Fit-Agent/backend/temp/scenario-langsmith/runs/<run_id>
```

该命令不联网，校验源执行/评分证据、已批准适用性、磁盘 payload 与任务 3 回执、逐行投影、run/Example 映射，以及已有回收索引与批次。输出 JSON：

- `status=complete`：已有批次评分证据齐备。
- `status=pending`：尚无回收批次，或仍有待回收/未上传检查点。
- `status=blocked`：身份、摘要、来源或批次校验失败。

没有回收批次时返回 `pending` 与 `collection_absent`。

### 12.3 执行回收

```powershell
uv run --locked --directory backend python -m evaluation.decision_feedback collect --source D:/Repository/Fit-Agent/backend/temp/scenario-langsmith/runs/<run_id>
uv run --locked --directory backend python -m evaluation.decision_feedback collect --source D:/Repository/Fit-Agent/backend/temp/scenario-langsmith/runs/<run_id> --wait-seconds 60
```

`collect` 从既有实验回执读取实际 `experiment_id` 与 run 映射，读取全部根 Run 的 run-level feedback；不按实验名称搜索，不读取实验汇总反馈，不补提交实验或 Run。默认单次读取；`--wait-seconds` 只接受有限非负数，到期仍缺反馈时保存 `pending` 并结束。SDK 单次请求超时受剩余时限约束，每次读取关闭重试。

`LANGSMITH_API_KEY` 从进程环境或 `backend/.env` 读取。真实云端回收的前置条件：有效实验回执、匹配的绑定文件、用户对本次只读云端调用的独立授权。

### 12.4 输出与状态

```text
external-judge/decision-feedback/
  feedback-binding.json
  index.json
  batches/batch-<content_sha256[:16]>.json
```

- `index.json`：`latest` 指向最新批次；保存源/绑定/实验摘要与历史批次列表。
- 批次：`fit-agent-external-judge-feedback-collection-1`，包含覆盖账本、逐检查点 `FeedbackSet`、可成立的 `ScoringAttempt`、原始反馈证据、排除原因、诊断与 `evidence_sha256`。
- 重复回收相同 feedback ID 与内容复用已有批次；新反馈或内容变化写入新批次，旧批次保留。
- 同一 feedback ID 内容漂移、同一指标多个不同 feedback ID、来源缺口、非法分数、缺少云端运行、索引/批次损坏均标记 `blocked`，保留真实候选与原始证据。

退出码：

| 退出码 | 含义 |
|---|---|
| 0 | `complete`：评分证据已齐备，不表示发布通过或质量达标 |
| 2 | `pending`：仍待回收或尚未产生回收批次 |
| 3 | `blocked`：校验阻断或缺少云端配置 |
| 1 | 未捕获的 SDK/网络/系统异常 |

### 12.5 当前验证状态

- 新增 `backend/test/test_decision_feedback.py`：45 passed，退出码 0，283.08s。覆盖 0/1 分、bool/字符串/非有限数/越界分数、模型版本缺失与别名、真实云端错误结构、缺失反馈与有界等待、同 ID 去重/漂移、不同 ID 冲突、来源错误/缺口、N/A 意外反馈、全 N/A、缺失未上传检查点、身份/摘要漂移、脱敏、幂等、历史保留、索引/批次损坏与 CLI 退出码。
- 相邻非 reranker 回归：任务 1～3 54 passed；契约与汇总 48 passed；LangSmith 场景与数据集 56 passed；场景执行与评分 63 passed；各批退出码 0。
- 本轮未执行真实云端 `collect`、任何生成模型/Embedding/reranker/外部裁判模型调用、Ragas 重评分、任务 5 聚合与发布门禁。
- 本轮未运行 reranker/Cross-Encoder 专项与真实服务测试，保留现有实现与测试：`test_retrieval_rerank_evaluation.py`、`test_retrieval_rerank_stages.py`、`test_retrieval_rrf.py`、`test_retrieval_stages.py`、`test_retrieval_mteb.py`、`test_retrieval_runtime_integration.py`、`check_retrieval_rerank_production.py` 等。

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
