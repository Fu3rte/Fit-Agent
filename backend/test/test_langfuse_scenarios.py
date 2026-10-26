import asyncio
import copy
import json
import sys
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from langfuse.api import (
    Experiment,
    ExperimentItem,
    ExperimentItemsResponse,
    ExperimentsResponse,
    ExperimentsResponseMeta,
    GetScoresV3Meta,
    GetScoresV3Response,
    NotFoundError,
    ScoreSource,
    ScoreSubjectV3_Experiment,
    ScoreSubjectV3_Observation,
    ScoreV3_Numeric,
)

from app.application.session import runtime as run_runtime
from evaluation import langfuse_scenarios as lf
from evaluation.contracts import (
    AssertionOutcome,
    CheckpointResult,
    EvidenceLink,
    MetricOutcome,
)
from evaluation.dataset import BACKEND, load_for_evaluation
from evaluation.generation_scoring import JudgeConfig
from evaluation.scenario_evidence import canonical_json, sha256_text
from evaluation.scenarios import (
    RAGAS_PROTOCOL_VERSION,
    AssertionCheckpointArtifact,
    GenerationCheckpointArtifact,
    ScenarioArtifact,
    execute_scenario,
    frozen_scenario_sha256,
)

DATASET_PATH = BACKEND / "dataset" / "evaluation" / "rebuild-v2"
VERIFY_STAMP = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def dataset():
    return load_for_evaluation(DATASET_PATH)


@pytest.fixture(scope="module")
def rows(dataset):
    return lf.example_rows(dataset)


class MemoryDataset:
    def __init__(self, name: str, metadata: dict) -> None:
        self.id = str(uuid4())
        self.name = name
        self.metadata = copy.deepcopy(metadata)
        self.items_by_id: dict[str, MemoryDatasetItem] = {}

    @property
    def items(self) -> list["MemoryDatasetItem"]:
        return list(self.items_by_id.values())


class MemoryDatasetItem:
    def __init__(
        self, dataset: MemoryDataset, row: dict, *, item_id: str | None = None
    ) -> None:
        self.id = item_id if item_id is not None else row["id"]
        self.dataset_id = dataset.id
        self.dataset_name = dataset.name
        self.input = copy.deepcopy(row["inputs"])
        self.expected_output = copy.deepcopy(row["outputs"])
        self.metadata = copy.deepcopy(row["metadata"])


class MemoryExperiment:
    def __init__(self, experiment: Experiment, items: list[ExperimentItem]) -> None:
        self.experiment = experiment
        self.items = items


class MemoryExperimentsApi:
    def __init__(self, client: "MemoryClient") -> None:
        self._client = client

    def list(
        self,
        *,
        from_start_time,
        fields=None,
        id=None,
        cursor=None,
        limit=None,
        **kwargs,
    ):
        matches = [
            entry.experiment
            for entry in self._client.experiments.values()
            if id is None or entry.experiment.id == id
        ]
        return ExperimentsResponse(
            data=matches, meta=ExperimentsResponseMeta(cursor=None)
        )

    def list_items(
        self,
        *,
        from_start_time,
        experiment_id=None,
        fields=None,
        cursor=None,
        **kwargs,
    ):
        entry = self._client.experiments[experiment_id]
        return ExperimentItemsResponse(
            data=list(entry.items), meta=ExperimentsResponseMeta(cursor=None)
        )


class MemoryScoresApi:
    def __init__(self, client: "MemoryClient") -> None:
        self._client = client

    def get_many(self, *, experiment_id=None, fields=None, limit=None, cursor=None, **kwargs):
        rows = self._client.scores_by_experiment.get(experiment_id, [])
        return GetScoresV3Response(
            data=list(rows), meta=GetScoresV3Meta(limit=limit or 50, cursor=None)
        )


class MemoryClient:
    def __init__(self) -> None:
        self.datasets: dict[str, MemoryDataset] = {}
        self.experiments: dict[str, MemoryExperiment] = {}
        self.scores_by_experiment: dict[str, list[ScoreV3_Numeric]] = {}
        self.api = SimpleNamespace(
            experiments=MemoryExperimentsApi(self),
            scores=MemoryScoresApi(self),
        )

    def auth_check(self) -> bool:
        return True

    def get_dataset(self, name: str) -> MemoryDataset:
        remote = self.datasets.get(name)
        if remote is None:
            raise NotFoundError({})
        return remote

    def create_dataset(self, *, name, description=None, metadata=None, **kwargs):
        assert name not in self.datasets, "重复创建同名远端数据集"
        remote = MemoryDataset(name, metadata or {})
        self.datasets[name] = remote
        return remote

    def create_dataset_item(
        self,
        *,
        dataset_name,
        id=None,
        input=None,
        expected_output=None,
        metadata=None,
        **kwargs,
    ):
        remote = self.datasets[dataset_name]
        existing = remote.items_by_id.get(id)
        if existing is not None:
            assert (
                existing.input == input
                and existing.expected_output == expected_output
                and existing.metadata == metadata
            ), "同 id 重复提交且内容不一致"
            return existing
        item = MemoryDatasetItem(
            remote,
            {
                "id": id,
                "inputs": input,
                "outputs": expected_output,
                "metadata": metadata,
            },
        )
        remote.items_by_id[id] = item
        return item


def make_score(name: str, value: float, trace_id: str) -> ScoreV3_Numeric:
    return ScoreV3_Numeric(
        value=value,
        id=str(uuid4()),
        project_id="test-project",
        name=name,
        source=ScoreSource.EVAL,
        timestamp=VERIFY_STAMP,
        environment="default",
        created_at=VERIFY_STAMP,
        updated_at=VERIFY_STAMP,
        subject=ScoreSubjectV3_Observation(id=str(uuid4()), trace_id=trace_id),
    )


def make_experiment_score(name: str, value: float, experiment_id: str) -> ScoreV3_Numeric:
    score = make_score(name, value, str(uuid4()))
    return score.model_copy(
        update={"subject": ScoreSubjectV3_Experiment(id=experiment_id)}
    )


def make_experiment_item(
    experiment_id: str,
    example_id: str,
    outputs,
    *,
    level: str = "DEFAULT",
    trace_id: str | None = None,
) -> ExperimentItem:
    return ExperimentItem(
        id=str(uuid4()),
        trace_id=trace_id or str(uuid4()),
        start_time=VERIFY_STAMP,
        end_time=None,
        level=level,
        environment="default",
        experiment_id=experiment_id,
        experiment_name="experiment",
        experiment_item_id=example_id,
        output=outputs,
    )


def make_experiment(
    experiment_id: str, name: str, metadata, items: list[ExperimentItem]
) -> MemoryExperiment:
    experiment = Experiment(
        id=experiment_id,
        name=name,
        start_time=VERIFY_STAMP,
        end_time=VERIFY_STAMP,
        item_count=len(items),
        metadata=metadata,
    )
    return MemoryExperiment(experiment, items)


def register_experiment(
    client: MemoryClient,
    *,
    experiment_id: str,
    items: list[ExperimentItem],
    scores: list[ScoreV3_Numeric],
    metadata=None,
) -> None:
    client.experiments[experiment_id] = make_experiment(
        experiment_id, "experiment", metadata or {}, items
    )
    client.scores_by_experiment[experiment_id] = list(scores)


@pytest.fixture
def isolated_receipts(tmp_path, monkeypatch):
    monkeypatch.setattr(lf, "DATASET_ROOT", tmp_path / "datasets")
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
    binding = lf.dataset_binding(dataset)
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
            "assertions": [dict(row) for row in scenario["assertions"]],
        },
        "metadata": {},
    }


def test_platform_constants_and_legacy_protocol_bindings():
    assert lf.SCHEMA == "fit-agent-scenario-langfuse-1"
    assert lf.DATASET_MARKER == "fit-agent-scenario-sdk-langfuse-1"
    assert (
        lf.EXTERNAL_PROTOCOL_VERSION
        == "fit-agent-external-scenario-langfuse-protocol-1"
    )
    assert lf.RAGAS_PROTOCOL_VERSION == RAGAS_PROTOCOL_VERSION
    assert lf.scoring_protocol("ragas") == RAGAS_PROTOCOL_VERSION
    assert lf.scoring_protocol("external") == lf.EXTERNAL_PROTOCOL_VERSION
    with pytest.raises(lf.ScenarioSDKError, match="scoring_mode"):
        lf.scoring_protocol("auto")
    assert lf.SDK_ROOT == BACKEND / "temp" / "scenario-langfuse"
    assert lf.RUNS_ROOT == lf.SDK_ROOT / "runs"
    assert lf.DATASET_ROOT == lf.SDK_ROOT / "datasets"
    assert lf.NUM_REPETITIONS == 1
    scenarios_source = (BACKEND / "evaluation" / "scenarios.py").read_text(
        encoding="utf-8"
    )
    # 外部运行摘要的裁判声明与本模块实验 metadata 保持同一 Langfuse 声明。
    assert '"ownership": "langfuse"' in scenarios_source
    assert '"ownership": "langsmith"' not in scenarios_source


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
    name = lf.dataset_name(dataset)
    assert name == (
        f"fit-agent-scenario-langfuse-{dataset.version}-{dataset.content_sha256[:12]}"
    )
    assert rows[0]["id"] == lf.stable_example_id(
        name, rows[0]["inputs"]["scenario_id"]
    )
    assert lf.example_rows(dataset) == rows
    scenarios, records = lf.select_validation_scenarios(dataset)
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
    first = lf.sync_dataset(client, dataset)
    assert first["dataset_status"] == "created"
    assert first["schema"] == lf.SCHEMA
    assert len(client.datasets[first["dataset_name"]].items_by_id) == 80
    second = lf.sync_dataset(client, dataset)
    assert second["dataset_status"] == "reused"
    assert second["dataset_id"] == first["dataset_id"]
    assert second["example_ids"] == first["example_ids"]
    assert len(client.datasets[first["dataset_name"]].items_by_id) == 80
    removed = next(iter(client.datasets[first["dataset_name"]].items_by_id))
    client.datasets[first["dataset_name"]].items_by_id.pop(removed)
    third = lf.sync_dataset(client, dataset)
    assert third["dataset_status"] == "reused"
    assert removed in client.datasets[first["dataset_name"]].items_by_id
    assert len(client.datasets[first["dataset_name"]].items_by_id) == 80


def test_sync_blocks_conflicting_metadata_and_examples(dataset, isolated_receipts):
    client = MemoryClient()
    receipt = lf.sync_dataset(client, dataset)
    remote = client.datasets[receipt["dataset_name"]]
    remote.metadata = {"marker": "unknown"}
    with pytest.raises(lf.ScenarioSDKError, match="缺少字段"):
        lf.sync_dataset(client, dataset)
    remote.metadata = {**lf.dataset_marker(dataset), "unknown": True}
    with pytest.raises(lf.ScenarioSDKError, match="未知字段"):
        lf.sync_dataset(client, dataset)

    client = MemoryClient()
    receipt = lf.sync_dataset(client, dataset)
    scenario_id = "s-action-verification-01"
    example_id = receipt["example_ids"][scenario_id]
    client.datasets[receipt["dataset_name"]].items_by_id[
        example_id
    ].input["scenario_id"] = "x"
    with pytest.raises(lf.ScenarioSDKError, match="inputs 不符"):
        lf.sync_dataset(client, dataset)

    client = MemoryClient()
    receipt = lf.sync_dataset(client, dataset)
    unknown = MemoryDatasetItem(
        client.datasets[receipt["dataset_name"]],
        {"id": str(uuid4()), "inputs": {}, "outputs": {}, "metadata": {}},
    )
    client.datasets[receipt["dataset_name"]].items_by_id[unknown.id] = unknown
    with pytest.raises(lf.ScenarioSDKError, match="未知样例"):
        lf.sync_dataset(client, dataset)


def test_sync_rejects_existing_item_with_same_id_and_new_content(
    dataset, isolated_receipts
):
    client = MemoryClient()
    lf.sync_dataset(client, dataset)
    name = lf.dataset_name(dataset)
    remote = client.datasets[name]
    some_id = next(iter(remote.items_by_id))
    with pytest.raises(AssertionError, match="同 id 重复提交且内容不一致"):
        client.create_dataset_item(
            dataset_name=name,
            id=some_id,
            input={"tampered": True},
            expected_output={},
            metadata={},
        )


def test_resolve_inputs_rejects_identity_drift(dataset, rows):
    by_scenario = lf.rows_by_scenario(rows)
    row = by_scenario["s-action-verification-01"]
    scenario, resolved = lf.resolve_inputs(row["inputs"], dataset, by_scenario)
    assert scenario["scenario_id"] == row["inputs"]["scenario_id"]
    assert resolved["id"] == row["id"]

    binding = lf.dataset_binding(dataset)
    with pytest.raises(lf.ScenarioSDKError, match="数据身份不符"):
        lf.resolve_inputs(
            {**row["inputs"], "dataset": {**binding, "content_sha256": "0" * 64}},
            dataset,
            by_scenario,
        )
    with pytest.raises(lf.ScenarioSDKError, match="场景摘要"):
        lf.resolve_inputs(
            {**row["inputs"], "scenario_sha256": "0" * 64},
            dataset,
            by_scenario,
        )
    with pytest.raises(lf.ScenarioSDKError, match="未声明字段"):
        lf.resolve_inputs({**row["inputs"], "message": "远端注入"}, dataset, by_scenario)
    with pytest.raises(lf.ScenarioSDKError, match="未知场景"):
        lf.resolve_inputs(
            {**row["inputs"], "scenario_id": "s-unknown"}, dataset, by_scenario
        )
    with pytest.raises(lf.ScenarioSDKError, match="场景摘要"):
        lf.resolve_inputs(
            {**row["inputs"], "scenario_id": "s-action-verification-02"},
            dataset,
            by_scenario,
        )


def test_judge_config_accepts_configured_model_and_fixed_endpoint(monkeypatch):
    monkeypatch.setenv("FIT_AGENT_EVAL_JUDGE_MODEL", "qwen3.6-plus")
    monkeypatch.setenv("FIT_AGENT_EVAL_JUDGE_BASE_URL", lf.JUDGE_BASE_URL)
    monkeypatch.setenv("FIT_AGENT_EVAL_JUDGE_API_KEY", "test-only-credential")
    judge = lf.judge_config()
    assert judge.model == "qwen3.6-plus"
    assert judge.base_url == lf.JUDGE_BASE_URL
    monkeypatch.setenv("FIT_AGENT_EVAL_JUDGE_MODEL", "step-5-preview")
    assert lf.judge_config().model == "step-5-preview"
    monkeypatch.setenv("FIT_AGENT_EVAL_JUDGE_MODEL", "   ")
    with pytest.raises(lf.ScenarioSDKError, match="裁判模型"):
        lf.judge_config()
    monkeypatch.setenv("FIT_AGENT_EVAL_JUDGE_MODEL", "qwen3.6-plus")
    monkeypatch.setenv("FIT_AGENT_EVAL_JUDGE_BASE_URL", "https://example.invalid/v1")
    with pytest.raises(lf.ScenarioSDKError, match="裁判服务地址"):
        lf.judge_config()
    monkeypatch.setenv("FIT_AGENT_EVAL_JUDGE_BASE_URL", lf.JUDGE_BASE_URL)
    monkeypatch.delenv("FIT_AGENT_EVAL_JUDGE_API_KEY")
    with pytest.raises(lf.ScenarioSDKError, match="裁判凭据"):
        lf.judge_config()


def test_require_credentials_blocks_missing_keys(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    with pytest.raises(lf.ScenarioSDKError, match="LANGFUSE_PUBLIC_KEY"):
        lf.require_credentials()
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test-only")
    with pytest.raises(lf.ScenarioSDKError, match="LANGFUSE_SECRET_KEY"):
        lf.require_credentials()
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test-only")
    lf.require_credentials()


async def test_run_experiment_blocks_without_credentials(dataset, tmp_path, monkeypatch):
    monkeypatch.setattr(lf, "RUNS_ROOT", tmp_path / "runs")
    # 指向空目录隔离真实 .env：load_dotenv 不得把已删除的凭据重新注入环境。
    monkeypatch.setattr(lf, "BACKEND", tmp_path)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    args = SimpleNamespace(
        dataset=DATASET_PATH,
        purpose="validation",
        scenario_id=None,
        judge_timeout=300.0,
        scoring_mode="ragas",
    )
    with pytest.raises(lf.ScenarioSDKError, match="LANGFUSE_PUBLIC_KEY"):
        await lf.run_experiment(args)
    run_dirs = list((tmp_path / "runs").iterdir())
    assert len(run_dirs) == 1
    document = json.loads(
        (run_dirs[0] / lf.SDK_RUN_NAME).read_text(encoding="utf-8")
    )
    assert document["schema"] == lf.SCHEMA
    assert document["status"] == "blocked"
    assert document["error_type"] == "ScenarioSDKError"
    assert document["protocol_version"] == RAGAS_PROTOCOL_VERSION
    assert document["num_repetitions"] == 1
    assert document["dataset_name"] == lf.dataset_name(dataset)


async def test_run_experiment_defaults_to_external_without_scoring_mode(
    dataset, tmp_path, monkeypatch
):
    monkeypatch.setattr(lf, "RUNS_ROOT", tmp_path / "runs")
    # 隔离全部本地裁判环境变量：external 默认推进时不得读取任何 JUDGE_* 配置。
    for name in (
        "FIT_AGENT_EVAL_JUDGE_MODEL",
        "FIT_AGENT_EVAL_JUDGE_BASE_URL",
        "FIT_AGENT_EVAL_JUDGE_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(lf, "judge_config", lambda: (_ for _ in ()).throw(
        AssertionError("external 默认不得读取本地裁判配置")
    ))

    # 裁判赋值之后的第一个本地依赖点：推进至此即证明 judge 为 None 且未读裁判配置。
    def forbidden_model_config():
        raise AssertionError("外部默认推进到生成模型配置读取")

    monkeypatch.setattr(lf, "load_model_config", forbidden_model_config)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test-only")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test-only")
    args = SimpleNamespace(
        dataset=DATASET_PATH,
        purpose="validation",
        scenario_id=None,
        judge_timeout=300.0,
    )
    with pytest.raises(AssertionError, match="生成模型配置"):
        await lf.run_experiment(args)
    run_dirs = list((tmp_path / "runs").iterdir())
    assert len(run_dirs) == 1
    document = json.loads(
        (run_dirs[0] / lf.SDK_RUN_NAME).read_text(encoding="utf-8")
    )
    assert document["scoring_mode"] == "external"
    assert document["feedback_version"] == lf.EXTERNAL_FEEDBACK_VERSION
    assert document["protocol_version"] == lf.EXTERNAL_PROTOCOL_VERSION
    assert document["status"] == "blocked"
    assert document["error_type"] == "AssertionError"
    # external 入口的终态语义锚定：只做确定性断言，评分等待 Langfuse 网页裁判回填。
    assert lf.EXTERNAL_RUN_STATUS == "awaiting_external_scoring"


def test_cli_run_scoring_mode_defaults_to_external(monkeypatch):
    captured = {}

    async def record(args):
        captured["args"] = args

    monkeypatch.setattr(lf, "run_experiment", record)
    for argv, expected in (
        (["langfuse_scenarios", "run"], "external"),
        (["langfuse_scenarios", "run", "--scoring-mode", "external"], "external"),
        (["langfuse_scenarios", "run", "--scoring-mode", "ragas"], "ragas"),
    ):
        monkeypatch.setattr(sys, "argv", argv)
        lf.main()
        assert captured["args"].scoring_mode == expected
    with pytest.raises(SystemExit):
        monkeypatch.setattr(
            sys, "argv", ["langfuse_scenarios", "run", "--scoring-mode", "auto"]
        )
        lf.main()


def test_execution_summary_keeps_relative_evidence():
    scenario = make_scenario("s-sdk-summary")
    example_id = str(uuid5(NAMESPACE_URL, "sdk-summary"))
    relative = lf.expected_execution_path(example_id, scenario["scenario_id"])
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
    return execution, lf.execution_summary(
        execution,
        run_id="run-sdk-offline",
        example_id=example_id,
        scenario_id=scenario["scenario_id"],
        dataset=dataset,
    )


async def _call_evaluator(evaluator, row, outputs):
    return await evaluator(
        input=row["inputs"],
        output=outputs,
        expected_output=row["outputs"],
        metadata=row["metadata"],
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
    evaluator = lf.make_evaluator(
        dataset=dataset,
        rows={scenario["scenario_id"]: row},
        run_id="run-sdk-offline",
        run_dir=run_dir,
        judge=None,
        scoring_mode="ragas",
    )
    first = await _call_evaluator(evaluator, row, outputs)
    first_feedback = {item.name: item.value for item in first}
    assert first_feedback == {
        "scenario_status": 1.0,
        "assertions_passed": 1.0,
        "assertions_failed": 0.0,
        "assertions_binding_errors": 0.0,
        "assertions_runner_errors": 0.0,
    }
    scoring_path = scenario_root / lf.SCORING_NAME
    scoring_bytes = scoring_path.read_bytes()
    evaluation = json.loads(
        (scenario_root / lf.EVALUATION_NAME).read_text(encoding="utf-8")
    )
    assert evaluation["schema"] == lf.SCHEMA
    assert evaluation["protocol_version"] == RAGAS_PROTOCOL_VERSION
    assert evaluation["execution_sha256"] == execution.execution_sha256
    assert evaluation["reused"] is False
    assert evaluation["incomplete"] == []

    second = await _call_evaluator(evaluator, row, outputs)
    assert [(item.name, item.value) for item in second] == [
        (item.name, item.value) for item in first
    ]
    assert scoring_path.read_bytes() == scoring_bytes
    evaluation = json.loads(
        (scenario_root / lf.EVALUATION_NAME).read_text(encoding="utf-8")
    )
    assert evaluation["reused"] is True
    assert evaluation["scoring_binding"] == "state_completed"

    with pytest.raises(lf.ScenarioSDKError, match="run_id"):
        await evaluator(
            input=row["inputs"],
            output={**outputs, "run_id": "other-run"},
            expected_output=row["outputs"],
            metadata=row["metadata"],
        )
    with pytest.raises(lf.ScenarioSDKError, match="scenario_id"):
        await evaluator(
            input=row["inputs"],
            output={**outputs, "scenario_id": "s-other"},
            expected_output=row["outputs"],
            metadata=row["metadata"],
        )
    with pytest.raises(lf.ScenarioSDKError, match="execution_sha256"):
        await evaluator(
            input=row["inputs"],
            output={**outputs, "execution_sha256": "0" * 64},
            expected_output=row["outputs"],
            metadata=row["metadata"],
        )
    with pytest.raises(lf.ScenarioSDKError, match="状态"):
        await evaluator(
            input=row["inputs"],
            output={**outputs, "status": "unknown"},
            expected_output=row["outputs"],
            metadata=row["metadata"],
        )
    with pytest.raises(lf.ScenarioSDKError, match="批准样例"):
        await evaluator(
            input=row["inputs"],
            output=outputs,
            expected_output={**row["outputs"], "scenario_id": "s-other"},
            metadata=row["metadata"],
        )
    with pytest.raises(lf.ScenarioSDKError, match="批准样例"):
        await evaluator(
            input=row["inputs"],
            output=outputs,
            expected_output={**row["outputs"], "dataset": {}},
            metadata=row["metadata"],
        )
    with pytest.raises(lf.ScenarioSDKError, match="metadata"):
        await evaluator(
            input=row["inputs"],
            output=outputs,
            expected_output=row["outputs"],
            metadata={"tampered": True},
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
    evaluator = lf.make_evaluator(
        dataset=dataset,
        rows={scenario["scenario_id"]: row},
        run_id="run-sdk-offline",
        run_dir=run_dir,
        judge=None,
        scoring_mode="ragas",
    )
    outside = tmp_path / "outside-execution.json"
    outside.write_text("{}", encoding="utf-8")
    for reference in (
        "../outside-execution.json",
        str(outside),
        "scenarios/other/execution.json",
    ):
        with pytest.raises(lf.ScenarioSDKError):
            await evaluator(
                input=row["inputs"],
                output={
                    **outputs,
                    "evidence": {
                        **outputs["evidence"],
                        "execution": reference,
                    },
                },
                expected_output=row["outputs"],
                metadata=row["metadata"],
            )
    payload = json.loads(
        (scenario_root / lf.EXECUTION_NAME).read_text(encoding="utf-8")
    )
    payload["events"][0]["session_id"] = "tampered"
    (scenario_root / lf.EXECUTION_NAME).write_text(
        json.dumps(payload), encoding="utf-8"
    )
    with pytest.raises(lf.ScenarioSDKError, match="摘要|scenario_id|执行结果"):
        await _call_evaluator(evaluator, row, outputs)


async def test_failed_scoring_state_blocks_repeat_without_new_attempt(
    dataset, tmp_path
):
    scenario = make_scenario("s-sdk-failed")
    example_id = str(uuid5(NAMESPACE_URL, "sdk-failed"))
    run_dir = tmp_path / "run"
    _, outputs = await _execute(dataset, scenario, example_id, run_dir)
    scenario_root = run_dir / "scenarios" / example_id / scenario["scenario_id"]
    (scenario_root / lf.SCORING_STATE_NAME).write_text(
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
    evaluator = lf.make_evaluator(
        dataset=dataset,
        rows={scenario["scenario_id"]: row},
        run_id="run-sdk-offline",
        run_dir=run_dir,
        judge=None,
        scoring_mode="ragas",
    )
    with pytest.raises(lf.ScoringBlocked, match="failed"):
        await _call_evaluator(evaluator, row, outputs)
    assert not (scenario_root / lf.SCORING_NAME).exists()


async def test_external_evaluator_reports_external_feedback_keys(
    dataset, tmp_path, monkeypatch
):
    scenario = make_scenario("s-sdk-external-feedback")
    example_id = str(uuid5(NAMESPACE_URL, "sdk-external-feedback"))
    run_dir = tmp_path / "run"
    _, outputs = await _execute(dataset, scenario, example_id, run_dir)
    row = synthetic_row(dataset, scenario, example_id)
    monkeypatch.setattr(lf, "judge_config", lambda: (_ for _ in ()).throw(
        AssertionError("外部模式不得读取本地裁判配置")
    ))
    evaluator = lf.make_evaluator(
        dataset=dataset,
        rows={scenario["scenario_id"]: row},
        run_id="run-sdk-offline",
        run_dir=run_dir,
        judge=None,
        scoring_mode="external",
    )
    first = await _call_evaluator(evaluator, row, outputs)
    feedback = {item.name: item.value for item in first}
    assert set(feedback) == set(lf.EXTERNAL_FEEDBACK_FIELDS)
    assert feedback["execution_completed"] == 1.0
    assert feedback["assertion_passed"] == 1.0
    assert feedback["generation_scoring_completed"] == 0.0
    scenario_root = run_dir / "scenarios" / example_id / scenario["scenario_id"]
    evaluation = json.loads(
        (scenario_root / lf.EXTERNAL_EVALUATION_NAME).read_text(encoding="utf-8")
    )
    assert evaluation["protocol_version"] == lf.EXTERNAL_PROTOCOL_VERSION
    assert evaluation["generation_scoring_status"] == "pending_external"
    assert not (scenario_root / lf.SCORING_NAME).exists()
    second = await _call_evaluator(evaluator, row, outputs)
    assert [(item.name, item.value) for item in second] == [
        (item.name, item.value) for item in first
    ]
    assert evaluation["reused"] is False


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
        assert (root / lf.EXECUTION_NAME).is_file()
        assert (root / "events.jsonl").is_file()
        assert not (root / lf.SCORING_NAME).exists()


async def test_concurrency_recorder_reports_bounded_overlap():
    recorder = lf.ScenarioConcurrencyRecorder()
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
    assert report["max_in_flight"] <= lf.MAX_CONCURRENCY


def _ragas_verify_setup(dataset, tmp_path, scenario_id: str):
    scenario = make_scenario(scenario_id)
    example_id = str(uuid5(NAMESPACE_URL, scenario_id))
    run_dir = tmp_path / scenario_id
    return scenario, example_id, run_dir


async def _scored_ragas_run(dataset, tmp_path, scenario_id: str):
    scenario, example_id, run_dir = _ragas_verify_setup(dataset, tmp_path, scenario_id)
    _, outputs = await _execute(dataset, scenario, example_id, run_dir)
    row = synthetic_row(dataset, scenario, example_id)
    evaluator = lf.make_evaluator(
        dataset=dataset,
        rows={scenario["scenario_id"]: row},
        run_id="run-sdk-offline",
        run_dir=run_dir,
        judge=None,
        scoring_mode="ragas",
    )
    evaluations = await _call_evaluator(evaluator, row, outputs)
    expected_feedback = {item.name: item.value for item in evaluations}
    return scenario, example_id, run_dir, outputs, row, expected_feedback


def _verify(client, *, experiment_id, example_id, row, run_dir, scoring_mode="ragas"):
    return lf.verify_runs(
        client,
        experiment_id=experiment_id,
        examples=[SimpleNamespace(id=example_id)],
        rows_by_example={example_id: row},
        run_dir=run_dir,
        scoring_mode=scoring_mode,
        from_start_time=VERIFY_STAMP,
    )


async def test_verify_runs_accepts_matching_remote_scores(dataset, tmp_path):
    (
        scenario,
        example_id,
        run_dir,
        outputs,
        row,
        expected_feedback,
    ) = await _scored_ragas_run(dataset, tmp_path, "s-sdk-verify-ok")
    trace_id = str(uuid4())
    client = MemoryClient()
    register_experiment(
        client,
        experiment_id="exp-verify-ok",
        items=[make_experiment_item("exp-verify-ok", example_id, outputs, trace_id=trace_id)],
        scores=[
            make_score(name, value, trace_id)
            for name, value in expected_feedback.items()
        ],
    )
    samples, coverage = _verify(
        client,
        experiment_id="exp-verify-ok",
        example_id=example_id,
        row=row,
        run_dir=run_dir,
    )
    assert coverage["status"] == "complete"
    assert coverage["expected_runs"] == 1
    assert coverage["observed_runs"] == 1
    assert coverage["unmapped_scores"] == []
    sample = samples[0]
    assert sample["status"] == "complete"
    assert sample["sdk_run_id"] == trace_id
    assert sample["trace_id"] == trace_id
    assert sample["run_error"] is None
    assert sample["feedback"] == pytest.approx(sample["expected_feedback"], abs=5e-5)
    assert sample["max_feedback_difference"] == 0.0


async def test_verify_runs_flags_score_drift_missing_and_unmapped(dataset, tmp_path):
    (
        scenario,
        example_id,
        run_dir,
        outputs,
        row,
        expected_feedback,
    ) = await _scored_ragas_run(dataset, tmp_path, "s-sdk-verify-drift")
    names = sorted(expected_feedback)
    trace_id = str(uuid4())

    client = MemoryClient()
    drifted = dict(expected_feedback)
    drifted[names[0]] = drifted[names[0]] + 1e-2
    register_experiment(
        client,
        experiment_id="exp-drift",
        items=[make_experiment_item("exp-drift", example_id, outputs, trace_id=trace_id)],
        scores=[make_score(name, value, trace_id) for name, value in drifted.items()],
    )
    samples, coverage = _verify(
        client, experiment_id="exp-drift", example_id=example_id, row=row, run_dir=run_dir
    )
    assert coverage["status"] == "incomplete"
    assert samples[0]["status"] == "failed"
    assert "远端反馈与本地评分不一致" in samples[0]["error"]

    client = MemoryClient()
    missing = {name: value for name, value in expected_feedback.items() if name != names[0]}
    register_experiment(
        client,
        experiment_id="exp-missing",
        items=[make_experiment_item("exp-missing", example_id, outputs, trace_id=trace_id)],
        scores=[make_score(name, value, trace_id) for name, value in missing.items()],
    )
    samples, coverage = _verify(
        client, experiment_id="exp-missing", example_id=example_id, row=row, run_dir=run_dir
    )
    assert coverage["status"] == "incomplete"
    assert samples[0]["status"] == "failed"
    assert "远端反馈指标集合" in samples[0]["error"]

    client = MemoryClient()
    scores = [make_score(name, value, trace_id) for name, value in expected_feedback.items()]
    scores.append(make_experiment_score("assertion_passed", 1.0, "exp-unmapped"))
    register_experiment(
        client,
        experiment_id="exp-unmapped",
        items=[make_experiment_item("exp-unmapped", example_id, outputs, trace_id=trace_id)],
        scores=scores,
    )
    samples, coverage = _verify(
        client, experiment_id="exp-unmapped", example_id=example_id, row=row, run_dir=run_dir
    )
    assert coverage["status"] == "incomplete"
    assert coverage["unmapped_scores"] == ["assertion_passed"]

    client = MemoryClient()
    duplicate = list(expected_feedback.items())
    scores = [make_score(name, value, trace_id) for name, value in duplicate]
    scores.append(make_score(names[0], expected_feedback[names[0]], trace_id))
    register_experiment(
        client,
        experiment_id="exp-duplicate",
        items=[make_experiment_item("exp-duplicate", example_id, outputs, trace_id=trace_id)],
        scores=scores,
    )
    samples, coverage = _verify(
        client, experiment_id="exp-duplicate", example_id=example_id, row=row, run_dir=run_dir
    )
    assert coverage["status"] == "incomplete"
    assert "反馈指标重复" in samples[0]["error"]


async def test_verify_runs_reports_failed_and_extra_items(dataset, tmp_path):
    (
        scenario,
        example_id,
        run_dir,
        outputs,
        row,
        expected_feedback,
    ) = await _scored_ragas_run(dataset, tmp_path, "s-sdk-verify-failed")
    trace_id = str(uuid4())

    client = MemoryClient()
    register_experiment(
        client,
        experiment_id="exp-error",
        items=[
            make_experiment_item(
                "exp-error", example_id, "Error: boom", level="ERROR", trace_id=trace_id
            )
        ],
        scores=[],
    )
    samples, coverage = _verify(
        client, experiment_id="exp-error", example_id=example_id, row=row, run_dir=run_dir
    )
    assert coverage["status"] == "incomplete"
    sample = samples[0]
    assert sample["status"] == "failed"
    assert sample["run_error"] == "Error: boom"
    assert sample["execution_status"] == "completed"

    client = MemoryClient()
    register_experiment(
        client,
        experiment_id="exp-missing-item",
        items=[],
        scores=[],
    )
    samples, coverage = _verify(
        client, experiment_id="exp-missing-item", example_id=example_id, row=row, run_dir=run_dir
    )
    assert coverage["status"] == "incomplete"
    assert samples[0]["status"] == "missing"

    client = MemoryClient()
    register_experiment(
        client,
        experiment_id="exp-extra",
        items=[
            make_experiment_item("exp-extra", example_id, outputs, trace_id=trace_id),
            make_experiment_item("exp-extra", str(uuid4()), outputs),
        ],
        scores=[
            make_score(name, value, trace_id)
            for name, value in expected_feedback.items()
        ],
    )
    samples, coverage = _verify(
        client, experiment_id="exp-extra", example_id=example_id, row=row, run_dir=run_dir
    )
    assert coverage["status"] == "incomplete"
    assert coverage["extra_example_ids"]


async def test_verify_external_runs_matches_external_feedback(dataset, tmp_path):
    scenario = make_scenario("s-sdk-verify-external")
    example_id = str(uuid5(NAMESPACE_URL, "sdk-verify-external"))
    run_dir = tmp_path / "run"
    _, outputs = await _execute(dataset, scenario, example_id, run_dir)
    row = synthetic_row(dataset, scenario, example_id)
    evaluator = lf.make_evaluator(
        dataset=dataset,
        rows={scenario["scenario_id"]: row},
        run_id="run-sdk-offline",
        run_dir=run_dir,
        judge=None,
        scoring_mode="external",
    )
    evaluations = await _call_evaluator(evaluator, row, outputs)
    expected_feedback = {item.name: item.value for item in evaluations}
    assert set(expected_feedback) == set(lf.EXTERNAL_FEEDBACK_FIELDS)
    trace_id = str(uuid4())
    client = MemoryClient()
    register_experiment(
        client,
        experiment_id="exp-verify-external",
        items=[
            make_experiment_item(
                "exp-verify-external", example_id, outputs, trace_id=trace_id
            )
        ],
        scores=[
            make_score(name, value, trace_id)
            for name, value in expected_feedback.items()
        ],
    )
    samples, coverage = _verify(
        client,
        experiment_id="exp-verify-external",
        example_id=example_id,
        row=row,
        run_dir=run_dir,
        scoring_mode="external",
    )
    assert coverage["status"] == "complete"
    assert samples[0]["status"] == "complete"
    assert samples[0]["scoring_mode"] == "external"
    assert samples[0]["feedback_version"] == lf.EXTERNAL_FEEDBACK_VERSION
    assert samples[0]["generation_scoring_status"] == "pending_external"


async def test_verify_runs_until_complete_polls_delayed_scores(
    dataset, tmp_path, monkeypatch
):
    (
        scenario,
        example_id,
        run_dir,
        outputs,
        row,
        expected_feedback,
    ) = await _scored_ragas_run(dataset, tmp_path, "s-sdk-verify-poll")
    trace_id = str(uuid4())
    client = MemoryClient()
    register_experiment(
        client,
        experiment_id="exp-poll",
        items=[make_experiment_item("exp-poll", example_id, outputs, trace_id=trace_id)],
        scores=[],
    )
    full_scores = [
        make_score(name, value, trace_id)
        for name, value in expected_feedback.items()
    ]
    calls = {"count": 0}
    original = client.api.scores.get_many

    def delayed_get_many(**kwargs):
        calls["count"] += 1
        if calls["count"] >= 2:
            client.scores_by_experiment["exp-poll"] = full_scores
        return original(**kwargs)

    monkeypatch.setattr(client.api.scores, "get_many", delayed_get_many)
    monkeypatch.setattr(lf, "REMOTE_VERIFY_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(lf, "REMOTE_VERIFY_TIMEOUT_SECONDS", 5.0)
    samples, coverage = await lf.verify_runs_until_complete(
        client,
        experiment_id="exp-poll",
        examples=[SimpleNamespace(id=example_id)],
        rows_by_example={example_id: row},
        run_dir=run_dir,
        scoring_mode="ragas",
        from_start_time=VERIFY_STAMP,
    )
    assert coverage["status"] == "complete"
    assert samples[0]["status"] == "complete"
    assert calls["count"] >= 2


def test_experiment_metadata_roundtrip_and_drift_detection(dataset):
    selections = lf.selection_records(dataset)
    code = {
        "commit": "a" * 40,
        "dirty": False,
        "lock_sha256": "b" * 64,
        "source_sha256": "c" * 64,
    }
    external = lf.experiment_metadata(
        run_id="run-sdk-offline",
        purpose="validation",
        dataset=dataset,
        selections=selections,
        model_identity={"model": "generation-model"},
        judge=None,
        index_identity={"collection": "fit-agent-exercises"},
        code=code,
        scoring_mode="external",
    )
    assert external["judge"] == {"ownership": "langfuse", "configuration": "external"}
    assert external["protocol_version"] == lf.EXTERNAL_PROTOCOL_VERSION
    assert external["num_repetitions"] == 1
    assert external["max_concurrency"] == lf.MAX_CONCURRENCY
    assert external["sdk_module_sha256"] == sha256_text(
        Path(lf.__file__).read_text(encoding="utf-8")
    )
    ragas = lf.experiment_metadata(
        run_id="run-sdk-offline",
        purpose="validation",
        dataset=dataset,
        selections=selections,
        model_identity={"model": "generation-model"},
        judge=JudgeConfig(
            model="qwen3.6-plus", base_url=lf.JUDGE_BASE_URL, api_key="test-only"
        ),
        index_identity={"collection": "fit-agent-exercises"},
        code=code,
        scoring_mode="ragas",
    )
    assert ragas["judge"] == {
        "model": "qwen3.6-plus",
        "endpoint_sha256": sha256_text(lf.JUDGE_BASE_URL),
    }
    assert ragas["protocol_version"] == RAGAS_PROTOCOL_VERSION

    client = MemoryClient()
    register_experiment(
        client,
        experiment_id="exp-meta",
        items=[],
        scores=[],
        metadata=external,
    )
    lf.verify_experiment_metadata(
        client, "exp-meta", external, from_start_time=VERIFY_STAMP
    )
    drifted = {**external, "purpose": "formal"}
    register_experiment(
        client, experiment_id="exp-meta", items=[], scores=[], metadata=drifted
    )
    with pytest.raises(lf.ScenarioSDKError, match="远端实验 metadata 不符"):
        lf.verify_experiment_metadata(
            client, "exp-meta", external, from_start_time=VERIFY_STAMP
        )
    client.experiments.pop("exp-meta")
    with pytest.raises(lf.ScenarioSDKError, match="远端实验不存在"):
        lf.verify_experiment_metadata(
            client, "exp-meta", external, from_start_time=VERIFY_STAMP
        )


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
    evaluator = lf.make_evaluator(
        dataset=dataset,
        rows={scenario_id: row},
        run_id="run-sdk-offline",
        run_dir=run_dir,
        judge=None,
        scoring_mode="ragas",
    )
    await _call_evaluator(evaluator, row, outputs)
    scoring_path = scenario_root / lf.SCORING_NAME
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
    evaluation_path = scenario_root / lf.EVALUATION_NAME
    evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
    evaluation["scoring_sha256"] = scoring_sha
    evaluation_path.write_text(
        json.dumps(evaluation, ensure_ascii=False), encoding="utf-8"
    )
    if mode == "legacy":
        feedback = lf.scenario_feedback(
            execution, passing, version=lf.LEGACY_FEEDBACK_VERSION
        )
        incomplete = lf.incomplete_items(
            passing, version=lf.LEGACY_FEEDBACK_VERSION
        )
        version = None
    elif mode == "stale_v2":
        feedback = lf.scenario_feedback(execution, passing)
        incomplete = []
        version = lf.FEEDBACK_VERSION
    else:
        feedback = lf.scenario_feedback(execution, failed)
        incomplete = lf.incomplete_items(failed)
        version = lf.FEEDBACK_VERSION
    sample = {
        "example_id": example_id,
        "scenario_id": scenario_id,
        "sdk_run_id": "sdk-finalize",
        "status": "complete",
        "execution_status": execution.status,
        "execution_sha256": execution.execution_sha256,
        "scoring_relative": str(
            PurePosixPath("scenarios") / example_id / scenario_id / lf.SCORING_NAME
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
        "schema": lf.SCHEMA,
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
    (run_dir / lf.SDK_RUN_NAME).write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    return run_dir, sample, failed


async def test_finalize_recomputes_status_from_real_scoring(dataset, tmp_path):
    for mode in ("legacy", "stale_v2", "consistent_v2"):
        run_dir, sample, artifact = await _failed_scoring_run(
            dataset, tmp_path, mode
        )
        finalized = lf.finalize_run(run_dir)
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
        "schema": lf.SCHEMA,
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
    (run_dir / lf.SDK_RUN_NAME).write_text(
        json.dumps(document), encoding="utf-8"
    )
    finalized = lf.finalize_run(run_dir)
    assert finalized["status"] == "complete"
    assert finalized["scenarios"][0]["scenario_id"] == "s-sdk-finalize"

    document["samples"][0] = {
        **sample,
        "incomplete": ["cp1:runner_error"],
    }
    (run_dir / lf.SDK_RUN_NAME).write_text(
        json.dumps(document), encoding="utf-8"
    )
    finalized = lf.finalize_run(run_dir)
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
    assert lf.feedback_fields(lf.FEEDBACK_VERSION) == (
        "faithfulness",
        "factual_correctness",
        "scenario_status",
        "assertions_passed",
        "assertions_failed",
        "assertions_binding_errors",
        "assertions_runner_errors",
    )
    assert lf.feedback_fields(lf.LEGACY_FEEDBACK_VERSION) == (
        "faithfulness",
        "factual_correctness",
        "scenario_status",
    )
    assert lf.feedback_fields(lf.EXTERNAL_FEEDBACK_VERSION) == (
        lf.EXTERNAL_FEEDBACK_FIELDS
    )
    assert lf.feedback_version({}) == lf.LEGACY_FEEDBACK_VERSION
    assert lf.feedback_version({"feedback_version": 2}) == 2
    assert lf.feedback_version({"feedback_version": 3}) == (
        lf.EXTERNAL_FEEDBACK_VERSION
    )
    for value in (0, 4, True, "2"):
        with pytest.raises(lf.ScenarioSDKError, match="版本"):
            lf.feedback_version({"feedback_version": value})
    with pytest.raises(lf.ScenarioSDKError, match="版本"):
        lf.feedback_fields(0)
    assert lf.feedback_field_set_matches(
        lf.FEEDBACK_VERSION, {"scenario_status", *lf.ASSERTION_FEEDBACK_FIELDS}
    )
    assert lf.feedback_field_set_matches(
        lf.FEEDBACK_VERSION,
        {
            "faithfulness",
            "scenario_status",
            *lf.ASSERTION_FEEDBACK_FIELDS,
        },
    )
    assert not lf.feedback_field_set_matches(
        lf.FEEDBACK_VERSION, {"scenario_status"}
    )
    assert not lf.feedback_field_set_matches(
        lf.FEEDBACK_VERSION, {"scenario_status", *lf.ASSERTION_FEEDBACK_FIELDS, "extra"}
    )
    assert not lf.feedback_field_set_matches(
        lf.LEGACY_FEEDBACK_VERSION,
        {"scenario_status", *lf.ASSERTION_FEEDBACK_FIELDS},
    )
    assert lf.feedback_field_set_matches(
        lf.EXTERNAL_FEEDBACK_VERSION, lf.EXTERNAL_FEEDBACK_FIELDS
    )
    assert not lf.feedback_field_set_matches(
        lf.EXTERNAL_FEEDBACK_VERSION, ("scenario_status",)
    )
    assert lf.run_scoring_mode({}) == "ragas"
    assert lf.run_scoring_mode({"feedback_version": 3}) == "external"
    assert lf.run_scoring_mode(
        {"feedback_version": 3, "scoring_mode": "external"}
    ) == "external"
    with pytest.raises(lf.ScenarioSDKError, match="模式"):
        lf.run_scoring_mode({"feedback_version": 3, "scoring_mode": "ragas"})
    with pytest.raises(lf.ScenarioSDKError, match="模式"):
        lf.run_scoring_mode({"feedback_version": 2, "scoring_mode": "external"})


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
    feedback = lf.scenario_feedback(execution, artifact)
    assert feedback["scenario_status"] == 1.0
    assert feedback["assertions_passed"] == 1.0
    assert feedback["assertions_failed"] == 2.0
    items = lf.incomplete_items(artifact)
    failures = [item for item in items if "assertion_failed" in item]
    assert len(failures) == 2
    assert failures[0] != failures[1]
    assert all(item.startswith("e01/cp1:assertion_failed:evidence_present:") for item in failures)

    legacy = lf.scenario_feedback(
        execution, artifact, version=lf.LEGACY_FEEDBACK_VERSION
    )
    assert legacy == {"scenario_status": 1.0}
    assert lf.incomplete_items(
        artifact, version=lf.LEGACY_FEEDBACK_VERSION
    ) == []
    with pytest.raises(lf.ScenarioSDKError, match="版本"):
        lf.scenario_feedback(execution, artifact, version=4)
    with pytest.raises(lf.ScenarioSDKError, match="版本"):
        lf.incomplete_items(artifact, version=4)
    with pytest.raises(lf.ScenarioSDKError, match="外部协议"):
        lf.scenario_feedback(
            execution, artifact, version=lf.EXTERNAL_FEEDBACK_VERSION
        )
    with pytest.raises(lf.ScenarioSDKError, match="外部协议"):
        lf.incomplete_items(artifact, version=lf.EXTERNAL_FEEDBACK_VERSION)


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
    feedback = lf.scenario_feedback(execution, artifact)
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
    assert lf._run_status(coverage, [sample], [], "ragas") == "incomplete"
    sample["feedback"]["assertions_failed"] = 0.0
    assert lf._run_status(coverage, [sample], [], "ragas") == "complete"
    external_sample = {
        "execution_status": "completed",
        "incomplete": [],
        "generation_scoring_status": "pending_external",
        "feedback": {
            "assertions_failed": 0.0,
            "generation_scoring_completed": 0.0,
        },
    }
    assert (
        lf._run_status(coverage, [external_sample], [], "external")
        == lf.EXTERNAL_RUN_STATUS
    )
