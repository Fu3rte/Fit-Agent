import pytest
from pydantic import TypeAdapter, ValidationError

from app.domain.business.models import (
    PendingProposalCancellation,
    PlanContent,
    PlanDay,
    PlanRecord,
    ProfileContent,
    ProfilePendingProposal,
    ProfileRecord,
    ProfileSaveRecord,
    ProfileSaveResult,
    SaveConfirmationOperation,
    WorkoutContent,
    WorkoutExercise,
    WorkoutRecord,
    WorkoutSet,
)
from evaluation.contracts import (
    RETRIEVAL_METRIC_STAGES,
    RETRIEVAL_METRICS,
    AnswerEvidence,
    AssertionOutcome,
    BusinessProjection,
    CheckpointResult,
    ContextItem,
    EvidenceLink,
    GenerationCheckpoint,
    GenerationReference,
    MetricApplicability,
    MetricOutcome,
    ModelInputContext,
    ModelVisibleSearch,
    PriorMessage,
    ProjectionComparison,
    ProposalBinding,
    ReferenceFact,
    RelevanceLabel,
    RetrievalInput,
    RetrievalMetric,
    RetrievalResult,
    SavedBusinessData,
    SavedDataComparison,
    ScenarioResult,
    SearchResponseEvidence,
    StateComparison,
    UserInputEvidence,
    VisibleExercise,
)

PROFILE_PROPOSAL = "11111111-1111-4111-8111-111111111111"
PLAN_ID = "22222222-2222-4222-8222-222222222222"
WORKOUT_ID = "33333333-3333-4333-8333-333333333333"
SESSION_ID = "44444444-4444-4444-8444-444444444444"
ENTRY_ID = "55555555-5555-4555-8555-555555555555"
OPERATION_ID = "66666666-6666-4666-8666-666666666666"
CONFIRMATION_ID = "77777777-7777-4777-8777-777777777777"


def profile_content(**overrides):
    values = {
        "goal": None,
        "experience": None,
        "environment": None,
        "availability": None,
        "health_notes": None,
        "movement_restrictions": None,
        "unavailable_equipment": None,
        "forbidden_exercise_ids": None,
    }
    values.update(overrides)
    return ProfileContent(**values)


def profile_record():
    return ProfileRecord(version=1, content=profile_content(), updated_at=1)


def plan_record():
    content = PlanContent(
        repeat=True,
        days=[PlanDay(kind="rest", focus=None, exercises=[], notes=None)],
        notes=None,
        suggested_fields=[],
    )
    return PlanRecord(id=PLAN_ID, is_current=True, created_at=1, content=content)


def workout_record():
    content = WorkoutContent(
        exercises=[
            WorkoutExercise(
                exercise_id="0001",
                name="深蹲",
                load_convention=None,
                sets=[WorkoutSet(reps=5, weight_kg=None, duration_seconds=None)],
            )
        ],
        notes=None,
    )
    return WorkoutRecord(
        performed_on="2026-01-01",
        id=WORKOUT_ID,
        version=1,
        content=content,
        created_at=1,
        updated_at=1,
    )


def confirmation_operation():
    return SaveConfirmationOperation(
        session_id=SESSION_ID,
        operation_id=OPERATION_ID,
        business_kind="profile",
        proposal_id=PROFILE_PROPOSAL,
        display_entry_id=ENTRY_ID,
        confirmation_entry_id=CONFIRMATION_ID,
        status="processing",
        result=None,
        error=None,
        created_at=1,
        updated_at=1,
    )


def pending_profile_proposal():
    return ProfilePendingProposal(
        business_kind="profile",
        proposal_id=PROFILE_PROPOSAL,
        status="pending",
        payload=profile_content(),
        request_entry_id=ENTRY_ID,
        source_entry_id=ENTRY_ID,
        display_entry_id=ENTRY_ID,
        confirmation_entry_id=None,
        profile_id=1,
        base_profile_version=None,
    )


def profile_save_record():
    result = ProfileSaveResult(
        proposal_id=PROFILE_PROPOSAL,
        profile_id=1,
        version=1,
        content=profile_content(),
        saved_at=1,
    )
    return ProfileSaveRecord(
        proposal_id=PROFILE_PROPOSAL,
        session_id=SESSION_ID,
        profile_id=1,
        display_entry_id=ENTRY_ID,
        confirmation_entry_id=CONFIRMATION_ID,
        result=result,
        saved_at=1,
    )


def evidence_link(checkpoint_id="cp-1", scenario_id="s-lookup-01"):
    return EvidenceLink(
        run_id="run-1",
        scenario_id=scenario_id,
        event_id="msg-1",
        checkpoint_id=checkpoint_id,
    )


def retrieval_metrics(value=0.5):
    return [
        RetrievalMetric(
            name=name,
            stage=RETRIEVAL_METRIC_STAGES[name][0],
            k=RETRIEVAL_METRIC_STAGES[name][1],
            value=value,
        )
        for name in RETRIEVAL_METRICS
    ]


def staged_metrics(recall=0.0, at_five=0.0):
    return [
        RetrievalMetric(
            name=name,
            stage=RETRIEVAL_METRIC_STAGES[name][0],
            k=RETRIEVAL_METRIC_STAGES[name][1],
            value=recall if name == "recall@20" else at_five,
        )
        for name in RETRIEVAL_METRICS
    ]


def test_candidate_pool_and_final_input_keep_order_and_leading_zero():
    pool = [f"{index:04d}" for index in range(1, 21)]
    final = [pool[2], pool[0], pool[4], pool[1], pool[3]]
    item = RetrievalInput(
        query_id="q-lookup-001",
        scenario_id="s-lookup-01",
        expect_empty=False,
        labels=[RelevanceLabel(exercise_id="0001", grade=2)],
        candidate_pool=pool,
        final_model_input=final,
    )
    assert len(item.candidate_pool) == 20
    assert len(item.final_model_input) == 5
    assert item.candidate_pool[0] == "0001"
    assert item.final_model_input == final


def test_candidate_pool_rejects_duplicates_and_over_limit():
    pool = [f"{index:04d}" for index in range(1, 21)]
    with pytest.raises(ValidationError):
        RetrievalInput(
            query_id="q-lookup-001",
            expect_empty=True,
            candidate_pool=["0001", "0001"],
        )
    with pytest.raises(ValidationError):
        RetrievalInput(
            query_id="q-lookup-001",
            expect_empty=True,
            candidate_pool=[f"{index:04d}" for index in range(1, 22)],
        )
    with pytest.raises(ValidationError):
        RetrievalInput(
            query_id="q-lookup-001",
            expect_empty=True,
            candidate_pool=pool,
            final_model_input=pool[:6],
        )
    with pytest.raises(ValidationError):
        RetrievalInput(
            query_id="q-lookup-001",
            expect_empty=True,
            candidate_pool=["not-an-id"],
        )


def test_final_input_must_come_from_candidate_pool():
    with pytest.raises(ValidationError):
        RetrievalInput(
            query_id="q-lookup-001",
            expect_empty=False,
            labels=[RelevanceLabel(exercise_id="0001", grade=2)],
            candidate_pool=["0001"],
            final_model_input=["0002"],
        )
    with pytest.raises(ValidationError):
        RetrievalInput(
            query_id="q-lookup-001",
            expect_empty=True,
            candidate_pool=["0001"],
            final_model_input=["0002"],
        )


def test_final_input_allows_reordering_within_pool():
    item = RetrievalInput(
        query_id="q-lookup-001",
        expect_empty=True,
        candidate_pool=["0003", "0001", "0002"],
        final_model_input=["0002", "0003"],
    )
    assert item.final_model_input == ["0002", "0003"]


def test_reviewed_negative_is_distinct_from_positive_label():
    negative = RetrievalInput(
        query_id="q-lookup-002",
        expect_empty=True,
        labels=[RelevanceLabel(exercise_id="0001", grade=0)],
    )
    assert negative.positive_labels == []
    with pytest.raises(ValidationError):
        RetrievalInput(
            query_id="q-lookup-002",
            expect_empty=True,
            labels=[RelevanceLabel(exercise_id="0001", grade=1)],
        )


def test_no_answer_result_records_false_return_at_final_stage():
    empty = RetrievalResult(
        query_id="q-lookup-002",
        expect_empty=True,
        candidate_pool=[],
        final_model_input=[],
        false_return=False,
    )
    returned = RetrievalResult(
        query_id="q-lookup-002",
        expect_empty=True,
        candidate_pool=["0001", "0002"],
        final_model_input=["0002"],
        false_return=True,
    )
    assert empty.metrics == []
    assert returned.false_return is True
    with pytest.raises(ValidationError):
        RetrievalResult(
            query_id="q-lookup-002",
            expect_empty=True,
            candidate_pool=["0001"],
            final_model_input=["0001"],
            false_return=False,
        )


def test_no_answer_nonempty_pool_but_empty_final_is_not_false_return():
    result = RetrievalResult(
        query_id="q-lookup-002",
        expect_empty=True,
        candidate_pool=["0001", "0002"],
        final_model_input=[],
        false_return=False,
    )
    assert result.false_return is False
    assert result.metrics == []


def test_labelled_result_keeps_stage_independent_outcome():
    result = RetrievalResult(
        query_id="q-lookup-003",
        expect_empty=False,
        candidate_pool=["0001"],
        final_model_input=[],
        metrics=staged_metrics(recall=1.0, at_five=0.0),
    )
    values = {metric.name: metric.value for metric in result.metrics}
    assert {metric.name for metric in result.metrics} == set(RETRIEVAL_METRICS)
    assert values["recall@20"] == 1.0
    assert all(values[name] == 0.0 for name in RETRIEVAL_METRICS if name != "recall@20")
    with pytest.raises(ValidationError):
        RetrievalResult(
            query_id="q-lookup-003",
            expect_empty=False,
            candidate_pool=["0001"],
            final_model_input=[],
            metrics=staged_metrics(recall=1.0, at_five=0.5),
        )
    with pytest.raises(ValidationError):
        RetrievalResult(
            query_id="q-lookup-003",
            expect_empty=False,
            candidate_pool=[],
            final_model_input=[],
            metrics=staged_metrics(recall=0.5, at_five=0.0),
        )
    with pytest.raises(ValidationError):
        RetrievalResult(
            query_id="q-lookup-003",
            expect_empty=False,
            candidate_pool=["0001"],
            final_model_input=["0001"],
            metrics=retrieval_metrics(0.5)[:4],
        )


def visible_exercise(exercise_id="0001", name="卧推"):
    return VisibleExercise(
        exercise_id=exercise_id,
        name=name,
        equipment="barbell",
        target="chest",
        matches=["equipment=barbell"],
    )


def search_evidence(items=None, has_more=False):
    items = [visible_exercise()] if items is None else items
    response = {
        "items": [
            {
                "id": item.exercise_id,
                "name": item.name,
                "equipment": item.equipment,
                "target": item.target,
                "matches": item.matches,
            }
            for item in items
        ],
        "has_more": has_more,
    }
    return SearchResponseEvidence(
        query_id="q-lookup-001",
        response_json=response,
        model_visible=ModelVisibleSearch(items=items, has_more=has_more),
    )


def test_model_visible_search_limits_five_and_rejects_duplicates():
    items = [visible_exercise(f"{index:04d}") for index in range(1, 6)]
    assert len(ModelVisibleSearch(items=items, has_more=False).items) == 5
    with pytest.raises(ValidationError):
        ModelVisibleSearch(
            items=[visible_exercise(f"{index:04d}") for index in range(1, 7)],
            has_more=False,
        )
    with pytest.raises(ValidationError):
        ModelVisibleSearch(
            items=[visible_exercise("0001"), visible_exercise("0001")],
            has_more=False,
        )


def test_search_response_evidence_requires_exact_correspondence():
    evidence = search_evidence(has_more=True)
    assert evidence.model_visible.items[0].exercise_id == "0001"
    evidence.require_scored_consistency(["0001"])
    with pytest.raises(ValueError, match="评分列表"):
        evidence.require_scored_consistency([])
    with pytest.raises(ValidationError):
        SearchResponseEvidence(
            query_id="q-lookup-001",
            response_json={"items": [], "has_more": False, "remaining_ids": ["0002"]},
            model_visible=ModelVisibleSearch(items=[], has_more=False),
        )
    with pytest.raises(ValidationError):
        SearchResponseEvidence(
            query_id="q-lookup-001",
            response_json={
                "items": [
                    {
                        "id": "0001",
                        "name": "卧推",
                        "equipment": "barbell",
                        "target": "chest",
                        "matches": ["equipment=barbell"],
                    }
                ],
                "has_more": True,
            },
            model_visible=ModelVisibleSearch(items=[visible_exercise()], has_more=False),
        )


def test_missing_record_and_empty_content_are_distinct():
    missing = SavedBusinessData()
    empty = SavedBusinessData(profile=profile_record())
    assert missing.profile is None
    assert empty.profile is not None
    assert empty.profile.content.is_empty()
    assert missing.model_dump() != empty.model_dump()


def test_saved_data_and_full_projection_scopes_are_distinct():
    link = evidence_link()
    saved = SavedDataComparison(
        kind="saved_data_unchanged",
        business_kind="all",
        checkpoint=link,
        before=SavedBusinessData(),
        after=SavedBusinessData(profile=profile_record(), plans=[plan_record()]),
    )
    projection = BusinessProjection(
        profile=profile_record(),
        plans=[plan_record()],
        workouts=[workout_record()],
        proposals=[pending_profile_proposal()],
        confirmation_operations=[confirmation_operation()],
        cancellations=[
            PendingProposalCancellation(business_kind="profile", proposal_id=PROFILE_PROPOSAL)
        ],
    )
    state = StateComparison(
        kind="state_unchanged",
        business_kind="profile",
        checkpoint=link,
        before=BusinessProjection(),
        after=projection,
    )
    assert saved.before.profile is None
    assert saved.after.plans[0].is_current is True
    assert state.after.proposals[0].proposal_id == PROFILE_PROPOSAL
    assert state.after.confirmation_operations[0].status == "processing"
    assert state.after.cancellations[0].status == "invalidated"
    outcome = AssertionOutcome(
        checkpoint=link,
        kind="state_unchanged",
        passed=True,
        evidence="完整业务投影一致",
        comparison=state,
    )
    assert outcome.comparison == state
    with pytest.raises(ValidationError):
        AssertionOutcome(
            checkpoint=link,
            kind="state_unchanged",
            passed=True,
            evidence="缺少比较证据",
        )
    with pytest.raises(ValidationError):
        SavedDataComparison(
            kind="saved_data_unchanged",
            business_kind="profile",
            checkpoint=link,
            before=SavedBusinessData(),
            after=SavedBusinessData(),
            projection=projection,
        )


def test_projection_comparison_round_trips_through_union():
    adapter = TypeAdapter(ProjectionComparison)
    state = StateComparison(
        kind="state_unchanged",
        business_kind="all",
        checkpoint=evidence_link(),
        before=BusinessProjection(),
        after=BusinessProjection(save_records=[profile_save_record()]),
    )
    restored = adapter.validate_json(state.model_dump_json())
    assert restored == state


def test_checkpoint_context_keeps_source_and_tool_binding():
    checkpoint = GenerationCheckpoint(
        checkpoint=evidence_link(),
        user_input=UserInputEvidence(
            message="给我一个训练计划",
            prior_messages=[PriorMessage(role="user", content="我想增肌")],
        ),
        answer=AnswerEvidence(
            visible_text="建议安排推拉腿循环。",
            structured_snapshot={"kind": "plan"},
        ),
        model_input=ModelInputContext(
            items=[
                ContextItem(
                    source="system_prompt", content="系统提示", fact_origin="system"
                ),
                ContextItem(
                    source="tool_result",
                    content="动作目录结果",
                    fact_origin="tool",
                    tool_call_id="call-1",
                    tool_name="search_exercises",
                ),
            ]
        ),
        reference=GenerationReference(
            answer="参考回答",
            facts=[ReferenceFact(fact_id="f1", text="参考事实")],
        ),
        applicability=[
            MetricApplicability(metric="faithfulness", applicable=True),
            MetricApplicability(metric="factual_correctness", applicable=True),
        ],
    )
    ragas = checkpoint.ragas_input()
    assert ragas.answer == checkpoint.answer.visible_text
    assert ragas.contexts == [item.content for item in checkpoint.model_input.items]
    assert ragas.reference == checkpoint.reference.answer
    assert checkpoint.reference.answer not in ragas.contexts
    with pytest.raises(ValidationError):
        ContextItem(source="tool_result", content="缺少绑定", fact_origin="tool")
    with pytest.raises(ValidationError):
        ContextItem(
            source="tool_result",
            content="被裁剪",
            fact_origin="tool",
            tool_call_id="call-1",
            tool_name="search_exercises",
            truncated=True,
        )


def test_generation_applicability_and_reference_are_consistent():
    user_input = UserInputEvidence(message="确认保存")
    answer = AnswerEvidence(visible_text="已保存。")
    model_input = ModelInputContext(
        items=[ContextItem(source="user_message", content="确认保存", fact_origin="user")]
    )
    with pytest.raises(ValidationError):
        GenerationCheckpoint(
            checkpoint=evidence_link(),
            user_input=user_input,
            answer=answer,
            model_input=model_input,
            applicability=[
                MetricApplicability(
                    metric="faithfulness", applicable=False, reason="纯确认"
                ),
                MetricApplicability(
                    metric="factual_correctness", applicable=False, reason="纯确认"
                ),
            ],
            reference=GenerationReference(answer="参考回答"),
        )
    with pytest.raises(ValidationError):
        GenerationCheckpoint(
            checkpoint=evidence_link(),
            user_input=user_input,
            answer=answer,
            model_input=model_input,
            applicability=[
                MetricApplicability(metric="faithfulness", applicable=True),
                MetricApplicability(
                    metric="factual_correctness", applicable=False, reason="无参考"
                ),
            ],
        )


def test_metric_outcome_status_score_reason_and_error_are_consistent():
    succeeded = MetricOutcome(metric="faithfulness", status="succeeded", score=0.0)
    failed = MetricOutcome(
        metric="faithfulness", status="failed", error="judge call failed"
    )
    not_applicable = MetricOutcome(
        metric="faithfulness", status="not_applicable", reason="纯确认"
    )
    applicable = MetricOutcome(metric="faithfulness", status="applicable")
    assert succeeded.score == 0.0
    assert failed.score is None
    assert not_applicable.reason == "纯确认"
    assert applicable.score is None
    with pytest.raises(ValidationError):
        MetricOutcome(metric="faithfulness", status="succeeded")
    with pytest.raises(ValidationError):
        MetricOutcome(metric="faithfulness", status="failed", score=0.0)
    with pytest.raises(ValidationError):
        MetricOutcome(metric="faithfulness", status="failed", error="x", score=0.0)
    with pytest.raises(ValidationError):
        MetricOutcome(
            metric="faithfulness", status="not_applicable", reason="纯确认", score=0.0
        )
    with pytest.raises(ValidationError):
        MetricOutcome(metric="faithfulness", status="not_applicable")
    with pytest.raises(ValidationError):
        MetricOutcome(metric="faithfulness", status="applicable", score=0.1)


def test_unknown_fields_and_non_finite_numbers_are_rejected():
    with pytest.raises(ValidationError):
        RetrievalInput(query_id="q-lookup-001", expect_empty=True, unknown=True)
    with pytest.raises(ValidationError):
        RetrievalInput(query_id="   ", expect_empty=True)
    with pytest.raises(ValidationError):
        MetricOutcome(metric="faithfulness", status="failed", error="   ")
    with pytest.raises(ValidationError):
        RetrievalMetric(
            name="hit@5", stage="final_model_input", k=5, value=float("nan")
        )
    with pytest.raises(ValidationError):
        RetrievalMetric(
            name="hit@5", stage="final_model_input", k=5, value=float("inf")
        )
    with pytest.raises(ValidationError):
        MetricOutcome(metric="faithfulness", status="succeeded", score=float("nan"))


def test_proposal_binding_and_assertion_kinds_are_closed():
    binding = ProposalBinding(
        proposal_ref=f"profile-proposal-{ENTRY_ID}",
        business_kind="profile",
        proposal_id=PROFILE_PROPOSAL,
        display_entry_id=ENTRY_ID,
        confirmation_entry_id=CONFIRMATION_ID,
        confirmation_operation_id=OPERATION_ID,
    )
    outcome = AssertionOutcome(
        checkpoint=evidence_link(),
        kind="save_authorization",
        passed=True,
        evidence="确认入口保存成功",
        proposal=binding,
    )
    assert outcome.proposal.confirmation_entry_id == CONFIRMATION_ID
    with pytest.raises(ValidationError):
        ProposalBinding(
            proposal_ref=f"plan-proposal-{ENTRY_ID}",
            business_kind="profile",
            proposal_id=PROFILE_PROPOSAL,
        )
    with pytest.raises(ValidationError):
        AssertionOutcome(
            checkpoint=evidence_link(),
            kind="save_authorization",
            passed=True,
            evidence="缺少提案绑定",
        )
    with pytest.raises(ValidationError):
        AssertionOutcome(
            checkpoint=evidence_link(),
            kind="equipment_restriction",
            passed=True,
            evidence="不接受提案绑定",
            proposal=binding,
        )


def test_checkpoint_and_scenario_results_keep_identity():
    checkpoint = CheckpointResult(
        checkpoint=evidence_link("cp-1", "s-lookup-01"),
        outcomes=[MetricOutcome(metric="deterministic_rules", status="succeeded", score=1.0)],
    )
    scenario = ScenarioResult(scenario_id="s-lookup-01", checkpoints=[checkpoint])
    assert scenario.checkpoints[0].checkpoint.scenario_id == scenario.scenario_id
    with pytest.raises(ValidationError):
        ScenarioResult(
            scenario_id="s-other-01",
            checkpoints=[checkpoint],
        )


def test_contracts_round_trip_through_json():
    item = RetrievalInput(
        query_id="q-lookup-001",
        expect_empty=False,
        labels=[RelevanceLabel(exercise_id="0001", grade=3)],
        candidate_pool=["0003", "0001", "0002"],
        final_model_input=["0002", "0003"],
    )
    assert RetrievalInput.model_validate_json(item.model_dump_json()) == item

    checkpoint = GenerationCheckpoint(
        checkpoint=evidence_link(),
        user_input=UserInputEvidence(message="问题"),
        answer=AnswerEvidence(visible_text="回答"),
        model_input=ModelInputContext(
            items=[ContextItem(source="user_message", content="问题", fact_origin="user")]
        ),
        reference=GenerationReference(answer="参考"),
        applicability=[
            MetricApplicability(metric="faithfulness", applicable=True),
            MetricApplicability(metric="factual_correctness", applicable=True),
        ],
    )
    assert (
        GenerationCheckpoint.model_validate_json(checkpoint.model_dump_json())
        == checkpoint
    )

    result = RetrievalResult(
        query_id="q-lookup-004",
        expect_empty=False,
        candidate_pool=["0001"],
        final_model_input=["0001"],
        metrics=retrieval_metrics(0.5),
    )
    assert RetrievalResult.model_validate_json(result.model_dump_json()) == result
