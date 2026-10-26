import hashlib
import importlib
import json
import math
from pathlib import Path

from jsonschema import Draft202012Validator

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
from app.application.business.retrieval_rerank import (
    RerankClient,
    RerankIdentity,
    RerankResult,
    RerankScored,
    RerankUsage,
    build_request,
)
from app.application.business.retrieval_response import (
    CANDIDATE_POOL_LIMIT,
    SEARCH_BUDGET,
    SEARCH_LIMIT,
    build_search_result,
    build_search_stage,
    encoded_length,
)
from app.application.business.retrieval_search import HybridRetriever, search_vectors
from app.domain.business.models import CatalogSearchArguments, ProfileContent
from evaluation.contracts import (
    ModelVisibleSearch,
    RerankEvidence,
    SearchResponseEvidence,
    VisibleExercise,
)

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
DATASET = BACKEND / "dataset"
QUERIES_PATH = DATASET / "action-retrieval-queries.json"
ADAPTATION_PATH = DATASET / "action-retrieval-purpose-adaptation.json"
EVAL_ROOT = BACKEND / "temp" / "retrieval-evaluation"
CACHE_ROOT = EVAL_ROOT / "query-vectors"
FROZEN_PATH = EVAL_ROOT / "frozen-parameters.json"
HOLDOUT_LEDGER_PATH = EVAL_ROOT / "holdout-executions.json"
REQUAL_ROOT = BACKEND / "temp" / "retrieval-evaluation-requalification"
REQUAL_AUTH_PATH = REQUAL_ROOT / "authorization.json"
REQUAL_LEDGER_PATH = REQUAL_ROOT / "executions.json"
FROZEN_CONFIG_PATH = BACKEND / "app/application/business/retrieval_frozen.py"
OUTPUT_ROOT = BACKEND / "temp" / "retrieval-mteb"
RECALL_LIMIT = CANDIDATE_POOL_LIMIT
EXPERIMENT_BASELINE = "rrf_baseline"
EXPERIMENT_RERANK = "rerank"
# 历史入口保持原 11→10 单阶段身份，不接入两阶段候选池。
LEGACY_RETRIEVAL_LIMIT = 10
LEGACY_RECALL_LIMIT = LEGACY_RETRIEVAL_LIMIT + 1
APPROVED_INPUTS = {"queries": 128, "labels": 405, "dev": 61, "holdout": 67, "families": 22, "catalog": 1320}


class EvaluationBlocked(RuntimeError):
    def __init__(self, reason: str, missing: tuple[str, ...] = ()) -> None:
        super().__init__(reason)
        self.reason, self.missing = reason, missing


def load_document(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_document(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")


def query_documents() -> list[dict]:
    return load_document(QUERIES_PATH)["queries"]


def load_queries(split: str) -> list[dict]:
    selected = [item for item in query_documents() if item["split"] == split]
    if not selected:
        raise EvaluationBlocked(f"拆分 {split} 没有查询")
    return selected


PURPOSES = ("lookup", "generation", "replacement")


def select_validation_queries(dataset) -> list[dict]:
    # 定向验证固定为每个用途各取第一条有答案与第一条无答案查询；本地与 LangSmith 共用。
    rows = sorted(dataset.queries, key=lambda item: item["query_id"])
    selected = []
    for purpose in PURPOSES:
        for expect_empty in (False, True):
            matches = [
                item
                for item in rows
                if item["purpose"] == purpose and item["expect_empty"] is expect_empty
            ]
            if not matches:
                raise EvaluationBlocked(
                    f"用途 {purpose} 缺少 expect_empty={expect_empty} 的批准查询"
                )
            selected.append(matches[0])
    if len(selected) != 6 or len({item["query_id"] for item in selected}) != 6:
        raise EvaluationBlocked("定向验收必须选择 6 条唯一查询")
    return selected


def verify_inputs(catalog: Catalog) -> dict:
    document = load_document(QUERIES_PATH)
    Draft202012Validator(load_document(DATASET / "action-retrieval-queries.schema.json")).validate(document)
    queries = document["queries"]
    ledger = load_document(DATASET / "action-supplement-reviews.json")
    decision = next(item for item in ledger["decisions"] if item["scope"] == "backend/dataset/action-retrieval-queries.json")
    digest = sha256(QUERIES_PATH)
    counts = {
        "queries": len(queries), "labels": sum(len(item["labels"]) for item in queries),
        "dev": sum(item["split"] == "dev" for item in queries),
        "holdout": sum(item["split"] == "holdout" for item in queries),
        "families": len({item["family"] for item in queries}), "catalog": len(catalog),
        "expect_empty": sum(item["expect_empty"] for item in queries),
    }
    family_splits = {}
    for item in queries:
        family_splits.setdefault(item["family"], set()).add(item["split"])
    checks = {
        "ledger_hash_bound": decision["content_sha256"] == digest,
        "ledger_approved": ledger["status"] == "approved" and bool(decision["conclusion"]),
        "catalog_hash_bound": document["catalog"]["sha256"] == sha256(DEFAULT_ACTIONS_PATH),
        "catalog_count": len(catalog) == document["catalog"]["count"],
        "label_status": document["label_status"] == "approved" and all(item["label_status"] == "approved" for item in queries),
        "family_not_crossing_splits": all(len(splits) == 1 for splits in family_splits.values()),
        "unique_query_ids": len({item["id"] for item in queries}) == len(queries),
        "label_ids_unique_per_query": all(len({label["id"] for label in item["labels"]}) == len(item["labels"]) for item in queries),
        "labels_reference_catalog": all(label["id"] in catalog.exercise_ids for item in queries for label in item["labels"]),
        "saved_references_catalog": all(eid in catalog.exercise_ids for item in queries for eid in item["saved_profile_restrictions"]["forbidden_exercise_ids"]),
        "labels_or_expect_empty": all(bool(item["labels"]) != item["expect_empty"] for item in queries),
        **{f"count_{key}": counts[key] == value for key, value in APPROVED_INPUTS.items()},
    }
    checks["passed"] = all(checks.values())
    return {"checks": checks, "counts": counts, "queries_sha256": digest}


def require_inputs(catalog: Catalog) -> dict:
    verification = verify_inputs(catalog)
    if not verification["checks"]["passed"]:
        raise EvaluationBlocked("批准输入核验未通过", tuple(key for key, value in verification["checks"].items() if not value))
    return verification


def load_adaptation() -> tuple[dict, dict[str, str]]:
    document = load_document(ADAPTATION_PATH)
    if document["status"] != "approved" or document["queries_sha256"] != sha256(QUERIES_PATH):
        raise EvaluationBlocked("用途适配未批准或查询哈希不一致")
    rows = {row["id"]: row for row in document["rows"]}
    if len(rows) != len(document["rows"]) or set(rows) != {item["id"] for item in query_documents()}:
        raise EvaluationBlocked("用途适配必须逐条覆盖金标且 ID 唯一")
    if any(row["purpose"] not in ("lookup", "generation", "replacement") for row in rows.values()):
        raise EvaluationBlocked("用途适配包含非法用途")
    if not set(document["deferred_query_ids"]) <= set(rows):
        raise EvaluationBlocked("暂缓查询引用非法 ID")
    return document, {qid: row["purpose"] for qid, row in rows.items()}


def dataset_conditions(query: dict, catalog: Catalog, mappings: dict) -> EvaluatedConditions:
    conditions = query["conditions"]
    arguments = CatalogSearchArguments(
        purpose=query["purpose"],
        name=query["query"],
        equipment=conditions["equipment"],
        body_part=conditions["body_part"],
        target=conditions["target"],
        muscle_group=conditions["muscle_group"],
        task_environment=conditions["environment"],
        available_equipment=conditions["declared_equipment"] or None,
        unavailable_equipment=conditions["unavailable_equipment"] or None,
        excluded_ids=conditions["excluded_ids"] or None,
    )
    saved = conditions["saved_profile"]
    profile = ProfileContent(
        goal=None, experience=None, environment=None, availability=None, health_notes=None,
        movement_restrictions=None,
        unavailable_equipment=saved["unavailable_equipment"] or None,
        forbidden_exercise_ids=saved["forbidden_exercise_ids"] or None,
    )
    return evaluate_search_conditions(arguments, profile, catalog, mappings)


def effective_conditions(query: dict, catalog: Catalog, mappings: dict, purpose: str, temporary: dict) -> EvaluatedConditions:
    arguments = CatalogSearchArguments(
        purpose=purpose, name=query["query"], **query["filters"],
        task_environment=query["environment"], available_equipment=query["declared_equipment"] or None,
        unavailable_equipment=temporary.get("unavailable_equipment") or None,
        excluded_ids=temporary.get("excluded_ids") or None,
    )
    profile = ProfileContent(
        goal=None, experience=None, environment=None, availability=None, health_notes=None,
        movement_restrictions=None,
        unavailable_equipment=query["saved_profile_restrictions"]["unavailable_equipment"] or None,
        forbidden_exercise_ids=query["saved_profile_restrictions"]["forbidden_exercise_ids"] or None,
    )
    return evaluate_search_conditions(arguments, profile, catalog, mappings)


def _conditions_payload(evaluated: EvaluatedConditions) -> dict:
    conditions = evaluated.conditions
    return {
        "equipment": conditions.equipment,
        "body_part": conditions.body_part,
        "target": conditions.target,
        "muscle_group": conditions.muscle_group,
        "available_equipment": list(conditions.available_equipment or ()),
        "excluded_ids": list(conditions.excluded_ids),
        "filtered_fields": list(evaluated.filtered_fields),
        "condition_notes": list(evaluated.condition_notes),
    }


def _rerank_payload(
    result: RerankResult | None, identity: RerankIdentity | None
) -> dict | None:
    # 证据与生产阶段同源；写入前用契约模型拒绝缺身份、空结果与非降序排序。
    if result is None:
        if identity is not None:
            raise ValueError("缺少重排结果时不得提供配置身份")
        return None
    if identity is None:
        raise ValueError("缺少重排配置身份")
    evidence = RerankEvidence(
        mode=EXPERIMENT_RERANK,
        identity={
            "model": identity.model,
            "endpoint": identity.endpoint,
            "text_version": identity.text_version,
        },
        scored=[
            {
                "id": row.id,
                "rerank_score": row.rerank_score,
                "rrf_rank": row.rrf_rank,
            }
            for row in result.scored
        ],
        usage={
            "requests": result.usage.requests,
            "prompt_tokens": result.usage.prompt_tokens,
            "total_tokens": result.usage.total_tokens,
        },
        request_id=result.request_id,
    )
    return evidence.model_dump(mode="json")


def rebuild_rerank(evidence: object) -> tuple[RerankResult, RerankIdentity]:
    # 回放只消费已保存数据：重建共享类型，由 build_search_stage 复核覆盖与顺序。
    parsed = RerankEvidence.model_validate(evidence)
    if parsed.mode != EXPERIMENT_RERANK:
        raise ValueError("回放重排必须使用 rerank 实验身份")
    if parsed.identity is None or parsed.usage is None:
        raise ValueError("重排证据缺少身份或用量")
    result = RerankResult(
        scored=tuple(
            RerankScored(row.id, row.rerank_score, row.rrf_rank)
            for row in parsed.scored
        ),
        usage=RerankUsage(
            requests=parsed.usage.requests,
            prompt_tokens=parsed.usage.prompt_tokens,
            total_tokens=parsed.usage.total_tokens,
        ),
        request_id=parsed.request_id,
    )
    identity = RerankIdentity(
        model=parsed.identity.model,
        endpoint=parsed.identity.endpoint,
        text_version=parsed.identity.text_version,
    )
    return result, identity


def public_rerank_evidence(evidence: dict | None) -> dict | None:
    # 上传与 LangSmith 只暴露模型、文本版本、地址摘要、排序、用量与 request_id。
    if evidence is None:
        return None
    identity = evidence["identity"]
    return {
        "mode": evidence["mode"],
        "identity": {
            "model": identity["model"],
            "endpoint": sha256_text(identity["endpoint"]),
            "text_version": identity["text_version"],
        },
        "scored": evidence["scored"],
        "usage": evidence["usage"],
        "request_id": evidence["request_id"],
    }


def project_response(
    catalog: Catalog,
    query_id: str,
    recalled: list,
    evaluated: EvaluatedConditions,
    threshold: float | None,
    *,
    rerank: RerankResult | None = None,
    rerank_identity: RerankIdentity | None = None,
    experiment: str | None = None,
) -> dict:
    # 生产与评测共用同一阶段构造：门槛前候选池与公开最终输入来自同一次检索。
    if any(not math.isfinite(item.score) for item in recalled):
        raise ValueError("候选分数非有限值")
    mode = experiment or (
        EXPERIMENT_RERANK if rerank is not None else EXPERIMENT_BASELINE
    )
    if mode == EXPERIMENT_BASELINE and rerank is not None:
        raise ValueError("RRF baseline 不得携带重排结果")
    if mode == EXPERIMENT_RERANK and rerank is None:
        raise ValueError("重排实验必须携带真实重排结果")
    stage = build_search_stage(
        recalled,
        evaluated,
        rerank=rerank,
        rerank_identity=rerank_identity,
        threshold=threshold,
    )
    payload = stage.response.model_dump()
    chars = encoded_length(payload)
    evidence = SearchResponseEvidence(
        query_id=query_id,
        response_json=payload,
        model_visible=ModelVisibleSearch(
            items=[
                VisibleExercise(
                    exercise_id=item.id,
                    name=item.name,
                    equipment=item.equipment,
                    target=item.target,
                    matches=item.matches,
                )
                for item in stage.response.items
            ],
            has_more=stage.response.has_more,
        ),
    )
    evidence.require_scored_consistency(
        [item.id for item in stage.final_input]
    )
    return {
        "experiment": mode,
        "rerank": _rerank_payload(stage.rerank, stage.rerank_identity),
        "recalled": [item.id for item in recalled],
        "candidate_pool": [item.id for item in stage.candidate_pool],
        "final_model_input": [item.id for item in stage.final_input],
        "scores": {item.id: item.score for item in recalled},
        "response": payload,
        "search_evidence": evidence.model_dump(),
        "conditions": _conditions_payload(evaluated),
        "threshold": threshold,
        "budget_chars": SEARCH_BUDGET,
        "item_limit": SEARCH_LIMIT,
        "serialized_chars": chars,
        "hard_constraint_violations": [
            item.id
            for item in stage.candidate_pool
            if not evaluated.conditions.matches(catalog.get(item.id))
        ],
        "budget_compliant": (
            chars <= SEARCH_BUDGET
            and len(stage.response.items) <= SEARCH_LIMIT
            and len(stage.candidate_pool) <= CANDIDATE_POOL_LIMIT
        ),
    }


def project_legacy_response(
    catalog: Catalog, recalled: list, evaluated: EvaluatedConditions, threshold: float | None
) -> dict:
    # 历史入口保留原 11→10 单阶段返回与指标身份；不生成两阶段候选池证据。
    if any(not math.isfinite(item.score) for item in recalled):
        raise ValueError("候选分数非有限值")
    filtered = [item for item in recalled if threshold is None or item.score >= threshold]
    result = build_search_result(
        filtered, evaluated, limit=LEGACY_RETRIEVAL_LIMIT
    )
    payload = result.model_dump()
    chars = encoded_length(payload)
    return {
        "recalled": [item.id for item in recalled],
        "returned": [item.id for item in result.items],
        "scores": {item.id: item.score for item in recalled},
        "response": payload,
        "hard_constraint_violations": [
            item.id
            for item in filtered
            if not evaluated.conditions.matches(catalog.get(item.id))
        ],
        "serialized_chars": chars,
        "budget_compliant": chars <= SEARCH_BUDGET
        and len(result.items) <= LEGACY_RETRIEVAL_LIMIT,
    }


async def recall_query(
    db: MilvusConnection,
    collection: str,
    corpus: RetrievalCorpus,
    mappings: dict,
    query: dict,
    vector: list[float],
    *,
    group: str,
    rrf_k: int,
    bm25_depth: int,
    dense_depth: int,
) -> tuple[EvaluatedConditions, list]:
    # 同池对照只调用一次真实召回；候选顺序与 RRF 分数由该次调用唯一确定。
    evaluated = dataset_conditions(query, corpus.catalog, mappings)
    recalled = await search_vectors(
        db,
        collection,
        corpus.catalog,
        query["query"],
        vector,
        conditions=evaluated.conditions,
        rrf_k=rrf_k,
        bm25_depth=bm25_depth,
        dense_depth=dense_depth,
        limit=RECALL_LIMIT,
        mode=group,
    )
    return evaluated, recalled


async def execute_query(
    db: MilvusConnection,
    collection: str,
    corpus: RetrievalCorpus,
    mappings: dict,
    query: dict,
    vector: list[float],
    *,
    group: str,
    rrf_k: int,
    bm25_depth: int,
    dense_depth: int,
    threshold: float | None,
) -> dict:
    # 旧全量入口与 LangSmith target 共用同一真实单查询执行与阶段构造。
    evaluated, recalled = await recall_query(
        db,
        collection,
        corpus,
        mappings,
        query,
        vector,
        group=group,
        rrf_k=rrf_k,
        bm25_depth=bm25_depth,
        dense_depth=dense_depth,
    )
    return project_response(
        corpus.catalog,
        query["query_id"],
        recalled,
        evaluated,
        threshold if group == "hybrid" else None,
        experiment=EXPERIMENT_BASELINE,
    )


async def execute_comparison_query(
    db: MilvusConnection,
    collection: str,
    corpus: RetrievalCorpus,
    mappings: dict,
    query: dict,
    vector: list[float],
    *,
    group: str,
    rrf_k: int,
    bm25_depth: int,
    dense_depth: int,
    threshold: float | None,
    rerank_client: RerankClient,
    rerank_identity: RerankIdentity,
) -> dict[str, dict]:
    # 同一候选池上的显式对照：先冻结 RRF Top-20，再构造基线响应与真实重排响应。
    if group != "hybrid":
        raise ValueError("重排实验只接受 hybrid 候选池")
    evaluated, recalled = await recall_query(
        db,
        collection,
        corpus,
        mappings,
        query,
        vector,
        group=group,
        rrf_k=rrf_k,
        bm25_depth=bm25_depth,
        dense_depth=dense_depth,
    )
    baseline = project_response(
        corpus.catalog,
        query["query_id"],
        recalled,
        evaluated,
        threshold,
        experiment=EXPERIMENT_BASELINE,
    )
    pool = recalled[:CANDIDATE_POOL_LIMIT]
    request = build_request(query["query"], pool, corpus.records)
    result = await rerank_client.rerank(request)
    reranked = project_response(
        corpus.catalog,
        query["query_id"],
        recalled,
        evaluated,
        threshold,
        rerank=result,
        rerank_identity=rerank_identity,
        experiment=EXPERIMENT_RERANK,
    )
    if (
        baseline["candidate_pool"] != reranked["candidate_pool"]
        or baseline["scores"] != reranked["scores"]
    ):
        raise ValueError("同池对照的候选池或 RRF 分数不一致")
    return {EXPERIMENT_BASELINE: baseline, EXPERIMENT_RERANK: reranked}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def query_vector_identity(embedding_config, corpus: RetrievalCorpus, dataset_identity: dict | None = None) -> dict:
    identity = {
        "model": embedding_config.model, "dimensions": embedding_config.dimensions,
        "encoding_rule_version": rt.ENCODING_RULE_VERSION,
        "normalization": "NFKC+whitespace-collapse+casefold",
        "service_fingerprint": sha256_text("endpoint:" + embedding_config.base_url),
        "service_class": WORKSPACE_DOMAIN + COMPATIBLE_MODE_PATH,
        "sources": corpus.sources, "text_sha256": sha256_text(json_text(corpus.records)),
    }
    if dataset_identity is not None:
        identity["dataset"] = dataset_identity
    return identity


def cache_path(identity: dict) -> Path:
    return CACHE_ROOT / f"query-vectors-{sha256_text(json_text(identity))[:20]}.json"


def read_query_cache(identity: dict, queries: list[dict]) -> tuple[dict, dict]:
    path = cache_path(identity)
    if not path.is_file():
        raise EvaluationBlocked("缺少真实查询向量缓存", (str(path),))
    document = load_document(path)
    if document["identity"] != identity:
        raise EvaluationBlocked("查询向量缓存身份不一致")
    entries = {}
    for entry in document["entries"]:
        qid = entry["id"]
        if qid in entries or entry["model"] != identity["model"] or entry["normalized_sha256"] != sha256_text(rt.normalize(entry["query"])):
            raise EvaluationBlocked(f"缓存条目身份不符: {qid}")
        vector = entry["vector"]
        if not isinstance(vector, list) or len(vector) != identity["dimensions"] or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector) or not any(vector):
            raise EvaluationBlocked(f"缓存向量非法: {qid}")
        entries[qid] = entry
    selected = {}
    for item in queries:
        qid = item["id"]
        if qid not in entries or entries[qid]["query"] != item["query"]:
            raise EvaluationBlocked(f"缓存未覆盖查询或文本不符: {qid}")
        selected[qid] = entries[qid]
    return {qid: entry["vector"] for qid, entry in selected.items()}, selected


def load_frozen() -> dict:
    return load_document(FROZEN_PATH)


def frozen_config_identity() -> dict:
    module = importlib.import_module("app.application.business.retrieval_frozen")
    return {"path": str(FROZEN_CONFIG_PATH.relative_to(BACKEND)).replace("\\", "/"),
            "sha256": sha256(FROZEN_CONFIG_PATH), "values": module.frozen_values()}


async def open_published(db: MilvusConnection, corpus: RetrievalCorpus) -> tuple[str, dict]:
    retriever = await HybridRetriever.open(db, corpus, EmbeddingClient.from_env())
    collection, manifest_json = retriever.validation_identity()
    return collection, {"collection": collection, "manifest_sha256": sha256_text(manifest_json)}
