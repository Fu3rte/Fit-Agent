"""plan 用例编排：PPL 首版模板生成（stage3.md §5 S3-03、§4.4 D9；正本 architecture/04 4.1–4.5）。

生成输入 = 正式档案（可含拟议长期补丁）+ 可推荐目录候选 + 显式候选日期与循环槽；输出 = D9
payload 或 fail-closed 阻断原因。本模块纯确定性、不碰 IO：正式档案读取归
``domain.profile.service``，候选来自 ``domain.actions.repo.list_recommendable``（D2：只查
``active=1 AND recommendable=1``）；保存草稿归 S3-04、确认事务归 S3-06，本模块不写库、
不推版本、不生成任何正式事实。

fail-closed 口径（stage3.md §4.4「缺档案/红旗 fail-closed」）：

- 未建档（``profile is None``）或档案未达首次建档完整条件（八项未全部明确回答）→ 不给处方；
- 身体情况命中六类红旗（正式档案或拟议补丁任一来源）→ 不给处方，只给线下专业评估提示
  （02 2.3：另一来源的「明确无」不覆盖命中；红旗不能因补丁而自动解除）；
- 按器械／限制过滤后任一训练日没有可用动作、循环训练日折合每周次数超过档案每周频率、
  或预计时长超过单次可用时长 → 不给处方；
- 生成结果一律再过 ``rules.validate_payload`` 自检，不自检通过不返回。

清单外身体情况原文只进入澄清、不判红旗也不判安全（02 2.3；stage1 2026-09-09 已拍）：
本阶段没有对话澄清入口，生成不据此阻断，也不因此输出任何安全许可结论。

无可信训练历史：所有外加负重动作一律 ``needs_calibration``（D3），不猜重量；RIR 只作展示
参考、不作为校准硬性指标。PPL 是可表示的首版模板，不是唯一结构，也不是模板插件系统。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import Literal, cast

from domain.actions.schema import Exercise
from domain.plan.rules import (
    ArrangementAdjustment,
    InvalidPlanPayload,
    needs_calibration_for,
    plan_item_revision,
    prescription_record_type_for,
    project_sessions,
    validate_calendar_cycle,
    validate_payload,
)
from domain.plan.schema import (
    CalendarCycle,
    CycleSlot,
    DisplaySnapshot,
    IntRange,
    Load,
    PlanExerciseItem,
    PlanPayload,
    PlanWorkout,
    PrescriptionRecordType,
    Progression,
    ProgressionMethod,
    RepsPrescription,
    RestCycleSlot,
    WorkoutCycleSlot,
)
from domain.profile.rules import apply_patch, missing_first_time_fields
from domain.profile.safety import (
    MESSAGE_RED_FLAG_SOURCE,
    RED_FLAG_BLOCK_ADVICE,
    RedFlagCheck,
    RedFlagFinding,
    RestrictionHit,
    SafetyCheckResult,
    evaluate_safety,
)
from domain.profile.schema import Profile, ProfilePatch

PPL_TEMPLATE_KEY = "ppl"

# D9 给出的 PPL 每周三练示例循环：长度 7、训练日 push/pull/legs 各一天，其余休息。
# 循环长度即实际长度；非 7 日循环（如练三休一）由调用方给出显式 slots，本模块不建
# 覆盖任意频率的通用排程算法（stage3.md §4.4）。
PPL_CALENDAR_SLOTS: tuple[CycleSlot, ...] = (
    WorkoutCycleSlot("push"),
    RestCycleSlot(),
    WorkoutCycleSlot("pull"),
    RestCycleSlot(),
    WorkoutCycleSlot("legs"),
    RestCycleSlot(),
    RestCycleSlot(),
)

# 渐进规则文本（D9：method + 明确 rule，custom 也必须有 rule；本模板只产出前两种）。
PROGRESSION_RULES: dict[ProgressionMethod, str] = {
    "double_progression": (
        "在次数区间内稳定完成全部工作组后先加次数；达到区间上限后按器械允许的最小增量"
        "加重，并回到次数下限"
    ),
    "repetition_progression": (
        "先增加次数，达到次数区间上限后按最小增量加重（自重动作改增加次数或难度），"
        "并回到次数下限"
    ),
    "duration_progression": (
        "稳定达到时长区间上限后，按最小档位增加负荷或难度，并回到时长下限"
    ),
}

# 目录器械变式 → 档案器械短语（沿用 stage1 建档短语与 stage2 mock 词表，不引入新器械名）。
# 档案器械里出现任一别名即视为该变式可用；未知变式与空档案器械一律视为不可用（fail-closed）。
EQUIPMENT_ALIASES: dict[str, tuple[str, ...]] = {
    "barbell": ("杠铃",),
    "dumbbell": ("哑铃", "哑铃凳"),
    "cable": ("绳索", "龙门架"),
    "bodyweight": ("引体架", "单杠", "自重", "徒手"),
    "leverage_machine": ("器械", "固定器械"),
    "sled_machine": ("器械", "固定器械"),
}

# 预计时长口径（确定性，不做个体建模）：热身 8 分钟 + 每组 3 分钟（沿用 stage2 mock）。
WARMUP_MINUTES = 8
MINUTES_PER_SET = 3

GenerationBlockCode = Literal[
    "no_profile",
    "incomplete_profile",
    "red_flag",
    "unschedulable",
    "invalid_payload",
]


@dataclass(frozen=True, slots=True)
class PlanGenerationBlocked:
    """fail-closed 阻断：不给任何处方，只说明原因（红旗附线下专业评估提示）。"""

    code: GenerationBlockCode
    reason: str
    missing_fields: tuple[str, ...] = ()
    red_flags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlanGenerationReady:
    """生成成功：D9 payload 已过结构自检；日程投影由 ``rules.project_sessions`` 按行字段区间完成。"""

    payload: PlanPayload


PlanGeneration = PlanGenerationReady | PlanGenerationBlocked


@dataclass(frozen=True, slots=True)
class _TemplateExercise:
    """模板动作：只给目录身份与处方，展示副本在生成时从目录冻结。"""

    exercise_id: str
    prescription: RepsPrescription
    progression_method: ProgressionMethod


@dataclass(frozen=True, slots=True)
class _TemplateWorkout:
    workout_key: str
    name: str
    exercises: tuple[_TemplateExercise, ...]


def _reps(
    exercise_id: str, work_sets: int, reps: tuple[int, int], method: ProgressionMethod
) -> _TemplateExercise:
    return _TemplateExercise(
        exercise_id=exercise_id,
        prescription=RepsPrescription(
            work_sets=work_sets,
            reps_range=IntRange(reps[0], reps[1]),
            target_rir=IntRange(1, 3),
        ),
        progression_method=method,
    )


# PPL 首版模板（stage2 mock F2-02 已校准的动作选择，逐项来自已拍 24 项目录）：
# 同一训练日内不重复动作身份；跨训练日复用（悬垂举腿在拉日与腿日各一次）不判冲突。
PPL_TEMPLATE: tuple[_TemplateWorkout, ...] = (
    _TemplateWorkout(
        workout_key="push",
        name="推日",
        exercises=(
            _reps("barbell-bench-press", 4, (6, 8), "double_progression"),
            _reps("seated-dumbbell-shoulder-press", 3, (8, 12), "double_progression"),
            _reps("parallel-bar-dip", 3, (8, 12), "repetition_progression"),
            _reps("cable-pushdown", 3, (10, 15), "repetition_progression"),
        ),
    ),
    _TemplateWorkout(
        workout_key="pull",
        name="拉日",
        exercises=(
            _reps("pull-up", 3, (6, 10), "repetition_progression"),
            _reps("barbell-bent-over-row", 4, (8, 10), "double_progression"),
            _reps("dumbbell-biceps-curl", 3, (10, 12), "repetition_progression"),
            _reps("hanging-leg-raise", 3, (10, 15), "repetition_progression"),
        ),
    ),
    _TemplateWorkout(
        workout_key="legs",
        name="腿日",
        exercises=(
            _reps("barbell-back-squat", 4, (6, 8), "double_progression"),
            _reps("barbell-romanian-deadlift", 3, (8, 10), "double_progression"),
            _reps("bulgarian-split-squat", 3, (8, 12), "repetition_progression"),
            _reps("hanging-leg-raise", 3, (10, 15), "repetition_progression"),
        ),
    ),
)


def equipment_available(equipment_variant: str, equipment: Sequence[str]) -> bool:
    """档案器械是否覆盖目录器械变式；未知变式一律不可用（fail-closed）。"""
    aliases = EQUIPMENT_ALIASES.get(equipment_variant)
    return aliases is not None and any(alias in equipment for alias in aliases)


def estimated_minutes(exercises: Sequence[PlanExerciseItem]) -> int:
    """训练日预计时长：热身 8 分钟 + 每组 3 分钟（确定性口径，不逐人建模）。"""
    work_sets = sum(exercise.prescription.work_sets for exercise in exercises)
    return WARMUP_MINUTES + MINUTES_PER_SET * work_sets


def weekly_frequency_violation(profile: Profile, cycle: CalendarCycle) -> str | None:
    """循环折合每周训练次数是否超过档案每周频率；超限返回原因文本，否则 None。

    循环长度即实际循环长度（D9），不一定是 7 天：必须把循环内训练日折合成每周次数再比较
    （练三休一：每 4 天 3 练 = 每周 5.25 次），整数交叉相乘，不用浮点。档案频率不是整数
    （含未收集）一律拒绝，不猜默认值。生成与确认共用本口径。
    """
    frequency = _known_int(profile, "weekly_frequency")
    if not cycle.slots:
        raise InvalidPlanPayload("calendar_cycle.slots 为空，无法折算每周频率")
    training_days = sum(1 for slot in cycle.slots if isinstance(slot, WorkoutCycleSlot))
    cycle_length = len(cycle.slots)
    if training_days * 7 > frequency * cycle_length:
        return (
            f"循环 {cycle_length} 天含 {training_days} 个训练日，折合每周 "
            f"{training_days * 7}/{cycle_length} 次，超过档案每周频率 {frequency} 次"
        )
    return None


def duration_violation(profile: Profile, *, minutes: int, label: str) -> str | None:
    """单个训练日的预计时长是否超过档案单次可用时长；超限返回原因文本，否则 None。"""
    limit = _known_int(profile, "session_duration_minutes")
    if minutes > limit:
        return f"{label}预计 {minutes} 分钟，超过档案单次可用时长 {limit} 分钟"
    return None


def plan_limit_violations(profile: Profile, payload: PlanPayload) -> tuple[str, ...]:
    """按给定（补丁后）档案条件复查计划的频率与时长上限：返回原因文本（空 = 通过）。

    确认事务的「频率/时长」复查项（stage3.md §4.2 步骤 2）：受限组合的档案补丁可以改
    ``weekly_frequency`` 与 ``session_duration_minutes``，因此必须按**应用补丁后的条件**
    复查已成型的 payload，不能只按生成时的旧上限。两条单规则与生成侧共用，口径只有一处。
    """
    reasons: list[str] = []
    frequency = weekly_frequency_violation(profile, payload.calendar_cycle)
    if frequency is not None:
        reasons.append(frequency)
    for workout in payload.plan_workouts:
        duration = duration_violation(
            profile, minutes=workout.estimated_minutes, label=workout.name
        )
        if duration is not None:
            reasons.append(duration)
    return tuple(reasons)


@dataclass(frozen=True, slots=True)
class PlanSafetyRecheck:
    """整份计划的确定性安全复核结果（04 4.5；S3-07）。

    ``safety`` 是既有本地安全判定（``domain.profile.safety``）对计划内动作的复核结果；
    ``unknown_exercise_ids`` 是计划引用了但目录内已读不到的动作身份——无法映射动作模式就
    无法按最新限制复核，按 fail-closed 阻断，不当作「无冲突」。

    限制冲突与红旗**各自独立阻断**：任一为真即 :attr:`is_blocked`，两者同时存在时
    :attr:`blocking_reasons` 两条都保留，不返回「第一个原因」，也不互相掩盖。
    """

    safety: SafetyCheckResult
    unknown_exercise_ids: tuple[str, ...] = ()
    #: C 层文本兜底命中的当前消息词（2026-09-12 拍板）：非空即阻断，不并入目录／档案判定。
    message_red_flags: tuple[str, ...] = ()

    @property
    def restriction_conflicts(self) -> tuple[RestrictionHit, ...]:
        """计划内动作被最新限制覆盖的命中项（具体动作或动作模式）。"""
        return self.safety.restrictions.hits

    @property
    def red_flags(self) -> RedFlagCheck:
        """最新条件的红旗评估（档案来源，独立于限制冲突）＋ C 层消息兜底命中。

        C 层命中（2026-09-12 拍板）没有档案来源，以 ``message`` 来源并入 ``confirmed``：
        只补强红旗结论，不改写任何档案事实，也不进 ``unknown_sources``。
        """
        if not self.message_red_flags:
            return self.safety.red_flags
        return replace(
            self.safety.red_flags,
            confirmed=self.safety.red_flags.confirmed
            + tuple(
                RedFlagFinding(source=MESSAGE_RED_FLAG_SOURCE, label=term)
                for term in self.message_red_flags
            ),
        )

    @property
    def restrictions_unknown(self) -> bool:
        """正式限制未收集：不得读成「无冲突」，只计入 :attr:`clarification_reasons`。"""
        return self.safety.restrictions.is_unknown

    @property
    def is_blocked(self) -> bool:
        """整份计划不得作为可执行训练建议：限制冲突、红旗、目录引用或消息兜底任一为真。"""
        return (
            bool(self.restriction_conflicts)
            or self.red_flags.is_blocked
            or bool(self.unknown_exercise_ids)
            or bool(self.message_red_flags)
        )

    @property
    def blocking_reasons(self) -> tuple[str, ...]:
        """阻断原因：限制冲突、红旗、目录引用读不到、消息兜底各自独立列出（空 = 未阻断）。"""
        reasons = [
            f"动作 {hit.standard_name}（{hit.exercise_id}）命中最新限制："
            "整份计划不作为可执行训练建议"
            for hit in self.restriction_conflicts
        ]
        if self.red_flags.is_blocked:
            labels = "、".join(
                dict.fromkeys(finding.label for finding in self.red_flags.confirmed)
            )
            reasons.append(f"{RED_FLAG_BLOCK_ADVICE}（命中：{labels}）")
        if self.unknown_exercise_ids:
            reasons.append(
                "计划引用的动作身份在目录内已读不到，无法按最新限制复核："
                f"{list(self.unknown_exercise_ids)}"
            )
        if self.message_red_flags:
            reasons.append(
                "当前用户消息命中已拍红旗兜底词（"
                + "、".join(self.message_red_flags)
                + "）：本 Run 不给可执行处方"
            )
        return tuple(reasons)

    @property
    def clarification_reasons(self) -> tuple[str, ...]:
        """未收集限制／未收集红旗／清单外症状的需澄清项：不等于无冲突，也不阻断（02 2.3）。"""
        return self.safety.clarification_reasons


def evaluate_plan_safety(
    profile: Profile,
    payload: PlanPayload,
    *,
    catalog: Mapping[str, Exercise],
    session_exercise_ids: Sequence[str] = (),
) -> PlanSafetyRecheck:
    """按**最新条件**复核整份计划（04 4.5；S3-07）：不只查当天训练日的动作。

    ``catalog`` 是计划引用动作的目录投影（``{exercise.id: exercise}``，含停用动作）：限制
    命中按动作模式集合与限制求交，必须拿到目录行；读不到的身份记入
    ``unknown_exercise_ids`` 并阻断，不静默跳过（跳过会把「无法复核」读成「无冲突」）。

    ``session_exercise_ids`` 是「当次条件」——某条已接受安排（04 4.3）在执行时实际要做的
    动作身份：未来安排使用时仍须按最新限制与红旗复核，故与整份计划一并评估（去重，同一
    身份只算一次）。调用方负责把当次条件的身份解析进来（S3-14 指导端点）。

    纯确定性、只读：不写正式事实、不改计划、不取消日程，也不替代生成侧（S3-03）与确认
    事务（S3-06）写入前的复查——本函数服务于「使用当前计划给出指导」这一时点。
    """
    validate_payload(payload)
    referenced = tuple(
        dict.fromkeys(
            [
                item.exercise_id
                for workout in payload.plan_workouts
                for item in workout.exercises
            ]
            + list(session_exercise_ids)
        )
    )
    actions = tuple(catalog[item_id] for item_id in referenced if item_id in catalog)
    return PlanSafetyRecheck(
        safety=evaluate_safety(profile, actions),
        unknown_exercise_ids=tuple(
            item_id for item_id in referenced if item_id not in catalog
        ),
    )


def generate_ppl_plan(
    profile: Profile | None,
    candidates: Sequence[Exercise],
    *,
    starts_on: date,
    review_on: date,
    anchor_date: date,
    cycle_slots: Sequence[CycleSlot] | None = None,
    patch: ProfilePatch | None = None,
) -> PlanGeneration:
    """从正式档案与可推荐候选生成 PPL 计划 payload，或在任一 fail-closed 条件下阻断。

    - ``candidates``：目录候选（D2 生成侧只接受 ``active`` 且 ``recommendable`` 的动作；
      传入清单不是信任边界，生成内逐项复查）。
    - ``starts_on``／``review_on``：计划的生效区间 ``[starts_on, review_on)``（行字段，不进
      payload）；``anchor_date``／``cycle_slots``：显式候选循环槽，缺省用 PPL 每周三练示例。
      本函数不建通用排程算法，只在给定区间内校验「至少有一个训练日」。
    - ``patch``：受限组合的拟议长期档案补丁（01 1.5）：器械／限制按补丁后条件过滤，红旗按
      正式与拟议两来源独立评估；补丁不改输入档案、不写正式事实。
    """
    if profile is None:
        return PlanGenerationBlocked(
            code="no_profile", reason="尚未建立正式档案：不生成计划处方"
        )
    missing = missing_first_time_fields(profile)
    if missing:
        return PlanGenerationBlocked(
            code="incomplete_profile",
            reason=f"档案缺少明确回答的事实，不给处方：{list(missing)}",
            missing_fields=missing,
        )
    candidate_actions = tuple(candidates)
    safety = evaluate_safety(profile, candidate_actions, patch=patch)
    if safety.red_flags.is_blocked:
        labels = tuple(finding.label for finding in safety.red_flags.confirmed)
        return PlanGenerationBlocked(
            code="red_flag",
            reason=f"{RED_FLAG_BLOCK_ADVICE}（命中：{'、'.join(labels)}）",
            red_flags=labels,
        )
    effective = apply_patch(profile, patch) if patch is not None else profile

    workout_keys = [workout.workout_key for workout in PPL_TEMPLATE]
    cycle = CalendarCycle(
        anchor_date=anchor_date,
        slots=tuple(cycle_slots) if cycle_slots is not None else PPL_CALENDAR_SLOTS,
    )
    try:
        validate_calendar_cycle(cycle, workout_keys)
    except InvalidPlanPayload as exc:
        return PlanGenerationBlocked(code="unschedulable", reason=str(exc))
    if type(starts_on) is not date or type(review_on) is not date:
        return PlanGenerationBlocked(
            code="unschedulable", reason="生效范围必须是 start_on 与 review_on 两个日期"
        )
    if starts_on >= review_on:
        return PlanGenerationBlocked(
            code="unschedulable",
            reason=(
                "生效范围须满足开始日期早于复核日期（[starts_on, review_on)）："
                f"{starts_on.isoformat()} / {review_on.isoformat()}"
            ),
        )

    frequency_violation = weekly_frequency_violation(effective, cycle)
    if frequency_violation is not None:
        return PlanGenerationBlocked(code="unschedulable", reason=frequency_violation)

    equipment = _profile_equipment(effective)
    restricted_ids = {hit.exercise_id for hit in safety.restrictions.hits}
    catalog = {exercise.id: exercise for exercise in candidate_actions}
    workouts: list[PlanWorkout] = []
    for template in PPL_TEMPLATE:
        items = []
        for position, template_exercise in enumerate(template.exercises, start=1):
            exercise = catalog.get(template_exercise.exercise_id)
            if exercise is None or not (exercise.active and exercise.recommendable):
                continue
            if exercise.id in restricted_ids:
                continue
            if not equipment_available(exercise.equipment_variant, equipment):
                continue
            items.append(
                _build_item(
                    workout_key=template.workout_key,
                    position=position,
                    template_exercise=template_exercise,
                    exercise=exercise,
                )
            )
        if not items:
            return PlanGenerationBlocked(
                code="unschedulable",
                reason=f"{template.name}的动作全部被器械或限制过滤，没有可用动作",
            )
        minutes = estimated_minutes(items)
        minutes_violation = duration_violation(
            effective, minutes=minutes, label=template.name
        )
        if minutes_violation is not None:
            return PlanGenerationBlocked(code="unschedulable", reason=minutes_violation)
        workouts.append(
            PlanWorkout(
                workout_key=template.workout_key,
                name=template.name,
                estimated_minutes=minutes,
                exercises=tuple(items),
            )
        )

    payload = PlanPayload(
        plan_workouts=tuple(workouts),
        calendar_cycle=cycle,
        template_key=PPL_TEMPLATE_KEY,
    )
    try:
        validate_payload(payload, catalog=catalog)
        sessions = project_sessions(payload, starts_on=starts_on, review_on=review_on)
    except InvalidPlanPayload as exc:
        return PlanGenerationBlocked(
            code="invalid_payload", reason=f"生成的计划未通过 D9 payload 自检：{exc}"
        )
    if not sessions:
        return PlanGenerationBlocked(
            code="unschedulable",
            reason=(
                "生效范围内没有任何训练日（检查 starts_on/review_on/anchor_date/slots）："
                f"{starts_on.isoformat()} / {review_on.isoformat()}"
            ),
        )
    return PlanGenerationReady(payload=payload)


def revise_plan_payload(
    current: PlanPayload,
    adjustments: Sequence[ArrangementAdjustment],
    *,
    catalog: Mapping[str, Exercise],
) -> PlanPayload:
    """以**当前正式 payload 为基线**的受限长期修订（04 4.5；决策 7；stage4.md S4-04）。

    不是从模板重新生成：只改被列出的动作条目（keep／deload／equivalent_replace／local_skip），
    未列出的条目、训练日集合与顺序、日历循环逐字保留。
    至少一个条目必须产生真实改动：全部处置加起来没有改动即拒绝（不落库），档案补丁不能
    代替计划本身的业务变化。任一处置越界、目录读不到或修订后 payload 自检不过即拒绝。
    """
    if not adjustments:
        raise InvalidPlanPayload("长期修订必须至少给出一个要调整的动作条目")
    by_item_key: dict[str, ArrangementAdjustment] = {}
    for adjustment in adjustments:
        if adjustment.item_key in by_item_key:
            raise InvalidPlanPayload(f"同一动作条目被调整两次：{adjustment.item_key}")
        by_item_key[adjustment.item_key] = adjustment
    known = {
        item.item_key for workout in current.plan_workouts for item in workout.exercises
    }
    unknown = sorted(set(by_item_key) - known)
    if unknown:
        raise InvalidPlanPayload(f"调整引用了当前计划不存在的动作条目：{unknown}")
    workouts: list[PlanWorkout] = []
    changed = False
    for workout in current.plan_workouts:
        items: list[PlanExerciseItem] = []
        for item in workout.exercises:
            adjustment = by_item_key.get(item.item_key)
            revised = (
                item
                if adjustment is None
                else plan_item_revision(item, adjustment, catalog=catalog)
            )
            if revised is not None:
                items.append(revised)
        if not items:
            raise InvalidPlanPayload(
                f"训练日 {workout.workout_key} 的动作不能被全部删除：至少保留一个动作"
            )
        if tuple(items) == workout.exercises:
            workouts.append(workout)
            continue
        changed = True
        workouts.append(
            replace(
                workout,
                exercises=tuple(items),
                estimated_minutes=estimated_minutes(items),
            )
        )
    if not changed:
        raise InvalidPlanPayload(
            "长期修订没有产生任何真实计划改动（未列出的条目一律保持原样）"
        )
    revised_payload = replace(current, plan_workouts=tuple(workouts))
    validate_payload(revised_payload, catalog=catalog)
    return revised_payload


def _build_item(
    *,
    workout_key: str,
    position: int,
    template_exercise: _TemplateExercise,
    exercise: Exercise,
) -> PlanExerciseItem:
    record_type = cast(
        PrescriptionRecordType, prescription_record_type_for(exercise.record_type)
    )
    prescription = template_exercise.prescription
    load: Load | None = None
    if record_type == "external_load_reps":
        # 无可信训练历史：一律需要校准（D3），不构造 verified、不猜重量。
        load = needs_calibration_for(record_type, prescription)
    return PlanExerciseItem(
        item_key=f"{workout_key}-{position:02d}",
        exercise_id=exercise.id,
        display_snapshot=DisplaySnapshot(
            name=exercise.standard_name_zh,
            equipment_variant=exercise.equipment_variant,
            load_convention=exercise.load_convention,
        ),
        record_type=record_type,
        prescription=prescription,
        load=load,
        progression=Progression(
            method=template_exercise.progression_method,
            rule=PROGRESSION_RULES[template_exercise.progression_method],
        ),
    )


def _profile_equipment(profile: Profile) -> tuple[str, ...]:
    """档案器械：known 取值；denied（明确无）取空元组。未知在完整档案检查处已阻断。"""
    fact = profile.available_equipment
    return fact.value if fact.is_known and fact.value is not None else ()


def _known_int(profile: Profile, name: str) -> int:
    value = getattr(profile, name).value
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidPlanPayload(f"档案字段 {name} 在完整档案上必须有整数值：{value!r}")
    return value
