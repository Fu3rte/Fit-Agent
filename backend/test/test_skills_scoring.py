import copy
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from langfuse.api import (
    Experiment,
    ExperimentItem,
    ExperimentItemsResponse,
    ExperimentsResponse,
    ExperimentsResponseMeta,
    GetScoresV3Meta,
    GetScoresV3Response,
    NotFoundError,
    ScoreSource,
    ScoreSubjectV3_Observation,
    ScoreSubjectV3_Trace,
    ScoreV3_Categorical,
)

from app.agent.tools.files import ReadArguments, WriteArguments, create_file_tools
from evaluation import skills_judge
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
    BINDING_SCHEMA,
    EVALUATORS,
    FIELD_PATHS,
    INTERFACE,
    KEYS,
    MAPPING,
    MODEL,
    PROGRAMMATIC_CHANNEL,
    PROVIDER,
    QUESTIONS,
    REFERENCE_STATUS,
    SCORE_SEMANTICS,
    SCORING_SCHEMA,
    WEB_JUDGE_SCORE_SOURCE,
    build_payload,
    check_public,
    collect,
    distribution,
    judge_score_id,
    load_binding,
    load_feedback,
    load_run,
    read_activity,
    render_report,
    sample_usage,
    scoring_dir,
    submit,
    summarize,
    sync,
    verify_dataset,
)
from evaluation.skills_scoring import (
    metadata as receipt_metadata,
)

VERIFY_STAMP = datetime(2026, 1, 1, tzinfo=UTC)


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


def write_run_manifest(root):
    cases = load_cases()
    identity = {
        "schema_version": SCHEMA, "expected_samples": 90, "groups": GROUPS,
        "data_sha256": digest_file(DATASET), "reference_status": REFERENCE_STATUS,
        "cases": {case.id: identity_digest(case.model_dump()) for case in cases},
    }
    document = {"run_id": root.name, "identity": identity,
                "identity_sha256": identity_digest(identity)}
    (root / "run.json").write_text(json.dumps(document), encoding="utf-8")
    return document


def complete_sample(root, document, case, group="A1"):
    # 构造结构完整的成功样本：唯一回答请求与可投影的真实请求证据。
    prompt = group_prompt(group)
    request = {"system": prompt, "messages": [
        {"role": "user", "content": case.prompt + "\n\n" + case.context["content"]}]}
    directory = root / group / case.id / "model-calls"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "0000.json").write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    event = EventCapture(
        run_id=root.name, scenario_id=case.id, event_id="message", event_type="message",
        source_event_id=None, session_id=str(uuid4()), operation_id=None,
        request_entry_id=None, terminal_status="completed", terminal_event="message",
        answer_text="离线冻结的真实回答正文。", answer_message_id="msg-answer",
        before=BusinessProjection(), after=BusinessProjection())
    record = seal_record(SampleRecord(
        run_id=root.name, group=group, case_id=case.id,
        identity_sha256=document["identity_sha256"],
        case_sha256=document["identity"]["cases"][case.id],
        user_input=user_input(case), initial_state=EMPTY_STATE, selected_knowledge=[],
        system_prompt=prompt, session_id=str(uuid4()),
        started_at="2026-01-01T00:00:00+00:00", finished_at="2026-01-01T00:00:05+00:00",
        status="completed", error=None, event=event,
        model_requests=[{"sequence": 0, "call_kind": "normal", "message_id": "msg-answer",
                         "compaction_id": None, "api": "anthropic", "provider": "glm",
                         "model": "glm-5.3-flash",
                         "request_sha256": digest_file(directory / "0000.json"),
                         "request_path": "model-calls/0000.json", "status": "stop",
                         "input_tokens": 10, "output_tokens": 5,
                         "request_file": "model-calls/0000.json"}],
        model_call_count=1, retrieval=[]))
    path = root / group / case.id / "sample.json"
    path.write_text(record.model_dump_json(), encoding="utf-8")
    return record


@pytest.fixture
def completed_skills_run(tmp_path):
    root = tmp_path / "offline-completed"
    root.mkdir()
    document = write_run_manifest(root)
    complete_sample(root, document, load_cases()[0], "A1")
    return root


def valid_binding() -> dict:
    return {
        "schema_version": BINDING_SCHEMA, "provider": PROVIDER, "model": MODEL,
        "interface": INTERFACE, "questions": QUESTIONS, "state_mapping": MAPPING,
        "field_paths": FIELD_PATHS, "output_contract": "categorical_pair_v2",
        "score_semantics": SCORE_SEMANTICS,
        "evaluators": {key: {**EVALUATORS[key], "identity": "declared_binding_material"}
                       for key in KEYS},
        "web_judge_score_source": WEB_JUDGE_SCORE_SOURCE,
        "programmatic_channel": PROGRAMMATIC_CHANNEL,
        "verified_by": "offline-verifier", "verified_at": "2026-01-01T00:00:00+00:00",
        "verification_evidence_ref": "offline-evidence-ref",
    }


def write_binding(root) -> object:
    path = root / "binding-input.json"
    path.write_text(json.dumps(valid_binding(), ensure_ascii=False), encoding="utf-8")
    return path


class MemoryDataset:
    def __init__(self, name: str, metadata: dict) -> None:
        self.id = str(uuid4())
        self.name = name
        self.metadata = copy.deepcopy(metadata)
        self.items_by_id: dict[str, MemoryDatasetItem] = {}

    @property
    def items(self) -> list["MemoryDatasetItem"]:
        return list(self.items_by_id.values())


class MemoryDatasetItem:
    def __init__(self, dataset: MemoryDataset, *, item_id, input,
                 expected_output, metadata) -> None:
        self.id = item_id
        self.dataset_id = dataset.id
        self.dataset_name = dataset.name
        self.input = copy.deepcopy(input)
        self.expected_output = copy.deepcopy(expected_output)
        self.metadata = copy.deepcopy(metadata)


class MemoryExperiment:
    def __init__(self, experiment: Experiment, items: list[ExperimentItem]) -> None:
        self.experiment = experiment
        self.items = items


class MemoryExperimentsApi:
    def __init__(self, client: "MemoryClient") -> None:
        self._client = client

    def list(self, *, from_start_time, id=None, name=None, fields=None,
             cursor=None, **kwargs):
        matches = [entry.experiment for entry in self._client.experiments.values()
                   if id is None or entry.experiment.id == id]
        matches = [item for item in matches if name is None or item.name == name]
        return ExperimentsResponse(data=matches, meta=ExperimentsResponseMeta(cursor=None))

    def list_items(self, *, from_start_time, experiment_id=None, fields=None,
                   cursor=None, **kwargs):
        entry = self._client.experiments[experiment_id]
        return ExperimentItemsResponse(
            data=list(entry.items), meta=ExperimentsResponseMeta(cursor=None))


class MemoryScoresApi:
    PAGE = 2

    def __init__(self, client: "MemoryClient") -> None:
        self._client = client

    def get_many(self, *, experiment_id=None, trace_id=None, fields=None,
                 limit=None, cursor=None, **kwargs):
        rows = [row for row in self._client.score_rows
                if (experiment_id is None or row[0] == experiment_id)
                and (trace_id is None or row[1] == trace_id)]
        start = int(cursor or 0)
        page = rows[start:start + self.PAGE]
        next_cursor = str(start + self.PAGE) if start + self.PAGE < len(rows) else None
        return GetScoresV3Response(
            data=[row[2] for row in page],
            meta=GetScoresV3Meta(limit=limit or 50, cursor=next_cursor))


class MemoryClient:
    # 覆盖 skills 链路使用的 Langfuse client 方法面：按名解析数据集、按 id upsert、
    # 实验 runner 冻结回放、score 读回分页与本地直写。
    project_id = "project-1"

    def __init__(self) -> None:
        self.datasets: dict[str, MemoryDataset] = {}
        self.experiments: dict[str, MemoryExperiment] = {}
        self.score_rows: list[tuple[str, str, ScoreV3_Categorical]] = []
        self.task_calls = 0
        self.api = SimpleNamespace(experiments=MemoryExperimentsApi(self),
                                   scores=MemoryScoresApi(self))

    def get_dataset(self, name: str) -> MemoryDataset:
        remote = self.datasets.get(name)
        if remote is None:
            raise NotFoundError({})
        return remote

    def create_dataset(self, *, name, description=None, metadata=None, **kwargs):
        assert name not in self.datasets, "重复创建同名远端数据集"
        remote = MemoryDataset(name, metadata or {})
        self.datasets[name] = remote
        return remote

    def create_dataset_item(self, *, dataset_name, id=None, input=None,
                            expected_output=None, metadata=None, **kwargs):
        remote = self.datasets[dataset_name]
        existing = remote.items_by_id.get(id)
        if existing is not None:
            assert (existing.input == input and existing.expected_output == expected_output
                    and existing.metadata == metadata), "同 id 重复提交且内容不一致"
            return existing
        item = MemoryDatasetItem(remote, item_id=id, input=input,
                                 expected_output=expected_output, metadata=metadata)
        remote.items_by_id[id] = item
        return item

    def run_experiment(self, *, name, run_name=None, description=None, data, task,
                       evaluators=None, run_evaluators=None, max_concurrency=50,
                       metadata=None, experiment_id=None, **kwargs):
        # 模拟 SDK：hosted item 携带 dataset_id，task 以 item 关键字冻结回放。
        dataset_id = data[0].dataset_id
        resolved = experiment_id or ("exp-" + uuid4().hex[:12])
        items = []
        for item in data:
            self.task_calls += 1
            output = task(item=item)
            items.append(ExperimentItem(
                id=str(uuid4()), trace_id="trace-" + uuid4().hex[:12],
                start_time=VERIFY_STAMP, end_time=VERIFY_STAMP, level="DEFAULT",
                environment="default", experiment_id=resolved, experiment_name=run_name,
                experiment_item_id=item.id, input=item.input, output=output))
        experiment = Experiment(
            id=resolved, name=run_name, description=description,
            start_time=VERIFY_STAMP, end_time=VERIFY_STAMP, item_count=len(items),
            dataset_id=dataset_id, metadata=copy.deepcopy(metadata))
        existing = self.experiments.get(resolved)
        if existing is not None:
            existing.items.extend(items)
        else:
            self.experiments[resolved] = MemoryExperiment(experiment, items)
        return SimpleNamespace(experiment_id=resolved, run_name=run_name,
                               experiment_url=(
                                   f"https://cloud.langfuse.test/project/{self.project_id}"
                                   f"/experiments/results?baseline={resolved}"),
                               item_results=[])

    def create_score(self, *, name, value, trace_id=None, score_id=None,
                     data_type=None, comment=None, metadata=None, timestamp=None, **kwargs):
        assert trace_id is not None, "本地直写必须绑定 trace"
        score = ScoreV3_Categorical(
            value=value, id=score_id or str(uuid4()), project_id=self.project_id,
            name=name, source=ScoreSource.API, timestamp=timestamp or VERIFY_STAMP,
            environment="default", created_at=VERIFY_STAMP, updated_at=VERIFY_STAMP,
            comment=comment, metadata=copy.deepcopy(metadata),
            subject=ScoreSubjectV3_Trace(id=trace_id))
        self.score_rows.append((self._experiment_of_trace(trace_id), trace_id, score))
        return score

    def seed_score(self, *, trace_id, name, value, source=ScoreSource.EVAL,
                   metadata=None, comment="网页裁判理由", subject="observation"):
        # 网页 evaluator 分数挂在 observation；模拟平台注入的 subject 结构。
        subject = (ScoreSubjectV3_Trace(id=trace_id) if subject == "trace"
                   else ScoreSubjectV3_Observation(id=str(uuid4()), trace_id=trace_id))
        score = ScoreV3_Categorical(
            value=value, id=str(uuid4()), project_id=self.project_id, name=name,
            source=source, timestamp=VERIFY_STAMP, environment="default",
            created_at=VERIFY_STAMP, updated_at=VERIFY_STAMP, comment=comment,
            metadata=copy.deepcopy(metadata), subject=subject)
        self.score_rows.append((self._experiment_of_trace(trace_id), trace_id, score))
        return score

    def _experiment_of_trace(self, trace_id: str) -> str:
        for entry in self.experiments.values():
            if any(item.trace_id == trace_id for item in entry.items):
                return entry.experiment.id
        raise AssertionError("未知 trace，无法归属实验")


def sync_and_submit(root, client):
    binding_path = write_binding(root)
    sync(root, client)
    return binding_path, submit(root, binding_path, client, confirm_cost=True)


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


def test_skills_scoring_binding_contract_langfuse(tmp_path):
    root = tmp_path
    binding = load_binding(write_binding(root))
    assert binding.schema_version == BINDING_SCHEMA
    assert binding.programmatic_channel == PROGRAMMATIC_CHANNEL
    assert all(spec.identity == "declared_binding_material" for spec in binding.evaluators.values())
    for field, message in (("questions", "Question原文"),
                           ("programmatic_channel", "程序化通道声明不一致"),
                           ("score_semantics", "分类语义")):
        broken = valid_binding()
        broken[field] = {} if field != "questions" else {"key": "篡改"}
        path = root / "broken.json"
        path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            load_binding(path)
    identity_drift = valid_binding()
    identity_drift["evaluators"][KEYS[0]]["commit_hash"] = "0" * 64
    path = root / "identity.json"
    path.write_text(json.dumps(identity_drift, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="身份与绑定声明材料不一致"):
        load_binding(path)
    schema_drift = valid_binding()
    schema_drift["schema_version"] = "fit-agent-skills-scoring-2"
    path = root / "schema.json"
    path.write_text(json.dumps(schema_drift, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        load_binding(path)


@pytest.mark.parametrize("value", [
    {"authorization": "redacted"}, {"context": "C:/private/data.txt"},
    {"context": "Bearer redacted"}, {"nested": {"API_KEY": "redacted"}},
    {"context": "LANGFUSE_SECRET_KEY=redacted"},
])
def test_skills_scoring_outbound_privacy_rejects_protected_content(value):
    with pytest.raises(ValueError):
        check_public(value)


def test_skills_scoring_outbound_privacy_rejects_leaked_langfuse_credentials(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-leak")
    with pytest.raises(ValueError, match="凭据"):
        check_public({"comment": "裁判理由中泄漏 pk-lf-leak"})


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


def test_skills_scoring_langfuse_payload_identity(completed_skills_run):
    payload = build_payload(completed_skills_run)
    assert payload["schema_version"] == SCORING_SCHEMA
    assert payload["dataset_name"] == payload["experiment_name"]
    assert payload["dataset_name"].startswith("fit-agent-skills-diagnostic-langfuse-")
    assert payload["dataset_name"].endswith(payload["source_sha256"][:12])
    assert build_payload(completed_skills_run) == payload
    row = payload["rows"][0]
    seed_material = copy.deepcopy(row["upload"])
    del seed_material["inputs"]["sample_id"]
    assert row["row_id"] == str(uuid5(
        NAMESPACE_URL,
        f"{SCORING_SCHEMA}:{payload['run_id']}:{row['upload']['metadata']['group']}:"
        f"{row['upload']['metadata']['case_id']}:{identity_digest(seed_material)}"))
    assert row["upload"]["inputs"]["sample_id"] == row["row_id"]
    assert row["upload"]["outputs"]["answer"]["visible_text"] == "离线冻结的真实回答正文。"


def test_skills_scoring_langfuse_dataset_sync_lifecycle(completed_skills_run):
    client = MemoryClient()
    payload = build_payload(completed_skills_run)
    row = payload["rows"][0]
    receipt = sync(completed_skills_run, client)
    remote = client.datasets[payload["dataset_name"]]
    assert receipt["dataset_id"] == str(remote.id)
    assert receipt["item_ids"] == [row["row_id"]]
    assert set(remote.items_by_id) == {row["row_id"]}
    assert not (scoring_dir(completed_skills_run) / "dataset.pending.json").exists()
    # 幂等：回执存在时重新 sync 只核验，不重复建库或建样例。
    again = sync(completed_skills_run, client)
    assert again["dataset_id"] == receipt["dataset_id"]
    assert len(remote.items_by_id) == 1
    # 同 id 同内容 upsert 幂等；远端内容漂移与未知样例均阻断。
    client.create_dataset_item(dataset_name=payload["dataset_name"], id=row["row_id"],
                               input=row["upload"]["inputs"],
                               expected_output=row["upload"]["reference_outputs"],
                               metadata=row["upload"]["metadata"])
    remote.items_by_id[row["row_id"]].input = {"user_request": {"prompt": "篡改"}}
    with pytest.raises(ValueError, match="远端样本内容漂移"):
        sync(completed_skills_run, client)
    remote.items_by_id[row["row_id"]].input = copy.deepcopy(row["upload"]["inputs"])
    remote.items_by_id[str(uuid4())] = MemoryDatasetItem(
        remote, item_id=str(uuid4()), input={}, expected_output={}, metadata={})
    with pytest.raises(ValueError, match="远端样本集合漂移"):
        sync(completed_skills_run, client)
    # 回执存在但远端数据集被删除：禁止自动重建。
    del remote.items_by_id[next(iter(set(remote.items_by_id) - {row["row_id"]}))]
    del client.datasets[payload["dataset_name"]]
    with pytest.raises(ValueError, match="禁止自动重建"):
        sync(completed_skills_run, client)


def test_skills_scoring_langfuse_dataset_pending_resume_and_backfill(completed_skills_run):
    client = MemoryClient()
    payload = build_payload(completed_skills_run)
    row = payload["rows"][0]
    receipt = sync(completed_skills_run, client)
    dataset_id = receipt["dataset_id"]
    # 模拟中断：回执删除、远端样例缺失、待处置回执仍持有真实 dataset_id。
    (scoring_dir(completed_skills_run) / "dataset.json").unlink()
    client.datasets[payload["dataset_name"]].items_by_id.pop(row["row_id"])
    pending = scoring_dir(completed_skills_run) / "dataset.pending.json"
    pending.write_text(json.dumps({**receipt_metadata(payload), "dataset_id": dataset_id}),
                       encoding="utf-8")
    resumed = sync(completed_skills_run, client)
    assert resumed["dataset_id"] == dataset_id
    assert set(client.datasets[payload["dataset_name"]].items_by_id) == {row["row_id"]}
    assert (scoring_dir(completed_skills_run) / "dataset.json").is_file()
    assert not pending.exists()
    # 待处置回执与本 payload 不一致：禁止自动复用。
    (scoring_dir(completed_skills_run) / "dataset.json").unlink()
    pending.write_text(json.dumps({**receipt_metadata(payload),
                                   "payload_sha256": "0" * 64, "dataset_id": dataset_id}),
                       encoding="utf-8")
    with pytest.raises(ValueError, match="禁止自动复用"):
        sync(completed_skills_run, client)


def test_skills_scoring_langfuse_submit_receipt_and_runs_mapping(completed_skills_run):
    client = MemoryClient()
    binding_path = write_binding(completed_skills_run)
    dataset_receipt = sync(completed_skills_run, client)
    receipt = submit(completed_skills_run, binding_path, client, confirm_cost=True)
    payload = build_payload(completed_skills_run)
    row = payload["rows"][0]
    assert receipt["experiment_name"] == payload["experiment_name"]
    assert receipt["dataset_id"] == dataset_receipt["dataset_id"]
    assert receipt["binding_sha256"] == identity_digest(load_binding(binding_path).model_dump())
    assert receipt["item_ids"] == [row["row_id"]]
    assert set(receipt["runs"]) == {row["row_id"]}
    trace_id = receipt["runs"][row["row_id"]]
    assert trace_id in {item.trace_id for item in client.experiments[receipt["experiment_id"]].items}
    assert receipt["evaluators_declared"] == {key: EVALUATORS[key] for key in KEYS}
    assert receipt["evaluator_material"] == "declared_binding_not_score_verified"
    assert (scoring_dir(completed_skills_run) / "binding.json").is_file()
    assert not (scoring_dir(completed_skills_run) / "experiment.pending.json").exists()
    assert client.task_calls == 1
    # 重复提交走回执核验，不再触发新的实验运行。
    assert submit(completed_skills_run, binding_path, client, confirm_cost=True) == receipt
    assert client.task_calls == 1
    # 回执映射漂移阻断。
    tampered = dict(receipt)
    tampered["runs"] = {row["row_id"]: "trace-forged"}
    (scoring_dir(completed_skills_run) / "experiment.json").write_text(
        json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="实验回执漂移|实验运行映射漂移"):
        submit(completed_skills_run, binding_path, client, confirm_cost=True)


def test_skills_scoring_langfuse_submit_gates_and_pending_block(completed_skills_run):
    client = MemoryClient()
    binding_path = write_binding(completed_skills_run)
    sync(completed_skills_run, client)
    with pytest.raises(ValueError, match="确认真实裁判费用"):
        submit(completed_skills_run, binding_path, client, confirm_cost=False)
    with pytest.raises(ValueError, match="实际网页Evaluator"):
        submit(completed_skills_run, completed_skills_run / "missing-binding.json",
               client, confirm_cost=True)
    pending = scoring_dir(completed_skills_run) / "experiment.pending.json"
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="提交中断"):
        submit(completed_skills_run, binding_path, client, confirm_cost=True)


def test_skills_scoring_langfuse_collect_web_evaluator_scores(completed_skills_run):
    client = MemoryClient()
    _, receipt = sync_and_submit(completed_skills_run, client)
    payload = build_payload(completed_skills_run)
    row = payload["rows"][0]
    trace_id = receipt["runs"][row["row_id"]]
    client.seed_score(trace_id=trace_id, name=KEYS[0], value="pass")
    client.seed_score(trace_id=trace_id, name=KEYS[1], value="absent")
    # 计划外指标分数仍在同一实验下；小页宽强制 collect 走 cursor 分页。
    client.seed_score(trace_id=trace_id, name="other_metric", value="x")
    result = collect(completed_skills_run, client)
    assert result["count"] == 3
    feedback, binding = load_feedback(completed_skills_run, payload)
    entry = feedback[("A1", row["upload"]["metadata"]["case_id"])]
    assert binding is not None
    assert entry["trace_id"] == trace_id
    assert entry["results"][KEYS[0]]["status"] == "scored"
    assert entry["results"][KEYS[0]]["category"] == "pass"
    assert entry["results"][KEYS[0]]["channel"] == "web_evaluator"
    assert entry["results"][KEYS[0]]["source"] == "EVAL"
    assert entry["results"][KEYS[0]]["score"] is None
    assert entry["results"][KEYS[1]]["category"] == "absent"
    summary = summarize(completed_skills_run)
    scored_row = next(item for item in summary["rows"] if item["evidence"])
    assert scored_row["scoring"] == "scored"
    assert scored_row["passed"] is True and scored_row["critical_error"] is False
    assert summary["binding"]["schema_version"] == BINDING_SCHEMA


def test_skills_scoring_langfuse_collect_normalization_rejects_drift(completed_skills_run):
    client = MemoryClient()
    _, receipt = sync_and_submit(completed_skills_run, client)
    payload = build_payload(completed_skills_run)
    row = payload["rows"][0]
    trace_id = receipt["runs"][row["row_id"]]
    # 类别不在声明 categories 内：阻断。
    client.seed_score(trace_id=trace_id, name=KEYS[0], value="excellent")
    client.seed_score(trace_id=trace_id, name=KEYS[1], value="absent")
    with pytest.raises(ValueError, match="既定类别"):
        collect(completed_skills_run, client)
    # 程序化通道（API 来源）缺少绑定声明字段：阻断。
    client.score_rows.clear()
    client.seed_score(trace_id=trace_id, name=KEYS[0], value="pass",
                      source=ScoreSource.API, metadata={"ls_provider": PROVIDER})
    client.seed_score(trace_id=trace_id, name=KEYS[1], value="absent",
                      source=ScoreSource.API, metadata={"model_name": MODEL})
    with pytest.raises(ValueError, match="程序化反馈声明与绑定不一致"):
        collect(completed_skills_run, client)
    # 未声明的来源（ANNOTATION）：阻断。
    client.score_rows.clear()
    client.seed_score(trace_id=trace_id, name=KEYS[0], value="pass",
                      source=ScoreSource.ANNOTATION)
    with pytest.raises(ValueError, match="来源未在绑定中声明"):
        collect(completed_skills_run, client)
    # 实验内出现无法归属运行映射的分数：阻断。
    client.score_rows.clear()
    client.score_rows.append((receipt["experiment_id"], "trace-unknown", ScoreV3_Categorical(
        value="pass", id=str(uuid4()), project_id=client.project_id, name=KEYS[0],
        source=ScoreSource.EVAL, timestamp=VERIFY_STAMP, environment="default",
        created_at=VERIFY_STAMP, updated_at=VERIFY_STAMP,
        subject=ScoreSubjectV3_Trace(id="trace-unknown"))))
    with pytest.raises(ValueError, match="反馈来自未知运行"):
        collect(completed_skills_run, client)
    # 同一 Evaluator 冲突反馈：阻断。
    client.score_rows.clear()
    client.seed_score(trace_id=trace_id, name=KEYS[0], value="pass")
    client.seed_score(trace_id=trace_id, name=KEYS[0], value="fail")
    with pytest.raises(ValueError, match="冲突反馈"):
        collect(completed_skills_run, client)


def test_skills_scoring_langfuse_collect_rejects_credential_leak(completed_skills_run,
                                                                 monkeypatch):
    client = MemoryClient()
    _, receipt = sync_and_submit(completed_skills_run, client)
    row = build_payload(completed_skills_run)["rows"][0]
    trace_id = receipt["runs"][row["row_id"]]
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-leaked")
    client.seed_score(trace_id=trace_id, name=KEYS[0], value="pass",
                      comment="裁判理由泄漏 sk-lf-leaked")
    client.seed_score(trace_id=trace_id, name=KEYS[1], value="absent")
    with pytest.raises(ValueError, match="凭据"):
        collect(completed_skills_run, client)


class StubCompletions:
    # 真实 ask 调用面下的离线裁判替身：返回与请求键一致的严格 JSON。
    def __init__(self, categories=None):
        self.calls = []
        self.categories = categories or {KEYS[0]: "pass", KEYS[1]: "absent"}

    def create(self, *, model, messages, response_format, temperature):
        text = messages[0]["content"]
        self.calls.append(text)
        key = next(k for k in KEYS if f'"{k}"' in text)
        category = self.categories[key]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps({key: category, "comment": "离线裁判理由"})))],
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7))


class StubOpenAI:
    def __init__(self, completions):
        self.chat = SimpleNamespace(completions=completions)


def test_skills_judge_gates_require_explicit_confirmation(completed_skills_run,
                                                          monkeypatch):
    with pytest.raises(ValueError, match="--enable-judge"):
        skills_judge.main(["--source", str(completed_skills_run)])
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GLM_API_KEY"):
        skills_judge.main(["--source", str(completed_skills_run), "--enable-judge"])


def test_skills_judge_writes_langfuse_scores_and_skips_completed(completed_skills_run):
    client = MemoryClient()
    _, receipt = sync_and_submit(completed_skills_run, client)
    payload = build_payload(completed_skills_run)
    row = payload["rows"][0]
    completions = StubCompletions()
    result = skills_judge.run_judge(
        source=completed_skills_run, group=None, limit=30,
        langfuse_client=client, openai_client=StubOpenAI(completions))
    assert result == {"rows_in_scope": 1, "scored_this_run": 1, "already_complete": 0}
    assert len(completions.calls) == 2
    scores = {entry[2].name: entry[2] for entry in client.score_rows}
    assert set(scores) == set(KEYS)
    scored_at = datetime.fromisoformat(receipt["submitted_at"])
    for key in KEYS:
        score = scores[key]
        assert score.data_type == "CATEGORICAL"
        assert score.value == ("pass" if key == KEYS[0] else "absent")
        assert score.id == judge_score_id(payload["run_id"], row["row_id"], key)
        assert score.timestamp == scored_at
        assert score.source == ScoreSource.API
        meta = score.metadata
        assert meta["ls_provider"] == PROVIDER and meta["model_name"] == MODEL
        assert meta["method"] == PROGRAMMATIC_CHANNEL["method"]
        assert meta["evaluator_repo_handle"] == EVALUATORS[key]["repo_handle"]
        assert meta["evaluator_repo_id"] == EVALUATORS[key]["repo_id"]
        assert meta["evaluator_commit_hash"] == EVALUATORS[key]["commit_hash"]
        assert meta["judge_usage"] == {"input_tokens": 11, "output_tokens": 7}
    # 幂等：已齐键跳过，不再触发真实裁判调用。
    repeat = skills_judge.run_judge(
        source=completed_skills_run, group=None, limit=30,
        langfuse_client=client, openai_client=StubOpenAI(completions))
    assert repeat["already_complete"] == 1 and repeat["scored_this_run"] == 0
    assert len(completions.calls) == 2
    # 程序化通道写回后，collect 按绑定声明口径接受并归一化。
    collect(completed_skills_run, client)
    feedback, _ = load_feedback(completed_skills_run, payload)
    entry = feedback[("A1", row["upload"]["metadata"]["case_id"])]
    assert entry["results"][KEYS[0]]["channel"] == "programmatic_judge"
    assert entry["results"][KEYS[0]]["judge_usage"]["input_tokens"] == 11


def test_skills_judge_rejects_invalid_category(completed_skills_run):
    client = MemoryClient()
    sync_and_submit(completed_skills_run, client)
    completions = StubCompletions(categories={KEYS[0]: "excellent", KEYS[1]: "absent"})
    with pytest.raises(ValueError, match="裁判返回类别非法"):
        skills_judge.run_judge(
            source=completed_skills_run, group=None, limit=30,
            langfuse_client=client, openai_client=StubOpenAI(completions))
    assert not client.score_rows


def test_skills_scoring_verify_dataset_requires_exact_receipt_digest(completed_skills_run):
    client = MemoryClient()
    payload = build_payload(completed_skills_run)
    receipt = sync(completed_skills_run, client)
    receipt["payload_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="数据集回执摘要漂移"):
        verify_dataset(client, payload, receipt)
