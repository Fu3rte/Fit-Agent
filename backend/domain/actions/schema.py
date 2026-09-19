"""actions 类型定义：动作目录一行身份。"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

RecordType = Literal["reps_weight", "reps_bodyweight", "time"]

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
    """动作目录的一行身份。"""

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
