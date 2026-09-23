from app.api.schemas.agent_dto import ConfirmWorkoutBody, WorkoutSetBody
from app.application.agent.harness.registry import (
    GENERAL_INTENT_SKILLS,
    GENERAL_INTENT_TOOLS,
)
from app.application.agent.harness.tools.prepare_workout_record import (
    ExtractedWorkout,
    _workout_payload,
    _workout_set_input,
)
from app.domain.records.rules import validate_session_sets
from app.infrastructure.skills.loader import SkillLoader
from config import skills_dir

SKILL_NAME = "workout-logging"
REFERENCE_PATHS = ("references/logging-contract.md", "references/few-shots.md")


def test_workout_logging_skill_reads_its_declared_references() -> None:
    loader = SkillLoader(skills_dir())
    metadata = next(item for item in loader.list_metadata() if item.name == SKILL_NAME)
    references = tuple(
        loader.read_reference(SKILL_NAME, path) for path in REFERENCE_PATHS
    )

    names = [name for names in GENERAL_INTENT_SKILLS.values() for name in names]
    assert SKILL_NAME in names
    assert metadata.description
    assert tuple(reference.path for reference in references) == REFERENCE_PATHS
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
    few_shots = SkillLoader(skills_dir()).read_reference(
        SKILL_NAME, REFERENCE_PATHS[1]
    ).text

    assert set(payload) | {"chat_id", "conversation_id"} == set(
        ConfirmWorkoutBody.model_fields
    )
    assert set(payload["sets"][0]) == set(WorkoutSetBody.model_fields)
    assert payload["plan_session_id"] is None
    assert payload["auto_link"] is True
    assert all(exercise_id in few_shots for exercise_id in ("weighted-pull-up", "pull-up", "plank"))
