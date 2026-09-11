"""records 确定性规则：负重换算键与可空事实校验，纯函数不碰 IO（正本 architecture/05 5.3、报告 §4.4）。

五条已定口径：

- **负重换算键唯一实现**（报告 §4.4）：lb 先按 ``1 lb = 0.45359237 kg`` 换算，再 ×1000 后按
  ``ROUND_HALF_EVEN`` 取整；SQLite 整数上限（2⁶³−1）溢出、负数、非有限值与未知单位一律拒绝。
  倍率或舍入规则变更须整体迁移重算，不得混用新旧尺度，故所有写入与查询入口都必须调用
  :func:`load_kg_key`，禁止各自实现舍入。
- **负重口径不混比**：:func:`comparison_key` 把 ``load_notation`` 绑进 :class:`LoadComparisonKey`；
  单只哑铃与双只总重等不同口径的键结构上永不相等，不引入隐藏容差或模糊合并。
- **可空事实不补造**：``rir`` 的 ``None``（未报告）原样保留，不读作 RIR 0（05 5.2）。
- **记录草稿结构校验**（S3-10）：:func:`validate_record_draft` 只拒绝结构上不合法的事实
  （词表／区间／类型／负重可解析／顺序连续），**不**要求必填事实完整——待补全载荷允许保存
  并确认（05 5.2 转正门槛 B，D8 已拍 A）。
- **修订状态派生**（05 5.2）：:func:`record_draft_status` 按事实完整性判定 ``incomplete``／
  ``valid``，不另存第二份可失步的状态；
  :func:`validate_record_draft_correction` 是纠错白名单：归属（``training_session_id``）与
  安排关联（``arrangement_revision_id``）不可经纠错改写（05 5.2「目标不唯一必须询问」）。
"""

import math
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from domain.actions.rules import LOAD_CONVENTIONS, RECORD_TYPES
from domain.records.schema import (
    ASSISTANCE_VALUES,
    SET_TYPES,
    TIME_PRECISIONS,
    DraftExerciseLog,
    LoadComparisonKey,
    RawLoad,
    RecordDraftPayload,
    RecordRevisionStatus,
    SetFacts,
    parse_started_at,
)

# 报告 §4.4 已定稿的换算尺度：与业务表设计报告一致，变更须整体迁移重算。
LB_TO_KG = Decimal("0.45359237")
LOAD_KEY_SCALE = Decimal(1000)
MAX_LOAD_KG_KEY = 2**63 - 1  # SQLite 整数上限


class InvalidRecordFact(ValueError):
    """记录事实不合口径（负重无法换算／RIR 越界等）：不落库、不静默兜底。"""


def _scaled_kg_key(value_text: str, unit: str) -> int:
    """十进制原文 + 单位 → 统一缩放整数键（kg×1000）；非法输入一律拒绝。"""
    try:
        value = Decimal(value_text)
    except (InvalidOperation, ValueError) as exc:
        raise InvalidRecordFact(f"负重数值无法解析：{value_text!r}") from exc
    if not value.is_finite():
        raise InvalidRecordFact(f"负重必须为有限数值：{value_text!r}")
    if value < 0:
        raise InvalidRecordFact(f"负重不能为负：{value_text!r}")
    if unit == "lb":
        value = value * LB_TO_KG
    elif unit != "kg":
        raise InvalidRecordFact(f"未知负重单位：{unit!r}")
    # 量级检查先于 quantize：超长／超大原文在默认 decimal 上下文会因精度溢出抛
    # decimal.InvalidOperation，这里先比上限，保证拒绝路径统一为 InvalidRecordFact。
    scaled = value * LOAD_KEY_SCALE
    if scaled > MAX_LOAD_KG_KEY:
        raise InvalidRecordFact(f"负重超出可存储范围：{value_text!r}")
    key = int(scaled.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
    return key


def load_kg_key(load: RawLoad | None) -> int | None:
    """原始负重 → 换算整数键；无负重（自重／计时／未记录）返回 ``None``。

    原文按十进制解析（不经过二进制浮点），因此 ``45.359237kg`` 与 ``100lb`` 得到同一键。
    """
    if load is None:
        return None
    return _scaled_kg_key(load.value_text, load.unit)


def comparison_key(
    *, exercise_id: str, load_notation: str, load: RawLoad | None
) -> LoadComparisonKey | None:
    """负重比较键：``None`` 表示该组不参与按重量比较（自重／计时／未记录负重）。

    口径必须落在目录已拍五种内；口径不同即不同键，调用方不得跨口径比较（报告 §4.4）。
    """
    if load is None:
        return None
    if load_notation not in LOAD_CONVENTIONS:
        raise InvalidRecordFact(f"负重口径不在已拍五种内：{load_notation!r}")
    return LoadComparisonKey(
        exercise_id=exercise_id,
        load_notation=load_notation,  # type: ignore[arg-type]
        kg_key=_scaled_kg_key(load.value_text, load.unit),
    )


def optional_rir(rir: float | None) -> float | None:
    """RIR 校验：``None`` 表示未报告，原样返回；非负有限值放行（SQLite CHECK 是补充防线）。"""
    if rir is None:
        return None
    if isinstance(rir, bool) or not isinstance(rir, (int, float)):
        raise InvalidRecordFact(f"RIR 必须为数值或空：{rir!r}")
    if not math.isfinite(rir) or rir < 0:
        raise InvalidRecordFact(f"RIR 必须为非负有限数值：{rir!r}")
    return float(rir)


def validate_record_draft(payload: RecordDraftPayload) -> None:
    """记录草稿结构校验：只拒绝结构上不合法的事实，不要求必填事实完整。

    - 词汇与形态：``record_type`` 落在目录三类；``load_notation`` 只属于外加负重次数型
      （同 009 的库内 CHECK 与 002 目录口径），其余类型为空；``set_type``／``assistance``
      落在已拍词表，未明确时为 ``None``（不静默认定热身组、无辅助）；
    - 顺序连续：动作 ``position`` 与组 ``set_no`` 各自 1 起连续，修订内不重号（与
      ``exercise_logs``／``training_sets`` 的唯一约束同口径，读回顺序稳定）；
    - 数值：次数／时长／辅助次数 ≥ 1；RIR 非负有限或空；负重原文能换算（:func:`load_kg_key`）；
    - 开始时刻与精度同现同隐：精度描述的是已知的开始时刻，没有时刻就没有精度。

    必填事实是否完整不在此判定（D8：待补全载荷允许保存与确认），由
    :func:`record_draft_status` 派生状态。调用方负责目录引用（动作身份）与安排绑定（IO）。
    """
    if not isinstance(payload, RecordDraftPayload):
        raise InvalidRecordFact(f"需要记录草稿载荷结构：{type(payload).__name__}")
    if isinstance(payload.occurred_on, datetime) or not isinstance(
        payload.occurred_on, date
    ):
        raise InvalidRecordFact(f"实际发生日期必须是日期：{payload.occurred_on!r}")
    if payload.training_session_id is not None:
        _require_text("training_session_id", payload.training_session_id)
    if payload.arrangement_revision_id is not None:
        _require_text("arrangement_revision_id", payload.arrangement_revision_id)
    if (payload.started_at is None) != (payload.time_precision is None):
        raise InvalidRecordFact("开始时刻与时间精度必须同现同隐，不单独出现")
    if payload.time_precision is not None:
        if payload.time_precision not in TIME_PRECISIONS:
            raise InvalidRecordFact(
                f"时间精度不在已定词表内：{payload.time_precision!r}"
            )
        assert payload.started_at is not None
        try:
            parse_started_at("started_at", payload.started_at)
        except ValueError as exc:
            raise InvalidRecordFact(str(exc)) from exc
    if payload.feedback is not None and not isinstance(payload.feedback, dict):
        raise InvalidRecordFact(f"feedback 必须是 JSON 对象：{payload.feedback!r}")
    expected_positions = list(range(1, len(payload.exercises) + 1))
    if [item.position for item in payload.exercises] != expected_positions:
        raise InvalidRecordFact("动作顺序 position 必须从 1 起连续且不重号")
    for item in payload.exercises:
        _validate_exercise(item)


def _validate_exercise(item: DraftExerciseLog) -> None:
    if not isinstance(item, DraftExerciseLog):
        raise InvalidRecordFact(f"需要记录草稿动作结构：{type(item).__name__}")
    facts = item.facts
    if facts.record_type not in RECORD_TYPES:
        raise InvalidRecordFact(f"记录类型不在目录三类内：{facts.record_type!r}")
    if facts.record_type == "reps_weight":
        if facts.load_notation not in LOAD_CONVENTIONS:
            raise InvalidRecordFact(
                f"外加负重次数型必须带已拍负重口径：{facts.load_notation!r}"
            )
    elif facts.load_notation is not None:
        raise InvalidRecordFact(
            f"非外加负重次数型不得携带负重口径：{facts.load_notation!r}"
        )
    _require_text("exercise_id", facts.exercise_id)
    for label, value in (
        ("target_item_key", facts.target_item_key),
        ("warmup_summary_text", facts.warmup_summary_text),
    ):
        if value is not None:
            _require_text(label, value)
    expected_set_numbers = list(range(1, len(item.sets) + 1))
    if [single.set_no for single in item.sets] != expected_set_numbers:
        raise InvalidRecordFact("组序号 set_no 必须从 1 起连续且不重号")
    for single in item.sets:
        _validate_set(single)


def _validate_set(single: SetFacts) -> None:
    if not isinstance(single, SetFacts):
        raise InvalidRecordFact(f"需要记录草稿组结构：{type(single).__name__}")
    if single.set_type is not None and single.set_type not in SET_TYPES:
        raise InvalidRecordFact(f"组类型不在已拍集合内：{single.set_type!r}")
    if single.assistance is not None and single.assistance not in ASSISTANCE_VALUES:
        raise InvalidRecordFact(f"辅助标记不在已拍集合内：{single.assistance!r}")
    for label, value in (
        ("reps", single.reps),
        ("duration_seconds", single.duration_seconds),
        ("assisted_reps", single.assisted_reps),
    ):
        if value is not None and (isinstance(value, bool) or value < 1):
            raise InvalidRecordFact(f"{label} 必须为 ≥1 的整数或空：{value!r}")
    optional_rir(single.rir)
    if single.target_set_key is not None:
        _require_text("target_set_key", single.target_set_key)
    if single.quality_text is not None:
        _require_text("quality_text", single.quality_text)
    if single.load is not None:
        if not isinstance(single.load, RawLoad):
            raise InvalidRecordFact(f"负重必须是原始值结构：{single.load!r}")
        load_kg_key(single.load)


def _require_text(label: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidRecordFact(f"{label} 必须是非空文本：{value!r}")
    return value


def record_draft_status(payload: RecordDraftPayload) -> RecordRevisionStatus:
    """按必填事实完整性派生修订状态（05 5.2 转正门槛 B；D8 已拍 A）。

    ``valid`` 当且仅当：至少一个动作、每个动作至少一组、每组都有明确的组类型，且每组的
    完成数据按记录类型明确（外加负重次数型：重量与次数；自重次数型：次数；计时型：时长）。
    未明确的 RIR／质量／辅助／反馈不阻止 ``valid``（可选事实，不强迫编造）。
    其余情况为 ``incomplete``：允许确认承载已明确事实，但整条不进 PR 与完成率分子（D8）。
    """
    if not payload.exercises:
        return "incomplete"
    for item in payload.exercises:
        if not item.sets:
            return "incomplete"
        for single in item.sets:
            if single.set_type is None:
                return "incomplete"
            if not _set_completion_known(item, single):
                return "incomplete"
    return "valid"


def _set_completion_known(item: DraftExerciseLog, single: SetFacts) -> bool:
    """该组的完成数据是否已明确（按记录类型取各自必填的执行结果）。"""
    record_type = item.facts.record_type
    if record_type == "reps_weight":
        return single.load is not None and single.reps is not None
    if record_type == "reps_bodyweight":
        return single.reps is not None
    return single.duration_seconds is not None


def validate_record_draft_correction(
    stored: RecordDraftPayload, submitted: RecordDraftPayload
) -> None:
    """纠错白名单：相对存储稿，只有已明确事实可整体替换，归属与来源不可改写。

    不可变（05 5.2「归属：新增、补充或更正明确；目标不唯一必须询问」、5.1「关联可信的
    当次安排快照」）：

    - ``training_session_id``（训练身份归属）：同日多练时日期不唯一，改归属就是重新提问，
      必须丢弃后重新准备，不能借纠错静默换目标；
    - ``arrangement_revision_id``（安排关联快照）：记录绑定的是执行时依据的那份安排，
      换关联等于换依据；
    - ``schema_version``。

    可变：日期、开始时刻与精度、完成申报、回归期标记、反馈、全部动作与组事实（用户看到的
    最终草稿允许轻量纠错；完整事实复查由调用方在同一事务内做）。越界修改一律拒绝，不部分接受。
    """
    if not isinstance(stored, RecordDraftPayload) or not isinstance(
        submitted, RecordDraftPayload
    ):
        raise InvalidRecordFact(
            f"纠错对比需要两份记录草稿载荷结构：{type(stored).__name__} / "
            f"{type(submitted).__name__}"
        )
    if submitted.schema_version != stored.schema_version:
        raise InvalidRecordFact(
            f"纠错不可改载荷 schema_version：{stored.schema_version} → "
            f"{submitted.schema_version}"
        )
    if submitted.training_session_id != stored.training_session_id:
        raise InvalidRecordFact(
            f"纠错不可改训练身份归属：{stored.training_session_id!r} → "
            f"{submitted.training_session_id!r}"
        )
    if submitted.arrangement_revision_id != stored.arrangement_revision_id:
        raise InvalidRecordFact(
            f"纠错不可改安排关联快照：{stored.arrangement_revision_id!r} → "
            f"{submitted.arrangement_revision_id!r}"
        )
