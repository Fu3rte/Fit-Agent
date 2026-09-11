"""Stage 3 S3-03：计划领域 schema 与生成（D9 payload、PPL 模板、过滤与安全前置、日程投影）。

验收对照（stage3.md §5 S3-03）：

- 计划只引用可推荐且条件匹配的目录动作；同一 workout 内 ``item_key`` 唯一；slots 引用存在
  的 ``workout_key``；红旗／缺档案不给处方；无可信记录无猜重；投影只含 workout 日、不含
  rest 与 ``review_on`` 当日。
- 领域单测覆盖生成、过滤、阻断、cycle 投影与日期边界（含练三休一非 7 日循环）。

边界：候选一律来自临时库迁移 007 后的 ``ExerciseRepo.list_recommendable``（恰 24 项）；
测试虚构动作只用 ``test-only-`` 前缀，不冒充产品种子；不写库、不建草稿、不推版本
（草稿与确认归 S3-04/S3-06）。所有用例只操作 ``tmp_path`` 下的临时文件库，不触碰真实用户库。
"""

import json
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from domain.actions.repo import ExerciseRepo
from domain.plan.rules import (
    InvalidPlanPayload,
    prescription_record_type_for,
    project_sessions,
    validate_payload,
)
from domain.plan.schema import (
    CalendarCycle,
    DisplaySnapshot,
    IntRange,
    NeedsCalibration,
    PlanExerciseItem,
    PlanPayload,
    PlanWorkout,
    Progression,
    RepsPrescription,
    RestCycleSlot,
    TimedPrescription,
    VerifiedLoad,
    WorkoutCycleSlot,
    payload_to_json,
)
from domain.plan.service import (
    PPL_TEMPLATE_KEY,
    PlanGenerationBlocked,
    PlanGenerationReady,
    generate_ppl_plan,
    needs_calibration_for,
)
from domain.profile.schema import ActionRestriction, Fact, Profile, ProfilePatch
from tests.support import open_database

STARTS_ON = date(2026, 9, 14)
REVIEW_ON = date(2026, 10, 12)
ANCHOR_DATE = date(2026, 9, 14)
FULL_EQUIPMENT = ("杠铃", "哑铃", "绳索", "引体架")


def _profile(**overrides: object) -> Profile:
    """完整档案（八项均已明确回答）；用例只在需要时覆盖指定事实。"""
    facts: dict[str, object] = {
        "training_goal": Fact.known("增肌"),
        "training_experience": Fact.known("1 年"),
        "weekly_frequency": Fact.known(3),
        "session_duration_minutes": Fact.known(60),
        "available_equipment": Fact.known(FULL_EQUIPMENT),
        "action_restrictions": Fact.denied(),
        "body_conditions": Fact.denied(),
        "body_weight_kg": Fact.known(70.0),
    }
    facts.update(overrides)
    return Profile(**facts)  # type: ignore[arg-type]


async def _recommendable(tmp_path: Path):
    """临时库（迁移 001–007）中的推荐候选：S3-02 系统迁移后恰为已核对 24 项。"""
    async with open_database(tmp_path / "app.db") as db:
        return await ExerciseRepo(db).list_recommendable()


def _generate(candidates, profile: Profile | None = None, **overrides):
    kwargs: dict[str, object] = {
        "starts_on": STARTS_ON,
        "review_on": REVIEW_ON,
        "anchor_date": ANCHOR_DATE,
    }
    kwargs.update(overrides)
    return generate_ppl_plan(
        profile if profile is not None else _profile(),
        candidates,
        **kwargs,  # type: ignore[arg-type]
    )


def _ready(result) -> PlanGenerationReady:
    assert isinstance(result, PlanGenerationReady), result
    return result


def _bodyweight_item(
    item_key: str = "w1-01",
    exercise_id: str = "test-only-bw",
) -> PlanExerciseItem:
    """最小合法自重动作：无 load、次数型处方、次数渐进。"""
    return PlanExerciseItem(
        item_key=item_key,
        exercise_id=exercise_id,
        display_snapshot=DisplaySnapshot(
            name="测试自重动作", equipment_variant="bodyweight", load_convention=None
        ),
        record_type="bodyweight_reps",
        prescription=RepsPrescription(
            work_sets=3, reps_range=IntRange(8, 12), target_rir=None
        ),
        load=None,
        progression=Progression(method="repetition_progression", rule="先加次数"),
    )


def _bare_calibration() -> NeedsCalibration:
    """最小校准载荷（校验用例只关心 load 类型互斥，不关心文案）。"""
    # 位置参数：三个字段依次为 steps / pass_criteria / stop_criteria。
    return NeedsCalibration(("热身",), "稳定完成次数下限", "疼痛或不稳即停")


def _payload_with_cycle(slots, *, anchor_date: date = STARTS_ON) -> PlanPayload:
    workout = PlanWorkout(
        workout_key="w1",
        name="测试日",
        estimated_minutes=30,
        exercises=(_bodyweight_item(),),
    )
    return PlanPayload(
        plan_workouts=(workout,),
        calendar_cycle=CalendarCycle(anchor_date=anchor_date, slots=tuple(slots)),
        template_key="test",
    )


# ---------- 生成：只引用可推荐目录动作 + 处方/映射/校准 ----------


async def test_ppl_generation_references_only_recommendable_catalog_actions(
    tmp_path: Path,
) -> None:
    candidates = await _recommendable(tmp_path)
    assert len(candidates) == 24
    catalog = {exercise.id: exercise for exercise in candidates}
    payload = _ready(_generate(candidates)).payload

    assert payload.template_key == PPL_TEMPLATE_KEY
    assert [workout.workout_key for workout in payload.plan_workouts] == [
        "push",
        "pull",
        "legs",
    ]
    referenced = set()
    for workout in payload.plan_workouts:
        assert workout.estimated_minutes > 0
        item_keys = [item.item_key for item in workout.exercises]
        assert len(set(item_keys)) == len(item_keys)
        for item in workout.exercises:
            referenced.add(item.exercise_id)
            exercise = catalog[item.exercise_id]
            assert exercise.active and exercise.recommendable
            assert item.record_type == prescription_record_type_for(
                exercise.record_type
            )
            assert item.display_snapshot.name == exercise.standard_name_zh
            assert item.display_snapshot.equipment_variant == exercise.equipment_variant
            assert item.progression.rule.strip()
            if item.record_type == "external_load_reps":
                assert isinstance(item.load, NeedsCalibration)
            else:
                assert item.load is None
    # 模板覆盖推/拉/腿三天的代表动作；全部来自已核对 24 项。
    assert {"barbell-bench-press", "pull-up", "barbell-back-squat"} <= referenced


async def test_generation_never_guesses_load_without_trusted_history(
    tmp_path: Path,
) -> None:
    candidates = await _recommendable(tmp_path)
    payload = _ready(_generate(candidates)).payload
    for workout in payload.plan_workouts:
        for item in workout.exercises:
            if item.record_type != "external_load_reps":
                continue
            load = item.load
            assert isinstance(load, NeedsCalibration)
            assert isinstance(item.prescription, RepsPrescription)
            # D3：通过标准是处方次数下限，RIR 不作硬性指标；不含任何重量。
            reps_floor = item.prescription.reps_range.min
            assert str(reps_floor) in load.pass_criteria
            assert "RIR" not in load.pass_criteria
            assert "RIR" not in load.stop_criteria
            assert load.pass_criteria.strip() and load.stop_criteria.strip()
            assert not hasattr(load, "value")


def test_timed_calibration_uses_shortest_duration_and_no_medical_threshold() -> None:
    load = needs_calibration_for(
        "timed", TimedPrescription(work_sets=3, duration_seconds_range=IntRange(30, 45))
    )
    assert "30" in load.pass_criteria
    assert "秒" in load.pass_criteria
    # 停止条件只写疼痛/失稳/无法维持，不引入医学阈值。
    assert "疼痛" in load.stop_criteria
    assert "RIR" not in load.stop_criteria


# ---------- 过滤：器械 + 具体动作/模式限制 ----------


async def test_generation_filters_actions_by_profile_equipment(tmp_path: Path) -> None:
    candidates = await _recommendable(tmp_path)
    profile = _profile(available_equipment=Fact.known(("哑铃",)))
    payload = _ready(_generate(candidates, profile)).payload

    ids = {
        item.exercise_id
        for workout in payload.plan_workouts
        for item in workout.exercises
    }
    assert "barbell-bench-press" not in ids
    assert "pull-up" not in ids  # 自重动作需要引体架/单杠，不在档案器械内
    dumped = payload_to_json(payload)
    assert "barbell" not in dumped.split('"equipment_variant": "')[1][:20]
    for workout in payload.plan_workouts:
        for item in workout.exercises:
            assert item.display_snapshot.equipment_variant == "dumbbell"


async def test_generation_filters_specific_action_and_movement_pattern_restrictions(
    tmp_path: Path,
) -> None:
    candidates = await _recommendable(tmp_path)
    restrictions = (
        ActionRestriction(scope="specific_action", target="barbell-back-squat"),
        ActionRestriction(scope="movement_pattern", target="水平推"),
    )
    profile = _profile(action_restrictions=Fact.known(restrictions))
    payload = _ready(_generate(candidates, profile)).payload
    ids = {
        item.exercise_id
        for workout in payload.plan_workouts
        for item in workout.exercises
    }
    assert "barbell-back-squat" not in ids
    assert "barbell-bench-press" not in ids  # 水平推限制覆盖
    assert "seated-dumbbell-shoulder-press" in ids  # 垂直推不受影响


async def test_generation_blocks_when_a_workout_has_no_available_action(
    tmp_path: Path,
) -> None:
    candidates = await _recommendable(tmp_path)
    profile = _profile(available_equipment=Fact.known(("绳索",)))
    result = _generate(candidates, profile)
    assert isinstance(result, PlanGenerationBlocked)
    assert result.code == "unschedulable"
    assert "拉日" in result.reason


async def test_generation_ignores_stopped_or_unrecommendable_candidates(
    tmp_path: Path,
) -> None:
    candidates = await _recommendable(tmp_path)
    patched = tuple(
        replace(exercise, active=False)
        if exercise.id == "barbell-bench-press"
        else replace(exercise, recommendable=False)
        if exercise.id == "pull-up"
        else exercise
        for exercise in candidates
    )
    payload = _ready(_generate(patched)).payload
    ids = {
        item.exercise_id
        for workout in payload.plan_workouts
        for item in workout.exercises
    }
    assert "barbell-bench-press" not in ids
    assert "pull-up" not in ids


# ---------- 阻断：缺档案 / 红旗 / 频率与时长 ----------


async def test_generation_blocks_without_profile(tmp_path: Path) -> None:
    candidates = await _recommendable(tmp_path)
    result = generate_ppl_plan(
        None,
        candidates,
        starts_on=STARTS_ON,
        review_on=REVIEW_ON,
        anchor_date=ANCHOR_DATE,
    )
    assert isinstance(result, PlanGenerationBlocked)
    assert result.code == "no_profile"
    assert "不生成" in result.reason


async def test_generation_blocks_on_incomplete_profile(tmp_path: Path) -> None:
    candidates = await _recommendable(tmp_path)
    profile = _profile(available_equipment=Fact.unknown())
    result = _generate(candidates, profile)
    assert isinstance(result, PlanGenerationBlocked)
    assert result.code == "incomplete_profile"
    assert result.missing_fields == ("available_equipment",)


async def test_generation_blocks_on_confirmed_red_flag(tmp_path: Path) -> None:
    candidates = await _recommendable(tmp_path)
    profile = _profile(body_conditions=Fact.known(("训练中出现胸部异常不适",)))
    result = _generate(candidates, profile)
    assert isinstance(result, PlanGenerationBlocked)
    assert result.code == "red_flag"
    assert result.red_flags == ("胸部异常不适",)
    assert "线下专业评估" in result.reason


async def test_generation_blocks_on_red_flag_from_patch_and_denied_cannot_clear_it(
    tmp_path: Path,
) -> None:
    candidates = await _recommendable(tmp_path)
    patch_with_flag = ProfilePatch(
        facts={"body_conditions": Fact.known(("运动中出现锐痛",))}
    )
    result = _generate(candidates, _profile(), patch=patch_with_flag)
    assert isinstance(result, PlanGenerationBlocked)
    assert result.code == "red_flag"

    formal_flag = _profile(body_conditions=Fact.known(("运动中出现锐痛",)))
    patch_denied = ProfilePatch(facts={"body_conditions": Fact.denied()})
    result = _generate(candidates, formal_flag, patch=patch_denied)
    assert isinstance(result, PlanGenerationBlocked)
    assert result.code == "red_flag"


async def test_generation_blocks_when_frequency_or_duration_does_not_fit(
    tmp_path: Path,
) -> None:
    candidates = await _recommendable(tmp_path)
    low_frequency = _generate(candidates, _profile(weekly_frequency=Fact.known(2)))
    assert isinstance(low_frequency, PlanGenerationBlocked)
    assert low_frequency.code == "unschedulable"
    assert "频率" in low_frequency.reason

    short_session = _generate(
        candidates, _profile(session_duration_minutes=Fact.known(30))
    )
    assert isinstance(short_session, PlanGenerationBlocked)
    assert short_session.code == "unschedulable"
    assert "超过档案单次可用时长" in short_session.reason


async def test_generation_normalizes_non_seven_day_cycle_to_weekly_frequency(
    tmp_path: Path,
) -> None:
    """练三休一：循环 4 天含 3 个训练日 ⇒ 折合每周 21/4 次，不能拿 3 个训练日充作每周 3 次。"""
    candidates = await _recommendable(tmp_path)
    slots = (
        WorkoutCycleSlot("push"),
        WorkoutCycleSlot("pull"),
        WorkoutCycleSlot("legs"),
        RestCycleSlot(),
    )
    blocked = _generate(
        candidates, _profile(weekly_frequency=Fact.known(3)), cycle_slots=slots
    )
    assert isinstance(blocked, PlanGenerationBlocked)
    assert blocked.code == "unschedulable"
    assert "超过档案每周频率" in blocked.reason

    # 整数交叉相乘的边界：21/4 = 5.25 次/周 ⇒ 每周 5 次仍超频，每周 6 次才容得下。
    assert isinstance(
        _generate(
            candidates, _profile(weekly_frequency=Fact.known(5)), cycle_slots=slots
        ),
        PlanGenerationBlocked,
    )
    payload = _ready(
        _generate(
            candidates, _profile(weekly_frequency=Fact.known(6)), cycle_slots=slots
        )
    ).payload
    assert len(payload.calendar_cycle.slots) == 4
    sessions = project_sessions(
        payload, starts_on=STARTS_ON, review_on=STARTS_ON + timedelta(days=8)
    )
    # 循环长度即实际循环长度：练三休一按日历日推进，不按周对齐。
    assert [session.scheduled_on for session in sessions] == [
        date(2026, 9, 14),
        date(2026, 9, 15),
        date(2026, 9, 16),
        date(2026, 9, 18),
        date(2026, 9, 19),
        date(2026, 9, 20),
    ]

    # 默认 7 日循环的等号边界：3 个训练日 = 每周 3 次，恰好等于档案频率时不得误阻断。
    assert _ready(_generate(candidates, _profile(weekly_frequency=Fact.known(3))))


async def test_generation_uses_patched_equipment_without_mutating_profile(
    tmp_path: Path,
) -> None:
    candidates = await _recommendable(tmp_path)
    profile = _profile(available_equipment=Fact.known(("杠铃",)))
    patch = ProfilePatch(facts={"available_equipment": Fact.known(("哑铃",))})
    payload = _ready(_generate(candidates, profile, patch=patch)).payload
    for workout in payload.plan_workouts:
        for item in workout.exercises:
            assert item.display_snapshot.equipment_variant == "dumbbell"
    assert profile.available_equipment.value == ("杠铃",)


# ---------- 日程投影：公式、rest、review_on、日期边界 ----------


async def test_ppl_projection_covers_scope_without_rest_or_review_on(
    tmp_path: Path,
) -> None:
    candidates = await _recommendable(tmp_path)
    payload = _ready(_generate(candidates)).payload
    sessions = project_sessions(payload, starts_on=STARTS_ON, review_on=REVIEW_ON)

    assert len(sessions) == 12  # 4 个完整 7 日循环 × 3 个训练日
    assert sessions[0].scheduled_on == STARTS_ON
    assert sessions[-1].scheduled_on == date(2026, 10, 9)
    dates = [session.scheduled_on for session in sessions]
    assert len(set(dates)) == len(dates)
    assert all(scheduled_on < REVIEW_ON for scheduled_on in dates)
    assert REVIEW_ON not in dates
    assert all(
        scheduled_on.weekday() in (0, 2, 4) for scheduled_on in dates
    )  # 周一/三/五
    workout_keys = {workout.workout_key for workout in payload.plan_workouts}
    assert {session.plan_workout_key for session in sessions} <= workout_keys


def test_projection_supports_non_seven_day_train_three_rest_one_cycle() -> None:
    # 练三休一：循环长度 4，不是 7 日循环；按日历日推进、完成与否不影响。
    slots = (
        WorkoutCycleSlot("w1"),
        WorkoutCycleSlot("w1"),
        WorkoutCycleSlot("w1"),
        RestCycleSlot(),
    )
    payload = _payload_with_cycle(slots, anchor_date=date(2026, 9, 14))
    starts_on = date(2026, 9, 14)
    sessions = project_sessions(
        payload, starts_on=starts_on, review_on=starts_on + timedelta(days=10)
    )
    assert [session.scheduled_on for session in sessions] == [
        date(2026, 9, 14),
        date(2026, 9, 15),
        date(2026, 9, 16),
        date(2026, 9, 18),
        date(2026, 9, 19),
        date(2026, 9, 20),
        date(2026, 9, 22),
        date(2026, 9, 23),
    ]
    # 第 4 天与第 8 天是休息槽：既不是应训练日，也不占完成率分母。
    assert date(2026, 9, 17) not in {s.scheduled_on for s in sessions}
    assert date(2026, 9, 21) not in {s.scheduled_on for s in sessions}
    assert len(sessions) == 8


def test_projection_phase_uses_anchor_offset_and_excludes_review_on() -> None:
    slots = (WorkoutCycleSlot("w1"), RestCycleSlot())
    starts_on = date(2026, 9, 14)
    payload = _payload_with_cycle(slots, anchor_date=starts_on - timedelta(days=1))
    sessions = project_sessions(
        payload, starts_on=starts_on, review_on=starts_on + timedelta(days=3)
    )
    # anchor 前一天 → starts_on 落在 rest 槽，第二天才是训练日；review_on 当日不生成。
    assert [session.scheduled_on for session in sessions] == [date(2026, 9, 15)]


def test_projection_handles_month_and_leap_day_boundaries() -> None:
    payload = _payload_with_cycle(
        (WorkoutCycleSlot("w1"),), anchor_date=date(2028, 2, 28)
    )
    sessions = project_sessions(
        payload, starts_on=date(2028, 2, 28), review_on=date(2028, 3, 2)
    )
    assert [session.scheduled_on for session in sessions] == [
        date(2028, 2, 28),
        date(2028, 2, 29),  # 闰日按日历日推进
        date(2028, 3, 1),
    ]


@pytest.mark.parametrize(
    ("starts_on", "review_on"),
    [(STARTS_ON, STARTS_ON), (REVIEW_ON, STARTS_ON)],
)
def test_projection_rejects_empty_or_reversed_scope(
    starts_on: date, review_on: date
) -> None:
    payload = _payload_with_cycle((WorkoutCycleSlot("w1"),))
    with pytest.raises(InvalidPlanPayload, match="starts_on 早于 review_on"):
        project_sessions(payload, starts_on=starts_on, review_on=review_on)


# ---------- payload 结构校验 ----------


def _external_item(item_key: str = "w1-01", *, load) -> PlanExerciseItem:
    return PlanExerciseItem(
        item_key=item_key,
        exercise_id="test-only-barbell",
        display_snapshot=DisplaySnapshot(
            name="测试负重动作",
            equipment_variant="barbell",
            load_convention="barbell_includes_bar_total",
        ),
        record_type="external_load_reps",
        prescription=RepsPrescription(work_sets=3, reps_range=IntRange(6, 8)),
        load=load,
        progression=Progression(method="double_progression", rule="达到上限后加重"),
    )


def _payload_with_items(items, slots=None) -> PlanPayload:
    workout = PlanWorkout(
        workout_key="w1", name="测试日", estimated_minutes=30, exercises=tuple(items)
    )
    return PlanPayload(
        plan_workouts=(workout,),
        calendar_cycle=CalendarCycle(
            anchor_date=STARTS_ON, slots=tuple(slots or (WorkoutCycleSlot("w1"),))
        ),
        template_key="test",
    )


def test_validate_payload_rejects_duplicate_keys_and_unknown_slot_reference() -> None:
    duplicate_item_key = PlanPayload(
        plan_workouts=(
            PlanWorkout(
                workout_key="w1",
                name="测试日",
                estimated_minutes=30,
                exercises=(
                    _bodyweight_item("dup", "test-only-a"),
                    _bodyweight_item("dup", "test-only-b"),
                ),
            ),
        ),
        calendar_cycle=CalendarCycle(
            anchor_date=STARTS_ON, slots=(WorkoutCycleSlot("w1"),)
        ),
    )
    with pytest.raises(InvalidPlanPayload, match="item_key"):
        validate_payload(duplicate_item_key)

    duplicate_identity = _payload_with_items(
        (_bodyweight_item("a", "test-only-a"), _bodyweight_item("b", "test-only-a"))
    )
    with pytest.raises(InvalidPlanPayload, match="重复同一动作身份"):
        validate_payload(duplicate_identity)

    unknown_slot = _payload_with_cycle((WorkoutCycleSlot("missing"),))
    with pytest.raises(InvalidPlanPayload, match="不存在的 workout_key"):
        validate_payload(unknown_slot)


def test_validate_payload_enforces_load_exclusivity_and_progression_match() -> None:
    with pytest.raises(InvalidPlanPayload, match="不得留空"):
        validate_payload(_payload_with_items((_external_item(load=None),)))

    bodyweight_with_load = _bodyweight_item()
    with pytest.raises(InvalidPlanPayload, match="load 仅用于外加负重动作"):
        validate_payload(
            _payload_with_items(
                (replace(bodyweight_with_load, load=_bare_calibration()),)
            )
        )

    bad_notation = _external_item(
        load=VerifiedLoad(value=60.0, unit="kg", load_notation="invented_notation")
    )
    with pytest.raises(InvalidPlanPayload, match="负重口径"):
        validate_payload(_payload_with_items((bad_notation,)))

    mismatch = _bodyweight_item()
    with pytest.raises(InvalidPlanPayload, match="不匹配"):
        validate_payload(
            _payload_with_items(
                (
                    replace(
                        mismatch,
                        progression=Progression(method="double_progression", rule="x"),
                    ),
                )
            )
        )

    with pytest.raises(InvalidPlanPayload, match="min ≤ max"):
        validate_payload(
            _payload_with_items(
                (
                    replace(
                        _bodyweight_item(),
                        prescription=RepsPrescription(
                            work_sets=3, reps_range=IntRange(12, 8)
                        ),
                    ),
                )
            )
        )


def test_validate_payload_requires_matching_schema_version() -> None:
    payload = replace(_payload_with_cycle((WorkoutCycleSlot("w1"),)), schema_version=2)
    with pytest.raises(InvalidPlanPayload, match="schema_version"):
        validate_payload(payload)


async def test_validate_payload_rechecks_catalog_recommendable_flag(
    tmp_path: Path,
) -> None:
    candidates = await _recommendable(tmp_path)
    payload = _ready(_generate(candidates)).payload
    catalog = {
        exercise.id: replace(exercise, recommendable=False)
        if exercise.id == "pull-up"
        else exercise
        for exercise in candidates
    }
    with pytest.raises(InvalidPlanPayload, match="不可推荐"):
        validate_payload(payload, catalog=catalog)


# ---------- D9 JSON 形状与生成回归 ----------


async def test_payload_json_matches_d9_shape_without_weekday(tmp_path: Path) -> None:
    candidates = await _recommendable(tmp_path)
    raw = payload_to_json(_ready(_generate(candidates)).payload)
    payload = json.loads(raw)
    assert set(payload) == {
        "plan_workouts",
        "calendar_cycle",
        "template_key",
        "schema_version",
    }
    assert payload["schema_version"] == 1
    assert "weekday" not in raw  # weekday 不进 payload，只由 scheduled_on 派生

    for workout in payload["plan_workouts"]:
        assert set(workout) == {
            "workout_key",
            "name",
            "estimated_minutes",
            "exercises",
        }
        for item in workout["exercises"]:
            assert {
                "item_key",
                "exercise_id",
                "display_snapshot",
                "record_type",
                "prescription",
                "progression",
            } <= set(item)
            if item["record_type"] == "external_load_reps":
                assert set(item["load"]) == {
                    "kind",
                    "steps",
                    "pass_criteria",
                    "stop_criteria",
                }
                assert item["load"]["kind"] == "needs_calibration"
                assert "value" not in item["load"]  # 不得猜重
            else:
                assert "load" not in item

    slots = payload["calendar_cycle"]["slots"]
    assert slots[0] == {"kind": "workout", "workout_key": "push"}
    assert {"kind": "rest"} in slots
    assert payload["calendar_cycle"]["anchor_date"] == ANCHOR_DATE.isoformat()


async def test_generation_does_not_touch_formal_profile_or_business_version(
    tmp_path: Path,
) -> None:
    candidates = await _recommendable(tmp_path)
    payload = _ready(_generate(candidates)).payload
    assert payload.plan_workouts  # 生成成功即已足够：本函数不碰库

    async with open_database(tmp_path / "app.db") as db:
        before = await _profile_row(db)
    async with open_database(tmp_path / "app.db") as db:
        _ready(_generate(candidates))
        after = await _profile_row(db)
    assert after == before


async def _profile_row(db) -> tuple[object, object]:
    async def op(conn):
        async with conn.execute(
            "SELECT profile_json, context_version FROM user_profile WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return (row["profile_json"], row["context_version"])

    return await db.under_lock(op)
