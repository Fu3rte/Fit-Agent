import json
import os
import shutil

import pytest

from app.application.business.retrieval_index import sha256
from evaluation.dataset import DatasetError, load_for_evaluation, load_for_review
from test.evaluation_fixtures import build_dataset


def manifest(version_dir):
    return json.loads((version_dir / "manifest.json").read_text(encoding="utf-8"))


def save_manifest(version_dir, document):
    (version_dir / "manifest.json").write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def rows(version_dir, name):
    return [json.loads(line) for line in (version_dir / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()]


def write_rows(version_dir, name, items):
    (version_dir / f"{name}.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n" for item in items),
        encoding="utf-8",
    )
    rebind(version_dir, name)


def rebind(version_dir, name):
    document = manifest(version_dir)
    document["files"][name]["sha256"] = sha256(version_dir / f"{name}.jsonl")
    save_manifest(version_dir, document)


def approved_dataset(tmp_path):
    version_dir = build_dataset(tmp_path)
    load_for_evaluation(version_dir)
    return version_dir


def test_review_and_formal_entries_accept_full_dataset(tmp_path):
    version_dir = approved_dataset(tmp_path)
    review = load_for_review(version_dir)
    formal = load_for_evaluation(version_dir)
    assert review.review["ready"] is True
    assert formal.review["ready"] is True
    assert review.review["counts"] == {"corpus": 1321, "queries": 150, "qrels": 390, "scenarios": 80, "assets": 1}
    assert review.review["quotas"]["purposes"] == {"lookup": 60, "generation": 45, "replacement": 45}
    assert review.review["quotas"]["expect_empty"] == {"lookup": 10, "generation": 10, "replacement": 10}
    assert set(review.review["quotas"]["scenario_categories"].values()) == {8}
    assert all(item["corpus_id"] in {row["corpus_id"] for row in review.corpus} for item in review.qrels)
    assert "0001" in {row["corpus_id"] for row in review.corpus}
    assert review.corpus_sha256 == formal.corpus_sha256


def test_draft_dataset_is_reviewable_but_not_formal(tmp_path):
    version_dir = build_dataset(tmp_path, approve=False)
    review = load_for_review(version_dir)
    assert review.review["ready"] is False
    assert any("审阅状态为 draft" in issue for issue in review.review["issues"])
    with pytest.raises(DatasetError, match="未就绪"):
        load_for_evaluation(version_dir)


def test_rejects_empty_line_and_bom(tmp_path):
    version_dir = approved_dataset(tmp_path)
    path = version_dir / "queries.jsonl"
    original = path.read_text(encoding="utf-8")
    path.write_text("\n" + original, encoding="utf-8")
    rebind(version_dir, "queries")
    with pytest.raises(DatasetError, match="空行"):
        load_for_review(version_dir)
    path.write_bytes(b"\xef\xbb\xbf" + original.encode("utf-8"))
    rebind(version_dir, "queries")
    with pytest.raises(DatasetError, match="BOM"):
        load_for_review(version_dir)


def test_rejects_non_object_record_and_duplicate_keys(tmp_path):
    version_dir = approved_dataset(tmp_path)
    path = version_dir / "queries.jsonl"
    original = path.read_text(encoding="utf-8")
    path.write_text("[]\n" + original, encoding="utf-8")
    rebind(version_dir, "queries")
    with pytest.raises(DatasetError, match="JSON 对象"):
        load_for_review(version_dir)
    first, rest = original.splitlines(keepends=True)[0], "".join(original.splitlines(keepends=True)[1:])
    duplicate = first.replace('{"query_id":', '{"query_id":"dup","query_id":', 1)
    path.write_text(duplicate + rest, encoding="utf-8")
    rebind(version_dir, "queries")
    with pytest.raises(DatasetError, match="重复 JSON 键"):
        load_for_review(version_dir)


def test_rejects_non_finite_and_unknown_fields(tmp_path):
    version_dir = approved_dataset(tmp_path)
    path = version_dir / "queries.jsonl"
    original = path.read_text(encoding="utf-8")
    first, rest = original.splitlines(keepends=True)[0], "".join(original.splitlines(keepends=True)[1:])
    path.write_text(first.replace('"expect_empty":true', '"expect_empty":NaN', 1) + rest, encoding="utf-8")
    rebind(version_dir, "queries")
    with pytest.raises(DatasetError, match="非有限数"):
        load_for_review(version_dir)
    extra = first[:-2] + ',"extra":1}\n'
    path.write_text(extra + rest, encoding="utf-8")
    rebind(version_dir, "queries")
    with pytest.raises(DatasetError, match="Schema 校验失败"):
        load_for_review(version_dir)


def test_rejects_duplicate_ids_and_missing_files(tmp_path):
    version_dir = approved_dataset(tmp_path)
    items = rows(version_dir, "queries")
    items.insert(1, items[0])
    write_rows(version_dir, "queries", items)
    document = manifest(version_dir)
    document["counts"]["queries"] = len(items)
    save_manifest(version_dir, document)
    with pytest.raises(DatasetError, match="query_id 重复"):
        load_for_review(version_dir)
    (version_dir / "qrels.jsonl").unlink()
    with pytest.raises(DatasetError, match="声明文件缺失"):
        load_for_review(version_dir)


def test_rejects_illegal_paths_and_extra_files(tmp_path):
    version_dir = approved_dataset(tmp_path)
    document = manifest(version_dir)
    document["files"]["queries"]["path"] = "../queries.jsonl"
    save_manifest(version_dir, document)
    with pytest.raises(DatasetError, match="Schema 校验失败"):
        load_for_review(version_dir)
    document = manifest(version_dir)
    document["files"]["queries"]["path"] = "queries.jsonl"
    save_manifest(version_dir, document)
    (version_dir / "extra.txt").write_text("x", encoding="utf-8")
    with pytest.raises(DatasetError, match="未声明文件"):
        load_for_review(version_dir)


def test_rejects_catalog_count_drift(tmp_path):
    version_dir = approved_dataset(tmp_path)
    items = rows(version_dir, "corpus")[:10]
    write_rows(version_dir, "corpus", items)
    with pytest.raises(DatasetError, match="数量"):
        load_for_review(version_dir)


def test_rejects_positive_label_outside_hard_conditions(tmp_path):
    version_dir = approved_dataset(tmp_path)
    queries = rows(version_dir, "queries")
    qrels = rows(version_dir, "qrels")
    query = next(item for item in queries if not item["expect_empty"])
    positive = next(item for item in qrels if item["query_id"] == query["query_id"] and item["grade"] >= 1)
    query["conditions"]["excluded_ids"] = [positive["corpus_id"]]
    write_rows(version_dir, "queries", queries)
    with pytest.raises(DatasetError, match="硬条件"):
        load_for_review(version_dir)


def test_rejects_empty_expectation_contradiction(tmp_path):
    version_dir = approved_dataset(tmp_path)
    queries = rows(version_dir, "queries")
    qrels = rows(version_dir, "qrels")
    query = next(item for item in queries if not item["expect_empty"])
    qrels = [item for item in qrels if item["query_id"] != query["query_id"]]
    write_rows(version_dir, "qrels", qrels)
    document = manifest(version_dir)
    document["counts"]["qrels"] = len(qrels)
    save_manifest(version_dir, document)
    with pytest.raises(DatasetError, match="至少需要一个 grade>=1"):
        load_for_review(version_dir)


def test_rejects_bad_evidence_pointer(tmp_path):
    version_dir = approved_dataset(tmp_path)
    qrels = rows(version_dir, "qrels")
    qrels[0]["evidence"]["json_pointer"] = "/999999"
    write_rows(version_dir, "qrels", qrels)
    with pytest.raises(DatasetError, match="json_pointer"):
        load_for_review(version_dir)


def test_rejects_event_order_and_checkpoint_references(tmp_path):
    version_dir = approved_dataset(tmp_path)
    scenarios = rows(version_dir, "scenarios")
    scenarios[0]["events"][1]["source_event_id"] = "e02"
    write_rows(version_dir, "scenarios", scenarios)
    with pytest.raises(DatasetError, match="更早事件"):
        load_for_review(version_dir)
    scenarios = rows(version_dir, "scenarios")
    scenarios[0]["events"][1]["source_event_id"] = "e01"
    scenarios[0]["rubric"][0]["event_id"] = "e02"
    write_rows(version_dir, "scenarios", scenarios)
    with pytest.raises(DatasetError, match="消息事件"):
        load_for_review(version_dir)
    scenarios = rows(version_dir, "scenarios")
    scenarios[0]["rubric"][0]["event_id"] = "e01"
    scenarios[0]["metric_applicability"].pop()
    write_rows(version_dir, "scenarios", scenarios)
    with pytest.raises(DatasetError, match="三项指标"):
        load_for_review(version_dir)


def business_metrics(checkpoint_id, event_id, *, generation=False):
    return [
        {"checkpoint_id": checkpoint_id, "event_id": event_id, "metric": "faithfulness",
         "applicable": generation, "reason": None if generation else "纯状态检查"},
        {"checkpoint_id": checkpoint_id, "event_id": event_id, "metric": "factual_correctness",
         "applicable": generation, "reason": None if generation else "纯状态检查"},
        {"checkpoint_id": checkpoint_id, "event_id": event_id, "metric": "deterministic_rules",
         "applicable": True, "reason": None},
    ]


def test_scenario_accepts_deterministic_checkpoint_after_confirmation(tmp_path):
    version_dir = approved_dataset(tmp_path)
    scenarios = rows(version_dir, "scenarios")
    scenario = scenarios[0]
    assert scenario["events"][1]["type"] == "confirm"
    assert any(item["kind"] == "no_saved_state" and item["event_id"] == "e01" for item in scenario["assertions"])
    scenario["assertions"].append(
        {"checkpoint_id": "cp2", "event_id": "e02", "kind": "save_authorization", "proposal_ref": "profile-proposal-e01", "expected_saved": True}
    )
    scenario["metric_applicability"].extend(business_metrics("cp2", "e02"))
    write_rows(version_dir, "scenarios", scenarios)
    review = load_for_review(version_dir)
    assert len(review.scenarios[0]["assertions"]) == 2
    assert review.review["ready"] is False


def test_scenario_rejects_generation_metrics_on_business_checkpoint(tmp_path):
    version_dir = approved_dataset(tmp_path)
    scenarios = rows(version_dir, "scenarios")
    scenario = scenarios[0]
    scenario["assertions"].append(
        {"checkpoint_id": "cp2", "event_id": "e02", "kind": "save_authorization", "proposal_ref": "profile-proposal-e01", "expected_saved": True}
    )
    scenario["metric_applicability"].extend(business_metrics("cp2", "e02", generation=True))
    write_rows(version_dir, "scenarios", scenarios)
    with pytest.raises(DatasetError, match="参考回答"):
        load_for_review(version_dir)


def test_scenario_accepts_pure_business_checkpoint_without_reference(tmp_path):
    version_dir = approved_dataset(tmp_path)
    scenarios = rows(version_dir, "scenarios")
    scenario = scenarios[0]
    scenario["reference"] = []
    scenario["rubric"] = []
    scenario["assertions"] = [
        {"checkpoint_id": "cp2", "event_id": "e02", "kind": "save_authorization", "proposal_ref": "profile-proposal-e01", "expected_saved": True}
    ]
    scenario["metric_applicability"] = business_metrics("cp2", "e02")
    write_rows(version_dir, "scenarios", scenarios)
    review = load_for_review(version_dir)
    assert review.scenarios[0]["reference"] == []
    assert review.review["ready"] is False


def test_rejects_prerecorded_answer_as_message_event(tmp_path):
    version_dir = approved_dataset(tmp_path)
    scenarios = rows(version_dir, "scenarios")
    scenario = scenarios[0]
    answer = "这份是完整的参考回答，包含动作名称、组次与重量口径"
    scenario["reference"][0]["answer"] = answer
    scenario["events"][0]["content"] = answer
    write_rows(version_dir, "scenarios", scenarios)
    with pytest.raises(DatasetError, match="预写助理回答"):
        load_for_review(version_dir)


def test_rejects_message_checkpoint_claiming_saved(tmp_path):
    version_dir = approved_dataset(tmp_path)
    scenarios = rows(version_dir, "scenarios")
    scenarios[0]["assertions"] = [
        {"checkpoint_id": "cp1", "event_id": "e01", "kind": "save_authorization",
         "proposal_ref": "profile-proposal-e01", "expected_saved": True}
    ]
    write_rows(version_dir, "scenarios", scenarios)
    with pytest.raises(DatasetError, match="零提交"):
        load_for_review(version_dir)


def test_rejects_save_success_outside_confirm_event(tmp_path):
    version_dir = approved_dataset(tmp_path)
    scenarios = rows(version_dir, "scenarios")
    scenario = scenarios[0]
    scenario["events"][1]["type"] = "cancel"
    scenario["events"][1]["target"]["display_ref"] = None
    scenario["events"][1]["target"]["confirmation_ref"] = None
    scenario["assertions"] = [
        {"checkpoint_id": "cp1", "event_id": "e02", "kind": "save_authorization",
         "proposal_ref": "profile-proposal-e01", "expected_saved": True}
    ]
    scenario["reference"] = []
    scenario["rubric"] = []
    scenario["metric_applicability"] = business_metrics("cp1", "e02")
    write_rows(version_dir, "scenarios", scenarios)
    with pytest.raises(DatasetError, match="确认事件"):
        load_for_review(version_dir)


def test_rejects_save_assertion_target_mismatch(tmp_path):
    version_dir = approved_dataset(tmp_path)
    scenarios = rows(version_dir, "scenarios")
    scenario = scenarios[0]
    scenario["events"][1]["target"]["business_kind"] = "plan"
    scenario["events"][1]["target"]["proposal_ref"] = "plan-proposal-e01"
    scenario["events"][1]["target"]["display_ref"] = "plan-display-e01"
    scenario["events"][1]["target"]["confirmation_ref"] = "plan-confirm-e01"
    scenario["assertions"] = [
        {"checkpoint_id": "cp1", "event_id": "e02", "kind": "save_authorization",
         "proposal_ref": "profile-proposal-e01", "expected_saved": True}
    ]
    scenario["reference"] = []
    scenario["rubric"] = []
    scenario["metric_applicability"] = business_metrics("cp1", "e02")
    write_rows(version_dir, "scenarios", scenarios)
    with pytest.raises(DatasetError, match="目标不一致"):
        load_for_review(version_dir)


def test_rejects_saved_transition_on_message_event(tmp_path):
    version_dir = approved_dataset(tmp_path)
    scenarios = rows(version_dir, "scenarios")
    scenarios[0]["assertions"] = [
        {"checkpoint_id": "cp1", "event_id": "e01", "kind": "state_transition",
         "business_kind": "profile", "from_status": "pending", "to_status": "saved"}
    ]
    write_rows(version_dir, "scenarios", scenarios)
    with pytest.raises(DatasetError, match="确认事件"):
        load_for_review(version_dir)


def test_rejects_dangling_event_reference(tmp_path):
    version_dir = approved_dataset(tmp_path)
    scenarios = rows(version_dir, "scenarios")
    scenarios[0]["reference"][0]["event_id"] = "e99"
    write_rows(version_dir, "scenarios", scenarios)
    with pytest.raises(DatasetError, match="未知事件"):
        load_for_review(version_dir)


def test_message_state_transition_requires_unique_later_cancel_target(tmp_path):
    version_dir = approved_dataset(tmp_path)
    scenarios = rows(version_dir, "scenarios")
    scenario = scenarios[0]
    scenario["events"] = [
        scenario["events"][0],
        {
            "event_id": "e02",
            "type": "message",
            "source_event_id": None,
            "content": "取消刚才的提案",
            "attachments": [],
            "target": None,
        },
        {
            "event_id": "e03",
            "type": "cancel",
            "source_event_id": "e01",
            "content": None,
            "attachments": [],
            "target": {
                "business_kind": "profile",
                "proposal_ref": "profile-proposal-e01",
                "display_ref": None,
                "confirmation_ref": None,
            },
        },
    ]
    scenario["assertions"] = [
        {
            "checkpoint_id": "cp1",
            "event_id": "e01",
            "kind": "no_saved_state",
            "business_kind": "profile",
        },
        {
            "checkpoint_id": "cp2",
            "event_id": "e02",
            "kind": "state_transition",
            "business_kind": "profile",
            "from_status": "pending",
            "to_status": "invalidated",
        },
        {
            "checkpoint_id": "cp3",
            "event_id": "e03",
            "kind": "state_transition",
            "business_kind": "profile",
            "from_status": "invalidated",
            "to_status": "invalidated",
        },
    ]
    scenario["metric_applicability"] = [
        {"checkpoint_id": "cp1", "event_id": "e01", "metric": "faithfulness",
         "applicable": True, "reason": None},
        {"checkpoint_id": "cp1", "event_id": "e01", "metric": "factual_correctness",
         "applicable": True, "reason": None},
        {"checkpoint_id": "cp1", "event_id": "e01", "metric": "deterministic_rules",
         "applicable": True, "reason": None},
        *business_metrics("cp2", "e02"),
        *business_metrics("cp3", "e03"),
    ]
    write_rows(version_dir, "scenarios", scenarios)
    review = load_for_review(version_dir)
    assert len(review.scenarios[0]["assertions"]) == 3
    assert review.review["ready"] is False

    scenarios = rows(version_dir, "scenarios")
    scenario = scenarios[0]
    scenario["events"] = [event for event in scenario["events"] if event["event_id"] != "e03"]
    scenario["assertions"] = [
        assertion for assertion in scenario["assertions"] if assertion["event_id"] != "e03"
    ]
    scenario["metric_applicability"] = [
        row for row in scenario["metric_applicability"] if row["event_id"] != "e03"
    ]
    write_rows(version_dir, "scenarios", scenarios)
    with pytest.raises(DatasetError, match="后续取消事件目标"):
        load_for_review(version_dir)


def test_saved_state_assertion_is_accepted(tmp_path):
    version_dir = approved_dataset(tmp_path)
    scenarios = rows(version_dir, "scenarios")
    scenarios[0]["assertions"] = [
        {"checkpoint_id": "cp2", "event_id": "e02", "kind": "saved_state", "business_kind": "profile"}
    ]
    scenarios[0]["reference"] = []
    scenarios[0]["rubric"] = []
    scenarios[0]["metric_applicability"] = business_metrics("cp2", "e02")
    write_rows(version_dir, "scenarios", scenarios)
    review = load_for_review(version_dir)
    assert review.scenarios[0]["assertions"][0]["kind"] == "saved_state"


def test_jsonl_unicode_separators_remain_inside_string(tmp_path):
    version_dir = approved_dataset(tmp_path)
    queries = rows(version_dir, "queries")
    text = "含\u0085、\u2028、\u2029的请求"
    queries[0]["query"] = text
    write_rows(version_dir, "queries", queries)
    review = load_for_review(version_dir)
    assert review.queries[0]["query"] == text


def test_rejects_symlinked_asset_parent(tmp_path):
    version_dir = approved_dataset(tmp_path)
    assets = version_dir / "assets"
    data = (assets / "plan-note.txt").read_bytes()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "plan-note.txt").write_bytes(data)
    shutil.rmtree(assets)
    try:
        os.symlink(outside, assets, target_is_directory=True)
    except OSError:
        pytest.skip("当前平台不允许创建目录符号链接")
    with pytest.raises(DatasetError, match="符号链接"):
        load_for_review(version_dir)


def test_content_and_asset_changes_invalidate_approval(tmp_path):
    version_dir = approved_dataset(tmp_path)
    queries = rows(version_dir, "queries")
    queries[0]["query"] += "（内容变化）"
    write_rows(version_dir, "queries", queries)
    review = load_for_review(version_dir)
    assert review.review["ready"] is False
    assert any("批准内容哈希" in issue for issue in review.review["issues"])
    with pytest.raises(DatasetError, match="未就绪"):
        load_for_evaluation(version_dir)
    version_dir = approved_dataset(tmp_path / "asset")
    asset = version_dir / "assets" / "plan-note.txt"
    asset.write_text("变化后的附件", encoding="utf-8")
    document = manifest(version_dir)
    document["assets"][0]["sha256"] = sha256(asset)
    save_manifest(version_dir, document)
    with pytest.raises(DatasetError, match="未就绪"):
        load_for_evaluation(version_dir)


def test_source_drift_is_rejected(tmp_path):
    version_dir = approved_dataset(tmp_path)
    document = manifest(version_dir)
    document["source"]["catalog"]["sha256"] = "0" * 64
    save_manifest(version_dir, document)
    with pytest.raises(DatasetError, match="来源"):
        load_for_review(version_dir)


def test_schema_hash_and_release_gates(tmp_path):
    version_dir = approved_dataset(tmp_path)
    document = manifest(version_dir)
    document["schemas"]["queries"]["sha256"] = "0" * 64
    save_manifest(version_dir, document)
    with pytest.raises(DatasetError, match="Schema 哈希不符"):
        load_for_review(version_dir)
    version_dir = approved_dataset(tmp_path / "release")
    document = manifest(version_dir)
    document["release"]["reviewer_type"] = "assistant"
    save_manifest(version_dir, document)
    with pytest.raises(DatasetError, match="审阅者必须为用户"):
        load_for_evaluation(version_dir)
    document = manifest(version_dir)
    document["release"]["reviewer_type"] = "user"
    document["release"]["content_sha256"] = "0" * 64
    save_manifest(version_dir, document)
    with pytest.raises(DatasetError, match="内容摘要"):
        load_for_evaluation(version_dir)


def test_deferred_and_incomplete_judgement_are_rejected(tmp_path):
    version_dir = approved_dataset(tmp_path)
    queries = rows(version_dir, "queries")
    queries[0]["review"]["status"] = "deferred"
    write_rows(version_dir, "queries", queries)
    with pytest.raises(DatasetError, match="deferred"):
        load_for_evaluation(version_dir)
    version_dir = approved_dataset(tmp_path / "judgement")
    queries = rows(version_dir, "queries")
    queries[0]["judgement_scope"]["complete"] = False
    write_rows(version_dir, "queries", queries)
    with pytest.raises(DatasetError, match="judgement_scope.complete"):
        load_for_evaluation(version_dir)


def test_review_reports_incomplete_quota_without_lowering_formal_gate(tmp_path):
    version_dir = approved_dataset(tmp_path)
    queries = rows(version_dir, "queries")
    qrels = rows(version_dir, "qrels")
    removed = queries[-1]["query_id"]
    queries = queries[:-1]
    qrels = [item for item in qrels if item["query_id"] != removed]
    write_rows(version_dir, "queries", queries)
    write_rows(version_dir, "qrels", qrels)
    document = manifest(version_dir)
    document["counts"]["queries"] = len(queries)
    document["counts"]["qrels"] = len(qrels)
    save_manifest(version_dir, document)
    review = load_for_review(version_dir)
    assert review.review["ready"] is False
    assert any("正式要求 150" in issue for issue in review.review["issues"])
    with pytest.raises(DatasetError, match="未就绪"):
        load_for_evaluation(version_dir)


def test_grade_zero_is_reviewed_negative_but_unknown_is_flagged(tmp_path):
    version_dir = approved_dataset(tmp_path)
    dataset = load_for_evaluation(version_dir)
    empty_query = next(item for item in dataset.queries if item["expect_empty"])
    negative = next(item for item in dataset.qrels if item["query_id"] == empty_query["query_id"] and item["grade"] == 0)
    assert dataset.unreviewed_candidates({empty_query["query_id"]: [negative["corpus_id"]]}) == {}
    unknown = next(iter(item for item in dataset.corpus if item["corpus_id"] != negative["corpus_id"]))["corpus_id"]
    assert dataset.unreviewed_candidates({empty_query["query_id"]: [unknown]}) == {empty_query["query_id"]: [unknown]}


def test_scoring_inputs_exclude_reviewed_negatives(tmp_path):
    version_dir = approved_dataset(tmp_path)
    dataset = load_for_evaluation(version_dir)
    by_query = {item["query_id"]: item for item in dataset.queries}
    for item in dataset.scoring_queries():
        query = by_query[item["id"]]
        expected = [qrel for qrel in dataset.qrels if qrel["query_id"] == item["id"] and qrel["grade"] >= 1]
        assert len(item["labels"]) == len(expected)
        assert all(label["grade"] >= 1 for label in item["labels"])
        assert item["tags"] == query["tags"]
        assert item["expect_empty"] == query["expect_empty"]


def test_dataset_files_use_lf_line_endings(tmp_path):
    version_dir = approved_dataset(tmp_path)
    for path in sorted(version_dir.rglob("*")):
        if path.is_file():
            assert b"\r" not in path.read_bytes(), path.name
    load_for_review(version_dir)


def test_pure_attachment_message_boundary(tmp_path):
    version_dir = approved_dataset(tmp_path)
    scenarios = rows(version_dir, "scenarios")
    scenarios[0]["input"]["message"] = ""
    scenarios[0]["input"]["attachments"] = ["plan-note"]
    scenarios[0]["events"][0]["content"] = ""
    scenarios[0]["events"][0]["attachments"] = ["plan-note"]
    write_rows(version_dir, "scenarios", scenarios)
    review = load_for_review(version_dir)
    assert review.review["counts"]["scenarios"] == 80
    scenarios[0]["input"]["attachments"] = []
    write_rows(version_dir, "scenarios", scenarios)
    with pytest.raises(DatasetError, match="Schema 校验失败"):
        load_for_review(version_dir)
    scenarios[0]["input"]["attachments"] = ["plan-note"]
    scenarios[0]["events"][0]["content"] = None
    write_rows(version_dir, "scenarios", scenarios)
    with pytest.raises(DatasetError, match="Schema 校验失败"):
        load_for_review(version_dir)


def test_state_unchanged_assertion_kinds(tmp_path):
    version_dir = approved_dataset(tmp_path)
    scenarios = rows(version_dir, "scenarios")
    scenarios[0]["assertions"][0] = {
        "checkpoint_id": "cp1",
        "event_id": "e01",
        "kind": "state_unchanged",
        "business_kind": "all",
    }
    write_rows(version_dir, "scenarios", scenarios)
    review = load_for_review(version_dir)
    assert review.review["counts"]["scenarios"] == 80
    scenarios[0]["assertions"][0]["business_kind"] = "session"
    write_rows(version_dir, "scenarios", scenarios)
    with pytest.raises(DatasetError, match="Schema 校验失败"):
        load_for_review(version_dir)


def test_saved_data_unchanged_assertion_kinds(tmp_path):
    version_dir = approved_dataset(tmp_path)
    scenarios = rows(version_dir, "scenarios")
    scenarios[0]["assertions"][0] = {
        "checkpoint_id": "cp1",
        "event_id": "e01",
        "kind": "saved_data_unchanged",
        "business_kind": "all",
    }
    write_rows(version_dir, "scenarios", scenarios)
    review = load_for_review(version_dir)
    assert review.scenarios[0]["assertions"][0]["kind"] == "saved_data_unchanged"
    scenarios[0]["assertions"][0]["business_kind"] = "session"
    write_rows(version_dir, "scenarios", scenarios)
    with pytest.raises(DatasetError, match="Schema 校验失败"):
        load_for_review(version_dir)


def test_no_saved_state_rejects_existing_records(tmp_path):
    version_dir = approved_dataset(tmp_path)
    scenarios = rows(version_dir, "scenarios")
    scenarios[0]["initial_state"]["profile"] = {
        "goal": "增肌",
        "experience": "新手",
        "environment": "健身房",
        "availability": "每周三练",
        "health_notes": None,
        "movement_restrictions": None,
        "unavailable_equipment": None,
        "forbidden_exercise_ids": None,
    }
    write_rows(version_dir, "scenarios", scenarios)
    with pytest.raises(DatasetError, match="已保存记录"):
        load_for_review(version_dir)
