"""plans 确定性规则：目录匹配、禁用动作、负荷来源与 10B／10B-1 渐进回退
（stage4.md §3.1／§3.3／§3.4／§3.5；讨论总结 §7.3／§8／§9；REFACTOR_PLAN §6.5／§6.6）。

纯函数：不读库、不写库、不取「今天」、不调用模型，也不依赖 FastAPI／LangGraph／模型 SDK
（``domain/__init__`` 约束）。全部事实由调用方注入，本层不猜、不补默认值：

- 目录动作（``domain.actions.schema.Exercise``）：动作是否存在、``recommendable``、记录口径与
  ``min_load_increment_kg``；
- 有效工作组（``domain.stats.schema.ValidWorkSet``）：其过滤口径（热身／assisted／不完整组不算）
  的唯一出处是 ``domain.stats.repo`` 的共享 SQL，本层不重写；
- 「关联当前 active 计划日程」的训练身份集合（``workout_sessions.plan_session_id``）：服务层把该
  关联与有效工作组按 ``workout_session_id`` 有界连接后注入；``plan_session_id IS NULL`` 的额外训练
  不在集合里，因此既不计入渐进历史，也不打断连续性。

两类输出：

- :func:`validate_plan_draft` 返回 :class:`~domain.plans.schema.RuleFailure` 元组（Evaluator 的确定性层
  直接消费），不抛异常——失败项要全量回传，供最多一次修订使用。规则标识是封闭的六项：
  ``weekly_frequency_mismatch``／``unknown_exercise``／``exercise_not_recommendable``／
  ``record_type_mismatch``／``forbidden_exercise``／``load_source_mismatch``。
- :func:`validate_plan_adjustment` 与它同形返回、共用同一封闭六项：结构与目录检查完全一致，只把
  「具体负荷必须等于最近一次有效工作组」换成「具体负荷必须等于按当前 active 的目标处方与关联日程
  训练算出的渐进决策」（stage5.md §3.4；讨论总结 §7.3 的起始负荷口径仍是 active 没有目标处方的
  动作的唯一来源）。
- :func:`resolve_starting_load`／:func:`resolve_progression` 是纯决策函数（Stage 4 只提供规则，
  调整计划的 Graph 接线留 Stage 5）；只有「输入不满足规则前提」才抛 :class:`InvalidPlanRule`。

边界：自重与计时动作本阶段不发明次数／时长递增阈值——本模块对它们只做处方结构与记录口径校验
（见 :func:`validate_plan_draft`），不套用重量加重或回退规则。
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from domain.actions.schema import Exercise, RecordType
from domain.plans.schema import (
    KnownLoad,
    NeedsCalibration,
    PlanDraft,
    PlannedExercise,
    RuleFailure,
    WeightedRepsPrescription,
)
from domain.profile.schema import Profile
from domain.records.rules import WEIGHT_KG_DECIMALS
from domain.stats.schema import ValidWorkSet

#: 处方类型与目录记录口径的一一对应（stage4.md §3.1：判别联合必须与 ``record_type`` 一致）。
RECORD_TYPE_BY_PRESCRIPTION: dict[str, RecordType] = {
    "weighted_reps": "reps_weight",
    "bodyweight_reps": "reps_bodyweight",
    "timed": "time",
}


class InvalidPlanRule(ValueError):
    """确定性规则的输入不满足前提（如加重单位非正）：不是计划内容失败，是调用方编程错误。"""


@dataclass(frozen=True, slots=True)
class ProgressionDecision:
    """渐进／回退的下一步：``increase``／``regress`` 携带具体重量，``needs_calibration`` 为 None。"""

    action: Literal["increase", "keep", "regress", "needs_calibration"]
    load_kg: float | None


def known_forbidden_exercise_ids(profile: Profile) -> tuple[str, ...]:
    """画像明确给出的禁用动作 ID：只取 ``known`` 值，``unknown``／``denied`` 都不补造 ID。

    已知伤病文本不参与推导（讨论总结 §8.3：系统不得根据伤病名称自行推导新的禁用动作）。
    """
    fact = profile.forbidden_exercise_ids
    return fact.value if fact.is_known and fact.value is not None else ()


def filter_forbidden_exercises(
    candidates: Sequence[Exercise], *, forbidden_exercise_ids: Collection[str]
) -> tuple[Exercise, ...]:
    """Planner 前的确定性硬过滤：从候选动作中删除禁用 ID，保持目录顺序（顺序即确定性）。"""
    forbidden = set(forbidden_exercise_ids)
    return tuple(exercise for exercise in candidates if exercise.id not in forbidden)


def resolve_starting_load(
    work_sets: Sequence[ValidWorkSet], *, exercise_id: str
) -> KnownLoad | NeedsCalibration:
    """起始负荷：该动作最近一次有效工作组（业务日期、训练身份、组序号排序），不使用 PB。

    没有有效工作组可用作负荷来源时返回 :class:`~domain.plans.schema.NeedsCalibration`，不猜重量、
    也不从 PB 反推日常训练重量（讨论总结 §7.3、stage4.md §3.4）。
    """
    candidates = [
        work_set
        for work_set in work_sets
        if work_set.exercise_id == exercise_id and work_set.weight_kg is not None
    ]
    if not candidates:
        return NeedsCalibration(status="needs_calibration")
    latest = max(
        candidates,
        key=lambda work_set: (
            work_set.performed_on,
            work_set.workout_session_id,
            work_set.set_no,
        ),
    )
    return KnownLoad(
        status="known",
        weight_kg=latest.weight_kg,
        source_workout_session_id=latest.workout_session_id,
        source_set_no=latest.set_no,
    )


@dataclass(frozen=True, slots=True)
class _TrainingAssessment:
    """一次关联计划训练的目标组判定（10B／10B-1 的原子结论）。"""

    load_kg: float | None
    completed: bool
    failed: bool
    at_reps_max: bool


def _assess_training(
    work_sets: Sequence[ValidWorkSet], *, target_sets: int, reps_min: int, reps_max: int
) -> _TrainingAssessment:
    """按计划要求的目标组判定一次训练（同一次训练只取计划要求数量的目标 work 组）。

    - ``failed``：目标组数不足，或任一目标组次数低于下限（10B-1 的「未达标」）；次数缺失同样算未达标。
    - ``completed``：目标组齐、全部在同一负荷、且每组次数不低于下限（规则 6：负荷不一致不算同一负荷达标）。
    - ``at_reps_max``：``completed`` 且每个目标组都达到次数上限。
    - 额外的 work 组不改变判定（只按 ``set_no`` 取前 ``target_sets`` 组）。
    """
    ordered = sorted(work_sets, key=lambda work_set: work_set.set_no)
    target = tuple(ordered[:target_sets])
    reps = [work_set.reps for work_set in target]
    loads = {work_set.weight_kg for work_set in target}
    sets_complete = len(target) == target_sets and all(
        reps_value is not None for reps_value in reps
    )
    failed = (not sets_complete) or any(
        reps_value is not None and reps_value < reps_min for reps_value in reps
    )
    same_load = sets_complete and len(loads) == 1 and None not in loads
    completed = same_load and not failed
    return _TrainingAssessment(
        load_kg=next(iter(loads)) if same_load else None,
        completed=completed,
        failed=failed,
        at_reps_max=completed and all(reps_value >= reps_max for reps_value in reps),
    )


def resolve_progression(
    work_sets: Sequence[ValidWorkSet],
    *,
    linked_workout_session_ids: Collection[int],
    target_sets: int,
    reps_min: int,
    reps_max: int,
    target_load_kg: float,
    increment_kg: float,
) -> ProgressionDecision:
    """10B／10B-1：按当前 active 计划关联的负重训练历史给出下一步负荷。

    - 最近两次关联计划训练都在当前目标负荷上完整完成全部目标组、且每组都达到次数上限：只加一次
      目录 ``min_load_increment_kg``；
    - 最近两次关联计划训练都未达标（组数不足或低于次数下限）：回退到关联历史中最近一次完整完成的
      负荷；没有可回退的完整完成负荷时变为待校准；
    - 其余情况（不足两次关联训练、只有一次达标、达标但未到上限、目标组负荷不一致）：保持目标负荷。

    ``work_sets`` 只包含有效工作组（热身／assisted／不完整组上游已排除）；``linked_workout_session_ids``
    之外的训练是额外训练，既不计入也不打断连续性。
    """
    if target_sets < 1 or reps_min < 1 or reps_min > reps_max or increment_kg <= 0:
        raise InvalidPlanRule(
            "渐进规则输入不满足前提："
            f"target_sets={target_sets} reps={reps_min}–{reps_max} increment={increment_kg}"
        )
    linked_ids = set(linked_workout_session_ids)
    by_training: dict[int, list[ValidWorkSet]] = {}
    for work_set in work_sets:
        if work_set.workout_session_id in linked_ids:
            by_training.setdefault(work_set.workout_session_id, []).append(work_set)
    linked = [
        _assess_training(
            by_training[workout_session_id],
            target_sets=target_sets,
            reps_min=reps_min,
            reps_max=reps_max,
        )
        for workout_session_id in sorted(
            by_training,
            key=lambda session_id: (
                by_training[session_id][0].performed_on,
                session_id,
            ),
        )
    ]
    if len(linked) < 2:
        return ProgressionDecision("keep", target_load_kg)
    recent_two = linked[-2:]
    if all(
        assessment.at_reps_max and assessment.load_kg == target_load_kg
        for assessment in recent_two
    ):
        return ProgressionDecision(
            "increase", round(target_load_kg + increment_kg, WEIGHT_KG_DECIMALS)
        )
    if all(assessment.failed for assessment in recent_two):
        fallback = next(
            (
                assessment
                for assessment in reversed(linked)
                if assessment.completed and assessment.load_kg is not None
            ),
            None,
        )
        if fallback is None:
            return ProgressionDecision("needs_calibration", None)
        return ProgressionDecision("regress", fallback.load_kg)
    return ProgressionDecision("keep", target_load_kg)


@dataclass(frozen=True, slots=True)
class ActiveLoadTarget:
    """当前 active 计划里一个动作的目标处方：渐进决策的全部输入面（无具体负荷的动作不产生它）。"""

    sets: int
    reps_min: int
    reps_max: int
    target_load_kg: float


def _weekly_frequency_failures(
    draft: PlanDraft, *, profile_weekly_frequency: int
) -> list[RuleFailure]:
    """每周训练次数必须复用画像的 ``known`` 值。"""
    if draft.weekly_frequency == profile_weekly_frequency:
        return []
    return [
        RuleFailure(
            code="weekly_frequency_mismatch",
            message=(
                f"计划每周训练次数 {draft.weekly_frequency} 与画像 "
                f"{profile_weekly_frequency} 不一致"
            ),
        )
    ]


def _prescription_failures(
    planned: PlannedExercise,
    *,
    exercises: Mapping[str, Exercise],
    forbidden: Collection[str],
) -> list[RuleFailure]:
    """一个候选动作的目录检查：动作存在、``recommendable``、记录口径一致、未被画像禁用。

    负荷来源检查由调用方按生成计划或调整计划的口径分别追加（本函数不决定负荷规则）。
    """
    exercise = exercises.get(planned.exercise_id)
    if exercise is None:
        return [
            RuleFailure(
                code="unknown_exercise",
                message=f"动作身份不在目录内：{planned.exercise_id}",
                exercise_id=planned.exercise_id,
            )
        ]
    failures: list[RuleFailure] = []
    if not exercise.recommendable:
        failures.append(
            RuleFailure(
                code="exercise_not_recommendable",
                message=f"动作不可用于计划（recommendable=0）：{exercise.id}",
                exercise_id=exercise.id,
            )
        )
    expected_record_type = RECORD_TYPE_BY_PRESCRIPTION[planned.prescription.type]
    if expected_record_type != exercise.record_type:
        failures.append(
            RuleFailure(
                code="record_type_mismatch",
                message=(
                    f"处方类型 {planned.prescription.type!r} 与目录动作 {exercise.id!r} 的"
                    f"记录口径 {exercise.record_type!r} 不一致"
                ),
                exercise_id=exercise.id,
            )
        )
    if exercise.id in forbidden:
        failures.append(
            RuleFailure(
                code="forbidden_exercise",
                message=f"计划含画像明确禁用的动作：{exercise.id}",
                exercise_id=exercise.id,
            )
        )
    return failures


def validate_plan_draft(
    draft: PlanDraft,
    *,
    exercises: Mapping[str, Exercise],
    profile_weekly_frequency: int,
    forbidden_exercise_ids: Collection[str] = (),
    work_sets: Sequence[ValidWorkSet] = (),
) -> tuple[RuleFailure, ...]:
    """确定性层：Schema 之外的全部计划检查，按训练日／动作顺序全量返回失败项。

    - 计划每周训练次数必须复用画像的 ``known`` 值；
    - 动作必须在目录内、``recommendable``，且处方类型与目录记录口径一致；
    - 禁用动作（画像 ``known`` 的稳定 ID）出现在计划里即失败；
    - 外加负重处方的具体负荷必须精确等于该动作最近一次有效工作组（无历史时只能待校准）。

    次数／时长区间与字段互斥已由 :class:`~domain.plans.schema.PlanDraft` 的 Schema 保证，本层不重复。
    """
    failures = _weekly_frequency_failures(
        draft, profile_weekly_frequency=profile_weekly_frequency
    )
    forbidden = set(forbidden_exercise_ids)
    for day in draft.training_days:
        for planned in day.exercises:
            failures.extend(
                _prescription_failures(planned, exercises=exercises, forbidden=forbidden)
            )
            exercise = exercises.get(planned.exercise_id)
            if exercise is None:
                continue
            if isinstance(planned.prescription, WeightedRepsPrescription) and isinstance(
                planned.prescription.load, KnownLoad
            ):
                failures.extend(
                    _load_source_failures(
                        planned.prescription.load, exercise_id=exercise.id, work_sets=work_sets
                    )
                )
    return tuple(failures)


def validate_plan_adjustment(
    draft: PlanDraft,
    *,
    active_draft: PlanDraft,
    linked_workout_session_ids: Collection[int],
    exercises: Mapping[str, Exercise],
    profile_weekly_frequency: int,
    forbidden_exercise_ids: Collection[str] = (),
    work_sets: Sequence[ValidWorkSet] = (),
) -> tuple[RuleFailure, ...]:
    """调整计划的确定性层：结构与目录检查同生成计划，负荷按当前 active 的渐进决策判断。

    对每个给出具体负荷的负重处方：active 里有同动作的目标处方（目标组数／次数区间／目标负荷）时，
    按该目标与 ``linked_workout_session_ids`` 里的关联日程训练调用 :func:`resolve_progression`，
    候选负荷必须等于决策给出的重量；决策为 ``needs_calibration`` 时候选不得给出具体重量。active 没有
    该动作的目标处方（调整新增的动作）时没有渐进决策可判，按讨论总结 §7.3 的起始负荷口径回退到
    生成计划的最近工作组规则。
    """
    failures = _weekly_frequency_failures(
        draft, profile_weekly_frequency=profile_weekly_frequency
    )
    forbidden = set(forbidden_exercise_ids)
    targets = _active_load_targets(active_draft)
    for day in draft.training_days:
        for planned in day.exercises:
            failures.extend(
                _prescription_failures(planned, exercises=exercises, forbidden=forbidden)
            )
            exercise = exercises.get(planned.exercise_id)
            if exercise is None or not isinstance(
                planned.prescription, WeightedRepsPrescription
            ):
                continue
            load = planned.prescription.load
            if not isinstance(load, KnownLoad):
                continue
            target = targets.get(planned.exercise_id)
            if target is None or exercise.min_load_increment_kg is None:
                # 没有 active 目标处方（或目录动作不是外加负重口径、已另有 record_type_mismatch）：
                # 没有渐进决策可判，按起始负荷口径检查来源。
                failures.extend(
                    _load_source_failures(load, exercise_id=exercise.id, work_sets=work_sets)
                )
                continue
            decision = resolve_progression(
                work_sets,
                linked_workout_session_ids=linked_workout_session_ids,
                target_sets=target.sets,
                reps_min=target.reps_min,
                reps_max=target.reps_max,
                target_load_kg=target.target_load_kg,
                increment_kg=exercise.min_load_increment_kg,
            )
            failures.extend(
                _progression_failures(
                    load,
                    decision=decision,
                    exercise_id=exercise.id,
                    target_load_kg=target.target_load_kg,
                )
            )
    return tuple(failures)


def _active_load_targets(active_draft: PlanDraft) -> dict[str, ActiveLoadTarget]:
    """当前 active 里每个动作的目标处方；同一动作重复出现时取第一处（顺序稳定，不猜合并口径）。"""
    targets: dict[str, ActiveLoadTarget] = {}
    for day in active_draft.training_days:
        for planned in day.exercises:
            prescription = planned.prescription
            if not isinstance(prescription, WeightedRepsPrescription) or not isinstance(
                prescription.load, KnownLoad
            ):
                continue
            targets.setdefault(
                planned.exercise_id,
                ActiveLoadTarget(
                    sets=planned.sets,
                    reps_min=prescription.reps_min,
                    reps_max=prescription.reps_max,
                    target_load_kg=prescription.load.weight_kg,
                ),
            )
    return targets


def _progression_failures(
    load: KnownLoad,
    *,
    decision: ProgressionDecision,
    exercise_id: str,
    target_load_kg: float,
) -> list[RuleFailure]:
    """候选负荷必须等于渐进决策：``needs_calibration`` 时不得给出具体重量（复用负荷来源规则标识）。"""
    if decision.action == "needs_calibration":
        return [
            RuleFailure(
                code="load_source_mismatch",
                message=(
                    f"渐进决策为 needs_calibration：调整候选不得给出具体重量（active 目标 "
                    f"{target_load_kg}kg）：{exercise_id}"
                ),
                exercise_id=exercise_id,
            )
        ]
    if load.weight_kg == decision.load_kg:
        return []
    return [
        RuleFailure(
            code="load_source_mismatch",
            message=(
                f"调整候选负荷必须等于渐进决策：期望 {decision.action} 到 {decision.load_kg}kg"
                f"（active 目标 {target_load_kg}kg）：{exercise_id}"
            ),
            exercise_id=exercise_id,
        )
    ]


def _load_source_failures(
    load: KnownLoad, *, exercise_id: str, work_sets: Sequence[ValidWorkSet]
) -> list[RuleFailure]:
    """具体负荷的来源必须是该动作最近一次有效工作组；PB 更大也不能成为来源。"""
    expected = resolve_starting_load(work_sets, exercise_id=exercise_id)
    if expected == load:
        return []
    if isinstance(expected, KnownLoad):
        message = (
            f"负荷来源必须是该动作最近一次有效工作组：期望训练 {expected.source_workout_session_id} "
            f"第 {expected.source_set_no} 组 {expected.weight_kg}kg"
        )
    else:
        message = f"该动作没有有效工作组历史，计划不得给出具体重量（只能待校准）：{exercise_id}"
    return [RuleFailure(code="load_source_mismatch", message=message, exercise_id=exercise_id)]
