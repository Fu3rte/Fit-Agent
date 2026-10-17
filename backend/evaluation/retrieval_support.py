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
from app.application.business.retrieval_response import (
    SEARCH_BUDGET,
    SEARCH_LIMIT,
    build_search_result,
)
from app.application.business.retrieval_search import HybridRetriever
from app.domain.business.models import CatalogSearchArguments, ProfileContent

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
OUTPUT_ROOT = BACKEND / "temp" / "retrieval-ranx"
REPORT_PATH = REPO / "quantitive-index/evaluation.md"
RECALL_LIMIT = SEARCH_LIMIT + 1
APPROVED_INPUTS = {"queries": 128, "labels": 405, "dev": 61, "holdout": 67, "families": 22, "catalog": 1324}


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


def project_response(catalog: Catalog, recalled: list, evaluated: EvaluatedConditions, threshold: float | None) -> dict:
    if any(not math.isfinite(item.score) for item in recalled):
        raise ValueError("候选分数非有限值")
    filtered = [item for item in recalled if threshold is None or item.score >= threshold]
    result = build_search_result(filtered, evaluated)
    payload = result.model_dump()
    chars = len(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    return {
        "recalled": [item.id for item in recalled],
        "returned": [item.id for item in result.items],
        "scores": {item.id: item.score for item in recalled},
        "response": payload,
        "hard_constraint_violations": [item.id for item in filtered if not evaluated.conditions.matches(catalog.get(item.id))],
        "serialized_chars": chars,
        "budget_compliant": chars <= SEARCH_BUDGET and len(result.items) <= SEARCH_LIMIT,
    }


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def query_vector_identity(embedding_config, corpus: RetrievalCorpus) -> dict:
    return {
        "model": embedding_config.model, "dimensions": embedding_config.dimensions,
        "encoding_rule_version": rt.ENCODING_RULE_VERSION,
        "normalization": "NFKC+whitespace-collapse+casefold",
        "service_fingerprint": sha256_text("endpoint:" + embedding_config.base_url),
        "service_class": WORKSPACE_DOMAIN + COMPATIBLE_MODE_PATH,
        "sources": corpus.sources, "text_sha256": sha256_text(json_text(corpus.records)),
    }


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
