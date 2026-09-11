"""stats 确定性规则：校验与判定，纯函数不碰 IO（正本 architecture/06）。

三条已定口径：

- **完成率按计划周 Wn**（06 6.1）：W1 是计划版本 ``starts_on`` 起连续 7 天，W2 随后 7 天，
  不使用全局「周一至周日」；本模块只给周边界，分母口径（已到期、锁定、未取消）在服务层。
- **三桶判定顺序**（06 6.2「三桶判定顺序」已拍 B）：存在已知未满足的目标即为未符合（可同时
  提示缺失字段），否则缺必要判定信息为待补全，全部适用目标满足才为符合目标。
- **闭区间、无隐藏容差**（06 6.2 RIR 边界已拍 C）：次数与 RIR 都按显式闭区间判定，包含端点，
  单值按同值上下界处理；判定基准是当次安排快照，不是计划版本、不事后改标准。

另加复盘（06 6.4）的两条纯规则：保存前的正文／引用形状校验，以及「依据是否已变更」的现算
判定（所引修订不再是该训练的当前修订）。两者都不改写正文与快照。
"""

from collections.abc import Mapping, Sequence
from datetime import date, timedelta

from domain.plan.schema import (
    ArrangementTarget,
    IntRange,
    PlanExerciseItem,
    RepsPrescription,
)
from domain.records.schema import DraftExerciseLog
from domain.stats.schema import BucketCounts, ReviewSourceRevisions

# 三种组级状态（06 6.2）：工作台与复盘共用同一计算结果与同一词汇，不另设同义状态。
BUCKET_UNMET = "unmet"
BUCKET_INCOMPLETE = "incomplete"
BUCKET_FIT = "fit"
# 判定顺序：已知未满足优先 → 缺必要判定信息 → 全部满足（06 6.2）。
BUCKET_ORDER: tuple[str, ...] = (BUCKET_UNMET, BUCKET_INCOMPLETE, BUCKET_FIT)

WEEK_LENGTH_DAYS = 7


def plan_week_bounds(starts_on: date, week_no: int) -> tuple[date, date]:
    """计划周 Wn 的 ``[start, end)``：W1 = ``starts_on`` 起连续 7 天（06 6.1）。

    计划版本从周四开始时，W1 就是该周四至下一周三、W2 从下一周四开始——按版本开始日期
    派生，不吸附自然周。周序号非法即拒绝，不静默取 W1。
    """
    if isinstance(week_no, bool) or not isinstance(week_no, int) or week_no < 1:
        raise ValueError(f"周序号必须是 >= 1 的整数：{week_no!r}")
    start = starts_on + timedelta(days=WEEK_LENGTH_DAYS * (week_no - 1))
    return start, start + timedelta(days=WEEK_LENGTH_DAYS)


def judge_work_set(
    *,
    reps: int | None,
    rir: float | None,
    reps_range: IntRange,
    target_rir: IntRange | None,
) -> str:
    """单组相对当次目标的状态（06 6.2）：已知未满足优先，其次缺必要事实，最后满足。

    - 次数越出已确认区间（超过上界不算符合）或 RIR 越出显式区间即为**已知未满足**；
    - 除已知未满足外，次数未记录、或处方有 RIR 目标而 RIR 未记录，为**待补全**：缺必要
      判定事实不当作满足，也不当作未符合（06 6.2「不能只因缺失信息判为未符合」）；
    - 处方没有 RIR 目标时 RIR 不是必要判定事实（次数型处方未必带 RIR 参考）。
    """
    known_unmet = False
    missing = False
    if reps is None:
        missing = True
    elif not _within(reps_range, reps):
        known_unmet = True
    if target_rir is not None:
        if rir is None:
            missing = True
        elif not _within(target_rir, rir):
            known_unmet = True
    if known_unmet:
        return BUCKET_UNMET
    if missing:
        return BUCKET_INCOMPLETE
    return BUCKET_FIT


def judge_target_sets(
    target: ArrangementTarget, exercises: Sequence[DraftExerciseLog]
) -> BucketCounts:
    """按当次安排快照判定该修订全部**工作组**的三桶组数（06 6.2）。

    - 判定基准是记录**执行时所依据的**当次安排快照，不读计划版本、不随后续计划变化改写
      （06 6.2、05 5.1）。
    - 只有组类型明确为工作组、且能对应到安排目标项（``target_item_key`` 命中 ``item_key``）
      的组进三桶：热身组、组类型未明确的组与无对照动作（额外组）都不进，不进三桶的组单独
      展示实际表现，不以「待补全」为由要求补造目标（06 6.2）。
    - 计时型处方不适用次数＋RIR 判定，同样不进三桶（06 6.2）。
    """
    items: Mapping[str, PlanExerciseItem] = {
        item.item_key: item for item in target.exercises
    }
    unmet = 0
    incomplete = 0
    fit = 0
    for exercise in exercises:
        item_key = exercise.facts.target_item_key
        if item_key is None:
            continue
        item = items.get(item_key)
        if item is None or not isinstance(item.prescription, RepsPrescription):
            continue
        for single in exercise.sets:
            if single.set_type != "work":
                continue
            bucket = judge_work_set(
                reps=single.reps,
                rir=single.rir,
                reps_range=item.prescription.reps_range,
                target_rir=item.prescription.target_rir,
            )
            if bucket == BUCKET_UNMET:
                unmet += 1
            elif bucket == BUCKET_INCOMPLETE:
                incomplete += 1
            else:
                fit += 1
    return BucketCounts(unmet=unmet, incomplete=incomplete, fit=fit)


def _within(target: IntRange, value: int | float) -> bool:
    """显式闭区间判定：包含端点，无隐藏容差（06 6.2 RIR 边界已拍 C）。"""
    return target.min <= value <= target.max


class InvalidReviewContent(Exception):
    """待保存的复盘内容本身不合法：正文为空或来源修订引用形状非法。

    与 :class:`~domain.stats.schema.InvalidReviewRow` 分开：这是**保存前**拒绝的调用方错误，
    那边是**已经存进库**的损坏行。
    """


def validate_review_content(
    *, body_markdown: str, source_revision_ids: Sequence[str]
) -> None:
    """保存前的正文与引用形状校验（06 6.4）；不碰库，引用是否存在由应用层在同一事务内查。

    - 正文：Markdown 非空白（空正文不是复盘，不允许生成一条没有内容的记录）。
    - 引用：非空文本、同一修订不得重复引用（重复只会把同一笔来源数两遍，不增加依据）。

    允许引用为空：尚无任何训练修订时（例如只在分母上取完成率）确实没有来源修订，不伪造引用。
    """
    if not isinstance(body_markdown, str) or not body_markdown.strip():
        raise InvalidReviewContent(f"复盘正文必须是非空白 Markdown：{body_markdown!r}")
    for revision_id in source_revision_ids:
        if not isinstance(revision_id, str) or not revision_id:
            raise InvalidReviewContent(f"来源修订引用必须是非空文本：{revision_id!r}")
    if len(set(source_revision_ids)) != len(source_revision_ids):
        raise InvalidReviewContent(
            f"来源修订引用不得重复：{list(source_revision_ids)!r}"
        )


def review_basis_changed(references: Sequence[ReviewSourceRevisions]) -> bool:
    """复盘依据是否已变更（06 6.4）：任一所引修订不再是该训练身份的当前修订即为已变更。

    更正追加新修订并切换当前指针、作废同样追加 ``voided`` 修订并切换（05 5.3），两者都使
    所引修订不再「当前」；读旧复盘时据此**现算** stale，不回写正文与快照（06 6.4 不静默
    改写）。没有任何来源修订（该次复盘没有训练依据）时无从对比，恒为未变更。
    """
    return any(
        reference.current_revision_id != reference.session_revision_id
        for reference in references
    )
