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
    assert rows(VERSION / "qrels.jsonl")[:1104] == rows(PARENT / "qrels.jsonl")


def test_rebuild_v4_complete_unique_grades_and_facts(dataset):
    assert dataset.review["ready"] is True
    assert len(dataset.queries) == 150
    assert len(dataset.qrels) == 3272
    keys = {(row["query_id"], row["corpus_id"]) for row in dataset.qrels}
    assert len(keys) == 3272
    added = dataset.qrels[1104:]
    assert len(added) == 2168
    assert len({row["query_id"] for row in added}) == 112
    assert Counter(row["grade"] for row in added) == {0: 1402, 1: 594, 2: 171, 3: 1}
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
    changed = {row["query_id"] for row in dataset.qrels[1104:]}
    grouped = {}
    for row in dataset.qrels:
        grouped.setdefault(row["query_id"], []).append(row)
    old_rows = rows(PARENT / "queries.jsonl")
    old_lines = (PARENT / "queries.jsonl").read_bytes().splitlines()
    new_lines = (VERSION / "queries.jsonl").read_bytes().splitlines()
    for old, new, old_line, new_line in zip(old_rows, dataset.queries, old_lines, new_lines, strict=True):
        assert {k: v for k, v in old.items() if k not in ("review", "judgement_scope")} == {
            k: v for k, v in new.items() if k not in ("review", "judgement_scope")
        }
        if new["query_id"] in changed:
            assert "四组Top20候选池汇集" in new["judgement_scope"]["checked_scope"]
            assert "未进行全语料穷尽新增审阅" in new["judgement_scope"]["omission_review"]
            assert new["review"]["content_sha256"] == query_digest(new, grouped[new["query_id"]])
            assert new["review"]["decision_ref"] == DECISION_REF
        else:
            assert new_line == old_line


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
    assert manifest["release"]["decision_ref"] == DECISION_REF
    for ref in manifest["files"].values():
        assert hashlib.sha256((VERSION / ref["path"]).read_bytes()).hexdigest() == ref["sha256"]
    record = (BACKEND.parent / DECISION_REF.split("#")[0]).read_text(encoding="utf-8")
    for text in ("你派个worker补齐答案吧，随便修改", "授权所有改动", "用户没有逐条亲自审阅", "实际标注者为 **Worker**", "父版本：`rebuild-v3`"):
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
