import hashlib
import io
import json
import math
import os
import subprocess
from collections import Counter
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import jsonlines

from app.application.business import retrieval_text as rt
from app.application.business.catalog import Catalog
from app.application.business.retrieval_response import (
    CANDIDATE_POOL_LIMIT,
    SEARCH_LIMIT,
)
from evaluation import retrieval_support as support
from evaluation.dataset import (
    EvaluationDataset,
    load_for_evaluation,
    qrel_digest,
    query_digest,
    scenario_digest,
)
from evaluation.retrieval import (
    RERANK_GROUP,
    SCHEMA_STAGED,
    STAGED_METRICS,
    _group_threshold,
    _validate_staged_evidence,
    bind_dataset,
    validate_projections,
)
from evaluation.retrieval_metrics import require

SCHEMA_RESCORE = "fit-agent-retrieval-rescore-staged-1"
SCHEMA_REVIEW_DRAFT = "fit-agent-retrieval-review-draft-1"
FOLLOWUP_DIR = "followup"
MARKER_PREFIX = "retrieval-rescore-followup"
TEST_REPORT_REFERENCE = "tmp/retrieval-metrics-rescore-fix/report.md"
GRADE_RULES = {
    3: "高度相关：动作与查询目标、条件及用途直接一致",
    2: "相关：动作满足查询意图，存在非关键差异",
    1: "弱相关：仅部分满足查询意图或条件",
    0: "已审阅且不相关：动作不能满足查询意图或条件",
}
NEW_REQUESTS = {
    "embedding": 0,
    "rerank": 0,
    "generation_model": 0,
    "judge": 0,
    "langsmith": 0,
    "milvus": 0,
    "network": 0,
}


def now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def _json_text(document: object, *, indent: int | None = 2) -> str:
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        indent=indent,
        separators=(",", ": ") if indent is not None else (",", ":"),
    ) + "\n"


def _jsonl_dumps(row: dict) -> str:
    return json.dumps(
        row, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )


def _jsonl_text(rows: list[dict]) -> str:
    buffer = io.StringIO()
    writer = jsonlines.Writer(buffer, dumps=_jsonl_dumps)
    for row in rows:
        writer.write(row)
    return buffer.getvalue()


def _dataset_digest(dataset: EvaluationDataset) -> dict:
    files = dataset.manifest["files"]
    return {
        "version": dataset.version,
        "version_dir": dataset.identity()["version_dir"],
        "manifest_sha256": dataset.manifest_sha256,
        "content_sha256": dataset.content_sha256,
        "corpus_sha256": dataset.corpus_sha256,
        "queries_sha256": files["queries"]["sha256"],
        "qrels_sha256": files["qrels"]["sha256"],
        "corpus_file_sha256": files["corpus"]["sha256"],
        "scenarios_sha256": files["scenarios"]["sha256"],
        "release_decision_ref": dataset.manifest["release"]["decision_ref"],
    }


def _implementation_identity() -> dict:
    paths = [
        *sorted((support.BACKEND / "app/application/business").glob("retrieval_*.py")),
        support.BACKEND / "app/application/business/catalog.py",
        support.BACKEND / "app/domain/business/models.py",
        support.BACKEND / "evaluation/contracts.py",
        support.BACKEND / "evaluation/dataset.py",
        support.BACKEND / "evaluation/retrieval.py",
        support.BACKEND / "evaluation/retrieval_metrics.py",
        support.BACKEND / "evaluation/retrieval_rescore.py",
        support.BACKEND / "evaluation/retrieval_support.py",
        *sorted((support.BACKEND / "dataset/evaluation/schemas").glob("*.schema.json")),
        support.BACKEND / "uv.lock",
    ]
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=support.REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=support.REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    files = {
        path.relative_to(support.BACKEND).as_posix(): support.sha256(path)
        for path in paths
    }
    versions = {
        name: version(name)
        for name in ("jsonlines", "jsonschema", "mteb", "pydantic")
    }
    lockfile_sha256 = support.sha256(support.BACKEND / "uv.lock")
    digest = support.sha256_text(
        support.json_text(
            {
                "head": head,
                "files": files,
                "versions": versions,
                "lockfile_sha256": lockfile_sha256,
            }
        )
    )
    return {
        "head": head,
        "worktree_dirty": bool(status.strip()),
        "files": files,
        "versions": versions,
        "lockfile_sha256": lockfile_sha256,
        "digest": digest,
    }


def _query_business(query: dict) -> dict:
    # 审阅记录与判断范围允许随补标更新；查询正文、用途和条件必须保持原值。
    return {
        key: value
        for key, value in query.items()
        if key not in ("review", "judgement_scope")
    }


def _qrel_business(qrel: dict) -> dict:
    return {key: value for key, value in qrel.items() if key != "review"}


def _scenario_digests(dataset: EvaluationDataset) -> dict[str, str]:
    return {
        scenario["scenario_id"]: scenario_digest(
            scenario, dataset.attachment_hashes(scenario)
        )
        for scenario in dataset.scenarios
    }


def _asset_declarations(dataset: EvaluationDataset) -> dict[str, tuple[str, str]]:
    return {
        row["asset_id"]: (row["path"], row["sha256"])
        for row in dataset.manifest["assets"]
    }


def _source_history_usage(document: dict) -> dict:
    requests = 0
    total_tokens = 0
    prompt_tokens = []
    evidence_count = 0
    empty_pools = {}
    for group, result in document["groups"].items():
        empty_pools[group] = sum(
            1
            for projection in result["projections"].values()
            if not projection["candidate_pool"]
        )
        for projection in result["projections"].values():
            rerank = projection.get("rerank")
            if not isinstance(rerank, dict):
                continue
            evidence_count += 1
            usage = rerank["usage"]
            requests += usage["requests"]
            total_tokens += usage["total_tokens"]
            prompt_tokens.append(usage.get("prompt_tokens"))
    reported_prompt = (
        None
        if not prompt_tokens or any(value is None for value in prompt_tokens)
        else sum(prompt_tokens)
    )
    return {
        "historical_only": True,
        "embedding_requests": document.get("embedding_requests"),
        "rerank_requests_recorded": requests,
        "rerank_total_tokens_recorded": total_tokens,
        "rerank_prompt_tokens_recorded": reported_prompt,
        "rerank_projection_count": evidence_count,
        "empty_pools_by_group": empty_pools,
        "source_started_at": document.get("started_at"),
        "source_finished_at": document.get("finished_at"),
    }


def _validate_source(source_path: Path) -> tuple[dict, EvaluationDataset, str]:
    source_sha256 = support.sha256(source_path)
    document = support.load_document(source_path)
    require(
        document.get("schema") == SCHEMA_STAGED,
        "rescore-staged 只接受两阶段冻结证据",
    )
    require(isinstance(document.get("groups"), dict) and document["groups"], "缺少检索组")
    require(
        isinstance(document.get("query_ids"), list) and document["query_ids"],
        "缺少查询列表",
    )
    require(
        len(set(document["query_ids"])) == len(document["query_ids"]),
        "查询列表重复",
    )
    _validate_staged_evidence(document)
    original = bind_dataset(document)
    require(
        original.content_sha256 == document["dataset"]["content_sha256"],
        "原始数据内容摘要无法复现",
    )
    return document, original, source_sha256


def _validate_compatibility(
    source: dict,
    original: EvaluationDataset,
    scoring: EvaluationDataset,
) -> tuple[dict, list[dict]]:
    source_query_ids = list(source["query_ids"])
    scoring_ids = [query["query_id"] for query in scoring.queries]
    scoring_set = set(scoring_ids)
    require(
        len(scoring_ids) == len(scoring_set), "评分数据查询 ID 重复"
    )
    require(
        set(source_query_ids) <= scoring_set,
        "评分数据缺少冻结证据中的查询",
    )
    filtered_scoring_ids = [qid for qid in scoring_ids if qid in set(source_query_ids)]
    require(
        filtered_scoring_ids == source_query_ids,
        "评分数据查询顺序与冻结证据不一致",
    )

    original_queries = {query["query_id"]: query for query in original.queries}
    scoring_queries = {query["query_id"]: query for query in scoring.queries}
    for query_id in source_query_ids:
        require(
            _query_business(original_queries[query_id])
            == _query_business(scoring_queries[query_id]),
            f"评分数据查询正文、用途或条件漂移: {query_id}",
        )

    require(
        original.corpus_sha256 == scoring.corpus_sha256,
        "评分数据语料快照漂移",
    )
    require(
        original.manifest["source"] == scoring.manifest["source"],
        "评分数据动作事实或编码语义来源漂移",
    )
    require(
        _scenario_digests(original) == _scenario_digests(scoring),
        "评分数据场景内容或引用附件漂移",
    )
    require(
        _asset_declarations(original) == _asset_declarations(scoring),
        "评分数据附件集合漂移",
    )

    original_qrels = {
        (qrel["query_id"], qrel["corpus_id"]): qrel for qrel in original.qrels
    }
    scoring_qrels = {
        (qrel["query_id"], qrel["corpus_id"]): qrel for qrel in scoring.qrels
    }
    require(
        len(original_qrels) == len(original.qrels), "原始金标组合重复"
    )
    require(
        len(scoring_qrels) == len(scoring.qrels), "评分数据金标组合重复"
    )
    for key, original_qrel in original_qrels.items():
        current = scoring_qrels.get(key)
        require(current is not None, f"评分数据删除了已有金标: {key[0]}/{key[1]}")
        require(
            _qrel_business(current) == _qrel_business(original_qrel),
            f"评分数据修改了已有金标: {key[0]}/{key[1]}",
        )
    added_keys = sorted(set(scoring_qrels) - set(original_qrels))
    added_qrels = [scoring_qrels[key] for key in added_keys]
    for qrel in added_qrels:
        review = qrel["review"]
        require(review["status"] == "approved", "新增金标未批准")
        require(review["reviewer_type"] == "user", "新增金标审阅者不是用户")
        require(review["reviewer"] and review["decision_ref"], "新增金标缺少用户决定引用")
        require(
            review["content_sha256"] == qrel_digest(qrel),
            "新增金标批准摘要与内容不一致",
        )

    added_queries = {qrel["query_id"] for qrel in added_qrels}
    for query_id in added_queries:
        related = [
            qrel for qrel in scoring.qrels if qrel["query_id"] == query_id
        ]
        require(
            scoring_queries[query_id]["review"]["content_sha256"]
            == query_digest(scoring_queries[query_id], related),
            f"补标后查询审阅摘要未更新: {query_id}",
        )

    parameters = source.get("parameters")
    require(isinstance(parameters, dict), "冻结证据缺少检索参数")
    require(
        parameters.get("candidate_pool_limit") == CANDIDATE_POOL_LIMIT,
        "冻结候选池预算与评分预算不一致",
    )
    require(
        parameters.get("final_input_limit") == SEARCH_LIMIT,
        "冻结最终输入预算与评分预算不一致",
    )
    threshold = parameters.get("threshold")
    require(
        threshold is None
        or (type(threshold) in (int, float) and math.isfinite(threshold)),
        "冻结门槛非法",
    )

    source_pool_pairs = {
        (query_id, corpus_id)
        for group in source["groups"].values()
        for query_id, projection in group["projections"].items()
        for corpus_id in projection["candidate_pool"]
    }
    added_in_pools = sum(1 for key in added_keys if key in source_pool_pairs)
    added_outside_pools = len(added_keys) - added_in_pools
    verification = {
        "source_evidence_valid": True,
        "source_dataset_identity_verified": True,
        "scoring_dataset_approved": True,
        "query_ids_compatible": True,
        "query_business_fields_equal": True,
        "corpus_identity_equal": True,
        "source_facts_and_encoding_equal": True,
        "scenarios_equal": True,
        "assets_equal": True,
        "existing_labels_unchanged": True,
        "added_labels_approved": True,
        "added_label_count": len(added_keys),
        "added_label_grades": dict(
            sorted(Counter(qrel["grade"] for qrel in added_qrels).items())
        ),
        "queries_with_added_labels": sorted(added_queries),
        "added_labels_in_source_pools": added_in_pools,
        "added_labels_outside_source_pools": added_outside_pools,
        "budget_limits_compatible": True,
        "thresholds_compatible": True,
        "same_pool_evidence_valid": True,
    }
    return verification, added_qrels


def _eligible_queries(
    query_ids: list[str],
    projections: dict,
    scoring: EvaluationDataset,
) -> tuple[list[str], dict[str, dict]]:
    reviewed = scoring.reviewed_corpus_ids()
    eligible = []
    excluded = {}
    for query_id in query_ids:
        projection = projections[query_id]
        missing = [
            corpus_id
            for corpus_id in projection["candidate_pool"]
            if corpus_id not in reviewed.get(query_id, ())
        ]
        if missing:
            excluded[query_id] = {
                "reason": "unreviewed_candidates",
                "missing_candidates": missing,
            }
        else:
            eligible.append(query_id)
    return eligible, excluded


def _score_subset(
    scoring: EvaluationDataset,
    projections: dict,
    eligible_ids: list[str],
    catalog: Catalog,
    mappings: dict,
    threshold: float | None,
) -> dict | None:
    if not eligible_ids:
        return None
    selected_set = set(eligible_ids)
    scoring_map = {item["id"]: item for item in scoring.scoring_queries()}
    selected = [scoring_map[query_id] for query_id in eligible_ids]
    evaluated_by_query = {
        query["query_id"]: support.dataset_conditions(query, catalog, mappings)
        for query in scoring.queries
        if query["query_id"] in selected_set
    }
    subset = {query_id: projections[query_id] for query_id in eligible_ids}
    return validate_projections(
        selected, subset, catalog, evaluated_by_query, threshold
    )


def _group_diagnostic(
    source: dict,
    scoring: EvaluationDataset,
    group: str,
    projections: dict,
    query_ids: list[str],
    catalog: Catalog,
    mappings: dict,
) -> dict:
    eligible, excluded = _eligible_queries(query_ids, projections, scoring)
    threshold = _group_threshold(source, group)
    metrics = _score_subset(
        scoring, projections, eligible, catalog, mappings, threshold
    )
    scoring_map = {item["query_id"]: item for item in scoring.queries}
    with_labels = sum(
        1 for query_id in eligible if not scoring_map[query_id]["expect_empty"]
    )
    empty_queries = len(eligible) - with_labels
    if metrics is not None:
        overall = metrics["overall"]
        require(overall["queries"] == len(eligible), f"{group} 诊断查询数不一致")
        require(
            overall["with_labels"] == with_labels,
            f"{group} 有答案分母不一致",
        )
        require(
            overall["empty_expectation_queries"] == empty_queries,
            f"{group} 无答案分母不一致",
        )
    unknown = {
        query_id: row["missing_candidates"]
        for query_id, row in excluded.items()
    }
    expected = len(query_ids)
    return {
        "group": group,
        "source_status": source["groups"][group]["status"],
        "status": "diagnostic_available" if metrics is not None else "diagnostic_unavailable",
        "scope": "full_source_queries" if len(eligible) == expected else "partial_eligible",
        "expected_query_ids": list(query_ids),
        "expected_count": expected,
        "eligible_query_ids": eligible,
        "eligible_count": len(eligible),
        "eligible_coverage": len(eligible) / expected,
        "excluded_query_ids": sorted(excluded),
        "excluded_count": len(excluded),
        "exclusion_reasons": excluded,
        "unreviewed_candidates": unknown,
        "unreviewed_candidate_count": sum(len(ids) for ids in unknown.values()),
        "metric_denominators": {
            "ranking_with_labels": with_labels,
            "expect_empty_false_return": empty_queries,
        },
        "fully_reviewed": len(eligible) == expected,
        "metrics": metrics,
    }


def _common_intersection(
    source: dict,
    scoring: EvaluationDataset,
    groups: dict[str, dict],
    projections_by_group: dict[str, dict],
    query_ids: list[str],
    catalog: Catalog,
    mappings: dict,
) -> dict:
    common = [
        query_id
        for query_id in query_ids
        if all(query_id in set(row["eligible_query_ids"]) for row in groups.values())
    ]
    common_set = set(common)
    scoring_map = {item["query_id"]: item for item in scoring.queries}
    result = {}
    for group, row in groups.items():
        metrics = _score_subset(
            scoring,
            projections_by_group[group],
            common,
            catalog,
            mappings,
            _group_threshold(source, group),
        )
        with_labels = sum(
            1 for query_id in common if not scoring_map[query_id]["expect_empty"]
        )
        if metrics is not None:
            require(
                metrics["overall"]["with_labels"] == with_labels,
                f"{group} 共同交集有答案分母不一致",
            )
        result[group] = {
            "status": "diagnostic_available" if metrics is not None else "diagnostic_unavailable",
            "metrics": metrics,
            "metric_denominators": {
                "ranking_with_labels": with_labels,
                "expect_empty_false_return": len(common) - with_labels,
            },
        }
    expected = len(query_ids)
    return {
        "query_ids": common,
        "count": len(common),
        "expected_count": expected,
        "coverage_of_expected": len(common) / expected,
        "eligible_query_ids_by_group": {
            group: sorted(common_set & set(row["eligible_query_ids"]))
            for group, row in groups.items()
        },
        "groups": result,
    }


def _same_pool_comparison(
    source: dict,
    scoring: EvaluationDataset,
    groups: dict[str, dict],
    projections_by_group: dict[str, dict],
    query_ids: list[str],
    catalog: Catalog,
    mappings: dict,
) -> dict:
    if RERANK_GROUP not in groups or "hybrid" not in groups:
        return {
            "status": "not_applicable",
            "reason": "冻结证据缺少 hybrid 与 hybrid-rerank 同池对照",
        }
    hybrid_eligible = set(groups["hybrid"]["eligible_query_ids"])
    rerank_eligible = set(groups[RERANK_GROUP]["eligible_query_ids"])
    pair_ids = [
        query_id
        for query_id in query_ids
        if query_id in hybrid_eligible and query_id in rerank_eligible
    ]
    baseline_projections = projections_by_group["hybrid"]
    rerank_projections = projections_by_group[RERANK_GROUP]
    for query_id in pair_ids:
        baseline = baseline_projections[query_id]
        reranked = rerank_projections[query_id]
        require(
            baseline["candidate_pool"] == reranked["candidate_pool"],
            f"同池诊断候选 ID 或顺序不一致: {query_id}",
        )
        require(
            baseline["scores"] == reranked["scores"],
            f"同池诊断 RRF 分数不一致: {query_id}",
        )
    baseline_metrics = _score_subset(
        scoring,
        baseline_projections,
        pair_ids,
        catalog,
        mappings,
        _group_threshold(source, "hybrid"),
    )
    rerank_metrics = _score_subset(
        scoring,
        rerank_projections,
        pair_ids,
        catalog,
        mappings,
        _group_threshold(source, RERANK_GROUP),
    )
    if baseline_metrics is None or rerank_metrics is None:
        return {
            "status": "no_eligible_queries",
            "query_ids": pair_ids,
            "count": len(pair_ids),
            "baseline": None,
            "reranked": None,
        }
    baseline_rows = baseline_metrics["per_query"]
    rerank_rows = rerank_metrics["per_query"]
    positive_ids = [
        query_id
        for query_id in pair_ids
        if not baseline_rows[query_id]["expect_empty"]
    ]
    recall_checks = {}
    for query_id in positive_ids:
        baseline_recall = next(
            metric["value"]
            for metric in baseline_rows[query_id]["metrics"]
            if metric["name"] == "recall@20"
        )
        rerank_recall = next(
            metric["value"]
            for metric in rerank_rows[query_id]["metrics"]
            if metric["name"] == "recall@20"
        )
        require(
            baseline_recall == rerank_recall,
            f"同池诊断 Recall@20 不一致: {query_id}",
        )
        recall_checks[query_id] = baseline_recall
    reordered = [
        query_id
        for query_id in pair_ids
        if baseline_projections[query_id]["final_model_input"]
        != rerank_projections[query_id]["final_model_input"]
    ]
    deltas = {}
    for name in STAGED_METRICS:
        baseline_value = baseline_metrics["overall"][name]
        rerank_value = rerank_metrics["overall"][name]
        deltas[name] = (
            None
            if baseline_value is None or rerank_value is None
            else rerank_value - baseline_value
        )
    return {
        "status": "diagnostic_available",
        "scope": "same_frozen_candidate_pool_same_labelled_subset",
        "query_ids": pair_ids,
        "count": len(pair_ids),
        "positive_query_count": len(positive_ids),
        "recall_at_20_equal": True,
        "recall_at_20_by_query": recall_checks,
        "final_input_reordered_query_count": len(reordered),
        "final_input_reordered_query_ids": reordered,
        "baseline": {
            "metrics": baseline_metrics,
            "mean": baseline_metrics["overall"],
        },
        "reranked": {
            "metrics": rerank_metrics,
            "mean": rerank_metrics["overall"],
        },
        "observed_metric_deltas": deltas,
        "note": "同一冻结候选池与同一已审阅子集上的观测差值；不代表新的真实检索或模型提升。",
    }


def _catalog_index() -> tuple[Catalog, dict[str, int], dict, str]:
    catalog = Catalog.load()
    actions_path = support.DATASET / "actions.agent.json"
    actions = support.load_document(actions_path)
    index = {row["id"]: position for position, row in enumerate(actions)}
    supplements_path = support.DATASET / "action-supplements.published.json"
    supplements = support.load_document(supplements_path)
    supplement_index = {
        row["id"]: (position, row)
        for position, row in enumerate(supplements["entries"])
    }
    return catalog, index, supplement_index, support.sha256(actions_path)


def _supplement_fact(
    corpus_id: str,
    supplement_index: dict,
    supplements_path: Path,
    supplements_sha256: str,
) -> dict | None:
    entry = supplement_index.get(corpus_id)
    if entry is None:
        return None
    position, row = entry
    english = row.get("english_name")
    aliases = row.get("aliases") or []
    return {
        "path": supplements_path.relative_to(support.REPO).as_posix(),
        "json_pointer": f"/entries/{position}",
        "sha256": supplements_sha256,
        "english_name": english.get("text") if isinstance(english, dict) else None,
        "aliases": [
            {"text": alias.get("text"), "lang": alias.get("lang")}
            for alias in aliases
            if isinstance(alias, dict)
        ],
    }


def _review_draft(
    source: dict,
    scoring: EvaluationDataset,
    projections_by_group: dict[str, dict],
    query_ids: list[str],
    source_sha256: str,
) -> tuple[list[dict], dict, str]:
    catalog, catalog_indices, supplement_index, catalog_sha256 = _catalog_index()
    supplements_path = support.DATASET / "action-supplements.published.json"
    supplements_sha256 = support.sha256(supplements_path)
    scoring_map = {query["query_id"]: query for query in scoring.queries}
    unknown_by_group = {
        group: scoring.unreviewed_candidates(
            {
                query_id: projection["candidate_pool"]
                for query_id, projection in projections.items()
            }
        )
        for group, projections in projections_by_group.items()
    }
    source_unknown_by_group = {
        group: result.get("unknown_candidates") or {}
        for group, result in source["groups"].items()
    }
    records = {}
    for query_id in query_ids:
        for group, projections in projections_by_group.items():
            projection = projections[query_id]
            missing = set(unknown_by_group[group].get(query_id, ()))
            for rank, corpus_id in enumerate(projection["candidate_pool"]):
                if corpus_id not in missing:
                    continue
                key = (query_id, corpus_id)
                record = records.setdefault(key, {"appearances": {}})
                rerank_score = None
                rerank = projection.get("rerank")
                if isinstance(rerank, dict):
                    rerank_score = next(
                        (
                            row["rerank_score"]
                            for row in rerank["scored"]
                            if row["id"] == corpus_id
                        ),
                        None,
                    )
                record["appearances"][group] = {
                    "candidate_pool_rank": rank + 1,
                    "final_input_rank": (
                        projection["final_model_input"].index(corpus_id) + 1
                        if corpus_id in projection["final_model_input"]
                        else None
                    ),
                    "rrf_score": projection["scores"].get(corpus_id),
                    "rerank_score": rerank_score,
                    "experiment": projection.get("experiment"),
                }

    rows = []
    mappings = rt.load_mappings()
    evaluated_by_query = {}
    for (query_id, corpus_id), record in records.items():
        query = scoring_map[query_id]
        exercise = catalog.get(corpus_id)
        require(exercise is not None, f"候选不在动作目录: {corpus_id}")
        if query_id not in evaluated_by_query:
            evaluated_by_query[query_id] = support.dataset_conditions(
                query, catalog, mappings
            )
        evaluated = evaluated_by_query[query_id]
        condition_match = evaluated.conditions.matches(exercise)
        supplement = _supplement_fact(
            corpus_id,
            supplement_index,
            supplements_path,
            supplements_sha256,
        )
        facts = [
            f"query={query['query']}",
            f"name={exercise.name}",
            f"equipment={exercise.equipment}",
            f"body_part={exercise.body_part}",
            f"target={exercise.target}",
            f"muscle_group={exercise.muscle_group}",
        ]
        rows.append(
            {
                "schema": SCHEMA_REVIEW_DRAFT,
                "draft_id": str(
                    uuid5(
                        NAMESPACE_URL,
                        f"{source['run_id']}:{query_id}:{corpus_id}",
                    )
                ),
                "dedup_key": f"{query_id}/{corpus_id}",
                "query": {
                    "query_id": query_id,
                    "text": query["query"],
                    "purpose": query["purpose"],
                    "conditions": query["conditions"],
                    "expect_empty": query["expect_empty"],
                    "tags": list(query["tags"]),
                    "family": query["family"],
                },
                "exercise": {
                    "corpus_id": corpus_id,
                    "name": exercise.name,
                    "equipment": exercise.equipment,
                    "body_part": exercise.body_part,
                    "target": exercise.target,
                    "muscle_group": exercise.muscle_group,
                    "secondary_muscles": list(exercise.secondary_muscles),
                    "load_convention": exercise.model_dump(mode="json")[
                        "load_convention"
                    ],
                    "steps_zh": list(exercise.steps.zh),
                    "catalog_ref": {
                        "path": "backend/dataset/actions.agent.json",
                        "json_pointer": f"/{catalog_indices[corpus_id]}",
                        "sha256": catalog_sha256,
                    },
                    "supplement_ref": supplement,
                },
                "appearances": record["appearances"],
                "retrieval_evidence_note": "排名、RRF 分数与重排分数仅作检索证据，不转换为金标等级。",
                "suggested_grade": None,
                "suggestion_basis": {
                    "query_intent": {
                        "purpose": query["purpose"],
                        "text": query["query"],
                        "expect_empty": query["expect_empty"],
                    },
                    "conditions": query["conditions"],
                    "condition_match": condition_match,
                    "candidate_facts": facts,
                    "grade_rule": GRADE_RULES,
                    "reason": "未获得足以确定 0-3 等级的确定性依据；等待用户审阅。",
                },
                "review_status": "pending_user_review",
                "review": {
                    "reviewer": None,
                    "reviewer_type": None,
                    "approved_at": None,
                    "decision_ref": None,
                    "content_sha256": None,
                },
            }
        )

    by_group = {
        group: sum(1 for row in rows if group in row["appearances"])
        for group in projections_by_group
    }
    presence = Counter(len(row["appearances"]) for row in rows)
    grade_counts = Counter(
        "null" if row["suggested_grade"] is None else str(row["suggested_grade"])
        for row in rows
    )
    review_counts = Counter(row["review_status"] for row in rows)
    draft_text = _jsonl_text(rows)
    summary = {
        "schema": SCHEMA_REVIEW_DRAFT,
        "source_run_id": source["run_id"],
        "source_sha256": source_sha256,
        "path": "review-draft.jsonl",
        "total_unique_pairs": len(rows),
        "queries_affected": len({row["query"]["query_id"] for row in rows}),
        "source_unknown_rows_by_group": {
            group: sum(len(ids) for ids in unknown.values())
            for group, unknown in source_unknown_by_group.items()
        },
        "remaining_unknown_rows_by_group": {
            group: sum(len(ids) for ids in unknown.values())
            for group, unknown in unknown_by_group.items()
        },
        "unique_pairs_by_group": by_group,
        "presence_counts": {
            str(count): presence[count] for count in sorted(presence)
        },
        "suggested_grade_counts": {
            key: grade_counts[key]
            for key in ("null", "0", "1", "2", "3")
        },
        "review_status_counts": dict(review_counts),
        "draft_sha256": support.sha256_text(draft_text),
        "rules": {
            "grades": GRADE_RULES,
            "retrieval_scores_are_not_labels": True,
            "unreviewed_is_not_zero": True,
            "no_model_or_judge_calls": True,
            "requires_user_approval": True,
        },
    }
    return rows, summary, draft_text


def _draft_markdown(summary: dict) -> str:
    lines = [
        "# 检索补标草案汇总",
        "",
        f"- Schema：`{summary['schema']}`",
        f"- 来源运行：`{summary['source_run_id']}`",
        f"- 去重后待审组合：{summary['total_unique_pairs']} 条",
        f"- 涉及查询：{summary['queries_affected']} 条",
        f"- 草案 SHA-256：`{summary['draft_sha256']}`",
        "",
        "## 逐组来源",
        "",
        "| group | 候选行 | 去重组合 |",
        "|---|---:|---:|",
    ]
    for group, rows in summary["remaining_unknown_rows_by_group"].items():
        lines.append(
            f"| {group} | {rows} | {summary['unique_pairs_by_group'][group]} |"
        )
    lines.extend(
        [
            "",
            "## 跨组出现次数",
            "",
            "| 出现组数 | 组合数 |",
            "|---|---:|",
        ]
    )
    for count, total in summary["presence_counts"].items():
        lines.append(f"| {count} | {total} |")
    lines.extend(
        [
            "",
            "## 建议等级",
            "",
            "| suggested_grade | 数量 |",
            "|---|---:|",
        ]
    )
    for grade, total in summary["suggested_grade_counts"].items():
        lines.append(f"| {grade} | {total} |")
    lines.extend(
        [
            "",
            "全部草案保持 `pending_user_review`；未推断等级、未写入 qrels、未参与正式算分。",
            "0-3 等级规则与逐条事实引用见 `review-draft.jsonl`。",
            "",
            "## 逐查询待审清单",
            "",
            "| query_id | 待审组合 | 出现组 |",
            "|---|---:|---|",
        ]
    )
    per_query = {}
    for row in summary["_rows"]:
        query_id = row["query"]["query_id"]
        per_query.setdefault(query_id, []).append(row)
    for query_id, rows in per_query.items():
        groups = sorted({group for row in rows for group in row["appearances"]})
        lines.append(f"| {query_id} | {len(rows)} | {', '.join(groups)} |")
    return "\n".join(lines) + "\n"


def _followup_report(rescore: dict, draft_summary: dict) -> str:
    def value(number) -> str:
        return "未测" if number is None else f"{number:.6f}"

    lines = [
        f"# 冻结检索输出离线复评 {rescore['rescore_id']}",
        "",
        "## 身份与边界",
        "",
        f"- 复评 Schema：`{rescore['schema']}`；类型：`{rescore['kind']}`。",
        f"- 原始运行：`{rescore['source']['run_id']}`；原始状态 `{rescore['source']['status']}`；原始 SHA-256 `{rescore['source']['sha256']}`。",
        f"- 原始数据：`{rescore['source']['dataset']['version']}`；content `{rescore['source']['dataset']['content_sha256']}`。",
        f"- 评分数据：`{rescore['scoring_dataset']['version']}`；content `{rescore['scoring_dataset']['content_sha256']}`。",
        f"- 复评实现：HEAD `{rescore['implementation']['head']}`；文件摘要 `{rescore['implementation']['digest']}`；MTEB `{rescore['implementation']['versions']['mteb']}`。",
        f"- 复评时间：`{rescore['created_at']}`。",
        "- 本文件只使用冻结检索输出与批准数据版本离线算分；未调用 Embedding、重排、生成模型、裁判或 LangSmith，未访问 Milvus。",
        "",
        "## 数据兼容核验",
        "",
        "| 检查 | 结果 |",
        "|---|---|",
    ]
    for key, item in rescore["verification"].items():
        if isinstance(item, bool):
            lines.append(f"| {key} | {'通过' if item else '失败'} |")
    lines.extend(
        [
            f"| added_label_count | {rescore['verification']['added_label_count']} |",
            f"| added_labels_in_source_pools | {rescore['verification']['added_labels_in_source_pools']} |",
            f"| added_labels_outside_source_pools | {rescore['verification']['added_labels_outside_source_pools']} |",
            "",
            "## 逐组诊断",
            "",
            "| group | 原始状态 | eligible / 预期 | 覆盖率 | 有答案 / 无答案 | Recall@20 | Hit@5 | MRR@5 | nDCG@5 | Precision@5 | 无答案误返回率 |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for group, row in rescore["groups"].items():
        metrics = row["metrics"]
        overall = metrics["overall"] if metrics is not None else None
        counts = (
            f"{overall['with_labels']} / {overall['empty_expectation_queries']}"
            if overall is not None
            else f"{row['metric_denominators']['ranking_with_labels']} / {row['metric_denominators']['expect_empty_false_return']}"
        )
        values = [
            value(overall[name]) if overall is not None else "未测"
            for name in STAGED_METRICS
        ]
        false_return = (
            value(overall["false_return_rate"])
            if overall is not None
            else "未测"
        )
        lines.append(
            f"| {group} | {row['source_status']} | {row['eligible_count']} / {row['expected_count']} | "
            f"{row['eligible_coverage']:.2%} | {counts} | "
            + " | ".join(values)
            + f" | {false_return} |"
        )
    lines.extend(
        [
            "",
            "排除查询与逐条缺标候选记录在 `rescore.json` 的 `groups.<group>.excluded_query_ids` 与 `unreviewed_candidates`。",
            "",
            "## 共同已审阅交集",
            "",
            f"- 交集查询数：{rescore['common_intersection']['count']} / {rescore['expected_query_count']}；覆盖率 {rescore['common_intersection']['coverage_of_expected']:.2%}。",
            f"- 查询集合：`{', '.join(rescore['common_intersection']['query_ids'])}`。",
            "",
            "| group | 查询数 | 有答案 / 无答案 | Recall@20 | Hit@5 | MRR@5 | nDCG@5 | Precision@5 | 无答案误返回率 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for group, row in rescore["common_intersection"]["groups"].items():
        metrics = row["metrics"]
        overall = metrics["overall"] if metrics is not None else None
        counts = (
            f"{overall['with_labels']} / {overall['empty_expectation_queries']}"
            if overall is not None
            else f"{row['metric_denominators']['ranking_with_labels']} / {row['metric_denominators']['expect_empty_false_return']}"
        )
        values = [
            value(overall[name]) if overall is not None else "未测"
            for name in STAGED_METRICS
        ]
        false_return = (
            value(overall["false_return_rate"])
            if overall is not None
            else "未测"
        )
        lines.append(
            f"| {group} | {rescore['common_intersection']['count']} | {counts} | "
            + " | ".join(values)
            + f" | {false_return} |"
        )
    comparison = rescore["same_pool_comparison"]
    lines.extend(["", "## 同池 baseline 与 rerank", ""])
    if comparison["status"] != "diagnostic_available":
        lines.append(f"- 状态：`{comparison['status']}`。")
    else:
        lines.extend(
            [
                f"- 同一 eligible 集合查询数：{comparison['count']}；有答案查询 {comparison['positive_query_count']} 条；Recall@20 逐查询相等。",
                f"- 最终输入发生换序的查询：{comparison['final_input_reordered_query_count']} 条。",
                "",
                "| 指标 | baseline | rerank | 观测差值 |",
                "|---|---:|---:|---:|",
            ]
        )
        for name in STAGED_METRICS:
            lines.append(
                f"| {name} | {value(comparison['baseline']['mean'][name])} | "
                f"{value(comparison['reranked']['mean'][name])} | "
                f"{value(comparison['observed_metric_deltas'][name])} |"
            )
        lines.append("")
        lines.append(comparison["note"])
    lines.extend(
        [
            "",
            "## 正式全量状态",
            "",
            f"- 状态：`{rescore['formal_metrics']['status']}`。",
            f"- 原因：{rescore['formal_metrics']['reason']}",
            f"- 上传/发布入口支持：`{rescore['formal_metrics']['upload_schema_supported']}`；该 Schema 不进入现有上传与发布门禁。",
            "",
            "## 补标草案",
            "",
            f"- 去重待审组合：{draft_summary['total_unique_pairs']} 条，涉及 {draft_summary['queries_affected']} 条查询。",
            f"- 状态：全部 `pending_user_review`；建议等级为空 {draft_summary['suggested_grade_counts']['null']} 条。",
            f"- 草案 SHA-256：`{draft_summary['draft_sha256']}`。",
            "- 既有 qrels 未修改；草案不参与正式算分。",
            "",
            "## 新增请求",
            "",
            "- Embedding 0；重排 0；生成模型 0；裁判 0；LangSmith 0；Milvus 0；网络请求 0。",
            f"- 来源历史用量（只读记录）：重排请求 {rescore['source_history_usage']['rerank_requests_recorded']} 次，总 tokens {rescore['source_history_usage']['rerank_total_tokens_recorded']}。",
            "",
            "## 输出",
            "",
            "- `rescore.json`：完整诊断、排除原因、缺标集合、共同交集与同池对照。",
            "- `review-draft.jsonl`：逐条待审草案，带目录事实引用与检索证据。",
            "- `review-summary.json`、`review-summary.md`：草案汇总与逐查询清单。",
            f"- 修复与测试证据：`{TEST_REPORT_REFERENCE}`。",
        ]
    )
    return "\n".join(lines) + "\n"


def _rescore_id(
    source: dict,
    source_sha256: str,
    scoring: EvaluationDataset,
    implementation: dict,
    inputs_sha256: dict,
) -> str:
    identity = {
        "source_run_id": source["run_id"],
        "source_sha256": source_sha256,
        "source_dataset": source["dataset"]["content_sha256"],
        "scoring_dataset": _dataset_digest(scoring),
        "implementation": implementation["digest"],
        "inputs_sha256": inputs_sha256,
    }
    return "rescore-" + hashlib.sha256(
        support.json_text(identity).encode("utf-8")
    ).hexdigest()[:20]


def _resolve_followup_root(
    source_path: Path, output: Path | None
) -> tuple[Path, Path]:
    allowed_root = (source_path.parent / FOLLOWUP_DIR).resolve()
    if output is None:
        return allowed_root, allowed_root
    requested = Path(output).resolve()
    require(
        requested == allowed_root or allowed_root in requested.parents,
        "输出目录必须位于源运行目录的 followup/ 下",
    )
    return allowed_root, requested


def _reusable_created_at(path: Path, rescore_id: str, inputs_sha256: dict) -> str | None:
    if not path.is_file():
        return None
    existing = support.load_document(path)
    if (
        existing.get("rescore_id") == rescore_id
        and existing.get("inputs_sha256") == inputs_sha256
    ):
        return existing.get("created_at")
    return None


def _append_source_report(
    source_path: Path, section: str, rescore_id: str
) -> Path:
    report_path = source_path.parent / "report.md"
    original_bytes = report_path.read_bytes() if report_path.is_file() else b""
    original = original_bytes.decode("utf-8")
    newline = "\r\n" if "\r\n" in original else "\n"
    current = section.replace("\n", newline)
    begin = f"<!-- {MARKER_PREFIX}:begin {rescore_id} -->"
    end = f"<!-- {MARKER_PREFIX}:end {rescore_id} -->"
    begin_count = original.count(begin)
    end_count = original.count(end)
    require(begin_count == end_count, "复评追加区段标记损坏")
    require(begin_count <= 1, "复评追加区段重复")
    if begin_count == 1:
        begin_index = original.index(begin)
        end_index = original.index(end, begin_index)
        suffix_start = end_index + len(end)
        if original.startswith(newline, suffix_start):
            suffix_start += len(newline)
        updated = original[:begin_index] + current + original[suffix_start:]
    else:
        separator = ""
        if original:
            separator = newline if original.endswith(newline) else newline + newline
        updated = original + separator + current
    _atomic_write_bytes(report_path, updated.encode("utf-8"))
    return report_path


def _report_section(
    rescore: dict, draft_summary: dict, rescore_sha256: str
) -> str:
    begin = f"<!-- {MARKER_PREFIX}:begin {rescore['rescore_id']} -->"
    end = f"<!-- {MARKER_PREFIX}:end {rescore['rescore_id']} -->"
    lines = [
        begin,
        f"## 离线金标复评 `{rescore['rescore_id']}`",
        "",
        f"- 原始采集：`{rescore['source']['run_id']}`；状态 `{rescore['source']['status']}`，原始 `evaluation.json` 保持原值，未重新检索。",
        f"- 原始数据身份：`{rescore['source']['dataset']['version']}` / `{rescore['source']['dataset']['content_sha256']}`。",
        f"- 评分数据身份：`{rescore['scoring_dataset']['version']}` / `{rescore['scoring_dataset']['content_sha256']}`。",
        f"- 复评实现身份：HEAD `{rescore['implementation']['head']}`；digest `{rescore['implementation']['digest']}`。",
        f"- 复评 JSON SHA-256：`{rescore_sha256}`。",
        f"- 诊断范围：预期 {rescore['expected_query_count']} 条；"
        + "；".join(
            f"{group} {row['eligible_count']}/{row['expected_count']}"
            for group, row in rescore["groups"].items()
        )
        + f"；共同交集 {rescore['common_intersection']['count']} 条。",
        f"- 正式全量状态：`{rescore['formal_metrics']['status']}`；原因：{rescore['formal_metrics']['reason']}",
        f"- 补标待批准：{draft_summary['total_unique_pairs']} 条去重组合，涉及 {draft_summary['queries_affected']} 条查询；全部 `pending_user_review`。",
        "- 本次新增模型/检索/云端请求：0（Embedding、重排、生成、裁判、LangSmith、Milvus、网络均为 0）。",
        f"- 修复与测试证据：`{TEST_REPORT_REFERENCE}`。",
        "",
        "| group | eligible / 预期 | 有答案 / 无答案 | Recall@20 | Hit@5 | MRR@5 | nDCG@5 | Precision@5 | 无答案误返回率 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for group, row in rescore["groups"].items():
        metrics = row["metrics"]
        overall = metrics["overall"] if metrics is not None else None
        counts = (
            f"{overall['with_labels']} / {overall['empty_expectation_queries']}"
            if overall is not None
            else f"{row['metric_denominators']['ranking_with_labels']} / {row['metric_denominators']['expect_empty_false_return']}"
        )
        values = [
            "未测" if overall is None or overall[name] is None else f"{overall[name]:.6f}"
            for name in STAGED_METRICS
        ]
        false_return = (
            "未测"
            if overall is None or overall["false_return_rate"] is None
            else f"{overall['false_return_rate']:.6f}"
        )
        lines.append(
            f"| {group} | {row['eligible_count']} / {row['expected_count']} | {counts} | "
            + " | ".join(values)
            + f" | {false_return} |"
        )
    lines.extend(
        [
            "",
            f"- 复评输出：[rescore.json]({FOLLOWUP_DIR}/{rescore['rescore_id']}/rescore.json) · "
            f"[report.md]({FOLLOWUP_DIR}/{rescore['rescore_id']}/report.md) · "
            f"[review-draft.jsonl]({FOLLOWUP_DIR}/{rescore['rescore_id']}/review-draft.jsonl) · "
            f"[review-summary.md]({FOLLOWUP_DIR}/{rescore['rescore_id']}/review-summary.md)",
            "- 用户批准兼容补标数据版本后，使用同一 `rescore-staged` 入口复评；新的完整结果以独立区段追加到本报告。",
            end,
        ]
    )
    return "\n".join(lines) + "\n"


def rescore_staged(args) -> None:
    source_path = Path(args.source)
    scoring_path = Path(args.dataset)
    source_sha256_before = support.sha256(source_path)
    source, original, source_sha256 = _validate_source(source_path)
    require(
        source_sha256 == source_sha256_before,
        "读取期间原始证据发生变化",
    )
    _, requested_root = _resolve_followup_root(source_path, args.output)
    scoring = load_for_evaluation(scoring_path)
    verification, _ = _validate_compatibility(source, original, scoring)

    catalog = Catalog.load()
    mappings = rt.load_mappings()
    query_ids = list(source["query_ids"])
    projections_by_group = {
        group: result["projections"] for group, result in source["groups"].items()
    }
    groups = {
        group: _group_diagnostic(
            source,
            scoring,
            group,
            projections,
            query_ids,
            catalog,
            mappings,
        )
        for group, projections in projections_by_group.items()
    }
    common = _common_intersection(
        source,
        scoring,
        groups,
        projections_by_group,
        query_ids,
        catalog,
        mappings,
    )
    same_pool = _same_pool_comparison(
        source,
        scoring,
        groups,
        projections_by_group,
        query_ids,
        catalog,
        mappings,
    )
    fully_reviewed = all(row["fully_reviewed"] for row in groups.values())
    formal_metrics = {
        "status": (
            "complete_reviewed_full_coverage"
            if fully_reviewed
            else "blocked_unreviewed_candidates"
        ),
        "reason": (
            "全部冻结查询的候选池均已获得批准标签；结果仍为离线复评，原采集保持原状态。"
            if fully_reviewed
            else "仍存在未审阅候选；正式全量 metrics 保持 null，仅输出已审阅子集诊断。"
        ),
        "groups": {
            group: {
                "fully_reviewed": row["fully_reviewed"],
                "evaluated_count": row["eligible_count"],
                "expected_count": row["expected_count"],
                "metrics": row["metrics"] if row["fully_reviewed"] else None,
            }
            for group, row in groups.items()
        },
        "publishable": False,
        "upload_schema_supported": False,
    }

    draft_rows, draft_summary, draft_text = _review_draft(
        source, scoring, projections_by_group, query_ids, source_sha256
    )
    draft_summary["_rows"] = draft_rows
    draft_markdown = _draft_markdown(draft_summary)
    draft_summary.pop("_rows")

    implementation = _implementation_identity()
    inputs_sha256 = {
        "source": source_sha256,
        "source_dataset": _dataset_digest(original),
        "scoring_dataset": _dataset_digest(scoring),
        "implementation": implementation["digest"],
        "parameters": support.sha256_text(support.json_text(source["parameters"])),
    }
    rescore_id = _rescore_id(
        source, source_sha256, scoring, implementation, inputs_sha256
    )
    followup_dir = (
        requested_root
        if requested_root.name == rescore_id
        else requested_root / rescore_id
    )
    existing_created_at = _reusable_created_at(
        followup_dir / "rescore.json", rescore_id, inputs_sha256
    )
    created_at = existing_created_at or now()
    rescore = {
        "schema": SCHEMA_RESCORE,
        "rescore_id": rescore_id,
        "kind": "offline_label_rescore",
        "collection_scope": "frozen_retrieval_outputs_only",
        "created_at": created_at,
        "expected_query_count": len(query_ids),
        "query_ids": query_ids,
        "source": {
            "path": source_path.as_posix(),
            "sha256": source_sha256,
            "run_id": source["run_id"],
            "mode": source.get("mode"),
            "status": source.get("status"),
            "started_at": source.get("started_at"),
            "finished_at": source.get("finished_at"),
            "identity": source.get("identity"),
            "dataset": source.get("dataset"),
            "parameters": source.get("parameters"),
            "experiments": source.get("experiments"),
            "index_identity": source.get("index_identity"),
            "rerank_identity": source.get("rerank_identity"),
            "vector_cache_identity": source.get("vector_cache_identity"),
            "embedding_requests": source.get("embedding_requests"),
            "query_ids": list(source["query_ids"]),
        },
        "scoring_dataset": _dataset_digest(scoring),
        "implementation": implementation,
        "inputs_sha256": inputs_sha256,
        "parameters": source["parameters"],
        "verification": verification,
        "groups": groups,
        "common_intersection": common,
        "same_pool_comparison": same_pool,
        "formal_metrics": formal_metrics,
        "source_history_usage": _source_history_usage(source),
        "new_requests": NEW_REQUESTS,
        "review_draft": draft_summary,
        "notes": [
            "对原始冻结检索输出使用批准数据版本离线重评分。",
            "未执行新的检索、Embedding、重排、生成模型、裁判或 LangSmith 调用。",
            "原始 evaluation.json 保持原状态与原摘要；正式 blocked 语义不变。",
            "本 Schema 不属于实时采集格式，不进入现有上传与发布入口。",
        ],
    }
    report_path = followup_dir / "report.md"
    draft_path = followup_dir / "review-draft.jsonl"
    summary_path = followup_dir / "review-summary.json"
    summary_markdown_path = followup_dir / "review-summary.md"
    rescore_path = followup_dir / "rescore.json"
    report_text = _followup_report(rescore, draft_summary)
    summary_text = _json_text(draft_summary)
    rescore["outputs"] = {
        "report.md": {
            "path": "report.md",
            "sha256": support.sha256_text(report_text),
        },
        "review-draft.jsonl": {
            "path": "review-draft.jsonl",
            "sha256": support.sha256_text(draft_text),
        },
        "review-summary.json": {
            "path": "review-summary.json",
            "sha256": support.sha256_text(summary_text),
        },
        "review-summary.md": {
            "path": "review-summary.md",
            "sha256": support.sha256_text(draft_markdown),
        },
    }
    rescore_text = _json_text(rescore)
    section = _report_section(
        rescore, draft_summary, support.sha256_text(rescore_text)
    )

    require(
        support.sha256(source_path) == source_sha256,
        "写入前原始证据发生变化",
    )
    _atomic_write_text(draft_path, draft_text)
    _atomic_write_text(summary_path, summary_text)
    _atomic_write_text(summary_markdown_path, draft_markdown)
    _atomic_write_text(report_path, report_text)
    _atomic_write_text(rescore_path, rescore_text)
    require(
        support.sha256(source_path) == source_sha256,
        "写入复评输出期间原始证据发生变化",
    )
    source_report = _append_source_report(source_path, section, rescore_id)
    require(
        support.sha256(source_path) == source_sha256,
        "追加复评索引期间原始证据发生变化",
    )
    print(
        json.dumps(
            {
                "rescore_id": rescore_id,
                "rescore": str(rescore_path),
                "report": str(report_path),
                "draft": str(draft_path),
                "source_report": str(source_report),
                "source_sha256": source_sha256,
                "source_sha256_unchanged": True,
                "formal_status": formal_metrics["status"],
                "groups": {
                    group: {
                        "eligible": row["eligible_count"],
                        "expected": row["expected_count"],
                        "coverage": row["eligible_coverage"],
                    }
                    for group, row in groups.items()
                },
                "common_intersection": common["count"],
                "draft_total": draft_summary["total_unique_pairs"],
                "new_requests": NEW_REQUESTS,
            },
            ensure_ascii=False,
        )
    )
