import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from app.agent.tools.files import (
    ReadArguments,
    WorkspaceAccessDenied,
    WriteArguments,
    create_file_tools,
)
from evaluation.scenarios import RuntimeHandle, _verify_initial_projection
from evaluation.skills_generation import (
    EMPTY_STATE,
    GROUPS,
    SampleRecord,
    group_prompt,
    identity_digest,
    load_cases,
    load_sample,
    sample_prompt,
    seal_record,
    selected_knowledge,
    topic_file,
    user_input,
)


def test_skills_generation_fixed_dataset_and_input_isolation():
    cases = load_cases()
    assert len(cases) == 30
    assert sum(case.should_use_skill for case in cases) == 24
    assert sum(not case.should_use_skill for case in cases) == 6
    for case in cases:
        assert user_input(case) == {"prompt": case.prompt, "context": case.context["content"]}
        assert case.review["status"] == "draft"
        assert set(user_input(case)).isdisjoint({"expected", "critical_errors", "review", "topic_paths"})
    assert EMPTY_STATE == {"profile": None, "plans": [], "workouts": []}


def test_skills_generation_group_prompts_and_preselected_bodies():
    case = load_cases()[0]
    assert GROUPS == {"A0": "without_skill", "A1": "auto_skill", "A2": "selected_knowledge"}
    assert "<available_skills>" in group_prompt("A1")
    assert "skills/" not in group_prompt("A0")
    assert "skills/" not in group_prompt("A2")
    assert selected_knowledge(case, "A0") == []
    assert selected_knowledge(case, "A1") == []
    knowledge = selected_knowledge(case, "A2")
    assert len(knowledge) == 1
    assert knowledge[0]["content"] == topic_file(case.topic_paths[0]).read_text(encoding="utf-8")
    assert knowledge[0]["content"] in sample_prompt("A2", knowledge)
    for negative in load_cases()[24:]:
        assert selected_knowledge(negative, "A2") == []


@pytest.mark.parametrize("path", [
    "backend/skills/training-theory/references/../../../../PRODUCT.md",
    "backend/skills/other/references/a.md", "/backend/skills/training-theory/references/a.md",
    "C:/backend/skills/training-theory/references/a.md",
])
def test_skills_generation_topic_boundary(path):
    with pytest.raises((ValueError, PermissionError)):
        topic_file(path)


def test_skills_generation_real_file_permissions_and_workspace_isolation(tmp_path):
    session = str(uuid4())
    other = str(uuid4())
    off = create_file_tools(session, tmp_root=tmp_path, allow_skills=False)
    on = create_file_tools(session, tmp_root=tmp_path)
    args = ReadArguments(path="skills/training-theory/SKILL.md")
    with pytest.raises(WorkspaceAccessDenied):
        off["read"].execute("denied", args, None, None)
    actual = on["read"].execute("allowed", args, None, None)
    assert "training-theory" in actual.content[0].text
    path = f"sessions/{session}/workspace/note.txt"
    off["write"].execute("write", WriteArguments(path=path, content="离线文件边界"), None, None)
    assert "离线文件边界" in off["read"].execute("read", ReadArguments(path=path), None, None).content[0].text
    foreign = create_file_tools(other, tmp_root=tmp_path, allow_skills=False)
    with pytest.raises(WorkspaceAccessDenied):
        foreign["read"].execute("foreign", ReadArguments(path=path), None, None)
    with pytest.raises(WorkspaceAccessDenied):
        off["read"].execute("escape", ReadArguments(path="../skills/training-theory/SKILL.md"), None, None)


def test_skills_generation_failed_artifact_resume_identity(tmp_path):
    # 无模型输出的失败记录用于核验续跑协议。
    record = seal_record(SampleRecord(
        run_id="offline", group="A0", case_id="skills-v1-001",
        identity_sha256="1" * 64, case_sha256="2" * 64,
        user_input=user_input(load_cases()[0]), initial_state=EMPTY_STATE,
        selected_knowledge=[], system_prompt=group_prompt("A0"), session_id=str(uuid4()),
        started_at="2026-01-01T00:00:00+00:00", finished_at="2026-01-01T00:00:01+00:00",
        status="blocked", error="ModelNotConfigured", event=None, model_requests=[],
        model_call_count=0, retrieval=[],
    ))
    path = tmp_path / "sample.json"
    path.write_text(record.model_dump_json(), encoding="utf-8")
    expected = dict(run_id="offline", group="A0", case_id="skills-v1-001",
                    identity_sha256="1" * 64, case_sha256="2" * 64)
    assert load_sample(path, **expected) == record
    with pytest.raises(ValueError, match="身份"):
        load_sample(path, **{**expected, "identity_sha256": "3" * 64})
    path.write_text(record.model_copy(update={"error": "changed"}).model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="摘要"):
        load_sample(path, **expected)
    assert identity_digest({"data": 1}) != identity_digest({"data": 2})


def test_skills_generation_real_isolated_business_database(tmp_path):
    async def check():
        for name in ("first", "second"):
            root = tmp_path / name
            root.mkdir()
            runtime = RuntimeHandle(root=root, evaluation_run_id="offline", scenario_id=name)
            try:
                await runtime.open()
                session_id = str(uuid4())
                await runtime.service.create_session_result(session_id, name, credentials=())
                projection = await runtime.projection([session_id])
                _verify_initial_projection(projection, EMPTY_STATE)
                assert projection.profile is None
                assert projection.plans == []
                assert projection.workouts == []
                assert runtime.db_path.is_relative_to(root)
            finally:
                await runtime.close()
        assert (tmp_path / "first/fit-agent.db").is_file()
        assert (tmp_path / "second/fit-agent.db").is_file()

    asyncio.run(check())


def test_skills_generation_normalizes_registered_topic():
    path = "backend/skills/training-theory/references/progression.md"
    assert topic_file(path) == topic_file(path.replace("/", "\\"))
    assert topic_file(path) == topic_file(path.replace("references/", "references/./"))
    assert isinstance(topic_file(path), Path)
