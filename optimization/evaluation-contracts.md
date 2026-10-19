## 1. 交付状态

A 部分已落地共享输入输出契约与评测依赖；任务 1 至 5 已实现两阶段检索指标计算、生产阶段构造、证据、报告与上传兼容接线、确定性业务断言引擎、Ragas 生成评分模块、冻结 80 场景的真实运行器与本地证据采集，以及任务 5 的本地汇总、诊断报告、生成评测显式上传和发布门禁。策略替换尚未实现；正式 baseline、正式 80 场景全量评测与 LangSmith 生成评测上传尚未执行。已执行单检查点真实 `step-5-preview` Ragas 集成检查、`step-3.7-flash` 真实裁判兼容检查、场景运行器离线业务链路测试与任务 5 离线汇总/门禁回归。

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
- 裁判模型（显式配置的 `step-5-preview`、`step-3.7-flash` 或 `deepseek-v4.1-flash`）、裁判提示与指标版本。

凭据、API Key 与受保护配置不进入契约。`EvaluationRun.blocking_issues()` 将覆盖不完整、执行失败和仍为 `applicable` 的指标列为阻塞项；发布器与上传白名单不在本部分实现。

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

- 裁判模型支持 `step-5-preview`、`step-3.7-flash` 与 `deepseek-v4.1-flash`；`load_judge_model()` 只在运行时读取 `FIT_AGENT_EVAL_JUDGE_MODEL`，模块导入不读取配置、不创建客户端。模型切换只能由显式环境变量完成，失败后自动切换被禁止；旧运行的原始证据与身份保持不变。
- `deepseek-v4.1-flash` 本次仅同步模型白名单与证据类型；独立裁判服务地址/密钥环境变量的读取接线尚未实现，真实调用兼容性尚未验证。
- 当前源码没有独立的裁判服务地址与凭据环境变量来源。`JudgeConfig(model, base_url, api_key)` 由调用者或任务 4/5 从现有受保护配置注入；不得猜测变量名、地址或借用其他服务凭据。集成检查使用受保护模型配置的服务地址与凭据，裁判模型名由 `FIT_AGENT_EVAL_JUDGE_MODEL` 指定，并由真实调用验证服务是否支持所选模型。
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
- 已执行：模块导入、Ruff、`compileall`、`pytest test/test_generation_scoring.py -q`，65 passed；真实 `step-5-preview` 集成检查通过，两项指标均 `succeeded`，评分 1.0/1.0，裁判调用 2/4 次，超时/取消资源释放通过，样例动作事实来自真实 Catalog `0001`。未执行：正式 80 场景、完整业务套件、全量检索与 LangSmith 上传。

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

- 运行前以 `load_for_evaluation` 校验冻结门禁；数据身份固定为 `rebuild-v1`。
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
- 生成评分使用 `GenerationScoringSession`，裁判由显式配置选择 `step-5-preview`、`step-3.7-flash` 或 `deepseek-v4.1-flash`，逐指标保留 `succeeded`、`failed`、`not_applicable` 与 `applicable`；失败不改为零分或不适用，`ScoringAttemptSnapshot` 与已成功证据分别保留。
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

- `pytest test/test_evaluation_scenarios.py test/test_deterministic_assertions.py test/test_generation_scoring.py test/test_evaluation_contracts.py test/test_retrieval_stages.py -q --basetemp=backend/temp/task4-followup/pytest-offline`：退出码 0，170 passed in 54.32s。覆盖取消证据与 invalidated 状态区分、实际取消边界阻断、工具完整正文与可见裁剪、工具 ID 规范化映射、纯工具调用缺口、纯状态检查点 N/A、逐指标覆盖、身份端点脱敏、`step-3.7-flash` 显式切换与混合裁判身份拒绝，同文用户消息与业务投影的身份顺序、历史与当前输入的 `prior_message`/`user_message` 区分、未绑定节点触发 `ambiguous` 并停止评分、跨会话节点与工具结果隔离、检索记录的事件/工具调用/索引身份绑定、重启后序号继续、单一运行索引身份聚合与多身份冲突、阻塞场景保留已完成指标计数；本轮新增真实 SQLite 用户节点正文等于生产投影（含 `[附件]` 引用行）并绑定 `attachment` 来源、候选池按回答节点父节点固定到被评分请求边界（请求边界之后的节点既不入池，后续投影也不裁剪请求之前的历史节点）、压缩后请求只含有效范围节点且摘要按前后缀全文绑定为系统事实、已被压缩移出本次请求的历史同名工具调用不进入冲突域而本次请求内的重复 ID 仍判歧义并停止评分、同运行压缩后复用同名取消调用 ID 时两份结果沿真实父链各自绑定发起助手节点与目标提案、来源参数与目标不一致的结果不被绑定、驱动事件按会话与运行复合身份匹配、普通异常的部分产物经 `partial.json` 进入运行汇总、裁判尝试模型冲突进入 `missing` 与 `incomplete`、离线运行汇总的缺失清单完整列出 `generation`/`judge`/`retrieval`、生成检查点的当前输入正文只由请求内 `user_message`/`attachment` 项拼合且忽略业务投影与历史项。
- `python temp/task4-rereview/repro_mapping.py` 与 `python temp/task4-rereview/repro_followup.py`：退出码均为 0，五个映射用例（同文身份、历史与当前输入、未绑定歧义、跨会话歧义、纯工具调用助手）全部通过；真实 SQLite 双会话同名 `tool_call_id` 取消各自保留（`single_counts=[1, 1]`、`combined=2`、`projection.cancellations=2`），跨事件检索序号为 `[0, 1]` 且重启后为 `[0, 1, 2]`。日志在 `backend/temp/task4-followup/`。
- `python -m evaluation.scenarios --all`：退出码 0，输出 80 场景、134 事件、127 检查点、254 个生成指标条目；`s-confirmation-cancellation-state-04` 计划为 3 检查点、6 个生成指标。
- `ruff check evaluation/ app/ test/test_evaluation_scenarios.py`、`compileall -q evaluation app`、`git diff --check`：全部通过。`ruff check test/` 全量运行另有 37 项未改动文件的既有告警（I001/E402/F401/F841 与 `test/test_stability_task2.py:504` 的 `AssistantMessage` 未定义名），属本轮范围外。
- `RunSummary.incomplete` 逐场景汇总绑定失败、运行器装配错误、失败指标、尚无结果的适用指标与身份缺失；`context_gaps` 单独进入覆盖统计。CLI 与真实集成检查在存在 `incomplete`、阻塞、失败或指标缺失时返回非零。
- 真实 `s-action-verification-01`（`step-3.7-flash`）：Agent 完成 5 次模型调用与 8 次工具调用（全部 `is_error=false`），检索证据为候选池 20、最终模型输入 5、公开条目 5、threshold=0.0；`state_unchanged` 与 `evidence_present` 两项断言通过；覆盖为 scenarios 1/1、events 1/1、checkpoints 1/1、metrics 2/2；`identity.index` 记录 collection、build_id、manifest_sha256 与兼容性清单；Faithfulness 在 180 s 内完成，得分 0.6667；FactualCorrectness 在 180 s 超时，保留 `failed`。同一真实检查点在独立评分中于 280 s 内完成 FactualCorrectness，得分 0.55，证据与尝试快照写入 `backend/temp/task4-fix/judge-compat/factual-compat.json`。两项指标均使用 `step-3.7-flash`，`judge_same_as_generation=true`。
- 真实批次（`--enable-model`，run-id `flow-*`，产物在 `backend/temp/evaluation-scenarios/`，日志在 `backend/temp/task4-followup/`）：单场景逐个运行覆盖附件回合、检索工具、会话切换、多回合同文输入、确认与取消/重启。全部运行零 `ambiguous` 缺口，用户/助手/工具/业务投影正文均按真实节点身份绑定：`s-plan-import-file-06` 的 37 项模型输入为 system_prompt 1、attachment 1、business_projection 13、tool_result 22；`s-profile-update-07` 三回合的 cp3 为 24 项（prior_message 2、user_message 1、assistant_message 2、business_projection 9、tool_result 9、system_prompt 1），历史与当前输入按顺序区分。
- 真实 `s-action-verification-01`（`--judge-timeout 120`，run-id `flow-act01-t120`）：`identity.missing=[]`、`identity.judge` 与 `cache_identity` 齐备，Faithfulness 成功得分 0.4359，FactualCorrectness 在第 3 次裁判调用处 `TimeoutError` 保留 `failed`，`incomplete` 仅两项评分相关；检索记录 1 条并绑定真实 `search_exercises` 的 `tool_call_id`、`event_id=e01` 与唯一 `(collection, manifest_sha256)`。
- 修复验证：附件-only 回合此前在真实运行中终态为 `failed`，运行汇总逐字记录 `s-plan-import-file-04: ValidationError: 1 validation error for UserInputEvidence message Value error, 必须包含非空白内容 [input_value='', input_type=str]`（根因为冻结事件正文为空而该字段是 `NonEmptyText`），该 run 目录已按放行删除。修复后 `flow-att04b` 场景 completed、`user_input.message` 为 125 字符的 `[附件]` 投影、Faithfulness 得分 0.7059；`flow-att05` 两个纯附件回合（e01/e03）同样按投影正文计分，cp1 断言 `state_unchanged` 与 `evidence_present(context min_count=2 actual=10)` 通过。
- 真实 SQLite 与生产取消服务回归（本次离线验证）：e02 消息回合真实完成 `cancel_pending_proposal`，断言验证 `pending→invalidated`、取消目标唯一且证据匹配本消息会话/运行/请求/来源/结果节点；e03 使用自身 `invalidated→invalidated` 前后边界并保留同一份取消证据；已保存数据不变、重复取消保持业务状态、重启后失效状态持续；错误目标、缺少证据、重复证据与非取消消息均不能通过。真实模型下的取消调用稳定性保留为正式 baseline 验证项。
- 真实 `s-workout-logging-query-01`：模型第二回合以 `stop_reason=length` 达到输出上限（output_tokens 16384），事件终态 `failed:model_output_limit_exceeded`，回答文本缺失按契约阻断且不填占位；cp1 记 `binding_error: proposal_ref 缺少唯一真实绑定`。
- 真实批次中的业务断言失败按真实状态保留：`s-replacement-constraints-03` cp1 `state_unchanged` 失败（模型创建 1 个 pending 提案），`s-profile-update-07` cp3 `evidence_present(snapshot min_count=2 actual=0)` 失败；两者均记录真实结果与证据，不做改写。
- 裁判预算实测：`--judge-timeout 30` 下全部指标 `TimeoutError`；90 s 与 120 s 下 Faithfulness 完成并产出真实分数，FactualCorrectness 仍需更长预算（历史 280 s 完成）。
- 运行器默认 `--judge-timeout 300`，按单项生成指标计时，并传入 Ragas、LLM 与 HTTP 客户端；可通过 CLI 显式覆盖。该预算不代表整个场景或批次的总时限，超时仍保留真实失败与尝试证据。
- 未执行：正式 80 场景全量真实运行、`step-5-preview` 全量评分与 LangSmith 上传。

## 7. 依赖记录

- `ragas==0.3.1`：dev 依赖，固定精确版本；在 Python 3.13 与现有 `openai==3.13.0` 下实际导入通过，新增依赖面小于 0.4.x。
- `langchain-community>=0.4,<0.4.2`：Ragas 0.3.1 在导入时引用 `langchain_community.chat_models.vertexai`；该模块在 0.4.2 被移除，故锁定 0.4.1。
- `mteb==2.26.7` 与 `openai==3.13.0` 保持原锁定值；`langchain-community` 上界未改动现有生产依赖上界。
- 新增 LangChain 依赖链使 `websockets` 从 17.2 解析为 16.1.1（`langgraph-sdk` 约束）。该变化仍在现有依赖允许范围内，未升级 MTEB。
- Python 3.13.13；`uv lock --check --directory backend` 通过。

## 8. 未交付与验证状态

20→5 两阶段契约修订、指标入口、生产阶段构造、评测证据/上传接线、确定性业务断言引擎与 Ragas 生成评分模块已在代码中实现。新增和修改的测试代码覆盖两阶段指标、候选池上限、空返回口径、预算裁剪、隐藏身份、证据一致性、未审阅候选门禁与历史路径身份；确定性断言测试覆盖 11 种断言、同检查点同类多条、比较范围、JSON Pointer 边界、真实目录器械、快照漂移、保存授权、状态迁移、四类证据、真实 SQLite 确认链路与错误输入。生成评分测试覆盖真实 Ragas 指标样本映射、上下文顺序与裁剪、reference 隔离、逐指标适用性、检查点身份、0/1/NaN/Infinity/类型边界、真实 prompt 摘要、配置与 timeout 拒绝路径、证据凭据隔离、地址内嵌凭据拒绝与序列化脱敏、成功/证据分数一致性、重复证据身份稳定性、遥测禁用缓存覆盖，以及本机传输失败、超时与取消的失败尝试证据保留。已执行 `pytest test/test_generation_scoring.py -q`，65 passed。

已执行 `pytest test/test_deterministic_assertions.py -q`，退出码 0，30 passed。新增用例覆盖消息检查点的 `pending→invalidated`、取消证据目标/运行/唯一性、后置 `invalidated→invalidated` 与缺少取消记录；同次静态检查为 Ruff、`compileall` 与 `git diff --check`。既有用例继续覆盖保存正文偏离准备快照、训练日期偏离、无法核验的动作身份、工具正文与上下文冲突、单次搜索超过 5 条、排名来源缺失与重复来源。

任务 4 场景运行器已落地并按审查修复：`evaluation/scenarios.py` 提供 80 场景选择、真实生产链路装配、五类事件编排、全会话投影、本地证据与逐指标覆盖；`evaluation/scenario_evidence.py` 提供真实业务投影读取、真实取消证据、模型请求全文采集、按真实消息节点归因的 `ContextItem` 映射与完整工具正文。身份边界对生成、Embedding 与错误产物统一脱敏；检索身份绑定实际 collection、build_id 与兼容性清单。本次修订后离线回归：`pytest test/test_evaluation_scenarios.py -q` 49 passed；`pytest test/test_evaluation_dataset.py -q` 37 passed；`pytest test/test_evaluation_contracts.py test/test_evaluation_summary.py -q` 41 passed；`pytest test/test_retrieval_stages.py test/test_retrieval_mteb.py test/test_retrieval_rrf.py -q` 95 passed；`python -m evaluation.scenarios --all` 退出码 0。真实定向验收覆盖 `s-action-verification-01`、`s-confirmation-cancellation-state-04`、`s-profile-update-08` 与 `s-confirmation-cancellation-state-08`：检索、断言、确认、重复确认、会话切换与纯状态 N/A 均通过；`step-3.7-flash` 下 Faithfulness 得分 0.6667、FactualCorrectness 在 280 s 独立评分中得分 0.55；取消场景按消息回合真实迁移与后置真实边界验证。未执行正式 80 场景全量、全量裁判评分与 LangSmith 上传。

生成评分模块的静态检查已执行：Ruff 与 `compileall` 通过，模块导入与纯字段映射检查通过。已执行 `pytest test/test_generation_scoring.py -q`，退出码 0，65 passed，无警告。已执行 `FIT_AGENT_GENERATION_SCORING_INTEGRATION=1` 的真实服务集成检查：受保护模型服务接受 `step-5-preview`，Faithfulness 与 FactualCorrectness 均 `succeeded`，评分 1.0/1.0，裁判调用分别为 2 与 4 次，超时/取消路径完成客户端释放，样例动作事实来自真实 Catalog `0001`（`equipment=body weight`、`target=abs`）。真实调用出现 `langchain-openai` 关于自定义 httpx transport 的代理自动探测警告，非致命；代理环境下的连通性仍属集中验证范围。尚未执行完整业务测试套件、正式全量 150 查询检索、80 个业务场景、真实 Agent 或 LangSmith 上传；正式场景下的模型稳定性、长上下文与限流行为仍属集中验证范围。未执行的测试不得视为通过。

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
