"""当次安排草稿应用层（S3-08）：同一快照准备、创建与按身份／会话查询。

正本：stage3.md §5 S3-08、§4.2（安排接受即落盘）、04 4.3（安排绑定计划版本与训练日、
保存目标和已接受调整的快照、临时调整不改长期计划）、01 1.3（草稿生命周期与基线绑定）。
四条硬边界：

- **同一快照准备**：:meth:`ArrangementDraftService.prepare_input` 在单一读事务内读正式档案＋
  ``context_version``＋当前正式计划及其日程；创建只凭这份快照绑定 ``base_business_version``
  与计划版本，不在保存时改读最新状态（01 1.3）。
- **内部创建，不开 HTTP 面**：Pending 安排草稿只经本服务创建（stage3.md §3：不发布公开
  建草稿路由）；来源关联现有会话／Run 身份，``run_id`` 可空（本阶段没有真实模型执行）。
- **只改已拍可变字段，且只能更安全**：目标从绑定版本的训练日**照抄**，只允许
  ``domain/plan/rules`` 的 ``ArrangementAdjustment`` 调整组次与目标 RIR，且只能向更安全方向
  移动（用户已拍 B）：组次只减不增、目标 RIR 只增不减、计划无 RIR 时不得新造；动作身份、
  次数区间、负荷、递增、展示快照全等。调整过（与计划不同）必须给出非空白
  ``adjustment_reason``（PRD §5.5：普通调整必须说明原因）。查询把绑定版本的计划目标与
  当次完整目标一起给出，便于「原计划／当次安排」对照。
- **不落正式事实**：创建与查询都不写 ``arrangement_revisions``／``plan_versions``／
  ``scheduled_sessions``／``user_profile``，不推进 ``context_version``。临时调整不改长期
  计划；接受即落盘与幂等凭据归确认事务（``app/confirm.py`` 的 ``confirm_arrangement_draft``）。

使用时按最新限制与红旗复核（04 4.3）不在本模块，也不在确认事务里提前放行或绕开：指导
端点复核归 S3-14（S3-07 证据残留 ④）。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.draft_repo import Draft, DraftRepo, InvalidDraftRow
from app.drafts import DraftKindMismatch, require_draft_source
from app.plan_drafts import replacement_condition_violations
from domain.actions.repo import ExerciseRepo
from domain.actions.schema import Exercise
from domain.plan.repo import PlanRepo, PlanVersionRecord, ScheduledSessionRecord
from domain.plan.rules import (
    ArrangementAdjustment,
    InvalidArrangementTarget,
    arrangement_target_exercises,
    validate_arrangement_target,
)
from domain.plan.schema import (
    ArrangementTarget,
    InvalidPlanRow,
    PlanExerciseItem,
    PlanWorkout,
    arrangement_target_from_json,
    arrangement_target_to_json,
)
from domain.plan.service import evaluate_plan_safety
from domain.profile.repo import ProfileRepo
from domain.profile.schema import (
    Profile,
    ProfileSnapshot,
    profile_from_json,
    profile_to_json,
)
from storage.db import Database
from storage.run_repo import RunRepo

ARRANGEMENT_DRAFT_KIND = "arrangement"


@dataclass(frozen=True, slots=True)
class ArrangementPreparation:
    """同一快照的准备输入：正式档案与业务版本、当前正式计划及其全部日程。

    ``current_plan`` 是准备读取时刻的当前正式计划版本（无正式计划时为 None），
    ``current_sessions`` 是同一快照内读到的该版本全部应训练名额（含已取消／已锁定）。
    创建草稿只凭这份快照绑定计划版本与 ``base_business_version``，不接受调用方另传。
    """

    snapshot: ProfileSnapshot
    current_plan: PlanVersionRecord | None
    current_sessions: tuple[ScheduledSessionRecord, ...]


@dataclass(frozen=True, slots=True)
class ArrangementFieldDiff:
    """单个安排字段的「基线 → 拟议」对比；形状与计划／记录 FieldDiff 一致。

    ``field`` 取 ``exercises``（完整动作集合，结构化保留处方与处置）与
    ``adjustment_reason``；绑定身份（``scheduled_session_id``／``plan_version_id``／
    ``plan_workout_key``／``scheduled_on``）不进 Diff：它是草稿身份与绑定，不是业务内容
    改动（与记录 Diff 排除 ``training_session_id`` 同口径）。``before`` 为 None（无基线）与
    空集合不是同一语义。
    """

    field: str
    before: Any
    after: Any

    @property
    def changed(self) -> bool:
        """基线与拟议是否不同；无基线（None）与空集合仍可区分。"""
        return self.before != self.after


#: 安排 Diff 的对比基线：绑定版本的正式计划训练日，或重算子草稿对应的旧草稿当次目标
#: （两者都有相同的完整动作集合字段，与计划 ``PlanDiffBaseline`` 同一约定）。
ArrangementDiffBaseline = ArrangementTarget | PlanWorkout


@dataclass(frozen=True, slots=True)
class ArrangementDraftView:
    """安排草稿的当前查询形态：行数据 + 当次完整目标 + 绑定版本的计划目标 + 结构化 Diff。

    ``base_profile is None`` 表示准备时未建档；``planned_workout`` 是绑定版本里该训练日的
    原始目标（原计划），``target`` 是当次完整目标（原计划加已接受调整）；未接受前两者只在
    草稿里，不写正式表。``session`` 含存储锁定与取消状态：安排确认与日程锁定分离，锁定不
    影响接受，取消才拒绝（04 4.2/4.3）。``diff`` 是「拟议 → 正式业务数据」，
    ``parent_diff`` 只在重算子草稿上给出「旧草稿 → 新草稿」（01 1.6）。
    """

    draft: Draft
    base_profile: Profile | None
    target: ArrangementTarget
    plan_version: PlanVersionRecord
    session: ScheduledSessionRecord
    planned_workout: PlanWorkout
    diff: tuple[ArrangementFieldDiff, ...]
    parent_diff: tuple[ArrangementFieldDiff, ...] | None = None


def require_arrangement_safety(
    target: ArrangementTarget,
    *,
    version: PlanVersionRecord,
    profile: Profile | None,
    catalog: Mapping[str, Exercise],
) -> None:
    """创建与确认共用的确定性安全复核（04 4.5、PRD §5.5／§5.7）；不过则不得落库。

    整份计划按**当刻条件**复核（不只当天的动作），并把当次目标实际要做的动作身份（含同等
    刺激替换的替代动作）一并评估：新报告的限制或红旗在落库前就阻断，不得等档案更新落盘。
    替换的器械与限制条件同入口重建，不靠调用方声明。
    """
    if profile is None:
        raise InvalidArrangementTarget(
            "尚未建立正式档案：不按计划给可执行处方，也不接受当次安排"
        )
    recheck = evaluate_plan_safety(
        profile,
        version.payload,
        catalog=catalog,
        session_exercise_ids=_session_exercise_ids(target),
    )
    violations = list(recheck.blocking_reasons)
    violations.extend(
        replacement_condition_violations(target, profile=profile, catalog=catalog)
    )
    if violations:
        raise InvalidArrangementTarget(
            "当次安排未通过最新限制与红旗复核，不落库：" + "；".join(violations)
        )


def _session_exercise_ids(target: ArrangementTarget) -> tuple[str, ...]:
    """当次目标实际要做的动作身份（去重，含替代动作）：复核「当次条件」用（04 4.5）。"""
    return tuple(
        dict.fromkeys(
            exercise_id
            for item in target.exercises
            for exercise_id in (
                item.exercise_id,
                item.replacement_exercise_id,
            )
            if exercise_id is not None
        )
    )


class ArrangementDraftService:
    """安排草稿生命周期应用层（S3-08）：准备、创建与查询；不写正式事实、不推进版本。"""

    def __init__(self, db: Database):
        self._db = db
        self._profiles = ProfileRepo(db)
        self._plans = PlanRepo(db)
        self._drafts = DraftRepo(db)
        self._runs = RunRepo(db)
        self._exercises = ExerciseRepo(db)

    async def _catalog(self) -> dict[str, Exercise]:
        """目录投影（含停用动作）：建草稿前的结构校验与替换等价复查用，不掺推荐判定。"""
        return {exercise.id: exercise for exercise in await self._exercises.list_all()}

    async def prepare_input(self) -> ArrangementPreparation:
        """在单一读事务内读正式档案与版本、当前正式计划及其日程，作为创建的唯一快照。"""
        async with self._db.transaction() as conn:
            snapshot = await self._profiles.read_in_transaction(conn)
            current_plan = await self._plans.read_current_in_transaction(conn)
            current_sessions = (
                ()
                if current_plan is None
                else await self._plans.list_sessions_in_transaction(
                    conn, current_plan.id
                )
            )
        return ArrangementPreparation(
            snapshot=snapshot,
            current_plan=current_plan,
            current_sessions=current_sessions,
        )

    async def create_arrangement_draft(
        self,
        *,
        draft_id: str,
        preparation: ArrangementPreparation,
        conversation_id: str,
        run_id: str | None,
        scheduled_session_id: str,
        adjustments: tuple[ArrangementAdjustment, ...] = (),
        adjustment_reason: str | None = None,
        parent_draft_id: str | None = None,
    ) -> ArrangementDraftView:
        """按准备快照保存一条 Pending 安排草稿（revision 从 1 起），返回查询形态。

        - **绑定只取 ``preparation``**：计划版本、训练日与旧日程都来自准备读取时刻的同一
          快照；生成与保存之间发生的正式提交不改变本草稿的绑定，过期在首次确认时拦截。
        - **目标完整**：未调整的条目原样照抄计划目标，调整只限 ``work_sets`` 与
          ``target_rir``，且只向更安全方向移动（用户已拍 B：组次只减不增、目标 RIR 只增不减、
          计划无 RIR 时不得新造）；有差异必须给出非空白 ``adjustment_reason``。任一校验
          失败不落库。
        - **不做正式写入**：不写 ``arrangement_revisions``、不改计划与档案、不推进版本。
        """
        plan = preparation.current_plan
        if plan is None:
            raise InvalidArrangementTarget(
                "尚无正式计划：当次安排必须绑定具体计划版本与训练日，不凭空造目标"
            )
        session = next(
            (
                item
                for item in preparation.current_sessions
                if item.id == scheduled_session_id
            ),
            None,
        )
        if session is None:
            raise InvalidArrangementTarget(
                f"应训练名额不在当前正式计划内：{scheduled_session_id}"
            )
        if session.cancelled_at is not None:
            raise InvalidArrangementTarget(
                f"已取消的日程不再是应训练义务，不接受当次安排：{scheduled_session_id}"
            )
        workout = _planned_workout(plan, session)
        # 目录在创建前读一次（参考表，同一业务版本内不因草稿而变）：替换等价必须真的看到
        # 双方身份与肌群，读不到就 fail-closed，不得当作「无冲突」。
        catalog = await self._catalog()
        target = ArrangementTarget(
            scheduled_session_id=session.id,
            plan_version_id=plan.id,
            plan_workout_key=session.plan_workout_key,
            scheduled_on=session.scheduled_on,
            exercises=arrangement_target_exercises(workout, adjustments),
            adjustment_reason=adjustment_reason,
        )
        validate_arrangement_target(target, workout=workout, catalog=catalog)
        require_arrangement_safety(
            target,
            version=plan,
            profile=preparation.snapshot.profile,
            catalog=catalog,
        )
        await require_draft_source(
            self._runs, conversation_id=conversation_id, run_id=run_id
        )
        base_profile = preparation.snapshot.profile
        draft = await self._drafts.create_arrangement_pending(
            draft_id=draft_id,
            conversation_id=conversation_id,
            run_id=run_id,
            base_profile_json=(
                None if base_profile is None else profile_to_json(base_profile)
            ),
            proposed_profile_json=profile_to_json(
                base_profile if base_profile is not None else Profile.empty()
            ),
            proposed_arrangement_json=arrangement_target_to_json(target),
            base_business_version=preparation.snapshot.context_version,
            parent_draft_id=parent_draft_id,
        )
        return await self._to_view(draft)

    async def get_arrangement_draft(self, draft_id: str) -> ArrangementDraftView | None:
        """按身份读取安排草稿（含绑定版本的计划目标与当次目标）；不存在返回 None。"""
        draft = await self._drafts.get(draft_id)
        return None if draft is None else await self._to_view(draft)

    async def list_arrangement_drafts(
        self, conversation_id: str
    ) -> tuple[ArrangementDraftView, ...]:
        """该会话已持久化安排草稿的当前状态（01 1.2）。"""
        drafts = await self._drafts.list_for_conversation(
            conversation_id, kind=ARRANGEMENT_DRAFT_KIND
        )
        return tuple([await self._to_view(draft) for draft in drafts])

    async def _to_view(self, draft: Draft) -> ArrangementDraftView:
        """行 → 查询形态：解码目标快照、读绑定版本与该训练日、复查结构与绑定。

        绑定版本按 ``plan_version_id`` 读取（计划版本只追加不删除），因此后续替换计划后
        读到的仍是生成时那一版；缺失即草稿数据损坏，显式失败不静默兜底。
        """
        if draft.kind != ARRANGEMENT_DRAFT_KIND:
            raise DraftKindMismatch(
                f"安排草稿查询只适用于 kind={ARRANGEMENT_DRAFT_KIND}，"
                f"收到 kind={draft.kind}：{draft.id}"
            )
        target = arrangement_target_from_json(_require_arrangement_json(draft))
        async with self._db.transaction() as conn:
            version = await self._plans.read_version_in_transaction(
                conn, target.plan_version_id
            )
            if version is None:
                raise InvalidPlanRow(
                    f"安排草稿绑定的计划版本不存在：{target.plan_version_id}"
                )
            session = await self._plans.read_session_in_transaction(
                conn, target.scheduled_session_id
            )
            catalog = await self._exercises.list_all_in_transaction(conn)
        if session is None:
            raise InvalidDraftRow(
                f"安排草稿绑定的应训练名额不存在：{target.scheduled_session_id}"
            )
        planned = require_arrangement_binding(target, version=version, session=session)
        validate_arrangement_target(
            target,
            workout=planned,
            catalog={exercise.id: exercise for exercise in catalog},
        )
        parent_diff = None
        if draft.parent_draft_id is not None:
            parent = await self._drafts.get(draft.parent_draft_id)
            if parent is None:
                raise InvalidDraftRow(
                    f"重算子草稿的旧草稿不存在：{draft.parent_draft_id}"
                )
            parent_target = arrangement_target_from_json(
                _require_arrangement_json(parent)
            )
            parent_diff = arrangement_field_diff(target, parent_target)
        return ArrangementDraftView(
            draft=draft,
            base_profile=(
                None
                if draft.base_profile_json is None
                else profile_from_json(draft.base_profile_json)
            ),
            target=target,
            plan_version=version,
            session=session,
            planned_workout=planned,
            diff=arrangement_field_diff(target, planned),
            parent_diff=parent_diff,
        )


def _require_arrangement_json(draft: Draft) -> str:
    """安排草稿的目标快照文本；缺失即草稿数据损坏，显式失败不静默兜底。"""
    if draft.proposed_arrangement_json is None:
        raise InvalidDraftRow(f"安排草稿缺少目标快照：{draft.id}")
    return draft.proposed_arrangement_json


def arrangement_field_diff(
    target: ArrangementTarget, baseline: ArrangementDiffBaseline | None
) -> tuple[ArrangementFieldDiff, ...]:
    """拟议当次目标相对基线的结构化字段对比；无基线时 ``before`` 为 None（不伪造旧值）。

    基线可以是绑定版本该训练日的正式计划目标（「拟议 → 正式业务数据」），也可以是旧草稿的
    当次目标（重算子草稿的「旧草稿 → 新草稿」，01 1.6）；两者都有完整动作集合，复用同一
    入口，不另造第二套 Diff 逻辑（与计划 :func:`plan_drafts.plan_field_diff` 同一约定）。
    """
    if baseline is None:
        before_exercises: tuple[PlanExerciseItem, ...] | None = None
        before_reason: str | None = None
    elif isinstance(baseline, ArrangementTarget):
        before_exercises = baseline.exercises
        before_reason = baseline.adjustment_reason
    else:
        before_exercises = baseline.exercises
        before_reason = None  # 正式计划训练日不是草稿，没有调整说明
    return (
        ArrangementFieldDiff(
            field="exercises", before=before_exercises, after=target.exercises
        ),
        ArrangementFieldDiff(
            field="adjustment_reason",
            before=before_reason,
            after=target.adjustment_reason,
        ),
    )


def require_arrangement_binding(
    target: ArrangementTarget,
    *,
    version: PlanVersionRecord,
    session: ScheduledSessionRecord,
) -> PlanWorkout:
    """校验目标的绑定与指定版本／日程一致，返回该训练日（不符即拒绝，不落库）。

    创建、查询与确认事务共用同一入口：安排绑定的是**具体计划版本与训练日**（04 4.3），
    不接受目标里的绑定与草稿实际指向的版本／日程不一致（那是数据损坏或越权改绑定）。
    取消状态不在本函数里判：已取消日程的历史草稿仍可查询，拒绝接受新安排的守卫在创建与
    确认两处各自显式执行（04 4.2）。
    """
    if target.plan_version_id != version.id:
        raise InvalidArrangementTarget(
            f"当次目标绑定的计划版本与草稿指向的版本不一致："
            f"{target.plan_version_id!r} / {version.id!r}"
        )
    if target.scheduled_session_id != session.id:
        raise InvalidArrangementTarget(
            f"当次目标绑定的应训练名额与草稿指向的日程不一致："
            f"{target.scheduled_session_id!r} / {session.id!r}"
        )
    if session.plan_version_id != version.id:
        raise InvalidArrangementTarget(
            f"应训练名额不属于绑定的计划版本：{session.id} / {version.id}"
        )
    if target.scheduled_on != session.scheduled_on or (
        target.plan_workout_key != session.plan_workout_key
    ):
        raise InvalidArrangementTarget(
            f"当次目标绑定的训练日与日程不一致：{session.id}"
        )
    return _planned_workout(version, session)


def _planned_workout(
    version: PlanVersionRecord, session: ScheduledSessionRecord
) -> PlanWorkout:
    """绑定版本里该日程对应的训练日；读不到即状态损坏，显式失败不静默兜底。"""
    for workout in version.payload.plan_workouts:
        if workout.workout_key == session.plan_workout_key:
            return workout
    raise InvalidPlanRow(
        f"计划版本 {version.id} 内没有该日程的训练日：{session.plan_workout_key}"
    )
