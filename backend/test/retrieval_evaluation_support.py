import hashlib
import importlib
import json
import math
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator
from openai import __version__ as openai_version
from pymilvus import __version__ as pymilvus_version

from app.application.business import retrieval_text as rt
from app.application.business.catalog import DEFAULT_ACTIONS_PATH, Catalog
from app.application.business.retrieval_conditions import (
    EvaluatedConditions,
    evaluate_search_conditions,
)
from app.application.business.retrieval_config import (
    COMPATIBLE_MODE_PATH,
    WORKSPACE_DOMAIN,
)
from app.application.business.retrieval_embedding import EmbeddingClient
from app.application.business.retrieval_index import (
    MilvusConnection,
    RetrievalCorpus,
    json_text,
    sha256,
)
from app.application.business.retrieval_response import (
    SEARCH_BUDGET,
    SEARCH_LIMIT,
    build_search_result,
)
from app.application.business.retrieval_search import (
    HybridRetriever,
    RetrievalCandidate,
    SearchConditions,
    search_vectors,
)
from app.domain.business.models import CatalogSearchArguments, ProfileContent
from test.check_retrieval_baseline import (
    aggregate,
    est_tokens,
    per_query_record,
    percentile,
)

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
DATASET = BACKEND / "dataset"
QUERIES_PATH = DATASET / "action-retrieval-queries.json"
QUERIES_SCHEMA_PATH = DATASET / "action-retrieval-queries.schema.json"
REVIEWS_PATH = DATASET / "action-supplement-reviews.json"
ADAPTATION_PATH = DATASET / "action-retrieval-purpose-adaptation.json"
EVAL_ROOT = REPO / "tmp" / "action-retrieval-t8"
CACHE_ROOT = EVAL_ROOT / "query-vectors"
FROZEN_PATH = EVAL_ROOT / "frozen-parameters.json"
HOLDOUT_LEDGER_PATH = EVAL_ROOT / "holdout-executions.json"
# 复验证据独立目录：授权与登记只写这里，旧冻结原件与旧留出账本一律保持原样。
REQUAL_ROOT = REPO / "tmp" / "action-retrieval-t8-requalification"
REQUAL_AUTH_PATH = REQUAL_ROOT / "authorization.json"
REQUAL_LEDGER_PATH = REQUAL_ROOT / "executions.json"
REQUAL_SCOPE = "operator-authorized-requalification"
REQUAL_REQUEST_CAPS = {"embedding": 0, "chat": 0, "summary": 0}
# 父证据：历史正式留出运行与其完成记录，复验授权逐项绑定其路径与 SHA-256。
PARENT_HOLDOUT_RUN_ID = "20261015T060247Z-d561b76b"
PARENT_HOLDOUT_DIR = EVAL_ROOT / PARENT_HOLDOUT_RUN_ID
# 冻结参数接线文件：声明式运行配置，不计入 run_identity 源码快照，改由冻结账本显式绑定取值与哈希。
FROZEN_CONFIG_RELATIVE = "app/application/business/retrieval_frozen.py"
FROZEN_CONFIG_PATH = BACKEND / FROZEN_CONFIG_RELATIVE

ONLINE_PURPOSES = ("lookup", "generation", "replacement")
APPROVED_INPUTS = {
    "queries": 128,
    "labels": 405,
    "dev": 61,
    "holdout": 67,
    "families": 22,
    "catalog": 1324,
}
# 开发集网格：权重端点量化稀疏与 dense 贡献，深度对称取有限值，门槛在已召回列表上事后过滤。
ALPHA_CANDIDATES = (0.0, 0.25, 0.5, 0.7, 0.85, 1.0)
DEPTH_CANDIDATES = (20, 50, 100)
THRESHOLD_CANDIDATES = tuple(round(step * 0.05, 2) for step in range(20))
# 线上 service 向 runtime 传入 SEARCH_LIMIT + 1 个召回候选；评测按同一召回口径复现。
RECALL_LIMIT = SEARCH_LIMIT + 1
SCORE_STAGES = ("recalled", "after_threshold", "returned")


class EvaluationBlocked(RuntimeError):
    # 缺少批准输入、缓存身份不兼容或授权不足时停止对应执行，不降级为可用结果。

    def __init__(self, reason: str, missing: tuple[str, ...] = ()) -> None:
        super().__init__(reason)
        self.reason = reason
        self.missing = missing


class EvidenceIdentityChanged(RuntimeError):
    pass


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def iso_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.strip()


def snapshot_paths() -> tuple[str, ...]:
    business = "app/application/business"
    return (
        f"{business}/catalog.py",
        f"{business}/retrieval_text.py",
        f"{business}/retrieval_config.py",
        f"{business}/retrieval_embedding.py",
        f"{business}/retrieval_index.py",
        f"{business}/retrieval_search.py",
        f"{business}/retrieval_runtime.py",
        f"{business}/retrieval_conditions.py",
        f"{business}/retrieval_response.py",
        f"{business}/service.py",
        "app/agent/tool.py",
        "app/agent/tools/exercises.py",
        "app/agent/tools/business.py",
        "app/agent/prompts.py",
        "app/domain/business/models.py",
        "dataset/actions.agent.json",
        "dataset/action-supplements.published.json",
        "dataset/action-supplements.candidates.json",
        "dataset/action-field-mappings.json",
        "dataset/action-supplement-reviews.json",
        "dataset/action-retrieval-queries.json",
        "dataset/action-retrieval-purpose-adaptation.json",
        "test/check_retrieval_baseline.py",
        "test/retrieval_evaluation_support.py",
        "test/check_retrieval_evaluation.py",
    )


def snapshot_file_hashes(paths: tuple[str, ...]) -> dict[str, str]:
    # 必需文件缺失时直接失败，不允许以 missing 通过身份核验。
    files = {}
    missing = []
    for relative in paths:
        path = BACKEND / relative
        if not path.is_file():
            missing.append(relative)
            continue
        files[relative] = sha256(path)
    if missing:
        raise EvaluationBlocked("运行身份要求的文件缺失", tuple(missing))
    return files


def run_identity() -> dict:
    # 同一 HEAD 不足以证明代码快照一致：逐个记录相关源码/数据/配置的 SHA-256 与工作区状态。
    files = snapshot_file_hashes(snapshot_paths())
    changed = git("status", "--porcelain").splitlines()
    return {
        "captured_at": iso_utc(),
        "head": git("rev-parse", "HEAD"),
        "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(changed),
        "changed_paths": changed,
        "files": files,
        "versions": {"pymilvus": pymilvus_version, "openai": openai_version},
    }


def stable_identity(identity: dict) -> dict:
    # 冻结/留出比对只取稳定字段：HEAD、逐文件 SHA-256 与依赖版本；不比较时间与工作区脏清单。
    return {
        "head": identity["head"],
        "files": identity["files"],
        "versions": identity["versions"],
    }


@dataclass
class RunContext:
    run_id: str
    directory: Path
    mode: str
    started_at_ms: int
    identity: dict
    finished_stages: list

    @classmethod
    def start(cls, mode: str, argv: list[str], env_names: list[str]) -> "RunContext":
        run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + hashlib.sha256(
            json_text(argv).encode("utf-8")
        ).hexdigest()[:8]
        directory = EVAL_ROOT / run_id
        directory.mkdir(parents=True, exist_ok=True)
        context = cls(run_id, directory, mode, now_ms(), run_identity(), [])
        context.write("command", {
            "mode": mode,
            "argv": argv,
            "cwd": str(BACKEND),
            "env_variable_names_present": [
                name for name in env_names if name in os.environ
            ],
            "env_variable_names_required": env_names,
            "secret_values_recorded": False,
            "started_at": iso_utc(),
            "identity": context.identity,
        })
        return context

    def write(self, name: str, payload: dict) -> Path:
        path = self.directory / f"{name}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def stage(self, name: str, payload: dict) -> None:
        self.write(name, payload)
        self.finished_stages.append(name)
        self.write("run-summary", {
            "run_id": self.run_id,
            "mode": self.mode,
            "directory": str(self.directory),
            "stages": self.finished_stages,
            "head": self.identity["head"],
        })

    def finish(self) -> None:
        current = run_identity()
        drift = {
            key: {"before": self.identity[key], "after": current[key]}
            for key in ("head", "changed_paths", "files", "versions")
            if current[key] != self.identity[key]
        }
        self.write("run-complete", {
            "finished_at": iso_utc(),
            "elapsed_seconds": round((now_ms() - self.started_at_ms) / 1000, 3),
            "stages": self.finished_stages,
            "identity_unchanged": not drift,
            "identity_drift": drift,
        })
        if drift:
            raise EvidenceIdentityChanged("运行期间相关源码或数据快照发生变化，比较结果不可用")

    def fingerprint(self) -> dict:
        return {
            "head": self.identity["head"],
            "file_sha256": self.identity["files"],
            "dirty": self.identity["dirty"],
        }


def load_document(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def verify_inputs(catalog: Catalog) -> dict:
    # 批准输入校验：Schema、账本内容哈希绑定、目录哈希与数量、拆分与家族不跨集、标注引用合法。
    schema = load_document(QUERIES_SCHEMA_PATH)
    document = load_document(QUERIES_PATH)
    Draft202012Validator(schema).validate(document)
    queries = document["queries"]
    ledger = load_document(REVIEWS_PATH)
    decision = next(
        item for item in ledger["decisions"]
        if item["scope"] == "backend/dataset/action-retrieval-queries.json"
    )
    queries_sha256 = sha256(QUERIES_PATH)
    checks = {
        "queries_sha256": queries_sha256,
        "ledger_conclusion": decision["conclusion"],
        "ledger_hash_bound": decision["content_sha256"] == queries_sha256,
        "catalog_hash_bound": document["catalog"]["sha256"] == sha256(DEFAULT_ACTIONS_PATH),
        "catalog_count": len(catalog) == document["catalog"]["count"] == APPROVED_INPUTS["catalog"],
        "label_status": {item["label_status"] for item in queries} == {"approved"},
        "ledger_label_status": ledger["status"] == "approved",
    }
    counts = {
        "queries": len(queries),
        "labels": sum(len(item["labels"]) for item in queries),
        "dev": sum(1 for item in queries if item["split"] == "dev"),
        "holdout": sum(1 for item in queries if item["split"] == "holdout"),
        "families": len({item["family"] for item in queries}),
        "catalog": len(catalog),
        "expect_empty": sum(1 for item in queries if item["expect_empty"]),
        "empty_without_labels": sum(
            1 for item in queries if item["expect_empty"] and not item["labels"]
        ),
    }
    for key, expected in APPROVED_INPUTS.items():
        checks[f"count_{key}"] = key in counts and counts[key] == expected
    family_splits: dict[str, set[str]] = {}
    for item in queries:
        family_splits.setdefault(item["family"], set()).add(item["split"])
    checks["family_not_crossing_splits"] = all(
        len(splits) == 1 for splits in family_splits.values()
    )
    ids = {item["id"] for item in queries}
    checks["unique_query_ids"] = len(ids) == len(queries)
    checks["labels_reference_catalog"] = all(
        label["id"] in catalog.exercise_ids for item in queries for label in item["labels"]
    )
    checks["label_ids_unique_per_query"] = all(
        len({label["id"] for label in item["labels"]}) == len(item["labels"]) for item in queries
    )
    checks["saved_references_catalog"] = all(
        exercise_id in catalog.exercise_ids
        for item in queries
        for exercise_id in item["saved_profile_restrictions"]["forbidden_exercise_ids"]
    )
    checks["purposes"] = sorted({item["purpose"] for item in queries})
    checks["passed"] = all(value for key, value in checks.items() if key != "purposes")
    return {"checks": checks, "counts": counts, "intents": sorted({item["intent"] for item in queries})}


def load_adaptation() -> tuple[dict, dict[str, str]]:
    # 适配表与金标原件分离；未获主会话确认时质量执行停止，不自行推断用途。
    if not ADAPTATION_PATH.is_file():
        raise EvaluationBlocked("缺少用途适配表", (str(ADAPTATION_PATH),))
    document = load_document(ADAPTATION_PATH)
    if document["status"] != "approved":
        raise EvaluationBlocked(
            "用途适配表状态为 " + document["status"] + "，未获主会话确认，质量执行停止",
            ("主会话确认 purpose 适配表",),
        )
    if document["queries_sha256"] != sha256(QUERIES_PATH):
        raise EvaluationBlocked("用途适配表绑定的查询文件哈希与当前金标不一致", ("适配表重新绑定",))
    rows = {row["id"]: row for row in document["rows"]}
    document_ids = set(rows)
    queries = load_document(QUERIES_PATH)["queries"]
    expected = {item["id"] for item in queries}
    if document_ids != expected:
        raise EvaluationBlocked("用途适配表未覆盖全部金标查询", ("补齐适配行",))
    invalid = sorted(
        row_id for row_id, row in rows.items() if row["purpose"] not in ONLINE_PURPOSES
    )
    if invalid:
        raise EvaluationBlocked("用途适配表包含非线上枚举用途", tuple(invalid))
    return document, {row_id: row["purpose"] for row_id, row in rows.items()}


def deferred_query_ids(adaptation: dict) -> tuple[str, ...]:
    # 主会话决定暂缓的查询：质量执行中单列为未执行，其余查询继续按批准映射执行。
    return tuple(adaptation.get("deferred_query_ids", ()))


def temporary_mapping(adaptation: dict, query_id: str) -> dict:
    rows = {row["id"]: row for row in adaptation["rows"]}
    return rows[query_id].get("temporary_mapping", {})


def effective_conditions(
    query: dict,
    catalog: Catalog,
    mappings: dict,
    purpose: str,
    temporary: dict,
) -> EvaluatedConditions:
    # 已批准条件求值：显式筛选 + 环境/声明/临时/画像限制，按线上 evaluate_search_conditions 同一实现。
    arguments = CatalogSearchArguments(
        purpose=purpose,
        name=query["query"],
        **{field: query["filters"][field] for field in rt.MAPPING_FIELDS},
        task_environment=query["environment"],
        available_equipment=query["declared_equipment"] or None,
        unavailable_equipment=temporary.get("unavailable_equipment") or None,
        excluded_ids=temporary.get("excluded_ids") or None,
    )
    profile = ProfileContent(
        goal=None,
        experience=None,
        environment=None,
        availability=None,
        health_notes=None,
        movement_restrictions=None,
        unavailable_equipment=query["saved_profile_restrictions"]["unavailable_equipment"] or None,
        forbidden_exercise_ids=query["saved_profile_restrictions"]["forbidden_exercise_ids"] or None,
    )
    return evaluate_search_conditions(arguments, profile, catalog, mappings)


def explicit_filter_conditions(query: dict) -> EvaluatedConditions:
    # 主对照口径：与金标 baseline_rule 一致，只应用四个显式精确筛选，不含画像与环境推导。
    conditions = SearchConditions(**{field: query["filters"][field] for field in rt.MAPPING_FIELDS})
    return EvaluatedConditions(
        conditions, tuple(field for field in rt.MAPPING_FIELDS if query["filters"][field]), ()
    )


def to_candidates(catalog: Catalog, exercises) -> list[RetrievalCandidate]:
    # 名称基线无相关度分数；score 仅作为统一投影的占位，门槛阶段对其标记为不适用。
    return [
        RetrievalCandidate(
            id=exercise.id,
            score=0.0,
            name=exercise.name,
            **{field: getattr(exercise, field) for field in rt.MAPPING_FIELDS},
        )
        for exercise in exercises
    ]


def name_candidates(catalog: Catalog, query: dict, evaluated: EvaluatedConditions) -> list[RetrievalCandidate]:
    hits = catalog.search(
        query["query"],
        **{field: query["filters"][field] for field in rt.MAPPING_FIELDS},
    )
    return to_candidates(catalog, [hit for hit in hits if evaluated.conditions.matches(hit)])


async def vector_candidates(
    db: MilvusConnection,
    collection: str,
    catalog: Catalog,
    query: dict,
    vector: list[float],
    evaluated: EvaluatedConditions,
    *,
    mode: str,
    alpha: float,
    bm25_depth: int,
    dense_depth: int,
    limit: int = RECALL_LIMIT,
) -> list[RetrievalCandidate]:
    return await search_vectors(
        db,
        collection,
        catalog,
        query["query"],
        # 稀疏路径不使用向量；空序列明确表达该参数未参与本次请求，不构造伪向量。
        vector if mode in ("dense", "hybrid") else [],
        conditions=evaluated.conditions,
        alpha=alpha,
        bm25_depth=bm25_depth,
        dense_depth=dense_depth,
        limit=limit,
        mode=mode,
    )


def apply_threshold(
    candidates: list[RetrievalCandidate], threshold: float | None
) -> list[RetrievalCandidate]:
    # 门槛只作用于融合分数（norm_score=True 的加权融合结果）；单路原始分数量纲不同，不套用。
    if threshold is None:
        return list(candidates)
    if any(not math.isfinite(candidate.score) for candidate in candidates):
        raise ValueError("候选分数非有限值")
    return [candidate for candidate in candidates if candidate.score >= threshold]


def project_response(
    catalog: Catalog,
    recalled: list[RetrievalCandidate],
    evaluated: EvaluatedConditions,
    *,
    threshold: float | None,
    scored: bool,
) -> dict:
    # 门槛后、结构化裁剪前保持完整候选；裁剪只按完整候选，预算口径为最终序列化 JSON。
    after_threshold = apply_threshold(recalled, threshold)
    result = build_search_result(after_threshold, evaluated)
    payload = json.dumps(result.model_dump(), ensure_ascii=False, allow_nan=False)
    returned = [item.id for item in result.items]
    recalled_ids = [candidate.id for candidate in recalled]
    violations = [
        candidate.id
        for candidate in after_threshold
        if not evaluated.conditions.matches(catalog.get(candidate.id))
    ]
    return {
        "threshold_applied": threshold is not None and scored,
        "scores": {candidate.id: candidate.score for candidate in recalled},
        "recalled": recalled_ids,
        "after_threshold": [candidate.id for candidate in after_threshold],
        "returned": returned,
        "has_more": result.has_more,
        "remaining_ids": result.remaining_ids,
        "dropped_by_threshold": [
            candidate.id for candidate in recalled if candidate not in after_threshold
        ],
        "dropped_by_budget": [
            candidate.id
            for candidate in after_threshold
            if candidate.id not in returned
        ],
        "hard_constraint_violations": violations,
        "serialized_chars": len(payload),
        "serialized_est_tokens": est_tokens(len(payload)),
        "serialized_valid_json": isinstance(json.loads(payload), dict),
        "budget_chars": SEARCH_BUDGET,
        "budget_compliant": len(payload) <= SEARCH_BUDGET,
    }


def metrics(queries: list[dict], projections: dict[str, dict]) -> dict:
    # 三个阶段各自的宏平均：召回原序、门槛后、预算裁剪后；expect_empty 独立于 intent 汇总。
    stage_metrics = {}
    per_query = {}
    for stage in SCORE_STAGES:
        rows = [
            per_query_record(item, projections[item["id"]][stage]) for item in queries
        ]
        per_query[stage] = {row["id"]: row for row in rows}
        stage_metrics[stage] = {
            "overall": aggregate(rows),
            "by_intent": {
                intent: aggregate([row for row in rows if row["intent"] == intent])
                for intent in sorted({row["intent"] for row in rows})
            },
            "by_split": {
                split: aggregate([row for row in rows if row["split"] == split])
                for split in ("dev", "holdout")
            },
            "by_family": {
                family: aggregate([row for row in rows if row["family"] == family])
                for family in sorted({row["family"] for row in rows})
            },
        }
    chars = [projections[item["id"]]["serialized_chars"] for item in queries]
    named = {"standard_name", "alias", "multi_alias"}
    return {
        "stage_metrics": stage_metrics,
        "per_query": per_query,
        "projection": {
            "returned_chars_p50": round(percentile(chars, 0.5), 1),
            "returned_chars_p95": round(percentile(chars, 0.95), 1),
            "returned_chars_max": max(chars) if chars else 0,
            "empty_queries": sum(1 for item in queries if not projections[item["id"]]["returned"]),
            "hard_constraint_violation_total": sum(
                len(projections[item["id"]]["hard_constraint_violations"]) for item in queries
            ),
            "budget_noncompliant": [
                item["id"] for item in queries if not projections[item["id"]]["budget_compliant"]
            ],
        },
        "named_query_ids": sorted(item["id"] for item in queries if item["intent"] in named),
    }


def compare_groups(reference: dict, candidate: dict, queries: list[dict]) -> dict:
    # 逐例差异：新增命中、漏召回、排名退化、无答案误返回，全部带实际 ID 与名次。
    rows = {}
    summary = {
        "new_hits": 0,
        "lost_hits": 0,
        "rank_regressions": 0,
        "rank_improvements": 0,
        "false_returns": 0,
    }
    for item in queries:
        before = reference["per_query"]["returned"][item["id"]]
        after = candidate["per_query"]["returned"][item["id"]]
        hit_new = sorted(
            exercise_id
            for exercise_id in set(after["top10"]) - set(before["top10"])
            if exercise_id in after["relevant"]
        )
        hit_lost = sorted(
            exercise_id
            for exercise_id in set(before["top10"]) - set(after["top10"])
            if exercise_id in before["relevant"]
        )
        first_before = first_relevant_rank(before)
        first_after = first_relevant_rank(after)
        rank_change = (
            None if first_before is None or first_after is None
            else "regression" if first_after > first_before
            else "improvement" if first_after < first_before
            else "unchanged"
        )
        rows[item["id"]] = {
            "intent": item["intent"],
            "split": item["split"],
            "family": item["family"],
            "new_hits": hit_new,
            "lost_hits": hit_lost,
            "first_relevant_reference": first_before,
            "first_relevant_candidate": first_after,
            "rank_change": rank_change,
            "false_return_reference": before["false_return"] if item["expect_empty"] else None,
            "false_return_candidate": after["false_return"] if item["expect_empty"] else None,
            "reference_top10": before["top10"],
            "candidate_top10": after["top10"],
        }
        summary["new_hits"] += len(hit_new)
        summary["lost_hits"] += len(hit_lost)
        summary["false_returns"] += int(
            item["expect_empty"] and not before["false_return"] and after["false_return"]
        )
        summary["rank_regressions"] += int(rank_change == "regression")
        summary["rank_improvements"] += int(rank_change == "improvement")
    return {"summary": summary, "per_query": rows}


def first_relevant_rank(row: dict) -> int | None:
    ranks = [
        row["top10"].index(exercise_id) + 1
        for exercise_id in row["relevant"]
        if exercise_id in row["top10"]
    ]
    return min(ranks) if ranks else None


def grid_cells() -> list[tuple[float, int]]:
    # 有限可复现网格：权重端点量化两路贡献，深度取对称有限值。
    return [
        (alpha, depth)
        for alpha in ALPHA_CANDIDATES
        for depth in DEPTH_CANDIDATES
    ]


def select_frozen(cells: list[dict]) -> dict:
    # 选择顺序在调参前固定：先按开发集质量指标选 (alpha, 深度)，再在该单元内按门槛规则选门槛。
    # 并列规则：Recall@10 → nDCG@10 → MRR@10 → 无答案误返回率 → 更小召回深度 → 更小 alpha。
    quality_ranked = sorted(
        cells,
        key=lambda cell: (
            -cell["baseline_metrics"]["recall@10"],
            -cell["baseline_metrics"]["ndcg@10"],
            -cell["baseline_metrics"]["mrr@10"],
            min(row["false_return_rate"] if row["false_return_rate"] is not None else 1.0
                for row in cell["thresholds"]),
            cell["depth"],
            cell["alpha"],
        ),
    )
    best = quality_ranked[0]
    # 阶段二：优先保留全部已标注相关候选，再在保留门槛中取无答案误返回率最低者，并列取最小门槛。
    eligible = [
        row for row in best["thresholds"] if row["relevant_candidates_dropped"] == 0
    ]
    if not eligible:
        return {
            "blocked": "选定单元内不存在不丢弃任何已标注相关候选的门槛",
            "best_cell": {"alpha": best["alpha"], "depth": best["depth"]},
        }
    chosen = min(
        eligible,
        key=lambda row: (
            row["false_return_rate"] if row["false_return_rate"] is not None else 1.0,
            row["threshold"],
        ),
    )
    return {
        "alpha": best["alpha"],
        "bm25_depth": best["bm25_depth"],
        "dense_depth": best["dense_depth"],
        "threshold": chosen["threshold"],
        "dev_metrics_at_threshold": chosen,
        "selection_rule": {
            "stage1_order": [
                "recall@10 desc", "ndcg@10 desc", "mrr@10 desc",
                "min false_return_rate asc", "depth asc", "alpha asc",
            ],
            "stage2_rule": "先筛选不丢弃任何已标注相关候选的门槛，再取其中无答案误返回率最低者，并列取最小门槛",
            "grid": {
                "alpha": list(ALPHA_CANDIDATES),
                "depth": list(DEPTH_CANDIDATES),
                "threshold": list(THRESHOLD_CANDIDATES),
            },
        },
        "runner_up": [
            {"alpha": cell["alpha"], "depth": cell["depth"],
             "baseline_metrics": cell["baseline_metrics"]}
            for cell in quality_ranked[1:4]
        ],
    }


def endpoint_fingerprint(base_url: str) -> str:
    # 端点不可逆指纹：以固定域前缀哈希完整 Base URL，区分真实服务配置且不回写 workspace 原文。
    return hashlib.sha256(("endpoint:" + base_url).encode("utf-8")).hexdigest()


def query_vector_identity(embedding_config, corpus: RetrievalCorpus) -> dict:
    # 缓存身份：模型、维度、编码版本、规范化规则、端点不可逆指纹、公开服务类别与已验证源快照哈希。
    # 不保存实际 hostname/workspace ID，也不输出可还原 workspace 的原始域名。
    return {
        "model": embedding_config.model,
        "dimensions": embedding_config.dimensions,
        "encoding_rule_version": rt.ENCODING_RULE_VERSION,
        "normalization": "NFKC+whitespace-collapse+casefold",
        "service_fingerprint": endpoint_fingerprint(embedding_config.base_url),
        "service_class": WORKSPACE_DOMAIN + COMPATIBLE_MODE_PATH,
        "sources": corpus.sources,
        "text_sha256": hashlib.sha256(json_text(corpus.records).encode("utf-8")).hexdigest(),
    }


def query_documents() -> list[dict]:
    return load_document(QUERIES_PATH)["queries"]


def query_texts() -> dict[str, str]:
    return {item["id"]: item["query"] for item in query_documents()}


def cache_path(identity: dict) -> Path:
    digest = hashlib.sha256(json_text(identity).encode("utf-8")).hexdigest()[:20]
    return CACHE_ROOT / f"query-vectors-{digest}.json"


def normalized_query(query_text: str) -> str:
    return rt.normalize(query_text)


def validate_cached_vector(vector: object, dimensions: int) -> list[float]:
    # 与 Embedding 客户端返回校验同规则：维度、数值类型、有限值与非零范数；用于缓存复读而非替代 SDK。
    if not isinstance(vector, (list, tuple)):
        raise EvaluationBlocked("缓存向量必须为序列")
    if len(vector) != dimensions:
        raise EvaluationBlocked(f"缓存向量维度 {len(vector)} 与期望 {dimensions} 不符")
    cleaned = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EvaluationBlocked("缓存向量元素必须是数值")
        number = float(value)
        if not math.isfinite(number):
            raise EvaluationBlocked("缓存向量元素必须为有限数值")
        cleaned.append(number)
    if math.sqrt(sum(value * value for value in cleaned)) == 0.0:
        raise EvaluationBlocked("缓存向量范数为零")
    return cleaned


def write_query_cache(identity: dict, entries: list[dict]) -> Path:
    # 同目录临时文件 + os.replace 原子替换：写入中断不会以半文件覆盖已有有效缓存。
    path = cache_path(identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"identity": identity, "entries": entries}, ensure_ascii=False, indent=1)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(payload, encoding="utf-8")
    os.replace(temp, path)
    return path


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def resolve_expression(
    query: dict,
    catalog: Catalog,
    mappings: dict,
    mode: str,
    purposes: dict,
    adaptation: dict,
) -> str:
    # 记录每组实际消费的过滤表达式，供四组同条件核验与逐例复核。
    if mode == "explicit-filters":
        return explicit_filter_conditions(query).conditions.expression(catalog)
    purpose = purposes[query["id"]]
    temporary = temporary_mapping(adaptation, query["id"])
    return effective_conditions(query, catalog, mappings, purpose, temporary).conditions.expression(catalog)


def read_cached_entries(identity: dict, query_texts: dict[str, str] | None = None) -> dict[str, dict]:
    # 身份不一致的缓存直接拒用；身份一致时逐条复核模型、规范化输入哈希与向量有效性。
    # 提供当前查询文本时，绑定条目 ID 与该 ID 的当前规范化文本，拒绝交换或错位 ID 的向量。
    path = cache_path(identity)
    if not path.is_file():
        return {}
    document = load_document(path)
    if document["identity"] != identity:
        raise EvaluationBlocked("查询向量缓存身份与当前配置不一致")
    entries = {}
    for entry in document["entries"]:
        entry_id = entry.get("id")
        if entry.get("model") != identity["model"]:
            raise EvaluationBlocked(f"缓存条目模型身份不符: {entry_id}")
        if entry.get("normalized_sha256") != sha256_text(normalized_query(entry["query"])):
            raise EvaluationBlocked(f"缓存条目规范化输入哈希不符: {entry_id}")
        if query_texts is not None and entry_id in query_texts and entry["query"] != query_texts[entry_id]:
            raise EvaluationBlocked(f"缓存条目文本与当前查询不符: {entry_id}")
        validate_cached_vector(entry.get("vector"), identity["dimensions"])
        entries[entry_id] = entry
    return entries


def read_query_cache(identity: dict, queries: list[dict]) -> tuple[dict[str, list[float]], dict[str, dict]]:
    # 质量执行要求全部请求查询都已有兼容真实向量，且逐条绑定当前查询 ID 与规范化文本；缺失即停止。
    texts = {item["id"]: item["query"] for item in queries}
    entries = read_cached_entries(identity, texts)
    missing = [query_id for query_id in texts if query_id not in entries]
    if missing:
        raise EvaluationBlocked("查询向量缓存未覆盖全部请求查询", tuple(missing))
    vectors = {
        query_id: validate_cached_vector(entries[query_id]["vector"], identity["dimensions"])
        for query_id in texts
    }
    return vectors, {query_id: entries[query_id] for query_id in texts}


def frozen_config_identity() -> dict:
    # 冻结参数接线身份：模块 SHA-256 与实际取值；与冻结账本比对以核验线上落点未漂移。
    module = importlib.import_module("app.application.business.retrieval_frozen")
    return {
        "path": FROZEN_CONFIG_RELATIVE,
        "sha256": sha256(FROZEN_CONFIG_PATH),
        "values": {
            "alpha": module.ALPHA,
            "bm25_depth": module.BM25_DEPTH,
            "dense_depth": module.DENSE_DEPTH,
            "threshold": module.THRESHOLD,
            "recall_limit": module.RECALL_LIMIT,
        },
    }


def validate_grid_cells(cells: list[dict]) -> None:
    if len(cells) != len(ALPHA_CANDIDATES) * len(DEPTH_CANDIDATES):
        raise EvaluationBlocked("网格单元数量不完整", (str(len(cells)),))
    for cell in cells:
        if len(cell.get("thresholds", [])) != len(THRESHOLD_CANDIDATES):
            raise EvaluationBlocked("网格单元门槛不完整", (f"{cell.get('alpha')}/{cell.get('depth')}",))
        if cell["baseline_metrics"].get("recall@10") is None:
            raise EvaluationBlocked("网格单元缺少有效基线指标", (f"{cell.get('alpha')}/{cell.get('depth')}",))


def validate_grid_source(grid_path: Path, current_fingerprint: dict) -> dict:
    # 冻结来源门禁：仅接受绑定已批准用途适配、dev 拆分与完整限网格的成功运行。
    if not grid_path.is_file():
        raise EvaluationBlocked("缺少开发集网格结果，先执行 grid 模式", (str(grid_path),))
    document = load_document(grid_path)
    if document.get("split") != "dev":
        raise EvaluationBlocked("网格来源不是开发集，拒绝冻结", ("split=dev",))
    if document.get("conditions_mode") != "adapted":
        raise EvaluationBlocked("诊断网格不能进入正式冻结", ("conditions_mode=adapted",))
    load_adaptation()
    if document.get("adaptation_sha256") != sha256(ADAPTATION_PATH):
        raise EvaluationBlocked("网格来源的用途适配哈希与当前批准不一致")
    run_dir = grid_path.parent
    summary_path, complete_path = run_dir / "run-summary.json", run_dir / "run-complete.json"
    for path in (summary_path, complete_path):
        if not path.is_file():
            raise EvaluationBlocked("网格运行记录缺失，拒绝冻结", (str(path),))
    summary, complete = load_document(summary_path), load_document(complete_path)
    if summary.get("mode") != "grid" or "grid" not in summary.get("stages", []):
        raise EvaluationBlocked("网格运行未成功完成，拒绝冻结")
    if complete.get("identity_unchanged") is not True:
        raise EvaluationBlocked("网格运行身份未保持稳定，拒绝冻结")
    if document.get("code_identity") != current_fingerprint:
        raise EvaluationBlocked("网格来源源码/数据快照与当前不一致，拒绝冻结")
    gates = document.get("gates")
    if not gates or gates.get("hard_constraint_violation_total") != 0 or gates.get("budget_noncompliant"):
        raise EvaluationBlocked("网格硬约束或预算检查不合规，拒绝冻结")
    validate_grid_cells(document.get("cells") or [])
    return document


def load_frozen() -> dict:
    if not FROZEN_PATH.is_file():
        raise EvaluationBlocked("尚未冻结参数，不能执行正式留出", (str(FROZEN_PATH),))
    return load_document(FROZEN_PATH)


def require_frozen_holdout(
    frozen: dict, current_fingerprint: dict, current_index_identity: dict, params: dict
) -> None:
    # 留出门禁：冻结身份、接线取值、批准输入与索引清单须与冻结账本一致，参数不得由命令行覆盖。
    if frozen.get("code_identity") != current_fingerprint:
        raise EvaluationBlocked("冻结配置与当前源码/数据快照不一致，拒绝留出")
    if frozen.get("frozen_config_identity", {}).get("sha256") != sha256(FROZEN_CONFIG_PATH):
        raise EvaluationBlocked("线上冻结参数接线与冻结账本不一致")
    if frozen.get("adaptation_sha256") != sha256(ADAPTATION_PATH):
        raise EvaluationBlocked("用途适配内容哈希与冻结账本不一致")
    load_adaptation()
    if not verify_inputs(Catalog.load())["checks"]["passed"]:
        raise EvaluationBlocked("批准输入核验未通过，拒绝留出")
    if frozen.get("index_identity") != current_index_identity:
        raise EvaluationBlocked("当前发布集合或清单指纹与冻结账本不一致，拒绝留出")
    if params["conditions"] != "adapted":
        raise EvaluationBlocked("正式留出必须使用已批准用途适配", ("--conditions adapted",))
    if (
        params["alpha"] != frozen["alpha"]
        or params["depth"] != frozen["bm25_depth"]
        or params["threshold"] != frozen["threshold"]
    ):
        raise EvaluationBlocked("正式留出参数不得通过命令行覆盖冻结值")


def holdout_ledger_key(frozen: dict) -> str:
    relevant = {
        key: frozen.get(key)
        for key in (
            "alpha", "bm25_depth", "dense_depth", "threshold", "queries_sha256",
            "adaptation_sha256", "code_identity", "frozen_config_identity", "index_identity",
        )
    }
    return sha256_text(json_text(relevant))


def begin_holdout_execution(key: str, run_id: str) -> None:
    # 执行前登记：同一冻结身份已记录即拒绝，防止按留出结果重复挑选参数。
    ledger = load_document(HOLDOUT_LEDGER_PATH) if HOLDOUT_LEDGER_PATH.is_file() else {"executions": []}
    if any(item["key"] == key for item in ledger["executions"]):
        raise EvaluationBlocked("该冻结身份的正式留出执行已记录，禁止重复挑选", (key,))
    ledger["executions"].append(
        {"key": key, "run_id": run_id, "started_at": iso_utc(), "status": "started"}
    )
    HOLDOUT_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    HOLDOUT_LEDGER_PATH.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")


def complete_holdout_execution(key: str, status: str) -> None:
    ledger = load_document(HOLDOUT_LEDGER_PATH)
    for item in ledger["executions"]:
        if item["key"] == key:
            item["status"] = status
            item["finished_at"] = iso_utc()
    HOLDOUT_LEDGER_PATH.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")


def load_requalification_authorization() -> dict:
    # 复验授权只认独立授权文件；缺失即停止，不允许用口头许可替代机器可读绑定。
    if not REQUAL_AUTH_PATH.is_file():
        raise EvaluationBlocked("缺少复验授权文件，先执行复验准备阶段", (str(REQUAL_AUTH_PATH),))
    return load_document(REQUAL_AUTH_PATH)


def requalification_parent_digests(vector_cache_path: Path) -> dict:
    # 父证据路径与内容哈希：冻结原件、历史留出结果与完成记录、旧留出账本、真实向量缓存文件。
    candidates = {
        "frozen_parameters": FROZEN_PATH,
        "holdout_compare": PARENT_HOLDOUT_DIR / "compare.json",
        "holdout_run_complete": PARENT_HOLDOUT_DIR / "run-complete.json",
        "legacy_holdout_ledger": HOLDOUT_LEDGER_PATH,
        "vector_cache": vector_cache_path,
    }
    missing = tuple(str(path) for path in candidates.values() if not path.is_file())
    if missing:
        raise EvaluationBlocked("复验父证据文件缺失", missing)
    return {name: {"path": str(path), "sha256": sha256(path)} for name, path in candidates.items()}


def historical_holdout_query_ids() -> list[str]:
    # 历史留出查询清单取自历史比较结果的 hybrid 逐查询投影键，逐 ID 固定复验范围。
    path = PARENT_HOLDOUT_DIR / "compare.json"
    if not path.is_file():
        raise EvaluationBlocked("缺少历史正式留出比较结果", (str(path),))
    document = load_document(path)
    projection = (document.get("groups") or {}).get("hybrid", {}).get("per_query_projection")
    if not isinstance(projection, dict) or not projection:
        raise EvaluationBlocked("历史留出结果缺少逐查询投影结构", (str(path),))
    return sorted(projection)


def require_requalification(
    auth: dict,
    frozen: dict,
    current_fingerprint: dict,
    current_index_identity: dict,
    params: dict,
    query_ids: list[str],
    vector_cache_identity: dict,
) -> dict:
    # 复验门禁：授权范围、父证据哈希、冻结取值与接线、批准输入、向量缓存、索引、源码身份、查询清单、
    # 命令行参数逐项核验；数据/用途适配/向量空间/索引兼容不接受“已获准重复留出”绕过。
    checks: dict[str, bool] = {}
    if auth.get("scope") != REQUAL_SCOPE:
        raise EvaluationBlocked("复验授权范围标识非法", (str(auth.get("scope")),))
    checks["scope"] = True
    if auth.get("request_caps") != REQUAL_REQUEST_CAPS:
        raise EvaluationBlocked("复验授权未把新增模型请求上限固定为 0", (json_text(auth.get("request_caps")),))
    checks["request_caps"] = True
    parent = auth.get("parent") or {}
    if parent.get("frozen_parameters", {}).get("sha256") != sha256(FROZEN_PATH):
        raise EvaluationBlocked("授权绑定的父冻结原件哈希与当前冻结文件不一致")
    if parent.get("legacy_holdout_ledger", {}).get("sha256") != sha256(HOLDOUT_LEDGER_PATH):
        raise EvaluationBlocked("授权登记的旧留出账本哈希与当前账本不一致")
    checks["parent_digests"] = True
    fixed = {"alpha": 0.5, "bm25_depth": 100, "dense_depth": 100, "threshold": 0.0}
    frozen_parameters = {key: frozen.get(key) for key in fixed}
    if frozen_parameters != fixed:
        raise EvaluationBlocked("冻结原件取值偏离已批准的固定参数", (json_text(frozen_parameters),))
    if auth.get("frozen_parameters") != frozen_parameters:
        raise EvaluationBlocked("授权登记的冻结参数与冻结原件不一致")
    checks["frozen_parameters"] = True
    wiring = frozen_config_identity()
    if frozen.get("frozen_config_identity", {}).get("sha256") != sha256(FROZEN_CONFIG_PATH):
        raise EvaluationBlocked("线上冻结参数接线文件与冻结账本不一致")
    if frozen.get("frozen_config_identity", {}).get("values") != wiring["values"]:
        raise EvaluationBlocked("线上冻结参数接线取值与 retrieval_frozen 实际值不一致")
    if auth.get("frozen_config_identity") != frozen.get("frozen_config_identity"):
        raise EvaluationBlocked("授权登记的冻结参数接线身份与冻结账本不一致")
    checks["frozen_config_identity"] = True
    if frozen.get("adaptation_sha256") != sha256(ADAPTATION_PATH):
        raise EvaluationBlocked("用途适配内容哈希与冻结账本不一致")
    if frozen.get("queries_sha256") != sha256(QUERIES_PATH):
        raise EvaluationBlocked("金标查询内容哈希与冻结账本不一致")
    approved = auth.get("approved_inputs") or {}
    if approved.get("queries_sha256") != sha256(QUERIES_PATH):
        raise EvaluationBlocked("授权登记的金标查询哈希与当前文件不一致")
    if approved.get("adaptation_sha256") != sha256(ADAPTATION_PATH):
        raise EvaluationBlocked("授权登记的用途适配哈希与当前文件不一致")
    load_adaptation()
    checks["approved_inputs"] = True
    cache_file = cache_path(vector_cache_identity)
    cache = auth.get("vector_cache") or {}
    if cache.get("identity") != vector_cache_identity:
        raise EvaluationBlocked("授权登记的向量缓存身份与当前配置不一致")
    if cache.get("path") != str(cache_file):
        raise EvaluationBlocked("授权登记的向量缓存路径与当前身份推导路径不一致", (str(cache_file),))
    if not cache_file.is_file():
        raise EvaluationBlocked("兼容的真实查询向量缓存缺失，复验不允许补编码", (str(cache_file),))
    if cache.get("sha256") != sha256(cache_file):
        raise EvaluationBlocked("查询向量缓存内容与授权登记不一致")
    if cache.get("holdout_covered") != len(query_ids):
        raise EvaluationBlocked("查询向量缓存未覆盖全部留出查询", (str(cache.get("holdout_covered")),))
    checks["vector_cache"] = True
    if auth.get("index_identity") != current_index_identity:
        raise EvaluationBlocked("当前发布集合或清单指纹与复验授权不一致")
    if current_index_identity != frozen.get("index_identity"):
        raise EvaluationBlocked("当前发布集合或清单指纹与冻结账本不一致")
    checks["index_identity"] = True
    if auth.get("code_identity") != current_fingerprint:
        raise EvaluationBlocked("复验授权登记的源码/数据快照与当前运行身份不一致")
    checks["code_identity"] = True
    historical_ids = historical_holdout_query_ids()
    if auth.get("query_ids") != query_ids or query_ids != historical_ids:
        raise EvaluationBlocked("复验查询 ID 清单与历史留出逐 ID 不一致")
    checks["query_ids"] = True
    if params.get("conditions") != "adapted":
        raise EvaluationBlocked("正式复验必须使用已批准用途适配", ("--conditions adapted",))
    if (
        params.get("alpha") != frozen.get("alpha")
        or params.get("depth") != frozen.get("bm25_depth")
        or params.get("threshold") != frozen.get("threshold")
    ):
        raise EvaluationBlocked("正式复验参数不得通过命令行覆盖冻结值")
    checks["parameters"] = True
    historical_files = (frozen.get("code_identity") or {}).get("file_sha256") or {}
    current_files = current_fingerprint.get("file_sha256") or {}
    identity_diff = {
        "changed": {
            path: {"historical": digest, "current": current_files[path]}
            for path, digest in sorted(historical_files.items())
            if path in current_files and current_files[path] != digest
        },
        "added": sorted(set(current_files) - set(historical_files)),
        "removed": sorted(set(historical_files) - set(current_files)),
        "head_equal": (frozen.get("code_identity") or {}).get("head") == current_fingerprint.get("head"),
    }
    return {"identity_diff": identity_diff, "checks": checks}


def requalification_ledger_key(auth: dict) -> str:
    relevant = {
        "scope": auth.get("scope"),
        "frozen_parameters": auth.get("frozen_parameters"),
        "queries_sha256": (auth.get("approved_inputs") or {}).get("queries_sha256"),
        "adaptation_sha256": (auth.get("approved_inputs") or {}).get("adaptation_sha256"),
        "code_identity": auth.get("code_identity"),
        "frozen_config_identity": auth.get("frozen_config_identity"),
        "index_identity": auth.get("index_identity"),
        "vector_cache": auth.get("vector_cache"),
        "query_ids": auth.get("query_ids"),
        "run_budget": auth.get("run_budget"),
    }
    return sha256_text(json_text(relevant))


def begin_requalification_execution(key: str, run_id: str) -> dict:
    # 复验登记发生在首条正式质量查询之前；同一复验身份重复登记一律拒绝。
    if not REQUAL_AUTH_PATH.is_file():
        raise EvaluationBlocked("缺少复验授权文件，拒绝正式留出复验", (str(REQUAL_AUTH_PATH),))
    ledger = load_document(REQUAL_LEDGER_PATH) if REQUAL_LEDGER_PATH.is_file() else {"executions": []}
    if any(item["key"] == key for item in ledger["executions"]):
        raise EvaluationBlocked("该复验身份已登记，拒绝重复正式留出执行", (key,))
    entry = {
        "key": key,
        "run_id": run_id,
        "authorization_sha256": sha256(REQUAL_AUTH_PATH),
        "started_at": iso_utc(),
        "status": "running",
        "finished_at": None,
    }
    ledger["executions"].append(entry)
    REQUAL_LEDGER_PATH.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    return entry


def complete_requalification_execution(key: str, status: str) -> None:
    if status not in ("completed", "failed"):
        raise EvaluationBlocked("复验登记结束状态非法", (status,))
    if not REQUAL_LEDGER_PATH.is_file():
        raise EvaluationBlocked("缺少复验登记账本", (str(REQUAL_LEDGER_PATH),))
    ledger = load_document(REQUAL_LEDGER_PATH)
    matched = False
    for item in ledger["executions"]:
        if item["key"] == key:
            item["status"] = status
            item["finished_at"] = iso_utc()
            matched = True
    if not matched:
        raise EvaluationBlocked("复验登记账本缺少对应条目", (key,))
    REQUAL_LEDGER_PATH.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")


async def open_published(db: MilvusConnection, corpus: RetrievalCorpus) -> tuple[str, dict]:
    # 真实向量检索入口复用 T5 既有 HybridRetriever/validate_index/validated_manifest：
    # 解析 alias 后校验实际物理集合与完整发布清单，并返回集合名与清单指纹。
    # 零请求验证路径只使用真实客户端配置身份，不调用编码 API。
    retriever = await HybridRetriever.open(db, corpus, EmbeddingClient.from_env())
    collection, manifest_json = retriever.validation_identity()
    return collection, {"collection": collection, "manifest_sha256": sha256_text(manifest_json)}
