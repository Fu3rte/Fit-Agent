import hashlib
import json
from collections import Counter

import jsonlines
import pytest
from jsonpointer import resolve_pointer

from evaluation.dataset import (
    BACKEND,
    REBUILD_V3_SCENARIO_CATEGORIES,
    load_for_evaluation,
    qrel_digest,
    query_digest,
    release_digest,
    scenario_baseline,
)

DATA_ROOT = BACKEND / "dataset/evaluation"
PARENT = DATA_ROOT / "rebuild-v3"
VERSION = DATA_ROOT / "rebuild-v4"
DECISION_REF = "optimization/evaluation-rebuild-v4-qrels-approval.md#用户授权"
RELEASE_DECISION_REF = "optimization/action-catalog-naming-cleanup.md#改名与条目删除"


def rows(path):
    with jsonlines.open(path) as reader:
        return list(reader)


@pytest.fixture(scope="module")
def dataset():
    return load_for_evaluation(VERSION)


def test_rebuild_v4_inherits_frozen_files_and_original_qrels():
    for name in ("corpus.jsonl", "scenarios.jsonl"):
        assert (VERSION / name).read_bytes() == (PARENT / name).read_bytes()
    for asset in (PARENT / "assets").iterdir():
        assert (VERSION / "assets" / asset.name).read_bytes() == asset.read_bytes()
    original = (PARENT / "qrels.jsonl").read_bytes()
    assert (VERSION / "qrels.jsonl").read_bytes().startswith(original)
    assert rows(VERSION / "qrels.jsonl")[:1103] == rows(PARENT / "qrels.jsonl")


def test_rebuild_v4_complete_unique_grades_and_facts(dataset):
    assert dataset.review["ready"] is True
    assert len(dataset.queries) == 150
    assert len(dataset.qrels) == 3266
    keys = {(row["query_id"], row["corpus_id"]) for row in dataset.qrels}
    assert len(keys) == 3266
    added = dataset.qrels[1103:]
    assert len(added) == 2163
    assert len({row["query_id"] for row in added}) == 110
    assert Counter(row["grade"] for row in added) == {0: 1401, 1: 593, 2: 168, 3: 1}
    catalog = json.loads((BACKEND / "dataset/actions.agent.json").read_text(encoding="utf-8"))
    queries = {row["query_id"]: row for row in dataset.queries}
    for row in added:
        assert type(row["grade"]) is int and 0 <= row["grade"] <= 3
        assert queries[row["query_id"]]["query"] in row["rationale"]
        assert row["evidence"]["source_path"] == "backend/dataset/actions.agent.json"
        fact = resolve_pointer(catalog, row["evidence"]["json_pointer"])
        assert fact["id"] == row["corpus_id"]
        assert fact["name"] in row["rationale"]
        assert fact["name"] in row["evidence"]["excerpt"]
        assert all(step in row["evidence"]["excerpt"] for step in fact["steps"]["zh"])
        assert row["review"]["content_sha256"] == qrel_digest(row)
        assert row["review"]["decision_ref"] == DECISION_REF
        assert row["review"]["reviewer"] == "用户（委托Worker标注）"
        assert "实际逐条语义标注者为Worker" in row["review"]["comment"]


def test_rebuild_v4_preserves_query_business_and_updates_only_reviewed_scope(dataset):
    changed = {row["query_id"] for row in dataset.qrels[1103:]}
    grouped = {}
    for row in dataset.qrels:
        grouped.setdefault(row["query_id"], []).append(row)
    old_rows = rows(PARENT / "queries.jsonl")
    for old, new in zip(old_rows, dataset.queries, strict=True):
        assert {k: v for k, v in old.items() if k not in ("review", "judgement_scope")} == {
            k: v for k, v in new.items() if k not in ("review", "judgement_scope")
        }
        if new["query_id"] in changed:
            assert "四组Top20候选池汇集" in new["judgement_scope"]["checked_scope"]
            assert "未进行全语料穷尽新增审阅" in new["judgement_scope"]["omission_review"]
            assert new["review"]["content_sha256"] == query_digest(new, grouped[new["query_id"]])
            assert new["review"]["decision_ref"] == DECISION_REF
        else:
            # 未新增判定的查询，业务字段在上一段已与父版本逐字段比对；
            # 目录改名后继承判定的证据指针变化，判据摘要按当前内容重算。
            assert new["review"]["content_sha256"] == query_digest(new, grouped[new["query_id"]])


def test_rebuild_v4_explicit_scenario_quota_and_legacy_defaults(dataset):
    assert len(dataset.scenarios) == 40
    assert scenario_baseline("rebuild-v4") == (40, REBUILD_V3_SCENARIO_CATEGORIES)
    assert scenario_baseline("rebuild-v3") == (40, REBUILD_V3_SCENARIO_CATEGORIES)
    for version in ("rebuild-v1", "rebuild-v2", "unregistered-version"):
        assert scenario_baseline(version)[0] == 80
    assert dataset.manifest["quotas"]["scenario_categories"] == REBUILD_V3_SCENARIO_CATEGORIES


def test_rebuild_v4_manifest_and_delegation_record(dataset):
    manifest = dataset.manifest
    assert manifest["release"]["content_sha256"] == release_digest(manifest)
    assert manifest["release"]["decision_ref"] == RELEASE_DECISION_REF
    for ref in manifest["files"].values():
        assert hashlib.sha256((VERSION / ref["path"]).read_bytes()).hexdigest() == ref["sha256"]
    record = (BACKEND.parent / RELEASE_DECISION_REF.split("#")[0]).read_text(encoding="utf-8")
    for text in ("用户在本会话指示执行改名与删除", "全量同步重签", "语义未重新人工复核", "1689 自重推拉"):
        assert text in record


def test_rebuild_v4_fact_limit_decisions_are_query_specific(dataset):
    labels = {(row["query_id"], row["corpus_id"]): row for row in dataset.qrels}
    expected = {
        ("q-lookup-038", "0290"): 1,
        ("q-replacement-007", "0016"): 0,
        ("q-generation-013", "1290"): 0,
        ("q-generation-028", "0075"): 0,
        ("q-lookup-036", "0316"): 1,
        ("q-replacement-001", "0316"): 2,
        ("q-replacement-021", "0316"): 2,
    }
    for key, grade in expected.items():
        assert labels[key]["grade"] == grade
