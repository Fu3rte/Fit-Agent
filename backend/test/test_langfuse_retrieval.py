import asyncio
import copy
import json
import threading
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from langfuse import Evaluation
from langfuse._client.datasets import DatasetClient
from langfuse.api.commons.types.dataset import Dataset
from langfuse.api.commons.types.dataset_item import DatasetItem
from langfuse.api.commons.types.dataset_status import DatasetStatus
from langfuse.api.commons.types.observation_level import ObservationLevel
from langfuse.api.commons.types.score_source import ScoreSource
from langfuse.api.core.api_error import ApiError
from langfuse.api.experiments.types.experiment import Experiment
from langfuse.api.experiments.types.experiment_item import ExperimentItem
from langfuse.api.experiments.types.experiment_items_response import (
    ExperimentItemsResponse,
)
from langfuse.api.experiments.types.experiments_response import ExperimentsResponse
from langfuse.api.experiments.types.experiments_response_meta import (
    ExperimentsResponseMeta,
)
from langfuse.api.scores.types.score_v3 import ScoreV3_Numeric
from langfuse.experiment import ExperimentItemResult, ExperimentResult

from evaluation import langfuse_retrieval as lf
from evaluation import retrieval
from evaluation import retrieval_support as support
from evaluation.contracts import RelevanceLabel, RetrievalInput
from evaluation.dataset import load_for_evaluation
from evaluation.retrieval import upload_payload
from evaluation.retrieval_metrics import evaluate
from test.evaluation_fixtures import build_dataset
from test.test_retrieval_rerank_evaluation import rerank_document

DATASET_PATH = support.BACKEND / "dataset/evaluation/rebuild-v1"
PROJECT_ID = "memory-project"
ENVIRONMENT = "default"


@pytest.fixture(scope="module")
def dataset():
    return load_for_evaluation(DATASET_PATH)


@pytest.fixture(scope="module")
def rows(dataset):
    return lf.example_rows(dataset)


@pytest.fixture(scope="module")
def reviewed(dataset):
    return dataset.reviewed_corpus_ids()


def now_utc() -> datetime:
    return datetime.now(UTC)


def score_v3(name: str, value: float) -> ScoreV3_Numeric:
    stamp = now_utc()
    return ScoreV3_Numeric(
        id=str(uuid4()),
        project_id=PROJECT_ID,
        name=name,
        value=value,
        source=ScoreSource.EVAL,
        timestamp=stamp,
        environment=ENVIRONMENT,
        created_at=stamp,
        updated_at=stamp,
    )


def experiment_item(
    example_id: str,
    *,
    output=None,
    scores=None,
    level=ObservationLevel.DEFAULT,
    item_metadata=None,
    experiment_id="experiment-1",
    experiment_name="run-1-hybrid-validation",
) -> ExperimentItem:
    start = now_utc()
    return ExperimentItem(
        id=str(uuid4()),
        trace_id=uuid4().hex,
        start_time=start,
        end_time=start,
        level=level,
        environment=ENVIRONMENT,
        experiment_id=experiment_id,
        experiment_name=experiment_name,
        experiment_item_id=example_id,
        output=output,
        scores=list(scores or []),
        experiment_item_metadata=dict(item_metadata or {}),
    )


class MemoryExperimentsApi:
    def __init__(self, client: "MemoryLangfuse") -> None:
        self._client = client

    def list(
        self,
        *,
        from_start_time,
        fields=None,
        id=None,
        name=None,
        dataset_id=None,
        cursor=None,
        **kwargs,
    ):
        self._client._count_read()
        wanted_id = set((id or "").split(",")) if id else None
        wanted_name = set((name or "").split(",")) if name else None
        wanted_dataset = set((dataset_id or "").split(",")) if dataset_id else None
        data = [
            experiment
            for experiment in self._client.experiments.values()
            if (wanted_id is None or experiment.id in wanted_id)
            and (wanted_name is None or experiment.name in wanted_name)
            and (wanted_dataset is None or str(experiment.dataset_id) in wanted_dataset)
        ]
        return ExperimentsResponse(data=data, meta=ExperimentsResponseMeta())

    def list_items(
        self,
        *,
        from_start_time,
        experiment_id=None,
        fields=None,
        limit=None,
        cursor=None,
        **kwargs,
    ):
        self._client._count_read()
        data = self._client.experiment_items.get(experiment_id, [])
        return ExperimentItemsResponse(data=list(data), meta=ExperimentsResponseMeta())


class MemoryApi:
    def __init__(self, client: "MemoryLangfuse") -> None:
        self.experiments = MemoryExperimentsApi(client)


class MemoryLangfuse:
    # 内存实现执行本模块实际调用的 client 方法面：run_experiment 在内存真实执行
    # task 与 evaluator 并登记 score；读 API 从登记构造真实 SDK 类型对象。

    def __init__(self) -> None:
        self.datasets: dict[str, dict] = {}
        self.experiments: dict[str, Experiment] = {}
        self.experiment_items: dict[str, list[ExperimentItem]] = {}
        self.pending: dict[str, tuple[Experiment, list[ExperimentItem]]] = {}
        self.auth_checks = 0
        self.api = MemoryApi(self)
        # 云端延迟/部分落库模拟：delay_reveal 提交后暂不可读；reveal_after_reads
        # 在第 N 次读 API 时才可读；commit_only 首次提交仅落库指定样例。
        self.delay_reveal = False
        self.reveal_after_reads = None
        self.read_count = 0
        self.commit_only = None
        self.task_item_ids: list[str] = []

    def _count_read(self) -> None:
        self.read_count += 1
        if self.reveal_after_reads is not None and self.read_count >= self.reveal_after_reads:
            self.reveal_pending()

    def auth_check(self) -> bool:
        self.auth_checks += 1
        return True

    def create_dataset(self, *, name, description=None, metadata=None, **kwargs):
        assert name not in self.datasets, "重复创建同名远端数据集"
        stamp = now_utc()
        record = Dataset(
            id=f"ds-{uuid4().hex[:12]}",
            name=name,
            description=description,
            metadata=copy.deepcopy(metadata),
            project_id=PROJECT_ID,
            created_at=stamp,
            updated_at=stamp,
        )
        self.datasets[name] = {"dataset": record, "items": {}}
        return record

    def get_dataset(self, name, **kwargs):
        entry = self.datasets.get(name)
        if entry is None:
            raise ApiError(status_code=404, body="dataset not found")
        return DatasetClient(
            dataset=entry["dataset"],
            items=list(entry["items"].values()),
            langfuse_client=self,
        )

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
        assert id is not None, "内存实现要求显式 id upsert"
        entry = self.datasets[dataset_name]
        stamp = now_utc()
        entry["items"][id] = DatasetItem(
            id=id,
            status=DatasetStatus.ACTIVE,
            input=copy.deepcopy(input),
            expected_output=copy.deepcopy(expected_output),
            metadata=copy.deepcopy(metadata),
            dataset_id=entry["dataset"].id,
            dataset_name=dataset_name,
            created_at=stamp,
            updated_at=stamp,
            media_references=[],
        )

    def run_experiment(
        self,
        *,
        name,
        run_name=None,
        description=None,
        data,
        task,
        evaluators=None,
        max_concurrency=50,
        metadata=None,
        experiment_id=None,
        delay_reveal=None,
    ):
        if delay_reveal is None:
            delay_reveal = self.delay_reveal
        dataset_id = next(
            (
                item.dataset_id
                for item in data
                if not isinstance(item, dict) and getattr(item, "dataset_id", None)
            ),
            None,
        )
        if experiment_id is None:
            payload = json.dumps(
                ["langfuse-experiment-v1", PROJECT_ID, dataset_id, run_name],
                separators=(",", ":"),
                ensure_ascii=False,
            )
            experiment_id = sha256(payload.encode("utf-8")).hexdigest()[:16]
        executed = asyncio.run(
            self._execute(data, task, list(evaluators or []), max_concurrency, experiment_id)
        )
        records = [entry[0] for entry in executed]
        start = min(item.start_time for item in records)
        end = max(item.start_time for item in records)
        experiment = Experiment(
            id=experiment_id,
            name=run_name,
            description=description,
            start_time=start,
            end_time=end,
            item_count=len(records),
            dataset_id=dataset_id,
            metadata=copy.deepcopy(metadata),
        )
        if delay_reveal:
            self.pending[experiment_id] = (experiment, records)
        else:
            self._commit(experiment, records)
        return ExperimentResult(
            name=name,
            run_name=run_name,
            description=description,
            item_results=[
                ExperimentItemResult(
                    item=source_item,
                    output=output,
                    evaluations=list(evaluations),
                    trace_id=record.trace_id,
                    experiment_id=experiment_id,
                )
                for record, source_item, output, evaluations in executed
            ],
            run_evaluations=[],
            experiment_id=experiment_id,
            experiment_url=None,
        )

    def _commit(self, experiment: Experiment, records: list[ExperimentItem]) -> None:
        # 服务端合并语义：同 id 实验追加条目并保留首次身份；commit_only 模拟部分落库（仅首次提交生效）。
        if self.commit_only is not None:
            records = [
                record
                for record in records
                if record.experiment_item_metadata.get("query_id") in self.commit_only
            ]
            self.commit_only = None
        existing = self.experiment_items.get(experiment.id)
        if existing is None:
            stored = experiment
            if len(records) != experiment.item_count:
                stored = experiment.model_copy(update={"item_count": len(records)})
            self.experiments[experiment.id] = stored
            self.experiment_items[experiment.id] = list(records)
            return
        first = self.experiments[experiment.id]
        self.experiments[experiment.id] = first.model_copy(
            update={
                "item_count": first.item_count + len(records),
                "end_time": max(first.end_time, experiment.end_time),
            }
        )
        self.experiment_items[experiment.id] = existing + list(records)

    def reveal_pending(self) -> None:
        for experiment, records in self.pending.values():
            self._commit(experiment, records)
        self.pending.clear()

    async def _execute(self, data, task, evaluators, max_concurrency, experiment_id):
        semaphore = asyncio.Semaphore(max_concurrency)

        async def process(item):
            async with semaphore:
                item_id = str(item["id"] if isinstance(item, dict) else item.id)
                input_data = item["input"] if isinstance(item, dict) else item.input
                expected = (
                    item.get("expected_output")
                    if isinstance(item, dict)
                    else item.expected_output
                )
                metadata = (
                    item.get("metadata") if isinstance(item, dict) else item.metadata
                )
                self.task_item_ids.append(item_id)
                output = task(item=item)
                if asyncio.iscoroutine(output):
                    output = await output
                evaluations = []
                for evaluator in evaluators:
                    result = evaluator(
                        input=input_data,
                        output=output,
                        expected_output=expected,
                        metadata=metadata,
                    )
                    if asyncio.iscoroutine(result):
                        result = await result
                    candidates = result if isinstance(result, list) else [result]
                    evaluations.extend(
                        candidate
                        if isinstance(candidate, Evaluation)
                        else Evaluation(**candidate)
                        for candidate in candidates
                    )
                record = experiment_item(
                    item_id,
                    output=output,
                    scores=[
                        score_v3(entry.name, float(entry.value))
                        for entry in evaluations
                    ],
                    item_metadata=metadata,
                    experiment_id=experiment_id,
                )
                return record, item, output, list(evaluations)

        executed = await asyncio.gather(*(process(item) for item in data))
        return list(executed)


@pytest.fixture
def isolated_root(tmp_path, monkeypatch):
    monkeypatch.setattr(lf, "DATASET_ROOT", tmp_path / "datasets")
    return tmp_path


def target_outputs(
    *,
    query_id: str,
    identity: dict,
    pool: list[str],
    final: list[str],
    group: str = "hybrid",
) -> dict:
    return {
        "query_id": query_id,
        "dataset_content_sha256": identity["content_sha256"],
        "strategy": group,
        "candidate_pool": pool,
        "final_model_input": final,
    }


def evaluator_payload(dataset, query_id: str, pool: list[str], final: list[str]):
    query = next(item for item in dataset.queries if item["query_id"] == query_id)
    rows = lf.example_rows(dataset)
    row = next(item for item in rows if item["inputs"]["query_id"] == query_id)
    outputs = target_outputs(
        query_id=query_id,
        identity=lf.dataset_binding(dataset),
        pool=pool,
        final=final,
    )
    return row["inputs"], outputs, row["outputs"], query


def feedback_map(result) -> dict[str, float]:
    return {item.name: item.value for item in result}


def test_example_rows_cover_all_queries_without_gold_in_inputs(dataset, rows):
    assert len(rows) == 150
    assert len({row["id"] for row in rows}) == 150
    assert {row["inputs"]["query_id"] for row in rows} == {
        query["query_id"] for query in dataset.queries
    }
    for row in rows:
        assert set(row["inputs"]) == {"query_id", "query", "purpose", "conditions", "dataset"}
        assert set(row["outputs"]) == {"query_id", "scenario_id", "expect_empty", "labels"}
        assert "labels" not in row["inputs"]
        qrels = [item for item in dataset.qrels if item["query_id"] == row["inputs"]["query_id"]]
        assert row["outputs"]["labels"] == sorted(
            [{"exercise_id": item["corpus_id"], "grade": item["grade"]} for item in qrels],
            key=lambda item: item["exercise_id"],
        )
        assert row["metadata"]["content_sha256"] == support.sha256_text(
            support.json_text(
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


def test_example_ids_bind_dataset_identity(dataset, rows):
    name = lf.dataset_name(dataset)
    assert name == f"fit-agent-retrieval-sdk-langfuse-{dataset.version}-{dataset.content_sha256[:12]}"
    assert rows[0]["id"] == lf.stable_example_id(name, rows[0]["inputs"]["query_id"])
    assert rows[0]["id"] == str(
        uuid5(NAMESPACE_URL, name + ":" + rows[0]["inputs"]["query_id"])
    )
    first = lf.example_rows(dataset)
    second = lf.example_rows(dataset)
    assert first == second


def test_validation_selection_is_fixed_six(dataset):
    selected = lf.select_validation_queries(dataset)
    assert [item["query_id"] for item in selected] == [
        "q-lookup-001",
        "q-lookup-051",
        "q-generation-001",
        "q-generation-036",
        "q-replacement-001",
        "q-replacement-036",
    ]
    coverage = {(item["purpose"], item["expect_empty"]) for item in selected}
    assert coverage == {
        (purpose, expect_empty)
        for purpose in lf.PURPOSES
        for expect_empty in (False, True)
    }
    formal = lf.select_formal_queries(dataset)
    assert [item["query_id"] for item in formal] == [
        query["query_id"] for query in dataset.queries
    ]
    from types import SimpleNamespace

    with pytest.raises(ValueError, match="150"):
        lf.select_formal_queries(SimpleNamespace(queries=[{}, {}]))


def test_sdk_identity_includes_new_module():
    identity = lf.sdk_code_identity()
    assert identity["files"]["evaluation/langfuse_retrieval.py"] == support.sha256(
        support.BACKEND / "evaluation/langfuse_retrieval.py"
    )
    assert "evaluation/retrieval.py" in identity["files"]
    assert identity["versions"]["langfuse"].startswith("5.")


def test_validated_parameters_match_frozen_baseline():
    values = lf.validated_parameters()
    assert values["fusion"] == "rrf"
    assert values["rrf_k"] == 60
    assert values["bm25_depth"] == 100
    assert values["dense_depth"] == 100
    assert values["threshold"] == 0.0
    assert values["candidate_pool_limit"] == 20
    assert values["final_input_limit"] == 5


def test_target_query_rejects_wrong_digest_and_cache_text(dataset):
    row = lf.example_rows(dataset)[0]
    identity = lf.dataset_binding(dataset)
    query = lf.target_query(
        row["inputs"], dataset_identity=identity, expected_vector_text=row["inputs"]["query"]
    )
    assert query["query_id"] == row["inputs"]["query_id"]
    with pytest.raises(ValueError, match="数据身份不符"):
        lf.target_query(
            {**row["inputs"], "dataset": {**identity, "content_sha256": "0" * 64}},
            dataset_identity=identity,
            expected_vector_text=None,
        )
    with pytest.raises(ValueError, match="缓存查询文本"):
        lf.target_query(
            row["inputs"],
            dataset_identity=identity,
            expected_vector_text=row["inputs"]["query"] + "x",
        )


async def test_evaluator_matches_existing_metrics(dataset, reviewed):
    query_id = lf.select_validation_queries(dataset)[0]["query_id"]
    rows = lf.example_rows(dataset)
    reference = next(row["outputs"] for row in rows if row["inputs"]["query_id"] == query_id)
    positives = [item["exercise_id"] for item in reference["labels"] if item["grade"] >= 1]
    extras = sorted(reviewed[query_id] - set(positives))[:2]
    pool = positives[:2] + extras
    final = positives[:2]
    inputs, outputs, reference_outputs, _ = evaluator_payload(dataset, query_id, pool, final)
    result = await lf.make_evaluator("hybrid", lf.dataset_binding(dataset), reviewed)(
        input=inputs, output=outputs, expected_output=reference_outputs, metadata=None
    )
    item = RetrievalInput(
        query_id=query_id,
        expect_empty=False,
        labels=[
            RelevanceLabel(exercise_id=label["exercise_id"], grade=label["grade"])
            for label in reference["labels"]
        ],
        candidate_pool=pool,
        final_model_input=final,
    )
    expected = {metric.name: metric.value for metric in evaluate(item).metrics}
    assert feedback_map(result) == expected
    assert list(feedback_map(result)) == list(lf.RETRIEVAL_METRICS)


async def test_evaluator_empty_samples_only_report_false_return(dataset, reviewed):
    query_id = lf.select_validation_queries(dataset)[1]["query_id"]
    inputs, outputs, reference_outputs, _ = evaluator_payload(dataset, query_id, [], [])
    result = await lf.make_evaluator("hybrid", lf.dataset_binding(dataset), reviewed)(
        input=inputs, output=outputs, expected_output=reference_outputs, metadata=None
    )
    assert feedback_map(result) == {"false_return": 0.0}
    assert lf.expected_feedback_keys(reference_outputs) == {"false_return"}


async def test_evaluator_rejects_identity_mismatch(dataset, reviewed):
    query_id = lf.select_validation_queries(dataset)[0]["query_id"]
    inputs, outputs, reference_outputs, _ = evaluator_payload(
        dataset, query_id, ["0025", "0030"], ["0025"]
    )
    evaluator = lf.make_evaluator("hybrid", lf.dataset_binding(dataset), reviewed)
    with pytest.raises(ValueError, match="query_id"):
        await evaluator(
            input=inputs,
            output={**outputs, "query_id": "q-other"},
            expected_output=reference_outputs,
            metadata=None,
        )
    with pytest.raises(ValueError, match="金标 query_id"):
        await evaluator(
            input=inputs,
            output=outputs,
            expected_output={**reference_outputs, "query_id": "q-other"},
            metadata=None,
        )
    with pytest.raises(ValueError, match="输出数据摘要"):
        await evaluator(
            input=inputs,
            output={**outputs, "dataset_content_sha256": "0" * 64},
            expected_output=reference_outputs,
            metadata=None,
        )
    with pytest.raises(ValueError, match="输出检索组"):
        await evaluator(
            input=inputs,
            output={**outputs, "strategy": "bm25"},
            expected_output=reference_outputs,
            metadata=None,
        )


def test_sync_is_idempotent_and_completes_missing_examples(dataset, isolated_root):
    client = MemoryLangfuse()
    first = lf.sync_dataset(client, dataset)
    assert first["dataset_status"] == "created"
    assert len(client.datasets[first["dataset_name"]]["items"]) == 150
    second = lf.sync_dataset(client, dataset)
    assert second["dataset_status"] == "reused"
    assert second["dataset_id"] == first["dataset_id"]
    assert len(client.datasets[first["dataset_name"]]["items"]) == 150
    assert second["example_ids"] == first["example_ids"]

    removed = next(iter(client.datasets[first["dataset_name"]]["items"]))
    client.datasets[first["dataset_name"]]["items"].pop(removed)
    third = lf.sync_dataset(client, dataset)
    assert third["dataset_status"] == "reused"
    assert removed in client.datasets[first["dataset_name"]]["items"]
    assert len(client.datasets[first["dataset_name"]]["items"]) == 150


def test_sync_recovers_after_remote_dataset_removed(dataset, isolated_root):
    client = MemoryLangfuse()
    first = lf.sync_dataset(client, dataset)
    client.datasets.pop(first["dataset_name"])
    recovered = lf.sync_dataset(client, dataset)
    assert recovered["dataset_status"] == "created"
    assert recovered["dataset_id"] != first["dataset_id"]
    assert len(client.datasets[recovered["dataset_name"]]["items"]) == 150


def test_sync_blocks_unknown_marker_and_conflicting_example(dataset, isolated_root):
    client = MemoryLangfuse()
    receipt = lf.sync_dataset(client, dataset)
    remote = client.datasets[receipt["dataset_name"]]["dataset"]
    remote = remote.model_copy(update={"metadata": {"marker": "unknown"}})
    client.datasets[receipt["dataset_name"]]["dataset"] = remote
    with pytest.raises(ValueError, match="缺少字段"):
        lf.sync_dataset(client, dataset)
    remote = remote.model_copy(
        update={"metadata": {**lf.dataset_marker(dataset), "unknown": True}}
    )
    client.datasets[receipt["dataset_name"]]["dataset"] = remote
    with pytest.raises(ValueError, match="未知字段"):
        lf.sync_dataset(client, dataset)

    client = MemoryLangfuse()
    receipt = lf.sync_dataset(client, dataset)
    example_id = receipt["example_ids"]["q-lookup-001"]
    stored = client.datasets[receipt["dataset_name"]]["items"][example_id]
    client.datasets[receipt["dataset_name"]]["items"][example_id] = stored.model_copy(
        update={"input": {**stored.input, "query": "被修改"}}
    )
    with pytest.raises(ValueError, match="inputs 不符"):
        lf.sync_dataset(client, dataset)


class SingleItemApi:
    def __init__(self, items: list[ExperimentItem]) -> None:
        self.items = items
        self.reads = 0

    @property
    def experiments(self):
        return self

    def list_items(self, **kwargs):
        self.reads += 1
        return ExperimentItemsResponse(data=list(self.items), meta=ExperimentsResponseMeta())


def verify_client(items: list[ExperimentItem]):
    class Client:
        def __init__(self) -> None:
            self.api = SingleItemApi(items)

    return Client()


def test_verify_group_requires_every_item_and_score(dataset, reviewed):
    row = lf.example_rows(dataset)[0]
    query_id = row["inputs"]["query_id"]
    identity = lf.dataset_binding(dataset)
    positives = [
        item["exercise_id"]
        for item in row["outputs"]["labels"]
        if item["grade"] >= 1
    ]
    outputs = target_outputs(
        query_id=query_id, identity=identity, pool=positives[:2], final=positives[:1]
    )
    expected = lf.expected_feedback(
        "hybrid", row["inputs"], outputs, row["outputs"], identity, reviewed
    )
    item = experiment_item(
        row["id"],
        output=outputs,
        scores=[score_v3(name, value) for name, value in expected.items()],
    )
    client = verify_client([item])
    samples, coverage = lf.verify_group(
        client,
        group="hybrid",
        dataset_identity=identity,
        reviewed=reviewed,
        rows={row["id"]: row},
        selected_example_ids=[row["id"]],
        experiment_id="experiment-1",
        from_start_time=now_utc(),
    )
    assert coverage["status"] == "complete"
    assert samples[0]["status"] == "complete"
    assert samples[0]["feedback"] == pytest.approx(expected)

    client = verify_client(
        [experiment_item(row["id"], output=outputs, scores=[])]
    )
    samples, coverage = lf.verify_group(
        client,
        group="hybrid",
        dataset_identity=identity,
        reviewed=reviewed,
        rows={row["id"]: row},
        selected_example_ids=[row["id"]],
        experiment_id="experiment-1",
        from_start_time=now_utc(),
    )
    assert coverage["status"] == "incomplete"
    assert samples[0]["status"] == "failed"
    assert "反馈指标集合不符" in samples[0]["error"]

    drifted = dict(expected)
    drifted["recall@20"] = expected["recall@20"] + 1e-3
    client = verify_client(
        [
            experiment_item(
                row["id"],
                output=outputs,
                scores=[score_v3(name, value) for name, value in drifted.items()],
            )
        ]
    )
    samples, coverage = lf.verify_group(
        client,
        group="hybrid",
        dataset_identity=identity,
        reviewed=reviewed,
        rows={row["id"]: row},
        selected_example_ids=[row["id"]],
        experiment_id="experiment-1",
        from_start_time=now_utc(),
    )
    assert samples[0]["status"] == "failed"
    assert "远端反馈与本地评分不一致" in samples[0]["error"]

    tolerated = dict(expected)
    tolerated["recall@20"] = expected["recall@20"] + 1e-6
    client = verify_client(
        [
            experiment_item(
                row["id"],
                output=outputs,
                scores=[score_v3(name, value) for name, value in tolerated.items()],
            )
        ]
    )
    samples, coverage = lf.verify_group(
        client,
        group="hybrid",
        dataset_identity=identity,
        reviewed=reviewed,
        rows={row["id"]: row},
        selected_example_ids=[row["id"]],
        experiment_id="experiment-1",
        from_start_time=now_utc(),
    )
    assert samples[0]["status"] == "complete"

    failed = experiment_item(
        row["id"], output="Error: target failed", level=ObservationLevel.ERROR
    )
    samples, _ = lf.verify_group(
        verify_client([failed]),
        group="hybrid",
        dataset_identity=identity,
        reviewed=reviewed,
        rows={row["id"]: row},
        selected_example_ids=[row["id"]],
        experiment_id="experiment-1",
        from_start_time=now_utc(),
    )
    assert samples[0]["status"] == "failed"
    assert "target failed" in samples[0]["error"]


def test_verify_group_marks_review_blocked_evidence(dataset, reviewed):
    row = lf.example_rows(dataset)[0]
    query_id = row["inputs"]["query_id"]
    identity = lf.dataset_binding(dataset)
    unreviewed = next(
        corpus_id
        for corpus_id in sorted(reviewed[query_id] | {"9999"})
        if corpus_id not in reviewed[query_id]
    )
    pool = [unreviewed, *[label["exercise_id"] for label in row["outputs"]["labels"] if label["grade"] >= 1][:2]]
    payload = target_outputs(query_id=query_id, identity=identity, pool=pool, final=pool[:2])
    item = experiment_item(row["id"], output=payload, scores=[])
    samples, coverage = lf.verify_group(
        verify_client([item]),
        group="hybrid",
        dataset_identity=identity,
        reviewed=reviewed,
        rows={row["id"]: row},
        selected_example_ids=[row["id"]],
        experiment_id="experiment-1",
        from_start_time=now_utc(),
    )
    assert coverage["status"] == "incomplete"
    assert coverage["unknown_candidates"] == {query_id: [unreviewed]}
    assert samples[0]["status"] == "blocked_unreviewed_candidates"
    assert samples[0]["outputs"] == payload
    assert samples[0]["unknown_candidates"] == [unreviewed]
    assert unreviewed in samples[0]["error"]


def test_run_experiment_executes_task_and_evaluator_and_registers_scores(
    dataset, reviewed, isolated_root
):
    client = MemoryLangfuse()
    receipt = lf.sync_dataset(client, dataset)
    selected = lf.select_validation_queries(dataset)[:2]
    items = lf._load_items(
        client,
        receipt["dataset_name"],
        {row["id"]: row for row in lf.example_rows(dataset)},
        [receipt["example_ids"][item["query_id"]] for item in selected],
    )
    rows = {row["id"]: row for row in lf.example_rows(dataset)}
    outputs_map = {}
    for row in rows.values():
        query_id = row["inputs"]["query_id"]
        positives = [
            label["exercise_id"]
            for label in row["outputs"]["labels"]
            if label["grade"] >= 1
        ]
        outputs_map[query_id] = target_outputs(
            query_id=query_id,
            identity=lf.dataset_binding(dataset),
            pool=positives[:2],
            final=positives[:1],
        )

    def task(*, item, **kwargs):
        return outputs_map[item.input["query_id"]]

    results = client.run_experiment(
        name="run-hybrid",
        run_name="run-hybrid-validation",
        data=items,
        task=task,
        evaluators=[lf.make_evaluator("hybrid", lf.dataset_binding(dataset), reviewed)],
        max_concurrency=lf.MAX_CONCURRENCY,
        metadata={"local_run_id": "run", "group": "hybrid"},
    )
    assert len(results.item_results) == 2
    experiment_id = results.experiment_id
    payload = json.dumps(
        [
            "langfuse-experiment-v1",
            PROJECT_ID,
            client.datasets[receipt["dataset_name"]]["dataset"].id,
            "run-hybrid-validation",
        ],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert experiment_id == sha256(payload.encode("utf-8")).hexdigest()[:16]
    samples, coverage = lf.verify_group(
        client,
        group="hybrid",
        dataset_identity=lf.dataset_binding(dataset),
        reviewed=reviewed,
        rows=rows,
        selected_example_ids=[
            receipt["example_ids"][item["query_id"]] for item in selected
        ],
        experiment_id=experiment_id,
        from_start_time=now_utc(),
    )
    assert coverage["status"] == "complete"
    assert all(sample["status"] == "complete" for sample in samples)
    lf.verify_experiment_metadata(
        client,
        experiment_id,
        {"local_run_id": "run", "group": "hybrid"},
        now_utc(),
    )
    stored = client.experiments[experiment_id]
    corrupted = stored.model_copy(update={"metadata": {"local_run_id": "other"}})
    client.experiments[experiment_id] = corrupted
    with pytest.raises(ValueError, match="远端实验 metadata 不符: local_run_id"):
        lf.verify_experiment_metadata(
            client,
            experiment_id,
            {"local_run_id": "run", "group": "hybrid"},
            now_utc(),
        )


def test_verify_group_until_complete_recovers_after_delayed_persist(
    dataset, reviewed, isolated_root, monkeypatch
):
    monkeypatch.setattr(lf, "REMOTE_VERIFY_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr(lf, "REMOTE_VERIFY_POLL_SECONDS", 0.02)
    client = MemoryLangfuse()
    receipt = lf.sync_dataset(client, dataset)
    row = lf.example_rows(dataset)[0]
    query_id = row["inputs"]["query_id"]
    identity = lf.dataset_binding(dataset)
    positives = [
        label["exercise_id"]
        for label in row["outputs"]["labels"]
        if label["grade"] >= 1
    ]
    outputs = target_outputs(
        query_id=query_id, identity=identity, pool=positives[:2], final=positives[:1]
    )

    def task(*, item, **kwargs):
        return outputs

    client.run_experiment(
        name="run-hybrid",
        run_name="run-hybrid-validation",
        data=[client.get_dataset(receipt["dataset_name"]).items[0]],
        task=task,
        evaluators=[
            lf.make_evaluator("hybrid", lf.dataset_binding(dataset), reviewed)
        ],
        delay_reveal=True,
    )
    experiment_id = next(iter(client.pending))
    timer = threading.Timer(0.1, client.reveal_pending)
    timer.start()
    samples, coverage = lf.verify_group_until_complete(
        client,
        group="hybrid",
        dataset_identity=identity,
        reviewed=reviewed,
        rows={row["id"]: row},
        selected_example_ids=[row["id"]],
        experiment_id=experiment_id,
        from_start_time=now_utc(),
    )
    timer.join()
    assert coverage["status"] == "complete"
    assert samples[0]["status"] == "complete"


def test_verify_group_until_complete_stops_at_deadline(
    dataset, reviewed, isolated_root, monkeypatch
):
    monkeypatch.setattr(lf, "REMOTE_VERIFY_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(lf, "REMOTE_VERIFY_POLL_SECONDS", 0.02)
    client = MemoryLangfuse()
    row = lf.example_rows(dataset)[0]
    client.experiment_items["experiment-1"] = []
    samples, coverage = lf.verify_group_until_complete(
        client,
        group="hybrid",
        dataset_identity=lf.dataset_binding(dataset),
        reviewed=reviewed,
        rows={row["id"]: row},
        selected_example_ids=[row["id"]],
        experiment_id="experiment-1",
        from_start_time=now_utc(),
    )
    assert coverage["status"] == "incomplete"
    assert samples[0]["status"] == "missing"


def test_experiment_metadata_records_full_identity(dataset):
    metadata = lf.experiment_metadata(
        run_id="run-1",
        group=lf.RERANK_GROUP,
        purpose="validation",
        dataset=dataset,
        parameters=lf.validated_parameters(),
        collection="collection",
        index_manifest_sha256="a" * 64,
        vector_identity_sha256="b" * 64,
        vector_required=True,
        selected=lf.select_validation_queries(dataset),
        identity=lf.sdk_code_identity(),
        rerank_identity={
            "model": "m",
            "text_version": "v",
            "endpoint_sha256": support.sha256_text("https://e"),
        },
    )
    assert metadata["experiment"] == "rerank"
    assert metadata["rerank_identity"]["endpoint_sha256"] == support.sha256_text("https://e")
    assert metadata["sample_scope"]["count"] == 6
    assert metadata["max_concurrency"] == lf.MAX_CONCURRENCY
    assert metadata["num_repetitions"] == lf.NUM_REPETITIONS


def test_comparison_report_records_same_pool_evidence():
    samples = [
        {
            "query_id": "q-1",
            "outputs": {
                "candidate_pool": ["0001", "0002"],
                "scores": {"0001": 0.9, "0002": 0.8},
                "final_model_input": ["0002", "0001"],
                "baseline": {
                    "candidate_pool": ["0001", "0002"],
                    "scores": {"0001": 0.9, "0002": 0.8},
                    "final_model_input": ["0001", "0002"],
                },
            },
        }
    ]
    report = lf.comparison_report(samples)
    assert report["status"] == "complete"
    assert report["order_changed"] == 1
    assert report["rows"][0]["candidate_pool_identical"] is True
    broken = copy.deepcopy(samples)
    broken[0]["outputs"]["baseline"]["candidate_pool"] = ["0002", "0001"]
    with pytest.raises(ValueError, match="同池候选不一致"):
        lf.comparison_report(broken)


def test_group_evidence_writes_one_file_per_sample(tmp_path):
    samples = [
        {"query_id": "q-lookup-001", "status": "complete"},
        {"query_id": "q-lookup-051", "status": "complete"},
    ]
    lf.write_group_evidence(tmp_path, "bm25", samples, {"status": "complete"})
    assert sorted(path.name for path in (tmp_path / "bm25" / "samples").glob("*.json")) == [
        "q-lookup-001.json",
        "q-lookup-051.json",
    ]
    assert (tmp_path / "bm25" / "result.json").is_file()


async def test_concurrency_recorder_observes_overlap():
    recorder = lf.ConcurrencyRecorder()
    release = asyncio.Event()

    async def worker(index: int):
        started = await recorder.start(f"q{index}")
        await release.wait()
        await recorder.finish(f"q{index}", started)

    tasks = [asyncio.create_task(worker(index)) for index in range(3)]
    await asyncio.sleep(0.01)
    release.set()
    await asyncio.gather(*tasks)
    report = recorder.report()
    assert report["calls"] == 3
    assert report["max_in_flight"] == 3
    assert report["overlap_pairs"] == 3
    assert report["max_in_flight"] <= lf.MAX_CONCURRENCY


def test_make_client_requires_env_credentials(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    with pytest.raises(ValueError, match="缺少 LANGFUSE_PUBLIC_KEY"):
        lf.make_client()
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    with pytest.raises(ValueError, match="缺少 LANGFUSE_SECRET_KEY"):
        lf.make_client()


def frozen_payload(name="fit-agent-retrieval-langfuse-test-x", run_name="run-x-hybrid"):
    rows = []
    for qid, pool in (("q-1", ["0001", "0002"]), ("q-2", [])):
        rows.append(
            {
                "row_id": str(uuid5(NAMESPACE_URL, name + ":" + qid)),
                "inputs": {"query": f"查询{qid}"},
                "expected_outputs": {"expect_empty": qid == "q-2"},
                "actual_outputs": {"candidate_pool": pool},
                "evaluation_scores": [
                    {"key": "hard_constraint_violation", "score": 0},
                    {"key": "budget_compliant", "score": 1},
                ],
                "run_metadata": {"query_id": qid},
            }
        )
    return {
        "dataset_name": name,
        "run_name": run_name,
        "dataset_marker": {
            "marker": "fit-agent-retrieval-upload-langfuse-1",
            "example_count": len(rows),
            "rows_sha256": "0" * 64,
        },
        "experiment_metadata": {"group": "hybrid", "evidence_replay": True},
        "summary_experiment_scores": [
            {"key": "hard_constraint_violation", "score": 0}
        ],
        "results": rows,
    }


def test_submit_frozen_experiment_syncs_and_verifies(isolated_root):
    client = MemoryLangfuse()
    payload = frozen_payload()
    pending_path = isolated_root / "langfuse-hybrid.pending.json"
    receipt = lf.submit_frozen_experiment(
        client,
        payload=payload,
        verify_from=now_utc(),
        pending_path=pending_path,
        source_sha256="a" * 64,
    )
    # submit 成功后 pending 保留并含实验身份，删除权在调用方持久化终态回执之后
    pending = support.load_document(pending_path)
    assert pending["schema"] == lf.PENDING_SCHEMA
    assert pending["status"] == "pending"
    assert pending["experiment_id"] == receipt["experiment_id"]
    assert pending["experiment_url"] == receipt["experiment_url"]
    assert receipt["dataset_name"] == payload["dataset_name"]
    assert receipt["run_name"] == payload["run_name"]
    stored = client.datasets[payload["dataset_name"]]
    assert {item.id for item in stored["items"].values()} == {
        row["row_id"] for row in payload["results"]
    }
    experiment_id = receipt["experiment_id"]
    items = client.experiment_items[experiment_id]
    assert len(items) == 2
    feedback = {
        metadata_query_id: scores
        for metadata_query_id, scores in (
            (item.experiment_item_metadata["query_id"], lf._item_scores(item))
            for item in items
        )
    }
    assert feedback == {
        row["run_metadata"]["query_id"]: {
            item["key"]: item["score"] for item in row["evaluation_scores"]
        }
        for row in payload["results"]
    }
    lf.verify_uploaded_experiment(
        client,
        experiment_id=experiment_id,
        dataset_id=receipt["dataset_id"],
        run_name=receipt["run_name"],
        from_start_time=now_utc(),
    )
    with pytest.raises(ValueError, match="远端实验数据集与上传回执不一致"):
        lf.verify_uploaded_experiment(
            client,
            experiment_id=experiment_id,
            dataset_id="ds-other",
            run_name=receipt["run_name"],
            from_start_time=now_utc(),
        )


def test_submit_frozen_experiment_blocks_score_drift(isolated_root):
    client = MemoryLangfuse()

    def evaluator(*, input, output, expected_output, metadata, **kwargs):
        return [
            Evaluation(name="hard_constraint_violation", value=0),
            Evaluation(name="budget_compliant", value=1),
            Evaluation(name="unexpected", value=0.5),
        ]

    original = client.run_experiment

    def patched(**kwargs):
        kwargs["evaluators"] = [evaluator]
        return original(**kwargs)

    client.run_experiment = patched
    with pytest.raises(ValueError, match="冻结反馈指标集合不符"):
        lf.submit_frozen_experiment(
            client,
            payload=frozen_payload(),
            verify_from=now_utc(),
            pending_path=isolated_root / "langfuse-hybrid.pending.json",
            source_sha256="a" * 64,
        )


def test_submit_writes_pending_before_run_experiment(isolated_root):
    # 中断恢复保证：pending 记录必须先于 run_experiment 调用落盘。
    client = MemoryLangfuse()
    pending_path = isolated_root / "langfuse-hybrid.pending.json"
    existed = []
    original = client.run_experiment

    def instrumented(**kwargs):
        existed.append(pending_path.is_file())
        return original(**kwargs)

    client.run_experiment = instrumented
    lf.submit_frozen_experiment(
        client,
        payload=frozen_payload(),
        verify_from=now_utc(),
        pending_path=pending_path,
        source_sha256="a" * 64,
    )
    assert existed == [True]
    assert support.load_document(pending_path)["experiment_id"]


def test_submit_succeeds_after_delayed_persist(isolated_root, monkeypatch):
    # 延迟落库：实验条目与 Score 在多次读 API 之后才可读，有界时限内提交成功并清理 pending。
    monkeypatch.setattr(lf, "REMOTE_VERIFY_TIMEOUT_SECONDS", 10.0)
    monkeypatch.setattr(lf, "REMOTE_VERIFY_POLL_SECONDS", 0.02)
    client = MemoryLangfuse()
    client.delay_reveal = True
    client.reveal_after_reads = 3
    pending_path = isolated_root / "langfuse-hybrid.pending.json"
    receipt = lf.submit_frozen_experiment(
        client,
        payload=frozen_payload(),
        verify_from=now_utc(),
        pending_path=pending_path,
        source_sha256="a" * 64,
    )
    assert client.read_count >= 3
    assert support.load_document(pending_path)["experiment_id"] == receipt["experiment_id"]
    stored = client.experiment_items[receipt["experiment_id"]]
    assert {item.experiment_item_metadata["query_id"] for item in stored} == {"q-1", "q-2"}
    lf.verify_uploaded_experiment(
        client,
        experiment_id=receipt["experiment_id"],
        dataset_id=receipt["dataset_id"],
        run_name=receipt["run_name"],
        from_start_time=now_utc(),
    )


def test_submit_timeout_keeps_pending_for_recovery(isolated_root, monkeypatch):
    # 超时诚实失败：pending 保留在磁盘、终态回执不写，报错说明可重跑恢复。
    monkeypatch.setattr(lf, "REMOTE_VERIFY_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(lf, "REMOTE_VERIFY_POLL_SECONDS", 0.02)
    client = MemoryLangfuse()
    client.delay_reveal = True
    pending_path = isolated_root / "langfuse-hybrid.pending.json"
    with pytest.raises(ValueError, match="重跑上传命令可恢复"):
        lf.submit_frozen_experiment(
            client,
            payload=frozen_payload(),
            verify_from=now_utc(),
            pending_path=pending_path,
            source_sha256="a" * 64,
        )
    pending = support.load_document(pending_path)
    payload = frozen_payload()
    assert pending["schema"] == lf.PENDING_SCHEMA
    assert pending["status"] == "pending"
    assert pending["dataset_name"] == payload["dataset_name"]
    assert pending["run_name"] == payload["run_name"]
    assert pending["source_sha256"] == "a" * 64
    assert pending["query_ids"] == ["q-1", "q-2"]
    # run_experiment 已返回：实验身份并入 pending 落盘，供按 id 恢复
    assert pending["experiment_id"]
    assert not (isolated_root / "langfuse-hybrid.json").exists()


def test_submit_resume_resubmits_only_missing_samples(isolated_root, monkeypatch):
    # 部分落库恢复：远端仅含部分样例时只补缺失样例，全量核验通过后清理 pending 并返回同一实验。
    monkeypatch.setattr(lf, "REMOTE_VERIFY_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(lf, "REMOTE_VERIFY_POLL_SECONDS", 0.02)
    client = MemoryLangfuse()
    client.commit_only = {"q-1"}
    payload = frozen_payload()
    pending_path = isolated_root / "langfuse-hybrid.pending.json"
    with pytest.raises(ValueError, match="重跑上传命令可恢复"):
        lf.submit_frozen_experiment(
            client,
            payload=payload,
            verify_from=now_utc(),
            pending_path=pending_path,
            source_sha256="a" * 64,
        )
    assert pending_path.exists()
    assert len(client.task_item_ids) == 2
    interrupted_experiment_id = next(iter(client.experiments))
    client.task_item_ids.clear()
    pending = support.load_document(pending_path)
    receipt = lf.submit_frozen_experiment(
        client,
        payload=payload,
        verify_from=now_utc(),
        pending_path=pending_path,
        pending=pending,
        source_sha256="a" * 64,
    )
    missing_id = next(
        row["row_id"]
        for row in payload["results"]
        if row["run_metadata"]["query_id"] == "q-2"
    )
    assert client.task_item_ids == [missing_id]
    recorded = support.load_document(pending_path)
    assert recorded["experiment_id"] == receipt["experiment_id"]
    assert recorded["status"] == "pending"
    assert receipt["experiment_id"] == interrupted_experiment_id
    items = client.experiment_items[receipt["experiment_id"]]
    assert sorted(item.experiment_item_metadata["query_id"] for item in items) == [
        "q-1",
        "q-2",
    ]


def test_submit_resume_with_complete_experiment_skips_task(isolated_root, monkeypatch):
    # 完整落库恢复：pending 存在且实验已含全部样例时 task 零调用，直接核验并清理 pending。
    monkeypatch.setattr(lf, "REMOTE_VERIFY_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(lf, "REMOTE_VERIFY_POLL_SECONDS", 0.02)
    client = MemoryLangfuse()
    client.delay_reveal = True
    payload = frozen_payload()
    pending_path = isolated_root / "langfuse-hybrid.pending.json"
    with pytest.raises(ValueError, match="重跑上传命令可恢复"):
        lf.submit_frozen_experiment(
            client,
            payload=payload,
            verify_from=now_utc(),
            pending_path=pending_path,
            source_sha256="a" * 64,
        )
    client.reveal_pending()
    client.task_item_ids.clear()
    pending = support.load_document(pending_path)
    receipt = lf.submit_frozen_experiment(
        client,
        payload=payload,
        verify_from=now_utc(),
        pending_path=pending_path,
        pending=pending,
        source_sha256="a" * 64,
    )
    assert client.task_item_ids == []
    recorded = support.load_document(pending_path)
    assert recorded["experiment_id"] == receipt["experiment_id"]
    assert recorded["experiment_url"] is None
    assert receipt["experiment_url"] is None
    assert receipt["verify_from"] == pending["verify_from"]
    assert len(client.experiment_items[receipt["experiment_id"]]) == 2


@pytest.fixture(scope="module")
def same_pool_document(tmp_path_factory):
    version_dir = build_dataset(tmp_path_factory.mktemp("langfuse-same-pool"))
    return rerank_document(version_dir)


def test_same_pool_groups_share_dataset_with_independent_experiments(
    same_pool_document, isolated_root
):
    # 同一 staged 证据含 hybrid（rrf_baseline）与 hybrid-rerank（rerank）两组：
    # 两组共享同一数据集与 rows_sha256，第二组复用数据集；实验身份相互独立。
    document, _ = same_pool_document
    client = MemoryLangfuse()
    baseline_payload = upload_payload(document, "hybrid")
    rerank_payload = upload_payload(document, "hybrid-rerank")
    assert baseline_payload["dataset_name"] == rerank_payload["dataset_name"]
    assert (
        baseline_payload["dataset_marker"]["rows_sha256"]
        == rerank_payload["dataset_marker"]["rows_sha256"]
    )
    assert baseline_payload["dataset_marker"] == rerank_payload["dataset_marker"]
    stable_fields = {"query_id", "tags", "family", "dataset_version", "timing_scope"}
    shared_ids = {row["row_id"] for row in baseline_payload["results"]}
    for payload in (baseline_payload, rerank_payload):
        assert {row["row_id"] for row in payload["results"]} == shared_ids
        for row in payload["results"]:
            assert set(row["run_metadata"]) == stable_fields
    for row in baseline_payload["results"]:
        assert row["actual_outputs"]["experiment"] == support.EXPERIMENT_BASELINE
        assert "rerank" not in row["actual_outputs"]
    for row in rerank_payload["results"]:
        assert row["actual_outputs"]["experiment"] == support.EXPERIMENT_RERANK
        assert row["actual_outputs"]["baseline"]["candidate_pool"] == row["actual_outputs"]["candidate_pool"]
    first = lf.submit_frozen_experiment(
        client,
        payload=baseline_payload,
        verify_from=now_utc(),
        pending_path=isolated_root / "langfuse-hybrid.pending.json",
        source_sha256="a" * 64,
    )
    second = lf.submit_frozen_experiment(
        client,
        payload=rerank_payload,
        verify_from=now_utc(),
        pending_path=isolated_root / "langfuse-hybrid-rerank.pending.json",
        source_sha256="a" * 64,
    )
    assert first["dataset_id"] == second["dataset_id"]
    # submit 不删 pending：两组 pending 保留且各自绑定实验 id
    baseline_pending = support.load_document(isolated_root / "langfuse-hybrid.pending.json")
    rerank_pending = support.load_document(
        isolated_root / "langfuse-hybrid-rerank.pending.json"
    )
    assert baseline_pending["experiment_id"] == first["experiment_id"]
    assert rerank_pending["experiment_id"] == second["experiment_id"]
    receipt = support.load_document(lf.receipt_path(rerank_payload["dataset_name"]))
    assert receipt["dataset_status"] == "reused"
    stored = client.datasets[rerank_payload["dataset_name"]]
    assert len(stored["items"]) == len(baseline_payload["results"])
    # 逐项核验：第二组的期望对第一组创建的样例全部通过。
    expected = {
        row["row_id"]: {
            "id": row["row_id"],
            "inputs": row["inputs"],
            "outputs": row["expected_outputs"],
            "metadata": row["run_metadata"],
        }
        for row in rerank_payload["results"]
    }
    for item in stored["items"].values():
        lf._verify_example(expected[str(item.id)], item)
    assert first["experiment_id"] != second["experiment_id"]
    baseline_experiment = client.experiments[first["experiment_id"]]
    rerank_experiment = client.experiments[second["experiment_id"]]
    assert baseline_experiment.name == f"{document['run_id']}-hybrid"
    assert rerank_experiment.name == f"{document['run_id']}-hybrid-rerank"
    assert baseline_experiment.metadata["experiment"] == support.EXPERIMENT_BASELINE
    assert rerank_experiment.metadata["experiment"] == support.EXPERIMENT_RERANK
    assert baseline_experiment.metadata["group"] == "hybrid"
    assert rerank_experiment.metadata["group"] == "hybrid-rerank"
    lf.verify_uploaded_experiment(
        client,
        experiment_id=first["experiment_id"],
        dataset_id=first["dataset_id"],
        run_name=baseline_payload["run_name"],
        from_start_time=now_utc(),
    )
    lf.verify_uploaded_experiment(
        client,
        experiment_id=second["experiment_id"],
        dataset_id=second["dataset_id"],
        run_name=rerank_payload["run_name"],
        from_start_time=now_utc(),
    )


def test_upload_interrupt_resumes_and_blocks_changed_source(
    same_pool_document, isolated_root, monkeypatch, capsys
):
    # upload 入口三态接线：落库超时保留 pending；源证据变更按冲突阻断；
    # 恢复后 hybrid 走恢复路径零补投、hybrid-rerank 走新提交，两组终态回执落盘。
    document, _ = same_pool_document
    source = isolated_root / "compare.json"
    support.save_document(source, document)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setattr(lf, "REMOTE_VERIFY_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(lf, "REMOTE_VERIFY_POLL_SECONDS", 0.02)
    client = MemoryLangfuse()
    client.delay_reveal = True
    monkeypatch.setattr(lf, "make_client", lambda: client)
    args = SimpleNamespace(source=source)
    with pytest.raises(ValueError, match="重跑上传命令可恢复"):
        retrieval.upload(args)
    pending_path = source.parent / "langfuse-hybrid.pending.json"
    assert pending_path.exists()
    assert not (source.parent / "langfuse-hybrid.json").exists()
    pending = support.load_document(pending_path)
    pending["source_sha256"] = "0" * 64
    support.save_document(pending_path, pending)
    with pytest.raises(ValueError, match="旧上传身份作废"):
        retrieval.upload(args)
    pending["source_sha256"] = support.sha256(source)
    support.save_document(pending_path, pending)
    client.reveal_pending()
    client.delay_reveal = False
    client.task_item_ids.clear()
    retrieval.upload(args)
    rerank_rows = upload_payload(document, "hybrid-rerank")["results"]
    assert set(client.task_item_ids) == {row["row_id"] for row in rerank_rows}
    assert len(client.task_item_ids) == len(rerank_rows)
    hybrid_items = client.experiment_items[
        support.load_document(source.parent / "langfuse-hybrid.json")["experiment_id"]
    ]
    assert len(hybrid_items) == len(document["query_ids"])
    assert not pending_path.exists()
    assert (source.parent / "langfuse-hybrid-rerank.json").is_file()
    assert not (source.parent / "langfuse-hybrid-rerank.pending.json").exists()
    resumed = [
        line
        for line in capsys.readouterr().out.splitlines()
        if '"resumed": true' in line
    ]
    assert resumed and '"group": "hybrid"' in resumed[0]
    assert (source.parent / "report.md").read_text(encoding="utf-8").count("\n- Langfuse：") == 2


def test_upload_recovers_after_submit_success_without_receipt(
    same_pool_document, isolated_root, monkeypatch, capsys
):
    # 崩溃窗口恢复：submit 成功返回但终态回执未写 → 重跑 upload 凭 pending 的
    # experiment_id 直接核验：零 task 调用、回执写入、pending 清除。
    document, _ = same_pool_document
    source = isolated_root / "compare.json"
    support.save_document(source, document)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    client = MemoryLangfuse()
    monkeypatch.setattr(lf, "make_client", lambda: client)
    pending_path = source.parent / "langfuse-hybrid.pending.json"
    interrupted = lf.submit_frozen_experiment(
        client,
        payload=upload_payload(document, "hybrid"),
        verify_from=now_utc(),
        pending_path=pending_path,
        source_sha256=support.sha256(source),
    )
    pending = support.load_document(pending_path)
    assert pending["experiment_id"] == interrupted["experiment_id"]
    client.task_item_ids.clear()
    retrieval.upload(SimpleNamespace(source=source))
    assert set(client.task_item_ids) == {
        row["row_id"] for row in upload_payload(document, "hybrid-rerank")["results"]
    }
    assert len(client.task_item_ids) == len(upload_payload(document, "hybrid-rerank")["results"])
    receipt = support.load_document(source.parent / "langfuse-hybrid.json")
    assert receipt["experiment_id"] == interrupted["experiment_id"]
    assert receipt["source_sha256"] == support.sha256(source)
    assert not pending_path.exists()
    assert '"resumed": true' in capsys.readouterr().out


def test_resume_with_pending_experiment_id_waits_for_visibility(
    isolated_root, monkeypatch
):
    # 含 experiment_id 的 pending + 远端实验记录暂不可读：有界等待后诚实失败，
    # pending 保留，期间禁止任何 run_experiment 重放。
    monkeypatch.setattr(lf, "REMOTE_VERIFY_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(lf, "REMOTE_VERIFY_POLL_SECONDS", 0.02)
    client = MemoryLangfuse()
    client.delay_reveal = True
    pending_path = isolated_root / "langfuse-hybrid.pending.json"
    with pytest.raises(ValueError, match="重跑上传命令可恢复"):
        lf.submit_frozen_experiment(
            client,
            payload=frozen_payload(),
            verify_from=now_utc(),
            pending_path=pending_path,
            source_sha256="a" * 64,
        )
    assert len(client.task_item_ids) == 2
    pending = support.load_document(pending_path)
    assert pending["experiment_id"]
    with pytest.raises(ValueError, match="远端暂不可读"):
        lf.submit_frozen_experiment(
            client,
            payload=frozen_payload(),
            verify_from=now_utc(),
            pending_path=pending_path,
            pending=pending,
            source_sha256="a" * 64,
        )
    assert len(client.task_item_ids) == 2
    assert pending_path.exists()


def test_upload_persists_receipt_before_removing_pending(
    same_pool_document, isolated_root, monkeypatch, capsys
):
    # 删除时序：终态回执落盘时刻 pending 仍在，回执持久化之后才删除；
    # 回执已存在时清理同身份残留 pending，身份不符的残留保留。
    document, _ = same_pool_document
    source = isolated_root / "compare.json"
    support.save_document(source, document)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    client = MemoryLangfuse()
    monkeypatch.setattr(lf, "make_client", lambda: client)
    pending_path = source.parent / "langfuse-hybrid.pending.json"
    events = []
    original_save = support.save_document

    def spy_save(path, saved):
        if path.name == "langfuse-hybrid.json":
            events.append(pending_path.is_file())
        return original_save(path, saved)

    monkeypatch.setattr(support, "save_document", spy_save)
    retrieval.upload(SimpleNamespace(source=source))
    assert events == [True]
    assert not pending_path.exists()
    receipt = support.load_document(source.parent / "langfuse-hybrid.json")
    support.save_document(
        pending_path,
        {
            "schema": lf.PENDING_SCHEMA,
            "dataset_name": receipt["dataset_name"],
            "run_name": receipt["run_name"],
            "verify_from": receipt["verify_from"],
            "source_sha256": support.sha256(source),
            "query_ids": list(document["query_ids"]),
            "status": "pending",
            "experiment_id": receipt["experiment_id"],
        },
    )
    retrieval.upload(SimpleNamespace(source=source))
    assert not pending_path.exists()
    support.save_document(
        pending_path,
        {
            "schema": lf.PENDING_SCHEMA,
            "dataset_name": receipt["dataset_name"],
            "run_name": "other-run",
            "verify_from": receipt["verify_from"],
            "source_sha256": support.sha256(source),
            "query_ids": list(document["query_ids"]),
            "status": "pending",
        },
    )
    retrieval.upload(SimpleNamespace(source=source))
    assert pending_path.exists()
    assert capsys.readouterr().out.count('"status": "verified_existing"') == 4
