import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluation.contracts import ContextItem, ToolNode
from evaluation.deterministic_assertions import ToolResultEvidence
from evaluation.scenario_evidence import ContextGap
from evaluation.scenarios import EventCapture, ExecutionIdentityError, ToolCallCapture, _seal_execution, load_execution
from evaluation.tool_failure_report import (calls_from_events, collect, context_gap_diagnostics, count_calls, credential_guard_for, error_code,
                                            error_code_counts, event_succeeded, export, require_detail_identity, sealed_counts_complete, verify_copies)


RUN = Path(__file__).resolve().parents[1] / "temp/scenario-langfuse/runs/20261025T064301Z-9f370396"
FIXTURE = Path(__file__).parent / "fixtures/tool_failure_run.json"


def retained_run():
    if not RUN.is_dir():
        pytest.skip("历史原件未配置")
    return RUN


def test_real_source_fixture_and_structured_code():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    call = ToolCallCapture.model_validate(fixture["tool_call"])
    assert call.tool_name == "get_current_plan"
    assert call.content == '{"id": null, "content": null}'
    assert error_code(call.content) is None
    assert error_code('{"code":"invalid_tool_arguments","message":"参数校验失败。"}') == "invalid_tool_arguments"
    assert error_code('{invalid') is None


def test_real_source_fixture_deduplicates_and_rejects_conflicts():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    call = ToolCallCapture.model_validate(fixture["tool_call"])
    event = EventCapture.model_construct(**fixture["event"], tool_calls=[call, call.model_copy(deep=True)])
    seen, bad = calls_from_events("20261025T064301Z-9f370396", event.scenario_id, [event], FIXTURE)
    assert count_calls(seen) == {"total": 1, "success": 1, "error": 0, "unknown": 0}
    assert bad == []
    event.tool_calls[1].is_error = None
    with pytest.raises(ValueError, match="工具调用身份冲突"):
        calls_from_events("20261025T064301Z-9f370396", event.scenario_id, [event], FIXTURE)
    event.tool_calls = [event.tool_calls[1]]
    seen, bad = calls_from_events("20261025T064301Z-9f370396", event.scenario_id, [event], FIXTURE)
    assert count_calls(seen)["unknown"] == 1
    assert bad[0]["arguments"] == fixture["tool_call"]["arguments"]


def test_credentials_guard_is_read_only(tmp_path):
    auth = tmp_path / "auth.json"
    journal = tmp_path / "provider.journal"
    models = tmp_path / "models.json"
    auth.write_text('{"api_key":"auth-secret-value"}', encoding="utf-8")
    models.write_text(json.dumps({"api": "openai-completions", "provider": "test", "model": "test", "base_url": "https://model-user:model%2Dsecret@example.com/v1"}), encoding="utf-8")
    guard = credential_guard_for({"FIT_AGENT_MILVUS_URI": "http://user-name:secret-pass@localhost:19530"}, auth, journal, models)
    assert guard.contains("secret-pass") and guard.contains("auth-secret-value")
    assert guard.contains("model-user") and guard.contains("model-secret") and guard.contains("model%2Dsecret")
    models.write_text('{"base_url":"https://user:private-value@example.com"}', encoding="utf-8")
    with pytest.raises(ValueError) as error:
        credential_guard_for({}, auth, journal, models)
    assert "private-value" not in str(error.value)
    journal.write_text("pending", encoding="utf-8")
    with pytest.raises(ValueError, match="journal"):
        credential_guard_for({}, auth, journal, models)
    assert journal.read_text(encoding="utf-8") == "pending"
    assert auth.read_text(encoding="utf-8") == '{"api_key":"auth-secret-value"}'


def test_event_status_and_sealed_coverage():
    event = SimpleNamespace(event_type="confirm", terminal_status="saved", error=None)
    assert event_succeeded(event)
    event.terminal_status = "completed"
    assert not event_succeeded(event)
    event.event_type = "message"
    assert event_succeeded(event)
    event.error = "failed"
    assert not event_succeeded(event)
    event.error = None
    event.event_type = "unknown"
    assert not event_succeeded(event)
    counts = {"unknown": 0}
    assert sealed_counts_complete(["s"], [], [], [], [], [], counts)
    assert not sealed_counts_complete(["s"], [], [], ["failed"], [], [], counts)
    assert not sealed_counts_complete(["s"], [], [], [], [], ["gap"], counts)
    assert not sealed_counts_complete(["s"], [], [], [], [], [], {"unknown": 1})
    assert not sealed_counts_complete(["s"], ["s"], [], [], [], [], counts)


def test_cross_event_entry_conflict_and_strict_classification():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    call = ToolCallCapture.model_validate(fixture["tool_call"])
    event = EventCapture.model_construct(**fixture["event"], tool_calls=[call])
    other = event.model_copy(update={"event_id": "e02"})
    with pytest.raises(ValueError, match="工具结果节点身份冲突"):
        calls_from_events("run", event.scenario_id, [event, other], FIXTURE)
    call.is_error = True
    call.content = '工具执行失败：参数校验失败'
    _, bad = calls_from_events("run", event.scenario_id, [event], FIXTURE)
    assert bad[0]["category"] == "unclassified_error"
    call.content = '{"code":"invalid_tool_arguments","message":"rejected"}'
    _, bad = calls_from_events("run", event.scenario_id, [event], FIXTURE)
    assert bad[0]["category"] == "parameter_validation"


def test_fixture_context_and_model_input_copies():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    call = ToolCallCapture.model_validate(fixture["tool_call"])
    event = EventCapture.model_construct(**fixture["event"], tool_calls=[call])
    item = ContextItem(source="tool_result", fact_origin="tool", tool_call_id=call.tool_call_id,
                       tool_name=call.tool_name, content=call.content[:5], truncated=True,
                       original_chars=len(call.content), omitted_chars=len(call.content) - 5)
    result = ToolResultEvidence.model_construct(tool_call_id=call.tool_call_id, tool_name=call.tool_name, content=call.content)
    evidence = SimpleNamespace(context_evidence=[SimpleNamespace(item=item)], tool_results=[result])
    assertion = SimpleNamespace(checkpoint_id="a", event_id=event.event_id, evidence=evidence)
    node = ToolNode(tool_call_id=call.tool_call_id, tool_name=call.tool_name, content=call.content,
                    entry_id=call.entry_id, session_id=event.session_id, is_error=False)
    boundary = SimpleNamespace(api="openai-completions", tool_nodes=[node])
    generation = SimpleNamespace(checkpoint_id="g", request_boundary=boundary,
                                 checkpoint=SimpleNamespace(model_input=SimpleNamespace(items=[item.model_copy(deep=True)])))
    execution = SimpleNamespace(events=[event], generation_checkpoints=[generation], assertion_checkpoints=[assertion])
    assert verify_copies(execution) == []
    item.content = "wrong"
    with pytest.raises(ValueError, match="正文"):
        verify_copies(execution)
    item.content = call.content[:5]
    item.tool_name += "-conflict"
    with pytest.raises(ValueError, match="名称"):
        verify_copies(execution)
    item.tool_name = call.tool_name
    generation.checkpoint.model_input.items[0].content = "wrong"
    with pytest.raises(ValueError, match="正文"):
        verify_copies(execution)


def test_context_gaps_preserved_and_tool_coverage():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    event = EventCapture.model_construct(**fixture["event"], tool_calls=[])
    event.context_gaps = [ContextGap(reason="助手工具调用消息没有可表达的文本内容", role="assistant")]
    execution = SimpleNamespace(events=[event], generation_checkpoints=[], assertion_checkpoints=[])
    assert verify_copies(execution) == []
    diagnostics = context_gap_diagnostics(event.scenario_id, [event], FIXTURE)
    assert diagnostics[0]["gap"] == event.context_gaps[0].model_dump(mode="json")
    assert diagnostics[0]["location"] == "events[0].context_gaps[0]"
    assert sealed_counts_complete(["s"], [], [], [], [], verify_copies(execution), {"unknown": 0})
    event.context_gaps.append(ContextGap(reason="工具身份缺口", tool_call_id="call-1"))
    event.context_gaps.append(ContextGap(reason="上下文身份不明确", ambiguous=True))
    assert verify_copies(execution) == ["e01:context_gap:1", "e01:context_gap:2"]
    assert len(context_gap_diagnostics(event.scenario_id, [event], Path("events.jsonl"))) == 3
    assert not sealed_counts_complete(["s"], [], [], [], [], verify_copies(execution), {"unknown": 0})


def test_error_code_counts_distinguish_errors_and_exclude_unknown():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    call = ToolCallCapture.model_validate(fixture["tool_call"])
    codes = ["invalid_business_payload", "tool_execution_failed", "invalid_business_payload", "tool_execution_failed"]
    calls = [call.model_copy(update={"tool_call_id": f"call-{index}", "entry_id": f"entry-{index}",
                                    "is_error": None if index == 3 else True, "content": json.dumps({"code": code})})
             for index, code in enumerate(codes)]
    event = EventCapture.model_construct(**fixture["event"], tool_calls=calls)
    _, bad = calls_from_events("run", event.scenario_id, [event], FIXTURE)
    assert error_code_counts(bad) == {"invalid_business_payload": 2, "tool_execution_failed": 1}
    assert all(row["stage"] is None for row in bad)
    assert [row["error_code"] for row in bad] == codes


def test_fixture_run_identity_rejected(tmp_path):
    run = tmp_path / "test-run"
    run.mkdir()
    (run / "sdk-run.json").write_text(json.dumps({"schema": "invalid", "run_id": "test-run"}), encoding="utf-8")
    with pytest.raises(ValueError, match="运行身份不一致"):
        collect(run)


def test_detail_identity_requires_boolean_dirty():
    summary = {"purpose": "formal", "generation": {"model": json.dumps({"api": "a", "provider": "p", "model": "m", "base_url": "https://example.com"})},
               "code_identity": {"commit": "c", "source_sha256": "s", "lock_sha256": "l", "dirty": False}}
    require_detail_identity(summary)
    for value in (None, "false", 0):
        summary["code_identity"]["dirty"] = value
        with pytest.raises(ValueError, match="模型和代码身份"):
            require_detail_identity(summary)
    del summary["code_identity"]["dirty"]
    with pytest.raises(ValueError, match="模型和代码身份"):
        require_detail_identity(summary)


@pytest.fixture
def copied_run(tmp_path):
    retained_run()
    target = tmp_path / RUN.name
    target.mkdir()
    shutil.copy2(RUN / "sdk-run.json", target / "sdk-run.json")
    for source in (RUN / "scenarios").glob("*/*/execution.json"):
        destination = target / source.relative_to(RUN)
        destination.parent.mkdir(parents=True)
        shutil.copy2(source, destination)
    return target


def rewrite_execution(run, change, predicate=lambda execution: any(event.tool_calls for event in execution.events)):
    path = next(path for path in (run / "scenarios").glob("*/*/execution.json")
                if predicate(load_execution(path)))
    execution = load_execution(path)
    change(execution)
    execution = _seal_execution(execution)
    path.write_text(execution.model_dump_json(indent=2), encoding="utf-8")
    sdk_path = run / "sdk-run.json"
    sdk = json.loads(sdk_path.read_text(encoding="utf-8"))
    for item in sdk["scenarios"]:
        if item["scenario_id"] == execution.scenario_id:
            item["execution_sha256"] = execution.execution_sha256
    for item in sdk["samples"]:
        if item["scenario_id"] == execution.scenario_id and item.get("outputs"):
            output = json.loads(item["outputs"])
            output["execution_sha256"] = execution.execution_sha256
            item["outputs"] = json.dumps(output, ensure_ascii=False)
    sdk_path.write_text(json.dumps(sdk, ensure_ascii=False), encoding="utf-8")


def test_retained_evidence_counts_and_rejection():
    summary, bad = collect(retained_run())
    assert summary["counts"] == {"total": 22, "success": 17, "error": 5, "unknown": 0}
    assert summary["coverage"]["expected"] == summary["coverage"]["observed"] == 6
    assert not summary["complete"]
    assert len(bad) == 5
    assert all(row["category"] == "parameter_validation" for row in bad)
    assert sum(row["count"] for row in summary["error_text_counts"]) == 5


def test_duplicate_and_unknown(copied_run):
    def change(execution):
        event = next(event for event in execution.events if event.tool_calls)
        event.tool_calls[0].is_error = None
        event.tool_calls.append(event.tool_calls[0].model_copy(deep=True))

    rewrite_execution(copied_run, change)
    summary, bad = collect(copied_run)
    assert summary["counts"]["total"] == 22
    assert summary["counts"]["unknown"] == 1
    assert any(row["category"] == "unknown" for row in bad)


def test_conflicting_duplicate_rejected(copied_run):
    def change(execution):
        event = next(event for event in execution.events if event.tool_calls)
        duplicate = event.tool_calls[0].model_copy(deep=True)
        duplicate.tool_name += "-conflict"
        event.tool_calls.append(duplicate)

    rewrite_execution(copied_run, change)
    with pytest.raises(ValueError, match="工具.*身份冲突"):
        collect(copied_run)


def test_missing_execution_and_corrupt_digest(copied_run):
    path = next((copied_run / "scenarios").glob("*/*/execution.json"))
    text = path.read_text(encoding="utf-8")
    path.unlink()
    summary, _ = collect(copied_run)
    assert summary["coverage"]["expected"] == 6
    assert summary["coverage"]["observed"] == 5
    assert len(summary["coverage"]["missing"]) == 1
    assert not summary["complete"]
    payload = json.loads(text)
    payload["execution_sha256"] = "0" * 64
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ExecutionIdentityError, match="执行结果摘要不一致"):
        collect(copied_run)


def test_historical_export_omits_original_text_and_arguments(copied_run, tmp_path):
    source_summary, bad = collect(copied_run)
    output = tmp_path / "report"
    summary = export(copied_run, output)
    assert summary["privacy_verification"] == "unverified"
    rendered = (output / "summary.json").read_text(encoding="utf-8") + (output / "bad-cases.jsonl").read_text(encoding="utf-8")
    assert bad[0]["error_text"] not in rendered
    assert '"arguments"' not in rendered
    assert sum(row["count"] for row in summary["error_text_counts"]) == source_summary["counts"]["error"]
    with pytest.raises(ValueError, match="详细导出需要正式运行"):
        export(copied_run, tmp_path / "details", include_details=True)
    assert not (tmp_path / "details").exists()


def test_partial_events_from_real_source(copied_run):
    source = next(path for path in (RUN / "scenarios").glob("*/*/events.jsonl")
                  if any(event.tool_calls for event in load_execution(path.parent / "execution.json").events))
    destination = copied_run / source.relative_to(RUN)
    shutil.copy2(source, destination)
    (destination.parent / "execution.json").unlink()
    summary, _ = collect(copied_run)
    assert summary["coverage"]["observed"] == 5
    assert len(summary["coverage"]["partial_events"]) == 1
    assert summary["coverage"]["partial_events"][0]["counts"]["total"] > 0
    assert not summary["complete"]


def test_checkpoint_copy_conflict(copied_run):
    def has_boundary(execution):
        return any(checkpoint.request_boundary and checkpoint.request_boundary.tool_nodes
                   for checkpoint in execution.generation_checkpoints)

    def change(execution):
        checkpoint = next(checkpoint for checkpoint in execution.generation_checkpoints
                          if checkpoint.request_boundary and checkpoint.request_boundary.tool_nodes)
        checkpoint.request_boundary.tool_nodes[0].tool_name += "-conflict"

    rewrite_execution(copied_run, change, has_boundary)
    with pytest.raises(ValueError, match="请求边界工具结果冲突"):
        collect(copied_run)


def test_assertion_checkpoint_copy_conflict(copied_run):
    def has_results(execution):
        return any(checkpoint.evidence and checkpoint.evidence.tool_results for checkpoint in execution.assertion_checkpoints)

    def change(execution):
        checkpoint = next(checkpoint for checkpoint in execution.assertion_checkpoints
                          if checkpoint.evidence and checkpoint.evidence.tool_results)
        checkpoint.evidence.tool_results[0].tool_name += "-conflict"

    rewrite_execution(copied_run, change, has_results)
    with pytest.raises(ValueError, match="工具结果名称与上下文不一致"):
        collect(copied_run)


def test_model_identity_conflict_and_missing_error_text(copied_run):
    def has_error_and_model(execution):
        return any(event.model_calls and any(call.is_error for call in event.tool_calls) for event in execution.events)

    def change(execution):
        event = next(event for event in execution.events if event.model_calls and any(call.is_error for call in event.tool_calls))
        event.model_calls[0].model += "-conflict"
        call = next(call for call in event.tool_calls if call.is_error)
        call.content = None

    rewrite_execution(copied_run, change, has_error_and_model)
    summary, _ = collect(copied_run)
    assert any("model_identity_conflict" in issue for issue in summary["model_verification"]["issues"])
    assert summary["coverage"]["missing_error_text"]
    assert not summary["complete"]
