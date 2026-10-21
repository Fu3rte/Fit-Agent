import json
import shutil
from uuid import uuid4

import pytest
from pydantic import ValidationError

from evaluation.dataset import load_for_evaluation
from evaluation.scenario_evidence import canonical_json, sha256_text
from evaluation.scenarios import (
    DEFAULT_VERSION_DIR,
    SCENARIO_ROOT,
    ExecutionIdentityError,
    ScenarioBlocked,
    ScenarioExecution,
    ScenarioModelUnavailable,
    ScoringResultExists,
    execute_scenario,
    load_execution,
    run_scenario,
    score_execution,
    write_scoring_result,
)
from evaluation.summary import _read_artifact


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
    prior_messages=None,
):
    return {
        "scenario_id": "s-task2-synthetic",
        "input": {
            "message": "",
            "prior_messages": list(prior_messages or []),
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


async def execute(root, dataset, scenario, run_id="run-task2"):
    return await execute_scenario(
        dataset,
        scenario,
        run_id=run_id,
        root=root,
        enable_model=True,
    )


async def test_execute_persists_and_reloads_boundary_evidence(dataset, root):
    scenario = make_scenario([
        {"event_id": "e01", "type": "session_switch"},
        {"event_id": "e02", "type": "restart"},
    ], assertions=[
        {
            "checkpoint_id": "cp1",
            "event_id": "e01",
            "kind": "state_unchanged",
            "business_kind": "all",
        },
        {
            "checkpoint_id": "cp2",
            "event_id": "e02",
            "kind": "state_unchanged",
            "business_kind": "all",
        },
    ])
    execution = await execute(root, dataset, scenario)
    scenario_root = root / scenario["scenario_id"]
    assert (scenario_root / "execution.json").is_file()
    assert not (scenario_root / "scenario.json").exists()
    loaded = load_execution(
        scenario_root,
        expected_run_id="run-task2",
        expected_scenario_id=scenario["scenario_id"],
    )
    assert loaded.execution_sha256 == execution.execution_sha256
    assert loaded.dataset == execution.dataset
    assert loaded.status == "completed"
    assert [event.event_id for event in loaded.events] == ["e01", "e02"]
    assert [entry.checkpoint_id for entry in loaded.assertion_checkpoints] == [
        "cp1",
        "cp2",
    ]
    for entry in loaded.assertion_checkpoints:
        event = next(item for item in loaded.events if item.event_id == entry.event_id)
        assert entry.evidence is not None
        assert entry.evidence.checkpoint.scenario_id == scenario["scenario_id"]
        assert entry.evidence.checkpoint.run_id == "run-task2"
        assert entry.evidence.before == event.before
        assert entry.evidence.after == event.after
    artifact = await score_execution(loaded, judge=None, judge_timeout=None)
    assert [outcome.passed for row in artifact.assertions for outcome in row.outcomes] == [
        True,
        True,
    ]


async def test_scoring_reads_disk_evidence_without_database(dataset, root):
    scenario = make_scenario([
        {"event_id": "e01", "type": "session_switch"},
    ])
    execution = await execute(root, dataset, scenario)
    loaded = load_execution(root / scenario["scenario_id"] / "execution.json")
    database = root / scenario["scenario_id"] / "fit-agent.db"
    moved = database.with_name("fit-agent.db.removed")
    database.rename(moved)
    artifact = await score_execution(loaded, judge=None, judge_timeout=None)
    assert artifact.assertions[0].outcomes[0].passed is True
    assert not database.exists()
    assert moved.exists()
    assert execution.execution_sha256 == loaded.execution_sha256


async def test_identity_tamper_and_link_mismatch_are_rejected(dataset, root):
    scenario = make_scenario([
        {"event_id": "e01", "type": "session_switch"},
    ])
    await execute(root, dataset, scenario)
    path = root / scenario["scenario_id"] / "execution.json"
    with pytest.raises(ExecutionIdentityError):
        load_execution(path, expected_run_id="other-run")

    payload = json.loads(path.read_text(encoding="utf-8"))
    tampered = json.loads(json.dumps(payload))
    tampered["events"][0]["session_id"] = "tampered"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ExecutionIdentityError):
        load_execution(path)

    mismatched = json.loads(json.dumps(payload))
    mismatched["assertion_checkpoints"][0]["evidence"]["checkpoint"][
        "scenario_id"
    ] = "other-scenario"
    digest = mismatched.pop("execution_sha256")
    mismatched["execution_sha256"] = sha256_text(canonical_json(mismatched))
    assert digest != mismatched["execution_sha256"]
    with pytest.raises(ValidationError):
        ScenarioExecution.model_validate(mismatched)


async def test_partial_execution_keeps_events_and_scores_after_failure(dataset, root):
    scenario = make_scenario([
        {"event_id": "e01", "type": "session_switch"},
        {"event_id": "e02", "type": "bogus"},
    ])
    with pytest.raises(ScenarioBlocked) as raised:
        await execute(root, dataset, scenario)
    execution = raised.value.execution
    assert execution is not None
    assert execution.status == "blocked"
    loaded = load_execution(root / scenario["scenario_id"])
    assert [event.event_id for event in loaded.events] == ["e01"]
    assert [entry.checkpoint_id for entry in loaded.assertion_checkpoints] == ["cp1"]
    artifact = await score_execution(loaded, judge=None, judge_timeout=None)
    assert artifact.blocked is not None
    assert artifact.runner_error is None
    assert len(artifact.events) == 1
    assert artifact.assertions[0].outcomes[0].passed is True


async def test_run_scenario_composes_execution_and_scoring(dataset, root):
    scenario = make_scenario([
        {"event_id": "e01", "type": "session_switch"},
        {"event_id": "e02", "type": "restart"},
    ])
    artifact = await run_scenario(
        dataset,
        scenario,
        run_id="run-task2",
        root=root,
        enable_model=True,
        judge=None,
        judge_timeout=None,
    )
    scenario_root = root / scenario["scenario_id"]
    for name in (
        "execution.json",
        "scenario.json",
        "events.jsonl",
        "assertions.jsonl",
        "assertion-evidence.jsonl",
        "context.jsonl",
        "initial-state.json",
    ):
        assert (scenario_root / name).is_file()
    assert not (scenario_root / "generation.jsonl").exists()
    execution = load_execution(scenario_root)
    rescored = await score_execution(execution, judge=None, judge_timeout=None)
    assert rescored.model_dump(mode="json") == artifact.model_dump(mode="json")
    scoring_path = scenario_root / "scoring.json"
    write_scoring_result(execution, artifact, path=scoring_path)
    with pytest.raises(ScoringResultExists):
        write_scoring_result(execution, artifact, path=scoring_path)


async def test_pure_state_checkpoint_scores_not_applicable_without_judge(
    dataset, root
):
    scenario = make_scenario(
        [{"event_id": "e01", "type": "session_switch"}],
        metric_applicability=[
            {
                "checkpoint_id": "cp1",
                "event_id": "e01",
                "metric": "faithfulness",
                "applicable": False,
                "reason": "纯状态检查点，没有模型生成的事实内容",
            },
            {
                "checkpoint_id": "cp1",
                "event_id": "e01",
                "metric": "factual_correctness",
                "applicable": False,
                "reason": "纯状态检查点，没有模型生成的事实内容",
            },
        ],
    )
    execution = await execute(root, dataset, scenario)
    assert execution.generation_checkpoints[0].checkpoint is None
    artifact = await score_execution(execution, judge=None, judge_timeout=None)
    outcomes = artifact.generation[0].result.outcomes
    assert [(row.metric, row.status) for row in outcomes] == [
        ("faithfulness", "not_applicable"),
        ("factual_correctness", "not_applicable"),
    ]
    assert all(row.reason for row in outcomes)


async def test_applicable_metrics_require_judge_before_execution(dataset, root):
    scenario = make_scenario(
        [{"event_id": "e01", "type": "session_switch"}],
        metric_applicability=[
            {
                "checkpoint_id": "cp1",
                "event_id": "e01",
                "metric": "faithfulness",
                "applicable": True,
                "reason": None,
            },
            {
                "checkpoint_id": "cp1",
                "event_id": "e01",
                "metric": "factual_correctness",
                "applicable": True,
                "reason": None,
            },
        ],
    )
    with pytest.raises(ScenarioModelUnavailable):
        await run_scenario(
            dataset,
            scenario,
            run_id="run-task2",
            root=root,
            enable_model=True,
            judge=None,
            judge_timeout=None,
        )
    assert not (root / scenario["scenario_id"]).exists()


async def test_blocked_run_scenario_preserves_partial_artifact(dataset, root):
    scenario = make_scenario([
        {"event_id": "e01", "type": "session_switch"},
        {"event_id": "e02", "type": "bogus"},
    ])
    with pytest.raises(ScenarioBlocked) as raised:
        await run_scenario(
            dataset,
            scenario,
            run_id="run-task2",
            root=root,
            enable_model=True,
            judge=None,
            judge_timeout=None,
        )
    scenario_root = root / scenario["scenario_id"]
    assert (scenario_root / "blocked.json").is_file()
    assert (scenario_root / "failure.json").exists() is False
    partial = json.loads(
        (scenario_root / "partial.json").read_text(encoding="utf-8")
    )
    assert partial["blocked"] is not None
    assert partial["runner_error"] is None
    assert [event["event_id"] for event in partial["events"]] == ["e01"]
    assert partial["assertions"][0]["outcomes"][0]["passed"] is True
    assert raised.value.artifact is not None
    assert raised.value.observed_events == 1


async def test_execution_only_directory_is_not_a_scored_artifact(dataset, root):
    scenario = make_scenario([
        {"event_id": "e01", "type": "session_switch"},
    ])
    await execute(root, dataset, scenario)
    issues = []
    state, artifact = _read_artifact(
        root / scenario["scenario_id"],
        scenario["scenario_id"],
        issues,
        "scenario:synthetic",
    )
    assert state == "missing"
    assert artifact is None
    assert issues == []
