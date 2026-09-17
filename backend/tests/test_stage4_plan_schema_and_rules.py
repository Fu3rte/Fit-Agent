"""Stage 4 子任务 02：统一计划 Schema、确定性规则与字段契约。

依据：``refactor-log/stage4.md`` §3.1／§3.3／§3.4／§3.5／§3.8／§8.1／§8.2／§8.3／§8.4／§8.5；
``Fit-Agent-LangGraph-重构讨论总结.md`` §3.3／§3.4／§7／§7.3／§8／§9；``LANGGRAPH_REFACTOR_PLAN.md``
§5.5／§6.5／§6.6。覆盖矩阵见 ``refactor-log/stage4.md`` §8.1–§8.5。

本文件只做纯领域断言（不读库、不调模型）：Schema／规则用真实 ``domain.plans.schema`` 与
``domain.plans.rules``；003 迁移另在 ``tests/test_stage4_migration.py``。

A+D 交接（§6 Subtask 01 的文档文本扫描 → 本文件，逐项落实）：

1. ``test_unauthorized_fields_only_appear_in_prohibitions`` 与其 ``UNAUTHORIZED_TERMS``／
   ``NEGATION_MARKERS``／否定语境启发式**删除**：未授权字段改由
   ``test_frozen_model_field_sets_reject_undeclared_fields`` 与
   ``test_unauthorized_field_payloads_are_rejected_by_schema`` 的真实 Schema 断言接手。
2. ``test_plan_status_enumeration_is_four_states_across_both_authorities``：四态与 ``PlanStatus``
   改由真实 Schema 断言；「两份权威文档互相一致」这一文档断言**保留**（它的依据是 stage4.md §6
   Subtask 01 验收「两份权威文档对状态、Stage 4/5 边界无冲突」，本 Subtask 未撤销）。
3. ``test_rejected_is_terminal_without_timestamp_and_hidden_from_draft_queries``：字段与时间字段
   部分转由 :func:`test_rejected_status_is_terminal_and_carries_no_timestamp_field` 的 Schema 断言；
   「不进 draft 列表／active 查询／日历 active 计划」是查询与持久化行为，**移交 §6 Subtask 03** 的
   repo／service 测试（``tests/test_stage4_plan_persistence.py``；本文件不再断言，也不删掉该移交）。
4. ``test_user_refusal_routes_to_archive_draft_not_rejected`` 与
   ``test_stage4_confirmed_decisions_are_present_in_the_stage_plan`` **保留**：它们无代码等价物
   （用户拒绝走 Stage 5 的 ``archive_draft``、已确认决策不受后续实现漂移影响），删除会静默丢掉
   阶段 4/5 边界与已确认决策的守卫。
"""

import ast
import copy
import dataclasses
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from domain.actions.schema import Exercise, RecordType
from domain.plans.rules import (
    InvalidPlanRule,
    filter_forbidden_exercises,
    known_forbidden_exercise_ids,
    resolve_progression,
    resolve_starting_load,
    validate_plan_draft,
)
from domain.plans.schema import (
    PLAN_STATUSES,
    BodyweightRepsPrescription,
    DeterministicResult,
    EvaluationResult,
    InvalidPlanRow,
    KnownLoad,
    NeedsCalibration,
    Plan,
    PlanDraft,
    PlannedExercise,
    PlanStatus,
    RubricResult,
    RubricVerdict,
    RuleFailure,
    TimedPrescription,
    TrainingDay,
    WeightedRepsPrescription,
    evaluation_result_from_json,
    evaluation_result_to_json,
    plan_draft_from_json,
    plan_draft_to_json,
)
from domain.profile.rules import WEEKLY_FREQUENCY_MAX, WEEKLY_FREQUENCY_MIN
from domain.profile.safety import MESSAGE_RED_FLAG_TERMS, message_red_flag_hits
from domain.profile.schema import Fact, Profile
from domain.records.rules import REPS_MAX, REPS_MIN
from domain.stats.schema import ValidWorkSet

# backend/tests/test_...py → backend/tests → backend → 仓库根；不依赖运行 cwd。
BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent

SUMMARY_PATH = REPO_ROOT / "Fit-Agent-LangGraph-重构讨论总结.md"
REFACTOR_PLAN_PATH = REPO_ROOT / "LANGGRAPH_REFACTOR_PLAN.md"
STAGE4_PATH = REPO_ROOT / "refactor-log" / "stage4.md"

#: Stage 4 领域模块不得依赖的框架（stage4.md §5.2；``domain/__init__`` 的同一约束）。
FORBIDDEN_DOMAIN_IMPORTS = (
    "fastapi",
    "langgraph",
    "langchain",
    "langchain_openai",
    "openai",
    "pydantic_ai",
)

#: 冻结的模型字段集合（stage4.md §3.1／§3.5 的逐字段转写、讨论总结 §7 的口径删除）。
FROZEN_MODEL_FIELDS: dict[type, set[str]] = {
    PlanDraft: {"goal", "starts_on", "explanation", "weekly_frequency", "training_days"},
    TrainingDay: {"scheduled_on", "exercises"},
    PlannedExercise: {"exercise_id", "sets", "prescription"},
    WeightedRepsPrescription: {"type", "reps_min", "reps_max", "load", "progression_note"},
    BodyweightRepsPrescription: {"type", "reps_min", "reps_max", "progression_note"},
    TimedPrescription: {
        "type",
        "duration_seconds_min",
        "duration_seconds_max",
        "progression_note",
    },
    KnownLoad: {"status", "weight_kg", "source_workout_session_id", "source_set_no"},
    NeedsCalibration: {"status"},
    DeterministicResult: {"passed", "failures"},
    RuleFailure: {"code", "message", "exercise_id"},
    RubricVerdict: {"passed", "reason"},
    RubricResult: {"goal_alignment", "schedule_reasonableness", "explanation_quality"},
    EvaluationResult: {
        "passed",
        "deterministic",
        "rubric",
        "blocking_failures",
        "warnings",
        "revision_count",
    },
}

#: plans 行的冻结列集合：四个状态字段之外没有任何 rejected 时间字段（讨论总结 §3.4）。
FROZEN_PLAN_ROW_FIELDS = {
    "id",
    "version",
    "status",
    "source_plan_id",
    "structured_content",
    "evaluator_result",
    "created_at",
    "confirmed_at",
    "archived_at",
}

#: Stage 4 不得授权出现的字段与口径（讨论总结 §7／§7.1／§9、stage4.md §2.2／§8.1）。
UNAUTHORIZED_KEYS = ("rejected_at", "RIR", "估算 1RM", "训练容量", "完成率")

#: 计划七天窗口的第一天（周一）。
STARTS_ON = date(2026, 6, 1)


def _error_types(exc: ValidationError) -> set[str]:
    return {error["type"] for error in exc.errors()}


def _lines_containing(path: Path, needle: str) -> list[tuple[int, str]]:
    """返回 ``path`` 中包含 ``needle`` 的 (行号, 行内容)，用于在断言失败时定位文档漂移。"""
    text = path.read_text(encoding="utf-8")
    return [
        (line_no, line)
        for line_no, line in enumerate(text.splitlines(), start=1)
        if needle in line
    ]


# ---------- 固定事实构造器（无 IO） ----------


def _exercise(
    exercise_id: str,
    record_type: RecordType,
    *,
    recommendable: bool = True,
    increment_kg: float | None = 2.5,
) -> Exercise:
    """一个目录动作：负重口径与加重单位只属于外加负重类型（与库内 CHECK 同集合）。"""
    weighted = record_type == "reps_weight"
    return Exercise(
        id=exercise_id,
        standard_name_zh=f"测试动作 {exercise_id}",
        equipment_variant="barbell" if weighted else "bodyweight",
        record_type=record_type,
        load_convention="barbell_includes_bar_total" if weighted else None,
        min_load_increment_kg=increment_kg if weighted else None,
        recommendable=recommendable,
        modes=("深蹲",),
        source_ref="tests/test_stage4_plan_schema_and_rules.py",
        attribution="测试固定事实",
    )


CATALOG: dict[str, Exercise] = {
    exercise.id: exercise
    for exercise in (
        _exercise("barbell-back-squat", "reps_weight"),
        _exercise("pull-up", "reps_bodyweight"),
        _exercise("plank", "time"),
        _exercise("forbidden-machine", "reps_weight"),
        _exercise("retired-machine", "reps_weight", recommendable=False),
    )
}

#: 目录里的最小加重单位（渐进只能加这一档，不自造阈值）。
SQUAT_INCREMENT_KG = 2.5


def _known_load(weight_kg: float = 60.0, *, session: int = 7, set_no: int = 2) -> KnownLoad:
    return KnownLoad(
        status="known",
        weight_kg=weight_kg,
        source_workout_session_id=session,
        source_set_no=set_no,
    )


def _needs_calibration() -> NeedsCalibration:
    return NeedsCalibration(status="needs_calibration")


def _weighted_prescription(
    *,
    load: KnownLoad | NeedsCalibration | None = None,
    reps_min: int = 8,
    reps_max: int = 12,
) -> WeightedRepsPrescription:
    return WeightedRepsPrescription(
        type="weighted_reps",
        reps_min=reps_min,
        reps_max=reps_max,
        load=_known_load() if load is None else load,
    )


def _bodyweight_prescription(reps_min: int = 8, reps_max: int = 12) -> BodyweightRepsPrescription:
    return BodyweightRepsPrescription(
        type="bodyweight_reps", reps_min=reps_min, reps_max=reps_max
    )


def _timed_prescription(
    duration_seconds_min: int = 30, duration_seconds_max: int = 60
) -> TimedPrescription:
    return TimedPrescription(
        type="timed",
        duration_seconds_min=duration_seconds_min,
        duration_seconds_max=duration_seconds_max,
    )


def _planned(
    exercise_id: str,
    prescription: WeightedRepsPrescription
    | BodyweightRepsPrescription
    | TimedPrescription,
    *,
    sets: int = 3,
) -> PlannedExercise:
    return PlannedExercise(exercise_id=exercise_id, sets=sets, prescription=prescription)


def _day(offset: int, *exercises: PlannedExercise) -> TrainingDay:
    return TrainingDay(
        scheduled_on=STARTS_ON + timedelta(days=offset), exercises=tuple(exercises)
    )


def _draft(
    *days: TrainingDay, weekly_frequency: int | None = None, explanation: str = "三练分化，逐周加重"
) -> PlanDraft:
    return PlanDraft(
        goal="增肌",
        starts_on=STARTS_ON,
        explanation=explanation,
        weekly_frequency=len(days) if weekly_frequency is None else weekly_frequency,
        training_days=tuple(days),
    )


def _weighted_day(offset: int, exercise_id: str = "barbell-back-squat") -> TrainingDay:
    return _day(offset, _planned(exercise_id, _weighted_prescription(load=_needs_calibration())))


def _work_set(
    *,
    performed_on: date,
    session: int,
    set_no: int,
    weight_kg: float | None = 60.0,
    reps: int | None = 8,
    exercise_id: str = "barbell-back-squat",
) -> ValidWorkSet:
    """一个有效工作组（热身／assisted／不完整组由 ``domain.stats.repo`` 的唯一 SQL 排除，不在本类型里）。"""
    return ValidWorkSet(
        exercise_id=exercise_id,
        exercise_name="杠铃背蹲",
        record_type="reps_weight",
        load_convention="barbell_includes_bar_total",
        weight_kg=weight_kg,
        reps=reps,
        duration_seconds=None,
        workout_session_id=session,
        set_no=set_no,
        performed_on=performed_on,
    )


def _training(
    session: int, performed_on: date, loads: tuple[float, ...], reps: tuple[int, ...]
) -> list[ValidWorkSet]:
    """一次训练里某动作的目标工作组（``set_no`` 从 1 起，与库内确定性排序一致）。"""
    return [
        _work_set(
            performed_on=performed_on,
            session=session,
            set_no=index + 1,
            weight_kg=weight_kg,
            reps=reps_value,
        )
        for index, (weight_kg, reps_value) in enumerate(zip(loads, reps, strict=True))
    ]


def _evaluation(
    *,
    deterministic_passed: bool,
    goal_alignment: bool = True,
    schedule_reasonableness: bool = True,
    explanation_quality: bool = True,
    passed: bool | None = None,
    warnings: tuple[str, ...] = (),
    revision_count: int = 0,
) -> EvaluationResult:
    expected = deterministic_passed and goal_alignment and schedule_reasonableness
    return EvaluationResult(
        passed=expected if passed is None else passed,
        deterministic=DeterministicResult(
            passed=deterministic_passed,
            failures=() if deterministic_passed else (RuleFailure(code="unknown_exercise", message="动作不在目录内"),),
        ),
        rubric=RubricResult(
            goal_alignment=RubricVerdict(passed=goal_alignment, reason="目标匹配"),
            schedule_reasonableness=RubricVerdict(
                passed=schedule_reasonableness, reason="安排合理"
            ),
            explanation_quality=RubricVerdict(passed=explanation_quality, reason="解释质量"),
        ),
        blocking_failures=() if expected else ("确定性校验未通过",),
        warnings=warnings,
        revision_count=revision_count,
    )


# ---------- §8.1 Schema 与目录匹配 ----------


def _codes(failures: tuple[RuleFailure, ...]) -> list[str]:
    return [failure.code for failure in failures]


def _exercise_json(prescription: dict[str, Any]) -> str:
    return json.dumps(
        {"exercise_id": "barbell-back-squat", "sets": 3, "prescription": prescription}
    )


def test_plan_status_enumeration_is_four_states_across_both_authorities() -> None:
    """Schema 四态（stage4.md §3.8）与两份权威文档的四状态清单必须一致（文档一致性断言保留）。"""
    assert PLAN_STATUSES == ("draft", "active", "archived", "rejected")
    assert set(PlanStatus.__args__) == set(PLAN_STATUSES)

    enumeration = " / ".join(PLAN_STATUSES)
    summary_hits = _lines_containing(SUMMARY_PATH, enumeration)
    plan_hits = _lines_containing(REFACTOR_PLAN_PATH, enumeration)
    # 讨论总结至少覆盖数据模型（§9）与沿用边界表（§12.2）两处。
    assert len(summary_hits) >= 2, "讨论总结缺少四状态清单"
    assert plan_hits, "REFACTOR_PLAN 缺少四状态清单"

    # 旧三状态口径不得残留：任何出现 "draft / active / archived" 的行都必须同时带 rejected。
    for path in (SUMMARY_PATH, REFACTOR_PLAN_PATH):
        for line_no, line in _lines_containing(path, "draft / active / archived"):
            assert "rejected" in line, f"{path.name}:{line_no} 仍是三状态清单：{line}"


def test_frozen_model_field_sets_reject_undeclared_fields() -> None:
    """每个模型的字段集合必须精确等于冻结契约，且 ``extra='forbid'``／strict 是共同底线。"""
    for model, expected in FROZEN_MODEL_FIELDS.items():
        assert set(model.model_fields) == expected, model.__name__
        assert model.model_config["extra"] == "forbid", model.__name__
        assert model.model_config["strict"] is True, model.__name__

    # 计划行也没有任何 rejected 时间字段（时间字段部分取代旧的文档扫描断言）。
    assert {field.name for field in dataclasses.fields(Plan)} == FROZEN_PLAN_ROW_FIELDS
    assert not [name for name in FROZEN_PLAN_ROW_FIELDS if "rejected" in name]


def test_unauthorized_field_payloads_are_rejected_by_schema() -> None:
    """未授权字段（rejected_at／RIR／估算 1RM／训练容量／完成率）在任何层级都被 Schema 拒绝。"""
    draft_payload = json.loads(plan_draft_to_json(_draft(_weighted_day(0))))
    for key in UNAUTHORIZED_KEYS:
        places: tuple[tuple[str, Any], ...] = (
            ("顶层", draft_payload),
            ("训练日", draft_payload["training_days"][0]),
            ("计划动作", draft_payload["training_days"][0]["exercises"][0]),
            ("处方", draft_payload["training_days"][0]["exercises"][0]["prescription"]),
            (
                "负荷",
                draft_payload["training_days"][0]["exercises"][0]["prescription"]["load"],
            ),
        )
        for label, place in places:
            mutated = copy.deepcopy(draft_payload)
            target = mutated
            if label == "训练日":
                target = mutated["training_days"][0]
            elif label == "计划动作":
                target = mutated["training_days"][0]["exercises"][0]
            elif label == "处方":
                target = mutated["training_days"][0]["exercises"][0]["prescription"]
            elif label == "负荷":
                target = mutated["training_days"][0]["exercises"][0]["prescription"]["load"]
            target[key] = 1
            with pytest.raises(ValidationError) as excinfo:
                PlanDraft.model_validate_json(json.dumps(mutated))
            assert "extra_forbidden" in _error_types(excinfo.value), f"{label} 接受未授权字段 {key}"

    result_payload = json.loads(evaluation_result_to_json(_evaluation(deterministic_passed=True)))
    for key in UNAUTHORIZED_KEYS:
        mutated = copy.deepcopy(result_payload)
        mutated[key] = 1
        with pytest.raises(ValidationError) as excinfo:
            EvaluationResult.model_validate_json(json.dumps(mutated))
        assert "extra_forbidden" in _error_types(excinfo.value)
        nested = copy.deepcopy(result_payload)
        nested["deterministic"]["failures"] = [{"code": "x", "message": "y", key: 1}]
        with pytest.raises(ValidationError) as excinfo:
            EvaluationResult.model_validate_json(json.dumps(nested))
        assert "extra_forbidden" in _error_types(excinfo.value)


def test_three_prescription_kinds_are_mutually_exclusive() -> None:
    """三类处方各自合法，且字段严格互斥；判别联合按 ``type`` 收敛。"""
    exercise_payload = _planned("barbell-back-squat", _weighted_prescription())
    assert exercise_payload.prescription == _weighted_prescription()
    assert _planned("pull-up", _bodyweight_prescription()).prescription.type == "bodyweight_reps"
    assert _planned("plank", _timed_prescription()).prescription.type == "timed"

    # 合法载荷（JSON 路径与 Planner 输出一致）逐个通过。
    legal = (
        {"type": "weighted_reps", "reps_min": 8, "reps_max": 12, "load": {"status": "known", "weight_kg": 60.0, "source_workout_session_id": 7, "source_set_no": 2}},
        {"type": "weighted_reps", "reps_min": 8, "reps_max": 12, "load": {"status": "needs_calibration"}, "progression_note": None},
        {"type": "bodyweight_reps", "reps_min": 5, "reps_max": 10},
        {"type": "timed", "duration_seconds_min": 30, "duration_seconds_max": 60},
    )
    for payload in legal:
        PlannedExercise.model_validate_json(_exercise_json(payload))

    illegal = (
        # 计时处方不得携带次数。
        {"type": "timed", "duration_seconds_min": 30, "duration_seconds_max": 60, "reps_min": 8},
        # 自重处方不得携带负荷或重量。
        {"type": "bodyweight_reps", "reps_min": 5, "reps_max": 10, "load": {"status": "needs_calibration"}},
        {"type": "bodyweight_reps", "reps_min": 5, "reps_max": 10, "weight_kg": 20.0},
        # 负重处方不得携带时长。
        {"type": "weighted_reps", "reps_min": 8, "reps_max": 12, "load": {"status": "needs_calibration"}, "duration_seconds_min": 30},
        # 处方类型不在三类之内。
        {"type": "rir_based", "reps_min": 8, "reps_max": 12},
    )
    for payload in illegal:
        with pytest.raises(ValidationError) as excinfo:
            PlannedExercise.model_validate_json(_exercise_json(payload))
        assert {"extra_forbidden", "union_tag_invalid"} & _error_types(excinfo.value), payload


def test_reps_and_duration_ranges_reuse_recorded_facts() -> None:
    """次数复用已合入记录规则的 1–100；计时下限至少 1 秒且无业务上限；区间 min <= max。"""
    assert (REPS_MIN, REPS_MAX) == (1, 100)
    assert (WEEKLY_FREQUENCY_MIN, WEEKLY_FREQUENCY_MAX) == (1, 7)

    for payload in (
        {"type": "weighted_reps", "reps_min": REPS_MIN, "reps_max": REPS_MAX, "load": {"status": "needs_calibration"}},
        {"type": "bodyweight_reps", "reps_min": REPS_MIN, "reps_max": REPS_MIN},
    ):
        PlannedExercise.model_validate_json(_exercise_json(payload))

    for payload in (
        {"type": "weighted_reps", "reps_min": REPS_MIN - 1, "reps_max": 10, "load": {"status": "needs_calibration"}},
        {"type": "weighted_reps", "reps_min": 8, "reps_max": REPS_MAX + 1, "load": {"status": "needs_calibration"}},
        {"type": "bodyweight_reps", "reps_min": 12, "reps_max": 8},
    ):
        with pytest.raises(ValidationError):
            PlannedExercise.model_validate_json(_exercise_json(payload))

    # 计时：下限 1 秒、无业务上限（超大秒数合法），区间倒置非法。
    PlannedExercise.model_validate_json(
        _exercise_json({"type": "timed", "duration_seconds_min": 1, "duration_seconds_max": 10**9})
    )
    for payload in (
        {"type": "timed", "duration_seconds_min": 0, "duration_seconds_max": 60},
        {"type": "timed", "duration_seconds_min": 60, "duration_seconds_max": 30},
        {"type": "timed", "duration_seconds_min": 30, "duration_seconds_max": -1},
    ):
        with pytest.raises(ValidationError):
            PlannedExercise.model_validate_json(_exercise_json(payload))

    # 组数：正整数（无自造上限）；0 与负数失败。
    for sets in (0, -3):
        with pytest.raises(ValidationError):
            PlannedExercise.model_validate_json(
                json.dumps(
                    {
                        "exercise_id": "barbell-back-squat",
                        "sets": sets,
                        "prescription": {"type": "bodyweight_reps", "reps_min": 5, "reps_max": 10},
                    }
                )
            )
    assert _planned("pull-up", _bodyweight_prescription(), sets=1).sets == 1


def test_seven_day_window_unique_dates_and_frequency_are_exact() -> None:
    """七天窗口、唯一训练日与「训练日数量 == 每周训练次数」必须精确成立。"""
    # 合法：频率 3 与窗口首尾日。
    assert _draft(_weighted_day(0), _weighted_day(3), _weighted_day(6)).weekly_frequency == 3

    # 数量与频率不一致。
    with pytest.raises(ValidationError) as excinfo:
        _draft(_weighted_day(0), _weighted_day(3), weekly_frequency=3)
    assert "训练日数量必须等于每周训练次数" in str(excinfo.value)

    # 训练日重复。
    with pytest.raises(ValidationError) as excinfo:
        _draft(_weighted_day(0), _weighted_day(0))
    assert "训练日期重复" in str(excinfo.value)

    # 越过七天窗口（前一天与第 8 天）。
    for offset in (-1, 7):
        with pytest.raises(ValidationError) as excinfo:
            _draft(_weighted_day(offset))
        assert "训练日期必须落在" in str(excinfo.value)

    # 频率值域复用画像 1–7：0 与 8 都失败。
    for frequency in (0, 8):
        with pytest.raises(ValidationError):
            _draft(_weighted_day(0), weekly_frequency=frequency)

    # 空训练日与同日重复动作。
    with pytest.raises(ValidationError):
        TrainingDay(scheduled_on=STARTS_ON, exercises=())
    with pytest.raises(ValidationError) as excinfo:
        _day(0, _planned("pull-up", _bodyweight_prescription()), _planned("pull-up", _bodyweight_prescription()))
    assert "同一训练日不得重复同一动作" in str(excinfo.value)

    # 非空文本：目标、解释与动作身份。
    payload = json.loads(plan_draft_to_json(_draft(_weighted_day(0))))
    payload["goal"] = "   "
    with pytest.raises(ValidationError) as excinfo:
        PlanDraft.model_validate_json(json.dumps(payload))
    assert "必须是非空文本" in str(excinfo.value)
    with pytest.raises(ValidationError) as excinfo:
        _draft(_weighted_day(0), explanation="")
    assert "必须是非空文本" in str(excinfo.value)
    for exercise_id in ("", "   "):
        with pytest.raises(ValidationError) as excinfo:
            _planned(exercise_id, _bodyweight_prescription())
        assert "必须是非空文本" in str(excinfo.value)


def test_catalog_record_type_and_recommendable_matching() -> None:
    """处方类型必须匹配目录记录口径；动作必须存在且可用于计划；频率必须复用画像值。"""
    # 合法：三类处方分别对上三类记录口径，待校准负荷无需历史。
    matching = _draft(
        _day(
            0,
            _planned("barbell-back-squat", _weighted_prescription(load=_needs_calibration())),
            _planned("pull-up", _bodyweight_prescription()),
            _planned("plank", _timed_prescription()),
        )
    )
    assert (
        validate_plan_draft(
            matching, exercises=CATALOG, profile_weekly_frequency=matching.weekly_frequency
        )
        == ()
    )

    # 负重动作使用计时处方。
    weighted_with_timed = _draft(_day(0, _planned("barbell-back-squat", _timed_prescription())))
    assert _codes(
        validate_plan_draft(
            weighted_with_timed,
            exercises=CATALOG,
            profile_weekly_frequency=weighted_with_timed.weekly_frequency,
        )
    ) == ["record_type_mismatch"]

    # 计时动作使用自重次数处方、自重动作使用负重处方。
    timed_with_reps = _draft(_day(0, _planned("plank", _bodyweight_prescription())))
    assert "record_type_mismatch" in _codes(
        validate_plan_draft(
            timed_with_reps,
            exercises=CATALOG,
            profile_weekly_frequency=timed_with_reps.weekly_frequency,
        )
    )

    # 未知动作与不可推荐动作。
    unknown = _draft(_day(0, _planned("no-such-action", _weighted_prescription(load=_needs_calibration()))))
    assert _codes(
        validate_plan_draft(
            unknown, exercises=CATALOG, profile_weekly_frequency=unknown.weekly_frequency
        )
    ) == ["unknown_exercise"]
    retired = _draft(_day(0, _planned("retired-machine", _weighted_prescription(load=_needs_calibration()))))
    assert _codes(
        validate_plan_draft(
            retired, exercises=CATALOG, profile_weekly_frequency=retired.weekly_frequency
        )
    ) == ["exercise_not_recommendable"]

    # 频率必须复用画像的 known 值。
    draft = _draft(_weighted_day(0), _weighted_day(3))
    assert _codes(
        validate_plan_draft(draft, exercises=CATALOG, profile_weekly_frequency=3)
    ) == ["weekly_frequency_mismatch"]
    assert (
        validate_plan_draft(draft, exercises=CATALOG, profile_weekly_frequency=2) == ()
    )


# ---------- §8.2 安全（领域层）与禁用动作 ----------


def test_forbidden_exercises_are_removed_before_planning_and_rejected_in_output() -> None:
    """禁用 ID 确定性排除候选动作，且出现在计划里即评估失败（评估器再检查一次）。"""
    candidates = tuple(CATALOG.values())
    filtered = filter_forbidden_exercises(
        candidates, forbidden_exercise_ids=("forbidden-machine",)
    )
    assert [exercise.id for exercise in filtered] == [
        exercise.id for exercise in candidates if exercise.id != "forbidden-machine"
    ]
    assert [exercise.id for exercise in filter_forbidden_exercises(candidates, forbidden_exercise_ids=())] == [
        exercise.id for exercise in candidates
    ]

    # 画像只取 known 值；unknown／denied 不补造 ID。
    assert known_forbidden_exercise_ids(
        Profile(forbidden_exercise_ids=Fact.known(("forbidden-machine",)))
    ) == ("forbidden-machine",)
    assert known_forbidden_exercise_ids(Profile(forbidden_exercise_ids=Fact.unknown())) == ()
    assert known_forbidden_exercise_ids(Profile(forbidden_exercise_ids=Fact.denied())) == ()

    draft = _draft(_day(0, _planned("forbidden-machine", _weighted_prescription(load=_needs_calibration()))))
    assert _codes(
        validate_plan_draft(
            draft,
            exercises=CATALOG,
            profile_weekly_frequency=draft.weekly_frequency,
            forbidden_exercise_ids=("forbidden-machine",),
        )
    ) == ["forbidden_exercise"]
    assert (
        validate_plan_draft(
            draft, exercises=CATALOG, profile_weekly_frequency=draft.weekly_frequency
        )
        == ()
    )


def test_acute_red_flag_vocabulary_is_reused_without_extension() -> None:
    """10 项封闭词表、精确子串、否定表达保守命中、非词表文本不命中。

    这是 §8.2 的领域层部分（纯函数：不改词表、不做同义词与否定语义分析）；「命中后不加载
    Skill、不装配 Memory、不进 Planner、不写库」的图层行为由 §6 Subtask 04 的节点测试覆盖。
    """
    assert MESSAGE_RED_FLAG_TERMS == (
        "胸部异常不适",
        "晕厥",
        "异常气短",
        "锐痛",
        "麻木",
        "放射痛",
        "疼痛持续加重",
        "明显肿胀",
        "卡锁",
        "关节失稳",
    )
    for term in MESSAGE_RED_FLAG_TERMS:
        assert message_red_flag_hits(f"昨天训练后{term}，想重新排计划") == (term,)
    # 一个请求命中多词时按封闭词表顺序返回。
    assert message_red_flag_hits("麻木并且晕厥") == ("晕厥", "麻木")
    # 否定表达仍保守命中。
    assert message_red_flag_hits("没有麻木，只是想换计划") == ("麻木",)
    # 不在词表的普通酸痛与无痛关节响不命中。
    for text in ("肩膀有点酸", "膝盖有一声响", "练完有点累"):
        assert message_red_flag_hits(text) == ()


def test_known_injuries_never_derive_forbidden_ids() -> None:
    """已知伤病文本不参与推导：画像的禁用 ID 只能来自 ``forbidden_exercise_ids.known``。"""
    profile = Profile(
        training_goal=Fact.known("增肌"),
        known_injuries=Fact.known(("左膝半月板损伤", "肩袖炎症")),
        forbidden_exercise_ids=Fact.unknown(),
    )
    assert known_forbidden_exercise_ids(profile) == ()
    candidates = tuple(CATALOG.values())
    assert (
        filter_forbidden_exercises(
            candidates, forbidden_exercise_ids=known_forbidden_exercise_ids(profile)
        )
        == candidates
    )


# ---------- §8.3 负荷来源 ----------


def test_starting_load_is_the_most_recent_valid_work_set() -> None:
    """起始负荷取最近一次有效工作组，并保留训练身份与组序号来源。"""
    history = (
        _work_set(performed_on=date(2026, 5, 1), session=1, set_no=1, weight_kg=50.0, reps=8),
        _work_set(performed_on=date(2026, 5, 1), session=1, set_no=2, weight_kg=55.0, reps=8),
        # 同日两次训练：按训练身份（session id）决定最近来源。
        _work_set(performed_on=date(2026, 5, 1), session=2, set_no=1, weight_kg=57.5, reps=6),
        _work_set(performed_on=date(2026, 5, 20), session=3, set_no=1, weight_kg=60.0, reps=8),
        _work_set(performed_on=date(2026, 5, 20), session=3, set_no=2, weight_kg=62.5, reps=8),
        # 另一个动作的记录不得成为来源。
        _work_set(
            performed_on=date(2026, 6, 1),
            session=4,
            set_no=1,
            weight_kg=200.0,
            reps=1,
            exercise_id="weighted-pull-up",
        ),
    )
    assert resolve_starting_load(history, exercise_id="barbell-back-squat") == _known_load(
        62.5, session=3, set_no=2
    )

    # 没有任何该动作的有效工作组：只能待校准（不猜重量、不取别的动作）。
    assert isinstance(
        resolve_starting_load(history, exercise_id="lat-pulldown"), NeedsCalibration
    )


def test_larger_personal_best_never_becomes_the_starting_load() -> None:
    """历史上更重的一次（PB）不能作为来源：只取最近一次有效工作组，不从 PB 反推。"""
    history = (
        _work_set(performed_on=date(2026, 1, 5), session=1, set_no=1, weight_kg=100.0, reps=3),
        _work_set(performed_on=date(2026, 5, 20), session=2, set_no=1, weight_kg=60.0, reps=8),
    )
    latest = resolve_starting_load(history, exercise_id="barbell-back-squat")
    assert latest == _known_load(60.0, session=2, set_no=1)
    assert latest.weight_kg == 60.0

    # 计划若按 PB（100kg）给出具体负荷，确定性层拒绝。
    draft = _draft(
        _day(0, _planned("barbell-back-squat", _weighted_prescription(load=_known_load(100.0, session=1, set_no=1))))
    )
    assert _codes(
        validate_plan_draft(
            draft,
            exercises=CATALOG,
            profile_weekly_frequency=draft.weekly_frequency,
            work_sets=history,
        )
    ) == ["load_source_mismatch"]

    # 来源必须精确指向最近一次有效工作组（内部一致但与最近一次不符同样失败）。
    stale = _draft(
        _day(0, _planned("barbell-back-squat", _weighted_prescription(load=_known_load(60.0, session=1, set_no=1))))
    )
    assert _codes(
        validate_plan_draft(
            stale,
            exercises=CATALOG,
            profile_weekly_frequency=stale.weekly_frequency,
            work_sets=history,
        )
    ) == ["load_source_mismatch"]
    # 与最近一次有效工作组一致则通过。
    assert (
        validate_plan_draft(
            _draft(
                _day(0, _planned("barbell-back-squat", _weighted_prescription(load=_known_load(60.0, session=2, set_no=1))))
            ),
            exercises=CATALOG,
            profile_weekly_frequency=1,
            work_sets=history,
        )
        == ()
    )


def test_without_valid_history_only_needs_calibration() -> None:
    """没有有效历史时只能待校准：具体重量失败，待校准结构不得携带重量或来源。"""
    draft = _draft(_day(0, _planned("barbell-back-squat", _weighted_prescription())))
    assert _codes(
        validate_plan_draft(
            draft, exercises=CATALOG, profile_weekly_frequency=1
        )
    ) == ["load_source_mismatch"]

    calibration_only = _draft(
        _day(0, _planned("barbell-back-squat", _weighted_prescription(load=_needs_calibration())))
    )
    assert (
        validate_plan_draft(
            calibration_only, exercises=CATALOG, profile_weekly_frequency=1
        )
        == ()
    )

    # 待校准不得携带重量或伪造来源。
    for payload in (
        {"status": "needs_calibration", "weight_kg": 60.0},
        {"status": "needs_calibration", "source_workout_session_id": 7},
        {"status": "needs_calibration", "source_set_no": 1},
    ):
        with pytest.raises(ValidationError) as excinfo:
            PlannedExercise.model_validate_json(
                _exercise_json(
                    {"type": "weighted_reps", "reps_min": 8, "reps_max": 12, "load": payload}
                )
            )
        assert "extra_forbidden" in _error_types(excinfo.value), payload

    # 具体重量与来源必须同现，且重量沿用记录口径（0–1000kg、最多一位小数）。
    for payload in (
        {"status": "known", "source_workout_session_id": 7, "source_set_no": 1},
        {"status": "known", "weight_kg": 60.0, "source_set_no": 1},
        {"status": "known", "weight_kg": 60.0, "source_workout_session_id": 7},
        {"status": "known", "weight_kg": 60.55, "source_workout_session_id": 7, "source_set_no": 1},
        {"status": "known", "weight_kg": 1000.5, "source_workout_session_id": 7, "source_set_no": 1},
        {"status": "known", "weight_kg": -1.0, "source_workout_session_id": 7, "source_set_no": 1},
    ):
        with pytest.raises(ValidationError):
            PlannedExercise.model_validate_json(
                _exercise_json({"type": "weighted_reps", "reps_min": 8, "reps_max": 12, "load": payload})
            )
    assert _known_load(62.5) == KnownLoad.model_validate(
        {"status": "known", "weight_kg": 62.5, "source_workout_session_id": 7, "source_set_no": 2}
    )


# ---------- §8.4 渐进与回退（10B／10B-1，纯函数固定事实复算） ----------


def _progression(
    sessions: dict[int, list[ValidWorkSet]],
    *linked: int,
    target_load_kg: float = 60.0,
    target_sets: int = 3,
) -> tuple[str, float | None]:
    decision = resolve_progression(
        tuple(work_set for work_sets in sessions.values() for work_set in work_sets),
        linked_workout_session_ids=set(linked),
        target_sets=target_sets,
        reps_min=8,
        reps_max=12,
        target_load_kg=target_load_kg,
        increment_kg=SQUAT_INCREMENT_KG,
    )
    return decision.action, decision.load_kg


def test_two_completed_trainings_at_target_load_increment_once() -> None:
    """最近两次关联计划训练都在目标负荷完整达标：只加一次目录 min_load_increment_kg。"""
    sessions = {
        1: _training(1, date(2026, 5, 4), (60.0, 60.0, 60.0), (12, 12, 12)),
        2: _training(2, date(2026, 5, 11), (60.0, 60.0, 60.0), (12, 12, 13)),
    }
    assert SQUAT_INCREMENT_KG == CATALOG["barbell-back-squat"].min_load_increment_kg
    assert _progression(sessions, 1, 2) == ("increase", 62.5)


def test_single_completed_training_does_not_increment() -> None:
    """只有一次达标不得加重（也还没到连续两次未达标）。"""
    sessions = {1: _training(1, date(2026, 5, 4), (60.0, 60.0, 60.0), (12, 12, 12))}
    assert _progression(sessions, 1) == ("keep", 60.0)

    sessions[2] = _training(2, date(2026, 5, 11), (60.0, 60.0, 60.0), (12, 12, 12))
    assert _progression(sessions, 1, 2) == ("increase", 62.5)


def test_extra_unlinked_training_neither_counts_nor_breaks_continuity() -> None:
    """``plan_session_id IS NULL`` 的额外训练既不计入，也不打断两次关联训练的连续性。"""
    sessions = {
        1: _training(1, date(2026, 5, 4), (60.0, 60.0, 60.0), (12, 12, 12)),
        # 额外训练：只有一组且明显未达标；若被计入，最近两次里就出现一次未达标。
        2: _training(2, date(2026, 5, 6), (60.0,), (3,)),
        3: _training(3, date(2026, 5, 11), (60.0, 60.0, 60.0), (12, 12, 12)),
    }
    assert _progression(sessions, 1, 3) == ("increase", 62.5)
    # 反向证据：把额外训练当作关联训练时不再加重（说明上一条结论确实来自「额外训练不计数」）。
    assert _progression(sessions, 1, 2, 3) == ("keep", 60.0)


def test_insufficient_target_sets_or_below_min_counts_as_failed() -> None:
    """目标组数不足与任一组低于次数下限都算未达标（阻断「两次达标」的加重）。"""
    complete = _training(1, date(2026, 5, 4), (60.0, 60.0, 60.0), (12, 12, 12))
    insufficient_sets = _training(2, date(2026, 5, 11), (60.0, 60.0), (12, 12))
    below_min = _training(3, date(2026, 5, 18), (60.0, 60.0, 60.0), (12, 7, 12))
    assert _progression({1: complete, 2: insufficient_sets}, 1, 2) == ("keep", 60.0)
    assert _progression({1: complete, 3: below_min}, 1, 3) == ("keep", 60.0)


def test_two_failed_trainings_regress_to_last_completed_load() -> None:
    """连续两次未达标回退到关联历史中最近一次完整完成的负荷。"""
    sessions = {
        1: _training(1, date(2026, 4, 27), (55.0, 55.0, 55.0), (12, 12, 12)),
        2: _training(2, date(2026, 5, 4), (60.0, 60.0), (12, 12)),
        3: _training(3, date(2026, 5, 11), (60.0, 60.0, 60.0), (12, 6, 12)),
    }
    assert _progression(sessions, 1, 2, 3) == ("regress", 55.0)


def test_failed_trainings_without_completed_history_become_needs_calibration() -> None:
    """没有可回退的完整完成负荷时变为待校准（不生成具体重量）。"""
    sessions = {
        2: _training(2, date(2026, 5, 4), (60.0, 60.0), (12, 12)),
        3: _training(3, date(2026, 5, 11), (60.0, 60.0, 60.0), (12, 6, 12)),
    }
    assert _progression(sessions, 2, 3) == ("needs_calibration", None)


def test_extra_work_sets_do_not_change_target_judgement() -> None:
    """同一次训练只按 ``set_no`` 取计划要求数量的目标组：额外 work 组不改变判定。

    热身组与 assisted 组不在 ``ValidWorkSet`` 里（其过滤口径的唯一出处是 ``domain.stats.repo``
    的共享 SQL），因此不可能进入本函数的判断。
    """
    sessions = {
        1: _training(1, date(2026, 5, 4), (60.0, 60.0, 60.0), (12, 12, 12))
        + [
            _work_set(performed_on=date(2026, 5, 4), session=1, set_no=4, weight_kg=80.0, reps=1),
        ],
        2: _training(2, date(2026, 5, 11), (60.0, 60.0, 60.0), (12, 12, 12))
        + [
            _work_set(performed_on=date(2026, 5, 11), session=2, set_no=4, weight_kg=60.0, reps=20),
        ],
    }
    assert _progression(sessions, 1, 2) == ("increase", 62.5)

    # 目标组负荷不一致：不算「在同一负荷完整达标」，也不构成 10B-1 的未达标 → 保持。
    mixed = {
        1: _training(1, date(2026, 5, 4), (60.0, 60.0, 62.5), (12, 12, 12)),
        2: _training(2, date(2026, 5, 11), (60.0, 57.5, 60.0), (12, 12, 12)),
    }
    assert _progression(mixed, 1, 2) == ("keep", 60.0)


def test_bodyweight_and_timed_prescriptions_carry_no_load_progression() -> None:
    """自重与计时动作不套用重量加重规则：处方不带负荷，确定性层也不产生负荷失败。"""
    assert "load" not in BodyweightRepsPrescription.model_fields
    assert "load" not in TimedPrescription.model_fields
    assert "reps_min" not in TimedPrescription.model_fields

    with pytest.raises(ValidationError) as excinfo:
        PlannedExercise.model_validate_json(
            _exercise_json(
                {
                    "type": "bodyweight_reps",
                    "reps_min": 5,
                    "reps_max": 10,
                    "load": {"status": "needs_calibration"},
                }
            )
        )
    assert "extra_forbidden" in _error_types(excinfo.value)

    draft = _draft(
        _day(
            0,
            _planned("pull-up", _bodyweight_prescription()),
            _planned("plank", _timed_prescription()),
        )
    )
    assert (
        validate_plan_draft(
            draft, exercises=CATALOG, profile_weekly_frequency=draft.weekly_frequency
        )
        == ()
    )

    # 提前量参数不合法是调用方错误（不是计划失败项）。
    with pytest.raises(InvalidPlanRule):
        resolve_progression(
            (),
            linked_workout_session_ids=set(),
            target_sets=3,
            reps_min=8,
            reps_max=12,
            target_load_kg=60.0,
            increment_kg=0.0,
        )


# ---------- §8.5 Evaluator Schema 与确定性分层 ----------


def test_evaluation_result_passed_matches_the_two_hard_gates() -> None:
    """``passed`` 必须精确等于「确定性层通过且两个硬门槛通过」的合取。"""
    assert _evaluation(deterministic_passed=True).passed is True
    assert _evaluation(deterministic_passed=False).passed is False
    assert _evaluation(deterministic_passed=True, goal_alignment=False).passed is False
    assert _evaluation(deterministic_passed=True, schedule_reasonableness=False).passed is False

    for kwargs in (
        {"deterministic_passed": False, "passed": True},
        {"deterministic_passed": True, "goal_alignment": False, "passed": True},
        {"deterministic_passed": True, "passed": False},
    ):
        with pytest.raises(ValidationError) as excinfo:
            _evaluation(**kwargs)
        assert "passed 必须等于" in str(excinfo.value)

    # revision_count 非负；确定性失败项全量保留。
    with pytest.raises(ValidationError):
        _evaluation(deterministic_passed=True, revision_count=-1)
    failed = _evaluation(deterministic_passed=False)
    assert failed.deterministic.passed is False
    assert [failure.code for failure in failed.deterministic.failures] == ["unknown_exercise"]


def test_explanation_quality_failure_stays_a_warning() -> None:
    """解释质量失败只进入 warning：不阻断、不影响 ``passed``，也不消耗修订次数。"""
    result = _evaluation(
        deterministic_passed=True, explanation_quality=False, warnings=("解释过简",)
    )
    assert result.passed is True
    assert result.rubric.explanation_quality.passed is False
    assert result.warnings == ("解释过简",)
    assert result.revision_count == 0


# ---------- 内容编解码与领域边界 ----------


def test_plan_and_evaluation_json_codecs_round_trip() -> None:
    """计划内容与 Evaluator 结果只在 schema 模块编解码；损坏内容大声失败。"""
    draft = _draft(_weighted_day(0), _weighted_day(3))
    draft_text = plan_draft_to_json(draft)
    assert set(json.loads(draft_text)) == set(PlanDraft.model_fields)
    assert plan_draft_from_json(draft_text) == draft
    # 读库路径：解码后的对象与 JSON 文本都得到同一草案。
    assert PlanDraft.model_validate(json.loads(draft_text)) == draft
    # JSON 里的数组形状（Planner 输出路径）与 Python 元组等价。
    assert PlanDraft.model_validate_json(draft_text) == draft

    result = _evaluation(deterministic_passed=True)
    result_text = evaluation_result_to_json(result)
    assert set(json.loads(result_text)) == set(EvaluationResult.model_fields)
    assert evaluation_result_from_json(result_text) == result
    assert EvaluationResult.model_validate(json.loads(result_text)) == result

    for corrupt in ("not json", "{}", "[]", '{"goal": "增肌"}'):
        with pytest.raises(InvalidPlanRow):
            plan_draft_from_json(corrupt)
        with pytest.raises(InvalidPlanRow):
            evaluation_result_from_json(corrupt)


def test_rejected_status_is_terminal_and_carries_no_timestamp_field() -> None:
    """rejected 是四态之一且不带时间字段（查询可见性断言移交 §6 Subtask 03 的 repo／service 测试）。"""
    assert "rejected" in PLAN_STATUSES
    row = {
        "id": 9,
        "version": 9,
        "status": "rejected",
        "source_plan_id": 3,
        "structured_content": plan_draft_to_json(_draft(_weighted_day(0))),
        "evaluator_result": evaluation_result_to_json(_evaluation(deterministic_passed=False)),
        "created_at": "2026-06-01T08:00:00+08:00",
        "confirmed_at": None,
        "archived_at": None,
    }
    plan = Plan.from_row(row)
    assert plan.status == "rejected"
    assert plan.confirmed_at is None and plan.archived_at is None
    # 读库后的解码内容可直接过统一 Schema（读取路径不把结构化内容当任意 Any）。
    assert PlanDraft.model_validate(plan.structured_content) == _draft(_weighted_day(0))
    assert (
        EvaluationResult.model_validate(plan.evaluator_result)
        == _evaluation(deterministic_passed=False)
    )

    for illegal_status in ("pending", "rejected_at", ""):
        with pytest.raises(InvalidPlanRow):
            Plan.from_row({**row, "status": illegal_status})

    # 终态语义无代码等价物（状态本身不表达「不可改回 draft」）：文档断言保留。
    summary = SUMMARY_PATH.read_text(encoding="utf-8")
    assert "`rejected` 记录收尾，它是终态：不可激活，也不得改回 draft" in summary


def test_domain_modules_import_no_agent_or_web_frameworks() -> None:
    """领域模块不得依赖 FastAPI／LangGraph／LangChain／OpenAI SDK（stage4.md §5.2）。"""
    offenders: list[str] = []
    for path in sorted((BACKEND_ROOT / "domain").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                roots = [node.module or ""]
            else:
                continue
            for root in roots:
                if root.split(".")[0] in FORBIDDEN_DOMAIN_IMPORTS:
                    offenders.append(f"{path.relative_to(BACKEND_ROOT)}:{node.lineno} {root}")
    assert offenders == []


# ---------- §6 Subtask 01 保留的文档契约（无代码等价物） ----------


def test_user_refusal_routes_to_archive_draft_not_rejected() -> None:
    """讨论总结 §3.3／§3.4 与 REFACTOR_PLAN §9.2：用户拒绝不是 rejected（阶段 4/5 边界）。"""
    summary = SUMMARY_PATH.read_text(encoding="utf-8")
    plan = REFACTOR_PLAN_PATH.read_text(encoding="utf-8")

    assert "`rejected` 只表示 Evaluator 二次阻断失败" in summary
    assert "用户拒绝仍在阶段 5 走 `archive_draft`" in summary
    assert "用户拒绝时将 draft 归档" in summary
    assert "用户拒绝仍走 `archive_draft`" in plan


def test_stage4_confirmed_decisions_are_present_in_the_stage_plan() -> None:
    """stage4.md §2.1／§3.6／§3.7／§3.8／§3.10／§4 的已确认决策必须保持原文可查。"""
    stage4 = STAGE4_PATH.read_text(encoding="utf-8")

    assert "任意时刻最多一个可确认 `draft`" in stage4  # 5A
    assert "最多交回 Planner 修订一次" in stage4  # 一次修订闭环
    assert "每个 Run 仍只允许一次修订" in stage4
    assert "10B-1" in stage4 and "任一目标组低于次数下限" in stage4  # 10B-1
    assert "12A-2" in stage4 and "解释质量为 warning" in stage4  # 12A-2
    assert "60 秒" in stage4 and "180 秒" in stage4 and "最多 5 次" in stage4  # 8B
    assert "业务 draft 先提交，再 interrupt" in stage4  # 11A
    assert "interrupt 载荷只携带 `draft_plan_id`" in stage4
