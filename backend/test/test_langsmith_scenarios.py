import asyncio
import copy
import json
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from app.application.session import runtime as run_runtime
from evaluation import langsmith_scenarios as ls
from evaluation.contracts import (
    AssertionOutcome,
    CheckpointResult,
    EvidenceLink,
    MetricOutcome,
)
from evaluation.dataset import BACKEND, load_for_evaluation
from evaluation.scenario_evidence import canonical_json, sha256_text
from evaluation.scenarios import (
    AssertionCheckpointArtifact,
    GenerationCheckpointArtifact,
    ScenarioArtifact,
    execute_scenario,
    frozen_scenario_sha256,
)

DATASET_PATH = BACKEND / "dataset" / "evaluation" / "rebuild-v2"


@pytest.fixture(scope="module")
def dataset():
    return load_for_evaluation(DATASET_PATH)


@pytest.fixture(scope="module")
def rows(dataset):
    return ls.example_rows(dataset)


class MemoryDataset:
    def __init__(self, name: str, metadata: dict) -> None:
        self.id = uuid4()
        self.name = name
        self.metadata = copy.deepcopy(metadata)


class MemoryExample:
    def __init__(self, row: dict) -> None:
        self.id = row["id"]
        self.inputs = copy.deepcopy(row["inputs"])
        self.outputs = copy.deepcopy(row["outputs"])
        self.metadata = copy.deepcopy(row["metadata"])


class MemoryClient:
    def __init__(self) -> None:
        self.datasets: dict[str, MemoryDataset] = {}
        self.examples: dict[str, dict[str, MemoryExample]] = {}

    def list_datasets(self, *, dataset_name=None, dataset_name_contains=None, **kwargs):
        if dataset_name is None:
            return list(self.datasets.values())
        dataset = self.datasets.get(dataset_name)
        return [dataset] if dataset is not None else []

    def read_dataset(self, *, dataset_name=None, dataset_id=None):
        if dataset_name is not None:
            dataset = self.datasets.get(dataset_name)
            if dataset is None:
                raise ls.LangSmithNotFoundError("dataset not found")
            return dataset
        for dataset in self.datasets.values():
            if str(dataset.id) == str(dataset_id):
                return dataset
        raise ls.LangSmithNotFoundError("dataset not found")

    def create_dataset(self, name, *, description=None, metadata=None, **kwargs):
        if name in self.datasets:
            raise AssertionError("重复创建同名远端数据集")
        dataset = MemoryDataset(name, metadata or {})
        self.datasets[name] = dataset
        self.examples[str(dataset.id)] = {}
        return dataset

    def create_examples(self, *, dataset_name=None, dataset_id=None, examples=None, **kwargs):
        target = self.examples[str(dataset_id)]
        for row in examples:
            if row["id"] in target:
                raise AssertionError("重复创建同一远端样例")
            target[row["id"]] = MemoryExample(row)
        return {"count": len(examples)}

    def list_examples(self, *, dataset_id=None, **kwargs):
        return list(self.examples[str(dataset_id)].values())


@pytest.fixture
def isolated_receipts(tmp_path, monkeypatch):
    monkeypatch.setattr(ls, "DATASET_ROOT", tmp_path / "datasets")
    return tmp_path


def make_scenario(scenario_id: str) -> dict:
    return {
        "scenario_id": scenario_id,
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
        "events": [
            {"event_id": "e01", "type": "session_switch"},
        ],
        "reference": [],
        "rubric": [],
        "assertions": [
            {
                "checkpoint_id": "cp1",
                "event_id": "e01",
                "kind": "state_unchanged",
                "business_kind": "all",
            }
        ],
        "metric_applicability": [
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
        "tags": ["synthetic"],
        "family": "synthetic",
        "review": {},
    }


def synthetic_row(dataset, scenario: dict, example_id: str) -> dict:
    scenario_id = scenario["scenario_id"]
    binding = ls.dataset_binding(dataset)
    scenario_sha256 = frozen_scenario_sha256(scenario)
    return {
        "id": example_id,
        "inputs": {
            "scenario_id": scenario_id,
            "scenario_sha256": scenario_sha256,
            "dataset": binding,
        },
        "outputs": {
            "scenario_id": scenario_id,
            "scenario_sha256": scenario_sha256,
            "dataset": binding,
        },
        "metadata": {},
    }


def test_example_rows_cover_all_scenarios_without_reference_in_inputs(dataset, rows):
    assert len(rows) == 80
    assert len({row["id"] for row in rows}) == 80
    scenarios = {scenario["scenario_id"]: scenario for scenario in dataset.scenarios}
    assert {row["inputs"]["scenario_id"] for row in rows} == set(scenarios)
    for row in rows:
        scenario = scenarios[row["inputs"]["scenario_id"]]
        assert set(row["inputs"]) == {
            "scenario_id",
            "scenario_sha256",
            "dataset",
        }
        assert row["inputs"]["scenario_sha256"] == frozen_scenario_sha256(scenario)
        assert row["outputs"]["reference"] == scenario["reference"]
        assert row["outputs"]["rubric"] == scenario["rubric"]
        assert row["outputs"]["assertions"] == scenario["assertions"]
        assert row["metadata"]["category"] == scenario["tags"][0]
        assert row["metadata"]["tags"] == scenario["tags"]
        assert "reference" not in json.dumps(row["inputs"], ensure_ascii=False)
        expected = sha256_text(
            canonical_json(
                {
                    "inputs": row["inputs"],
                    "outputs": row["outputs"],
                    "metadata": {
                        key: value
                        for key, value in row["metadata"].items()
                        if key != "content_sha256"
                    },
                }
            )
        )
        assert row["metadata"]["content_sha256"] == expected


def test_example_ids_and_validation_selection_are_stable(dataset, rows):
    name = ls.dataset_name(dataset)
    assert name == (
        f"fit-agent-scenario-sdk-{dataset.version}-{dataset.content_sha256[:12]}"
    )
    assert rows[0]["id"] == ls.stable_example_id(
        name, rows[0]["inputs"]["scenario_id"]
    )
    assert ls.example_rows(dataset) == rows
    scenarios, records = ls.select_validation_scenarios(dataset)
    assert [item["scenario_id"] for item in scenarios] == [
        "s-action-verification-01",
        "s-clarification-authorization-medical-risk-02",
        "s-confirmation-cancellation-state-01",
        "s-confirmation-cancellation-state-05",
        "s-confirmation-cancellation-state-07",
        "s-confirmation-cancellation-state-08",
    ]
    assert len(records) == 6
    assert {row["rule"] for row in records} == {
        "固定验收场景",
        "稳定 ID 顺序首条单轮无工具简单问答",
        "稳定 ID 顺序首条需要生产检索且无保存事件的场景",
        "稳定 ID 顺序首条包含真实确认保存事件的场景",
    }
    assert all(row["event_count"] >= 1 for row in records)
    assert all(row["applicable_metric_count"] >= 1 for row in records)


def test_sync_is_idempotent_and_completes_missing_examples(
    dataset, isolated_receipts
):
    client = MemoryClient()
    first = ls.sync_dataset(client, dataset)
    assert first["dataset_status"] == "created"
    assert len(client.examples[first["dataset_id"]]) == 80
    second = ls.sync_dataset(client, dataset)
    assert second["dataset_status"] == "reused"
    assert second["dataset_id"] == first["dataset_id"]
    assert second["example_ids"] == first["example_ids"]
    assert len(client.examples[first["dataset_id"]]) == 80
    removed = next(iter(client.examples[first["dataset_id"]]))
    client.examples[first["dataset_id"]].pop(removed)
    third = ls.sync_dataset(client, dataset)
    assert third["dataset_status"] == "reused"
    assert removed in client.examples[first["dataset_id"]]
    assert len(client.examples[first["dataset_id"]]) == 80


def test_sync_blocks_unknown_dataset_and_conflicting_content(
    dataset, isolated_receipts
):
    client = MemoryClient()
    receipt = ls.sync_dataset(client, dataset)
    remote = client.datasets[receipt["dataset_name"]]
    remote.metadata = {"marker": "unknown"}
    with pytest.raises(ls.ScenarioSDKError, match="缺少字段"):
        ls.sync_dataset(client, dataset)
    remote.metadata = {**ls.dataset_marker(dataset), "unknown": True}
    with pytest.raises(ls.ScenarioSDKError, match="未知字段"):
        ls.sync_dataset(client, dataset)

    client = MemoryClient()
    receipt = ls.sync_dataset(client, dataset)
    scenario_id = "s-action-verification-01"
    example_id = receipt["example_ids"][scenario_id]
    client.examples[receipt["dataset_id"]][example_id].inputs["scenario_id"] = "x"
    with pytest.raises(ls.ScenarioSDKError, match="inputs 不符"):
        ls.sync_dataset(client, dataset)

    client = MemoryClient()
    receipt = ls.sync_dataset(client, dataset)
    unknown = MemoryExample(
        {
            "id": str(uuid4()),
            "inputs": {},
            "outputs": {},
            "metadata": {},
        }
    )
    client.examples[receipt["dataset_id"]][unknown.id] = unknown
    with pytest.raises(ls.ScenarioSDKError, match="未知样例"):
        ls.sync_dataset(client, dataset)


def test_resolve_inputs_rejects_identity_drift(dataset, rows):
    by_scenario = ls.rows_by_scenario(rows)
    row = by_scenario["s-action-verification-01"]
    scenario, resolved = ls.resolve_inputs(row["inputs"], dataset, by_scenario)
    assert scenario["scenario_id"] == row["inputs"]["scenario_id"]
    assert resolved["id"] == row["id"]

    binding = ls.dataset_binding(dataset)
    with pytest.raises(ls.ScenarioSDKError, match="数据身份不符"):
        ls.resolve_inputs(
            {**row["inputs"], "dataset": {**binding, "content_sha256": "0" * 64}},
            dataset,
            by_scenario,
        )
    with pytest.raises(ls.ScenarioSDKError, match="场景摘要"):
        ls.resolve_inputs(
            {**row["inputs"], "scenario_sha256": "0" * 64},
            dataset,
            by_scenario,
        )
    with pytest.raises(ls.ScenarioSDKError, match="未声明字段"):
        ls.resolve_inputs({**row["inputs"], "message": "远端注入"}, dataset, by_scenario)
    with pytest.raises(ls.ScenarioSDKError, match="未知场景"):
        ls.resolve_inputs(
            {**row["inputs"], "scenario_id": "s-unknown"}, dataset, by_scenario
        )
    with pytest.raises(ls.ScenarioSDKError, match="场景摘要"):
        ls.resolve_inputs(
            {**row["inputs"], "scenario_id": "s-action-verification-02"},
            dataset,
            by_scenario,
        )


def test_judge_config_accepts_configured_model_and_fixed_endpoint(monkeypatch):
    monkeypatch.setenv("FIT_AGENT_EVAL_JUDGE_MODEL", "qwen3.6-plus")
    monkeypatch.setenv("FIT_AGENT_EVAL_JUDGE_BASE_URL", ls.JUDGE_BASE_URL)
    monkeypatch.setenv("FIT_AGENT_EVAL_JUDGE_API_KEY", "test-only-credential")
    judge = ls.judge_config()
    assert judge.model == "qwen3.6-plus"
    assert judge.base_url == ls.JUDGE_BASE_URL
    monkeypatch.setenv("FIT_AGENT_EVAL_JUDGE_MODEL", "step-5-preview")
    assert ls.judge_config().model == "step-5-preview"
    monkeypatch.setenv("FIT_AGENT_EVAL_JUDGE_MODEL", "   ")
    with pytest.raises(ls.ScenarioSDKError, match="裁判模型"):
        ls.judge_config()
    monkeypatch.setenv("FIT_AGENT_EVAL_JUDGE_MODEL", "qwen3.6-plus")
    monkeypatch.setenv("FIT_AGENT_EVAL_JUDGE_BASE_URL", "https://example.invalid/v1")
    with pytest.raises(ls.ScenarioSDKError, match="裁判服务地址"):
        ls.judge_config()
    monkeypatch.setenv("FIT_AGENT_EVAL_JUDGE_BASE_URL", ls.JUDGE_BASE_URL)
    monkeypatch.delenv("FIT_AGENT_EVAL_JUDGE_API_KEY")
    with pytest.raises(ls.ScenarioSDKError, match="裁判凭据"):
        ls.judge_config()


def test_execution_summary_keeps_relative_evidence():
    scenario = make_scenario("s-sdk-summary")
    example_id = str(uuid5(NAMESPACE_URL, "sdk-summary"))
    relative = ls.expected_execution_path(example_id, scenario["scenario_id"])
    assert not relative.is_absolute()
    assert ".." not in relative.parts
    summary = {
        "scenario_id": scenario["scenario_id"],
        "scenario_sha256": frozen_scenario_sha256(scenario),
        "status": "blocked",
        "evidence": {"execution": str(relative)},
    }
    assert not Path(summary["evidence"]["execution"]).is_absolute()
    assert ".." not in Path(summary["evidence"]["execution"]).parts


async def _execute(dataset, scenario, example_id, run_dir):
    execution = await execute_scenario(
        dataset,
        scenario,
        run_id="run-sdk-offline",
        root=run_dir / "scenarios" / example_id,
        enable_model=True,
    )
    return execution, ls.execution_summary(
        execution,
        run_id="run-sdk-offline",
        example_id=example_id,
        scenario_id=scenario["scenario_id"],
        dataset=dataset,
    )


async def test_evaluator_scores_after_runtime_close_and_reuses_result(
    dataset, tmp_path
):
    scenario = make_scenario("s-sdk-evaluator")
    example_id = str(uuid5(NAMESPACE_URL, "sdk-evaluator"))
    run_dir = tmp_path / "run"
    execution, outputs = await _execute(dataset, scenario, example_id, run_dir)
    scenario_root = run_dir / "scenarios" / example_id / scenario["scenario_id"]
    database = scenario_root / "fit-agent.db"
    assert database.is_file()
    database.rename(scenario_root / "fit-agent.db.renamed")
    assert not database.exists()
    row = synthetic_row(dataset, scenario, example_id)
    evaluator = ls.make_evaluator(
        dataset=dataset,
        rows={scenario["scenario_id"]: row},
        run_id="run-sdk-offline",
        run_dir=run_dir,
        judge=None,
    )
    first = await evaluator(row["inputs"], outputs, row["outputs"])
    first_feedback = {
        item["key"]: item["score"] for item in first["results"]
    }
    assert first_feedback == {
        "scenario_status": 1.0,
        "assertions_passed": 1.0,
        "assertions_failed": 0.0,
        "assertions_binding_errors": 0.0,
        "assertions_runner_errors": 0.0,
    }
    scoring_path = scenario_root / ls.SCORING_NAME
    scoring_bytes = scoring_path.read_bytes()
    evaluation = json.loads(
        (scenario_root / ls.EVALUATION_NAME).read_text(encoding="utf-8")
    )
    assert evaluation["execution_sha256"] == execution.execution_sha256
    assert evaluation["reused"] is False
    assert evaluation["incomplete"] == []

    second = await evaluator(row["inputs"], outputs, row["outputs"])
    assert second == first
    assert scoring_path.read_bytes() == scoring_bytes
    evaluation = json.loads(
        (scenario_root / ls.EVALUATION_NAME).read_text(encoding="utf-8")
    )
    assert evaluation["reused"] is True
    assert evaluation["scoring_binding"] == "state_completed"

    with pytest.raises(ls.ScenarioSDKError, match="run_id"):
        await evaluator(
            row["inputs"], {**outputs, "run_id": "other-run"}, row["outputs"]
        )
    with pytest.raises(ls.ScenarioSDKError, match="scenario_id"):
        await evaluator(
            row["inputs"],
            {**outputs, "scenario_id": "s-other"},
            row["outputs"],
        )
    with pytest.raises(ls.ScenarioSDKError, match="execution_sha256"):
        await evaluator(
            row["inputs"],
            {**outputs, "execution_sha256": "0" * 64},
            row["outputs"],
        )
    with pytest.raises(ls.ScenarioSDKError, match="状态"):
        await evaluator(
            row["inputs"], {**outputs, "status": "unknown"}, row["outputs"]
        )
    with pytest.raises(ls.ScenarioSDKError, match="批准样例"):
        await evaluator(
            row["inputs"], outputs, {**row["outputs"], "scenario_id": "s-other"}
        )
    with pytest.raises(ls.ScenarioSDKError, match="参考|reference|批准样例"):
        await evaluator(
            row["inputs"], outputs, {**row["outputs"], "dataset": {}}
        )


async def test_evaluator_rejects_path_escape_and_tampered_execution(
    dataset, tmp_path
):
    scenario = make_scenario("s-sdk-path")
    example_id = str(uuid5(NAMESPACE_URL, "sdk-path"))
    run_dir = tmp_path / "run"
    _, outputs = await _execute(dataset, scenario, example_id, run_dir)
    scenario_root = run_dir / "scenarios" / example_id / scenario["scenario_id"]
    row = synthetic_row(dataset, scenario, example_id)
    evaluator = ls.make_evaluator(
        dataset=dataset,
        rows={scenario["scenario_id"]: row},
        run_id="run-sdk-offline",
        run_dir=run_dir,
        judge=None,
    )
    outside = tmp_path / "outside-execution.json"
    outside.write_text("{}", encoding="utf-8")
    for reference in (
        "../outside-execution.json",
        str(outside),
        "scenarios/other/execution.json",
    ):
        with pytest.raises(ls.ScenarioSDKError):
            await evaluator(
                row["inputs"],
                {
                    **outputs,
                    "evidence": {
                        **outputs["evidence"],
                        "execution": reference,
                    },
                },
                row["outputs"],
            )
    payload = json.loads(
        (scenario_root / ls.EXECUTION_NAME).read_text(encoding="utf-8")
    )
    payload["events"][0]["session_id"] = "tampered"
    (scenario_root / ls.EXECUTION_NAME).write_text(
        json.dumps(payload), encoding="utf-8"
    )
    with pytest.raises(ls.ScenarioSDKError, match="摘要|scenario_id|执行结果"):
        await evaluator(row["inputs"], outputs, row["outputs"])


async def test_failed_scoring_state_blocks_repeat_without_new_attempt(
    dataset, tmp_path
):
    scenario = make_scenario("s-sdk-failed")
    example_id = str(uuid5(NAMESPACE_URL, "sdk-failed"))
    run_dir = tmp_path / "run"
    _, outputs = await _execute(dataset, scenario, example_id, run_dir)
    scenario_root = run_dir / "scenarios" / example_id / scenario["scenario_id"]
    (scenario_root / ls.SCORING_STATE_NAME).write_text(
        json.dumps(
            {
                "status": "failed",
                "execution_sha256": outputs["execution_sha256"],
                "error_type": "TimeoutError",
            }
        ),
        encoding="utf-8",
    )
    row = synthetic_row(dataset, scenario, example_id)
    evaluator = ls.make_evaluator(
        dataset=dataset,
        rows={scenario["scenario_id"]: row},
        run_id="run-sdk-offline",
        run_dir=run_dir,
        judge=None,
    )
    with pytest.raises(ls.ScoringBlocked, match="failed"):
        await evaluator(row["inputs"], outputs, row["outputs"])
    assert not (scenario_root / ls.SCORING_NAME).exists()


def test_run_slots_and_fault_closure_are_scoped():
    scope_a = f"evaluation:{uuid4().hex}"
    scope_b = f"evaluation:{uuid4().hex}"
    assert run_runtime.acquire_run_slot(scope_a) is True
    assert run_runtime.acquire_run_slot(scope_b) is True
    assert run_runtime.acquire_run_slot(scope_a) is False
    run_runtime.release_run_slot(scope_a)
    run_runtime.release_run_slot(scope_b)
    assert run_runtime.acquire_run_slot(scope_a) is True
    run_runtime.release_run_slot(scope_a)

    assert run_runtime.acquire_run_slot("global") is True
    assert run_runtime.acquire_run_slot("global") is False
    run_runtime.release_run_slot("global")
    assert run_runtime.acquire_run_slot("global") is True
    run_runtime.release_run_slot("global")

    run_runtime.enter_fault_closure(scope_a)
    assert run_runtime.is_fault_closed(scope_a) is True
    assert run_runtime.is_fault_closed(scope_b) is False
    run_runtime.reset_fault_closure(scope_b)
    assert run_runtime.is_fault_closed(scope_a) is True
    run_runtime.reset_fault_closure(scope_a)
    assert run_runtime.is_fault_closed(scope_a) is False

    token = run_runtime.set_run_scope(scope_a)
    try:
        run_runtime.enter_fault_closure()
        assert run_runtime.is_fault_closed() is True
        assert run_runtime.is_fault_closed(scope_b) is False
        run_runtime.reset_fault_closure()
        assert run_runtime.is_fault_closed() is False
    finally:
        run_runtime.reset_run_scope(token)
    assert run_runtime.current_run_scope() == "global"


async def test_six_scenario_units_execute_in_isolated_directories(
    dataset, tmp_path
):
    run_dir = tmp_path / "run"
    scenarios = [
        make_scenario(f"s-sdk-concurrent-{index:02d}") for index in range(6)
    ]
    examples = [
        str(uuid5(NAMESPACE_URL, f"sdk-concurrent-{index}"))
        for index in range(6)
    ]

    async def one(scenario, example_id):
        return await execute_scenario(
            dataset,
            scenario,
            run_id="run-sdk-offline",
            root=run_dir / "scenarios" / example_id,
            enable_model=True,
        )

    executions = await asyncio.gather(
        *[
            one(scenario, example_id)
            for scenario, example_id in zip(scenarios, examples)
        ]
    )
    assert [execution.status for execution in executions] == ["completed"] * 6
    assert [execution.scenario_id for execution in executions] == [
        scenario["scenario_id"] for scenario in scenarios
    ]
    databases = [
        run_dir / "scenarios" / example_id / scenario["scenario_id"] / "fit-agent.db"
        for scenario, example_id in zip(scenarios, examples)
    ]
    recorders = [
        run_dir
        / "scenarios"
        / example_id
        / scenario["scenario_id"]
        / "model-calls"
        for scenario, example_id in zip(scenarios, examples)
    ]
    assert len({path.resolve() for path in databases}) == 6
    assert len({path.resolve() for path in recorders}) == 6
    for scenario, example_id in zip(scenarios, examples):
        root = run_dir / "scenarios" / example_id / scenario["scenario_id"]
        assert (root / ls.EXECUTION_NAME).is_file()
        assert (root / "events.jsonl").is_file()
        assert not (root / ls.SCORING_NAME).exists()


async def test_concurrency_recorder_reports_bounded_overlap():
    recorder = ls.ScenarioConcurrencyRecorder()
    release = asyncio.Event()

    async def worker(index: int):
        started = await recorder.start(f"example-{index}", f"scenario-{index}")
        await release.wait()
        await recorder.finish(
            f"example-{index}", f"scenario-{index}", started, "completed"
        )

    tasks = [asyncio.create_task(worker(index)) for index in range(6)]
    await asyncio.sleep(0.01)
    release.set()
    await asyncio.gather(*tasks)
    report = recorder.report()
    assert report["calls"] == 6
    assert report["max_in_flight"] == 6
    assert report["overlap_pairs"] == 15
    assert report["max_in_flight"] <= ls.MAX_CONCURRENCY


async def _failed_scoring_run(
    dataset, tmp_path, mode: str
) -> tuple[Path, dict, dict]:
    scenario_id = f"s-sdk-finalize-{mode}"
    scenario = make_scenario(scenario_id)
    example_id = str(uuid5(NAMESPACE_URL, scenario_id))
    run_dir = tmp_path / mode
    execution, outputs = await _execute(dataset, scenario, example_id, run_dir)
    scenario_root = run_dir / "scenarios" / example_id / scenario_id
    row = synthetic_row(dataset, scenario, example_id)
    evaluator = ls.make_evaluator(
        dataset=dataset,
        rows={scenario_id: row},
        run_id="run-sdk-offline",
        run_dir=run_dir,
        judge=None,
    )
    await evaluator(row["inputs"], outputs, row["outputs"])
    scoring_path = scenario_root / ls.SCORING_NAME
    passing = ScenarioArtifact.model_validate_json(
        scoring_path.read_text(encoding="utf-8")
    )
    flipped = False
    failed_assertions = []
    for checkpoint in passing.assertions:
        outcomes = []
        for outcome in checkpoint.outcomes:
            if not flipped:
                outcomes.append(outcome.model_copy(update={"passed": False}))
                flipped = True
            else:
                outcomes.append(outcome)
        failed_assertions.append(
            checkpoint.model_copy(update={"outcomes": outcomes})
        )
    failed = passing.model_copy(update={"assertions": failed_assertions})
    scoring_path.write_text(
        failed.model_dump_json(indent=2), encoding="utf-8"
    )
    scoring_sha = sha256_text(scoring_path.read_text(encoding="utf-8"))
    evaluation_path = scenario_root / ls.EVALUATION_NAME
    evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
    evaluation["scoring_sha256"] = scoring_sha
    evaluation_path.write_text(
        json.dumps(evaluation, ensure_ascii=False), encoding="utf-8"
    )
    if mode == "legacy":
        feedback = ls.scenario_feedback(
            execution, passing, version=ls.LEGACY_FEEDBACK_VERSION
        )
        incomplete = ls.incomplete_items(
            passing, version=ls.LEGACY_FEEDBACK_VERSION
        )
        version = None
    elif mode == "stale_v2":
        feedback = ls.scenario_feedback(execution, passing)
        incomplete = []
        version = ls.FEEDBACK_VERSION
    else:
        feedback = ls.scenario_feedback(execution, failed)
        incomplete = ls.incomplete_items(failed)
        version = ls.FEEDBACK_VERSION
    sample = {
        "example_id": example_id,
        "scenario_id": scenario_id,
        "sdk_run_id": "sdk-finalize",
        "status": "complete",
        "execution_status": execution.status,
        "execution_sha256": execution.execution_sha256,
        "scoring_relative": str(
            PurePosixPath("scenarios") / example_id / scenario_id / ls.SCORING_NAME
        ),
        "scoring_sha256": scoring_sha,
        "feedback": feedback,
        "incomplete": incomplete,
        "run_error": None,
        "run_start_time": "2026-01-01T00:00:00+00:00",
        "run_end_time": "2026-01-01T00:01:00+00:00",
    }
    if version is not None:
        sample["feedback_version"] = version
    document = {
        "schema": ls.SCHEMA,
        "run_id": "run-sdk-offline",
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
    if version is not None:
        document["feedback_version"] = version
    (run_dir / ls.SDK_RUN_NAME).write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    return run_dir, sample, failed


async def test_finalize_recomputes_status_from_real_scoring(dataset, tmp_path):
    for mode in ("legacy", "stale_v2", "consistent_v2"):
        run_dir, sample, artifact = await _failed_scoring_run(
            dataset, tmp_path, mode
        )
        finalized = ls.finalize_run(run_dir)
        assert finalized["status"] == "incomplete"
        assert finalized["samples"][0]["feedback"] == sample["feedback"]
        assert finalized["samples"][0]["incomplete"] == sample["incomplete"]
        record = finalized["scenarios"][0]
        assert record["assertions"]["failed"] == sum(
            1
            for checkpoint in artifact.assertions
            for outcome in checkpoint.outcomes
            if not outcome.passed
        )
        assert record["assertions"]["failed"] > 0


def test_finalize_run_recomputes_status_without_external_calls(tmp_path):
    sample = {
        "example_id": "example-1",
        "scenario_id": "s-sdk-finalize",
        "status": "complete",
        "execution_status": "completed",
        "incomplete": [],
    }
    document = {
        "schema": ls.SCHEMA,
        "run_id": "run-sdk-finalize",
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
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / ls.SDK_RUN_NAME).write_text(
        json.dumps(document), encoding="utf-8"
    )
    finalized = ls.finalize_run(run_dir)
    assert finalized["status"] == "complete"
    assert finalized["scenarios"][0]["scenario_id"] == "s-sdk-finalize"

    document["samples"][0] = {
        **sample,
        "incomplete": ["cp1:runner_error"],
    }
    (run_dir / ls.SDK_RUN_NAME).write_text(
        json.dumps(document), encoding="utf-8"
    )
    finalized = ls.finalize_run(run_dir)
    assert finalized["status"] == "incomplete"


def _link(checkpoint_id: str, event_id: str = "e01") -> EvidenceLink:
    return EvidenceLink(
        run_id="run-feedback",
        scenario_id="s-feedback",
        event_id=event_id,
        checkpoint_id=checkpoint_id,
    )


def _outcome(
    checkpoint_id: str,
    *,
    kind: str = "evidence_present",
    passed: bool,
    event_id: str = "e01",
) -> AssertionOutcome:
    return AssertionOutcome(
        checkpoint=_link(checkpoint_id, event_id),
        kind=kind,
        passed=passed,
        evidence="fixture",
    )


def _artifact(
    *,
    assertions: list[AssertionCheckpointArtifact] | None = None,
    generation: list[GenerationCheckpointArtifact] | None = None,
) -> ScenarioArtifact:
    return ScenarioArtifact(
        run_id="run-feedback",
        scenario_id="s-feedback",
        session_ids=["session-feedback"],
        events=[],
        assertions=list(assertions or []),
        generation=list(generation or []),
        proposal_bindings={},
    )


def test_feedback_versions_are_explicit():
    assert ls.feedback_fields(ls.FEEDBACK_VERSION) == (
        "faithfulness",
        "factual_correctness",
        "scenario_status",
        "assertions_passed",
        "assertions_failed",
        "assertions_binding_errors",
        "assertions_runner_errors",
    )
    assert ls.feedback_fields(ls.LEGACY_FEEDBACK_VERSION) == (
        "faithfulness",
        "factual_correctness",
        "scenario_status",
    )
    assert ls.feedback_fields(ls.EXTERNAL_FEEDBACK_VERSION) == (
        ls.EXTERNAL_FEEDBACK_FIELDS
    )
    assert ls.feedback_version({}) == ls.LEGACY_FEEDBACK_VERSION
    assert ls.feedback_version({"feedback_version": 2}) == 2
    assert ls.feedback_version({"feedback_version": 3}) == (
        ls.EXTERNAL_FEEDBACK_VERSION
    )
    for value in (0, 4, True, "2"):
        with pytest.raises(ls.ScenarioSDKError, match="版本"):
            ls.feedback_version({"feedback_version": value})
    with pytest.raises(ls.ScenarioSDKError, match="版本"):
        ls.feedback_fields(0)
    assert ls.feedback_field_set_matches(
        ls.FEEDBACK_VERSION, {"scenario_status", *ls.ASSERTION_FEEDBACK_FIELDS}
    )
    assert ls.feedback_field_set_matches(
        ls.FEEDBACK_VERSION,
        {
            "faithfulness",
            "scenario_status",
            *ls.ASSERTION_FEEDBACK_FIELDS,
        },
    )
    assert not ls.feedback_field_set_matches(
        ls.FEEDBACK_VERSION, {"scenario_status"}
    )
    assert not ls.feedback_field_set_matches(
        ls.FEEDBACK_VERSION, {"scenario_status", *ls.ASSERTION_FEEDBACK_FIELDS, "extra"}
    )
    assert not ls.feedback_field_set_matches(
        ls.LEGACY_FEEDBACK_VERSION,
        {"scenario_status", *ls.ASSERTION_FEEDBACK_FIELDS},
    )
    assert ls.feedback_field_set_matches(
        ls.EXTERNAL_FEEDBACK_VERSION, ls.EXTERNAL_FEEDBACK_FIELDS
    )
    assert not ls.feedback_field_set_matches(
        ls.EXTERNAL_FEEDBACK_VERSION, ("scenario_status",)
    )
    assert ls.run_scoring_mode({}) == "ragas"
    assert ls.run_scoring_mode({"feedback_version": 3}) == "external"
    assert ls.run_scoring_mode(
        {"feedback_version": 3, "scoring_mode": "external"}
    ) == "external"
    with pytest.raises(ls.ScenarioSDKError, match="模式"):
        ls.run_scoring_mode({"feedback_version": 3, "scoring_mode": "ragas"})
    with pytest.raises(ls.ScenarioSDKError, match="模式"):
        ls.run_scoring_mode({"feedback_version": 2, "scoring_mode": "external"})


def test_feedback_reports_each_failed_assertion_without_changing_status():
    execution = SimpleNamespace(status="completed")
    artifact = _artifact(assertions=[AssertionCheckpointArtifact(
        checkpoint_id="cp1",
        event_id="e01",
        outcomes=[
            _outcome("cp1", passed=True),
            _outcome("cp1", passed=False),
            _outcome("cp1", passed=False),
        ],
    )])
    feedback = ls.scenario_feedback(execution, artifact)
    assert feedback["scenario_status"] == 1.0
    assert feedback["assertions_passed"] == 1.0
    assert feedback["assertions_failed"] == 2.0
    items = ls.incomplete_items(artifact)
    failures = [item for item in items if "assertion_failed" in item]
    assert len(failures) == 2
    assert failures[0] != failures[1]
    assert all(item.startswith("e01/cp1:assertion_failed:evidence_present:") for item in failures)

    legacy = ls.scenario_feedback(
        execution, artifact, version=ls.LEGACY_FEEDBACK_VERSION
    )
    assert legacy == {"scenario_status": 1.0}
    assert ls.incomplete_items(
        artifact, version=ls.LEGACY_FEEDBACK_VERSION
    ) == []
    with pytest.raises(ls.ScenarioSDKError, match="版本"):
        ls.scenario_feedback(execution, artifact, version=4)
    with pytest.raises(ls.ScenarioSDKError, match="版本"):
        ls.incomplete_items(artifact, version=4)
    with pytest.raises(ls.ScenarioSDKError, match="外部协议"):
        ls.scenario_feedback(
            execution, artifact, version=ls.EXTERNAL_FEEDBACK_VERSION
        )
    with pytest.raises(ls.ScenarioSDKError, match="外部协议"):
        ls.incomplete_items(artifact, version=ls.EXTERNAL_FEEDBACK_VERSION)


def test_feedback_keeps_real_zero_and_omits_na_and_failed_metrics():
    execution = SimpleNamespace(status="completed")
    link = _link("cp2", "e02")
    failed_link = _link("cp3", "e03")
    artifact = _artifact(generation=[
        GenerationCheckpointArtifact(
            checkpoint_id="cp2",
            event_id="e02",
            result=CheckpointResult(
                checkpoint=link,
                outcomes=[
                    MetricOutcome(
                        metric="faithfulness", status="succeeded", score=0.0
                    ),
                    MetricOutcome(
                        metric="factual_correctness",
                        status="not_applicable",
                        reason="纯状态检查点",
                    ),
                ],
            ),
        ),
        GenerationCheckpointArtifact(
            checkpoint_id="cp3",
            event_id="e03",
            result=CheckpointResult(
                checkpoint=failed_link,
                outcomes=[
                    MetricOutcome(
                        metric="faithfulness",
                        status="failed",
                        error="fixture failure",
                    )
                ],
            ),
        ),
    ])
    feedback = ls.scenario_feedback(execution, artifact)
    assert feedback["faithfulness"] == 0.0
    assert "factual_correctness" not in feedback
    assert feedback["scenario_status"] == 1.0


def test_run_status_marks_failed_assertion_feedback_incomplete():
    coverage = {"status": "complete"}
    sample = {
        "execution_status": "completed",
        "incomplete": [],
        "feedback": {"assertions_failed": 1.0, "scenario_status": 1.0},
    }
    assert ls._run_status(coverage, [sample], []) == "incomplete"
    sample["feedback"]["assertions_failed"] = 0.0
    assert ls._run_status(coverage, [sample], []) == "complete"
