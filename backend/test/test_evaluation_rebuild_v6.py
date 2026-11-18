import copy
import shutil
from argparse import Namespace
from pathlib import Path

import pytest

from evaluation.dataset import (
    DatasetError,
    V6_DECISION_REF,
    _approval_report,
    _load_schemas,
    _validator,
    load_for_evaluation,
    load_for_review,
    release_digest,
    scenario_digest,
)

DATA = Path(__file__).resolve().parents[1] / "dataset" / "evaluation"


@pytest.fixture(scope="module")
def dataset():
    return load_for_evaluation(DATA / "rebuild-v6")


def test_default_scenario_runner_uses_rebuild_v6():
    from evaluation.scenarios import DEFAULT_VERSION_DIR, _parse_args

    assert DEFAULT_VERSION_DIR == DATA / "rebuild-v6"
    assert _parse_args([]).version_dir == DEFAULT_VERSION_DIR


def test_integration_selection_uses_current_business_scenarios(dataset):
    from evaluation.scenarios import select_scenarios
    from test.check_evaluation_scenarios_integration import VALIDATION_SCENARIOS

    selected = select_scenarios(dataset, VALIDATION_SCENARIOS)
    assert [scenario["scenario_id"] for scenario in selected] == list(VALIDATION_SCENARIOS)
    assert len(selected) == 7


def test_rebuild_v6_inherits_frozen_retrieval_files_and_assets(dataset):
    parent = DATA / "rebuild-v5"
    for name in ("corpus.jsonl", "queries.jsonl", "qrels.jsonl"):
        assert (dataset.version_dir / name).read_bytes() == (parent / name).read_bytes()
    for asset in dataset.manifest["assets"]:
        assert (dataset.version_dir / asset["path"]).read_bytes() == (parent / asset["path"]).read_bytes()
    assert dataset.review["ready"]
    assert dataset.review["counts"] == {"corpus": 1321, "queries": 150, "qrels": 3679, "scenarios": 40, "assets": 8}
    assert dataset.manifest["release"]["content_sha256"] == release_digest(dataset.manifest)


def test_rebuild_v6_profiles_and_delegated_provenance(dataset):
    schemas, registry = _load_schemas("rebuild-v6")
    validator = _validator(schemas["scenarios"], registry)
    for scenario in dataset.scenarios:
        assert validator.is_valid(scenario)
        profile = scenario["initial_state"]["profile"]
        if profile is not None:
            assert (profile["height_cm"], profile["body_weight_kg"], profile["body_weight_measured_on"]) == (170, 70, None)
            invalid = copy.deepcopy(scenario)
            del invalid["initial_state"]["profile"]["height_cm"]
            assert not validator.is_valid(invalid)
            invalid["initial_state"]["profile"]["height_cm"] = 0
            assert not validator.is_valid(invalid)
        review = scenario["review"]
        assert (review["reviewer"], review["reviewer_type"], review["decision_ref"]) == ("Worker", "assistant", V6_DECISION_REF)
        assert review["content_sha256"] == scenario_digest(scenario, dataset.attachment_hashes(scenario))
    first = next(row for row in dataset.scenarios if row["scenario_id"] == "s-profile-update-01")
    assert "身高170厘米" in first["events"][0]["content"]
    assert "体重70公斤" in first["events"][0]["content"]
    assert first["events"][1]["type"] == "confirm"
    assert first["events"][1]["source_event_id"] == first["events"][0]["event_id"]
    assert "测量日期未知" in first["reference"][0]["answer"]
    assert dataset.manifest["release"]["reviewer_type"] == "assistant"
    assert dataset.manifest["release"]["decision_ref"] == V6_DECISION_REF
    forged = copy.deepcopy(list(dataset.scenarios))
    forged[0]["review"]["decision_ref"] = "other"
    assert any("委托审阅来源不符" in issue for issue in _approval_report(dataset.manifest, list(dataset.queries), list(dataset.qrels), forged, dataset.assets)["issues"])


def test_historical_versions_use_frozen_schema_and_approved_offline_loading():
    for number in range(1, 6):
        historical = load_for_review(DATA / f"rebuild-v{number}")
        assert historical.review["ready"]
        assert historical.manifest["schemas"]["defs"]["path"] == "backend/dataset/evaluation/schemas/defs.schema.json"
    assert load_for_evaluation(DATA / "rebuild-v5").review["ready"]


async def test_current_scenario_execution_rejects_historical_dataset(dataset, tmp_path):
    from evaluation.scenarios import (
        ScenarioModelUnavailable,
        _dataset,
        execute_scenario,
        run_scenario,
        run_selected,
    )

    historical = load_for_evaluation(DATA / "rebuild-v5")
    with pytest.raises(DatasetError, match="当前业务场景执行要求 rebuild-v6"):
        _dataset(historical.version_dir)
    with pytest.raises(DatasetError, match="当前业务场景执行要求 rebuild-v6"):
        await execute_scenario(
            historical, historical.scenarios[0], run_id="historical", root=tmp_path,
            enable_model=True,
        )
    with pytest.raises(DatasetError, match="当前业务场景执行要求 rebuild-v6"):
        await run_scenario(
            historical, historical.scenarios[0], run_id="historical", root=tmp_path,
            enable_model=False, judge=None, judge_timeout=None,
        )
    with pytest.raises(DatasetError, match="当前业务场景执行要求 rebuild-v6"):
        await run_selected(
            historical, historical.scenarios, run_id="historical", root=tmp_path,
            enable_model=False, judge=None, judge_timeout=None,
        )
    assert not list(tmp_path.iterdir())
    assert _dataset(dataset.version_dir).version == "rebuild-v6"
    with pytest.raises(ScenarioModelUnavailable):
        await execute_scenario(
            dataset, dataset.scenarios[0], run_id="current", root=tmp_path,
            enable_model=False,
        )


async def test_scenario_execution_binds_frozen_scenario_and_attachments(dataset, tmp_path):
    from evaluation.scenarios import ScenarioModelUnavailable, execute_scenario, run_selected

    historical = load_for_evaluation(DATA / "rebuild-v5")
    old = next(item for item in historical.scenarios if item["initial_state"]["profile"] is not None)
    current = next(item for item in dataset.scenarios if item["scenario_id"] == old["scenario_id"])
    unknown = copy.deepcopy(current)
    unknown["scenario_id"] = "s-unknown-01"
    changed = copy.deepcopy(current)
    changed["input"]["message"] += "改写"
    for scenario in (unknown, changed, old):
        with pytest.raises(DatasetError, match="场景身份或内容与批准数据不一致"):
            await execute_scenario(dataset, scenario, run_id="invalid", root=tmp_path, enable_model=True)
        with pytest.raises(DatasetError, match="场景身份或内容与批准数据不一致"):
            await run_selected(
                dataset, [scenario], run_id="invalid", root=tmp_path,
                enable_model=False, judge=None, judge_timeout=None,
            )
    assert not list(tmp_path.iterdir())
    with pytest.raises(ScenarioModelUnavailable):
        await execute_scenario(dataset, current, run_id="valid", root=tmp_path, enable_model=False)

    copied_dir = tmp_path / "copy" / "rebuild-v6"
    shutil.copytree(dataset.version_dir, copied_dir)
    copied = load_for_evaluation(copied_dir)
    attached = next(item for item in copied.scenarios if copied.attachment_hashes(item))
    asset_id = next(iter(copied.attachment_hashes(attached)))
    copied.assets[asset_id].write_bytes(b"tampered")
    with pytest.raises(DatasetError, match="场景或附件摘要与批准数据不一致"):
        await execute_scenario(copied, attached, run_id="tampered", root=tmp_path, enable_model=True)
    assert not (tmp_path / attached["scenario_id"]).exists()


async def test_approved_v6_scenario_enters_isolated_execution(dataset, tmp_path, monkeypatch):
    import json

    from app import model_config
    from evaluation.scenarios import ScenarioModelUnavailable, execute_scenario, load_execution

    monkeypatch.setattr(model_config, "DATA_ROOT", tmp_path / "config")
    scenario = next(item for item in dataset.scenarios if item["initial_state"]["profile"] is not None)
    run_root = tmp_path / "run"
    with pytest.raises(ScenarioModelUnavailable):
        await execute_scenario(dataset, scenario, run_id="current", root=run_root, enable_model=True)
    scenario_root = run_root / scenario["scenario_id"]
    initial = json.loads((scenario_root / "initial-state.json").read_text(encoding="utf-8"))
    assert initial["verified"]["profile_present"] is True
    assert initial["projection"]["profile"]["content"] == scenario["initial_state"]["profile"]
    assert load_execution(scenario_root).status == "blocked"


async def test_sdk_experiments_reject_historical_before_start(monkeypatch):
    from evaluation.langfuse_scenarios import run_experiment as run_langfuse
    from evaluation.langsmith_scenarios import run_experiment as run_langsmith

    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    args = Namespace(dataset=DATA / "rebuild-v5", scoring_mode="external")
    for run_experiment in (run_langfuse, run_langsmith):
        with pytest.raises(DatasetError, match="当前业务场景执行要求 rebuild-v6"):
            await run_experiment(args)
