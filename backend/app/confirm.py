from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import uuid4

import aiosqlite

from app.arrangement_drafts import (
    ARRANGEMENT_DRAFT_KIND,
    require_arrangement_binding,
    require_arrangement_safety,
)
from app.draft_repo import PROFILE_UPDATE_KIND, Draft, DraftRepo, InvalidDraftRow
from app.drafts import (
    DraftKindMismatch,
    DraftRevisionConflict,
    ProfileFieldDiff,
    UnknownDraft,
    profile_diff,
)
from app.plan_drafts import (
    PLAN_DRAFT_KIND,
    proposed_cancellations,
    require_valid_proposal_fields,
)
from app.record_drafts import RECORD_DRAFT_KIND, require_target_items_match
from domain.actions.repo import ExerciseRepo
from domain.plan.repo import (
    ArrangementRevisionRecord,
    PlanRepo,
    PlanVersionRecord,
)
from domain.plan.rules import (
    InvalidArrangementTarget,
    InvalidPlanPayload,
    project_sessions,
    validate_arrangement_target,
)
from domain.plan.schema import (
    arrangement_target_from_json,
    proposal_from_json,
)
from domain.plan.service import plan_limit_violations
from domain.profile.repo import ProfileRepo
from domain.profile.rules import (
    UnknownExerciseReference,
    apply_patch,
    ensure_first_time_complete,
    validate_patch,
    validate_profile_structure,
)
from domain.profile.schema import (
    Profile,
    patch_from_json,
    profile_from_json,
)
from domain.profile.service import ProfileService
from domain.records.repo import RecordRepo, SessionRevisionRecord
from domain.records.rules import (
    InvalidRecordFact,
    record_draft_status,
    validate_record_draft,
)
from domain.records.schema import RecordDraftPayload, record_draft_from_json
from storage.db import Database


def _now() -> str:
    return datetime.now(UTC).isoformat()


class DraftDiscarded(ValueError):
    """草稿已 Discarded：不可确认（§4.2 步骤 2；丢弃不撤销、也不重新激活）。"""


def verifiable_field_changes(
    base: Profile | None, current: Profile | None
) -> tuple[ProfileFieldDiff, ...]:
    """已保存基线与当前正式档案之间**可核实的字段变化**：只列确有差异的字段。

    报告的是两份快照本身的差别，不虚构「谁何时修改」（stage2.md §5 S2-06「信息边界」）。
    任一侧未建档（``None``）时返回空元组：「此前未建档」与「已建档但字段显式全未知」不是
    同一语义，不能把前者摊成逐字段 unknown → known 的假差异；未建档这一事实由
    :attr:`DraftStale.base_profile` / :attr:`DraftStale.current_profile` 为 ``None`` 表达。
    """
    if base is None or current is None:
        return ()
    return tuple(item for item in profile_diff(base, current) if item.changed)


class DraftStale(ValueError):
    """草稿业务基线过期（409 ``draft_stale`` 语义）：基线版本与当前 ``context_version`` 不符。

    这是 §4.2 步骤 3 的拦截点；「过期是基线冲突，不新建持久化生命周期状态」——草稿保持
    Pending、基线／拟议内容／状态按原样可查（Stage 4 重算要凭草稿身份、基线与内容生成新
    草稿）、正式数据不变，用户可据此重算或丢弃。按版本号比较，不做字段快照对比：版本变过
    又恢复到相同值仍算过期（stage2.md §5 S2-06「信息边界」），此时 :attr:`changes` 为空而
    版本对仍说明业务版本已变化。

    实例携带全部可核实信息（供 S2-07 渲染 409 响应体）：两个业务版本、两边解码后的档案
    快照（``None`` = 未建档）与 :func:`verifiable_field_changes` 的结果。无法核实的猜测量
    （修改来源、修改时间、客户端传来的内容）一律不报告。
    """

    error_code = "draft_stale"

    def __init__(
        self,
        *,
        draft_id: str,
        base_business_version: int,
        current_business_version: int,
        base_profile: Profile | None,
        current_profile: Profile | None,
        changes: tuple[ProfileFieldDiff, ...],
    ) -> None:
        changed = ", ".join(item.field for item in changes) or "无字段差异"
        super().__init__(
            f"草稿业务基线 {base_business_version} 与当前 context_version "
            f"{current_business_version} 不符：{draft_id}；可核实字段变化：{changed}"
        )
        self.draft_id = draft_id
        self.base_business_version = base_business_version
        self.current_business_version = current_business_version
        self.base_profile = base_profile
        self.current_profile = current_profile
        self.changes = changes


class NoBusinessChange(ValueError):
    """最终草稿与正式档案完全相同：无业务变更，拒绝提交（stage2.md §8 已拍方案 A）。

    不提交、``context_version`` 不变、草稿保持 Pending（可继续纠错或丢弃），也不返回一份
    像成功的假凭据。已 Committed 草稿仍优先走幂等返回，不受本判定影响。
    """


@dataclass(frozen=True, slots=True)
class ProfileCommitResult:
    """一次确认提交的不可变结果（01 1.3「提交凭据」）。

    至少识别草稿、已提交 revision 与该次提交后的业务版本；写入草稿行后不再被任何路径改写，
    因此后续业务变化（新版本、档案被再次确认更新）不能改写这份结果——重复确认返回的就是
    同一份数据（:attr:`draft_id` + 草稿行的 ``committed_revision`` /
    ``committed_business_version``）。客户端要看最新正式档案时另查只读接口。
    """

    draft_id: str
    committed_revision: int
    committed_business_version: int


def _committed_result(draft: Draft) -> ProfileCommitResult:
    """已 Committed 草稿行 → 提交结果；凭据缺失即草稿数据损坏，显式失败不静默兜底。

    库内 CHECK 保证「Committed 必有凭据、且 ``committed_revision = revision``」；
    违反只可能来自库外改写，按损坏处理（与 :class:`InvalidDraftRow` 同口径）。
    """
    revision = draft.committed_revision
    version = draft.committed_business_version
    if revision is None or version is None:
        raise InvalidDraftRow(f"已 Committed 草稿缺少提交凭据：{draft.id}")
    return ProfileCommitResult(
        draft_id=draft.id,
        committed_revision=revision,
        committed_business_version=version,
    )


@dataclass(frozen=True, slots=True)
class PlanCommitResult:
    """一次计划确认提交的不可变结果（01 1.3「提交凭据」＋该次确认建立的计划版本）。

    凭据部分与 :class:`ProfileCommitResult` 同口径（写成草稿行后不再被改写）；
    ``plan_version_id``／``plan_version`` 指向**首次确认**建立的版本，由
    ``plan_versions.source_draft_id`` 唯一确定（不取「最新版本」，后续替换不会改变本结果），
    因此重复确认、关闭重开、后续业务版本变化后重试都返回同一份数据。日程实例 id 不回传：
    它们是投影结果，可由版本与日期确定，客户端要看日程时另查只读接口。
    """

    draft_id: str
    committed_revision: int
    committed_business_version: int
    plan_version_id: str
    plan_version: int


def _plan_commit_result(draft: Draft, record: PlanVersionRecord) -> PlanCommitResult:
    """已 Committed 草稿行＋该次建立的计划版本 → 计划提交结果。"""
    revision = draft.committed_revision
    version = draft.committed_business_version
    if revision is None or version is None:
        raise InvalidDraftRow(f"已 Committed 草稿缺少提交凭据：{draft.id}")
    return PlanCommitResult(
        draft_id=draft.id,
        committed_revision=revision,
        committed_business_version=version,
        plan_version_id=record.id,
        plan_version=record.version,
    )


@dataclass(frozen=True, slots=True)
class ArrangementCommitResult:
    """一次安排确认提交的不可变结果（01 1.3「提交凭据」＋该次接受的安排修订）。

    凭据部分与 :class:`PlanCommitResult` 同口径（写成草稿行后不再被改写）；
    ``arrangement_revision_id``／``arrangement_revision_no``／``accepted_at`` 指向**首次
    确认**写下的那条修订，由 ``arrangement_revisions.source_draft_id`` 唯一确定（不取「最新
    修订」，后续再次接受不会改变本结果），因此重复确认、关闭重开、后续业务版本变化后重试
    都返回同一份数据。``accepted_at`` 是接受时的**真实时间**（不得倒填），与训练发生的
    时间、训练记录与更正确认时间分列保存（v1 L88–95）。
    """

    draft_id: str
    committed_revision: int
    committed_business_version: int
    arrangement_revision_id: str
    arrangement_revision_no: int
    scheduled_session_id: str
    accepted_at: str


def _arrangement_commit_result(
    draft: Draft, record: ArrangementRevisionRecord
) -> ArrangementCommitResult:
    """已 Committed 草稿行＋该次写下的安排修订 → 安排提交结果。"""
    revision = draft.committed_revision
    version = draft.committed_business_version
    if revision is None or version is None:
        raise InvalidDraftRow(f"已 Committed 草稿缺少提交凭据：{draft.id}")
    return ArrangementCommitResult(
        draft_id=draft.id,
        committed_revision=revision,
        committed_business_version=version,
        arrangement_revision_id=record.id,
        arrangement_revision_no=record.revision_no,
        scheduled_session_id=record.scheduled_session_id,
        accepted_at=record.accepted_at,
    )


@dataclass(frozen=True, slots=True)
class RecordCommitResult:
    """一次训练记录确认（或作废）提交的不可变结果（01 1.3「提交凭据」＋该次写下的修订）。

    凭据部分与:class:`PlanCommitResult` 同口径（写成草稿行后不再被改写）；
    ``training_session_id`` 是该次训练身份（新增或补充的既有身份），
    ``session_revision_id``／``revision_no``／``status`` 指向**首次确认**写下的那条修订，
    由 ``session_revisions.source_draft_id`` 唯一确定（不取「当前修订」，后续更正／作废不会改
    变本结果），因此重复确认、关闭重开、后续业务版本变化后重试都返回同一份数据。
    """

    draft_id: str
    committed_revision: int
    committed_business_version: int
    training_session_id: str
    session_revision_id: str
    revision_no: int
    status: str


def _record_commit_result(
    draft: Draft, record: SessionRevisionRecord
) -> RecordCommitResult:
    """已 Committed 草稿行＋该次写下的训练修订 → 记录提交结果。"""
    revision = draft.committed_revision
    version = draft.committed_business_version
    if revision is None or version is None:
        raise InvalidDraftRow(f"已 Committed 草稿缺少提交凭据：{draft.id}")
    return RecordCommitResult(
        draft_id=draft.id,
        committed_revision=revision,
        committed_business_version=version,
        training_session_id=record.session_id,
        session_revision_id=record.id,
        revision_no=record.revision_no,
        status=record.status,
    )


class ConfirmService:
    """档案草稿确认事务编排（S2-05）：幂等返回、基线／revision 拦截、原子提交。

    ``DraftService`` 负责草稿创建／纠错／丢弃；本类只做确认：正式档案写入、业务版本推进与
    提交凭据都由这里编排，且都在同一次 ``Database.transaction()`` 内完成。
    """

    def __init__(self, db: Database):
        self._db = db
        self._drafts = DraftRepo(db)
        self._profiles = ProfileRepo(db)
        self._writes = ProfileService(db)
        self._exercises = ExerciseRepo(db)
        self._plans = PlanRepo(db)
        self._records = RecordRepo(db)

    async def confirm_profile_draft(
        self, *, draft_id: str, seen_revision: int
    ) -> ProfileCommitResult:
        """确认草稿身份＋用户所见 revision；返回该次提交的不可变结果。

        ``seen_revision`` 只用于「首次确认」的乐观并发检查；草稿已 Committed 时忽略它并直接
        返回原凭据（§4.2 步骤 1：重试不因旧业务版本或旧 revision 失败）。确认请求不携带业务
        内容——写入的永远是数据库保存的最终草稿（§4.2：不信任客户端传入可替换内容）。

        只服务 ``kind='profile_update'``：确认入口按 kind 分派，计划草稿的确认归 S3-06，
        不得在本事务里把计划草稿的 ``proposed_profile_json`` 当正式档案提交（那会把受限组合
        的补丁当成一次独立档案变更，单独推进 ``context_version`` 而不建计划）。
        """
        async with self._db.transaction() as conn:
            draft = await self._drafts.get_in_transaction(conn, draft_id)
            if draft is None:
                raise UnknownDraft(f"草稿不存在：{draft_id}")
            # kind 分派必须在任何读写之前：非档案草稿走各自确认编排（S3-06/S3-08）。
            if draft.kind != PROFILE_UPDATE_KIND:
                raise DraftKindMismatch(
                    f"档案草稿确认只适用于 kind={PROFILE_UPDATE_KIND}，"
                    f"收到 kind={draft.kind}：{draft_id}"
                )
            if draft.status == "committed":
                result = _committed_result(draft)
            elif draft.status == "discarded":
                raise DraftDiscarded(f"草稿已丢弃，不可确认：{draft_id}")
            else:
                result = await self._commit_pending(conn, draft, seen_revision)
        # 事务已 COMMIT（或本就不需要写）：只有到这里才把结果交给调用方（「提交后再响应」）。
        return result

    async def _commit_pending(
        self, conn: aiosqlite.Connection, draft: Draft, seen_revision: int
    ) -> ProfileCommitResult:
        """§4.2 步骤 3–5：在确认事务内检查、复查、写入并读回提交凭据。"""
        snapshot = await self._profiles.read_in_transaction(conn)
        if snapshot.context_version != draft.base_business_version:
            # 只报告可核实的字段变化：保存的基线快照 → 本事务读到的当前正式档案。
            base_profile = (
                None
                if draft.base_profile_json is None
                else profile_from_json(draft.base_profile_json)
            )
            raise DraftStale(
                draft_id=draft.id,
                base_business_version=draft.base_business_version,
                current_business_version=snapshot.context_version,
                base_profile=base_profile,
                current_profile=snapshot.profile,
                changes=verifiable_field_changes(base_profile, snapshot.profile),
            )
        if draft.revision != seen_revision:
            raise DraftRevisionConflict(
                f"所见 revision {seen_revision} 与草稿当前 revision "
                f"{draft.revision} 不符：{draft.id}"
            )
        # 复查数据库保存的最终草稿内容，不信任任何客户端传入内容（§4.2 步骤 4）。
        proposed = profile_from_json(draft.proposed_profile_json)
        validate_profile_structure(proposed)
        await self._require_known_restriction_targets(conn, proposed)
        if snapshot.profile is None:
            ensure_first_time_complete(proposed)
        elif snapshot.profile == proposed:
            raise NoBusinessChange(
                f"最终草稿与正式档案相同，无业务变更，不提交：{draft.id}"
            )
        await self._writes.write_profile_in_transaction(conn, proposed)
        committed_version = await self._profiles.bump_context_version_in_transaction(
            conn
        )
        committed = await self._drafts.record_commit_in_transaction(
            conn,
            draft_id=draft.id,
            committed_revision=draft.revision,
            committed_business_version=committed_version,
        )
        return _committed_result(committed)

    async def confirm_plan_draft(
        self, *, draft_id: str, seen_revision: int, business_date: date
    ) -> PlanCommitResult:
        """确认计划草稿身份＋用户所见 revision；返回该次提交的不可变结果（S3-06）。

        迁移与档案草稿确认同口径：幂等已提交 → 拒绝已丢弃 → 基线／revision → 事务内领域复查
        → 原子写入 → COMMIT 之后才响应。额外步骤（stage3.md §4.2）：

        1. 有拟议档案补丁时先校验补丁，再按**应用补丁后的拟议条件**复查计划；确认前不改正式
           档案（01 1.5）。
        2. 复查计划结构、目录引用、频率／时长／器械／限制／同日重复动作与生效范围（S3-03 同口径）。
        3. 替换：保留旧版本历史 → 只取消旧版**未来未锁定**日程（按固定业务时区的业务日期规则
           重算，不照搬草稿保存的取消预览）→ 写新版本与投影日程。
        4. 有补丁时写正式档案；``context_version`` 恰好 +1；草稿 Committed 与凭据。

        ``business_date`` 是确认当刻的**固定业务时区日期**（07 7.3），只用于「已到期即锁定」
        的规则判定：``business_date >= scheduled_on`` 的旧日程即使未写存储锁定标记也不取消；
        调用方（S3-14）负责按业务时区算出这个日期，本编排不自己取「今天」（可注入、可测试）。

        只服务 ``kind='plan'``：档案草稿与安排／记录草稿的确认走各自入口；不得在本事务里把
        计划草稿的拟议条件当一次独立档案变更提交（受限组合只推一次 ``context_version``）。
        """
        if type(business_date) is not date:
            raise InvalidPlanPayload(
                f"business_date 必须是 date 日期：{business_date!r}"
            )
        async with self._db.transaction() as conn:
            draft = await self._drafts.get_in_transaction(conn, draft_id)
            if draft is None:
                raise UnknownDraft(f"草稿不存在：{draft_id}")
            # kind 分派必须在任何读写之前：非计划草稿走各自确认编排（S2-05/S3-08）。
            if draft.kind != PLAN_DRAFT_KIND:
                raise DraftKindMismatch(
                    f"计划草稿确认只适用于 kind={PLAN_DRAFT_KIND}，"
                    f"收到 kind={draft.kind}：{draft_id}"
                )
            if draft.status == "committed":
                result = await self._committed_plan_result(conn, draft)
            elif draft.status == "discarded":
                raise DraftDiscarded(f"草稿已丢弃，不可确认：{draft_id}")
            else:
                result = await self._commit_pending_plan(
                    conn, draft, seen_revision, business_date
                )
        # 事务已 COMMIT（或本就不需要写）：只有到这里才把结果交给调用方（「提交后再响应」）。
        return result

    async def _committed_plan_result(
        self, conn: aiosqlite.Connection, draft: Draft
    ) -> PlanCommitResult:
        """已 Committed 计划草稿的幂等返回（§4.2 步骤 1）。

        不重查基线、不重算领域规则、不重复建版本或日程；该次建立的计划版本由
        ``source_draft_id`` 唯一确定，因此凭据可以指向首次确认那一版（而不是「当前最新」）。
        缺版本行即数据损坏，显式失败不静默兜底（与 :class:`InvalidDraftRow` 同口径）。
        """
        record = await self._plans.read_by_source_draft_in_transaction(conn, draft.id)
        if record is None:
            raise InvalidDraftRow(
                f"已 Committed 计划草稿没有对应的计划版本：{draft.id}"
            )
        return _plan_commit_result(draft, record)

    async def _commit_pending_plan(
        self,
        conn: aiosqlite.Connection,
        draft: Draft,
        seen_revision: int,
        business_date: date,
    ) -> PlanCommitResult:
        """§4.2 步骤 3–5 的计划版：基线／revision → 复查 → 原子写入 → 读回凭据。"""
        snapshot = await self._profiles.read_in_transaction(conn)
        if snapshot.context_version != draft.base_business_version:
            base_profile = (
                None
                if draft.base_profile_json is None
                else profile_from_json(draft.base_profile_json)
            )
            raise DraftStale(
                draft_id=draft.id,
                base_business_version=draft.base_business_version,
                current_business_version=snapshot.context_version,
                base_profile=base_profile,
                current_profile=snapshot.profile,
                changes=verifiable_field_changes(base_profile, snapshot.profile),
            )
        if draft.revision != seen_revision:
            raise DraftRevisionConflict(
                f"所见 revision {seen_revision} 与草稿当前 revision "
                f"{draft.revision} 不符：{draft.id}"
            )
        # 复查数据库保存的最终草稿（不信任客户端传入内容）：缺档案时 fail-closed 不给处方。
        if draft.proposed_plan_json is None:
            raise InvalidDraftRow(f"计划草稿缺少拟议载荷：{draft.id}")
        proposal = proposal_from_json(draft.proposed_plan_json)
        if snapshot.profile is None:
            raise InvalidPlanPayload(
                f"尚未建立正式档案：不给结构化处方，计划草稿不可确认：{draft.id}"
            )
        patch = (
            None
            if draft.proposed_profile_patch_json is None
            else patch_from_json(draft.proposed_profile_patch_json)
        )
        if patch is not None:
            validate_patch(patch)
        proposed_profile = (
            apply_patch(snapshot.profile, patch)
            if patch is not None
            else snapshot.profile
        )
        # 拟议条件必须能从「本事务正式档案＋草稿补丁」重建；不一致即草稿数据损坏。
        if profile_from_json(draft.proposed_profile_json) != proposed_profile:
            raise InvalidDraftRow(
                f"计划草稿的拟议条件与正式档案＋拟议补丁不一致：{draft.id}"
            )
        await self._require_known_restriction_targets(conn, proposed_profile)
        candidates = await self._exercises.list_recommendable_in_transaction(conn)
        # 计划结构／目录引用／日程与生效范围，以及按补丁后条件的限制／红旗／器械复查。
        require_valid_proposal_fields(
            starts_on=proposal.starts_on,
            review_on=proposal.review_on,
            payload=proposal.payload,
            base_profile=snapshot.profile,
            candidates=candidates,
            patch=patch,
        )
        limit_violations = plan_limit_violations(proposed_profile, proposal.payload)
        if limit_violations:
            raise InvalidPlanPayload(
                "计划与拟议条件的频率／时长上限冲突，不确认："
                + "；".join(limit_violations)
            )
        # 迟到确认（决策 2）：生效范围 [starts_on, review_on) 已全部过去即拒绝，不向前补齐。
        if business_date >= proposal.review_on:
            raise InvalidPlanPayload(
                "计划的生效范围 [starts_on, review_on) 已全部过去"
                f"（当刻业务日期 {business_date.isoformat()}）：不接受迟到的计划确认，"
                "请按当前日期重新生成计划草稿"
            )
        # 未过去的只从**确认业务日期当天**起投影日程（确认日保留），不生成确认日前的名额；
        # 因此确认日前的日子不进入完成率分母（06 6.1），starts_on／review_on 行字段不变。
        effective_starts_on = max(proposal.starts_on, business_date)
        # 只替换「当刻正式计划」：草稿绑定的来源版本与它不一致时拒结，否则会把未基于该版本的
        # 草稿用在当前版本上（取消错版本日程、留下两份未取消的未来日程）。
        current = await self._plans.read_current_in_transaction(conn)
        if proposal.source_plan_version_id != (None if current is None else current.id):
            raise InvalidPlanPayload(
                f"计划草稿绑定的来源版本 {proposal.source_plan_version_id!r} 与当刻正式计划"
                f" {None if current is None else current.id!r} 不一致：{draft.id}"
            )
        now = _now()
        if current is not None:
            old_sessions = await self._plans.list_sessions_in_transaction(
                conn, current.id
            )
            # 按当刻业务日期重算取消集（仅未来未锁定），不照搬草稿里的取消预览。
            cancel_ids = tuple(
                item.scheduled_session_id
                for item in proposed_cancellations(
                    old_sessions, business_date=business_date
                )
            )
            await self._plans.cancel_sessions_in_transaction(
                conn, session_ids=cancel_ids, cancelled_at=now
            )
        plan_version_id = uuid4().hex
        record = await self._plans.append_version_in_transaction(
            conn,
            plan_version_id=plan_version_id,
            source_plan_version_id=proposal.source_plan_version_id,
            starts_on=proposal.starts_on,
            review_on=proposal.review_on,
            mode=proposal.mode,
            payload=proposal.payload,
            source_draft_id=draft.id,
            confirmed_at=now,
        )
        projected = project_sessions(
            proposal.payload,
            starts_on=effective_starts_on,
            review_on=proposal.review_on,
        )
        if not projected:
            raise InvalidPlanPayload(
                "确认日到复核日之间没有任何训练日：计划的剩余生效范围没有可执行日程，"
                f"不接受本次确认（{effective_starts_on.isoformat()} / "
                f"{proposal.review_on.isoformat()}）"
            )
        await self._plans.insert_sessions_in_transaction(
            conn,
            sessions=tuple(
                (uuid4().hex, plan_version_id, item.plan_workout_key, item.scheduled_on)
                for item in projected
            ),
        )
        if patch is not None:
            await self._writes.write_profile_in_transaction(conn, proposed_profile)
        committed_version = await self._profiles.bump_context_version_in_transaction(
            conn
        )
        committed = await self._drafts.record_commit_in_transaction(
            conn,
            draft_id=draft.id,
            committed_revision=draft.revision,
            committed_business_version=committed_version,
        )
        return _plan_commit_result(committed, record)

    async def confirm_arrangement_draft(
        self, *, draft_id: str, seen_revision: int, business_date: date
    ) -> ArrangementCommitResult:
        """确认安排草稿身份＋用户所见 revision；返回该次提交的不可变结果（S3-08）。

        迁移与档案／计划草稿确认同口径：幂等已提交 → 拒绝已丢弃 → 基线／revision → 事务内
        领域复查 → 原子写入 → COMMIT 之后才响应。额外步骤只有一步：把存储的当次目标
        **完整**快照写入 ``arrangement_revisions``（不只差异补丁，04 4.3）。

        - **接受即落盘的真实时间**：``accepted_at`` 取本事务的当刻 UTC 时间，不接受调用方
          传入，也不从训练日或草稿时间倒推（PRD §5.5：不得先在会话中视为已接受、等打卡时
          再补写）。训练发生时间、系统接受时间、记录与更正确认时间分列保存。
        - **过期按绑定的训练日判定**：``business_date`` 是确认当刻的**固定业务时区日期**
          （07 7.3，服务端算出、不接受客户端传入）。 ``business_date > scheduled_on`` 即该次
          训练日已过，草稿不可再接受、必须重新生成（04 4.3）；训练日当天仍可接受。本口径
          不叫用长期计划的「生成日 +1」规则。
        - **临时调整不改长期计划版本**：本事务不写 ``plan_versions``、不写
          ``scheduled_sessions``、不改正式档案，只追加安排修订并将 ``context_version``
          恰好 +1（01 1.4、04 4.3）。
        - **锁定与处方接受分离**：已到期锁定的日程仍可接受减组等调整（04 4.2/4.3），只要
          它未被取消；已取消的日程不再是应训练义务，拒结（04 4.2）。
        - **只用内部分派**：只服务 ``kind='arrangement'``；档案与计划草稿的确认走各自入口。
          安排草稿不接受档案补丁，也不推进计划版本（不得把当次目标当长期计划提交）。
        """
        if type(business_date) is not date:
            raise InvalidPlanPayload(
                f"business_date 必须是 date 日期：{business_date!r}"
            )
        async with self._db.transaction() as conn:
            draft = await self._drafts.get_in_transaction(conn, draft_id)
            if draft is None:
                raise UnknownDraft(f"草稿不存在：{draft_id}")
            # kind 分派必须在任何读写之前：非安排草稿走各自确认编排（S2-05/S3-06）。
            if draft.kind != ARRANGEMENT_DRAFT_KIND:
                raise DraftKindMismatch(
                    f"安排草稿确认只适用于 kind={ARRANGEMENT_DRAFT_KIND}，"
                    f"收到 kind={draft.kind}：{draft_id}"
                )
            if draft.status == "committed":
                result = await self._committed_arrangement_result(conn, draft)
            elif draft.status == "discarded":
                raise DraftDiscarded(f"草稿已丢弃，不可确认：{draft_id}")
            else:
                result = await self._commit_pending_arrangement(
                    conn, draft, seen_revision, business_date
                )
        # 事务已 COMMIT（或本就不需要写）：只有到这里才把结果交给调用方（「提交后再响应」）。
        return result

    async def _committed_arrangement_result(
        self, conn: aiosqlite.Connection, draft: Draft
    ) -> ArrangementCommitResult:
        """已 Committed 安排草稿的幂等返回（§4.2 步骤 1）。

        不重查基线、不重算领域规则、不重复追加修订；该次写下的安排修订由
        ``arrangement_revisions.source_draft_id`` 唯一确定，因此 ``accepted_at`` 就是首次
        接受时间，后续再次接受不会改写它（不得倒填）。缺修订行即数据损坏，显式失败不静默
        兕底（与 :class:`InvalidDraftRow` 同口径）。
        """
        record = await self._plans.read_arrangement_by_source_draft_in_transaction(
            conn, draft.id
        )
        if record is None:
            raise InvalidDraftRow(
                f"已 Committed 安排草稿没有对应的安排修订：{draft.id}"
            )
        return _arrangement_commit_result(draft, record)

    async def _commit_pending_arrangement(
        self,
        conn: aiosqlite.Connection,
        draft: Draft,
        seen_revision: int,
        business_date: date,
    ) -> ArrangementCommitResult:
        """§4.2 步骤 3–5 的安排版：基线／revision → 绑定与结构复查 → 写入修订 → 读回凭据。"""
        snapshot = await self._profiles.read_in_transaction(conn)
        if snapshot.context_version != draft.base_business_version:
            base_profile = (
                None
                if draft.base_profile_json is None
                else profile_from_json(draft.base_profile_json)
            )
            raise DraftStale(
                draft_id=draft.id,
                base_business_version=draft.base_business_version,
                current_business_version=snapshot.context_version,
                base_profile=base_profile,
                current_profile=snapshot.profile,
                changes=verifiable_field_changes(base_profile, snapshot.profile),
            )
        if draft.revision != seen_revision:
            raise DraftRevisionConflict(
                f"所见 revision {seen_revision} 与草稿当前 revision "
                f"{draft.revision} 不符：{draft.id}"
            )
        # 复查数据库保存的最终草稿（不信任客户端传入内容）。
        if draft.proposed_arrangement_json is None:
            raise InvalidDraftRow(f"安排草稿缺少目标快照：{draft.id}")
        target = arrangement_target_from_json(draft.proposed_arrangement_json)
        version = await self._plans.read_version_in_transaction(
            conn, target.plan_version_id
        )
        if version is None:
            raise InvalidArrangementTarget(
                f"当次目标绑定的计划版本不存在：{target.plan_version_id}"
            )
        session = await self._plans.read_session_in_transaction(
            conn, target.scheduled_session_id
        )
        if session is None:
            raise InvalidArrangementTarget(
                f"当次目标绑定的应训练名额不存在：{target.scheduled_session_id}"
            )
        if session.cancelled_at is not None:
            # 已取消日程不再是应训练义务，接受新安排拒结（04 4.2）；查询仍可看历史草稿。
            raise InvalidArrangementTarget(
                f"已取消的日程不再是应训练义务，不接受当次安排：{session.id}"
            )
        if business_date > session.scheduled_on:
            # 当次安排按其**绑定的训练日**判过期（不是长期计划的「生成日 +1」口径）：
            # 训练日已过则拒结并重新生成，不事后倒改执行标准（04 4.3）。
            raise InvalidArrangementTarget(
                f"绑定的训练日已过，当次安排不可再接受，请重新生成：{session.id} "
                f"{session.scheduled_on.isoformat()} < {business_date.isoformat()}"
            )
        # 绑定与训练日复查：返回绑定版本里的该训练日；再按**本事务当刻条件**复核整份计划、
        # 当次目标实际动作与替换等价（普通新报限制或红旗在落库前就阻断）。
        workout = require_arrangement_binding(target, version=version, session=session)
        catalog = await self._exercises.list_all_in_transaction(conn)
        validate_arrangement_target(
            target, workout=workout, catalog={item.id: item for item in catalog}
        )
        require_arrangement_safety(
            target,
            version=version,
            profile=snapshot.profile,
            catalog={item.id: item for item in catalog},
        )
        accepted_at = _now()
        record = await self._plans.append_arrangement_revision_in_transaction(
            conn,
            arrangement_revision_id=uuid4().hex,
            scheduled_session_id=target.scheduled_session_id,
            target=target,
            source_draft_id=draft.id,
            accepted_at=accepted_at,
        )
        committed_version = await self._profiles.bump_context_version_in_transaction(
            conn
        )
        committed = await self._drafts.record_commit_in_transaction(
            conn,
            draft_id=draft.id,
            committed_revision=draft.revision,
            committed_business_version=committed_version,
        )
        return _arrangement_commit_result(committed, record)

    async def confirm_record_draft(
        self, *, draft_id: str, seen_revision: int
    ) -> RecordCommitResult:
        """确认训练记录草稿（S3-11）：新增训练身份或追加完整修订。

        迁移与档案／计划／安排草稿确认同口径：幂等已提交 → 拒绝已丢弃 → 基线／revision →
        事务内领域复查 → 原子写入 → COMMIT 之后才响应。额外步骤只有一步：把存储的最终载荷
        落成完整修订。

        - **稳定身份与同日多练**（05 5.1／5.4）：载荷的 ``training_session_id`` 是显式归属——
          ``None`` 时**新建**一个 ``training_sessions`` 身份（同日多练各自身份，不按日期合并），
          给出 id 时向该身份**追加**修订（补充同次不增加训练次数）。本编排不按日期、安排或
          当前计划推断归属。
        - **完整修订与原子切换**（05 5.3）：每笔写完整动作与逐组事实，随即把
          ``current_revision_id`` 切到新修订；旧修订保留（只追加不删除），旧修订不再是当前事实。
        - **原样落盘**（01 1.4）：写入的永远是数据库保存的最终草稿——显式 ``assistance='none'``
          落成 ``none``，未明确的可空事实保持 NULL（不替确认卡归类，05 5.5）；
          ``arrangement_revision_id`` 是执行时所依据的那条安排修订，不重解析「当刻最新」。
        - **一次确认只推一次版本**：``context_version`` 恰好 +1，草稿 Committed 与凭据同事务。
        """
        return await self._confirm_record(
            draft_id=draft_id, seen_revision=seen_revision, void=False
        )

    async def void_record_draft(
        self, *, draft_id: str, seen_revision: int
    ) -> RecordCommitResult:
        """作废整次训练（S3-11）：向同一身份追加 ``voided`` 修订并切换当前指针。

        与 :meth:`confirm_record_draft` 同一套拦截与事务，只在写入时把修订状态定为
        ``voided``（事实仍完整落盘便于审计）。

        - **不物理删除、不回退**（05 5.3）：旧修订与其动作／组事实全部保留；当前修订为作废时
          整次退出统计，**不回退采用**旧有效版本（统计口径归 S3-12）。
        - **必须绑定既有身份**：尚未建立的训练无法「作废」（新增即作废是无意义的空事实），
          载荷 ``training_session_id=None`` 时拒结。
        - 身份与次数不变（不新增 ``training_sessions``）、``context_version`` 恰好 +1、
          重复确认返回原凭据且不重复追加修订。
        """
        return await self._confirm_record(
            draft_id=draft_id, seen_revision=seen_revision, void=True
        )

    async def _confirm_record(
        self, *, draft_id: str, seen_revision: int, void: bool
    ) -> RecordCommitResult:
        """记录确认／作废的共同事务形状：幂等返回 → 状态分派 → 事务内复查与写入。"""
        async with self._db.transaction() as conn:
            draft = await self._drafts.get_in_transaction(conn, draft_id)
            if draft is None:
                raise UnknownDraft(f"草稿不存在：{draft_id}")
            # kind 分派必须在任何读写之前：非记录草稿走各自确认编排（S2-05/S3-06/S3-08）。
            if draft.kind != RECORD_DRAFT_KIND:
                raise DraftKindMismatch(
                    f"记录草稿确认只适用于 kind={RECORD_DRAFT_KIND}，"
                    f"收到 kind={draft.kind}：{draft_id}"
                )
            if draft.status == "committed":
                result = await self._committed_record_result(conn, draft)
            elif draft.status == "discarded":
                raise DraftDiscarded(f"草稿已丢弃，不可确认：{draft_id}")
            else:
                result = await self._commit_pending_record(
                    conn, draft, seen_revision, void=void
                )
        # 事务已 COMMIT（或本就不需要写）：只有到这里才把结果交给调用方（「提交后再响应」）。
        return result

    async def _committed_record_result(
        self, conn: aiosqlite.Connection, draft: Draft
    ) -> RecordCommitResult:
        """已 Committed 记录草稿的幂等返回（§4.2 步骤 1）。

        不重查基线、不重算领域规则、不重复追加修订；该次写下的修订由 ``source_draft_id``
        唯一确定，因此结果指向首次确认那一笔（不是后续更正／作废后的「当前修订」）。缺修订行
        即数据损坏，显式失败不静默兜底（与:class:`InvalidDraftRow` 同口径）。
        """
        record = await self._records.read_by_source_draft_in_transaction(conn, draft.id)
        if record is None:
            raise InvalidDraftRow(
                f"已 Committed 记录草稿没有对应的训练修订：{draft.id}"
            )
        return _record_commit_result(draft, record)

    async def _commit_pending_record(
        self,
        conn: aiosqlite.Connection,
        draft: Draft,
        seen_revision: int,
        *,
        void: bool,
    ) -> RecordCommitResult:
        """§4.2 步骤 3–5 的记录版：基线／revision → 复查 → 追加修订并切换指针 → 读回凭据。"""
        snapshot = await self._profiles.read_in_transaction(conn)
        if snapshot.context_version != draft.base_business_version:
            base_profile = (
                None
                if draft.base_profile_json is None
                else profile_from_json(draft.base_profile_json)
            )
            raise DraftStale(
                draft_id=draft.id,
                base_business_version=draft.base_business_version,
                current_business_version=snapshot.context_version,
                base_profile=base_profile,
                current_profile=snapshot.profile,
                changes=verifiable_field_changes(base_profile, snapshot.profile),
            )
        if draft.revision != seen_revision:
            raise DraftRevisionConflict(
                f"所见 revision {seen_revision} 与草稿当前 revision "
                f"{draft.revision} 不符：{draft.id}"
            )
        # 复查数据库保存的最终草稿（不信任客户端传入内容，§4.2 步骤 4）。
        if draft.proposed_record_json is None:
            raise InvalidDraftRow(f"记录草稿缺少拟议载荷：{draft.id}")
        payload = record_draft_from_json(draft.proposed_record_json)
        validate_record_draft(payload)
        for item in payload.exercises:
            if (
                await self._exercises.get_by_id_in_transaction(
                    conn, item.facts.exercise_id
                )
                is None
            ):
                raise UnknownExerciseReference(
                    f"记录动作不在目录内：{item.facts.exercise_id}"
                )
        if payload.arrangement_revision_id is not None:
            arrangement = await self._plans.read_arrangement_revision_in_transaction(
                conn, payload.arrangement_revision_id
            )
            if arrangement is None:
                raise InvalidArrangementTarget(
                    f"安排修订不存在：{payload.arrangement_revision_id}"
                )
            require_target_items_match(payload, arrangement)
        now = _now()
        session_id, previous_revision_id, revision_no = await self._resolve_target(
            conn, draft, payload, void=void, created_at=now
        )
        record = await self._records.append_revision_in_transaction(
            conn,
            revision_id=uuid4().hex,
            session_id=session_id,
            revision_no=revision_no,
            previous_revision_id=previous_revision_id,
            status="voided" if void else record_draft_status(payload),
            source_draft_id=draft.id,
            confirmed_at=now,
            payload=payload,
        )
        committed_version = await self._profiles.bump_context_version_in_transaction(
            conn
        )
        committed = await self._drafts.record_commit_in_transaction(
            conn,
            draft_id=draft.id,
            committed_revision=draft.revision,
            committed_business_version=committed_version,
        )
        return _record_commit_result(committed, record)

    async def _resolve_target(
        self,
        conn: aiosqlite.Connection,
        draft: Draft,
        payload: RecordDraftPayload,
        *,
        void: bool,
        created_at: str,
    ) -> tuple[str, str | None, int]:
        """把显式归属解析成（训练身份 id、被替换的当前修订、本笔修订号）。

        ``payload.training_session_id is None`` 是显式的「新增一次训练」：同事务建立新身份，
        本笔为 ``revision_no=1`` 且无前序修订。给出 id 时只向该既有身份追加：修订号按当前修订
        +1（只追加、恰好 +1），``previous_revision_id`` 取当前修订（历史链不断），绝不按日期
        挑选身份（05 5.4）。作废不能作用在尚未建立的训练上。

        作废即终态（05 5.3；2026-09-13 用户拍板 A）：当前修订为 ``voided`` 的身份不再接受
        任何后续修订——更正、复活与补全都 fail-closed 拒绝，复用既有 ``invalid_request``
        语义（``InvalidRecordFact`` → 422），不新增错误码。只约束当前修订已作废的身份：
        ``valid``／``incomplete`` 记录的更正与补全照旧，历史修订与稳定身份不动。
        """
        training_session_id = payload.training_session_id
        if training_session_id is None:
            if void:
                raise InvalidRecordFact(
                    f"作废必须绑定既有训练身份，不能作废尚未建立的训练：{draft.id}"
                )
            session_id = uuid4().hex
            await self._records.create_session_in_transaction(
                conn, session_id=session_id, created_at=created_at
            )
            return (session_id, None, 1)
        session = await self._records.read_session_in_transaction(
            conn, training_session_id
        )
        if session is None or session.current is None:
            raise InvalidDraftRow(
                f"记录草稿绑定的训练身份不存在或没有当前修订：{training_session_id}"
            )
        if session.current.status == "voided":
            raise InvalidRecordFact(
                "训练身份的当前修订已作废（终态），不再接受后续更正／复活／补全修订："
                f"{training_session_id}"
            )
        return (
            session.id,
            session.current_revision_id,
            session.current.revision_no + 1,
        )

    async def _require_known_restriction_targets(
        self, conn: aiosqlite.Connection, proposed: Profile
    ) -> None:
        """复查具体动作限制引用目录内身份（含停用动作）；模式词表由结构校验覆盖。

        与草稿创建／纠错同口径，但复用当前事务连接（不嵌套取锁）；目录读取走
        ``ExerciseRepo.get_by_id_in_transaction``，因此读到的是本事务自己的快照。
        """
        for restriction in proposed.restrictions:
            if restriction.scope != "specific_action":
                continue
            if (
                await self._exercises.get_by_id_in_transaction(conn, restriction.target)
                is None
            ):
                raise UnknownExerciseReference(
                    f"限制引用的动作身份不在目录内：{restriction.target}"
                )
