from typing import Annotated, Any, Literal, Self, TypeAlias

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.domain.business.models import (
    STANDARD_UUID,
    PendingProposalCancellation,
    PlanPendingProposal,
    PlanRecord,
    PlanSaveRecord,
    ProfilePendingProposal,
    ProfileRecord,
    ProfileSaveRecord,
    SaveConfirmationOperation,
    WorkoutPendingProposal,
    WorkoutRecord,
    WorkoutSaveRecord,
)


class EvaluationContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


def _nonblank_text(value: str) -> str:
    if not value.strip():
        raise ValueError("必须包含非空白内容")
    return value


NonEmptyText = Annotated[str, AfterValidator(_nonblank_text)]
ExerciseId = Annotated[str, Field(pattern=r"^[0-9]+$")]
UuidText = Annotated[str, Field(pattern=STANDARD_UUID)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
EventId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")]
ProposalRef = Annotated[str, Field(pattern=r"^(profile|plan|workout)-proposal-.+$")]

Grade = Literal[0, 1, 2, 3]
RetrievalStage = Literal["candidate_pool", "final_model_input"]
RetrievalMetricName = Literal["recall@20", "hit@5", "mrr@5", "ndcg@5", "precision@5"]
BusinessKind = Literal["profile", "plan", "workout"]
BusinessScope = Literal["profile", "plan", "workout", "all"]
AssertionKind = Literal[
    "state_unchanged",
    "saved_data_unchanged",
    "equipment_restriction",
    "forbidden_exercise",
    "parameter_preserved",
    "snapshot_integrity",
    "save_authorization",
    "state_transition",
    "evidence_present",
    "no_saved_state",
    "saved_state",
]
ContextSource = Literal[
    "system_prompt",
    "prior_message",
    "user_message",
    "assistant_message",
    "retrieved_exercise",
    "tool_result",
    "business_projection",
    "attachment",
]
FactOrigin = Literal["user", "tool", "system", "retrieved", "assistant"]
GenerationMetric = Literal["faithfulness", "factual_correctness"]
MetricName = Literal[
    "recall@20",
    "hit@5",
    "mrr@5",
    "ndcg@5",
    "precision@5",
    "faithfulness",
    "factual_correctness",
    "deterministic_rules",
]
ResultStatus = Literal["applicable", "succeeded", "failed", "not_applicable"]

RETRIEVAL_METRIC_STAGES: dict[str, tuple[RetrievalStage, int]] = {
    "recall@20": ("candidate_pool", 20),
    "hit@5": ("final_model_input", 5),
    "mrr@5": ("final_model_input", 5),
    "ndcg@5": ("final_model_input", 5),
    "precision@5": ("final_model_input", 5),
}
RETRIEVAL_METRICS: tuple[str, ...] = tuple(RETRIEVAL_METRIC_STAGES)
GENERATION_METRICS: tuple[str, ...] = ("faithfulness", "factual_correctness")
JUDGE_MODEL = "step-5-preview"
JUDGE_MODELS = ("step-5-preview", "step-3.7-flash", "deepseek-v4.1-flash")
JudgeModel = Literal["step-5-preview", "step-3.7-flash", "deepseek-v4.1-flash"]


def _require_unique_ids(items: list[str], label: str) -> None:
    for item in items:
        if not item or item != item.strip():
            raise ValueError(f"{label}必须为非空且无首尾空白的字符串")
    if len(set(items)) != len(items):
        raise ValueError(f"{label}不能重复")


class RelevanceLabel(EvaluationContract):
    exercise_id: ExerciseId
    grade: Grade


class RetrievalInput(EvaluationContract):
    query_id: NonEmptyText
    scenario_id: NonEmptyText | None = None
    expect_empty: bool
    labels: list[RelevanceLabel] = Field(default_factory=list)
    candidate_pool: list[ExerciseId] = Field(default_factory=list, max_length=20)
    final_model_input: list[ExerciseId] = Field(default_factory=list, max_length=5)

    @field_validator("candidate_pool", "final_model_input")
    @classmethod
    def validate_order(cls, value: list[str]) -> list[str]:
        _require_unique_ids(value, "动作 ID")
        return value

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        _require_unique_ids([label.exercise_id for label in self.labels], "金标动作 ID")
        positives = self.positive_labels
        if self.expect_empty and positives:
            raise ValueError("expect_empty 查询不得包含 grade>=1 的金标")
        if not self.expect_empty and not positives:
            raise ValueError("有答案查询必须包含至少一个 grade>=1 的金标")
        if not set(self.final_model_input) <= set(self.candidate_pool):
            raise ValueError("最终模型输入必须来自候选池")
        return self

    @property
    def positive_labels(self) -> list[RelevanceLabel]:
        return [label for label in self.labels if label.grade >= 1]


class RetrievalMetric(EvaluationContract):
    name: RetrievalMetricName
    stage: RetrievalStage
    k: int = Field(gt=0)
    value: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_stage(self) -> Self:
        expected_stage, expected_k = RETRIEVAL_METRIC_STAGES[self.name]
        if self.stage != expected_stage or self.k != expected_k:
            raise ValueError(f"{self.name} 必须绑定 {expected_stage} 阶段且 k={expected_k}")
        return self


class RetrievalResult(EvaluationContract):
    query_id: NonEmptyText
    scenario_id: NonEmptyText | None = None
    expect_empty: bool
    candidate_pool: list[ExerciseId] = Field(default_factory=list, max_length=20)
    final_model_input: list[ExerciseId] = Field(default_factory=list, max_length=5)
    metrics: list[RetrievalMetric] = Field(default_factory=list)
    false_return: bool | None = None

    @field_validator("candidate_pool", "final_model_input")
    @classmethod
    def validate_order(cls, value: list[str]) -> list[str]:
        _require_unique_ids(value, "动作 ID")
        return value

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if not set(self.final_model_input) <= set(self.candidate_pool):
            raise ValueError("最终模型输入必须来自候选池")
        names = [metric.name for metric in self.metrics]
        _require_unique_ids(names, "指标")
        if self.expect_empty:
            if self.metrics:
                raise ValueError("无答案查询不得填写排序指标")
            if self.false_return is None:
                raise ValueError("无答案查询必须记录误返回结果")
            if self.false_return != bool(self.final_model_input):
                raise ValueError("误返回必须按最终模型输入阶段判定")
            return self
        if self.false_return is not None:
            raise ValueError("有金标查询不得填写误返回字段")
        if set(names) != set(RETRIEVAL_METRICS):
            raise ValueError("有金标查询必须保留五项检索指标")
        values = {metric.name: metric.value for metric in self.metrics}
        # 候选阶段与最终输入阶段独立：Recall@20 只由候选池决定。
        if not self.candidate_pool and values["recall@20"] != 0.0:
            raise ValueError("候选池为空时 Recall@20 必须为 0")
        if not self.final_model_input:
            if any(values[name] != 0.0 for name in RETRIEVAL_METRICS if name != "recall@20"):
                raise ValueError("有金标空返回时四项 @5 指标必须为 0")
        return self


class VisibleExercise(EvaluationContract):
    exercise_id: ExerciseId
    name: NonEmptyText
    equipment: NonEmptyText
    target: NonEmptyText
    matches: list[NonEmptyText] = Field(default_factory=list)


class ModelVisibleSearch(EvaluationContract):
    items: list[VisibleExercise] = Field(default_factory=list, max_length=5)
    has_more: bool

    @field_validator("items")
    @classmethod
    def validate_unique_items(cls, value: list[VisibleExercise]) -> list[VisibleExercise]:
        _require_unique_ids([item.exercise_id for item in value], "模型可见动作 ID")
        return value


SEARCH_RESPONSE_FIELDS = {"id", "name", "equipment", "target", "matches"}


class SearchResponseEvidence(EvaluationContract):
    query_id: NonEmptyText
    response_json: dict[str, Any]
    model_visible: ModelVisibleSearch

    @model_validator(mode="after")
    def validate_response_correspondence(self) -> Self:
        response = self.response_json
        if set(response) != {"items", "has_more"}:
            raise ValueError("公开搜索响应必须仅包含 items 与 has_more")
        raw_items = response["items"]
        if not isinstance(raw_items, list):
            raise ValueError("公开搜索响应的 items 必须为列表")
        if not isinstance(response["has_more"], bool):
            raise ValueError("公开搜索响应的 has_more 必须为布尔值")
        if response["has_more"] != self.model_visible.has_more:
            raise ValueError("公开响应与模型可见的 has_more 必须一致")
        if len(raw_items) != len(self.model_visible.items):
            raise ValueError("公开响应与模型可见条目数量必须一致")
        for raw, visible in zip(raw_items, self.model_visible.items, strict=True):
            if not isinstance(raw, dict) or set(raw) != SEARCH_RESPONSE_FIELDS:
                raise ValueError("公开搜索条目字段非法")
            if (
                raw["id"] != visible.exercise_id
                or raw["name"] != visible.name
                or raw["equipment"] != visible.equipment
                or raw["target"] != visible.target
                or raw["matches"] != visible.matches
            ):
                raise ValueError("公开响应与模型可见条目内容必须一致")
        return self

    def require_scored_consistency(self, final_model_input: list[str]) -> None:
        visible = [item.exercise_id for item in self.model_visible.items]
        if visible != final_model_input:
            raise ValueError("模型可见结果与评分列表不一致")


class EvidenceLink(EvaluationContract):
    run_id: NonEmptyText
    scenario_id: NonEmptyText
    event_id: EventId
    checkpoint_id: EventId
    tool_call_id: NonEmptyText | None = None


class ToolFact(EvaluationContract):
    tool_call_id: NonEmptyText
    tool_name: NonEmptyText
    fact: NonEmptyText


PendingProposal: TypeAlias = Annotated[
    ProfilePendingProposal | PlanPendingProposal | WorkoutPendingProposal,
    Field(discriminator="business_kind"),
]
SaveRecord: TypeAlias = ProfileSaveRecord | PlanSaveRecord | WorkoutSaveRecord


class SavedBusinessData(EvaluationContract):
    profile: ProfileRecord | None = Field(
        default=None,
        description="null 表示记录不存在；非 null 且 content 全为空表示记录存在但内容为空。",
    )
    plans: list[PlanRecord] = Field(default_factory=list)
    workouts: list[WorkoutRecord] = Field(default_factory=list)
    save_records: list[SaveRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_saved_records(self) -> Self:
        _require_unique_ids([plan.id for plan in self.plans], "计划记录 ID")
        if sum(1 for plan in self.plans if plan.is_current) > 1:
            raise ValueError("最多一个当前计划")
        _require_unique_ids([workout.id for workout in self.workouts], "训练记录 ID")
        _require_unique_ids(
            [record.proposal_id for record in self.save_records], "保存记录 proposal_id"
        )
        return self


class BusinessProjection(SavedBusinessData):
    proposals: list[PendingProposal] = Field(default_factory=list)
    confirmation_operations: list[SaveConfirmationOperation] = Field(default_factory=list)
    cancellations: list[PendingProposalCancellation] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_projection(self) -> Self:
        _require_unique_ids(
            [proposal.proposal_id for proposal in self.proposals], "待确认提案 ID"
        )
        _require_unique_ids(
            [operation.operation_id for operation in self.confirmation_operations],
            "确认操作 ID",
        )
        _require_unique_ids(
            [cancellation.proposal_id for cancellation in self.cancellations],
            "取消提案 ID",
        )
        return self


class SavedDataComparison(EvaluationContract):
    kind: Literal["saved_data_unchanged"]
    business_kind: BusinessScope
    checkpoint: EvidenceLink
    before: SavedBusinessData
    after: SavedBusinessData


class StateComparison(EvaluationContract):
    kind: Literal["state_unchanged"]
    business_kind: BusinessScope
    checkpoint: EvidenceLink
    before: BusinessProjection
    after: BusinessProjection


ProjectionComparison: TypeAlias = Annotated[
    SavedDataComparison | StateComparison, Field(discriminator="kind")
]


class ProposalBinding(EvaluationContract):
    proposal_ref: ProposalRef
    business_kind: BusinessKind
    proposal_id: UuidText
    display_entry_id: UuidText | None = None
    confirmation_entry_id: UuidText | None = None
    confirmation_operation_id: UuidText | None = None

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        kind, separator, source = self.proposal_ref.partition("-proposal-")
        if not separator or not source or kind != self.business_kind:
            raise ValueError("proposal_ref 的业务类型必须与 business_kind 一致")
        if self.confirmation_operation_id is not None and self.confirmation_entry_id is None:
            raise ValueError("确认操作必须绑定确认入口")
        return self


class AssertionOutcome(EvaluationContract):
    checkpoint: EvidenceLink
    kind: AssertionKind
    passed: bool
    evidence: NonEmptyText
    proposal: ProposalBinding | None = None
    comparison: ProjectionComparison | None = None

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        proposal_required = self.kind in ("snapshot_integrity", "save_authorization")
        if proposal_required and self.proposal is None:
            raise ValueError("该断言必须提供提案绑定")
        if not proposal_required and self.proposal is not None:
            raise ValueError("该断言不接受提案绑定")
        comparison_required = self.kind in ("state_unchanged", "saved_data_unchanged")
        if comparison_required and self.comparison is None:
            raise ValueError("该断言必须提供事件前后比较证据")
        if not comparison_required and self.comparison is not None:
            raise ValueError("该断言不接受事件前后比较证据")
        return self


class PriorMessage(EvaluationContract):
    role: Literal["user", "assistant"]
    content: NonEmptyText


class UserInputEvidence(EvaluationContract):
    message: NonEmptyText
    prior_messages: list[PriorMessage] = Field(default_factory=list)


class AnswerEvidence(EvaluationContract):
    visible_text: NonEmptyText
    structured_snapshot: dict[str, Any] | None = None


class ContextItem(EvaluationContract):
    source: ContextSource
    content: NonEmptyText
    fact_origin: FactOrigin
    tool_call_id: NonEmptyText | None = None
    tool_name: NonEmptyText | None = None
    message_entry_id: NonEmptyText | None = None
    message_role: Literal["system", "user", "assistant"] | None = None
    truncated: bool = False
    omitted_chars: int | None = Field(default=None, ge=0)
    original_chars: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_provenance(self) -> Self:
        if self.source == "tool_result" and (
            self.tool_call_id is None or self.tool_name is None
        ):
            raise ValueError("工具结果必须关联 tool_call_id 与工具名称")
        if self.fact_origin == "tool" and self.tool_call_id is None:
            raise ValueError("工具事实必须关联 tool_call_id")
        if self.source == "assistant_message" and (
            self.fact_origin != "assistant" or self.message_role != "assistant"
        ):
            raise ValueError("助手消息必须记录助手来源与助手角色")
        if self.message_role == "assistant" and self.source != "assistant_message":
            raise ValueError("助手角色只能用于助手消息来源")
        if self.truncated:
            if self.omitted_chars is None or self.original_chars is None:
                raise ValueError("裁剪条目必须记录裁剪信息")
        elif self.omitted_chars is not None:
            raise ValueError("未裁剪条目不得记录省略字符数")
        if self.original_chars is not None and self.original_chars < len(self.content):
            raise ValueError("原始字符数不得小于实际内容长度")
        return self


class ModelInputContext(EvaluationContract):
    items: list[ContextItem] = Field(min_length=1)
    truncated: bool = False
    omitted_chars: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_truncation(self) -> Self:
        if self.truncated and self.omitted_chars is None:
            raise ValueError("整体裁剪必须记录省略字符数")
        if not self.truncated and self.omitted_chars is not None:
            raise ValueError("未整体裁剪时不得记录省略字符数")
        return self


class ReferenceFact(EvaluationContract):
    fact_id: NonEmptyText
    text: NonEmptyText
    source: NonEmptyText | None = None
    asset_id: NonEmptyText | None = None


class GenerationReference(EvaluationContract):
    answer: NonEmptyText
    facts: list[ReferenceFact] = Field(default_factory=list)
    must_include: list[NonEmptyText] = Field(default_factory=list)
    allowed_variation: list[NonEmptyText] = Field(default_factory=list)
    forbidden_claims: list[NonEmptyText] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_reference(self) -> Self:
        _require_unique_ids([fact.fact_id for fact in self.facts], "reference fact_id")
        _require_unique_ids(list(self.must_include), "must_include")
        _require_unique_ids(list(self.allowed_variation), "allowed_variation")
        _require_unique_ids(list(self.forbidden_claims), "forbidden_claims")
        return self


class MetricApplicability(EvaluationContract):
    metric: GenerationMetric
    applicable: bool
    reason: NonEmptyText | None = None

    @model_validator(mode="after")
    def validate_reason(self) -> Self:
        if not self.applicable and self.reason is None:
            raise ValueError("不适用指标必须写明原因")
        return self


class RagasInput(EvaluationContract):
    answer: NonEmptyText
    contexts: list[NonEmptyText] = Field(min_length=1)
    reference: NonEmptyText


class GenerationCheckpoint(EvaluationContract):
    checkpoint: EvidenceLink
    user_input: UserInputEvidence
    answer: AnswerEvidence
    model_input: ModelInputContext
    reference: GenerationReference | None = None
    applicability: list[MetricApplicability] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def validate_applicability(self) -> Self:
        metrics = [row.metric for row in self.applicability]
        if sorted(metrics) != sorted(GENERATION_METRICS):
            raise ValueError("生成检查点必须逐项声明 Faithfulness 与 FactualCorrectness")
        applicable = any(row.applicable for row in self.applicability)
        if applicable and self.reference is None:
            raise ValueError("生成指标适用时必须提供审阅 reference")
        if not applicable and self.reference is not None:
            raise ValueError("生成指标不适用时不得携带 reference")
        return self

    def ragas_input(self) -> RagasInput:
        if self.reference is None:
            raise ValueError("生成指标不适用时没有可用的 Ragas 输入")
        return RagasInput(
            answer=self.answer.visible_text,
            contexts=[item.content for item in self.model_input.items],
            reference=self.reference.answer,
        )


class MetricOutcome(EvaluationContract):
    metric: MetricName
    status: ResultStatus
    score: float | None = Field(default=None, ge=0, le=1)
    reason: NonEmptyText | None = None
    error: NonEmptyText | None = None

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        if self.status == "succeeded":
            if self.score is None:
                raise ValueError("成功状态必须提供真实分数")
            if self.error is not None:
                raise ValueError("成功状态不得携带错误依据")
        elif self.status == "failed":
            if self.score is not None:
                raise ValueError("失败状态不得使用分数")
            if self.error is None:
                raise ValueError("失败状态必须保留错误依据")
        elif self.status == "not_applicable":
            if self.score is not None or self.error is not None:
                raise ValueError("不适用状态不得使用分数或错误依据")
            if self.reason is None:
                raise ValueError("不适用状态必须保留原因")
        else:
            if self.score is not None or self.error is not None:
                raise ValueError("适用状态不得提前填写分数或错误依据")
        return self


class CheckpointResult(EvaluationContract):
    checkpoint: EvidenceLink
    outcomes: list[MetricOutcome] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_metrics(self) -> Self:
        _require_unique_ids([outcome.metric for outcome in self.outcomes], "检查点指标")
        return self


class ScenarioResult(EvaluationContract):
    scenario_id: NonEmptyText
    checkpoints: list[CheckpointResult] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_scenario(self) -> Self:
        _require_unique_ids(
            [item.checkpoint.checkpoint_id for item in self.checkpoints], "检查点 ID"
        )
        if any(item.checkpoint.scenario_id != self.scenario_id for item in self.checkpoints):
            raise ValueError("检查点的 scenario_id 必须与场景一致")
        return self


class DatasetIdentity(EvaluationContract):
    version: NonEmptyText
    manifest_sha256: Sha256
    content_sha256: Sha256
    corpus_sha256: Sha256


class CodeIdentity(EvaluationContract):
    commit: NonEmptyText
    lock_sha256: Sha256
    dirty: bool


class RetrievalIdentity(EvaluationContract):
    strategy: NonEmptyText
    model: NonEmptyText
    index: NonEmptyText
    encoding_rule_version: NonEmptyText
    cache_identity: NonEmptyText


class GenerationIdentity(EvaluationContract):
    model: NonEmptyText
    prompt_version: NonEmptyText


class JudgeIdentity(EvaluationContract):
    model: JudgeModel = JUDGE_MODEL
    prompt_version: NonEmptyText
    metric_version: NonEmptyText


class RunIdentity(EvaluationContract):
    run_id: NonEmptyText
    dataset: DatasetIdentity
    code: CodeIdentity
    retrieval: RetrievalIdentity
    generation: GenerationIdentity
    judge: JudgeIdentity


class RunCoverage(EvaluationContract):
    expected_scenarios: int = Field(ge=0)
    observed_scenarios: int = Field(ge=0)
    expected_checkpoints: int = Field(ge=0)
    observed_checkpoints: int = Field(ge=0)

    @property
    def complete(self) -> bool:
        return (
            self.observed_scenarios == self.expected_scenarios
            and self.observed_checkpoints == self.expected_checkpoints
        )


class EvaluationRun(EvaluationContract):
    identity: RunIdentity
    coverage: RunCoverage
    scenarios: list[ScenarioResult] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_coverage(self) -> Self:
        _require_unique_ids([scenario.scenario_id for scenario in self.scenarios], "场景 ID")
        if len(self.scenarios) != self.coverage.observed_scenarios:
            raise ValueError("实际场景数与 coverage 不一致")
        checkpoints = sum(len(scenario.checkpoints) for scenario in self.scenarios)
        if checkpoints != self.coverage.observed_checkpoints:
            raise ValueError("实际检查点数与 coverage 不一致")
        return self

    def blocking_issues(self) -> list[str]:
        issues = []
        if not self.coverage.complete:
            issues.append("评测覆盖不完整")
        for scenario in self.scenarios:
            for checkpoint in scenario.checkpoints:
                for outcome in checkpoint.outcomes:
                    if outcome.status == "failed":
                        issues.append(
                            f"指标执行失败: {checkpoint.checkpoint.checkpoint_id}/{outcome.metric}"
                        )
                    elif outcome.status == "applicable":
                        issues.append(
                            f"指标尚无结果: {checkpoint.checkpoint.checkpoint_id}/{outcome.metric}"
                        )
        return issues
