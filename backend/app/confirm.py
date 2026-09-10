"""确认事务编排：幂等返回 → 基线／revision 检查 → 事务内领域复查 → 原子提交（01 1.4/1.5）。

正本：architecture/01 1.4/1.5（确认顺序、原子性、幂等凭据、版本唯一推进点）、
stage2.md §4.2（逐步顺序）、§5 S2-05、§8 已拍「无业务变化首次确认」方案 A。

单连接、唯一锁、同一事务（07 7.1）内依次执行 §4.2：

1. 读草稿；已 Committed 直接返回保存的原提交结果：不重查旧业务版本或旧 revision、不重复
   写入（响应丢失重试、关闭重开、后续业务版本变化后的重试都走这条）。
2. 拒绝 Discarded；身份不存在明确失败，不创建新草稿。
3. 读当前档案与 ``context_version``（同一快照）；严格检查业务基线（版本号相等）与用户所见
   revision。事务内读取只经 repo 的 ``*_in_transaction`` 入口：在持锁事务内调用自取锁方法
   会死锁（锁不可重入，stage2.md §2）。
4. 对数据库保存的最终草稿内容重新做事务内确定性复查：结构、动作／模式引用、首次建档
   完整性（§4.3 已拍 1B）。本阶段草稿内容就是完整档案而非增量补丁，因此没有补丁复查项；
   计划草稿的补丁复查归 Stage 3。
5. 写正式档案（复用 Stage 1 内部写入）→ ``context_version`` 恰好 +1 → 草稿 Committed 与
   不可变提交凭据。
6. COMMIT 之后才把结果交给调用方；任一步异常或取消由 ``Database.transaction()`` 整体回滚，
   不留部分档案、版本、草稿状态或凭据。

过期拦截（S2-06）：首次确认业务基线不符时抛 :class:`DraftStale`，并按保存的业务基线与当前
正式档案快照算出**可核实的字段变化**一并带出（只列确有差异的字段，版本已变但快照无字段差异
时为空）；revision 不符抛 :attr:`DraftRevisionConflict.error_code` = ``draft_modified``。两条
拦截都不写库：过期草稿保持 Pending、基线／内容／状态原样，正式数据不变。两类冲突同时出现
时，按 §4.2 步骤 3 的先后先报 :class:`DraftStale`（结果是确定的，不因 revision 更旧而改变拦截
口径）。重算归 Stage 4，本模块不提供。

事务内只做本地确定性计算与数据库操作：不调用模型、工具执行或 SSE，不新增第二把业务数据库
锁，也不新增第二个版本计数器。本模块不做传输校验与 HTTP 错误码映射（S2-07）。
"""

from dataclasses import dataclass

import aiosqlite
from domain.actions.repo import ExerciseRepo
from domain.profile.repo import ProfileRepo
from domain.profile.rules import (
    UnknownExerciseReference,
    ensure_first_time_complete,
    validate_profile_structure,
)
from domain.profile.schema import Profile, profile_from_json
from domain.profile.service import ProfileService
from storage.db import Database

from app.draft_repo import Draft, DraftRepo, InvalidDraftRow
from app.drafts import (
    DraftRevisionConflict,
    ProfileFieldDiff,
    UnknownDraft,
    profile_diff,
)


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

    async def confirm_profile_draft(
        self, *, draft_id: str, seen_revision: int
    ) -> ProfileCommitResult:
        """确认草稿身份＋用户所见 revision；返回该次提交的不可变结果。

        ``seen_revision`` 只用于「首次确认」的乐观并发检查；草稿已 Committed 时忽略它并直接
        返回原凭据（§4.2 步骤 1：重试不因旧业务版本或旧 revision 失败）。确认请求不携带业务
        内容——写入的永远是数据库保存的最终草稿（§4.2：不信任客户端传入可替换内容）。
        """
        async with self._db.transaction() as conn:
            draft = await self._drafts.get_in_transaction(conn, draft_id)
            if draft is None:
                raise UnknownDraft(f"草稿不存在：{draft_id}")
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
