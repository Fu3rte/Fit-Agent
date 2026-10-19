import json
import time
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.ai.messages import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
)
from app.application.business.catalog import Catalog
from app.application.business.confirmation import ConfirmationService
from app.application.business.coordination import ReplacementCoordinator
from app.application.business.profile import business_date
from app.application.business.service import BusinessService
from app.application.session.service import SessionService
from app.domain.business.models import (
    BusinessContext,
    PendingProposalCancellation,
    PlanContent,
    PlanDay,
    PlanExercise,
    PlanPendingProposal,
    PlanProposal,
    PlanRecord,
    PlanSaveRecord,
    PlanSaveResult,
    PlanSnapshot,
    ProfileContent,
    ProfilePendingProposal,
    ProfileProposal,
    ProfileProposalArguments,
    ProfileRecord,
    ProfileSaveRecord,
    ProfileSaveResult,
    ProfileSnapshot,
    SaveConfirmationOperation,
    SaveConfirmationRequest,
    WorkoutContent,
    WorkoutExercise,
    WorkoutListArguments,
    WorkoutPendingProposal,
    WorkoutProposal,
    WorkoutRecord,
    WorkoutSaveRecord,
    WorkoutSaveResult,
    WorkoutSet,
    WorkoutSnapshot,
)
from app.domain.session.models import SendCommand, SendRequest, SessionMessageEntry
from app.infrastructure.persistence.sqlite.business_repository import (
    SqliteBusinessRepository,
)
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from evaluation.contracts import (
    BusinessProjection,
    ContextItem,
    EvidenceLink,
    ModelVisibleSearch,
    ProposalBinding,
    SavedDataComparison,
    SearchResponseEvidence,
    StateComparison,
    VisibleExercise,
)
from evaluation.deterministic_assertions import (
    AssertionEvidence,
    AssertionInputError,
    BusinessContentEvidence,
    CancellationEvidence,
    ContextEvidence,
    DisplayContentEvidence,
    EventIdentity,
    EventTarget,
    PlanSnapshotEvidence,
    ProfileSnapshotEvidence,
    RankingEvidence,
    ToolResultEvidence,
    WorkoutSnapshotEvidence,
    evaluate_assertion,
    evaluate_checkpoint,
)
from test.regression_support import temporary_root

SESSION = "44444444-4444-4444-8444-444444444444"
REQUEST = "55555555-5555-4555-8555-555555555555"
SOURCE = "66666666-6666-4666-8666-666666666666"
DISPLAY = "77777777-7777-4777-8777-777777777777"
CONFIRM = "88888888-8888-4888-8888-888888888888"
OPERATION = "99999999-9999-4999-8999-999999999999"
PROFILE_PROPOSAL = "11111111-1111-4111-8111-111111111111"
PLAN_PROPOSAL = "22222222-2222-4222-8222-222222222222"
WORKOUT_PROPOSAL = "33333333-3333-4333-8333-333333333333"
PLAN_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WORKOUT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SECOND_PROPOSAL = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
SECOND_OPERATION = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
SECOND_DISPLAY = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"

PROFILE_REF = f"profile-proposal-{REQUEST}"
PLAN_REF = f"plan-proposal-{REQUEST}"
WORKOUT_REF = f"workout-proposal-{REQUEST}"


def link(event_id="e01", checkpoint_id="cp1", scenario_id="s-test-01", run_id="run-1"):
    return EvidenceLink(
        run_id=run_id,
        scenario_id=scenario_id,
        event_id=event_id,
        checkpoint_id=checkpoint_id,
    )


def profile(**overrides):
    values = {
        "goal": "增肌",
        "experience": "新手",
        "environment": "健身房",
        "availability": "每周四次",
        "health_notes": None,
        "movement_restrictions": None,
        "unavailable_equipment": None,
        "forbidden_exercise_ids": None,
    }
    values.update(overrides)
    return ProfileContent(**values)


def plan(exercise_ids=("0001",), *, repeat=True, days=None):
    if days is None:
        exercises = [
            PlanExercise(
                exercise_id=exercise_id,
                name=f"动作{exercise_id}",
                sets=3,
                reps=8,
                duration_seconds=None,
                weight_kg=None,
                load_convention=None,
                rest_seconds=60,
            )
            for exercise_id in exercise_ids
        ]
        days = [
            PlanDay(
                kind="training",
                focus="全身",
                exercises=exercises,
                notes=None,
            )
        ]
    return PlanContent(repeat=repeat, days=days, notes=None, suggested_fields=[])


def workout(exercise_ids=("0001",)):
    return WorkoutContent(
        exercises=[
            WorkoutExercise(
                exercise_id=exercise_id,
                name=f"动作{exercise_id}",
                load_convention=None,
                sets=[WorkoutSet(reps=8, weight_kg=None, duration_seconds=None)],
            )
            for exercise_id in exercise_ids
        ],
        notes=None,
    )


def profile_record(content=None, *, version=1, updated_at=1):
    return ProfileRecord(
        version=version, content=content or profile(), updated_at=updated_at
    )


def plan_record(content=None, *, record_id=PLAN_ID, is_current=True, created_at=1):
    return PlanRecord(
        id=record_id,
        is_current=is_current,
        created_at=created_at,
        content=content or plan(),
    )


def workout_record(
    content=None, *, record_id=WORKOUT_ID, version=1, performed_on="2026-01-01"
):
    return WorkoutRecord(
        performed_on=performed_on,
        id=record_id,
        version=version,
        content=content or workout(),
        created_at=1,
        updated_at=1,
    )


def pending_profile(
    payload=None,
    *,
    proposal_id=PROFILE_PROPOSAL,
    status="pending",
    display=DISPLAY,
    confirmation=None,
    base=None,
):
    return ProfilePendingProposal(
        business_kind="profile",
        proposal_id=proposal_id,
        status=status,
        payload=payload or profile(),
        request_entry_id=REQUEST,
        source_entry_id=SOURCE,
        display_entry_id=display,
        confirmation_entry_id=confirmation,
        profile_id=1,
        base_profile_version=base,
    )


def pending_plan(
    payload=None,
    *,
    proposal_id=PLAN_PROPOSAL,
    status="pending",
    display=DISPLAY,
    confirmation=None,
    preparation_kind="generation",
    base_profile=1,
    base_plan=None,
):
    return PlanPendingProposal(
        business_kind="plan",
        proposal_id=proposal_id,
        status=status,
        payload=payload or plan(),
        request_entry_id=REQUEST,
        source_entry_id=SOURCE,
        display_entry_id=display,
        confirmation_entry_id=confirmation,
        preparation_kind=preparation_kind,
        base_profile_version=base_profile,
        base_plan_id=base_plan,
    )


def pending_workout(
    payload=None,
    *,
    proposal_id=WORKOUT_PROPOSAL,
    status="pending",
    display=DISPLAY,
    confirmation=None,
):
    return WorkoutPendingProposal(
        business_kind="workout",
        proposal_id=proposal_id,
        status=status,
        payload=payload or workout(),
        request_entry_id=REQUEST,
        source_entry_id=SOURCE,
        display_entry_id=display,
        confirmation_entry_id=confirmation,
        performed_on="2026-01-01",
        base_workout_id=None,
        base_workout_version=None,
    )


def projection(
    *,
    profile_value=None,
    plans=(),
    workouts=(),
    save_records=(),
    proposals=(),
    operations=(),
    cancellations=(),
):
    return BusinessProjection(
        profile=profile_value,
        plans=list(plans),
        workouts=list(workouts),
        save_records=list(save_records),
        proposals=list(proposals),
        confirmation_operations=list(operations),
        cancellations=list(cancellations),
    )


def search_evidence(query_id, exercise_ids, *, has_more=False):
    catalog = Catalog.load()
    raw_items = []
    visible_items = []
    for exercise_id in exercise_ids:
        exercise = catalog.get(exercise_id)
        name = exercise.name if exercise is not None else f"动作{exercise_id}"
        equipment = exercise.equipment if exercise is not None else "body weight"
        target = exercise.target if exercise is not None else "unknown"
        raw_items.append(
            {
                "id": exercise_id,
                "name": name,
                "equipment": equipment,
                "target": target,
                "matches": [],
            }
        )
        visible_items.append(
            VisibleExercise(
                exercise_id=exercise_id,
                name=name,
                equipment=equipment,
                target=target,
                matches=[],
            )
        )
    response = {"items": raw_items, "has_more": has_more}
    return SearchResponseEvidence(
        query_id=query_id,
        response_json=response,
        model_visible=ModelVisibleSearch(items=visible_items, has_more=has_more),
    )


def ranking_fixture(
    exercise_ids=("0001", "0002"),
    *,
    query_id="q-test-1",
    has_more=False,
    call_id="call-search",
):
    search = search_evidence(query_id, exercise_ids, has_more=has_more)
    content = json.dumps(search.response_json, ensure_ascii=False)
    tool = ToolResultEvidence(
        checkpoint=link(),
        tool_call_id=call_id,
        tool_name="search_exercises",
        content=content,
    )
    context = ContextEvidence(
        checkpoint=link(),
        item=ContextItem(
            source="tool_result",
            content=content,
            fact_origin="tool",
            tool_call_id=call_id,
            tool_name="search_exercises",
        ),
    )
    ranking = RankingEvidence(
        checkpoint=link(),
        tool_call_id=call_id,
        tool_name="search_exercises",
        search=search,
    )
    return tool, context, ranking


def profile_save_record(
    *,
    proposal_id=PROFILE_PROPOSAL,
    content=None,
    version=1,
    display=DISPLAY,
    confirmation=CONFIRM,
    saved_at=10,
):
    result = ProfileSaveResult(
        proposal_id=proposal_id,
        profile_id=1,
        version=version,
        content=content or profile(),
        saved_at=saved_at,
    )
    return ProfileSaveRecord(
        proposal_id=proposal_id,
        session_id=SESSION,
        profile_id=1,
        display_entry_id=display,
        confirmation_entry_id=confirmation,
        result=result,
        saved_at=saved_at,
    )


def plan_save_record(
    *,
    proposal_id=PLAN_PROPOSAL,
    content=None,
    record_id=PLAN_ID,
    display=DISPLAY,
    confirmation=CONFIRM,
    saved_at=10,
):
    result = PlanSaveResult(
        proposal_id=proposal_id,
        id=record_id,
        content=content or plan(),
        created_at=saved_at,
        saved_at=saved_at,
    )
    return PlanSaveRecord(
        proposal_id=proposal_id,
        session_id=SESSION,
        display_entry_id=display,
        confirmation_entry_id=confirmation,
        result=result,
        saved_at=saved_at,
    )


def workout_save_record(
    *,
    proposal_id=WORKOUT_PROPOSAL,
    content=None,
    record_id=WORKOUT_ID,
    display=DISPLAY,
    confirmation=CONFIRM,
    saved_at=10,
    performed_on="2026-01-01",
):
    result = WorkoutSaveResult(
        performed_on=performed_on,
        id=record_id,
        version=1,
        content=content or workout(),
        created_at=saved_at,
        updated_at=saved_at,
        proposal_id=proposal_id,
        saved_at=saved_at,
    )
    return WorkoutSaveRecord(
        proposal_id=proposal_id,
        session_id=SESSION,
        display_entry_id=display,
        confirmation_entry_id=confirmation,
        result=result,
        saved_at=saved_at,
    )


def saved_operation(
    *,
    proposal_id=PROFILE_PROPOSAL,
    operation_id=OPERATION,
    business_kind="profile",
    display=DISPLAY,
    confirmation=CONFIRM,
    result=None,
):
    if result is None:
        result = ProfileSaveResult(
            proposal_id=proposal_id,
            profile_id=1,
            version=1,
            content=profile(),
            saved_at=10,
        )
    return SaveConfirmationOperation(
        session_id=SESSION,
        operation_id=operation_id,
        business_kind=business_kind,
        proposal_id=proposal_id,
        display_entry_id=display,
        confirmation_entry_id=confirmation,
        status="saved",
        result=result,
        error=None,
        created_at=1,
        updated_at=2,
    )


def processing_operation(
    *,
    proposal_id=PROFILE_PROPOSAL,
    operation_id=OPERATION,
    display=DISPLAY,
    confirmation=CONFIRM,
):
    return SaveConfirmationOperation(
        session_id=SESSION,
        operation_id=operation_id,
        business_kind="profile",
        proposal_id=proposal_id,
        display_entry_id=display,
        confirmation_entry_id=confirmation,
        status="processing",
        result=None,
        error=None,
        created_at=1,
        updated_at=1,
    )


def binding(
    *,
    proposal_ref=PROFILE_REF,
    business_kind="profile",
    proposal_id=PROFILE_PROPOSAL,
    display=DISPLAY,
    confirmation=None,
    operation=None,
):
    return ProposalBinding(
        proposal_ref=proposal_ref,
        business_kind=business_kind,
        proposal_id=proposal_id,
        display_entry_id=display,
        confirmation_entry_id=confirmation,
        confirmation_operation_id=operation,
    )


def profile_snapshot(
    *,
    payload=None,
    proposal_id=PROFILE_PROPOSAL,
    status="pending",
    display=DISPLAY,
    confirmation=None,
    base=None,
):
    return ProfileSnapshot(
        proposal_id=proposal_id,
        session_id=SESSION,
        request_entry_id=REQUEST,
        source_entry_id=SOURCE,
        profile_id=1,
        base_profile_version=base,
        payload=payload or profile(),
        display_entry_id=display,
        confirmation_entry_id=confirmation,
        status=status,
        created_at=1,
    )


def plan_snapshot(
    *,
    payload=None,
    proposal_id=PLAN_PROPOSAL,
    status="pending",
    display=DISPLAY,
    confirmation=None,
    preparation_kind="generation",
    base_profile=1,
    base_plan=None,
):
    return PlanSnapshot(
        proposal_id=proposal_id,
        base_profile_version=base_profile,
        base_plan_id=base_plan,
        payload=payload or plan(),
        preparation_kind=preparation_kind,
        session_id=SESSION,
        request_entry_id=REQUEST,
        source_entry_id=SOURCE,
        display_entry_id=display,
        confirmation_entry_id=confirmation,
        status=status,
        created_at=1,
    )


def workout_snapshot(
    *,
    payload=None,
    proposal_id=WORKOUT_PROPOSAL,
    status="pending",
    display=DISPLAY,
    confirmation=None,
    performed_on="2026-01-01",
):
    return WorkoutSnapshot(
        proposal_id=proposal_id,
        performed_on=performed_on,
        base_workout_id=None,
        base_workout_version=None,
        payload=payload or workout(),
        session_id=SESSION,
        request_entry_id=REQUEST,
        source_entry_id=SOURCE,
        display_entry_id=display,
        confirmation_entry_id=confirmation,
        status=status,
        created_at=1,
    )


def profile_display(
    *,
    payload=None,
    proposal_id=PROFILE_PROPOSAL,
    base=None,
    display=DISPLAY,
    checkpoint=None,
    tool_call_id="call-profile",
):
    return DisplayContentEvidence(
        proposal_ref=PROFILE_REF,
        business_kind="profile",
        checkpoint=checkpoint or link(),
        display_entry_id=display,
        tool_call_id=tool_call_id,
        tool_name="prepare_profile_update",
        content=ProfileProposal(
            proposal_id=proposal_id,
            profile_id=1,
            base_profile_version=base,
            payload=payload or profile(),
        ).model_dump(mode="json"),
    )


def plan_display(
    *,
    payload=None,
    proposal_id=PLAN_PROPOSAL,
    display=DISPLAY,
    checkpoint=None,
):
    return DisplayContentEvidence(
        proposal_ref=PLAN_REF,
        business_kind="plan",
        checkpoint=checkpoint or link(),
        display_entry_id=display,
        tool_call_id="call-plan",
        tool_name="prepare_plan",
        content=PlanProposal(
            proposal_id=proposal_id,
            base_profile_version=1,
            base_plan_id=None,
            payload=payload or plan(),
        ).model_dump(mode="json"),
    )


def workout_display(
    *,
    payload=None,
    proposal_id=WORKOUT_PROPOSAL,
    display=DISPLAY,
    checkpoint=None,
):
    return DisplayContentEvidence(
        proposal_ref=WORKOUT_REF,
        business_kind="workout",
        checkpoint=checkpoint or link(),
        display_entry_id=display,
        tool_call_id="call-workout",
        tool_name="prepare_workout",
        content=WorkoutProposal(
            proposal_id=proposal_id,
            performed_on="2026-01-01",
            base_workout_id=None,
            base_workout_version=None,
            payload=payload or workout(),
        ).model_dump(mode="json"),
    )


def content(
    business_kind,
    payload,
    *,
    source="proposal",
    proposal_ref=None,
    record_id=None,
    checkpoint=None,
):
    if proposal_ref is None and source != "saved":
        proposal_ref = {
            "profile": PROFILE_REF,
            "plan": PLAN_REF,
            "workout": WORKOUT_REF,
        }[business_kind]
    return BusinessContentEvidence(
        business_kind=business_kind,
        source=source,
        checkpoint=checkpoint or link(),
        proposal_ref=proposal_ref,
        record_id=record_id,
        payload=payload.model_dump(mode="json"),
    )


def evidence(
    *,
    checkpoint=None,
    event_type="message",
    event_target=None,
    event_identity=None,
    before=None,
    after=None,
    proposal_bindings=(),
    profile_snapshots=(),
    plan_snapshots=(),
    workout_snapshots=(),
    display_contents=(),
    business_contents=(),
    parameter_target=None,
    context_evidence=(),
    tool_results=(),
    rankings=(),
    cancel_evidence=(),
):
    checkpoint = checkpoint or link()
    return AssertionEvidence(
        checkpoint=checkpoint,
        event_type=event_type,
        event_target=event_target,
        event_identity=event_identity,
        before=before or projection(),
        after=after or projection(),
        proposal_bindings=list(proposal_bindings),
        profile_snapshots=list(profile_snapshots),
        plan_snapshots=list(plan_snapshots),
        workout_snapshots=list(workout_snapshots),
        display_contents=list(display_contents),
        business_contents=list(business_contents),
        parameter_target=parameter_target,
        context_evidence=list(context_evidence),
        tool_results=list(tool_results),
        rankings=list(rankings),
        cancel_evidence=list(cancel_evidence),
    )


def definition(kind, *, checkpoint_id="cp1", event_id="e01", **fields):
    return {
        "checkpoint_id": checkpoint_id,
        "event_id": event_id,
        "kind": kind,
        **fields,
    }


def confirm_target(proposal_ref=PROFILE_REF, business_kind="profile"):
    return EventTarget(
        business_kind=business_kind,
        proposal_ref=proposal_ref,
        display_ref=f"{business_kind}-display-e01",
        confirmation_ref=f"{business_kind}-confirm-e01",
    )


def cancel_target(proposal_ref=PROFILE_REF, business_kind="profile"):
    return EventTarget(
        business_kind=business_kind,
        proposal_ref=proposal_ref,
        display_ref=None,
        confirmation_ref=None,
    )


def event_identity(
    *,
    session_id=SESSION,
    run_id="run-1",
    request_entry_id=REQUEST,
):
    return EventIdentity(
        session_id=session_id,
        run_id=run_id,
        request_entry_id=request_entry_id,
    )


def cancellation(
    *,
    checkpoint=None,
    proposal_ref=PROFILE_REF,
    business_kind="profile",
    proposal_id=PROFILE_PROPOSAL,
    session_id=SESSION,
    assistant_run_id="run-1",
    request_entry_id=REQUEST,
    source_entry_id=SOURCE,
    result_entry_id="12121212-1212-4212-8212-121212121212",
    tool_call_id="cancel-call-1",
):
    return CancellationEvidence(
        checkpoint=checkpoint or link(),
        session_id=session_id,
        assistant_run_id=assistant_run_id,
        request_entry_id=request_entry_id,
        source_entry_id=source_entry_id,
        result_entry_id=result_entry_id,
        tool_call_id=tool_call_id,
        business_kind=business_kind,
        proposal_id=proposal_id,
        result=PendingProposalCancellation(
            business_kind=business_kind, proposal_id=proposal_id
        ),
    )


def test_state_unchanged_compares_full_projection_and_scope():
    before = projection(
        profile_value=profile_record(),
        plans=[plan_record()],
        proposals=[pending_profile()],
    )
    identical = before.model_copy(deep=True)
    outcome = evaluate_assertion(
        definition("state_unchanged", business_kind="all"),
        evidence(before=before, after=identical),
    )
    assert outcome.passed
    assert isinstance(outcome.comparison, StateComparison)
    assert outcome.comparison.after.proposals[0].proposal_id == PROFILE_PROPOSAL

    changed = projection(
        profile_value=profile_record(),
        plans=[plan_record(is_current=False)],
        proposals=[pending_profile()],
    )
    profile_outcome = evaluate_assertion(
        definition("state_unchanged", business_kind="profile"),
        evidence(before=before, after=changed),
    )
    assert profile_outcome.passed
    assert profile_outcome.comparison.after.plans == []
    plan_outcome = evaluate_assertion(
        definition("state_unchanged", business_kind="plan"),
        evidence(before=before, after=changed),
    )
    assert not plan_outcome.passed
    assert "is_current" in plan_outcome.evidence
    all_outcome = evaluate_assertion(
        definition("state_unchanged", business_kind="all"),
        evidence(before=before, after=changed),
    )
    assert not all_outcome.passed


def test_state_unchanged_sees_new_proposal_and_cancellation():
    before = projection(profile_value=profile_record())
    after = projection(
        profile_value=profile_record(),
        proposals=[pending_profile(status="invalidated")],
        cancellations=[
            PendingProposalCancellation(
                business_kind="profile", proposal_id=PROFILE_PROPOSAL
            )
        ],
    )
    outcome = evaluate_assertion(
        definition("state_unchanged", business_kind="all"),
        evidence(before=before, after=after),
    )
    assert not outcome.passed
    assert "proposals" in outcome.evidence or "cancellations" in outcome.evidence


def test_saved_data_unchanged_ignores_new_proposal_and_checks_order():
    profile_value = profile_record()
    record = plan_record()
    before = projection(profile_value=profile_value, plans=[record])
    after = projection(
        profile_value=profile_value,
        plans=[record.model_copy(deep=True)],
        proposals=[pending_profile()],
    )
    outcome = evaluate_assertion(
        definition("saved_data_unchanged", business_kind="all"),
        evidence(before=before, after=after),
    )
    assert outcome.passed
    assert isinstance(outcome.comparison, SavedDataComparison)
    assert outcome.comparison.after.profile == profile_value

    version_changed = projection(
        profile_value=profile_record(updated_at=2),
        plans=[record.model_copy(deep=True)],
        proposals=[pending_profile()],
    )
    failed = evaluate_assertion(
        definition("saved_data_unchanged", business_kind="all"),
        evidence(before=before, after=version_changed),
    )
    assert not failed.passed
    assert "updated_at" in failed.evidence

    version_failed = evaluate_assertion(
        definition("saved_data_unchanged", business_kind="profile"),
        evidence(
            before=before,
            after=projection(
                profile_value=profile_record(version=2),
                plans=[record.model_copy(deep=True)],
                proposals=[pending_profile()],
            ),
        ),
    )
    assert not version_failed.passed
    assert "version" in version_failed.evidence

    id_failed = evaluate_assertion(
        definition("saved_data_unchanged", business_kind="plan"),
        evidence(
            before=before,
            after=projection(
                profile_value=profile_value,
                plans=[plan_record(record_id=SECOND_DISPLAY)],
            ),
        ),
    )
    assert not id_failed.passed
    assert "id" in id_failed.evidence

    created_failed = evaluate_assertion(
        definition("saved_data_unchanged", business_kind="plan"),
        evidence(
            before=before,
            after=projection(
                profile_value=profile_value,
                plans=[plan_record(created_at=2)],
            ),
        ),
    )
    assert not created_failed.passed
    assert "created_at" in created_failed.evidence

    reordered = plan(
        days=[
            PlanDay(kind="rest", focus=None, exercises=[], notes=None),
            PlanDay(kind="training", focus="全身", exercises=[], notes=None),
        ]
    )
    order_changed = projection(
        profile_value=profile_value,
        plans=[plan_record(content=reordered)],
    )
    order_failed = evaluate_assertion(
        definition("saved_data_unchanged", business_kind="plan"),
        evidence(before=before, after=order_changed),
    )
    assert not order_failed.passed


def test_equipment_restriction_uses_real_catalog():
    passing = evaluate_assertion(
        definition(
            "equipment_restriction",
            allowed_equipment=["body weight"],
            forbidden_equipment=["barbell", "cable", "dumbbell"],
        ),
        evidence(
            after=projection(proposals=[pending_plan()]),
            business_contents=[content("plan", plan())],
            proposal_bindings=[binding(proposal_ref=PLAN_REF, business_kind="plan", proposal_id=PLAN_PROPOSAL)],
        ),
    )
    assert passing.passed
    assert "0001:body weight" in passing.evidence

    failing = evaluate_assertion(
        definition(
            "equipment_restriction",
            allowed_equipment=["body weight"],
            forbidden_equipment=["barbell"],
        ),
        evidence(
            after=projection(proposals=[pending_plan(payload=plan(exercise_ids=("0023",)))]),
            business_contents=[
                content("plan", plan(exercise_ids=("0023",)))
            ],
            proposal_bindings=[binding(proposal_ref=PLAN_REF, business_kind="plan", proposal_id=PLAN_PROPOSAL)],
        ),
    )
    assert not failing.passed
    assert "0023" in failing.evidence

    unknown_id = evaluate_assertion(
        definition(
            "equipment_restriction",
            allowed_equipment=["body weight"],
            forbidden_equipment=[],
        ),
        evidence(
            after=projection(proposals=[pending_plan(payload=plan(exercise_ids=("9999",)))]),
            business_contents=[content("plan", plan(exercise_ids=("9999",)))],
            proposal_bindings=[binding(proposal_ref=PLAN_REF, business_kind="plan", proposal_id=PLAN_PROPOSAL)],
        ),
    )
    assert not unknown_id.passed
    assert "不在动作目录" in unknown_id.evidence

    empty_plan = plan(
        days=[PlanDay(kind="training", focus=None, exercises=[], notes=None)]
    )
    empty = evaluate_assertion(
        definition(
            "equipment_restriction",
            allowed_equipment=["body weight"],
            forbidden_equipment=[],
        ),
        evidence(
            after=projection(proposals=[pending_plan(payload=empty_plan)]),
            business_contents=[content("plan", empty_plan)],
            proposal_bindings=[binding(proposal_ref=PLAN_REF, business_kind="plan", proposal_id=PLAN_PROPOSAL)],
        ),
    )
    assert not empty.passed

    missing = evaluate_assertion(
        definition(
            "equipment_restriction",
            allowed_equipment=["body weight"],
            forbidden_equipment=[],
        ),
        evidence(),
    )
    assert not missing.passed

    with pytest.raises(AssertionInputError, match="目录外器械"):
        evaluate_assertion(
            definition(
                "equipment_restriction",
                allowed_equipment=["unknown"],
                forbidden_equipment=[],
            ),
            evidence(
                after=projection(proposals=[pending_plan()]),
                business_contents=[content("plan", plan())],
                proposal_bindings=[binding(proposal_ref=PLAN_REF, business_kind="plan", proposal_id=PLAN_PROPOSAL)],
            ),
        )


def test_equipment_rejects_supplied_payload_that_differs_from_real_proposal():
    with pytest.raises(AssertionInputError, match="真实提案"):
        evaluate_assertion(
            definition(
                "equipment_restriction",
                allowed_equipment=["body weight"],
                forbidden_equipment=[],
            ),
            evidence(
                after=projection(
                    proposals=[pending_plan(payload=plan(exercise_ids=("0023",)))]
                ),
                business_contents=[content("plan", plan(exercise_ids=("0001",)))],
                proposal_bindings=[binding(proposal_ref=PLAN_REF, business_kind="plan", proposal_id=PLAN_PROPOSAL)],
            ),
        )


def test_forbidden_exercise_scans_plan_and_workout():
    passing = evaluate_assertion(
        definition("forbidden_exercise", exercise_ids=["0198"]),
        evidence(
            after=projection(proposals=[pending_plan()]),
            business_contents=[content("plan", plan())],
            proposal_bindings=[binding(proposal_ref=PLAN_REF, business_kind="plan", proposal_id=PLAN_PROPOSAL)],
        ),
    )
    assert passing.passed

    failed = evaluate_assertion(
        definition("forbidden_exercise", exercise_ids=["0198"]),
        evidence(
            after=projection(
                proposals=[pending_workout(payload=workout(exercise_ids=("0198",)))]
            ),
            business_contents=[
                content("workout", workout(exercise_ids=("0198",)))
            ],
            proposal_bindings=[binding(proposal_ref=WORKOUT_REF, business_kind="workout", proposal_id=WORKOUT_PROPOSAL)],
        ),
    )
    assert not failed.passed
    assert "0198" in failed.evidence

    missing = evaluate_assertion(
        definition("forbidden_exercise", exercise_ids=["0198"]),
        evidence(),
    )
    assert not missing.passed


def test_forbidden_exercise_rejects_unverifiable_action_ids():
    cases = [
        ("plan", plan(exercise_ids=(None,))),
        ("plan", plan(exercise_ids=("9999",))),
        ("workout", workout(exercise_ids=(None,))),
        ("workout", workout(exercise_ids=("9999",))),
    ]
    for business_kind, payload in cases:
        if business_kind == "plan":
            after = projection(proposals=[pending_plan(payload=payload)])
            proposal_binding = binding(
                proposal_ref=PLAN_REF,
                business_kind="plan",
                proposal_id=PLAN_PROPOSAL,
            )
        else:
            after = projection(proposals=[pending_workout(payload=payload)])
            proposal_binding = binding(
                proposal_ref=WORKOUT_REF,
                business_kind="workout",
                proposal_id=WORKOUT_PROPOSAL,
            )
        outcome = evaluate_assertion(
            definition("forbidden_exercise", exercise_ids=["0001"]),
            evidence(
                after=after,
                business_contents=[content(business_kind, payload)],
                proposal_bindings=[proposal_binding],
            ),
        )
        assert not outcome.passed
        assert "目录身份" in outcome.evidence


def test_forbidden_exercise_stays_on_bound_target():
    other_ref = f"plan-proposal-{SECOND_DISPLAY}"
    after = projection(
        proposals=[
            pending_plan(),
            pending_plan(
                payload=plan(exercise_ids=("0198",)),
                proposal_id=SECOND_PROPOSAL,
                display=SECOND_DISPLAY,
            ),
        ]
    )
    outcome = evaluate_assertion(
        definition("forbidden_exercise", exercise_ids=["0198"]),
        evidence(
            after=after,
            business_contents=[content("plan", plan())],
            proposal_bindings=[
                binding(proposal_ref=PLAN_REF, business_kind="plan", proposal_id=PLAN_PROPOSAL),
                binding(proposal_ref=other_ref, business_kind="plan", proposal_id=SECOND_PROPOSAL, display=SECOND_DISPLAY),
            ],
        ),
    )
    assert outcome.passed

    with pytest.raises(AssertionInputError, match="事件目标"):
        evaluate_assertion(
            definition(
                "forbidden_exercise",
                checkpoint_id="cp2",
                event_id="e03",
                exercise_ids=["0198"],
            ),
            evidence(
                checkpoint=link(event_id="e03", checkpoint_id="cp2"),
                event_type="confirm",
                event_target=confirm_target(proposal_ref=other_ref, business_kind="plan"),
                after=after,
                business_contents=[
                    content("plan", plan(), checkpoint=link(event_id="e03", checkpoint_id="cp2"))
                ],
                proposal_bindings=[
                    binding(proposal_ref=other_ref, business_kind="plan", proposal_id=SECOND_PROPOSAL, display=SECOND_DISPLAY)
                ],
            ),
        )


def test_parameter_preserved_pointer_values_and_missing_paths():
    target = content("profile", profile(), source="proposal")
    passing = evaluate_assertion(
        definition("parameter_preserved", field_path="/goal", expected="增肌"),
        evidence(
            after=projection(proposals=[pending_profile()]),
            proposal_bindings=[binding()],
            parameter_target=target,
        ),
    )
    assert passing.passed

    failing = evaluate_assertion(
        definition("parameter_preserved", field_path="/goal", expected="减脂"),
        evidence(
            after=projection(proposals=[pending_profile()]),
            proposal_bindings=[binding()],
            parameter_target=target,
        ),
    )
    assert not failing.passed

    null_value = evaluate_assertion(
        definition("parameter_preserved", field_path="/health_notes", expected=None),
        evidence(
            after=projection(proposals=[pending_profile()]),
            proposal_bindings=[binding()],
            parameter_target=target,
        ),
    )
    assert null_value.passed

    null_failure = evaluate_assertion(
        definition("parameter_preserved", field_path="/health_notes", expected="无"),
        evidence(
            after=projection(proposals=[pending_profile()]),
            proposal_bindings=[binding()],
            parameter_target=target,
        ),
    )
    assert not null_failure.passed

    missing = evaluate_assertion(
        definition("parameter_preserved", field_path="/missing", expected="x"),
        evidence(
            after=projection(proposals=[pending_profile()]),
            proposal_bindings=[binding()],
            parameter_target=target,
        ),
    )
    assert not missing.passed
    assert "路径不存在" in missing.evidence

    plan_target = content("plan", plan())
    out_of_bounds = evaluate_assertion(
        definition("parameter_preserved", field_path="/days/9/kind", expected="training"),
        evidence(
            after=projection(proposals=[pending_plan()]),
            proposal_bindings=[binding(proposal_ref=PLAN_REF, business_kind="plan", proposal_id=PLAN_PROPOSAL)],
            parameter_target=plan_target,
        ),
    )
    assert not out_of_bounds.passed
    assert "数组索引越界" in out_of_bounds.evidence

    index_value = evaluate_assertion(
        definition(
            "parameter_preserved", field_path="/days/0/exercises/0/sets", expected=3
        ),
        evidence(
            after=projection(proposals=[pending_plan()]),
            proposal_bindings=[binding(proposal_ref=PLAN_REF, business_kind="plan", proposal_id=PLAN_PROPOSAL)],
            parameter_target=plan_target,
        ),
    )
    assert index_value.passed

    boolean_strict = evaluate_assertion(
        definition("parameter_preserved", field_path="/repeat", expected=1),
        evidence(
            after=projection(proposals=[pending_plan()]),
            proposal_bindings=[binding(proposal_ref=PLAN_REF, business_kind="plan", proposal_id=PLAN_PROPOSAL)],
            parameter_target=plan_target,
        ),
    )
    assert not boolean_strict.passed

    with pytest.raises(ValidationError):
        evaluate_assertion(
            definition("parameter_preserved", field_path="goal", expected="增肌"),
            evidence(
                after=projection(proposals=[pending_profile()]),
                proposal_bindings=[binding()],
                parameter_target=target,
            ),
        )

    no_target = evaluate_assertion(
        definition("parameter_preserved", field_path="/goal", expected="增肌"),
        evidence(
            after=projection(proposals=[pending_profile()]),
            proposal_bindings=[binding()],
        ),
    )
    assert not no_target.passed


def test_snapshot_integrity_passes_and_reports_drift():
    snapshot = profile_snapshot()
    proposal = pending_profile()
    passing = evaluate_assertion(
        definition("snapshot_integrity", proposal_ref=PROFILE_REF),
        evidence(
            after=projection(proposals=[proposal]),
            proposal_bindings=[binding()],
            profile_snapshots=[
                ProfileSnapshotEvidence(
                    proposal_ref=PROFILE_REF, checkpoint=link(), snapshot=snapshot
                )
            ],
            display_contents=[profile_display()],
        ),
    )
    assert passing.passed
    assert passing.proposal.proposal_id == PROFILE_PROPOSAL

    drifted = evaluate_assertion(
        definition("snapshot_integrity", proposal_ref=PROFILE_REF),
        evidence(
            after=projection(proposals=[proposal]),
            proposal_bindings=[binding()],
            profile_snapshots=[
                ProfileSnapshotEvidence(
                    proposal_ref=PROFILE_REF, checkpoint=link(), snapshot=snapshot
                )
            ],
            display_contents=[
                profile_display(payload=profile(goal="减脂"))
            ],
        ),
    )
    assert not drifted.passed
    assert "展示 payload 漂移" in drifted.evidence

    missing_field_display = profile_display()
    missing_field_display = missing_field_display.model_copy(
        update={"content": {"proposal_id": PROFILE_PROPOSAL}}
    )
    missing = evaluate_assertion(
        definition("snapshot_integrity", proposal_ref=PROFILE_REF),
        evidence(
            after=projection(proposals=[proposal]),
            proposal_bindings=[binding()],
            profile_snapshots=[
                ProfileSnapshotEvidence(
                    proposal_ref=PROFILE_REF, checkpoint=link(), snapshot=snapshot
                )
            ],
            display_contents=[missing_field_display],
        ),
    )
    assert not missing.passed
    assert "缺字段" in missing.evidence

    wrong_display = evaluate_assertion(
        definition("snapshot_integrity", proposal_ref=PROFILE_REF),
        evidence(
            after=projection(proposals=[proposal]),
            proposal_bindings=[binding()],
            profile_snapshots=[
                ProfileSnapshotEvidence(
                    proposal_ref=PROFILE_REF, checkpoint=link(), snapshot=snapshot
                )
            ],
            display_contents=[profile_display(display=SECOND_DISPLAY)],
        ),
    )
    assert not wrong_display.passed
    assert "展示绑定" in wrong_display.evidence

    current_drift = evaluate_assertion(
        definition("snapshot_integrity", proposal_ref=PROFILE_REF),
        evidence(
            after=projection(proposals=[pending_profile(payload=profile(goal="减脂"))]),
            proposal_bindings=[binding()],
            profile_snapshots=[
                ProfileSnapshotEvidence(
                    proposal_ref=PROFILE_REF, checkpoint=link(), snapshot=snapshot
                )
            ],
            display_contents=[profile_display()],
        ),
    )
    assert not current_drift.passed
    assert "准备快照" in current_drift.evidence

    missing_snapshot = evaluate_assertion(
        definition("snapshot_integrity", proposal_ref=PROFILE_REF),
        evidence(
            after=projection(proposals=[proposal]),
            proposal_bindings=[binding()],
            display_contents=[profile_display()],
        ),
    )
    assert not missing_snapshot.passed

    with pytest.raises(AssertionInputError, match="真实绑定"):
        evaluate_assertion(
            definition("snapshot_integrity", proposal_ref=PROFILE_REF),
            evidence(
                after=projection(proposals=[proposal]),
                display_contents=[profile_display()],
            ),
        )


def test_snapshot_integrity_supports_plan_and_workout():
    plan_outcome = evaluate_assertion(
        definition("snapshot_integrity", proposal_ref=PLAN_REF),
        evidence(
            after=projection(proposals=[pending_plan()]),
            proposal_bindings=[
                binding(
                    proposal_ref=PLAN_REF,
                    business_kind="plan",
                    proposal_id=PLAN_PROPOSAL,
                )
            ],
            plan_snapshots=[
                PlanSnapshotEvidence(
                    proposal_ref=PLAN_REF, checkpoint=link(), snapshot=plan_snapshot()
                )
            ],
            display_contents=[plan_display()],
        ),
    )
    assert plan_outcome.passed

    workout_outcome = evaluate_assertion(
        definition("snapshot_integrity", proposal_ref=WORKOUT_REF),
        evidence(
            after=projection(proposals=[pending_workout()]),
            proposal_bindings=[
                binding(
                    proposal_ref=WORKOUT_REF,
                    business_kind="workout",
                    proposal_id=WORKOUT_PROPOSAL,
                )
            ],
            workout_snapshots=[
                WorkoutSnapshotEvidence(
                    proposal_ref=WORKOUT_REF,
                    checkpoint=link(),
                    snapshot=workout_snapshot(),
                )
            ],
            display_contents=[workout_display()],
        ),
    )
    assert workout_outcome.passed


def test_save_authorization_target_identity_and_chain():
    pending = pending_profile()
    passing = evaluate_assertion(
        definition(
            "save_authorization", proposal_ref=PROFILE_REF, expected_saved=False
        ),
        evidence(
            after=projection(proposals=[pending]),
            proposal_bindings=[binding()],
        ),
    )
    assert passing.passed
    assert passing.proposal.proposal_id == PROFILE_PROPOSAL

    other_record = plan_save_record()
    with_other_saved = evaluate_assertion(
        definition(
            "save_authorization", proposal_ref=PROFILE_REF, expected_saved=False
        ),
        evidence(
            after=projection(
                proposals=[pending],
                plans=[plan_record()],
                save_records=[other_record],
            ),
            proposal_bindings=[binding()],
        ),
    )
    assert with_other_saved.passed
    assert "其他保存记录=1" in with_other_saved.evidence

    already_saved = evaluate_assertion(
        definition(
            "save_authorization",
            checkpoint_id="cp2",
            event_id="e03",
            proposal_ref=PROFILE_REF,
            expected_saved=False,
        ),
        evidence(
            checkpoint=link(event_id="e03", checkpoint_id="cp2"),
            event_type="confirm",
            event_target=confirm_target(),
            after=projection(
                profile_value=profile_record(),
                proposals=[pending_profile(status="saved")],
                save_records=[profile_save_record()],
            ),
            proposal_bindings=[binding(confirmation=CONFIRM, operation=OPERATION)],
        ),
    )
    assert not already_saved.passed

    missing_target = evaluate_assertion(
        definition(
            "save_authorization", proposal_ref=PROFILE_REF, expected_saved=False
        ),
        evidence(after=projection(), proposal_bindings=[binding()]),
    )
    assert not missing_target.passed

    confirm_link = link(event_id="e03", checkpoint_id="cp2")
    operation = saved_operation()
    saved = evaluate_assertion(
        definition(
            "save_authorization",
            checkpoint_id="cp2",
            event_id="e03",
            proposal_ref=PROFILE_REF,
            expected_saved=True,
        ),
        evidence(
            checkpoint=confirm_link,
            event_type="confirm",
            event_target=confirm_target(),
            after=projection(
                profile_value=profile_record(),
                proposals=[pending_profile(status="saved", confirmation=CONFIRM)],
                save_records=[profile_save_record()],
                operations=[operation],
            ),
            proposal_bindings=[
                binding(confirmation=CONFIRM, operation=OPERATION)
            ],
            profile_snapshots=[
                ProfileSnapshotEvidence(
                    proposal_ref=PROFILE_REF,
                    checkpoint=confirm_link,
                    snapshot=profile_snapshot(
                        status="saved", confirmation=CONFIRM
                    ),
                )
            ],
        ),
    )
    assert saved.passed

    processing = evaluate_assertion(
        definition(
            "save_authorization",
            checkpoint_id="cp2",
            event_id="e03",
            proposal_ref=PROFILE_REF,
            expected_saved=True,
        ),
        evidence(
            checkpoint=confirm_link,
            event_type="confirm",
            event_target=confirm_target(),
            after=projection(
                proposals=[pending_profile(confirmation=CONFIRM)],
                operations=[processing_operation()],
            ),
            proposal_bindings=[
                binding(confirmation=CONFIRM, operation=OPERATION)
            ],
        ),
    )
    assert not processing.passed
    assert "processing" in processing.evidence

    wrong_operation = evaluate_assertion(
        definition(
            "save_authorization",
            checkpoint_id="cp2",
            event_id="e03",
            proposal_ref=PROFILE_REF,
            expected_saved=True,
        ),
        evidence(
            checkpoint=confirm_link,
            event_type="confirm",
            event_target=confirm_target(),
            after=projection(
                profile_value=profile_record(),
                proposals=[pending_profile(status="saved", confirmation=CONFIRM)],
                save_records=[profile_save_record()],
                operations=[saved_operation(operation_id=SECOND_OPERATION)],
            ),
            proposal_bindings=[
                binding(confirmation=CONFIRM, operation=OPERATION)
            ],
        ),
    )
    assert not wrong_operation.passed

    with pytest.raises(AssertionInputError, match="确认事件"):
        evaluate_assertion(
            definition(
                "save_authorization", proposal_ref=PROFILE_REF, expected_saved=True
            ),
            evidence(
                after=projection(
                    profile_value=profile_record(),
                    proposals=[pending_profile(status="saved", confirmation=CONFIRM)],
                    save_records=[profile_save_record()],
                    operations=[saved_operation()],
                ),
                proposal_bindings=[
                    binding(confirmation=CONFIRM, operation=OPERATION)
                ],
            ),
        )

    with pytest.raises(AssertionInputError, match="未保存断言"):
        evaluate_assertion(
            definition(
                "save_authorization", proposal_ref=PROFILE_REF, expected_saved=False
            ),
            evidence(
                after=projection(proposals=[pending]),
                proposal_bindings=[
                    binding(confirmation=CONFIRM, operation=OPERATION)
                ],
            ),
        )


def test_save_authorization_rejects_saved_content_drift():
    confirm_link = link(event_id="e03", checkpoint_id="cp2")
    wrong = profile(goal="减脂")
    record = profile_save_record(content=wrong)
    outcome = evaluate_assertion(
        definition(
            "save_authorization",
            checkpoint_id="cp2",
            event_id="e03",
            proposal_ref=PROFILE_REF,
            expected_saved=True,
        ),
        evidence(
            checkpoint=confirm_link,
            event_type="confirm",
            event_target=confirm_target(),
            after=projection(
                profile_value=profile_record(content=wrong),
                proposals=[pending_profile(status="saved", confirmation=CONFIRM)],
                save_records=[record],
                operations=[saved_operation(result=record.result)],
            ),
            proposal_bindings=[
                binding(confirmation=CONFIRM, operation=OPERATION)
            ],
            profile_snapshots=[
                ProfileSnapshotEvidence(
                    proposal_ref=PROFILE_REF,
                    checkpoint=confirm_link,
                    snapshot=profile_snapshot(
                        payload=profile(goal="增肌"),
                        status="saved",
                        confirmation=CONFIRM,
                    ),
                )
            ],
        ),
    )
    assert not outcome.passed
    assert "保存正文" in outcome.evidence


def test_save_authorization_rejects_workout_date_drift():
    confirm_link = link(event_id="e03", checkpoint_id="cp2")
    record = workout_save_record(performed_on="2026-01-02")
    outcome = evaluate_assertion(
        definition(
            "save_authorization",
            checkpoint_id="cp2",
            event_id="e03",
            proposal_ref=WORKOUT_REF,
            expected_saved=True,
        ),
        evidence(
            checkpoint=confirm_link,
            event_type="confirm",
            event_target=confirm_target(WORKOUT_REF, "workout"),
            after=projection(
                workouts=[workout_record(performed_on="2026-01-02")],
                proposals=[
                    pending_workout(status="saved", confirmation=CONFIRM)
                ],
                save_records=[record],
                operations=[
                    saved_operation(
                        business_kind="workout",
                        proposal_id=WORKOUT_PROPOSAL,
                        result=record.result,
                    )
                ],
            ),
            proposal_bindings=[
                binding(
                    proposal_ref=WORKOUT_REF,
                    business_kind="workout",
                    proposal_id=WORKOUT_PROPOSAL,
                    confirmation=CONFIRM,
                    operation=OPERATION,
                )
            ],
            workout_snapshots=[
                WorkoutSnapshotEvidence(
                    proposal_ref=WORKOUT_REF,
                    checkpoint=confirm_link,
                    snapshot=workout_snapshot(
                        status="saved",
                        confirmation=CONFIRM,
                        performed_on="2026-01-01",
                    ),
                )
            ],
        ),
    )
    assert not outcome.passed
    assert "训练日期" in outcome.evidence


def test_state_transition_pending_saved_cancel_and_repeat():
    confirm_link = link(event_id="e03", checkpoint_id="cp2")
    before = projection(proposals=[pending_profile()])
    after_saved = projection(
        profile_value=profile_record(),
        proposals=[pending_profile(status="saved", confirmation=CONFIRM)],
        save_records=[profile_save_record()],
        operations=[saved_operation()],
    )
    passed = evaluate_assertion(
        definition(
            "state_transition",
            checkpoint_id="cp2",
            event_id="e03",
            business_kind="profile",
            from_status="pending",
            to_status="saved",
        ),
        evidence(
            checkpoint=confirm_link,
            event_type="confirm",
            event_target=confirm_target(),
            before=before,
            after=after_saved,
            proposal_bindings=[
                binding(confirmation=CONFIRM, operation=OPERATION)
            ],
        ),
    )
    assert passed.passed

    repeated = evaluate_assertion(
        definition(
            "state_transition",
            checkpoint_id="cp2",
            event_id="e03",
            business_kind="profile",
            from_status="saved",
            to_status="saved",
        ),
        evidence(
            checkpoint=confirm_link,
            event_type="confirm",
            event_target=confirm_target(),
            before=after_saved,
            after=after_saved.model_copy(deep=True),
            proposal_bindings=[
                binding(confirmation=CONFIRM, operation=OPERATION)
            ],
        ),
    )
    assert repeated.passed

    new_target = evaluate_assertion(
        definition(
            "state_transition",
            checkpoint_id="cp2",
            event_id="e03",
            business_kind="profile",
            from_status=None,
            to_status="saved",
        ),
        evidence(
            checkpoint=confirm_link,
            event_type="confirm",
            event_target=confirm_target(),
            before=projection(),
            after=after_saved,
            proposal_bindings=[
                binding(confirmation=CONFIRM, operation=OPERATION)
            ],
        ),
    )
    assert new_target.passed

    unexpected_before = evaluate_assertion(
        definition(
            "state_transition",
            checkpoint_id="cp2",
            event_id="e03",
            business_kind="profile",
            from_status=None,
            to_status="saved",
        ),
        evidence(
            checkpoint=confirm_link,
            event_type="confirm",
            event_target=confirm_target(),
            before=before,
            after=after_saved,
            proposal_bindings=[
                binding(confirmation=CONFIRM, operation=OPERATION)
            ],
        ),
    )
    assert not unexpected_before.passed

    cancel_link = link(event_id="e04", checkpoint_id="cp3")
    before_cancel = projection(proposals=[pending_profile()])
    after_cancel = projection(
        proposals=[pending_profile(status="invalidated")],
        cancellations=[
            PendingProposalCancellation(
                business_kind="profile", proposal_id=PROFILE_PROPOSAL
            )
        ],
    )
    cancelled = evaluate_assertion(
        definition(
            "state_transition",
            checkpoint_id="cp3",
            event_id="e04",
            business_kind="profile",
            from_status="pending",
            to_status="invalidated",
        ),
        evidence(
            checkpoint=cancel_link,
            event_type="cancel",
            event_target=cancel_target(),
            event_identity=event_identity(),
            before=before_cancel,
            after=after_cancel,
            proposal_bindings=[binding()],
            cancel_evidence=[cancellation(checkpoint=cancel_link)],
        ),
    )
    assert cancelled.passed

    missing_cancellation = evaluate_assertion(
        definition(
            "state_transition",
            checkpoint_id="cp3",
            event_id="e04",
            business_kind="profile",
            from_status="pending",
            to_status="invalidated",
        ),
        evidence(
            checkpoint=cancel_link,
            event_type="cancel",
            event_target=cancel_target(),
            event_identity=event_identity(),
            before=before_cancel,
            after=projection(proposals=[pending_profile(status="invalidated")]),
            proposal_bindings=[binding()],
        ),
    )
    assert not missing_cancellation.passed
    assert "取消证据" in missing_cancellation.evidence

    other_cancellation = evaluate_assertion(
        definition(
            "state_transition",
            checkpoint_id="cp3",
            event_id="e04",
            business_kind="profile",
            from_status="pending",
            to_status="invalidated",
        ),
        evidence(
            checkpoint=cancel_link,
            event_type="cancel",
            event_target=cancel_target(),
            event_identity=event_identity(),
            before=before_cancel,
            after=projection(
                proposals=[pending_profile(status="invalidated")],
                cancellations=[
                    PendingProposalCancellation(
                        business_kind="profile", proposal_id=SECOND_PROPOSAL
                    )
                ],
            ),
            proposal_bindings=[binding()],
            cancel_evidence=[cancellation(checkpoint=cancel_link)],
        ),
    )
    assert not other_cancellation.passed
    assert "取消记录" in other_cancellation.evidence

    with pytest.raises(AssertionInputError, match="saved"):
        evaluate_assertion(
            definition(
                "state_transition",
                checkpoint_id="cp3",
                event_id="e04",
                business_kind="profile",
                from_status="pending",
                to_status="saved",
            ),
            evidence(
                checkpoint=cancel_link,
                event_type="cancel",
                event_target=cancel_target(),
                before=before,
                after=after_saved,
                proposal_bindings=[binding()],
            ),
        )

    with pytest.raises(AssertionInputError, match="invalidated"):
        evaluate_assertion(
            definition(
                "state_transition",
                checkpoint_id="cp2",
                event_id="e03",
                business_kind="profile",
                from_status="pending",
                to_status="invalidated",
            ),
            evidence(
                checkpoint=confirm_link,
                event_type="confirm",
                event_target=confirm_target(),
                before=before_cancel,
                after=after_cancel,
                proposal_bindings=[binding()],
            ),
        )


def test_state_transition_invalidated_on_message_requires_real_cancel_evidence():
    message_link = link(event_id="e02", checkpoint_id="cp2")
    before = projection(proposals=[pending_profile()])
    after = projection(
        proposals=[pending_profile(status="invalidated")],
        cancellations=[
            PendingProposalCancellation(
                business_kind="profile", proposal_id=PROFILE_PROPOSAL
            )
        ],
    )
    transition = definition(
        "state_transition",
        checkpoint_id="cp2",
        event_id="e02",
        business_kind="profile",
        from_status="pending",
        to_status="invalidated",
    )
    passed = evaluate_assertion(
        transition,
        evidence(
            checkpoint=message_link,
            event_target=cancel_target(),
            event_identity=event_identity(),
            before=before,
            after=after,
            proposal_bindings=[binding()],
            cancel_evidence=[cancellation(checkpoint=message_link)],
        ),
    )
    assert passed.passed
    assert "pending->invalidated" in passed.evidence

    missing = evaluate_assertion(
        transition,
        evidence(
            checkpoint=message_link,
            event_target=cancel_target(),
            event_identity=event_identity(),
            before=before,
            after=after,
            proposal_bindings=[binding()],
        ),
    )
    assert not missing.passed
    assert "缺少目标取消证据" in missing.evidence

    wrong_target = evaluate_assertion(
        transition,
        evidence(
            checkpoint=message_link,
            event_target=cancel_target(),
            event_identity=event_identity(),
            before=before,
            after=after,
            proposal_bindings=[binding()],
            cancel_evidence=[
                cancellation(
                    checkpoint=message_link,
                    proposal_id=SECOND_PROPOSAL,
                    result_entry_id="13131313-1313-4313-8313-131313131313",
                )
            ],
        ),
    )
    assert not wrong_target.passed
    assert "缺少目标取消证据" in wrong_target.evidence

    wrong_run = evaluate_assertion(
        transition,
        evidence(
            checkpoint=message_link,
            event_target=cancel_target(),
            event_identity=event_identity(),
            before=before,
            after=after,
            proposal_bindings=[binding()],
            cancel_evidence=[
                cancellation(
                    checkpoint=message_link, assistant_run_id="other-run"
                )
            ],
        ),
    )
    assert not wrong_run.passed
    assert "运行" in wrong_run.evidence

    ambiguous = evaluate_assertion(
        transition,
        evidence(
            checkpoint=message_link,
            event_target=cancel_target(),
            event_identity=event_identity(),
            before=before,
            after=after,
            proposal_bindings=[binding()],
            cancel_evidence=[
                cancellation(checkpoint=message_link),
                cancellation(
                    checkpoint=message_link,
                    result_entry_id="14141414-1414-4414-8414-141414141414",
                ),
            ],
        ),
    )
    assert not ambiguous.passed
    assert "不唯一" in ambiguous.evidence

    with pytest.raises(AssertionInputError, match="事件身份"):
        evaluate_assertion(
            transition,
            evidence(
                checkpoint=message_link,
                event_target=cancel_target(),
                before=before,
                after=after,
                proposal_bindings=[binding()],
                cancel_evidence=[cancellation(checkpoint=message_link)],
            ),
        )

    unchanged = evaluate_assertion(
        transition,
        evidence(
            checkpoint=message_link,
            event_target=cancel_target(),
            event_identity=event_identity(),
            before=before,
            after=before,
            proposal_bindings=[binding()],
        ),
    )
    assert not unchanged.passed
    assert "检查点后状态为 pending" in unchanged.evidence


def test_state_transition_invalidated_post_checkpoint_keeps_real_evidence():
    message_link = link(event_id="e03", checkpoint_id="cp3")
    invalidated = projection(
        proposals=[pending_profile(status="invalidated")],
        cancellations=[
            PendingProposalCancellation(
                business_kind="profile", proposal_id=PROFILE_PROPOSAL
            )
        ],
    )
    outcome = evaluate_assertion(
        definition(
            "state_transition",
            checkpoint_id="cp3",
            event_id="e03",
            business_kind="profile",
            from_status="invalidated",
            to_status="invalidated",
        ),
        evidence(
            checkpoint=message_link,
            event_type="cancel",
            event_target=cancel_target(),
            event_identity=event_identity(),
            before=invalidated,
            after=invalidated.model_copy(deep=True),
            proposal_bindings=[binding()],
            cancel_evidence=[cancellation(checkpoint=message_link)],
        ),
    )
    assert outcome.passed

    unbacked = evaluate_assertion(
        definition(
            "state_transition",
            checkpoint_id="cp3",
            event_id="e03",
            business_kind="profile",
            from_status="invalidated",
            to_status="invalidated",
        ),
        evidence(
            checkpoint=message_link,
            event_type="cancel",
            event_target=cancel_target(),
            event_identity=event_identity(),
            before=projection(proposals=[pending_profile(status="invalidated")]),
            after=invalidated,
            proposal_bindings=[binding()],
            cancel_evidence=[cancellation(checkpoint=message_link)],
        ),
    )
    assert not unbacked.passed
    assert "检查点前缺少目标取消记录" in unbacked.evidence


def test_state_transition_conflicted_status_is_preserved():
    confirm_link = link(event_id="e03", checkpoint_id="cp2")
    outcome = evaluate_assertion(
        definition(
            "state_transition",
            checkpoint_id="cp2",
            event_id="e03",
            business_kind="profile",
            from_status="pending",
            to_status="conflicted",
        ),
        evidence(
            checkpoint=confirm_link,
            event_type="confirm",
            event_target=confirm_target(),
            before=projection(proposals=[pending_profile()]),
            after=projection(proposals=[pending_profile(status="conflicted")]),
            proposal_bindings=[binding()],
        ),
    )
    assert outcome.passed
    assert "conflicted" in outcome.evidence


def test_snapshot_integrity_rejects_binding_to_other_proposal():
    with pytest.raises(AssertionInputError, match="proposal_id"):
        evaluate_assertion(
            definition("snapshot_integrity", proposal_ref=PROFILE_REF),
            evidence(
                after=projection(
                    proposals=[
                        pending_profile(
                            proposal_id=SECOND_PROPOSAL, display=SECOND_DISPLAY
                        )
                    ]
                ),
                proposal_bindings=[
                    binding(
                        proposal_id=SECOND_PROPOSAL, display=SECOND_DISPLAY
                    )
                ],
                profile_snapshots=[
                    ProfileSnapshotEvidence(
                        proposal_ref=PROFILE_REF,
                        checkpoint=link(),
                        snapshot=profile_snapshot(),
                    )
                ],
                display_contents=[
                    profile_display(
                        proposal_id=SECOND_PROPOSAL, display=SECOND_DISPLAY
                    )
                ],
            ),
        )


def test_evidence_present_context_tool_and_snapshot_sources():
    context_items = [
        ContextEvidence(
            checkpoint=link(),
            item=ContextItem(
                source="user_message", content="训练问题", fact_origin="user"
            ),
        ),
        ContextEvidence(
            checkpoint=link(),
            item=ContextItem(
                source="tool_result",
                content="动作结果",
                fact_origin="tool",
                tool_call_id="call-1",
                tool_name="search_exercises",
            ),
        ),
    ]
    context_pass = evaluate_assertion(
        definition("evidence_present", evidence_kind="context", min_count=2),
        evidence(context_evidence=context_items),
    )
    assert context_pass.passed
    context_fail = evaluate_assertion(
        definition("evidence_present", evidence_kind="context", min_count=3),
        evidence(context_evidence=context_items),
    )
    assert not context_fail.passed

    tool = ToolResultEvidence(
        checkpoint=link(),
        tool_call_id="call-1",
        tool_name="search_exercises",
        content="动作结果",
    )
    tool_pass = evaluate_assertion(
        definition("evidence_present", evidence_kind="tool_result", min_count=1),
        evidence(context_evidence=context_items, tool_results=[tool]),
    )
    assert tool_pass.passed
    tool_fail = evaluate_assertion(
        definition("evidence_present", evidence_kind="tool_result", min_count=2),
        evidence(context_evidence=context_items, tool_results=[tool]),
    )
    assert not tool_fail.passed

    with pytest.raises(AssertionInputError, match="上下文"):
        evaluate_assertion(
            definition("evidence_present", evidence_kind="tool_result", min_count=1),
            evidence(tool_results=[tool]),
        )

    snapshot_pass = evaluate_assertion(
        definition("evidence_present", evidence_kind="snapshot", min_count=1),
        evidence(
            profile_snapshots=[
                ProfileSnapshotEvidence(
                    proposal_ref=PROFILE_REF,
                    checkpoint=link(),
                    snapshot=profile_snapshot(),
                )
            ]
        ),
    )
    assert snapshot_pass.passed


def test_tool_result_evidence_rejects_context_body_mismatch():
    tool = ToolResultEvidence(
        checkpoint=link(),
        tool_call_id="call-1",
        tool_name="search_exercises",
        content="tool-content-B",
    )
    conflicting = ContextEvidence(
        checkpoint=link(),
        item=ContextItem(
            source="tool_result",
            content="context-content-A",
            fact_origin="tool",
            tool_call_id="call-1",
            tool_name="search_exercises",
        ),
    )
    with pytest.raises(AssertionInputError, match="正文与上下文不一致"):
        evaluate_assertion(
            definition("evidence_present", evidence_kind="tool_result", min_count=1),
            evidence(context_evidence=[conflicting], tool_results=[tool]),
        )

    truncated_tool = ToolResultEvidence(
        checkpoint=link(),
        tool_call_id="call-2",
        tool_name="search_exercises",
        content="abcdef",
    )
    truncated_context = ContextEvidence(
        checkpoint=link(),
        item=ContextItem(
            source="tool_result",
            content="abc",
            fact_origin="tool",
            tool_call_id="call-2",
            tool_name="search_exercises",
            truncated=True,
            omitted_chars=3,
            original_chars=6,
        ),
    )
    truncated = evaluate_assertion(
        definition("evidence_present", evidence_kind="tool_result", min_count=1),
        evidence(
            context_evidence=[truncated_context],
            tool_results=[truncated_tool],
        ),
    )
    assert truncated.passed

    invalid_metadata = ContextEvidence(
        checkpoint=link(),
        item=ContextItem(
            source="tool_result",
            content="abc",
            fact_origin="tool",
            tool_call_id="call-2",
            tool_name="search_exercises",
            truncated=True,
            omitted_chars=2,
            original_chars=6,
        ),
    )
    with pytest.raises(AssertionInputError, match="裁剪元数据"):
        evaluate_assertion(
            definition("evidence_present", evidence_kind="tool_result", min_count=1),
            evidence(
                context_evidence=[invalid_metadata],
                tool_results=[truncated_tool],
            ),
        )

    with pytest.raises(AssertionInputError, match="缺少实际工具结果"):
        evaluate_assertion(
            definition("evidence_present", evidence_kind="tool_result", min_count=1),
            evidence(context_evidence=[truncated_context]),
        )


def test_evidence_present_ranking_requires_real_search_response():
    tool, context, ranking = ranking_fixture(has_more=True)
    ranking_pass = evaluate_assertion(
        definition("evidence_present", evidence_kind="ranking", min_count=2),
        evidence(
            context_evidence=[context],
            tool_results=[tool],
            rankings=[ranking],
        ),
    )
    assert ranking_pass.passed
    assert "actual=2" in ranking_pass.evidence

    ranking_fail = evaluate_assertion(
        definition("evidence_present", evidence_kind="ranking", min_count=3),
        evidence(
            context_evidence=[context],
            tool_results=[tool],
            rankings=[ranking],
        ),
    )
    assert not ranking_fail.passed
    assert "actual=2" in ranking_fail.evidence

    with pytest.raises(AssertionInputError, match="唯一工具结果"):
        evaluate_assertion(
            definition("evidence_present", evidence_kind="ranking", min_count=1),
            evidence(rankings=[ranking]),
        )

    crossed_tool = ToolResultEvidence(
        checkpoint=link(),
        tool_call_id="call-search",
        tool_name="search_exercises",
        content=json.dumps({"items": [], "has_more": False}),
    )
    with pytest.raises(AssertionInputError, match="响应不一致"):
        evaluate_assertion(
            definition("evidence_present", evidence_kind="ranking", min_count=1),
            evidence(
                context_evidence=[
                    ContextEvidence(
                        checkpoint=link(),
                        item=ContextItem(
                            source="tool_result",
                            content=crossed_tool.content,
                            fact_origin="tool",
                            tool_call_id="call-search",
                            tool_name="search_exercises",
                        ),
                    )
                ],
                tool_results=[crossed_tool],
                rankings=[ranking],
            ),
        )

    conflicting_context = ContextEvidence(
        checkpoint=link(),
        item=ContextItem(
            source="tool_result",
            content="other",
            fact_origin="tool",
            tool_call_id="call-search",
            tool_name="search_exercises",
        ),
    )
    with pytest.raises(AssertionInputError, match="正文与上下文不一致"):
        evaluate_assertion(
            definition("evidence_present", evidence_kind="ranking", min_count=1),
            evidence(
                context_evidence=[conflicting_context],
                tool_results=[tool],
                rankings=[ranking],
            ),
        )


def test_ranking_unknown_catalog_identity_is_rejected():
    search = search_evidence("q-unknown", ("9999",))
    content = json.dumps(search.response_json, ensure_ascii=False)
    tool = ToolResultEvidence(
        checkpoint=link(),
        tool_call_id="call-unknown",
        tool_name="search_exercises",
        content=content,
    )
    context = ContextEvidence(
        checkpoint=link(),
        item=ContextItem(
            source="tool_result",
            content=content,
            fact_origin="tool",
            tool_call_id="call-unknown",
            tool_name="search_exercises",
        ),
    )
    ranking = RankingEvidence(
        checkpoint=link(),
        tool_call_id="call-unknown",
        tool_name="search_exercises",
        search=search,
    )
    with pytest.raises(AssertionInputError, match="未知动作"):
        evaluate_assertion(
            definition("evidence_present", evidence_kind="ranking", min_count=1),
            evidence(
                context_evidence=[context],
                tool_results=[tool],
                rankings=[ranking],
            ),
        )


def test_ranking_rejects_more_than_five_items_per_search():
    with pytest.raises(ValidationError):
        search_evidence(
            "q-too-many",
            ("0001", "0002", "0003", "0004", "0005", "0006"),
        )
    catalog_ids = tuple(sorted(Catalog.load().exercise_ids)[:20])
    with pytest.raises(ValidationError):
        search_evidence("q-twenty", catalog_ids)


def test_ranking_rejects_duplicate_sources():
    first_tool, first_context, first = ranking_fixture(query_id="q-1")
    second_search = search_evidence("q-2", ("0003",))
    second = RankingEvidence(
        checkpoint=link(),
        tool_call_id="call-search",
        tool_name="search_exercises",
        search=second_search,
    )
    with pytest.raises(ValidationError, match="rankings.tool_call_id"):
        evidence(
            context_evidence=[first_context],
            tool_results=[first_tool],
            rankings=[first, second],
        )

    other_tool, other_context, other = ranking_fixture(
        query_id="q-1", call_id="call-other"
    )
    with pytest.raises(ValidationError, match="rankings.query_id"):
        evidence(
            context_evidence=[first_context, other_context],
            tool_results=[first_tool, other_tool],
            rankings=[first, other],
        )


def test_no_saved_state_distinguishes_missing_and_empty_profile():
    missing = evaluate_assertion(
        definition("no_saved_state", business_kind="profile"),
        evidence(after=projection()),
    )
    assert missing.passed

    empty_profile = profile(
        goal=None,
        experience=None,
        environment=None,
        availability=None,
        health_notes=None,
        movement_restrictions=None,
        unavailable_equipment=None,
        forbidden_exercise_ids=None,
    )
    empty_record = evaluate_assertion(
        definition("no_saved_state", business_kind="profile"),
        evidence(after=projection(profile_value=profile_record(content=empty_profile))),
    )
    assert not empty_record.passed
    assert "empty=True" in empty_record.evidence

    plan_present = evaluate_assertion(
        definition("no_saved_state", business_kind="plan"),
        evidence(after=projection(plans=[plan_record()])),
    )
    assert not plan_present.passed


def test_saved_state_reads_real_saved_records():
    profile_saved = evaluate_assertion(
        definition("saved_state", business_kind="profile"),
        evidence(after=projection(profile_value=profile_record())),
    )
    assert profile_saved.passed

    pending_only = evaluate_assertion(
        definition("saved_state", business_kind="profile"),
        evidence(after=projection(proposals=[pending_profile()])),
    )
    assert not pending_only.passed

    plan_saved = evaluate_assertion(
        definition("saved_state", business_kind="plan"),
        evidence(after=projection(plans=[plan_record()])),
    )
    assert plan_saved.passed

    workout_saved = evaluate_assertion(
        definition("saved_state", business_kind="workout"),
        evidence(after=projection(workouts=[workout_record()])),
    )
    assert workout_saved.passed


def test_checkpoint_keeps_duplicate_same_kind_assertions_in_order():
    outcome = evaluate_checkpoint(
        [
            definition("no_saved_state", business_kind="profile"),
            definition("no_saved_state", business_kind="plan"),
            definition("saved_state", business_kind="workout"),
        ],
        evidence(after=projection(workouts=[workout_record()])),
    )
    assert [item.kind for item in outcome] == [
        "no_saved_state",
        "no_saved_state",
        "saved_state",
    ]
    assert [item.passed for item in outcome] == [True, True, True]


def test_unknown_kind_field_and_invalid_input_are_rejected():
    with pytest.raises(ValidationError):
        evaluate_assertion(
            definition("unknown_kind"), evidence()
        )
    with pytest.raises(ValidationError):
        evaluate_assertion(
            definition("no_saved_state", business_kind="profile", unknown=True),
            evidence(),
        )
    with pytest.raises(ValidationError):
        evaluate_assertion(
            definition("no_saved_state", business_kind="invalid"), evidence()
        )
    with pytest.raises(AssertionInputError, match="event_id"):
        evaluate_assertion(
            definition("no_saved_state", business_kind="profile"),
            evidence(checkpoint=link(event_id="e99")),
        )
    with pytest.raises(AssertionInputError, match="身份不一致"):
        evaluate_assertion(
            definition("no_saved_state", business_kind="profile"),
            evidence(
                profile_snapshots=[
                    ProfileSnapshotEvidence(
                        proposal_ref=PROFILE_REF,
                        checkpoint=link(scenario_id="s-other-01"),
                        snapshot=profile_snapshot(),
                    )
                ]
            ),
        )


SYSTEM = SystemMessage(
    role="system", content="系统提示词", tools_added=[], timestamp=100
)
USAGE = Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2)


def now_ms() -> int:
    return time.time_ns() // 1_000_000


async def _append(service, session_id, run_id, parent_id, message):
    node_id = str(uuid4())
    await service.entries.append_entry(
        SessionMessageEntry(
            session_id=session_id,
            id=node_id,
            parent_id=parent_id,
            run_id=run_id,
            type="message",
            messages=[message],
            created_at=now_ms(),
        )
    )
    return node_id


async def _stage_profile(service, business, session_id, payload, base_version):
    outcome = await service.accept_send(
        SendCommand(
            operation_id=str(uuid4()),
            session_id=session_id,
            request=SendRequest(text="请整理待确认画像"),
        ),
        system_message=SYSTEM,
    )
    run = outcome.run
    request_id = run.request_entry_id
    call_id = "call-" + uuid4().hex[:8]
    source_id = await _append(
        service,
        session_id,
        run.id,
        request_id,
        AssistantMessage(
            role="assistant",
            content=[
                ToolCall(
                    type="toolCall",
                    id=call_id,
                    name="prepare_profile_update",
                    arguments={
                        "profile_id": 1,
                        "base_profile_version": base_version,
                        "payload": payload.model_dump(mode="json"),
                    },
                )
            ],
            api="openai-completions",
            provider="example",
            model="model-1",
            usage=USAGE,
            stop_reason="toolUse",
            timestamp=now_ms(),
        ),
    )
    context = BusinessContext(
        timezone="Asia/Shanghai",
        business_date=business_date(now_ms()),
        session_id=session_id,
        run_id=run.id,
        request_entry_id=request_id,
        source_entry_id=source_id,
    )
    proposal = await business.profile.prepare_profile_update(
        context,
        ProfileProposalArguments(
            profile_id=1,
            base_profile_version=base_version,
            payload=payload,
        ),
    )
    display_id = await _append(
        service,
        session_id,
        run.id,
        source_id,
        ToolResultMessage(
            role="toolResult",
            tool_call_id=call_id,
            tool_name="prepare_profile_update",
            content=[TextContent(type="text", text=proposal.model_dump_json())],
            is_error=False,
            timestamp=now_ms(),
        ),
    )
    await business.profile.bind_display_entry(proposal.proposal_id, display_id)
    await service.runs.finish_run(session_id, run.id, "completed")
    return {
        "session_id": session_id,
        "request_id": request_id,
        "source_id": source_id,
        "display_id": display_id,
        "call_id": call_id,
        "proposal": proposal,
    }


async def _read_projection(repository, session_id):
    async with repository.transaction():
        profile_value = await repository.get_profile()
        plans = await repository.list_plans()
        workouts = (
            await repository.list_workouts(WorkoutListArguments(page_size=100))
        ).items
        save_records = [
            *await repository.list_save_records(session_id),
            *await repository.list_plan_save_records(session_id),
            *await repository.list_workout_save_records(session_id),
        ]
        operations = await repository.list_confirmation_operations(session_id)
        proposals = []
        cancellations = []
        for snapshot in await repository.list_snapshots(session_id):
            if snapshot.display_entry_id is None:
                continue
            proposals.append(
                ProfilePendingProposal(
                    business_kind="profile",
                    proposal_id=snapshot.proposal_id,
                    status=snapshot.status,
                    payload=snapshot.payload,
                    request_entry_id=snapshot.request_entry_id,
                    source_entry_id=snapshot.source_entry_id,
                    display_entry_id=snapshot.display_entry_id,
                    confirmation_entry_id=snapshot.confirmation_entry_id,
                    profile_id=snapshot.profile_id,
                    base_profile_version=snapshot.base_profile_version,
                )
            )
            if snapshot.status == "invalidated":
                cancellations.append(
                    PendingProposalCancellation(
                        business_kind="profile",
                        proposal_id=snapshot.proposal_id,
                    )
                )
        return BusinessProjection(
            profile=profile_value,
            plans=plans,
            workouts=workouts,
            save_records=save_records,
            proposals=proposals,
            confirmation_operations=operations,
            cancellations=cancellations,
        )


async def test_real_sqlite_confirmation_chain_feeds_assertions():
    root = temporary_root("deterministic-assertions")
    database_path = root / f"{uuid4().hex}.db"
    database = await open_database(database_path)
    try:
        repository = SqliteBusinessRepository(database)
        catalog = Catalog.load()
        replacements = ReplacementCoordinator()
        service = SessionService(SqliteSessionRepository(database))
        business = BusinessService(
            repository, catalog, service.queries, replacements
        )
        service.attach_snapshots(business.proposals)
        confirmations = ConfirmationService(
            repository,
            service.queries,
            service.operations,
            service.entries,
            business,
        )
        session_id = str(uuid4())
        created, _ = await service.create_session_result(session_id, "断言链路")
        assert created

        staged = await _stage_profile(
            service, business, session_id, profile(), None
        )
        proposal = staged["proposal"]
        proposal_ref = f"profile-proposal-{staged['request_id']}"
        before = await _read_projection(repository, session_id)
        after_message = await _read_projection(repository, session_id)
        async with repository.transaction():
            snapshot = await repository.get_snapshot(proposal.proposal_id)

        message_link = link(
            event_id="e01", checkpoint_id="cp1", scenario_id="s-real-01"
        )
        message_binding = binding(
            proposal_ref=proposal_ref,
            proposal_id=proposal.proposal_id,
            display=staged["display_id"],
        )
        message_display = DisplayContentEvidence(
            proposal_ref=proposal_ref,
            business_kind="profile",
            checkpoint=message_link,
            display_entry_id=staged["display_id"],
            tool_call_id=staged["call_id"],
            tool_name="prepare_profile_update",
            content=proposal.model_dump(mode="json"),
        )
        message_evidence = AssertionEvidence(
            checkpoint=message_link,
            event_type="message",
            before=before,
            after=after_message,
            proposal_bindings=[message_binding],
            profile_snapshots=[
                ProfileSnapshotEvidence(
                    proposal_ref=proposal_ref,
                    checkpoint=message_link,
                    snapshot=snapshot,
                )
            ],
            display_contents=[message_display],
            business_contents=[
                BusinessContentEvidence(
                    business_kind="profile",
                    source="proposal",
                    checkpoint=message_link,
                    proposal_ref=proposal_ref,
                    payload=proposal.payload.model_dump(mode="json"),
                )
            ],
            parameter_target=BusinessContentEvidence(
                business_kind="profile",
                source="proposal",
                checkpoint=message_link,
                proposal_ref=proposal_ref,
                payload=proposal.payload.model_dump(mode="json"),
            ),
        )
        message_outcomes = evaluate_checkpoint(
            [
                {
                    "checkpoint_id": "cp1",
                    "event_id": "e01",
                    "kind": "snapshot_integrity",
                    "proposal_ref": proposal_ref,
                },
                {
                    "checkpoint_id": "cp1",
                    "event_id": "e01",
                    "kind": "save_authorization",
                    "proposal_ref": proposal_ref,
                    "expected_saved": False,
                },
                {
                    "checkpoint_id": "cp1",
                    "event_id": "e01",
                    "kind": "no_saved_state",
                    "business_kind": "profile",
                },
                {
                    "checkpoint_id": "cp1",
                    "event_id": "e01",
                    "kind": "parameter_preserved",
                    "field_path": "/goal",
                    "expected": "增肌",
                },
            ],
            message_evidence,
        )
        assert all(item.passed for item in message_outcomes), [
            item.evidence for item in message_outcomes
        ]

        operation = await confirmations.confirm(
            session_id,
            SaveConfirmationRequest(
                operation_id=str(uuid4()),
                business_kind="profile",
                proposal_id=proposal.proposal_id,
                display_entry_id=staged["display_id"],
            ),
        )
        assert operation.status == "saved"
        after_confirm = await _read_projection(repository, session_id)
        async with repository.transaction():
            saved_snapshot = await repository.get_snapshot(proposal.proposal_id)

        confirm_link = link(
            event_id="e03", checkpoint_id="cp2", scenario_id="s-real-01"
        )
        confirm_binding = binding(
            proposal_ref=proposal_ref,
            proposal_id=proposal.proposal_id,
            display=staged["display_id"],
            confirmation=operation.confirmation_entry_id,
            operation=operation.operation_id,
        )
        confirm_display = message_display.model_copy(
            update={"checkpoint": confirm_link}
        )
        confirm_evidence = AssertionEvidence(
            checkpoint=confirm_link,
            event_type="confirm",
            event_target=EventTarget(
                business_kind="profile",
                proposal_ref=proposal_ref,
                display_ref=f"profile-display-{staged['request_id']}",
                confirmation_ref=f"profile-confirm-{staged['request_id']}",
            ),
            before=after_message,
            after=after_confirm,
            proposal_bindings=[confirm_binding],
            profile_snapshots=[
                ProfileSnapshotEvidence(
                    proposal_ref=proposal_ref,
                    checkpoint=confirm_link,
                    snapshot=saved_snapshot,
                )
            ],
            display_contents=[confirm_display],
        )
        confirm_outcomes = evaluate_checkpoint(
            [
                {
                    "checkpoint_id": "cp2",
                    "event_id": "e03",
                    "kind": "snapshot_integrity",
                    "proposal_ref": proposal_ref,
                },
                {
                    "checkpoint_id": "cp2",
                    "event_id": "e03",
                    "kind": "save_authorization",
                    "proposal_ref": proposal_ref,
                    "expected_saved": True,
                },
                {
                    "checkpoint_id": "cp2",
                    "event_id": "e03",
                    "kind": "state_transition",
                    "business_kind": "profile",
                    "from_status": "pending",
                    "to_status": "saved",
                },
                {
                    "checkpoint_id": "cp2",
                    "event_id": "e03",
                    "kind": "saved_state",
                    "business_kind": "profile",
                },
            ],
            confirm_evidence,
        )
        assert all(item.passed for item in confirm_outcomes), [
            item.evidence for item in confirm_outcomes
        ]

        second = await _stage_profile(
            service,
            business,
            session_id,
            profile(goal="减脂"),
            1,
        )
        second_ref = f"profile-proposal-{second['request_id']}"
        after_second = await _read_projection(repository, session_id)
        second_link = link(
            event_id="e05", checkpoint_id="cp4", scenario_id="s-real-01"
        )
        second_binding = binding(
            proposal_ref=second_ref,
            proposal_id=second["proposal"].proposal_id,
            display=second["display_id"],
        )
        second_evidence = AssertionEvidence(
            checkpoint=second_link,
            event_type="message",
            before=after_confirm,
            after=after_second,
            proposal_bindings=[second_binding],
        )
        second_outcome = evaluate_assertion(
            {
                "checkpoint_id": "cp4",
                "event_id": "e05",
                "kind": "save_authorization",
                "proposal_ref": second_ref,
                "expected_saved": False,
            },
            second_evidence,
        )
        assert second_outcome.passed
        assert "其他保存记录=1" in second_outcome.evidence
    finally:
        await database.close()
        database_path.unlink(missing_ok=True)
