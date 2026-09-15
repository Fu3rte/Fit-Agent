"""body_metrics 类型定义：一条身体指标事实（讨论总结 §9、REFACTOR_PLAN §5.3；Stage 1 子任务 02 §6）。

与 ``body_metrics`` 表列一一对应：发生日期（业务时区自然日）、体重（必填）、体脂（可选）。
两条硬边界：

- **日期是日期对象**：``measured_on`` 用 ``datetime.date``，由调用方按业务时区算好后注入；
  repo 与领域服务不得自行取「今天」（REFACTOR_PLAN §5.5：禁止 ``date.today()``）。
- **无数据保持空值**：体脂未记录时为 ``None``／库内 NULL，不补 0，也不与其他日期的值比较。
"""

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
