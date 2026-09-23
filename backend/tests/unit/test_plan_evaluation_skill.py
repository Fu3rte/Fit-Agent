from typing import cast

import pytest
from langgraph.runtime import Runtime

from app.application.agent.contracts import (
    ADJUSTMENT_SKILL_NAME,
    PLAN_EVALUATION_SKILL_NAME,
    PLANNING_SKILL_NAME,
    GeneratePlanRun,
    PlanLlmNodeDeps,
    ToolEvidence,
    WorkflowState,
)
from app.application.agent.plan_nodes import (
    _evaluation_result,
    load_skill,
)
from app.domain.plans.schema import (
    SNAPSHOT_MISMATCH_CODE,
    DeterministicResult,
    RubricResult,
)
from app.infrastructure.skills.loader import SkillLoader
from config import skills_dir


def _rubric(
    *, goal: bool = True, schedule: bool = True, explanation: bool = True
) -> RubricResult:
    return RubricResult.model_validate({
        "goal_alignment": {"passed": goal, "reason": "目标"},
        "schedule_reasonableness": {"passed": schedule, "reason": "日程"},
        "explanation_quality": {"passed": explanation, "reason": "解释"},
    })


def test_plan_evaluation_skill_reads_the_rubric_reference_by_path() -> None:
    loader = SkillLoader(skills_dir())
    metadata = {
        item.name: item for item in loader.list_metadata()
    }[PLAN_EVALUATION_SKILL_NAME]
    body = loader.read_skill(metadata.name)
    rubric = loader.read_reference(
        metadata.name, "references/evaluation-rubric.md"
    )

    assert metadata.description
    assert "RubricResult" in body
    assert rubric.path == "references/evaluation-rubric.md"
    assert "goal_alignment" in rubric.text
    assert "schedule_reasonableness" in rubric.text
    assert "explanation_quality" in rubric.text


@pytest.mark.parametrize(
    ("intent", "name", "rules_path"),
    (
        ("generate_plan", PLANNING_SKILL_NAME, "references/planning-rules.md"),
        ("adjust_plan", ADJUSTMENT_SKILL_NAME, "references/adjustment-rules.md"),
    ),
)
async def test_planner_skill_loads_only_the_selected_rules_reference(
    intent: str, name: str, rules_path: str
) -> None:
    loaded = await load_skill(
        cast("WorkflowState", {"intent": intent}),
        cast("Runtime[GeneratePlanRun]", None),
        skills=SkillLoader(skills_dir()),
    )
    planner_skill = loaded.get("loaded_skill")
    assert planner_skill is not None

    assert planner_skill.metadata.name == name
    assert planner_skill.body.strip()
    assert tuple(reference.path for reference in planner_skill.references) == (rules_path,)


async def test_planner_skill_selection_rejects_non_plan_intents() -> None:
    with pytest.raises(ValueError, match="未登记的 Planner Intent"):
        await load_skill(
            cast("WorkflowState", {"intent": "general"}),
            cast("Runtime[GeneratePlanRun]", None),
            skills=SkillLoader(skills_dir()),
        )


def test_evaluation_rules_cover_gates_warning_and_revision_mismatch() -> None:
    deterministic = DeterministicResult(passed=True, failures=())
    evidence = (
        ToolEvidence(
            tool_name="read_user_profile", revision_domain="profile", revision=1
        ),
    )

    for rubric in (_rubric(goal=False), _rubric(schedule=False)):
        result = _evaluation_result(
            deterministic, rubric, 0, candidate=evidence, evaluation=evidence
        )
        assert result.passed is False
        assert result.blocking_failures

    warning = _evaluation_result(
        deterministic,
        _rubric(explanation=False),
        0,
        candidate=evidence,
        evaluation=evidence,
    )
    assert warning.passed is True
    assert warning.blocking_failures == ()
    assert warning.warnings == ("explanation_quality: 解释",)

    mismatch = _evaluation_result(
        deterministic,
        _rubric(),
        0,
        candidate=evidence,
        evaluation=(
            ToolEvidence(
                tool_name="read_user_profile", revision_domain="profile", revision=2
            ),
        ),
    )
    assert mismatch.passed is False
    assert {failure.code for failure in mismatch.deterministic.failures} == {
        SNAPSHOT_MISMATCH_CODE
    }


def test_evaluator_dependencies_expose_no_business_write_boundary() -> None:
    assert set(PlanLlmNodeDeps.__dataclass_fields__).isdisjoint(
        {"writes", "database", "transaction", "plan_writes"}
    )
