from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

import aiosqlite

from app.draft_repo import Draft, DraftRepo, InvalidDraftRow
from app.drafts import (
    DraftKindMismatch,
    DraftNotCorrectable,
    DraftNotDiscardable,
    DraftRevisionConflict,
    ProfileFieldDiff,
    UnknownDraft,
    profile_diff,
    require_draft_source,
)
from domain.actions.repo import ExerciseRepo
from domain.actions.schema import Exercise
from domain.plan.repo import PlanRepo, PlanVersionRecord, ScheduledSessionRecord
from domain.plan.rules import (
    InvalidPlanPayload,
    project_sessions,
    session_lock_state,
    validate_payload,
    validate_payload_correction,
)
from domain.plan.schema import (
    PLAN_MODES,
    ArrangementTarget,
    PlanMode,
    PlanPayload,
    PlanProposal,
    ProjectedSession,
    ProposedSessionCancellation,
    proposal_from_json,
    proposal_to_json,
)
from domain.plan.service import equipment_available
from domain.profile.repo import ProfileRepo
from domain.profile.rules import (
    UnknownExerciseReference,
    apply_patch,
    validate_patch,
)
from domain.profile.safety import evaluate_safety
from domain.profile.schema import (
    Profile,
    ProfilePatch,
    ProfileSnapshot,
    patch_from_json,
    patch_to_json,
    profile_from_json,
    profile_to_json,
)
from storage.db import Database
from storage.run_repo import RunRepo

PLAN_DRAFT_KIND = "plan"


@dataclass(frozen=True, slots=True)
class PlanPreparation:
    """同一快照的生成输入：正式档案＋业务版本、可推荐候选、当前正式计划及其日程。

    ``current_plan`` 是生成读取时刻的当前正式计划版本（无正式计划时为 None），
    ``current_sessions`` 是同一快照内读到的该版本全部应训练名额（含已取消／已锁定）。
    创建草稿只凭这份快照绑定 ``base_business_version`` 与替换基线，不接受调用方另传。
    """

    snapshot: ProfileSnapshot
    candidates: tuple[Exercise, ...]
    current_plan: PlanVersionRecord | None
    current_sessions: tuple[ScheduledSessionRecord, ...]


@dataclass(frozen=True, slots=True)
class PlanFieldDiff:
    """单个计划字段的「基线 → 拟议」对比；无正式计划基线时 ``before`` 为 None。

    ``field`` 取计划字段名（``starts_on``／``review_on``／``mode``／``workouts``／
    ``cancellations``）；``before``／``after`` 保持结构化值（日期、工作组合集、取消清单），
    不压成文本 diff，也不折叠取消预览。传输层展示映射归 S3-14。
    """

    field: str
    before: Any
    after: Any

    @property
    def changed(self) -> bool:
        """基线与拟议是否不同；无基线（None）与空集合仍可区分。"""
        return self.before != self.after


@dataclass(frozen=True, slots=True)
class PlanDraftView:
    """计划草稿的当前查询形态：行数据 + 解码后的拟议内容 + 结构化 Diff。

    ``base_profile is None`` 表示生成时未建档；``proposed_profile`` 是拟议条件（基线应用
    补丁后的档案，无补丁时等于基线）；``proposed_profile_patch`` 仅受限组合草稿有值，
    ``profile_diff`` 只在有补丁时给出（无补丁不伪造档案变更）；``schedules`` 是拟议日程
    投影（``[starts_on, review_on)`` 内 workout 日），``proposal.cancellations`` 是替换预览。
    """

    draft: Draft
    base_profile: Profile | None
    proposed_profile: Profile
    proposed_profile_patch: ProfilePatch | None
    proposal: PlanProposal
    schedules: tuple[ProjectedSession, ...]
    plan_diff: tuple[PlanFieldDiff, ...]
    profile_diff: tuple[ProfileFieldDiff, ...] | None
    parent_plan_diff: tuple[PlanFieldDiff, ...] | None = None


class PlanDraftService:
    """计划草稿生命周期应用层（S3-04/S3-05）：准备、创建、查询、Diff、纠错与丢弃。

    纠错只作用于 Pending 计划草稿的已拍可变字段（日期与 D9 payload）并全量复查后
    revision+1；丢弃只改状态且重复丢弃幂等。确认事务与日程原子切换（S3-06）不在本类；
    本类不写正式事实、不推进 ``context_version``、不取消任何正式日程。
    """

    def __init__(self, db: Database):
        self._db = db
        self._profiles = ProfileRepo(db)
        self._plans = PlanRepo(db)
        self._drafts = DraftRepo(db)
        self._runs = RunRepo(db)
        self._exercises = ExerciseRepo(db)

    async def prepare_generation_input(self) -> PlanPreparation:
        """在单一读事务内读取档案／限制／版本／候选／当前计划，作为生成与创建的唯一快照。

        返回值就是读取时刻的业务基线；后续 :meth:`create_plan_draft` 必须凭这一份快照绑定
        ``base_business_version`` 与替换基线，不得在保存时改读最新版本（01 1.3）。
        """
        async with self._db.transaction() as conn:
            snapshot = await self._profiles.read_in_transaction(conn)
            candidates = await self._exercises.list_recommendable_in_transaction(conn)
            current_plan = await self._plans.read_current_in_transaction(conn)
            current_sessions = (
                ()
                if current_plan is None
                else await self._plans.list_sessions_in_transaction(
                    conn, current_plan.id
                )
            )
        return PlanPreparation(
            snapshot=snapshot,
            candidates=candidates,
            current_plan=current_plan,
            current_sessions=current_sessions,
        )

    async def create_plan_draft(
        self,
        *,
        draft_id: str,
        preparation: PlanPreparation,
        conversation_id: str,
        run_id: str | None,
        payload: PlanPayload,
        starts_on: date,
        review_on: date,
        business_date: date,
        mode: PlanMode = "regular",
        patch: ProfilePatch | None = None,
        parent_draft_id: str | None = None,
    ) -> PlanDraftView:
        """按生成快照保存一条 Pending 计划草稿（revision 从 1 起），返回查询形态。

        - **基线只取 ``preparation``**：业务版本、当前计划版本与旧日程都来自准备读取时刻的
          同一快照；生成与保存之间发生的正式提交不改变本草稿的基线与替换预览，过期在首次
          确认时拦截（S3-06）。
        - **拟议内容先校验再落库**：``payload`` 做 D9 结构校验并按准备快照的推荐候选复查目录
          引用（只引用未停用且可推荐、口径一致的动作）；``starts_on``／``review_on`` 必须构成
          ``[starts_on, review_on)`` 并至少投影出一个训练日；``patch`` 走 ``validate_patch``
          并复查具体动作限制引用的目录身份；有补丁时**按应用补丁后的拟议条件**复查计划
          （受限组合：受限或器械不可用的动作在落库前就被拦住，01 1.5）。任一失败不落库。
        - **替换预览**：当前正式计划存在时，取消清单 = 该版本全部「未取消、未存储锁定、且按
          固定业务时区业务日期尚未到期锁定」的日程（04 4.2：仅取消未来未锁定日程）；这正是
          生成读取时刻的提案，``scheduled_sessions`` 不被修改。
        - **来源必须成立**：会话存在、Run 属于同一会话；``source_plan_version_id`` 由准备
          快照给出，不接受调用方传入。不写正式事实、不推进 ``context_version``。
        """
        if mode not in PLAN_MODES:
            raise InvalidPlanPayload(f"计划模式不在已拍集合内：{mode!r}")
        _require_date("business_date", business_date)
        base_profile = preparation.snapshot.profile
        # 拟议字段全量复查（与纠错同一入口）：日期边界与投影非空、目录引用（未停用且可推荐、
        # 口径一致），以及按**补丁后拟议条件**的限制／红旗／器械复查（01 1.5：不能只按旧
        # 条件校验）。任一失败不落库。
        require_valid_proposal_fields(
            starts_on=starts_on,
            review_on=review_on,
            payload=payload,
            base_profile=base_profile,
            candidates=preparation.candidates,
            patch=patch,
        )
        # 迟到草稿（决策 2）：生效范围已全部过去即拒绝，不落库、不向前补齐。
        if business_date >= review_on:
            raise InvalidPlanPayload(
                f"计划的生效范围 [starts_on, review_on) 已全部过去"
                f"（当刻业务日期 {business_date.isoformat()}）：不接受迟到的计划草稿，"
                "请按当前日期重新生成"
            )
        if patch is not None:
            validate_patch(patch)
            await self._require_known_patch_restrictions(patch)
        await require_draft_source(
            self._runs, conversation_id=conversation_id, run_id=run_id
        )
        # 拟议条件：无补丁时等于基线（未建档时用显式全未知档案，不把未建档当已建档）；
        # 有补丁时按补丁应用后的条件保存，确认前不改正式档案（01 1.5）。
        effective_baseline = (
            base_profile if base_profile is not None else Profile.empty()
        )
        proposed_profile = (
            effective_baseline
            if patch is None
            else apply_patch(effective_baseline, patch)
        )
        source_plan = preparation.current_plan
        proposal = PlanProposal(
            starts_on=starts_on,
            review_on=review_on,
            payload=payload,
            source_plan_version_id=None if source_plan is None else source_plan.id,
            mode=mode,
            cancellations=proposed_cancellations(
                preparation.current_sessions, business_date=business_date
            ),
        )
        draft = await self._drafts.create_plan_pending(
            draft_id=draft_id,
            conversation_id=conversation_id,
            run_id=run_id,
            base_profile_json=(
                None if base_profile is None else profile_to_json(base_profile)
            ),
            proposed_profile_json=profile_to_json(proposed_profile),
            proposed_plan_json=proposal_to_json(proposal),
            proposed_profile_patch_json=None if patch is None else patch_to_json(patch),
            base_business_version=preparation.snapshot.context_version,
            parent_draft_id=parent_draft_id,
        )
        return await self._to_view(draft)

    async def get_plan_draft(self, draft_id: str) -> PlanDraftView | None:
        """按身份读取计划草稿（含结构化 Diff）；不存在返回 None，不创建新草稿。"""
        draft = await self._drafts.get(draft_id)
        return None if draft is None else await self._to_view(draft)

    async def revise_plan_draft(
        self,
        *,
        draft_id: str,
        seen_revision: int,
        payload: PlanPayload,
        starts_on: date,
        review_on: date,
    ) -> PlanDraftView:
        """以所见 revision 纠错 Pending 计划草稿：只替换日期与 D9 payload，复查后 revision+1。

        - **只接受已拍可变字段**（stage3.md §5 S3-05；F2-03：日期／训练日／动作候选／组次／
          次数区间／RIR）：调用方能给的只有 ``starts_on``／``review_on``／``payload``，三者
          整体替换拟议计划，但提交的 payload 先与**存储稿**逐字段对比
          （:func:`~domain.plan.rules.validate_payload_correction`）：模板来源（``template_key``）、
          身份（``workout_key``／``item_key``）、训练日展示字段与已验证负荷的记录来源引用
          一律不可改，越界修改拒绝；草稿身份、来源（会话／Run）、``kind``、``base_profile_json``／
          ``base_business_version``、``source_plan_version_id``、``mode``、取消预览、拟议档案
          补丁、状态与提交凭据都不接受传入、也不被本方法改写（写入语句的 SET 列表结构上
          只同步写计划信封与 revision）。
        - **全量重解码重验证**：落库的不是增量补丁而是完整 payload，写前按当前目录候选与
          **补丁应用后的拟议条件**（档案基线＋补丁，无补丁即基线）重新校验结构、目录引用、
          限制／红旗／器械与日期边界；不信任调用方声称「与旧内容相同」，不部分接受非法纠错。
        - **所见 revision 乐观并发控制**：``seen_revision`` 必须等于草稿当前 revision，两个
          页面不得用同一所见版本静默互相覆盖；不符即拒绝且不产生任何写入。
        - 读取草稿、判定终态／kind／revision、复查、写回在同一事务同一连接上完成：与确认
          （S3-06）和丢弃共用唯一锁串行判定最终状态（01 1.4）；任一步异常整体回滚。
        """
        async with self._db.transaction() as conn:
            draft = await self._require_pending_plan_in_transaction(conn, draft_id)
            if draft.revision != seen_revision:
                raise DraftRevisionConflict(
                    f"所见 revision {seen_revision} 与草稿当前 revision "
                    f"{draft.revision} 不符：{draft_id}"
                )
            proposal = proposal_from_json(_require_plan_json(draft))
            # 纠错白名单：先比存储稿，再做全量结构／目录／条件复查（顺序固定：越界修改
            # 即使自身合法也拒绝，不做部分接受）。
            validate_payload_correction(proposal.payload, payload)
            candidates, patch = await self._revision_context_in_transaction(conn, draft)
            base_profile = (
                None
                if draft.base_profile_json is None
                else profile_from_json(draft.base_profile_json)
            )
            require_valid_proposal_fields(
                starts_on=starts_on,
                review_on=review_on,
                payload=payload,
                base_profile=base_profile,
                candidates=candidates,
                patch=patch,
            )
            # 整体替换拟议计划：身份／来源／基线／补丁／取消预览取自**存储的草稿行**，
            # 不取自请求（取消预览绑定生成读取时刻的旧日程事实，不随纠错重算）。
            revised = replace(
                proposal, starts_on=starts_on, review_on=review_on, payload=payload
            )
            updated = await self._drafts.update_plan_proposal_in_transaction(
                conn,
                draft_id=draft_id,
                proposed_plan_json=proposal_to_json(revised),
                expected_revision=seen_revision,
            )
        return await self._to_view(updated)

    async def discard_plan_draft(self, *, draft_id: str) -> PlanDraftView:
        """丢弃 Pending 计划草稿：只改变草稿状态，正式计划／档案／版本不变（01 1.3）。

        - 已 Committed 计划草稿不可被丢弃撤销；已 Discarded 重复丢弃幂等返回已丢弃结果：
          不再写入、不刷新更新时间、不新增任何正式副作用。
        - 已丢弃计划草稿不可确认：本方法不提供恢复路径，确认入口（S3-06）按状态拒绝；
          档案确认入口对 plan kind 直接 :class:`DraftKindMismatch`。
        - 状态读取与写入在同一事务同一连接上完成：与纠错和确认（S3-06）经唯一锁串行。
        """
        async with self._db.transaction() as conn:
            draft = await self._drafts.get_in_transaction(conn, draft_id)
            if draft is None:
                raise UnknownDraft(f"草稿不存在：{draft_id}")
            # kind 分派必须在任何写入之前（见 DraftKindMismatch）。
            if draft.kind != PLAN_DRAFT_KIND:
                raise DraftKindMismatch(
                    f"计划草稿丢弃只适用于 kind={PLAN_DRAFT_KIND}，"
                    f"收到 kind={draft.kind}：{draft_id}"
                )
            if draft.status == "committed":
                raise DraftNotDiscardable(f"已提交草稿不可被丢弃撤销：{draft_id}")
            updated = (
                draft
                if draft.status == "discarded"
                else await self._drafts.record_discard_in_transaction(
                    conn, draft_id=draft_id
                )
            )
        return await self._to_view(updated)

    async def _require_pending_plan_in_transaction(
        self, conn: aiosqlite.Connection, draft_id: str
    ) -> Draft:
        """外层事务内读出可纠错的计划草稿：不存在／kind 不符／非 Pending 一律显式拒绝。

        kind 分派在任何写入之前：计划纠错不得把档案草稿或记录草稿按计划信封解码。终态
        （Committed／Discarded）不可纠错，也不重激活（stage3.md §5 S3-05 验收）。
        """
        draft = await self._drafts.get_in_transaction(conn, draft_id)
        if draft is None:
            raise UnknownDraft(f"草稿不存在：{draft_id}")
        if draft.kind != PLAN_DRAFT_KIND:
            raise DraftKindMismatch(
                f"计划草稿纠错只适用于 kind={PLAN_DRAFT_KIND}，"
                f"收到 kind={draft.kind}：{draft_id}"
            )
        if not draft.is_pending:
            raise DraftNotCorrectable(
                f"草稿已 {draft.status}，不可继续纠错：{draft_id}"
            )
        return draft

    async def _revision_context_in_transaction(
        self, conn: aiosqlite.Connection, draft: Draft
    ) -> tuple[tuple[Exercise, ...], ProfilePatch | None]:
        """纠错复查输入：**当前**目录候选与草稿存储的拟议补丁（均取本事务快照）。

        与创建路径的差别是刻意的：候选取纠错当刻的目录（目录是正式事实，纠错不保留旧
        候选快照，可推荐标记被收回的动作立即不可再用）；补丁取草稿行——纠错不能改补丁，
        故「补丁后的拟议条件」就是基线＋存储补丁。
        """
        candidates = await self._exercises.list_recommendable_in_transaction(conn)
        patch = (
            None
            if draft.proposed_profile_patch_json is None
            else patch_from_json(draft.proposed_profile_patch_json)
        )
        return candidates, patch

    async def list_plan_drafts(self, conversation_id: str) -> tuple[PlanDraftView, ...]:
        """该会话已持久化计划草稿的当前状态，各带结构化 Diff（01 1.2）。"""
        drafts = await self._drafts.list_for_conversation(
            conversation_id, kind=PLAN_DRAFT_KIND
        )
        return tuple([await self._to_view(draft) for draft in drafts])

    async def _require_known_patch_restrictions(self, patch: ProfilePatch) -> None:
        """拟议补丁新增的具体动作限制必须引用目录内身份（含停用动作）。

        删除不复查目录：删除按值匹配基线限制，不引入新的目录引用；模式词表由
        ``validate_patch`` 复查。
        """
        for restriction in patch.add_restrictions:
            if restriction.scope != "specific_action":
                continue
            if await self._exercises.get_by_id(restriction.target) is None:
                raise UnknownExerciseReference(
                    f"限制引用的动作身份不在目录内：{restriction.target}"
                )

    async def _to_view(self, draft: Draft) -> PlanDraftView:
        """行 → 查询形态：解码拟议信封与快照、读绑定来源版本、计算日程与结构化 Diff。

        绑定来源版本按 ``source_plan_version_id`` 读取（历史版本只追加不删除），因此重开或
        后续提交后读到的仍是生成时那一版；缺失即草稿数据损坏，显式失败不静默兜底。
        """
        if draft.kind != PLAN_DRAFT_KIND:
            raise InvalidDraftRow(f"计划草稿形状不适用于 kind={draft.kind}：{draft.id}")
        proposal = proposal_from_json(_require_plan_json(draft))
        validate_payload(proposal.payload)
        base_profile = (
            None
            if draft.base_profile_json is None
            else profile_from_json(draft.base_profile_json)
        )
        proposed_profile = profile_from_json(draft.proposed_profile_json)
        patch = (
            None
            if draft.proposed_profile_patch_json is None
            else patch_from_json(draft.proposed_profile_patch_json)
        )
        base_plan: PlanVersionRecord | None = None
        if proposal.source_plan_version_id is not None:
            base_plan = await self._plans.read_version(proposal.source_plan_version_id)
            if base_plan is None:
                raise InvalidDraftRow(
                    f"草稿绑定的来源计划版本不存在：{proposal.source_plan_version_id}"
                )
        parent_plan_diff = None
        if draft.parent_draft_id is not None:
            parent = await self._drafts.get(draft.parent_draft_id)
            if parent is None:
                raise InvalidDraftRow(
                    f"重算子草稿的旧草稿不存在：{draft.parent_draft_id}"
                )
            parent_proposal = proposal_from_json(_require_plan_json(parent))
            parent_plan_diff = plan_field_diff(proposal, parent_proposal)
        return PlanDraftView(
            draft=draft,
            base_profile=base_profile,
            proposed_profile=proposed_profile,
            proposed_profile_patch=patch,
            proposal=proposal,
            schedules=project_sessions(
                proposal.payload,
                starts_on=proposal.starts_on,
                review_on=proposal.review_on,
            ),
            plan_diff=plan_field_diff(proposal, base_plan),
            profile_diff=(
                None if patch is None else profile_diff(base_profile, proposed_profile)
            ),
            parent_plan_diff=parent_plan_diff,
        )


def _require_plan_json(draft: Draft) -> str:
    """计划草稿的拟议信封文本；缺失即草稿数据损坏，显式失败不静默兜底。"""
    if draft.proposed_plan_json is None:
        raise InvalidDraftRow(f"计划草稿缺少拟议载荷：{draft.id}")
    return draft.proposed_plan_json


def require_valid_proposal_fields(
    *,
    starts_on: date,
    review_on: date,
    payload: PlanPayload,
    base_profile: Profile | None,
    candidates: tuple[Exercise, ...],
    patch: ProfilePatch | None,
) -> None:
    """拟议可变字段的全量确定性复查（创建与纠错同一入口，01 1.5、stage3.md §5 S3-04/S3-05）。

    任一失败抛 :class:`InvalidPlanPayload`，调用方不得落库：

    - 日期：必须是纯 ``date``、构成 ``[starts_on, review_on)`` 且范围内至少投影一个训练日；
    - D9 结构 + 目录引用：``validate_payload`` 按候选复查未停用／可推荐／口径一致；
    - 拟议条件：限制命中、红旗、器械不可用按 **patch 应用后的档案** 复查（不能只按旧条件）。

    不信任调用方关于「与旧内容相同」的声明：纠错给出的是完整 payload，必须整份重验。
    """
    _require_date("starts_on", starts_on)
    _require_date("review_on", review_on)
    catalog = {exercise.id: exercise for exercise in candidates}
    validate_payload(payload, catalog=catalog)
    # 投影同时复查生效范围：范围非法或范围内没有任何训练日都不落库。
    if not project_sessions(payload, starts_on=starts_on, review_on=review_on):
        raise InvalidPlanPayload(
            "生效范围内没有任何训练日（检查 starts_on/review_on/anchor_date/slots）："
            f"{starts_on.isoformat()} / {review_on.isoformat()}"
        )
    violations = plan_condition_violations(
        payload,
        profile=base_profile if base_profile is not None else Profile.empty(),
        candidates=candidates,
        patch=patch,
    )
    if violations:
        raise InvalidPlanPayload("计划与拟议条件冲突，不落库：" + "；".join(violations))


def proposed_cancellations(
    sessions: tuple[ScheduledSessionRecord, ...],
    *,
    business_date: date,
) -> tuple[ProposedSessionCancellation, ...]:
    """旧版日程取消预览：未取消、未存储锁定、且按日期规则尚未到期锁定的名额。

    锁定以固定业务时区的日期规则强制判定（04 4.2：``business_date >= scheduled_on`` 即已
    锁定），存储锁定标记（已确认完成／漏练）同样视为已锁定——两者都不是「唯一依据」，
    而是取并集（与只读投影同一入口）。只做拟议：不写 ``cancelled_at``，正式取消归确认事务
    （S3-06）。
    """
    return tuple(
        ProposedSessionCancellation(
            scheduled_session_id=session.id,
            plan_version_id=session.plan_version_id,
            plan_workout_key=session.plan_workout_key,
            scheduled_on=session.scheduled_on,
        )
        for session in sessions
        if session.cancelled_at is None
        and not session_lock_state(
            session.scheduled_on,
            stored_locked_at=session.locked_at,
            business_date=business_date,
        ).effective
    )


#: 计划 Diff 的对比基线：正式版本，或重算子草稿对应的旧草稿拟议（两者字段同形）。
PlanDiffBaseline = PlanVersionRecord | PlanProposal


def plan_field_diff(
    proposal: PlanProposal, base_plan: PlanDiffBaseline | None
) -> tuple[PlanFieldDiff, ...]:
    """拟议计划相对绑定基线的结构化字段对比；无基线时 before 为 None（不伪造旧值）。

    基准可以是绑定的来源正式版本，也可以是旧草稿的拟议（重算子草稿的「旧草稿 → 新草稿」
    Diff，01 1.6）；两者都有相同的日期／模式／D9 payload 字段，不另造第二套 Diff 逻辑。

    列出计划全部可变语义（行字段、完整训练日处方、日历循环、模板来源、旧日程取消预览），
    不把 D9 payload 压成摘要或文本 diff：处方（组次／次数／RIR／时限）、负荷与校准、渐进、
    展示名、预计时长、循环相位、模板来源任一变化都必须在 Diff 中可见（stage3.md §5
    S3-04）。基准固定为草稿绑定的来源版本，不是查询时的最新正式计划。
    """
    before_payload = None if base_plan is None else base_plan.payload
    return (
        PlanFieldDiff(
            field="starts_on",
            before=None if base_plan is None else base_plan.starts_on,
            after=proposal.starts_on,
        ),
        PlanFieldDiff(
            field="review_on",
            before=None if base_plan is None else base_plan.review_on,
            after=proposal.review_on,
        ),
        PlanFieldDiff(
            field="mode",
            before=None if base_plan is None else base_plan.mode,
            after=proposal.mode,
        ),
        # 模板来源（template_key）：不决定结构，但变更同样要在 Diff 中可见。
        PlanFieldDiff(
            field="template_key",
            before=None if before_payload is None else before_payload.template_key,
            after=proposal.payload.template_key,
        ),
        # 完整训练日结构（含每个动作的处方、负荷、渐进与展示名），不是 key 摘要。
        PlanFieldDiff(
            field="workouts",
            before=None if before_payload is None else before_payload.plan_workouts,
            after=proposal.payload.plan_workouts,
        ),
        # 日历循环（相位与循环槽）：长度或相位变化同样要在 Diff 中可见。
        PlanFieldDiff(
            field="calendar_cycle",
            before=None if before_payload is None else before_payload.calendar_cycle,
            after=proposal.payload.calendar_cycle,
        ),
        PlanFieldDiff(
            field="cancellations",
            before=(),
            after=proposal.cancellations,
        ),
    )


def plan_condition_violations(
    payload: PlanPayload,
    *,
    profile: Profile,
    candidates: tuple[Exercise, ...],
    patch: ProfilePatch | None,
) -> tuple[str, ...]:
    """按**补丁后的拟议条件**复查计划，返回冲突原因（空 = 无冲突）。

    01 1.5：含拟议档案补丁时不能只按旧条件校验计划。生成侧（S3-03）已按拟议条件过滤，
    但落库前的复查不得依赖调用方；本函数在创建路径上重建同一判定：

    - 限制命中（具体动作或动作模式）：拟议限制覆盖了计划内动作即冲突；
    - 红旗：正式或拟议身体情况命中六类红旗即不给处方（02 2.3，红旗独立阻断）；
    - 器械：拟议器械条件不覆盖计划动作的器械变式即冲突（未知变式 fail-closed）。
    """
    effective = apply_patch(profile, patch) if patch is not None else profile
    catalog = {exercise.id: exercise for exercise in candidates}
    actions = tuple(
        catalog[item.exercise_id]
        for workout in payload.plan_workouts
        for item in workout.exercises
        if item.exercise_id in catalog
    )
    safety = evaluate_safety(profile, actions, patch=patch)
    reasons = [
        f"动作 {hit.standard_name}（{hit.exercise_id}）命中限制"
        for hit in safety.restrictions.hits
    ]
    if safety.red_flags.is_blocked:
        reasons.append("身体情况命中红旗，不给结构化处方")
    equipment = _available_equipment(effective)
    for workout in payload.plan_workouts:
        for item in workout.exercises:
            exercise = catalog.get(item.exercise_id)
            if exercise is None:
                continue
            if not equipment_available(exercise.equipment_variant, equipment):
                reasons.append(
                    f"动作 {exercise.standard_name_zh}（{exercise.id}）的器械"
                    f" {exercise.equipment_variant} 在拟议器械条件下不可用"
                )
    return tuple(reasons)


def _available_equipment(profile: Profile) -> tuple[str, ...]:
    """拟议档案器械：known 取值；denied（明确无）取空元组；未知同样按空处理（fail-closed）。"""
    fact = profile.available_equipment
    return fact.value if fact.is_known and fact.value is not None else ()


def require_long_term_revision(
    *,
    payload: PlanPayload,
    review_on: date,
    current: PlanVersionRecord,
) -> None:
    """长期调整版必须真的改当前基线，且保留循环与训练日结构（04 4.5、stage4.md S4-04）。

    用户确认需要调整长期计划后，修订以**原计划为基线**保留宏观周期／复核节点、训练频率与
    未受影响部分；本函数只做确定性边界，任一不符抛 :class:`InvalidPlanPayload` 不落库：

    - **不是原样续期**：拟议 payload 与当前正式计划逐字相同（只有日期信封不同）就不是调整，
      拒绝生成草稿——档案补丁不能代替计划本身的业务变化（2026-09-12 已拍口径）；真实改动
      由 :func:`~domain.plan.service.revise_plan_payload` 的受限修订（仅受影响条目变化）产生；
    - **保留训练频率与循环结构**：日历循环的槽位序列（休息／训练与引用的 ``workout_key``）
      与训练日的 ``workout_key`` 序列必须与基线一致，不按调整之名改频率或改循环；
    - **保留原复核节点**：``review_on`` 必须等于基线的复核日，不延长周期。

    首次建档（无当前正式计划）不走本函数：那不叫调整，也不存在可保留的基线。
    """
    if payload == current.payload:
        raise InvalidPlanPayload(
            "拟议计划与当前基线内容完全相同，也没有真实计划改动：没有实际调整，"
            "不生成长期调整草稿（档案补丁不能代替计划本身的业务变化）"
        )
    if _slot_pattern(payload) != _slot_pattern(current.payload):
        raise InvalidPlanPayload(
            "长期调整必须保留原日历循环结构（休息／训练槽与训练频率）"
        )
    if [workout.workout_key for workout in payload.plan_workouts] != [
        workout.workout_key for workout in current.payload.plan_workouts
    ]:
        raise InvalidPlanPayload("长期调整必须保留原训练日集合与顺序")
    if review_on != current.review_on:
        raise InvalidPlanPayload(
            f"长期调整必须保留原复核节点 {current.review_on.isoformat()}："
            f"收到 {review_on.isoformat()}"
        )


def _slot_pattern(payload: PlanPayload) -> tuple[tuple[str, str | None], ...]:
    """循环结构指纹：槽类型与引用的训练日（相位 ``anchor_date`` 属日期信封，不计入）。"""
    return tuple(
        (
            slot.kind,
            None if slot.kind == "rest" else slot.workout_key,
        )
        for slot in payload.calendar_cycle.slots
    )


def replacement_condition_violations(
    target: ArrangementTarget,
    *,
    profile: Profile,
    catalog: Mapping[str, Exercise],
) -> tuple[str, ...]:
    """当次安排中同等刺激替换的器械与限制复查（04 4.7 的后两项条件）。

    结构、动作模式与主要肌群条件归 ``domain/plan/rules.replacement_violations``；器械可用与
    最新限制冲突需要正式条件，由本函数在**创建与确认两处**用同一入口重建：

    - 器械：拟议（当刻正式）器械条件不覆盖替代动作的器械变式即冲突，未知变式 fail-closed；
    - 限制：替代动作命中最新限制即冲突；已确认限制不得被替换绕开（PRD §5.7）。

    缺失目录行同样列入冲突（宁可不替换，不得读成「无冲突」）。
    """
    equipment = _available_equipment(profile)
    violations: list[str] = []
    for item in target.exercises:
        if item.disposition != "equivalent_replace":
            continue
        replacement = catalog.get(item.replacement_exercise_id or "")
        if replacement is None:
            violations.append(
                f"替代动作身份在目录内读不到：{item.replacement_exercise_id}"
            )
            continue
        if not equipment_available(replacement.equipment_variant, equipment):
            violations.append(
                f"替代动作 {replacement.standard_name_zh}（{replacement.id}）的器械"
                f" {replacement.equipment_variant} 在当前器械条件下不可用"
            )
        hits = evaluate_safety(profile, (replacement,)).restrictions.hits
        violations.extend(
            f"替代动作 {hit.standard_name}（{hit.exercise_id}）命中最新限制"
            for hit in hits
        )
    return tuple(violations)


def _require_date(name: str, value: object) -> None:
    # datetime 是 date 的子类但带时刻；生效范围与业务日期只接受纯日期（07 7.3）。
    if type(value) is not date:
        raise InvalidPlanPayload(f"{name} 必须是 date 日期：{value!r}")
