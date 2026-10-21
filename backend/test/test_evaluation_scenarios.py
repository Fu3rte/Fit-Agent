import json
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from app.agent.compaction_summary import (
    COMPACTION_SUMMARY_PREFIX,
    COMPACTION_SUMMARY_SUFFIX,
)
from app.ai.messages import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from app.application.business.retrieval_conditions import EvaluatedConditions
from app.application.business.retrieval_response import (
    SearchObservation,
    build_search_stage,
)
from app.application.business.retrieval_search import (
    RetrievalCandidate,
    SearchConditions,
)
from app.application.session.positions import set_leaf
from app.domain.business.errors import ProfileProposalInvalidated
from app.domain.business.models import (
    BusinessContext,
    CatalogSearchArguments,
    PendingProposalArguments,
    PendingProposalCancellation,
    PlanContent,
    PlanRecord,
    ProfileContent,
    ProfileProposal,
    ProfileSnapshot,
    WorkoutContent,
    WorkoutExercise,
    WorkoutRecord,
    WorkoutSet,
)
from app.domain.session.attachments import (
    AttachmentMetadata,
    attachment_storage_ref,
)
from app.domain.session.models import (
    CompactionEntry,
    SessionMessageEntry,
    SessionRun,
)
from evaluation.contracts import (
    BusinessProjection,
    CheckpointResult,
    ContextItem,
    EvidenceLink,
    MessageNode,
    MetricOutcome,
    ProfilePendingProposal,
    ProposalBinding,
    ToolNode,
)
from evaluation.dataset import BACKEND, load_for_evaluation
from evaluation.deterministic_assertions import (
    AssertionEvidence,
    AssertionInputError,
    CancellationEvidence,
    ContextEvidence,
    EventIdentity,
    EventTarget,
    ToolResultEvidence,
    evaluate_checkpoint,
    load_catalog,
)
from evaluation.generation_scoring import (
    JudgeConfig,
    load_judge_model,
    run_judge_identity,
)
from evaluation.scenario_evidence import (
    CancelEvidence,
    ContextMappingError,
    EvaluationCallRecorder,
    build_proposal_bindings,
    canonical_json,
    context_items_from_request,
    current_input_entry_ids,
    model_identity_summary,
    normalize_tool_id,
    read_business_projection,
    read_business_projection_with_cancellations,
    read_cancel_evidence,
    read_request_nodes,
    sanitize_error_text,
    search_evidence_from_tool,
    sha256_text,
    tool_nodes_by_visible,
)
from evaluation.scenarios import (
    DEFAULT_JUDGE_TIMEOUT,
    SCENARIO_ROOT,
    AssertionCheckpointArtifact,
    EventCapture,
    GenerationCheckpointArtifact,
    RunAggregate,
    RuntimeHandle,
    ScenarioArtifact,
    ScenarioBlocked,
    SearchStageRecord,
    ToolCallCapture,
    _assertion_evidence,
    _attachments_for_event,
    _cancel_driving_capture,
    _current_input_text,
    _incomplete_items,
    _judge_config,
    _not_applicable_generation,
    _parse_args,
    _partial_artifact,
    _seed_initial_state,
    _seed_prior_messages,
    _verify_initial_projection,
    _write_json,
    accumulate_artifact,
    accumulate_partial,
    build_coverage,
    build_execution_plan,
    executed_index_identity,
    judge_attempt_identity,
    run_selected,
    scenario_plan,
    select_scenarios,
)

MODEL = type("EvaluationModel", (), {
    "api": "openai-completions",
    "provider": "evaluation",
    "id": "evaluation-model",
    "model": "evaluation-model",
    "api_key": "not-a-secret",
})()


def test_judge_timeout_default_and_override():
    assert _parse_args([]).judge_timeout == 300.0
    assert _parse_args(["--judge-timeout", "600"]).judge_timeout == 600.0


@pytest.fixture(scope="module")
def dataset():
    return load_for_evaluation(BACKEND / "dataset" / "evaluation" / "rebuild-v2")


@pytest.fixture
def root():
    path = SCENARIO_ROOT / "tests" / uuid4().hex
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@asynccontextmanager
async def open_runtime(root: Path):
    runtime = RuntimeHandle(root=root)
    await runtime.open()
    try:
        yield runtime
    finally:
        await runtime.close()


def test_frozen_dataset_plan_has_full_coverage(dataset):
    scenarios = select_scenarios(dataset, None)
    plan = build_execution_plan(dataset, scenarios, "run")
    assert plan.expected_scenarios == 80
    assert plan.expected_events == 134
    assert plan.expected_checkpoints == 127
    assert {item.scenario_id for item in plan.scenarios} == {
        scenario["scenario_id"] for scenario in dataset.scenarios
    }


def test_selection_rejects_unknown_and_duplicate_ids(dataset):
    with pytest.raises(Exception, match="未知 scenario_id"):
        select_scenarios(dataset, ["s-unknown-01"])
    with pytest.raises(Exception, match="不得重复"):
        select_scenarios(
            dataset,
            ["s-plan-generation-01", "s-plan-generation-01"],
        )


def test_input_message_is_not_a_second_send(dataset):
    scenario = next(
        item for item in dataset.scenarios
        if item["scenario_id"] == "s-plan-generation-01"
    )
    plan = scenario_plan(scenario)
    messages = [
        event["content"]
        for event in scenario["events"]
        if event["type"] == "message"
    ]
    assert messages == [scenario["input"]["message"]]
    assert plan.event_ids == ["e01"]


async def test_initial_state_distinguishes_missing_and_empty_profile(dataset, root):
    scenario = next(
        item for item in dataset.scenarios
        if item["scenario_id"] == "s-profile-update-01"
    )
    state = dict(scenario["initial_state"])
    state["profile"] = None
    missing_root = root / "missing"
    missing_root.mkdir()
    async with open_runtime(missing_root) as runtime:
        session_id = str(uuid4())
        await runtime.service.create_session_result(session_id, "missing")
        await _seed_initial_state(runtime, state, 1_760_000_000_000)
        projection = await read_business_projection(
            runtime.business_repository, [session_id]
        )
        assert projection.profile is None
        verified = _verify_initial_projection(projection, state)
        assert verified["profile_present"] is False

    empty_root = root / "empty"
    empty_root.mkdir()
    state["profile"] = {
        "goal": None,
        "experience": None,
        "environment": None,
        "availability": None,
        "health_notes": None,
        "movement_restrictions": None,
        "unavailable_equipment": None,
        "forbidden_exercise_ids": None,
    }
    async with open_runtime(empty_root) as runtime:
        session_id = str(uuid4())
        await runtime.service.create_session_result(session_id, "empty")
        await _seed_initial_state(runtime, state, 1_760_000_000_000)
        projection = await read_business_projection(
            runtime.business_repository, [session_id]
        )
        assert projection.profile is not None
        assert projection.profile.version == 1
        assert projection.profile.content.is_empty() is True
        verified = _verify_initial_projection(projection, state)
        assert verified["profile_present"] is True


async def test_projection_covers_all_workout_pages(root):
    async with open_runtime(root) as runtime:
        session_id = str(uuid4())
        await runtime.service.create_session_result(session_id, "paging")
        base_day = date(2025, 1, 1)
        for index in range(205):
            record = WorkoutRecord(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"workout:{index}")),
                performed_on=(base_day + timedelta(days=index)).isoformat(),
                version=1,
                content=WorkoutContent(
                    exercises=[
                        WorkoutExercise(
                            exercise_id="0001",
                            name="动作",
                            load_convention=None,
                            sets=[WorkoutSet(
                                reps=8, weight_kg=None, duration_seconds=None
                            )],
                        )
                    ],
                    notes=None,
                ),
                created_at=1_760_000_000_000 + index,
                updated_at=1_760_000_000_000 + index,
            )
            await runtime.business_repository.insert_workout(record)
        projection = await read_business_projection(
            runtime.business_repository, [session_id]
        )
        assert len(projection.workouts) == 205
        assert projection.workouts == sorted(
            projection.workouts,
            key=lambda item: (item.performed_on, item.id),
            reverse=True,
        )


async def test_initial_plan_and_workout_versions_are_preserved(root):
    profile = ProfileContent(
        goal="增肌",
        experience="中级",
        environment="健身房",
        availability="每周四练",
        health_notes=None,
        movement_restrictions=None,
        unavailable_equipment=None,
        forbidden_exercise_ids=None,
    )
    plan = PlanRecord(
        id=str(uuid4()),
        is_current=True,
        created_at=1_760_000_000_000,
        content=PlanContent(
            repeat=None,
            days=[{
                "kind": "training",
                "focus": "胸",
                "exercises": [],
                "notes": None,
            }],
            notes=None,
            suggested_fields=[],
        ),
    )
    workout = WorkoutRecord(
        id=str(uuid4()),
        performed_on="2026-10-01",
        version=2,
        content=WorkoutContent(
            exercises=[WorkoutExercise(
                exercise_id="0001",
                name="动作",
                load_convention=None,
                sets=[WorkoutSet(reps=8, weight_kg=None, duration_seconds=None)],
            )],
            notes=None,
        ),
        created_at=1_760_000_000_000,
        updated_at=1_760_000_000_000,
    )
    state = {
        "profile": profile.model_dump(mode="json"),
        "plans": [plan.model_dump(mode="json")],
        "workouts": [workout.model_dump(mode="json")],
        "current_plan_id": plan.id,
        "now": "2026-10-18T09:00:00+08:00",
        "timezone": "Asia/Shanghai",
    }
    async with open_runtime(root) as runtime:
        session_id = str(uuid4())
        await runtime.service.create_session_result(session_id, "state")
        await _seed_initial_state(runtime, state, 1_760_000_000_000)
        projection = await read_business_projection(
            runtime.business_repository, [session_id]
        )
        verified = _verify_initial_projection(projection, state)
        assert verified["current_plan_id"] == plan.id
        assert projection.profile.version == 1
        assert projection.workouts[0].version == 2
        assert projection.workouts[0].created_at == projection.workouts[0].updated_at


async def test_prior_messages_keep_roles_and_order(root):
    async with open_runtime(root) as runtime:
        session_id = str(uuid4())
        await runtime.service.create_session_result(session_id, "prior")
        prior = [
            {"role": "user", "content": "我每周三练"},
            {"role": "assistant", "content": "收到，你的时间安排已了解"},
            {"role": "user", "content": "继续"},
        ]
        await _seed_prior_messages(
            runtime, session_id, prior, MODEL, {}, 1_760_000_000_000
        )
        history = await runtime.service.queries.get_session_history(session_id)
        messages = [
            entry.messages[0]
            for entry in history.entries
            if isinstance(entry, SessionMessageEntry)
            and entry.messages[0].role in {"user", "assistant"}
        ]
        assert [message.role for message in messages] == [
            "user", "assistant", "user"
        ]
        assert [message_text(message) for message in messages] == [
            "我每周三练", "收到，你的时间安排已了解", "继续"
        ]


def message_text(message) -> str:
    if isinstance(message, UserMessage):
        return message.content
    return "".join(
        block.text for block in message.content
        if isinstance(block, TextContent)
    )


def test_attachment_identity_uses_frozen_asset(dataset):
    scenario = next(
        item for item in dataset.scenarios
        if item["scenario_id"] == "s-plan-import-file-02"
    )
    assets = scenario["input"]["attachments"] or scenario["events"][0]["attachments"]
    attachments = _attachments_for_event(dataset, assets, 0)
    assert len(attachments) == len(assets)
    for item, asset_id in zip(attachments, assets, strict=True):
        path = dataset.assets[asset_id]
        assert item["file_name"] == path.name
        assert item["kind"] == "upload"
        assert uuid.UUID(item["attachment_id"])
        import base64
        assert base64.b64decode(item["data_base64"]) == path.read_bytes()


def test_binding_ambiguity_is_rejected():
    proposal = ProfilePendingProposal(
        business_kind="profile",
        proposal_id=str(uuid4()),
        status="pending",
        payload=ProfileContent(
            goal="增肌",
            experience=None,
            environment=None,
            availability=None,
            health_notes=None,
            movement_restrictions=None,
            unavailable_equipment=None,
            forbidden_exercise_ids=None,
        ),
        request_entry_id=str(uuid4()),
        source_entry_id=str(uuid4()),
        display_entry_id=str(uuid4()),
        confirmation_entry_id=None,
        profile_id=1,
        base_profile_version=None,
    )
    projection = BusinessProjection(
        profile=None,
        plans=[],
        workouts=[],
        save_records=[],
        proposals=[proposal, proposal.model_copy(
            update={"proposal_id": str(uuid4())}
        )],
        confirmation_operations=[],
        cancellations=[],
    )
    with pytest.raises(ValueError, match="多个真实提案"):
        build_proposal_bindings(
            projection,
            {proposal.request_entry_id: "e01"},
        )


def test_same_checkpoint_assertions_are_preserved_in_order():
    projection = BusinessProjection(
        profile=None, plans=[], workouts=[], save_records=[],
        proposals=[], confirmation_operations=[], cancellations=[],
    )
    link = EvidenceLink(
        run_id="run",
        scenario_id="s-test-01",
        event_id="e01",
        checkpoint_id="cp1",
    )
    evidence = AssertionEvidence(
        checkpoint=link,
        event_type="message",
        event_target=None,
        before=projection,
        after=projection,
    )
    outcomes = evaluate_checkpoint(
        [
            {"checkpoint_id": "cp1", "event_id": "e01", "kind": "state_unchanged", "business_kind": "all"},
            {"checkpoint_id": "cp1", "event_id": "e01", "kind": "state_unchanged", "business_kind": "all"},
        ],
        evidence,
    )
    assert len(outcomes) == 2
    assert all(outcome.passed for outcome in outcomes)


async def test_real_profile_confirm_is_idempotent(root):
    async with open_runtime(root) as runtime:
        session_id, proposal_id, display_entry_id = await seed_profile_proposal(
            runtime, "增肌"
        )
        first = await runtime.confirmations.confirm(
            session_id,
            confirmation_request(session_id, proposal_id, display_entry_id, "confirm-1"),
            now=1_760_000_000_100,
        )
        assert first.status == "saved"
        second = await runtime.confirmations.confirm(
            session_id,
            confirmation_request(session_id, proposal_id, display_entry_id, "confirm-1"),
            now=1_760_000_000_200,
        )
        assert second.status == "saved"
        assert second.operation_id == first.operation_id
        third = await runtime.confirmations.confirm(
            session_id,
            confirmation_request(session_id, proposal_id, display_entry_id, "confirm-2"),
            now=1_760_000_000_300,
        )
        assert third.status == "saved"
        projection = await read_business_projection(
            runtime.business_repository, [session_id]
        )
        assert projection.profile is not None
        assert projection.profile.version == 1
        assert len(projection.save_records) == 1
        assert len(projection.confirmation_operations) == 2
        assert all(
            record.proposal_id == proposal_id for record in projection.save_records
        )


def confirmation_request(session_id, proposal_id, display_entry_id, name):
    from app.domain.business.models import SaveConfirmationRequest

    return SaveConfirmationRequest(
        operation_id=str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"operation:{session_id}:{name}")
        ),
        business_kind="profile",
        proposal_id=proposal_id,
        display_entry_id=display_entry_id,
    )


async def seed_profile_proposal(runtime: RuntimeHandle, goal: str):
    session_id = str(uuid4())
    await runtime.service.create_session_result(session_id, "proposal")
    now = 1_760_000_000_000
    user_id = str(uuid4())
    assistant_id = str(uuid4())
    display_id = str(uuid4())
    proposal_id = str(uuid4())
    tool_call_id = str(uuid4())
    payload = ProfileContent(
        goal=goal,
        experience=None,
        environment=None,
        availability=None,
        health_notes=None,
        movement_restrictions=None,
        unavailable_equipment=None,
        forbidden_exercise_ids=None,
    )
    proposal = ProfileProposal(
        proposal_id=proposal_id,
        profile_id=1,
        base_profile_version=None,
        payload=payload,
    )
    async with runtime.session_repository.transaction():
        session = await runtime.service.queries.require_session(session_id)
        system = SessionMessageEntry(
            session_id=session_id, id=str(uuid4()), parent_id=None, run_id=None,
            type="message", messages=[SystemMessage(
                role="system", content="评价测试系统消息", timestamp=now
            )], created_at=now,
        )
        await runtime.session_repository.insert_entry(system)
        user = SessionMessageEntry(
            session_id=session_id, id=user_id, parent_id=system.id, run_id=None,
            type="message", messages=[UserMessage(
                role="user", content="把画像存一下", timestamp=now
            )], created_at=now,
        )
        await runtime.session_repository.insert_entry(user)
        assistant = SessionMessageEntry(
            session_id=session_id, id=assistant_id, parent_id=user_id, run_id=None,
            type="message", messages=[AssistantMessage(
                role="assistant",
                content=[ToolCall(
                    type="toolCall",
                    id=tool_call_id,
                    name="prepare_profile_update",
                    arguments={"payload": payload.model_dump(mode="json")},
                )],
                api="openai-completions",
                provider="evaluation",
                model="evaluation-model",
                usage=Usage(
                    input=0, output=0, cache_read=0, cache_write=0,
                    total_tokens=0,
                ),
                stop_reason="toolUse",
                timestamp=now,
            )], created_at=now,
        )
        await runtime.session_repository.insert_entry(assistant)
        display = SessionMessageEntry(
            session_id=session_id, id=display_id, parent_id=assistant_id, run_id=None,
            type="message", messages=[ToolResultMessage(
                role="toolResult",
                tool_call_id=tool_call_id,
                tool_name="prepare_profile_update",
                content=[TextContent(
                    type="text",
                    text=json.dumps(
                        proposal.model_dump(mode="json"), ensure_ascii=False
                    ),
                )],
                is_error=False,
                timestamp=now,
            )], created_at=now,
        )
        await runtime.session_repository.insert_entry(display)
        await set_leaf(runtime.session_repository, session, display_id, now)
        snapshot = ProfileSnapshot(
            proposal_id=proposal_id,
            session_id=session_id,
            request_entry_id=user_id,
            source_entry_id=assistant_id,
            profile_id=1,
            base_profile_version=None,
            payload=payload,
            display_entry_id=display_id,
            confirmation_entry_id=None,
            status="pending",
            created_at=now,
        )
        await runtime.business_repository.insert_snapshot(snapshot)
    return session_id, proposal_id, display_id


async def test_real_cancel_and_restart_persistence(root):
    async with open_runtime(root) as runtime:
        session_id, proposal_id, display_id = await seed_profile_proposal(
            runtime, "增肌"
        )
        now = 1_760_000_000_000
        request_id = str(uuid4())
        assistant_id = str(uuid4())
        tool_call_id = str(uuid4())
        run_id = str(uuid4())
        arguments = {"business_kind": "profile", "proposal_id": proposal_id}
        async with runtime.session_repository.transaction():
            session = await runtime.service.queries.require_session(session_id)
            request = SessionMessageEntry(
                session_id=session_id, id=request_id, parent_id=display_id,
                run_id=None, type="message",
                messages=[UserMessage(
                    role="user", content="取消刚才那份画像", timestamp=now + 1
                )],
                created_at=now + 1,
            )
            await runtime.session_repository.insert_entry(request)
            await runtime.session_repository.insert_run(SessionRun(
                session_id=session_id,
                id=run_id,
                request_entry_id=request_id,
                last_entry_id=None,
                status="running",
                started_at=now,
                finished_at=None,
                error_code=None,
                error_message=None,
                stop_reason=None,
                stop_diagnostic=None,
            ))
            assistant = SessionMessageEntry(
                session_id=session_id, id=assistant_id, parent_id=request_id,
                run_id=run_id, type="message",
                messages=[AssistantMessage(
                    role="assistant",
                    content=[ToolCall(
                        type="toolCall", id=tool_call_id,
                        name="cancel_pending_proposal", arguments=arguments,
                    )],
                    api="openai-completions",
                    provider="evaluation",
                    model="evaluation-model",
                    usage=Usage(
                        input=0, output=0, cache_read=0, cache_write=0,
                        total_tokens=0,
                    ),
                    stop_reason="toolUse",
                    timestamp=now + 2,
                )],
                created_at=now + 2,
            )
            await runtime.session_repository.insert_entry(assistant)
            await set_leaf(runtime.session_repository, session, assistant_id, now + 2)
        context = BusinessContext(
            timezone="Asia/Shanghai",
            business_date="2026-10-18",
            session_id=session_id,
            run_id=str(uuid4()),
            request_entry_id=request_id,
            source_entry_id=assistant_id,
        )
        cancellation = await runtime.business.proposals.cancel_pending_proposal(
            context,
            PendingProposalArguments(
                business_kind="profile", proposal_id=proposal_id
            ),
        )
        assert cancellation.status == "invalidated"
        result_id = str(uuid4())
        async with runtime.session_repository.transaction():
            session = await runtime.service.queries.require_session(session_id)
            result_entry = SessionMessageEntry(
                session_id=session_id,
                id=result_id,
                parent_id=assistant_id,
                run_id=run_id,
                type="message",
                messages=[ToolResultMessage(
                    role="toolResult",
                    tool_call_id=tool_call_id,
                    tool_name="cancel_pending_proposal",
                    content=[TextContent(
                        type="text",
                        text=json.dumps(
                            cancellation.model_dump(mode="json"),
                            ensure_ascii=False,
                        ),
                    )],
                    is_error=False,
                    timestamp=now + 3,
                )],
                created_at=now + 3,
            )
            await runtime.session_repository.insert_entry(result_entry)
            await set_leaf(
                runtime.session_repository, session, result_id, now + 3
            )
            run = await runtime.session_repository.get_run(session_id, run_id)
            await runtime.session_repository.update_run(run.model_copy(update={
                "last_entry_id": result_id,
                "status": "completed",
                "finished_at": now + 3,
                "stop_reason": "stop",
            }))
        evidence = await read_cancel_evidence(
            runtime.service.queries, [session_id]
        )
        assert len(evidence) == 1
        assert evidence[0].tool_call_id == tool_call_id
        assert evidence[0].proposal_id == proposal_id
        assert evidence[0].source_entry_id == assistant_id
        assert evidence[0].result_entry_id == result_id
        projection = await read_business_projection_with_cancellations(
            runtime.business_repository,
            runtime.service.queries,
            [session_id],
        )
        assert projection.cancellations[0].proposal_id == proposal_id

    async with open_runtime(root) as reopened:
        projection = await read_business_projection_with_cancellations(
            reopened.business_repository,
            reopened.service.queries,
            [session_id],
        )
        snapshot = await reopened.business_repository.get_snapshot(proposal_id)
        assert snapshot.status == "invalidated"
        assert projection.cancellations[0].proposal_id == proposal_id
        evidence = await read_cancel_evidence(
            reopened.service.queries, [session_id]
        )
        assert len(evidence) == 1
        assert evidence[0].tool_call_id == tool_call_id


async def test_invalidated_snapshot_alone_is_not_cancel_evidence(root):
    async with open_runtime(root) as runtime:
        session_id, proposal_id, _ = await seed_profile_proposal(
            runtime, "增肌"
        )
        await runtime.business_repository.set_snapshot_status(
            proposal_id, "invalidated"
        )
        projection = await read_business_projection(
            runtime.business_repository, [session_id]
        )
        assert projection.cancellations == []
        with_cancellations = await read_business_projection_with_cancellations(
            runtime.business_repository,
            runtime.service.queries,
            [session_id],
        )
        assert with_cancellations.cancellations == []
        assert await read_cancel_evidence(
            runtime.service.queries, [session_id]
        ) == []


def test_pure_state_checkpoint_emits_not_applicable(dataset):
    scenario = dict(next(
        item for item in dataset.scenarios
        if item["scenario_id"] == "s-confirmation-cancellation-state-04"
    ))
    scenario["_evaluation_run_id"] = "run"
    plan = scenario_plan(scenario)
    assert plan.generation_metrics["cp2"] == [
        "factual_correctness", "faithfulness"
    ]
    artifact = _not_applicable_generation(
        scenario, {"event_id": "e03"}, "cp2"
    )
    assert artifact.checkpoint is None
    assert artifact.result is not None
    assert [
        (outcome.metric, outcome.status, outcome.reason)
        for outcome in artifact.result.outcomes
    ] == [
        ("faithfulness", "not_applicable", "纯状态检查点，没有模型生成的事实内容"),
        ("factual_correctness", "not_applicable", "纯状态检查点，没有模型生成的事实内容"),
    ]


def test_incomplete_items_include_failed_and_applicable_metrics():
    link = EvidenceLink(
        run_id="run",
        scenario_id="s-test-01",
        event_id="e01",
        checkpoint_id="cp1",
    )
    artifact = ScenarioArtifact(
        run_id="run",
        scenario_id="s-test-01",
        session_ids=[],
        events=[],
        assertions=[AssertionCheckpointArtifact(
            checkpoint_id="cp1",
            event_id="e01",
            binding_error="proposal_ref 缺少唯一真实绑定",
        )],
        generation=[GenerationCheckpointArtifact(
            checkpoint_id="cp1",
            event_id="e01",
            result=CheckpointResult(
                checkpoint=link,
                outcomes=[
                    MetricOutcome(
                        metric="faithfulness", status="failed", error="TimeoutError"
                    ),
                    MetricOutcome(
                        metric="factual_correctness", status="applicable"
                    ),
                ],
            ),
            runner_error="TimeoutError: ",
            incomplete_metrics=["factual_correctness"],
        )],
        proposal_bindings={},
    )
    items = _incomplete_items(artifact)
    assert "s-test-01/cp1:binding_error" in items
    assert "s-test-01/cp1:scoring_error" in items
    assert "s-test-01/cp1/faithfulness:failed" in items
    assert "s-test-01/cp1/factual_correctness:applicable" in items


def test_context_items_map_sources_and_truncation():
    tool_call_id = "call-1"
    full_tool_text = "完整工具结果"
    request = {
        "model": "evaluation-model",
        "messages": [
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "用户正文"},
            {
                "role": "assistant",
                "content": "历史回答",
                "tool_calls": [{
                    "id": tool_call_id,
                    "type": "function",
                    "function": {"name": "search_exercises", "arguments": "{}"},
                }],
            },
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": "完整工具",
            },
        ],
    }
    mapping = context_items_from_request(
        request,
        api="openai-completions",
        session_id="session-evidence",
        current_entry_ids={"user-entry"},
        nodes=[
            MessageNode(
                entry_id="user-entry", session_id="session-evidence",
                role="user", content="用户正文", kind="message",
            ),
            MessageNode(
                entry_id="assistant-entry", session_id="session-evidence",
                role="assistant", content="历史回答", kind="message",
            ),
        ],
        tool_nodes=[ToolNode(
            tool_call_id=tool_call_id,
            tool_name="search_exercises",
            content=full_tool_text,
            entry_id="tool-entry",
            session_id="session-evidence",
            is_error=False,
        )],
    )
    sources = [(item.source, item.fact_origin) for item in mapping.items]
    assert sources == [
        ("system_prompt", "system"),
        ("user_message", "user"),
        ("assistant_message", "assistant"),
        ("tool_result", "tool"),
    ]
    assert mapping.items[1].message_entry_id == "user-entry"
    assert mapping.items[2].message_entry_id == "assistant-entry"
    assert mapping.items[2].message_role == "assistant"
    tool_item = mapping.items[-1]
    assert tool_item.tool_call_id == tool_call_id
    assert tool_item.tool_name == "search_exercises"
    assert tool_item.truncated is True
    assert tool_item.original_chars == len(full_tool_text)
    assert tool_item.omitted_chars == len(full_tool_text) - len("完整工具")
    assert mapping.gaps == []
    assert mapping.tool_identity_map == {tool_call_id: tool_call_id}


def test_reference_and_future_facts_are_not_in_model_input():
    request = {
        "model": "evaluation-model",
        "messages": [{"role": "user", "content": "现在的用户消息"}],
    }
    mapping = context_items_from_request(
        request,
        api="openai-completions",
        session_id="session-evidence",
        current_entry_ids={"user-entry"},
        nodes=[MessageNode(
            entry_id="user-entry", session_id="session-evidence",
            role="user", content="现在的用户消息", kind="message",
        )],
        tool_nodes=[],
    )
    combined = "\n".join(item.content for item in mapping.items)
    assert "审阅参考答案" not in combined
    assert "后续事实" not in combined
    assert len(mapping.items) == 1
    assert mapping.gaps == []


def test_business_context_tag_does_not_prove_trusted_projection():
    tagged = "<business_context>\n{\"mode\":\"snapshot\"}\n</business_context>"
    mapping = context_items_from_request(
        {
            "model": "evaluation-model",
            "messages": [{"role": "user", "content": tagged}],
        },
        api="openai-completions",
        session_id="session-evidence",
        current_entry_ids={"user-entry"},
        nodes=[MessageNode(
            entry_id="user-entry", session_id="session-evidence",
            role="user", content=tagged, kind="message",
        )],
        tool_nodes=[],
    )
    assert mapping.items[0].source == "user_message"
    assert mapping.items[0].fact_origin == "user"
    assert any("business_context 标签" in gap.reason for gap in mapping.gaps)


def test_tool_only_assistant_message_is_persisted_as_gap():
    request = {
        "model": "evaluation-model",
        "messages": [{
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "search_exercises", "arguments": "{}"},
            }],
        }],
    }
    mapping = context_items_from_request(
        request,
        api="openai-completions",
        session_id="session-evidence",
        current_entry_ids=set(),
        nodes=[MessageNode(
            entry_id="assistant-entry", session_id="session-evidence",
            role="assistant", content="", kind="message",
        )],
        tool_nodes=[],
    )
    assert mapping.items == []
    assert len(mapping.gaps) == 1
    assert mapping.gaps[0].message_entry_id == "assistant-entry"
    assert "工具调用" in mapping.gaps[0].reason


def test_normalized_tool_id_maps_to_persisted_original():
    original = "tool id with spaces"
    visible = normalize_tool_id(original, "openai-completions")
    assert visible.startswith("call_")
    mapping = context_items_from_request(
        {
            "model": "evaluation-model",
            "messages": [{
                "role": "tool",
                "tool_call_id": visible,
                "content": "结果",
            }],
        },
        api="openai-completions",
        session_id="session-evidence",
        current_entry_ids=set(),
        nodes=[],
        tool_nodes=[ToolNode(
            tool_call_id=original,
            tool_name="search_exercises",
            content="结果",
            entry_id="tool-entry",
            session_id="session-evidence",
            is_error=False,
        )],
    )
    assert mapping.items[0].tool_call_id == visible
    assert mapping.tool_identity_map == {original: visible}


def test_tool_evidence_uses_full_text_and_accepts_truncated_context():
    projection = BusinessProjection(
        profile=None, plans=[], workouts=[], save_records=[],
        proposals=[], confirmation_operations=[], cancellations=[],
    )
    link = EvidenceLink(
        run_id="run", scenario_id="s-test-01",
        event_id="e01", checkpoint_id="cp1",
    )
    full = "abcdef"
    tool = ToolResultEvidence(
        checkpoint=link,
        tool_call_id="call-1",
        tool_name="search_exercises",
        content=full,
    )
    context = ContextEvidence(
        checkpoint=link,
        item=ContextItem(
            source="tool_result",
            content="abc",
            fact_origin="tool",
            tool_call_id="call-1",
            tool_name="search_exercises",
            truncated=True,
            omitted_chars=3,
            original_chars=6,
        ),
    )
    evidence = AssertionEvidence(
        checkpoint=link,
        event_type="message",
        event_target=None,
        before=projection,
        after=projection,
        tool_results=[tool],
        context_evidence=[context],
    )
    outcomes = evaluate_checkpoint(
        [{
            "checkpoint_id": "cp1",
            "event_id": "e01",
            "kind": "evidence_present",
            "evidence_kind": "tool_result",
            "min_count": 1,
        }],
        evidence,
    )
    assert outcomes[0].passed is True


def test_tool_evidence_conflict_is_rejected():
    projection = BusinessProjection(
        profile=None, plans=[], workouts=[], save_records=[],
        proposals=[], confirmation_operations=[], cancellations=[],
    )
    link = EvidenceLink(
        run_id="run", scenario_id="s-test-01",
        event_id="e01", checkpoint_id="cp1",
    )
    evidence = AssertionEvidence(
        checkpoint=link,
        event_type="message",
        event_target=None,
        before=projection,
        after=projection,
        tool_results=[ToolResultEvidence(
            checkpoint=link,
            tool_call_id="call-1",
            tool_name="search_exercises",
            content="CHANGED",
        )],
        context_evidence=[ContextEvidence(
            checkpoint=link,
            item=ContextItem(
                source="tool_result",
                content="abc",
                fact_origin="tool",
                tool_call_id="call-1",
                tool_name="search_exercises",
            ),
        )],
    )
    with pytest.raises(AssertionInputError, match="工具结果正文与上下文不一致"):
        evaluate_checkpoint(
            [{
                "checkpoint_id": "cp1",
                "event_id": "e01",
                "kind": "evidence_present",
                "evidence_kind": "tool_result",
                "min_count": 1,
            }],
            evidence,
        )


async def seed_search_request(runtime, *, include_success: bool):
    session_id = str(uuid4())
    await runtime.service.create_session_result(session_id, "search")
    run_id = str(uuid4())
    now = 1_760_000_000_000
    user_id = str(uuid4())
    assistant_id = str(uuid4())
    answer_id = str(uuid4())
    exercise = load_catalog().all()[0]
    calls = []
    if include_success:
        calls.append({
            "items": [{
                "id": exercise.id,
                "name": exercise.name,
                "equipment": exercise.equipment,
                "target": exercise.target,
                "matches": [f"equipment={exercise.equipment}"],
            }],
            "has_more": False,
        })
    calls.append({
        "code": "invalid_search_condition",
        "message": "body_part 取值无法映射到动作目录",
    })
    calls.append({
        "code": "invalid_search_condition",
        "message": "muscle_group 取值无法映射到动作目录",
    })
    request_calls = []
    result_entries = []
    for index, payload in enumerate(calls):
        call_id = f"call-search-{index}"
        text = json.dumps(payload, ensure_ascii=False)
        request_calls.append({"id": call_id, "name": "search_exercises", "text": text})
    async with runtime.session_repository.transaction():
        session = await runtime.service.queries.require_session(session_id)
        user_text = "请按条件检索动作"
        await runtime.session_repository.insert_entry(SessionMessageEntry(
            session_id=session_id, id=user_id, parent_id=None, run_id=None,
            type="message", messages=[UserMessage(
                role="user", content=user_text, timestamp=now
            )], created_at=now,
        ))
        await runtime.session_repository.insert_run(SessionRun(
            session_id=session_id,
            id=run_id,
            request_entry_id=user_id,
            last_entry_id=None,
            status="running",
            started_at=now,
            finished_at=None,
            error_code=None,
            error_message=None,
            stop_reason=None,
            stop_diagnostic=None,
        ))
        await runtime.session_repository.insert_entry(SessionMessageEntry(
            session_id=session_id, id=assistant_id, parent_id=user_id,
            run_id=run_id, type="message", messages=[AssistantMessage(
                role="assistant",
                content=[
                    ToolCall(
                        type="toolCall",
                        id=call["id"],
                        name=call["name"],
                        arguments={},
                    )
                    for call in request_calls
                ],
                api="openai-completions",
                provider="evaluation",
                model="evaluation-model",
                usage=Usage(
                    input=0, output=0, cache_read=0, cache_write=0,
                    total_tokens=0,
                ),
                stop_reason="toolUse",
                timestamp=now,
            )], created_at=now,
        ))
        parent_id = assistant_id
        for call, payload in zip(request_calls, calls):
            entry = SessionMessageEntry(
                session_id=session_id, id=str(uuid4()), parent_id=parent_id,
                run_id=run_id, type="message", messages=[ToolResultMessage(
                    role="toolResult",
                    tool_call_id=call["id"],
                    tool_name=call["name"],
                    content=[TextContent(type="text", text=call["text"])],
                    is_error="code" in payload,
                    timestamp=now,
                )], created_at=now,
            )
            await runtime.session_repository.insert_entry(entry)
            result_entries.append(entry)
            parent_id = entry.id
        await runtime.session_repository.insert_entry(SessionMessageEntry(
            session_id=session_id, id=answer_id, parent_id=parent_id,
            run_id=run_id, type="message", messages=[AssistantMessage(
                role="assistant",
                content=[TextContent(type="text", text="已按条件检索")],
                api="openai-completions",
                provider="evaluation",
                model="evaluation-model",
                usage=Usage(
                    input=0, output=0, cache_read=0, cache_write=0,
                    total_tokens=0,
                ),
                stop_reason="stop",
                timestamp=now,
            )], created_at=now,
        ))
        await set_leaf(runtime.session_repository, session, answer_id, now)
    request = {
        "model": "evaluation-model",
        "messages": [
            {"role": "user", "content": user_text},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": "{}",
                        },
                    }
                    for call in request_calls
                ],
            },
            *[
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": call["text"],
                }
                for call in request_calls
            ],
        ],
    }
    sequence = runtime.call_recorder.record_request(
        request, MODEL, "answer", answer_id, None
    )
    runtime.call_recorder.record_outcome(sequence, "stop", None)
    capture = EventCapture(
        run_id=run_id,
        scenario_id="s-tool-error",
        event_id="e01",
        event_type="message",
        source_event_id=None,
        session_id=session_id,
        operation_id=None,
        request_entry_id=user_id,
        terminal_status="completed",
        terminal_event="message_end",
        before=BusinessProjection(),
        after=BusinessProjection(),
        answer_message_id=answer_id,
    )
    return capture, {
        call["id"]: call["text"] for call in request_calls
    }


def search_assertions() -> list[dict]:
    return [
        {
            "checkpoint_id": "cp1",
            "event_id": "e01",
            "kind": "evidence_present",
            "evidence_kind": "ranking",
            "min_count": 1,
        },
        {
            "checkpoint_id": "cp1",
            "event_id": "e01",
            "kind": "state_unchanged",
            "business_kind": "all",
        },
    ]


async def test_failed_search_output_stays_in_context_without_ranking(root):
    async with open_runtime(root) as runtime:
        capture, contents = await seed_search_request(
            runtime, include_success=True
        )
        success_id, *error_ids = contents
        assertions = search_assertions()
        evidence = await _assertion_evidence(
            runtime,
            {"_evaluation_run_id": "run-eval", "scenario_id": "s-tool-error"},
            {"event_id": "e01", "type": "message"},
            capture,
            "cp1",
            assertions,
            {},
        )
        assert [entry.tool_call_id for entry in evidence.rankings] == [success_id]
        assert {
            entry.tool_call_id: entry.content for entry in evidence.tool_results
        } == contents
        contexts = {
            item.item.tool_call_id: item.item.content
            for item in evidence.context_evidence
            if item.item.source == "tool_result"
        }
        assert contexts == contents
        outcomes = evaluate_checkpoint(assertions, evidence)
        assert [outcome.passed for outcome in outcomes] == [True, True]


async def test_ranking_assertion_keeps_real_failure_without_success_search(root):
    async with open_runtime(root) as runtime:
        capture, contents = await seed_search_request(
            runtime, include_success=False
        )
        assertions = search_assertions()
        evidence = await _assertion_evidence(
            runtime,
            {"_evaluation_run_id": "run-eval", "scenario_id": "s-tool-error"},
            {"event_id": "e01", "type": "message"},
            capture,
            "cp1",
            assertions,
            {},
        )
        assert evidence.rankings == []
        assert {
            entry.tool_call_id: entry.content for entry in evidence.tool_results
        } == contents
        outcomes = evaluate_checkpoint(assertions, evidence)
        assert outcomes[0].passed is False
        assert "rankings=0" in outcomes[0].evidence
        assert outcomes[1].passed is True


def test_tool_content_map_uses_full_text():
    original = "tool id with spaces"
    visible = normalize_tool_id(original, "openai-completions")
    nodes = tool_nodes_by_visible(
        [ToolNode(
            tool_call_id=original,
            tool_name="search_exercises",
            content="完整正文",
            entry_id="tool-entry",
            session_id="session-evidence",
            is_error=False,
        )],
        "openai-completions",
    )
    assert nodes[visible].content == "完整正文"
    assert nodes[visible].is_error is False


def test_identity_endpoints_are_sanitized():
    model = type("SyntheticModel", (), {
        "api": "openai-completions",
        "provider": "synthetic",
        "model": "synthetic-model",
        "base_url": "https://sentinel-user:sentinel-pass@example.invalid/v1?token=SENTINEL_SECRET",
    })()
    summary = model_identity_summary(model)
    serialized = json.dumps(summary)
    assert "sentinel-user" not in serialized
    assert "sentinel-pass" not in serialized
    assert "SENTINEL_SECRET" not in serialized
    assert summary["base_url"] == "https://example.invalid/v1"
    text = sanitize_error_text(
        "failed https://sentinel-user:sentinel-pass@example.invalid/v1?token=SENTINEL_SECRET"
    )
    assert "sentinel-user" not in text
    assert "sentinel-pass" not in text
    assert "SENTINEL_SECRET" not in text


def test_judge_model_accepts_configured_model():
    for model in ("decision-model-preview", "qwen3.6-plus", "unknown-judge"):
        assert load_judge_model({
            "FIT_AGENT_EVAL_JUDGE_MODEL": model
        }) == model
    with pytest.raises(Exception):
        load_judge_model({"FIT_AGENT_EVAL_JUDGE_MODEL": "   "})
    config = JudgeConfig(
        model="qwen3.6-plus",
        base_url="https://example.invalid/v1",
        api_key="synthetic-key",
    )
    assert config.model == "qwen3.6-plus"
    evidence = [
        type("Evidence", (), {
            "judge_model": "decision-model-preview",
            "prompt_digest": "a" * 64,
            "metric": "faithfulness",
            "metric_version": "b" * 64,
        })(),
        type("Evidence", (), {
            "judge_model": "decision-model-preview",
            "prompt_digest": "a" * 64,
            "metric": "faithfulness",
            "metric_version": "c" * 64,
        })(),
    ]
    with pytest.raises(ValueError, match="同一指标的配置版本冲突"):
        run_judge_identity(evidence)
    identity = run_judge_identity(evidence[:1])
    assert identity.model == "decision-model-preview"


def _forbid_generation_model_config(monkeypatch):
    def fail_load_model_config():
        raise AssertionError("裁判配置不得读取生成模型配置")

    monkeypatch.setattr(
        "evaluation.scenarios.load_model_config", fail_load_model_config
    )


def test_judge_config_reads_all_fields_from_environment(monkeypatch):
    _forbid_generation_model_config(monkeypatch)
    monkeypatch.setenv("FIT_AGENT_EVAL_JUDGE_MODEL", "decision-model-preview")
    monkeypatch.setenv(
        "FIT_AGENT_EVAL_JUDGE_BASE_URL",
        "https://judge.example.invalid/compatible-mode/v1",
    )
    monkeypatch.setenv("FIT_AGENT_EVAL_JUDGE_API_KEY", "judge-synthetic-key")
    judge, timeout = _judge_config()
    assert judge is not None
    assert judge.model == "decision-model-preview"
    assert judge.base_url == "https://judge.example.invalid/compatible-mode/v1"
    assert judge.api_key == "judge-synthetic-key"
    assert timeout == DEFAULT_JUDGE_TIMEOUT


def test_judge_config_requires_dedicated_base_url(monkeypatch):
    _forbid_generation_model_config(monkeypatch)
    monkeypatch.setenv("FIT_AGENT_EVAL_JUDGE_API_KEY", "judge-synthetic-key")
    monkeypatch.setenv("FIT_AGENT_EVAL_JUDGE_MODEL", "decision-model-preview")
    monkeypatch.delenv("FIT_AGENT_EVAL_JUDGE_BASE_URL", raising=False)
    assert _judge_config() == (None, None)
    for blank in ("", "   "):
        monkeypatch.setenv("FIT_AGENT_EVAL_JUDGE_BASE_URL", blank)
        assert _judge_config() == (None, None)
    monkeypatch.setenv("FIT_AGENT_EVAL_JUDGE_BASE_URL", "https://judge.example.invalid/v1")
    monkeypatch.delenv("FIT_AGENT_EVAL_JUDGE_MODEL", raising=False)
    assert _judge_config() == (None, None)
    monkeypatch.setenv("FIT_AGENT_EVAL_JUDGE_MODEL", "qwen3.6-plus")
    judge, _ = _judge_config()
    assert judge is not None
    assert judge.model == "qwen3.6-plus"


def test_judge_config_requires_dedicated_api_key(monkeypatch):
    _forbid_generation_model_config(monkeypatch)
    monkeypatch.setenv("FIT_AGENT_EVAL_JUDGE_MODEL", "decision-model-preview")
    monkeypatch.setenv(
        "FIT_AGENT_EVAL_JUDGE_BASE_URL",
        "https://judge.example.invalid/compatible-mode/v1",
    )
    monkeypatch.delenv("FIT_AGENT_EVAL_JUDGE_API_KEY", raising=False)
    assert _judge_config() == (None, None)
    for blank in ("", "   "):
        monkeypatch.setenv("FIT_AGENT_EVAL_JUDGE_API_KEY", blank)
        assert _judge_config() == (None, None)
    monkeypatch.setenv("FIT_AGENT_EVAL_JUDGE_API_KEY", "judge-synthetic-key")
    judge, _ = _judge_config()
    assert judge is not None
    assert judge.api_key == "judge-synthetic-key"


def test_hidden_candidate_identity_is_rejected_from_public_response():
    payload = {
        "items": [],
        "has_more": True,
        "remaining_ids": ["0001"],
    }
    with pytest.raises(ValueError, match="仅包含 items 与 has_more"):
        search_evidence_from_tool(
            json.dumps(payload), "call-1"
        )


def test_full_request_recorder_rejects_credentials(root):
    from app.ai.types import ModelSpec

    recorder = EvaluationCallRecorder(root / "model-calls")
    model = ModelSpec(
        api="openai-completions",
        provider="evaluation",
        id="evaluation-model",
        base_url="https://example.invalid/v1",
        context_window=1000,
        max_tokens=100,
    )
    request = {
        "model": "evaluation-model",
        "messages": [{"role": "user", "content": "包含 secret-value 的正文"}],
    }
    with pytest.raises(ValueError, match="拒绝写入"):
        recorder.record_request(
            request,
            model,
            "normal",
            "message-1",
            None,
            contains=lambda value: "secret-value" in canonical_json(value),
        )
    assert recorder.records == []
    assert not (root / "model-calls" / "requests").exists()


async def test_session_switch_shares_business_and_scenarios_are_isolated(root):
    first_root = root / "first"
    second_root = root / "second"
    first_root.mkdir()
    second_root.mkdir()
    async with open_runtime(first_root) as runtime:
        first_session, proposal_id, _ = await seed_profile_proposal(
            runtime, "增肌"
        )
        second_session = str(uuid4())
        await runtime.service.create_session_result(second_session, "second")
        projection = await read_business_projection(
            runtime.business_repository, [first_session, second_session]
        )
        assert any(
            proposal.proposal_id == proposal_id
            for proposal in projection.proposals
        )
        current_only = await read_business_projection(
            runtime.business_repository, [second_session]
        )
        assert current_only.proposals == []
    async with open_runtime(second_root) as isolated:
        session_id = str(uuid4())
        await isolated.service.create_session_result(session_id, "isolated")
        projection = await read_business_projection(
            isolated.business_repository, [session_id]
        )
        assert projection.profile is None


def node(entry_id, content, *, kind="message", role="user", session="session-a", **extra):
    return MessageNode(
        entry_id=entry_id,
        session_id=session,
        role=role,
        content=content,
        kind=kind,
        **extra,
    )


def map_request(messages, nodes, *, current_entry_ids=frozenset(), session="session-a", tools=()):
    return context_items_from_request(
        {"messages": messages},
        api="openai-completions",
        session_id=session,
        current_entry_ids=set(current_entry_ids),
        nodes=nodes,
        tool_nodes=list(tools),
    )


def test_same_text_keeps_user_and_projection_identity():
    text = "<business_context>合法业务投影</business_context>"
    mapping = map_request(
        [{"role": "user", "content": text}, {"role": "user", "content": text}],
        [node("user-first", text), node("projection-second", text, kind="business_context")],
        current_entry_ids={"user-first"},
    )
    assert [
        (item.source, item.message_entry_id) for item in mapping.items
    ] == [("user_message", "user-first"), ("business_projection", "projection-second")]
    assert not mapping.ambiguous


def test_repeated_text_distinguishes_history_from_current_input():
    mapping = map_request(
        [{"role": "user", "content": "重复"}, {"role": "user", "content": "重复"}],
        [node("old", "重复"), node("current", "重复")],
        current_entry_ids={"current"},
    )
    assert [
        (item.source, item.message_entry_id) for item in mapping.items
    ] == [("prior_message", "old"), ("user_message", "current")]
    assert mapping.gaps == []


def test_current_input_includes_steering_nodes_of_the_same_run():
    nodes = [
        node("request", "原始问题", run_id="run-1"),
        node("steering", "补充说明", run_id="run-1"),
        node("earlier", "更早的补充说明", run_id="run-2"),
    ]
    current = current_input_entry_ids(nodes, "run-1", "request")
    assert current == {"request", "steering"}
    mapping = map_request(
        [{"role": "user", "content": "补充说明"}],
        nodes,
        current_entry_ids=current,
    )
    assert mapping.items[0].source == "user_message"


def test_unbound_text_keeps_gap_and_stops_scoring():
    mapping = map_request([{"role": "user", "content": "仅存在于模型请求"}], [])
    assert mapping.items == []
    assert mapping.ambiguous
    assert mapping.gaps[0].ambiguous is True


def test_nodes_from_another_session_are_not_bound():
    mapping = map_request(
        [{"role": "user", "content": "同文"}],
        [node("other-session", "同文", session="session-b")],
        session="session-a",
    )
    assert mapping.items == []
    assert mapping.gaps[0].ambiguous is True


def stage_record(collection, manifest_json='{"build_id":"b-1","compatibility":{}}'):
    return SearchStageRecord(
        sequence=0,
        run_id="run-1",
        scenario_id="s-test-01",
        event_id="e01",
        tool_call_id="call-1",
        index_collection=collection,
        index_manifest_sha256=sha256_text(manifest_json),
        index_manifest_json=manifest_json,
        arguments={"purpose": "lookup", "name": "动作"},
        candidate_pool=["0001"],
        final_model_input=["0001"],
        response_json={"items": [], "has_more": False},
        scores=[0.9],
        threshold=0.0,
    )


def executed_observation(tool_call_id, collection="collection-a"):
    stage = build_search_stage(
        [RetrievalCandidate(
            id="0001", score=0.9, name="动作", equipment="body weight",
            body_part="back", target="lats", muscle_group="back",
        )],
        EvaluatedConditions(SearchConditions(), (), ()),
        rerank=None,
        threshold=0.0,
    )
    return SearchObservation(
        tool_call_id=tool_call_id,
        arguments=CatalogSearchArguments(purpose="lookup", name="动作"),
        stage=stage,
        collection=collection,
        manifest_json='{"build_id":"b-1","compatibility":{}}',
    )


async def test_search_records_bind_event_tool_call_and_index(root):
    async with open_runtime(root) as runtime:
        runtime.scenario_id = "s-test-01"
        runtime.evaluation_run_id = "run-1"
        with pytest.raises(RuntimeError, match="事件或工具调用身份"):
            runtime._observe_search(executed_observation("call-1"))
        runtime.event_id = "e01"
        runtime._observe_search(executed_observation("call-1"))
        runtime.event_id = "e02"
        runtime._observe_search(executed_observation("call-2", "collection-b"))
        records = runtime.search_records
        assert [item.sequence for item in records] == [0, 1]
        assert [item.event_id for item in records] == ["e01", "e02"]
        assert [item.tool_call_id for item in records] == ["call-1", "call-2"]
        assert [item.index_collection for item in records] == [
            "collection-a", "collection-b"
        ]
        assert records[0].candidate_pool == ["0001"]
        first = records[0]
        await runtime.close()
        await runtime.open()
        runtime.event_id = "e03"
        runtime._observe_search(executed_observation("call-3"))
        assert [item.sequence for item in runtime.search_records] == [0, 1, 2]
        assert runtime.search_records[2].event_id == "e03"
        assert runtime.search_records[0].model_dump() == first.model_dump()


def test_executed_index_identity_requires_one_collection_per_run():
    retrieval = {"missing": [], "query_vector_identity": canonical_json({"k": "v"})}
    executed_index_identity([stage_record("collection-a")], retrieval)
    index = json.loads(retrieval["index"])
    assert index == {
        "collection": "collection-a",
        "manifest_sha256": sha256_text('{"build_id":"b-1","compatibility":{}}'),
        "build_id": "b-1",
        "compatibility": {},
    }
    assert "cache_identity" in retrieval
    assert retrieval["missing"] == []

    conflict = {"missing": [], "query_vector_identity": canonical_json({"k": "v"})}
    executed_index_identity(
        [stage_record("collection-a"), stage_record("collection-b")], conflict
    )
    assert "index" not in conflict
    assert any("多个物理索引身份" in item for item in conflict["missing"])

    empty = {"missing": [], "query_vector_identity": "{}"}
    executed_index_identity([], empty)
    assert "index" not in empty
    assert any("未执行真实检索" in item for item in empty["missing"])


async def cancel_proposal_with_shared_call_id(
    runtime, session_id, display_id, proposal_id, tool_call_id
):
    now = 1_760_000_000_500
    request_id, assistant_id, result_id, run_id = [str(uuid4()) for _ in range(4)]
    arguments = PendingProposalArguments(
        business_kind="profile", proposal_id=proposal_id
    )
    async with runtime.session_repository.transaction():
        session = await runtime.service.queries.require_session(session_id)
        await runtime.session_repository.insert_entry(SessionMessageEntry(
            session_id=session_id, id=request_id, parent_id=display_id, run_id=None,
            type="message",
            messages=[UserMessage(
                role="user", content="取消刚才那份画像", timestamp=now
            )],
            created_at=now,
        ))
        await runtime.session_repository.insert_run(SessionRun(
            session_id=session_id, id=run_id, request_entry_id=request_id,
            last_entry_id=None, status="running", started_at=now, finished_at=None,
            error_code=None, error_message=None, stop_reason=None, stop_diagnostic=None,
        ))
        await runtime.session_repository.insert_entry(SessionMessageEntry(
            session_id=session_id, id=assistant_id, parent_id=request_id,
            run_id=run_id, type="message",
            messages=[AssistantMessage(
                role="assistant",
                content=[ToolCall(
                    type="toolCall", id=tool_call_id,
                    name="cancel_pending_proposal",
                    arguments=arguments.model_dump(mode="json"),
                )],
                api="openai-completions",
                provider="evaluation",
                model="evaluation-model",
                usage=Usage(
                    input=0, output=0, cache_read=0, cache_write=0, total_tokens=0
                ),
                stop_reason="toolUse",
                timestamp=now + 1,
            )],
            created_at=now + 1,
        ))
        await set_leaf(runtime.session_repository, session, assistant_id, now + 1)
    context = BusinessContext(
        timezone="Asia/Shanghai",
        business_date="2026-10-18",
        session_id=session_id,
        run_id=run_id,
        request_entry_id=request_id,
        source_entry_id=assistant_id,
    )
    cancellation = await runtime.business.proposals.cancel_pending_proposal(
        context,
        arguments,
    )
    assert cancellation.status == "invalidated"
    async with runtime.session_repository.transaction():
        session = await runtime.service.queries.require_session(session_id)
        await runtime.session_repository.insert_entry(SessionMessageEntry(
            session_id=session_id, id=result_id, parent_id=assistant_id,
            run_id=run_id, type="message",
            messages=[ToolResultMessage(
                role="toolResult",
                tool_call_id=tool_call_id,
                tool_name="cancel_pending_proposal",
                content=[TextContent(
                    type="text", text=cancellation.model_dump_json()
                )],
                is_error=False,
                timestamp=now + 2,
            )],
            created_at=now + 2,
        ))
        await set_leaf(runtime.session_repository, session, result_id, now + 2)
    return context, arguments


async def test_cross_session_cancel_evidence_is_not_overwritten(root):
    async with open_runtime(root) as runtime:
        proposals = []
        for goal in ("增肌", "减脂"):
            session_id, proposal_id, display_id = await seed_profile_proposal(
                runtime, goal
            )
            await cancel_proposal_with_shared_call_id(
                runtime, session_id, display_id, proposal_id, "same-call-id"
            )
            proposals.append((session_id, proposal_id))
        session_ids = [session_id for session_id, _ in proposals]
        evidence = await read_cancel_evidence(
            runtime.service.queries, session_ids
        )
        assert len(evidence) == 2
        assert {(item.session_id, item.tool_call_id) for item in evidence} == {
            (session_id, "same-call-id") for session_id in session_ids
        }
        assert {item.proposal_id for item in evidence} == {
            proposal_id for _, proposal_id in proposals
        }
        projection = await read_business_projection_with_cancellations(
            runtime.business_repository, runtime.service.queries, session_ids
        )
        assert len(projection.cancellations) == 2


def cancellation_contract(item: CancelEvidence, link: EvidenceLink) -> CancellationEvidence:
    return CancellationEvidence(
        checkpoint=link,
        session_id=item.session_id,
        assistant_run_id=item.run_id,
        request_entry_id=item.request_entry_id,
        source_entry_id=item.source_entry_id,
        result_entry_id=item.result_entry_id,
        tool_call_id=item.tool_call_id,
        business_kind=item.business_kind,
        proposal_id=item.proposal_id,
        result=PendingProposalCancellation.model_validate(item.result),
    )


async def test_message_and_post_cancel_assertions_use_real_sqlite_evidence(root):
    session_id = ""
    proposal_id = ""
    display_id = ""
    context = None
    arguments = None
    async with open_runtime(root) as runtime:
        session_id, proposal_id, display_id = await seed_profile_proposal(
            runtime, "增肌"
        )
        pending = await read_business_projection_with_cancellations(
            runtime.business_repository, runtime.service.queries, [session_id]
        )
        context, arguments = await cancel_proposal_with_shared_call_id(
            runtime, session_id, display_id, proposal_id, "cancel-call"
        )
        after = await read_business_projection_with_cancellations(
            runtime.business_repository, runtime.service.queries, [session_id]
        )
        evidence = await read_cancel_evidence(
            runtime.service.queries, [session_id]
        )
        assert len(evidence) == 1
        item = evidence[0]
        assert (item.session_id, item.run_id, item.request_entry_id) == (
            session_id, context.run_id, context.request_entry_id
        )
        binding = ProposalBinding(
            proposal_ref="profile-proposal-e01",
            business_kind="profile",
            proposal_id=proposal_id,
            display_entry_id=display_id,
        )
        target = EventTarget(
            business_kind="profile",
            proposal_ref="profile-proposal-e01",
            display_ref=None,
            confirmation_ref=None,
        )
        message_link = EvidenceLink(
            run_id="run-eval",
            scenario_id="s-cancel-message",
            event_id="e02",
            checkpoint_id="cp2",
        )
        identity = EventIdentity(
            session_id=session_id,
            run_id=item.run_id,
            request_entry_id=item.request_entry_id,
        )
        message_outcomes = evaluate_checkpoint(
            [
                {
                    "checkpoint_id": "cp2",
                    "event_id": "e02",
                    "kind": "state_transition",
                    "business_kind": "profile",
                    "from_status": "pending",
                    "to_status": "invalidated",
                },
                {
                    "checkpoint_id": "cp2",
                    "event_id": "e02",
                    "kind": "saved_data_unchanged",
                    "business_kind": "profile",
                },
            ],
            AssertionEvidence(
                checkpoint=message_link,
                event_type="message",
                event_target=target,
                event_identity=identity,
                before=pending,
                after=after,
                proposal_bindings=[binding],
                cancel_evidence=[cancellation_contract(item, message_link)],
            ),
        )
        assert [outcome.passed for outcome in message_outcomes] == [True, True]

        post_link = EvidenceLink(
            run_id="run-eval",
            scenario_id="s-cancel-message",
            event_id="e03",
            checkpoint_id="cp3",
        )
        post_outcomes = evaluate_checkpoint(
            [
                {
                    "checkpoint_id": "cp3",
                    "event_id": "e03",
                    "kind": "state_transition",
                    "business_kind": "profile",
                    "from_status": "invalidated",
                    "to_status": "invalidated",
                },
                {
                    "checkpoint_id": "cp3",
                    "event_id": "e03",
                    "kind": "no_saved_state",
                    "business_kind": "profile",
                },
            ],
            AssertionEvidence(
                checkpoint=post_link,
                event_type="cancel",
                event_target=target,
                event_identity=identity,
                before=after,
                after=after.model_copy(deep=True),
                proposal_bindings=[binding],
                cancel_evidence=[cancellation_contract(item, post_link)],
            ),
        )
        assert [outcome.passed for outcome in post_outcomes] == [True, True]

        wrong_id = str(uuid4())
        wrong_target = cancellation_contract(item, message_link).model_copy(
            update={
                "proposal_id": wrong_id,
                "result": PendingProposalCancellation(
                    business_kind="profile", proposal_id=wrong_id
                ),
                "result_entry_id": str(uuid4()),
            }
        )
        wrong_outcome = evaluate_checkpoint(
            [
                {
                    "checkpoint_id": "cp2",
                    "event_id": "e02",
                    "kind": "state_transition",
                    "business_kind": "profile",
                    "from_status": "pending",
                    "to_status": "invalidated",
                }
            ],
            AssertionEvidence(
                checkpoint=message_link,
                event_type="message",
                event_target=target,
                event_identity=identity,
                before=pending,
                after=after,
                proposal_bindings=[binding],
                cancel_evidence=[wrong_target],
            ),
        )[0]
        assert not wrong_outcome.passed
        assert "缺少目标取消证据" in wrong_outcome.evidence

        ambiguous = [
            cancellation_contract(item, message_link),
            cancellation_contract(item, message_link).model_copy(
                update={"result_entry_id": str(uuid4())}
            ),
        ]
        ambiguous_outcome = evaluate_checkpoint(
            [
                {
                    "checkpoint_id": "cp2",
                    "event_id": "e02",
                    "kind": "state_transition",
                    "business_kind": "profile",
                    "from_status": "pending",
                    "to_status": "invalidated",
                }
            ],
            AssertionEvidence(
                checkpoint=message_link,
                event_type="message",
                event_target=target,
                event_identity=identity,
                before=pending,
                after=after,
                proposal_bindings=[binding],
                cancel_evidence=ambiguous,
            ),
        )[0]
        assert not ambiguous_outcome.passed
        assert "不唯一" in ambiguous_outcome.evidence

        no_cancel = evaluate_checkpoint(
            [
                {
                    "checkpoint_id": "cp2",
                    "event_id": "e02",
                    "kind": "state_transition",
                    "business_kind": "profile",
                    "from_status": "pending",
                    "to_status": "invalidated",
                }
            ],
            AssertionEvidence(
                checkpoint=message_link,
                event_type="message",
                event_target=target,
                event_identity=identity,
                before=pending,
                after=pending,
                proposal_bindings=[binding],
            ),
        )[0]
        assert not no_cancel.passed
        assert "检查点后状态为 pending" in no_cancel.evidence

    async with open_runtime(root) as reopened:
        snapshot = await reopened.business_repository.get_snapshot(proposal_id)
        assert snapshot.status == "invalidated"
        projection = await read_business_projection_with_cancellations(
            reopened.business_repository, reopened.service.queries, [session_id]
        )
        assert [row.proposal_id for row in projection.cancellations] == [proposal_id]
        evidence = await read_cancel_evidence(
            reopened.service.queries, [session_id]
        )
        assert len(evidence) == 1
        assert evidence[0].tool_call_id == "cancel-call"
        with pytest.raises(ProfileProposalInvalidated):
            await reopened.business.proposals.cancel_pending_proposal(
                context, arguments
            )
        unchanged = await read_business_projection_with_cancellations(
            reopened.business_repository, reopened.service.queries, [session_id]
        )
        assert unchanged == projection


async def test_blocked_partial_keeps_completed_metric_aggregates(dataset, root):
    scenario = next(
        item for item in dataset.scenarios
        if scenario_plan(item).generation_metrics.get("cp1")
    )
    plan = build_execution_plan(dataset, [scenario], "run-partial")
    link = EvidenceLink(
        run_id="run-partial",
        scenario_id=scenario["scenario_id"],
        event_id="e01",
        checkpoint_id="cp1",
    )
    scored = GenerationCheckpointArtifact(
        checkpoint_id="cp1",
        event_id="e01",
        result=CheckpointResult(
            checkpoint=link,
            outcomes=[
                MetricOutcome(metric="faithfulness", status="succeeded", score=1.0),
                MetricOutcome(
                    metric="factual_correctness", status="failed", error="TimeoutError"
                ),
            ],
        ),
    )
    error = ScenarioBlocked(
        scenario["scenario_id"], "e02", "冻结要求与当前生产协议无法同时满足"
    )
    handle = RuntimeHandle(
        root=root,
        evaluation_run_id="run-partial",
        scenario_id=scenario["scenario_id"],
    )
    partial = await _partial_artifact(
        handle,
        dict(scenario, _evaluation_run_id="run-partial", _session_ids=["session-a"]),
        run_id="run-partial",
        captures=[],
        assertions=[],
        generation=[scored],
        bindings={},
        retrieval=[stage_record("collection-a")],
        error=error,
    )
    assert partial.blocked == str(error)
    assert partial.runner_error is None
    assert [item.checkpoint_id for item in partial.generation] == ["cp1"]
    aggregate = RunAggregate()
    accumulate_artifact(aggregate, partial)
    coverage = build_coverage(plan, aggregate, [])
    assert coverage.observed_scenarios == 0
    assert coverage.missing == [scenario["scenario_id"]]
    assert coverage.observed_checkpoints == 1
    assert coverage.succeeded_metrics == 1
    assert coverage.failed_metrics == 1
    assert coverage.unexecuted_metrics == plan.expected_metrics - 2
    assert coverage.observed_metrics == 2
    assert [item.index_collection for item in aggregate.search_records] == [
        "collection-a"
    ]
    assert f"{scenario['scenario_id']}/cp1/faithfulness:succeeded" not in (
        aggregate.incomplete
    )


async def test_projection_nodes_carry_persisted_attachments(root):
    from app.agent.compaction import attachment_lines

    async with open_runtime(root) as runtime:
        session_id = str(uuid4())
        await runtime.service.create_session_result(session_id, "attachment")
        entry_id = str(uuid4())
        attachment_id = str(uuid4())
        now = 1_760_000_000_000
        attachment = AttachmentMetadata(
            attachment_id=attachment_id,
            session_id=session_id,
            file_name="计划.md",
            size_bytes=10,
            storage_ref=attachment_storage_ref(session_id, attachment_id, "计划.md"),
            created_at=now,
        )
        async with runtime.session_repository.transaction():
            await runtime.session_repository.insert_entry(SessionMessageEntry(
                session_id=session_id, id=entry_id, parent_id=None, run_id=None,
                type="message",
                messages=[UserMessage(
                    role="user", content="看一下这份文件", timestamp=now
                )],
                created_at=now,
            ))
            await runtime.session_repository.insert_attachment(attachment)
            await runtime.session_repository.bind_entry_attachments(
                session_id, entry_id, [attachment_id]
            )
            session = await runtime.service.queries.require_session(session_id)
            await set_leaf(runtime.session_repository, session, entry_id, now)
        request_nodes = await read_request_nodes(
            runtime.service.queries, session_id, entry_id
        )
        projected = next(
            item for item in request_nodes.nodes if item.entry_id == entry_id
        )
        assert projected.content == f"看一下这份文件\n\n{attachment_lines([attachment])}"
        assert projected.has_attachments is True
        later_id = str(uuid4())
        async with runtime.session_repository.transaction():
            await runtime.session_repository.insert_entry(SessionMessageEntry(
                session_id=session_id, id=later_id, parent_id=entry_id, run_id=None,
                type="message",
                messages=[UserMessage(
                    role="user", content="请求边界之后的新回合", timestamp=now + 5
                )],
                created_at=now + 5,
            ))
            session = await runtime.service.queries.require_session(session_id)
            await set_leaf(runtime.session_repository, session, later_id, now + 5)
        pinned = await read_request_nodes(
            runtime.service.queries, session_id, entry_id
        )
        assert [item.entry_id for item in pinned.nodes] == [entry_id]
        at_leaf = await read_request_nodes(
            runtime.service.queries, session_id, later_id
        )
        assert later_id in {item.entry_id for item in at_leaf.nodes}
        mapping = map_request(
            [{"role": "user", "content": projected.content}],
            pinned.nodes,
            current_entry_ids={entry_id},
            session=session_id,
        )
        assert [(item.source, item.message_entry_id) for item in mapping.items] == [
            ("attachment", entry_id)
        ]
        assert mapping.gaps == []


async def test_projection_nodes_follow_compaction_range(root):
    async with open_runtime(root) as runtime:
        session_id = str(uuid4())
        await runtime.service.create_session_result(session_id, "compaction")
        now = 1_760_000_000_000
        old_id, answer_id, old_tool_id, recent_id = [str(uuid4()) for _ in range(4)]
        new_assistant_id, new_tool_id, checkpoint_id, run_id = [
            str(uuid4()) for _ in range(4)
        ]
        shared_call_id = "shared-tool-call"
        summary = "早前回合已压缩"

        def tool_result(entry_id, parent_id, text, stamp):
            return SessionMessageEntry(
                session_id=session_id, id=entry_id, parent_id=parent_id,
                run_id=run_id, type="message",
                messages=[ToolResultMessage(
                    role="toolResult",
                    tool_call_id=shared_call_id,
                    tool_name="search_exercises",
                    content=[TextContent(type="text", text=text)],
                    is_error=False,
                    timestamp=stamp,
                )],
                created_at=stamp,
            )

        async with runtime.session_repository.transaction():
            await runtime.session_repository.insert_entry(SessionMessageEntry(
                session_id=session_id, id=old_id, parent_id=None,
                run_id=None, type="message",
                messages=[UserMessage(
                    role="user", content="重复的旧问题", timestamp=now
                )], created_at=now,
            ))
            await runtime.session_repository.insert_run(SessionRun(
                session_id=session_id, id=run_id, request_entry_id=old_id,
                last_entry_id=None, status="running", started_at=now, finished_at=None,
                error_code=None, error_message=None, stop_reason=None,
                stop_diagnostic=None,
            ))
            for entry in (
                SessionMessageEntry(
                    session_id=session_id, id=answer_id, parent_id=old_id,
                    run_id=run_id, type="message",
                    messages=[AssistantMessage(
                        role="assistant",
                        content=[TextContent(type="text", text="重复的旧问题")],
                        api="openai-completions",
                        provider="evaluation",
                        model="evaluation-model",
                        usage=Usage(
                            input=0, output=0, cache_read=0, cache_write=0,
                            total_tokens=0,
                        ),
                        stop_reason="stop",
                        timestamp=now + 1,
                    )], created_at=now + 1,
                ),
                tool_result(old_tool_id, answer_id, "压缩前的旧结果", now + 2),
                SessionMessageEntry(
                    session_id=session_id, id=recent_id, parent_id=old_tool_id,
                    run_id=run_id, type="message",
                    messages=[UserMessage(
                        role="user", content="重复的旧问题", timestamp=now + 3
                    )], created_at=now + 3,
                ),
                SessionMessageEntry(
                    session_id=session_id, id=new_assistant_id, parent_id=recent_id,
                    run_id=run_id, type="message",
                    messages=[AssistantMessage(
                        role="assistant",
                        content=[ToolCall(
                            type="toolCall", id=shared_call_id,
                            name="search_exercises", arguments={"name": "划船"},
                        )],
                        api="openai-completions",
                        provider="evaluation",
                        model="evaluation-model",
                        usage=Usage(
                            input=0, output=0, cache_read=0, cache_write=0,
                            total_tokens=0,
                        ),
                        stop_reason="toolUse",
                        timestamp=now + 4,
                    )], created_at=now + 4,
                ),
                tool_result(new_tool_id, new_assistant_id, "压缩后的新结果", now + 5),
                CompactionEntry(
                    session_id=session_id, id=checkpoint_id, parent_id=new_tool_id,
                    run_id=run_id, type="compaction", summary=summary,
                    first_kept_entry_id=recent_id, tokens_before=100,
                    usage=Usage(
                        input=10, output=5, cache_read=0, cache_write=0,
                        total_tokens=15,
                    ),
                    system_message=SystemMessage(
                        role="system", content="检查点系统状态", timestamp=now + 6
                    ),
                    details=None,
                    created_at=now + 6,
                ),
            ):
                await runtime.session_repository.insert_entry(entry)
            session = await runtime.service.queries.require_session(session_id)
            await set_leaf(runtime.session_repository, session, checkpoint_id, now + 6)

        pinned = await read_request_nodes(
            runtime.service.queries, session_id, old_tool_id
        )
        assert [item.entry_id for item in pinned.nodes] == [old_id, answer_id]
        assert [item.content for item in pinned.tool_nodes] == ["压缩前的旧结果"]
        at_checkpoint = await read_request_nodes(
            runtime.service.queries, session_id, checkpoint_id
        )
        assert old_id not in {item.entry_id for item in at_checkpoint.nodes}
        assert answer_id not in {item.entry_id for item in at_checkpoint.nodes}
        assert old_tool_id not in {item.entry_id for item in at_checkpoint.tool_nodes}
        assert [item.entry_id for item in at_checkpoint.tool_nodes] == [new_tool_id]
        summary_node = next(
            item for item in at_checkpoint.nodes
            if item.kind == "compaction" and item.role == "user"
        )
        assert summary_node.entry_id == checkpoint_id
        assert summary_node.content == (
            COMPACTION_SUMMARY_PREFIX + summary + COMPACTION_SUMMARY_SUFFIX
        )
        assert {
            key: node.content
            for key, node in tool_nodes_by_visible(
                at_checkpoint.tool_nodes, "openai-completions"
            ).items()
        } == {shared_call_id: "压缩后的新结果"}
        mapping = map_request(
            [
                {"role": "user", "content": summary_node.content},
                {"role": "user", "content": "重复的旧问题"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": shared_call_id,
                        "type": "function",
                        "function": {"name": "search_exercises", "arguments": "{}"},
                    }],
                },
                {
                    "role": "tool",
                    "tool_call_id": shared_call_id,
                    "content": "压缩后的新结果",
                },
            ],
            at_checkpoint.nodes,
            current_entry_ids={recent_id},
            session=session_id,
            tools=at_checkpoint.tool_nodes,
        )
        assert [
            (item.source, item.fact_origin, item.message_entry_id)
            for item in mapping.items
        ] == [
            ("prior_message", "system", checkpoint_id),
            ("user_message", "user", recent_id),
            ("tool_result", "tool", None),
        ]
        assert not mapping.ambiguous


def tool_node(entry_id, tool_call_id, content, *, run_id="run-1", session="session-a"):
    return ToolNode(
        tool_call_id=tool_call_id,
        tool_name="search_exercises",
        content=content,
        entry_id=entry_id,
        session_id=session,
        run_id=run_id,
        is_error=False,
    )


def test_reused_tool_call_id_is_an_identity_conflict():
    nodes = [
        tool_node("tool-first", "call-1", "第一次结果"),
        tool_node("tool-second", "call-1", "第二次结果", run_id="run-2"),
    ]
    mapping = map_request(
        [{"role": "tool", "tool_call_id": "call-1", "content": "第一次结果"}],
        [],
        tools=nodes,
    )
    assert mapping.items == []
    assert mapping.gaps[0].ambiguous is True
    assert "多份持久化结果" in mapping.gaps[0].reason
    with pytest.raises(ContextMappingError, match="多份持久化结果"):
        tool_nodes_by_visible(nodes, "openai-completions")
    single = map_request(
        [{"role": "tool", "tool_call_id": "call-1", "content": "第一次结果"}],
        [],
        tools=[nodes[0]],
    )
    assert single.items[0].tool_call_id == "call-1"
    assert not single.ambiguous


def empty_projection():
    return BusinessProjection(
        profile=None,
        plans=[],
        workouts=[],
        save_records=[],
        proposals=[],
        confirmation_operations=[],
        cancellations=[],
    )


def cancel_evidence(session_id, run_id, request_entry_id):
    return CancelEvidence(
        tool_call_id="shared-call",
        business_kind="profile",
        proposal_id=str(uuid4()),
        result={},
        session_id=session_id,
        run_id=run_id,
        request_entry_id=request_entry_id,
        source_entry_id="source-entry",
        result_entry_id="result-entry",
    )


def message_capture(session_id, run_id, tool_call_id, request_entry_id=None):
    return EventCapture(
        run_id=run_id,
        scenario_id="s-test-01",
        event_id="e01",
        event_type="message",
        source_event_id=None,
        session_id=session_id,
        operation_id=None,
        request_entry_id=request_entry_id,
        terminal_status="completed",
        terminal_event=None,
        before=empty_projection(),
        after=empty_projection(),
        tool_calls=[ToolCallCapture(
            tool_call_id=tool_call_id,
            tool_name="cancel_pending_proposal",
            arguments={},
        )],
    )


def test_cancel_driving_capture_requires_session_and_run():
    evidence = cancel_evidence("session-a", "run-a", "request-a")
    other_session = message_capture("session-b", "run-a", "shared-call")
    other_run = message_capture("session-a", "run-b", "shared-call")
    same = message_capture("session-a", "run-a", "shared-call")
    assert _cancel_driving_capture([other_session, other_run], evidence) is None
    assert _cancel_driving_capture([other_session, same], evidence) is same
    fallback = message_capture(
        "session-a", "run-a", "another-call", request_entry_id="request-a"
    )
    assert _cancel_driving_capture([fallback, other_session], evidence) is fallback


def test_judge_attempt_conflict_is_reported_as_missing():
    fields, missing = judge_attempt_identity(
        ["model-a", "model-b"], "model-a", "model-a"
    )
    assert fields["judge_attempt_model_conflict"] == ["model-a", "model-b"]
    assert fields["judge_same_as_generation"] is None
    assert missing == ["judge_model_conflict"]
    fields, missing = judge_attempt_identity(["model-b"], "model-a", None)
    assert fields == {
        "judge_attempt_model": "model-b",
        "judge_same_as_generation": False,
    }
    assert missing == []


async def test_failed_partial_is_handed_to_run_aggregate(dataset, root):
    scenario = next(
        item for item in dataset.scenarios
        if scenario_plan(item).generation_metrics.get("cp1")
    )
    scenario = dict(scenario, _evaluation_run_id="run-failed", _session_ids=[])
    link = EvidenceLink(
        run_id="run-failed",
        scenario_id=scenario["scenario_id"],
        event_id="e01",
        checkpoint_id="cp1",
    )
    scored = GenerationCheckpointArtifact(
        checkpoint_id="cp1",
        event_id="e01",
        result=CheckpointResult(
            checkpoint=link,
            outcomes=[
                MetricOutcome(metric="faithfulness", status="succeeded", score=1.0)
            ],
        ),
    )
    handle = RuntimeHandle(
        root=root,
        evaluation_run_id="run-failed",
        scenario_id=scenario["scenario_id"],
    )
    partial = await _partial_artifact(
        handle,
        scenario,
        run_id="run-failed",
        captures=[],
        assertions=[],
        generation=[scored],
        bindings={},
        retrieval=[stage_record("collection-a")],
        error=RuntimeError("模型连接中断"),
    )
    assert partial.runner_error == "RuntimeError: 模型连接中断"
    assert partial.blocked is None
    scenario_root = root / scenario["scenario_id"]
    scenario_root.mkdir()
    _write_json(scenario_root / "partial.json", partial.model_dump(mode="json"))
    aggregate = RunAggregate()
    accumulate_partial(scenario_root, aggregate)
    assert [item.index_collection for item in aggregate.search_records] == [
        "collection-a"
    ]
    assert (scenario["scenario_id"], "cp1", "faithfulness", "succeeded") in (
        aggregate.statuses
    )
    assert aggregate.judge_evidence == []
    absent = RunAggregate()
    accumulate_partial(root / "absent-scenario", absent)
    assert absent.search_records == []


async def test_run_identity_lists_every_missing_component(dataset, root):
    summary = await run_selected(
        dataset,
        [dataset.scenarios[0]],
        run_id=uuid4().hex,
        root=root,
        enable_model=False,
        judge=None,
        judge_timeout=None,
    )
    assert summary.scenarios == []
    assert summary.identity["missing"] == ["generation", "judge", "retrieval"]
    assert summary.identity["status"] == "incomplete"
    assert summary.coverage.unexecuted_metrics == summary.coverage.expected_metrics


async def cancel_in_session_with_reused_call_id(
    runtime, session_id, display_id, proposal_id, tool_call_id, now
):
    request_id, assistant_id, result_id, run_id = [str(uuid4()) for _ in range(4)]
    arguments = PendingProposalArguments(
        business_kind="profile", proposal_id=proposal_id
    )
    async with runtime.session_repository.transaction():
        session = await runtime.service.queries.require_session(session_id)
        await runtime.session_repository.insert_entry(SessionMessageEntry(
            session_id=session_id, id=request_id, parent_id=display_id, run_id=None,
            type="message",
            messages=[UserMessage(
                role="user", content="取消刚才那份画像", timestamp=now
            )],
            created_at=now,
        ))
        await runtime.session_repository.insert_run(SessionRun(
            session_id=session_id, id=run_id, request_entry_id=request_id,
            last_entry_id=None, status="running", started_at=now, finished_at=None,
            error_code=None, error_message=None, stop_reason=None, stop_diagnostic=None,
        ))
        await runtime.session_repository.insert_entry(SessionMessageEntry(
            session_id=session_id, id=assistant_id, parent_id=request_id,
            run_id=run_id, type="message",
            messages=[AssistantMessage(
                role="assistant",
                content=[ToolCall(
                    type="toolCall", id=tool_call_id,
                    name="cancel_pending_proposal",
                    arguments=arguments.model_dump(mode="json"),
                )],
                api="openai-completions",
                provider="evaluation",
                model="evaluation-model",
                usage=Usage(
                    input=0, output=0, cache_read=0, cache_write=0, total_tokens=0
                ),
                stop_reason="toolUse",
                timestamp=now + 1,
            )],
            created_at=now + 1,
        ))
        await set_leaf(runtime.session_repository, session, assistant_id, now + 1)
    cancellation = await runtime.business.proposals.cancel_pending_proposal(
        BusinessContext(
            timezone="Asia/Shanghai",
            business_date="2026-10-18",
            session_id=session_id,
            run_id=run_id,
            request_entry_id=request_id,
            source_entry_id=assistant_id,
        ),
        arguments,
    )
    assert cancellation.status == "invalidated"
    async with runtime.session_repository.transaction():
        session = await runtime.service.queries.require_session(session_id)
        await runtime.session_repository.insert_entry(SessionMessageEntry(
            session_id=session_id, id=result_id, parent_id=assistant_id,
            run_id=run_id, type="message",
            messages=[ToolResultMessage(
                role="toolResult",
                tool_call_id=tool_call_id,
                tool_name="cancel_pending_proposal",
                content=[TextContent(
                    type="text", text=cancellation.model_dump_json()
                )],
                is_error=False,
                timestamp=now + 2,
            )],
            created_at=now + 2,
        ))
        await set_leaf(runtime.session_repository, session, result_id, now + 2)
    return run_id, assistant_id, result_id


async def append_profile_proposal(runtime, session_id, goal, parent_id, now):
    user_id, assistant_id, display_id, proposal_id, tool_call_id = [
        str(uuid4()) for _ in range(5)
    ]
    payload = ProfileContent(
        goal=goal,
        experience=None,
        environment=None,
        availability=None,
        health_notes=None,
        movement_restrictions=None,
        unavailable_equipment=None,
        forbidden_exercise_ids=None,
    )
    proposal = ProfileProposal(
        proposal_id=proposal_id,
        profile_id=1,
        base_profile_version=None,
        payload=payload,
    )
    async with runtime.session_repository.transaction():
        session = await runtime.service.queries.require_session(session_id)
        for entry_id, parent, message in (
            (user_id, parent_id, UserMessage(
                role="user", content="再把画像存一下", timestamp=now
            )),
            (assistant_id, user_id, AssistantMessage(
                role="assistant",
                content=[ToolCall(
                    type="toolCall", id=tool_call_id,
                    name="prepare_profile_update",
                    arguments={"payload": payload.model_dump(mode="json")},
                )],
                api="openai-completions",
                provider="evaluation",
                model="evaluation-model",
                usage=Usage(
                    input=0, output=0, cache_read=0, cache_write=0, total_tokens=0
                ),
                stop_reason="toolUse",
                timestamp=now,
            )),
            (display_id, assistant_id, ToolResultMessage(
                role="toolResult",
                tool_call_id=tool_call_id,
                tool_name="prepare_profile_update",
                content=[TextContent(
                    type="text",
                    text=json.dumps(
                        proposal.model_dump(mode="json"), ensure_ascii=False
                    ),
                )],
                is_error=False,
                timestamp=now,
            )),
        ):
            await runtime.session_repository.insert_entry(SessionMessageEntry(
                session_id=session_id, id=entry_id, parent_id=parent, run_id=None,
                type="message", messages=[message], created_at=now,
            ))
        await set_leaf(runtime.session_repository, session, display_id, now)
        await runtime.business_repository.insert_snapshot(ProfileSnapshot(
            proposal_id=proposal_id,
            session_id=session_id,
            request_entry_id=user_id,
            source_entry_id=assistant_id,
            profile_id=1,
            base_profile_version=None,
            payload=payload,
            display_entry_id=display_id,
            confirmation_entry_id=None,
            status="pending",
            created_at=now,
        ))
    return proposal_id, display_id


async def test_reused_cancel_call_id_in_same_session_keeps_each_identity(root):
    async with open_runtime(root) as runtime:
        session_id, first_proposal, first_display = await seed_profile_proposal(
            runtime, "增肌"
        )
        first = await cancel_in_session_with_reused_call_id(
            runtime, session_id, first_display, first_proposal, "reused-call",
            1_760_000_001_000,
        )
        session = await runtime.service.queries.require_session(session_id)
        second_proposal, second_display = await append_profile_proposal(
            runtime, session_id, "减脂", session.active_leaf_id, 1_760_000_002_000
        )
        second = await cancel_in_session_with_reused_call_id(
            runtime, session_id, second_display, second_proposal, "reused-call",
            1_760_000_003_000,
        )
        evidence = await read_cancel_evidence(runtime.service.queries, [session_id])
        assert len(evidence) == 2
        by_run = {item.run_id: item for item in evidence}
        assert set(by_run) == {first[0], second[0]}
        assert by_run[first[0]].source_entry_id == first[1]
        assert by_run[first[0]].result_entry_id == first[2]
        assert by_run[first[0]].proposal_id == first_proposal
        assert by_run[second[0]].source_entry_id == second[1]
        assert by_run[second[0]].proposal_id == second_proposal


async def test_same_run_reused_cancel_id_binds_each_source_node(root):
    async with open_runtime(root) as runtime:
        session_id = str(uuid4())
        await runtime.service.create_session_result(session_id, "same-run")
        now = 1_760_000_000_000
        request_id, run_id, call_id = str(uuid4()), str(uuid4()), str(uuid4())
        first_assistant, first_result, second_assistant, second_result = [
            str(uuid4()) for _ in range(4)
        ]
        mismatched_assistant, mismatched_result = str(uuid4()), str(uuid4())
        first_target, second_target, wrong_target = [str(uuid4()) for _ in range(3)]

        def cancel_call(entry_id, parent_id, target, stamp):
            return SessionMessageEntry(
                session_id=session_id, id=entry_id, parent_id=parent_id,
                run_id=run_id, type="message",
                messages=[AssistantMessage(
                    role="assistant",
                    content=[ToolCall(
                        type="toolCall", id=call_id,
                        name="cancel_pending_proposal",
                        arguments=PendingProposalArguments(
                            business_kind="profile", proposal_id=target
                        ).model_dump(mode="json"),
                    )],
                    api="openai-completions",
                    provider="evaluation",
                    model="evaluation-model",
                    usage=Usage(
                        input=0, output=0, cache_read=0, cache_write=0, total_tokens=0
                    ),
                    stop_reason="toolUse",
                    timestamp=stamp,
                )],
                created_at=stamp,
            )

        def cancel_result(entry_id, parent_id, target, stamp):
            return SessionMessageEntry(
                session_id=session_id, id=entry_id, parent_id=parent_id,
                run_id=run_id, type="message",
                messages=[ToolResultMessage(
                    role="toolResult",
                    tool_call_id=call_id,
                    tool_name="cancel_pending_proposal",
                    content=[TextContent(
                        type="text",
                        text=PendingProposalCancellation(
                            business_kind="profile", proposal_id=target
                        ).model_dump_json(),
                    )],
                    is_error=False,
                    timestamp=stamp,
                )],
                created_at=stamp,
            )

        async with runtime.session_repository.transaction():
            await runtime.session_repository.insert_entry(SessionMessageEntry(
                session_id=session_id, id=request_id, parent_id=None, run_id=None,
                type="message",
                messages=[UserMessage(
                    role="user", content="取消这两份画像", timestamp=now
                )],
                created_at=now,
            ))
            await runtime.session_repository.insert_run(SessionRun(
                session_id=session_id, id=run_id, request_entry_id=request_id,
                last_entry_id=None, status="running", started_at=now, finished_at=None,
                error_code=None, error_message=None, stop_reason=None,
                stop_diagnostic=None,
            ))
            for entry in (
                cancel_call(first_assistant, request_id, first_target, now + 1),
                cancel_result(first_result, first_assistant, first_target, now + 2),
                cancel_call(second_assistant, first_result, second_target, now + 3),
                cancel_result(second_result, second_assistant, second_target, now + 4),
                cancel_call(mismatched_assistant, second_result, first_target, now + 5),
                cancel_result(mismatched_result, mismatched_assistant, wrong_target, now + 6),
            ):
                await runtime.session_repository.insert_entry(entry)
            session = await runtime.service.queries.require_session(session_id)
            await set_leaf(runtime.session_repository, session, mismatched_result, now + 6)
        evidence = await read_cancel_evidence(runtime.service.queries, [session_id])
        assert len(evidence) == 2
        by_source = {item.source_entry_id: item for item in evidence}
        assert set(by_source) == {first_assistant, second_assistant}
        assert by_source[first_assistant].result_entry_id == first_result
        assert by_source[first_assistant].proposal_id == first_target
        assert by_source[second_assistant].result_entry_id == second_result
        assert by_source[second_assistant].proposal_id == second_target
        assert {item.run_id for item in evidence} == {run_id}
        assert all(item.tool_call_id == call_id for item in evidence)


def test_current_input_text_uses_projected_user_items():
    items = [
        ContextItem(
            source="system_prompt", content="系统提示",
            fact_origin="system", message_role="system",
        ),
        ContextItem(
            source="attachment", content="看一下这份文件\n\n[附件] 计划.md: a.md",
            fact_origin="user", message_entry_id="entry-attachment",
            message_role="user",
        ),
        ContextItem(
            source="business_projection", content="业务资料",
            fact_origin="system", message_entry_id="entry-projection",
            message_role="user",
        ),
        ContextItem(
            source="prior_message", content="更早的回合",
            fact_origin="user", message_entry_id="entry-history",
            message_role="user",
        ),
        ContextItem(
            source="user_message", content="补充说明",
            fact_origin="user", message_entry_id="entry-steering",
            message_role="user",
        ),
    ]
    assert _current_input_text(items, {"entry-attachment", "entry-steering"}) == (
        "看一下这份文件\n\n[附件] 计划.md: a.md\n\n补充说明"
    )
    assert _current_input_text(items, {"entry-projection", "entry-history"}) == ""
