"""统计确定性现算（S3-12）：完成率 Wn、组级三桶、PR 查询。

正本：stage3.md §5 S3-12、architecture/06 6.1–6.4。四条硬边界：

- **只现算、不落盘**：不建统计结果表、不缓存当前 PR 或其他统计值；更正／作废后下一次查询
  立即反映最新有效事实（06 6.3／6.4）。
- **只消费当前有效修订**：完成率分子与三桶都只读 ``training_sessions.current_revision_id``
  指向的修订（草稿不在业务表内、待补全与已作废当前修订被排除），旧修订不重复计入
  （06 6.3、05 5.3）。
- **分母来自已锁定应训练日程**：只算**有效锁定**（存储标记 ∪ 到期日期规则，复用
  :func:`~domain.plan.rules.session_lock_state`）且未取消的名额——没有打卡也保留、休息日
  不生成名额、未到期不在内；已过去才投影出的名额按同一到期规则计入，不另立政策。
  分母为零返回 ``None``（显示「暂无」，不是 0%，06 验收 2）。
- **「今天」按固定业务时区**（07 7.3）：``business_date`` 由调用方注入（生产取当刻业务时区
  日期，测试固定时钟），本模块不取系统「今天」。
"""

from datetime import date

import aiosqlite

from domain.plan.repo import PlanRepo, PlanVersionRecord, ScheduledSessionRecord
from domain.plan.rules import session_lock_state
from domain.plan.schema import InvalidPlanRow
from domain.records.repo import RecordRepo
from domain.records.schema import InvalidRecordRow
from domain.stats.repo import StatsRepo
from domain.stats.rules import judge_target_sets, plan_week_bounds
from domain.stats.schema import (
    BucketCounts,
    ReviewBasis,
    ReviewStatSnapshot,
    TargetJudgement,
    WeekCompletion,
)
from storage.db import Database


class StatsService:
    """统计只读用例编排（06 6.1–6.3）；不写库、不推进版本、不做传输层映射。"""

    def __init__(self, db: Database):
        self._db = db
        self._plans = PlanRepo(db)
        self._records = RecordRepo(db)
        self._prs = StatsRepo(db)

    async def weekly_completion(
        self,
        plan_version_id: str,
        week_no: int,
        *,
        business_date: date,
    ) -> WeekCompletion | None:
        """同一计划版本同一 Wn 的完成率（06 6.1）；没有已到期应训练次数即 None（「暂无」）。

        分子：分母集合内，当前有效修订明确申报完成且至少含一个实际工作组的日程数，**按日程
        去重**——同一次安排无论反馈几次最多贡献一次完成，无安排的额外训练不增加分子
        （06 6.1、验收 1／3）。计划版本不存在或该周无到期名额都是「暂无」，不返回 0%。
        """
        async with self._db.transaction() as conn:
            version = await self._plans.read_version_in_transaction(
                conn, plan_version_id
            )
            if version is None:
                return None
            completion, _ = await self._week_completion_in_transaction(
                conn, version=version, week_no=week_no, business_date=business_date
            )
            return completion

    async def review_basis(self, *, business_date: date) -> ReviewBasis:
        """复盘生成时冻结的确定性依据（06 6.4）：快照 + 精确来源修订 id。

        同一事务内现算：当前正式计划各已到期周的完成率与全部 PR 数值；来源修订 id 只包
        括真正参与快照的行（完成率分子命中的训练身份当前修订 + 进入 PR 快照的候选修订），
        去重后按固定顺序排列。无正式计划时 ``per_week`` 为空，但 PR 快照仍照常现算：
        「没有完成率」不等于「没有可解释的 PR」。

        本方法不写库、不推进版本；返回的快照就是保存时冻存的同一份数值（不再现算）。
        """
        per_week: list[WeekCompletion] = []
        source_ids: list[str] = []
        async with self._db.transaction() as conn:
            version = await self._plans.read_current_in_transaction(conn)
            if version is not None:
                week_no = 1
                while True:
                    week_start, _ = plan_week_bounds(version.starts_on, week_no)
                    if week_start >= version.review_on:
                        break
                    (
                        completion,
                        revision_ids,
                    ) = await self._week_completion_in_transaction(
                        conn,
                        version=version,
                        week_no=week_no,
                        business_date=business_date,
                    )
                    if completion is not None:
                        per_week.append(completion)
                        source_ids.extend(revision_ids)
                    week_no += 1
            prs, pr_source_ids = await self._prs.pr_values_in_transaction(conn)
        return ReviewBasis(
            snapshot=ReviewStatSnapshot(
                per_week=tuple(per_week),
                prs=prs,
            ),
            source_revision_ids=tuple(dict.fromkeys([*source_ids, *pr_source_ids])),
        )

    async def _week_completion_in_transaction(
        self,
        conn: aiosqlite.Connection,
        *,
        version: PlanVersionRecord,
        week_no: int,
        business_date: date,
    ) -> tuple[WeekCompletion | None, tuple[str, ...]]:
        """一个 Wn 的完成率 + 命中分子的训练身份当前修订 id（供复盘冻结来源）。"""
        week_start, week_end = plan_week_bounds(version.starts_on, week_no)
        sessions = await self._plans.list_sessions_in_transaction(conn, version.id)
        due = [
            session
            for session in sessions
            if _is_due_denominator_member(
                session,
                week_start=week_start,
                week_end=week_end,
                business_date=business_date,
            )
        ]
        if not due:
            return None, ()
        numerator = 0
        revision_ids: list[str] = []
        for session in due:
            arrangement_ids = (
                await self._plans.list_arrangement_revision_ids_in_transaction(
                    conn, session.id
                )
            )
            if not arrangement_ids:
                continue
            for arrangement_id in arrangement_ids:
                if await self._records.is_completed_arrangement_revision_in_transaction(
                    conn, arrangement_id
                ):
                    # 同一次安排最多贡献一次完成：命中即跳出，不按记录条数累计。
                    numerator += 1
                    record = await self._records.read_session_in_transaction(
                        conn, session.id
                    )
                    if record is not None and record.current_revision_id is not None:
                        revision_ids.append(record.current_revision_id)
                    break
        return (
            WeekCompletion(
                plan_version_id=version.id,
                week_no=week_no,
                week_start=week_start,
                week_end=week_end,
                numerator=numerator,
                denominator=len(due),
            ),
            tuple(revision_ids),
        )

    async def judge_session(self, session_id: str) -> TargetJudgement | None:
        """一次训练当前修订的组级三桶判定（06 6.2）；不作判定时返回 None。

        None 的三种情形：训练身份不存在、没有当前修订、当前修订已作废（作废后不再参与统计，
        06 6.4）。当前修订为 ``incomplete`` 时仍判定：组级判定与整条记录的正式有效性分开
        （06 6.2），待补全记录不进 PR 但组仍可显示「待补全」。

        判定基准是该次**执行时所依据的**当次安排快照（05 5.1、S3-08 交接⑦），没有对照安排
        时 ``has_comparison`` 为假、三桶全零：无对照不判定，不要求补造目标（06 6.2）。
        """
        async with self._db.transaction() as conn:
            session = await self._records.read_session_in_transaction(conn, session_id)
            if session is None or session.current is None:
                return None
            if session.current.status == "voided":
                return None
            payload = await self._records.read_current_payload_in_transaction(
                conn, session_id
            )
            if payload is None:  # 身份已在上面读到，读不到载荷即数据损坏
                raise InvalidRecordRow(f"训练身份没有当前修订：{session_id}")
            if payload.arrangement_revision_id is None:
                return TargetJudgement(
                    session_revision_id=session.current.id,
                    has_comparison=False,
                    is_return_phase=payload.is_return_phase,
                    counts=BucketCounts(),
                )
            arrangement = await self._plans.read_arrangement_revision_in_transaction(
                conn, payload.arrangement_revision_id
            )
            if arrangement is None:
                # 记录侧外键指向的安排修订必须存在；读不到即数据损坏，不静默当「无对照」。
                raise InvalidPlanRow(
                    f"记录绑定的安排修订不存在：{payload.arrangement_revision_id}"
                )
            return TargetJudgement(
                session_revision_id=session.current.id,
                has_comparison=True,
                is_return_phase=payload.is_return_phase,
                counts=judge_target_sets(arrangement.target, payload.exercises),
            )

    async def pr_max_load(self, *, exercise_id: str, load_notation: str) -> int | None:
        """该动作与负重口径的现算最高重量（换算整数键）；无候选即 None（06 6.3）。"""
        return await self._prs.pr_max_load(
            exercise_id=exercise_id, load_notation=load_notation
        )

    async def pr_max_reps_at_load(
        self, *, exercise_id: str, load_notation: str, load_kg_key: int
    ) -> int | None:
        """该动作、口径与重量下的现算单组最高次数；同重量不累计多组（06 验收 7）。"""
        return await self._prs.pr_max_reps_at_load(
            exercise_id=exercise_id,
            load_notation=load_notation,
            load_kg_key=load_kg_key,
        )


def _is_due_denominator_member(
    session: ScheduledSessionRecord,
    *,
    week_start: date,
    week_end: date,
    business_date: date,
) -> bool:
    """该名额是否进入 Wn 的完成率分母（06 6.1、04 4.2）。

    - 落在计划周 ``[week_start, week_end)`` 内；
    - **有效锁定**：存储锁定标记与到期日期规则取并集（复用 ``session_lock_state``）；未到期
      的未来名额不进分母（已过去才投影出的名额按同一到期规则计入，不另立政策）；
    - 已取消的名额不是应训练义务：04 4.2 只允许取消未锁定的未来日程，取消后不再重新进入
      分母（历史漏练不因后续变更消失的是「已锁定」的那些，不是取消的那些）。
    """
    if not week_start <= session.scheduled_on < week_end:
        return False
    if session.cancelled_at is not None:
        return False
    return session_lock_state(
        session.scheduled_on,
        stored_locked_at=session.locked_at,
        business_date=business_date,
    ).effective
