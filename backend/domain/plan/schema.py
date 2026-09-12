"""plan 类型定义：D9 计划版本 payload（schema_version=1）与日程投影结果（正本 architecture/04 4.1–4.4）。

D9 已拍（stage3.md §4.4、§8 D9）。五条硬边界：

- **行字段不进 payload**：``id``／``version``／``source_plan_version_id``／``starts_on``／
  ``review_on``／``mode``／``confirmed_at`` 在 ``plan_versions`` 行上；本模块只承载
  ``payload_json`` 内容与投影结果。日程区间 ``[starts_on, review_on)`` 由行字段给出。
- **``weekday`` 不进 payload**：具体训练日的星期由 ``scheduled_on`` 按固定业务时区派生，
  避免与 ``calendar_cycle`` 双写。
- **三类处方口径**：``record_type`` 取 ``external_load_reps``／``bodyweight_reps``／``timed``；
  与目录 ``record_type``（``reps_weight``／``reps_bodyweight``／``time``）的映射与校验在
  ``domain/plan/rules``，本模块不发明目录外口径。
- **负荷互斥**：``load`` 仅外加负重动作携带，且 ``verified`` 与 ``needs_calibration`` 是
  两个互斥类型——``NeedsCalibration`` 结构上不携带任何重量，不靠运行时约定「不许猜重」。
- **休息槽不生成名额**：``RestCycleSlot`` 结构上不携带 ``workout_key``；只有
  ``WorkoutCycleSlot`` 引用组件，投影时才生成 ``scheduled_sessions``（rules.project_sessions）。

编解码成对：``payload_to_json``／``payload_from_json`` 互为逆；计划草稿的完整拟议内容
（D9 行字段 + payload + 取消预览）由 :class:`PlanProposal` 与其编解码承载。解码只负责
还原结构与类型，业务校验仍归 ``domain/plan/rules``；形状不符一律大声失败（数据损坏），
不静默兜底。
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date
from typing import Any, Literal, cast

from domain.actions.rules import LOAD_CONVENTIONS

# D9 payload schema 版本；结构变更必须新版本号，不原地改写语义（07 7.2 同精神）。
PLAN_PAYLOAD_SCHEMA_VERSION = 1

# 计划草稿提议信封的 schema 版本（D9 行字段 + payload + 取消预览的草稿级包装）。
PLAN_PROPOSAL_SCHEMA_VERSION = 1

# 当次安排目标快照的 schema 版本（S3-08；结构变更必须新版本号，不原地改写语义）。
ARRANGEMENT_TARGET_SCHEMA_VERSION = 1

# 当次安排的逐项处置恰四类（04 4.3 已拍）：保留、减载（方案 1–3）、同等刺激替换、局部
# 跳过。快照始终保存该项的**完整最终目标**，处置只是标注，不是差异补丁；未标注（既有的
# S3-08 快照与计划 payload）为 None，按保守规则校验，不改已存数据。
ArrangementItemDisposition = Literal[
    "keep", "deload", "equivalent_replace", "local_skip"
]
ARRANGEMENT_ITEM_DISPOSITIONS: tuple[ArrangementItemDisposition, ...] = (
    "keep",
    "deload",
    "equivalent_replace",
    "local_skip",
)

# 计划模式（D9 行字段）：常规 / 接回。关系字段不进 payload，见 :class:`PlanProposal`。
PlanMode = Literal["regular", "return"]
PLAN_MODES: tuple[PlanMode, ...] = ("regular", "return")

# 处方记录口径恰三类（D9；不建第四类，也不把辅助负重型搬进来）。
PrescriptionRecordType = Literal["external_load_reps", "bodyweight_reps", "timed"]
PRESCRIPTION_RECORD_TYPES: tuple[PrescriptionRecordType, ...] = (
    "external_load_reps",
    "bodyweight_reps",
    "timed",
)

# 渐进方式（D9）：method 与记录口径的匹配表见 rules；custom 也必须有明确 rule。
ProgressionMethod = Literal[
    "double_progression",
    "repetition_progression",
    "duration_progression",
    "custom",
]
PROGRESSION_METHODS: tuple[ProgressionMethod, ...] = (
    "double_progression",
    "repetition_progression",
    "duration_progression",
    "custom",
)

# payload 的 ``load_notation`` 词表复用目录已拍五种负重口径（stage3.md §2：不改目录已拍
# 词表、不发明目录外口径）；映射与校验见 rules.load_notation_for。
LOAD_NOTATIONS: tuple[str, ...] = LOAD_CONVENTIONS


@dataclass(frozen=True, slots=True)
class IntRange:
    """显式闭区间：min ≤ max，不表达「约」「左右」等模糊口径。"""

    min: int
    max: int


@dataclass(frozen=True, slots=True)
class RepsPrescription:
    """次数型处方（D9）：``work_sets`` + ``reps_range`` + 可选 ``target_rir``。

    ``target_rir`` 只作展示参考：D3 已拍 RIR 不是校准的硬性指标。
    """

    work_sets: int
    reps_range: IntRange
    target_rir: IntRange | None = None


@dataclass(frozen=True, slots=True)
class TimedPrescription:
    """计时型处方（D9）：``work_sets`` + ``duration_seconds_range``（秒）；首版不强制 RIR。"""

    work_sets: int
    duration_seconds_range: IntRange


@dataclass(frozen=True, slots=True)
class VerifiedLoad:
    """可信历史给出的外加负重（D9）。

    生成侧在无可信记录时不得构造本类型（不得猜重，stage3.md §4.4）；只有按可信记录
    复核后才可出现。``load_notation`` 取自目录负重口径词表。
    """

    value: float
    unit: str
    load_notation: str
    basis_record_revision_id: str | None = None
    kind: Literal["verified"] = field(default="verified", init=False)


@dataclass(frozen=True, slots=True)
class NeedsCalibration:
    """需要校准（D3）：只给逐级试重步骤与通过／停止标准，结构上不携带任何重量。"""

    steps: tuple[str, ...]
    pass_criteria: str
    stop_criteria: str
    kind: Literal["needs_calibration"] = field(default="needs_calibration", init=False)


Load = VerifiedLoad | NeedsCalibration


@dataclass(frozen=True, slots=True)
class Progression:
    """渐进方式 + 明确规则文本（D9）：method 与 record_type 的匹配校验在 rules。"""

    method: ProgressionMethod
    rule: str


@dataclass(frozen=True, slots=True)
class DisplaySnapshot:
    """确认时冻结的展示副本（D9）：仅供展示；校验仍以目录与最新限制为准。"""

    name: str
    equipment_variant: str
    load_convention: str | None


@dataclass(frozen=True, slots=True)
class PlanExerciseItem:
    """一个计划动作：目录稳定身份 + 处方 + 负荷 + 渐进（D9）。

    ``disposition``／``replacement_exercise_id`` 只由当次安排快照填写（04 4.3 四种处置：
    保留、减载、同等刺激替换、局部跳过）；计划 payload 不带处置，既有快照不带这两个字段时
    解码为 ``None``（视为未标注的既有形态），因此本结构的扩展不改已存数据的解码。
    """

    item_key: str
    exercise_id: str
    display_snapshot: DisplaySnapshot
    record_type: PrescriptionRecordType
    prescription: RepsPrescription | TimedPrescription
    load: Load | None
    progression: Progression
    #: 当次安排的该项处置；计划 payload 与既有快照为 None（未标注，按保守规则校验）。
    disposition: ArrangementItemDisposition | None = None
    #: 同等刺激替换的替代动作身份；只有 ``disposition='equivalent_replace'`` 时出现。
    replacement_exercise_id: str | None = None


@dataclass(frozen=True, slots=True)
class PlanWorkout:
    """可被 ``calendar_cycle`` 引用的训练处方（D9）：``workout_key`` 版本内唯一。"""

    workout_key: str
    name: str
    estimated_minutes: int
    exercises: tuple[PlanExerciseItem, ...]


@dataclass(frozen=True, slots=True)
class WorkoutCycleSlot:
    """日历循环中的训练槽：引用同版本存在的 ``workout_key``；投影时生成一条应训练名额。"""

    workout_key: str
    kind: Literal["workout"] = field(default="workout", init=False)


@dataclass(frozen=True, slots=True)
class RestCycleSlot:
    """日历循环中的休息槽：不生成应训练名额（D9；休息日不进入完成率分母）。"""

    kind: Literal["rest"] = field(default="rest", init=False)


CycleSlot = WorkoutCycleSlot | RestCycleSlot


@dataclass(frozen=True, slots=True)
class CalendarCycle:
    """日历循环（D9）：``anchor_date`` 定相位，``slots`` 长度即循环长度。

    按日历日推进，是否完成不影响推进；只负责首次投影日程，不负责事后重排。
    """

    anchor_date: date
    slots: tuple[CycleSlot, ...]


@dataclass(frozen=True, slots=True)
class PlanPayload:
    """D9 ``plan_versions.payload_json``（schema_version=1）。"""

    plan_workouts: tuple[PlanWorkout, ...]
    calendar_cycle: CalendarCycle
    template_key: str | None = None
    schema_version: int = PLAN_PAYLOAD_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class ProjectedSession:
    """投影出的一条应训练名额（D9）：``scheduled_on`` 为固定业务时区日期。"""

    plan_workout_key: str
    scheduled_on: date


@dataclass(frozen=True, slots=True)
class ProposedSessionCancellation:
    """替换计划时拟议取消的旧版日程（stage3.md §5 S3-04：仅拟议，不落正式取消）。

    绑定草稿生成读取时刻的旧版日程事实（``scheduled_session_id`` 是正式表身份）；取消动作
    本身由确认事务（S3-06）按当刻业务日期与规则重算并写入正式表，本结构只承载用户所见
    的取消清单预览——它不是正式取消，也不保证确认时逐条照搬。
    """

    scheduled_session_id: str
    plan_version_id: str
    plan_workout_key: str
    scheduled_on: date


@dataclass(frozen=True, slots=True)
class PlanProposal:
    """计划草稿的完整拟议内容（草稿信封）：scope 行字段 + D9 payload + 取消预览。

    D9 已拍行字段（``starts_on``／``review_on``／``mode``／``source_plan_version_id``）在
    ``plan_versions`` 行上、不进 ``payload_json``；草稿表没有这些列，因此在信封顶层保存，
    ``payload`` 保持 D9 纯净。``source_plan_version_id`` 是生成时绑定的替换基线（首个计划
    为 None），不是「查询时的最新版本」；``cancellations`` 在创建时按读取基线与业务日期
    固定，重开、后续提交后都不改（确认时不自动采纳，见 S3-06）。
    """

    starts_on: date
    review_on: date
    payload: PlanPayload
    source_plan_version_id: str | None = None
    mode: PlanMode = "regular"
    cancellations: tuple[ProposedSessionCancellation, ...] = ()
    schema_version: int = PLAN_PROPOSAL_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class ArrangementTarget:
    """一次接受的当次目标快照（04 4.3、S3-08）：绑定 + 该次完整目标，不是差异补丁。

    ``scheduled_session_id``／``plan_version_id``／``plan_workout_key``／``scheduled_on`` 是绑定：
    当次安排明确对应训练日与具体计划版本（04 4.3）。``exercises`` 是**完整**目标（照抄绑定
    版本该训练日的动作身份与处方，只允许已拍的临时调整改动组次／目标 RIR），因此长期计划
    后续变化不改写这份快照（v1 L88–95：不得倒改执行标准）。accept 时间不进本结构：真实
    ``accepted_at`` 是 ``arrangement_revisions`` 行字段，由确认事务写入（不得倒填）。
    """

    scheduled_session_id: str
    plan_version_id: str
    plan_workout_key: str
    scheduled_on: date
    exercises: tuple[PlanExerciseItem, ...]
    adjustment_reason: str | None = None
    schema_version: int = ARRANGEMENT_TARGET_SCHEMA_VERSION


def arrangement_target_to_json(target: ArrangementTarget) -> str:
    """当次目标快照 → ``proposed_arrangement_json``／``target_snapshot_json`` 文本。

    与 :func:`arrangement_target_from_json` 互为逆；本函数只编码，不校验（结构与绑定校验归
    ``rules.validate_arrangement_target``），``adjustment_reason`` 缺省时不输出。
    """
    return json.dumps(_encode(target), ensure_ascii=False)


def arrangement_target_from_json(raw: str) -> ArrangementTarget:
    """当次目标快照文本 → :class:`ArrangementTarget`（形状不符即数据损坏，大声失败）。"""
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidPlanRow(f"当次目标快照 JSON 无法解析：{raw!r}") from exc
    obj = _decode_object(
        "arrangement_target",
        decoded,
        required=frozenset(
            {
                "schema_version",
                "scheduled_session_id",
                "plan_version_id",
                "plan_workout_key",
                "scheduled_on",
                "exercises",
            }
        ),
        optional=frozenset({"adjustment_reason"}),
    )
    version = _decode_int("schema_version", obj["schema_version"])
    if version != ARRANGEMENT_TARGET_SCHEMA_VERSION:
        raise InvalidPlanRow(
            f"当次目标快照 schema_version 必须为 {ARRANGEMENT_TARGET_SCHEMA_VERSION}：{version!r}"
        )
    raw_reason = obj.get("adjustment_reason")
    return ArrangementTarget(
        scheduled_session_id=_decode_text(
            "scheduled_session_id", obj["scheduled_session_id"]
        ),
        plan_version_id=_decode_text("plan_version_id", obj["plan_version_id"]),
        plan_workout_key=_decode_text("plan_workout_key", obj["plan_workout_key"]),
        scheduled_on=_decode_date("scheduled_on", obj["scheduled_on"]),
        exercises=tuple(
            _decode_item(item) for item in _decode_list("exercises", obj["exercises"])
        ),
        adjustment_reason=(
            None
            if raw_reason is None
            else _decode_text("adjustment_reason", raw_reason)
        ),
        schema_version=version,
    )


def payload_to_json(payload: PlanPayload) -> str:
    """payload → ``payload_json`` 文本（字段名即 D9 契约；None 字段不输出）。

    - ``load`` 仅外加负重动作输出，``template_key``／``target_rir``／
      ``basis_record_revision_id`` 缺省时不出现。
    - 日期以 ISO ``YYYY-MM-DD`` 输出；不做时区换算（固定业务时区归 07 7.3）。
    - 本函数只编码，不校验：调用前必须已过 ``rules.validate_payload``。
    """
    return json.dumps(_encode(payload), ensure_ascii=False)


def _encode(value: Any) -> Any:
    """递归编码 dataclass／date／tuple；None 字段省略（D9 的「仅…时输出」靠此表达）。"""
    if isinstance(value, date):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _encode(getattr(value, item.name))
            for item in fields(value)
            if getattr(value, item.name) is not None
        }
    if isinstance(value, (tuple, list)):
        return [_encode(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _encode(item) for key, item in value.items()}
    return value


class InvalidPlanRow(ValueError):
    """``plan_versions.payload_json`` 或草稿拟议载荷无法解析：数据损坏，不静默吞掉。"""


def payload_from_json(raw: str) -> PlanPayload:
    """``payload_json`` 文本 → payload（与 :func:`payload_to_json` 互为逆）。

    只做结构解码：字段集／类型／判别字段（``kind``）不符即抛 :class:`InvalidPlanRow`，
    不猜测、不补默认值；业务规则（区间大小、引用一致性）仍归 ``rules.validate_payload``。
    """
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidPlanRow(f"计划 payload JSON 无法解析：{raw!r}") from exc
    return _decode_payload(decoded)


def proposal_to_json(proposal: PlanProposal) -> str:
    """计划草稿提议 → ``proposed_plan_json`` 文本（行字段在信封顶层，payload 保持 D9 纯净）。"""
    encoded: dict[str, Any] = {
        "schema_version": proposal.schema_version,
        "starts_on": proposal.starts_on.isoformat(),
        "review_on": proposal.review_on.isoformat(),
        "mode": proposal.mode,
        "payload": _encode(proposal.payload),
        "cancellations": [
            {
                "scheduled_session_id": item.scheduled_session_id,
                "plan_version_id": item.plan_version_id,
                "plan_workout_key": item.plan_workout_key,
                "scheduled_on": item.scheduled_on.isoformat(),
            }
            for item in proposal.cancellations
        ],
    }
    if proposal.source_plan_version_id is not None:
        encoded["source_plan_version_id"] = proposal.source_plan_version_id
    return json.dumps(encoded, ensure_ascii=False)


def proposal_from_json(raw: str) -> PlanProposal:
    """``proposed_plan_json`` 文本 → 计划草稿提议（与 :func:`proposal_to_json` 互为逆）。"""
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidPlanRow(f"计划草稿提议 JSON 无法解析：{raw!r}") from exc
    obj = _decode_object(
        "plan_proposal",
        decoded,
        required=frozenset(
            {
                "schema_version",
                "starts_on",
                "review_on",
                "mode",
                "payload",
            }
        ),
        optional=frozenset({"source_plan_version_id", "cancellations"}),
    )
    version = _decode_int("schema_version", obj["schema_version"])
    if version != PLAN_PROPOSAL_SCHEMA_VERSION:
        raise InvalidPlanRow(
            f"草稿提议 schema_version 必须为 {PLAN_PROPOSAL_SCHEMA_VERSION}：{version!r}"
        )
    mode = obj["mode"]
    if mode not in PLAN_MODES:
        raise InvalidPlanRow(f"计划模式不在已拍集合内：{mode!r}")
    source = obj.get("source_plan_version_id")
    raw_cancellations = obj.get("cancellations")
    return PlanProposal(
        starts_on=_decode_date("starts_on", obj["starts_on"]),
        review_on=_decode_date("review_on", obj["review_on"]),
        payload=_decode_payload(obj["payload"]),
        source_plan_version_id=(
            None if source is None else _decode_text("source_plan_version_id", source)
        ),
        mode=cast(PlanMode, mode),
        cancellations=(
            ()
            if raw_cancellations is None
            else tuple(
                _decode_cancellation(item)
                for item in _decode_list("cancellations", raw_cancellations)
            )
        ),
        schema_version=version,
    )


def _decode_payload(decoded: Any) -> PlanPayload:
    obj = _decode_object(
        "payload",
        decoded,
        required=frozenset({"schema_version", "plan_workouts", "calendar_cycle"}),
        optional=frozenset({"template_key"}),
    )
    version = _decode_int("schema_version", obj["schema_version"])
    if version != PLAN_PAYLOAD_SCHEMA_VERSION:
        raise InvalidPlanRow(
            f"payload schema_version 必须为 {PLAN_PAYLOAD_SCHEMA_VERSION}：{version!r}"
        )
    template_key = obj.get("template_key")
    return PlanPayload(
        plan_workouts=tuple(
            _decode_workout(item)
            for item in _decode_list("plan_workouts", obj["plan_workouts"])
        ),
        calendar_cycle=_decode_cycle(obj["calendar_cycle"]),
        template_key=(
            None if template_key is None else _decode_text("template_key", template_key)
        ),
        schema_version=version,
    )


def _decode_workout(decoded: Any) -> PlanWorkout:
    obj = _decode_object(
        "plan_workout",
        decoded,
        required=frozenset(
            {
                "workout_key",
                "name",
                "estimated_minutes",
                "exercises",
            }
        ),
    )
    return PlanWorkout(
        workout_key=_decode_text("workout_key", obj["workout_key"]),
        name=_decode_text("name", obj["name"]),
        estimated_minutes=_decode_int("estimated_minutes", obj["estimated_minutes"]),
        exercises=tuple(
            _decode_item(item) for item in _decode_list("exercises", obj["exercises"])
        ),
    )


def _decode_item(decoded: Any) -> PlanExerciseItem:
    obj = _decode_object(
        "plan_exercise",
        decoded,
        required=frozenset(
            {
                "item_key",
                "exercise_id",
                "display_snapshot",
                "record_type",
                "prescription",
                "progression",
            }
        ),
        optional=frozenset({"load", "disposition", "replacement_exercise_id"}),
    )
    record_type = obj["record_type"]
    if record_type not in PRESCRIPTION_RECORD_TYPES:
        raise InvalidPlanRow(f"处方 record_type 不在三类内：{record_type!r}")
    raw_load = obj.get("load")
    raw_disposition = obj.get("disposition")
    if (
        raw_disposition is not None
        and raw_disposition not in ARRANGEMENT_ITEM_DISPOSITIONS
    ):
        raise InvalidPlanRow(f"处置不在已拍四类内：{raw_disposition!r}")
    raw_replacement = obj.get("replacement_exercise_id")
    return PlanExerciseItem(
        item_key=_decode_text("item_key", obj["item_key"]),
        exercise_id=_decode_text("exercise_id", obj["exercise_id"]),
        display_snapshot=_decode_snapshot(obj["display_snapshot"]),
        record_type=cast(PrescriptionRecordType, record_type),
        prescription=_decode_prescription(record_type, obj["prescription"]),
        load=None if raw_load is None else _decode_load(raw_load),
        progression=_decode_progression(obj["progression"]),
        disposition=(
            None
            if raw_disposition is None
            else cast(ArrangementItemDisposition, raw_disposition)
        ),
        replacement_exercise_id=(
            None
            if raw_replacement is None
            else _decode_text("replacement_exercise_id", raw_replacement)
        ),
    )


def _decode_snapshot(decoded: Any) -> DisplaySnapshot:
    obj = _decode_object(
        "display_snapshot",
        decoded,
        required=frozenset({"name", "equipment_variant"}),
        optional=frozenset({"load_convention"}),
    )
    load_convention = obj.get("load_convention")
    return DisplaySnapshot(
        name=_decode_text("display_snapshot.name", obj["name"]),
        equipment_variant=_decode_text(
            "display_snapshot.equipment_variant", obj["equipment_variant"]
        ),
        load_convention=(
            None
            if load_convention is None
            else _decode_text("display_snapshot.load_convention", load_convention)
        ),
    )


def _decode_prescription(
    record_type: str, decoded: Any
) -> RepsPrescription | TimedPrescription:
    if record_type == "timed":
        obj = _decode_object(
            "timed_prescription",
            decoded,
            required=frozenset({"work_sets", "duration_seconds_range"}),
        )
        return TimedPrescription(
            work_sets=_decode_int("work_sets", obj["work_sets"]),
            duration_seconds_range=_decode_range(obj["duration_seconds_range"]),
        )
    obj = _decode_object(
        "reps_prescription",
        decoded,
        required=frozenset({"work_sets", "reps_range"}),
        optional=frozenset({"target_rir"}),
    )
    target_rir = obj.get("target_rir")
    return RepsPrescription(
        work_sets=_decode_int("work_sets", obj["work_sets"]),
        reps_range=_decode_range(obj["reps_range"]),
        target_rir=None if target_rir is None else _decode_range(target_rir),
    )


def _decode_range(decoded: Any) -> IntRange:
    obj = _decode_object("range", decoded, required=frozenset({"min", "max"}))
    return IntRange(
        min=_decode_int("range.min", obj["min"]),
        max=_decode_int("range.max", obj["max"]),
    )


def _decode_load(decoded: Any) -> Load:
    if not isinstance(decoded, dict):
        raise InvalidPlanRow(f"load 不是对象：{decoded!r}")
    kind = decoded.get("kind")
    if kind == "verified":
        obj = _decode_object(
            "verified_load",
            decoded,
            required=frozenset({"kind", "value", "unit", "load_notation"}),
            optional=frozenset({"basis_record_revision_id"}),
        )
        basis = obj.get("basis_record_revision_id")
        return VerifiedLoad(
            value=_decode_number("value", obj["value"]),
            unit=_decode_text("unit", obj["unit"]),
            load_notation=_decode_text("load_notation", obj["load_notation"]),
            basis_record_revision_id=(
                None
                if basis is None
                else _decode_text("basis_record_revision_id", basis)
            ),
        )
    if kind == "needs_calibration":
        obj = _decode_object(
            "needs_calibration",
            decoded,
            required=frozenset(
                {
                    "kind",
                    "steps",
                    "pass_criteria",
                    "stop_criteria",
                }
            ),
        )
        return NeedsCalibration(
            steps=tuple(
                _decode_text("steps[]", step)
                for step in _decode_list("steps", obj["steps"])
            ),
            pass_criteria=_decode_text("pass_criteria", obj["pass_criteria"]),
            stop_criteria=_decode_text("stop_criteria", obj["stop_criteria"]),
        )
    raise InvalidPlanRow(f"负荷类型不在已拍两类内：{kind!r}")


def _decode_progression(decoded: Any) -> Progression:
    obj = _decode_object("progression", decoded, required=frozenset({"method", "rule"}))
    method = obj["method"]
    if method not in PROGRESSION_METHODS:
        raise InvalidPlanRow(f"渐进方式不在已拍集合内：{method!r}")
    return Progression(
        method=cast(ProgressionMethod, method),
        rule=_decode_text("progression.rule", obj["rule"]),
    )


def _decode_cycle(decoded: Any) -> CalendarCycle:
    obj = _decode_object(
        "calendar_cycle",
        decoded,
        required=frozenset({"anchor_date", "slots"}),
    )
    return CalendarCycle(
        anchor_date=_decode_date("anchor_date", obj["anchor_date"]),
        slots=tuple(_decode_slot(item) for item in _decode_list("slots", obj["slots"])),
    )


def _decode_slot(decoded: Any) -> CycleSlot:
    if not isinstance(decoded, dict):
        raise InvalidPlanRow(f"循环槽不是对象：{decoded!r}")
    kind = decoded.get("kind")
    if kind == "workout":
        obj = _decode_object(
            "workout_slot",
            decoded,
            required=frozenset({"kind", "workout_key"}),
        )
        return WorkoutCycleSlot(
            workout_key=_decode_text("workout_slot.workout_key", obj["workout_key"])
        )
    if kind == "rest":
        _decode_object("rest_slot", decoded, required=frozenset({"kind"}))
        return RestCycleSlot()
    raise InvalidPlanRow(f"未知循环槽类型：{kind!r}")


def _decode_cancellation(decoded: Any) -> ProposedSessionCancellation:
    obj = _decode_object(
        "cancellation",
        decoded,
        required=frozenset(
            {
                "scheduled_session_id",
                "plan_version_id",
                "plan_workout_key",
                "scheduled_on",
            }
        ),
    )
    return ProposedSessionCancellation(
        scheduled_session_id=_decode_text(
            "cancellation.scheduled_session_id", obj["scheduled_session_id"]
        ),
        plan_version_id=_decode_text(
            "cancellation.plan_version_id", obj["plan_version_id"]
        ),
        plan_workout_key=_decode_text(
            "cancellation.plan_workout_key", obj["plan_workout_key"]
        ),
        scheduled_on=_decode_date("cancellation.scheduled_on", obj["scheduled_on"]),
    )


def _decode_object(
    label: str,
    decoded: Any,
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """对象字段集校验：未知字段、缺字段一律拒绝（不猜、不补默认值）。"""
    if not isinstance(decoded, dict):
        raise InvalidPlanRow(f"{label} 不是 JSON 对象：{decoded!r}")
    unknown = sorted(set(decoded) - required - optional)
    if unknown:
        raise InvalidPlanRow(f"{label} 含未登记字段：{unknown}")
    missing = sorted(required - set(decoded))
    if missing:
        raise InvalidPlanRow(f"{label} 缺字段：{missing}")
    return decoded


def _decode_text(label: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidPlanRow(f"{label} 必须是非空文本：{value!r}")
    return value


def _decode_int(label: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidPlanRow(f"{label} 必须是整数：{value!r}")
    return value


def _decode_number(label: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidPlanRow(f"{label} 必须是数值：{value!r}")
    return float(value)


def _decode_list(label: str, value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise InvalidPlanRow(f"{label} 必须是数组：{value!r}")
    return value


def _decode_date(label: str, value: Any) -> date:
    text = _decode_text(label, value)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise InvalidPlanRow(f"{label} 不是 ISO 日期：{text!r}") from exc
