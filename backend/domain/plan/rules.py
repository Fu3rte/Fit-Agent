"""plan 确定性规则：D9 payload 校验、目录词表映射与 ``[starts_on, review_on)`` 日程投影。

正本 architecture/04 4.1–4.4、stage3.md §4.4 D9。纯函数不碰 IO（domain/__init__ 硬约束），
不读库、不写库、不推版本；正式写入归 S3-06 确认事务。

五组职责：

- **目录 → payload 映射**：``prescription_record_type_for`` 把目录 ``record_type``
  （``reps_weight``／``reps_bodyweight``／``time``）映射为三类处方口径；``load_notation_for``
  把目录 ``load_convention`` 映射为 payload ``load_notation``。两处都不发明目录外口径。
- **结构校验** ``validate_payload``：``workout_key`` 版本内唯一、``item_key`` 同 workout 内
  唯一、同一 workout 不重复动作身份、slots 只引用存在的 ``workout_key``、load 互斥、
  渐进方式与 record_type 匹配、区间 min ≤ max。传入目录映射时同时复查「只引用未停用且
  可推荐的目录动作、记录口径与负重口径一致」。
- **纠错白名单** ``validate_payload_correction``：对比存储稿与提交稿，只放行已拍可变字段
  （日期／训练日／动作候选／组次／次数区间／RIR），身份与来源（``workout_key``／``item_key``／
  ``template_key``）及记录来源引用（``basis_record_revision_id``）一律拒绝（stage3.md §5 S3-05）。
- **日程投影** ``project_sessions``：按 D9 公式 ``slot_index = floor(date - anchor_date)
  mod len(slots)`` 在 ``[starts_on, review_on)`` 逐日投影；workout 槽生成名额、rest 槽跳过；
  完成与否不影响推进，不做事后重排。
- **当次安排目标** ``validate_arrangement_target``／``arrangement_target_exercises``（S3-08）：
  目标必须与绑定版本的**同一训练日**逐条对应，只允许 ``work_sets`` 与 ``target_rir`` 两个
  已拍可变字段不同（其余全等），且只向更安全方向移动（用户已拍 B）：组次只减不增、
  目标 RIR 只增不减、计划无 RIR 时不得新造；有差异即属普通调整，必须给出非空白
  ``adjustment_reason``（PRD §5.5）。只校验，不写库。
- **失败一律大声**：结构违规抛 :class:`InvalidPlanPayload`（当次安排目标抛
  :class:`InvalidArrangementTarget`），不静默跳过、不降级。
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta

from domain.actions.rules import LOAD_CONVENTIONS
from domain.actions.schema import Exercise
from domain.plan.schema import (
    ARRANGEMENT_ITEM_DISPOSITIONS,
    ARRANGEMENT_TARGET_SCHEMA_VERSION,
    PLAN_PAYLOAD_SCHEMA_VERSION,
    PRESCRIPTION_RECORD_TYPES,
    PROGRESSION_METHODS,
    ArrangementItemDisposition,
    ArrangementTarget,
    CalendarCycle,
    DisplaySnapshot,
    IntRange,
    Load,
    NeedsCalibration,
    PlanExerciseItem,
    PlanPayload,
    PlanWorkout,
    ProjectedSession,
    RepsPrescription,
    RestCycleSlot,
    TimedPrescription,
    VerifiedLoad,
    WorkoutCycleSlot,
)

# 目录 record_type → 处方 record_type（stage3.md §2：词表映射在领域层做，不改目录口径）。
CATALOG_RECORD_TYPE_TO_PRESCRIPTION: dict[str, str] = {
    "reps_weight": "external_load_reps",
    "reps_bodyweight": "bodyweight_reps",
    "time": "timed",
}

# 渐进方式 × 处方类型匹配表（D9）：``custom`` 三类都可，但必须有明确 rule。
PROGRESSION_METHODS_BY_RECORD_TYPE: dict[str, tuple[str, ...]] = {
    "external_load_reps": ("double_progression", "repetition_progression", "custom"),
    "bodyweight_reps": ("repetition_progression", "custom"),
    "timed": ("duration_progression", "custom"),
}


class InvalidPlanPayload(ValueError):
    """payload 结构违规：调用方不得落库、不得确认，必须回到生成／纠错重新给出。"""


class InvalidArrangementTarget(ValueError):
    """当次目标快照违规（S3-08）：绑定不符、动作身份被改、调整越界一律拒绝，不落库。

    与 :class:`InvalidPlanPayload` 分开：安排不是计划版本，不得用计划写入路径或计划错误
    码把当次调整当成长期计划变更。
    """


@dataclass(frozen=True, slots=True)
class ArrangementAdjustment:
    """当次调整的已拍可变字段（04 4.3）：处置 + 减载参数 + 替换身份。

    ``disposition`` 四类已拍：保留（默认）、减载、同等刺激替换、局部跳过；未给出时沿用既有
    行为（只允许减组与提高目标 RIR 的兼容形态）。减载参数按 04 4.3 方案 1–3：``work_sets``／
    ``reps_range`` 只减不增，``load_value`` 是用户选定器械实际可用重量，必须落在已验证重量的
    50–70%；未校准〔需校准」动作不得改负荷（回落方案 1）。``replacement_exercise_id`` 只用于
    同等刺激替换：原动作身份不变，处方与负荷照抄计划（不凭空造重）。
    """

    item_key: str
    work_sets: int | None = None
    target_rir: IntRange | None = None
    disposition: ArrangementItemDisposition | None = None
    reps_range: IntRange | None = None
    load_value: float | None = None
    replacement_exercise_id: str | None = None


def arrangement_target_exercises(
    workout: PlanWorkout, adjustments: Sequence[ArrangementAdjustment]
) -> tuple[PlanExerciseItem, ...]:
    """按绑定版本的训练日与临时调整，生成**完整**目标条目（未调整的条目原样照抄）。

    结果是可以直接存进 ``arrangement_revisions.target_snapshot_json`` 的完整目标，不是差异
    补丁（04 4.3）；不做任何正式写入，是否合法由调用方再跑
    :func:`validate_arrangement_target`。
    """
    if not isinstance(workout, PlanWorkout):
        raise InvalidArrangementTarget(f"不是训练日结构：{type(workout).__name__}")
    by_item_key: dict[str, ArrangementAdjustment] = {}
    for adjustment in adjustments:
        if not isinstance(adjustment, ArrangementAdjustment):
            raise InvalidArrangementTarget(
                f"不是当次调整结构：{type(adjustment).__name__}"
            )
        if adjustment.item_key in by_item_key:
            raise InvalidArrangementTarget(
                f"同一动作条目被调整两次：{adjustment.item_key}"
            )
        by_item_key[adjustment.item_key] = adjustment
    known = {item.item_key for item in workout.exercises}
    unknown = sorted(set(by_item_key) - known)
    if unknown:
        raise InvalidArrangementTarget(f"调整引用了该训练日不存在的动作条目：{unknown}")
    return tuple(
        _adjusted_item(item, by_item_key.get(item.item_key))
        for item in workout.exercises
    )


def _adjusted_item(
    item: PlanExerciseItem, adjustment: ArrangementAdjustment | None
) -> PlanExerciseItem:
    """单个条目应用当次调整：未给出时原样返回，不顺手“修正”其他字段。

    只构造候选目标；合法性（含仅向更安全方向移动与替换等价）由
    :func:`validate_arrangement_target` 按处置逐项复核。
    """
    if adjustment is None:
        return item
    try:
        if adjustment.work_sets is not None:
            _require_positive_int("work_sets", adjustment.work_sets)
        if adjustment.target_rir is not None:
            # RIR 只作展示参考（D3）：允许 0，只要求 min ≤ max，不引入医学或强度阈值。
            _validate_range("target_rir", adjustment.target_rir, allow_zero=True)
        if adjustment.reps_range is not None:
            _validate_range("reps_range", adjustment.reps_range)
        if adjustment.load_value is not None and (
            isinstance(adjustment.load_value, bool)
            or not isinstance(adjustment.load_value, (int, float))
            or adjustment.load_value <= 0
        ):
            raise InvalidPlanPayload(
                f"load_value 必须是正数值：{adjustment.load_value!r}"
            )
        if adjustment.disposition is not None and (
            adjustment.disposition not in ARRANGEMENT_ITEM_DISPOSITIONS
        ):
            raise InvalidPlanPayload(f"处置不在已拍四类内：{adjustment.disposition!r}")
    except InvalidPlanPayload as exc:
        # 调整越界统一报 :class:`InvalidArrangementTarget`，不把当次调整的结构错误
        # 与长期计划 payload 的错误码混用。
        raise InvalidArrangementTarget(f"当次调整越界：{exc}") from exc
    disposition = adjustment.disposition
    if disposition in ("equivalent_replace", "local_skip"):
        # 替换照抄计划处方与负荷（不凭空造重）；局部跳过的目标保持计划值，处置只作标注。
        return replace(
            item,
            disposition=disposition,
            replacement_exercise_id=(
                adjustment.replacement_exercise_id
                if disposition == "equivalent_replace"
                else None
            ),
        )
    prescription = item.prescription
    load = item.load
    if disposition == "deload" and adjustment.load_value is not None:
        if not isinstance(load, VerifiedLoad):
            # 未校准或计划无负荷：不得凭当次调整造出重量（04 4.3 方案 2）。
            raise InvalidArrangementTarget(
                f"计划该动作没有已验证重量，减载不得改为负荷调整：{item.item_key}"
            )
        load = replace(load, value=float(adjustment.load_value))
    if isinstance(prescription, TimedPrescription):
        if adjustment.target_rir is not None:
            raise InvalidArrangementTarget(
                f"计时型处方没有 RIR，不得调整目标 RIR：{item.item_key}"
            )
        return replace(
            item,
            prescription=TimedPrescription(
                work_sets=(
                    prescription.work_sets
                    if adjustment.work_sets is None
                    else adjustment.work_sets
                ),
                duration_seconds_range=prescription.duration_seconds_range,
            ),
            load=load,
            disposition=disposition,
        )
    return replace(
        item,
        prescription=RepsPrescription(
            work_sets=(
                prescription.work_sets
                if adjustment.work_sets is None
                else adjustment.work_sets
            ),
            reps_range=(
                prescription.reps_range
                if adjustment.reps_range is None
                else adjustment.reps_range
            ),
            target_rir=(
                prescription.target_rir
                if adjustment.target_rir is None
                else adjustment.target_rir
            ),
        ),
        load=load,
        disposition=disposition,
    )


def validate_arrangement_target(
    target: ArrangementTarget,
    *,
    workout: PlanWorkout,
    catalog: Mapping[str, Exercise] | None = None,
) -> None:
    """校验当次目标快照结构与绑定（S3-08，纯函数不碰 IO）。

    - 绑定：``plan_workout_key`` 必须是绑定版本里真实存在的训练日；身份／训练日文本非空、
      ``scheduled_on`` 是纯日期。目标条目与绑定版本的**同一训练日逐条对应**（数量、顺序、
      ``item_key``／``exercise_id``／``record_type``／展示快照／负荷／递增不得改）；
    - 可变字段：仅处方里的 ``work_sets`` 与 ``target_rir``（04 4.3 状态调整表的已拍两种）
      允许不同，其余全等；调整只能向**更安全**方向移动（用户已拍 B）：``work_sets`` 只减不增，
      ``target_rir`` 只增不减，计划目标没有 RIR 时不得凭当次调整新造一个；有差异即属于普通
      调整，必须给出非空白 ``adjustment_reason``（PRD §5.5：普通调整必须说明原因）；
    - 结构：每个条目再跑一次 :func:`_validate_item` 的结构与区间校验（目录引用复查与替换等价
      归调用方：器械、限制与保存时必须拿到目录；本函数只按 ``catalog`` 复查替换等价，缺目录
      时带替换项的条目一律 fail-closed）。

    仅校验与绑定版本的一致性：安排不能改动作身份、不能改长期计划，也不会在这里写任何库。
    """
    if not isinstance(target, ArrangementTarget):
        raise InvalidArrangementTarget(f"不是当次目标快照结构：{type(target).__name__}")
    if target.schema_version != ARRANGEMENT_TARGET_SCHEMA_VERSION:
        raise InvalidArrangementTarget(
            f"当次目标快照 schema_version 必须为 {ARRANGEMENT_TARGET_SCHEMA_VERSION}："
            f"{target.schema_version!r}"
        )
    _require_arrangement_text("scheduled_session_id", target.scheduled_session_id)
    _require_arrangement_text("plan_version_id", target.plan_version_id)
    _require_arrangement_text("plan_workout_key", target.plan_workout_key)
    try:
        _require_date("scheduled_on", target.scheduled_on)
    except InvalidPlanPayload as exc:
        raise InvalidArrangementTarget(f"当次目标结构非法：{exc}") from exc
    if target.plan_workout_key != workout.workout_key:
        raise InvalidArrangementTarget(
            f"当次目标绑定的训练日不在计划版本内：{target.plan_workout_key!r}"
        )
    if not isinstance(target.exercises, tuple) or not target.exercises:
        raise InvalidArrangementTarget("当次目标必须是非空条目元组")
    if [item.item_key for item in target.exercises] != [
        item.item_key for item in workout.exercises
    ]:
        raise InvalidArrangementTarget(
            "当次目标必须与绑定训练日逐条对应，不得增删、重排或改名动作条目"
        )
    for planned, item in zip(workout.exercises, target.exercises, strict=True):
        try:
            _validate_item(item, None)
        except InvalidPlanPayload as exc:
            # 目标条目的结构错误统一报 :class:`InvalidArrangementTarget`，
            # 不把当次安排的结构违规混进长期计划 payload 的错误码。
            raise InvalidArrangementTarget(f"当次目标结构非法：{exc}") from exc
        _validate_arrangement_item(planned, item, catalog=catalog)
    if target.adjustment_reason is None:
        if tuple(target.exercises) != tuple(workout.exercises):
            raise InvalidArrangementTarget(
                "当次目标与计划不同却没有 adjustment_reason：普通调整必须说明原因"
            )
    else:
        # 空串与空白同样视为未说明原因，且错误码归安排（不冒用计划 payload 的错误码）。
        _require_arrangement_text("adjustment_reason", target.adjustment_reason)


def _validate_arrangement_item(
    planned: PlanExerciseItem,
    item: PlanExerciseItem,
    *,
    catalog: Mapping[str, Exercise] | None,
) -> None:
    """目标条目相对计划条目按**处置**逐项复核（04 4.3 四种处置；已拍）。

    - ``disposition is None``（既有的 S3-08 快照）：沿用保守规则——只允许减组与提高目标 RIR，
      其余全等；不把已存快照读成某个新处置。
    - ``keep``：动作身份、组数、次数区间、负荷与递增全等，只允许提高目标 RIR。
    - ``deload``：方案 1–3——组数与次数区间只减不增、负荷只降且在已验证重量的 50–70%；
      未校准／无已验证重量时不得改负荷（回落方案 1）；至少有一项真的降低。
    - ``equivalent_replace``：原动作身份不变、处方与负荷照抄计划（不凭空造重），替代动作按
      ``catalog`` 复核等价（04 4.7）；缺目录或任一身份／肌群未知一律 fail-closed。
    - ``local_skip``：目标保持计划值，只作推进；不得同时改处方或替换。
    """
    disposition = item.disposition
    if disposition is None:
        _require_same_identity(planned, item)
        _validate_legacy_adjustment(planned, item)
        if item.replacement_exercise_id is not None:
            raise InvalidArrangementTarget(
                f"未标注处置的条目不得携带替代动作身份：{planned.item_key}"
            )
        return
    if disposition == "keep":
        if item.replacement_exercise_id is not None:
            raise InvalidArrangementTarget(
                f"保留项不得携带替代动作身份：{planned.item_key}"
            )
        _require_same_identity(planned, item)
        if item.load != planned.load:
            raise InvalidArrangementTarget(f"保留项不得改负荷：{planned.item_key}")
        _require_same_prescription_shape(planned, item)
        if isinstance(planned.prescription, RepsPrescription) and isinstance(
            item.prescription, RepsPrescription
        ):
            if item.prescription.work_sets != planned.prescription.work_sets or (
                item.prescription.reps_range != planned.prescription.reps_range
            ):
                raise InvalidArrangementTarget(
                    f"保留项的组数与次数区间必须与计划相同：{planned.item_key}"
                )
            _require_non_decreasing_rir(
                planned.item_key,
                planned_rir=planned.prescription.target_rir,
                target_rir=item.prescription.target_rir,
            )
        return
    if disposition == "local_skip":
        if item != replace(planned, disposition="local_skip"):
            raise InvalidArrangementTarget(
                f"局部跳过的目标必须保持计划值，只标注处置：{planned.item_key}"
            )
        return
    if disposition == "equivalent_replace":
        violations = replacement_violations(item, catalog=catalog)
        if violations:
            raise InvalidArrangementTarget(
                f"同等刺激替换不等价（{planned.item_key}）：" + "；".join(violations)
            )
        _require_same_identity(planned, item)
        if item.prescription != planned.prescription or item.load != planned.load:
            raise InvalidArrangementTarget(
                f"同等刺激替换必须照抄计划的处方与负荷，不得自行造处方或重量：{planned.item_key}"
            )
        return
    if disposition == "deload":
        if item.replacement_exercise_id is not None:
            raise InvalidArrangementTarget(
                f"减载项不得携带替代动作身份：{planned.item_key}"
            )
        _require_same_identity(planned, item)
        if item.progression != planned.progression:
            raise InvalidArrangementTarget(f"减载项不得改递增方式：{planned.item_key}")
        _apply_deload_bounds(planned, item)
        return
    raise InvalidArrangementTarget(f"处置不在已拍四类内：{disposition!r}")


def _require_same_identity(planned: PlanExerciseItem, item: PlanExerciseItem) -> None:
    """身份与展示快照不得变：动作身份、处方口径、展示副本、递增方式（04 4.3）。"""
    if (
        item.exercise_id != planned.exercise_id
        or item.record_type != planned.record_type
        or item.display_snapshot != planned.display_snapshot
        or item.progression != planned.progression
    ):
        raise InvalidArrangementTarget(
            f"当次目标不得改动作身份／展示快照／递增：{planned.item_key}"
        )


def _require_same_prescription_shape(
    planned: PlanExerciseItem, item: PlanExerciseItem
) -> None:
    """处方类型不得改（次数型不得变计时型），否则无法与计划逐项对比。"""
    if type(item.prescription) is not type(planned.prescription):
        raise InvalidArrangementTarget(f"当次目标不得改处方类型：{planned.item_key}")


def _validate_legacy_adjustment(
    planned: PlanExerciseItem, item: PlanExerciseItem
) -> None:
    """既有 S3-08 快照的保守规则：只允许减组与提高目标 RIR，其余全等。"""
    _require_same_prescription_shape(planned, item)
    if item.load != planned.load:
        raise InvalidArrangementTarget(f"当次目标不得改负荷：{planned.item_key}")
    if isinstance(planned.prescription, TimedPrescription) and isinstance(
        item.prescription, TimedPrescription
    ):
        if (
            item.prescription.duration_seconds_range
            != planned.prescription.duration_seconds_range
        ):
            raise InvalidArrangementTarget(
                f"当次目标不得改时长处方：{planned.item_key}"
            )
        _require_non_increasing_sets(
            planned.item_key,
            planned_sets=planned.prescription.work_sets,
            work_sets=item.prescription.work_sets,
        )
        return
    if isinstance(planned.prescription, RepsPrescription) and isinstance(
        item.prescription, RepsPrescription
    ):
        if item.prescription.reps_range != planned.prescription.reps_range:
            raise InvalidArrangementTarget(
                f"当次目标不得改次数区间：{planned.item_key}"
            )
        _require_non_increasing_sets(
            planned.item_key,
            planned_sets=planned.prescription.work_sets,
            work_sets=item.prescription.work_sets,
        )
        _require_non_decreasing_rir(
            planned.item_key,
            planned_rir=planned.prescription.target_rir,
            target_rir=item.prescription.target_rir,
        )


def _apply_deload_bounds(planned: PlanExerciseItem, item: PlanExerciseItem) -> None:
    """减载方案 1–3 的确定性边界（04 4.3）：只减不增、负荷限 50–70%、至少一项降低。"""
    _require_same_prescription_shape(planned, item)
    if item.load != planned.load:
        if not isinstance(planned.load, VerifiedLoad) or not isinstance(
            item.load, VerifiedLoad
        ):
            # 未校准或计划无负荷：不得凭当次减载造出重量（方案 2 回落方案 1）。
            raise InvalidArrangementTarget(
                f"减载改负荷只适用于已有已验证重量的动作：{planned.item_key}"
            )
        if (
            item.load.unit != planned.load.unit
            or item.load.load_notation != planned.load.load_notation
        ):
            raise InvalidArrangementTarget(
                f"减载不得改负荷单位或负重口径：{planned.item_key}"
            )
        ratio = item.load.value / planned.load.value
        if not 0.5 <= ratio <= 0.7:
            raise InvalidArrangementTarget(
                f"减载只能降到已验证重量的 50–70%：{planned.item_key} "
                f"{planned.load.value} → {item.load.value}"
            )
    if isinstance(planned.prescription, TimedPrescription) and isinstance(
        item.prescription, TimedPrescription
    ):
        if (
            item.prescription.duration_seconds_range
            != planned.prescription.duration_seconds_range
        ):
            raise InvalidArrangementTarget(
                f"减载不得改时长处方（计时型只允许减组）：{planned.item_key}"
            )
        _require_non_increasing_sets(
            planned.item_key,
            planned_sets=planned.prescription.work_sets,
            work_sets=item.prescription.work_sets,
        )
    elif isinstance(planned.prescription, RepsPrescription) and isinstance(
        item.prescription, RepsPrescription
    ):
        _require_non_increasing_sets(
            planned.item_key,
            planned_sets=planned.prescription.work_sets,
            work_sets=item.prescription.work_sets,
        )
        planned_reps = planned.prescription.reps_range
        item_reps = item.prescription.reps_range
        if item_reps.min > planned_reps.min or item_reps.max > planned_reps.max:
            raise InvalidArrangementTarget(
                f"减载只能减少每组次数：{planned.item_key} "
                f"{planned_reps.min}-{planned_reps.max} → {item_reps.min}-{item_reps.max}"
            )
        if item.prescription.target_rir != planned.prescription.target_rir:
            raise InvalidArrangementTarget(
                f"减载方案 1–3 不改目标用力（提高用力属于保留）：{planned.item_key}"
            )
        reduced = (
            item.prescription.work_sets < planned.prescription.work_sets
            or item_reps.min < planned_reps.min
            or item_reps.max < planned_reps.max
            or item.load != planned.load
        )
        if not reduced:
            raise InvalidArrangementTarget(
                f"减载必须真的减少组数、次数或负荷之一：{planned.item_key}"
            )


#: 减载方案 2 的展示区间（04 4.3）：50–70% 已验证重量，用户选器械实际可用重量。
DELOAD_LOAD_BAND: tuple[float, float] = (0.5, 0.7)


def replacement_violations(
    item: PlanExerciseItem, *, catalog: Mapping[str, Exercise] | None
) -> tuple[str, ...]:
    """同等刺激替换的确定性等价复查（04 4.7；PRD §5.7），返回不等价原因（空 = 等价）。

    已拍条件全数执行，缺一不可：``modes`` 有交集、**主要肌群**有交集、替代动作启用且可推荐、
    目录身份可读；任一来源缺失（无目录、无肌群、身份读不到）都算不等价——缺失数据不得猜测，
    也不得当成「无冲突」放行。器械可用与限制冲突需要正式条件，归创建与确认路径的调用方。

    替换**不要求**旁记录口径相等（2026-09-12 已拍）：跨自重／外加负重替换允许，处方与负荷
    如何承载由写进 payload 的修订条目决定（见 :func:`plan_item_revision`）。替代动作仍须能映射
    到已拍三类处方口径，那是 payload 有效性，不是等价条件。
    """
    if catalog is None:
        return ("缺少动作目录，无法判定主要肌群与动作模式交集",)
    replacement_id = item.replacement_exercise_id
    if replacement_id is None:
        return ("同等刺激替换缺少替代动作身份",)
    if replacement_id == item.exercise_id:
        return ("替代动作身份与原动作相同，不构成替换",)
    original = catalog.get(item.exercise_id)
    replacement = catalog.get(replacement_id)
    if original is None:
        return (f"原动作身份在目录内读不到：{item.exercise_id}",)
    if replacement is None:
        return (f"替代动作身份在目录内读不到：{replacement_id}",)
    violations: list[str] = []
    if not original.modes or not replacement.modes:
        violations.append("动作模式未知：无法判定训练目的相同")
    elif set(original.modes).isdisjoint(replacement.modes):
        violations.append(
            f"动作模式无交集：{list(original.modes)} / {list(replacement.modes)}"
        )
    if not original.muscle or not replacement.muscle:
        violations.append("主要肌群未知：缺失数据不得猜测，按不等价处理")
    elif original.muscle != replacement.muscle:
        violations.append(f"主要肌群不同：{original.muscle} / {replacement.muscle}")
    if not (replacement.active and replacement.recommendable):
        violations.append(
            f"替代动作不在启用且可推荐状态：{replacement.id}"
            f"（active={replacement.active}, recommendable={replacement.recommendable}）"
        )
    return tuple(violations)


def needs_calibration_for(
    record_type: str, prescription: RepsPrescription | TimedPrescription
) -> NeedsCalibration:
    """D3 校准文案：次数型按次数下限、计时型按最短时长；RIR 不作硬性指标。

    通过 = 稳定完成处方下限；停止 = 疼痛／不适、动作明显失稳，或加重后完不成下限
    （计时型为无法维持动作）。不输出任何具体起始重量。
    """
    if record_type == "timed":
        if not isinstance(prescription, TimedPrescription):
            raise InvalidPlanPayload("timed 处方的校准必须是 TimedPrescription")
        shortest = prescription.duration_seconds_range.min
        return NeedsCalibration(
            steps=(
                "从最容易的变式或最轻档位完成一组热身，观察动作是否稳定",
                f"逐级增加负荷或难度，以能稳定完成最短时长（{shortest} 秒）的档位为准",
            ),
            pass_criteria=f"能稳定完成该组处方的最短时长（{shortest} 秒）",
            stop_criteria=(
                "出现疼痛或其他不适、动作明显失稳，或无法维持动作时停止，不继续加重"
            ),
        )
    if not isinstance(prescription, RepsPrescription):
        raise InvalidPlanPayload("次数型处方的校准必须是 RepsPrescription")
    floor = prescription.reps_range.min
    return NeedsCalibration(
        steps=(
            "从该动作最轻可用档位（自重动作取最轻辅助档）完成一组热身",
            "逐级加重，每级完成 5 次，观察动作是否稳定",
            f"以能稳定完成处方次数下限（{floor} 次）的档位为起始负荷",
        ),
        pass_criteria=f"能稳定完成该组处方的次数下限（{floor} 次）",
        stop_criteria=(
            "出现疼痛或其他不适、动作明显失稳，或加重后完不成次数下限时停止，不继续加重"
        ),
    )


def plan_item_revision(
    planned: PlanExerciseItem,
    adjustment: ArrangementAdjustment,
    *,
    catalog: Mapping[str, Exercise] | None = None,
) -> PlanExerciseItem | None:
    """长期修订：对单个计划条目应用已拍四类处置（04 4.5；决策 7），返回修订条目。

    返回 ``None`` 表示局部跳过：该条目从训练日删除（04 4.5「只对受影响部分微调、减载、
    作同等刺激替换或删除」）。与当次安排快照的差别：长期修订直接写进候选计划 payload，
    ``equivalent_replace`` 换成替代动作身份（计划不再包含原动作）；只有当替代动作能承载
    计划处方与负荷时才照抄，承载不了（跨自重／外加负重或负重口径不同）时负荷转未校准，
    绝不猜重量。替代动作仍须能映射到已拍三类处方口径（payload 有效性）。

    未列出处置、处置越界、参数与处置矛盾、等价条件不满足或目录读不到一律拒绝，不落库。
    """
    if not isinstance(adjustment, ArrangementAdjustment):
        raise InvalidPlanPayload(f"不是长期修订条目结构：{type(adjustment).__name__}")
    disposition = adjustment.disposition
    if disposition in ("keep", "local_skip"):
        _require_no_revision_params(adjustment, disposition=disposition)
        return None if disposition == "local_skip" else planned
    if disposition == "deload":
        revised = _adjusted_item(planned, adjustment)
        _validate_arrangement_item(planned, revised, catalog=None)
        return revised
    if disposition == "equivalent_replace":
        marked = replace(
            planned,
            disposition="equivalent_replace",
            replacement_exercise_id=adjustment.replacement_exercise_id,
        )
        violations = replacement_violations(marked, catalog=catalog)
        if violations:
            raise InvalidPlanPayload(
                f"同等刺激替换不等价（{planned.item_key}）：" + "；".join(violations)
            )
        replacement = (
            None
            if catalog is None
            else catalog.get(adjustment.replacement_exercise_id or "")
        )
        if replacement is None:
            raise InvalidPlanPayload(
                f"替代动作身份在目录内读不到：{adjustment.replacement_exercise_id}"
            )
        record_type = prescription_record_type_for(replacement.record_type)
        if record_type == "timed":
            if not isinstance(planned.prescription, TimedPrescription):
                raise InvalidPlanPayload(
                    f"替代动作的处方口径与计划处方不兼容（{planned.item_key}）："
                    f"{replacement.record_type} 不能承载次数型处方"
                )
            prescription: RepsPrescription | TimedPrescription = planned.prescription
            load: Load | None = None
        else:
            if not isinstance(planned.prescription, RepsPrescription):
                raise InvalidPlanPayload(
                    f"替代动作的处方口径与计划处方不兼容（{planned.item_key}）："
                    f"{replacement.record_type} 不能承载计时型处方"
                )
            prescription = planned.prescription
            load = _replacement_load(
                planned,
                replacement,
                record_type=record_type,
                prescription=prescription,
            )
        return replace(
            planned,
            exercise_id=replacement.id,
            display_snapshot=DisplaySnapshot(
                name=replacement.standard_name_zh,
                equipment_variant=replacement.equipment_variant,
                load_convention=replacement.load_convention,
            ),
            record_type=record_type,  # type: ignore[arg-type]
            prescription=prescription,
            load=load,
            disposition="equivalent_replace",
            replacement_exercise_id=None,
        )
    raise InvalidPlanPayload(f"长期修订的处置不在已拍四类内：{disposition!r}")


def _replacement_load(
    planned: PlanExerciseItem,
    replacement: Exercise,
    *,
    record_type: str,
    prescription: RepsPrescription,
) -> Load | None:
    """替代动作的负荷承载（决策 4）：能照抄已验证重量才照抄，否则未校准，绝不猜重。"""
    if record_type == "bodyweight_reps":
        # 自重替代不携带外加负重：计划本来无负荷则保持无负荷，否则转未校准。
        return (
            None
            if planned.load is None
            else needs_calibration_for(record_type, prescription)
        )
    if (
        isinstance(planned.load, VerifiedLoad)
        and planned.record_type == record_type
        and replacement.load_convention is not None
        and load_notation_for(replacement.load_convention) == planned.load.load_notation
    ):
        return planned.load
    return needs_calibration_for(record_type, prescription)


def _require_no_revision_params(
    adjustment: ArrangementAdjustment, *, disposition: str
) -> None:
    """保留／局部跳过不带任何调整参数：处置与参数矛盾即拒绝，不静默忽略。"""
    if (
        adjustment.work_sets is not None
        or adjustment.target_rir is not None
        or adjustment.reps_range is not None
        or adjustment.load_value is not None
        or adjustment.replacement_exercise_id is not None
    ):
        raise InvalidPlanPayload(
            f"处置 {disposition} 不得携带其他调整参数：{adjustment.item_key}"
        )


def _require_non_increasing_sets(
    item_key: str, *, planned_sets: int, work_sets: int
) -> None:
    """组次只能持平或减少：增组不是已拍的当次调整（已拍 B）。"""
    if work_sets > planned_sets:
        raise InvalidArrangementTarget(
            f"当次调整只能减组、不得增组：{item_key} {planned_sets} → {work_sets}"
        )


def _require_non_decreasing_rir(
    item_key: str, *, planned_rir: IntRange | None, target_rir: IntRange | None
) -> None:
    """目标 RIR 只能持平或增大；计划没有 RIR 时不得新造一个可比的 RIR 调整。"""
    if planned_rir is None:
        if target_rir is not None:
            raise InvalidArrangementTarget(
                f"计划目标没有目标 RIR，不得凭当次调整新造一个：{item_key}"
            )
        return
    if target_rir is None:
        raise InvalidArrangementTarget(
            f"当次目标不得删除计划已有的目标 RIR：{item_key}"
        )
    if target_rir.min < planned_rir.min or target_rir.max < planned_rir.max:
        raise InvalidArrangementTarget(
            f"当次调整只能提高目标 RIR、不得降低：{item_key} "
            f"{planned_rir.min}-{planned_rir.max} → {target_rir.min}-{target_rir.max}"
        )


def prescription_record_type_for(catalog_record_type: str) -> str:
    """目录 ``record_type`` → 三类处方口径；目录外口径一律拒绝（不猜第四类）。"""
    try:
        return CATALOG_RECORD_TYPE_TO_PRESCRIPTION[catalog_record_type]
    except KeyError as exc:
        raise InvalidPlanPayload(
            f"目录记录口径不在已拍三类内：{catalog_record_type!r}"
        ) from exc


def load_notation_for(load_convention: str | None) -> str:
    """目录 ``load_convention`` → payload ``load_notation``（同一五种负重口径词表）。

    外加负重动作没有负重口径时拒绝：不得凭空造一个口径。
    """
    if load_convention is None:
        raise InvalidPlanPayload("外加负重动作缺少目录负重口径，不能给出 load_notation")
    if load_convention not in LOAD_CONVENTIONS:
        raise InvalidPlanPayload(f"负重口径不在已拍五种内：{load_convention!r}")
    return load_convention


def validate_payload(
    payload: PlanPayload,
    *,
    catalog: Mapping[str, Exercise] | None = None,
) -> None:
    """校验 D9 payload 结构；``catalog`` 给出时复查目录引用（身份、未停用、可推荐、口径一致）。

    ``catalog`` 映射通常是 ``{exercise.id: exercise}`` 的推荐候选或目录快照。校验只做结构
    与目录口径；器械／限制等档案条件由生成与确认事务的领域复查负责（S3-04/S3-06）。
    """
    if not isinstance(payload, PlanPayload):
        raise InvalidPlanPayload(f"不是计划 payload 结构：{type(payload).__name__}")
    if payload.schema_version != PLAN_PAYLOAD_SCHEMA_VERSION:
        raise InvalidPlanPayload(
            f"payload schema_version 必须为 {PLAN_PAYLOAD_SCHEMA_VERSION}："
            f"{payload.schema_version!r}"
        )
    if payload.template_key is not None:
        _require_text("template_key", payload.template_key)
    if not payload.plan_workouts:
        raise InvalidPlanPayload("计划没有任何 plan_workouts")
    workout_keys: set[str] = set()
    for workout in payload.plan_workouts:
        _validate_workout(workout, catalog)
        if workout.workout_key in workout_keys:
            raise InvalidPlanPayload(f"workout_key 版本内重复：{workout.workout_key}")
        workout_keys.add(workout.workout_key)
    validate_calendar_cycle(payload.calendar_cycle, workout_keys)


def validate_calendar_cycle(
    cycle: CalendarCycle, workout_keys: Collection[str]
) -> None:
    """校验日历循环结构：slots 非空、workout 槽引用存在的 key、rest 槽不携带 key。"""
    if not isinstance(cycle, CalendarCycle):
        raise InvalidPlanPayload(f"不是日历循环结构：{type(cycle).__name__}")
    _require_date("anchor_date", cycle.anchor_date)
    if not isinstance(cycle.slots, tuple) or not cycle.slots:
        raise InvalidPlanPayload("calendar_cycle.slots 必须是非空循环槽元组")
    known = set(workout_keys)
    has_workout = False
    for slot in cycle.slots:
        if isinstance(slot, WorkoutCycleSlot):
            _require_text("workout_key", slot.workout_key)
            if slot.workout_key not in known:
                raise InvalidPlanPayload(
                    f"循环槽引用了不存在的 workout_key：{slot.workout_key}"
                )
            has_workout = True
        elif not isinstance(slot, RestCycleSlot):
            raise InvalidPlanPayload(f"未知循环槽类型：{slot!r}")
    if not has_workout:
        raise InvalidPlanPayload(
            "日历循环至少要有一个 workout 槽（否则没有应训练名额）"
        )


def validate_payload_correction(stored: PlanPayload, submitted: PlanPayload) -> None:
    """纠错白名单：只有已拍可变字段可以与存储稿不同（stage3.md §5 S3-05、F2-03 轻量纠错）。

    与 :func:`validate_payload` 的分工：那个函数管「提交稿自己是否合法」，本函数管「相对
    存储稿改了哪些字段」。可变（F2-03 已拍）：

    - 训练日与相位：``calendar_cycle.anchor_date``／``slots``；
    - 动作候选与处方：每个条目的 ``exercise_id``／``display_snapshot``／``record_type``／
      ``prescription``／``progression``（渐进与 record_type 匹配，随候选变化）与负荷值。

    不可变：``schema_version``、``template_key``（来源说明）、``workout_key``／``item_key``
    （版本内身份，含数量与顺序）、训练日展示字段（``name``／``estimated_minutes``，后者是
    确认时认可的估计，不随纠错重估）。已验证负荷的记录来源引用
    ``basis_record_revision_id`` 必须原样保留，且不得把校准负荷写成已验证负荷
    （D3：无可信记录不猜重）。越界修改一律拒绝，不部分接受。
    """
    if not isinstance(stored, PlanPayload) or not isinstance(submitted, PlanPayload):
        raise InvalidPlanPayload(
            f"纠错对比需要两份计划 payload 结构：{type(stored).__name__} / "
            f"{type(submitted).__name__}"
        )
    if submitted.schema_version != stored.schema_version:
        raise InvalidPlanPayload(
            f"纠错不可改 payload schema_version：{stored.schema_version} → "
            f"{submitted.schema_version}"
        )
    if submitted.template_key != stored.template_key:
        raise InvalidPlanPayload(
            f"纠错不可改模板来源 template_key：{stored.template_key!r} → "
            f"{submitted.template_key!r}"
        )
    if [workout.workout_key for workout in submitted.plan_workouts] != [
        workout.workout_key for workout in stored.plan_workouts
    ]:
        raise InvalidPlanPayload("纠错不可增删、重排或改名训练日（workout_key）")
    for stored_workout, submitted_workout in zip(
        stored.plan_workouts, submitted.plan_workouts, strict=True
    ):
        _validate_workout_correction(stored_workout, submitted_workout)


def _validate_workout_correction(stored: PlanWorkout, submitted: PlanWorkout) -> None:
    if submitted.name != stored.name:
        raise InvalidPlanPayload(
            f"纠错不可改训练日名称（{stored.workout_key}）：{stored.name!r} → "
            f"{submitted.name!r}"
        )
    if submitted.estimated_minutes != stored.estimated_minutes:
        raise InvalidPlanPayload(
            f"纠错不可改确认时认可的预计时长（{stored.workout_key}）："
            f"{stored.estimated_minutes} → {submitted.estimated_minutes}"
        )
    if [item.item_key for item in submitted.exercises] != [
        item.item_key for item in stored.exercises
    ]:
        raise InvalidPlanPayload(
            f"纠错不可增删或重排动作条目（{stored.workout_key} 的 item_key）"
        )
    for stored_item, submitted_item in zip(
        stored.exercises, submitted.exercises, strict=True
    ):
        _validate_item_correction(stored_item, submitted_item)


def _validate_item_correction(
    stored: PlanExerciseItem, submitted: PlanExerciseItem
) -> None:
    if isinstance(submitted.load, VerifiedLoad) and not isinstance(
        stored.load, VerifiedLoad
    ):
        raise InvalidPlanPayload(
            f"纠错不得把校准负荷写成已验证负荷（{stored.item_key}）：verified 只能来自可信记录"
        )
    if _record_provenance(submitted.load) != _record_provenance(stored.load):
        raise InvalidPlanPayload(
            f"纠错不可改已验证负荷的记录来源引用（{stored.item_key} 的 "
            f"basis_record_revision_id）：{_record_provenance(stored.load)!r} → "
            f"{_record_provenance(submitted.load)!r}"
        )


def _record_provenance(load: Load | None) -> str | None:
    """已验证负荷的来源记录修订引用；校准负荷不携带记录来源（D9 互斥）。"""
    return load.basis_record_revision_id if isinstance(load, VerifiedLoad) else None


def project_sessions(
    payload: PlanPayload,
    *,
    starts_on: date,
    review_on: date,
) -> tuple[ProjectedSession, ...]:
    """在 ``[starts_on, review_on)`` 逐日投影应训练名额（D9）。

    ``slot_index = (scheduled_on - anchor_date).days mod len(slots)``：按日历日推进，
    完成与否不影响推进；rest 槽不生成名额；``review_on`` 当日不生成（右开区间）。
    只负责首次投影，不负责事后重排；锁定与取消归 S3-06/S3-07。
    """
    validate_payload(payload)
    _require_date("starts_on", starts_on)
    _require_date("review_on", review_on)
    if starts_on >= review_on:
        raise InvalidPlanPayload(
            f"生效范围须满足 starts_on 早于 review_on（[starts_on, review_on)）："
            f"{starts_on.isoformat()} / {review_on.isoformat()}"
        )
    cycle = payload.calendar_cycle
    slots = cycle.slots
    sessions: list[ProjectedSession] = []
    scheduled_on = starts_on
    while scheduled_on < review_on:
        slot = slots[(scheduled_on - cycle.anchor_date).days % len(slots)]
        if isinstance(slot, WorkoutCycleSlot):
            sessions.append(
                ProjectedSession(
                    plan_workout_key=slot.workout_key, scheduled_on=scheduled_on
                )
            )
        scheduled_on += timedelta(days=1)
    return tuple(sessions)


def is_locked_by_date(scheduled_on: date, *, as_of: date) -> bool:
    """到期即锁的日期规则：业务日期 >= 应训练日即已锁定（04 4.2）。

    锁定字段不是唯一依据：停机跨过训练日、标记未写时仍按本规则判锁定。本函数只提供日期
    规则这一纯判断，供替换预览（S3-04）与只读投影（S3-07）共用；存储标记与日期规则的
    并集判定见 :func:`session_lock_state`。
    """
    _require_date("scheduled_on", scheduled_on)
    _require_date("as_of", as_of)
    return as_of >= scheduled_on


@dataclass(frozen=True, slots=True)
class SessionLockState:
    """一条日程的锁定状态：存储标记与日期规则各自独立可查，``effective`` 取并集。

    只读投影必须同时给出两者（04 4.2：锁定字段不是唯一依据）：只读 ``stored`` 会把停机
    跨过训练日、未写标记的到期日程误报成未锁定，而改期／删除的拒绝依据是 ``effective``。
    """

    stored: bool
    by_business_date: bool

    @property
    def effective(self) -> bool:
        """是否已锁定（不得改期或删除的依据）：存储标记或到期日期规则任一为真。"""
        return self.stored or self.by_business_date


def session_lock_state(
    scheduled_on: date, *, stored_locked_at: str | None, business_date: date
) -> SessionLockState:
    """按固定业务时区的**业务日期**判定单条日程的锁定状态（04 4.2、07 7.3）。

    ``stored_locked_at`` 是存储锁定标记（已确认完成／漏练写下，未标记为 None）；
    ``business_date`` 是当刻业务时区日期，由调用方按固定业务时区算出并注入（不取「今天」，
    可注入、可测试）。到期即锁不需要后台任务：停机跨过训练日后重开，这里仍判为已锁定。
    """
    return SessionLockState(
        stored=stored_locked_at is not None,
        by_business_date=is_locked_by_date(scheduled_on, as_of=business_date),
    )


def _validate_workout(
    workout: PlanWorkout, catalog: Mapping[str, Exercise] | None
) -> None:
    if not isinstance(workout, PlanWorkout):
        raise InvalidPlanPayload(f"不是训练处方结构：{type(workout).__name__}")
    _require_text("workout_key", workout.workout_key)
    _require_text("name", workout.name)
    _require_positive_int("estimated_minutes", workout.estimated_minutes)
    if not workout.exercises:
        raise InvalidPlanPayload(f"训练处方没有动作：{workout.workout_key}")
    item_keys: set[str] = set()
    exercise_ids: set[str] = set()
    for item in workout.exercises:
        _validate_item(item, catalog)
        if item.item_key in item_keys:
            raise InvalidPlanPayload(
                f"item_key 在 {workout.workout_key} 内重复：{item.item_key}"
            )
        item_keys.add(item.item_key)
        if item.exercise_id in exercise_ids:
            raise InvalidPlanPayload(
                f"同一训练日重复同一动作身份：{workout.workout_key} / {item.exercise_id}"
            )
        exercise_ids.add(item.exercise_id)


def _validate_item(
    item: PlanExerciseItem, catalog: Mapping[str, Exercise] | None
) -> None:
    if not isinstance(item, PlanExerciseItem):
        raise InvalidPlanPayload(f"不是计划动作结构：{type(item).__name__}")
    _require_text("item_key", item.item_key)
    _require_text("exercise_id", item.exercise_id)
    if item.record_type not in PRESCRIPTION_RECORD_TYPES:
        raise InvalidPlanPayload(f"处方 record_type 不在三类内：{item.record_type!r}")
    _validate_prescription(item)
    _validate_load(item)
    _validate_progression(item)
    snapshot = item.display_snapshot
    _require_text("display_snapshot.name", snapshot.name)
    _require_text("display_snapshot.equipment_variant", snapshot.equipment_variant)
    if snapshot.load_convention is not None:
        load_notation_for(snapshot.load_convention)
    if catalog is not None:
        _validate_catalog_reference(item, catalog)


def _validate_prescription(item: PlanExerciseItem) -> None:
    prescription = item.prescription
    if item.record_type == "timed":
        if not isinstance(prescription, TimedPrescription):
            raise InvalidPlanPayload(
                "timed 处方必须是 TimedPrescription（无次数区间与 RIR）"
            )
        _require_positive_int("work_sets", prescription.work_sets)
        _validate_range("duration_seconds_range", prescription.duration_seconds_range)
        return
    if not isinstance(prescription, RepsPrescription):
        raise InvalidPlanPayload(
            f"{item.record_type} 处方必须是 RepsPrescription（work_sets + reps_range）"
        )
    _require_positive_int("work_sets", prescription.work_sets)
    _validate_range("reps_range", prescription.reps_range)
    if prescription.target_rir is not None:
        # RIR 只作展示参考（D3）：允许 0，只要求 min ≤ max，不引入医学或强度阈值。
        _validate_range("target_rir", prescription.target_rir, allow_zero=True)


def _validate_load(item: PlanExerciseItem) -> None:
    if item.record_type == "external_load_reps":
        if isinstance(item.load, VerifiedLoad):
            _validate_verified_load(item.load)
        elif isinstance(item.load, NeedsCalibration):
            _validate_needs_calibration(item.load)
        else:
            raise InvalidPlanPayload(
                "外加负重动作必须给出 verified 或 needs_calibration 负荷，不得留空"
            )
        return
    if item.load is not None:
        raise InvalidPlanPayload(
            f"load 仅用于外加负重动作（{item.record_type} 不得携带负荷）"
        )


def _validate_verified_load(load: VerifiedLoad) -> None:
    if isinstance(load.value, bool) or not isinstance(load.value, (int, float)):
        raise InvalidPlanPayload(f"verified 负荷值需要数值：{load.value!r}")
    if not load.value > 0:
        raise InvalidPlanPayload(f"verified 负荷值必须为正：{load.value!r}")
    _require_text("verified.unit", load.unit)
    load_notation_for(load.load_notation)
    if load.basis_record_revision_id is not None:
        _require_text(
            "verified.basis_record_revision_id", load.basis_record_revision_id
        )


def _validate_needs_calibration(load: NeedsCalibration) -> None:
    if not isinstance(load.steps, tuple) or not load.steps:
        raise InvalidPlanPayload("needs_calibration.steps 必须是非空步骤元组")
    for step in load.steps:
        _require_text("needs_calibration.steps[]", step)
    _require_text("needs_calibration.pass_criteria", load.pass_criteria)
    _require_text("needs_calibration.stop_criteria", load.stop_criteria)


def _validate_progression(item: PlanExerciseItem) -> None:
    progression = item.progression
    if progression.method not in PROGRESSION_METHODS:
        raise InvalidPlanPayload(f"渐进方式不在已拍集合内：{progression.method!r}")
    allowed = PROGRESSION_METHODS_BY_RECORD_TYPE[item.record_type]
    if progression.method not in allowed:
        raise InvalidPlanPayload(
            f"渐进方式 {progression.method} 与 record_type {item.record_type} 不匹配"
        )
    _require_text("progression.rule", progression.rule)


def _validate_catalog_reference(
    item: PlanExerciseItem, catalog: Mapping[str, Exercise]
) -> None:
    exercise = catalog.get(item.exercise_id)
    if exercise is None:
        raise InvalidPlanPayload(f"计划引用了目录外动作身份：{item.exercise_id}")
    if not exercise.active:
        raise InvalidPlanPayload(f"计划引用了已停用动作：{item.exercise_id}")
    if not exercise.recommendable:
        raise InvalidPlanPayload(f"计划引用了不可推荐动作：{item.exercise_id}")
    expected = prescription_record_type_for(exercise.record_type)
    if item.record_type != expected:
        raise InvalidPlanPayload(
            f"处方 record_type 与目录不一致：{item.exercise_id} 目录为 {expected}，"
            f"payload 为 {item.record_type}"
        )
    if isinstance(item.load, VerifiedLoad) and exercise.load_convention is not None:
        expected_notation = load_notation_for(exercise.load_convention)
        if item.load.load_notation != expected_notation:
            raise InvalidPlanPayload(
                f"verified.load_notation 与目录负重口径不一致：{item.exercise_id} "
                f"目录为 {expected_notation}，payload 为 {item.load.load_notation}"
            )


def _validate_range(name: str, value: object, *, allow_zero: bool = False) -> None:
    if not isinstance(value, IntRange):
        raise InvalidPlanPayload(f"{name} 必须是区间结构 IntRange：{value!r}")
    floor = 0 if allow_zero else 1
    for label, bound in (("min", value.min), ("max", value.max)):
        if isinstance(bound, bool) or not isinstance(bound, int):
            raise InvalidPlanPayload(f"{name}.{label} 需要整数：{bound!r}")
        if bound < floor:
            raise InvalidPlanPayload(f"{name}.{label} 不得小于 {floor}：{bound!r}")
    if value.min > value.max:
        raise InvalidPlanPayload(f"{name} 须满足 min ≤ max：{value.min} > {value.max}")


def _require_positive_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidPlanPayload(f"{name} 必须是正整数：{value!r}")


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidPlanPayload(f"{name} 必须是非空文本：{value!r}")


def _require_arrangement_text(name: str, value: object) -> None:
    """安排字段的非空文本检查：违规报 :class:`InvalidArrangementTarget`。

    不冒用 :class:`InvalidPlanPayload`：当次安排不是计划版本，错误码不得把两者混用
    （见 :class:`InvalidArrangementTarget`）。
    """
    try:
        _require_text(name, value)
    except InvalidPlanPayload as exc:
        raise InvalidArrangementTarget(f"当次目标结构非法：{exc}") from exc


def _require_date(name: str, value: object) -> None:
    # datetime 是 date 的子类，但带时刻；payload 与日程区间只接受纯日期（业务时区日期）。
    if type(value) is not date:
        raise InvalidPlanPayload(f"{name} 必须是 date 日期：{value!r}")
