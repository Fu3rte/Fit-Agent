"""actions 类型定义：动作目录一行身份（正本 storage/migrations/001_initial.sql、讨论总结 §9、REFACTOR_PLAN §5.2）。

``Exercise`` 与 ``exercises`` 表列一一对应：稳定身份 ``id``（PB 分组与计划渐进的确定性来源）、
中文标准名、器械变式、记录口径、负重口径、最小加重单位、是否可用于计划、动作模式、来源与
许可。旧实现里的别名候选、停用标记、单侧标记、计划模板与肌群／媒体字段随旧目录语义一并
删除（Stage 1 子任务 02 §4：稳定 ID 与明确负重口径是唯一需要保留的目录语义）。

本模块只做结构解码：JSON 列解析失败视为目录数据损坏（库内 CHECK 只保证 ``json_valid``，
不保证元素），大声失败不静默兜底；13 项模式词表校验归 ``domain.actions.rules``。
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

#: 记录口径（讨论总结 §9 只三类；库内 CHECK 同集合）。
RecordType = Literal["reps_weight", "reps_bodyweight", "time"]

#: 负重口径（仅外加负重类型需要；库内 CHECK 同集合）。
#: ``external_added_weight`` 是独立负重引体的「外加重量（不含体重）」口径。
LoadConvention = Literal[
    "barbell_includes_bar_total",
    "dumbbell_per_hand",
    "machine_pin_displayed_value",
    "plate_loaded_total_excluding_empty",
    "unilateral_setting_per_side",
    "external_added_weight",
]


class InvalidCatalogRow(ValueError):
    """exercises 行的 JSON 列无法解析为文本数组：目录数据损坏。"""


def _json_array(raw: object) -> tuple[str, ...]:
    """解析 JSON 数组列；非法值视为目录数据损坏，不静默吞掉。"""
    try:
        values = json.loads(str(raw))
    except (TypeError, ValueError) as exc:
        raise InvalidCatalogRow(f"目录 JSON 列损坏: {raw!r}") from exc
    if not isinstance(values, list):
        raise InvalidCatalogRow(f"目录 JSON 列不是数组: {raw!r}")
    return tuple(str(value) for value in values)


@dataclass(frozen=True, slots=True)
class Exercise:
    """动作目录的一行身份。

    ``load_convention``／``min_load_increment_kg`` 只有外加负重类型携带：自重／计时型为
    ``None``，不虚构 0kg 或 0 加重（库内 CHECK 同样强制）。
    """

    id: str
    standard_name_zh: str
    equipment_variant: str
    record_type: RecordType
    load_convention: LoadConvention | None
    min_load_increment_kg: float | None
    recommendable: bool
    modes: tuple[str, ...]
    source_ref: str
    attribution: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Exercise":
        """把 exercises 行映射为身份；``modes_json`` 在此反序列化（元素校验归 rules）。"""
        load_convention = row["load_convention"]
        increment = row["min_load_increment_kg"]
        return cls(
            id=str(row["id"]),
            standard_name_zh=str(row["standard_name_zh"]),
            equipment_variant=str(row["equipment_variant"]),
            # 库内 CHECK 已保证取值属于三类记录口径与六种负重口径；此处按列语义收敛类型。
            record_type=cast(RecordType, row["record_type"]),
            load_convention=(
                None
                if load_convention is None
                else cast(LoadConvention, str(load_convention))
            ),
            min_load_increment_kg=None if increment is None else float(increment),
            recommendable=bool(row["recommendable"]),
            modes=_json_array(row["modes_json"]),
            source_ref=str(row["source_ref"]),
            attribution=str(row["attribution"]),
        )
