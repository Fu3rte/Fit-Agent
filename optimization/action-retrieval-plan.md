# 动作检索设计方案

数据结构、接口和业务边界见 [动作检索契约](action-retrieval-contract.md)。

## 1. 范围

- 支持动作核实、计划生成和动作替换，覆盖标准名称、别名、中英文表达、口语描述及带条件需求。
- 基于原始 1321 条动作，补充已审阅的中文俗称、英文名称及器械、肌群中文映射。
- 使用 Milvus 内置 BM25 与 dense 两路召回，通过 `hybrid_search` 和 `RRFRanker(k=60)` 融合排序。
- 搜索返回简要候选，选定后按 ID 获取完整说明；两类响应均限制体积。
- 器械和禁用动作限制参与硬过滤；长期限制复用画像确认保存流程。
- RRF Top-20 之后接入阿里云百炼 `qwen3.7-text-rerank` Cross-Encoder 重排，重排结果在最终 Top-5 裁剪前生效。
- 实际设施依赖补标与精确匹配优先不在范围内。`body weight` 中依赖单杠、双杠等设施的动作仍受目录标签覆盖限制。

## 2. 数据与索引

原始 Catalog 提供动作身份、事实字段和完整说明。别名、英文名称及中文映射作为补充检索数据，保留来源、审阅状态和版本；仅已审阅内容进入索引。别名允许关联多个动作，保留具体变式。

每个动作对应一条 Milvus 记录，原始字符串 ID 为主键。名称、别名、英文名称、器械、肌群及中文映射、中英文步骤组成统一文本，同时用于 BM25 和 dense 编码；器械与肌群另存结构化筛选字段。

中文分词及 sparse 向量生成由 Milvus analyzer 和 BM25 Function 完成。目录 dense 向量预计算，在线仅编码查询。目录和查询共用模型与规范化规则，dense 使用 `COSINE`。

索引清单绑定源数据哈希、补充数据版本、模型、维度、编码规则、analyzer、索引参数及服务和 SDK 版本。新集合完整校验后切换正式 alias，保留既有集合；配置不兼容、索引缺失或调用失败时明确报错。

## 3. Embedding 与部署

使用 OpenAI Python SDK 异步客户端调用阿里云百炼 `embeddings.create`：

- Base URL：`https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`。
- 模型从 `FIT_AGENT_EMBEDDING_MODEL` 读取；`dimensions=1024`、`encoding_format="float"`。实际模型与编码身份写入索引清单，切换后按兼容性规则重建。
- 目录每批最多 20 条，查询单独编码；校验返回数量、index 对齐、维度、有限值及非零范数。
- 必需配置为 `DASHSCOPE_API_KEY`、`DASHSCOPE_WORKSPACE_ID`，由后端环境读取。密钥禁止进入前端、日志、索引或 Trace。

Milvus Standalone 通过 Docker Compose 部署，使用 etcd、MinIO、内置 Woodpecker 和持久化 named volume。端口仅发布到本机回环或受控网络，常规停机保留卷。

检索配置在索引构建或检索入口加载；画像、历史浏览及有效快照确认保存继续依赖原始目录和业务存储。

## 4. 召回与排序

1. 规范化查询，结合用途、环境、显式条件及已保存画像计算硬过滤条件。
2. BM25 与 dense 使用相同过滤条件分别召回，通过 Milvus `hybrid_search` 按主键合并。
3. 固定路径顺序 `[BM25, dense]`，使用 Milvus 内置 `RRFRanker(k=60)` 按两路排名融合。
4. 按融合分数排序，取门槛与预算裁剪前的前 20 条为候选池，保留原 RRF 分数与顺序。
5. 对候选池全部候选调用重排服务，按重排分数降序得到完整排序；同分保留原 RRF 相对顺序。
6. 在重排顺序上应用 RRF 门槛、硬约束复核与完整 JSON 预算，裁剪出最多 5 条最终模型输入。

融合公式为 `S(d) = Σ 1 / (60 + rank_i(d))`，rank 从 1 开始；未进入某路召回的动作在该路贡献为零。融合分数仅表示排序依据，原 `score` 字段继续表示 RRF 分数；重排分数独立保存。

正式 baseline 为 `fusion=rrf`、`rrf_k=60`、两路深度 `100/100`、`threshold=0.0`，详见 [检索 Baseline 定义](retrieval-baseline.md)。运行证据记录实际策略及参数；历史加权融合结果保持原身份。门槛作用于 RRF 分数；零门槛不保证无答案拦截质量。

### 4.1 重排配置与协议

- 服务为阿里云百炼 DashScope HTTP 重排接口。`RERANKER_BASE_URL` 的值是完整请求地址；运行时按该地址直接请求，不拼接路径。当前部署地域为华北2（北京），地址形如 `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank`。
- 模型由 `RERANKER_MODEL` 指定，当前为 `qwen3.7-text-rerank`；密钥由 `RERANKER_API_KEY` 提供，使用 `Authorization: Bearer` 鉴权。
- 超时由 `RERANKER_TIMEOUT` 指定，未配置时使用模块常量默认值 10 秒；取值必须为有限正数。
- 必需配置为 `RERANKER_MODEL`、`RERANKER_BASE_URL`、`RERANKER_API_KEY`。缺失或空白时在检索资源初始化阶段立即失败。模块导入、画像、历史浏览及有效快照确认不要求重排服务可用。
- 请求方法为 `POST`，请求体使用官方嵌套结构：`model`、`input.query`、`input.documents`。不发送 `top_n`、`return_documents` 与 `instruct`：`top_n` 省略时返回全部候选的完整排序，`instruct` 省略时沿用服务默认问答检索指令。后续变更 `instruct` 属于新的重排实验身份。
- 官方限制：单次最多 500 个文档，单条 Query 或 Document 最多 30,000 Token，单次请求建议不超过 120,000 Token；超出单项限制返回 HTTP 400 且不截断。本项目的实际输入为最多 20 条已发布语料文本，查询与文档规范化后提交。
- 本地输入规则：查询使用 `retrieval_text.normalize()` 并限制为 4,000 字符；文档文本逐字复用 `RetrievalCorpus.records` 的 `text`，UTF-8 不超过 4,096 字节；候选超过 20 条、语料缺失或重复、文本为空或超长时在请求前明确失败。Token 级超限以服务返回的 HTTP 400 为准，失败直接传播。
- 响应 `output.results` 按 `relevance_score` 从高到低排列，分数范围 0.0 到 1.0，越高越相关，且为本次请求内的相对分数。每条结果携带输入 `documents` 的原始 `index`。实现必须校验索引唯一、在范围内且完整覆盖输入，并为每个候选保存独立 `rerank_score`。
- `qwen3.7-text-rerank` 的 `usage` 返回 `prompt_tokens` 与 `total_tokens`；响应不返回独立模型版本字段，证据记录配置的模型标识与实际 `request_id`。空候选池跳过模型调用。
- 服务失败、超时、非 2xx 与非法响应均明确传播；不自动重试，不替换模型，不降级为纯 RRF 结果。

## 5. 器械与画像

动作核实允许读取受限动作并说明真实要求；生成和替换必须遵守器械及禁用动作限制。硬过滤进入两路候选选择，输出时再次校验。

居家或户外默认仅 `body weight`，其余或未知环境默认目录器械可用。明确声明可用器械时放行对应器械；“只用某器械”形成精确筛选。例如“家里有哑铃，想练背”允许符合限制的徒手和哑铃背部候选。

临时限制只作用于对应请求。长期不可用器械复用 `unavailable_equipment`，禁用动作复用 `forbidden_exercise_ids`；修改及解除均经完整画像快照展示和按钮确认后持久化。可用声明与已保存限制冲突时澄清并确认解除。

画像页、对话画像和待确认快照共用展示组件。画像与计划各自确认；画像版本变化后，既有计划提案继续执行依据有效性及保存限制校验。

## 6. 返回与观测

搜索最多返回 5 条简要候选与 `has_more`，完整 JSON 上限 4000 字符；不暴露内部剩余候选身份。详情按 ID 批量读取，每批 1..5 条，完整 JSON 上限 7000 字符，继续保留剩余 ID。两类工具均按完整条目裁剪。

检索与重排耗时进入现有 tool Span，Embedding 与重排用量独立于聊天、摘要和模型 Span 统计。生产 Trace 继续遵循字段白名单，不新增重排属性，不记录查询原文、画像、候选、分数或凭据。评测证据在既有授权及字段白名单内保存候选 ID、RRF 分数、重排分数与请求身份。工具取消等待资源收尾；Milvus 阻塞 RPC 及在途 HTTP 请求的取消与关闭可能持续等待。

## 7. 质量约束

- 查询集覆盖标准名称、别名、英文、中英混合、描述、条件、歧义、禁用及无答案；按意图和别名家族打标签并分组报告，统一全量评测。
- 对照名称检索、BM25、dense 和混合检索，保持语料及结果预算可比，分别记录别名补充和裁剪贡献。
- 候选阶段记录 Recall@20；最终模型输入记录 Hit@5、MRR@5、nDCG@5、Precision@5；无答案误返回只按最终输入统计并保留逐例证据；标准名称检查排序退化。
- Recall@20 始终消费重排前候选池；重排只改变最终公开顺序。本次不增加重排拒答门槛，重排分数不参与无答案拦截。
- 显式硬约束违反数为零，候选数、字符预算及 JSON 结构全部合规。
- 业务覆盖动作核实、计划生成、反馈替换、确认前零提交、保存及解除、跨会话、重启、幂等与提案依据变化。
- API 和索引覆盖返回对齐、构建校验、重启复用、失败发布保护及明确失败；端到端统计覆盖全部搜索、详情、模型输入及响应耗时。

质量提升不设百分比门槛，量化评测独立于基本功能完成判定。

## 8. LangSmith + MTEB + Ragas 评测分工

LangSmith 负责统一看板、实验管理、对照及逐例分析，MTEB 负责检索指标，Ragas 负责生成指标。检索评分迁移、数据加载、证据与上传适配已完成开发验证；`backend/uv.lock` 固定 `mteb==2.26.7` 与 `jsonlines==4.0.0`。正式 150 条检索、80 个业务场景执行、云端上传、Ragas 与裁判调用尚未执行。详细操作和验收要求见 [评测方案](retrieval-evaluation-guide.md)。

- MTEB 接入真实 BM25、dense、hybrid 的检索结果及业务金标。Recall@20 消费门槛前候选池；Hit@5、MRR@5、nDCG@5、Precision@5 消费实际公开的最终输入顺序。两类阶段分别调用 MTEB。
- 重排实验使用独立身份，记录 `RERANKER_MODEL`、完整请求地址、文本版本 `retrieval-text-2026-10-14.2`、语料 sources 及 `request_id`。原 RRF baseline、冻结参数与历史实验身份保持不变；仅重排同一候选池时 Recall@20 不变，最终 @5 按重排后的公开顺序评分。新实验不重建向量索引。
- 二元相关性沿用 grade≥1。grade 为金标中的相关等级：3 高度相关、2 相关、1 弱相关、0 不相关。
- Precision@5 固定分母为 5；nDCG@5 使用 `2^grade−1` 增益。有金标空返回时四项 @5 记 0，Recall@20 保持候选阶段真实值；无答案样例单独统计误返回率。
- 硬约束、候选数、字符预算及 JSON 结构检查继续保留。MTEB 接口、依赖兼容性、默认指标口径及评分一致性已通过固定金标、边界测试和本地 payload 验证；历史候选重评分与真实全量运行尚未执行。
- 全新编写 150 条检索请求和 80 个端到端业务场景，配套重写 qrels、初始状态、参考答案、审阅标准及确定性断言，由用户最终审阅。数据独立版本化，准备要求见 [全新评测数据准备方案](evaluation-dataset-rebuild-plan.md)。`backend/dataset/evaluation/rebuild-v1/` 已冻结并通过正式加载门禁，全部记录为 approved。取消检查点按用户授权修订，见 [取消评测契约修订记录](evaluation-rebuild-v1-cancellation-amendment.md)。
- 历史量化指标、逐例结果、旧数据版本和实验身份完整存档；历史 holdout 保留 `previously_observed_holdout_regression` 身份。
- Ragas 生成评测采集真实回答和实际上下文；Faithfulness 检查上下文对回答事实的支持，FactualCorrectness 使用经审阅的参考答案核对事实正确性。动作详情及其他工具事实纳入对应回答的证据范围。
- 检索动作 ID 金标及生成参考答案分别维护；检索和生成指标分别命名，通过样例 ID、数据版本及运行证据关联。生成参考答案和审阅标准在构建生成测评集及语料库时编写。
- 裁判模型、地址与密钥分别从当前受保护配置的 `FIT_AGENT_EVAL_JUDGE_MODEL`、`FIT_AGENT_EVAL_JUDGE_BASE_URL`、`FIT_AGENT_EVAL_JUDGE_API_KEY` 读取。Ragas 调用配置所指定的裁判服务，结果记录到 LangSmith；费用不设上限。运行证据绑定实际模型及接口身份，模型可用性与兼容性按本次运行核验。
- Ragas 生成评测及 LLM 裁判作为后续需求，本次检索迁移不新增生成运行或裁判调用，并在生成评测实现时记录裁判及提示版本。
- 已授权生成评测将请求、回答、实际上下文及参考答案发送至用户当前配置的裁判服务，并将样例内容、模型输出及评分结果上传 LangSmith。生成评测实现时依照授权范围构造独立字段白名单；现有检索上传白名单、生产 Trace 隐私契约及历史评测原件保持完整，服务凭据保持本地。
- 器械限制、禁用动作、响应预算、无答案误返回及保存授权继续通过项目确定性规则检查。

## 9. 重排接入落地清单

配置、共享类型与纯校验、客户端、在线接线与评测证据均已落地，以下为各落点的实际位置与状态。

- 配置读取：`ActionRetrievalRuntime._ensure` 在检索资源初始化上下文栈中创建重排客户端；配置缺失或非法时仅检索请求失败。
- 在线管线：`ActionRetrievalRuntime.search` 取得原 RRF 召回后截取门槛前的前 20 条候选池，用 `build_request(query, pool, corpus.records)` 构造请求并执行 `await rerank.rerank(request, signal=signal)`，把候选池、重排结果与配置身份一同放入 `SearchExecution`；`BusinessService.search_exercises` 再用同一候选池与该结果调用 `build_search_stage`，按重排顺序应用门槛与预算。
- 响应阶段：`build_search_stage` 以显式关键字参数接收本次真实重排结果与配置身份，基线调用显式传入 `rerank=None` 与 `rerank_identity=None`；`SearchStage` 增加 `rerank` 与 `rerank_identity` 字段；公开 `items/has_more` 结构不变。
- 观测：`SearchObservation` 与评测 `SearchStageRecord` 增加候选 ID、RRF 分数、重排分数、重排用量及请求身份；生产 Trace 白名单不变。
- 评测：`evaluation/retrieval_support.py` 的 `project_response` 与 `evaluation/retrieval.py` 的 `collect_dataset` 绑定重排身份；Recall@20 继续读取 `candidate_pool`，最终 @5 读取公开重排顺序。`evaluation/contracts.py` 证据模型增加对应字段。
- 业务场景：`evaluation/scenarios.py` 的 `RuntimeHandle._observe_search` 按 `SearchObservation.stage` 透传重排证据并记录 `experiment`；完整覆盖、`rrf_rank` 合法范围、唯一 ID、分数降序与同分次序由 `contracts.py` 的 `RerankEvidence` 与 `SearchStageRecord` 模型校验拒绝，候选池与重排结果的跨字段一致性由 `evaluation/summary.py` 的检索证据校验与 `executed_index_identity` 的身份聚合拒绝。
- 测试：新增客户端协议、配置、失败与阶段测试；更新 `test_retrieval_stages.py`、`test_retrieval_rrf.py`、`test_retrieval_mteb.py` 及场景证据测试的重排预期。
- 不变项：`retrieval_index.py` 与索引清单不加入重排依赖；`retrieval_frozen.py` 冻结参数、正式 alias、向量索引与历史 baseline 身份保持不变；前端消费的搜索响应结构不变。
