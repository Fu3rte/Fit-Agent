"""Stage 2 S2-04：Pending 专用轻量纠错、所见 revision 并发控制与丢弃生命周期。

验收对照（stage2.md §5 S2-04，含拍板 2B）：

- 纠错只接受档案草稿允许纠错的内容（普通档案字段、动作限制、红旗与身体状态均允许
  内联纠错，拍板 2B），复查最终结构后整体替换拟议内容并递增 revision／重算 Diff；
  不借纠错修改草稿身份、来源、业务基线、状态或凭据，过期草稿纠错后不重基业务基线。
- 纠错携带所见 revision：旧 revision 拒绝、两个页面用同一所见版本只允许一个生效；
  非法类型与非法引用整体拒绝，不做部分更新。
- 终态：Committed／Discarded 不可继续纠错，Discarded 不可确认（用确认事务将采用的
  同一 repo 写入作替身），Committed 不可被丢弃撤销；重复丢弃幂等返回已丢弃结果。
- 丢弃只改变草稿状态：纠错与丢弃不写正式事实、不推进业务版本（Diff 与正式库隔离）。
- 并发：确认替身与纠错／丢弃两类竞态在唯一锁下串行判定最终状态，恰有一方生效。

边界：不实现确认事务与幂等（S2-05）、过期拦截（S2-06）与业务 API（S2-07）；确认
替身只调用 S2-05 将复用的 ``DraftRepo.record_commit_in_transaction``，不代表确认编排
已实现。本文件只用内部应用层与 pytest ``tmp_path`` 下的临时文件库，不触碰真实用户
数据目录；非 Windows 平台结果不作为 Windows 门槛（stage2.md §6）。
"""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from app.draft_repo import INITIAL_REVISION, DraftRepo
from app.drafts import (
    DraftNotCorrectable,
    DraftNotDiscardable,
    DraftRevisionConflict,
    DraftService,
    UnknownDraft,
)
from domain.profile.repo import ProfileRepo
from domain.profile.rules import InvalidProfile, UnknownExerciseReference
from domain.profile.schema import (
    ActionRestriction,
    Fact,
    Profile,
    ProfileSnapshot,
    profile_to_json,
)
from domain.profile.service import ProfileService
from storage.db import Database
from storage.run_repo import RunRepo
from tests.support import open_database

SEEDED_EXERCISE_ID = "barbell-back-squat"  # 003 迁移种子：合法具体动作身份


def _seeded_profile() -> Profile:
    """已建档正式档案样本：带限制与红旗事实（供 2B 安全字段纠错对照）。"""
    return Profile(
        training_goal=Fact.known("力量"),
        body_weight_kg=Fact.known(72.5),
        available_equipment=Fact.known(("哑铃",)),
        action_restrictions=Fact.known(
            (ActionRestriction(scope="specific_action", target=SEEDED_EXERCISE_ID),)
        ),
        body_state=Fact.known(("肩部偶有不适",)),
        red_flags=Fact.known(("锐痛",)),
    )


def _proposed_profile() -> Profile:
    """草稿拟议结果样本：与 S2-03 同口径的结构合法提议。"""
    return Profile(
        training_goal=Fact.known("增肌"),
        training_experience=Fact.known("零基础"),
        weekly_frequency=Fact.known(3),
        session_duration_minutes=Fact.known(60),
        available_equipment=Fact.denied(),
        action_restrictions=Fact.known(
            (ActionRestriction(scope="movement_pattern", target="深蹲"),)
        ),
        body_weight_kg=Fact.known(73.0),
    )


def _revised_profile() -> Profile:
    """纠错后内容样本：改目标与体重，保留其余拟议。"""
    return Profile(
        training_goal=Fact.known("耐力"),
        training_experience=Fact.known("零基础"),
        weekly_frequency=Fact.known(3),
        session_duration_minutes=Fact.known(60),
        available_equipment=Fact.denied(),
        action_restrictions=Fact.known(
            (ActionRestriction(scope="movement_pattern", target="深蹲"),)
        ),
        body_weight_kg=Fact.known(74.0),
    )


def _corrected_safety_profile() -> Profile:
    """2B 纠错样本：内联更正红旗、身体状态与动作限制（结构化、可校验）。"""
    return Profile(
        training_goal=Fact.known("增肌"),
        training_experience=Fact.known("零基础"),
        weekly_frequency=Fact.known(3),
        session_duration_minutes=Fact.known(60),
        available_equipment=Fact.denied(),
        # 明确无限制（denied）与明确无症状（known(())）都是 2B 允许的内联纠错
        action_restrictions=Fact.denied(),
        body_state=Fact.known(("腰部紧绷感",)),
        red_flags=Fact.known(()),
        body_weight_kg=Fact.known(73.0),
    )


async def _simulate_formal_commit(db: Database, profile: Profile) -> None:
    """模拟一次正式业务提交（S2-05 之前的测试替身）：写档案并推进版本。"""
    async with db.transaction() as conn:
        await ProfileService(db).write_profile_in_transaction(conn, profile)
        await conn.execute(
            "UPDATE user_profile SET context_version = context_version + 1 WHERE id = 1"
        )


async def _commit_draft(
    db: Database, *, draft_id: str, revision: int, version: int
) -> None:
    """确认替身：用确认事务（S2-05）将采用的同一 repo 条件写入提交凭据。

    只模拟草稿行的 Committed 写入，不代表确认编排（幂等返回、基线检查、正式写入）
    已实现；竞态用例据此验证纠错／丢弃与确认写入串行判定最终状态。
    """
    async with db.transaction() as conn:
        await DraftRepo(db).record_commit_in_transaction(
            conn,
            draft_id=draft_id,
            committed_revision=revision,
            committed_business_version=version,
        )


async def _setup_service_with_draft(
    db: Database,
    *,
    draft_id: str = "d1",
    proposed: Profile | None = None,
) -> tuple[DraftService, ProfileSnapshot]:
    """准备来源会话、正式档案与一条 Pending 草稿；返回服务与读取基线。"""
    await RunRepo(db).create_conversation("c1")
    await RunRepo(db).create_run_with_user_message("c1", "r1", "cri-1", "帮我调整档案")
    await _simulate_formal_commit(db, _seeded_profile())
    service = DraftService(db)
    baseline = await service.prepare_generation_baseline()
    await service.create_profile_draft(
        draft_id=draft_id,
        generation_baseline=baseline,
        conversation_id="c1",
        run_id="r1",
        proposed=proposed if proposed is not None else _proposed_profile(),
    )
    return service, baseline


def _entry(diff, field: str):
    return next(entry for entry in diff if entry.field == field)


# ---------- 纠错成功：revision／Diff 更新，正式库隔离 ----------


async def test_revise_updates_proposal_revision_and_diff_without_touching_formal_facts(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service, baseline = await _setup_service_with_draft(db, draft_id="d1")
        before = await service.get_draft("d1")
        assert before is not None

        view = await service.revise_profile_draft(
            draft_id="d1",
            seen_revision=before.draft.revision,
            proposed=_revised_profile(),
        )

        # 拟议内容整体替换、revision 递增、Diff 按存储快照重算
        assert view.draft.revision == before.draft.revision + 1
        assert view.proposed_profile == _revised_profile()
        goal = _entry(view.diff, "training_goal")
        assert goal.before == Fact.known("力量")
        assert goal.after == Fact.known("耐力")
        assert goal.changed is True
        weight = _entry(view.diff, "body_weight_kg")
        assert (weight.before.value, weight.after.value) == (72.5, 74.0)

        # 元数据不可借纠错修改：身份、来源、生成基线、状态与凭据原样
        assert (view.draft.id, view.draft.conversation_id, view.draft.run_id) == (
            "d1",
            "c1",
            "r1",
        )
        assert view.draft.base_profile_json == before.draft.base_profile_json
        assert view.draft.base_business_version == baseline.context_version == 1
        assert view.draft.status == "pending"
        assert (
            view.draft.committed_revision,
            view.draft.committed_business_version,
        ) == (
            None,
            None,
        )
        assert view.draft.created_at == before.draft.created_at
        # 读取形态一致（Diff 从落库快照重算，不是回显调用方对象）
        assert await service.get_draft("d1") == view

        # 纠错前后 Diff 与正式库隔离：正式档案与统一业务版本原样（01 1.4）
        assert await ProfileRepo(db).read() == baseline


async def test_correction_covers_all_approved_content_including_safety_fields(
    tmp_path: Path,
) -> None:
    """拍板 2B：动作限制、红旗与身体状态允许在草稿中内联纠错（结构化、经后端校验）。"""
    async with open_database(tmp_path / "app.db") as db:
        service, baseline = await _setup_service_with_draft(db, draft_id="d1")

        view = await service.revise_profile_draft(
            draft_id="d1", seen_revision=1, proposed=_corrected_safety_profile()
        )

        # 安全字段纠错在 Diff 中原样表达（denied／显式空集合不压成默认值）
        restrictions = _entry(view.diff, "action_restrictions")
        assert restrictions.before.value == (
            ActionRestriction(scope="specific_action", target=SEEDED_EXERCISE_ID),
        )
        assert restrictions.after.is_denied
        red_flags = _entry(view.diff, "red_flags")
        assert red_flags.before == Fact.known(("锐痛",))
        assert red_flags.after == Fact.known(())
        body_state = _entry(view.diff, "body_state")
        assert body_state.after == Fact.known(("腰部紧绷感",))
        # 允许用户纠错不等于正式限制／红旗被解除：正式档案原样（草稿级纠错而已）
        assert await ProfileRepo(db).read() == baseline


async def test_correction_does_not_apply_the_first_time_completeness_gate(
    tmp_path: Path,
) -> None:
    """纠错不是确认入口：首次建档完整性判定归 S2-05，部分事实纠错仍可保存。"""
    async with open_database(tmp_path / "app.db") as db:
        service, _ = await _setup_service_with_draft(db, draft_id="d1")

        view = await service.revise_profile_draft(
            draft_id="d1",
            seen_revision=1,
            proposed=Profile(training_goal=Fact.known("耐力")),  # 其余保持未知
        )

        assert view.draft.status == "pending"
        assert view.draft.revision == 2
        assert view.proposed_profile == Profile(training_goal=Fact.known("耐力"))


# ---------- 终态：Committed／Discarded 不可纠错，Committed 不可丢弃 ----------


async def test_committed_draft_cannot_be_revised(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service, _ = await _setup_service_with_draft(db, draft_id="d1")
        await _commit_draft(db, draft_id="d1", revision=1, version=2)
        committed = await service.get_draft("d1")
        assert committed is not None and committed.draft.status == "committed"

        with pytest.raises(DraftNotCorrectable, match="committed"):
            await service.revise_profile_draft(
                draft_id="d1", seen_revision=1, proposed=_revised_profile()
            )

        # 终态不可编辑：草稿行（含提交凭据）原样
        assert await service.get_draft("d1") == committed


async def test_discarded_draft_cannot_be_revised(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service, _ = await _setup_service_with_draft(db, draft_id="d1")
        discarded = await service.discard_draft(draft_id="d1")

        with pytest.raises(DraftNotCorrectable, match="discarded"):
            await service.revise_profile_draft(
                draft_id="d1", seen_revision=1, proposed=_revised_profile()
            )

        assert await service.get_draft("d1") == discarded


async def test_committed_draft_cannot_be_discarded(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service, _ = await _setup_service_with_draft(db, draft_id="d1")
        await _commit_draft(db, draft_id="d1", revision=1, version=2)
        committed = await service.get_draft("d1")
        assert committed is not None

        with pytest.raises(DraftNotDiscardable, match="不可被丢弃撤销"):
            await service.discard_draft(draft_id="d1")

        # Committed 不可被丢弃撤销：状态与提交凭据原样
        assert await service.get_draft("d1") == committed


async def test_discard_changes_only_status_and_repeat_is_idempotent(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service, baseline = await _setup_service_with_draft(db, draft_id="d1")
        before = await service.get_draft("d1")
        assert before is not None

        discarded = await service.discard_draft(draft_id="d1")

        # 丢弃只改变草稿状态：revision、拟议内容、生成基线与凭据一律不动
        assert discarded.draft.status == "discarded"
        assert discarded.draft.revision == before.draft.revision
        assert (
            discarded.draft.proposed_profile_json == before.draft.proposed_profile_json
        )
        assert discarded.draft.base_business_version == baseline.context_version
        assert (
            discarded.draft.committed_revision,
            discarded.draft.committed_business_version,
        ) == (
            None,
            None,
        )
        # 正式档案与业务版本不变：丢弃不新增正式副作用
        assert await ProfileRepo(db).read() == baseline

        # 重复丢弃幂等返回已丢弃结果：不再写入（行数据逐字段一致）、无新增副作用
        again = await service.discard_draft(draft_id="d1")
        assert again == discarded
        assert await service.get_draft("d1") == discarded
        assert await ProfileRepo(db).read() == baseline


async def test_unknown_draft_identity_is_rejected_for_both_operations(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c1")
        service = DraftService(db)

        with pytest.raises(UnknownDraft, match="d-missing"):
            await service.revise_profile_draft(
                draft_id="d-missing", seen_revision=1, proposed=_revised_profile()
            )
        with pytest.raises(UnknownDraft, match="d-missing"):
            await service.discard_draft(draft_id="d-missing")
        # 明确返回未找到，不创建新草稿
        assert await service.list_drafts("c1") == ()


# ---------- 非法纠错不部分更新 ----------


async def test_invalid_correction_is_rejected_atomically(tmp_path: Path) -> None:
    invalid_corrections = (
        (
            "字段类型非法",
            Profile(weekly_frequency=Fact.known("3")),  # type: ignore[arg-type]
            InvalidProfile,
        ),
        (
            "模式不在已拍词表",
            Profile(
                action_restrictions=Fact.known(
                    (ActionRestriction(scope="movement_pattern", target="太极"),)
                )
            ),
            InvalidProfile,
        ),
        (
            "具体动作不在目录内",
            Profile(
                action_restrictions=Fact.known(
                    (
                        ActionRestriction(
                            scope="specific_action", target="no-such-action"
                        ),
                    )
                )
            ),
            UnknownExerciseReference,
        ),
    )
    async with open_database(tmp_path / "app.db") as db:
        service, _ = await _setup_service_with_draft(db, draft_id="d1")
        before = await service.get_draft("d1")
        assert before is not None

        for reason, proposed, expected_error in invalid_corrections:
            with pytest.raises(expected_error):
                await service.revise_profile_draft(
                    draft_id="d1", seen_revision=1, proposed=proposed
                )

            # 非法纠错不部分更新：revision、拟议内容、状态与时间戳整行不变
            after = await service.get_draft("d1")
            assert after == before, reason


# ---------- 所见 revision 并发控制 ----------


async def test_stale_seen_revision_is_rejected(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service, _ = await _setup_service_with_draft(db, draft_id="d1")
        assert (
            await service.revise_profile_draft(
                draft_id="d1", seen_revision=1, proposed=_revised_profile()
            )
        ).draft.revision == 2
        current = await service.get_draft("d1")
        assert current is not None

        # 旧 revision 拒绝（含越界的 0／负值不匹配任何草稿版本）
        for stale in (1, 0, -3):
            with pytest.raises(DraftRevisionConflict, match="revision"):
                await service.revise_profile_draft(
                    draft_id="d1",
                    seen_revision=stale,
                    proposed=_corrected_safety_profile(),
                )

        # 拒绝不产生任何写入：避免两个页面用旧所见版本静默互相覆盖
        assert await service.get_draft("d1") == current


async def test_concurrent_corrections_with_same_seen_revision_allow_exactly_one(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service, _ = await _setup_service_with_draft(db, draft_id="d1")

        results = await asyncio.wait_for(
            asyncio.gather(
                service.revise_profile_draft(
                    draft_id="d1", seen_revision=1, proposed=_revised_profile()
                ),
                service.revise_profile_draft(
                    draft_id="d1", seen_revision=1, proposed=_corrected_safety_profile()
                ),
                return_exceptions=True,
            ),
            timeout=10,
        )

        views = [r for r in results if not isinstance(r, BaseException)]
        conflicts = [r for r in results if isinstance(r, DraftRevisionConflict)]
        # 同一所见 revision 只允许一个纠错生效，另一个明确冲突
        assert len(views) == 1 and len(conflicts) == 1
        final = await service.get_draft("d1")
        assert final is not None
        assert final.draft.revision == 2  # 只递增一次，无重复叠加
        assert final.proposed_profile == views[0].proposed_profile


# ---------- 竞态一：纠错与确认写入串行判定最终状态 ----------


async def test_confirmation_landing_first_blocks_correction(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service, _ = await _setup_service_with_draft(db, draft_id="d1")
        await _commit_draft(db, draft_id="d1", revision=1, version=2)

        with pytest.raises(DraftNotCorrectable, match="committed"):
            await service.revise_profile_draft(
                draft_id="d1", seen_revision=1, proposed=_revised_profile()
            )

        final = await service.get_draft("d1")
        assert final is not None
        assert final.draft.status == "committed"
        assert (
            final.draft.committed_revision,
            final.draft.committed_business_version,
        ) == (1, 2)


async def test_correction_invalidates_confirmation_based_on_old_revision(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service, _ = await _setup_service_with_draft(db, draft_id="d1")

        revised = await service.revise_profile_draft(
            draft_id="d1", seen_revision=1, proposed=_revised_profile()
        )
        assert revised.draft.revision == 2

        # 确认仍按旧所见 revision 写凭据：条件更新不命中即拒绝，无半提交状态
        with pytest.raises(RuntimeError, match="revision"):
            await _commit_draft(db, draft_id="d1", revision=1, version=2)

        final = await service.get_draft("d1")
        assert final is not None
        assert final.draft.status == "pending"
        assert final.draft.revision == 2
        assert (
            final.draft.committed_revision,
            final.draft.committed_business_version,
        ) == (
            None,
            None,
        )


async def test_correction_and_confirmation_race_serializes_to_exactly_one_effect(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service, _ = await _setup_service_with_draft(db, draft_id="d1")

        results = await asyncio.wait_for(
            asyncio.gather(
                service.revise_profile_draft(
                    draft_id="d1", seen_revision=1, proposed=_revised_profile()
                ),
                _commit_draft(db, draft_id="d1", revision=1, version=2),
                return_exceptions=True,
            ),
            timeout=10,
        )

        final = await service.get_draft("d1")
        assert final is not None
        revise_result, commit_result = results
        if isinstance(commit_result, BaseException):
            # 纠错先生效：确认写入按旧 revision 落空，草稿保持 Pending
            assert isinstance(commit_result, RuntimeError)
            assert not isinstance(revise_result, BaseException)
            assert final.draft.status == "pending"
            assert final.draft.revision == 2
            assert final.draft.committed_revision is None
        else:
            # 确认先生效：纠错被终态拒绝，草稿保持 Committed 凭据
            assert isinstance(revise_result, DraftNotCorrectable)
            assert final.draft.status == "committed"
            assert (
                final.draft.committed_revision,
                final.draft.committed_business_version,
            ) == (
                1,
                2,
            )


# ---------- 竞态二：丢弃与确认写入串行判定最终状态 ----------


async def test_confirmation_landing_first_blocks_discard(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service, _ = await _setup_service_with_draft(db, draft_id="d1")
        await _commit_draft(db, draft_id="d1", revision=1, version=2)

        with pytest.raises(DraftNotDiscardable, match="不可被丢弃撤销"):
            await service.discard_draft(draft_id="d1")

        final = await service.get_draft("d1")
        assert final is not None
        assert final.draft.status == "committed"
        assert final.draft.committed_business_version == 2


async def test_discard_blocks_confirmation_of_the_discarded_draft(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service, _ = await _setup_service_with_draft(db, draft_id="d1")
        discarded = await service.discard_draft(draft_id="d1")

        # 丢弃后不能提交：确认写入按 Pending 条件落空，无凭据、无半状态
        with pytest.raises(RuntimeError, match="Pending"):
            await _commit_draft(db, draft_id="d1", revision=1, version=2)

        final = await service.get_draft("d1")
        assert final == discarded
        assert final is not None
        assert final.draft.status == "discarded"
        assert final.draft.committed_revision is None


async def test_discard_and_confirmation_race_serializes_to_exactly_one_effect(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service, _ = await _setup_service_with_draft(db, draft_id="d1")

        results = await asyncio.wait_for(
            asyncio.gather(
                service.discard_draft(draft_id="d1"),
                _commit_draft(db, draft_id="d1", revision=1, version=2),
                return_exceptions=True,
            ),
            timeout=10,
        )

        final = await service.get_draft("d1")
        assert final is not None
        discard_result, commit_result = results
        if isinstance(commit_result, BaseException):
            # 丢弃先生效：确认写入落空，草稿保持 Discarded 无凭据
            assert isinstance(commit_result, RuntimeError)
            assert not isinstance(discard_result, BaseException)
            assert final.draft.status == "discarded"
            assert final.draft.committed_revision is None
        else:
            # 确认先生效：丢弃被终态拒绝，草稿保持 Committed 凭据
            assert isinstance(discard_result, DraftNotDiscardable)
            assert final.draft.status == "committed"
            assert final.draft.committed_business_version == 2


# ---------- repo 层串行化接缝：CAS、列范围与事务边界 ----------


async def test_revise_and_discard_writers_require_an_outer_transaction(
    tmp_path: Path,
) -> None:
    """纠错／丢弃写入只复用外层事务连接：不在事务内即拒绝，不留任何写入。

    与 S2-02 的事务内读取守卫同口径：这两个写入若自行取锁会与 ``transaction()`` 的锁
    嵌套（锁不可重入会死锁）；若允许自行提交，多语句原子性就落回调用方。
    """
    async with open_database(tmp_path / "app.db") as db:
        service, _ = await _setup_service_with_draft(db, draft_id="d1")
        drafts = DraftRepo(db)
        before = await service.get_draft("d1")
        assert before is not None

        async def op(conn):
            with pytest.raises(RuntimeError, match="外层事务"):
                await drafts.update_proposal_in_transaction(
                    conn,
                    draft_id="d1",
                    proposed_profile_json=profile_to_json(_revised_profile()),
                    expected_revision=before.draft.revision,
                )
            with pytest.raises(RuntimeError, match="外层事务"):
                await drafts.record_discard_in_transaction(conn, draft_id="d1")

        await db.under_lock(op)
        assert await service.get_draft("d1") == before  # 非事务内调用不留任何写入


async def test_revise_and_discard_writers_accept_only_pending_drafts(
    tmp_path: Path,
) -> None:
    """条件更新只对 Pending 命中：终态草稿的拟议内容、凭据与状态都不被重复写入。

    行数据先经确认替身置为 Committed（另一份草稿置为 Discarded），再直接调用 repo 写入：
    不命中即拒绝且整行原样——串行化接缝不是只靠服务层的前置判定。
    """
    async with open_database(tmp_path / "app.db") as db:
        service, _ = await _setup_service_with_draft(db, draft_id="d1")
        drafts = DraftRepo(db)
        await _commit_draft(db, draft_id="d1", revision=1, version=2)
        committed = await drafts.get("d1")
        assert committed is not None and committed.status == "committed"

        # d1 已 Committed：纠错与丢弃的条件更新都不命中，提交凭据原样
        async with db.transaction() as conn:
            with pytest.raises(RuntimeError, match="Pending"):
                await drafts.update_proposal_in_transaction(
                    conn,
                    draft_id="d1",
                    proposed_profile_json=profile_to_json(_revised_profile()),
                    expected_revision=committed.revision,
                )
            with pytest.raises(RuntimeError, match="Pending"):
                await drafts.record_discard_in_transaction(conn, draft_id="d1")
        assert await drafts.get("d1") == committed

        # 另一条草稿先被丢弃：同样不可再纠错，也不走重复写入路径（幂等返回归服务层）
        await service.create_profile_draft(
            draft_id="d3",
            generation_baseline=await service.prepare_generation_baseline(),
            conversation_id="c1",
            run_id=None,
            proposed=_proposed_profile(),
        )
        discarded_draft = (await service.discard_draft(draft_id="d3")).draft
        async with db.transaction() as conn:
            with pytest.raises(RuntimeError, match="Pending"):
                await drafts.update_proposal_in_transaction(
                    conn,
                    draft_id="d3",
                    proposed_profile_json=profile_to_json(_revised_profile()),
                    expected_revision=discarded_draft.revision,
                )
            with pytest.raises(RuntimeError, match="Pending"):
                await drafts.record_discard_in_transaction(conn, draft_id="d3")
        assert await drafts.get("d3") == discarded_draft


async def test_proposal_update_cas_rejects_any_revision_but_the_seen_one(
    tmp_path: Path,
) -> None:
    """纠错写入的 CAS 只接受行当前 revision：旧版本与「未来版本」都不命中且不落任何写入。"""
    async with open_database(tmp_path / "app.db") as db:
        service, _ = await _setup_service_with_draft(db, draft_id="d1")
        drafts = DraftRepo(db)
        revised = await service.revise_profile_draft(
            draft_id="d1", seen_revision=INITIAL_REVISION, proposed=_revised_profile()
        )
        assert revised.draft.revision == INITIAL_REVISION + 1

        for expected_revision in (INITIAL_REVISION, INITIAL_REVISION + 2):
            async with db.transaction() as conn:
                with pytest.raises(RuntimeError, match="revision"):
                    await drafts.update_proposal_in_transaction(
                        conn,
                        draft_id="d1",
                        proposed_profile_json=profile_to_json(
                            _corrected_safety_profile()
                        ),
                        expected_revision=expected_revision,
                    )
            assert await drafts.get("d1") == revised.draft


async def test_revise_and_discard_change_only_the_columns_they_own(
    tmp_path: Path,
) -> None:
    """纠错只写拟议内容／revision／更新时间，丢弃只写状态／更新时间。

    用 ``dataclasses.replace`` 表达「整行仅这几个字段不同」：身份、来源、基线快照、
    ``base_business_version``、状态与提交凭据逐列都不可能被纠错改写（丢弃同理）。
    """
    async with open_database(tmp_path / "app.db") as db:
        service, _ = await _setup_service_with_draft(db, draft_id="d1")
        drafts = DraftRepo(db)
        before = await drafts.get("d1")
        assert before is not None

        revised = await service.revise_profile_draft(
            draft_id="d1",
            seen_revision=before.revision,
            proposed=_revised_profile(),
        )
        after = await drafts.get("d1")
        assert after is not None
        assert after.updated_at != before.updated_at
        assert after == replace(
            before,
            proposed_profile_json=profile_to_json(_revised_profile()),
            revision=before.revision + 1,
            updated_at=after.updated_at,
        )
        assert revised.draft == after  # 返回给调用方的是同一行数据

        discarded = await service.discard_draft(draft_id="d1")
        final = await drafts.get("d1")
        assert final is not None
        assert final.updated_at != after.updated_at
        assert final == replace(after, status="discarded", updated_at=final.updated_at)
        assert discarded.draft == final


# ---------- 过期基线不重基；重开持久化 ----------


async def test_concurrent_discards_are_idempotent_with_a_single_write(
    tmp_path: Path,
) -> None:
    """重复丢弃不新增正式副作用：并发双击只产生一次状态写入，双方拿到同一已丢弃结果。"""
    async with open_database(tmp_path / "app.db") as db:
        service, baseline = await _setup_service_with_draft(db, draft_id="d1")

        results = await asyncio.wait_for(
            asyncio.gather(
                service.discard_draft(draft_id="d1"),
                service.discard_draft(draft_id="d1"),
            ),
            timeout=10,
        )

        first, second = results
        assert first == second
        assert first.draft.status == "discarded"
        # 第二次丢弃没有刷新时间戳，也没有写入任何行
        assert await service.get_draft("d1") == first
        assert await ProfileRepo(db).read() == baseline


async def test_correcting_a_stale_draft_does_not_rebase_the_business_baseline(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c1")
        service = DraftService(db)
        baseline = await service.prepare_generation_baseline()
        assert baseline.context_version == 0 and baseline.profile is None
        await service.create_profile_draft(
            draft_id="d1",
            generation_baseline=baseline,
            conversation_id="c1",
            run_id=None,
            proposed=_proposed_profile(),
        )

        # 生成期间发生一次正式提交：业务版本 V=0 → V=1，草稿过期
        await _simulate_formal_commit(db, _seeded_profile())

        view = await service.revise_profile_draft(
            draft_id="d1", seen_revision=INITIAL_REVISION, proposed=_revised_profile()
        )

        # 过期草稿即使纠错也不刷新业务基线：仍绑定读取时的 V=0 与未建档基线
        assert view.draft.revision == 2
        assert view.draft.base_business_version == 0
        assert view.draft.base_profile_json is None
        assert view.base_profile is None
        # 纠错本身不推进业务版本
        assert (await ProfileRepo(db).read()).context_version == 1


async def test_correction_and_discard_survive_close_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    async with open_database(path) as db:
        service, _ = await _setup_service_with_draft(db, draft_id="d1")
        await service.create_profile_draft(
            draft_id="d2",
            generation_baseline=await service.prepare_generation_baseline(),
            conversation_id="c1",
            run_id=None,
            proposed=_proposed_profile(),
        )
        revised = await service.revise_profile_draft(
            draft_id="d1", seen_revision=1, proposed=_revised_profile()
        )
        discarded = await service.discard_draft(draft_id="d2")

    async with open_database(path) as db:
        reloaded = DraftService(db)
        assert await reloaded.get_draft("d1") == revised
        assert await reloaded.get_draft("d2") == discarded
        final = await reloaded.get_draft("d1")
        assert final is not None
        assert final.draft.revision == 2
        assert final.proposed_profile == _revised_profile()
