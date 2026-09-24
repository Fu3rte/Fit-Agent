# resolve_progression 的加重口径：锚定最近两次完整完成的实际负荷，计划目标只是出发点。
# apply_return_period 的抑制口径：停训达到阈值时 increase 降为 keep。

from datetime import date

from app.domain.actions.schema import Exercise
from app.domain.plans.rules import (
    RETURN_PERIOD_GAP_DAYS,
    ProgressionDecision,
    apply_return_period,
    resolve_progression,
    validate_plan_adjustment,
)
from app.domain.plans.schema import PlanDraft
from app.domain.stats.schema import ValidWorkSet

SQUAT = "barbell-back-squat"
BENCH = "barbell-bench-press"


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


def _exercise(exercise_id: str) -> Exercise:
    return Exercise(
        id=exercise_id,
        standard_name_zh=exercise_id,
        aliases=(),
        equipment_variant="barbell",
        record_type="reps_weight",
        load_convention=None,
        min_load_increment_kg=2.5,
        recommendable=True,
        modes=(),
        source_ref="catalog",
        attribution="catalog",
    )


CATALOG = {SQUAT: _exercise(SQUAT), BENCH: _exercise(BENCH)}


def _weighted(exercise_id: str, *, sets: int, weight_kg: float) -> dict[str, object]:
    return {
        "exercise_id": exercise_id,
        "sets": sets,
        "prescription": {
            "type": "weighted_reps",
            "reps_min": 5,
            "reps_max": 8,
            "load": {
                "status": "known",
                "weight_kg": weight_kg,
                "source_workout_session_id": 1,
                "source_set_no": 1,
            },
        },
    }


def _calibrating(exercise_id: str, *, sets: int) -> dict[str, object]:
    return {
        "exercise_id": exercise_id,
        "sets": sets,
        "prescription": {
            "type": "weighted_reps",
            "reps_min": 5,
            "reps_max": 8,
            "load": {"status": "needs_calibration"},
        },
    }


def _draft(
    days: tuple[tuple[dict[str, object], ...], ...],
) -> PlanDraft:
    return PlanDraft.model_validate(
        {
            "goal": "增肌",
            "starts_on": date(2026, 6, 1),
            "explanation": "调整",
            "weekly_frequency": len(days),
            "training_days": [
                {
                    "scheduled_on": date(2026, 6, 1 + index),
                    "exercises": list(exercises),
                }
                for index, exercises in enumerate(days)
            ],
        }
    )


def _active() -> PlanDraft:
    """active 基线：一天两个负重动作，各三组 60kg。"""
    return _draft(
        ((_weighted(SQUAT, sets=3, weight_kg=60.0), _weighted(BENCH, sets=3, weight_kg=60.0)),)
    )


def _active_single() -> PlanDraft:
    """单动作 active：工作日组不按动作过滤（``resolve_progression`` 只按训练身份分组），

    因此两个负重动作同处一天时后者的判定会被前者的工作组污染。需要断言某个动作自己的渐进决策时
    用这一份基线，避免把上述既有行为混进回归口径的断言。
    """
    return _draft(((_weighted(SQUAT, sets=3, weight_kg=60.0),),))


def _codes(
    draft: PlanDraft,
    *,
    active: PlanDraft | None = None,
    gap_days: int | None = RETURN_PERIOD_GAP_DAYS,
    linked: tuple[int, ...] = (),
    work_sets: tuple[ValidWorkSet, ...] = (),
) -> list[str]:
    failures = validate_plan_adjustment(
        draft,
        active_draft=_active() if active is None else active,
        linked_workout_session_ids=linked,
        exercises=CATALOG,
        profile_weekly_frequency=draft.weekly_frequency,
        work_sets=work_sets,
        gap_days=gap_days,
    )
    return [failure.code for failure in failures]


def test_return_period_threshold_is_twenty_one_days() -> None:
    assert RETURN_PERIOD_GAP_DAYS == 21


def test_return_period_suppresses_increase_only_at_the_threshold() -> None:
    increase = ProgressionDecision("increase", 62.5)

    assert apply_return_period(increase, gap_days=None, target_load_kg=60.0) == increase
    assert (
        apply_return_period(
            increase, gap_days=RETURN_PERIOD_GAP_DAYS - 1, target_load_kg=60.0
        )
        == increase
    )
    assert apply_return_period(
        increase, gap_days=RETURN_PERIOD_GAP_DAYS, target_load_kg=60.0
    ) == ProgressionDecision("keep", 60.0)


def test_return_period_leaves_keep_regress_and_needs_calibration_alone() -> None:
    at_threshold = RETURN_PERIOD_GAP_DAYS

    assert apply_return_period(
        ProgressionDecision("keep", 60.0), gap_days=at_threshold, target_load_kg=60.0
    ) == ProgressionDecision("keep", 60.0)
    assert apply_return_period(
        ProgressionDecision("regress", 55.0), gap_days=at_threshold, target_load_kg=60.0
    ) == ProgressionDecision("regress", 55.0)
    assert apply_return_period(
        ProgressionDecision("needs_calibration", None),
        gap_days=at_threshold,
        target_load_kg=60.0,
    ) == ProgressionDecision("needs_calibration", None)


def test_return_period_reduces_the_first_exercise_and_keeps_the_rest() -> None:
    draft = _draft(
        ((_weighted(SQUAT, sets=2, weight_kg=60.0), _weighted(BENCH, sets=3, weight_kg=60.0)),)
    )

    assert _codes(draft) == []


def test_return_period_reduces_the_first_exercise_of_every_training_day() -> None:
    active = _draft(
        (
            (
                _weighted(SQUAT, sets=3, weight_kg=60.0),
                _weighted(BENCH, sets=3, weight_kg=60.0),
            ),
            (
                _weighted(BENCH, sets=4, weight_kg=60.0),
                _weighted(SQUAT, sets=3, weight_kg=60.0),
            ),
        )
    )
    draft = _draft(
        (
            (
                _weighted(SQUAT, sets=2, weight_kg=60.0),
                _weighted(BENCH, sets=3, weight_kg=60.0),
            ),
            (
                _weighted(BENCH, sets=3, weight_kg=60.0),
                _weighted(SQUAT, sets=3, weight_kg=60.0),
            ),
        )
    )

    assert _codes(draft, active=active) == []


def test_return_period_floors_the_first_exercise_at_one_set() -> None:
    active = _draft(
        ((_weighted(SQUAT, sets=1, weight_kg=60.0), _weighted(BENCH, sets=3, weight_kg=60.0)),)
    )

    assert _codes(active, active=active) == []


def test_return_period_does_not_apply_below_the_threshold() -> None:
    untouched = _draft(
        ((_weighted(SQUAT, sets=3, weight_kg=60.0), _weighted(BENCH, sets=3, weight_kg=60.0)),)
    )

    assert _codes(untouched, gap_days=RETURN_PERIOD_GAP_DAYS - 1) == []
    assert _codes(untouched) == ["sets_mismatch"]


def test_return_period_suppression_applies_only_at_the_threshold() -> None:
    work_sets = tuple(
        _workout(1, date(2026, 5, 20), 60.0, 8) + _workout(2, date(2026, 5, 27), 60.0, 8)
    )
    suppressed = _draft(((_weighted(SQUAT, sets=2, weight_kg=60.0),),))
    active = _active_single()

    assert _codes(suppressed, active=active, linked=(1, 2), work_sets=work_sets) == []
    assert _codes(
        suppressed,
        active=active,
        gap_days=RETURN_PERIOD_GAP_DAYS - 1,
        linked=(1, 2),
        work_sets=work_sets,
    ) == ["load_source_mismatch"]


def test_return_period_rejects_keeping_the_original_first_sets() -> None:
    draft = _draft(
        ((_weighted(SQUAT, sets=3, weight_kg=60.0), _weighted(BENCH, sets=3, weight_kg=60.0)),)
    )

    assert _codes(draft) == ["sets_mismatch"]


def test_return_period_rejects_dropping_more_than_one_set() -> None:
    draft = _draft(
        ((_weighted(SQUAT, sets=1, weight_kg=60.0), _weighted(BENCH, sets=3, weight_kg=60.0)),)
    )

    assert _codes(draft) == ["sets_mismatch"]


def test_return_period_rejects_changing_a_non_first_exercise_sets() -> None:
    draft = _draft(
        ((_weighted(SQUAT, sets=2, weight_kg=60.0), _weighted(BENCH, sets=2, weight_kg=60.0)),)
    )

    assert _codes(draft) == ["sets_mismatch"]


def test_return_period_rejects_reordering_the_day_to_dodge_the_reduction() -> None:
    draft = _draft(
        ((_weighted(BENCH, sets=2, weight_kg=60.0), _weighted(SQUAT, sets=3, weight_kg=60.0)),)
    )

    assert _codes(draft) == ["structure_mismatch"]


def test_return_period_rejects_a_changed_training_day_count() -> None:
    active = _active()
    draft = _draft(
        (
            (_weighted(SQUAT, sets=2, weight_kg=60.0),),
            (_weighted(SQUAT, sets=2, weight_kg=60.0),),
        )
    )

    assert _codes(draft, active=active) == ["structure_mismatch"]


def test_return_period_does_not_stack_a_sets_change_on_a_regressed_load() -> None:
    work_sets = tuple(
        _workout(1, date(2026, 5, 20), 55.0, 5)
        + _workout(2, date(2026, 5, 27), 60.0, 4)
        + _workout(3, date(2026, 6, 3), 60.0, 4)
    )
    regressed = _draft(((_weighted(SQUAT, sets=3, weight_kg=55.0),),))
    stacked = _draft(((_weighted(SQUAT, sets=2, weight_kg=55.0),),))
    active = _active_single()

    assert _codes(regressed, active=active, linked=(1, 2, 3), work_sets=work_sets) == []
    assert _codes(stacked, active=active, linked=(1, 2, 3), work_sets=work_sets) == [
        "sets_mismatch"
    ]


def test_return_period_keeps_sets_when_the_decision_is_needs_calibration() -> None:
    work_sets = tuple(
        _workout(2, date(2026, 5, 27), 60.0, 4) + _workout(3, date(2026, 6, 3), 60.0, 4)
    )
    draft = _draft(((_calibrating(SQUAT, sets=3),),))

    assert _codes(draft, active=_active_single(), linked=(2, 3), work_sets=work_sets) == []
