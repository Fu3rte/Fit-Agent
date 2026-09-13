"""stats 业务类型定义（正本 architecture/06 6.1–6.4）。

统计一律**现算**：不落当前统计结果作为事实源（06 6.3／6.4），因此这里只有查询结果类型，
没有表行类型。分母为零的「暂无」用 ``None`` 表达，不构造 0% 或 100%（06 验收 2）。

复盘（06 6.4）是唯一的例外形态：它**保存**生成时的 Markdown 正文与统计依据快照，但快照只
作历史解释证据，不参与当前统计；当前统计读数一律重新现算。
"""

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

__all__ = [
    "BucketCounts",
    "InvalidReviewRow",
    "PrValue",
    "ReviewSourceRevisions",
    "ReviewStatSnapshot",
    "TargetJudgement",
    "WeekCompletion",
    "review_basis_from_json",
    "review_basis_to_json",
]

# 快照 JSON 的契约版本：结构变化才推进，未知版本大声失败（与计划载荷／记录草稿同一口径）。
REVIEW_BASIS_SCHEMA_VERSION = 1


class InvalidReviewRow(Exception):
    """库内复盘行本身不合法：快照 JSON 解不开、结构或取值不符契约。

    与 :class:`~domain.records.schema.InvalidRecordRow` 同口径：读到的正式行损坏即显式失败，
    不静默兜底成空快照（那会把「依据不明」伪装成「无依据」）。
    """


@dataclass(frozen=True, slots=True)
class WeekCompletion:
    """一个计划版本某一 Wn 的完成率（06 6.1）：已完成计划训练次数 ÷ 已到期应训练次数。

    调用方拿到本类型即为「有应训练次数」；分母为零时服务返回 ``None``（显示「暂无」），
    不返回 0% 或 100%。``week_start``／``week_end`` 是 ``[start, end)`` 的计划周边界。
    """

    plan_version_id: str
    week_no: int
    week_start: date
    week_end: date
    numerator: int
    denominator: int


@dataclass(frozen=True, slots=True)
class PrValue:
    """生成时冻结的一笔现算 PR 数值（06 6.3）：与现算查询同一比较键与取值口径。

    ``load_kg_key`` 是换算整数键（kg×1000），``best_reps`` 是该重量下的**单组**最高次数
    （同重量不累计多组，06 验收 7）。冻结的是数值本身，读旧复盘时不重算。
    """

    exercise_id: str
    load_notation: str
    load_kg_key: int
    best_reps: int


@dataclass(frozen=True, slots=True)
class ReviewStatSnapshot:
    """复盘生成时的统计依据快照（06 6.4）：冻结当时数值，**不作为当前统计输入**。

    两个组成部分都是当时已现算出来的确定性结果：计划周完成率（``per_week``）与 PR 数值
    （``prs``）。读旧复盘只回放这些数值，不重新计算、也不把它们回填进统计查询。
    """

    per_week: tuple[WeekCompletion, ...] = ()
    prs: tuple[PrValue, ...] = ()


@dataclass(frozen=True, slots=True)
class ReviewBasis:
    """复盘生成时冻结的确定性依据（06 6.4）：统计快照 + 精确来源修订 id。

    由 :meth:`~domain.stats.service.StatsService.review_basis` 在**同一事务**内现算并冻结；
    模型只收到这些事实写解释正文，保存时 ``ReviewStore`` 再校验来源修订仍是当前修订。
    """

    snapshot: ReviewStatSnapshot = ReviewStatSnapshot()
    source_revision_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReviewSourceRevisions:
    """一条来源修订引用的**读取**结果：所引修订 + 该训练身份的当刻当前修订。

    ``current_revision_id`` 与 ``session_revision_id`` 不等即为该笔来源已变更：更正追加新
    修订并切换当前指针、作废同样追加 ``voided`` 修订并切换（05 5.3），两者都使所引修订不再
    是「当前」。判定规则见 :func:`~domain.stats.rules.review_basis_changed`。
    """

    session_revision_id: str
    session_id: str
    current_revision_id: str | None


def review_basis_to_json(snapshot: ReviewStatSnapshot) -> str:
    """快照 → 存库 JSON 文本（06 6.4）；只序列化已定义字段，不夹带其他统计口径。"""
    payload: dict[str, Any] = {
        "schema_version": REVIEW_BASIS_SCHEMA_VERSION,
        "per_week": [
            {
                "plan_version_id": item.plan_version_id,
                "week_no": item.week_no,
                "week_start": item.week_start.isoformat(),
                "week_end": item.week_end.isoformat(),
                "numerator": item.numerator,
                "denominator": item.denominator,
            }
            for item in snapshot.per_week
        ],
        "prs": [
            {
                "exercise_id": item.exercise_id,
                "load_notation": item.load_notation,
                "load_kg_key": item.load_kg_key,
                "best_reps": item.best_reps,
            }
            for item in snapshot.prs
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def review_basis_from_json(text: str) -> ReviewStatSnapshot:
    """存库 JSON 文本 → 快照；任何不符契约的形态都抛 :class:`InvalidReviewRow`。

    ``schema_version`` 不是本版本即拒绝（不按旧结构猜测），缺字段、类型错、日期非法同样拒绝。
    """
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidReviewRow(f"复盘快照不是合法 JSON：{text!r}") from exc
    if not isinstance(raw, dict):
        raise InvalidReviewRow(f"复盘快照必须是 JSON 对象：{raw!r}")
    version = raw.get("schema_version")
    if version != REVIEW_BASIS_SCHEMA_VERSION:
        raise InvalidReviewRow(
            f"复盘快照 schema_version 只支持 {REVIEW_BASIS_SCHEMA_VERSION}：{version!r}"
        )
    return ReviewStatSnapshot(
        per_week=tuple(
            _week_from_json(item) for item in _require_list(raw, "per_week")
        ),
        prs=tuple(_pr_from_json(item) for item in _require_list(raw, "prs")),
    )


def _week_from_json(raw: object) -> WeekCompletion:
    item = _require_object(raw, "per_week")
    return WeekCompletion(
        plan_version_id=_require_text(item, "plan_version_id"),
        week_no=_require_int(item, "week_no"),
        week_start=_require_date(item, "week_start"),
        week_end=_require_date(item, "week_end"),
        numerator=_require_int(item, "numerator"),
        denominator=_require_int(item, "denominator"),
    )


def _pr_from_json(raw: object) -> PrValue:
    item = _require_object(raw, "prs")
    return PrValue(
        exercise_id=_require_text(item, "exercise_id"),
        load_notation=_require_text(item, "load_notation"),
        load_kg_key=_require_int(item, "load_kg_key"),
        best_reps=_require_int(item, "best_reps"),
    )


def _require_list(raw: dict[str, Any], field: str) -> list[Any]:
    value = raw.get(field)
    if not isinstance(value, list):
        raise InvalidReviewRow(f"复盘快照 {field} 必须是数组：{value!r}")
    return value


def _require_object(raw: object, field: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise InvalidReviewRow(f"复盘快照 {field} 的元素必须是对象：{raw!r}")
    return raw


def _require_text(raw: dict[str, Any], field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value:
        raise InvalidReviewRow(f"复盘快照 {field} 必须是非空文本：{value!r}")
    return value


def _require_int(raw: dict[str, Any], field: str) -> int:
    value = raw.get(field)
    # bool 是 int 的子类但不表示数量，显式拒绝
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidReviewRow(f"复盘快照 {field} 必须是整数：{value!r}")
    return value


def _require_date(raw: dict[str, Any], field: str) -> date:
    value = _require_text(raw, field)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidReviewRow(f"复盘快照 {field} 不是 ISO 日期：{value!r}") from exc


@dataclass(frozen=True, slots=True)
class BucketCounts:
    """组级三桶组数（06 6.2）：未符合／待补全／符合目标；展示顺序见 ``rules.BUCKET_ORDER``。"""

    unmet: int = 0
    incomplete: int = 0
    fit: int = 0


@dataclass(frozen=True, slots=True)
class TargetJudgement:
    """一次训练**当前**修订的组级判定结果（06 6.2、6.4）。

    ``has_comparison`` 为假表示该次没有可对照的当次安排：无对照不判定，只展示实际表现，
    不以「待补全」为由要求补造目标（06 6.2）。``is_return_phase`` 为真表示按接回安排单独
    计算，不得混入常规「处方目标符合情况」对比（06 6.4）。
    """

    session_revision_id: str
    has_comparison: bool
    is_return_phase: bool
    counts: BucketCounts
