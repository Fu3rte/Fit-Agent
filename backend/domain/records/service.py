"""records 用例编排：训练记录的新增、查询、修改与删除（正本讨论总结 §9；REFACTOR_PLAN §5.4）。

边界：不接 HTTP／Agent，表单直接调用本服务（04 表单 API），不创建草稿、不写 ``plan_sessions``。
落库前做三件事，顺序固定：

1. **rules 校验事实**：日期、动作身份、负重口径、重量、次数、组数、组类型（``domain.records.rules``）；
2. **目录复验**：逐个动作查 ``domain.actions.service.ActionCatalogService.validate_record_write``，
   确认动作存在且负重口径与目录一致（外加负重型必须带口径，自重／计时型不得带口径与重量）。
   ``workout_sets`` 不存记录口径，故按目录动作自身派生后复验——记录侧只声明负重口径这一件事。
   这一步必须在写事务之前（``under_lock`` 与写事务共用同一把不可重入的锁）；
3. **关联日程**：在写事务内解析，避免「先查候选、再写库」之间的竞态与裸 ``IntegrityError``。

关联口径（讨论总结 §9）：``plan_session_id`` 可空，``NULL`` 明确表示额外训练；用户可直接选择
某个未完成日程；``auto_link=True`` 时仅当当天恰有一个未完成日程才关联，零个或多个候选一律抛
:class:`PlanSessionLinkAmbiguous`（不猜、不静默写 NULL）；显式给出的 ``plan_session_id`` 优先于
``auto_link``。同一日程最多被一条训练关联（库内 UNIQUE 兜底）。注意「未完成」按讨论总结 §9 判定：
已被某条记录关联的日程即为已完成，因此对一条已关联日程的训练调用 ``update(..., auto_link=True)``
会看到 0 个未完成候选并抛 :class:`PlanSessionLinkAmbiguous`——这是正确行为，编辑既有训练时调用方
必须显式传 ``plan_session_id``。

领域错误：身份不存在是 :class:`WorkoutRecordNotFound`，日程不可用是
:class:`PlanSessionLinkUnavailable`，自动关联候选不唯一是 :class:`PlanSessionLinkAmbiguous`，
输入事实不合法是 :class:`~domain.records.rules.InvalidRecordFact`（动作不在目录内沿用
:class:`~domain.actions.rules.UnknownExercise`，口径不符沿用
:class:`~domain.actions.rules.RecordLoadMismatch`）——它们都只继承 ``ValueError``、互不继承，
调用方按类型区分。
"""

from collections.abc import Sequence
from datetime import date

import aiosqlite

from domain.actions.rules import UnknownExercise
from domain.actions.service import ActionCatalogService
from domain.plans.schema import PlanSession
from domain.records.repo import WorkoutRecordsRepo
from domain.records.rules import validate_performed_on, validate_session_sets
from domain.records.schema import WorkoutSession, WorkoutSetInput
from storage.db import Database


class WorkoutRecordNotFound(ValueError):
    """按身份修改或删除时该次训练不存在。"""


class PlanSessionLinkUnavailable(ValueError):
    """要关联的计划日程不可用：不存在、已取消，或已被其他训练关联。"""


class PlanSessionLinkAmbiguous(ValueError):
    """自动关联时当天未完成日程不是恰好一个（零个或多个）：不猜，要求用户选择或保持额外训练。"""


class WorkoutRecordsService:
    """训练记录 CRUD（写入前统一校验事实、目录口径与关联日程）。"""

    def __init__(self, db: Database):
        self._db = db
        self._repo = WorkoutRecordsRepo(db)
        self._catalog = ActionCatalogService(db)

    async def create(
        self,
        performed_on: date,
        sets: Sequence[WorkoutSetInput],
        *,
        plan_session_id: int | None = None,
        auto_link: bool = False,
    ) -> WorkoutSession:
        """新增一次训练（连同全部组，原子写入）；``plan_session_id`` 为 None 即额外训练。"""
        day = validate_performed_on(performed_on)
        facts = validate_session_sets(sets)
        await self._validate_sets_against_catalog(facts)
        async with self._db.transaction() as conn:
            link = await self._resolve_link_in_transaction(
                conn, day, plan_session_id, auto_link, exclude_session_id=None
            )
            return await self._repo.create_in_transaction(conn, day, link, facts)

    async def get(self, session_id: int) -> WorkoutSession | None:
        """按身份读取一次训练及其全部组；不存在即 None。"""
        return await self._repo.read(session_id)

    async def list_all(self) -> tuple[WorkoutSession, ...]:
        """查询全部训练及其全部组（按发生日期排序）。"""
        return await self._repo.list_all()

    async def update(
        self,
        session_id: int,
        performed_on: date,
        sets: Sequence[WorkoutSetInput],
        *,
        plan_session_id: int | None = None,
        auto_link: bool = False,
    ) -> WorkoutSession:
        """整条覆盖一次训练（日期、关联日程与全部组行一起替换）；身份不存在抛领域错误。"""
        day = validate_performed_on(performed_on)
        facts = validate_session_sets(sets)
        await self._validate_sets_against_catalog(facts)
        async with self._db.transaction() as conn:
            link = await self._resolve_link_in_transaction(
                conn, day, plan_session_id, auto_link, exclude_session_id=session_id
            )
            record = await self._repo.replace_in_transaction(
                conn, session_id, day, link, facts
            )
            if record is None:
                raise WorkoutRecordNotFound(f"训练记录不存在：{session_id}")
            return record

    async def delete(self, session_id: int) -> None:
        """物理删除一次训练（组行随训练一并删除）；身份不存在抛领域错误。"""
        if not await self._repo.delete(session_id):
            raise WorkoutRecordNotFound(f"训练记录不存在：{session_id}")

    async def auto_plan_session_id(self, performed_on: date) -> int | None:
        """当天恰好一个未完成日程时返回其身份，否则 None（零个或多个候选都不猜）。"""
        day = validate_performed_on(performed_on)
        candidates = await self._repo.list_unfinished_plan_sessions(day)
        return candidates[0].id if len(candidates) == 1 else None

    async def list_unfinished_plan_sessions(
        self, performed_on: date
    ) -> tuple[PlanSession, ...]:
        """当天可关联的日程候选（未取消且未被其他训练关联）；供表单选择。"""
        return await self._repo.list_unfinished_plan_sessions(
            validate_performed_on(performed_on)
        )

    async def _validate_sets_against_catalog(
        self, facts: Sequence[WorkoutSetInput]
    ) -> None:
        """逐个动作复验目录：动作存在，且负重口径与目录一致。

        ``workout_sets`` 没有记录口径列，故取目录动作自身的 ``record_type`` 后调用
        ``validate_record_write``：真正被复验的是负重口径（外加负重型必须给出与目录相同的口径，
        自重／计时型不得给出任何口径）。
        """
        for fact in facts:
            exercise = await self._catalog.get_by_id(fact.exercise_id)
            if exercise is None:
                raise UnknownExercise(f"动作身份不在目录内：{fact.exercise_id}")
            await self._catalog.validate_record_write(
                fact.exercise_id,
                record_type=exercise.record_type,
                load_convention=fact.load_convention,
            )

    async def _resolve_link_in_transaction(
        self,
        conn: aiosqlite.Connection,
        day: date,
        plan_session_id: int | None,
        auto_link: bool,
        *,
        exclude_session_id: int | None,
    ) -> int | None:
        """在写事务内定下这次训练要关联的日程身份；None 即额外训练。"""
        if plan_session_id is not None:
            await self._require_linkable_in_transaction(
                conn, plan_session_id, exclude_session_id
            )
            return plan_session_id
        if not auto_link:
            return None
        candidates = await self._repo.link_candidates_in_transaction(conn, day)
        if len(candidates) != 1:
            raise PlanSessionLinkAmbiguous(
                f"当天未完成日程候选不是恰好一个（{len(candidates)}），"
                f"不自动关联：{day.isoformat()}"
            )
        return candidates[0].id

    async def _require_linkable_in_transaction(
        self,
        conn: aiosqlite.Connection,
        plan_session_id: int,
        exclude_session_id: int | None,
    ) -> None:
        """显式关联的日程必须存在、未取消、且未被其他训练占用。"""
        plan_session = await self._repo.read_plan_session_in_transaction(
            conn, plan_session_id
        )
        if plan_session is None:
            raise PlanSessionLinkUnavailable(f"计划日程不存在：{plan_session_id}")
        if plan_session.cancelled_at is not None:
            raise PlanSessionLinkUnavailable(f"计划日程已取消：{plan_session_id}")
        linked = await self._repo.read_linked_session_id_in_transaction(
            conn, plan_session_id
        )
        if linked is not None and linked != exclude_session_id:
            raise PlanSessionLinkUnavailable(
                f"计划日程已被其他训练关联：{plan_session_id}（训练 {linked}）"
            )
