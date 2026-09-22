# resolve_progression 的加重口径：锚定最近两次完整完成的实际负荷，计划目标只是出发点。

from datetime import date

from app.domain.plans.rules import ProgressionDecision, resolve_progression
from app.domain.stats.schema import ValidWorkSet

SQUAT = "barbell-back-squat"


def _workout(session_id: int, performed_on: date, weight_kg: float, reps: int) -> list[ValidWorkSet]:
    return [
        ValidWorkSet(
            exercise_id=SQUAT,
            exercise_name="杠铃背蹲",
            record_type="reps_weight",
            load_convention="barbell_includes_bar_total",
            weight_kg=weight_kg,
            reps=reps,
            duration_seconds=None,
            workout_session_id=session_id,
            set_no=set_no,
            performed_on=performed_on,
        )
        for set_no in (1, 2, 3)
    ]


def _decide(
    work_sets: list[ValidWorkSet], *, linked: tuple[int, ...]
) -> ProgressionDecision:
    return resolve_progression(
        work_sets,
        linked_workout_session_ids=linked,
        target_sets=3,
        reps_min=5,
        reps_max=8,
        target_load_kg=60.0,
        increment_kg=2.5,
    )


def test_progression_increases_from_the_planned_load() -> None:
    work_sets = _workout(1, date(2026, 6, 1), 60.0, 8) + _workout(
        2, date(2026, 6, 3), 60.0, 8
    )

    assert _decide(work_sets, linked=(1, 2)) == ProgressionDecision("increase", 62.5)


def test_progression_follows_the_load_the_user_actually_lifted() -> None:
    work_sets = _workout(1, date(2026, 6, 1), 70.0, 8) + _workout(
        2, date(2026, 6, 3), 70.0, 8
    )

    assert _decide(work_sets, linked=(1, 2)) == ProgressionDecision("increase", 72.5)


def test_progression_keeps_the_planned_load_without_two_linked_sessions() -> None:
    work_sets = _workout(1, date(2026, 6, 1), 70.0, 8) + _workout(
        2, date(2026, 6, 3), 70.0, 8
    )

    assert _decide(work_sets, linked=(1,)) == ProgressionDecision("keep", 60.0)
    assert _decide(work_sets, linked=()) == ProgressionDecision("keep", 60.0)


def test_progression_keeps_the_planned_load_when_only_one_session_hits_the_top() -> None:
    work_sets = _workout(1, date(2026, 6, 1), 60.0, 8) + _workout(
        2, date(2026, 6, 3), 60.0, 7
    )

    assert _decide(work_sets, linked=(1, 2)) == ProgressionDecision("keep", 60.0)


def test_progression_never_increases_without_a_shared_load() -> None:
    work_sets = _workout(1, date(2026, 6, 1), 65.0, 8) + _workout(
        2, date(2026, 6, 3), 70.0, 8
    )

    assert _decide(work_sets, linked=(1, 2)) == ProgressionDecision("keep", 60.0)
