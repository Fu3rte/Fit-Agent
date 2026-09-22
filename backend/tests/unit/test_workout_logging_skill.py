from app.api.schemas.agent_dto import ConfirmWorkoutBody, WorkoutSetBody
from app.application.agent.harness.tools.general import (
    GENERAL_INTENT_TOOLS,
    GENERAL_SKILL_NAMES,
    ExtractedWorkout,
    _workout_payload,
    _workout_set_input,
)
from app.domain.records.rules import validate_session_sets
from app.infrastructure.skills.loader import SkillLoader
from config import skills_dir

SKILL_NAME = "workout-logging"
REFERENCE_PATHS = ("references/logging-contract.md", "references/few-shots.md")


def test_workout_logging_skill_loads_with_its_references() -> None:
    skill = SkillLoader(skills_dir()).load(SKILL_NAME)

    assert GENERAL_SKILL_NAMES.count(SKILL_NAME) == 1
    assert skill.metadata.name == SKILL_NAME
    assert tuple(reference.path for reference in skill.references) == REFERENCE_PATHS
    assert tuple(
        tool.name for tool in GENERAL_INTENT_TOOLS["natural_language_record"]
    ) == ("prepare_workout_record", "search_exercises")


def test_few_shot_fields_map_to_the_current_record_contract() -> None:
    workout = ExtractedWorkout.model_validate({
        "performed_on": "2026-06-01",
        "sets": [
            {
                "exercise_id": "weighted-pull-up",
                "set_no": 1,
                "set_type": "work",
                "reps": 5,
                "load_convention": "external_added_weight",
                "weight_kg": 10.0,
            },
            {
                "exercise_id": "pull-up",
                "set_no": 1,
                "set_type": "work",
                "reps": 8,
            },
            {
                "exercise_id": "plank",
                "set_no": 1,
                "set_type": "work",
                "duration_seconds": 45,
            },
        ],
    })
    facts = validate_session_sets(tuple(_workout_set_input(row) for row in workout.sets))
    payload = _workout_payload(workout.performed_on, facts)
    few_shots = SkillLoader(skills_dir()).load(SKILL_NAME).references[1].text

    assert set(payload) | {"chat_id", "conversation_id"} == set(
        ConfirmWorkoutBody.model_fields
    )
    assert set(payload["sets"][0]) == set(WorkoutSetBody.model_fields)
    assert payload["plan_session_id"] is None
    assert payload["auto_link"] is True
    assert all(exercise_id in few_shots for exercise_id in ("weighted-pull-up", "pull-up", "plank"))
