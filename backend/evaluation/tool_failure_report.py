import argparse
import json
import os
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

from dotenv import dotenv_values
from pydantic import ValidationError

from app.application.session.credentials import CredentialFilter
from app.model_config import StoredAuth, StoredModelSettings, auth_path, journal_path, models_path
from evaluation.contracts import DatasetIdentity
from evaluation.deterministic_assertions import ToolResultEvidence, _context_tool_entries, _require_context_matches_tool
from evaluation.langfuse_scenarios import SCHEMA, stable_example_id
from evaluation.scenario_evidence import tool_nodes_by_visible
from evaluation.scenarios import EventCapture, load_execution


def error_code(content: str | None) -> str | None:
    if not content or not content.lstrip().startswith("{"):
        return None
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and isinstance(payload.get("code"), str):
        return payload["code"]
    return None


def verify_copies(execution) -> list[str]:
    gaps = []
    by_entry = {}
    for event in execution.events:
        for call in event.tool_calls:
            if call.entry_id:
                key = (event.session_id, call.entry_id)
                if key in by_entry and (by_entry[key][0] != event.event_id or by_entry[key][1] != call):
                    raise ValueError("工具结果节点身份冲突")
                by_entry[key] = (event.event_id, call)
            else:
                gaps.append(f"{event.event_id}:missing_tool_entry:{call.tool_call_id}")
            if call.content is None:
                gaps.append(f"{event.event_id}:missing_tool_content:{call.tool_call_id}")
        gaps.extend(f"{event.event_id}:context_gap:{index}" for index, gap in enumerate(event.context_gaps)
                    if gap.tool_call_id is not None or gap.ambiguous)
    events = {event.event_id: event for event in execution.events}
    for checkpoint in execution.generation_checkpoints:
        boundary = checkpoint.request_boundary
        if boundary is None:
            continue
        for node in boundary.tool_nodes:
            entry = by_entry.get((node.session_id, node.entry_id))
            if entry is None:
                gaps.append(f"{checkpoint.checkpoint_id}:unlinked_tool_node:{node.entry_id}")
                continue
            call = entry[1]
            if (node.tool_call_id != call.tool_call_id or node.tool_name != call.tool_name
                    or (call.content is not None and node.content != call.content)
                    or (call.is_error is not None and node.is_error != call.is_error)):
                raise ValueError("请求边界工具结果冲突")
        if checkpoint.checkpoint is not None:
            nodes = tool_nodes_by_visible(boundary.tool_nodes, boundary.api)
            for item in checkpoint.checkpoint.model_input.items:
                if item.source != "tool_result":
                    continue
                node = nodes.get(item.tool_call_id)
                if node is None:
                    gaps.append(f"{checkpoint.checkpoint_id}:unlinked_model_input:{item.tool_call_id}")
                    continue
                result = ToolResultEvidence.model_construct(tool_call_id=item.tool_call_id, tool_name=node.tool_name, content=node.content)
                _require_context_matches_tool(item, result)
    for checkpoint in execution.assertion_checkpoints:
        if checkpoint.evidence is None:
            continue
        event = events[checkpoint.event_id]
        context = _context_tool_entries(checkpoint.evidence)
        for item in context.values():
            matches = [result for result in checkpoint.evidence.tool_results if result.tool_call_id == item.tool_call_id]
            if not matches:
                gaps.append(f"{checkpoint.checkpoint_id}:unlinked_context:{item.tool_call_id}")
            elif len(matches) != 1:
                raise ValueError("检查点工具身份不唯一")
            else:
                _require_context_matches_tool(item, matches[0])
        for result in checkpoint.evidence.tool_results:
            if (result.tool_call_id, result.tool_name) not in context:
                gaps.append(f"{checkpoint.checkpoint_id}:unlinked_result_context:{result.tool_call_id}")
            matches = [entry[1] for (session, _), entry in by_entry.items()
                       if session == event.session_id and
                       event.tool_identity_map.get(entry[1].tool_call_id, entry[1].tool_call_id) == result.tool_call_id]
            if not matches:
                gaps.append(f"{checkpoint.checkpoint_id}:unlinked_tool_result:{result.tool_call_id}")
                continue
            if len(matches) != 1:
                raise ValueError("检查点工具身份不唯一")
            call = matches[0]
            if call.tool_name != result.tool_name or (call.content is not None and call.content != result.content):
                raise ValueError("检查点工具结果冲突")
    return gaps


def calls_from_events(run_id, scenario_id, events, source):
    seen = {}
    bad = []
    owners = {}
    for event_index, event in enumerate(events):
        for call_index, call in enumerate(event.tool_calls):
            if call.entry_id:
                entry = (event.session_id, call.entry_id)
                if entry in owners and owners[entry] != event.event_id:
                    raise ValueError("工具结果节点身份冲突")
                owners[entry] = event.event_id
            key = (run_id, scenario_id, event.event_id, call.tool_call_id)
            if not call.tool_call_id or not call.tool_name:
                raise ValueError("工具调用缺少身份")
            value = call.model_dump(mode="json")
            if key in seen:
                if seen[key] != value:
                    raise ValueError("工具调用身份冲突")
                continue
            seen[key] = value
            if call.is_error is not False:
                code = error_code(call.content)
                validation_format = bool(call.content and call.content.startswith(f'工具 "{call.tool_name}" 参数校验失败：\n')
                                         and "\n允许的参数约束：\n" in call.content and "\n收到的参数：\n" in call.content)
                category = "unknown" if call.is_error is None else (
                    "parameter_validation" if code == "invalid_tool_arguments" or validation_format
                    else "structured_error" if code else "unclassified_error")
                bad.append({
                    "run_id": run_id, "scenario_id": scenario_id, "event_id": event.event_id,
                    "business_run_id": event.run_id or None, "tool_call_id": call.tool_call_id,
                    "tool_name": call.tool_name, "arguments": call.arguments,
                    "is_error": call.is_error, "error_text": call.content, "category": category,
                    "stage": "parameter_validation" if category == "parameter_validation" else None,
                    "error_code": code, "source": str(source),
                    "location": (f"line {event_index + 1}.tool_calls[{call_index}]" if source.suffix == ".jsonl"
                                 else f"events[{event_index}].tool_calls[{call_index}]"),
                })
    return seen, bad


def count_calls(seen):
    counts = Counter("unknown" if item["is_error"] is None else "error" if item["is_error"] else "success" for item in seen.values())
    return {"total": len(seen), "success": counts["success"], "error": counts["error"], "unknown": counts["unknown"]}


def error_code_counts(bad):
    return dict(Counter(row["error_code"] for row in bad if row["is_error"] is True and row["error_code"] is not None))


def context_gap_diagnostics(scenario_id, events, source):
    return [{"scenario_id": scenario_id, "event_id": event.event_id, "source": str(source),
             "location": (f"line {event_index + 1}.context_gaps[{gap_index}]" if source.suffix == ".jsonl"
                          else f"events[{event_index}].context_gaps[{gap_index}]"), "gap": gap.model_dump(mode="json")}
            for event_index, event in enumerate(events) for gap_index, gap in enumerate(event.context_gaps)]


def event_succeeded(event: EventCapture) -> bool:
    success_status = {"confirm": "saved", "message": "completed", "cancel": "completed",
                      "session_switch": "completed", "restart": "completed"}
    return event.error is None and success_status.get(event.event_type) == event.terminal_status


def sealed_counts_complete(scenario_ids, missing, interrupted, event_failures, partial, copy_gaps, counts) -> bool:
    return bool(scenario_ids) and not (missing or interrupted or event_failures or partial or copy_gaps or counts["unknown"])


def collect(run_dir: Path) -> tuple[dict, list[dict]]:
    run_dir = run_dir.resolve()
    run = json.loads((run_dir / "sdk-run.json").read_text(encoding="utf-8"))
    if run["schema"] != SCHEMA or run["run_id"] != run_dir.name:
        raise ValueError("运行身份不一致")
    scope = run["sample_scope"]
    scenario_ids = scope["scenario_ids"]
    if len(set(scenario_ids)) != len(scenario_ids) or scope["count"] != len(scenario_ids):
        raise ValueError("场景范围身份不一致")
    dataset = DatasetIdentity(**{key: run["dataset"][key] for key in (
        "version", "manifest_sha256", "content_sha256", "corpus_sha256")})
    expected = {stable_example_id(run["dataset_name"], scenario_id): scenario_id for scenario_id in scenario_ids}
    if len(expected) != len(scenario_ids):
        raise ValueError("样例身份冲突")
    if "example_ids" in run and any(run["example_ids"].get(scenario_id) != example_id for example_id, scenario_id in expected.items()):
        raise ValueError("运行样例身份不一致")
    samples = {item["scenario_id"]: item for item in run.get("scenarios", [])}
    if len(samples) != len(run.get("scenarios", [])) or not set(samples) <= set(scenario_ids):
        raise ValueError("场景结果身份冲突")
    outputs = {}
    sample_ids = set()
    for sample in run.get("samples", []):
        scenario_id = sample["scenario_id"]
        if scenario_id in sample_ids or scenario_id not in scenario_ids:
            raise ValueError("远端样例身份冲突")
        sample_ids.add(scenario_id)
        output = sample.get("outputs")
        if isinstance(output, str):
            output = json.loads(output)
        if output is not None:
            if output.get("scenario_id") != scenario_id or output.get("run_id") != run["run_id"]:
                raise ValueError("远端样例输出身份冲突")
            outputs[scenario_id] = output
    generation = run.get("generation") or {}
    model = json.loads(generation["model"]) if generation.get("model") else None
    model_issues = []
    model_call_count = 0
    seen, bad, observed, interrupted, event_failures, copy_gaps, context_gaps = {}, [], [], [], [], [], []
    paths = sorted((run_dir / "scenarios").glob("*/*/execution.json"))
    for path in paths:
        example_id, scenario_id = path.parent.parent.name, path.parent.name
        if expected.get(example_id) != scenario_id:
            raise ValueError("执行路径身份不一致")
        execution = load_execution(path, expected_run_id=run["run_id"], expected_scenario_id=scenario_id, expected_dataset=dataset)
        sample = samples.get(scenario_id)
        if sample and sample.get("execution_sha256") and sample["execution_sha256"] != execution.execution_sha256:
            raise ValueError("场景结果摘要冲突")
        output = outputs.get(scenario_id)
        if output and (output.get("execution_sha256") != execution.execution_sha256
                       or output.get("scenario_sha256") != execution.scenario_sha256
                       or output.get("example_id") != example_id or output.get("dataset") != dataset.model_dump(mode="json")):
            raise ValueError("远端执行身份冲突")
        observed.append(scenario_id)
        copy_gaps.extend(f"{scenario_id}:{gap}" for gap in verify_copies(execution))
        context_gaps.extend(context_gap_diagnostics(scenario_id, execution.events, path))
        if execution.status != "completed":
            interrupted.append({"scenario_id": scenario_id, "status": execution.status, "error": execution.error, "source": str(path)})
        model_call_count += sum(len(event.model_calls) for event in execution.events)
        for index, event in enumerate(execution.events):
            if not event_succeeded(event):
                event_failures.append({"scenario_id": scenario_id, "event_id": event.event_id, "business_run_id": event.run_id or None,
                                       "terminal_status": event.terminal_status, "error": event.error, "source": str(path), "location": f"events[{index}]"})
            if event.event_type == "message" and not event.model_calls:
                model_issues.append(f"{scenario_id}/{event.event_id}:missing_model_calls")
            for call in event.model_calls:
                if model is None:
                    model_issues.append(f"{scenario_id}/{event.event_id}:missing_generation_identity")
                elif any(getattr(call, key) != model.get(key) for key in ("api", "provider", "model")):
                    model_issues.append(f"{scenario_id}/{event.event_id}:model_identity_conflict")
        event_calls, event_bad = calls_from_events(run["run_id"], scenario_id, execution.events, path)
        seen.update(event_calls)
        bad.extend(event_bad)
    partial = []
    partial_bad = []
    for path in sorted((run_dir / "scenarios").glob("*/*/events.jsonl")):
        if (path.parent / "execution.json").is_file():
            continue
        example_id, scenario_id = path.parent.parent.name, path.parent.name
        if expected.get(example_id) != scenario_id:
            raise ValueError("部分事件路径身份不一致")
        events = [EventCapture.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines()]
        if any(event.scenario_id != scenario_id for event in events) or len({event.event_id for event in events}) != len(events):
            raise ValueError("部分事件身份冲突")
        model_call_count += sum(len(event.model_calls) for event in events)
        context_gaps.extend(context_gap_diagnostics(scenario_id, events, path))
        for event in events:
            if event.event_type == "message" and not event.model_calls:
                model_issues.append(f"{scenario_id}/{event.event_id}:missing_model_calls")
            for call in event.model_calls:
                if model is None:
                    model_issues.append(f"{scenario_id}/{event.event_id}:missing_generation_identity")
                elif any(getattr(call, key) != model.get(key) for key in ("api", "provider", "model")):
                    model_issues.append(f"{scenario_id}/{event.event_id}:model_identity_conflict")
        partial_calls, rows = calls_from_events(run["run_id"], scenario_id, events, path)
        partial_bad.extend(rows)
        partial.append({"scenario_id": scenario_id, "example_id": example_id, "source": str(path),
                        "event_ids": [event.event_id for event in events], "counts": count_calls(partial_calls),
                        "identity": "路径与场景身份已核验；执行结果及事件覆盖未封存"})
    counts = count_calls(seen)
    missing = sorted(set(scenario_ids) - set(observed))
    missing_text = [f"{row['scenario_id']}/{row['event_id']}/{row['tool_call_id']}" for row in bad + partial_bad
                    if row["is_error"] is True and row["error_text"] is None]
    counts_complete = sealed_counts_complete(scenario_ids, missing, interrupted, event_failures, partial, copy_gaps, counts)
    model_status = "conflict" if any("model_identity_conflict" in issue for issue in model_issues) else (
        "unverified" if model_issues or missing or interrupted or event_failures or partial or not model_call_count else "verified")
    summary = {
        "run_id": run["run_id"], "purpose": run["purpose"], "run_status": run["status"],
        "run_error": {"error_type": run.get("error_type"), "error": run.get("error"), "error_code": error_code(run.get("error")), "source": str(run_dir / "sdk-run.json"), "location": "error_type,error"},
        "dataset": dataset.model_dump(mode="json"), "generation": run.get("generation"), "code_identity": run.get("code_identity"), "experiment": run.get("experiment"),
        "model_verification": {"issues": model_issues, "api_provider_model": model_status, "endpoint": "未核验逐调用 endpoint"},
        "semantic_scoring": "external_unverified" if run.get("scoring_mode") == "external" else "unverified",
        "coverage": {"expected": len(scenario_ids), "observed": len(observed), "missing": missing,
                     "interrupted": interrupted, "event_failures": event_failures, "partial_events": partial, "copy_gaps": copy_gaps,
                     "context_gaps": context_gaps, "missing_error_text": missing_text},
        "counts_scope": "仅封存execution中的工具调用；部分事件调用单列，缺失调用数量未知",
        "counts": counts,
        "error_text_counts": [{"text": text, "count": count} for text, count in Counter(item["error_text"] for item in bad if item["is_error"] is True).most_common()],
        "categories": dict(Counter(item["category"] for item in bad)),
        "error_code_counts": error_code_counts(bad),
        "complete": counts_complete,
        "scoring_mode": run.get("scoring_mode"),
        "sources": ["evaluation.scenarios._tool_calls", "evaluation.scenarios._write_event_evidence", "evaluation.scenarios.load_execution", "evaluation.langfuse_scenarios.ScenarioTarget"],
    }
    for row in partial_bad:
        row["partial_event_only"] = True
    return summary, bad + partial_bad


def credential_guard() -> CredentialFilter:
    env = {**dotenv_values(Path(__file__).resolve().parents[1] / ".env"), **os.environ}
    return credential_guard_for(env, auth_path(), journal_path(), models_path())


def credential_guard_for(env, auth_file: Path, journal: Path, models_file: Path) -> CredentialFilter:
    if journal.exists():
        raise ValueError("模型配置journal未恢复，拒绝离线导出")
    secrets = [value for key, value in env.items() if value and any(part in key.upper() for part in ("KEY", "TOKEN", "SECRET", "PASSWORD"))]
    urls = [value for key, value in env.items() if value and ("URI" in key.upper() or "URL" in key.upper())]
    if models_file.is_file():
        try:
            urls.append(StoredModelSettings.model_validate_json(models_file.read_text(encoding="utf-8")).base_url)
        except ValidationError:
            raise ValueError("models.json 内容不合法") from None
    for value in urls:
        parsed = urlsplit(value)
        for part in (parsed.username, parsed.password):
            if part:
                secrets.extend((part, unquote(part)))
    if auth_file.is_file():
        try:
            auth = StoredAuth.model_validate_json(auth_file.read_text(encoding="utf-8"))
        except ValidationError:
            raise ValueError("auth.json 内容不合法") from None
        if auth.api_key:
            secrets.append(auth.api_key)
    if journal.exists():
        raise ValueError("模型配置journal未恢复，拒绝离线导出")
    return CredentialFilter(secrets)


def require_detail_identity(summary: dict) -> None:
    generation = summary["generation"] or {}
    model = json.loads(generation["model"]) if generation.get("model") else {}
    code = summary["code_identity"] or {}
    if (summary["purpose"] != "formal"
            or not all(isinstance(model.get(key), str) and model[key] for key in ("api", "provider", "model", "base_url"))
            or not all(isinstance(code.get(key), str) and code[key] for key in ("commit", "source_sha256", "lock_sha256"))
            or not isinstance(code.get("dirty"), bool)):
        raise ValueError("详细导出需要正式运行、模型和代码身份")


def export(run_dir: Path, output: Path, *, include_details: bool = False) -> dict:
    guard = credential_guard()
    summary, bad = collect(run_dir)
    if include_details:
        require_detail_identity(summary)
        summary["privacy_verification"] = "known_credentials_checked;caller_authorized"
    else:
        summary["privacy_verification"] = "unverified"
        summary["run_error"].pop("error")
        for row in summary["coverage"]["interrupted"] + summary["coverage"]["event_failures"]:
            row.pop("error")
        texts = list(dict.fromkeys(row["error_text"] for row in bad if row["is_error"] is True))
        groups = {text: f"error-{index + 1}" for index, text in enumerate(texts)}
        summary["error_text_counts"] = [{"group": groups[row["text"]], "count": row["count"]}
                                        for row in summary["error_text_counts"]]
        for row in bad:
            text = row.pop("error_text")
            row.pop("arguments")
            row["error_text_available"] = text is not None
            if row["is_error"] is True:
                row["error_text_group"] = groups[text]
    if guard.contains(summary) or guard.contains(bad):
        raise ValueError("输出包含受保护凭据")
    output.mkdir(parents=True, exist_ok=False)
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "bad-cases.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in bad), encoding="utf-8")
    report = ("# 工具调用离线统计\n\n" + "```json\n" + json.dumps(summary, ensure_ascii=False, indent=2) + "\n```\n\n"
              + "错误及未知调用清单：bad-cases.jsonl。参数校验失败为输入拒绝证据，计入真实 error。external 模式生成语义评分由外部完成。\n")
    (output / "report.md").write_text(report, encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--include-details", action="store_true", help="调用方授权披露本次正式实验原文和参数")
    args = parser.parse_args()
    summary = export(args.run_dir, args.output, include_details=args.include_details)
    print(json.dumps(summary["counts"], ensure_ascii=False))


if __name__ == "__main__":
    main()
