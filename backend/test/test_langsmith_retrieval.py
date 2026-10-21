import asyncio
import copy
import json
from uuid import uuid4

import pytest

from evaluation import langsmith_retrieval as lr
from evaluation import retrieval_support as support
from evaluation.contracts import RelevanceLabel, RetrievalInput
from evaluation.dataset import load_for_evaluation
from evaluation.retrieval_metrics import evaluate

DATASET_PATH = support.BACKEND / "dataset/evaluation/rebuild-v1"


@pytest.fixture(scope="module")
def dataset():
    return load_for_evaluation(DATASET_PATH)


@pytest.fixture(scope="module")
def rows(dataset):
    return lr.example_rows(dataset)


@pytest.fixture(scope="module")
def reviewed(dataset):
    return dataset.reviewed_corpus_ids()


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
        self.hide_names = False

    def list_datasets(self, *, dataset_name=None, dataset_name_contains=None, **kwargs):
        if self.hide_names:
            return []
        if dataset_name is None:
            return list(self.datasets.values())
        dataset = self.datasets.get(dataset_name)
        return [dataset] if dataset is not None else []

    def read_dataset(self, *, dataset_name=None, dataset_id=None):
        if dataset_name is not None:
            dataset = self.datasets.get(dataset_name)
            if dataset is None:
                raise lr.LangSmithNotFoundError("dataset not found")
            return dataset
        for dataset in self.datasets.values():
            if str(dataset.id) == str(dataset_id):
                return dataset
        raise lr.LangSmithNotFoundError("dataset not found")

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
def isolated_root(tmp_path, monkeypatch):
    monkeypatch.setattr(lr, "DATASET_ROOT", tmp_path / "datasets")
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
    rows = lr.example_rows(dataset)
    row = next(item for item in rows if item["inputs"]["query_id"] == query_id)
    outputs = target_outputs(
        query_id=query_id,
        identity=lr.dataset_binding(dataset),
        pool=pool,
        final=final,
    )
    return row["inputs"], outputs, row["outputs"], query


def feedback_map(result: dict) -> dict[str, float]:
    return {item["key"]: item["score"] for item in result["results"]}


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
    name = lr.dataset_name(dataset)
    assert name == f"fit-agent-retrieval-sdk-{dataset.version}-{dataset.content_sha256[:12]}"
    assert rows[0]["id"] == lr.stable_example_id(name, rows[0]["inputs"]["query_id"])
    first = lr.example_rows(dataset)
    second = lr.example_rows(dataset)
    assert first == second


def test_validation_selection_is_fixed_six(dataset):
    selected = lr.select_validation_queries(dataset)
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
        for purpose in lr.PURPOSES
        for expect_empty in (False, True)
    }


def test_sdk_identity_includes_new_module():
    identity = lr.sdk_code_identity()
    assert identity["files"]["evaluation/langsmith_retrieval.py"] == support.sha256(
        support.BACKEND / "evaluation/langsmith_retrieval.py"
    )
    assert "evaluation/retrieval.py" in identity["files"]


def test_validated_parameters_match_frozen_baseline():
    values = lr.validated_parameters()
    assert values["fusion"] == "rrf"
    assert values["rrf_k"] == 60
    assert values["bm25_depth"] == 100
    assert values["dense_depth"] == 100
    assert values["threshold"] == 0.0
    assert values["candidate_pool_limit"] == 20
    assert values["final_input_limit"] == 5


def test_target_query_rejects_wrong_digest_and_cache_text(dataset):
    row = lr.example_rows(dataset)[0]
    identity = lr.dataset_binding(dataset)
    query = lr.target_query(
        row["inputs"], dataset_identity=identity, expected_vector_text=row["inputs"]["query"]
    )
    assert query["query_id"] == row["inputs"]["query_id"]
    with pytest.raises(ValueError, match="数据身份不符"):
        lr.target_query(
            {**row["inputs"], "dataset": {**identity, "content_sha256": "0" * 64}},
            dataset_identity=identity,
            expected_vector_text=None,
        )
    with pytest.raises(ValueError, match="缓存查询文本"):
        lr.target_query(
            row["inputs"],
            dataset_identity=identity,
            expected_vector_text=row["inputs"]["query"] + "x",
        )


async def test_evaluator_matches_existing_metrics(dataset, reviewed):
    query_id = lr.select_validation_queries(dataset)[0]["query_id"]
    rows = lr.example_rows(dataset)
    reference = next(row["outputs"] for row in rows if row["inputs"]["query_id"] == query_id)
    positives = [item["exercise_id"] for item in reference["labels"] if item["grade"] >= 1]
    extras = sorted(reviewed[query_id] - set(positives))[:2]
    pool = positives[:2] + extras
    final = positives[:2]
    inputs, outputs, reference, _ = evaluator_payload(dataset, query_id, pool, final)
    result = await lr.make_evaluator(
        "hybrid", lr.dataset_binding(dataset), reviewed
    )(inputs, outputs, reference)
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
    assert list(feedback_map(result)) == list(lr.RETRIEVAL_METRICS)


async def test_evaluator_empty_samples_only_report_false_return(dataset, reviewed):
    query_id = lr.select_validation_queries(dataset)[1]["query_id"]
    inputs, outputs, reference, _ = evaluator_payload(dataset, query_id, [], [])
    result = await lr.make_evaluator(
        "hybrid", lr.dataset_binding(dataset), reviewed
    )(inputs, outputs, reference)
    assert feedback_map(result) == {"false_return": 0.0}
    assert lr.expected_feedback_keys(reference) == {"false_return"}


async def test_evaluator_rejects_identity_mismatch(dataset, reviewed):
    query_id = lr.select_validation_queries(dataset)[0]["query_id"]
    inputs, outputs, reference, _ = evaluator_payload(
        dataset, query_id, ["0025", "0030"], ["0025"]
    )
    evaluator = lr.make_evaluator("hybrid", lr.dataset_binding(dataset), reviewed)
    with pytest.raises(ValueError, match="query_id"):
        await evaluator(inputs, {**outputs, "query_id": "q-other"}, reference)
    with pytest.raises(ValueError, match="金标 query_id"):
        await evaluator(inputs, outputs, {**reference, "query_id": "q-other"})
    with pytest.raises(ValueError, match="输出数据摘要"):
        await evaluator(inputs, {**outputs, "dataset_content_sha256": "0" * 64}, reference)
    with pytest.raises(ValueError, match="输出检索组"):
        await evaluator(inputs, {**outputs, "strategy": "bm25"}, reference)


def test_query_cache_identity_mismatch_is_rejected(tmp_path, monkeypatch):
    identity = {
        "model": "test-model",
        "dimensions": 2,
        "encoding_rule_version": "test",
    }
    monkeypatch.setattr(support, "CACHE_ROOT", tmp_path)
    path = support.cache_path(identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"identity": {"other": True}, "entries": []}), encoding="utf-8"
    )
    with pytest.raises(support.EvaluationBlocked, match="身份不一致"):
        support.read_query_cache(identity, [])


def test_sync_is_idempotent_and_completes_missing_examples(dataset, isolated_root):
    client = MemoryClient()
    first = lr.sync_dataset(client, dataset)
    assert first["dataset_status"] == "created"
    assert len(client.examples[first["dataset_id"]]) == 150
    second = lr.sync_dataset(client, dataset)
    assert second["dataset_status"] == "reused"
    assert second["dataset_id"] == first["dataset_id"]
    assert len(client.examples[first["dataset_id"]]) == 150
    assert second["example_ids"] == first["example_ids"]

    removed = next(iter(client.examples[first["dataset_id"]]))
    client.examples[first["dataset_id"]].pop(removed)
    third = lr.sync_dataset(client, dataset)
    assert third["dataset_status"] == "reused"
    assert removed in client.examples[first["dataset_id"]]
    assert len(client.examples[first["dataset_id"]]) == 150


def test_sync_blocks_unknown_name_and_conflicting_example(dataset, isolated_root):
    client = MemoryClient()
    receipt = lr.sync_dataset(client, dataset)
    remote = client.datasets[receipt["dataset_name"]]
    remote.metadata = {"marker": "unknown"}
    with pytest.raises(ValueError, match="缺少字段"):
        lr.sync_dataset(client, dataset)
    remote.metadata = {**lr.dataset_marker(dataset), "unknown": True}
    with pytest.raises(ValueError, match="未知字段"):
        lr.sync_dataset(client, dataset)

    client = MemoryClient()
    receipt = lr.sync_dataset(client, dataset)
    example_id = receipt["example_ids"]["q-lookup-001"]
    client.examples[receipt["dataset_id"]][example_id].inputs["query"] = "被修改"
    with pytest.raises(ValueError, match="inputs 不符"):
        lr.sync_dataset(client, dataset)


def test_sync_reuses_receipt_when_name_lookup_is_stale(dataset, isolated_root):
    client = MemoryClient()
    first = lr.sync_dataset(client, dataset)
    client.hide_names = True
    second = lr.sync_dataset(client, dataset)
    assert second["dataset_status"] == "reused"
    assert second["dataset_id"] == first["dataset_id"]
    assert len(client.datasets) == 1


class MemoryRun:
    def __init__(self, run_id, example_id, outputs, error=None):
        self.id = run_id
        self.reference_example_id = example_id
        self.outputs = outputs
        self.error = error
        self.start_time = None
        self.end_time = None


class MemoryFeedback:
    def __init__(self, key, score):
        self.key = key
        self.score = score


class MemoryProject:
    def __init__(self, metadata: dict) -> None:
        self.extra = {"metadata": metadata}


class MemoryRunClient:
    def __init__(self) -> None:
        self.runs = []
        self.feedback = []
        self.project = None

    def read_project(self, *, project_id=None):
        return self.project

    def list_runs(self, *, project_id=None, is_root=None):
        return list(self.runs)

    def list_feedback(self, *, run_ids=None):
        selected = set(run_ids)
        return [item for run_id, item in self.feedback if run_id in selected]


def test_verify_experiment_metadata_requires_all_recorded_keys():
    expected = {
        "local_run_id": "run-1",
        "group": "bm25",
        "purpose": "validation",
        "dataset": {"version": "v"},
        "parameters": {"rrf_k": 60},
        "code_identity": {"head": "abc"},
        "index_collection": "collection",
        "index_manifest_sha256": "a" * 64,
        "vector_cache_identity_sha256": "b" * 64,
        "max_concurrency": 6,
        "num_repetitions": 1,
        "sample_scope": {"count": 6},
    }
    client = MemoryRunClient()
    client.project = MemoryProject({**expected, "__ls_runner": "py_sdk_evaluate"})
    lr.verify_experiment_metadata(client, "experiment", expected)
    client.project.extra["metadata"]["max_concurrency"] = 3
    with pytest.raises(ValueError, match="远端实验 metadata 不符: max_concurrency"):
        lr.verify_experiment_metadata(client, "experiment", expected)


def test_verify_group_requires_every_run_and_feedback(dataset, reviewed):
    row = lr.example_rows(dataset)[0]
    query_id = row["inputs"]["query_id"]
    identity = lr.dataset_binding(dataset)
    positives = [
        item["exercise_id"]
        for item in row["outputs"]["labels"]
        if item["grade"] >= 1
    ]
    outputs = target_outputs(
        query_id=query_id, identity=identity, pool=positives[:2], final=positives[:1]
    )
    expected = lr.expected_feedback(
        "hybrid", row["inputs"], outputs, row["outputs"], identity, reviewed
    )
    client = MemoryRunClient()
    run = MemoryRun(uuid4(), row["id"], outputs)
    client.runs = [run]
    client.feedback = [(str(run.id), MemoryFeedback(name, value)) for name, value in expected.items()]
    samples, coverage = lr.verify_group(
        client,
        group="hybrid",
        dataset_identity=identity,
        reviewed=reviewed,
        rows={row["id"]: row},
        selected_example_ids=[row["id"]],
        experiment_id="experiment",
    )
    assert coverage["status"] == "complete"
    assert samples[0]["status"] == "complete"
    assert samples[0]["feedback"] == pytest.approx(expected)

    client.feedback = []
    samples, coverage = lr.verify_group(
        client,
        group="hybrid",
        dataset_identity=identity,
        reviewed=reviewed,
        rows={row["id"]: row},
        selected_example_ids=[row["id"]],
        experiment_id="experiment",
    )
    assert coverage["status"] == "incomplete"
    assert samples[0]["status"] == "failed"
    assert "反馈指标集合不符" in samples[0]["error"]

    client.feedback = [(str(run.id), MemoryFeedback(name, value)) for name, value in expected.items()]
    run.error = "target failed"
    samples, coverage = lr.verify_group(
        client,
        group="hybrid",
        dataset_identity=identity,
        reviewed=reviewed,
        rows={row["id"]: row},
        selected_example_ids=[row["id"]],
        experiment_id="experiment",
    )
    assert samples[0]["status"] == "failed"
    assert samples[0]["error"] == "target failed"


def test_group_evidence_writes_one_file_per_sample(tmp_path):
    samples = [
        {"query_id": "q-lookup-001", "status": "complete"},
        {"query_id": "q-lookup-051", "status": "complete"},
    ]
    lr.write_group_evidence(tmp_path, "bm25", samples, {"status": "complete"})
    assert sorted(path.name for path in (tmp_path / "bm25" / "samples").glob("*.json")) == [
        "q-lookup-001.json",
        "q-lookup-051.json",
    ]
    assert (tmp_path / "bm25" / "result.json").is_file()


async def test_concurrency_recorder_observes_overlap():
    recorder = lr.ConcurrencyRecorder()
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
    assert report["max_in_flight"] <= lr.MAX_CONCURRENCY
