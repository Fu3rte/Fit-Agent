from pathlib import Path

import jsonlines
from jsonschema import Draft202012Validator

VERSION_DIR = Path(__file__).resolve().parent
REPO = VERSION_DIR.parents[3]
TEXT = {"type": "string", "minLength": 1}
TEXT_LIST = {"type": "array", "minItems": 1, "items": TEXT, "uniqueItems": True}
CASE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "id", "type", "tags", "prompt", "context", "should_use_skill",
        "topic_paths", "expected", "critical_errors", "allowed_variation",
        "reference_answer_points", "reference_sources", "review",
    ],
    "properties": {
        "id": {"type": "string", "pattern": "^skills-v1-[0-9]{3}$"},
        "type": TEXT,
        "tags": TEXT_LIST,
        "prompt": TEXT,
        "context": {
            "type": "object",
            "additionalProperties": False,
            "required": ["synthetic", "content"],
            "properties": {"synthetic": {"const": True}, "content": TEXT},
        },
        "should_use_skill": {"type": "boolean"},
        "topic_paths": {"type": "array", "items": TEXT, "uniqueItems": True},
        "expected": {"type": "array", "minItems": 3, "maxItems": 5, "items": TEXT},
        "critical_errors": TEXT_LIST,
        "allowed_variation": TEXT_LIST,
        "reference_answer_points": TEXT_LIST,
        "reference_sources": TEXT_LIST,
        "review": {
            "type": "object",
            "additionalProperties": False,
            "required": ["status", "external_verified", "note"],
            "properties": {
                "status": {"const": "draft"},
                "external_verified": {"const": False},
                "note": TEXT,
            },
        },
    },
}


def test_cases_structure_distribution_and_local_sources():
    Draft202012Validator.check_schema(CASE_SCHEMA)
    validator = Draft202012Validator(CASE_SCHEMA)
    with jsonlines.open(VERSION_DIR / "cases.jsonl") as reader:
        cases = list(reader)

    assert len(cases) == 30
    assert [case["id"] for case in cases] == [f"skills-v1-{number:03d}" for number in range(1, 31)]
    assert sum(case["should_use_skill"] for case in cases) == 24
    assert sum(not case["should_use_skill"] for case in cases) == 6

    topics = set()
    for case in cases:
        validator.validate(case)
        assert "合成" in case["context"]["content"], case["id"]
        assert "training-theory" not in case["prompt"], case["id"]
        assert "references/" not in case["prompt"], case["id"]
        assert bool(case["topic_paths"]) == case["should_use_skill"], case["id"]
        for topic in case["topic_paths"]:
            assert topic.startswith("backend/skills/training-theory/references/"), case["id"]
            assert (REPO / topic).is_file(), (case["id"], topic)
            topics.add(Path(topic).name)
        for source in case["reference_sources"]:
            if source.startswith("backend/"):
                assert (REPO / source.split("#", 1)[0]).is_file(), (case["id"], source)
        if case["should_use_skill"]:
            assert any(source.startswith("backend/") for source in case["reference_sources"]), case["id"]

    reference_dir = REPO / "backend/skills/training-theory/references"
    assert topics == {path.name for path in reference_dir.glob("*.md")}
    by_id = {case["id"]: case for case in cases}
    assert "regression_55kg" in by_id["skills-v1-013"]["tags"]
    assert "regression_retraining" in by_id["skills-v1-015"]["tags"]
    assert "regression_endurance_scope" in by_id["skills-v1-022"]["tags"]
