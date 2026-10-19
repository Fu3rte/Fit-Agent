import json
from collections.abc import Mapping, Sequence
from functools import cache
from typing import Annotated, Any, Literal, TypeAlias

from jsonpointer import EndOfList, JsonPointer, JsonPointerException, resolve_pointer
from pydantic import (
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from app.application.business.catalog import Catalog
from app.domain.business.models import (
    BusinessModel,
    PendingProposalCancellation,
    PlanAdjustmentProposal,
    PlanContent,
    PlanImportProposal,
    PlanProposal,
    PlanSnapshot,
    ProfileContent,
    ProfileProposal,
    ProfileSnapshot,
    WorkoutContent,
    WorkoutProposal,
    WorkoutSnapshot,
)
from evaluation.contracts import (
    AssertionOutcome,
    BusinessKind,
    BusinessProjection,
    BusinessScope,
    ContextItem,
    EvaluationContract,
    EventId,
    EvidenceLink,
    ExerciseId,
    NonEmptyText,
    ProposalBinding,
    ProposalRef,
    SavedBusinessData,
    SavedDataComparison,
    SearchResponseEvidence,
    StateComparison,
    UuidText,
)


class AssertionInputError(ValueError):
    pass


ASSERTION_STATUSES = frozenset(
    {"pending", "processing", "saved", "invalidated", "conflicted"}
)
ASSERTION_VALUE_TYPES = str | int | float | bool | None
_CONTENT_MODELS: dict[str, type[BusinessModel]] = {
    "profile": ProfileContent,
    "plan": PlanContent,
    "workout": WorkoutContent,
}


def _proposal_kind(proposal_ref: str) -> str:
    kind, separator, source = proposal_ref.partition("-proposal-")
    if not separator or not source or kind not in _CONTENT_MODELS:
        raise ValueError("proposal_ref 必须为 <business_kind>-proposal-<来源事件>")
    return kind


def _require_unique(values: list[str], label: str) -> list[str]:
    if len(set(values)) != len(values):
        raise ValueError(f"{label}不能重复")
    return values


def _unique_items_validator(field: str):
    def validate(value: list[str]) -> list[str]:
        return _require_unique(value, field)

    return field_validator(field)(validate)


class EventTarget(EvaluationContract):
    business_kind: BusinessKind
    proposal_ref: ProposalRef
    display_ref: NonEmptyText | None = None
    confirmation_ref: NonEmptyText | None = None

    @model_validator(mode="after")
    def validate_refs(self) -> "EventTarget":
        if (self.display_ref is None) != (self.confirmation_ref is None):
            raise ValueError("展示引用与确认引用必须同时存在或同时为空")
        return self


class EventIdentity(EvaluationContract):
    session_id: NonEmptyText
    run_id: NonEmptyText
    request_entry_id: NonEmptyText


class CancellationEvidence(EvaluationContract):
    checkpoint: EvidenceLink
    session_id: NonEmptyText
    assistant_run_id: NonEmptyText
    request_entry_id: NonEmptyText
    source_entry_id: NonEmptyText
    result_entry_id: NonEmptyText
    tool_call_id: NonEmptyText
    business_kind: BusinessKind
    proposal_id: UuidText
    result: PendingProposalCancellation

    @model_validator(mode="after")
    def validate_result(self) -> "CancellationEvidence":
        if self.result.business_kind != self.business_kind:
            raise ValueError("取消结果业务类型与目标不一致")
        if self.result.proposal_id != self.proposal_id:
            raise ValueError("取消结果 proposal_id 与目标不一致")
        return self


class AssertionDefinition(EvaluationContract):
    checkpoint_id: EventId
    event_id: EventId


class StateUnchangedAssertion(AssertionDefinition):
    kind: Literal["state_unchanged"]
    business_kind: BusinessScope


class SavedDataUnchangedAssertion(AssertionDefinition):
    kind: Literal["saved_data_unchanged"]
    business_kind: BusinessScope


class EquipmentRestrictionAssertion(AssertionDefinition):
    kind: Literal["equipment_restriction"]
    allowed_equipment: list[NonEmptyText]
    forbidden_equipment: list[NonEmptyText]

    _validate_allowed = _unique_items_validator("allowed_equipment")
    _validate_forbidden = _unique_items_validator("forbidden_equipment")


class ForbiddenExerciseAssertion(AssertionDefinition):
    kind: Literal["forbidden_exercise"]
    exercise_ids: list[ExerciseId] = Field(min_length=1)

    @field_validator("exercise_ids")
    @classmethod
    def validate_exercise_ids(cls, value: list[str]) -> list[str]:
        return _require_unique(value, "exercise_ids")


class ParameterPreservedAssertion(AssertionDefinition):
    kind: Literal["parameter_preserved"]
    field_path: NonEmptyText
    expected: ASSERTION_VALUE_TYPES

    @field_validator("field_path")
    @classmethod
    def validate_field_path(cls, value: str) -> str:
        try:
            JsonPointer(value)
        except JsonPointerException as error:
            raise ValueError("field_path 必须为合法 JSON Pointer") from error
        return value


class SnapshotIntegrityAssertion(AssertionDefinition):
    kind: Literal["snapshot_integrity"]
    proposal_ref: ProposalRef


class SaveAuthorizationAssertion(AssertionDefinition):
    kind: Literal["save_authorization"]
    proposal_ref: ProposalRef
    expected_saved: bool


class StateTransitionAssertion(AssertionDefinition):
    kind: Literal["state_transition"]
    business_kind: BusinessKind
    from_status: NonEmptyText | None
    to_status: NonEmptyText

    @field_validator("from_status", "to_status")
    @classmethod
    def validate_status(cls, value: str | None) -> str | None:
        if value is not None and value not in ASSERTION_STATUSES:
            raise ValueError("状态必须属于现有业务提案状态")
        return value


class EvidencePresentAssertion(AssertionDefinition):
    kind: Literal["evidence_present"]
    evidence_kind: Literal["context", "tool_result", "snapshot", "ranking"]
    min_count: int = Field(ge=1)


class NoSavedStateAssertion(AssertionDefinition):
    kind: Literal["no_saved_state"]
    business_kind: BusinessKind


class SavedStateAssertion(AssertionDefinition):
    kind: Literal["saved_state"]
    business_kind: BusinessKind


Assertion: TypeAlias = Annotated[
    StateUnchangedAssertion
    | SavedDataUnchangedAssertion
    | EquipmentRestrictionAssertion
    | ForbiddenExerciseAssertion
    | ParameterPreservedAssertion
    | SnapshotIntegrityAssertion
    | SaveAuthorizationAssertion
    | StateTransitionAssertion
    | EvidencePresentAssertion
    | NoSavedStateAssertion
    | SavedStateAssertion,
    Field(discriminator="kind"),
]
_ASSERTION_ADAPTER = TypeAdapter(Assertion)


class ContextEvidence(EvaluationContract):
    checkpoint: EvidenceLink
    item: ContextItem


class ToolResultEvidence(EvaluationContract):
    checkpoint: EvidenceLink
    tool_call_id: NonEmptyText
    tool_name: NonEmptyText
    content: NonEmptyText


class RankingEvidence(EvaluationContract):
    checkpoint: EvidenceLink
    tool_call_id: NonEmptyText
    tool_name: NonEmptyText
    search: SearchResponseEvidence


class ProfileSnapshotEvidence(EvaluationContract):
    proposal_ref: ProposalRef
    checkpoint: EvidenceLink
    snapshot: ProfileSnapshot

    @field_validator("proposal_ref")
    @classmethod
    def validate_ref(cls, value: str) -> str:
        if _proposal_kind(value) != "profile":
            raise ValueError("proposal_ref 必须为画像提案")
        return value


class PlanSnapshotEvidence(EvaluationContract):
    proposal_ref: ProposalRef
    checkpoint: EvidenceLink
    snapshot: PlanSnapshot

    @field_validator("proposal_ref")
    @classmethod
    def validate_ref(cls, value: str) -> str:
        if _proposal_kind(value) != "plan":
            raise ValueError("proposal_ref 必须为计划提案")
        return value


class WorkoutSnapshotEvidence(EvaluationContract):
    proposal_ref: ProposalRef
    checkpoint: EvidenceLink
    snapshot: WorkoutSnapshot

    @field_validator("proposal_ref")
    @classmethod
    def validate_ref(cls, value: str) -> str:
        if _proposal_kind(value) != "workout":
            raise ValueError("proposal_ref 必须为训练记录提案")
        return value


class DisplayContentEvidence(EvaluationContract):
    proposal_ref: ProposalRef
    business_kind: BusinessKind
    checkpoint: EvidenceLink
    display_entry_id: UuidText
    tool_call_id: NonEmptyText | None = None
    tool_name: NonEmptyText | None = None
    content: dict[str, Any]

    @model_validator(mode="after")
    def validate_display(self) -> "DisplayContentEvidence":
        if _proposal_kind(self.proposal_ref) != self.business_kind:
            raise ValueError("proposal_ref 的业务类型必须与 business_kind 一致")
        if (self.tool_call_id is None) != (self.tool_name is None):
            raise ValueError("展示来源的工具调用 ID 与名称必须同时存在或同时为空")
        return self


class BusinessContentEvidence(EvaluationContract):
    business_kind: BusinessKind
    source: Literal["proposal", "display", "saved"]
    checkpoint: EvidenceLink
    proposal_ref: ProposalRef | None = None
    record_id: UuidText | None = None
    payload: dict[str, Any]

    @model_validator(mode="after")
    def validate_content(self) -> "BusinessContentEvidence":
        if self.source in ("proposal", "display"):
            if self.proposal_ref is None or self.record_id is not None:
                raise ValueError("提案内容必须绑定 proposal_ref 且不得携带 record_id")
            if _proposal_kind(self.proposal_ref) != self.business_kind:
                raise ValueError("proposal_ref 的业务类型必须与 business_kind 一致")
        else:
            if self.proposal_ref is not None:
                raise ValueError("已保存内容不得携带 proposal_ref")
            if self.business_kind in ("plan", "workout") and self.record_id is None:
                raise ValueError("已保存计划或训练记录必须绑定 record_id")
        try:
            _CONTENT_MODELS[self.business_kind].model_validate(self.payload)
        except ValidationError as error:
            raise ValueError(f"payload 不符合 {self.business_kind} 业务模型") from error
        return self


class AssertionEvidence(EvaluationContract):
    checkpoint: EvidenceLink
    event_type: Literal["message", "confirm", "cancel", "session_switch", "restart"]
    event_target: EventTarget | None = None
    event_identity: EventIdentity | None = None
    before: BusinessProjection
    after: BusinessProjection
    proposal_bindings: list[ProposalBinding] = Field(default_factory=list)
    profile_snapshots: list[ProfileSnapshotEvidence] = Field(default_factory=list)
    plan_snapshots: list[PlanSnapshotEvidence] = Field(default_factory=list)
    workout_snapshots: list[WorkoutSnapshotEvidence] = Field(default_factory=list)
    display_contents: list[DisplayContentEvidence] = Field(default_factory=list)
    business_contents: list[BusinessContentEvidence] = Field(default_factory=list)
    parameter_target: BusinessContentEvidence | None = None
    context_evidence: list[ContextEvidence] = Field(default_factory=list)
    tool_results: list[ToolResultEvidence] = Field(default_factory=list)
    rankings: list[RankingEvidence] = Field(default_factory=list)
    cancel_evidence: list[CancellationEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_references(self) -> "AssertionEvidence":
        _require_unique(
            [binding.proposal_ref for binding in self.proposal_bindings],
            "proposal_bindings.proposal_ref",
        )
        for entries, label in (
            (self.profile_snapshots, "profile_snapshots"),
            (self.plan_snapshots, "plan_snapshots"),
            (self.workout_snapshots, "workout_snapshots"),
            (self.display_contents, "display_contents"),
        ):
            _require_unique([entry.proposal_ref for entry in entries], label)
        _require_unique(
            [
                f"{entry.business_kind}:{entry.source}:"
                f"{entry.proposal_ref or entry.record_id or ''}"
                for entry in self.business_contents
            ],
            "business_contents",
        )
        _require_unique(
            [item.tool_call_id for item in self.tool_results], "tool_results.tool_call_id"
        )
        _require_unique(
            [entry.tool_call_id for entry in self.rankings], "rankings.tool_call_id"
        )
        _require_unique(
            [entry.search.query_id for entry in self.rankings], "rankings.query_id"
        )
        return self


def parse_assertion(value: Assertion | Mapping[str, Any]) -> Assertion:
    if isinstance(value, AssertionDefinition):
        return value
    return _ASSERTION_ADAPTER.validate_python(value)


@cache
def load_catalog() -> Catalog:
    return Catalog.load()


def evaluate_assertion(
    assertion: Assertion | Mapping[str, Any], evidence: AssertionEvidence
) -> AssertionOutcome:
    definition = parse_assertion(assertion)
    _validate_scope(definition, evidence)
    evaluator = {
        "state_unchanged": _evaluate_state_unchanged,
        "saved_data_unchanged": _evaluate_saved_data_unchanged,
        "equipment_restriction": _evaluate_equipment_restriction,
        "forbidden_exercise": _evaluate_forbidden_exercise,
        "parameter_preserved": _evaluate_parameter_preserved,
        "snapshot_integrity": _evaluate_snapshot_integrity,
        "save_authorization": _evaluate_save_authorization,
        "state_transition": _evaluate_state_transition,
        "evidence_present": _evaluate_evidence_present,
        "no_saved_state": _evaluate_no_saved_state,
        "saved_state": _evaluate_saved_state,
    }[definition.kind]
    return evaluator(definition, evidence)


def evaluate_checkpoint(
    assertions: Sequence[Assertion | Mapping[str, Any]], evidence: AssertionEvidence
) -> list[AssertionOutcome]:
    return [evaluate_assertion(assertion, evidence) for assertion in assertions]


def _scope_key(link: EvidenceLink) -> tuple[str, str, str, str]:
    return (link.run_id, link.scenario_id, link.event_id, link.checkpoint_id)


def _require_link(label: str, actual: EvidenceLink, expected: EvidenceLink) -> None:
    if _scope_key(actual) != _scope_key(expected):
        raise AssertionInputError(f"{label} 与当前检查点身份不一致")


def _validate_scope(definition: AssertionDefinition, evidence: AssertionEvidence) -> None:
    if (
        definition.checkpoint_id != evidence.checkpoint.checkpoint_id
        or definition.event_id != evidence.checkpoint.event_id
    ):
        raise AssertionInputError("断言定义与当前检查点的 event_id 或 checkpoint_id 不一致")
    if evidence.event_type in ("confirm", "cancel"):
        if evidence.event_target is None:
            raise AssertionInputError("确认或取消检查点缺少事件目标")
        if evidence.event_type == "confirm":
            if (
                evidence.event_target.display_ref is None
                or evidence.event_target.confirmation_ref is None
            ):
                raise AssertionInputError("确认事件缺少展示引用或确认引用")
        elif evidence.event_target.display_ref is not None:
            raise AssertionInputError("取消事件不得携带展示引用")
    elif evidence.event_type == "message":
        if evidence.event_target is not None and (
            evidence.event_target.display_ref is not None
            or evidence.event_target.confirmation_ref is not None
        ):
            raise AssertionInputError("消息检查点的目标不得携带展示或确认引用")
    elif evidence.event_target is not None:
        raise AssertionInputError("会话切换或重启检查点不得携带事件目标")
    for label, entries in (
        ("profile_snapshots", evidence.profile_snapshots),
        ("plan_snapshots", evidence.plan_snapshots),
        ("workout_snapshots", evidence.workout_snapshots),
        ("display_contents", evidence.display_contents),
        ("business_contents", evidence.business_contents),
        ("context_evidence", evidence.context_evidence),
        ("tool_results", evidence.tool_results),
        ("rankings", evidence.rankings),
        ("cancel_evidence", evidence.cancel_evidence),
    ):
        for entry in entries:
            _require_link(label, entry.checkpoint, evidence.checkpoint)
    if evidence.parameter_target is not None:
        _require_link("parameter_target", evidence.parameter_target.checkpoint, evidence.checkpoint)


def _filter_saved(data: SavedBusinessData, scope: BusinessScope) -> SavedBusinessData:
    if scope == "all":
        return SavedBusinessData(
            profile=data.profile,
            plans=list(data.plans),
            workouts=list(data.workouts),
            save_records=list(data.save_records),
        )
    return SavedBusinessData(
        profile=data.profile if scope == "profile" else None,
        plans=list(data.plans) if scope == "plan" else [],
        workouts=list(data.workouts) if scope == "workout" else [],
        save_records=[
            record
            for record in data.save_records
            if _save_record_kind(record) == scope
        ],
    )


def _filter_projection(
    projection: BusinessProjection, scope: BusinessScope
) -> BusinessProjection:
    if scope == "all":
        return projection.model_copy(deep=True)
    saved = _filter_saved(projection, scope)
    return BusinessProjection(
        profile=saved.profile,
        plans=saved.plans,
        workouts=saved.workouts,
        save_records=saved.save_records,
        proposals=[
            proposal
            for proposal in projection.proposals
            if proposal.business_kind == scope
        ],
        confirmation_operations=[
            operation
            for operation in projection.confirmation_operations
            if operation.business_kind == scope
        ],
        cancellations=[
            cancellation
            for cancellation in projection.cancellations
            if cancellation.business_kind == scope
        ],
    )


def _save_record_kind(record) -> str:
    from app.domain.business.models import (
        PlanSaveRecord,
        ProfileSaveRecord,
        WorkoutSaveRecord,
    )

    if isinstance(record, ProfileSaveRecord):
        return "profile"
    if isinstance(record, PlanSaveRecord):
        return "plan"
    if isinstance(record, WorkoutSaveRecord):
        return "workout"
    raise AssertionInputError("未知保存记录类型")


def _first_difference(before: Any, after: Any, path: str = "") -> str | None:
    if type(before) is not type(after):
        return f"{path or '/'} 类型 {type(before).__name__} -> {type(after).__name__}"
    if isinstance(before, dict):
        keys = list(dict.fromkeys([*before, *after]))
        for key in keys:
            if key not in before:
                return f"{path}/{key} 新增"
            if key not in after:
                return f"{path}/{key} 缺失"
            difference = _first_difference(before[key], after[key], f"{path}/{key}")
            if difference is not None:
                return difference
        return None
    if isinstance(before, list):
        if len(before) != len(after):
            return f"{path or '/'} 长度 {len(before)} -> {len(after)}"
        for index, (left, right) in enumerate(zip(before, after, strict=True)):
            difference = _first_difference(left, right, f"{path}/{index}")
            if difference is not None:
                return difference
        return None
    if before != after:
        return f"{path or '/'} 值 {before!r} -> {after!r}"
    return None


def _comparison_evidence(before: BusinessModel, after: BusinessModel) -> str:
    difference = _first_difference(
        before.model_dump(mode="json"), after.model_dump(mode="json")
    )
    return "一致" if difference is None else difference


def _evaluate_state_unchanged(
    definition: StateUnchangedAssertion, evidence: AssertionEvidence
) -> AssertionOutcome:
    before = _filter_projection(evidence.before, definition.business_kind)
    after = _filter_projection(evidence.after, definition.business_kind)
    passed = before == after
    comparison = StateComparison(
        kind="state_unchanged",
        business_kind=definition.business_kind,
        checkpoint=evidence.checkpoint,
        before=before,
        after=after,
    )
    detail = _comparison_evidence(before, after)
    return AssertionOutcome(
        checkpoint=evidence.checkpoint,
        kind="state_unchanged",
        passed=passed,
        evidence=(
            f"business_kind={definition.business_kind} 完整业务投影{detail}；"
            f"提案={len(after.proposals)}，确认操作={len(after.confirmation_operations)}，"
            f"取消={len(after.cancellations)}"
        ),
        comparison=comparison,
    )


def _evaluate_saved_data_unchanged(
    definition: SavedDataUnchangedAssertion, evidence: AssertionEvidence
) -> AssertionOutcome:
    before = _filter_saved(evidence.before, definition.business_kind)
    after = _filter_saved(evidence.after, definition.business_kind)
    passed = before == after
    comparison = SavedDataComparison(
        kind="saved_data_unchanged",
        business_kind=definition.business_kind,
        checkpoint=evidence.checkpoint,
        before=before,
        after=after,
    )
    detail = _comparison_evidence(before, after)
    return AssertionOutcome(
        checkpoint=evidence.checkpoint,
        kind="saved_data_unchanged",
        passed=passed,
        evidence=(
            f"business_kind={definition.business_kind} 已保存业务内容{detail}；"
            f"计划={len(after.plans)}，训练记录={len(after.workouts)}，"
            f"保存记录={len(after.save_records)}"
        ),
        comparison=comparison,
    )


def _business_content_actions(
    entry: BusinessContentEvidence,
) -> list[tuple[str | None, str]]:
    content = _CONTENT_MODELS[entry.business_kind].model_validate(entry.payload)
    if isinstance(content, PlanContent):
        return [
            (exercise.exercise_id, day.kind)
            for day in content.days
            for exercise in day.exercises
        ]
    if isinstance(content, WorkoutContent):
        return [(exercise.exercise_id, "workout") for exercise in content.exercises]
    return []


def _display_payload(entry: DisplayContentEvidence, proposal) -> BusinessModel:
    if entry.business_kind == "profile":
        return ProfileProposal.model_validate(entry.content).payload
    if entry.business_kind == "plan":
        if proposal.preparation_kind == "generation":
            return PlanProposal.model_validate(entry.content).payload
        if proposal.preparation_kind == "import":
            return PlanImportProposal.model_validate(entry.content).payload
        return PlanAdjustmentProposal.model_validate(entry.content).payload
    return WorkoutProposal.model_validate(entry.content).payload


def _validate_content_binding(
    entry: BusinessContentEvidence,
    evidence: AssertionEvidence,
) -> None:
    target = evidence.event_target
    if entry.source in ("proposal", "display"):
        if target is not None and entry.proposal_ref != target.proposal_ref:
            raise AssertionInputError("业务内容 proposal_ref 与事件目标不一致")
        if entry.proposal_ref is None:
            raise AssertionInputError("业务内容缺少 proposal_ref")
        binding = _binding(evidence, entry.proposal_ref)
        proposal = _find_proposal(
            evidence.after, entry.business_kind, binding.proposal_id
        )
        if proposal is None:
            raise AssertionInputError("业务内容引用的提案未出现在当前检查点投影中")
        supplied = _CONTENT_MODELS[entry.business_kind].model_validate(entry.payload)
        if supplied != proposal.payload:
            raise AssertionInputError("业务内容 payload 与真实提案不一致")
        if entry.source == "display":
            display = _display_entry(evidence, entry.proposal_ref)
            if display is None:
                raise AssertionInputError("展示来源缺少完整展示内容")
            if display.business_kind != entry.business_kind:
                raise AssertionInputError("展示内容业务类型不一致")
            if display.display_entry_id != proposal.display_entry_id:
                raise AssertionInputError("展示内容与真实展示绑定不一致")
            if _display_payload(display, proposal) != supplied:
                raise AssertionInputError("展示内容 payload 与业务内容不一致")
        return
    supplied = _CONTENT_MODELS[entry.business_kind].model_validate(entry.payload)
    if entry.business_kind == "profile":
        if evidence.after.profile is None:
            raise AssertionInputError("已保存画像未出现在当前检查点投影中")
        actual = evidence.after.profile.content
    elif entry.business_kind == "plan":
        records = [
            plan for plan in evidence.after.plans if plan.id == entry.record_id
        ]
        if not records:
            raise AssertionInputError("已保存计划未出现在当前检查点投影中")
        actual = records[0].content
    else:
        records = [
            workout
            for workout in evidence.after.workouts
            if workout.id == entry.record_id
        ]
        if not records:
            raise AssertionInputError("已保存训练记录未出现在当前检查点投影中")
        actual = records[0].content
    if supplied != actual:
        raise AssertionInputError("已保存业务内容 payload 与真实记录不一致")


def _content_entries(evidence: AssertionEvidence) -> list[BusinessContentEvidence]:
    return list(evidence.business_contents)


def _evaluate_equipment_restriction(
    definition: EquipmentRestrictionAssertion, evidence: AssertionEvidence
) -> AssertionOutcome:
    catalog = load_catalog()
    allowed = set(definition.allowed_equipment)
    forbidden = set(definition.forbidden_equipment)
    unknown = (allowed | forbidden) - catalog.equipment
    if unknown:
        raise AssertionInputError(f"断言包含目录外器械: {sorted(unknown)}")
    entries = _content_entries(evidence)
    if not entries:
        return _plain_failure(
            evidence, "equipment_restriction", "当前检查点缺少可核验的业务内容"
        )
    actions: list[tuple[str | None, str]] = []
    for entry in entries:
        _validate_content_binding(entry, evidence)
        actions.extend(_business_content_actions(entry))
    if not actions:
        return _plain_failure(
            evidence, "equipment_restriction", "当前检查点业务内容不包含动作"
        )
    failures: list[str] = []
    inspected: list[str] = []
    for exercise_id, location in actions:
        if exercise_id is None:
            failures.append(f"{location} 存在空动作 ID")
            continue
        exercise = catalog.get(exercise_id)
        if exercise is None:
            failures.append(f"{exercise_id} 不在动作目录")
            continue
        inspected.append(f"{exercise_id}:{exercise.equipment}")
        if exercise.equipment not in allowed:
            failures.append(f"{exercise_id} 使用未允许器械 {exercise.equipment}")
        if exercise.equipment in forbidden:
            failures.append(f"{exercise_id} 使用禁止器械 {exercise.equipment}")
    evidence_text = (
        f"器械检查 allowed={sorted(allowed)} forbidden={sorted(forbidden)}；"
        f"动作={inspected}"
    )
    if failures:
        evidence_text += "；失败=" + "，".join(failures)
    return AssertionOutcome(
        checkpoint=evidence.checkpoint,
        kind="equipment_restriction",
        passed=not failures,
        evidence=evidence_text,
    )


def _evaluate_forbidden_exercise(
    definition: ForbiddenExerciseAssertion, evidence: AssertionEvidence
) -> AssertionOutcome:
    entries = _content_entries(evidence)
    if not entries:
        return _plain_failure(
            evidence, "forbidden_exercise", "当前检查点缺少可核验的业务内容"
        )
    forbidden = set(definition.exercise_ids)
    catalog = load_catalog()
    actions: list[str | None] = []
    for entry in entries:
        _validate_content_binding(entry, evidence)
        actions.extend(exercise_id for exercise_id, _ in _business_content_actions(entry))
    if not actions:
        return _plain_failure(
            evidence, "forbidden_exercise", "当前检查点业务内容不包含动作"
        )
    failures: list[str] = []
    inspected: list[str] = []
    for exercise_id in actions:
        if exercise_id is None:
            failures.append("存在空动作 ID，无法核对目录身份")
            continue
        if exercise_id not in catalog.exercise_ids:
            failures.append(f"{exercise_id} 不在动作目录，无法核对目录身份")
            continue
        inspected.append(exercise_id)
    hits = [exercise_id for exercise_id in inspected if exercise_id in forbidden]
    evidence_text = (
        f"禁止动作={sorted(forbidden)}；检查动作={actions}；目录动作={inspected}；"
        f"命中={hits}"
    )
    if failures:
        evidence_text += "；失败=" + "，".join(failures)
    return AssertionOutcome(
        checkpoint=evidence.checkpoint,
        kind="forbidden_exercise",
        passed=not failures and not hits,
        evidence=evidence_text,
    )


def _evaluate_parameter_preserved(
    definition: ParameterPreservedAssertion, evidence: AssertionEvidence
) -> AssertionOutcome:
    target = evidence.parameter_target
    if target is None:
        return _plain_failure(
            evidence, "parameter_preserved", "当前检查点缺少参数检查目标对象"
        )
    _validate_content_binding(target, evidence)
    try:
        actual = resolve_pointer(target.payload, definition.field_path)
    except JsonPointerException as error:
        message = str(error)
        if "out of bounds" in message:
            return _plain_failure(
                evidence,
                "parameter_preserved",
                f"{definition.field_path} 数组索引越界: {message}",
            )
        if "not found" in message:
            return _plain_failure(
                evidence,
                "parameter_preserved",
                f"{definition.field_path} 路径不存在: {message}",
            )
        raise AssertionInputError(f"无法解析 field_path: {message}") from error
    if isinstance(actual, EndOfList):
        return _plain_failure(
            evidence,
            "parameter_preserved",
            f"{definition.field_path} 指向数组末尾占位",
        )
    passed = _strict_value_equal(definition.expected, actual)
    return AssertionOutcome(
        checkpoint=evidence.checkpoint,
        kind="parameter_preserved",
        passed=passed,
        evidence=(
            f"business_kind={target.business_kind} source={target.source} "
            f"target={target.proposal_ref or target.record_id or 'saved-profile'}；"
            f"{definition.field_path} expected={definition.expected!r} actual={actual!r}"
        ),
    )


def _strict_value_equal(expected: Any, actual: Any) -> bool:
    if expected is None or actual is None:
        return expected is None and actual is None
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(expected) is type(actual) and expected == actual
    if isinstance(expected, int | float) and isinstance(actual, int | float):
        return expected == actual
    return type(expected) is type(actual) and expected == actual


def _binding(evidence: AssertionEvidence, proposal_ref: str) -> ProposalBinding:
    matches = [
        binding
        for binding in evidence.proposal_bindings
        if binding.proposal_ref == proposal_ref
    ]
    if not matches:
        raise AssertionInputError(f"缺少 proposal_ref 对应的真实绑定: {proposal_ref}")
    if len(matches) > 1:
        raise AssertionInputError(f"proposal_ref 存在多个绑定: {proposal_ref}")
    return matches[0]


def _require_target_match(
    evidence: AssertionEvidence, binding: ProposalBinding
) -> None:
    target = evidence.event_target
    if target is None:
        return
    if target.proposal_ref != binding.proposal_ref:
        raise AssertionInputError("事件目标与断言提案引用不一致")
    if target.business_kind != binding.business_kind:
        raise AssertionInputError("事件目标与断言业务类型不一致")


def _snapshot_entry(evidence: AssertionEvidence, binding: ProposalBinding):
    entries = {
        "profile": evidence.profile_snapshots,
        "plan": evidence.plan_snapshots,
        "workout": evidence.workout_snapshots,
    }[binding.business_kind]
    matches = [entry for entry in entries if entry.proposal_ref == binding.proposal_ref]
    if len(matches) > 1:
        raise AssertionInputError("同一 proposal_ref 存在多个准备快照")
    return matches[0] if matches else None


def _display_entry(evidence: AssertionEvidence, proposal_ref: str):
    matches = [
        entry
        for entry in evidence.display_contents
        if entry.proposal_ref == proposal_ref
    ]
    if len(matches) > 1:
        raise AssertionInputError("同一 proposal_ref 存在多个完整展示内容")
    return matches[0] if matches else None


def _snapshot_display_differences(
    snapshot: ProfileSnapshot | PlanSnapshot | WorkoutSnapshot,
    content: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    try:
        if isinstance(snapshot, ProfileSnapshot):
            displayed = ProfileProposal.model_validate(content)
            if displayed.proposal_id != snapshot.proposal_id:
                failures.append("展示 proposal_id 不一致")
            if displayed.profile_id != snapshot.profile_id:
                failures.append("展示 profile_id 不一致")
            if displayed.base_profile_version != snapshot.base_profile_version:
                failures.append("展示基础画像版本不一致")
            if displayed.payload != snapshot.payload:
                failures.append("展示 payload 漂移")
        elif isinstance(snapshot, PlanSnapshot):
            if snapshot.preparation_kind == "generation":
                displayed = PlanProposal.model_validate(content)
            elif snapshot.preparation_kind == "import":
                displayed = PlanImportProposal.model_validate(content)
            else:
                displayed = PlanAdjustmentProposal.model_validate(content)
            if displayed.proposal_id != snapshot.proposal_id:
                failures.append("展示 proposal_id 不一致")
            if displayed.base_profile_version != snapshot.base_profile_version:
                failures.append("展示基础画像版本不一致")
            if displayed.base_plan_id != snapshot.base_plan_id:
                failures.append("展示基础计划 ID 不一致")
            if displayed.payload != snapshot.payload:
                failures.append("展示 payload 漂移")
            if snapshot.preparation_kind != "generation":
                if displayed.preparation_kind != snapshot.preparation_kind:
                    failures.append("展示准备类型不一致")
        else:
            displayed = WorkoutProposal.model_validate(content)
            if displayed.proposal_id != snapshot.proposal_id:
                failures.append("展示 proposal_id 不一致")
            if displayed.performed_on != snapshot.performed_on:
                failures.append("展示训练日期不一致")
            if displayed.base_workout_id != snapshot.base_workout_id:
                failures.append("展示基础训练记录 ID 不一致")
            if displayed.base_workout_version != snapshot.base_workout_version:
                failures.append("展示基础训练记录版本不一致")
            if displayed.payload != snapshot.payload:
                failures.append("展示 payload 漂移")
    except ValidationError as error:
        failures.append(f"展示内容缺字段或字段非法: {error.errors()[0]['msg']}")
    return failures


def _snapshot_projection_differences(snapshot, projection) -> list[str]:
    failures: list[str] = []
    if snapshot.payload != projection.payload:
        failures.append("检查点快照 payload 与准备快照不一致")
    if isinstance(snapshot, ProfileSnapshot):
        if snapshot.profile_id != projection.profile_id:
            failures.append("检查点快照 profile_id 不一致")
        if snapshot.base_profile_version != projection.base_profile_version:
            failures.append("检查点快照基础画像版本不一致")
    elif isinstance(snapshot, PlanSnapshot):
        if snapshot.base_profile_version != projection.base_profile_version:
            failures.append("检查点快照基础画像版本不一致")
        if snapshot.base_plan_id != projection.base_plan_id:
            failures.append("检查点快照基础计划 ID 不一致")
        if snapshot.preparation_kind != projection.preparation_kind:
            failures.append("检查点快照准备类型不一致")
    else:
        if snapshot.performed_on != projection.performed_on:
            failures.append("检查点快照训练日期不一致")
        if snapshot.base_workout_id != projection.base_workout_id:
            failures.append("检查点快照基础训练记录 ID 不一致")
        if snapshot.base_workout_version != projection.base_workout_version:
            failures.append("检查点快照基础训练记录版本不一致")
    if snapshot.status != projection.status:
        failures.append(f"检查点快照状态 {snapshot.status} 与提案状态 {projection.status} 不一致")
    if snapshot.display_entry_id != projection.display_entry_id:
        failures.append("检查点快照展示绑定与提案不一致")
    if snapshot.confirmation_entry_id != projection.confirmation_entry_id:
        failures.append("检查点快照确认绑定与提案不一致")
    return failures


def _evaluate_snapshot_integrity(
    definition: SnapshotIntegrityAssertion, evidence: AssertionEvidence
) -> AssertionOutcome:
    binding = _binding(evidence, definition.proposal_ref)
    _require_target_match(evidence, binding)
    entry = _snapshot_entry(evidence, binding)
    if entry is None:
        return _binding_failure(
            evidence, "snapshot_integrity", binding, "缺少 proposal_ref 对应的准备快照"
        )
    snapshot = entry.snapshot
    if snapshot.proposal_id != binding.proposal_id:
        raise AssertionInputError("准备快照的 proposal_id 与绑定不一致")
    failures: list[str] = []
    if snapshot.display_entry_id != binding.display_entry_id:
        failures.append("准备快照展示绑定与 ProposalBinding 不一致")
    if snapshot.confirmation_entry_id != binding.confirmation_entry_id:
        failures.append("准备快照确认绑定与 ProposalBinding 不一致")
    display = _display_entry(evidence, binding.proposal_ref)
    if display is None:
        failures.append("缺少完整展示内容")
    else:
        if display.business_kind != binding.business_kind:
            raise AssertionInputError("展示内容业务类型与绑定不一致")
        if display.display_entry_id != binding.display_entry_id:
            failures.append("完整展示内容与展示绑定不一致")
        failures.extend(_snapshot_display_differences(snapshot, display.content))
    proposal = _find_proposal(
        evidence.after, binding.business_kind, binding.proposal_id
    )
    if proposal is not None:
        failures.extend(_snapshot_projection_differences(snapshot, proposal))
    else:
        record = _find_save_record(
            evidence.after, binding.business_kind, binding.proposal_id
        )
        if record is None:
            failures.append("当前检查点缺少目标提案或保存记录")
        else:
            if record.result.proposal_id != binding.proposal_id:
                failures.append("保存记录的 proposal_id 不一致")
            if record.result.content != snapshot.payload:
                failures.append("保存内容与准备快照 payload 不一致")
            if record.display_entry_id != binding.display_entry_id:
                failures.append("保存记录展示绑定不一致")
            if record.confirmation_entry_id != binding.confirmation_entry_id:
                failures.append("保存记录确认绑定不一致")
    if binding.confirmation_operation_id is not None:
        operation = _find_operation(
            evidence.after, binding.confirmation_operation_id
        )
        if operation is None:
            failures.append("确认操作账本缺失")
        else:
            if operation.proposal_id != binding.proposal_id:
                failures.append("确认操作 proposal_id 不一致")
            if operation.display_entry_id != binding.display_entry_id:
                failures.append("确认操作展示绑定不一致")
            if operation.confirmation_entry_id != binding.confirmation_entry_id:
                failures.append("确认操作确认绑定不一致")
    evidence_text = (
        f"proposal_id={binding.proposal_id} status={snapshot.status} "
        f"display_entry={binding.display_entry_id} confirmation_entry={binding.confirmation_entry_id}；"
        + ("一致" if not failures else "；".join(failures))
    )
    return AssertionOutcome(
        checkpoint=evidence.checkpoint,
        kind="snapshot_integrity",
        passed=not failures,
        evidence=evidence_text,
        proposal=binding,
    )


def _find_proposal(projection: BusinessProjection, business_kind: str, proposal_id: str):
    for proposal in projection.proposals:
        if (
            proposal.business_kind == business_kind
            and proposal.proposal_id == proposal_id
        ):
            return proposal
    return None


def _find_save_record(
    projection: BusinessProjection, business_kind: str, proposal_id: str
):
    for record in projection.save_records:
        if (
            _save_record_kind(record) == business_kind
            and record.proposal_id == proposal_id
        ):
            return record
    return None


def _find_operation(projection: BusinessProjection, operation_id: str):
    for operation in projection.confirmation_operations:
        if operation.operation_id == operation_id:
            return operation
    return None


def _proposal_status(
    projection: BusinessProjection, business_kind: str, proposal_id: str
) -> str | None:
    statuses: list[str] = []
    proposal = _find_proposal(projection, business_kind, proposal_id)
    if proposal is not None:
        statuses.append(proposal.status)
    record = _find_save_record(projection, business_kind, proposal_id)
    if record is not None:
        statuses.append("saved")
    if len(set(statuses)) > 1:
        raise AssertionInputError("提案状态与保存记录冲突")
    return statuses[0] if statuses else None


def _save_content_differences(
    binding: ProposalBinding,
    snapshot: ProfileSnapshot | PlanSnapshot | WorkoutSnapshot,
    record,
    evidence: AssertionEvidence,
) -> list[str]:
    failures: list[str] = []
    result = record.result
    if result.content != snapshot.payload:
        failures.append("保存正文与准备快照 payload 不一致")
    if snapshot.display_entry_id != binding.display_entry_id:
        failures.append("准备快照展示绑定与保存绑定不一致")
    if snapshot.confirmation_entry_id != binding.confirmation_entry_id:
        failures.append("准备快照确认绑定与保存绑定不一致")
    if isinstance(snapshot, ProfileSnapshot):
        if result.profile_id != snapshot.profile_id:
            failures.append("保存画像 ID 与准备快照不一致")
        profile = evidence.after.profile
        if profile is None:
            failures.append("保存记录未出现在已保存画像投影")
        else:
            if profile.content != result.content:
                failures.append("已保存画像内容与保存记录不一致")
            if profile.version != result.version:
                failures.append("已保存画像版本与保存记录不一致")
    elif isinstance(snapshot, PlanSnapshot):
        saved_plan = next(
            (item for item in evidence.after.plans if item.id == result.id), None
        )
        if saved_plan is None:
            failures.append("保存记录未出现在已保存计划投影")
        elif saved_plan.content != result.content:
            failures.append("已保存计划内容与保存记录不一致")
    else:
        if result.performed_on != snapshot.performed_on:
            failures.append("保存训练日期与准备快照不一致")
        saved_workout = next(
            (item for item in evidence.after.workouts if item.id == result.id), None
        )
        if saved_workout is None:
            failures.append("保存记录未出现在已保存训练投影")
        else:
            if saved_workout.content != result.content:
                failures.append("已保存训练内容与保存记录不一致")
            if saved_workout.performed_on != result.performed_on:
                failures.append("已保存训练日期与保存记录不一致")
            if saved_workout.version != result.version:
                failures.append("已保存训练版本与保存记录不一致")
    return failures


def _evaluate_save_authorization(
    definition: SaveAuthorizationAssertion, evidence: AssertionEvidence
) -> AssertionOutcome:
    binding = _binding(evidence, definition.proposal_ref)
    _require_target_match(evidence, binding)
    if definition.expected_saved:
        if evidence.event_type != "confirm":
            raise AssertionInputError("保存成功断言必须绑定确认事件检查点")
        if evidence.event_target is None:
            raise AssertionInputError("保存成功断言缺少事件目标")
        if (
            binding.display_entry_id is None
            or binding.confirmation_entry_id is None
        ):
            raise AssertionInputError("保存成功断言缺少展示或确认绑定")
        if binding.confirmation_operation_id is None:
            raise AssertionInputError("保存成功断言缺少确认操作编号")
        record = _find_save_record(
            evidence.after, binding.business_kind, binding.proposal_id
        )
        operation = _find_operation(
            evidence.after, binding.confirmation_operation_id
        )
        failures: list[str] = []
        if record is None:
            failures.append("缺少目标保存记录")
        else:
            if record.result.proposal_id != binding.proposal_id:
                failures.append("保存记录 proposal_id 不一致")
            if record.display_entry_id != binding.display_entry_id:
                failures.append("保存记录展示绑定不一致")
            if record.confirmation_entry_id != binding.confirmation_entry_id:
                failures.append("保存记录确认绑定不一致")
        snapshot_entry = _snapshot_entry(evidence, binding)
        if snapshot_entry is None:
            failures.append("缺少准备快照，无法核对保存正文")
        else:
            snapshot = snapshot_entry.snapshot
            if snapshot.proposal_id != binding.proposal_id:
                raise AssertionInputError("准备快照的 proposal_id 与绑定不一致")
            if record is not None:
                failures.extend(
                    _save_content_differences(binding, snapshot, record, evidence)
                )
        if operation is None:
            failures.append("缺少确认操作账本")
        else:
            if operation.status != "saved":
                failures.append(f"确认操作状态为 {operation.status}")
            if operation.business_kind != binding.business_kind:
                failures.append("确认操作业务类型不一致")
            if operation.proposal_id != binding.proposal_id:
                failures.append("确认操作 proposal_id 不一致")
            if operation.display_entry_id != binding.display_entry_id:
                failures.append("确认操作展示绑定不一致")
            if operation.confirmation_entry_id != binding.confirmation_entry_id:
                failures.append("确认操作确认绑定不一致")
            if operation.result is None:
                failures.append("确认操作缺少保存结果")
            elif record is not None and operation.result != record.result:
                failures.append("确认操作结果与保存记录不一致")
        proposal = _find_proposal(
            evidence.after, binding.business_kind, binding.proposal_id
        )
        if proposal is not None and proposal.status != "saved":
            failures.append(f"目标提案状态为 {proposal.status}")
        evidence_text = (
            f"expected_saved=true proposal_id={binding.proposal_id} "
            f"record={'存在' if record is not None else '缺失'} "
            f"operation={'存在' if operation is not None else '缺失'}；"
            + ("一致" if not failures else "；".join(failures))
        )
        return AssertionOutcome(
            checkpoint=evidence.checkpoint,
            kind="save_authorization",
            passed=not failures,
            evidence=evidence_text,
            proposal=binding,
        )
    if evidence.event_type == "message" and (
        binding.confirmation_entry_id is not None
        or binding.confirmation_operation_id is not None
    ):
        raise AssertionInputError("消息检查点的未保存断言不得携带确认链路")
    record = _find_save_record(
        evidence.after, binding.business_kind, binding.proposal_id
    )
    operation = next(
        (
            item
            for item in evidence.after.confirmation_operations
            if item.business_kind == binding.business_kind
            and item.proposal_id == binding.proposal_id
            and item.status == "saved"
        ),
        None,
    )
    proposal = _find_proposal(
        evidence.after, binding.business_kind, binding.proposal_id
    )
    if record is not None or operation is not None:
        return AssertionOutcome(
            checkpoint=evidence.checkpoint,
            kind="save_authorization",
            passed=False,
            evidence=(
                f"expected_saved=false proposal_id={binding.proposal_id} "
                f"已存在保存记录={record is not None} 已保存操作={operation is not None}"
            ),
            proposal=binding,
        )
    if proposal is None:
        return AssertionOutcome(
            checkpoint=evidence.checkpoint,
            kind="save_authorization",
            passed=False,
            evidence=(
                f"expected_saved=false proposal_id={binding.proposal_id} "
                "目标提案缺失，无法验证未提交"
            ),
            proposal=binding,
        )
    if proposal.status == "saved":
        return AssertionOutcome(
            checkpoint=evidence.checkpoint,
            kind="save_authorization",
            passed=False,
            evidence=(
                f"expected_saved=false proposal_id={binding.proposal_id} "
                "目标提案状态为 saved"
            ),
            proposal=binding,
        )
    other_records = [
        item.proposal_id
        for item in evidence.after.save_records
        if item.proposal_id != binding.proposal_id
    ]
    return AssertionOutcome(
        checkpoint=evidence.checkpoint,
        kind="save_authorization",
        passed=True,
        evidence=(
            f"expected_saved=false proposal_id={binding.proposal_id} "
            f"status={proposal.status} 保存记录=0 其他保存记录={len(other_records)}"
        ),
        proposal=binding,
    )


def _evaluate_state_transition(
    definition: StateTransitionAssertion, evidence: AssertionEvidence
) -> AssertionOutcome:
    target = evidence.event_target
    if target is None:
        raise AssertionInputError("状态迁移断言缺少事件目标")
    if target.business_kind != definition.business_kind:
        raise AssertionInputError("状态迁移断言的业务类型与事件目标不一致")
    binding = _binding(evidence, target.proposal_ref)
    if binding.business_kind != definition.business_kind:
        raise AssertionInputError("状态迁移断言的业务类型与绑定不一致")
    if definition.to_status == "saved" and evidence.event_type != "confirm":
        raise AssertionInputError("迁移至 saved 必须绑定确认事件检查点")
    if definition.to_status == "invalidated" and evidence.event_type not in (
        "message",
        "cancel",
    ):
        raise AssertionInputError(
            "迁移至 invalidated 必须绑定消息或取消事件检查点"
        )
    before_status = _proposal_status(
        evidence.before, definition.business_kind, binding.proposal_id
    )
    after_status = _proposal_status(
        evidence.after, definition.business_kind, binding.proposal_id
    )
    failures: list[str] = []
    if definition.from_status is None:
        if before_status is not None:
            failures.append(f"检查点前已存在提案状态 {before_status}")
    elif before_status != definition.from_status:
        failures.append(f"检查点前状态为 {before_status}")
    if after_status != definition.to_status:
        failures.append(f"检查点后状态为 {after_status}")
    if definition.to_status == "saved":
        record = _find_save_record(
            evidence.after, definition.business_kind, binding.proposal_id
        )
        if record is None:
            failures.append("缺少目标保存记录")
    cancel_detail = "未要求"
    if definition.to_status == "invalidated":
        if evidence.event_identity is None:
            raise AssertionInputError("失效状态迁移缺少真实事件身份")
        matches = [
            item
            for item in evidence.cancel_evidence
            if item.business_kind == definition.business_kind
            and item.proposal_id == binding.proposal_id
        ]
        if not matches:
            failures.append("缺少目标取消证据")
            cancel_detail = "缺少目标取消证据"
        elif len(matches) > 1:
            failures.append("目标取消证据不唯一")
            cancel_detail = f"目标取消证据不唯一 count={len(matches)}"
        else:
            item = matches[0]
            cancel_detail = (
                f"{item.tool_call_id}:{item.session_id}/"
                f"{item.assistant_run_id}/{item.request_entry_id}"
            )
            if item.session_id != evidence.event_identity.session_id:
                failures.append("取消证据会话与当前事件不一致")
            if item.assistant_run_id != evidence.event_identity.run_id:
                failures.append("取消证据运行与当前事件不一致")
            if item.request_entry_id != evidence.event_identity.request_entry_id:
                failures.append("取消证据请求节点与当前事件不一致")
            if item.source_entry_id == item.result_entry_id:
                failures.append("取消来源助手与工具结果节点相同")
        before_cancelled = any(
            item.business_kind == definition.business_kind
            and item.proposal_id == binding.proposal_id
            for item in evidence.before.cancellations
        )
        after_cancelled = any(
            item.business_kind == definition.business_kind
            and item.proposal_id == binding.proposal_id
            for item in evidence.after.cancellations
        )
        if definition.from_status == "invalidated" and not before_cancelled:
            failures.append("检查点前缺少目标取消记录")
        if definition.from_status != "invalidated" and before_cancelled:
            failures.append("检查点前已存在目标取消记录")
        if not after_cancelled:
            failures.append("缺少目标取消记录")
    return AssertionOutcome(
        checkpoint=evidence.checkpoint,
        kind="state_transition",
        passed=not failures,
        evidence=(
            f"business_kind={definition.business_kind} proposal_id={binding.proposal_id} "
            f"event_type={evidence.event_type} before={before_status} after={after_status} "
            f"expected={definition.from_status}->{definition.to_status} "
            f"cancel_evidence={cancel_detail}；"
            + ("一致" if not failures else "；".join(failures))
        ),
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"非法 JSON 常量: {value}")


def _context_tool_entries(evidence: AssertionEvidence) -> dict[tuple[str, str], ContextItem]:
    entries: dict[tuple[str, str], ContextItem] = {}
    for entry in evidence.context_evidence:
        item = entry.item
        if item.source != "tool_result":
            continue
        key = (item.tool_call_id, item.tool_name)
        if key in entries:
            raise AssertionInputError(
                f"同一工具调用存在多个上下文条目: {item.tool_call_id}"
            )
        entries[key] = item
    return entries


def _require_context_matches_tool(item: ContextItem, tool: ToolResultEvidence) -> None:
    if item.tool_name != tool.tool_name:
        raise AssertionInputError(
            f"工具结果名称与上下文不一致: {tool.tool_call_id}"
        )
    if not item.truncated:
        if item.content != tool.content:
            raise AssertionInputError(
                f"工具结果正文与上下文不一致: {tool.tool_call_id}"
            )
        return
    if (
        item.omitted_chars is None
        or item.original_chars is None
        or item.omitted_chars <= 0
    ):
        raise AssertionInputError(
            f"工具结果裁剪缺少有效元数据: {tool.tool_call_id}"
        )
    if (
        item.original_chars != len(tool.content)
        or item.omitted_chars != len(tool.content) - len(item.content)
        or not tool.content.startswith(item.content)
    ):
        raise AssertionInputError(
            f"工具结果裁剪元数据与正文不一致: {tool.tool_call_id}"
        )


def _verified_tool_results(
    evidence: AssertionEvidence, *, require_all_context: bool
) -> dict[str, ToolResultEvidence]:
    context = _context_tool_entries(evidence)
    verified: dict[str, ToolResultEvidence] = {}
    for tool in evidence.tool_results:
        key = (tool.tool_call_id, tool.tool_name)
        item = context.get(key)
        if item is None:
            raise AssertionInputError(
                f"工具结果未出现在当前检查点上下文: {tool.tool_call_id}"
            )
        _require_context_matches_tool(item, tool)
        verified[tool.tool_call_id] = tool
    if require_all_context:
        result_keys = {
            (tool.tool_call_id, tool.tool_name) for tool in evidence.tool_results
        }
        missing = [key for key in context if key not in result_keys]
        if missing:
            raise AssertionInputError(
                f"上下文工具结果缺少实际工具结果证据: {missing}"
            )
    return verified


def _ranking_tool(evidence: AssertionEvidence, entry: RankingEvidence) -> None:
    matches = [
        tool
        for tool in evidence.tool_results
        if tool.tool_call_id == entry.tool_call_id
    ]
    if len(matches) != 1:
        raise AssertionInputError(
            f"排名证据必须绑定唯一工具结果: {entry.tool_call_id}"
        )
    tool = matches[0]
    if tool.tool_name != entry.tool_name:
        raise AssertionInputError(
            f"排名证据工具名称与工具结果不一致: {entry.tool_call_id}"
        )
    item = _context_tool_entries(evidence).get(
        (tool.tool_call_id, tool.tool_name)
    )
    if item is None:
        raise AssertionInputError(
            f"排名证据未出现在当前检查点上下文: {entry.tool_call_id}"
        )
    _require_context_matches_tool(item, tool)
    if item.truncated:
        raise AssertionInputError(
            f"排名证据要求未裁剪的模型可见工具响应: {entry.tool_call_id}"
        )
    try:
        parsed = json.loads(tool.content, parse_constant=_reject_json_constant)
    except (TypeError, ValueError) as error:
        raise AssertionInputError(
            f"工具结果正文不是合法 JSON: {entry.tool_call_id}"
        ) from error
    if parsed != entry.search.response_json:
        raise AssertionInputError(
            f"排名证据与工具结果响应不一致: {entry.tool_call_id}"
        )


def _validate_visible_search(entry: RankingEvidence) -> int:
    catalog = load_catalog()
    for item in entry.search.model_visible.items:
        exercise = catalog.get(item.exercise_id)
        if exercise is None:
            raise AssertionInputError(
                f"排名证据包含未知动作 ID: {item.exercise_id}"
            )
        if (
            exercise.name,
            exercise.equipment,
            exercise.target,
        ) != (item.name, item.equipment, item.target):
            raise AssertionInputError(
                f"排名证据动作字段与目录不一致: {item.exercise_id}"
            )
    return len(entry.search.model_visible.items)


def _evaluate_evidence_present(
    definition: EvidencePresentAssertion, evidence: AssertionEvidence
) -> AssertionOutcome:
    if definition.evidence_kind == "context":
        count = len(evidence.context_evidence)
        detail = f"entries={count}"
    elif definition.evidence_kind == "tool_result":
        verified = _verified_tool_results(evidence, require_all_context=True)
        count = len(verified)
        detail = "calls=" + ",".join(verified)
    elif definition.evidence_kind == "snapshot":
        count = (
            len(evidence.profile_snapshots)
            + len(evidence.plan_snapshots)
            + len(evidence.workout_snapshots)
        )
        detail = f"snapshots={count}"
    else:
        rows: list[str] = []
        count = 0
        for entry in evidence.rankings:
            _ranking_tool(evidence, entry)
            visible = _validate_visible_search(entry)
            count += visible
            rows.append(f"{entry.tool_call_id}:{entry.search.query_id}={visible}")
        detail = "rankings=" + ",".join(rows) if rows else "rankings=0"
    return AssertionOutcome(
        checkpoint=evidence.checkpoint,
        kind="evidence_present",
        passed=count >= definition.min_count,
        evidence=(
            f"evidence_kind={definition.evidence_kind} min_count={definition.min_count} "
            f"actual={count}；{detail}"
        ),
    )


def _saved_counts(projection: BusinessProjection, business_kind: str) -> tuple[int, str]:
    if business_kind == "profile":
        profile = projection.profile
        if profile is None:
            return 0, "profile=null"
        return 1, f"profile.version={profile.version} empty={profile.content.is_empty()}"
    if business_kind == "plan":
        return len(projection.plans), "plan_ids=" + ",".join(
            plan.id for plan in projection.plans
        )
    return len(projection.workouts), "workout_ids=" + ",".join(
        workout.id for workout in projection.workouts
    )


def _evaluate_no_saved_state(
    definition: NoSavedStateAssertion, evidence: AssertionEvidence
) -> AssertionOutcome:
    count, detail = _saved_counts(evidence.after, definition.business_kind)
    return AssertionOutcome(
        checkpoint=evidence.checkpoint,
        kind="no_saved_state",
        passed=count == 0,
        evidence=f"business_kind={definition.business_kind} saved_count={count}；{detail}",
    )


def _evaluate_saved_state(
    definition: SavedStateAssertion, evidence: AssertionEvidence
) -> AssertionOutcome:
    count, detail = _saved_counts(evidence.after, definition.business_kind)
    return AssertionOutcome(
        checkpoint=evidence.checkpoint,
        kind="saved_state",
        passed=count > 0,
        evidence=f"business_kind={definition.business_kind} saved_count={count}；{detail}",
    )


def _plain_failure(
    evidence: AssertionEvidence, kind: str, message: str
) -> AssertionOutcome:
    return AssertionOutcome(
        checkpoint=evidence.checkpoint,
        kind=kind,
        passed=False,
        evidence=message,
    )


def _binding_failure(
    evidence: AssertionEvidence,
    kind: Literal["snapshot_integrity", "save_authorization"],
    binding: ProposalBinding,
    message: str,
) -> AssertionOutcome:
    return AssertionOutcome(
        checkpoint=evidence.checkpoint,
        kind=kind,
        passed=False,
        evidence=f"proposal_id={binding.proposal_id}；{message}",
        proposal=binding,
    )
