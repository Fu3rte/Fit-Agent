## 1. 交付状态

A 部分已落地共享输入输出契约与评测依赖；任务 1 至 5 已实现两阶段检索指标计算、生产阶段构造、证据、报告与上传兼容接线、确定性业务断言引擎、Ragas 生成评分模块、冻结 40 条默认正式场景（并保留 80 条历史版本）的真实运行器与本地证据采集，以及任务 5 的本地汇总、诊断报告、生成评测显式上传和发布门禁。策略替换尚未实现；正式 baseline、正式 40 条默认场景全量评测与 LangSmith 生成评测上传尚未执行。已执行单检查点真实 Ragas 集成检查、真实裁判兼容检查、场景运行器离线业务链路测试与任务 5 离线汇总/门禁回归。文档以配置项描述模型选择，实际模型身份以各次运行原始证据为准；既有检查不代表当前新配置已经验证。

LangSmith 外部裁判迁移的任务 1 已新增独立契约：新指标身份、检查点上传结构、外部裁判反馈、裁判来源与评分尝试契约统一位于 `evaluation.contracts`，使用独立 Schema 版本；Ragas 历史契约与数据保持可读。任务 2～4 已实现运行模式、检查点上传与反馈回收；发布门禁接线仍归任务 5，详见第 11～14 节。

任务 2 已增加显式 `scoring_mode`：`ragas` 保持既有本地 Ragas 评分语义，`external` 只执行真实场景与确定性断言，生成评分保留待外部处理；两种模式使用独立协议版本、独立本地文件与独立反馈版本，详见第 12 节。外部记录尚不能被旧汇总接收，旧 `summary` 与 `sdk_summary` 遇到外部协议明确拒绝。

任务 3 已在 `generation_upload.py` 增加显式外部分支：从封存执行证据构造检查点投影，使用独立数据集与 SDK 实验提交，返回可追溯回执；反馈回收与落盘已在任务 4 落地，分数聚合与发布门禁仍归任务 5，详见第 13、14 节。第 13 节记录任务 3 的历史验证状态。

任务 4 已在 `evaluation.decision_feedback` 增加离线核验、显式绑定、云端读取、严格归一化与版本化落盘；`check` 默认不联网，真实云端回收需要有效实验回执与用户独立授权，详见第 14 节。

共享契约唯一导入路径：

```python
from evaluation.contracts import (
    RetrievalInput,
    RetrievalResult,
    SavedBusinessData,
    BusinessProjection,
    SavedDataComparison,
    StateComparison,
    GenerationCheckpoint,
    MetricOutcome,
)
```

所有契约继承 `EvaluationContract`：封闭字段、严格类型、拒绝未知字段与非有限数。动作 ID 保持原始字符串，前导零不丢失。契约模块只导入 Pydantic 与稳定业务模型，不初始化检索、数据库、网络或外部服务客户端。

## 2. 检索契约

`RetrievalInput` 区分两份有序列表：

- `candidate_pool`：硬约束后、门槛与响应预算裁剪前的前 20 条候选，最多 20 条。
- `final_model_input`：实际进入模型输入的候选，最多 5 条，保留真实可见顺序；必须是候选池的子集，允许重排。

`labels` 保存 0..3 的已审阅等级：`grade>=1` 为二元正例，`grade=0` 为已审阅负例。`expect_empty` 为真时不得包含正例，但可保留负例；有答案查询至少一个正例。`positive_labels` 属性直接给出正例集合。Recall@20 的分母是该查询全部正例数量。

`RetrievalResult` 保留查询身份、场景身份、五项指标及阶段身份：

- `recall@20`：`candidate_pool`，k=20。
- `hit@5`、`mrr@5`、`ndcg@5`、`precision@5`：`final_model_input`，k=5。

有金标空返回时只约束四项 @5 指标为 0；Recall@20 保持候选阶段独立语义，候选池为空时必须为 0。无答案查询不填排序指标，`false_return` 按最终模型输入阶段判定。Precision@5 固定分母 5；nDCG@5 使用 `2^grade-1` 增益。

`SearchResponseEvidence` 承载完整公开搜索响应 JSON 与模型可见元信息。`ModelVisibleSearch` 最多 5 条、动作 ID 唯一，只包含 `items` 与 `has_more`，不接受 `remaining_ids` 等内部候选池字段；契约同时核验公开响应与模型可见条目逐项一致。模型可见 ID 必须与参与评分的 `final_model_input` 一致。

`RerankEvidence` 承载重排实验的可离线回放数据：`mode` 为 `rrf_baseline` 或 `rerank`；重排模式要求 `identity{model,endpoint,text_version}`、完整 `scored`、`usage` 与 `request_id`。`scored` 每项保存 `id`、`rerank_score` 与从 0 开始的原 RRF `rrf_rank`，按分数降序排列，同分保持原 RRF 次序；空候选池必须记录零请求与空 `request_id`。基线模式不得携带重排字段，历史缺少该证据的投影按显式基线解释。

评测编排层不信任投影中的 `budget_compliant` 与 `hard_constraint_violations` 标记：按生产序列化重算完整公开响应字符数，校验固定预算与保存的长度字段；从已批准查询和真实目录重建条件，重做整个候选池的硬约束；再用共享阶段构造核对门槛、裁剪和最终响应。

## 3. 事件、投影与断言契约

`EvidenceLink` 以 `run_id`、`scenario_id`、`event_id`、`checkpoint_id` 关联证据；工具事实通过 `ToolFact` 或 `ContextItem.tool_call_id` 绑定实际工具调用。

`SavedBusinessData` 表示已保存业务内容：

- `profile`、`plans`、`workouts` 直接承载现有完整业务记录。
- `save_records` 承载保存幂等记录与保存时间。
- `profile=null` 表示记录不存在；`profile` 存在但 `content` 全空表示记录存在但内容为空。两者可区分。
- 列表保留业务内容内部顺序；计划最多一个 `is_current`。

`BusinessProjection` 在已保存内容之外增加待确认提案、快照状态、展示与确认绑定、确认操作账本及取消结果。确认结果使用现有 `SaveConfirmationOperation`，取消结果使用 `PendingProposalCancellation`。

两种比较范围：

- `SavedDataComparison`（`saved_data_unchanged`）：只比较已保存内容、记录 ID、版本、`is_current` 与保存元数据。
- `StateComparison`（`state_unchanged`）：比较完整 `BusinessProjection`，包含提案、依据、快照状态与展示/确认绑定。

两者的 `business_kind` 均为 `profile`、`plan`、`workout` 或 `all`。会话消息、Trace、耗时与 tokens 不进入契约。

`AssertionOutcome` 使用封闭的 `AssertionKind`，覆盖当前冻结 Schema 的全部 11 种断言。`snapshot_integrity` 与 `save_authorization` 必须提供 `ProposalBinding`，绑定 `proposal_ref`、实际提案、展示节点、确认入口与确认操作；其他断言不接受提案绑定。`state_unchanged` 与 `saved_data_unchanged` 必须携带 `comparison`，即对应的 `StateComparison` 或 `SavedDataComparison`；其他断言不接受比较证据。契约不执行任意表达式或代码。

## 4. 生成评分契约

`GenerationCheckpoint` 按回答检查点独立保存：

- `user_input`：实际用户输入与必要真实前文。
- `answer`：实际用户可见回答与结构化快照。
- `model_input`：该回答生成前实际进入模型输入的上下文。
- `reference`：审阅参考答案、事实、必须包含项、允许变化与禁止断言。
- `applicability`：`faithfulness` 与 `factual_correctness` 的逐指标适用性。

`ContextItem` 保留来源、事实来源、工具调用关联与裁剪信息。`fact_origin=user` 与 `fact_origin=tool` 可区分用户提供事实与工具事实；工具结果必须绑定 `tool_call_id` 与工具名称。`reference` 只作为评估器输入，不进入 `model_input`；不得从候选池补造模型未看到的上下文。

`GenerationCheckpoint.ragas_input()` 只做字段映射，输出 `RagasInput(answer, contexts, reference)`。生成指标适用时必须提供 reference；两项均不适用时不得携带 reference。纯确认、取消或状态检查点的适用性以冻结数据为准，`deterministic_rules` 由确定性断言模块负责。

## 5. 结果、失败与身份

`MetricOutcome` 统一表达四种状态：

- `applicable`：已声明适用、尚无结果；不得携带分数或错误。
- `succeeded`：必须提供 0..1 的真实分数，不得携带错误。
- `failed`：不得使用分数，必须保留错误依据。
- `not_applicable`：不得使用分数或错误，必须保留原因。

零分只表示真实成功评分为 0，不能冒充失败或不适用。本契约不设生成分数通过门槛。

`CheckpointResult` 与 `ScenarioResult` 保留逐检查点结果与场景身份。汇总语义为“先按场景聚合”，聚合实现不属于 A 部分。

`evaluation.generation_scoring` 为每项成功生成指标输出 `GenerationMetricEvidence`：检查点身份、指标名称与类、实际生效参数、Ragas 与适配依赖版本、裁判模型及非敏感配置、真实 prompt 指纹、实际发送的评分输入、上下文来源/裁剪/工具绑定、Ragas 回调链步骤与裁判调用原文。证据不包含 API Key、认证头或其他受保护配置值。裁判调用失败、超时、取消或非法评分不会被改写为成功或 `not_applicable`；异常原样传播，任务 4 使用 `failed_outcome()` 生成 `failed` 结果并保留已经产生的真实指标结果与证据。

`RunIdentity` 绑定：

- 数据版本、manifest/内容/语料摘要。
- 实际代码提交、锁文件摘要与工作区状态。
- 检索策略、模型、编码规则版本与查询向量身份属于配置身份；物理索引身份（集合名、构建清单摘要、build_id、兼容性清单）只由实际执行检索的调用产生，逐次写入 `<scenario>/retrieval.jsonl`。一次运行出现多个物理索引身份时登记 `retrieval.index_conflict` 并进入 `missing`，运行保持 incomplete；未执行真实检索时索引身份记为缺失，禁止用运行结束后的新客户端补填。
- 生成模型与提示版本。
- 裁判模型（由当前 `FIT_AGENT_EVAL_JUDGE_MODEL` 显式指定）、裁判提示与指标版本。

凭据、API Key 与受保护配置不进入契约。`EvaluationRun.blocking_issues()` 将覆盖不完整、执行失败和仍为 `applicable` 的指标列为阻塞项；发布器与上传白名单不在本部分实现。

### 5.1 正式数据版本基线

场景数量与类别配额按已发布版本固定，加载器同时校验实际数量、实际类别分布、manifest 数量、manifest 配额与该版本基线；只信任 manifest 或任意缩小数据不能通过正式批准检查。

| 版本 | 场景数 | 类别配额 |
|---|---:|---|
| `rebuild-v1` | 80 | 10 类各 8 |
| `rebuild-v2` | 80 | 10 类各 8 |
| `rebuild-v3` | 40 | 8 / 5 / 4 / 2 / 4 / 3 / 3 / 3 / 5 / 3 |

`rebuild-v3` 类别顺序为 `action_verification`、`profile_update`、`plan_import_file`、`incomplete_plan_structure`、`plan_generation`、`replacement_constraints`、`performance_adjustment`、`workout_logging_query`、`confirmation_cancellation_state`、`clarification_authorization_medical_risk`。查询配额保持 150 条与 `lookup=60`、`generation=45`、`replacement=45`，各含 10 条 `expect_empty`。

`rebuild-v3` 继承 `rebuild-v2` 的 corpus、queries、qrels、附件与场景正文/审阅记录，只筛选 40 条场景并以自身数据身份发布。`rebuild-v1`、`rebuild-v2` 保持原 80 条基线与原文件；显式指定旧版本时继续按旧版本读取、校验、选择与计算 80 条分母。未知版本名沿用历史 80 条全量规则，避免未登记数据绕过正式门禁。

默认数据版本为 `rebuild-v3`：`evaluation.scenarios`、`evaluation.langsmith_scenarios` 的 `sync`/`run`、`evaluation.generation_upload` 外部命令与 `evaluation.decision_feedback` 共用同一默认常量。正式运行仍要求覆盖该版本全部批准场景；定向验收保持 6 条并包含固定取消场景 05、07、08。历史 80 条运行结果不重标为新版本结果。

## 6. 三个后续模块的独占边界

三个模块只依赖共享契约、稳定业务模型和各自的成熟库。模块之间不得互相导入、调用或消费对方输出。

### 6.1 检索指标计算

- 独占文件：`backend/evaluation/retrieval_metrics.py`、`backend/test/test_retrieval_mteb.py`、`backend/test/test_retrieval_stages.py`。
- 导入路径：`from evaluation.contracts import RetrievalInput, RetrievalResult, SearchResponseEvidence`。
- 输入：`RetrievalInput`。
- 输出：`RetrievalResult`；需要证据时使用 `SearchResponseEvidence`。
- 可共用依赖：`evaluation.contracts`、`mteb==2.26.7`。
- 阶段边界：正式入口分别调用 MTEB 计算候选池 Recall@20 与最终输入四项 @5；不得把同一份最终排名同时用于 k=5 与 k=20。无答案误返回只按最终模型输入判断。历史入口保留单阶段 @5 身份。

### 6.2 确定性业务断言

- 独占文件：`backend/evaluation/deterministic_assertions.py`、`backend/test/test_deterministic_assertions.py`。
- 导入路径：

```python
from evaluation.deterministic_assertions import (
    AssertionEvidence,
    AssertionInputError,
    BusinessContentEvidence,
    ContextEvidence,
    DisplayContentEvidence,
    EventTarget,
    PlanSnapshotEvidence,
    ProfileSnapshotEvidence,
    RankingEvidence,
    ToolResultEvidence,
    WorkoutSnapshotEvidence,
    evaluate_assertion,
    evaluate_checkpoint,
    parse_assertion,
)
from evaluation.contracts import AssertionOutcome, EvidenceLink
```

公开入口与参数：

- `parse_assertion(value: Assertion | Mapping[str, Any]) -> Assertion`：把冻结场景中的断言对象解析为 11 种封闭类型之一；未知 `kind`、未知字段、非法业务类型由 Pydantic 直接报错。
- `evaluate_assertion(assertion, evidence: AssertionEvidence) -> AssertionOutcome`：单条断言判定。
- `evaluate_checkpoint(assertions: Sequence[Assertion | Mapping], evidence: AssertionEvidence) -> list[AssertionOutcome]`：按输入顺序执行同一检查点的全部断言，同类多条与重复定义完整保留。
- 所有输出保留 `EvidenceLink` 的 `run_id`、`scenario_id`、`event_id`、`checkpoint_id`；断言定义与证据的检查点身份必须一致。

`AssertionEvidence` 是封闭输入，字段职责：

- `checkpoint`、`event_type`、`event_target`：当前检查点身份、事件类型与真实事件目标。
- `before`、`after`：事件执行前后的完整 `BusinessProjection`。
- `proposal_bindings`：`proposal_ref` 到实际 `ProposalBinding` 的唯一映射。
- `profile_snapshots`、`plan_snapshots`、`workout_snapshots`：按业务类型分组的真实准备快照。
- `display_contents`：准备工具实际返回并绑定到展示节点的完整内容。
- `business_contents`、`parameter_target`：当前检查点实际待提交或展示的业务 payload 及其来源绑定。
- `context_evidence`、`tool_results`、`rankings`：模型输入上下文、真实工具结果与真实检索可见条目。每条 `rankings` 绑定唯一 `tool_call_id`、`tool_name` 与 `SearchResponseEvidence`；`SearchResponseEvidence` 复用 `ModelVisibleSearch`，单次搜索最多 5 条。

`AssertionInputError` 只用于输入或证据结构错误。业务对象缺失、内容漂移或条件未满足输出 `passed=false`，不通过异常或默认值改变判定。

11 种断言的证据与业务对象：

- `state_unchanged`：比较 `before`/`after` 的完整 `BusinessProjection`。`business_kind=profile|plan|workout` 时保留该类型画像或记录、提案、确认操作与取消记录；`all` 比较三类全部内容。比较包含业务内容、依据、展示与确认绑定、确认账本、取消记录、记录 ID、版本、保存时间与 `is_current`。会话消息、Trace、tokens 与耗时不进入比较。
- `saved_data_unchanged`：只比较 `SavedBusinessData` 中的已保存画像、计划、训练记录与保存幂等记录。待确认提案、展示绑定与确认绑定不参与比较。生成新提案的消息检查点使用该断言验证已保存内容未变。
- `equipment_restriction`：读取 `business_contents` 的实际 `PlanContent` 或 `WorkoutContent`，按动作 ID 查询真实 `Catalog.equipment`，再按 `allowed_equipment` 与 `forbidden_equipment` 判定。空动作列表、空 `exercise_id`、目录未知 ID 与目录外器械声明均形成失败或输入错误，不采信模型写出的器械名称。`allowed_equipment` 为空表示没有允许器械；`forbidden_equipment` 为空表示没有额外禁止器械。
- `forbidden_exercise`：完整遍历当前检查点绑定的计划各天与训练记录动作 ID。每个动作 ID 必须非空且存在于真实 `Catalog`；空 ID 与目录外 ID 形成明确失败证据。命中 `exercise_ids` 任一 ID 即失败。`business_contents` 必须绑定当前事件目标，历史无关计划与隐藏候选不得混入。
- `parameter_preserved`：只读取唯一的 `parameter_target`。JSON Pointer 根对象是该目标业务 payload（`ProfileContent`、`PlanContent` 或 `WorkoutContent`），例如 `/goal`、`/days/0/exercises/0/sets`、`/days/3/kind`、`/exercises/0/sets/2/reps`。解析使用 `jsonpointer` 库；非法语法抛错，成员缺失、数组越界与值为 `null` 分别形成带定位证据的失败。比较保持严格值语义，布尔值不与整数等同。
- `snapshot_integrity`：通过 `proposal_ref` 定位唯一 `ProposalBinding`，再核对准备快照 payload、检查点快照、完整展示内容、当前提案或保存记录。比较完整 payload、内部列表顺序、基础画像版本、基础计划/训练记录身份、准备类型、展示绑定与确认绑定。展示内容缺字段、payload 漂移、展示节点错配或确认前新增事实改写快照均失败。
- `save_authorization`：只检查 `proposal_ref` 绑定的目标提案及其保存链路。`expected_saved=false` 在消息检查点验证目标未提交，其他已保存记录不影响目标身份。`expected_saved=true` 必须绑定 `confirm` 事件，并要求真实保存记录、确认操作账本、展示节点与确认入口逐项一致。保存结果正文必须等于准备快照 payload，保存后的画像/计划/训练投影必须与保存结果一致；训练记录同时核对 `performed_on` 与训练记录身份。普通文字确认不构成按钮授权。
- `state_transition`：通过事件目标与 `proposal_ref` 绑定同一目标，比较 `before`/`after` 中该目标的真实状态。`from_status=null` 表示检查点前不存在。`saved` 必须发生在 `confirm` 检查点并存在保存记录。`invalidated` 必须发生在消息或取消事件检查点，并要求唯一真实取消证据与目标一致；消息检查点的目标来自场景中明确的后续 `cancel` 事件目标。取消证据必须匹配当前会话、助手运行、请求节点、来源助手节点、结果节点与 `tool_call_id`。`from_status=invalidated` 的后置检查同时要求事件前后均存在目标取消记录。重复确认保持同一目标身份。
- `evidence_present`：按 `evidence_kind` 从当前检查点实际证据集合重算数量。`context` 以真实上下文条目计数；`tool_result` 以真实工具调用结果计数，未裁剪上下文必须与工具正文逐字一致，裁剪上下文必须满足 `original_chars = len(工具正文)`、`omitted_chars = original_chars - len(上下文正文)` 且上下文正文为工具正文前缀；`snapshot` 以真实准备快照计数；`ranking` 逐条绑定真实 `search_exercises` 工具调用、当前上下文与 `SearchResponseEvidence`，从 `model_visible.items` 重算数量，单次搜索最多 5 条，隐藏候选不计入。同一工具调用与同一 `query_id` 不得重复计数。reference、预期回答与占位条目不得补足数量。
- `no_saved_state`：检查 `after` 中对应业务类别是否存在已保存记录。画像不存在与画像记录存在但内容全空分别判定；空内容画像记录仍属于记录存在。该断言不用于表达新提案尚未保存。
- `saved_state`：检查 `after` 中对应业务类别是否存在已保存记录。待确认提案与模型声称保存成功不构成保存记录。

比较范围与顺序：

- 两种不变性断言均支持 `business_kind=all`；单一业务类型只保留该类型的相关数据。
- 投影集合保留原始列表顺序，不排序、不按 `kind` 或 `checkpoint_id` 去重或覆盖。
- `state_unchanged` 的 `comparison` 为 `StateComparison`；`saved_data_unchanged` 的 `comparison` 为 `SavedDataComparison`。
- `snapshot_integrity` 与 `save_authorization` 的输出携带对应 `ProposalBinding`；其他断言不接受提案绑定或比较证据。

任务 4 必须采集并写入 `AssertionEvidence` 的证据：

- 每个事件执行前后各读取一次完整 `BusinessProjection`，保留记录 ID、版本、`is_current`、保存时间、提案状态、确认操作与取消结果。
- 从真实确认链路生成 `proposal_ref -> ProposalBinding`：来源用户消息事件决定 `proposal_ref`，真实快照、展示节点、确认节点与确认操作提供实际 UUID。不得猜测 UUID、生成新 UUID 或用最近记录补齐。
- 准备事件检查点采集准备快照、准备工具完整返回内容、展示节点 ID 与当前提案。
- 确认检查点采集确认事件目标、确认操作账本、目标保存记录、保存后的快照状态与准备快照。`save_authorization` 即使所在检查点没有 `snapshot_integrity` 断言，也必须采集准备快照以核对保存正文。
- 取消检查点采集取消事件目标、失效快照、真实取消结果与事件身份；消息检查点同时采集该消息回合的成功取消证据。取消证据必须可由 `business_kind`、`proposal_id`、会话、助手运行、请求节点、来源节点、结果节点与 `tool_call_id` 唯一定位。
- 查询与生成检查点采集当前检查点实际模型输入上下文、工具调用结果与模型可见检索证据。`ranking` 每条证据包含 `tool_call_id`、`tool_name` 与对应 `SearchResponseEvidence`；`tool_result` 证据包含与工具正文一致的上下文条目。
- 所有证据条目的 `run_id`、`scenario_id`、`event_id`、`checkpoint_id` 必须与当前检查点一致；跨场景、跨事件或跨检查点证据直接报错。

错误边界：

- 未知断言类型、未知字段、非法业务类型、非法状态、重复 ID、非法 JSON Pointer 语法由 Pydantic 封闭模型拒绝。
- 断言定义与证据检查点不一致、证据跨场景、`proposal_ref` 缺少绑定或存在重复绑定、事件目标与断言引用不一致、消息检查点携带确认链路、指针在目标 payload 上非法索引均抛 `AssertionInputError`。
- `tool_result` 与 `ranking` 证据要求工具调用 ID/名称、工具正文、当前上下文正文与检索响应逐项一致；同 ID 同名称但正文冲突、排名证据脱离真实工具响应或上下文、同一来源重复出现均抛 `AssertionInputError`。
- 不执行任意表达式或代码，不进行宽松类型转换、吞错或 fallback。

已验证与待验证：

- 已执行：模块导入、Ruff、`compileall`，以及 `pytest test/test_deterministic_assertions.py -q`，28 passed。
- 未执行：完整业务测试套件、正式 150 查询检索、80 场景真实 Agent、Ragas 评分与 LangSmith 上传。

### 6.3 Ragas 生成评分

- 独占文件：`backend/evaluation/generation_scoring.py`、`backend/test/test_generation_scoring.py`、`backend/test/check_generation_scoring_integration.py`。
- 依赖：`evaluation.contracts`、`ragas==0.3.1`、`ragas.metrics.Faithfulness`、`ragas.metrics.FactualCorrectness`、`ragas.llms.LangchainLLMWrapper`、`langchain-openai`（Ragas 适配链已锁定依赖）与 `openai==3.13.0`。未新增自研评分算法或依赖。
- 公开接口：

```python
from evaluation.generation_scoring import (
    GenerationScoringSession,
    JudgeConfig,
    build_metric_evidence,
    build_metric_plan,
    failed_outcome,
    load_judge_model,
    prompt_fingerprints,
    run_judge_identity,
    score_checkpoint,
    score_checkpoint_with_evidence,
    validate_score,
    validate_timeout,
)

async def score_checkpoint(
    checkpoint: GenerationCheckpoint,
    judge: JudgeConfig | None = None,
    *,
    timeout: float | None = None,
) -> CheckpointResult

async def score_checkpoint_with_evidence(
    checkpoint: GenerationCheckpoint,
    judge: JudgeConfig | None = None,
    *,
    timeout: float | None = None,
) -> GenerationScoringResult
```

`CheckpointResult.outcomes` 按 `faithfulness`、`factual_correctness` 固定顺序逐项给出 `MetricOutcome`。`GenerationScoringResult` 同时携带逐指标 `GenerationMetricEvidence`。

会话与部分完成：

```python
async with GenerationScoringSession(judge, timeout=timeout) as session:
    result = await session.score(checkpoint)
    # session.outcomes / session.evidence 保留已经完成的真实结果
```

`GenerationScoringSession` 惰性创建客户端，并在 `__aexit__`、显式 `aclose()`、超时与取消路径关闭 HTTP 与 SDK 资源。两项指标共享同一 `JudgeConfig` 与同一个 Ragas LLM 包装器；后续指标抛错时，先前成功指标的结果与证据已经保存在会话中，不会被覆盖。

失败尝试交接：每次适用指标在调用前都会创建并登记 `ScoringAttempt`，可通过 `session.current_attempt` 与 `session.attempts` 访问。`ScoringAttemptSnapshot` 保留该次尝试的实际 `scoring_input`、上下文、prompt 指纹与摘要、`judge_calls`、`chain_steps`，以及 `status`、`score`、`error`。超时、取消、传输失败、解析失败或非法评分时，异常原样传播，尝试状态标记为 `failed`，已产生的回调记录仍可读取；`attempt.to_evidence()` 可构造带 error 的失败证据。此前成功的 `session.outcomes` 与 `session.evidence` 保持不变，禁止伪造完整成功结果。

输入映射：

- `faithfulness`：`user_input=checkpoint.user_input.message`，`response=checkpoint.answer.visible_text`，`retrieved_contexts=[item.content for item in checkpoint.model_input.items]`，保持原顺序与真实裁剪后的内容；该样本不携带 `reference`。
- `factual_correctness`：`response=checkpoint.answer.visible_text`，`reference=checkpoint.reference.answer`；该样本不携带用户输入或上下文。
- 两项输入均通过 `checkpoint.ragas_input()` 取得回答、上下文与经审阅 reference；不重写回答口径，不补入 rubric、预期回答、候选池、后续工具结果或已被裁剪的原文。结构化快照继续保留在原始检查点中。

适用性：

- 两项均不适用时直接返回两项 `not_applicable`，逐项保留冻结 reason；该路径不要求 reference、裁判配置或 timeout，不创建客户端，也不调用 `ragas_input()`。
- 仅一项适用时只执行该项；另一项保留 `not_applicable` 与 reason。不得根据回答风格推翻冻结适用性。
- 缺输入、超时、取消、解析失败或非法评分不会被改写为 `not_applicable`；异常原样向调用者传播。

裁判接入：

- `load_judge_model()` 只在运行时读取 `FIT_AGENT_EVAL_JUDGE_MODEL`，模块导入不读取配置、不创建客户端。模型支持范围由当前代码契约校验，文档不固定模型名称列表。模型切换只能由显式配置完成，失败后禁止自动切换；旧运行的原始证据与身份保持不变。
- 裁判服务地址与密钥分别从受保护配置 `FIT_AGENT_EVAL_JUDGE_BASE_URL`、`FIT_AGENT_EVAL_JUDGE_API_KEY` 读取，由调用者构造 `JudgeConfig(model, base_url, api_key)`。不得猜测地址或借用其他服务凭据。每次配置切换后核验当前模型契约、接口与凭据兼容性，真实调用结果以该次运行证据为准。
- `run_judge_identity()` 从真实指标证据读取 `judge_model`；同一运行的证据出现多个裁判模型身份时拒绝生成身份。`RunIdentity` 记录实际的 `judge.model`、`judge_attempt_model` 与 `judge_same_as_generation`；`judge_same_as_generation` 在身份缺失时为 `null`，不得把“未记录”写成“不同模型”。
- `JudgeConfig` 在配置边界拒绝含 userinfo、query 或 fragment 的地址；证据中的 `judge_base_url` 统一经 `sanitize_endpoint()` 移除 userinfo、query 与 fragment，避免地址内嵌凭据进入可序列化证据。
- 客户端使用 `ChatOpenAI` + `LangchainLLMWrapper`，显式传入模型、Base URL、API Key 与 timeout；SDK `max_retries=0`，Ragas `RunConfig` 保留内置 `max_retries=10`、`max_wait=60`、`max_workers=16`，适配 OpenAI 后实际重试异常类型为 `RateLimitError`。不增加自研重试、降级或模型切换。
- timeout 必须为显式有限正数；`single_turn_ascore(timeout=...)` 提供既有异步超时语义，取消异常不被吞掉。评分边界用 `validate_score()` 拒绝 NaN、Infinity、布尔值、越界值与非法类型；不取整、不夹取，真实 0 分保留为成功评分。

证据与身份：

- `prompt_fingerprints` 直接读取 `metric.get_prompts()` 的实际 instruction、examples、输入/输出 Schema 与语言，生成稳定 SHA-256；`prompt_digest` 由实际指纹组合，不使用手写版本号。
- `metric_parameters` 记录实际生效参数：Faithfulness `max_retries`；FactualCorrectness 锁定默认 `mode=f1`、`beta=1.0`、`atomicity=low`、`coverage=low`、`language=english`。
- `dependency_versions` 记录 `ragas`、`langchain-core`、`langchain-openai` 与 `openai` 的实际版本。
- `scoring_input` 记录该指标实际发送的回答、上下文、用户输入或 reference；`contexts` 对 Faithfulness 保留完整 `ContextItem` 来源、`fact_origin`、工具调用绑定与裁剪信息。
- `judge_calls` 由 LangChain 回调记录实际发送的 prompt 与原始输出；`chain_steps` 由 Ragas `RagasTracer` 记录真实声明拆分、NLI 判定与最终分数。证据不记录 API Key、认证头或受保护配置值。
- 评分边界在首次创建客户端前调用 `ensure_ragas_tracking_disabled()`：覆盖 `RAGAS_DO_NOT_TRACK=true`、清除 Ragas `do_not_track` 缓存并校验官方检查返回 True；无法保证时抛 `RagasTelemetryError` 拒绝评分。生产 Trace 隐私契约与既有上传白名单保持不变。
- `run_judge_identity()` 按指标去重相同的 `metric_version`；同一指标出现不同 `metric_version` 或 `prompt_digest` 时拒绝生成身份，避免跨检查点重复证据改变版本身份。
- `GenerationScoringResult` 逐指标核对 `succeeded` 结果的分数与证据分数，拒绝带 `error`、缺少分数或分数冲突的成功证据。

任务 4 交接：

- 传入 `GenerationCheckpoint`、`JudgeConfig` 与显式 timeout；接收 `CheckpointResult` 与逐指标 `GenerationMetricEvidence`。
- 使用 `GenerationScoringSession` 逐检查点运行；某指标抛错时，用 `failed_outcome(metric, error)` 写入 failed，保留 `session.evidence` 中此前真实结果，禁止伪造完整成功列表。
- 失败尝试通过 `session.current_attempt` 或 `session.attempts` 读取 `ScoringAttemptSnapshot`；快照包含实际评分输入、上下文、prompt 指纹与摘要、已产生的 `judge_calls`/`chain_steps`、`status`、`score` 与 `error`。需要落盘失败证据时使用 `attempt.to_evidence()`。
- 将 evidence 写入 `backend/temp/` 等既有 gitignore 目录；聚合、发布门禁与上传由任务 5 负责。
- 真实服务集成检查通过 `FIT_AGENT_GENERATION_SCORING_INTEGRATION=1` 显式启用，默认测试收集不会加载本地密钥或发起裁判调用。
- 已执行：模块导入、Ruff、`compileall`、`pytest test/test_generation_scoring.py -q`，65 passed；真实裁判集成检查通过，两项指标均 `succeeded`，评分 1.0/1.0，裁判调用 2/4 次，超时/取消资源释放通过，样例动作事实来自真实 Catalog `0001`。未执行：正式 80 场景、完整业务套件、全量检索与 LangSmith 上传。

### 6.4 冻结场景运行器与真实证据采集

- 独占文件：`backend/evaluation/scenarios.py`、`backend/evaluation/scenario_evidence.py`、`backend/test/test_evaluation_scenarios.py`、`backend/test/check_evaluation_scenarios_integration.py`。
- 依赖：`evaluation.contracts`、`evaluation.dataset.load_for_evaluation`、`evaluation.deterministic_assertions.evaluate_checkpoint`、`evaluation.generation_scoring.GenerationScoringSession`、真实生产服务与 SQLite 仓储。

公开入口：

```python
from evaluation.scenarios import (
    EvaluationRunState,
    RuntimeHandle,
    build_execution_plan,
    run_scenario,
    run_selected,
    scenario_plan,
    select_scenarios,
)

def select_scenarios(dataset: EvaluationDataset, scenario_ids: Sequence[str] | None) -> list[dict]
def build_execution_plan(dataset, scenarios, run_id: str) -> ExecutionPlan
async def run_scenario(dataset, scenario, *, run_id, root, enable_model,
                       judge, judge_timeout) -> ScenarioArtifact
async def run_selected(dataset, scenarios, *, run_id, root, enable_model,
                       judge, judge_timeout) -> RunSummary
```

CLI：

```powershell
uv run python -m evaluation.scenarios --all
uv run python -m evaluation.scenarios --scenario-id s-plan-generation-01
uv run python -m evaluation.scenarios --scenario-id s-plan-generation-01 --enable-model
```

默认入口只执行 `load_for_evaluation`、选择范围与计划展示，不读取密钥、不创建模型或检索客户端、不发起外呼。`--enable-model` 才装配真实 Agent、Embedding 与裁判调用。

真实链路装配：

- 运行前以 `load_for_evaluation` 校验冻结门禁；默认数据身份为 `rebuild-v3`，显式 `--version-dir` 可指定 `rebuild-v1`/`rebuild-v2` 并按各自基线校验。
- 每个场景使用独立根目录 `backend/temp/evaluation-scenarios/<run_id>/<scenario_id>/`，数据库为根目录下 `fit-agent.db`，附件与文件工具根为根目录下 `tmp/`。
- 复用 `open_database`、`SqliteSessionRepository`、`SessionService`、`SqliteBusinessRepository`、`Catalog.load`、`BusinessService`、`ConfirmationService`、`SteeringCoordinator`、`admit_run`、`coordinate`、`execute` 与真实工具注册。宿主根、数据库路径与可信时钟通过构造参数注入；默认值保持生产行为。
- 初始化阶段按冻结 `initial_state` 装载画像、计划与训练记录，保留原始 ID、版本、创建/更新时间与 `is_current`；画像不存在时不写记录，画像存在但内容全空时写入 `version=1` 的空内容记录。训练记录按真实 `insert_workout`/`update_workout` 语义逐版本装载。
- 初始化后读取完整 `BusinessProjection` 与冻结状态核对。`initial_state.now` 转换为毫秒时间戳作为首个事件的可信业务时钟，后续事件按序递增 1 ms；运行 deadline 与实际耗时仍使用生产单调时钟。
- `input.prior_messages` 作为真实历史消息节点装载，保留角色与顺序；`input.attachments` 只在首个事件未声明附件时使用；`input.message` 不单独发送，实际发送严格按 `events` 的 `message` 事件执行。

事件执行：

- `message`：`SessionService.accept_send` + `admit_run` + 真实 Agent 循环。附件以冻结 asset 的原始文件名、字节与 SHA-256 校验后进入真实附件流程。
- `confirm`：`ConfirmationService.confirm`，使用真实 `proposal_id` 与 `display_entry_id`；同一目标的重复确认复用同一 `operation_id`，保留生产幂等账本语义。
- `cancel`：生产取消入口要求同一场景中更早的真实用户消息节点与包含 `cancel_pending_proposal` 工具调用的真实助手消息。运行器把 `cancel` 事件作为取消后状态检查点，使用该事件自身的真实 `before`/`after`；取消证据只来自成功的 `cancel_pending_proposal` 工具结果。未发现真实工具调用时以 `ScenarioBlocked` 停止，不手工补造工具调用。
- `session_switch`：`SessionService.create_session_result` 创建并切换真实新会话，业务数据库不复制；投影按场景内全部会话聚合读取。
- `restart`：关闭检索运行时、执行器与数据库连接，再用同一路径重新打开、执行真实恢复流程并重建服务；后续读取来自持久化数据库。

模型请求证据：

- `RunState.observation_factory` 为显式装配点，默认 `None` 时沿用生产 `CacheObserver`。
- `EvaluationObserver` 继承生产 `CacheObserver`，保留原有指纹链与故障隔离，在协议最终投影 `observe(request, model)` 处把真实请求写入隔离目录 `model-calls/`；请求只观测、不改变发送内容或执行结果。
- 全文采集前执行凭据检查，命中受保护凭据时拒绝落盘并向上失败；不记录认证头、API Key 或带凭据端点。
- 回答检查点以 `message_end` 的 `entry_id` 绑定同一 `message_id` 的真实模型调用，`GenerationCheckpoint.model_input` 只取该回答生成前的最终请求，不拼接后续工具结果。

断言与评分接线：

- 每个事件前后各读取一次完整 `BusinessProjection`；训练记录按 `page_size=100` 分页读取全部记录。集合顺序沿用仓储读取约定，场景内会话按切换顺序聚合。
- 逐检查点构造 `AssertionEvidence`，直接调用 `evaluate_checkpoint`；`proposal_ref` 由真实快照的 `request_entry_id` 映射到触发事件，展示内容、确认入口与确认操作均取自真实节点与账本。
- 消息与 `cancel` 检查点分别使用各自事件自身的真实前后边界。取消证据只来自成功的 `cancel_pending_proposal` 工具结果，按（会话、来源助手节点、`tool_call_id`）复合身份登记目标提案、返回的 `PendingProposalCancellation`、请求/来源/结果节点身份。来源助手沿结果节点的真实父链定位，每份结果绑定自己的发起节点，并校验该助手工具调用的 `business_kind`、`proposal_id` 与返回目标一致；`run_id` 缺失的节点不承担取消身份，来源与结果不同运行时不成立。工具调用 ID 无跨调用唯一性：跨会话同名调用、同会话跨运行同名调用以及同一运行内压缩后复用同名调用各自独立保留，完整业务投影的 `cancellations` 与取消证据逐会话全部条目一致。驱动事件匹配限定到证据的会话与运行，同名调用属于其它运行或会话时不得关联。`invalidated` 快照只作为状态事实，不单独构成取消证据。
- 真实取消在消息事件完成时，消息检查点使用该消息自身的 `before`/`after` 验证 `pending→invalidated`；后置 `cancel` 事件使用自身 `invalidated→invalidated` 边界并保留同一份真实取消证据。两个检查点分别读取自己的前后投影，禁止借用驱动事件的 `before`。消息检查点缺少目标、证据目标不匹配、证据来自其它运行、证据重复或事件身份缺失时，断言失败或场景阻断。
- `GenerationCheckpoint` 的 reference 与 rubric 只进入评估器；模型输入不注入参考答案、未来事实或隐藏候选。
- 生成检查点的 `UserInputEvidence.message` 取真实请求中绑定到本次运行用户节点的当前输入正文（纯附件回合即 `[附件]` 投影文本），历史回合仍由 `prior_messages` 表达；冻结事件正文与该正文不一致或该正文为空时记录 `ScenarioBlocked`，禁止用冻结字面量或空串填充。`UserInputEvidence.message` 的非空约束因此只由真实请求满足。
- 生成评分使用 `GenerationScoringSession`，裁判由运行启动时的 `FIT_AGENT_EVAL_JUDGE_MODEL` 显式选择，逐指标保留 `succeeded`、`failed`、`not_applicable` 与 `applicable`；失败不改为零分或不适用，`ScoringAttemptSnapshot` 与已成功证据分别保留。
- 无 reference 的纯状态检查点直接产出两项 `not_applicable` 与冻结 reason，不创建假回答、假上下文或评分请求。覆盖统计按预期检查点与指标集合核对 `expected_metrics`、`observed_metrics` 与 `missing_metrics`。
- 模型输入来源按真实节点身份与会话顺序确定：候选节点逐条取自被评分请求自己的 canonical 投影（`SessionQueries.get_projection` 的 `context.messages` 与 `source_entry_ids`），投影 leaf 固定为回答节点的真实父节点——生产链在每次模型调用前按该 leaf 重放投影并以此作为助手节点的父亲，因此本次请求实际可见的节点集合与证据候选集合同源；请求之后的 Steering 消费、后续轮次与后续压缩都不改变候选集合，请求之前的历史节点也不会被后来的投影裁剪掉。正文就是模型实际收到的文本——附件引用行与压缩摘要固定前后缀都已包含在投影内，节点身份与顺序由 `source_entry_ids` 给出，`kind` 取来源节点类型；候选范围由该 leaf 的投影自身决定：最新压缩检查点的有效范围（摘要头、`first_kept_entry_id` 起的保留原文、检查点之后的新增节点）之外与 leaf 之后的分支都不进入候选。用户流与助手流各自按投影顺序消费，第一个可投影该正文的未消费节点即为其来源，来源与事实归属取自节点自身的 `kind`、`entry_id` 与 `has_attachments`；正文相同、包含 `<business_context>` 标签或带附件引用都不改变判定，匹配只按投影后全文精确进行。回答节点缺少父节点时视为无法定位请求边界，直接报上下文冲突。压缩摘要以 user 角色进入投影，绑定摘要节点身份并按系统事实记 `prior_message`，同时写入非歧义缺口说明其投影角色。当前输入由 `request_entry_id` 与同一运行的 Steering 节点身份识别，其余用户节点为 `prior_message`。无法绑定唯一节点身份时写入 `ambiguous` 缺口，该检查点停止断言与生成评分并记 `runner_error`，缺口仍逐条写入 `context.jsonl` 并计入 `coverage.context_gaps`。
- `ToolResultEvidence` 使用持久化完整工具正文，`ContextItem` 保留实际发送文本与裁剪元数据；模型可见工具 ID 与持久化原始 ID 的映射写入 `tool_identity_map`，工具结果节点同时保留 `run_id`。工具候选与完整正文索引只来自同一请求投影的节点：已被压缩移出本次请求的历史同名调用不进入冲突域，本次请求内一个可见 ID 对应多份持久化结果时才按身份冲突处理——映射阶段写入 `ambiguous` 缺口并停止该检查点评分，完整正文索引直接报冲突。正文冲突或缺少完整原文时阻断相关断言与生成评分。
- `run_judge_identity` 只消费真实评分证据；未产生评分证据时身份保留 incomplete 与 missing 字段。

本地产物：

- `run.json`：运行选择、数据集/代码/生成/检索/裁判身份、覆盖统计（`observed_metrics`、`succeeded_metrics`、`failed_metrics`、`not_applicable_metrics`、`unexecuted_metrics`、`missing_metrics`）、阻塞与失败。
- `<scenario>/initial-state.json`：冻结初始状态、真实投影与核对结果。
- `<scenario>/events.jsonl`：逐事件输入、目标、终态、前后完整投影、工具调用与模型调用索引。
- `<scenario>/assertions.jsonl`：逐检查点 `AssertionOutcome` 与运行器装配错误。
- `<scenario>/generation.jsonl`：真实 `GenerationCheckpoint`、`CheckpointResult`、逐指标证据、失败尝试快照、纯状态检查点的 `not_applicable` 结果与未完成项。
- `<scenario>/retrieval.jsonl`：逐次真实搜索调用的候选池、最终模型输入、公开响应与分数，并绑定 `run_id`、`scenario_id`、`event_id`、`tool_call_id` 与本次调用实际命中的 `index_collection`、`index_manifest_sha256`、`index_manifest_json`。序号在场景内单调递增，重启后继续；观测回调缺少事件或工具调用身份时立即失败。
- `<scenario>/context.jsonl`：逐事件模型可见工具 ID 映射、上下文缺口与省略原因。
- `<scenario>/model-calls/calls.jsonl` 与 `requests/*.json`：逐次真实模型调用的身份、请求摘要与原始请求正文。
- `<scenario>/scenario.json`：场景汇总、最终 `proposal_ref` 绑定与真实取消证据。
- `<scenario>/blocked.json`：阻塞原因、取消证据、驱动事件、实际边界状态与已观测事件/检查点数量。
- 场景阻塞或失败时，已完成的事件、断言、评分、检索与取消证据按真实状态与身份保留在 `<scenario>/partial.json`：`ScenarioBlocked` 同时作为 `error.artifact` 交付运行汇总，其它异常由运行汇总从 `<scenario>/partial.json` 读回并聚合。已评分指标计入 `observed_metrics` 与对应状态计数，已产出证据继续参与裁判身份与检索身份聚合，失败场景实际命中的物理索引同样参与单次运行的索引身份判定，未执行的检查点进入 `missing_metrics`；运行整体仍为 blocked 或 failed。
- `identity.missing` 在全部身份诊断（生成、检索与索引冲突、裁判、裁判尝试模型冲突）登记完成后一次性生成，`incomplete` 与 `status` 由该列表派生。
- 运行根使用 `exist_ok=False`，已存在 `run_id` 时拒绝覆盖；异常写 `blocked.json` 或 `failure.json`（两者均记录已观测事件与检查点数量），保留已产生证据。

阻塞与失败边界：

- `cancel` 事件 `content=null`，`source_event_id` 指向提案消息事件；生产 `PendingProposalService.cancel_pending_proposal` 要求更晚的用户请求节点与匹配的助手工具调用。运行器只接受成功的取消工具结果作为证据；模型未产生该调用时记录 `ScenarioBlocked`，不修改冻结目标或补造 UUID。
- 重复取消在提案已失效时由生产服务拒绝；运行器保留该真实结果，业务投影保持不变，取消证据仍唯一指向首次成功调用。
- 消息检查点缺少后续取消事件目标、证据目标不匹配、证据来自其它运行、证据重复或事件身份缺失时，断言失败或场景阻断。

已验证：

- `pytest test/test_evaluation_scenarios.py test/test_deterministic_assertions.py test/test_generation_scoring.py test/test_evaluation_contracts.py test/test_retrieval_stages.py -q --basetemp=backend/temp/task4-followup/pytest-offline`：退出码 0，170 passed in 54.32s。覆盖取消证据与 invalidated 状态区分、实际取消边界阻断、工具完整正文与可见裁剪、工具 ID 规范化映射、纯工具调用缺口、纯状态检查点 N/A、逐指标覆盖、身份端点脱敏、裁判配置显式切换与混合裁判身份拒绝，同文用户消息与业务投影的身份顺序、历史与当前输入的 `prior_message`/`user_message` 区分、未绑定节点触发 `ambiguous` 并停止评分、跨会话节点与工具结果隔离、检索记录的事件/工具调用/索引身份绑定、重启后序号继续、单一运行索引身份聚合与多身份冲突、阻塞场景保留已完成指标计数；本轮新增真实 SQLite 用户节点正文等于生产投影（含 `[附件]` 引用行）并绑定 `attachment` 来源、候选池按回答节点父节点固定到被评分请求边界（请求边界之后的节点既不入池，后续投影也不裁剪请求之前的历史节点）、压缩后请求只含有效范围节点且摘要按前后缀全文绑定为系统事实、已被压缩移出本次请求的历史同名工具调用不进入冲突域而本次请求内的重复 ID 仍判歧义并停止评分、同运行压缩后复用同名取消调用 ID 时两份结果沿真实父链各自绑定发起助手节点与目标提案、来源参数与目标不一致的结果不被绑定、驱动事件按会话与运行复合身份匹配、普通异常的部分产物经 `partial.json` 进入运行汇总、裁判尝试模型冲突进入 `missing` 与 `incomplete`、离线运行汇总的缺失清单完整列出 `generation`/`judge`/`retrieval`、生成检查点的当前输入正文只由请求内 `user_message`/`attachment` 项拼合且忽略业务投影与历史项。
- `python temp/task4-rereview/repro_mapping.py` 与 `python temp/task4-rereview/repro_followup.py`：退出码均为 0，五个映射用例（同文身份、历史与当前输入、未绑定歧义、跨会话歧义、纯工具调用助手）全部通过；真实 SQLite 双会话同名 `tool_call_id` 取消各自保留（`single_counts=[1, 1]`、`combined=2`、`projection.cancellations=2`），跨事件检索序号为 `[0, 1]` 且重启后为 `[0, 1, 2]`。日志在 `backend/temp/task4-followup/`。
- `python -m evaluation.scenarios --all`：退出码 0，输出 80 场景、134 事件、127 检查点、254 个生成指标条目；`s-confirmation-cancellation-state-04` 计划为 3 检查点、6 个生成指标。
- `ruff check evaluation/ app/ test/test_evaluation_scenarios.py`、`compileall -q evaluation app`、`git diff --check`：全部通过。`ruff check test/` 全量运行另有 37 项未改动文件的既有告警（I001/E402/F401/F841 与 `test/test_stability_task2.py:504` 的 `AssistantMessage` 未定义名），属本轮范围外。
- `RunSummary.incomplete` 逐场景汇总绑定失败、运行器装配错误、失败指标、尚无结果的适用指标与身份缺失；`context_gaps` 单独进入覆盖统计。CLI 与真实集成检查在存在 `incomplete`、阻塞、失败或指标缺失时返回非零。
- 真实 `s-action-verification-01` 检查：Agent 完成 5 次模型调用与 8 次工具调用（全部 `is_error=false`），检索证据为候选池 20、最终模型输入 5、公开条目 5、threshold=0.0；`state_unchanged` 与 `evidence_present` 两项断言通过；覆盖为 scenarios 1/1、events 1/1、checkpoints 1/1、metrics 2/2；`identity.index` 记录 collection、build_id、manifest_sha256 与兼容性清单；Faithfulness 在 180 s 内完成，得分 0.6667；FactualCorrectness 在 180 s 超时，保留 `failed`。同一真实检查点在独立评分中于 280 s 内完成 FactualCorrectness，得分 0.55，证据与尝试快照写入 `backend/temp/task4-fix/judge-compat/factual-compat.json`。两项指标使用同一裁判，实际模型身份保留于原始运行证据，`judge_same_as_generation=true`。
- 真实批次（`--enable-model`，run-id `flow-*`，产物在 `backend/temp/evaluation-scenarios/`，日志在 `backend/temp/task4-followup/`）：单场景逐个运行覆盖附件回合、检索工具、会话切换、多回合同文输入、确认与取消/重启。全部运行零 `ambiguous` 缺口，用户/助手/工具/业务投影正文均按真实节点身份绑定：`s-plan-import-file-06` 的 37 项模型输入为 system_prompt 1、attachment 1、business_projection 13、tool_result 22；`s-profile-update-07` 三回合的 cp3 为 24 项（prior_message 2、user_message 1、assistant_message 2、business_projection 9、tool_result 9、system_prompt 1），历史与当前输入按顺序区分。
- 真实 `s-action-verification-01`（`--judge-timeout 120`，run-id `flow-act01-t120`）：`identity.missing=[]`、`identity.judge` 与 `cache_identity` 齐备，Faithfulness 成功得分 0.4359，FactualCorrectness 在第 3 次裁判调用处 `TimeoutError` 保留 `failed`，`incomplete` 仅两项评分相关；检索记录 1 条并绑定真实 `search_exercises` 的 `tool_call_id`、`event_id=e01` 与唯一 `(collection, manifest_sha256)`。
- 修复验证：附件-only 回合此前在真实运行中终态为 `failed`，运行汇总逐字记录 `s-plan-import-file-04: ValidationError: 1 validation error for UserInputEvidence message Value error, 必须包含非空白内容 [input_value='', input_type=str]`（根因为冻结事件正文为空而该字段是 `NonEmptyText`），该 run 目录已按放行删除。修复后 `flow-att04b` 场景 completed、`user_input.message` 为 125 字符的 `[附件]` 投影、Faithfulness 得分 0.7059；`flow-att05` 两个纯附件回合（e01/e03）同样按投影正文计分，cp1 断言 `state_unchanged` 与 `evidence_present(context min_count=2 actual=10)` 通过。
- 真实 SQLite 与生产取消服务回归（本次离线验证）：e02 消息回合真实完成 `cancel_pending_proposal`，断言验证 `pending→invalidated`、取消目标唯一且证据匹配本消息会话/运行/请求/来源/结果节点；e03 使用自身 `invalidated→invalidated` 前后边界并保留同一份取消证据；已保存数据不变、重复取消保持业务状态、重启后失效状态持续；错误目标、缺少证据、重复证据与非取消消息均不能通过。真实模型下的取消调用稳定性保留为正式 baseline 验证项。
- 真实 `s-workout-logging-query-01`：模型第二回合以 `stop_reason=length` 达到输出上限（output_tokens 16384），事件终态 `failed:model_output_limit_exceeded`，回答文本缺失按契约阻断且不填占位；cp1 记 `binding_error: proposal_ref 缺少唯一真实绑定`。
- 真实批次中的业务断言失败按真实状态保留：`s-replacement-constraints-03` cp1 `state_unchanged` 失败（模型创建 1 个 pending 提案），`s-profile-update-07` cp3 `evidence_present(snapshot min_count=2 actual=0)` 失败；两者均记录真实结果与证据，不做改写。
- 裁判预算实测：`--judge-timeout 30` 下全部指标 `TimeoutError`；90 s 与 120 s 下 Faithfulness 完成并产出真实分数，FactualCorrectness 仍需更长预算（历史 280 s 完成）。
- 运行器默认 `--judge-timeout 300`，按单项生成指标计时，并传入 Ragas、LLM 与 HTTP 客户端；可通过 CLI 显式覆盖。该预算不代表整个场景或批次的总时限，超时仍保留真实失败与尝试证据。
- 未执行：正式 80 场景全量真实运行、全量裁判评分与 LangSmith 上传。

## 7. 依赖记录

- `ragas==0.3.1`：dev 依赖，固定精确版本；在 Python 3.13 与现有 `openai==3.13.0` 下实际导入通过，新增依赖面小于 0.4.x。
- `langchain-community>=0.4,<0.4.2`：Ragas 0.3.1 在导入时引用 `langchain_community.chat_models.vertexai`；该模块在 0.4.2 被移除，故锁定 0.4.1。
- `mteb==2.26.7` 与 `openai==3.13.0` 保持原锁定值；`langchain-community` 上界未改动现有生产依赖上界。
- 新增 LangChain 依赖链使 `websockets` 从 17.2 解析为 16.1.1（`langgraph-sdk` 约束）。该变化仍在现有依赖允许范围内，未升级 MTEB。
- 外部裁判契约不新增依赖，只使用 Pydantic 与标准库 `math`、`numbers`。
- Python 3.13.13；`uv lock --check --directory backend` 通过。

## 8. 未交付与验证状态

20→5 两阶段契约修订、指标入口、生产阶段构造、评测证据/上传接线、确定性业务断言引擎与 Ragas 生成评分模块已在代码中实现。新增和修改的测试代码覆盖两阶段指标、候选池上限、空返回口径、预算裁剪、隐藏身份、证据一致性、未审阅候选门禁与历史路径身份；确定性断言测试覆盖 11 种断言、同检查点同类多条、比较范围、JSON Pointer 边界、真实目录器械、快照漂移、保存授权、状态迁移、四类证据、真实 SQLite 确认链路与错误输入。生成评分测试覆盖真实 Ragas 指标样本映射、上下文顺序与裁剪、reference 隔离、逐指标适用性、检查点身份、0/1/NaN/Infinity/类型边界、真实 prompt 摘要、配置与 timeout 拒绝路径、证据凭据隔离、地址内嵌凭据拒绝与序列化脱敏、成功/证据分数一致性、重复证据身份稳定性、遥测禁用缓存覆盖，以及本机传输失败、超时与取消的失败尝试证据保留。已执行 `pytest test/test_generation_scoring.py -q`，65 passed。

已执行 `pytest test/test_deterministic_assertions.py -q`，退出码 0，30 passed。新增用例覆盖消息检查点的 `pending→invalidated`、取消证据目标/运行/唯一性、后置 `invalidated→invalidated` 与缺少取消记录；同次静态检查为 Ruff、`compileall` 与 `git diff --check`。既有用例继续覆盖保存正文偏离准备快照、训练日期偏离、无法核验的动作身份、工具正文与上下文冲突、单次搜索超过 5 条、排名来源缺失与重复来源。

任务 4 场景运行器已落地并按审查修复：`evaluation/scenarios.py` 提供 80 场景选择、真实生产链路装配、五类事件编排、全会话投影、本地证据与逐指标覆盖；`evaluation/scenario_evidence.py` 提供真实业务投影读取、真实取消证据、模型请求全文采集、按真实消息节点归因的 `ContextItem` 映射与完整工具正文。身份边界对生成、Embedding 与错误产物统一脱敏；检索身份绑定实际 collection、build_id 与兼容性清单。本次修订后离线回归：`pytest test/test_evaluation_scenarios.py -q` 49 passed；`pytest test/test_evaluation_dataset.py -q` 37 passed；`pytest test/test_evaluation_contracts.py test/test_evaluation_summary.py -q` 41 passed；`pytest test/test_retrieval_stages.py test/test_retrieval_mteb.py test/test_retrieval_rrf.py -q` 95 passed；`python -m evaluation.scenarios --all` 退出码 0。真实定向验收覆盖 `s-action-verification-01`、`s-confirmation-cancellation-state-04`、`s-profile-update-08` 与 `s-confirmation-cancellation-state-08`：检索、断言、确认、重复确认、会话切换与纯状态 N/A 均通过；真实定向评分中 Faithfulness 得分 0.6667、FactualCorrectness 在 280 s 独立评分中得分 0.55；取消场景按消息回合真实迁移与后置真实边界验证。未执行正式 80 场景全量、全量裁判评分与 LangSmith 上传。

生成评分模块的静态检查已执行：Ruff 与 `compileall` 通过，模块导入与纯字段映射检查通过。已执行 `pytest test/test_generation_scoring.py -q`，退出码 0，65 passed，无警告。已执行 `FIT_AGENT_GENERATION_SCORING_INTEGRATION=1` 的真实服务集成检查：受保护配置指定的裁判服务完成真实调用，Faithfulness 与 FactualCorrectness 均 `succeeded`，评分 1.0/1.0，裁判调用分别为 2 与 4 次，超时/取消路径完成客户端释放，样例动作事实来自真实 Catalog `0001`（`equipment=body weight`、`target=abs`）。真实调用出现 `langchain-openai` 关于自定义 httpx transport 的代理自动探测警告，非致命；代理环境下的连通性仍属集中验证范围。尚未执行完整业务测试套件、正式全量 150 查询检索、80 个业务场景、真实 Agent 或 LangSmith 上传；正式场景下的模型稳定性、长上下文与限流行为仍属集中验证范围。未执行的测试不得视为通过。

## 9. 汇总、报告、生成评测上传与发布门禁

任务 5 已实现两个独立入口：

```powershell
uv run --locked --directory backend python -m evaluation.summary report --source backend/temp/evaluation-scenarios/<run_id>
uv run --locked --directory backend python -m evaluation.summary check --source backend/temp/evaluation-scenarios/<run_id>
uv run --locked --directory backend python -m evaluation.generation_upload check --source backend/temp/evaluation-scenarios/<run_id>
uv run --locked --directory backend python -m evaluation.generation_upload upload --source backend/temp/evaluation-scenarios/<run_id>
```

`summary` 只读本地运行目录，不发起网络请求。`generation_upload check` 只构造并校验 payload，不加载 LangSmith 凭据；`upload` 才进行外部调用。

本地读取与身份核验：

- 读取 `run.json`、`plan.json`、`<scenario>/scenario.json` 或 `partial.json`，以及 `events.jsonl`、`assertions.jsonl`、`generation.jsonl`、`retrieval.jsonl`、`context.jsonl` 与 `<scenario>/model-calls/` 下的调用索引与请求全文。
- 运行、场景、事件、检查点、指标结果、评分证据与评分尝试必须沿同一 `run_id`、`scenario_id`、`event_id`、`checkpoint_id` 关联；JSONL 与场景产物逐条一致。
- 逐生成检查点对照来源证据：审阅 `reference`/`rubric` 由冻结数据同构重建并逐字段比对；适用性逐项等于冻结 `metric_applicability`；回答正文与结构化快照等于该事件捕获记录；当前用户正文包含冻结事件输入；回答绑定的模型调用按消息 ID 与 `stop` 状态选取，其请求全文文件摘要等于 `request_sha256`，调用 `api`/`provider`/`model` 等于 `run.json` 生成模型身份。
- 每个生成检查点同时保存被评分请求的节点边界（`request_boundary`：`api`、会话、运行、请求入口、消息节点与工具节点）。汇总器沿该边界调用生产同一 `current_input_entry_ids` 与 `context_items_from_request` 重放映射，逐条比对正文、顺序、来源、事实归属、`message_entry_id`、附件标记、工具身份与裁剪信息；当前输入只计入该运行及实际消费 Steering 节点绑定的用户条目，历史附件保留为历史上下文。缺少节点边界、边界与事件或模型调用不一致、重放结果偏离均阻断发布。
- 重复检查点、重复指标结果、跨场景或跨事件错绑、`ambiguous` 上下文、工具正文冲突、缺失证据与冲突身份全部进入诊断并阻断有效发布。
- 数据身份通过 `load_for_evaluation` 重新核验；执行计划与冻结数据、场景选择、事件/检查点/指标预期必须一致；`run.json` 覆盖统计必须与重新计算结果一致。
- 物理索引身份只按实际 `retrieval.jsonl` 聚合；缺少检索、多个物理索引或运行身份与记录不一致均阻断。代码身份比较提交、锁文件摘要与源码摘要；生成身份必须同时具备 `api`、`provider`、`model` 与 `prompt_version`，与裁判、提示、指标身份一起缺失或冲突即阻断。

生成指标聚合：

- 逐检查点保留 `succeeded`、`failed`、`not_applicable`、`applicable` 与未执行状态；真实 0 分是成功评分。
- 场景内按该指标 `succeeded` 检查点取算术平均；`not_applicable` 与 `failed` 不进入分母，失败不得填零分。
- 跨场景并列输出两套口径：`mean_including_partial` 只要求场景至少有一个成功检查点；`mean_complete_only` 还要求该场景该指标没有 failed、applicable 或未执行检查点。两套口径各自记录可用场景与完整场景分母。
- 有效发布只认完整成功口径；`failed`、`applicable`、未执行、断言失败、覆盖不足或证据不全均阻断。`observed == expected` 不作为评分完成的充分条件。
- `scope=auto` 以是否覆盖冻结全量计划区分正式全量与定向验证；定向验证只生成诊断报告，不能作为有效发布。`--scope formal` 对非全量计划直接阻断。

生成评测上传：

- `backend/evaluation/generation_upload.py` 逐字段构造 payload，不直接序列化 `ScenarioArtifact` 或运行目录。
- 上传字段仅包含合成输入、当前用户输入（含 `message_sha256` 绑定）、真实模型输入上下文（逐条含 `content_sha256`）、真实回答、审阅 reference/rubric、断言通过状态、逐指标状态与分数、数据/代码/生成/裁判/提示/指标/物理索引身份及聚合口径。
- 禁止上传 API Key、认证头、服务地址与本地端点、`judge_base_url`、裁判调用原文、Ragas 回调链、prompt 指纹原文、原始模型调用、`scoring_input`、本地路径与运行目录调试内容。出站时把会话存储引用与绝对盘符路径统一替换为按原文派生的 `attachment:<sha256[:12]>` 或 `local:<sha256[:12]>` 令牌，原始正文与其摘要仅保存在本地运行目录；白名单校验在替换后再次复扫本地路径残留，命中即阻断。数据批准文本中的仓库相对引用保持原样。
- 数据集名称为 `fit-agent-generation-<version>-<content_sha256[:12]>`；行 ID 由数据身份、场景与检查点稳定派生；实验身份绑定真实数据、代码、生成模型、裁判、提示、指标与实际物理索引。
- `experiment_metadata` 保存 `source_sha256` 与 `rows_sha256`；回执与 pending 标记同时保存 `rows_sha256`，`payload_sha256` 覆盖含摘要的完整 payload。
- 回执保存数据集/实验名称与 ID、行数与远端运行数。再次上传先核对回执与本地重算摘要，再按实验 ID 读取远端。
- 提交前写入 pending 标记。若远端已提交而本地回执缺失，重试先按实验名与数据集核对远端名称、样例数与运行数，再逐行回读远端内容：以 payload 派生的期望对每行的 `inputs`、`expected_outputs`、`actual_outputs` 与逐指标分数摘要做全量比对，样例 ID 集合或任一摘要不一致即拒绝；全部一致才恢复回执。提交后同样执行该回读校验，并把返回的数据集 ID 与实验 ID 绑定到校验结果后写入回执。

验证状态：

- `pytest test/test_evaluation_summary.py -q`：21 passed。该套件离线构造运行产物与逐检查点真实请求全文证据，只验证汇总、报告、payload 与门禁逻辑，不代表真实 80 场景成绩。覆盖场景权重、两套分母、真实 0 分、N/A、failed、applicable、未执行、partial 保留成功结果、断言失败、覆盖缺失、重复身份、分数/提示/模型/数据/索引篡改、评分证据与输入/尝试一致性、检查点 reference 与回答与适用性与历史输入篡改、请求全文摘要篡改、请求节点边界重放、历史附件当前输入口径、同角色来源与事实归属错标、工具身份漂移、生成提示身份缺失、字段白名单、本地路径出站令牌与逐条摘要绑定、源摘要、回执复用、提交中断边界、远端逐行回读的分数/样例集合/输入/运行元数据漂移、远端实验元数据与汇总分数漂移，以及两阶段检索门禁。
- `pytest test/test_retrieval_mteb.py test/test_retrieval_stages.py test/test_retrieval_rrf.py -q`：95 passed。`publishable()` 在结构核验外，对两阶段证据按绑定数据版本、批准参数与物理索引身份重跑算分阶段校验：逐组重算指标、公开响应字符数与预算、硬约束、目录成员与未审阅候选必须复现记录值；缺少 `dataset`、`parameters` 或 `index_identity` 直接拒绝发布。历史单阶段身份保持不变。
- `pytest test/test_evaluation_contracts.py test/test_evaluation_dataset.py test/test_deterministic_assertions.py -q`：84 passed。
- `pytest test/test_evaluation_scenarios.py test/test_generation_scoring.py -q`：117 passed。
- Ruff、`compileall` 与 `git diff --check` 通过。
- 真实产物对照：对已捕获的 `flow-att04b` 与 `validation-20261019T012723Z-4b26f748` 运行目录重算门禁，来源对照类诊断命中 0 条；两次运行自身的其余诊断（评分失败、覆盖缺失、代码身份漂移）保持原样。
- 未执行：正式 150 查询检索、正式 80 场景全量运行、真实 LangSmith 生成评测上传。远端逐行回读、样例与分数反馈的实际返回结构、回执写入与中断恢复链路只经过离线比较函数验证，账号授权与真实云端行为仍未验证；未运行项不得视为通过。

## 10. 场景执行与评分接缝（任务 2）

### 10.1 公开接口

```python
from evaluation.scenarios import (
    ExecutionIdentityError,
    ExternalScenarioOutcome,
    ExternalScenarioScoring,
    ScenarioExecution,
    ScoringResultExists,
    execute_scenario,
    load_execution,
    load_external_scoring,
    run_external_scenario,
    run_scenario,
    score_execution,
    score_execution_external,
    verify_execution_identity,
    write_external_scoring_result,
    write_scoring_result,
)

async def execute_scenario(
    dataset: EvaluationDataset,
    scenario: dict,
    *,
    run_id: str,
    root: Path,
    enable_model: bool,
) -> ScenarioExecution

def load_execution(
    path: Path,
    *,
    expected_run_id: str | None = None,
    expected_scenario_id: str | None = None,
    expected_dataset: DatasetIdentity | None = None,
    expected_scenario_sha256: str | None = None,
) -> ScenarioExecution

def verify_execution_identity(
    execution: ScenarioExecution,
    *,
    expected_run_id: str | None = None,
    expected_scenario_id: str | None = None,
    expected_dataset: DatasetIdentity | None = None,
    expected_scenario_sha256: str | None = None,
) -> ScenarioExecution

async def score_execution(
    execution: ScenarioExecution,
    *,
    judge: JudgeConfig | None = None,
    judge_timeout: float | None = 300.0,
) -> ScenarioArtifact

def score_execution_external(
    execution: ScenarioExecution,
    assertions: Sequence[dict[str, Any]],
) -> ExternalScenarioScoring

def write_scoring_result(
    execution: ScenarioExecution,
    artifact: ScenarioArtifact,
    *,
    path: Path,
) -> None

def write_external_scoring_result(
    execution: ScenarioExecution,
    record: ExternalScenarioScoring,
    *,
    path: Path,
) -> None

def load_external_scoring(
    path: Path,
    *,
    expected_execution_sha256: str | None = None,
) -> ExternalScenarioScoring

async def run_scenario(
    dataset: EvaluationDataset,
    scenario: dict,
    *,
    run_id: str,
    root: Path,
    enable_model: bool,
    judge: JudgeConfig | None,
    judge_timeout: float | None,
    scoring_mode: Literal["ragas", "external"] = "ragas",
) -> ScenarioArtifact | ExternalScenarioScoring

async def run_external_scenario(
    dataset: EvaluationDataset,
    scenario: dict,
    *,
    run_id: str,
    root: Path,
    enable_model: bool,
) -> ExternalScenarioOutcome
```

- `execute_scenario` 只执行场景并持久化证据。它不读取裁判配置、不调用裁判、不写评分产物。
- `score_execution` 只消费 `ScenarioExecution`，不接收 runtime、数据库、服务或检索客户端；返回旧入口兼容的 `ScenarioArtifact`，Ragas 语义保持不变。
- `score_execution_external` 只读取封存执行的断言检查点并运行确定性断言，不创建 `GenerationScoringSession`、不进入 `_score_generation_entries`、不调用 Ragas 或任何裁判。
- `run_scenario` 默认 `scoring_mode=ragas` 并保持原签名兼容；`external` 分支调用 `run_external_scenario` 并返回本地外部结果。
- `run_external_scenario` 同时返回本地结果与封存执行，供运行汇总聚合事件、检查点与检索身份；不复制执行证据。

### 10.2 执行结果模型

`ScenarioExecution` 保存：

- `schema_version`、`run_id`、`scenario_id`、`status`（`completed`、`blocked`、`failed`）与 `error`。
- `dataset`：`DatasetIdentity`，绑定数据版本、manifest、内容与语料摘要。
- `scenario_sha256`：冻结场景正文的 canonical SHA-256，排除运行时 `_` 字段。
- `execution_sha256`：执行结果正文（排除该字段自身）的 canonical SHA-256。
- `expected_event_ids`、`expected_checkpoint_ids`：冻结执行计划身份。
- `session_ids`、`initial_state_sha256`、`initial_projection`。
- `events`：原有 `EventCapture`，保留真实 before/after、工具调用、模型调用与上下文缺口。
- `assertion_checkpoints`：逐检查点的原始断言定义与完整 `AssertionEvidence`，或明确的 `binding_error`/`runner_error`。
- `generation_checkpoints`：逐检查点的 `GenerationCheckpoint`、`RequestBoundaryEvidence`、冻结适用性与执行阶段错误。
- `retrieval`、`proposal_bindings`、`generation_metrics`、`cancel_evidence`、`context_gaps`。

身份约束：

- 所有 `EvidenceLink` 必须同时绑定本次 `run_id`、`scenario_id`、`event_id` 与 `checkpoint_id`。
- 完成执行必须覆盖全部冻结事件与检查点；部分执行的事件必须是冻结顺序的前缀，检查点必须是冻结集合的子集。
- `load_execution` 重新计算 `execution_sha256`；摘要不符、显式期望身份不符或证据身份错绑均明确失败。错场景、错事件或错检查点的链路在模型构造阶段由 Pydantic 拒绝。
- 生成检查点的请求边界必须与所属事件的实际 `session_id`、业务 `run_id` 与请求入口一致。
- 执行结果不包含 API Key、认证头、裁判配置、服务地址或开放连接。

### 10.3 文件布局

每个场景目录新增：

- `execution.json`：完整执行结果，供磁盘重载与评分。
- `assertion-evidence.jsonl`：逐事件追加的断言证据。
- `generation-input.jsonl`：逐事件追加的生成评分输入与请求边界。

旧入口继续输出原文件：

- `events.jsonl`、`retrieval.jsonl`、`context.jsonl`、`initial-state.json`。
- `scenario.json`、`partial.json`、`blocked.json`、`failure.json`。
- `assertions.jsonl`、`generation.jsonl`。

`write_scoring_result` 可把 `ScenarioArtifact` 写入独立评分文件；目标已存在时抛出 `ScoringResultExists`，不静默覆盖。`run_scenario` 使用新建场景目录，因此旧产物不会被覆盖。

### 10.4 生命周期与失败流转

- 执行阶段在 `execute_scenario` 内打开 runtime，逐事件采集证据，正常或失败后都在 `finally` 中关闭检索运行时、执行器与数据库连接。
- 事件证据在所属事件边界采集；断言证据与生成检查点在写入 `events.jsonl`/`context.jsonl` 前完成，随后评分只读取磁盘上的真实证据。
- 执行失败时仍写入 `execution.json`，保留已发生事件、初始投影、检索、取消证据与上下文缺口。
- 旧入口在执行失败后对已持久化证据执行评分，再写 `blocked.json`/`failure.json` 与 `partial.json`；阻塞场景的 `ScenarioBlocked.artifact` 与观测计数保持原语义。
- 适用生成指标缺少裁判配置或 timeout 无效时，`run_scenario` 在执行前拒绝，不发起业务模型请求。
- 执行状态与评分状态分离：仅有 `execution.json` 的目录不构成完整场景产物，`summary` 与上传门禁不会视为有效评测结果。
- 评分结果保留 `succeeded`、`failed`、`applicable`、`not_applicable`、失败尝试快照、评分证据、`runner_error` 与未完成指标；评分失败不会写成成功或零分。
- 取消场景的 `pending→invalidated`、`invalidated→invalidated`、重复取消与重启边界沿用原生产证据逻辑，未改变证据采集时点。

### 10.5 验证状态

- `pytest test/test_scenario_execution_scoring.py -q`：9 passed。覆盖执行结果序列化与磁盘重载、摘要与身份篡改、事件边界前后状态、部分执行证据、N/A、缺裁判阻断、旧入口组合、评分结果不覆盖以及仅执行目录不构成完整产物。
- `pytest test/test_evaluation_scenarios.py test/test_deterministic_assertions.py -q`：82 passed。
- `pytest test/test_evaluation_contracts.py test/test_evaluation_summary.py -q`：41 passed。
- `pytest test/test_generation_scoring.py test/test_evaluation_dataset.py -q`：首次因 `--basetemp` 父目录不存在产生 37 个 setup error；补建 `backend/temp/task2/` 后重跑 106 passed。
- `uv run --locked python -m evaluation.scenarios --all`：退出码 0，输出 80 场景、134 事件、127 检查点、254 个生成指标条目。
- Ruff、`compileall` 与 `git diff --check` 通过。
- 未执行：真实模型、Embedding、Milvus、LangSmith、正式 80 场景全量 baseline 与真实裁判评分。未运行项不得视为通过。

## 11. LangSmith 外部裁判契约（任务 1）

### 11.1 交付边界

本轮只新增独立数据契约与本地校验，未实现上传、构造器、模型评分、反馈回收或门禁接线。Ragas 历史契约、`GenerationMetric`、`GENERATION_METRICS`、`GenerationCheckpoint`、`MetricOutcome` 与既有证据模块保持原语义；旧数据继续按原契约校验。

共享导入路径：

```python
from evaluation.contracts import (
    EXTERNAL_JUDGE_CHECKPOINT_SCHEMA_VERSION,
    EXTERNAL_JUDGE_FEEDBACK_SCHEMA_VERSION,
    EXTERNAL_JUDGE_METRICS,
    ExternalJudgeCheckpointUpload,
    ExternalJudgeFeedback,
    ExternalJudgeFeedbackSet,
    ExternalJudgeScoringAttempt,
)
```

### 11.2 指标身份与版本

- `evidence_supported_v1`：回答事实是否得到实际证据支持。
- `reference_requirements_met_v1`：回答是否满足参考事实与任务要求。
- `EXTERNAL_JUDGE_METRICS` 固定上述两项；指标身份不包含供应商、模型或别名字段。
- 检查点上传契约版本为 `fit-agent-external-judge-checkpoint-1`；反馈与评分尝试契约版本为 `fit-agent-external-judge-feedback-1`。两者与 `fit-agent-generation-upload-payload-1` 等历史版本隔离。
- `GenerationMetric` 与 `MetricApplicability` 不接受新指标；`ExternalJudgeApplicability`、`ExternalJudgeFeedback` 与 `ExternalJudgeFeedbackSet` 不接受 `faithfulness`、`factual_correctness`。
- 旧 `faithfulness`/`factual_correctness` 的适用性不会自动转换为新指标；调用方必须显式提供两项新适用性，契约校验唯一性与完整性。旧到新指标的语义映射留待明确审阅。
- 本契约不新增质量分数阈值。

### 11.3 检查点上传契约

`ExternalJudgeCheckpointUpload` 的字段分区：

- `schema_version`：必须显式填写上传契约版本。
- `metadata`：`dataset`（`DatasetIdentity`）、`checkpoint`（`EvidenceLink`，含 run_id、scenario_id、event_id、checkpoint_id）与 `execution_sha256`。
- `inputs.user_request`：`ExternalJudgeUserRequest` 继承 `UserInputEvidence`，保留当前用户请求 `message` 与必要 `prior_messages`，并携带 `checkpoint` 身份。
- `outputs.answer`：复用 `AnswerEvidence`，保留可见文本与完整结构化快照。
- `outputs.context`：`ExternalJudgeContext` 保存 `ContextItem`（来源、事实归属、工具绑定、逐条裁剪信息）、`request_boundary`（`RequestBoundaryEvidence`，真实消息节点与工具节点边界）和聚合裁剪字段。`items` 是裁判可见上下文的唯一来源；`request_boundary` 与 `retrieval` 只用于离线审计与复核。
- `outputs.context.retrieval`：每项 `ExternalJudgeRetrievalEvidence` 绑定 `tool_call_id` 与 `tool_name`，复用 `SearchResponseEvidence` 和 `RerankEvidence`；重排证据存在时必须是 `rerank` 实验，基线以不携带重排证据表达。重排身份的 `endpoint` 沿用既有出站约定，只保存地址 SHA-256 摘要，原始服务地址被拒绝。检索与重排算法及字段语义沿用现有契约。
- `reference_outputs.reference`：复用 `GenerationReference`，保存审阅回答、已审阅事实、必须要求、允许变化与禁止声明。
- `reference_outputs.applicability`：恰含两项 `ExternalJudgeApplicability`，逐项唯一且完整；不适用必须写明原因。

本地校验：

- 用户请求的 `checkpoint` 必须等于 `metadata.checkpoint`。
- 每条检索证据必须绑定 `outputs.context.items` 中同 `tool_call_id`、同 `tool_name` 的 `tool_result`；重复工具调用被拒绝。检索记录按请求边界内可见的 `tool_call_id` 选取，可来自同一场景中不晚于当前事件的历史事件；请求边界外的记录不进入投影，审计保留每条记录的真实 `event_id`。
- 上下文整体裁剪必须与 `omitted_chars` 一致。
- 契约封闭字段；承载面与既有上传 payload 的白名单分区一致，不包含 API Key、认证头、服务地址、裁判调用原文、请求路径、prompt 指纹、原始模型请求或评分输入全文。数据身份、运行身份与执行摘要均可复核。
- 实际上下文位于 `outputs.context`，参考答案位于 `reference_outputs.reference`，两者不共用承载结构。

本轮不构造、不脱敏、不上传。后续任务实现构造器时仍须先按既有出站规则脱敏，再执行白名单复核。

### 11.4 外部裁判反馈契约

`ExternalJudgeFeedback` 保存指标名、状态、分数、适用性原因与失败依据：

- `pending`：待评分；不得携带分数或失败依据。
- `succeeded`：必须提供有限的 0..1 数值；真实 0 分属于成功；不得携带失败依据。
- `failed`：不得携带成功分数；必须保留失败依据。
- `not_applicable`：不得携带分数或失败依据；必须保留原因。
- 缺失状态直接拒绝；缺失反馈不会被解释为零分、通过或不适用。
- 分数拒绝 bool、NaN、Infinity、越界值与非数值输入。
- `ExternalJudgeFeedbackSet` 固定两项指标，按检查点与 `execution_sha256` 绑定；缺失、重复与未知指标均被拒绝。
- 反馈字段不使用 Ragas 指标标识；旧 `MetricOutcome` 的状态语义与旧数据保持不变。

### 11.5 裁判来源与评分尝试

`ExternalJudgeScoringAttempt` 绑定：

- 本地记录身份：`attempt_id`、`checkpoint`（`EvidenceLink`）与 `execution_sha256`。
- LangSmith 来源：`source.experiment`（实验 ID 与可选名称）、`source.run_id`、`source.example_id` 与 `feedback_source`。
- 模型与 Evaluator 身份：`model.provider`、`model.evaluator`、`model.model_alias`、`model.model_version` 与 `model.config_sha256`；不硬编码 Jev、TypeSafe 或具体模型名。
- 定义身份：`definition.question_id`、`question_sha256`、`rubric_id` 与 `rubric_sha256`，摘要沿用 SHA-256（canonical JSON）约定。
- 出站评分投影：`outbound.row_id` 与 `outbound.projection_sha256`，把评分绑定到实际发送的脱敏后输入。
- 反馈：`feedback` 使用 11.4 的状态语义；`cloud_feedback` 只承载 LangSmith 实际返回的 `experiment_id`、`run_id` 与 `feedback_id`。

模型版本表达：

- `ReportedModelVersion(kind="reported", version=...)` 表示服务返回了具体版本。
- `UnreportedModelVersion(kind="unreported")` 表示服务未返回版本；不得补造版本字符串。
- `model_alias` 与 `model_version.version` 分开记录，别名与服务实际返回的具体版本可明确区分。

身份边界：

- `pending` 反馈不得绑定 `cloud_feedback`；已返回的反馈必须绑定 `cloud_feedback`，其 `experiment_id` 与 `run_id` 必须与 `source` 一致。
- 本地记录身份与云端实际返回身份分开保存，禁止用本地尝试 ID 冒充云端反馈 ID。
- 自定义 run metadata 不作为反馈身份或回读依据；只使用 SDK 实际返回的反馈字段。
- 本任务不调用 SDK、不读取密钥、不访问网络。

### 11.6 已验证范围

- 新增 `backend/test/test_external_judge_contracts.py`，覆盖指标完整性与唯一性、未知指标拒绝、身份交叉引用、上传结构、上下文与参考分离、反馈状态组合、0/1 分、非法类型与非法数值、模型别名与版本区分、云端身份一致性、版本隔离与请求边界类型共享。
- 旧 `GenerationCheckpoint`、`MetricApplicability`、`MetricOutcome` 与 `GENERATION_METRICS` 的回归由既有契约测试继续覆盖。
- 未执行：上传、模型评分、反馈回收、发布门禁接线，以及任何真实 LangSmith 调用。

## 12. LangSmith 外部裁判迁移——任务 2：运行模式与状态拆分

### 12.1 显式运行协议

- `scoring_mode` 只有 `ragas` 与 `external`，由 CLI 或内部入口显式传入；默认 `ragas`，历史运行缺少该字段时按已知 Ragas 协议解释。
- 协议版本：`fit-agent-scenario-ragas-protocol-1` 与 `fit-agent-external-scenario-protocol-1`，分别绑定两种模式。`plan.json`、`run.json` 与 LangSmith `sdk-run.json` 同时保存实际 `scoring_mode` 与 `protocol_version`。
- 模式选择不读取 Key、不根据配置存在性推断、不做自动 fallback；未知模式直接拒绝。
- 场景运行器：

```powershell
uv run --locked --directory backend python -m evaluation.scenarios --scenario-id s-plan-generation-01 --enable-model --scoring-mode external
uv run --locked --directory backend python -m evaluation.scenarios --all --enable-model --scoring-mode ragas
```

- LangSmith SDK 运行器：

```powershell
uv run --locked --directory backend python -m evaluation.langsmith_scenarios run --purpose validation --scoring-mode external
uv run --locked --directory backend python -m evaluation.langsmith_scenarios finalize --run backend/temp/scenario-langsmith/runs/<run_id>
```

`run` 的默认值仍为 `ragas`；`finalize` 从运行摘要的 `scoring_mode` 与 `feedback_version` 判定协议，不新增隐式模式参数。

### 12.2 确定性断言与生成评分拆分

- `score_execution_external(execution, assertions)` 复用 `_score_assertion_entries` 与既有身份验证，直接读取封存 `ScenarioExecution` 的断言检查点；`assertions` 为冻结场景的 `assertions` 行，独立给出预期检查点与断言种类。不进入 `_score_generation_entries`/`_score_generation_group`，不创建 `GenerationScoringSession`，不调用任何 Ragas 方法或裁判客户端。
- `score_execution` 的旧 Ragas 语义不变：断言照常执行，生成检查点仍按 `GenerationScoringSession` 与裁判配置评分。
- 外部路径不要求 `FIT_AGENT_EVAL_JUDGE_MODEL`、`FIT_AGENT_EVAL_JUDGE_BASE_URL`、`FIT_AGENT_EVAL_JUDGE_API_KEY` 或 `TYPESAFE_API_KEY`；`langsmith_scenarios.run_experiment` 的外部分支不调用 `judge_config()`。生成模型、检索、LangSmith 凭据与索引预检仍按原规则执行。
- 外部路径不修改已封存的 `execution.json`，不重算执行摘要，也不重新执行任何业务事件；重复读取与重复处理只读取执行证据。

### 12.3 本地外部结果契约

`ExternalScenarioScoring`（版本 `fit-agent-external-scenario-scoring-1`）保存：

- 身份：`run_id`、`scenario_id`、`execution_sha256`、`scoring_mode`、`protocol_version`。
- 三个独立状态：`execution_status`（`completed`/`blocked`/`failed`）、`assertion_status`、`generation_scoring_status`。
- `assertion_status` 取 `passed`、`failed`、`binding_error`、`runner_error`、`incomplete`，按运行器错误、绑定错误、真实断言失败、缺失检查点的顺序保留依据。
- `generation_scoring_status` 取 `pending_external`（证据可供后续外部评分）或 `incomplete`（存在缺失证据）；外部模式不会把执行完成写成生成评分完成。
- `applicability_status` 固定为 `awaiting_external_applicability`：不把旧 `faithfulness`/`factual_correctness` 适用性转换为两个新指标，不伪造 `ExternalJudgeFeedbackSet`，也不填写猜测的逐项 pending 或不适用。
- `generation`：逐检查点 `ExternalScoringReadiness`，状态为 `ready`、`no_generation_evidence` 或 `missing`；`missing` 逐项列出 `generation_checkpoint`、`generation_runner_error`、`request_boundary`、`answer`。
- `assertions`：真实 `AssertionCheckpointArtifact`，包含 `passed=false`、`binding_error`、`runner_error` 与证据。
- `incomplete`：断言失败、绑定/运行器错误、缺失断言检查点与缺失生成证据的可核验清单。预期断言按冻结场景 `assertions` 的 `(event_id, checkpoint_id)` 与种类集合逐项核对；同一检查点上的生成检查点存在不能抵消业务断言证据缺失，缺失记 `assertion_missing`，定义种类不符记 `assertion_definition_mismatch`。
- `blocked`/`runner_error`：分别保留执行阻断与失败依据；执行失败不会转成断言失败或评分成功。

外部模式不写 `ScenarioArtifact`，因此不生成 `scenario.json`、`partial.json` 与 `generation.jsonl`；断言结果写 `assertions.jsonl`。

### 12.4 文件布局与模式隔离

外部模式每个场景目录新增：

- `external-scoring.json`：上节本地结果。
- `external-scoring-state.json`：评分状态，含 `scoring_mode: "external"` 与执行摘要。
- `external-evaluation.json`：LangSmith evaluator 的本地反馈记录，含 `feedback_version: 3`、三个状态、`generation` 就绪情况与 `incomplete`。
- `blocked.json`/`failure.json` 在外部模式仍记录错误类型与原因，并额外记录 `scoring_mode`、三个状态与 `observed_events`/`observed_checkpoints`。

既有 Ragas 文件原样保留：`scoring.json`、`scoring-state.json`、`evaluation.json`。`scoring_guard` 校验状态中的 `scoring_mode`，缺少模式身份的旧状态只按 Ragas 解释；不同模式的评分状态直接拒绝复用。外部评分产物写入前检查目标存在，已存在时抛 `ScoringResultExists`；外部模式不读取、不覆盖 `scoring.json`。

### 12.5 LangSmith 外部运行协议

- 外部运行使用独立反馈版本 `3`。字段固定为 `execution_completed`、`assertion_passed`、`assertions_passed`、`assertions_failed`、`assertions_binding_errors`、`assertions_runner_errors`、`generation_scoring_completed`；外部模式 `generation_scoring_completed` 恒为 `0.0`。
- 历史反馈版本 `1` 与 `2` 的字段集合和解析语义不变；版本 `3` 不使用 Ragas 指标字段，`scenario_feedback`/`incomplete_items` 对外部版本明确报错并指向外部函数。
- `experiment_metadata.judge` 在外部模式记录 `{ownership: "langsmith", configuration: "external"}`，不编造裁判模型、Evaluator 或 `jev-latest` 身份；实际 Evaluator 身份由后续上传与回收证据记录。
- `finalize` 在外部模式只重算执行与断言状态、校验本地摘要与执行身份，保留 `pending_external`；完成且无问题的运行状态为 `awaiting_external_scoring`，不是 `complete`。断言失败、覆盖缺失或执行失败仍为 `incomplete`。
- 旧场景汇总 `evaluation.summary` 遇到 `scoring_mode=external` 的 `run.json` 直接抛 `SummaryError`；旧 LangSmith 汇总 `evaluation.sdk_summary` 遇到反馈版本 `3` 标记为不可用并记录“后续任务接线”，不按 Ragas 结果解析。

### 12.6 后续任务 3 的证据入口

任务 3 从本地运行目录读取：

- `backend/temp/scenario-langsmith/runs/<run_id>/scenarios/<example_id>/<scenario_id>/execution.json`：`generation_checkpoints` 保存完整 `GenerationCheckpoint`（用户输入、实际回答、结构化快照、模型输入上下文、reference/rubric）与 `RequestBoundaryEvidence`；`retrieval` 保存真实搜索与重排证据；`events` 保存工具调用与真实前后投影。
- 同一目录的 `external-scoring.json`：检查 `execution_sha256`、检查点就绪状态和 `incomplete`，但不上传其中的本地状态字段。
- `execution.generation_metrics` 的键给出需要外部评分的检查点集合；任务 3 必须显式提供 `ExternalJudgeApplicability` 与 `ExternalJudgeFeedbackSet`，不得从旧 Ragas 适用性推导。

### 12.7 未接线范围

- 未实现 `generation_upload.py` 的外部检查点上传、TypeSafe/Evaluator 调用、网页评分、反馈回收与 `decision_feedback.py`。
- 未修改正式汇总与发布门禁；外部记录目前不能被旧汇总接收，旧汇总按 12.5 明确拒绝。
- 未运行收费模型、LangSmith 网络调用或正式 80 场景全量评测。

### 12.8 验证状态

- `pytest test/test_external_scenario_mode.py -q --basetemp=temp/task2-external/pytest`：退出码 0，14 passed。覆盖显式模式解析、计划与历史默认、外部运行不进入 Ragas、失败断言/绑定错误/阻断执行、缺失生成检查点、封存执行与检索/重排证据保持、重复处理、evaluator 写入与复用、模式文件隔离、finalize 外部状态、旧汇总拒绝。
- `pytest test/test_langsmith_scenarios.py test/test_scenario_execution_scoring.py -q`：28 passed。
- `pytest test/test_external_judge_contracts.py test/test_evaluation_contracts.py -q`：44 passed。
- `pytest test/test_evaluation_summary.py -q`：28 passed。
- `pytest test/test_sdk_summary.py -q`：22 passed。
- `pytest test/test_evaluation_scenarios.py -q`：54 passed。
- `ruff check evaluation/scenarios.py evaluation/langsmith_scenarios.py evaluation/summary.py evaluation/sdk_summary.py test/test_external_scenario_mode.py test/test_langsmith_scenarios.py`：通过。
- `python -m evaluation.scenarios --all`：退出码 0，输出 80 场景、134 事件、127 检查点、254 个生成指标条目，`scoring_mode=ragas`，未触发模型调用。
- 未执行：外部模式的真实 LangSmith 上传/评分/反馈回读、真实模型场景与正式 80 场景外部全量运行。未运行项不得视为通过。

## 13. LangSmith 外部裁判迁移——任务 3：检查点投影与上传

### 13.1 交付边界

外部分支只实现“执行证据 → 检查点投影 → 独立数据集 → SDK 实验提交”。不读取旧 `load_run`/`build_summary`，不要求 `release.effective`、Ragas 分数或旧评分文件；不写 `scoring.json`、`generation.jsonl`、`scenario.json`，也不覆盖 `execution.json` 与 `external-scoring.json`。反馈回收、分数聚合、发布门禁分别属于任务 4、5，本轮未实现。

入口位于 `evaluation.generation_upload`，与旧 `check`/`upload` 共用脱敏、白名单、回执校验和原子写入工具，但没有复制旧上传模块，也没有新增通用 Provider 框架。

### 13.2 源证据校验

`load_external_run()` 接受 `backend/temp/scenario-langsmith/runs/<run_id>/`，要求：

- `sdk-run.json` 为 `fit-agent-scenario-langsmith-1`，`scoring_mode=external`、`feedback_version=3`、`protocol_version=fit-agent-external-scenario-protocol-1`，目录名与 `run_id` 一致，运行已终结（不是 `running`）。
- 运行数据身份与当前 `--dataset` 的四项摘要一致；`sample_scope.scenario_ids`、`example_ids` 与样本集合一致；样本不得缺少声明场景。
- 每个样本的 `scenarios/<example_id>/<scenario_id>/execution.json` 通过 `load_execution` 的 run、scenario、数据身份与冻结场景摘要校验，且 `generation_metrics` 与冻结 `scenario_plan` 一致。
- 同目录 `external-scoring.json` 通过 `load_external_scoring` 的执行摘要校验，并用 `score_execution_external(execution, scenario["assertions"])` 重算整份记录；身份、断言、就绪记录任一不一致即失败。
- 样本中的 `scoring_relative`、`scoring_sha256`、`execution_sha256`、`execution_status` 必须与真实文件一致。

拒绝 Ragas 运行、进行中运行、缺失样本、缺失执行/评分文件、摘要篡改与数据身份冲突。证据缺失不静默跳过，而是进入覆盖账本；整个场景执行缺失属于无效证据，直接失败。

### 13.3 适用性审阅与显式批准

文件固定在源运行目录：

- `external-judge/applicability.review.json`：审阅清单，版本 `fit-agent-external-judge-applicability-review-1`，状态 `draft_requires_user_approval`。
- `external-judge/applicability.json`：用户批准的输入，版本 `fit-agent-external-judge-applicability-1`。

审阅清单逐检查点给出场景 family/tags/message、证据是否存在（检查点、回答、请求边界、上下文条目、检索工具调用、readiness）、冻结参考要求、旧 Ragas 适用性，以及两项新指标的 `applicable=null`/`reason=null` 待填项。它不代替用户判断，也不把无回答当作不适用。

批准输入逐条目绑定 `execution_sha256`、`EvidenceLink` 与恰好两项 `ExternalJudgeApplicability`；两项指标必须完整且唯一，不适用必须写明原因。加载时拒绝：

- 缺少检查点条目或包含未知检查点；
- 重复检查点或重复指标；
- `run_id`、数据身份、事件、检查点或执行摘要冲突；
- 批准摘要 `approval.content_sha256` 与条目不一致；
- 审阅清单原样作为批准输入提交。

批准身份沿用数据审阅约定：`status=approved`、`reviewer`、`reviewer_type=user`、`reviewed_at`、`comment`、`decision_ref`、`content_sha256`。摘要材料为 `{schema_version, run_id, dataset, entries(按 scenario/event/checkpoint 排序)}` 的 canonical JSON；摘要不符时命令会打印期望值。旧 Ragas 适用性只作为审阅信息展示，不自动沿用。

### 13.4 检查点投影映射

对每个“任一指标适用且证据就绪”的检查点构造 `ExternalJudgeCheckpointUpload`：

| 契约字段 | 内容 | 来源 |
|---|---|---|
| `metadata.dataset` | 数据身份 | 封存执行 `dataset` |
| `metadata.checkpoint` | run/scenario/event/checkpoint | 封存执行与冻结场景 |
| `metadata.execution_sha256` | 执行摘要 | 本地原始 `execution.json` |
| `inputs.user_request` | 当前请求与 prior_messages | `GenerationCheckpoint.user_input` |
| `outputs.answer` | visible_text 与结构化快照 | `AnswerEvidence` |
| `outputs.context.items` | 实际 ContextItem、逐条裁剪字段 | `ModelInputContext.items` |
| `outputs.context.truncated/omitted_chars` | 整体裁剪信息 | `ModelInputContext` |
| `outputs.context.request_boundary` | 真实消息/工具节点边界 | `RequestBoundaryEvidence` |
| `outputs.context.retrieval` | 本次真实可见检索与重排证据 | `SearchStageRecord` + 上下文工具结果 |
| `reference_outputs.reference` | 参考答案、事实、必须/允许/禁止要求 | `GenerationReference` |
| `reference_outputs.applicability` | 两项显式批准 | `applicability.json` |

投影规则：

- 只有 `event_id` 与当前检查点一致、且对应工具结果实际出现在本次请求上下文中的检索记录才进入 `retrieval`；按 `tool_call_id` 与 `tool_name` 与上下文 `tool_result` 及 `request_boundary.tool_nodes` 双向绑定。检索工具结果缺少记录、名称不一致、被裁剪或与记录响应不一致均失败。
- `request_boundary.tool_nodes.is_error=true` 的检索工具调用没有检索阶段记录；该错误结果仍保留在 `context.items`，只跳过检索证据绑定。失败调用若仍带检索记录，直接失败。
- `SearchResponseEvidence` 与 `RerankEvidence` 直接复用既有契约；重排 `identity.endpoint` 只保存 `sha256_text(原始地址)`，原始 URL 不进入出站 payload。
- 候选池、重排分数（审计副本）、检索参数只写入本地 payload 的 `audit`，不混入上下文正文；SDK 出站只使用契约字段。
- 上下文条目的 `message_entry_id` 必须属于 `request_boundary.nodes`，工具结果必须属于 `request_boundary.tool_nodes`；不加入后续事件或参考答案。
- 对整份投影执行现有 `_sanitize_outbound`，再以 `ExternalJudgeCheckpointUpload.model_validate` 重新校验。`execution_sha256` 始终引用本地原始执行证据；脱敏只作用于出站副本。
- 脱敏后计算 `projection_sha256 = sha256(canonical_json(upload))` 与稳定 `row_id = uuid5(NAMESPACE_URL, "fit-agent-external-judge:<run>:<scenario>:<event>:<checkpoint>:<execution_sha256>")`。
- 旧白名单保持原语义；外部 payload 中的 `rerank.identity.endpoint` 为 64 位摘要，白名单复核时按既有 `endpoint_sha256` 约定替换后再扫描。外部白名单逐字符串值扫描禁止字段、路径模式与凭据标记；canonical JSON 的 `\n` 转义不作为盘符路径依据，避免把正文中的 `字:\n\n1.` 误判为本地路径。

覆盖账本把每个预期检查点归类为 `uploadable`、`missing`、`not_applicable` 或 `pending_review`，并记录 readiness、缺失项和场景状态。`missing` 不静默缩分母：payload/receipt 会保存 `coverage.missing` 与 `coverage_complete=false`，只用于诊断上传，不宣称运行完整或发布通过。

### 13.5 数据集、实验与回执

名称：

- 数据集：`fit-agent-external-judge-<run_id>-<applicability_sha256[:12]>`
- 实验：`fit-agent-external-judge-<run_id>-<projections_sha256[:12]>`

数据集 Example：

- `inputs` = 契约 `inputs`
- `outputs` = 契约 `reference_outputs`
- `metadata` = row_id、数据身份、检查点、execution_sha256、projection_sha256

SDK 字段投影：

- Example inputs 保存稳定用户请求；prior_messages 随执行快照进入 inputs，row_id 与执行摘要绑定。
- Example outputs 保存参考要求与显式适用性。
- 实验 Run outputs 保存实际 `answer`/`context`；target 只按输入摘要查找冻结 outputs，不执行任何业务、不调用生成模型或本地裁判。
- 实验使用固定名称 `client.create_project(..., reference_dataset_id=...)` 创建，再用 `client.aevaluate(target.arun, data=examples, experiment=project)` 提交既有实验；这是 SDK 官方实验路径，数据集绑定的 Evaluator 会在新实验上自动触发。SDK 0.14.11 只把协程函数识别为异步 target；传入可调用实例会被当作 runs 迭代。

同步与提交互相独立：`external-sync` 只创建/核验数据集；`external-submit` 必须已有数据集回执，且显式传入 `--confirm-external-judge-cost`。提交后只校验远端根运行数量、样例绑定、inputs/outputs 与元数据；不读取、不推断 feedback。回执固定记录：

- `external-judge/checkpoint-dataset.json`：数据集 ID/名称/页面地址、Example ID、payload/rows/applicability 摘要。
- `external-judge/checkpoint-experiment.json`：实验 ID/名称/页面地址、dataset ID、逐 row 的 example_id 与 run_id、checkpoint、execution_sha256、projection_sha256、覆盖状态。
- `external-judge/checkpoint-experiment.pending.json`：提交前写入的幂等标记；中断后只核验并补齐缺失运行，不自动重试失败运行。
- `checkpoints-payload.json`：完整脱敏投影与审计信息。
- `coverage-ledger.json`：覆盖账本。

同一身份重复执行只核验已有数据集/实验，不创建重复实验；回执、payload 或批准摘要不一致直接拒绝。远端已存在同名实验时先核验元数据与运行内容，匹配才复用。旧回执不覆盖。外部评分状态始终为 `pending_collection`，上传成功只代表数据已提交。

### 13.6 命令

```powershell
uv run --locked --directory backend python -m evaluation.generation_upload external-review --source D:/Repository/Fit-Agent/backend/temp/scenario-langsmith/runs/<run_id>
uv run --locked --directory backend python -m evaluation.generation_upload external-project --source D:/Repository/Fit-Agent/backend/temp/scenario-langsmith/runs/<run_id>
uv run --locked --directory backend python -m evaluation.generation_upload external-sync --source D:/Repository/Fit-Agent/backend/temp/scenario-langsmith/runs/<run_id>
uv run --locked --directory backend python -m evaluation.generation_upload external-submit --source D:/Repository/Fit-Agent/backend/temp/scenario-langsmith/runs/<run_id> --confirm-external-judge-cost
```

`external-review` 与 `external-project` 不发起网络请求；`external-sync` 只提交数据集；只有 `external-submit` 会触发 Evaluator。所有命令默认数据集目录为 `backend/dataset/evaluation/rebuild-v3`，可用 `--dataset` 覆盖。

### 13.7 网页 Evaluator 配置

1. LangSmith 密钥写入被 gitignore 的 `backend/.env`，变量名 `LANGSMITH_API_KEY`；不粘贴到任务文档或对话。
2. 在 LangSmith `Settings > Integrations > Provider secrets` 点击 `+ Secret`，选择 `TypeSafe`，保存为工作区 secret `TYPESAFE_API_KEY`；若使用自定义名称，在 Evaluator 的 API Key Name 中填写同名。
3. 打开 `external-sync` 返回的检查点数据集，进入 `Evaluators` 标签，点击 `+ Evaluator`，选择 `LLM-as-a-Judge Evaluator`，模型配置选择 `TypeSafe` 与 Jev（`jev-latest`）。决策模型 Evaluator 只能在网页创建，SDK 不支持创建。
4. State 映射四个变量。网页编辑器中的来源分别选择 Input、Output、Output、Reference output；下列是 SDK Example/Run JSON 的实际路径：
   - `user_request` ← `inputs.user_request`（消息在 `inputs.user_request.message`，历史在 `.prior_messages`）
   - `answer` ← `outputs.answer`（可见文本在 `outputs.answer.visible_text`）
   - `context` ← `outputs.context.items`（只取模型实际可见的上下文条目）
   - `reference` ← `reference_outputs.reference`（要求在 `.must_include`、`.forbidden_claims`、`.allowed_variation`）

   `outputs.context.request_boundary` 与 `outputs.context.retrieval` 保留为离线审计材料，不映射到评分 State；裁判不得读取请求节点中的完整工具正文或内部重排候选与分数。
   上述路径已与本地投影的 Example.inputs、Run.outputs、Example.outputs 逐字核对；真实 SDK 回读结果见第 13.8 节。
5. 两项 Question 均为 Noul，名称即 feedback key：
   - `evidence_supported_v1`：判断回答中的事实陈述是否全部由提供的证据上下文支持。只评估动作名称、器械、目标肌群、重量口径、步骤与用户已有事实；忽略寒暄、训练建议、计划安排和没有事实内容的表述。回答中的任一具体事实若在证据上下文中没有依据，或与证据上下文矛盾，则整条回答为否。真：全部事实陈述都能在证据上下文中找到依据，且没有与证据矛盾的事实。假：至少包含一条没有依据或与证据矛盾的具体事实。
   - `reference_requirements_met_v1`：判断回答是否满足参考答案中的任务要求。必须同时检查：`reference.must_include` 列出的内容全部出现；`reference.forbidden_claims` 列出的任一表述都没有出现；`reference.allowed_variation` 只用于允许的替代表述，不能替代必须内容。参考事实只用于核对事实，不要求逐字复述。缺少任一必须内容或出现任一禁止声明，则整条回答为否。真：全部必须内容出现且没有禁止声明。假：缺少至少一项必须内容或出现至少一项禁止声明。
6. Preview 至少应显示检查点数据集中的真实 `inputs` 与 `reference_outputs`；若面板同时提供 Run 预览，应显示真实 `answer` 与 `context`。`context` 只能呈现 `outputs.context.items` 的条目，请求边界中的完整工具正文与检索审计不得出现。路径映射后为空、仍是占位内容或与数据集内容不符时，不要保存/启用 Evaluator。数据集同步完成、适用性批准之后再启用 Evaluator；启用后每次 `external-submit` 创建新实验都会触发两项 Question。
7. 提交会产生真实 Jev 调用与费用（每行两条 Question）。反馈回收与分数读取属于任务 4；本轮不写 `succeeded` 反馈、不预写分数、不假定 run metadata 会出现在 feedback 中。

### 13.8 验证状态

- `uv run --locked python -m pytest test/test_external_judge_checkpoints.py -q --basetemp=temp/task3-external/final-fix`：16 passed。覆盖审阅/未批准阻断、投影字段、回答/上下文/边界、工具绑定、失败检索工具结果、脱敏、裁剪、重排地址摘要、候选池审计隔离、白名单转义误判、适用性缺失/重复/未知/冲突/摘要错误、真实生产执行证据、Example/回执身份、远端运行核验与 CLI 成本闸门。
- 相邻回归 `test_evaluation_summary.py test_external_judge_contracts.py test_external_scenario_mode.py test_langsmith_scenarios.py`：85 passed，旧 Ragas 上传入口保持通过。
- `ruff check evaluation/generation_upload.py test/test_external_judge_checkpoints.py`：通过。
- 2026-10-21 正式外部运行 `20261021T031501Z-e91b6334`：80 个期望场景，74 complete、6 failed；5 个失败样本缺少 `external-scoring.json`，整次运行被 `external-review` 判为无效，禁止通过缩小分母绕过。失败场景为 `s-profile-update-06`、`s-plan-import-file-02`、`s-performance-adjustment-04`、`s-clarification-authorization-medical-risk-01/03/07`。
- 2026-10-21 定向验证运行 `20261021T034031Z-b5fb95de`：6/6 样本具备 `execution.json` 与 `external-scoring.json`；`external-review` 通过，expected 15、uploadable 8、not_applicable 7、missing 0、pending_review 0。运行状态为 `incomplete`（`s-action-verification-01` 执行阻塞），7 个检查点经用户显式批准为不适用；回执 `coverage_complete=true` 表示没有未处理的 uploadable/missing，8/15 的覆盖范围仍属诊断上传。
- 真实证据暴露并修复三处缺陷：失败的检索工具结果按 `request_boundary.tool_nodes.is_error` 跳过检索证据绑定；外部白名单改为逐字符串扫描，避免 canonical JSON 的 `\n` 被误判为盘符路径；SDK target 改用协程方法 `arun`，避免可调用实例被当作 runs。
- 数据集已同步：`fit-agent-external-judge-20261021T034031Z-b5fb95de-6cfd1f851629`，ID `b342ad9f-365a-4512-adab-758cf1ebd09e`，页面 `https://smith.langchain.com/o/9b70e395-fb97-4ba4-922f-b6246212e102/datasets/b342ad9f-365a-4512-adab-758cf1ebd09e`，8 个 Example；回执为运行目录 `external-judge/checkpoint-dataset.json`。
- 实验已提交：`fit-agent-external-judge-20261021T034031Z-b5fb95de-1cdf82ccc3a4`，ID `c26774c1-7ba9-4afe-8bed-266590d5a53b`，页面 `https://smith.langchain.com/o/9b70e395-fb97-4ba4-922f-b6246212e102/projects/p/c26774c1-7ba9-4afe-8bed-266590d5a53b`，8 个 run 与 Example 逐行绑定；`external_scoring_status=pending_collection`，回执为 `external-judge/checkpoint-experiment.json`。首个实验 `88b2de48-3a84-4430-a721-ddb7998be261` 经用户授权删除，旧回执保留为 `external-judge/checkpoint-experiment.context-limit.json`。
- 网页 Evaluator 已触发两轮：首轮 `context` 映射完整 `outputs.context`，16 条 feedback 全部为 `The model's context limit was exceeded`；改为 `outputs.context.items` 后，8 个 run 中 5 个产生真实分数（`evidence_supported_v1` 0.58–0.82，`reference_requirements_met_v1` 0.38–0.59），3 个仍超限：`s-confirmation-cancellation-state-07/cp4`、`s-confirmation-cancellation-state-05/cp2`、`s-confirmation-cancellation-state-05/cp1`，对应上下文 69k–77k 字符。
- 剩余 3 行的候选收窄路径为 `outputs.context.items[*].content`（仅条目正文，61k–66k 字符），需确认网页路径选择器是否支持数组通配；不支持时需新增紧凑评分子段，属于契约变更。
- 未完成：8/8 成功 Jev 分数、真实云端反馈回收与聚合。任务 4 已提供回收实现，但本轮未执行真实云端 collect；任务 5 未实现。

## 14. LangSmith 外部裁判迁移——任务 4：反馈回收与评分证据落盘

### 14.1 交付边界

任务 4 的入口为 `evaluation.decision_feedback`，提供 `check` 与 `collect` 两个命令。它只读取任务 3 已提交的实验回执、逐检查点读取 run-level feedback，并把归一化结果保存为版本化证据。它不创建实验、Run、Example 或 feedback，不触发业务执行、生成模型、Embedding、reranker、Jev 或任何本地裁判，不接入发布门禁，不计算质量总分，不新增分数阈值。

不修改 `execution.json`、`external-scoring.json`、`checkpoints-payload.json`、`coverage-ledger.json`、任务 3 数据集/实验回执或历史 Ragas 文件。所有新产物位于源运行目录的 `external-judge/decision-feedback/`。

复用 `generation_upload.py` 的源证据校验、适用性加载、投影重建、回执身份校验、根运行收集、脱敏与原子写入能力。旧协议拒绝语义保持不变：`summary.py` 与 `sdk_summary.py` 继续拒绝外部协议，`decision_feedback.py` 不调用旧 `load_run`/`build_summary` 接纳外部运行。

### 14.2 显式反馈绑定

任务 3 的提交回执不包含 Evaluator 配置身份。回收前必须提供：

`external-judge/decision-feedback/feedback-binding.json`

版本为 `fit-agent-external-judge-feedback-binding-1`。字段：

| 字段 | 内容 |
|---|---|
| `run_id` | 源运行 ID，必须等于运行目录名与回执 `run_id` |
| `source_sha256` | 源证据摘要，必须等于 `sdk-run.json` 与执行/评分文件的 canonical 摘要 |
| `applicability_sha256` | 已批准适用性摘要；有上传行时等于 payload 的 `applicability_sha256`，全不适用时等于批准条目的 `content_sha256` 材料 |
| `experiment` | 有上传行时必填：实验 ID/名称、数据集 ID/名称、`payload_sha256`、`rows_sha256`、`projections_sha256`；全不适用时为 `null` |
| `evaluator.provider` | 人工声明的 Provider，例如 `TypeSafe` |
| `evaluator.evaluator` | 人工声明的 Evaluator 名称 |
| `evaluator.evaluator_id` | 人工声明的 Evaluator 标识；当前软链策略只把它作为声明记录 |
| `evaluator.model_alias` | 模型别名，例如 `jev-latest` |
| `evaluator.config` | Evaluator 配置快照；不含认证头、API Key、Cookie、Token 或本地路径 |
| `evaluator.config_sha256` | `sha256(canonical_json(config))` |
| `state_mapping` | 固定为 `inputs.user_request`、`outputs.answer`、`outputs.context.items`、`reference_outputs.reference` |
| `metrics` | 恰好两项：`evidence_supported_v1` 与 `reference_requirements_met_v1` |

每项指标包含 `feedback_key`、`question_id`、`question`、`question_sha256`、可空 `rubric_id`、`rubric`、`rubric_sha256`。摘要公式：

- `question_sha256 = sha256(canonical_json({"question_id": question_id, "question": question}))`
- `rubric_sha256 = sha256(canonical_json({"rubric_id": rubric_id, "rubric": rubric}))`

两个 `feedback_key` 必须唯一；`context` 映射必须逐字为 `outputs.context.items`。`config` 中的禁止字段、凭据标记、本地路径直接拒绝。

来源保证口径：本任务的 `succeeded` 归档采用“显式绑定 + 服务返回 `feedback_source.metadata.typesafe`”的软链。`feedback_source.type` 必须为 `model` 或 `evaluator`；成功分数必须同时具备 `typesafe` 对象。该口径不要求 SDK Evaluator 注册表唯一匹配，因此归档固定写入 `source_assurance=declared_binding`，不宣称服务已唯一证明 Evaluator 身份。自定义 run metadata、同名 feedback key、人工声明本身都不作为独立来源证明。

### 14.3 离线核验（`check`）

`check` 不创建 LangSmith Client、不访问网络，校验：

- `load_external_run` 的 external 协议、数据身份、冻结场景、封存执行与 `external-scoring.json` 重算结果。
- `external-judge/applicability.json` 覆盖全部预期检查点，且批准摘要、执行摘要、事件与检查点身份一致。
- 有上传行时重建 `checkpoints-payload.json`，逐字比对磁盘 payload；校验 `rows_sha256`、`projections_sha256` 与逐行 `projection_sha256`。
- `checkpoint-dataset.json` 与 `checkpoint-experiment.json` 的 dataset/source/applicability/rows/payload/projections 摘要、Example ID、run 映射与逐行投影。
- `checkpoint-experiment.pending.json` 存在且没有回执时报告“提交未完成”并阻断；回执与 pending 并存同样阻断。
- 已有 `decision-feedback/index.json` 与批次文件；索引来源、绑定摘要、实验身份、批次内容摘要或检查点集合漂移时阻断。
- 全不适用且没有上传行的运行允许没有 payload、数据集回执与实验回执；存在陈旧回执或 pending 标记时阻断。

`check` 只报告状态，不写文件。没有回收批次时返回 `pending` 与 `collection_absent`。

### 14.4 云端读取（`collect`）

`collect` 先执行与 `check` 相同的离线核验，再读取指定实验：

- 从 `checkpoint-experiment.json` 读取实际 `experiment_id`、`dataset_id`、`runs(row_id -> run_id)` 与逐行投影。禁止按实验名称搜索、选择最近实验/运行或最新反馈。
- 通过 SDK 读取数据集与实验项目，校验实验名称、`reference_dataset_id`、实验 metadata、数据集名称/metadata、Example 的 inputs/outputs/metadata；Example 的参考要求与适用性必须等于已批准出站内容。
- 读取全部根运行，校验 `session_id` 属于回执实验、`reference_example_id` 等于 `row_id`、inputs/outputs 等于冻结投影；缺失、未知、重复根运行或 run 映射漂移直接阻断。
- 使用安装版 SDK 的 `list_feedback(run_ids=...)` 读取每个根 Run 的全部 run-level 反馈；SDK 负责分页。只读取回执实验的根 Run 集合，不读取实验汇总/session-level feedback，不把汇总反馈补作检查点反馈。
- 默认单次读取。`--wait-seconds` 只接受有限非负数；有界轮询使用 `time.monotonic()` deadline，单次 SDK 请求 `timeout_ms` 不超过剩余时限，并关闭 SDK 重试（`Retry(total=0)`）。到期仍缺失时保存已验证结果与 `pending`，命令结束；没有后台轮询或无限重试。
- `LANGSMITH_API_KEY` 从进程环境或 `backend/.env` 读取；命令不要求粘贴密钥，不打印密钥。

反馈来源字段：

- `succeeded` 要求 `feedback_source.type` 为 `model`/`evaluator`，且 `feedback_source.metadata.typesafe` 为对象。
- `failed` 要求同一来源类型，且反馈对象带 `extra.error=true`。真实错误对象保留原始 score（通常为 `null`）、comment、extra 与来源；不凭缺失反馈、超时或网络失败推断云端失败。
- 模型版本只读取 `feedback_source.metadata.typesafe.model`。值等于声明别名或以 `-latest`/`-preview` 结尾时记为 `UnreportedModelVersion`；其他非空具体版本记为 `ReportedModelVersion`。配置别名不冒充解析版本。

### 14.5 严格归一化

分数只接受真实、有限的 0..1 数值。`0.0` 与 `1.0` 有效；bool、字符串、NaN、Infinity 与越界值拒绝。禁止 `float()` 强制转换、裁剪、默认分或用 value/comment 猜分数。Noul 分数按概率原样保存。

每个预期检查点生成恰含两项指标的 `ExternalJudgeFeedbackSet`：

- `pending`：适用指标尚无有效终态反馈，或反馈对象缺少分数与错误标记。
- `succeeded`：分数合法，来源与绑定全部通过。
- `failed`：`extra.error=true` 且来源类型为自动裁判，保留 comment 或真实错误结构作为失败依据。
- `not_applicable`：来自本次批准的适用性及原因；无真实云端反馈时不创建 `ScoringAttempt`。

候选处理：

- 相同 feedback ID 在分页中重复且语义内容相同，按 ID 与语义摘要去重。
- 相同 feedback ID 内容漂移，保留全部版本并阻断；不静默替换历史证据。
- 同一指标出现多个不同 feedback ID 的终态反馈，保留全部候选与评分尝试，报告冲突，不自动选最高分、最近一条或平均分。
- 无关 key 写入排除原因；同名但来源为 `api`/`annotation` 或缺少 `typesafe` 的反馈不满足指标；来源缺口阻断可信完成。
- 不适用指标意外收到云端分数时保存原始审计证据并排除出成功统计，`FeedbackSet` 保持不适用。

`ExternalJudgeScoringAttempt` 只在存在真实云端反馈时创建；`attempt_id` 由本地运行、检查点、执行摘要、指标与 feedback ID 派生，云端 `feedback_id` 单独保存在 `cloud_feedback`。没有反馈对象时不创建 pending 尝试，也不补造 feedback ID。终态尝试必须绑定云端实际返回身份。

原始证据在落盘前执行既有脱敏：本地盘符与会话路径替换为不透明 token，凭据标记、认证字段与实际 LangSmith 密钥直接阻断。`evidence_sha256` 绑定原始证据列表、`FeedbackSet` 与 `ScoringAttempt`，离线可重新校验。

### 14.6 覆盖与不适用

回收批次继承任务 3 覆盖账本：`expected` 包含全部预期检查点，`uploadable`、`missing`、`not_applicable`、`pending_review` 不缩小分母。已批准适用但缺少上传证据的检查点保持 `pending`，诊断记录 `checkpoint_missing_upload` 与真实缺失项、执行状态；不适用不会消除证据缺失或源执行错误。

仅含不适用项且没有上传行的运行可以离线核验与记录：`collect` 不访问网络，生成全 `not_applicable` 批次并返回 `complete`；`experiment` 为 `null`，没有 `ScoringAttempt`。

### 14.7 版本化落盘

布局：

```
external-judge/decision-feedback/
  feedback-binding.json
  index.json
  batches/batch-<content_sha256[:16]>.json
```

- 批次版本：`fit-agent-external-judge-feedback-collection-1`。
- 索引版本：`fit-agent-external-judge-feedback-index-1`。
- 批次保存：源运行/数据身份、回执摘要、读取时间、绑定快照与摘要、实验摘要、覆盖账本、逐检查点 `FeedbackSet`、可成立的 `ScoringAttempt`、原始反馈证据、排除原因、诊断、`evidence_sha256`、状态与阻断依据。
- `content_sha256` 按 canonical JSON 计算，排除 `read_at`；`read_at` 只表示读取时间。
- 批次先原子写入并重新校验，再原子更新索引；索引损坏、批次缺失/损坏、来源/绑定/实验摘要变化时拒绝继续。
- 相同 feedback ID 与内容重复回收复用已有批次，不新增批次或评分尝试；新反馈、重评分候选或内容变化写入新批次，旧批次保留。
- 历史批次中的 feedback ID 未出现在本次读取，或同 ID 语义内容相对历史变化，当前批次标记阻断并保留两侧证据。

索引与批次的 `status`：

- `complete`：所有适用指标已得到合法终态（`succeeded` 或 `failed`），没有校验阻断。该状态只表示评分证据完整，不表示发布通过或质量达标。
- `pending`：仍有适用指标等待反馈，或仍有已批准适用但未上传的检查点。
- `blocked`：存在非法分数、来源缺口、终态冲突、内容漂移、缺少云端运行、身份/摘要不一致等校验阻断。

### 14.8 命令与退出码

```powershell
uv run --locked --directory backend python -m evaluation.decision_feedback check --source D:/Repository/Fit-Agent/backend/temp/scenario-langsmith/runs/<run_id>
uv run --locked --directory backend python -m evaluation.decision_feedback collect --source D:/Repository/Fit-Agent/backend/temp/scenario-langsmith/runs/<run_id>
uv run --locked --directory backend python -m evaluation.decision_feedback collect --source D:/Repository/Fit-Agent/backend/temp/scenario-langsmith/runs/<run_id> --wait-seconds 60
```

默认数据集目录为 `backend/dataset/evaluation/rebuild-v3`，可用 `--dataset` 覆盖。`check` 与 `collect` 输出 JSON 摘要，字段包括 `status`、`network`、`coverage`、`checkpoints`、`metrics`、`blocking`、`batch_id`、`batch_path`、`index_path`、`source_assurance`、`reads`、`reused`。

退出码：

| 退出码 | 含义 |
|---|---|
| 0 | `complete`：评分证据已齐备 |
| 2 | `pending`：仍待回收或尚未产生回收批次；argparse 参数错误同样退出 2 |
| 3 | `blocked`：校验阻断或云端配置缺失，命令输出 `status=blocked` |
| 1 | 未捕获的 SDK/网络/系统异常按 Fast-fail 传播 |

`collect` 不提供重评分命令，不创建实验或反馈，不调用外部裁判模型。真实云端回收的前置条件：有效 `checkpoint-experiment.json`、匹配的 `feedback-binding.json`、用户对本次只读云端调用的独立授权、可用的 `LANGSMITH_API_KEY`。

### 14.9 任务 5 读取入口

任务 5 从以下稳定入口读取：

- `external-judge/decision-feedback/index.json` 的 `latest`；
- `external-judge/decision-feedback/batches/<latest>.json` 的 `source.coverage`；
- 逐检查点 `checkpoints[].feedback_set` 与 `checkpoints[].attempts`；
- 批次 `status`、`blocking`、`source_assurance` 与 `content_sha256`。

聚合与门禁只消费已验证批次；`complete` 不替代质量阈值或发布审批。任务 5 已按该入口实现，见第 15 节。

### 14.10 验证状态

- `uv run --locked python -m pytest test/test_decision_feedback.py -q`：45 passed，退出码 0，283.08s。
- 相邻非 reranker 回归：任务 1～3 契约/检查点/模式 54 passed；`test_evaluation_contracts.py` 与 `test_evaluation_summary.py` 48 passed；`test_langsmith_scenarios.py` 与 `test_evaluation_dataset.py` 56 passed；`test_evaluation_scenarios.py` 与 `test_scenario_execution_scoring.py` 63 passed。各批退出码均为 0。
- `ruff check evaluation/decision_feedback.py test/test_decision_feedback.py`：通过。
- 本轮使用任务 3 真实错误日志中的 context-limit comment 与 `extra.error=true` 作为离线失败归一化样本；该样本只证明真实错误结构可被识别，不证明本次云端成功回收。
- 本轮未执行：真实云端 `collect`、任何生成模型/Embedding/reranker/外部裁判模型调用、Ragas 重评分、任务 5 聚合与发布门禁。
- 本轮未运行 reranker/Cross-Encoder 专项与真实服务测试：`test_retrieval_rerank_evaluation.py`、`test_retrieval_rerank_stages.py`、`test_retrieval_rrf.py`、`test_retrieval_stages.py`、`test_retrieval_mteb.py`、`test_retrieval_runtime_integration.py`、`check_retrieval_rerank_production.py` 等。既有实现与测试保持原状。

## 15. LangSmith 外部裁判迁移——任务 5：汇总、发布门禁与离线集成验证

### 15.1 交付边界

操作手册见 `optimization/external-judge-operations.md`。

外部分支新增 `evaluation/external_summary.py`，负责外部运行的离线加载、已验证批次消费、适用性分母、场景宏平均、执行与断言门禁、独立报告 Schema。`evaluation/sdk_summary.py` 承担 SDK 综合入口的协议分发、检索门禁组合与 `report`/`check`/`write-v3` 接线；`evaluation/summary.py` 继续只处理本地 `run.json` 布局，并对 `scoring_mode=external` 明确拒绝。

旧 Ragas 汇总、指标名、Schema、CLI 默认行为、检索分组与阈值全部保持原语义。外部分数不写入 `faithfulness`、`factual_correctness` 或 `MetricOutcome`。`write-v3` 只在显式调用时更新目标文件；本轮未用任何外部结果改写 `quantitive-index/v3.md`。

### 15.2 显式协议分发

- `sdk_summary.build_summary` 先读取 `sdk-run.json` 的 `feedback_version` 与 `scoring_mode`：反馈版本 `3` 进入外部分支，历史版本进入原 Ragas 分支。
- `feedback_version` 缺失按历史版本 `1` 解释；未知版本、`scoring_mode` 未知、模式与反馈版本冲突、`protocol_version` 与模式不匹配直接拒绝。
- 外部运行必须显式保存 `protocol_version=fit-agent-external-scenario-protocol-1`；缺少该字段拒绝。
- `summary.load_run` 读取本地 `run.json` 时校验 `scoring_mode` 与 `protocol_version`：外部模式或外部协议一律拒绝，并提示使用 `evaluation.sdk_summary`。外部源只支持 SDK 运行目录布局 `<run_id>/sdk-run.json`。
- 分发只依据已保存字段，不读取密钥、不依据文件是否存在推断模式、不做自动回退。

### 15.3 离线加载与共享校验

外部汇总调用：

- `load_external_run`：校验 external 协议、数据身份、运行范围、样例/场景集合、封存执行、冻结场景摘要、`external-scoring.json` 重算结果与样本摘要。
- 请求边界：沿 `answer_message_id` 找到同一回答的最后一次真实模型调用，读取该场景 `model-calls` 下的请求全文，校验 `request_path` 不越界、文件存在、`request_sha256` 一致、调用 API/Provider/Model 与 `sdk-run.json` 生成身份一致，并按 `request_boundary` 重放 `ContextItem` 的正文、来源、工具身份与裁剪信息。检查点 `answer.visible_text` 必须等于事件捕获的 `answer_text`，`answer.structured_snapshot` 必须等于事件捕获的 `answer_structured`。请求缺失、摘要不符、调用身份不符、重放不一致或回答与事件捕获不一致均进入阻断。
- `load_external_applicability`：校验已批准适用性覆盖、执行摘要、批准摘要与检查点身份。
- `decision_feedback.load_verified_collection`：与 `check_feedback` 共用 `load_feedback_context`、`_load_collection` 与新增 `verify_batch`；逐项绑定当前源证据、payload 行、`projection_sha256`、实验回执 `runs` 映射、评分尝试、原始反馈证据、feedback key、模型/Evaluator 配置与问题/评分标准。
- `verify_batch` 使用 `_normalize_candidate` 从匹配的原始反馈重新计算终态状态、分数、失败依据与 reported/unreported 版本，并用 `_attempt` 重放评分尝试的来源、模型、定义、出站投影与云端身份。归一化结果、原始反馈与已保存尝试不一致时阻断。
- `verify_batch` 对每个指标从全部映射后的原始 evidence 重算有效终态候选集合：保存的评分尝试必须逐项且完整对应该集合，不能遗漏有效候选；`feedback_conflict` 诊断由原始集合决定，存在多个终态时必须存在该诊断，单一或零终态时不得保留该诊断。
- 不创建 LangSmith Client、不执行 `collect`、不访问网络、不写源证据或任务 1～4 的封存文件。

拒绝与诊断：

- 不可解析的 `sdk-run.json`、未知协议、目录名与 `run_id` 不一致、仍在运行、缺少 `sdk-run.json`：命令以 `ExternalSummaryError` 退出。
- 源证据、适用性、绑定、payload、回执、索引或批次校验失败：`UploadBlocked` 转换为报告阻断项；预期分母仍由冻结计划与声明范围构造。
- 没有 `decision-feedback/index.json`：`feedback_status=absent`，逐项显示 `pending`/`missing`/`not_applicable`，不生成空报告。
- 索引存在时只消费 `index.latest`。`latest` 为 `pending` 或 `blocked` 时如实呈现，不回退历史 `complete` 批次。
- 同一指标出现多个终态候选时批次阻断，该指标不产生数值；两项指标不跨尝试拼接。

### 15.4 分母与宏平均

预期键按已批准适用性逐指标构造：`(scenario_id, event_id, checkpoint_id, metric)`。正式范围的预期场景为批准冻结数据集全部场景；定向范围为 `sample_scope.scenario_ids`。缺失场景、缺失事件、缺失检查点与未上传反馈不缩小分母。

指标恒等关系：

- `expected = applicable + not_applicable`
- `applicable = succeeded + failed + pending + missing + blocked + unapproved`
- 检查点分类来自任务 3 覆盖账本；指标状态来自任务 4 批次。两者是独立维度，`blocked` 诊断不与非阻断状态互斥相加。

聚合：

- 场景内：该指标所有成功适用检查点分数的算术平均。
- 场景间：有数值场景的等权宏平均。
- `mean_including_partial`：至少一项指标成功的场景。
- `mean_complete_only`：该指标全部适用检查点成功且至少有一个成功数值的场景。
- N/A 场景 `value=null`，不进入数值分母；失败、缺失、待回收、阻断与未批准项不填 0；真实 `0.0` 与 `1.0` 属于成功分数。
- 无可用数值时均值为 `null`，JSON 以 `allow_nan=False` 写出。

### 15.5 发布门禁

`release.effective=true` 要求同时满足：

- 请求范围与来源范围均为正式全量，场景集合等于批准冻结数据集。
- 每个场景执行状态为 `completed`，事件与检查点覆盖完整，无运行错误与无法绑定的上下文缺口。
- 每个生成检查点的回答关联真实模型调用；回答正文与结构化快照等于事件捕获记录；请求全文路径、`request_sha256`、生成模型身份与上下文重放全部一致。
- 冻结确定性断言全部执行且全部通过，无绑定错误、运行器错误与缺失检查点。
- 源运行数据集、代码、生成模型、物理索引、并发与重复次数身份满足既有校验。
- 任务 3 数据集/实验回执、payload、适用性、绑定摘要与任务 4 索引/批次逐项一致；评分尝试由原始反馈按生产语义重算后与已保存状态、分数、失败依据、模型版本和来源身份一致；原始候选的完整终态集合与评分尝试集合逐项对应，冲突诊断由原始集合决定。
- 最新回收批次为 `complete`；两项外部指标的全部适用检查点 `succeeded`，不存在 `failed`、`pending`、`missing`、校验 `blocked` 或未批准适用性。
- SDK 检索来源为 `formal`，三组覆盖、物理索引、并发与冻结参数满足既有 `_retrieval_formal_issues` 要求；场景与检索的数据/代码/索引身份一致。

明确边界：

- `batch.status=complete` 只表示适用指标均有合法终态，含 `failed` 时发布仍阻断。
- 全 N/A 生成评分不提供数值，不替代执行、断言与检索条件；显式批准下的全 N/A 可按既有产品规则进入发布判定。
- 定向范围、缺失检索产物、局部证据不能通过 `--scope=formal` 变为有效发布。
- 外部契约没有生成分数通过阈值；合法低分与 0 分不触发新的质量阈值。报告区分“证据与流程门禁满足”和“模型质量达标”；没有人工审阅时 `quality_reviewed=false`。

### 15.6 报告、CLI 与来源限制

外部 JSON 使用独立版本 `fit-agent-external-judge-summary-1`，报告字段包括：协议与反馈版本、两项指标定义与聚合口径、原始场景实验与检查点裁判实验、索引/最新批次/`content_sha256`/`binding_sha256`、`source_assurance=declared_binding` 与限制、Provider/Evaluator/模型别名/reported 与 unreported 版本、数据/代码/执行/投影身份与来源路径、覆盖分母、场景值、断言、检索、阻断项与 `release.effective`。

命令：

```powershell
uv run --locked --directory backend python -m evaluation.sdk_summary check --retrieval <retrieval-run> --scenarios <sdk-run-dir> --dataset backend/dataset/evaluation/rebuild-v3
uv run --locked --directory backend python -m evaluation.sdk_summary report --retrieval <retrieval-run> --scenarios <sdk-run-dir> --dataset backend/dataset/evaluation/rebuild-v3 --output <output-dir>
uv run --locked --directory backend python -m evaluation.sdk_summary write-v3 --retrieval <retrieval-run> --scenarios <sdk-run-dir> --dataset backend/dataset/evaluation/rebuild-v3 --output <output-dir> --v3 quantitive-index/v3.md
```

`check` 退出码 `0` 表示有效发布，`1` 表示阻断；`report` 与 `write-v3` 退出码 `0` 表示文件写入完成，发布结论在 JSON 与报告中。`--scope` 接受 `auto`、`formal`、`validation`。`check` 与 `report` 不触发 collect、上传、重评分或业务重跑；`write-v3` 只在显式调用时写目标文档。

来源限制固定写入 JSON 与报告：`declared_binding` 只证明显式绑定、feedback key 映射、自动裁判来源类型与 `feedback_source.metadata.typesafe` 对象；不宣称服务端唯一证明 Evaluator 身份；服务未返回具体版本时只记录 `unreported`，不推断可复现版本。上述限制与具体模型版本未返回都不新增发布阻断；任务 4 的来源缺口、身份冲突、内容漂移与配置漂移阻断保持不变。

### 15.7 验证状态

2026-10-21 由 subagent 实际运行，全部离线，无网络、无真实模型或云服务调用。每条命令退出码均为 0。

| 命令 | 结果 | 耗时 |
| --- | --- | --- |
| `uv run --locked --directory backend python -m pytest test/test_external_summary.py -q -k "normalized_feedback or request_evidence or request_digest or request_context or zero_one_probability or unreported_versions or approved_applicability" --basetemp=temp/p1-fix-targeted` | 8 passed，22 deselected | 74.99s |
| `uv run --locked --directory backend python -m pytest test/test_external_summary.py -q --basetemp=temp/p1-fix-full` | 30 passed | 260.48s |
| `uv run --locked --directory backend python -m pytest test/test_external_judge_checkpoints.py -q --basetemp=temp/p1-fix-checkpoints` | 16 passed | 27.32s |
| `uv run --locked --directory backend python -m pytest test/test_decision_feedback.py -q --basetemp=temp/p1-fix-feedback-a -k "two_feedback_zero or invalid_scores or missing_metric_pending or bounded_wait or real_cloud_failure or remote_run_identity or example_reference or source_gap or duplicate_same_id or different_feedback_ids or history_conflict or missing_upload or unexpected_feedback_for_not_applicable or all_not_applicable"` | 20 passed，25 deselected | 104.51s |
| `uv run --locked --directory backend python -m pytest test/test_decision_feedback.py -q --basetemp=temp/p1-fix-feedback-b -k "check_is_offline or raw_evidence or batch_diagnostics or persistence_idempotent or corrupt_index or binding_rejects or terminal_attempt or malformed_feedback_without_identity or experiment_receipt_drift or binding_digest_drift or binding_identity_drift or missing_or_pending_experiment or payload_projection_drift or feedback_scope or wait_seconds or cli_exit_codes"` | 25 passed，20 deselected | 204.18s |
| `uv run --locked --directory backend python -m pytest test/test_sdk_summary.py -q --basetemp=temp/p1-fix-sdk` | 22 passed | 97.85s |
| `uv run --locked --directory backend python -m pytest test/test_evaluation_summary.py -q --basetemp=temp/p1-fix-summary -k "not rerank and not cross_encoder"` | 22 passed，6 deselected | 160.57s |
| `uv run --locked --directory backend python -m pytest test/test_external_scenario_mode.py -q --basetemp=temp/p1-fix-mode` | 14 passed | 25.66s |
| `ruff check backend/evaluation/external_summary.py backend/evaluation/decision_feedback.py backend/test/test_external_summary.py backend/test/test_external_judge_checkpoints.py` | All checks passed | - |

`test_decision_feedback.py` 全文件单进程约 308.69s，超过 300s 单命令上限，按测试名拆分为 20 + 25 两批，节点无遗漏、无重叠。`test_evaluation_summary.py` 的 6 个 reranker 相关 case 按本轮约束排除。

同日两项 P1 补丁的复验结果：

| 命令 | 结果 | 耗时 |
| --- | --- | --- |
| `uv run --locked --directory backend python -m pytest test/test_external_summary.py -q -k "conflict_attempt_cannot_be_dropped or checkpoint_answer_must_match" --basetemp=temp/p1b-targeted` | 3 passed，30 deselected | 39.63s |
| `uv run --locked --directory backend python -m pytest test/test_external_summary.py -q --basetemp=temp/p1b-external-summary-full` | 33 passed | 298.99s |
| `uv run --locked --directory backend python -m pytest test/test_external_judge_checkpoints.py -q --basetemp=temp/p1b-checkpoints` | 16 passed | 27.03s |
| `uv run --locked --directory backend python -m pytest test/test_sdk_summary.py -q --basetemp=temp/p1b-sdk` | 22 passed | 79.03s |
| `uv run --locked --directory backend python -m pytest test/test_decision_feedback.py -q -k "two_feedback_zero_and_one or invalid_scores_block_success or missing_metric_pending_then_later or bounded_wait_stops or real_cloud_failure or remote_run_identity or example_reference_and_applicability or source_gap_and_annotation or duplicate_same_id_dedup or different_feedback_ids or history_conflict_and_drift or missing_upload_and_not_applicable or unexpected_feedback_for_not_applicable or all_not_applicable_collect or check_is_offline_and_collect or raw_evidence_sanitized" --basetemp=temp/p1b-decision-feedback-a` | 22 passed，23 deselected | 141.85s |
| `uv run --locked --directory backend python -m pytest test/test_decision_feedback.py -q -k "batch_diagnostics_are_sanitized or persistence_idempotent or corrupt_index_and_batch or binding_rejects_old_metrics or terminal_attempt_requires_cloud_identity or malformed_feedback_without_identity or experiment_receipt_drift or binding_digest_drift or binding_identity_drift or missing_or_pending_experiment_receipt or payload_projection_drift or feedback_scope_rejects or wait_seconds_rejects or cli_exit_codes" --basetemp=temp/p1b-decision-feedback-b` | 23 passed，22 deselected | 193.72s |
| `ruff check backend/evaluation/decision_feedback.py backend/evaluation/external_summary.py backend/test/test_external_summary.py backend/test/test_external_judge_checkpoints.py` | All checks passed | - |

新增回归覆盖：保留两个原始终态候选、移除其中一个评分尝试并重算全部包装摘要后，共享加载器拒绝该批次且不产生数值；正常输入下请求检查为空；仅改写事件回答正文或结构化快照分别产生对应阻断。共享夹具补齐 `event.tool_identity_map`，与生产 capture 语义一致。

### 15.8 未运行项

- reranker/Cross-Encoder 专项与真实服务测试全部暂停：`test_retrieval_rerank_evaluation.py`、`test_retrieval_rerank_stages.py`、`test_retrieval_rrf.py`、`test_retrieval_stages.py`、`test_retrieval_mteb.py`、`test_retrieval_runtime_integration.py`、`check_retrieval_rerank_production.py` 等。
- 未执行真实生成模型、Embedding、reranker、裁判模型或 LangSmith 云端调用；未执行真实 `collect`、未提交新实验、未改写任何正式成绩。
- 未建立真实 40 条默认场景外部全量运行、正式检索来源与人工裁判质量审阅，因此不存在真实外部正式发布。
