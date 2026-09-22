from dataclasses import asdict
from datetime import date
from typing import cast

from langgraph.runtime import Runtime

from app.application.agent.contracts import (
    PLAN_EVALUATION_SKILL_NAME,
    PLANNING_SKILL_NAME,
    GeneratePlanRun,
    PlanLlmNodeDeps,
    ToolEvidence,
    WorkflowState,
)
from app.application.agent.plan_nodes import (
    _evaluation_result,
    _evaluator_payload,
    load_skill,
)
from app.domain.plans.schema import (
    SNAPSHOT_MISMATCH_CODE,
    DeterministicResult,
    PlanDraft,
    RubricResult,
)
from app.infrastructure.skills.loader import SkillLoader
from config import skills_dir

DAY = date(2026, 6, 1)
REFERENCE_PATHS = ("references/evaluation-rubric.md", "references/few-shots.md")


def _draft() -> PlanDraft:
    return PlanDraft.model_validate({
        "goal": "增肌",
        "starts_on": DAY,
        "explanation": "按目标与目录事实安排",
        "weekly_frequency": 1,
        "training_days": [
            {
                "scheduled_on": DAY,
                "exercises": [
                    {
                        "exercise_id": "pull-up",
                        "sets": 3,
                        "prescription": {
                            "type": "bodyweight_reps",
                            "reps_min": 5,
                            "reps_max": 8,
                            "progression_note": None,
                        },
                    }
                ],
            }
        ],
    })


def _rubric(
    *, goal: bool = True, schedule: bool = True, explanation: bool = True
) -> RubricResult:
    return RubricResult.model_validate({
        "goal_alignment": {"passed": goal, "reason": "目标"},
        "schedule_reasonableness": {"passed": schedule, "reason": "日程"},
        "explanation_quality": {"passed": explanation, "reason": "解释"},
    })


async def test_plan_evaluation_skill_loads_and_enters_the_evaluator_payload() -> None:
    loader = SkillLoader(skills_dir())
    loaded = await load_skill(
        cast("WorkflowState", {"intent": "generate_plan"}),
        cast("Runtime[GeneratePlanRun]", None),
        skills=loader,
    )
    skill = loaded.get("evaluation_skill")
    planner_skill = loaded.get("loaded_skill")
    assert skill is not None and planner_skill is not None
    state = cast(
        "WorkflowState",
        {
            "request": "生成增肌计划",
            "draft_plan": _draft(),
            "evaluation_skill": skill,
        },
    )
    payload = _evaluator_payload(state, GeneratePlanRun(DAY), facts=())

    assert planner_skill.metadata.name == PLANNING_SKILL_NAME
    assert skill.metadata.name == PLAN_EVALUATION_SKILL_NAME
    assert tuple(reference.path for reference in skill.references) == REFERENCE_PATHS
    assert payload["skill"] == asdict(skill)
    assert set(payload) == {"request", "plan", "business_day", "skill", "facts"}


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
