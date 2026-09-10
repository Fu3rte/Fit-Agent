"""草稿生命周期应用层（S2-03）：生成基线准备、内部 Pending 档案草稿创建、查询与结构化 Diff。

边界（正本 architecture/01 1.2/1.3；stage2.md §4.1、§5 S2-03）：

- **基线绑定读取时刻**：:meth:`DraftService.prepare_generation_baseline` 读取正式档案与
  ``context_version`` 的同一快照；调用方「生成」结束后凭该快照保存草稿，
  ``base_business_version`` 只来自读取时刻，保存时不得改读最新版本（01 1.3）。
- **内部创建，不开 HTTP 面**：Pending 档案草稿只能经本服务创建（stage2.md §5 S2-01：
  草稿创建不属于 HTTP 面），来源关联现有会话／Run 身份；``run_id`` 可空——本阶段没有
  真实模型执行，不得凭空伪造一次 Run。落库前复用 Stage 1 领域规则做结构校验与限制
  引用校验；**不做**首次建档完整性判定（完整性是确认入口契约，归 S2-05）。
- **Diff 由后端根据快照计算**：不信任客户端传入的 before／after；unknown（未收集）、
  denied（明确无）、known（有值）三态在 Diff 字段对中原样保留，不得压成空数组或
  默认值（stage2.md §4.1）。
- **纠错与丢弃只作用于 Pending（S2-04）**：纠错以所见 revision 做乐观并发控制，复查
  最终结构后整体替换拟议内容并递增 revision；丢弃只改变草稿状态。两个操作的终态判定
  与写入在单一事务同一连接上完成，与确认事务（S2-05）经唯一锁串行判定最终状态；确认
  编排与幂等仍归 :mod:`app.confirm`。草稿行读写经 :mod:`app.draft_repo`：创建／查询走
  各 repo 的自取锁入口，纠错与丢弃的事务内访问只复用外层 ``transaction()`` 连接。
"""

from dataclasses import dataclass
from typing import Any

import aiosqlite

from app.draft_repo import Draft, DraftRepo
from domain.actions.repo import ExerciseRepo
from domain.actions.service import ActionCatalogService
from domain.profile.repo import ProfileRepo
from domain.profile.rules import UnknownExerciseReference, validate_profile_structure
from domain.profile.schema import (
    FACT_FIELDS,
    Fact,
    Profile,
    ProfileSnapshot,
    profile_from_json,
    profile_to_json,
)
from storage.db import Database
from storage.run_repo import RunRepo


class UnknownDraftSource(ValueError):
    """草稿来源关联不成立：会话不存在、Run 不存在或 Run 不属于该会话。"""


class UnknownDraft(ValueError):
    """草稿身份不存在：明确返回未找到，不创建新草稿（与 stage2.md §4.2 同口径）。"""


class DraftNotCorrectable(ValueError):
    """草稿已 Committed／Discarded：终态不可继续纠错（stage2.md §5 S2-04）。"""


class DraftRevisionConflict(ValueError):
    """纠错／确认携带的所见 revision 与草稿当前 revision 不符（S2-04、S2-06）。

    阻止两个页面用同一所见版本静默互相覆盖：纠错（S2-04）与首次确认（S2-06）都报同一
    机器错误码 :attr:`error_code`（409 ``draft_modified``，S2-01 错误映射表）；HTTP 状态码
    与响应体的映射归 S2-07。
    """

    error_code = "draft_modified"


class DraftNotDiscardable(ValueError):
    """已提交草稿不可被丢弃撤销（正式事实不回滚，01 1.3）。"""


@dataclass(frozen=True, slots=True)
class ProfileFieldDiff:
    """单个档案事实字段的「基线 → 拟议」对比（前端契约 A4：字段对，不是文本 diff）。

    ``before``／``after`` 保留三态 :class:`Fact` 原语义：unknown（未收集）、denied
    （明确无）、known（有值）互不混同——显式空集合 ``known(())`` 与 ``denied`` 也不是
    同一语义。基线为「未建档」（``base_profile is None``）时每个字段的 before 都是
    unknown；「未建档」与「显式全未知基线」的顶层区别由 :attr:`DraftView.base_profile`
    承载，不在字段对里折叠。
    """

    field: str
    before: Fact[Any]
    after: Fact[Any]

    @property
    def changed(self) -> bool:
        """基线与拟议是否不同：状态不同即不同；同为 known 时比较值。"""
        if self.before.state != self.after.state:
            return True
        if self.before.is_known and self.after.is_known:
            return self.before.value != self.after.value
        return False


def profile_diff(
    base: Profile | None, proposed: Profile
) -> tuple[ProfileFieldDiff, ...]:
    """按 :data:`FACT_FIELDS` 固定顺序计算全部事实字段的「基线 → 拟议」对比。

    ``base is None``（未建档）按全未知基线逐字段表达 before；两个输入的结构合法性由
    调用方保证（创建路径已做结构校验），本函数保持纯计算、不修改输入对象。
    """
    baseline = base if base is not None else Profile.empty()
    return tuple(
        ProfileFieldDiff(
            field=name, before=getattr(baseline, name), after=getattr(proposed, name)
        )
        for name in FACT_FIELDS
    )


@dataclass(frozen=True, slots=True)
class DraftView:
    """草稿的当前查询形态：行数据 + 解码后的基线／拟议档案 + 结构化 Diff。

    ``base_profile is None`` 表示生成时未建档，与显式全未知基线
    （``base_profile == Profile.empty()``）是两种语义。
    """

    draft: Draft
    base_profile: Profile | None
    proposed_profile: Profile
    diff: tuple[ProfileFieldDiff, ...]


class DraftService:
    """草稿生命周期应用层（S2-03/S2-04 范围）：创建、查询与 Diff、纠错与丢弃。

    确认编排（幂等返回、基线／revision 检查、原子提交）归 ``app/confirm.py``（S2-05）；
    本类不推进 ``context_version``、不写正式档案。
    """

    def __init__(self, db: Database, catalog: ActionCatalogService | None = None):
        self._db = db
        self._profiles = ProfileRepo(db)
        self._drafts = DraftRepo(db)
        self._runs = RunRepo(db)
        self._catalog = catalog if catalog is not None else ActionCatalogService(db)
        self._exercises = ExerciseRepo(db)

    async def prepare_generation_baseline(self) -> ProfileSnapshot:
        """读取正式档案与 ``context_version`` 的同一快照，作为草稿生成输入（01 1.3）。

        返回值就是读取时刻的业务基线；后续 :meth:`create_profile_draft` 必须凭这一份
        快照绑定 ``base_business_version``，不得在保存时改读最新版本。
        """
        return await self._profiles.read()

    async def create_profile_draft(
        self,
        *,
        draft_id: str,
        generation_baseline: ProfileSnapshot,
        conversation_id: str,
        run_id: str | None,
        proposed: Profile,
    ) -> DraftView:
        """按生成基线保存一条 Pending 档案草稿（revision 从 1 起），返回查询形态。

        - 基线只取 ``generation_baseline``：生成与保存之间发生的正式提交不改变本草稿
          的基线，过期在首次确认时拦截（stage2.md §4.1）。
        - 拟议内容先做结构校验与限制引用校验（复用 Stage 1 规则），任一失败不落库；
          不做首次建档完整性判定（归确认入口，S2-05）。
        - 来源必须成立（:meth:`_require_source`）；``run_id=None`` 表示无 Run 来源。
        - 不写正式档案、不推进 ``context_version``、不修改任何输入对象。
        """
        validate_profile_structure(proposed)
        await self._require_known_restriction_targets(proposed)
        await self._require_source(conversation_id=conversation_id, run_id=run_id)
        draft = await self._drafts.create_pending(
            draft_id=draft_id,
            conversation_id=conversation_id,
            run_id=run_id,
            base_profile_json=(
                None
                if generation_baseline.profile is None
                else profile_to_json(generation_baseline.profile)
            ),
            proposed_profile_json=profile_to_json(proposed),
            base_business_version=generation_baseline.context_version,
        )
        return self._to_view(draft)

    async def get_draft(self, draft_id: str) -> DraftView | None:
        """按身份读取当前草稿（含结构化 Diff）；不存在返回 None，不创建新草稿。"""
        draft = await self._drafts.get(draft_id)
        return None if draft is None else self._to_view(draft)

    async def list_drafts(self, conversation_id: str) -> tuple[DraftView, ...]:
        """该会话已持久化草稿的当前状态，各带结构化 Diff（01 1.2：不依赖历史通知）。"""
        return tuple(
            self._to_view(draft)
            for draft in await self._drafts.list_for_conversation(conversation_id)
        )

    async def revise_profile_draft(
        self, *, draft_id: str, seen_revision: int, proposed: Profile
    ) -> DraftView:
        """以所见 revision 纠错 Pending 档案草稿：复查最终结构，整体替换拟议内容。

        - **只接受档案草稿允许纠错的内容**（stage2.md §5 S2-04 拍板 2B：普通档案字段、
          动作限制、红旗与身体状态均允许内联纠错）：拟议内容整体替换，并做与创建路径
          同口径的结构校验与限制引用校验；不做首次建档完整性判定（完整性是确认入口
          契约，归 S2-05）。允许用户纠错不等于 Agent 获得自动解除限制／红旗的权限——
          本方法只改 Pending 草稿，正式限制／红旗只在确认事务按最终内容复查后生效。
        - **所见 revision 乐观并发控制**（S2-04 并发约束）：``seen_revision`` 必须等于
          草稿当前 revision，两个页面不得用同一所见版本静默互相覆盖；不符即拒绝，
          不产生任何写入（非法纠错不部分更新）。
        - **元数据不可借纠错修改**：身份、来源、生成基线（``base_business_version`` 与
          基线快照）、状态与提交凭据不接受传入、也不被本方法改写；过期草稿纠错后仍保留
          原业务基线，过期在首次确认时拦截（stage2.md §4.1）。
        - 读取草稿、判定终态／revision、复查结构、写回在同一事务同一连接上完成：
          纠错与确认（S2-05）、丢弃的最终状态判定经唯一锁串行（01 1.4）；任一步异常
          整体回滚，不留部分更新。Diff 不在本方法落库，由读取方按存储快照重算。
        """
        async with self._db.transaction() as conn:
            draft = await self._drafts.get_in_transaction(conn, draft_id)
            if draft is None:
                raise UnknownDraft(f"草稿不存在：{draft_id}")
            if not draft.is_pending:
                raise DraftNotCorrectable(
                    f"草稿已 {draft.status}，不可继续纠错：{draft_id}"
                )
            if draft.revision != seen_revision:
                raise DraftRevisionConflict(
                    f"所见 revision {seen_revision} 与草稿当前 revision "
                    f"{draft.revision} 不符：{draft_id}"
                )
            validate_profile_structure(proposed)
            await self._require_known_restriction_targets_in_transaction(conn, proposed)
            updated = await self._drafts.update_proposal_in_transaction(
                conn,
                draft_id=draft_id,
                proposed_profile_json=profile_to_json(proposed),
                expected_revision=seen_revision,
            )
        return self._to_view(updated)

    async def discard_draft(self, *, draft_id: str) -> DraftView:
        """丢弃待确认草稿：只改变草稿状态，正式事实与业务版本不变（01 1.3）。

        - 已 Committed 草稿不可被丢弃撤销（正式事实不回滚，S2-04 验收）。
        - 已 Discarded 草稿重复丢弃幂等返回已丢弃结果：不再写入、不刷新更新时间、
          不新增任何正式副作用。
        - 状态读取与写入在同一事务同一连接上完成：丢弃与确认（S2-05）串行判定
          最终状态，已丢弃草稿不会被后续确认重复写入。
        """
        async with self._db.transaction() as conn:
            draft = await self._drafts.get_in_transaction(conn, draft_id)
            if draft is None:
                raise UnknownDraft(f"草稿不存在：{draft_id}")
            if draft.status == "committed":
                raise DraftNotDiscardable(f"已提交草稿不可被丢弃撤销：{draft_id}")
            if draft.status == "discarded":
                return self._to_view(draft)
            updated = await self._drafts.record_discard_in_transaction(
                conn, draft_id=draft_id
            )
        return self._to_view(updated)

    async def _require_known_restriction_targets(self, proposed: Profile) -> None:
        """具体动作限制必须引用目录内身份（含停用动作）；模式词表由结构校验覆盖。"""
        for restriction in proposed.restrictions:
            if restriction.scope != "specific_action":
                continue
            if await self._catalog.get_by_id(restriction.target) is None:
                raise UnknownExerciseReference(
                    f"限制引用的动作身份不在目录内：{restriction.target}"
                )

    async def _require_known_restriction_targets_in_transaction(
        self, conn: aiosqlite.Connection, proposed: Profile
    ) -> None:
        """纠错路径的引用复查：与创建同口径，但复用当前事务连接（不嵌套取锁）。

        与 §4.2 确认事务「读取与复查均使用当前事务连接」同形态：在持锁事务内调用
        自取锁的目录查询会死锁（锁不可重入），故走
        :meth:`ExerciseRepo.get_by_id_in_transaction`。
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

    async def _require_source(
        self, *, conversation_id: str, run_id: str | None
    ) -> None:
        """来源关联必须成立：会话存在；Run 存在且属于同一会话（来源不混淆）。

        外键只保证引用存在；会话与 Run 的从属关系在这里校验，避免把 A 会话的草稿
        记到 B 会话的 Run 上。
        """
        if await self._runs.get_conversation(conversation_id) is None:
            raise UnknownDraftSource(f"来源会话不存在：{conversation_id}")
        if run_id is None:
            return
        run = await self._runs.get_run(run_id)
        if run is None:
            raise UnknownDraftSource(f"来源 Run 不存在：{run_id}")
        run_conversation = str(run["conversation_id"])
        if run_conversation != conversation_id:
            raise UnknownDraftSource(
                f"来源 Run {run_id} 属于会话 {run_conversation}，"
                f"不属于草稿会话 {conversation_id}"
            )

    def _to_view(self, draft: Draft) -> DraftView:
        """行 → 查询形态：解码快照并计算 Diff；解码失败即草稿数据损坏，显式失败。"""
        base_profile = (
            None
            if draft.base_profile_json is None
            else profile_from_json(draft.base_profile_json)
        )
        proposed = profile_from_json(draft.proposed_profile_json)
        return DraftView(
            draft=draft,
            base_profile=base_profile,
            proposed_profile=proposed,
            diff=profile_diff(base_profile, proposed),
        )
