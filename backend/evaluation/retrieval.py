import argparse
import asyncio
import json
import math
import os
import subprocess
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from statistics import mean
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
from app.application.business.retrieval_rerank import RerankClient, RerankIdentity
from app.application.business.retrieval_response import (
    CANDIDATE_POOL_LIMIT,
    SEARCH_BUDGET,
    SEARCH_LIMIT,
    build_search_stage,
    encoded_length,
)
from app.application.business.retrieval_search import (
    RetrievalCandidate,
    search_vectors,
    validate_parameters,
)
from evaluation import retrieval_support as support
from evaluation.contracts import (
    RETRIEVAL_METRICS,
    RelevanceLabel,
    RetrievalInput,
    RetrievalResult,
    SearchResponseEvidence,
)
from evaluation.dataset import (
    DatasetError,
    EvaluationDataset,
    load_for_evaluation,
    retrieval_corpus_identity,
)
from evaluation.retrieval_metrics import evaluate_staged, require, score_legacy
from evaluation.retrieval_support import load_queries

SCHEMA_STAGED = "fit-agent-retrieval-mteb-2"
SCHEMA_LEGACY = "fit-agent-retrieval-mteb-1"
STAGED_METRICS = RETRIEVAL_METRICS
LEGACY_METRICS = ("hit@5", "recall@5", "precision@5", "mrr@5", "ndcg@5")
LEGACY_RETRIEVAL_LIMIT = 10
RERANK_GROUP = "hybrid-rerank"
RERANK_THRESHOLD_GROUPS = ("hybrid", RERANK_GROUP)
DISPLAY = {
    "recall@20": "Recall@20",
    "hit@5": "Hit@5",
    "mrr@5": "MRR@5",
    "ndcg@5": "nDCG@5",
    "precision@5": "Precision@5",
    "recall@5": "Recall@5",
}


def now() -> str:
    return datetime.now(UTC).isoformat()


def identity() -> dict:
    paths = [
        *sorted((support.BACKEND / "app/application/business").glob("retrieval_*.py")),
        Path(__file__), Path(support.__file__),
        support.BACKEND / "evaluation/contracts.py",
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


def _retrieval_inputs(
    queries: list[dict],
    projections: dict,
    catalog: Catalog,
    evaluated_by_query: dict,
    expected_threshold: float | None,
) -> list[RetrievalInput]:
    require(set(projections) == {item["id"] for item in queries}, "候选证据与查询 ID 不一致")
    inputs = []
    for query in queries:
        qid = query["id"]
        projection = projections[qid]
        require(qid in evaluated_by_query, f"缺少批准查询条件: {qid}")
        evaluated = evaluated_by_query[qid]
        require("candidate_pool" in projection, f"缺少候选池: {qid}")
        require("final_model_input" in projection, f"缺少最终模型输入: {qid}")
        pool = projection["candidate_pool"]
        final = projection["final_model_input"]
        require(isinstance(pool, list) and len(pool) <= CANDIDATE_POOL_LIMIT, f"候选池非法: {qid}")
        require(isinstance(final, list) and len(final) <= SEARCH_LIMIT, f"最终输入非法: {qid}")
        require(all(eid in catalog.exercise_ids for eid in pool), f"候选池含目录外动作: {qid}")
        require(all(eid in catalog.exercise_ids for eid in final), f"最终输入含目录外动作: {qid}")
        for key in ("response", "search_evidence", "scores", "threshold"):
            require(key in projection, f"缺少{key}: {qid}")
        require("budget_compliant" in projection, f"缺少预算标记: {qid}")
        require("hard_constraint_violations" in projection, f"缺少硬约束标记: {qid}")
        require(projection["hard_constraint_violations"] == [], f"硬约束违规: {qid}")
        require(projection["budget_compliant"] is True, f"字符预算违规: {qid}")

        response = projection["response"]
        require(isinstance(response, dict), f"公开响应非法: {qid}")
        actual_chars = encoded_length(response)
        require(projection.get("serialized_chars") == actual_chars, f"响应字符数与证据不一致: {qid}")
        require(projection.get("budget_chars") == SEARCH_BUDGET, f"响应预算与冻结预算不一致: {qid}")
        require(projection.get("item_limit") == SEARCH_LIMIT, f"条目上限与冻结上限不一致: {qid}")
        require(actual_chars <= SEARCH_BUDGET, f"公开响应超出字符预算: {qid}")

        require(
            response == projection["search_evidence"]["response_json"],
            f"公开响应与证据不一致: {qid}",
        )
        evidence = SearchResponseEvidence.model_validate(projection["search_evidence"])
        evidence.require_scored_consistency(final)

        scores = projection["scores"]
        require(isinstance(scores, dict), f"原始分数非法: {qid}")
        require(all(eid in scores for eid in pool), f"候选池缺少原始分数: {qid}")
        for exercise_id, value in scores.items():
            require(
                type(value) in (int, float) and math.isfinite(value),
                f"候选分数非法: {qid}:{exercise_id}",
            )
        threshold = projection["threshold"]
        require(
            threshold is None or (type(threshold) in (int, float) and math.isfinite(threshold)),
            f"门槛非法: {qid}",
        )
        require(threshold == expected_threshold, f"门槛与批准参数不一致: {qid}")

        candidates = []
        for exercise_id in pool:
            exercise = catalog.get(exercise_id)
            require(
                evaluated.conditions.matches(exercise),
                f"候选违反查询条件: {qid}:{exercise_id}",
            )
            candidates.append(
                RetrievalCandidate(
                    id=exercise.id,
                    score=scores[exercise_id],
                    name=exercise.name,
                    equipment=exercise.equipment,
                    body_part=exercise.body_part,
                    target=exercise.target,
                    muscle_group=exercise.muscle_group,
                )
            )
        experiment = projection.get("experiment")
        rerank_evidence = projection.get("rerank")
        if experiment is None:
            require(
                rerank_evidence is None,
                f"历史证据不得携带重排字段: {qid}",
            )
            stage = build_search_stage(candidates, evaluated, rerank=None, threshold=threshold)
        elif experiment == support.EXPERIMENT_BASELINE:
            require(rerank_evidence is None, f"RRF baseline 携带重排证据: {qid}")
            stage = build_search_stage(candidates, evaluated, rerank=None, threshold=threshold)
        elif experiment == support.EXPERIMENT_RERANK:
            require(isinstance(rerank_evidence, dict), f"重排实验缺少证据: {qid}")
            result, rerank_identity = support.rebuild_rerank(rerank_evidence)
            stage = build_search_stage(
                candidates,
                evaluated,
                rerank=result,
                rerank_identity=rerank_identity,
                threshold=threshold,
            )
        else:
            raise ValueError(f"实验身份非法: {qid}:{experiment}")
        require(
            [item.id for item in stage.candidate_pool] == pool,
            f"候选池与共享阶段不一致: {qid}",
        )
        require(
            [item.id for item in stage.final_input] == final,
            f"最终输入与候选池顺序或裁剪不一致: {qid}",
        )
        require(
            stage.response.model_dump() == response,
            f"公开响应与共享阶段不一致: {qid}",
        )

        inputs.append(
            RetrievalInput(
                query_id=qid,
                scenario_id=query.get("scenario_id"),
                expect_empty=query["expect_empty"],
                labels=[
                    RelevanceLabel(exercise_id=label["id"], grade=label["grade"])
                    for label in query["labels"]
                ],
                candidate_pool=pool,
                final_model_input=final,
            )
        )
    return inputs


def _result_scores(result: RetrievalResult) -> dict[str, float]:
    if result.expect_empty:
        return {"false_return": float(bool(result.false_return))}
    return {metric.name: metric.value for metric in result.metrics}


def summarize_staged(results: list[RetrievalResult], queries: list[dict]) -> dict:
    metadata = {item["id"]: item for item in queries}
    rows = {}
    for result in results:
        query = metadata[result.query_id]
        rows[result.query_id] = {
            "id": result.query_id,
            "intent": query.get("intent"),
            "purpose": query.get("purpose"),
            "tags": list(query["tags"]),
            "split": query["split"],
            "family": query["family"],
            "expected_ids": {
                label["id"]: label["grade"] for label in query["labels"]
            },
            "expect_empty": result.expect_empty,
            "candidate_pool": list(result.candidate_pool),
            "final_model_input": list(result.final_model_input),
            "metrics": [metric.model_dump() for metric in result.metrics],
            "scores": _result_scores(result),
            "false_return": result.false_return,
        }

    def summarize(selected: list[dict]) -> dict:
        positives = [row for row in selected if not row["expect_empty"]]
        empties = [row for row in selected if row["expect_empty"]]
        return {
            "queries": len(selected),
            "with_labels": len(positives),
            "empty_expectation_queries": len(empties),
            **{
                key: mean(row["scores"][key] for row in positives) if positives else None
                for key in STAGED_METRICS
            },
            "false_return_rate": mean(row["scores"]["false_return"] for row in empties) if empties else None,
        }

    return {
        "engine": {
            "name": "mteb",
            "version": version("mteb"),
            "entry": "mteb._evaluators.retrieval_metrics.calculate_retrieval_scores",
            "mrr_entry": "mteb._evaluators.retrieval_metrics.mrr",
            "relevance": "grade>=1",
            "ndcg_gain": "2^grade-1",
            "precision_denominator": "5",
            "stages": {
                "recall@20": "candidate_pool",
                "hit@5": "final_model_input",
                "mrr@5": "final_model_input",
                "ndcg@5": "final_model_input",
                "precision@5": "final_model_input",
            },
            "averaging": "macro over labelled queries from per-query raw scores; expect_empty scored separately",
            "identity": "staged_candidate_pool_20_final_input_5",
        },
        "overall": summarize(list(rows.values())),
        "by_intent": {
            intent: summarize([row for row in rows.values() if row["intent"] == intent])
            for intent in sorted({item["intent"] for item in queries if item.get("intent") is not None})
        },
        "by_tag": {
            tag: summarize([row for row in rows.values() if tag in row["tags"]])
            for tag in sorted({tag for item in queries for tag in item.get("tags", [])})
        },
        "per_query": rows,
    }


def validate_projections(
    queries: list[dict],
    projections: dict,
    catalog: Catalog,
    evaluated_by_query: dict,
    expected_threshold: float | None,
) -> dict:
    inputs = _retrieval_inputs(
        queries, projections, catalog, evaluated_by_query, expected_threshold
    )
    return summarize_staged(evaluate_staged(inputs), queries)


def validate_legacy_projections(queries: list[dict], projections: dict, catalog: Catalog) -> dict:
    # 历史入口保留单阶段 @5 身份；缺少候选池时不得推算 Recall@20。
    require(set(projections) == {item["id"] for item in queries}, "候选证据与查询 ID 不一致")
    rankings = {}
    for qid, projection in projections.items():
        ids = projection.get("final_model_input", projection.get("returned"))
        require(isinstance(ids, list) and len(ids) <= LEGACY_RETRIEVAL_LIMIT, f"返回候选非法: {qid}")
        require(all(eid in catalog.exercise_ids for eid in ids), f"返回目录外动作: {qid}")
        require(projection["budget_compliant"] is True, f"字符预算违规: {qid}")
        require(not projection["hard_constraint_violations"], f"硬约束违规: {qid}")
        rankings[qid] = ids
    return score_legacy(queries, rankings)


def _metric_keys(document: dict) -> list[str]:
    return list(STAGED_METRICS if document.get("schema") == SCHEMA_STAGED else LEGACY_METRICS)


def _report_lines(document: dict) -> list[str]:
    dataset = document.get("dataset")
    lines = [
        f"\n## {document['run_id']}\n",
        f"- 证据 Schema：`{document['schema']}`；类型：`{document['mode']}`；拆分：`{document['split']}`。",
        f"- 评分引擎：MTEB `{version('mteb')}`；相关性 grade>=1；nDCG 增益 `2^grade-1`；Precision@5 固定分母 5。",
        (
            "- 阶段：Recall@20 取候选池；Hit@5、MRR@5、nDCG@5、Precision@5 取最终模型输入。"
            if document.get("schema") == SCHEMA_STAGED
            else "- 历史单阶段：仅记录 @5 指标，不推算 Recall@20。"
        ),
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
    if "parameters" in document:
        lines.append(f"- 检索参数：`{json.dumps(document['parameters'], ensure_ascii=False, sort_keys=True)}`。")
    if document.get("status") != "complete":
        lines.append("- 发布门禁：未通过。")
    keys = _metric_keys(document)
    header = "| group | 有标注 / 无答案 | " + " | ".join(DISPLAY[key] for key in keys) + " | 无答案误返回率 |"
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


def base_document(mode: str, split: str, *, staged: bool = True) -> dict:
    return {"schema": SCHEMA_STAGED if staged else SCHEMA_LEGACY,
            "run_id": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8],
            "mode": mode, "split": split, "status": "running", "started_at": now(), "identity": identity(), "groups": {}}


def _group_threshold(document: dict, group: str) -> float | None:
    return document["parameters"].get("threshold") if group in RERANK_THRESHOLD_GROUPS else None


def _validate_same_pool(document: dict) -> None:
    # 重排实验必须与同一次召回产生的显式基线共用候选 ID、顺序与 RRF 分数。
    groups = document.get("groups")
    require(isinstance(groups, dict), "缺少检索组")
    if RERANK_GROUP not in groups:
        return
    require("hybrid" in groups, "重排实验缺少同池 RRF 基线")
    baseline = groups["hybrid"]
    reranked = groups[RERANK_GROUP]
    base_projection = baseline.get("projections")
    rerank_projection = reranked.get("projections")
    require(isinstance(base_projection, dict), "同池基线缺少候选证据")
    require(isinstance(rerank_projection, dict), "重排实验缺少候选证据")
    require(set(base_projection) == set(rerank_projection), "同池对照查询覆盖不一致")
    for qid, base in base_projection.items():
        current = rerank_projection[qid]
        require(current.get("experiment") == support.EXPERIMENT_RERANK, f"重排实验身份不符: {qid}")
        require(base.get("experiment") == support.EXPERIMENT_BASELINE, f"同池基线身份不符: {qid}")
        require(current["candidate_pool"] == base["candidate_pool"], f"同池候选 ID 或顺序不一致: {qid}")
        require(current["scores"] == base["scores"], f"同池 RRF 分数不一致: {qid}")
        require(current["threshold"] == base["threshold"], f"同池门槛不一致: {qid}")
        require(current["hard_constraint_violations"] == [], f"重排候选违反硬约束: {qid}")
        require(current["budget_compliant"] is True, f"重排响应超出预算: {qid}")
    if baseline.get("status") != "complete" or reranked.get("status") != "complete":
        return
    base_rows = baseline["metrics"]["per_query"]
    rerank_rows = reranked["metrics"]["per_query"]
    require(set(base_rows) == set(rerank_rows), "同池对照指标覆盖不一致")
    for qid, row in rerank_rows.items():
        if row["expect_empty"]:
            continue
        base_recall = next(
            metric["value"]
            for metric in base_rows[qid]["metrics"]
            if metric["name"] == "recall@20"
        )
        rerank_recall = next(
            metric["value"]
            for metric in row["metrics"]
            if metric["name"] == "recall@20"
        )
        require(base_recall == rerank_recall, f"同池 Recall@20 不一致: {qid}")


def validate_group(
    dataset: EvaluationDataset,
    group: str,
    projections: dict,
    timings: dict,
    catalog: Catalog,
    mappings: dict | None = None,
    expected_threshold: float | None = None,
    queries: list[dict] | None = None,
) -> dict:
    # 未审阅候选门禁覆盖整个参与 Recall@20 的候选池。
    selected = list(queries) if queries is not None else list(dataset.queries)
    selected_ids = [item["query_id"] for item in selected]
    unknown = dataset.unreviewed_candidates(
        {qid: row["candidate_pool"] for qid, row in projections.items()}
    )
    if unknown:
        return {"status": "blocked_unreviewed_candidates", "unknown_candidates": unknown,
                "metrics": None, "projections": projections, "timings": timings}
    require(catalog is not None, "评测算分需要 Catalog")
    require(mappings is not None, "评测算分需要字段映射")
    evaluated_by_query = {
        item["query_id"]: support.dataset_conditions(item, catalog, mappings)
        for item in selected
    }
    scoring_map = {item["id"]: item for item in dataset.scoring_queries()}
    require(set(selected_ids) <= set(scoring_map), "子集查询不属于批准查询集")
    scoring = [scoring_map[qid] for qid in selected_ids]
    return {"status": "complete", "metrics": validate_projections(
                scoring, projections, catalog, evaluated_by_query, expected_threshold
            ),
            "projections": projections, "timings": timings}


def _structural_publishable(document: dict) -> bool:
    groups = document.get("groups")
    if not isinstance(groups, dict) or not groups:
        return False
    query_ids = document.get("query_ids")
    if not isinstance(query_ids, list) or not query_ids:
        return False
    if len(set(query_ids)) != len(query_ids):
        return False
    keys = _metric_keys(document)
    staged = document.get("schema") == SCHEMA_STAGED
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
        if set(per_query) != set(query_ids):
            return False
        projections = group.get("projections") if staged else None
        if staged and (
            not isinstance(projections, dict)
            or set(projections) != set(query_ids)
        ):
            return False
        for qid, row in per_query.items():
            scores = row.get("scores")
            if not isinstance(scores, dict):
                return False
            expected = {"false_return"} if row.get("expect_empty") else set(keys)
            if set(scores) != expected:
                return False
            if not staged:
                continue
            projection = projections[qid]
            if not isinstance(projection, dict):
                return False
            pool = projection.get("candidate_pool")
            final = projection.get("final_model_input")
            if row.get("candidate_pool") != pool:
                return False
            if row.get("final_model_input") != final:
                return False
            if not isinstance(pool, list) or len(pool) > CANDIDATE_POOL_LIMIT:
                return False
            if not isinstance(final, list) or len(final) > SEARCH_LIMIT:
                return False
            if projection.get("hard_constraint_violations") != []:
                return False
            if projection.get("budget_compliant") is not True:
                return False
            candidate_scores = projection.get("scores")
            if (
                not isinstance(candidate_scores, dict)
                or not set(pool) <= set(candidate_scores)
            ):
                return False
            evidence = projection.get("search_evidence")
            if not isinstance(evidence, dict):
                return False
            visible = evidence.get("model_visible")
            if not isinstance(visible, dict):
                return False
            items = visible.get("items")
            if not isinstance(items, list) or len(items) > SEARCH_LIMIT:
                return False
            if [item.get("exercise_id") for item in items] != final:
                return False
    return True


def settle_document(document: dict) -> str:
    document["finished_at"] = now()
    document["status"] = "complete" if publishable(document) else "blocked"
    return document["status"]


def _validate_staged_evidence(document: dict) -> None:
    # 发布判定复用算分阶段的同一校验：候选、公开响应、预算、硬约束与金标必须能重算已记录指标。
    require(isinstance(document.get("dataset"), dict), "两阶段证据缺少数据身份")
    require(isinstance(document.get("parameters"), dict), "两阶段证据缺少批准检索参数")
    require(isinstance(document.get("index_identity"), dict), "两阶段证据缺少物理索引身份")
    dataset = bind_dataset(document)
    selected_ids = document.get("query_ids")
    require(
        isinstance(selected_ids, list) and bool(selected_ids),
        "查询列表缺失",
    )
    selected_set = set(selected_ids)
    scoring_map = {item["id"]: item for item in dataset.scoring_queries()}
    require(selected_set <= set(scoring_map), "查询列表包含数据版本外查询")
    scoring = [scoring_map[qid] for qid in selected_ids]
    catalog = Catalog.load()
    mappings = rt.load_mappings()
    evaluated_by_query = {
        item["query_id"]: support.dataset_conditions(item, catalog, mappings)
        for item in dataset.queries
        if item["query_id"] in selected_set
    }
    query_ids = set(selected_ids)
    for group, result in document["groups"].items():
        projections = result.get("projections")
        require(isinstance(projections, dict), f"{group} 缺少候选证据")
        require(set(projections) == query_ids, f"{group} 候选证据覆盖与数据版本不一致")
        require(
            all(isinstance(row, dict) for row in projections.values()),
            f"{group} 候选证据结构非法",
        )
        threshold = _group_threshold(document, group)
        status = result.get("status")
        if status == "complete":
            recomputed = validate_projections(
                scoring, projections, catalog, evaluated_by_query, threshold
            )
            require(recomputed == result["metrics"], f"{group} 评分与候选、金标或引擎身份不一致")
            unknown = dataset.unreviewed_candidates(
                {qid: row["candidate_pool"] for qid, row in projections.items()}
            )
            require(not unknown, f"{group} 存在未审阅候选")
        elif status == "blocked_unreviewed_candidates":
            require(result.get("metrics") is None, f"{group} 阻断证据不得携带评分")
            _retrieval_inputs(
                scoring, projections, catalog, evaluated_by_query, threshold
            )
            unknown = dataset.unreviewed_candidates(
                {qid: row["candidate_pool"] for qid, row in projections.items()}
            )
            require(bool(unknown), f"{group} 阻断状态缺少未审阅候选")
            require(unknown == result.get("unknown_candidates"), f"{group} 未审阅候选证据不一致")
        else:
            raise ValueError(f"{group} 组状态非法: {status}")
    _validate_same_pool(document)


def replay_document(document: dict) -> dict:
    # 回放只消费已保存证据：重建候选、重排结果与共享阶段，不调用模型或数据库检索。
    schema = document.get("schema")
    if schema == SCHEMA_LEGACY:
        catalog = Catalog.load()
        support.require_inputs(catalog)
        support.load_adaptation()
        queries = query_documents_for_upload(document)
        groups = {}
        for group, result in document["groups"].items():
            metrics = validate_legacy_projections(
                queries, result["projections"], catalog
            )
            require(metrics == result["metrics"], f"{group} 历史评分与证据不一致")
            groups[group] = {"metrics": metrics["overall"], "experiment": "historical_single_stage"}
        return {"schema": schema, "status": "complete", "groups": groups}
    if schema != SCHEMA_STAGED:
        raise ValueError("评测证据格式非法")
    _validate_staged_evidence(document)
    groups = {}
    blocked = []
    for group, result in document["groups"].items():
        status = result.get("status")
        metrics = result.get("metrics")
        if status != "complete":
            blocked.append(group)
        groups[group] = {
            "status": status,
            "metrics": metrics["overall"] if isinstance(metrics, dict) else None,
            "unknown_candidates": result.get("unknown_candidates"),
            "experiment": document.get("experiments", {}).get(
                group, support.EXPERIMENT_BASELINE
            ),
        }
    return {
        "schema": schema,
        "status": "blocked" if blocked else "complete",
        "blocked_groups": blocked,
        "query_ids": list(document["query_ids"]),
        "groups": groups,
    }


def replay_command(args) -> None:
    document = support.load_document(args.source)
    report = replay_document(document)
    if args.output is not None:
        support.save_document(args.output, report)
    print(json.dumps(report, ensure_ascii=False))


def publishable(document: dict) -> bool:
    if not _structural_publishable(document):
        return False
    schema = document.get("schema")
    if schema == SCHEMA_LEGACY:
        return True
    if schema != SCHEMA_STAGED:
        return False
    try:
        _validate_staged_evidence(document)
    except (ValueError, DatasetError):
        return False
    return True


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
    selected_queries = list(dataset.queries)
    if args.sample is not None:
        selected_queries = support.select_validation_queries(dataset)
    elif args.limit is not None:
        require(
            type(args.limit) is int and 0 < args.limit <= len(selected_queries),
            "子集规模必须位于 1 到批准查询数量之间",
        )
        selected_queries = selected_queries[: args.limit]
    require(
        len({item["query_id"] for item in selected_queries}) == len(selected_queries),
        "子集查询 ID 重复",
    )
    values = frozen_values()
    params = {"fusion": values["fusion"], "rrf_k": values["rrf_k"] if args.rrf_k is None else args.rrf_k,
              "bm25_depth": values["bm25_depth"] if args.depth is None else args.depth,
              "dense_depth": values["dense_depth"] if args.depth is None else args.depth,
              "threshold": values["threshold"],
              "candidate_pool_limit": values["candidate_pool_limit"],
              "final_input_limit": values["final_input_limit"]}
    require(args.frozen is None, "新数据入口不接受冻结来源参数")
    validate_parameters("评测", params["rrf_k"], support.RECALL_LIMIT, params["bm25_depth"], params["dense_depth"])
    require(params["threshold"] is None or math.isfinite(params["threshold"]), "门槛非法")
    groups = list(args.groups)
    if args.rerank:
        require("hybrid" in groups, "重排实验必须选择 hybrid 组")
        require(RERANK_GROUP not in groups, "重排实验由 --rerank 生成独立实验身份")
    embedding = EmbeddingClient.from_env()
    dataset_identity = dataset.identity()
    vector_identity = support.query_vector_identity(embedding.config, corpus, dataset_identity)
    vectors = support.read_query_cache(
        vector_identity,
        [
            {"id": item["query_id"], "query": item["query"]}
            for item in selected_queries
        ],
    )[0] if any(group != "bm25" for group in groups) else {}
    document = base_document("live", dataset.version)
    document.update(dataset=dataset_identity, parameters=params, query_ids=[item["query_id"] for item in selected_queries],
                    vector_cache_identity=vector_identity, embedding_requests=0,
                    timing_scope="cached_vector_search_and_response",
                    observation_scope="new_evaluation_dataset_full_run")
    if args.limit is not None or args.sample is not None:
        document["sample_scope"] = {
            "kind": (
                "validation_selection" if args.sample is not None else "validation_subset"
            ),
            "limit": args.limit,
            "query_ids": [item["query_id"] for item in selected_queries],
        }
        document["observation_scope"] = "new_evaluation_dataset_subset"
    if args.rerank:
        document["experiments"] = {"hybrid": support.EXPERIMENT_BASELINE, RERANK_GROUP: support.EXPERIMENT_RERANK}
    mappings = rt.load_mappings()
    async with AsyncExitStack() as stack:
        db = await stack.enter_async_context(MilvusConnection.from_env())
        rerank_client = None
        rerank_identity = None
        if args.rerank:
            rerank_client = await stack.enter_async_context(RerankClient.from_env())
            rerank_identity = RerankIdentity.from_config(rerank_client.config)
            document["rerank_identity"] = {
                "model": rerank_identity.model,
                "endpoint": rerank_identity.endpoint,
                "text_version": rerank_identity.text_version,
            }
        collection, index_identity = await support.open_published(db, corpus)
        document["index_identity"] = index_identity
        for group in groups:
            projections, timings = {}, {}
            rerank_projections = {}
            for item in selected_queries:
                qid = item["query_id"]
                started = now()
                if args.rerank and group == "hybrid":
                    comparison = await support.execute_comparison_query(
                        db,
                        collection,
                        corpus,
                        mappings,
                        item,
                        vectors[qid],
                        group=group,
                        rrf_k=params["rrf_k"],
                        bm25_depth=params["bm25_depth"],
                        dense_depth=params["dense_depth"],
                        threshold=params["threshold"],
                        rerank_client=rerank_client,
                        rerank_identity=rerank_identity,
                    )
                    projections[qid] = comparison[support.EXPERIMENT_BASELINE]
                    rerank_projections[qid] = comparison[support.EXPERIMENT_RERANK]
                else:
                    projections[qid] = await support.execute_query(
                        db,
                        collection,
                        corpus,
                        mappings,
                        item,
                        vectors[qid] if group != "bm25" else [],
                        group=group,
                        rrf_k=params["rrf_k"],
                        bm25_depth=params["bm25_depth"],
                        dense_depth=params["dense_depth"],
                        threshold=params["threshold"],
                    )
                timings[qid] = {"start_time": started, "end_time": now()}
            document["groups"][group] = validate_group(
                dataset, group, projections, timings, corpus.catalog, mappings,
                params["threshold"] if group in RERANK_THRESHOLD_GROUPS else None,
                selected_queries,
            )
            if rerank_projections:
                document["groups"][RERANK_GROUP] = validate_group(
                    dataset, RERANK_GROUP, rerank_projections,
                    {qid: dict(row) for qid, row in timings.items()},
                    corpus.catalog, mappings, params["threshold"],
                    selected_queries,
                )
    _validate_same_pool(document)
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
    params = {"fusion": values["fusion"], "rrf_k": values["rrf_k"] if args.rrf_k is None else args.rrf_k,
              "bm25_depth": values["bm25_depth"] if args.depth is None else args.depth,
              "dense_depth": values["dense_depth"] if args.depth is None else args.depth,
              "threshold": values["threshold"]}
    if args.legacy_split == "holdout":
        require(args.frozen is not None and args.rrf_k is None and args.depth is None, "留出运行需要 --frozen 开发集证据，且不得覆盖参数")
        frozen = support.load_document(args.frozen)
        require(frozen["mode"] == "live" and frozen["split"] == "dev", "冻结来源必须为真实开发集运行")
        require(frozen["dataset_sha256"] == sha256(support.QUERIES_PATH) and frozen["identity"] == identity(), "冻结来源与当前数据或代码不一致")
        params = frozen["parameters"]
    else:
        require(args.frozen is None, "开发集通过 --rrf-k / --depth 指定参数")
    require(params["fusion"] == "rrf", "冻结来源必须使用 RRF 融合")
    validate_parameters("评测", params["rrf_k"], support.LEGACY_RECALL_LIMIT, params["bm25_depth"], params["dense_depth"])
    require(params["threshold"] is None or math.isfinite(params["threshold"]), "门槛非法")
    vector_identity = support.query_vector_identity(EmbeddingClient.from_env().config, corpus)
    vectors = support.read_query_cache(vector_identity, selected)[0] if any(group != "bm25" for group in args.groups) else {}
    document = base_document("live", args.legacy_split, staged=False)
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
                                                conditions=evaluated.conditions, rrf_k=params["rrf_k"], bm25_depth=params["bm25_depth"],
                                                dense_depth=params["dense_depth"], limit=support.LEGACY_RECALL_LIMIT, mode=group)
                projections[qid] = support.project_legacy_response(
                    corpus.catalog, recalled, evaluated,
                    params["threshold"] if group == "hybrid" else None,
                )
                timings[qid] = {"start_time": started, "end_time": now()}
            document["groups"][group] = {"status": "complete", "metrics": validate_legacy_projections(selected, projections, corpus.catalog),
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
    document = base_document("historical_rescore", history["split"], staged=False)
    document.update(source=str(args.source.resolve()), source_sha256=sha256(args.source),
                    dataset_sha256=sha256(support.QUERIES_PATH), adaptation_sha256=sha256(support.ADAPTATION_PATH),
                    source_identity=history["run_identity"], index_identity=history["index_identity"],
                    deferred_query_ids=history["deferred_query_ids"], query_ids=[item["id"] for item in selected],
                    timing_scope="offline_batch_rescoring", observation_scope="historical_retrieval_results_only")
    for group, result in history["groups"].items():
        projections = result["per_query_projection"]
        started = now()
        metrics = validate_legacy_projections(selected, projections, catalog)
        finished = now()
        document["groups"][group] = {"status": "complete", "metrics": metrics, "projections": projections,
                                     "timings": {item["id"]: {"start_time": started, "end_time": finished} for item in selected}}
    document["finished_at"] = now()
    document["status"] = "complete"
    save_result(document)


def _parameter_metadata(document: dict) -> dict:
    if "parameters" not in document:
        return {}
    allowed = (
        "fusion", "rrf_k", "alpha", "bm25_depth", "dense_depth", "threshold",
        "candidate_pool_limit", "final_input_limit", "recall_limit",
    )
    return {"parameters": {key: value for key, value in document["parameters"].items() if key in allowed}}


def _upload_dataset_payload(document: dict, group: str) -> dict:
    require(document.get("status") == "complete", "证据未通过发布门禁")
    require("sample_scope" not in document, "子集验证证据不得作为正式实验上传")
    dataset = bind_dataset(document)
    queries = {item["query_id"]: item for item in dataset.queries}
    qrels = dataset.qrels_by_query()
    result = document["groups"][group]
    require(result.get("status") == "complete", "检索组未通过发布门禁")
    require(set(queries) == set(result["metrics"]["per_query"]) == set(result["projections"]) == set(result["timings"]), "上传样例覆盖不完整")
    require(document["query_ids"] == [item["query_id"] for item in dataset.queries], "上传查询列表与数据版本不一致")
    unknown = dataset.unreviewed_candidates(
        {qid: row["candidate_pool"] for qid, row in result["projections"].items()}
    )
    require(not unknown, f"存在未审阅候选: {unknown}")
    catalog = Catalog.load()
    mappings = rt.load_mappings()
    evaluated_by_query = {
        item["query_id"]: support.dataset_conditions(item, catalog, mappings)
        for item in dataset.queries
    }
    require("parameters" in document, "缺少批准检索参数")
    _validate_same_pool(document)
    expected_threshold = _group_threshold(document, group)
    recomputed = validate_projections(
        dataset.scoring_queries(), result["projections"], catalog, evaluated_by_query, expected_threshold
    )
    require(recomputed == result["metrics"], "上传评分与实际候选、金标或引擎身份不一致")
    expected_keys = set(STAGED_METRICS)
    dataset_name = f"fit-agent-retrieval-{dataset.version}-{dataset.content_sha256[:12]}"
    baseline_projections = (
        document["groups"]["hybrid"]["projections"]
        if group == RERANK_GROUP
        else None
    )
    rows = []
    for qid, item in queries.items():
        row = result["metrics"]["per_query"][qid]
        projection = result["projections"][qid]
        require(
            all(eid in projection["scores"] for eid in row["candidate_pool"]),
            "上传候选缺少原始分数",
        )
        require(
            all(
                type(value) in (int, float) and math.isfinite(value)
                for value in projection["scores"].values()
            ),
            "上传候选分数非法",
        )
        keys = {"false_return"} if item["expect_empty"] else expected_keys
        require(set(row["scores"]) == keys, "上传排序指标必须为契约字段")
        evidence = SearchResponseEvidence.model_validate(projection["search_evidence"])
        evidence.require_scored_consistency(row["final_model_input"])
        actual_outputs = {
            "candidate_pool": row["candidate_pool"],
            "final_model_input": row["final_model_input"],
            "candidate_scores": {
                eid: projection["scores"][eid] for eid in row["candidate_pool"]
            },
            "search_response": projection["response"],
            "search_evidence": projection["search_evidence"],
            "experiment": projection.get("experiment"),
        }
        if group == RERANK_GROUP:
            public = support.public_rerank_evidence(projection.get("rerank"))
            require(public is not None, f"重排上传缺少结果: {qid}")
            baseline = baseline_projections[qid]
            actual_outputs["rerank"] = public
            actual_outputs["baseline"] = {
                "candidate_pool": baseline["candidate_pool"],
                "final_model_input": baseline["final_model_input"],
                "search_response": baseline["response"],
            }
        rows.append({
            "row_id": str(uuid5(NAMESPACE_URL, dataset_name + ":" + qid)),
            "inputs": {key: item[key] for key in ("query", "conditions", "purpose", "tags", "family")},
            "expected_outputs": {"qrels": [{"corpus_id": qrel["corpus_id"], "grade": qrel["grade"]} for qrel in qrels.get(qid, ())],
                                 "expect_empty": item["expect_empty"]},
            "actual_outputs": actual_outputs,
            "evaluation_scores": [{"key": key, "score": round(value, 4)} for key, value in row["scores"].items()]
                                 + [{"key": "hard_constraint_violation", "score": 0}, {"key": "budget_compliant", "score": 1}],
            **result["timings"][qid], "run_name": group,
            "run_metadata": {"query_id": qid, "tags": item["tags"], "family": item["family"],
                             "dataset_version": dataset.version, "timing_scope": document["timing_scope"],
                             "experiment": projection.get("experiment")},
        })
    overall = result["metrics"]["overall"]
    experiment_metadata = {"mode": document["mode"], "dataset": document["dataset"], "identity": document["identity"],
                           "index_identity": document["index_identity"], "timing_scope": document["timing_scope"],
                           "observation_scope": document["observation_scope"], "group": group,
                           "experiment": document.get("experiments", {}).get(group),
                           "scoring": result["metrics"]["engine"]}
    if group == RERANK_GROUP:
        require(isinstance(document.get("rerank_identity"), dict), "重排实验缺少配置身份")
        require(
            isinstance(document["groups"].get("hybrid", {}).get("projections"), dict),
            "重排实验缺少同池基线证据",
        )
        identity = document["rerank_identity"]
        experiment_metadata["rerank"] = {
            "model": identity["model"],
            "text_version": identity["text_version"],
            "endpoint_sha256": support.sha256_text(identity["endpoint"]),
        }
    return {
        "dataset_name": dataset_name, "experiment_name": f"{document['run_id']}-{group}",
        "experiment_start_time": min(row["start_time"] for row in rows),
        "experiment_end_time": max(row["end_time"] for row in rows),
        "experiment_metadata": experiment_metadata | _parameter_metadata(document),
        "summary_experiment_scores": [
            {"key": key, "score": round(value, 4)}
            for key, value in overall.items()
            if value is not None and (key in expected_keys or key == "false_return_rate")
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
    require(validate_legacy_projections(list(queries.values()), result["projections"], Catalog.load()) == result["metrics"], "上传评分与实际候选或金标不一致")
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
                               | {"group": group, "scoring": result["metrics"]["engine"]}
                               | _parameter_metadata(document),
        "summary_experiment_scores": [{"key": key, "score": round(value, 4)} for key, value in result["metrics"]["overall"].items() if value is not None],
        "results": rows,
    }


def upload_payload(document: dict, group: str) -> dict:
    schema = document.get("schema")
    if schema == SCHEMA_STAGED:
        require("dataset" in document, "两阶段证据必须绑定评测数据版本")
        return _upload_dataset_payload(document, group)
    if schema == SCHEMA_LEGACY:
        require("dataset" not in document, "历史格式不接受两阶段数据证据")
        return _upload_legacy_payload(document, group)
    raise ValueError("评测证据格式非法")


def query_documents_for_upload(document: dict) -> list[dict]:
    selected = [item for item in support.query_documents() if item["id"] in document["query_ids"]]
    require(len(selected) == len(document["query_ids"]), "上传包含非法或重复查询 ID")
    return selected


def upload(args) -> None:
    require(bool(os.environ.get("LANGSMITH_API_KEY")), "请在 backend/.env 配置 LANGSMITH_API_KEY")
    document = support.load_document(args.source)
    require(document.get("schema") in (SCHEMA_STAGED, SCHEMA_LEGACY), "评测证据格式非法")
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
    live.add_argument("--rerank", action="store_true", help="在同一 hybrid 候选池上执行显式 RRF baseline 与真实重排对照")
    sample = live.add_mutually_exclusive_group()
    sample.add_argument("--limit", type=int, help="仅执行批准查询的前 N 条，用于本地验证，不作为正式成绩上传")
    sample.add_argument("--sample", choices=("validation",), help="使用与 LangSmith 验证一致的 6 条已批准查询，不作为正式成绩上传")
    live.add_argument("--rrf-k", type=int, help="RRF 平滑参数，默认读取正式运行配置")
    live.add_argument("--depth", type=int)
    live.add_argument("--frozen", type=Path)
    replay = commands.add_parser("rescore", help="对历史 compare.json 的实际候选按 MTEB 重新评分")
    replay.add_argument("--source", type=Path, required=True)
    staged = commands.add_parser(
        "rescore-staged",
        help="对冻结的两阶段检索证据使用批准数据版本离线重评分并生成补标草案",
    )
    staged.add_argument("--source", type=Path, required=True)
    staged.add_argument("--dataset", type=Path, required=True)
    staged.add_argument(
        "--output",
        type=Path,
        help="followup 根目录；必须位于源运行目录的 followup/ 下",
    )
    offline = commands.add_parser("replay", help="仅使用已保存证据离线重建响应与评分，不调用模型或数据库检索")
    offline.add_argument("--source", type=Path, required=True)
    offline.add_argument("--output", type=Path)
    publish = commands.add_parser("upload", help="上传已批准评测数据与本地评分")
    publish.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "run":
        require(len(set(args.groups)) == len(args.groups), "检索组重复")
        if args.dataset is None:
            require(not args.rerank, "历史入口不支持重排实验")
            require(args.limit is None, "历史入口不支持子集限制")
        asyncio.run(collect_dataset(args) if args.dataset is not None else collect_legacy(args))
    elif args.command == "rescore":
        rescore(args)
    elif args.command == "rescore-staged":
        from evaluation.retrieval_rescore import rescore_staged

        rescore_staged(args)
    elif args.command == "replay":
        replay_command(args)
    else:
        upload(args)


if __name__ == "__main__":
    try:
        main()
    except DatasetError as exc:
        raise SystemExit(f"评测数据未就绪: {exc}") from None
    except support.EvaluationBlocked as exc:
        raise SystemExit(f"发布门禁未通过: {exc}") from None
