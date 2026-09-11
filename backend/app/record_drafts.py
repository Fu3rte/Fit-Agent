"""训练记录草稿应用层（S3-10）：同一快照准备、创建、查询与结构化 Diff、纠错。

正本：stage3.md §5 S3-10、04 → 05（训练身份、同日多练、待补全转正式）、01 1.3（草稿
生命周期与基线绑定）、§5 S3-05（纠错白名单与乐观并发的同口径）。

五条硬边界：

- **同一快照准备**：:meth:`RecordDraftService.prepare_input` 在单一读事务内读正式档案与
  ``context_version``、以及该日期**现有训练身份**；创建只凭这份快照绑定
  ``base_business_version`` 与发生日期，不在保存时改读最新状态（01 1.3）。
- **内部创建，不开 HTTP 面**：Pending 记录草稿只经本服务创建（stage3.md §3：不发布公开
  建草稿路由）；来源关联现有会话／Run 身份，``run_id`` 可空（本阶段没有真实模型执行）。
- **归属与安排关联显式，绝不推断**（05 5.1／5.2／5.4）：``training_session_id`` 是必填
  关键字参数（无默认值）——``None`` 是显式的「新增一次训练」，给出 id 才是补充／更正既有
  身份；日期不唯一，同日多练时准备结果会把该日既有身份列出来供调用方显式选择，本服务
  不按日期猜测归属。``arrangement_revision_id`` 同样可空且只由调用方显式给出，绝不从日期
  或当前计划推断（05 5.1：无可信安排时允许不关联计划，不强行套用）。
- **不落正式事实**：创建、查询与纠错都不写 ``training_sessions``／``session_revisions``／
  ``exercise_logs``／``training_sets``、不改档案、不推进 ``context_version``。待补全载荷
  允许保存（D8 已拍 A）；确认、更正落盘与作废归 S3-11 的确认事务。
- **纠错只作用于 Pending 的允许字段**：以所见 revision 做乐观并发控制，整体替换载荷后
  revision+1；归属（``training_session_id``）与安排关联（``arrangement_revision_id``）不可
  经纠错改写（05 5.2「目标不唯一必须询问」），终态（Committed／Discarded）不可纠错。
"""

from dataclasses import dataclass
from datetime import date
from typing import Any

import aiosqlite

from app.draft_repo import Draft, DraftRepo, InvalidDraftRow
from app.drafts import (
    DraftKindMismatch,
    DraftNotCorrectable,
    DraftRevisionConflict,
    UnknownDraft,
    require_draft_source,
)
from domain.actions.repo import ExerciseRepo
from domain.plan.repo import ArrangementRevisionRecord, PlanRepo
from domain.plan.rules import InvalidArrangementTarget
from domain.profile.repo import ProfileRepo
from domain.profile.rules import UnknownExerciseReference
from domain.profile.schema import (
    Profile,
    ProfileSnapshot,
    profile_from_json,
    profile_to_json,
)
from domain.records.repo import RecordRepo, TrainingSessionRecord
from domain.records.rules import (
    record_draft_status,
    validate_record_draft,
    validate_record_draft_correction,
)
from domain.records.schema import (
    DraftExerciseLog,
    RecordDraftPayload,
    RecordRevisionStatus,
    TimePrecision,
    record_draft_from_json,
    record_draft_to_json,
)
from storage.db import Database
from storage.run_repo import RunRepo

RECORD_DRAFT_KIND = "training_record"


@dataclass(frozen=True, slots=True)
class RecordPreparation:
    """同一快照的准备输入：正式档案与业务版本、发生日期、该日现有训练身份。

    ``same_day_sessions`` 是读取时刻该日期的既有训练身份（按各自当前修订的实际发生日期
    归属，05 5.4）：同日多练时归属不唯一，调用方**必须**在创建时显式选择（新身份传
    ``None``，补充／更正传对应 id）；本快照只提供选择依据，不代替选择、不按日期推断。
    """

    occurred_on: date
    snapshot: ProfileSnapshot
    same_day_sessions: tuple[TrainingSessionRecord, ...]


@dataclass(frozen=True, slots=True)
class RecordFieldDiff:
    """单个记录字段的「基线修订 → 拟议」对比；新增训练（无基线）时 ``before`` 为 None。

    ``field`` 取记录字段名（``occurred_on``／``started_at``／``time_precision``／
    ``arrangement_revision_id``／``completion_declared``／``is_return_phase``／``feedback``／
    ``exercises``）；``exercises`` 保持结构化（动作事实 + 逐组事实），不压成文本 diff。
    ``before`` 为 None 与空集合不是同一语义（新增训练与「基线里没有该事实」可区分）。
    """

    field: str
    before: Any
    after: Any

    @property
    def changed(self) -> bool:
        """基线与拟议是否不同；无基线（None）与空集合仍可区分。"""
        return self.before != self.after


@dataclass(frozen=True, slots=True)
class RecordDraftView:
    """记录草稿的当前查询形态：行数据 + 解码后的拟议载荷 + 派生状态 + 结构化 Diff。

    ``baseline`` 是既有训练身份当前修订的完整事实（``training_session_id=None`` 时为 None，
    表示新增一次训练、无既有修订可对照）；``status`` 按事实完整性派生
    （:func:`domain.records.rules.record_draft_status`），不存第二份状态。Diff 由后端按存储
    载荷与库内基线现算，不信任客户端传入的 before／after。
    """

    draft: Draft
    payload: RecordDraftPayload
    status: RecordRevisionStatus
    baseline: RecordDraftPayload | None
    diff: tuple[RecordFieldDiff, ...]


class RecordDraftService:
    """记录草稿生命周期应用层（S3-10）：准备、创建、查询、Diff 与纠错。

    确认（含 ``incomplete`` 转正、更正追加修订、作废）与统计不在本类；本类不写正式事实、
    不推进 ``context_version``。
    """

    def __init__(self, db: Database):
        self._db = db
        self._profiles = ProfileRepo(db)
        self._drafts = DraftRepo(db)
        self._runs = RunRepo(db)
        self._exercises = ExerciseRepo(db)
        self._plans = PlanRepo(db)
        self._sessions = RecordRepo(db)

    async def prepare_input(self, occurred_on: date) -> RecordPreparation:
        """在单一读事务内读档案与版本、该日现有训练身份，作为创建的唯一快照。

        返回值就是读取时刻的业务基线；后续 :meth:`create_record_draft` 必须凭这一份快照
        绑定 ``base_business_version`` 与 ``occurred_on``，不得在保存时改读最新状态（01 1.3）。
        """
        async with self._db.transaction() as conn:
            snapshot = await self._profiles.read_in_transaction(conn)
            same_day_sessions = await self._sessions.list_sessions_on_in_transaction(
                conn, occurred_on
            )
        return RecordPreparation(
            occurred_on=occurred_on,
            snapshot=snapshot,
            same_day_sessions=same_day_sessions,
        )

    async def create_record_draft(
        self,
        *,
        draft_id: str,
        preparation: RecordPreparation,
        conversation_id: str,
        run_id: str | None,
        training_session_id: str | None,
        exercises: tuple[DraftExerciseLog, ...],
        arrangement_revision_id: str | None = None,
        started_at: str | None = None,
        time_precision: TimePrecision | None = None,
        completion_declared: bool = False,
        is_return_phase: bool = False,
        feedback: dict[str, Any] | None = None,
    ) -> RecordDraftView:
        """按准备快照保存一条 Pending 记录草稿（revision 从 1 起），返回查询形态。

        - **归属必须显式**：``training_session_id`` 没有默认值（无默认 = 不接受「没想过」）。
          ``None`` 是显式的「新增一次训练」（确认时建立新身份）；给出稳定身份 id 才是补充／
          更正既有训练。同日多练时日期不唯一，本方法不按日期、安排或当前计划推断归属。
        - **基线只取 ``preparation``**：发生日期与 ``base_business_version`` 都来自准备读取
          时刻的同一快照；生成与保存之间发生的正式提交不改变本草稿的基线，过期在首次确认时
          拦截（stage2.md §4.1 同口径）。
        - **引用与绑定复查**：动作身份必须在目录内（停用不删除，仍然可补录历史）；给出
          ``arrangement_revision_id`` 时必须是已接受的安排修订，且带 ``target_item_key`` 的
          动作与该项准确对应（05 5.2「关联时准确匹配安排」），不做任何推断关联。
        - **不落正式事实**：不写记录侧四表、不改档案、不推进 ``context_version``。载荷可以
          不完整：状态由事实完整性派生，待补全载荷仍可保存（D8 已拍 A）。
        """
        payload = RecordDraftPayload(
            occurred_on=preparation.occurred_on,
            training_session_id=training_session_id,
            exercises=exercises,
            arrangement_revision_id=arrangement_revision_id,
            started_at=started_at,
            time_precision=time_precision,
            completion_declared=completion_declared,
            is_return_phase=is_return_phase,
            feedback=feedback,
        )
        validate_record_draft(payload)
        await self._require_amendable_session(training_session_id)
        await self._require_known_exercises(exercises)
        await self._require_arrangement_match(payload)
        await require_draft_source(
            self._runs, conversation_id=conversation_id, run_id=run_id
        )
        base_profile = preparation.snapshot.profile
        draft = await self._drafts.create_record_pending(
            draft_id=draft_id,
            conversation_id=conversation_id,
            run_id=run_id,
            base_profile_json=(
                None if base_profile is None else profile_to_json(base_profile)
            ),
            proposed_profile_json=profile_to_json(
                base_profile if base_profile is not None else Profile.empty()
            ),
            proposed_record_json=record_draft_to_json(payload),
            base_business_version=preparation.snapshot.context_version,
        )
        return await self._to_view(draft)

    async def get_record_draft(self, draft_id: str) -> RecordDraftView | None:
        """按身份读取记录草稿（含派生状态与结构化 Diff）；不存在返回 None。

        只服务 ``kind='training_record'``：其他 kind 明确报错，不按记录形状解码。
        """
        draft = await self._drafts.get(draft_id)
        return None if draft is None else await self._to_view(draft)

    async def list_record_drafts(
        self, conversation_id: str
    ) -> tuple[RecordDraftView, ...]:
        """该会话已持久化**记录草稿**的当前状态（01 1.2：不依赖历史通知）。"""
        drafts = await self._drafts.list_for_conversation(
            conversation_id, kind=RECORD_DRAFT_KIND
        )
        return tuple([await self._to_view(draft) for draft in drafts])

    async def revise_record_draft(
        self, *, draft_id: str, seen_revision: int, payload: RecordDraftPayload
    ) -> RecordDraftView:
        """以所见 revision 纠错 Pending 记录草稿：复查后整体替换拟议载荷并 revision+1。

        - **白名单**（:func:`validate_record_draft_correction`）：只有已明确的事实可改
          （日期、开始时刻与精度、完成申报、回归期标记、反馈、全部动作与组事实）；归属与
          安排关联不可改——同日多练的归属歧义必须重新提问（丢弃后按新归属重新准备），不能
          借纠错静默换目标。
        - **完整结构复查**：提交载荷先过 :func:`validate_record_draft`，再按当刻目录复查动作
          身份、按绑定的安排修订复查对应关系；非法纠错不部分更新。
        - **乐观并发**：``seen_revision`` 必须等于草稿当前 revision（S3-05 同口径）；终态草稿
          （Committed／Discarded）不可纠错，重复丢弃由丢弃入口幂等处理。
        - 读取草稿、判定终态／revision、复查、写回在同一事务同一连接上完成；Diff 不单独
          存储，由读取方按存储载荷与库内基线重算。
        """
        async with self._db.transaction() as conn:
            draft = await self._drafts.get_in_transaction(conn, draft_id)
            if draft is None:
                raise UnknownDraft(f"草稿不存在：{draft_id}")
            # kind 分派必须在任何写入之前（见 DraftKindMismatch）。
            if draft.kind != RECORD_DRAFT_KIND:
                raise DraftKindMismatch(
                    f"记录草稿纠错只适用于 kind={RECORD_DRAFT_KIND}，"
                    f"收到 kind={draft.kind}：{draft_id}"
                )
            if not draft.is_pending:
                raise DraftNotCorrectable(
                    f"草稿已 {draft.status}，不可继续纠错：{draft_id}"
                )
            if draft.revision != seen_revision:
                raise DraftRevisionConflict(
                    f"所见 revision {seen_revision} 与草稿当前 revision "
                    f"{draft.revision} 不符：{draft_id}"
                )
            stored = record_draft_from_json(_require_record_json(draft))
            validate_record_draft(payload)
            validate_record_draft_correction(stored, payload)
            await self._require_known_exercises_in_transaction(conn, payload.exercises)
            await self._require_arrangement_match_in_transaction(conn, payload)
            updated = await self._drafts.update_record_proposal_in_transaction(
                conn,
                draft_id=draft_id,
                proposed_record_json=record_draft_to_json(payload),
                expected_revision=seen_revision,
            )
        return await self._to_view(updated)

    async def _require_amendable_session(self, training_session_id: str | None) -> None:
        """归属指向的既有训练身份必须存在且已有当前修订（更正需要可对照的基线）。

        ``None`` 不在此判定：那是显式的「新增一次训练」，不是缺省，也不代表可以按日期推断。
        """
        if training_session_id is None:
            return
        session = await self._sessions.read_session(training_session_id)
        if session is None:
            raise UnknownDraft(f"训练身份不存在：{training_session_id}")
        if session.current is None:
            raise InvalidDraftRow(
                f"训练身份没有当前修订，不能作为更正目标：{training_session_id}"
            )

    async def _require_known_exercises(
        self, exercises: tuple[DraftExerciseLog, ...]
    ) -> None:
        """动作身份必须在目录内（停用动作仍可补录历史，不做可推荐过滤）。"""
        for item in exercises:
            if await self._exercises.get_by_id(item.facts.exercise_id) is None:
                raise UnknownExerciseReference(
                    f"记录动作不在目录内：{item.facts.exercise_id}"
                )

    async def _require_known_exercises_in_transaction(
        self, conn: aiosqlite.Connection, exercises: tuple[DraftExerciseLog, ...]
    ) -> None:
        """纠错路径的目录复查：复用当前事务连接（不嵌套取锁，锁不可重入）。"""
        for item in exercises:
            if (
                await self._exercises.get_by_id_in_transaction(
                    conn, item.facts.exercise_id
                )
                is None
            ):
                raise UnknownExerciseReference(
                    f"记录动作不在目录内：{item.facts.exercise_id}"
                )

    async def _require_arrangement_match(self, payload: RecordDraftPayload) -> None:
        """显式给出的安排关联必须存在，且目标项与动作准确对应（不推断关联）。"""
        arrangement = await self._read_arrangement(payload.arrangement_revision_id)
        _require_target_items_match(payload, arrangement)

    async def _require_arrangement_match_in_transaction(
        self, conn: aiosqlite.Connection, payload: RecordDraftPayload
    ) -> None:
        """纠错路径的安排复查：复用当前事务连接的读取入口。"""
        arrangement = await self._read_arrangement_in_transaction(
            conn, payload.arrangement_revision_id
        )
        _require_target_items_match(payload, arrangement)

    async def _read_arrangement(
        self, arrangement_revision_id: str | None
    ) -> ArrangementRevisionRecord | None:
        if arrangement_revision_id is None:
            return None
        arrangement = await self._plans.read_arrangement_revision(
            arrangement_revision_id
        )
        if arrangement is None:
            raise InvalidArrangementTarget(f"安排修订不存在：{arrangement_revision_id}")
        return arrangement

    async def _read_arrangement_in_transaction(
        self, conn: aiosqlite.Connection, arrangement_revision_id: str | None
    ) -> ArrangementRevisionRecord | None:
        if arrangement_revision_id is None:
            return None
        arrangement = await self._plans.read_arrangement_revision_in_transaction(
            conn, arrangement_revision_id
        )
        if arrangement is None:
            raise InvalidArrangementTarget(f"安排修订不存在：{arrangement_revision_id}")
        return arrangement

    async def _to_view(self, draft: Draft) -> RecordDraftView:
        """行 → 查询形态：解码载荷、按库内基线现算 Diff、派生修订状态。

        只接受记录草稿形状：其他 kind 是 kind 分派遗漏（:class:`DraftKindMismatch`），
        不得把其他 kind 静默按记录形状发出。
        """
        if draft.kind != RECORD_DRAFT_KIND:
            raise DraftKindMismatch(
                f"记录草稿查询只适用于 kind={RECORD_DRAFT_KIND}，"
                f"收到 kind={draft.kind}：{draft.id}"
            )
        payload = record_draft_from_json(_require_record_json(draft))
        validate_record_draft(payload)
        if draft.base_profile_json is not None:
            profile_from_json(draft.base_profile_json)
        baseline = await self._read_baseline(payload)
        return RecordDraftView(
            draft=draft,
            payload=payload,
            status=record_draft_status(payload),
            baseline=baseline,
            diff=record_draft_diff(baseline, payload),
        )

    async def _read_baseline(
        self, payload: RecordDraftPayload
    ) -> RecordDraftPayload | None:
        """既有训练身份当前修订的完整事实；新增训练（归属为 None）没有基线。"""
        if payload.training_session_id is None:
            return None
        async with self._db.transaction() as conn:
            baseline = await self._sessions.read_current_payload_in_transaction(
                conn, payload.training_session_id
            )
        if baseline is None:
            raise UnknownDraft(f"训练身份不存在：{payload.training_session_id}")
        return baseline


def record_draft_diff(
    baseline: RecordDraftPayload | None, payload: RecordDraftPayload
) -> tuple[RecordFieldDiff, ...]:
    """按固定字段顺序计算「基线修订 → 拟议载荷」的结构化 Diff（后端现算，不信客户端）。

    ``baseline is None``（新增一次训练）时每个字段的 before 都是 None，与「基线里该字段为
    空」可区分。归属（``training_session_id``）不进 Diff：它是草稿身份，不是事实改动。
    """
    return tuple(
        RecordFieldDiff(
            field=name,
            before=None if baseline is None else getattr(baseline, name),
            after=getattr(payload, name),
        )
        for name in _DIFF_FIELDS
    )


_DIFF_FIELDS = (
    "occurred_on",
    "started_at",
    "time_precision",
    "arrangement_revision_id",
    "completion_declared",
    "is_return_phase",
    "feedback",
    "exercises",
)


def _require_record_json(draft: Draft) -> str:
    """记录草稿的载荷文本；缺失即草稿数据损坏，显式失败不静默兜底。"""
    if draft.proposed_record_json is None:
        raise InvalidDraftRow(f"记录草稿缺少拟议载荷：{draft.id}")
    return draft.proposed_record_json


def _require_target_items_match(
    payload: RecordDraftPayload, arrangement: ArrangementRevisionRecord | None
) -> None:
    """关联安排时的准确对应：目标项必须存在且指向同一动作身份（不做模糊匹配）。"""
    if payload.arrangement_revision_id is None:
        return
    assert arrangement is not None
    items = {item.item_key: item for item in arrangement.target.exercises}
    for exercise in payload.exercises:
        item_key = exercise.facts.target_item_key
        if item_key is None:
            continue
        item = items.get(item_key)
        if item is None:
            raise InvalidArrangementTarget(
                f"记录动作对应的安排目标项不存在：{item_key}"
            )
        if item.exercise_id != exercise.facts.exercise_id:
            raise InvalidArrangementTarget(
                f"记录动作与安排目标项的动作身份不一致：{item_key} / "
                f"{item.exercise_id} → {exercise.facts.exercise_id}"
            )
