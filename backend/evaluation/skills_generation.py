import argparse
import asyncio
import hashlib
import json
import math
import posixpath
import re
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import jsonlines
from pydantic import BaseModel, ConfigDict

from app.agent.prompts import SYSTEM_PROMPT, build_system_prompt
from app.agent.skills import SKILLS_ROOT, resolve_skill_path
from app.agent.tools.files import create_file_tools
from app.application.business.catalog import DEFAULT_ACTIONS_PATH, DEFAULT_SCHEMA_PATH
from app.application.session.execution import compaction_settings
from app.model_config import load_model_config
from evaluation.scenario_evidence import (
    canonical_json,
    model_identity_summary,
    sha256_text,
)
from evaluation.scenarios import (
    BACKEND,
    EventCapture,
    RuntimeHandle,
    _run_message_event,
    _verify_initial_projection,
    _write_json,
    code_identity,
)

DATASET = BACKEND / "dataset/skills-evaluation/v1/cases.jsonl"
RUN_ROOT = BACKEND / "temp/skills-generation"
SCHEMA = "fit-agent-skills-generation-1"
GROUPS = {"A0": "without_skill", "A1": "auto_skill", "A2": "selected_knowledge"}
EMPTY_STATE = {"profile": None, "plans": [], "workouts": []}
INPUT_SOURCE = "synthetic_user_supplied_background_empty_business_state"


class SkillCase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str
    type: str
    tags: list[str]
    prompt: str
    context: dict
    should_use_skill: bool
    topic_paths: list[str]
    expected: list[str]
    critical_errors: list[str]
    allowed_variation: list[str]
    reference_answer_points: list[str]
    reference_sources: list[str]
    review: dict


class SampleRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: str = SCHEMA
    run_id: str
    group: str
    case_id: str
    identity_sha256: str
    case_sha256: str
    reference_status: str = "diagnostic_draft_reference"
    input_source: str = INPUT_SOURCE
    user_input: dict
    initial_state: dict
    selected_knowledge: list[dict]
    system_prompt: str
    session_id: str
    started_at: str
    finished_at: str
    status: str
    error: str | None
    event: EventCapture | None
    model_requests: list[dict]
    model_call_count: int
    retrieval: list[dict]
    execution_sha256: str = ""


def digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def topic_file(path: str) -> Path:
    normalized = posixpath.normpath(path.replace("\\", "/"))
    prefix = "backend/skills/training-theory/references/"
    if not normalized.startswith(prefix):
        raise ValueError("主题路径超出登记知识边界")
    target = resolve_skill_path(normalized.removeprefix("backend/"))
    if not target.is_file():
        raise ValueError("主题文件不存在")
    return target


def load_cases(path: Path = DATASET) -> list[SkillCase]:
    with jsonlines.open(path) as reader:
        cases = [SkillCase.model_validate(row) for row in reader]
    if [case.id for case in cases] != [f"skills-v1-{i:03d}" for i in range(1, 31)]:
        raise ValueError("必须为固定30题且ID顺序一致")
    if sum(case.should_use_skill for case in cases) != 24:
        raise ValueError("必须为24个训练题及6个负例")
    for case in cases:
        if case.context.get("synthetic") is not True or not case.context.get("content"):
            raise ValueError("背景必须明确为合成用户输入")
        if case.review.get("status") != "draft" or case.review.get("external_verified") is not False:
            raise ValueError("参考必须保持未外部核实的draft身份")
        if bool(case.topic_paths) != case.should_use_skill:
            raise ValueError("负例不得预置训练知识")
        for topic in case.topic_paths:
            topic_file(topic)
    return cases


def user_input(case: SkillCase) -> dict:
    return {"prompt": case.prompt, "context": case.context["content"]}


def group_prompt(group: str) -> str:
    if group not in GROUPS:
        raise ValueError("未知组")
    if group == "A1":
        return build_system_prompt()
    prompt = SYSTEM_PROMPT
    start = prompt.index("技能使用：")
    end = prompt.index("执行边界：", start)
    prompt = prompt[:start] + prompt[end:]
    for clause in (
        "调用前按需读取 skills/training-theory/SKILL.md 中与计划编排相关的主题；",
        "调用前按需读取 skills/training-theory/SKILL.md 中与停滞、变化与调整相关的主题；",
        "与固定 Skills 只读命名空间 skills/",
    ):
        if clause not in prompt:
            raise ValueError("生产提示发生变化，需要核对知识可用性边界")
        prompt = prompt.replace(clause, "")
    return prompt


def selected_knowledge(case: SkillCase, group: str) -> list[dict]:
    if group != "A2":
        return []
    return [
        {"path": path.relative_to(BACKEND).as_posix(),
         "sha256": digest_file(path), "content": path.read_text(encoding="utf-8")}
        for path in dict.fromkeys(topic_file(topic) for topic in case.topic_paths)
    ]


def sample_prompt(group: str, knowledge: list[dict]) -> str:
    prompt = group_prompt(group)
    if knowledge:
        prompt += "\n\n预选通用知识（独立于用户事实）：\n" + "\n\n".join(
            f"[{item['path']}]\n{item['content']}" for item in knowledge
        )
    return prompt


def run_identity(cases: list[SkillCase], model, budget_seconds: float) -> dict:
    return {
        "schema_version": SCHEMA,
        "data_sha256": digest_file(DATASET),
        "cases": {case.id: sha256_text(canonical_json(case.model_dump())) for case in cases},
        "code": code_identity(),
        "model": model_identity_summary(model),
        "parameters": {
            "budget_seconds": budget_seconds, "model_parameters": "production_defaults",
            "compaction": asdict(compaction_settings()),
        },
        "catalog": {"actions_sha256": digest_file(DEFAULT_ACTIONS_PATH),
                    "schema_sha256": digest_file(DEFAULT_SCHEMA_PATH)},
        "groups": GROUPS,
        "prompts": {group: group_prompt(group) for group in GROUPS},
        "knowledge": {
            path.relative_to(BACKEND).as_posix(): digest_file(path)
            for path in sorted(SKILLS_ROOT.rglob("*.md"))
        },
        "initial_state": EMPTY_STATE,
        "input_source": INPUT_SOURCE,
        "reference_status": "diagnostic_draft_reference",
        "expected_samples": 90,
    }


def identity_digest(identity: dict) -> str:
    return sha256_text(canonical_json(identity))


def seal_record(record: SampleRecord) -> SampleRecord:
    payload = record.model_dump(mode="json")
    payload.pop("execution_sha256")
    return record.model_copy(update={"execution_sha256": identity_digest(payload)})


def load_sample(path: Path, *, run_id: str, group: str, case_id: str,
                identity_sha256: str, case_sha256: str) -> SampleRecord:
    record = SampleRecord.model_validate_json(path.read_text(encoding="utf-8"))
    if (record.schema_version, record.run_id, record.group, record.case_id,
        record.identity_sha256, record.case_sha256) != (
        SCHEMA, run_id, group, case_id, identity_sha256, case_sha256
    ):
        raise ValueError("样本身份不一致")
    if record.status not in {"completed", "failed", "blocked"}:
        raise ValueError("样本状态非法")
    if seal_record(record).execution_sha256 != record.execution_sha256:
        raise ValueError("样本摘要不一致")
    for call in record.model_requests:
        request_path = Path(call["request_file"])
        if request_path.is_absolute() or ".." in request_path.parts:
            raise ValueError("请求证据路径越界")
        if digest_file(path.parent / request_path) != call["request_sha256"]:
            raise ValueError("请求证据摘要不一致")
    return record


async def execute_case(case: SkillCase, group: str, run_id: str, root: Path,
                       identity: dict, model, budget_seconds: float) -> SampleRecord:
    root.mkdir(parents=True, exist_ok=False)
    session_id = str(uuid4())
    knowledge = selected_knowledge(case, group)
    prompt = sample_prompt(group, knowledge)
    runtime = RuntimeHandle(root=root, evaluation_run_id=run_id, scenario_id=case.id)
    started = datetime.now(UTC).isoformat()
    event = None
    status, error = "failed", None
    requests = []
    record = None
    try:
        await runtime.open()
        await runtime.service.create_session_result(session_id, case.id, credentials=())
        projection = await runtime.projection([session_id])
        _verify_initial_projection(projection, EMPTY_STATE)
        _write_json(root / "initial-state.json", {
            "fixture": EMPTY_STATE, "input_source": INPUT_SOURCE,
            "projection": projection.model_dump(mode="json"),
        })
        tools = create_file_tools(session_id, tmp_root=root / "tmp", allow_skills=group == "A1")
        runtime.event_id = "message"
        event = await _run_message_event(
            runtime, None,
            {"scenario_id": case.id, "input": {"attachments": []}},
            {"event_id": "message", "content": case.prompt + "\n\n" + case.context["content"],
             "attachments": []},
            0, session_id, [session_id], int(datetime.now(UTC).timestamp() * 1000),
            model=model, file_tools=tools, system_prompt=prompt, budget_seconds=budget_seconds,
        )
        status = "completed" if event.error is None else "failed"
        error = event.error
    except Exception as failure:
        # 编排层保留真实故障；错误正文仅由既有凭据过滤链路公开。
        error = type(failure).__name__
        status = "blocked" if event is None else "failed"
        raise
    finally:
        if runtime.call_recorder is not None:
            requests = [
                {**call.model_dump(mode="json"),
                 "request_file": "model-calls/" + call.request_path}
                for call in runtime.call_recorder.records
            ]
        record = seal_record(SampleRecord(
            run_id=run_id, group=group, case_id=case.id,
            identity_sha256=identity_digest(identity), case_sha256=identity["cases"][case.id],
            user_input=user_input(case), initial_state=EMPTY_STATE,
            selected_knowledge=knowledge, system_prompt=prompt, session_id=session_id,
            started_at=started, finished_at=datetime.now(UTC).isoformat(),
            status=status, error=error, event=event, model_requests=requests,
            model_call_count=len(requests),
            retrieval=[item.model_dump(mode="json") for item in runtime.search_records],
        ))
        _write_json(root / "sample.json", record.model_dump(mode="json"))
        await runtime.close()
    return record


async def run_selected(args) -> int:
    if not args.enable_model:
        raise ValueError("必须显式--enable-model；此操作调用真实服务")
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    lock = RUN_ROOT / "generation.lock"
    with lock.open("x", encoding="utf-8") as handle:
        handle.write(args.run_id)
    try:
        return await _run_selected(args)
    finally:
        lock.unlink()


async def _run_selected(args) -> int:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", args.run_id):
        raise ValueError("run_id非法")
    if not math.isfinite(args.budget_seconds) or not 0 < args.budget_seconds <= 300:
        raise ValueError("样本预算须在0至300秒内")
    cases = load_cases()
    ids = args.case or [case.id for case in cases]
    if len(set(ids)) != len(ids) or set(ids) - {case.id for case in cases}:
        raise ValueError("case未知或重复")
    if not 1 <= args.limit <= 5:
        raise ValueError("每批limit须在1至5内")
    if not args.enable_model:
        raise ValueError("必须显式--enable-model；此操作调用真实服务")
    model = load_model_config()
    identity = run_identity(cases, model, args.budget_seconds)
    root = RUN_ROOT / args.run_id
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / "run.json"
    document = {"run_id": args.run_id, "identity": identity, "identity_sha256": identity_digest(identity)}
    if manifest.exists():
        if json.loads(manifest.read_text(encoding="utf-8")) != document:
            raise ValueError("续跑身份不一致")
    else:
        if any(root.iterdir()):
            raise ValueError("运行目录已有未绑定产物")
        _write_json(manifest, document)
    pending = []
    for case in cases:
        for group in GROUPS:
            target = root / group / case.id
            sample = target / "sample.json"
            if sample.exists():
                load_sample(sample, run_id=args.run_id, group=group, case_id=case.id,
                            identity_sha256=document["identity_sha256"], case_sha256=identity["cases"][case.id])
            elif target.exists():
                raise ValueError("样本执行中断且无终结产物，拒绝静默重跑")
            elif case.id in ids and group == args.group:
                pending.append((case, group, target))
    for case, group, target in pending[:args.limit]:
        record = await execute_case(case, group, args.run_id, target, identity, model, args.budget_seconds)
        if record.status != "completed":
            return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="30题三组单次诊断生成；串行小批与缺失样本续跑")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--group", choices=GROUPS, required=True)
    parser.add_argument("--case", action="append")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--budget-seconds", type=float, default=240)
    parser.add_argument("--enable-model", action="store_true")
    return asyncio.run(run_selected(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
