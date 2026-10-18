import argparse
import asyncio
import json
import math
import os
import subprocess
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from langsmith import Client

from app.application.business import retrieval_text as rt
from app.application.business.catalog import Catalog
from app.application.business.retrieval_embedding import EmbeddingClient
from app.application.business.retrieval_frozen import frozen_values
from app.application.business.retrieval_index import (
    MilvusConnection,
    RetrievalCorpus,
    sha256,
)
from app.application.business.retrieval_search import (
    search_vectors,
    validate_parameters,
)
from evaluation import retrieval_support as support
from evaluation.dataset import (
    DatasetError,
    EvaluationDataset,
    load_for_evaluation,
    retrieval_corpus_identity,
)
from evaluation.retrieval_metrics import DEFAULT_KS, require, score
from evaluation.retrieval_support import load_queries

SCHEMA = "fit-agent-retrieval-mteb-1"
METRICS = ("hit", "recall", "precision", "mrr", "ndcg")
DISPLAY = {"hit": "Hit", "recall": "Recall", "precision": "Precision", "mrr": "MRR", "ndcg": "nDCG"}


def now() -> str:
    return datetime.now(UTC).isoformat()


def identity() -> dict:
    paths = [
        *sorted((support.BACKEND / "app/application/business").glob("retrieval_*.py")),
        Path(__file__), Path(support.__file__),
        support.BACKEND / "evaluation/retrieval_metrics.py",
        support.BACKEND / "evaluation/dataset.py",
        *sorted((support.BACKEND / "dataset/evaluation/schemas").glob("*.schema.json")),
        support.BACKEND / "uv.lock", support.QUERIES_PATH, support.ADAPTATION_PATH,
        support.DATASET / "actions.agent.json", support.DATASET / "action-supplements.published.json",
        support.DATASET / "action-field-mappings.json",
    ]
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=support.REPO, check=True, capture_output=True, text=True).stdout.strip()
    return {"head": head, "files": {path.relative_to(support.BACKEND).as_posix(): sha256(path) for path in paths},
            "versions": {name: version(name) for name in ("mteb", "langsmith", "pymilvus", "openai")}}


def validate_projections(queries: list[dict], projections: dict, catalog: Catalog) -> dict:
    require(set(projections) == {item["id"] for item in queries}, "候选证据与查询 ID 不一致")
    for qid, projection in projections.items():
        ids = projection["returned"]
        require(isinstance(ids, list) and len(ids) <= 10, f"返回候选非法: {qid}")
        require(all(eid in catalog.exercise_ids for eid in ids), f"返回目录外动作: {qid}")
        require(projection["budget_compliant"] is True, f"字符预算违规: {qid}")
        require(not projection["hard_constraint_violations"], f"硬约束违规: {qid}")
    return score(queries, {qid: row["returned"] for qid, row in projections.items()})


def _metric_keys() -> list[str]:
    return [f"{name}@{k}" for k in DEFAULT_KS for name in METRICS]


def _report_lines(document: dict) -> list[str]:
    dataset = document.get("dataset")
    lines = [
        f"\n## {document['run_id']}\n",
        f"- 证据 Schema：`{document['schema']}`；类型：`{document['mode']}`；拆分：`{document['split']}`。",
        f"- 评分引擎：MTEB `{version('mteb')}`；相关性 grade>=1；nDCG 增益 `2^grade-1`；Precision@k 固定分母 k。",
        f"- 运行身份：`{document['identity']['head']}`。",
        f"- 时间范围：`{document['started_at']}` 至 `{document.get('finished_at', '未完成')}`。",
    ]
    if dataset is not None:
        lines.extend([
            f"- 数据版本：`{dataset['version']}`。",
            f"- manifest SHA-256：`{dataset['manifest_sha256']}`。",
            f"- 内容摘要：`{dataset['content_sha256']}`。",
            f"- 语料摘要：`{dataset['corpus_sha256']}`。",
        ])
    else:
        lines.extend([
            f"- 数据集 SHA-256：`{document.get('dataset_sha256', '')}`。",
            f"- 用途适配 SHA-256：`{document.get('adaptation_sha256', '')}`。",
        ])
    if document.get("status") != "complete":
        lines.append("- 发布门禁：未通过。")
    keys = _metric_keys()
    header = "| group | 有标注 / 无答案 | " + " | ".join(DISPLAY[name] + key[key.index("@"):] for key, name in ((key, key.split("@")[0]) for key in keys)) + " | 无答案误返回率 |"
    lines.extend([header, "|" + "---|" * (len(keys) + 3)])
    for group, result in document["groups"].items():
        metrics = result.get("metrics")
        if metrics is None:
            values = ["未测"] * len(keys)
            counts = "未测"
        else:
            stats = metrics["overall"]
            values = ["未测" if stats[key] is None else f"{stats[key]:.6f}" for key in keys]
            counts = f"{stats['with_labels']} / {stats['empty_expectation_queries']}"
        false_return = "未测" if metrics is None or metrics["overall"]["false_return_rate"] is None else f"{metrics['overall']['false_return_rate']:.6f}"
        lines.append(f"| {group} | {counts} | " + " | ".join([*values, false_return]) + " |")
    unknown = {
        group: result["unknown_candidates"]
        for group, result in document["groups"].items()
        if result.get("unknown_candidates")
    }
    for group, rows in unknown.items():
        lines.append(f"- `{group}` 未审阅候选（阻止发布）：")
        for qid, ids in rows.items():
            lines.append(f"  - `{qid}`：`{', '.join(ids)}`。")
    return lines


def record_report(document: dict, artifact: Path) -> Path:
    path = artifact.parent / "report.md"
    path.write_text("\n".join(_report_lines(document)) + "\n", encoding="utf-8")
    return path


def save_result(document: dict) -> Path:
    artifact = support.OUTPUT_ROOT / document["run_id"] / "evaluation.json"
    support.save_document(artifact, document)
    report = record_report(document, artifact)
    groups = {}
    for name, row in document["groups"].items():
        metrics = row.get("metrics")
        summary = {"status": row.get("status"), "overall": metrics.get("overall") if isinstance(metrics, dict) else None}
        if row.get("unknown_candidates"):
            summary["unknown_candidates"] = row["unknown_candidates"]
        groups[name] = summary
    print(json.dumps({"artifact": str(artifact), "report": str(report), "status": document.get("status"), "groups": groups},
                     ensure_ascii=False))
    return artifact


def base_document(mode: str, split: str) -> dict:
    return {"schema": SCHEMA, "run_id": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8],
            "mode": mode, "split": split, "status": "running", "started_at": now(), "identity": identity(), "groups": {}}


def validate_group(dataset: EvaluationDataset, group: str, projections: dict, timings: dict, catalog: Catalog) -> dict:
    unknown = dataset.unreviewed_candidates({qid: row["returned"] for qid, row in projections.items()})
    if unknown:
        return {"status": "blocked_unreviewed_candidates", "unknown_candidates": unknown,
                "metrics": None, "projections": projections, "timings": timings}
    return {"status": "complete", "metrics": validate_projections(dataset.scoring_queries(), projections, catalog),
            "projections": projections, "timings": timings}


def publishable(document: dict) -> bool:
    groups = document.get("groups")
    if not isinstance(groups, dict) or not groups:
        return False
    keys = _metric_keys()
    for group in groups.values():
        if group.get("status") != "complete":
            return False
        metrics = group.get("metrics")
        if not isinstance(metrics, dict):
            return False
        overall = metrics.get("overall")
        per_query = metrics.get("per_query")
        if not isinstance(overall, dict) or not isinstance(per_query, dict) or not per_query:
            return False
        if any(key not in overall for key in keys):
            return False
    return True


def settle_document(document: dict) -> str:
    document["finished_at"] = now()
    document["status"] = "complete" if publishable(document) else "blocked"
    return document["status"]


def dataset_directory(document: dict) -> Path:
    value = document["dataset"]["version_dir"]
    path = Path(value)
    return path if path.is_absolute() else support.REPO / path


def bind_dataset(document: dict) -> EvaluationDataset:
    dataset = load_for_evaluation(dataset_directory(document))
    bound = document["dataset"]
    require(dataset.version == bound["version"], "评测数据版本已变化")
    require(dataset.manifest_sha256 == bound["manifest_sha256"], "评测数据 manifest 已变化")
    require(dataset.content_sha256 == bound["content_sha256"], "评测数据内容摘要已变化")
    require(dataset.corpus_sha256 == bound["corpus_sha256"], "评测语料快照已变化")
    return dataset


async def collect_dataset(args) -> None:
    dataset = load_for_evaluation(args.dataset)
    corpus = RetrievalCorpus.load()
    require(dataset.corpus_sha256 == retrieval_corpus_identity(corpus), "评测语料快照与真实目录不一致")
    values = frozen_values()
    params = {"alpha": values["alpha"] if args.alpha is None else args.alpha,
              "bm25_depth": values["bm25_depth"] if args.depth is None else args.depth,
              "dense_depth": values["dense_depth"] if args.depth is None else args.depth,
              "threshold": values["threshold"]}
    require(args.frozen is None, "新数据入口不接受冻结来源参数")
    validate_parameters("评测", params["alpha"], support.RECALL_LIMIT, params["bm25_depth"], params["dense_depth"])
    require(params["threshold"] is None or math.isfinite(params["threshold"]), "门槛非法")
    embedding = EmbeddingClient.from_env()
    dataset_identity = dataset.identity()
    vector_identity = support.query_vector_identity(embedding.config, corpus, dataset_identity)
    vectors = support.read_query_cache(vector_identity, dataset.query_cache_queries())[0] if any(group != "bm25" for group in args.groups) else {}
    document = base_document("live", dataset.version)
    document.update(dataset=dataset_identity, parameters=params, query_ids=[item["query_id"] for item in dataset.queries],
                    vector_cache_identity=vector_identity, embedding_requests=0,
                    timing_scope="cached_vector_search_and_response",
                    observation_scope="new_evaluation_dataset_full_run")
    mappings = rt.load_mappings()
    async with MilvusConnection.from_env() as db:
        collection, index_identity = await support.open_published(db, corpus)
        document["index_identity"] = index_identity
        for group in args.groups:
            projections, timings = {}, {}
            for item in dataset.queries:
                qid = item["query_id"]
                evaluated = support.dataset_conditions(item, corpus.catalog, mappings)
                started = now()
                recalled = await search_vectors(db, collection, corpus.catalog, item["query"], vectors[qid] if group != "bm25" else [],
                                                conditions=evaluated.conditions, alpha=params["alpha"], bm25_depth=params["bm25_depth"],
                                                dense_depth=params["dense_depth"], limit=support.RECALL_LIMIT, mode=group)
                projections[qid] = support.project_response(corpus.catalog, recalled, evaluated, params["threshold"] if group == "hybrid" else None)
                timings[qid] = {"start_time": started, "end_time": now()}
            document["groups"][group] = validate_group(dataset, group, projections, timings, corpus.catalog)
    require(document["identity"] == identity(), "运行期间代码或数据改变，拒绝记录有效结果")
    settle_document(document)
    save_result(document)
    if document["status"] != "complete":
        raise support.EvaluationBlocked("存在未审阅候选，已保留实际证据并阻止发布")


async def collect_legacy(args) -> None:
    corpus = RetrievalCorpus.load()
    support.require_inputs(corpus.catalog)
    adaptation, purposes = support.load_adaptation()
    deferred = adaptation["deferred_query_ids"]
    selected = [item for item in load_queries(args.legacy_split) if item["id"] not in deferred]
    values = frozen_values()
    params = {"alpha": values["alpha"] if args.alpha is None else args.alpha,
              "bm25_depth": values["bm25_depth"] if args.depth is None else args.depth,
              "dense_depth": values["dense_depth"] if args.depth is None else args.depth,
              "threshold": values["threshold"]}
    if args.legacy_split == "holdout":
        require(args.frozen is not None and args.alpha is None and args.depth is None, "留出运行需要 --frozen 开发集证据，且不得覆盖参数")
        frozen = support.load_document(args.frozen)
        require(frozen["mode"] == "live" and frozen["split"] == "dev", "冻结来源必须为真实开发集运行")
        require(frozen["dataset_sha256"] == sha256(support.QUERIES_PATH) and frozen["identity"] == identity(), "冻结来源与当前数据或代码不一致")
        params = frozen["parameters"]
    else:
        require(args.frozen is None, "开发集通过 --alpha / --depth 指定参数")
    validate_parameters("评测", params["alpha"], support.RECALL_LIMIT, params["bm25_depth"], params["dense_depth"])
    require(params["threshold"] is None or math.isfinite(params["threshold"]), "门槛非法")
    vector_identity = support.query_vector_identity(EmbeddingClient.from_env().config, corpus)
    vectors = support.read_query_cache(vector_identity, selected)[0] if any(group != "bm25" for group in args.groups) else {}
    document = base_document("live", args.legacy_split)
    document.update(parameters=params, deferred_query_ids=deferred, query_ids=[item["id"] for item in selected],
                    vector_cache_identity=vector_identity, embedding_requests=0, timing_scope="cached_vector_search_and_response",
                    observation_scope="previously_observed_holdout_regression" if args.legacy_split == "holdout" else "development",
                    dataset_sha256=sha256(support.QUERIES_PATH), adaptation_sha256=sha256(support.ADAPTATION_PATH))
    mappings = rt.load_mappings()
    adapted = {row["id"]: row for row in adaptation["rows"]}
    async with MilvusConnection.from_env() as db:
        collection, index_identity = await support.open_published(db, corpus)
        document["index_identity"] = index_identity
        if args.legacy_split == "holdout":
            require(index_identity == frozen["index_identity"], "发布索引与开发集冻结来源不一致")
            document["frozen_source_sha256"] = sha256(args.frozen)
        for group in args.groups:
            projections, timings = {}, {}
            for item in selected:
                qid = item["id"]
                evaluated = support.effective_conditions(item, corpus.catalog, mappings, purposes[qid], adapted[qid]["temporary_mapping"])
                started = now()
                recalled = await search_vectors(db, collection, corpus.catalog, item["query"], vectors[qid] if group != "bm25" else [],
                                                conditions=evaluated.conditions, alpha=params["alpha"], bm25_depth=params["bm25_depth"],
                                                dense_depth=params["dense_depth"], limit=support.RECALL_LIMIT, mode=group)
                projections[qid] = support.project_response(corpus.catalog, recalled, evaluated, params["threshold"] if group == "hybrid" else None)
                timings[qid] = {"start_time": started, "end_time": now()}
            document["groups"][group] = {"status": "complete", "metrics": validate_projections(selected, projections, corpus.catalog),
                                         "projections": projections, "timings": timings}
    require(document["identity"] == identity(), "运行期间代码或数据改变，拒绝记录有效结果")
    settle_document(document)
    save_result(document)


def validate_history(history: dict) -> None:
    require(history["conditions_mode"] == "adapted", "历史证据必须使用已批准用途适配")
    files = history["run_identity"]["file_sha256"]
    require(files["dataset/action-retrieval-queries.json"] == sha256(support.QUERIES_PATH), "历史证据金标哈希不符")
    require(files["dataset/action-retrieval-purpose-adaptation.json"] == sha256(support.ADAPTATION_PATH), "历史证据用途适配哈希不符")


def rescore(args) -> None:
    catalog = Catalog.load()
    support.require_inputs(catalog)
    support.load_adaptation()
    history = support.load_document(args.source)
    validate_history(history)
    selected = [item for item in load_queries(history["split"]) if item["id"] not in history["deferred_query_ids"]]
    document = base_document("historical_rescore", history["split"])
    document.update(source=str(args.source.resolve()), source_sha256=sha256(args.source),
                    dataset_sha256=sha256(support.QUERIES_PATH), adaptation_sha256=sha256(support.ADAPTATION_PATH),
                    source_identity=history["run_identity"], index_identity=history["index_identity"],
                    deferred_query_ids=history["deferred_query_ids"], query_ids=[item["id"] for item in selected],
                    timing_scope="offline_batch_rescoring", observation_scope="historical_retrieval_results_only")
    for group, result in history["groups"].items():
        projections = result["per_query_projection"]
        started = now()
        metrics = validate_projections(selected, projections, catalog)
        finished = now()
        document["groups"][group] = {"status": "complete", "metrics": metrics, "projections": projections,
                                     "timings": {item["id"]: {"start_time": started, "end_time": finished} for item in selected}}
    document["finished_at"] = now()
    document["status"] = "complete"
    save_result(document)


def _upload_dataset_payload(document: dict, group: str) -> dict:
    require(document.get("status") == "complete", "证据未通过发布门禁")
    dataset = bind_dataset(document)
    queries = {item["query_id"]: item for item in dataset.queries}
    qrels = dataset.qrels_by_query()
    result = document["groups"][group]
    require(result.get("status") == "complete", "检索组未通过发布门禁")
    require(set(queries) == set(result["metrics"]["per_query"]) == set(result["projections"]) == set(result["timings"]), "上传样例覆盖不完整")
    require(document["query_ids"] == [item["query_id"] for item in dataset.queries], "上传查询列表与数据版本不一致")
    unknown = dataset.unreviewed_candidates({qid: row["returned"] for qid, row in result["projections"].items()})
    require(not unknown, f"存在未审阅候选: {unknown}")
    recomputed = validate_projections(dataset.scoring_queries(), result["projections"], Catalog.load())
    require(recomputed == result["metrics"], "上传评分与实际候选、金标或引擎身份不一致")
    aliases = set(recomputed["engine"]["aliases"])
    dataset_name = f"fit-agent-retrieval-{dataset.version}-{dataset.content_sha256[:12]}"
    rows = []
    for qid, item in queries.items():
        row = result["metrics"]["per_query"][qid]
        projection = result["projections"][qid]
        require(all(eid in projection["scores"] for eid in row["returned_ids"]), "上传候选缺少原始分数")
        require(all(type(value) in (int, float) and math.isfinite(value) for value in projection["scores"].values()), "上传候选分数非法")
        expected_keys = {"false_return"} if item["expect_empty"] else aliases
        require(set(row["scores"]) == expected_keys, "上传排序指标必须为契约字段")
        rows.append({
            "row_id": str(uuid5(NAMESPACE_URL, dataset_name + ":" + qid)),
            "inputs": {key: item[key] for key in ("query", "conditions", "purpose", "tags", "family")},
            "expected_outputs": {"qrels": [{"corpus_id": qrel["corpus_id"], "grade": qrel["grade"]} for qrel in qrels.get(qid, ())],
                                 "expect_empty": item["expect_empty"]},
            "actual_outputs": {"returned_ids": row["returned_ids"], "candidate_scores": projection["scores"]},
            "evaluation_scores": [{"key": key, "score": round(value, 4)} for key, value in row["scores"].items()]
                                 + [{"key": "hard_constraint_violation", "score": 0}, {"key": "budget_compliant", "score": 1}],
            **result["timings"][qid], "run_name": group,
            "run_metadata": {"query_id": qid, "tags": item["tags"], "family": item["family"],
                             "dataset_version": dataset.version, "timing_scope": document["timing_scope"]},
        })
    overall = result["metrics"]["overall"]
    return {
        "dataset_name": dataset_name, "experiment_name": f"{document['run_id']}-{group}",
        "experiment_start_time": min(row["start_time"] for row in rows),
        "experiment_end_time": max(row["end_time"] for row in rows),
        "experiment_metadata": {"mode": document["mode"], "dataset": document["dataset"], "identity": document["identity"],
                                "index_identity": document["index_identity"], "timing_scope": document["timing_scope"],
                                "observation_scope": document["observation_scope"], "group": group,
                                "scoring": result["metrics"]["engine"]},
        "summary_experiment_scores": [
            {"key": key, "score": round(value, 4)}
            for key, value in overall.items()
            if value is not None and (key in aliases or key == "false_return_rate")
        ],
        "results": rows,
    }


def _upload_legacy_payload(document: dict, group: str) -> dict:
    require(document.get("dataset_sha256") == sha256(support.QUERIES_PATH), "评测证据与当前查询集不一致")
    require(document.get("adaptation_sha256") == sha256(support.ADAPTATION_PATH), "用途适配已变化")
    adaptation, purposes = support.load_adaptation()
    adapted = {row["id"]: row for row in adaptation["rows"]}
    queries = {item["id"]: item for item in query_documents_for_upload(document)}
    result = document["groups"][group]
    require(set(queries) == set(result["metrics"]["per_query"]) == set(result["projections"]) == set(result["timings"]), "上传样例覆盖不完整")
    require(validate_projections(list(queries.values()), result["projections"], Catalog.load()) == result["metrics"], "上传评分与实际候选或金标不一致")
    dataset_name = f"fit-agent-retrieval-{document['split']}-{document['dataset_sha256'][:12]}-{document['adaptation_sha256'][:12]}"
    rows = []
    for qid, item in queries.items():
        row = result["metrics"]["per_query"][qid]
        projection = result["projections"][qid]
        require(all(eid in projection["scores"] for eid in row["returned_ids"]), "上传候选缺少原始分数")
        require(all(type(value) in (int, float) and math.isfinite(value) for value in projection["scores"].values()), "上传候选分数非法")
        rows.append({
            "row_id": str(uuid5(NAMESPACE_URL, dataset_name + ":" + qid)),
            "inputs": {key: item[key] for key in ("query", "filters", "environment", "declared_equipment", "saved_profile_restrictions")}
                      | {"purpose": purposes[qid], "temporary_mapping": adapted[qid]["temporary_mapping"]},
            "expected_outputs": {"labels": item["labels"], "expect_empty": item["expect_empty"]},
            "actual_outputs": {"returned_ids": row["returned_ids"], "candidate_scores": projection["scores"]},
            "evaluation_scores": [{"key": key, "score": round(value, 4)} for key, value in row["scores"].items()]
                                 + [{"key": "hard_constraint_violation", "score": 0}, {"key": "budget_compliant", "score": 1}],
            **result["timings"][qid], "run_name": group,
            "run_metadata": {"query_id": qid, "intent": item["intent"], "family": item["family"], "split": item["split"], "timing_scope": document["timing_scope"]},
        })
    return {
        "dataset_name": dataset_name, "experiment_name": f"{document['run_id']}-{group}",
        "experiment_start_time": min(row["start_time"] for row in rows),
        "experiment_end_time": max(row["end_time"] for row in rows),
        "experiment_metadata": {key: document[key] for key in ("mode", "split", "dataset_sha256", "adaptation_sha256", "identity", "index_identity", "timing_scope", "observation_scope")}
                               | {"group": group, "scoring": result["metrics"]["engine"]},
        "summary_experiment_scores": [{"key": key, "score": round(value, 4)} for key, value in result["metrics"]["overall"].items() if value is not None],
        "results": rows,
    }


def upload_payload(document: dict, group: str) -> dict:
    require(document.get("schema") == SCHEMA, "评测证据格式非法")
    return _upload_dataset_payload(document, group) if "dataset" in document else _upload_legacy_payload(document, group)


def query_documents_for_upload(document: dict) -> list[dict]:
    selected = [item for item in support.query_documents() if item["id"] in document["query_ids"]]
    require(len(selected) == len(document["query_ids"]), "上传包含非法或重复查询 ID")
    return selected


def upload(args) -> None:
    require(bool(os.environ.get("LANGSMITH_API_KEY")), "请在 backend/.env 配置 LANGSMITH_API_KEY")
    document = support.load_document(args.source)
    require(document.get("schema") == SCHEMA, "评测证据格式非法")
    require(document.get("status") == "complete", "证据未通过发布门禁")
    if "dataset" not in document:
        support.require_inputs(Catalog.load())
    client = Client(auto_batch_tracing=False)
    for group in document["groups"]:
        receipt_path = args.source.parent / f"langsmith-{group}.json"
        if receipt_path.exists():
            receipt = support.load_document(receipt_path)
            require(receipt["source_sha256"] == sha256(args.source), "上传回执与源文件不一致")
            project = client.read_project(project_id=receipt["experiment_id"])
            require(str(project.reference_dataset_id) == receipt["dataset_id"] and project.name == f"{document['run_id']}-{group}", "远端实验与上传回执不一致")
            print(json.dumps({"group": group, "experiment_id": receipt["experiment_id"], "dataset_id": receipt["dataset_id"], "status": "verified_existing"}))
            continue
        response = client.request_with_retries("POST", "/datasets/upload-experiment", request_kwargs={"json": upload_payload(document, group)}, stop_after_attempt=1)
        response.raise_for_status()
        receipt = response.json()
        require(receipt["experiment"]["id"] and receipt["dataset"]["id"], "LangSmith 未返回实验和数据集 ID")
        support.save_document(receipt_path, {"source_sha256": sha256(args.source), "experiment_id": receipt["experiment"]["id"], "dataset_id": receipt["dataset"]["id"]})
        with (args.source.parent / "report.md").open("a", encoding="utf-8") as stream:
            stream.write(f"\n- LangSmith：`{document['run_id']}/{group}`；experiment ID `{receipt['experiment']['id']}`；dataset ID `{receipt['dataset']['id']}`。\n")
        print(json.dumps({"group": group, "experiment_id": receipt["experiment"]["id"], "dataset_id": receipt["dataset"]["id"]}))


def main() -> None:
    parser = argparse.ArgumentParser(description="MTEB 检索评测与 LangSmith 实验上传")
    commands = parser.add_subparsers(dest="command", required=True)
    live = commands.add_parser("run", help="真实检索；新数据绑定版本目录并全量执行，旧拆分显式标记为历史入口")
    source = live.add_mutually_exclusive_group(required=True)
    source.add_argument("--dataset", type=Path, help="新评测数据版本目录（正式门禁）")
    source.add_argument("--legacy-split", choices=("dev", "holdout"), help="历史查询集拆分，仅用于历史身份运行")
    live.add_argument("--groups", choices=("bm25", "dense", "hybrid"), nargs="+", default=["bm25", "dense", "hybrid"])
    live.add_argument("--alpha", type=float)
    live.add_argument("--depth", type=int)
    live.add_argument("--frozen", type=Path)
    replay = commands.add_parser("rescore", help="对历史 compare.json 的实际候选按 MTEB 重新评分")
    replay.add_argument("--source", type=Path, required=True)
    publish = commands.add_parser("upload", help="上传已批准评测数据与本地评分")
    publish.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "run":
        require(len(set(args.groups)) == len(args.groups), "检索组重复")
        asyncio.run(collect_dataset(args) if args.dataset is not None else collect_legacy(args))
    elif args.command == "rescore":
        rescore(args)
    else:
        upload(args)


if __name__ == "__main__":
    try:
        main()
    except DatasetError as exc:
        raise SystemExit(f"评测数据未就绪: {exc}") from None
    except support.EvaluationBlocked as exc:
        raise SystemExit(f"发布门禁未通过: {exc}") from None
