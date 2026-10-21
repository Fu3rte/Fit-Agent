import math
import numbers
import re
from typing import Annotated, Any, Literal, Self, TypeAlias

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
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
            raise ValueError(
                f"{self.name} 必须绑定 {expected_stage} 阶段且 k={expected_k}"
            )
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
            if any(
                values[name] != 0.0 for name in RETRIEVAL_METRICS if name != "recall@20"
            ):
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
    def validate_unique_items(
        cls, value: list[VisibleExercise]
    ) -> list[VisibleExercise]:
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


RerankExperiment = Literal["rrf_baseline", "rerank"]


class RerankIdentityEvidence(EvaluationContract):
    model: NonEmptyText
    endpoint: NonEmptyText
    text_version: NonEmptyText


class RerankUsageEvidence(EvaluationContract):
    requests: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class RerankScoredEvidence(EvaluationContract):
    id: ExerciseId
    rerank_score: float = Field(ge=0, le=1)
    rrf_rank: int = Field(ge=0, lt=20)


class RerankEvidence(EvaluationContract):
    # 重排实验证据只保存可离线回放的数据；基线不携带重排字段。
    mode: RerankExperiment
    identity: RerankIdentityEvidence | None = None
    scored: list[RerankScoredEvidence] = Field(default_factory=list, max_length=20)
    usage: RerankUsageEvidence | None = None
    request_id: NonEmptyText | None = None

    @model_validator(mode="after")
    def validate_mode(self) -> Self:
        if self.mode == "rrf_baseline":
            if (
                self.identity is not None
                or self.scored
                or self.usage is not None
                or self.request_id is not None
            ):
                raise ValueError("RRF baseline 不得携带重排证据")
            return self
        if self.identity is None or self.usage is None:
            raise ValueError("重排实验必须提供配置身份与用量")
        _require_unique_ids([row.id for row in self.scored], "重排动作 ID")
        ranks = [row.rrf_rank for row in self.scored]
        _require_unique_ids([str(rank) for rank in ranks], "重排原排名")
        previous: tuple[float, int] | None = None
        for row in self.scored:
            if previous is not None:
                if row.rerank_score > previous[0]:
                    raise ValueError("重排结果必须按分数降序")
                if row.rerank_score == previous[0] and row.rrf_rank < previous[1]:
                    raise ValueError("重排同分必须保持原 RRF 顺序")
            previous = (row.rerank_score, row.rrf_rank)
        if self.scored:
            if self.usage.requests != 1 or self.request_id is None:
                raise ValueError("非空重排必须记录一次请求与 request_id")
        elif self.usage.requests != 0 or self.request_id is not None:
            raise ValueError("空候选池必须记录零请求与空 request_id")
        return self


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
    confirmation_operations: list[SaveConfirmationOperation] = Field(
        default_factory=list
    )
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
        if (
            self.confirmation_operation_id is not None
            and self.confirmation_entry_id is None
        ):
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


class MessageNode(EvaluationContract):
    entry_id: NonEmptyText
    session_id: NonEmptyText
    role: str
    content: str
    kind: str
    has_attachments: bool = False
    run_id: NonEmptyText | None = None


class ToolNode(EvaluationContract):
    tool_call_id: NonEmptyText
    tool_name: NonEmptyText
    content: str
    entry_id: NonEmptyText
    session_id: NonEmptyText
    run_id: NonEmptyText | None = None
    is_error: bool


class RequestBoundaryEvidence(EvaluationContract):
    # 被评分请求的节点边界：来源分类与当前输入只能沿这些真实节点身份复核。
    api: NonEmptyText
    session_id: NonEmptyText
    run_id: NonEmptyText
    request_entry_id: NonEmptyText | None = None
    nodes: list[MessageNode]
    tool_nodes: list[ToolNode]


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
            raise ValueError(
                "生成检查点必须逐项声明 Faithfulness 与 FactualCorrectness"
            )
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
        if any(
            item.checkpoint.scenario_id != self.scenario_id for item in self.checkpoints
        ):
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
    model: NonEmptyText
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
        _require_unique_ids(
            [scenario.scenario_id for scenario in self.scenarios], "场景 ID"
        )
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


# LangSmith 外部裁判：与 Ragas 生成契约并行，独立版本、独立指标身份、独立反馈状态。
EXTERNAL_JUDGE_CHECKPOINT_SCHEMA_VERSION = "fit-agent-external-judge-checkpoint-1"
EXTERNAL_JUDGE_FEEDBACK_SCHEMA_VERSION = "fit-agent-external-judge-feedback-1"
ExternalJudgeCheckpointSchemaVersion = Literal[
    "fit-agent-external-judge-checkpoint-1"
]
ExternalJudgeFeedbackSchemaVersion = Literal[
    "fit-agent-external-judge-feedback-1"
]
ExternalJudgeMetric = Literal[
    "evidence_supported_v1", "reference_requirements_met_v1"
]
EXTERNAL_JUDGE_METRICS: tuple[str, ...] = (
    "evidence_supported_v1",
    "reference_requirements_met_v1",
)
ExternalJudgeStatus = Literal["pending", "succeeded", "failed", "not_applicable"]


def _probability_score(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise ValueError("评分必须为实数")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("评分必须为有限数值")
    if number < 0.0 or number > 1.0:
        raise ValueError("评分必须在 0 到 1 之间")
    return number


ProbabilityScore = Annotated[float, BeforeValidator(_probability_score)]


def _require_external_metrics(metrics: list[str], label: str) -> None:
    _require_unique_ids(metrics, label)
    if set(metrics) != set(EXTERNAL_JUDGE_METRICS):
        expected = "、".join(EXTERNAL_JUDGE_METRICS)
        raise ValueError(f"{label}必须逐项且仅包含 {expected}")


class ExternalJudgeApplicability(EvaluationContract):
    metric: ExternalJudgeMetric
    applicable: bool
    reason: NonEmptyText | None = None

    @model_validator(mode="after")
    def validate_reason(self) -> Self:
        if not self.applicable and self.reason is None:
            raise ValueError("不适用指标必须写明原因")
        return self


class ExternalJudgeUserRequest(UserInputEvidence):
    checkpoint: EvidenceLink


class ExternalJudgeRetrievalEvidence(EvaluationContract):
    tool_call_id: NonEmptyText
    tool_name: NonEmptyText
    search: SearchResponseEvidence
    rerank: RerankEvidence | None = None

    @model_validator(mode="after")
    def validate_experiment(self) -> Self:
        if self.rerank is None:
            return self
        if self.rerank.mode != "rerank":
            raise ValueError("检索证据中的重排记录必须为重排实验")
        # 出站只暴露地址摘要，原始服务地址不进入上传契约。
        if re.fullmatch(r"[0-9a-f]{64}", self.rerank.identity.endpoint) is None:
            raise ValueError("出站重排身份必须使用地址 SHA-256 摘要")
        return self


class ExternalJudgeContext(EvaluationContract):
    items: list[ContextItem] = Field(min_length=1)
    truncated: bool = False
    omitted_chars: int | None = Field(default=None, ge=0)
    request_boundary: RequestBoundaryEvidence
    retrieval: list[ExternalJudgeRetrievalEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_context(self) -> Self:
        if self.truncated and self.omitted_chars is None:
            raise ValueError("上下文整体裁剪必须记录省略字符数")
        if not self.truncated and self.omitted_chars is not None:
            raise ValueError("上下文未整体裁剪时不得记录省略字符数")
        _require_unique_ids(
            [row.tool_call_id for row in self.retrieval], "检索证据工具调用"
        )
        tools = {
            (item.tool_call_id, item.tool_name)
            for item in self.items
            if item.source == "tool_result"
        }
        for row in self.retrieval:
            if (row.tool_call_id, row.tool_name) not in tools:
                raise ValueError("检索证据必须绑定实际上下文中的工具结果")
        return self


class ExternalJudgeCheckpointInputs(EvaluationContract):
    user_request: ExternalJudgeUserRequest


class ExternalJudgeCheckpointOutputs(EvaluationContract):
    answer: AnswerEvidence
    context: ExternalJudgeContext


class ExternalJudgeReferenceOutputs(EvaluationContract):
    reference: GenerationReference
    applicability: list[ExternalJudgeApplicability] = Field(
        min_length=2, max_length=2
    )

    @model_validator(mode="after")
    def validate_applicability(self) -> Self:
        _require_external_metrics(
            [row.metric for row in self.applicability], "外部裁判适用性"
        )
        return self


class ExternalJudgeMetadata(EvaluationContract):
    dataset: DatasetIdentity
    checkpoint: EvidenceLink
    execution_sha256: Sha256


class ExternalJudgeCheckpointUpload(EvaluationContract):
    schema_version: ExternalJudgeCheckpointSchemaVersion
    metadata: ExternalJudgeMetadata
    inputs: ExternalJudgeCheckpointInputs
    outputs: ExternalJudgeCheckpointOutputs
    reference_outputs: ExternalJudgeReferenceOutputs

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.inputs.user_request.checkpoint != self.metadata.checkpoint:
            raise ValueError("用户请求身份必须与元数据检查点一致")
        return self


class ExternalJudgeFeedback(EvaluationContract):
    metric: ExternalJudgeMetric
    status: ExternalJudgeStatus
    score: ProbabilityScore | None = None
    applicability_reason: NonEmptyText | None = None
    failure_evidence: NonEmptyText | None = None

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        if self.status == "succeeded":
            if self.score is None:
                raise ValueError("成功反馈必须提供真实分数")
            if self.failure_evidence is not None:
                raise ValueError("成功反馈不得携带失败依据")
        elif self.status == "failed":
            if self.score is not None:
                raise ValueError("失败反馈不得携带成功分数")
            if self.failure_evidence is None:
                raise ValueError("失败反馈必须保留失败依据")
        elif self.status == "not_applicable":
            if self.score is not None or self.failure_evidence is not None:
                raise ValueError("不适用反馈不得携带分数或失败依据")
            if self.applicability_reason is None:
                raise ValueError("不适用反馈必须保留原因")
        else:
            if self.score is not None or self.failure_evidence is not None:
                raise ValueError("待评分反馈不得携带分数或失败依据")
        return self


class ExternalJudgeFeedbackSet(EvaluationContract):
    schema_version: ExternalJudgeFeedbackSchemaVersion
    checkpoint: EvidenceLink
    execution_sha256: Sha256
    feedback: list[ExternalJudgeFeedback] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def validate_feedback(self) -> Self:
        _require_external_metrics(
            [row.metric for row in self.feedback], "外部裁判反馈"
        )
        return self


class ReportedModelVersion(EvaluationContract):
    kind: Literal["reported"]
    version: NonEmptyText


class UnreportedModelVersion(EvaluationContract):
    kind: Literal["unreported"]


ExternalJudgeModelVersion: TypeAlias = Annotated[
    ReportedModelVersion | UnreportedModelVersion,
    Field(discriminator="kind"),
]


class ExternalJudgeModelIdentity(EvaluationContract):
    provider: NonEmptyText
    evaluator: NonEmptyText
    model_alias: NonEmptyText
    model_version: ExternalJudgeModelVersion
    config_sha256: Sha256


class ExternalJudgeDefinitionIdentity(EvaluationContract):
    question_id: NonEmptyText
    question_sha256: Sha256
    rubric_id: NonEmptyText | None = None
    rubric_sha256: Sha256


class ExternalJudgeExperimentIdentity(EvaluationContract):
    experiment_id: NonEmptyText
    experiment_name: NonEmptyText | None = None


class ExternalJudgeFeedbackSource(EvaluationContract):
    kind: Literal["api", "model", "evaluator", "annotation"]
    identity: NonEmptyText | None = None


class ExternalJudgeSource(EvaluationContract):
    experiment: ExternalJudgeExperimentIdentity
    run_id: NonEmptyText
    example_id: NonEmptyText
    feedback_source: ExternalJudgeFeedbackSource


class ExternalJudgeOutboundProjection(EvaluationContract):
    row_id: NonEmptyText
    projection_sha256: Sha256


class ExternalJudgeCloudFeedbackIdentity(EvaluationContract):
    # 只承载 LangSmith 实际返回的反馈身份；自定义 run metadata 不作为回读依据。
    experiment_id: NonEmptyText
    run_id: NonEmptyText
    feedback_id: NonEmptyText


class ExternalJudgeScoringAttempt(EvaluationContract):
    schema_version: ExternalJudgeFeedbackSchemaVersion
    attempt_id: NonEmptyText
    checkpoint: EvidenceLink
    execution_sha256: Sha256
    source: ExternalJudgeSource
    model: ExternalJudgeModelIdentity
    definition: ExternalJudgeDefinitionIdentity
    outbound: ExternalJudgeOutboundProjection
    feedback: ExternalJudgeFeedback
    cloud_feedback: ExternalJudgeCloudFeedbackIdentity | None = None

    @model_validator(mode="after")
    def validate_attempt(self) -> Self:
        cloud = self.cloud_feedback
        if cloud is None:
            if self.feedback.status != "pending":
                raise ValueError("已完成的反馈必须绑定云端实际返回身份")
            return self
        if self.feedback.status == "pending":
            raise ValueError("待评分反馈不得绑定云端实际返回身份")
        if (
            cloud.experiment_id != self.source.experiment.experiment_id
            or cloud.run_id != self.source.run_id
        ):
            raise ValueError("云端反馈身份必须与评分来源一致")
        return self
