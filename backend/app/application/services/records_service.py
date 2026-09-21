"""records 用例编排：训练记录的新增、查询、修改与删除。"""

from collections.abc import Sequence
from datetime import date

from app.domain.actions.rules import UnknownExercise, validate_record_against_exercise
from app.domain.plans.schema import PlanSession
from app.domain.records.rules import (
    validate_performed_on,
    validate_session_sets,
    validate_set_fields_for_record_type,
)
from app.domain.records.schema import WorkoutSession, WorkoutSetInput
from app.infrastructure.database.connection import Database
from app.infrastructure.database.repositories.actions_repository import ExerciseRepo
from app.infrastructure.database.repositories.records_repository import (
    WorkoutRecordsRepo,
)
from app.infrastructure.database.repositories.tool_cache_repository import (
    ToolCacheRevisionsRepo,
)


class WorkoutRecordNotFound(ValueError):
    """按身份修改或删除时该次训练不存在。"""


class PlanSessionLinkUnavailable(ValueError):
    """要关联的计划日程不可用：不存在、已取消，或已被其他训练关联。"""


class PlanSessionLinkAmbiguous(ValueError):
    """自动关联时当天未完成日程不是恰好一个（零个或多个）：不猜，要求用户选择或保持额外训练。"""


class WorkoutRecordsService:
    """训练记录 CRUD（写入前统一校验事实、目录口径与关联日程）。"""

    def __init__(
        self,
        records: WorkoutRecordsRepo,
        exercises: ExerciseRepo,
        revisions: ToolCacheRevisionsRepo,
        db: Database,
    ) -> None:
        self._records = records
        self._exercises = exercises
        self._revisions = revisions
        self._db = db

    async def validate_record_facts(
        self, performed_on: date, sets: Sequence[WorkoutSetInput]
    ) -> tuple[date, tuple[WorkoutSetInput, ...]]:
        """写入前的完整事实校验（日期 ＋ 组规则 ＋ 目录口径），不写库、不碰关联日程。"""
        day = validate_performed_on(performed_on)
        facts = validate_session_sets(sets)
        await self._validate_sets_against_catalog(facts)
        return day, facts

    async def create(
        self,
        performed_on: date,
        sets: Sequence[WorkoutSetInput],
        *,
        plan_session_id: int | None = None,
        auto_link: bool = False,
    ) -> WorkoutSession:
        """新增一次训练（连同全部组，原子写入）；``plan_session_id`` 为 None 即额外训练。"""
        day, facts = await self.validate_record_facts(performed_on, sets)
        async with self._db.transaction() as conn:
            link = await self._resolve_link_in_transaction(
                conn, day, plan_session_id, auto_link, exclude_session_id=None
            )
            record = await self._records.create_in_transaction(conn, day, link, facts)
            await self._revisions.bump_in_transaction(conn, "workouts")
            return record

    async def get(self, session_id: int) -> WorkoutSession | None:
        """按身份读取一次训练及其全部组；不存在即 None。"""
        return await self._records.read(session_id)

    async def list_all(self) -> tuple[WorkoutSession, ...]:
        """查询全部训练及其全部组（按发生日期排序）。"""
        return await self._records.list_all()

    async def list_recent(self, limit: int) -> tuple[WorkoutSession, ...]:
        """最近 ``limit`` 次训练及其全部组（最新在前，同日训练各算一次）。"""
        return await self._records.list_recent(limit)

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
        day, facts = await self.validate_record_facts(performed_on, sets)
        async with self._db.transaction() as conn:
            link = await self._resolve_link_in_transaction(
                conn, day, plan_session_id, auto_link, exclude_session_id=session_id
            )
            record = await self._records.replace_in_transaction(
                conn, session_id, day, link, facts
            )
            if record is None:
                raise WorkoutRecordNotFound(f"训练记录不存在：{session_id}")
            await self._revisions.bump_in_transaction(conn, "workouts")
            return record

    async def delete(self, session_id: int) -> None:
        """物理删除一次训练（组行随训练一并删除）；身份不存在抛领域错误。"""
        async with self._db.transaction() as conn:
            if not await self._records.delete_in_transaction(conn, session_id):
                raise WorkoutRecordNotFound(f"训练记录不存在：{session_id}")
            await self._revisions.bump_in_transaction(conn, "workouts")

    async def list_unfinished_plan_sessions(
        self, performed_on: date
    ) -> tuple[PlanSession, ...]:
        """当天可关联的日程候选（未取消且未被其他训练关联）；供表单选择。"""
        return await self._records.list_unfinished_plan_sessions(
            validate_performed_on(performed_on)
        )

    async def _validate_sets_against_catalog(
        self, facts: Sequence[WorkoutSetInput]
    ) -> None:
        """逐个动作复验目录：动作存在，负重口径与目录一致，且字段符合该动作的记录口径。"""
        for fact in facts:
            exercise = await self._exercises.get_by_id(fact.exercise_id)
            if exercise is None:
                raise UnknownExercise(f"动作身份不在目录内：{fact.exercise_id}")
            validate_record_against_exercise(
                exercise,
                record_type=exercise.record_type,
                load_convention=fact.load_convention,
            )
            validate_set_fields_for_record_type(
                exercise.record_type,
                reps=fact.reps,
                duration_seconds=fact.duration_seconds,
            )

    async def _resolve_link_in_transaction(
        self,
        conn,
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
        candidates = await self._records.link_candidates_in_transaction(conn, day)
        if len(candidates) != 1:
            raise PlanSessionLinkAmbiguous(
                f"当天未完成日程候选不是恰好一个（{len(candidates)}），"
                f"不自动关联：{day.isoformat()}"
            )
        return candidates[0].id

    async def _require_linkable_in_transaction(
        self,
        conn,
        plan_session_id: int,
        exclude_session_id: int | None,
    ) -> None:
        """显式关联的日程必须存在、未取消、且未被其他训练占用。"""
        plan_session = await self._records.read_plan_session_in_transaction(
            conn, plan_session_id
        )
        if plan_session is None:
            raise PlanSessionLinkUnavailable(f"计划日程不存在：{plan_session_id}")
        if plan_session.cancelled_at is not None:
            raise PlanSessionLinkUnavailable(f"计划日程已取消：{plan_session_id}")
        linked = await self._records.read_linked_session_id_in_transaction(
            conn, plan_session_id
        )
        if linked is not None and linked != exclude_session_id:
            raise PlanSessionLinkUnavailable(
                f"计划日程已被其他训练关联：{plan_session_id}（训练 {linked}）"
            )
