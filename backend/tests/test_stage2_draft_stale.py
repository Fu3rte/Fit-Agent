"""Stage 2 S2-06：过期拦截（409 ``draft_stale``）、可核实变更项与 ``draft_modified``。

验收对照（stage2.md §5 S2-06、§6「过期」组，architecture/01 1.4/1.6）：

- 同一业务版本生成两份草稿，第一份提交后第二份首次确认被拦截，**即使修改字段无关**（01 1.4
  「接受保守拦截」）；已提交草稿的重复确认优先幂等返回原凭据，不被后续版本变化误报为过期。
- 只报告**可核实**的字段变化：用保存的生成基线快照与当前正式档案快照逐字段比较；不虚构
  「谁何时修改」。版本变过又恢复到相同值仍拦截，此时报告版本已变化且当前快照无字段差异。
- 拦截不改变过期草稿的状态、基线、拟议内容与 revision，不写正式数据、不推进版本；旧草稿身份
  ／基线／内容仍可查（Stage 4 重算的交接前提）。revision 不符报 ``draft_modified``。
- 两种冲突同时出现时按 §4.2 步骤 3 顺序确定性报 ``draft_stale``；失败不产生提交凭据，不伪装
  为成功。

边界：不实现重算与 ``parent_draft_id`` 关联（Stage 4）、不做 HTTP 错误码与响应体映射（S2-07）
·只用内部应用层与 pytest ``tmp_path`` 下的临时文件库，不触碰真实用户数据目录；非 Windows
平台结果不作为 Windows 阶段门槛（stage2.md §6）。
"""

from dataclasses import replace
from pathlib import Path

import pytest

from app.confirm import (
    ConfirmService,
    DraftStale,
    ProfileCommitResult,
    verifiable_field_changes,
)
from app.draft_repo import INITIAL_REVISION
from app.drafts import (
    DraftRevisionConflict,
    DraftService,
    ProfileFieldDiff,
)
from domain.profile.repo import ProfileRepo
from domain.profile.schema import Fact, Profile, ProfileSnapshot
from domain.profile.service import ProfileService
from storage.db import Database
from storage.run_repo import RunRepo
from tests.support import open_database


def _buildable_profile() -> Profile:
    """可过首次建档完整性门的档案（九项明确回答齐备）。"""
    return Profile(
        training_goal=Fact.known("增肌"),
        training_experience=Fact.known("零基础"),
        weekly_frequency=Fact.known(3),
        session_duration_minutes=Fact.known(60),
        available_equipment=Fact.known(("哑铃",)),
        action_restrictions=Fact.denied(),
        body_state=Fact.known(()),
        red_flags=Fact.denied(),
        body_weight_kg=Fact.known(73.0),
    )


async def _write_formal_profile(db: Database, profile: Profile) -> None:
    """测试替身：确认事务之外直接写正式档案并推进一次版本（构造后续正式提交）。"""
    async with db.transaction() as conn:
        await ProfileService(db).write_profile_in_transaction(conn, profile)
        await ProfileRepo(db).bump_context_version_in_transaction(conn)


async def _formal(db: Database) -> ProfileSnapshot:
    return await ProfileRepo(db).read()


async def _setup_same_version_drafts(
    db: Database,
    *,
    base: Profile | None,
    sibling_changed: Profile,
    stale_proposed: Profile,
) -> tuple[DraftService, ProfileSnapshot]:
    """准备来源会话／正式档案与**同一业务版本上的两份草稿**（d1、d2）。

    ``sibling_changed`` 是 d1 的拟议内容（先提交的那份）；``stale_proposed`` 是 d2 的拟议内容
    （被拦截的那份）。两份草稿共用同一份生成基线快照。
    """
    await RunRepo(db).create_conversation("c1")
    await RunRepo(db).create_run_with_user_message("c1", "r1", "cri-1", "帮我建档")
    if base is not None:
        await _write_formal_profile(db, base)
    drafts = DraftService(db)
    baseline = await drafts.prepare_generation_baseline()
    for draft_id, proposed in (("d1", sibling_changed), ("d2", stale_proposed)):
        await drafts.create_profile_draft(
            draft_id=draft_id,
            generation_baseline=baseline,
            conversation_id="c1",
            run_id="r1",
            proposed=proposed,
        )
    return drafts, baseline


def _changes_of(error: DraftStale) -> dict[str, ProfileFieldDiff]:
    return {item.field: item for item in error.changes}


# ---------- 同版本两草稿：先提交者生效，后提交者被拦截 ----------


async def test_same_field_conflict_blocks_the_second_sibling_draft(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        base = _buildable_profile()
        drafts, baseline = await _setup_same_version_drafts(
            db,
            base=base,
            sibling_changed=replace(base, body_weight_kg=Fact.known(80.0)),
            stale_proposed=replace(base, body_weight_kg=Fact.known(79.0)),
        )
        assert baseline.context_version == 1
        committed = await ConfirmService(db).confirm_profile_draft(
            draft_id="d1", seen_revision=INITIAL_REVISION
        )
        formal_after_commit = await _formal(db)
        stale_before = await drafts.get_draft("d2")
        assert stale_before is not None

        with pytest.raises(DraftStale) as caught:
            await ConfirmService(db).confirm_profile_draft(
                draft_id="d2", seen_revision=INITIAL_REVISION
            )

        error = caught.value
        assert error.error_code == "draft_stale"
        assert (error.base_business_version, error.current_business_version) == (1, 2)
        # 可核实变更项：保存基线 → 当前正式档案（先提交的那份改动），不是 d2 自己的拟议内容
        changes = _changes_of(error)
        assert set(changes) == {"body_weight_kg"}
        assert changes["body_weight_kg"].before == Fact.known(73.0)
        assert changes["body_weight_kg"].after == Fact.known(80.0)
        assert error.base_profile == base
        assert error.current_profile == formal_after_commit.profile

        # 拦截不写库：过期草稿的状态／基线／内容／revision 原样，正式数据与版本不变
        assert await drafts.get_draft("d2") == stale_before
        assert await _formal(db) == formal_after_commit
        assert stale_before.draft.base_business_version == baseline.context_version
        # 失败不是成功：没有凭据，重复确认仍是同一拦截
        with pytest.raises(DraftStale):
            await ConfirmService(db).confirm_profile_draft(
                draft_id="d2", seen_revision=INITIAL_REVISION
            )
        assert committed.committed_business_version == 2


async def test_unrelated_field_change_still_blocks_the_second_sibling_draft(
    tmp_path: Path,
) -> None:
    """保守拦截（01 1.4）：先提交者改的字段与被拦截草稿的拟议字段无关，仍然过期。"""
    async with open_database(tmp_path / "app.db") as db:
        base = _buildable_profile()
        drafts, _baseline = await _setup_same_version_drafts(
            db,
            base=base,
            sibling_changed=replace(base, training_goal=Fact.known("力量")),
            stale_proposed=replace(base, body_weight_kg=Fact.known(70.0)),
        )
        await ConfirmService(db).confirm_profile_draft(
            draft_id="d1", seen_revision=INITIAL_REVISION
        )
        formal_after_commit = await _formal(db)
        stale_before = await drafts.get_draft("d2")

        with pytest.raises(DraftStale) as caught:
            await ConfirmService(db).confirm_profile_draft(
                draft_id="d2", seen_revision=INITIAL_REVISION
            )

        # 报告的是基线与当前快照的差别（training_goal），不是过期草稿自己要改的字段
        assert set(_changes_of(caught.value)) == {"training_goal"}
        assert await drafts.get_draft("d2") == stale_before
        assert await _formal(db) == formal_after_commit


async def test_changed_back_value_still_reports_stale_with_no_field_differences(
    tmp_path: Path,
) -> None:
    """版本变过又恢复到相同值：仍拦截，且明确「版本已变化、当前快照无字段差异」。"""
    async with open_database(tmp_path / "app.db") as db:
        base = _buildable_profile()
        drafts, _baseline = await _setup_same_version_drafts(
            db,
            base=base,
            sibling_changed=replace(base, body_weight_kg=Fact.known(80.0)),
            stale_proposed=replace(base, body_weight_kg=Fact.known(79.0)),
        )
        # 一次提交改掉体重，又一次提交改回原值：版本 +2，快照与基线逐字段相同
        await _write_formal_profile(db, replace(base, body_weight_kg=Fact.known(80.0)))
        await _write_formal_profile(db, base)
        formal_before = await _formal(db)
        assert formal_before.context_version == 3
        stale_before = await drafts.get_draft("d2")

        with pytest.raises(DraftStale) as caught:
            await ConfirmService(db).confirm_profile_draft(
                draft_id="d2", seen_revision=INITIAL_REVISION
            )

        error = caught.value
        assert (error.base_business_version, error.current_business_version) == (1, 3)
        assert error.changes == ()
        assert error.current_profile == base
        assert "无字段差异" in str(error)
        # 无字段差异不等于可以提交：正式数据与草稿行原样
        assert await drafts.get_draft("d2") == stale_before
        assert await _formal(db) == formal_before


async def test_unbuilt_baseline_reports_no_field_differences_but_is_never_unknown_baseline(
    tmp_path: Path,
) -> None:
    """基线为「未建档」时：报过期且不把未建档摊成逐字段 unknown → known 的假差异。"""
    async with open_database(tmp_path / "app.db") as db:
        drafts, baseline = await _setup_same_version_drafts(
            db,
            base=None,
            sibling_changed=_buildable_profile(),
            stale_proposed=Profile(training_goal=Fact.known("增肌")),
        )
        assert baseline.profile is None and baseline.context_version == 0
        # 生成期间另一次正式提交完成了首次建档
        await _write_formal_profile(db, _buildable_profile())
        formal_before = await _formal(db)
        stale_before = await drafts.get_draft("d2")

        with pytest.raises(DraftStale) as caught:
            await ConfirmService(db).confirm_profile_draft(
                draft_id="d2", seen_revision=INITIAL_REVISION
            )

        error = caught.value
        assert (error.base_business_version, error.current_business_version) == (0, 1)
        assert error.changes == ()
        assert error.base_profile is None  # 未建档，不是「显式全未知基线」
        assert error.current_profile == _buildable_profile()
        assert await drafts.get_draft("d2") == stale_before
        assert await _formal(db) == formal_before


async def test_stale_rejection_keeps_the_old_draft_queryable_for_later_recompute(
    tmp_path: Path,
) -> None:
    """Stage 4 交接前提：被拦截的旧草稿身份、基线、内容与 revision 仍可查，仍可丢弃。"""
    async with open_database(tmp_path / "app.db") as db:
        base = _buildable_profile()
        drafts, baseline = await _setup_same_version_drafts(
            db,
            base=base,
            sibling_changed=replace(base, training_goal=Fact.known("力量")),
            stale_proposed=replace(base, body_weight_kg=Fact.known(70.0)),
        )
        await ConfirmService(db).confirm_profile_draft(
            draft_id="d1", seen_revision=INITIAL_REVISION
        )

        with pytest.raises(DraftStale):
            await ConfirmService(db).confirm_profile_draft(
                draft_id="d2", seen_revision=INITIAL_REVISION
            )

        view = await drafts.get_draft("d2")
        assert view is not None
        assert view.draft.id == "d2"
        assert view.draft.base_business_version == baseline.context_version
        assert view.draft.base_profile_json is not None
        assert view.base_profile == base
        assert view.proposed_profile == replace(base, body_weight_kg=Fact.known(70.0))
        assert view.draft.revision == INITIAL_REVISION
        assert view.draft.status == "pending"
        assert len(await drafts.list_drafts("c1")) == 2
        # 过期草稿仍可由用户丢弃（不重算、不自动采纳）
        assert (await drafts.discard_draft(draft_id="d2")).draft.status == "discarded"


# ---------- revision 冲突与两种冲突同时出现 ----------


async def test_revision_mismatch_is_draft_modified_and_writes_nothing(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        base = _buildable_profile()
        drafts, baseline = await _setup_same_version_drafts(
            db,
            base=base,
            sibling_changed=replace(base, body_weight_kg=Fact.known(80.0)),
            stale_proposed=replace(base, body_weight_kg=Fact.known(79.0)),
        )
        revised = await drafts.revise_profile_draft(
            draft_id="d2",
            seen_revision=INITIAL_REVISION,
            proposed=replace(base, body_weight_kg=Fact.known(78.0)),
        )
        assert revised.draft.revision == INITIAL_REVISION + 1

        with pytest.raises(DraftRevisionConflict) as caught:
            await ConfirmService(db).confirm_profile_draft(
                draft_id="d2", seen_revision=INITIAL_REVISION
            )

        assert caught.value.error_code == "draft_modified"
        # 版本未变、草稿未被改写、正式数据未变、无凭据
        assert await drafts.get_draft("d2") == revised
        assert await _formal(db) == baseline
        assert revised.draft.committed_revision is None


async def test_version_and_revision_conflict_together_reports_stale_deterministically(
    tmp_path: Path,
) -> None:
    """两种冲突同时出现：按 §4.2 步骤 3 顺序先报 draft_stale，且不写任何东西。"""
    async with open_database(tmp_path / "app.db") as db:
        base = _buildable_profile()
        drafts, _baseline = await _setup_same_version_drafts(
            db,
            base=base,
            sibling_changed=replace(base, body_weight_kg=Fact.known(80.0)),
            stale_proposed=replace(base, body_weight_kg=Fact.known(79.0)),
        )
        # d2 先被纠错到 revision 2，随后基线过期：两个冲突同时成立
        revised = await drafts.revise_profile_draft(
            draft_id="d2",
            seen_revision=INITIAL_REVISION,
            proposed=replace(base, body_weight_kg=Fact.known(78.0)),
        )
        await ConfirmService(db).confirm_profile_draft(
            draft_id="d1", seen_revision=INITIAL_REVISION
        )
        formal_before = await _formal(db)

        for seen_revision in (INITIAL_REVISION, revised.draft.revision):
            with pytest.raises(DraftStale) as caught:
                await ConfirmService(db).confirm_profile_draft(
                    draft_id="d2", seen_revision=seen_revision
                )
            assert caught.value.error_code == "draft_stale"
            assert await drafts.get_draft("d2") == revised
        assert await _formal(db) == formal_before


async def test_repeat_confirmation_of_the_committed_sibling_stays_idempotent(
    tmp_path: Path,
) -> None:
    """01 验收 3：第一份重复确认返回原结果，第二份被 draft_stale 拦截，版本只 +1 一次。"""
    async with open_database(tmp_path / "app.db") as db:
        base = _buildable_profile()
        drafts, _baseline = await _setup_same_version_drafts(
            db,
            base=base,
            sibling_changed=replace(base, body_weight_kg=Fact.known(80.0)),
            stale_proposed=replace(base, body_weight_kg=Fact.known(79.0)),
        )
        service = ConfirmService(db)
        original = await service.confirm_profile_draft(
            draft_id="d1", seen_revision=INITIAL_REVISION
        )
        committed_row = await drafts.get_draft("d1")

        # 已提交草稿忽略后续版本变化与错误 revision：返回原凭据，不重复写入
        retried = await service.confirm_profile_draft(draft_id="d1", seen_revision=99)
        assert retried == original == ProfileCommitResult("d1", INITIAL_REVISION, 2)
        assert await drafts.get_draft("d1") == committed_row
        assert (await _formal(db)).context_version == 2
        with pytest.raises(DraftStale):
            await service.confirm_profile_draft(
                draft_id="d2", seen_revision=INITIAL_REVISION
            )
        assert (await _formal(db)).context_version == 2


# ---------- 可核实变更项计算的边界 ----------


async def test_verifiable_field_changes_only_lists_real_field_differences() -> None:
    base = _buildable_profile()
    assert verifiable_field_changes(base, base) == ()
    assert verifiable_field_changes(None, base) == ()
    assert verifiable_field_changes(base, None) == ()

    changed = verifiable_field_changes(
        base,
        replace(
            base,
            body_weight_kg=Fact.known(80.0),
            available_equipment=Fact.denied(),  # 状态从 known 变为 denied 也是差异
        ),
    )
    assert [item.field for item in changed] == [
        "available_equipment",
        "body_weight_kg",
    ]
    assert all(item.changed for item in changed)
