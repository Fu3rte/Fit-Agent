import json
import shutil
from pathlib import Path

from app.application.business import retrieval_text as rt
from app.application.business.retrieval_frozen import frozen_values
from app.application.business.retrieval_index import RetrievalCorpus, sha256
from app.application.business.retrieval_search import RetrievalCandidate
from evaluation import retrieval_support as support
from evaluation.dataset import (
    DATA_FILES,
    EMPTY_QUOTAS,
    PURPOSE_QUOTAS,
    SCENARIO_CATEGORIES,
    SCHEMA_DIR,
    SCHEMA_PATHS,
    load_for_evaluation,
    qrel_digest,
    query_digest,
    release_digest,
    scenario_digest,
)
from evaluation.retrieval import (
    base_document,
    settle_document,
    validate_group,
    validate_projections,
)

REVIEWED_AT = "2026-10-18T00:00:00+00:00"


def draft_review() -> dict:
    return {
        "status": "draft",
        "reviewer": None,
        "reviewer_type": None,
        "reviewed_at": None,
        "comment": None,
        "content_sha256": None,
        "decision_ref": None,
    }


def approve(review: dict, digest: str, reference: str) -> dict:
    review.update({
        "status": "approved",
        "reviewer": "用户",
        "reviewer_type": "user",
        "reviewed_at": REVIEWED_AT,
        "comment": "测试审批结构，仅用于验证实现",
        "content_sha256": digest,
        "decision_ref": reference,
    })
    return review


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(
            "".join(
                json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
                for row in rows
            )
        )


def _write_json(path: Path, document: dict) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def _source(corpus: RetrievalCorpus) -> dict:
    return {
        "catalog": {
            "path": "backend/dataset/actions.agent.json",
            "sha256": corpus.sources["catalog"]["sha256"],
            "count": len(corpus.catalog),
        },
        "supplements": {
            "path": "backend/dataset/action-supplements.published.json",
            "sha256": corpus.sources["published"]["sha256"],
            "version": corpus.sources["published"]["version"],
        },
        "mappings": {
            "path": "backend/dataset/action-field-mappings.json",
            "sha256": corpus.sources["mappings"]["sha256"],
        },
        "encoding_rule_version": rt.ENCODING_RULE_VERSION,
    }


def _conditions() -> dict:
    return {
        "equipment": None,
        "body_part": None,
        "target": None,
        "muscle_group": None,
        "environment": "健身房",
        "declared_equipment": [],
        "unavailable_equipment": [],
        "excluded_ids": [],
        "saved_profile": {"unavailable_equipment": [], "forbidden_exercise_ids": []},
    }


def _judgement_scope() -> dict:
    return {
        "checked_scope": "真实动作目录全量",
        "candidate_sources": ["catalog"],
        "omission_review": "逐条复核已标注与易混淆负例",
        "complete": True,
    }


def _query_rows(corpus: RetrievalCorpus, approve_rows: bool) -> tuple[list[dict], list[dict], list[dict]]:
    index = {record["id"]: position for position, record in enumerate(corpus.records)}
    ids = [record["id"] for record in corpus.records]
    queries, qrels = [], []
    for purpose, count, empty_count in (
        ("lookup", PURPOSE_QUOTAS["lookup"], EMPTY_QUOTAS["lookup"]),
        ("generation", PURPOSE_QUOTAS["generation"], EMPTY_QUOTAS["generation"]),
        ("replacement", PURPOSE_QUOTAS["replacement"], EMPTY_QUOTAS["replacement"]),
    ):
        for number in range(1, count + 1):
            query_id = f"q-test-{purpose}-{number:03d}"
            empty = number <= empty_count
            queries.append({
                "query_id": query_id,
                "scenario_id": None,
                "query": f"测试{purpose}请求{number:03d}",
                "purpose": purpose,
                "conditions": _conditions(),
                "tags": [purpose, "test-data"],
                "family": "test-family",
                "expect_empty": empty,
                "judgement_scope": _judgement_scope(),
                "review": draft_review(),
            })
            if empty:
                corpus_id = ids[number - 1]
                qrels.append({
                    "query_id": query_id,
                    "corpus_id": corpus_id,
                    "grade": 0,
                    "rationale": "测试用已审阅负例",
                    "evidence": {
                        "source_path": "backend/dataset/actions.agent.json",
                        "json_pointer": f"/{index[corpus_id]}",
                        "excerpt": "测试负例",
                    },
                    "review": draft_review(),
                })
            else:
                for grade, offset in ((3, number - 1), (2, number + 9), (1, number + 19)):
                    corpus_id = ids[offset % len(ids)]
                    qrels.append({
                        "query_id": query_id,
                        "corpus_id": corpus_id,
                        "grade": grade,
                        "rationale": f"测试用 grade={grade} 判断",
                        "evidence": {
                            "source_path": "backend/dataset/actions.agent.json",
                            "json_pointer": f"/{index[corpus_id]}",
                            "excerpt": "测试依据",
                        },
                        "review": draft_review(),
                    })
    if approve_rows:
        for qrel in qrels:
            approve(qrel["review"], qrel_digest(qrel), f"test-qrel-{qrel['query_id']}-{qrel['corpus_id']}")
        for query in queries:
            related = [qrel for qrel in qrels if qrel["query_id"] == query["query_id"]]
            approve(query["review"], query_digest(query, related), f"test-query-{query['query_id']}")
    return queries, qrels, []


def _scenario_rows(asset_hash: str, approve_rows: bool) -> list[dict]:
    scenarios = []
    for category in SCENARIO_CATEGORIES:
        for number in range(1, SCENARIO_CATEGORIES[category] + 1):
            scenario_id = f"s-test-{category.replace('_', '-')}-{number:02d}"
            attachment = ["plan-note"] if category == "plan_import_file" and number == 1 else []
            scenario = {
                "scenario_id": scenario_id,
                "input": {"message": "测试场景输入", "prior_messages": [], "attachments": attachment},
                "initial_state": {
                    "profile": None,
                    "plans": [],
                    "workouts": [],
                    "current_plan_id": None,
                    "now": "2026-10-18T09:00:00+00:00",
                    "timezone": "Asia/Shanghai",
                },
                "events": [
                    {"event_id": "e01", "type": "message", "source_event_id": None,
                     "content": "测试场景输入", "attachments": attachment, "target": None},
                    {"event_id": "e02", "type": "confirm", "source_event_id": "e01", "content": None, "attachments": [],
                     "target": {"business_kind": "profile", "proposal_ref": "profile-proposal-e01",
                                "display_ref": "profile-display-e01", "confirmation_ref": "profile-confirm-e01"}},
                ],
                "reference": [{
                    "checkpoint_id": "cp1",
                    "event_id": "e01",
                    "answer": "测试参考回答",
                    "facts": [{"fact_id": "f1", "text": "测试事实", "source": "backend/dataset/actions.agent.json", "asset_id": None}],
                }],
                "rubric": [{
                    "checkpoint_id": "cp1",
                    "event_id": "e01",
                    "must_include": ["测试事实"],
                    "allowed_variation": [],
                    "forbidden_claims": [],
                }],
                "assertions": [{
                    "checkpoint_id": "cp1",
                    "event_id": "e01",
                    "kind": "no_saved_state",
                    "business_kind": "profile",
                }],
                "metric_applicability": [
                    {"checkpoint_id": "cp1", "event_id": "e01", "metric": "faithfulness", "applicable": True, "reason": None},
                    {"checkpoint_id": "cp1", "event_id": "e01", "metric": "factual_correctness", "applicable": True, "reason": None},
                    {"checkpoint_id": "cp1", "event_id": "e01", "metric": "deterministic_rules", "applicable": True, "reason": None},
                ],
                "tags": [category, "test-data"],
                "family": "test-family",
                "review": draft_review(),
            }
            if approve_rows:
                hashes = {"plan-note": asset_hash} if "plan-note" in attachment else {}
                approve(scenario["review"], scenario_digest(scenario, hashes), f"test-scenario-{scenario_id}")
            scenarios.append(scenario)
    return scenarios


def build_dataset(root: Path, *, approve: bool = True) -> Path:
    version_dir = Path(root) / "v-test-1"
    if version_dir.exists():
        shutil.rmtree(version_dir)
    (version_dir / "assets").mkdir(parents=True)
    asset_path = version_dir / "assets" / "plan-note.txt"
    asset_path.write_text("测试附件", encoding="utf-8")
    asset_hash = sha256(asset_path)
    corpus = RetrievalCorpus.load()
    source = _source(corpus)
    corpus_rows = [
        {
            "corpus_id": record["id"],
            "text": record["text"],
            "metadata": {field: record[field] for field in rt.MAPPING_FIELDS},
            "source": source,
        }
        for record in corpus.records
    ]
    queries, qrels, _ = _query_rows(corpus, approve)
    scenarios = _scenario_rows(asset_hash, approve)
    files = {
        "corpus": corpus_rows,
        "queries": queries,
        "qrels": qrels,
        "scenarios": scenarios,
    }
    for name, rows in files.items():
        _write_jsonl(version_dir / DATA_FILES[name], rows)
    manifest = {
        "dataset_version": version_dir.name,
        "schema_version": "evaluation-dataset-1",
        "created_at": "2026-10-18T00:00:00+00:00",
        "source": source,
        "schemas": {
            name: {"path": SCHEMA_PATHS[name], "sha256": sha256(SCHEMA_DIR / f"{name}.schema.json")}
            for name in SCHEMA_PATHS
        },
        "files": {
            name: {"path": DATA_FILES[name], "sha256": sha256(version_dir / DATA_FILES[name])}
            for name in DATA_FILES
        },
        "assets": [{"asset_id": "plan-note", "path": "assets/plan-note.txt", "sha256": asset_hash}],
        "counts": {
            "corpus": len(corpus_rows),
            "queries": len(queries),
            "qrels": len(qrels),
            "scenarios": len(scenarios),
            "assets": 1,
        },
        "quotas": {
            "purposes": PURPOSE_QUOTAS,
            "expect_empty": EMPTY_QUOTAS,
            "scenario_categories": SCENARIO_CATEGORIES,
        },
        "release": {
            "status": "draft",
            "reviewer": None,
            "reviewer_type": None,
            "reviewed_at": None,
            "comment": None,
            "decision_ref": None,
            "content_sha256": None,
        },
    }
    if approve:
        digest = release_digest(manifest)
        manifest["release"].update({
            "status": "approved",
            "reviewer": "用户",
            "reviewer_type": "user",
            "reviewed_at": REVIEWED_AT,
            "comment": "测试发布审批结构，仅用于验证实现",
            "decision_ref": "test-release",
            "content_sha256": digest,
        })
    _write_json(version_dir / "manifest.json", manifest)
    return version_dir


def _recalled(catalog, rows: list[tuple[str, float]]) -> list[RetrievalCandidate]:
    candidates = []
    for corpus_id, score in rows:
        exercise = catalog.get(corpus_id)
        candidates.append(
            RetrievalCandidate(
                id=exercise.id,
                score=score,
                name=exercise.name,
                equipment=exercise.equipment,
                body_part=exercise.body_part,
                target=exercise.target,
                muscle_group=exercise.muscle_group,
            )
        )
    return candidates


def make_document(version_dir: Path, group: str = "hybrid", *, unknown: bool = False) -> tuple[dict, object]:
    dataset = load_for_evaluation(version_dir)
    corpus = RetrievalCorpus.load()
    mappings = rt.load_mappings()
    qrels = dataset.qrels_by_query()
    threshold = 0.5 if unknown else 0.0
    projections, timings, evaluated_by_query = {}, {}, {}
    for item in dataset.queries:
        query_id = item["query_id"]
        evaluated_by_query[query_id] = evaluated = support.dataset_conditions(
            item, corpus.catalog, mappings
        )
        rows = []
        if not item["expect_empty"]:
            rows.append((next(qrel["corpus_id"] for qrel in qrels[query_id] if qrel["grade"] >= 1), 1.0))
            if unknown:
                reviewed = {qrel["corpus_id"] for qrel in qrels[query_id]}
                extra = next(
                    corpus_id for corpus_id in corpus.catalog.exercise_ids if corpus_id not in reviewed
                )
                # 未审阅候选仅出现在候选池，用于验证 Recall@20 阶段门禁。
                rows.append((extra, 0.0))
        projections[query_id] = support.project_response(
            corpus.catalog, query_id, _recalled(corpus.catalog, rows), evaluated, threshold
        )
        timings[query_id] = {"start_time": "2026-10-18T09:00:00+00:00", "end_time": "2026-10-18T09:00:01+00:00"}
    document = base_document("live", dataset.version)
    document.update(
        dataset=dataset.identity(),
        parameters=frozen_values(),
        query_ids=[item["query_id"] for item in dataset.queries],
        index_identity={},
        timing_scope="unit_test",
        observation_scope="unit_test",
    )
    if unknown:
        document["groups"][group] = validate_group(
            dataset, group, projections, timings, corpus.catalog, mappings, threshold
        )
    else:
        document["groups"][group] = {
            "status": "complete",
            "metrics": validate_projections(
                dataset.scoring_queries(), projections, corpus.catalog, evaluated_by_query, threshold
            ),
            "projections": projections,
            "timings": timings,
        }
    settle_document(document)
    return document, dataset
