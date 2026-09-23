"""plans 用例编排：计划草案落库与确认／归档事务。"""

from datetime import date

from app.application.ports import ActivePlanSnapshot
from app.domain.plans.rules import (
    known_forbidden_exercise_ids,
    validate_plan_adjustment,
    validate_plan_draft,
)
from app.domain.plans.schema import (
    EvaluationResult,
    Plan,
    PlanDraft,
    RuleFailure,
    evaluation_result_to_json,
    plan_draft_to_json,
)
from app.infrastructure.database.connection import Database
from app.infrastructure.database.repositories.actions_repository import ExerciseRepo
from app.infrastructure.database.repositories.plans_repository import PlanRepo
from app.infrastructure.database.repositories.profile_repository import ProfileRepo
from app.infrastructure.database.repositories.stats_repository import StatsRepo
from app.infrastructure.database.repositories.tool_cache_repository import (
    ToolCacheRevisionsRepo,
)


class PlanDraftConflict(ValueError):
    """条件更新未命中，或原 draft 已不存在或不再是 draft。"""


class EvaluationNotPassed(ValueError):
    """``persist_draft`` 收到未通过评估的结果：不写任何行（§7.1）。"""


class PlanActivationError(ValueError):
    """确认／归档事务的明确错误：不写任何行，draft 保持可确认（stage5.md §3.1、§8）。"""


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


class PlansService:
    """计划用例的唯一写入口：``persist_draft``／``activate_plan``／``archive_draft``。

    无模型：模型调用不在业务事务内，本服务不导入任何模型／Graph SDK。每个方法各自在单个数据库事务
    内完成全部写入，任一步失败整体回滚。``idx_plans_single_active``／``idx_plans_single_draft`` 是
    最后防线：数据库部分唯一索引与条件更新一起保证重复确认不会产生第二条 active。
    """

    def __init__(
        self,
        plans: PlanRepo,
        profiles: ProfileRepo,
        exercises: ExerciseRepo,
        stats: StatsRepo,
        revisions: ToolCacheRevisionsRepo,
        db: Database,
    ) -> None:
        self._plans = plans
        self._profiles = profiles
        self._exercises = exercises
        self._stats = stats
        self._revisions = revisions
        self._db = db

    #: 只读 revision 端口：计划路径据此填 ``ToolExecutionContext`` 的事实快照键。
    @property
    def revisions(self) -> ToolCacheRevisionsRepo:
        return self._revisions

    async def get_unique_draft(self) -> Plan | None:
        """当前唯一可确认 draft；没有即 None。"""
        return await self._plans.read_draft()

    async def persist_draft(
        self,
        draft: PlanDraft,
        evaluation: EvaluationResult,
        *,
        existing_draft_id: int | None,
        created_at: str,
        source_plan_id: int | None = None,
    ) -> Plan:
        """通过路径：无原 draft 则插入，有原 draft 则条件替换同一 id／version，返回写入行。

        只接受 ``evaluation.passed=True``；未通过评估即就地快速失败，``plans`` 行数与 revision 均不变。
        """
        if not evaluation.passed:
            raise EvaluationNotPassed(
                "persist_draft 只接受通过评估的草案，未通过评估的计划不落库"
            )
        async with self._db.transaction() as conn:
            before = await self._plans.read_active_snapshot_in_transaction(conn)
            if existing_draft_id is None:
                written = await self._plans.write_draft_in_transaction(
                    conn,
                    structured_content_json=plan_draft_to_json(draft),
                    evaluator_result_json=evaluation_result_to_json(evaluation),
                    created_at=created_at,
                    source_plan_id=source_plan_id,
                )
            else:
                written = await self._plans.replace_draft_in_transaction(
                    conn,
                    existing_draft_id,
                    structured_content_json=plan_draft_to_json(draft),
                    evaluator_result_json=evaluation_result_to_json(evaluation),
                )
                if written is None:
                    raise PlanDraftConflict(
                        f"draft 已变化（不存在或不再是 draft），不覆盖：{existing_draft_id}"
                    )
            await self._require_active_unchanged(conn, before)
            await self._revisions.bump_in_transaction(conn, "plans")
            return written

    async def _require_active_unchanged(
        self, conn, before: ActivePlanSnapshot
    ) -> None:
        """写路径结束时原 active 必须逐字段不变：变了即回滚并大声失败。"""
        after = await self._plans.read_active_snapshot_in_transaction(conn)
        if after != before:
            raise RuntimeError(
                f"写路径修改了原 active 计划（stage4.md §9.2）：{before!r} -> {after!r}"
            )

    # ---------- 确认端点的事务入口 ----------

    async def activate_plan(
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
        - ``starts_on < business_day``、再校验失败或缺少再校验事实：不归档、不改 draft 状态。
        """
        plan = await self._plans.read_by_id(plan_id)
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
        active = await self._plans.read_active()
        failures = await self._revalidation_failures(
            draft, source_plan_id=plan.source_plan_id, active=active
        )
        if failures:
            raise PlanRevalidationFailed(
                f"激活前确定性校验未通过，计划保持 draft：{plan_id}", failures=failures
            )
        async with self._db.transaction() as conn:
            if active is not None:
                archived = await self._plans.archive_active_in_transaction(
                    conn, active.id, archived_at=archived_at
                )
                if archived is None:
                    raise PlanActivationConflict(
                        f"当前 active 已变化，拒绝激活：{active.id}"
                    )
                await self._plans.cancel_sessions_in_transaction(
                    conn,
                    active.id,
                    business_day=business_day,
                    cancelled_at=archived_at,
                )
            activated = await self._plans.activate_draft_in_transaction(
                conn, plan_id, confirmed_at=confirmed_at
            )
            if activated is None:
                raise PlanActivationConflict(f"计划已不是 draft，拒绝激活：{plan_id}")
            await self._plans.create_sessions_in_transaction(
                conn,
                plan_id,
                scheduled_on=[day.scheduled_on for day in draft.training_days],
            )
            await self._revisions.bump_in_transaction(conn, "plans")
            return activated

    async def archive_draft(self, plan_id: int, *, archived_at: str) -> Plan:
        """用户拒绝：``draft -> archived``＋``archived_at``，原 active 不变，不写 ``rejected``。"""
        plan = await self._plans.read_by_id(plan_id)
        if plan is None:
            raise PlanNotFound(f"计划不存在：{plan_id}")
        if plan.status == "archived":
            return plan
        if plan.status != "draft":
            raise PlanActivationConflict(
                f"计划不是可归档的 draft：{plan_id}（{plan.status}）"
            )
        async with self._db.transaction() as conn:
            archived = await self._plans.archive_draft_in_transaction(
                conn, plan_id, archived_at=archived_at
            )
            if archived is None:
                raise PlanActivationConflict(f"计划已不是 draft：{plan_id}")
            await self._revisions.bump_in_transaction(conn, "plans")
            return archived

    async def _revalidation_failures(
        self,
        draft: PlanDraft,
        *,
        source_plan_id: int | None,
        active: Plan | None,
    ) -> tuple[RuleFailure, ...]:
        """按当前目录、画像与有效工作组再跑一次确定性校验（不调模型 Rubric）。

        再校验所需的只读事实在事务外经只读端口读取：``Database`` 的唯一锁不可重入，读事实不能与写
        事务同时持锁（app/infrastructure/database/connection.py）。事务内只做带来源状态条件的写入，
        因此并发变化只会让条件更新未命中而整体回滚，不会写出部分状态。
        """
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
        training_mode = (
            profile.training_mode.value if profile.training_mode.is_known else None
        )
        if adjustment_active is None:
            return validate_plan_draft(
                draft,
                exercises=exercises,
                profile_weekly_frequency=profile.weekly_frequency.value,
                forbidden_exercise_ids=forbidden,
                training_mode=training_mode,
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
            training_mode=training_mode,
            work_sets=work_sets,
        )

    async def _linked_workout_session_ids(self, active: Plan) -> tuple[int, ...]:
        """当前 active 关联日程下的训练身份：渐进只统计它们，额外训练不计入也不打断（rules.py）。"""
        session_ids = {
            session.id for session in await self._plans.list_sessions(active.id)
        }
        return tuple(
            fact.workout_session_id
            for fact in await self._stats.list_linked_workouts()
            if fact.plan_session_id in session_ids
        )
