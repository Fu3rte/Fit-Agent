"""plans 类型定义：计划版本行与计划日程行（讨论总结 §9、REFACTOR_PLAN §5.5；Stage 1 子任务 02 §8）。

与 ``plans``／``plan_sessions`` 表列一一对应。三条硬边界：

- **只读**：本阶段只有读取结构，没有草稿、确认或激活结构；计划创建与激活事务留到 Stage 5。
- **JSON 列按不透明合法 JSON 读取**：``structured_content`` 与 ``evaluator_result`` 只解码成
  通用 Python 对象，不校验任何计划载荷形状（4A 裁剪版形状留到计划链路阶段）；库内 CHECK 保证
  ``json_valid``，解码失败即数据损坏，大声失败不静默兜底。
- **可追溯与单调**：``source_plan_id`` 指向生成该版本所基于的旧计划（首个计划为 None），
  ``version`` 只追加、恰好 +1（由 Stage 5 的确认事务保证，库内 UNIQUE 兜底）。
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, cast

#: 计划状态（讨论总结 §3.3：draft → active → archived；库内 CHECK 同集合）。
PlanStatus = Literal["draft", "active", "archived"]
PLAN_STATUSES: tuple[PlanStatus, ...] = ("draft", "active", "archived")


class InvalidPlanRow(ValueError):
    """plans／plan_sessions 行无法解析（JSON 列损坏或状态越界）：数据损坏，不静默吞掉。"""


def _json_value(label: str, raw: object) -> Any:
    """JSON 列 → 通用对象；只解码形状，不解释计划语义。"""
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError) as exc:
        raise InvalidPlanRow(f"{label} JSON 列损坏：{raw!r}") from exc


def _optional_text(raw: object) -> str | None:
    return None if raw is None else str(raw)


@dataclass(frozen=True, slots=True)
class Plan:
    """一条计划版本行（含历史版本）；状态决定它是 active／draft／archived。"""

    id: int
    version: int
    status: PlanStatus
    source_plan_id: int | None
    structured_content: Any
    evaluator_result: Any | None
    created_at: str
    confirmed_at: str | None
    archived_at: str | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Plan":
        status = str(row["status"])
        if status not in PLAN_STATUSES:
            raise InvalidPlanRow(f"计划状态非法：{status!r}")
        raw_evaluator = row["evaluator_result"]
        source_plan_id = row["source_plan_id"]
        return cls(
            id=int(row["id"]),
            version=int(row["version"]),
            status=cast(PlanStatus, status),
            source_plan_id=None if source_plan_id is None else int(source_plan_id),
            structured_content=_json_value("structured_content", row["structured_content"]),
            evaluator_result=(
                None
                if raw_evaluator is None
                else _json_value("evaluator_result", raw_evaluator)
            ),
            created_at=str(row["created_at"]),
            confirmed_at=_optional_text(row["confirmed_at"]),
            archived_at=_optional_text(row["archived_at"]),
        )


@dataclass(frozen=True, slots=True)
class PlanSession:
    """一条计划日程；``cancelled_at`` 非空表示该日程已取消（行不物理删除，历史保留）。"""

    id: int
    plan_id: int
    scheduled_on: date
    cancelled_at: str | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "PlanSession":
        return cls(
            id=int(row["id"]),
            plan_id=int(row["plan_id"]),
            scheduled_on=date.fromisoformat(str(row["scheduled_on"])),
            cancelled_at=_optional_text(row["cancelled_at"]),
        )
