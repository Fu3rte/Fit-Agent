import json
import shutil
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from evaluation import langsmith_scenarios as ls
from evaluation.contracts import (
    AnswerEvidence,
    BusinessProjection,
    ContextItem,
    DatasetIdentity,
    EvidenceLink,
    GenerationCheckpoint,
    GenerationReference,
    MetricApplicability,
    ModelInputContext,
    RequestBoundaryEvidence,
    RerankEvidence,
    RerankIdentityEvidence,
    RerankScoredEvidence,
    RerankUsageEvidence,
    UserInputEvidence,
)
from evaluation.dataset import load_for_evaluation
from evaluation.scenarios import (
    DEFAULT_VERSION_DIR,
    EXTERNAL_PROTOCOL_VERSION,
    EXTERNAL_SCORING_FILENAME,
    RAGAS_PROTOCOL_VERSION,
    SCENARIO_ROOT,
    EventCapture,
    ExecutionPlan,
    ExternalScenarioScoring,
    GenerationCheckpointEvidence,
    RunSummary,
    ScenarioArtifact,
    ScenarioBlocked,
    ScenarioExecution,
    ScoringResultExists,
    SearchStageRecord,
    _parse_args,
    _seal_execution,
    execute_scenario,
    load_execution,
    load_external_scoring,
    run_scenario,
    score_execution_external,
    write_external_scoring_result,
)


@pytest.fixture(scope="module")
def dataset():
    return load_for_evaluation(DEFAULT_VERSION_DIR)


@pytest.fixture
def root():
    path = SCENARIO_ROOT / "tests" / uuid4().hex
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def make_scenario(
    events,
    *,
    assertions=None,
    metric_applicability=None,
):
    return {
        "scenario_id": "s-external-mode",
        "input": {
            "message": "",
            "prior_messages": [],
            "attachments": [],
        },
        "initial_state": {
            "profile": None,
            "plans": [],
            "workouts": [],
            "now": "2026-01-01T00:00:00+00:00",
        },
        "events": list(events),
        "reference": [],
        "rubric": [],
        "assertions": list(
            assertions
            if assertions is not None
            else [
                {
                    "checkpoint_id": "cp1",
                    "event_id": "e01",
                    "kind": "state_unchanged",
                    "business_kind": "all",
                }
            ]
        ),
        "metric_applicability": list(metric_applicability or []),
        "tags": ["synthetic"],
        "family": "synthetic",
        "review": {},
    }


def link(run_id="run-ext", scenario_id="s-ext", event_id="e01", checkpoint_id="cp1"):
    return EvidenceLink(
        run_id=run_id,
        scenario_id=scenario_id,
        event_id=event_id,
        checkpoint_id=checkpoint_id,
    )


def bind_dataset(dataset) -> DatasetIdentity:
    identity = dataset.identity()
    return DatasetIdentity(
        version=identity["version"],
        manifest_sha256=identity["manifest_sha256"],
        content_sha256=identity["content_sha256"],
        corpus_sha256=identity["corpus_sha256"],
    )


def make_generation_execution(dataset) -> ScenarioExecution:
    run_id = "run-ext"
    scenario_id = "s-ext"
    event_id = "e01"
    checkpoint_id = "cp1"
    event = EventCapture(
        run_id=run_id,
        scenario_id=scenario_id,
        event_id=event_id,
        event_type="session_switch",
        source_event_id=None,
        session_id="session-1",
        operation_id=None,
        request_entry_id=None,
        terminal_status="completed",
        terminal_event="session_end",
        before=BusinessProjection(),
        after=BusinessProjection(),
    )
    checkpoint = GenerationCheckpoint(
        checkpoint=link(run_id, scenario_id, event_id, checkpoint_id),
        user_input=UserInputEvidence(message="问题"),
        answer=AnswerEvidence(visible_text="回答", structured_snapshot={"stop": True}),
        model_input=ModelInputContext(
            items=[
                ContextItem(source="user_message", content="问题", fact_origin="user")
            ]
        ),
        reference=GenerationReference(answer="参考"),
        applicability=[
            MetricApplicability(metric="faithfulness", applicable=True),
            MetricApplicability(metric="factual_correctness", applicable=True),
        ],
    )
    boundary = RequestBoundaryEvidence(
        api="openai-chat",
        session_id="session-1",
        run_id=run_id,
        request_entry_id=None,
        nodes=[],
        tool_nodes=[],
    )
    retrieval = SearchStageRecord(
        sequence=0,
        run_id=run_id,
        scenario_id=scenario_id,
        event_id=event_id,
        tool_call_id="call-1",
        index_collection="fit-agent-exercises",
        index_manifest_sha256="c" * 64,
        index_manifest_json='{"build_id":"build-1"}',
        arguments={"query": "训练"},
        candidate_pool=["0001"],
        final_model_input=["0001"],
        response_json={"items": [], "has_more": False},
        scores=[0.5],
        threshold=0.0,
        experiment="rerank",
        rerank=RerankEvidence(
            mode="rerank",
            identity=RerankIdentityEvidence(
                model="reranker-1",
                endpoint="d" * 64,
                text_version="text-1",
            ),
            scored=[
                RerankScoredEvidence(id="0001", rerank_score=0.9, rrf_rank=0)
            ],
            usage=RerankUsageEvidence(
                requests=1, prompt_tokens=3, total_tokens=3
            ),
            request_id="request-1",
        ),
    )
    return _seal_execution(ScenarioExecution(
        run_id=run_id,
        scenario_id=scenario_id,
        dataset=bind_dataset(dataset),
        scenario_sha256="b" * 64,
        execution_sha256="0" * 64,
        status="completed",
        session_ids=["session-1"],
        initial_state_sha256="a" * 64,
        initial_projection=BusinessProjection(),
        expected_event_ids=[event_id],
        expected_checkpoint_ids=[checkpoint_id],
        events=[event],
        generation_checkpoints=[
            GenerationCheckpointEvidence(
                checkpoint_id=checkpoint_id,
                event_id=event_id,
                applicability=checkpoint.applicability,
                checkpoint=checkpoint,
                request_boundary=boundary,
            )
        ],
        retrieval=[retrieval],
        proposal_bindings={},
        generation_metrics={
            checkpoint_id: ["faithfulness", "factual_correctness"]
        },
    ))


def make_missing_generation_execution(dataset) -> ScenarioExecution:
    return _seal_execution(ScenarioExecution(
        run_id="run-ext",
        scenario_id="s-ext",
        dataset=bind_dataset(dataset),
        scenario_sha256="b" * 64,
        execution_sha256="0" * 64,
        status="blocked",
        error="测试阻断：生成检查点未产生",
        session_ids=[],
        initial_state_sha256="a" * 64,
        expected_event_ids=["e01"],
        expected_checkpoint_ids=["cp1"],
        events=[],
        generation_metrics={"cp1": ["faithfulness", "factual_correctness"]},
        proposal_bindings={},
    ))


def write_execution(root: Path, execution: ScenarioExecution) -> Path:
    path = root / execution.scenario_id / ls.EXECUTION_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(execution.model_dump_json(), encoding="utf-8")
    return path


def test_cli_scoring_mode_is_explicit_and_defaults_to_ragas():
    assert _parse_args([]).scoring_mode == "ragas"
    assert _parse_args(["--scoring-mode", "external"]).scoring_mode == "external"
    with pytest.raises(SystemExit):
        _parse_args(["--scoring-mode", "auto"])


def test_plan_records_mode_and_old_plan_defaults_to_ragas(dataset):
    scenario = make_scenario([{"event_id": "e01", "type": "session_switch"}])
    from evaluation.scenarios import build_execution_plan

    external = build_execution_plan(dataset, [scenario], "run-ext", "external")
    assert external.scoring_mode == "external"
    assert external.protocol_version == EXTERNAL_PROTOCOL_VERSION
    legacy_payload = external.model_dump(mode="json")
    legacy_payload.pop("scoring_mode")
    legacy_payload.pop("protocol_version")
    legacy = ExecutionPlan.model_validate(legacy_payload)
    assert legacy.scoring_mode == "ragas"
    assert legacy.protocol_version == RAGAS_PROTOCOL_VERSION
    summary = RunSummary(
        run_id="run-ext",
        purpose="validation",
        dataset={},
        identity={},
        coverage={
            "expected_scenarios": 0,
            "observed_scenarios": 0,
            "expected_events": 0,
            "observed_events": 0,
            "expected_checkpoints": 0,
            "observed_checkpoints": 0,
            "expected_metrics": 0,
            "observed_metrics": 0,
        },
        scenarios=[],
    )
    restored = RunSummary.model_validate(
        {
            key: value
            for key, value in summary.model_dump(mode="json").items()
            if key not in ("scoring_mode", "protocol_version")
        }
    )
    assert restored.scoring_mode == "ragas"


async def test_external_run_skips_ragas_and_writes_local_result(
    dataset, root, monkeypatch
):
    scenario = make_scenario(
        [{"event_id": "e01", "type": "session_switch"}],
        metric_applicability=[
            {
                "checkpoint_id": "cp1",
                "event_id": "e01",
                "metric": "faithfulness",
                "applicable": False,
                "reason": "纯状态检查点，没有模型生成内容",
            },
            {
                "checkpoint_id": "cp1",
                "event_id": "e01",
                "metric": "factual_correctness",
                "applicable": False,
                "reason": "纯状态检查点，没有模型生成内容",
            },
        ],
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("外部模式不得进入 Ragas 评分")

    monkeypatch.setattr("evaluation.scenarios._score_generation_entries", forbidden)
    monkeypatch.setattr("evaluation.scenarios.GenerationScoringSession", forbidden)

    record = await run_scenario(
        dataset,
        scenario,
        run_id="run-ext",
        root=root,
        enable_model=True,
        judge=None,
        judge_timeout=None,
        scoring_mode="external",
    )
    assert isinstance(record, ExternalScenarioScoring)
    scenario_root = root / scenario["scenario_id"]
    assert (scenario_root / EXTERNAL_SCORING_FILENAME).is_file()
    assert (scenario_root / "execution.json").is_file()
    assert (scenario_root / "assertions.jsonl").is_file()
    assert not (scenario_root / "scenario.json").exists()
    assert not (scenario_root / "generation.jsonl").exists()
    assert not (scenario_root / "scoring.json").exists()
    loaded = load_external_scoring(
        scenario_root, expected_execution_sha256=record.execution_sha256
    )
    assert loaded == record
    assert record.scoring_mode == "external"
    assert record.protocol_version == EXTERNAL_PROTOCOL_VERSION
    assert record.execution_status == "completed"
    assert record.assertion_status == "passed"
    assert record.generation_scoring_status == "pending_external"
    assert record.applicability_status == "awaiting_external_applicability"
    assert [item.status for item in record.generation] == [
        "no_generation_evidence"
    ]
    assert record.incomplete == []
    execution = load_execution(scenario_root)
    assert execution.execution_sha256 == record.execution_sha256


async def test_external_run_keeps_failed_assertion_real(dataset, root):
    scenario = make_scenario(
        [{"event_id": "e01", "type": "session_switch"}],
        assertions=[
            {
                "checkpoint_id": "cp1",
                "event_id": "e01",
                "kind": "saved_state",
                "business_kind": "profile",
            }
        ],
    )
    record = await run_scenario(
        dataset,
        scenario,
        run_id="run-ext",
        root=root,
        enable_model=True,
        judge=None,
        judge_timeout=None,
        scoring_mode="external",
    )
    assert record.execution_status == "completed"
    assert record.assertion_status == "failed"
    assert record.assertions[0].outcomes[0].passed is False
    assert any("assertion_failed" in item for item in record.incomplete)
    assert record.generation_scoring_status == "pending_external"


async def test_external_run_keeps_binding_error(dataset, root):
    scenario = make_scenario(
        [{"event_id": "e01", "type": "session_switch"}],
        assertions=[
            {
                "checkpoint_id": "cp1",
                "event_id": "e01",
                "kind": "snapshot_integrity",
                "proposal_ref": "profile-proposal-00000000-0000-0000-0000-000000000000",
            }
        ],
    )
    record = await run_scenario(
        dataset,
        scenario,
        run_id="run-ext",
        root=root,
        enable_model=True,
        judge=None,
        judge_timeout=None,
        scoring_mode="external",
    )
    assert record.assertion_status == "binding_error"
    assert record.assertions[0].binding_error is not None
    assert any("binding_error" in item for item in record.incomplete)
    assert record.generation_scoring_status == "pending_external"


async def test_external_blocked_execution_is_not_scoring_success(dataset, root):
    scenario = make_scenario(
        [
            {"event_id": "e01", "type": "session_switch"},
            {"event_id": "e02", "type": "bogus"},
        ]
    )
    with pytest.raises(ScenarioBlocked) as raised:
        await run_scenario(
            dataset,
            scenario,
            run_id="run-ext",
            root=root,
            enable_model=True,
            judge=None,
            judge_timeout=None,
            scoring_mode="external",
        )
    scenario_root = root / scenario["scenario_id"]
    record = load_external_scoring(scenario_root)
    assert record.execution_status == "blocked"
    assert record.blocked is not None
    assert record.runner_error is None
    assert record.assertion_status == "passed"
    assert record.generation_scoring_status == "pending_external"
    blocked = json.loads((scenario_root / "blocked.json").read_text(encoding="utf-8"))
    assert blocked["scoring_mode"] == "external"
    assert blocked["execution_status"] == "blocked"
    assert blocked["generation_scoring_status"] == "pending_external"
    assert not (scenario_root / "partial.json").exists()
    assert not (scenario_root / "failure.json").exists()
    assert raised.value.external_outcome is not None


def test_missing_generation_checkpoint_is_incomplete_not_success(dataset):
    execution = make_missing_generation_execution(dataset)
    record = score_execution_external(execution, [])
    assert record.execution_status == "blocked"
    assert record.generation_scoring_status == "incomplete"
    assert [item.status for item in record.generation] == ["missing"]
    assert record.generation[0].missing == ["generation_checkpoint"]
    assert any(
        "generation_evidence_missing" in item for item in record.incomplete
    )
    assert record.applicability_status == "awaiting_external_applicability"


def test_external_scoring_preserves_sealed_execution_evidence(dataset, tmp_path):
    execution = make_generation_execution(dataset)
    path = write_execution(tmp_path, execution)
    before = path.read_bytes()
    loaded = load_execution(path)
    assert loaded.retrieval[0].rerank is not None
    assert loaded.retrieval[0].rerank.request_id == "request-1"
    assert loaded.generation_checkpoints[0].request_boundary is not None
    assert (
        loaded.generation_checkpoints[0].checkpoint.answer.structured_snapshot
        == {"stop": True}
    )
    record = score_execution_external(loaded, [])
    assert record.execution_sha256 == loaded.execution_sha256
    assert [item.status for item in record.generation] == ["ready"]
    assert record.generation_scoring_status == "pending_external"
    assert record.incomplete == []
    repeated = score_execution_external(load_execution(path), [])
    assert repeated.model_dump(mode="json") == record.model_dump(mode="json")
    assert path.read_bytes() == before
    write_external_scoring_result(
        loaded, record, path=tmp_path / "s-ext" / EXTERNAL_SCORING_FILENAME
    )
    with pytest.raises(ScoringResultExists):
        write_external_scoring_result(
            loaded, record, path=tmp_path / "s-ext" / EXTERNAL_SCORING_FILENAME
        )


async def _execute_layout(dataset, run_dir, scenario):
    example_id = str(uuid5(NAMESPACE_URL, f"external-{uuid4().hex}"))
    execution = await execute_scenario(
        dataset,
        scenario,
        run_id="run-sdk-offline",
        root=run_dir / "scenarios" / example_id,
        enable_model=True,
    )
    return example_id, execution


def _synthetic_row(dataset, scenario, example_id):
    from evaluation.scenarios import frozen_scenario_sha256

    binding = ls.dataset_binding(dataset)
    scenario_sha256 = frozen_scenario_sha256(scenario)
    return {
        "id": example_id,
        "inputs": {
            "scenario_id": scenario["scenario_id"],
            "scenario_sha256": scenario_sha256,
            "dataset": binding,
        },
        "outputs": {
            "scenario_id": scenario["scenario_id"],
            "scenario_sha256": scenario_sha256,
            "dataset": binding,
            "assertions": [dict(row) for row in scenario["assertions"]],
        },
        "metadata": {},
    }


async def _external_evaluator_run(dataset, run_dir, scenario, monkeypatch):
    example_id, execution = await _execute_layout(dataset, run_dir, scenario)
    summary = ls.execution_summary(
        execution,
        run_id="run-sdk-offline",
        example_id=example_id,
        scenario_id=scenario["scenario_id"],
        dataset=dataset,
    )
    row = _synthetic_row(dataset, scenario, example_id)
    monkeypatch.setattr(ls, "judge_config", lambda: (_ for _ in ()).throw(
        AssertionError("外部模式不得读取本地裁判配置")
    ))

    async def forbidden(*args, **kwargs):
        raise AssertionError("外部模式不得进入 Ragas 评分")

    monkeypatch.setattr(ls, "score_execution", forbidden)
    evaluator = ls.make_evaluator(
        dataset=dataset,
        rows={scenario["scenario_id"]: row},
        run_id="run-sdk-offline",
        run_dir=run_dir,
        judge=None,
        scoring_mode="external",
    )
    return example_id, execution, row, summary, evaluator


async def test_external_evaluator_writes_and_reuses_local_result(
    dataset, tmp_path, monkeypatch
):
    scenario = make_scenario(
        [{"event_id": "e01", "type": "session_switch"}],
        metric_applicability=[
            {
                "checkpoint_id": "cp1",
                "event_id": "e01",
                "metric": "faithfulness",
                "applicable": False,
                "reason": "纯状态检查点，没有模型生成内容",
            },
            {
                "checkpoint_id": "cp1",
                "event_id": "e01",
                "metric": "factual_correctness",
                "applicable": False,
                "reason": "纯状态检查点，没有模型生成内容",
            },
        ],
    )
    run_dir = tmp_path / "run"
    example_id, execution, row, summary, evaluator = await _external_evaluator_run(
        dataset, run_dir, scenario, monkeypatch
    )
    scenario_root = run_dir / "scenarios" / example_id / scenario["scenario_id"]
    execution_before = (scenario_root / ls.EXECUTION_NAME).read_bytes()
    first = await evaluator(row["inputs"], summary, row["outputs"])
    feedback = {item["key"]: item["score"] for item in first["results"]}
    assert set(feedback) == set(ls.EXTERNAL_FEEDBACK_FIELDS)
    assert feedback["execution_completed"] == 1.0
    assert feedback["assertion_passed"] == 1.0
    assert feedback["generation_scoring_completed"] == 0.0
    assert (scenario_root / ls.EXTERNAL_SCORING_NAME).is_file()
    assert (scenario_root / ls.EXTERNAL_EVALUATION_NAME).is_file()
    assert not (scenario_root / ls.SCORING_NAME).exists()
    evaluation = json.loads(
        (scenario_root / ls.EXTERNAL_EVALUATION_NAME).read_text(encoding="utf-8")
    )
    assert evaluation["feedback_version"] == ls.EXTERNAL_FEEDBACK_VERSION
    assert evaluation["scoring_mode"] == "external"
    assert evaluation["execution_sha256"] == execution.execution_sha256
    assert evaluation["generation_scoring_status"] == "pending_external"
    assert evaluation["reused"] is False
    state = json.loads(
        (scenario_root / ls.EXTERNAL_SCORING_STATE_NAME).read_text(encoding="utf-8")
    )
    assert state["scoring_mode"] == "external"
    scoring_bytes = (scenario_root / ls.EXTERNAL_SCORING_NAME).read_bytes()
    second = await evaluator(row["inputs"], summary, row["outputs"])
    assert second == first
    assert (scenario_root / ls.EXTERNAL_SCORING_NAME).read_bytes() == scoring_bytes
    evaluation = json.loads(
        (scenario_root / ls.EXTERNAL_EVALUATION_NAME).read_text(encoding="utf-8")
    )
    assert evaluation["reused"] is True
    assert (scenario_root / ls.EXECUTION_NAME).read_bytes() == execution_before
    with pytest.raises(ls.ScenarioSDKError, match="execution_sha256"):
        await evaluator(
            row["inputs"],
            {**summary, "execution_sha256": "0" * 64},
            row["outputs"],
        )


async def test_external_mode_does_not_reuse_or_overwrite_ragas_files(
    dataset, tmp_path, monkeypatch
):
    scenario = make_scenario(
        [{"event_id": "e01", "type": "session_switch"}],
        metric_applicability=[
            {
                "checkpoint_id": "cp1",
                "event_id": "e01",
                "metric": "faithfulness",
                "applicable": False,
                "reason": "纯状态检查点，没有模型生成内容",
            },
            {
                "checkpoint_id": "cp1",
                "event_id": "e01",
                "metric": "factual_correctness",
                "applicable": False,
                "reason": "纯状态检查点，没有模型生成内容",
            },
        ],
    )
    run_dir = tmp_path / "run"
    example_id, execution, row, summary, evaluator = await _external_evaluator_run(
        dataset, run_dir, scenario, monkeypatch
    )
    scenario_root = run_dir / "scenarios" / example_id / scenario["scenario_id"]
    ragas_path = scenario_root / ls.SCORING_NAME
    ragas_path.write_text('{"ragas": true}\n', encoding="utf-8")
    ragas_bytes = ragas_path.read_bytes()
    state_path = scenario_root / ls.EXTERNAL_SCORING_STATE_NAME
    state_path.write_text(
        json.dumps({"status": "running", "scoring_mode": "ragas"}),
        encoding="utf-8",
    )
    with pytest.raises(ls.ScoringBlocked, match="其他模式"):
        ls.scoring_guard(
            scenario_root / ls.EXTERNAL_SCORING_NAME,
            state_path,
            scoring_mode="external",
        )
    state_path.unlink()
    await evaluator(row["inputs"], summary, row["outputs"])
    assert ragas_path.read_bytes() == ragas_bytes
    artifact = ScenarioArtifact(
        run_id=execution.run_id,
        scenario_id=execution.scenario_id,
        session_ids=list(execution.session_ids),
        events=list(execution.events),
        assertions=[],
        generation=[],
        proposal_bindings={},
    )
    fake_external = scenario_root / "fake-external.json"
    fake_external.write_text(artifact.model_dump_json(), encoding="utf-8")
    with pytest.raises(ls.ScoringBlocked, match="外部评分产物"):
        ls.verify_existing_external_scoring(execution, fake_external)


def _external_run_document(evaluation, example_id, scenario_id):
    sample = {
        "example_id": example_id,
        "scenario_id": scenario_id,
        "sdk_run_id": "sdk-run-external",
        "status": "complete",
        "execution_status": evaluation["execution_status"],
        "assertion_status": evaluation["assertion_status"],
        "generation_scoring_status": evaluation["generation_scoring_status"],
        "execution_sha256": evaluation["execution_sha256"],
        "scoring_relative": evaluation["scoring_relative"],
        "scoring_sha256": evaluation["scoring_sha256"],
        "feedback": evaluation["feedback"],
        "incomplete": evaluation["incomplete"],
        "feedback_version": ls.EXTERNAL_FEEDBACK_VERSION,
        "scoring_mode": "external",
        "run_error": None,
        "run_start_time": "2026-01-01T00:00:00+00:00",
        "run_end_time": "2026-01-01T00:01:00+00:00",
    }
    return {
        "schema": ls.SCHEMA,
        "run_id": "run-sdk-offline",
        "feedback_version": ls.EXTERNAL_FEEDBACK_VERSION,
        "scoring_mode": "external",
        "protocol_version": EXTERNAL_PROTOCOL_VERSION,
        "status": "running",
        "coverage": {
            "status": "complete",
            "expected_runs": 1,
            "observed_runs": 1,
            "extra_example_ids": [],
        },
        "samples": [sample],
        "issues": [],
        "scenarios": [],
    }


async def test_finalize_external_keeps_generation_scoring_pending(
    dataset, tmp_path, monkeypatch
):
    scenario = make_scenario(
        [{"event_id": "e01", "type": "session_switch"}],
        metric_applicability=[
            {
                "checkpoint_id": "cp1",
                "event_id": "e01",
                "metric": "faithfulness",
                "applicable": False,
                "reason": "纯状态检查点，没有模型生成内容",
            },
            {
                "checkpoint_id": "cp1",
                "event_id": "e01",
                "metric": "factual_correctness",
                "applicable": False,
                "reason": "纯状态检查点，没有模型生成内容",
            },
        ],
    )
    run_dir = tmp_path / "run"
    example_id, _, row, summary, evaluator = await _external_evaluator_run(
        dataset, run_dir, scenario, monkeypatch
    )
    await evaluator(row["inputs"], summary, row["outputs"])
    scenario_root = run_dir / "scenarios" / example_id / scenario["scenario_id"]
    evaluation = json.loads(
        (scenario_root / ls.EXTERNAL_EVALUATION_NAME).read_text(encoding="utf-8")
    )
    document = _external_run_document(
        evaluation, example_id, scenario["scenario_id"]
    )
    (run_dir / ls.SDK_RUN_NAME).write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    finalized = ls.finalize_run(run_dir)
    assert finalized["status"] == ls.EXTERNAL_RUN_STATUS
    assert finalized["scoring_mode"] == "external"
    assert finalized["protocol_version"] == EXTERNAL_PROTOCOL_VERSION
    sample = finalized["samples"][0]
    assert sample["generation_scoring_status"] == "pending_external"
    assert sample["feedback"]["generation_scoring_completed"] == 0.0
    assert sample["feedback"] == evaluation["feedback"]
    assert sample["incomplete"] == evaluation["incomplete"]
    record = finalized["scenarios"][0]
    assert record["scoring_mode"] == "external"
    assert record["assertions"]["passed"] == 1


async def test_finalize_external_failed_assertion_is_incomplete(
    dataset, tmp_path, monkeypatch
):
    scenario = make_scenario(
        [{"event_id": "e01", "type": "session_switch"}],
        assertions=[
            {
                "checkpoint_id": "cp1",
                "event_id": "e01",
                "kind": "saved_state",
                "business_kind": "profile",
            }
        ],
    )
    run_dir = tmp_path / "run"
    example_id, _, row, summary, evaluator = await _external_evaluator_run(
        dataset, run_dir, scenario, monkeypatch
    )
    await evaluator(row["inputs"], summary, row["outputs"])
    scenario_root = run_dir / "scenarios" / example_id / scenario["scenario_id"]
    evaluation = json.loads(
        (scenario_root / ls.EXTERNAL_EVALUATION_NAME).read_text(encoding="utf-8")
    )
    document = _external_run_document(
        evaluation, example_id, scenario["scenario_id"]
    )
    (run_dir / ls.SDK_RUN_NAME).write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(ls, "judge_config", lambda: (_ for _ in ()).throw(
        AssertionError("finalize 不得读取裁判配置")
    ))
    finalized = ls.finalize_run(run_dir)
    assert finalized["status"] == "incomplete"
    assert finalized["samples"][0]["incomplete"]
    assert finalized["samples"][0]["feedback"]["assertions_failed"] == 1.0


def test_old_scenario_summary_rejects_external_run(tmp_path):
    from evaluation.summary import SummaryError, load_run

    run_dir = tmp_path / "external-run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": "run-ext",
                "scoring_mode": "external",
                "protocol_version": EXTERNAL_PROTOCOL_VERSION,
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "plan.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SummaryError, match="SDK 目录布局"):
        load_run(run_dir)
    (run_dir / "run.json").write_text(
        json.dumps({"run_id": "run-ext", "scoring_mode": "external"}),
        encoding="utf-8",
    )
    with pytest.raises(SummaryError, match="不一致"):
        load_run(run_dir)


def test_old_langsmith_summary_rejects_external_protocol(dataset, tmp_path):
    from evaluation.sdk_summary import load_scenario_source

    path = tmp_path / ls.SDK_RUN_NAME
    path.write_text(
        json.dumps(
            {
                "schema": ls.SCHEMA,
                "run_id": "run-ext",
                "purpose": "validation",
                "feedback_version": ls.EXTERNAL_FEEDBACK_VERSION,
                "scoring_mode": "external",
            }
        ),
        encoding="utf-8",
    )
    source = load_scenario_source(path, dataset)
    assert source.available is False
    assert source.feedback_version == ls.EXTERNAL_FEEDBACK_VERSION
    assert any("协议分发" in item for item in source.issues)
