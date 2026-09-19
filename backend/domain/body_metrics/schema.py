"""body_metrics 类型定义：一条身体指标事实。"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True, slots=True)
class BodyMetric:
    """一条身体指标事实；``body_fat_pct`` 为 None 表示该次未记录体脂。"""

    id: int
    measured_on: date
    weight_kg: float
    body_fat_pct: float | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "BodyMetric":
        body_fat = row["body_fat_pct"]
        return cls(
            id=int(row["id"]),
            measured_on=date.fromisoformat(str(row["measured_on"])),
            weight_kg=float(row["weight_kg"]),
            body_fat_pct=None if body_fat is None else float(body_fat),
        )
