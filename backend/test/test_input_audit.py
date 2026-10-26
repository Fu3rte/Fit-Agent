import json
from pathlib import Path

import pytest

from evaluation.dataset import load_for_evaluation, load_for_review
from evaluation.input_audit import audit_dataset, scan_text
from evaluation.review_report import render

BACKEND = Path(__file__).resolve().parents[1]
VERSION = BACKEND / "dataset" / "evaluation" / "rebuild-v1"


def catalog():
    return json.loads((BACKEND / "dataset" / "actions.agent.json").read_text(encoding="utf-8"))


def catalog_ids():
    return {row["id"] for row in catalog()}


def supplements():
    return json.loads((BACKEND / "dataset" / "action-supplements.published.json").read_text(encoding="utf-8"))


def resolve_expression(expression):
    # 按目录名称与已批准别名反查动作 ID，用于确认修订后的表述唯一指向原动作。
    ids = {row["id"] for row in catalog() if row["name"] == expression}
    for entry in supplements()["entries"]:
        values = []
        if entry.get("english_name"):
            values.append(entry["english_name"]["text"])
        values.extend(alias["text"] for alias in entry.get("aliases") or [])
        if expression in values:
            ids.add(entry["id"])
    return ids


def field_text(scenario, path):
    if path == "input.message":
        return scenario["input"]["message"]
    if path.startswith("reference."):
        checkpoint_id = path.split(".")[1]
        return next(item for item in scenario["reference"] if item["checkpoint_id"] == checkpoint_id)["answer"]
    event_id = path.split(".")[1]
    return next(event for event in scenario["events"] if event["event_id"] == event_id)["content"]


def test_scan_flags_catalog_id_in_user_text():
    findings = scan_text("找站姿提踵，但排除 1373 自重站姿提踵", catalog_ids(), "t", "query")
    assert [(item.kind, item.token) for item in findings] == [("catalog_id", "1373")]


def test_scan_ignores_dates_weights_and_set_counts():
    text = "2026-10-18 训练：杠铃卧推 1000 kg，第 1000 组 8 次，组间 1200 秒，2026 年目标不变"
    assert scan_text(text, catalog_ids(), "t", "query") == []


def test_scan_flags_long_digits_and_identifier_wording():
    findings = scan_text("请按动作 ID 12345 和 exercise_id 输出", catalog_ids(), "t", "query")
    kinds = {(item.kind, item.token) for item in findings}
    assert ("long_digit", "12345") in kinds
    assert ("identifier_wording", "动作 ID") in kinds
    assert ("identifier_wording", "exercise_id") in kinds


def test_scan_flags_stale_id_inside_catalog_range():
    findings = scan_text("换成 0004 那个动作", catalog_ids(), "t", "query")
    assert [(item.kind, item.token) for item in findings] == [("catalog_range", "0004")]


def test_scan_requires_catalog_ids():
    with pytest.raises(ValueError):
        scan_text("1373", set(), "t", "query")


REVISIONS = [
    ("q-lookup-050", "query", "找站姿提踵，但排除自重站姿提踵", "自重站姿提踵", "1373"),
    ("q-lookup-059", "query", "除了手摇健身车，还有别的上肢功率车动作吗", "手摇健身车", "2139"),
    ("q-generation-006", "query", "哑铃练二头，今天不做哑铃锤式弯举", "哑铃锤式弯举", "0313"),
    ("q-generation-007", "query", "背日想选绳索下拉，但画像里已经禁用了这个动作", "绳索下拉", "0198"),
    ("q-generation-008", "query", "肩日想做哑铃侧平举，但长期不做这个动作", "哑铃侧平举", "0334"),
    ("q-generation-026", "query", "在家背日暂时不做引体向上", "引体向上", "0652"),
    ("q-replacement-008", "query", "哑铃侧平举不适合，找其他侧平举", "哑铃侧平举", "0334"),
    ("q-replacement-011", "query", "卧推换胸部飞鸟，但哑铃飞鸟已禁用", "哑铃飞鸟", "0308"),
    ("q-replacement-017", "query", "不用哑铃单臂划船，哑铃背部还有哪些划船", "哑铃单臂划船", "0292"),
    ("q-replacement-020", "query", "中立握引体向上换其他自重引体", "中立握引体向上", "0651"),
    ("s-action-verification-05", "input.message", "找站姿提踵的动作，这一次先把自重站姿提踵排除掉", "自重站姿提踵", "1373"),
    (
        "s-action-verification-05",
        "events.e01.content",
        "找站姿提踵的动作，这一次先把自重站姿提踵排除掉",
        "自重站姿提踵",
        "1373",
    ),
    (
        "s-profile-update-08",
        "reference.cp1.answer",
        "跨会话读取的已保存画像是：不可用器械为杠铃和绳索，禁用动作含 0086 杠铃坐姿颈后军事推举；训练环境居家，目标增肌，每周三练。这些限制在生成和调整计划时生效。",
        "杠铃坐姿颈后军事推举",
        "0086",
    ),
    (
        "s-replacement-constraints-07",
        "reference.cp1.answer",
        "把哑铃俯身划船替换为哑铃单臂俯身划船 4 组 10 次，每只 22 kg；0293 哑铃俯身划船在画像里已禁用，不会再出现。其余动作与参数不变。确认后点击保存。",
        "哑铃俯身划船",
        "0293",
    ),
]


def test_revised_inputs_use_unique_catalog_expressions():
    dataset = load_for_review(VERSION)
    queries = {row["query_id"]: row for row in dataset.queries}
    scenarios = {row["scenario_id"]: row for row in dataset.scenarios}
    for record_id, path, expected_text, expression, expected_id in REVISIONS:
        if record_id in queries:
            actual = queries[record_id]["query"]
        else:
            actual = field_text(scenarios[record_id], path)
        assert actual == expected_text
        assert expression in actual
        assert resolve_expression(expression) == {expected_id}


def test_draft_dataset_user_inputs_have_no_catalog_ids():
    dataset = load_for_review(VERSION)
    report = audit_dataset(dataset)
    expected_events = sum(
        1
        for scenario in dataset.scenarios
        for event in scenario["events"]
        if event["type"] == "message" and event["content"]
    )
    assert report.findings == ()
    assert report.coverage["queries"] == len(dataset.queries) == 150
    assert report.coverage["scenario_messages"] == len(dataset.scenarios) == 80
    assert report.coverage["event_messages"] == expected_events
    assert report.coverage["assets"] == len(dataset.assets) == 8
    assert report.catalog_size == 1321


def test_review_material_covers_approved_version():
    dataset = load_for_evaluation(VERSION)
    document = render(VERSION, None)
    assert document.count("<article id=") == len(dataset.queries) + len(dataset.scenarios)
    assert all(query["query_id"] in document for query in dataset.queries)
    assert all(scenario["scenario_id"] in document for scenario in dataset.scenarios)
    assert "<td>命中项</td><td>0</td>" in document
    assert dataset.review["ready"] is True
