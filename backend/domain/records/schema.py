"""records 业务表类型定义（正本 architecture/05 5.1-5.5、报告 §4.4）。

记录事实的领域类型，与 S3-09 迁移（009）的四张记录侧表一一对应：

- **记录口径复用目录词表**（03 3.1；2026-09-11 拍板 A）：``RecordType`` 直接用
  ``domain.actions.schema`` 的三类，不另立第二套词汇；需要处方／统计措辞时经既有映射
  ``domain/plan/rules.CATALOG_RECORD_TYPE_TO_PRESCRIPTION`` 转换。
- **可空语义**：``set_type``（热身／工作组）、``rir``、``assistance``、``assisted_reps`` 与
  ``warmup_summary_text`` 允许 ``None``——未明确的事实保持为空，不被读作工作组、RIR=0
  或「无辅助」（05 5.2／5.5）。
- **负重原始值与比较键**：``RawLoad`` 保留十进制原文与单位（不转二进制浮点、不归一单位）；
  ``LoadComparisonKey`` 把负重口径与换算整数键绑成一个键，不同口径的键结构上不相等
  （报告 §4.4：单只哑铃与双只总重必须不同口径，PR 不混比）。
- **训练记录草稿载荷**（S3-10）：``RecordDraftPayload`` + ``DraftExerciseLog`` 是
  ``proposed_record_json`` 的结构与编解码，与 ``session_revisions``／``exercise_logs``／
  ``training_sets`` 行一一对应（确认落盘归 S3-11）；修订状态不在结构里存储，按事实完整性派生
  （``rules.record_draft_status``）。
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime
from typing import Any, Literal, cast

from domain.actions.schema import LoadConvention, RecordType

__all__ = [
    "ASSISTANCE_VALUES",
    "LOAD_UNITS",
    "RECORD_DRAFT_SCHEMA_VERSION",
    "SET_TYPES",
    "TIME_PRECISIONS",
    "Assistance",
    "DraftExerciseLog",
    "ExerciseLogFacts",
    "InvalidRecordRow",
    "LoadComparisonKey",
    "LoadUnit",
    "RawLoad",
    "RecordDraftPayload",
    "RecordRevisionStatus",
    "SetFacts",
    "SetType",
    "TimePrecision",
    "record_draft_from_json",
    "record_draft_to_json",
]

# 组类型：热身组／工作组（05 5.2）。未明确时保存为 None，不静默认定。
SetType = Literal["warmup", "work"]
SET_TYPES: tuple[SetType, ...] = ("warmup", "work")

# 人工辅助（05 5.5）：独立完成／仅旁边保护／实际发力帮助。未申报时保存为 None。
Assistance = Literal["none", "spotter_only", "assisted"]
ASSISTANCE_VALUES: tuple[Assistance, ...] = ("none", "spotter_only", "assisted")

# 负重单位：原始输入单位（换算规则见 domain.records.rules.load_kg_key）。
LoadUnit = Literal["kg", "lb"]
LOAD_UNITS: tuple[LoadUnit, ...] = ("kg", "lb")


@dataclass(frozen=True, slots=True)
class RawLoad:
    """一组的原始负重：十进制原文 + 单位。

    原文按用户输入保存（不折算、不四舍五入）；换算整数键是派生列，由
    :func:`domain.records.rules.load_kg_key` 统一计算，不接受调用方自行指定。
    """

    value_text: str
    unit: LoadUnit


@dataclass(frozen=True, slots=True)
class LoadComparisonKey:
    """负重比较键：同一动作、同一负重口径内才可比。

    负重口径是键的一部分，因此不同口径（单只哑铃／双只总重等）的键结构上永不相等，
    不引入隐藏容差或模糊合并（报告 §4.4）。
    """

    exercise_id: str
    load_notation: LoadConvention
    kg_key: int


@dataclass(frozen=True, slots=True)
class ExerciseLogFacts:
    """一笔修订里的一个动作事实（05 5.1-5.2）。

    ``record_type`` 取目录三类（拍板 A）；``load_notation`` 仅外加负重次数型携带（其余为
    ``None``，不虚构口径）；热身摘要保留原文，未提及时为 ``None``。
    """

    exercise_id: str
    record_type: RecordType
    load_notation: LoadConvention | None = None
    target_item_key: str | None = None
    warmup_summary_text: str | None = None


@dataclass(frozen=True, slots=True)
class SetFacts:
    """一组实际训练事实（05 5.2）。

    可空字段一律表示「未明确」，读取方不得用默认值补造：``set_type=None`` 不是工作组，
    ``rir=None`` 不是 RIR 0，``assistance=None`` 不是「无辅助」（需经确认卡确认）；
    自重次数型与计时型的 ``load`` 为 ``None``（不虚构 0kg）。
    """

    set_no: int
    set_type: SetType | None = None
    target_set_key: str | None = None
    load: RawLoad | None = None
    reps: int | None = None
    duration_seconds: int | None = None
    rir: float | None = None
    assistance: Assistance | None = None
    assisted_reps: int | None = None
    quality_text: str | None = None


# 修订状态：待补全／有效（05 5.3）。作废（voided）由 S3-11 的作废路径追加，
# 记录草稿只承载「待确认的修订」，不预写作废。
RecordRevisionStatus = Literal["incomplete", "valid"]
REVISION_STATUSES: tuple[RecordRevisionStatus, ...] = ("incomplete", "valid")

# 时间精度：已拍依据（报告 §4.2）只出现 ``timestamp`` 一个取值，故词表只收这一个已证据
# 取值；未明确的时间精度保持 ``None``，不发明第二套枚举（S3-09 证据残留 ②）。
TimePrecision = Literal["timestamp"]
TIME_PRECISIONS: tuple[TimePrecision, ...] = ("timestamp",)

RECORD_DRAFT_SCHEMA_VERSION = 1


class InvalidRecordRow(ValueError):
    """``proposed_record_json`` 或记录修订行无法解析：数据损坏，不静默吞掉。"""


@dataclass(frozen=True, slots=True)
class DraftExerciseLog:
    """一条拟议／基线动作事实 + 其全部组（05 5.3：完整修订含全部动作，不只是差异）。

    ``position`` 是修订内动作顺序（1 起、连续）；``facts`` 复用 :class:`ExerciseLogFacts`，
    ``sets`` 是逐组事实（``set_no`` 1 起、连续）。基线（既有修订）与拟议（草稿载荷）用同一
    结构表达，Diff 因此可逐层比较，不引入第二套词汇。
    """

    position: int
    facts: ExerciseLogFacts
    sets: tuple[SetFacts, ...] = ()


@dataclass(frozen=True, slots=True)
class RecordDraftPayload:
    """训练记录草稿的完整拟议载荷（``proposed_record_json``；S3-10，确认归 S3-11）。

    与 ``session_revisions``／``exercise_logs``／``training_sets`` 行一一对应，供 S3-11 在确认
    事务里原样落盘：

    - ``training_session_id`` 是**显式**归属：``None`` = 新增一次训练（确认时建立新的稳定
      身份），非空 = 补充／更正该既有训练身份（05 5.1／5.4：日期不唯一，同日多练各自身份，
      歧义必须询问，不按日期推断）。
    - ``arrangement_revision_id`` 可空且只由调用方显式给出，绝不从日期或当前计划推断
      （05 5.1：无可信安排时允许不关联计划，不强行套用）。
    - ``status`` 不在本结构里存储：修订状态由 :func:`domain.records.rules.record_draft_status`
      按事实完整性派生（05 5.2），避免出现与事实不一致的第二份状态。
    - 未明确的 RIR／质量／辅助保持 ``None``（不写 0、不写 work、不写无辅助）。
    """

    occurred_on: date
    training_session_id: str | None
    exercises: tuple[DraftExerciseLog, ...] = ()
    arrangement_revision_id: str | None = None
    started_at: str | None = None
    time_precision: TimePrecision | None = None
    completion_declared: bool = False
    is_return_phase: bool = False
    feedback: dict[str, Any] | None = None
    schema_version: int = RECORD_DRAFT_SCHEMA_VERSION


def record_draft_to_json(payload: RecordDraftPayload) -> str:
    """记录草稿载荷 → ``proposed_record_json`` 文本（与 ``from_json`` 互为逆）。

    只编码、不校验（结构与事实校验归 ``rules.validate_record_draft``）；``None`` 字段不输出，
    日期以 ISO ``YYYY-MM-DD`` 输出。动作事实展平为一行（``position`` + 动作事实 + ``sets``），
    与服务端行结构一致，不把内部组合（``DraftExerciseLog.facts``）泄进存储契约。
    """
    encoded = _encode(payload)
    assert isinstance(encoded, dict)
    encoded["exercises"] = [_encode_exercise_log(item) for item in payload.exercises]
    return json.dumps(encoded, ensure_ascii=False)


def _encode_exercise_log(item: DraftExerciseLog) -> dict[str, Any]:
    facts = _encode(item.facts)
    assert isinstance(facts, dict)
    return {
        "position": item.position,
        **facts,
        "sets": [_encode(single) for single in item.sets],
    }


def record_draft_from_json(raw: str) -> RecordDraftPayload:
    """``proposed_record_json`` 文本 → :class:`RecordDraftPayload`（形状不符即大声失败）。"""
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidRecordRow(f"记录草稿载荷 JSON 无法解析：{raw!r}") from exc
    obj = _decode_object(
        "record_draft",
        decoded,
        required=frozenset(
            # ``training_session_id`` 可缺：None 与「未输出」同义（显式的新增训练在创建入口
            # 强制表态；存储文本里缺席就是没有既有身份可归属）。
            {"schema_version", "occurred_on", "exercises"}
        ),
        optional=frozenset(
            {
                "training_session_id",
                "arrangement_revision_id",
                "started_at",
                "time_precision",
                "completion_declared",
                "is_return_phase",
                "feedback",
            }
        ),
    )
    version = _decode_int("schema_version", obj["schema_version"])
    if version != RECORD_DRAFT_SCHEMA_VERSION:
        raise InvalidRecordRow(
            f"记录草稿 schema_version 必须为 {RECORD_DRAFT_SCHEMA_VERSION}：{version!r}"
        )
    session_id = obj.get("training_session_id")
    raw_arrangement = obj.get("arrangement_revision_id")
    raw_started = obj.get("started_at")
    raw_precision = obj.get("time_precision")
    raw_feedback = obj.get("feedback")
    time_precision = (
        None if raw_precision is None else _decode_text("time_precision", raw_precision)
    )
    if time_precision is not None and time_precision not in TIME_PRECISIONS:
        raise InvalidRecordRow(f"时间精度不在已定词表内：{time_precision!r}")
    return RecordDraftPayload(
        occurred_on=_decode_date("occurred_on", obj["occurred_on"]),
        training_session_id=(
            None
            if session_id is None
            else _decode_text("training_session_id", session_id)
        ),
        exercises=tuple(
            _decode_exercise_log(item)
            for item in _decode_list("exercises", obj["exercises"])
        ),
        arrangement_revision_id=(
            None
            if raw_arrangement is None
            else _decode_text("arrangement_revision_id", raw_arrangement)
        ),
        started_at=(
            None if raw_started is None else _decode_text("started_at", raw_started)
        ),
        time_precision=cast(TimePrecision | None, time_precision),
        completion_declared=_decode_bool(
            "completion_declared", obj.get("completion_declared", False)
        ),
        is_return_phase=_decode_bool(
            "is_return_phase", obj.get("is_return_phase", False)
        ),
        feedback=(None if raw_feedback is None else _decode_feedback(raw_feedback)),
        schema_version=version,
    )


def _decode_exercise_log(decoded: Any) -> DraftExerciseLog:
    obj = _decode_object(
        "exercise_log",
        decoded,
        required=frozenset({"position", "exercise_id", "record_type", "sets"}),
        optional=frozenset({"load_notation", "target_item_key", "warmup_summary_text"}),
    )
    return DraftExerciseLog(
        position=_decode_int("position", obj["position"]),
        facts=ExerciseLogFacts(
            exercise_id=_decode_text("exercise_id", obj["exercise_id"]),
            record_type=cast(
                RecordType, _decode_text("record_type", obj["record_type"])
            ),
            load_notation=cast(
                LoadConvention | None,
                _decode_optional_text("load_notation", obj.get("load_notation")),
            ),
            target_item_key=_decode_optional_text(
                "target_item_key", obj.get("target_item_key")
            ),
            warmup_summary_text=_decode_optional_text(
                "warmup_summary_text", obj.get("warmup_summary_text")
            ),
        ),
        sets=tuple(_decode_set(item) for item in _decode_list("sets", obj["sets"])),
    )


def _decode_set(decoded: Any) -> SetFacts:
    obj = _decode_object(
        "set",
        decoded,
        required=frozenset({"set_no"}),
        optional=frozenset(
            {
                "set_type",
                "target_set_key",
                "load",
                "reps",
                "duration_seconds",
                "rir",
                "assistance",
                "assisted_reps",
                "quality_text",
            }
        ),
    )
    raw_load = obj.get("load")
    return SetFacts(
        set_no=_decode_int("set_no", obj["set_no"]),
        set_type=cast(
            SetType | None, _decode_optional_text("set_type", obj.get("set_type"))
        ),
        target_set_key=_decode_optional_text(
            "target_set_key", obj.get("target_set_key")
        ),
        load=None if raw_load is None else _decode_load(raw_load),
        reps=_decode_optional_int("reps", obj.get("reps")),
        duration_seconds=_decode_optional_int(
            "duration_seconds", obj.get("duration_seconds")
        ),
        rir=_decode_optional_number("rir", obj.get("rir")),
        assistance=cast(
            Assistance | None,
            _decode_optional_text("assistance", obj.get("assistance")),
        ),
        assisted_reps=_decode_optional_int("assisted_reps", obj.get("assisted_reps")),
        quality_text=_decode_optional_text("quality_text", obj.get("quality_text")),
    )


def _decode_load(decoded: Any) -> RawLoad:
    obj = _decode_object(
        "set.load", decoded, required=frozenset({"value_text", "unit"})
    )
    return RawLoad(
        value_text=_decode_text("load.value_text", obj["value_text"]),
        unit=cast(LoadUnit, _decode_text("load.unit", obj["unit"])),
    )


def _decode_feedback(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidRecordRow(f"feedback 必须是 JSON 对象：{value!r}")
    return value


def _decode_object(
    label: str,
    decoded: Any,
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """对象字段集校验：未知字段、缺字段一律拒绝（不猜、不补默认值）。"""
    if not isinstance(decoded, dict):
        raise InvalidRecordRow(f"{label} 不是 JSON 对象：{decoded!r}")
    unknown = sorted(set(decoded) - required - optional)
    if unknown:
        raise InvalidRecordRow(f"{label} 含未登记字段：{unknown}")
    missing = sorted(required - set(decoded))
    if missing:
        raise InvalidRecordRow(f"{label} 缺字段：{missing}")
    return decoded


def _decode_text(label: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidRecordRow(f"{label} 必须是非空文本：{value!r}")
    return value


def _decode_optional_text(label: str, value: Any) -> str | None:
    return None if value is None else _decode_text(label, value)


def _decode_int(label: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRecordRow(f"{label} 必须是整数：{value!r}")
    return value


def _decode_optional_int(label: str, value: Any) -> int | None:
    return None if value is None else _decode_int(label, value)


def _decode_optional_number(label: str, value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidRecordRow(f"{label} 必须是数值：{value!r}")
    return float(value)


def _decode_bool(label: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise InvalidRecordRow(f"{label} 必须是布尔值：{value!r}")
    return value


def _decode_list(label: str, value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise InvalidRecordRow(f"{label} 必须是数组：{value!r}")
    return value


def _decode_date(label: str, value: Any) -> date:
    text = _decode_text(label, value)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise InvalidRecordRow(f"{label} 不是 ISO 日期：{text!r}") from exc


def parse_started_at(label: str, value: str) -> str:
    """开始时刻必须是带时区的 ISO 时间戳文本（报告 §4.2 示例口径）；否则拒绝。"""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidRecordRow(f"{label} 不是 ISO 时间戳：{value!r}") from exc
    if parsed.tzinfo is None:
        raise InvalidRecordRow(f"{label} 缺少时区：{value!r}")
    return value


def _encode(value: Any) -> Any:
    """递归编码 dataclass／date／tuple；``None`` 字段省略（与 domain.plan.schema 同口径）。"""
    if isinstance(value, datetime):
        return value.isoformat()
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
