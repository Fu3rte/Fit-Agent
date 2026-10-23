import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agent.tools.files import ReadArguments, WriteArguments, create_file_tools
from evaluation.contracts import BusinessProjection
from evaluation.scenarios import EventCapture, ToolCallCapture
from evaluation.skills_generation import (
    DATASET,
    EMPTY_STATE,
    GROUPS,
    SCHEMA,
    SampleRecord,
    digest_file,
    group_prompt,
    identity_digest,
    load_cases,
    seal_record,
    user_input,
)
from evaluation.skills_scoring import (
    KEYS,
    REFERENCE_STATUS,
    build_payload,
    check_public,
    distribution,
    load_binding,
    load_run,
    read_activity,
    render_report,
    sample_usage,
    summarize,
)


@pytest.fixture
def unrun_skills_run(tmp_path):
    # 仅登记未运行身份，无回答、分数或审批。
    root = tmp_path / "offline-unrun"
    root.mkdir()
    cases = load_cases()
    identity = {
        "schema_version": SCHEMA, "expected_samples": 90, "groups": GROUPS,
        "data_sha256": digest_file(DATASET), "reference_status": REFERENCE_STATUS,
        "cases": {case.id: identity_digest(case.model_dump()) for case in cases},
    }
    document = {"run_id": root.name, "identity": identity,
                "identity_sha256": identity_digest(identity)}
    (root / "run.json").write_text(json.dumps(document), encoding="utf-8")
    return root


def test_skills_scoring_missing_results_keep_fixed_denominators(unrun_skills_run):
    summary = summarize(unrun_skills_run)
    assert len(summary["rows"]) == 90
    assert len(summary["pairs"]) == 60
    assert summary["binding"] is None
    for group in summary["groups"].values():
        assert (group["target"], group["professional_target"], group["negative_target"]) == (30, 24, 6)
        assert group["execution"] == {"not_run": 30}
        assert group["scoring"] == {"missing_feedback": 30}
        assert group["professional_pass_rate"] is None
        assert group["negative_pass_rate"] is None
        assert group["confirmed_critical_error_rate"] is None
        assert group["critical_error_unknown"] == 24
        assert group["scores"][KEYS[0]]["observed"] == 0
        assert group["generation_usage"]["input_tokens"]["median"] is None
    trigger = summary["groups"]["A1"]["natural_trigger"]
    assert (trigger["tp"], trigger["fp"], trigger["fn"]) == (0, 0, 0)
    assert trigger["precision"] is None
    assert trigger["recall"] is None
    assert trigger["negative_false_trigger_rate"] is None
    assert trigger["observed_cases"] == 0
    assert trigger["unobserved_cases"] == 30
    assert trigger["professional_unknown_cases"] == 24
    assert trigger["negative_unknown_cases"] == 6
    assert all(row["activity"]["triggered"] is None for row in summary["rows"])
    assert "natural_trigger" not in summary["groups"]["A0"]
    assert "natural_trigger" not in summary["groups"]["A2"]
    report = render_report(summary)
    assert report.count("：执行=not_run") == 90
    assert REFERENCE_STATUS in report
    assert "未经独立科学或专家核验" in report
    assert build_payload(unrun_skills_run)["rows"] == []


def test_skills_scoring_actual_absent_service_failure_has_no_score(unrun_skills_run):
    document, cases, _ = load_run(unrun_skills_run)
    case = cases[0]
    record = seal_record(SampleRecord(
        run_id=unrun_skills_run.name, group="A1", case_id=case.id,
        identity_sha256=document["identity_sha256"], case_sha256=document["identity"]["cases"][case.id],
        user_input=user_input(case), initial_state=EMPTY_STATE, selected_knowledge=[],
        system_prompt=group_prompt("A1"), session_id=str(uuid4()),
        started_at="2026-01-01T00:00:00+00:00", finished_at="2026-01-01T00:00:00+00:00",
        status="blocked", error="NoModelWasRun", event=None, model_requests=[],
        model_call_count=0, retrieval=[],
    ))
    path = unrun_skills_run / "A1" / case.id / "sample.json"
    path.parent.mkdir(parents=True)
    path.write_text(record.model_dump_json(), encoding="utf-8")
    summary = summarize(unrun_skills_run)
    row = next(row for row in summary["rows"] if row["group"] == "A1" and row["case_id"] == case.id)
    assert row["execution"] == "blocked"
    assert row["passed"] is None and row["critical_error"] is None
    assert row["results"] == {}
    assert row["activity"]["observed"] is False
    assert row["activity"]["triggered"] is None
    trigger = summary["groups"]["A1"]["natural_trigger"]
    assert (trigger["tp"], trigger["fp"], trigger["fn"]) == (0, 0, 0)
    assert trigger["unobserved_cases"] == 30
    assert trigger["recall"] is None
    assert trigger["negative_false_trigger_rate"] is None
    assert summary["groups"]["A1"]["target"] == 30
    assert sample_usage(record)["input_tokens"] is None
    assert sample_usage(record)["requests"] == 0
    assert build_payload(unrun_skills_run)["rows"] == []


def test_skills_scoring_identity_drift_rejected(unrun_skills_run):
    path = unrun_skills_run / "run.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["identity"]["expected_samples"] = 1
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="摘要"):
        summarize(unrun_skills_run)


def test_skills_scoring_binding_missing_or_unverified_fails(tmp_path):
    path = tmp_path / "binding.json"
    with pytest.raises(ValueError, match="实际网页Evaluator"):
        load_binding(path)
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        load_binding(path)


@pytest.mark.parametrize("value", [
    {"authorization": "redacted"}, {"context": "C:/private/data.txt"},
    {"context": "Bearer redacted"}, {"nested": {"API_KEY": "redacted"}},
])
def test_skills_scoring_outbound_privacy_rejects_protected_content(value):
    with pytest.raises(ValueError):
        check_public(value)


def test_skills_scoring_real_read_entry_and_topic_are_separate(tmp_path):
    tools = create_file_tools(str(uuid4()), tmp_root=tmp_path)
    calls = []
    for index, path in enumerate(("skills/training-theory/SKILL.md",
                                  "skills/training-theory/references/effort-and-fatigue.md")):
        args = ReadArguments(path=path)
        actual = tools["read"].execute(str(index), args, None, None)
        calls.append(ToolCallCapture(tool_call_id=str(index), tool_name="read",
                                     arguments=args.model_dump(),
                                     content=actual.content[0].text, is_error=False))
    record = SimpleNamespace(event=SimpleNamespace(tool_calls=calls))
    activity = read_activity(record, load_cases()[0])
    assert activity["triggered"] and activity["entry"]
    assert activity["attempts"] == 2
    assert activity["related_registered"] == ["skills/training-theory/references/effort-and-fatigue.md"]
    assert activity["unregistered_pending_equivalence_review"] == []
    negative = read_activity(record, load_cases()[-1])
    assert len(negative["unrelated"]) == 1
    assert negative["unregistered_pending_equivalence_review"] == []
    equivalent = read_activity(record, load_cases()[2], {
        (load_cases()[2].id, "skills/training-theory/references/effort-and-fatigue.md"): "related",
    })
    assert equivalent["related_including_reviewed_equivalents"]
    assert activity["observed"] is True
    assert read_activity(None, load_cases()[0])["triggered"] is None
    assert read_activity(SimpleNamespace(event=None), load_cases()[0])["observed"] is False


@pytest.mark.parametrize("trigger_reads", [True, False])
def test_skills_scoring_local_event_coverage(unrun_skills_run, tmp_path, trigger_reads):
    document, cases, _ = load_run(unrun_skills_run)
    professional = [case for case in cases if case.should_use_skill]
    negative = [case for case in cases if not case.should_use_skill]

    def observe(case, read):
        # 记录真实本地工具执行；无模型回答及裁判反馈。
        session_id = str(uuid4())
        tools = create_file_tools(session_id, tmp_root=tmp_path / "local-tools")
        started = datetime.now(UTC).isoformat()
        if read:
            name = "read"
            args = ReadArguments(path="skills/training-theory/SKILL.md")
        else:
            name = "write"
            args = WriteArguments(path=f"sessions/{session_id}/workspace/observation.txt",
                                  content="离线工具观测")
        actual = tools[name].execute(case.id, args, None, None)
        event = EventCapture(
            run_id=unrun_skills_run.name, scenario_id=case.id,
            event_id=case.id, event_type="local_tool_observation",
            source_event_id=None, session_id=session_id, operation_id=None,
            request_entry_id=None, terminal_status="completed", terminal_event="local_tool_result",
            before=BusinessProjection(), after=BusinessProjection(),
            tool_calls=[ToolCallCapture(tool_call_id=case.id, tool_name=name,
                                        arguments=args.model_dump(),
                                        content=actual.content[0].text, is_error=False)],
        )
        record = seal_record(SampleRecord(
            run_id=unrun_skills_run.name, group="A1", case_id=case.id,
            identity_sha256=document["identity_sha256"],
            case_sha256=document["identity"]["cases"][case.id],
            user_input=user_input(case), initial_state=EMPTY_STATE, selected_knowledge=[],
            system_prompt=group_prompt("A1"), session_id=session_id,
            started_at=started, finished_at=datetime.now(UTC).isoformat(),
            status="failed", error="OfflineToolObservationNoModel", event=event,
            model_requests=[], model_call_count=0, retrieval=[],
        ))
        path = unrun_skills_run / "A1" / case.id / "sample.json"
        path.parent.mkdir(parents=True)
        path.write_text(record.model_dump_json(), encoding="utf-8")
        return read_activity(record, case)

    observe(professional[0], trigger_reads)
    activity = observe(professional[1], False)
    assert activity["observed"] is True
    assert activity["read_count"] == 0
    assert activity["triggered"] is False
    trigger = summarize(unrun_skills_run)["groups"]["A1"]["natural_trigger"]
    assert (trigger["tp"], trigger["fp"], trigger["fn"]) == (int(trigger_reads), 0, 2 - int(trigger_reads))
    assert trigger["professional_observed_cases"] == 2
    assert trigger["professional_unknown_cases"] == 22
    assert trigger["negative_unknown_cases"] == 6
    assert trigger["precision"] is None and trigger["recall"] is None
    assert trigger["negative_false_trigger_rate"] is None

    for case in professional[2:]:
        observe(case, False)
    trigger = summarize(unrun_skills_run)["groups"]["A1"]["natural_trigger"]
    assert trigger["recall"] == int(trigger_reads) / 24
    assert trigger["fn"] == 24 - int(trigger_reads)
    assert trigger["precision"] is None
    assert trigger["negative_false_trigger_rate"] is None

    observe(negative[0], trigger_reads)
    trigger = summarize(unrun_skills_run)["groups"]["A1"]["natural_trigger"]
    assert trigger["negative_observed_cases"] == 1
    assert trigger["negative_unknown_cases"] == 5
    assert trigger["fp"] == int(trigger_reads)
    assert trigger["precision"] is None
    assert trigger["negative_false_trigger_rate"] is None
    for case in negative[1:]:
        observe(case, False)
    summary = summarize(unrun_skills_run)
    trigger = summary["groups"]["A1"]["natural_trigger"]
    assert trigger["observed_cases"] == 30 and trigger["unobserved_cases"] == 0
    assert trigger["professional_unknown_cases"] == trigger["negative_unknown_cases"] == 0
    assert trigger["negative_false_trigger_rate"] == int(trigger_reads) / 6
    assert trigger["precision"] == (0.5 if trigger_reads else None)
    assert build_payload(unrun_skills_run)["rows"] == []
    assert all(row["passed"] is None for row in summary["rows"])
    assert "precision须覆盖全部30题且有触发" in render_report(summary)


def test_skills_scoring_missing_usage_and_nearest_rank():
    assert distribution([None, None]) == {"observed": 0, "missing": 2, "median": None, "p95": None}
    assert distribution([1, 3, None, 2]) == {"observed": 3, "missing": 1, "median": 2, "p95": 3}
    assert sample_usage(None)["requests"] is None
