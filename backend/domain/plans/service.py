"""plans 用例编排：计划持久化与确认／拒绝事务。"""

from datetime import date

from aiosqlite import Connection

from domain.actions.repo import ExerciseRepo
from domain.plans.repo import ActivePlanSnapshot, PlanRepo
from domain.plans.rules import (
    known_forbidden_exercise_ids,
    validate_plan_adjustment,
    validate_plan_draft,
)
from domain.plans.schema import (
    EvaluationResult,
    Plan,
    PlanDraft,
    RuleFailure,
    evaluation_result_to_json,
    plan_draft_to_json,
)
from domain.profile.service import ProfileService
from domain.stats.repo import StatsRepo
from storage.db import Database


class PlanDraftConflict(ValueError):
    """条件更新未命中，或原 draft 已不存在或不再是 draft。"""


class PlanPersistenceService:
    """计划持久化：draft／rejected 最小写路径与原 active 保护。"""

    def __init__(self, db: Database):
        self._db = db
        self._repo = PlanRepo(db)

    async def get_unique_draft(self) -> Plan | None:
        """当前唯一可确认 draft；没有即 None。"""
        return await self._repo.read_draft()

    async def persist_plan_result(
        self,
        draft: PlanDraft,
        evaluation: EvaluationResult,
        *,
        existing_draft_id: int | None,
        created_at: str,
        source_plan_id: int | None = None,
    ) -> Plan:
        """落到唯一正确的业务写入，返回写入（或保持不变的）计划行。"""
        content_json = plan_draft_to_json(draft)
        result_json = evaluation_result_to_json(evaluation)
        if evaluation.passed:
            return await self._persist_passing(
                content_json,
                result_json,
                existing_draft_id=existing_draft_id,
                created_at=created_at,
                source_plan_id=source_plan_id,
            )
        return await self._persist_blocking_failure(
            content_json,
            result_json,
            existing_draft_id=existing_draft_id,
            created_at=created_at,
        )

    async def _persist_passing(
        self,
        content_json: str,
        result_json: str,
        *,
        existing_draft_id: int | None,
        created_at: str,
        source_plan_id: int | None,
    ) -> Plan:
        """通过路径：无原 draft 则插入，有原 draft 则条件替换同一 id/version。"""
        async with self._db.transaction() as conn:
            before = await self._repo.read_active_snapshot_in_transaction(conn)
            if existing_draft_id is None:
                written = await self._repo.write_draft_in_transaction(
                    conn,
                    structured_content_json=content_json,
                    evaluator_result_json=result_json,
                    created_at=created_at,
                    source_plan_id=source_plan_id,
                )
            else:
                written = await self._repo.replace_draft_in_transaction(
                    conn,
                    existing_draft_id,
                    structured_content_json=content_json,
                    evaluator_result_json=result_json,
                )
                if written is None:
                    raise PlanDraftConflict(
                        f"draft 已变化（不存在或不再是 draft），不覆盖：{existing_draft_id}"
                    )
            await self._require_active_unchanged(conn, before)
            return written

    async def _persist_blocking_failure(
        self,
        content_json: str,
        result_json: str,
        *,
        existing_draft_id: int | None,
        created_at: str,
    ) -> Plan:
        """阻断失败路径：无原 draft 写 rejected；有原 draft 则保持原 draft，不写任何行。"""
        if existing_draft_id is not None:
            existing = await self._repo.read_by_id(existing_draft_id)
            if existing is None or existing.status != "draft":
                raise PlanDraftConflict(
                    f"原 draft 不存在或不再是 draft：{existing_draft_id}"
                )
            return existing
        async with self._db.transaction() as conn:
            before = await self._repo.read_active_snapshot_in_transaction(conn)
            written = await self._repo.write_rejected_in_transaction(
                conn,
                structured_content_json=content_json,
                evaluator_result_json=result_json,
                created_at=created_at,
            )
            await self._require_active_unchanged(conn, before)
            return written

    async def _require_active_unchanged(
        self, conn: Connection, before: ActivePlanSnapshot
    ) -> None:
        """写路径结束时原 active 必须逐字段不变：变了即回滚并大声失败。"""
        after = await self._repo.read_active_snapshot_in_transaction(conn)
        if after != before:
            raise RuntimeError(
                f"写路径修改了原 active 计划（stage4.md §9.2）：{before!r} -> {after!r}"
            )


class PlanActivationError(ValueError):
    """确认／拒绝事务的明确错误：不写任何行，draft 保持可确认（stage5.md §3.1、§8）。"""


class PlanNotFound(PlanActivationError):
    """请求的 ``plan_id`` 不存在：不猜目标，不写任何行。"""


class PlanActivationConflict(PlanActivationError):
    """状态冲突：目标既不是可激活 draft 也不是同一 active；archived／rejected 拒绝重新激活。"""


class PlanDraftStale(PlanActivationError):
    """draft 的 ``starts_on`` 早于本次注入业务日：拒绝激活，不归档、不改 draft 状态。"""


class PlanRevalidationFailed(PlanActivationError):
    """激活前的确定性再校验未通过，或缺少再校验所需的业务事实（如画像每周训练次数）。"""

    def __init__(
        self, message: str, *, failures: tuple[RuleFailure, ...] = ()
    ) -> None:
        super().__init__(message)
        self.failures = failures


class PlanActivationService:
    """Stage 5 唯一的确认／拒绝事务入口：§3.1 激活事务、§3.2 ``archive_draft`` 与四态幂等。

    无模型：模型调用不在业务事务内，本服务不导入任何模型／Graph SDK。全部写入在一个短事务里，
    任一步失败整体回滚、draft 保持可确认；``idx_plans_single_active``／``idx_plans_single_draft``
    是最后防线。数据库部分唯一索引与条件更新一起保证重复确认不会产生第二条 active。

    再校验所需的只读事实（画像、目录、有效工作组、关联日程训练）在事务外经既有领域服务读取：
    ``Database`` 的唯一锁不可重入，读事实不能与写事务同时持锁（storage/db.py）。事务内只做带来源状态
    条件的写入，因此并发变化只会让条件更新未命中而整体回滚，不会写出部分状态。
    """

    def __init__(self, db: Database):
        self._db = db
        self._repo = PlanRepo(db)
        self._profiles = ProfileService(db)
        self._exercises = ExerciseRepo(db)
        self._stats = StatsRepo(db)

    async def activate(
        self,
        plan_id: int,
        *,
        business_day: date,
        confirmed_at: str,
        archived_at: str,
    ) -> Plan:
        """按 §3.1 的七步顺序把一个 draft 置为 active，返回激活后的计划行。

        ``business_day`` 是本次注入的业务日；``confirmed_at`` 写新计划，``archived_at`` 写被换下的
        active 计划与同一事务内被取消的其未到期日程（同一业务瞬间）。

        - 同一 id 已是 active：幂等返回当前行，不再写任何行；
        - ``archived``／``rejected``：拒绝重新激活；``rejected`` 永不改回 draft；
        - ``starts_on < business_day``、再校验失败或缺少再校验事实：不归档、不改 draft 状态；
        """
        plan = await self._repo.read_by_id(plan_id)
        if plan is None:
            raise PlanNotFound(f"计划不存在：{plan_id}")
        if plan.status == "active":
            return plan
        if plan.status != "draft":
            raise PlanActivationConflict(
                f"计划不是可确认 draft，拒绝重新激活：{plan_id}（{plan.status}）"
            )
        draft = PlanDraft.model_validate(plan.structured_content)
        if draft.starts_on < business_day:
            raise PlanDraftStale(
                "计划开始日期早于本次业务日，拒绝激活："
                f"{draft.starts_on.isoformat()} < {business_day.isoformat()}"
            )
        active = await self._repo.read_active()
        failures = await self._revalidation_failures(
            draft, source_plan_id=plan.source_plan_id, active=active
        )
        if failures:
            raise PlanRevalidationFailed(
                f"激活前确定性校验未通过，计划保持 draft：{plan_id}", failures=failures
            )
        async with self._db.transaction() as conn:
            if active is not None:
                archived = await self._repo.archive_active_in_transaction(
                    conn, active.id, archived_at=archived_at
                )
                if archived is None:
                    raise PlanActivationConflict(
                        f"当前 active 已变化，拒绝激活：{active.id}"
                    )
                await self._repo.cancel_sessions_in_transaction(
                    conn,
                    active.id,
                    business_day=business_day,
                    cancelled_at=archived_at,
                )
            activated = await self._repo.activate_draft_in_transaction(
                conn, plan_id, confirmed_at=confirmed_at
            )
            if activated is None:
                raise PlanActivationConflict(f"计划已不是 draft，拒绝激活：{plan_id}")
            await self._repo.create_sessions_in_transaction(
                conn,
                plan_id,
                scheduled_on=[day.scheduled_on for day in draft.training_days],
            )
            return activated

    async def reject(self, plan_id: int, *, archived_at: str) -> Plan:
        """用户拒绝：``draft -> archived``＋``archived_at``，原 active 不变，不写 ``rejected``。"""
        plan = await self._repo.read_by_id(plan_id)
        if plan is None:
            raise PlanNotFound(f"计划不存在：{plan_id}")
        if plan.status == "archived":
            return plan
        if plan.status != "draft":
            raise PlanActivationConflict(
                f"计划不是可拒绝的 draft：{plan_id}（{plan.status}）"
            )
        async with self._db.transaction() as conn:
            archived = await self._repo.archive_draft_in_transaction(
                conn, plan_id, archived_at=archived_at
            )
            if archived is None:
                raise PlanActivationConflict(f"计划已不是 draft：{plan_id}")
            return archived

    async def _revalidation_failures(
        self,
        draft: PlanDraft,
        *,
        source_plan_id: int | None,
        active: Plan | None,
    ) -> tuple[RuleFailure, ...]:
        """按当前目录、画像与有效工作组再跑一次确定性校验（不调模型 Rubric）。"""
        adjustment_active: Plan | None = None
        if source_plan_id is not None:
            if active is None or active.id != source_plan_id:
                raise PlanActivationConflict(
                    "调整 draft 的来源计划不是当前 active："
                    f"source_plan_id={source_plan_id}"
                )
            adjustment_active = active
        profile = await self._profiles.read()
        if (
            profile is None
            or not profile.weekly_frequency.is_known
            or profile.weekly_frequency.value is None
        ):
            raise PlanRevalidationFailed(
                "画像缺少每周训练次数，无法再校验计划；计划保持 draft"
            )
        exercises = {
            exercise.id: exercise for exercise in await self._exercises.list_all()
        }
        work_sets = await self._stats.list_valid_work_sets()
        forbidden = known_forbidden_exercise_ids(profile)
        if adjustment_active is None:
            return validate_plan_draft(
                draft,
                exercises=exercises,
                profile_weekly_frequency=profile.weekly_frequency.value,
                forbidden_exercise_ids=forbidden,
                work_sets=work_sets,
            )
        return validate_plan_adjustment(
            draft,
            active_draft=PlanDraft.model_validate(adjustment_active.structured_content),
            linked_workout_session_ids=await self._linked_workout_session_ids(
                adjustment_active
            ),
            exercises=exercises,
            profile_weekly_frequency=profile.weekly_frequency.value,
            forbidden_exercise_ids=forbidden,
            work_sets=work_sets,
        )

    async def _linked_workout_session_ids(self, active: Plan) -> tuple[int, ...]:
        """当前 active 关联日程下的训练身份：渐进只统计它们，额外训练不计入也不打断（rules.py）。"""
        session_ids = {
            session.id for session in await self._repo.list_sessions(active.id)
        }
        return tuple(
            fact.workout_session_id
            for fact in await self._stats.list_linked_workouts()
            if fact.plan_session_id in session_ids
        )
