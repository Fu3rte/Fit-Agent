"""actions 类型定义：动作身份、别名候选与解析结果（正本 architecture/03 3.1）。

``Exercise`` 是动作目录的一条身份：停用（``active=False``）后仍保留别名、负重口径
与可推荐标记等语义（03「本章已拍结论」：停用不删除）。``AliasResolution`` 保留
全部候选，不静默取第一项（03 3.1）。
"""

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
]
MatchKind = Literal["standard_name", "alias"]


class InvalidCatalogRow(ValueError):
        """exercises 行的 JSON 列无法解析：目录数据损坏（库内 CHECK 已保证 json_valid）。"""


def _json_array(raw: object) -> tuple[str, ...]:
        """解析 JSON 数组列；非法值视为目录数据损坏，不静默吞掉。"""
        try:
                values = json.loads(str(raw))
        except (TypeError, ValueError) as exc:
                raise InvalidCatalogRow(f"目录 JSON 列损坏: {raw!r}") from exc
        return tuple(str(value) for value in values)


@dataclass(frozen=True, slots=True)
class Exercise:
        """动作目录的一行身份（不含任何媒体字段：媒体整体移出本阶段）。"""

        id: str
        standard_name_zh: str
        equipment_variant: str
        record_type: RecordType
        load_convention: LoadConvention | None
        unilateral: bool
        recommendable: bool
        active: bool
        aliases: tuple[str, ...]
        modes: tuple[str, ...]
        source_ref: str
        attribution: str
        instructions_zh: str | None

        @classmethod
        def from_row(cls, row: Mapping[str, Any]) -> "Exercise":
                """把 exercises 行映射为身份；JSON 列在此反序列化为元组。"""
                load_convention = row["load_convention"]
                return cls(
                        id=str(row["id"]),
                        standard_name_zh=str(row["standard_name_zh"]),
                        equipment_variant=str(row["equipment_variant"]),
                        # 库内 CHECK 已保证取值属于三类记录口径与五种负重口径；此处按列语义收敛类型。
                        record_type=cast(RecordType, row["record_type"]),
                        load_convention=(
                                None
                                if load_convention is None
                                else cast(LoadConvention, str(load_convention))
                        ),
                        unilateral=bool(row["unilateral"]),
                        recommendable=bool(row["recommendable"]),
                        active=bool(row["active"]),
                        aliases=_json_array(row["aliases_json"]),
                        modes=_json_array(row["modes_json"]),
                        source_ref=str(row["source_ref"]),
                        attribution=str(row["attribution"]),
                        instructions_zh=(
                                None
                                if row["instructions_zh"] is None
                                else str(row["instructions_zh"])
                        ),
                )


@dataclass(frozen=True, slots=True)
class AliasCandidate:
        """一次查询命中：身份 + 命中文本 + 命中空间（标准名 / 别名）。"""

        exercise: Exercise
        matched_text: str
        match_kind: MatchKind


@dataclass(frozen=True, slots=True)
class AliasResolution:
        """别名／标准名解析结果：保留全部候选，命中多个身份时由调用方询问用户。"""

        term: str
        candidates: tuple[AliasCandidate, ...]

        @property
        def is_ambiguous(self) -> bool:
                return len(self.candidates) > 1

        @property
        def exercises(self) -> tuple[Exercise, ...]:
                return tuple(candidate.exercise for candidate in self.candidates)
