from datetime import date

from app.domain.actions.schema import Exercise
from app.domain.plans.rules import validate_plan_adjustment, validate_plan_draft
from app.domain.plans.schema import PlanDraft


def _exercise(exercise_id: str, equipment_variant: str, record_type: str) -> Exercise:
    return Exercise(
        id=exercise_id,
        standard_name_zh=exercise_id,
        aliases=(),
        equipment_variant=equipment_variant,
        record_type=record_type,
        load_convention=None,
        min_load_increment_kg=None,
        recommendable=True,
        modes=(),
        source_ref="catalog",
        attribution="catalog",
    )


def _draft(exercise_id: str, prescription_type: str) -> PlanDraft:
    prescription = {"type": prescription_type}
    if prescription_type == "bodyweight_reps":
        prescription |= {"reps_min": 5, "reps_max": 10}
    else:
        prescription |= {
            "reps_min": 5,
            "reps_max": 10,
            "load": {"status": "needs_calibration"},
        }
    return PlanDraft.model_validate(
        {
            "goal": "fitness",
            "starts_on": date(2026, 6, 1),
            "explanation": "plan",
            "weekly_frequency": 1,
            "training_days": [
                {
                    "scheduled_on": date(2026, 6, 1),
                    "exercises": [
                        {"exercise_id": exercise_id, "sets": 3, "prescription": prescription}
                    ],
                }
            ],
        }
    )


def test_bodyweight_training_mode_uses_canonical_equipment_variant() -> None:
    bodyweight = _exercise("pull-up", "bodyweight", "reps_bodyweight")
    equipment = _exercise("barbell-back-squat", "barbell", "reps_weight")
    catalog = {bodyweight.id: bodyweight, equipment.id: equipment}

    valid = validate_plan_draft(
        _draft("pull-up", "bodyweight_reps"),
        exercises=catalog,
        profile_weekly_frequency=1,
        training_mode="bodyweight",
    )
    restricted = validate_plan_draft(
        _draft("barbell-back-squat", "weighted_reps"),
        exercises=catalog,
        profile_weekly_frequency=1,
        training_mode="bodyweight",
    )
    unrestricted = validate_plan_draft(
        _draft("barbell-back-squat", "weighted_reps"),
        exercises=catalog,
        profile_weekly_frequency=1,
    )

    assert not valid
    assert [failure.code for failure in restricted] == ["training_mode_mismatch"]
    assert not unrestricted

    adjusted = validate_plan_adjustment(
        _draft("barbell-back-squat", "weighted_reps"),
        active_draft=_draft("barbell-back-squat", "weighted_reps"),
        linked_workout_session_ids=(),
        exercises=catalog,
        profile_weekly_frequency=1,
        training_mode="bodyweight",
    )
    assert [failure.code for failure in adjusted] == ["training_mode_mismatch"]
