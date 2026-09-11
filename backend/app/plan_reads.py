"""计划与日程只读投影（S3-07）：当前／历史版本、存储与生效锁定、整份计划安全复核。

正本：stage3.md §5 S3-07、§4.3（日程锁定与「今天」）、04 4.2/4.5。四条硬边界：

- **按固定业务时区日期判定锁定**（07 7.3）：判定入口是 :func:`~domain.plan.rules.session_lock_state`，
  ``business_date`` 由调用方按固定业务时区算出并**注入**（生产取当刻，测试固定时钟），
  本模块不取「今天」、不读系统时区，也不写任何锁定标记——到期即锁不需要后台任务。
- **存储与生效锁定同时暴露**：每条日程给出 ``lock.stored``（存储标记）与
  ``lock.by_business_date``（到期判定）及两者的并集 ``lock.effective``；只读标记会把停机
  跨过训练日、未写标记的到期日程误报成未锁定（04 4.2）。
- **历史仍可查看**：当前计划与历史版本用同一投影形状，``is_current`` 区分；已取消日程保留
  在投影里（行不物理删除），旧版本不重激活。
- **只读**：不写正式事实、不取消日程、不推进版本，也不做传输层映射（HTTP 与 DTO 归 S3-14）。
  「基于计划的指导」前置复核（04 4.5）由 :meth:`PlanReadService.read_current_plan_guidance`
  给出：整份计划按最新限制与红旗确定性复核，阻断时只返回原因，不修改任何数据。
"""

from dataclasses import dataclass
from datetime import date

import aiosqlite

from domain.actions.repo import ExerciseRepo
from domain.actions.schema import Exercise
from domain.plan.repo import PlanRepo, PlanVersionRecord, ScheduledSessionRecord
from domain.plan.rules import SessionLockState, session_lock_state
from domain.plan.schema import InvalidPlanRow, PlanPayload
from domain.plan.service import PlanSafetyRecheck, evaluate_plan_safety
from domain.profile.repo import ProfileRepo
from storage.db import Database


@dataclass(frozen=True, slots=True)
class ScheduledSessionView:
    """一条应训练名额的只读投影：行数据 + 存储锁定与生效锁定。

    ``cancelled`` 为真时该名额不再是应训练义务（行仍保留历史）；``lock.effective`` 是
    「不得改期或删除」的依据，存储标记与到期日期规则取并集（04 4.2）。
    """

    session: ScheduledSessionRecord
    lock: SessionLockState

    @property
    def cancelled(self) -> bool:
        return self.session.cancelled_at is not None


@dataclass(frozen=True, slots=True)
class PlanScheduleView:
    """一个计划版本的只读投影：版本行 + 是否当前 + 全部日程（含已取消与已锁定）。

    ``sessions`` 按应训练日排序；``is_current`` 由当刻正式计划（最新版本）判定，历史版本
    恒为 False。展示字段（weekly 安排、处方文案）由 DTO 层从 ``version.payload`` 派生，
    本投影不重复展开。
    """

    version: PlanVersionRecord
    is_current: bool
    sessions: tuple[ScheduledSessionView, ...]


@dataclass(frozen=True, slots=True)
class PlanGuidance:
    """「基于计划的指导」前置复核结果：当前计划投影 + 整份计划安全复核（04 4.5）。

    ``safety.is_blocked`` 为真时整份计划不得作为可执行训练建议，计划与历史仍可查看；
    本结果不做任何写入，也不产生替代处方。
    """

    plan: PlanScheduleView
    safety: PlanSafetyRecheck


class PlanReadService:
    """当前／历史计划与日程只读投影、整份计划安全复核（S3-07）；不写库、不推进版本。"""

    def __init__(self, db: Database):
        self._db = db
        self._plans = PlanRepo(db)
        self._profiles = ProfileRepo(db)
        self._exercises = ExerciseRepo(db)

    async def read_current_plan(
        self, *, business_date: date
    ) -> PlanScheduleView | None:
        """当前正式计划及其日程投影；尚无正式计划时返回 None（不是空版本）。"""
        async with self._db.transaction() as conn:
            current = await self._plans.read_current_in_transaction(conn)
            if current is None:
                return None
            return await self._build_view(
                conn, current, is_current=True, business_date=business_date
            )

    async def read_plan_version(
        self, plan_version_id: str, *, business_date: date
    ) -> PlanScheduleView | None:
        """按身份读取指定计划版本（含历史版本）；不存在即 None。

        历史版本不重激活、不改写：仅当它与当刻正式计划同一身份时 ``is_current`` 为真。
        """
        async with self._db.transaction() as conn:
            version = await self._plans.read_version_in_transaction(
                conn, plan_version_id
            )
            if version is None:
                return None
            current = await self._plans.read_current_in_transaction(conn)
            return await self._build_view(
                conn,
                version,
                is_current=current is not None and current.id == version.id,
                business_date=business_date,
            )

    async def read_current_plan_guidance(
        self, *, business_date: date
    ) -> PlanGuidance | None:
        """请求「基于计划的指导」前的整份计划安全复核；尚无正式计划时返回 None。

        复核按**最新**正式条件（当刻档案的限制与身体情况）与目录动作模式执行，不只查当天
        训练日；限制冲突与红旗各自独立阻断，计划内动作读不到目录行同样 fail-closed。正式
        限制未收集时只给需澄清项，不当作「无冲突」。
        """
        async with self._db.transaction() as conn:
            current = await self._plans.read_current_in_transaction(conn)
            if current is None:
                return None
            snapshot = await self._profiles.read_in_transaction(conn)
            if snapshot.profile is None:
                # 计划只能经确认事务建立，而确认要求正式档案；到这里即库内状态损坏，显式失败。
                raise InvalidPlanRow(
                    f"存在正式计划却没有正式档案，无法复核计划安全：{current.id}"
                )
            plan = await self._build_view(
                conn, current, is_current=True, business_date=business_date
            )
            safety = evaluate_plan_safety(
                snapshot.profile,
                current.payload,
                catalog=await self._plan_catalog_in_transaction(conn, current.payload),
            )
        return PlanGuidance(plan=plan, safety=safety)

    async def _build_view(
        self,
        conn: aiosqlite.Connection,
        version: PlanVersionRecord,
        *,
        is_current: bool,
        business_date: date,
    ) -> PlanScheduleView:
        sessions = await self._plans.list_sessions_in_transaction(conn, version.id)
        return PlanScheduleView(
            version=version,
            is_current=is_current,
            sessions=tuple(
                ScheduledSessionView(
                    session=item,
                    lock=session_lock_state(
                        item.scheduled_on,
                        stored_locked_at=item.locked_at,
                        business_date=business_date,
                    ),
                )
                for item in sessions
            ),
        )

    async def _plan_catalog_in_transaction(
        self, conn: aiosqlite.Connection, payload: PlanPayload
    ) -> dict[str, Exercise]:
        """计划引用动作的目录投影（含停用动作）；读不到的身份不补造，留给复核记入缺失集。"""
        catalog: dict[str, Exercise] = {}
        for workout in payload.plan_workouts:
            for item in workout.exercises:
                if item.exercise_id in catalog:
                    continue
                exercise = await self._exercises.get_by_id_in_transaction(
                    conn, item.exercise_id
                )
                if exercise is not None:
                    catalog[item.exercise_id] = exercise
        return catalog
