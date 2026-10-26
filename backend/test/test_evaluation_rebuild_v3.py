import hashlib
import json
import shutil
import sys
from pathlib import Path

import jsonlines
import pytest

from evaluation import decision_feedback as df
from evaluation import generation_upload as gu
from evaluation import langfuse_scenarios as ls
from evaluation.dataset import (
    BACKEND,
    REBUILD_V3_SCENARIO_CATEGORIES,
    load_for_evaluation,
    load_for_review,
    release_digest,
    scenario_baseline,
)
from evaluation.scenarios import (
    DEFAULT_VERSION_DIR,
    _parse_args,
    build_execution_plan,
    scenario_plan,
    select_scenarios,
)

DATA_ROOT = BACKEND / "dataset" / "evaluation"
V1 = DATA_ROOT / "rebuild-v1"
V2 = DATA_ROOT / "rebuild-v2"
V3 = DATA_ROOT / "rebuild-v3"
FIXED_CANCELLATION_SCENARIOS = (
    "s-confirmation-cancellation-state-05",
    "s-confirmation-cancellation-state-07",
    "s-confirmation-cancellation-state-08",
)
RETAINED = (
    [f"s-action-verification-{i:02d}" for i in range(1, 9)]
    + [f"s-profile-update-{i:02d}" for i in (1, 3, 4, 5, 8)]
    + [f"s-plan-import-file-{i:02d}" for i in (2, 4, 6, 8)]
    + [f"s-incomplete-plan-structure-{i:02d}" for i in (1, 6)]
    + [f"s-plan-generation-{i:02d}" for i in (1, 4, 5, 7)]
    + [f"s-replacement-constraints-{i:02d}" for i in (1, 2, 5)]
    + [f"s-performance-adjustment-{i:02d}" for i in (3, 5, 7)]
    + [f"s-workout-logging-query-{i:02d}" for i in (2, 4, 5)]
    + [f"s-confirmation-cancellation-state-{i:02d}" for i in (3, 4, 5, 7, 8)]
    + [f"s-clarification-authorization-medical-risk-{i:02d}" for i in (2, 4, 7)]
)


@pytest.fixture(scope="module")
def dataset():
    return load_for_evaluation(V3)


def _read_jsonl(path: Path) -> list[dict]:
    with jsonlines.open(path) as reader:
        return list(reader)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with jsonlines.open(path, mode="w") as writer:
        writer.write_all(rows)


def _rewrite_manifest(version_dir: Path, mutate) -> None:
    path = version_dir / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    mutate(manifest)
    manifest["files"]["scenarios"]["sha256"] = hashlib.sha256(
        (version_dir / "scenarios.jsonl").read_bytes()
    ).hexdigest()
    manifest["release"]["content_sha256"] = release_digest(manifest)
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _copy_version(tmp_path: Path) -> Path:
    target = tmp_path / "rebuild-v3"
    shutil.copytree(V3, target)
    return target


def test_default_entry_points_use_rebuild_v3():
    assert DEFAULT_VERSION_DIR == V3
    assert _parse_args([]).version_dir == V3
    assert df._parse_args(["check", "--source", "x"]).dataset == V3
    assert gu._parse_args(["external-review", "--source", "x"]).dataset == V3


def test_langfuse_cli_defaults_use_rebuild_v3(monkeypatch):
    seen = {}

    def fake_sync(args):
        seen["sync"] = args.dataset

    async def fake_run(args):
        seen["run"] = args.dataset

    def run_coroutine(coro):
        try:
            coro.send(None)
        except StopIteration:
            pass

    monkeypatch.setattr(ls, "sync_command", fake_sync)
    monkeypatch.setattr(ls, "run_experiment", fake_run)
    monkeypatch.setattr(ls.asyncio, "run", run_coroutine)

    monkeypatch.setattr(sys, "argv", ["langfuse_scenarios", "sync"])
    ls.main()
    monkeypatch.setattr(sys, "argv", ["langfuse_scenarios", "run"])
    ls.main()

    assert seen == {"sync": V3, "run": V3}


def test_rebuild_v3_exact_scenario_set_and_quota(dataset):
    assert dataset.version == "rebuild-v3"
    assert dataset.review["ready"] is True
    assert [row["scenario_id"] for row in dataset.scenarios] == RETAINED
    assert len({row["scenario_id"] for row in dataset.scenarios}) == 40
    assert dataset.review["counts"]["scenarios"] == 40
    assert dataset.review["quotas"]["scenario_categories"] == REBUILD_V3_SCENARIO_CATEGORIES
    assert dataset.manifest["quotas"]["scenario_categories"] == REBUILD_V3_SCENARIO_CATEGORIES
    assert sum(REBUILD_V3_SCENARIO_CATEGORIES.values()) == 40
    assert scenario_baseline("rebuild-v3") == (40, REBUILD_V3_SCENARIO_CATEGORIES)


def test_rebuild_v3_inherits_parent_bytes_and_rows():
    for name in ("corpus.jsonl", "queries.jsonl", "qrels.jsonl"):
        assert (V3 / name).read_bytes() == (V2 / name).read_bytes()
    for asset in (V2 / "assets").iterdir():
        assert (V3 / "assets" / asset.name).read_bytes() == asset.read_bytes()

    parent_lines = (V2 / "scenarios.jsonl").read_text(encoding="utf-8").splitlines()
    child_lines = (V3 / "scenarios.jsonl").read_text(encoding="utf-8").splitlines()
    parent_ids = [json.loads(line)["scenario_id"] for line in parent_lines]
    expected = [
        line for line, scenario_id in zip(parent_lines, parent_ids) if scenario_id in set(RETAINED)
    ]
    assert child_lines == expected


def test_all_query_scenario_references_are_retained(dataset):
    retained = {row["scenario_id"] for row in dataset.scenarios}
    referenced = {
        query["scenario_id"] for query in dataset.queries if query["scenario_id"] is not None
    }
    assert referenced
    assert referenced <= retained


def test_formal_selection_covers_exact_approved_scenarios(dataset):
    scenarios = ls.select_formal_scenarios(dataset)
    assert [row["scenario_id"] for row in scenarios] == [
        row["scenario_id"] for row in dataset.scenarios
    ]
    plan = build_execution_plan(dataset, scenarios, "rebuild-v3-plan")
    assert plan.dataset_version == "rebuild-v3"
    assert plan.expected_scenarios == 40
    assert plan.expected_events == sum(len(scenario_plan(row).event_ids) for row in scenarios)
    assert plan.expected_checkpoints == sum(
        len(scenario_plan(row).checkpoint_ids) for row in scenarios
    )
    assert plan.expected_metrics == sum(
        len(metrics)
        for row in scenarios
        for metrics in scenario_plan(row).generation_metrics.values()
    )
    assert (plan.expected_events, plan.expected_checkpoints, plan.expected_metrics) == (
        70,
        66,
        132,
    )


def test_validation_selection_stays_six_with_fixed_cancellations(dataset):
    scenarios, records = ls.select_validation_scenarios(dataset)
    ids = [row["scenario_id"] for row in scenarios]
    assert len(ids) == 6
    assert len(set(ids)) == 6
    assert set(FIXED_CANCELLATION_SCENARIOS) <= set(ids)
    assert {row["rule"] for row in records} == {
        "固定验收场景",
        "稳定 ID 顺序首条单轮无工具简单问答",
        "稳定 ID 顺序首条需要生产检索且无保存事件的场景",
        "稳定 ID 顺序首条包含真实确认保存事件的场景",
    }


def test_count_drift_is_rejected_by_formal_baseline(tmp_path):
    short = _copy_version(tmp_path / "short")
    rows = _read_jsonl(short / "scenarios.jsonl")
    removed = next(row for row in rows if row["scenario_id"] == "s-confirmation-cancellation-state-08")
    _write_jsonl(short / "scenarios.jsonl", [row for row in rows if row is not removed])
    _rewrite_manifest(short, lambda manifest: manifest["counts"].update(scenarios=39))
    issues = load_for_review(short).review["issues"]
    assert any("正式要求 40" in issue for issue in issues)

    extra = _copy_version(tmp_path / "extra")
    rows = _read_jsonl(extra / "scenarios.jsonl")
    clone = dict(next(row for row in rows if row["scenario_id"] == "s-confirmation-cancellation-state-08"))
    clone["scenario_id"] = "s-extra-01"
    _write_jsonl(extra / "scenarios.jsonl", [*rows, clone])
    _rewrite_manifest(extra, lambda manifest: manifest["counts"].update(scenarios=41))
    issues = load_for_review(extra).review["issues"]
    assert any("正式要求 40" in issue for issue in issues)


def test_category_quota_drift_is_rejected(tmp_path):
    target = _copy_version(tmp_path / "quota")
    rows = _read_jsonl(target / "scenarios.jsonl")
    changed = []
    for row in rows:
        if row["scenario_id"] == "s-profile-update-01":
            row = {
                **row,
                "tags": ["action_verification", *row["tags"][1:]],
            }
        changed.append(row)
    _write_jsonl(target / "scenarios.jsonl", changed)
    _rewrite_manifest(target, lambda manifest: None)
    issues = load_for_review(target).review["issues"]
    assert any(
        "类别 profile_update" in issue and "正式配额为 5" in issue for issue in issues
    )
    assert any(
        "类别 action_verification" in issue and "正式配额为 8" in issue for issue in issues
    )


def test_legacy_versions_still_select_full_eighty():
    for path in (V1, V2):
        legacy = load_for_evaluation(path)
        assert scenario_baseline(legacy.version)[0] == 80
        assert len(select_scenarios(legacy, None)) == 80
        assert len(ls.select_formal_scenarios(legacy)) == 80
        assert legacy.review["ready"] is True
