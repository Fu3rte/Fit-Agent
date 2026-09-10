"""Stage 2 S2-03：同快照生成基线、内部 Pending 档案草稿创建、按身份／会话查询与结构化 Diff。

验收对照（stage2.md §5 S2-03）：

- 建草稿前后正式档案及版本不变；保存时间不能替代读取基线（读版本 V → 另一次正式提交
  推进到 V+1 → 保存的草稿仍绑定 V 与原基线）。
- 不同来源关联不混淆：会话各自隔离、Run 归属校验、不存在的来源拒绝且不落库。
- 未建档、字段变更、限制增删均能正确表达前后值；unknown／denied／known（含显式空集合）
  不压成默认值。
- 重开后内容、基线、来源和 revision 一致；调用方传入对象不被原地修改。

边界：不实现纠错与丢弃（S2-04）、确认事务（S2-05）、过期拦截（S2-06）与业务 API
（S2-07）。本文件只用内部应用层与 pytest ``tmp_path`` 下的临时文件库，不触碰真实用户
数据目录；非 Windows 平台结果不作为 Windows 门槛（stage2.md §6）。
"""

import dataclasses
from pathlib import Path

import pytest

from app.draft_repo import INITIAL_REVISION
from app.drafts import DraftService, UnknownDraftSource, profile_diff
from domain.profile.repo import ProfileRepo
from domain.profile.rules import InvalidProfile, UnknownExerciseReference
from domain.profile.schema import (
    FACT_FIELDS,
    ActionRestriction,
    Fact,
    Profile,
    ProfileSnapshot,
)
from domain.profile.service import ProfileService
from storage.db import Database
from storage.run_repo import RunRepo
from tests.support import open_database

SEEDED_EXERCISE_ID = "barbell-back-squat"  # 003 迁移种子：合法具体动作身份


def _seeded_profile() -> Profile:
    """已建档正式档案样本：部分事实（建档过程允许，Stage 1 契约）。"""
    return Profile(training_goal=Fact.known("力量"), body_weight_kg=Fact.known(72.5))


def _formal_with_restriction() -> Profile:
    """带一条具体动作限制与器械事实的正式档案样本（用于限制增删与状态变化 Diff）。"""
    return Profile(
        training_goal=Fact.known("力量"),
        body_weight_kg=Fact.known(72.5),
        available_equipment=Fact.known(("哑铃",)),
        action_restrictions=Fact.known(
            (ActionRestriction(scope="specific_action", target=SEEDED_EXERCISE_ID),)
        ),
    )


def _proposed_profile() -> Profile:
    """草稿拟议结果样本（与 S2-02 同口径）：改目标、补经验、明确无器械、换一条模式限制。"""
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


async def _simulate_formal_commit(db: Database, profile: Profile) -> None:
    """模拟一次正式业务提交（S2-05 之前的测试替身）：外层事务内写档案并推进版本。

    与 S2-05 将实现的确认事务同形态（档案写入与 ``context_version +1`` 同成同败）；
    仅用于在测试库准备业务变更，不是生产旁路。
    """
    async with db.transaction() as conn:
        await ProfileService(db).write_profile_in_transaction(conn, profile)
        await conn.execute(
            "UPDATE user_profile SET context_version = context_version + 1 WHERE id = 1"
        )


async def _draft_count(db: Database) -> int:
    async def op(conn):
        async with conn.execute("SELECT COUNT(*) FROM business_drafts") as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return int(row[0])

    return await db.under_lock(op)


def _entry(diff, field: str):
    return next(entry for entry in diff if entry.field == field)


# ---------- 创建不碰正式事实；基线绑定读取时刻 ----------


async def test_creating_a_draft_leaves_formal_profile_and_version_untouched(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c1")
        await _simulate_formal_commit(db, _seeded_profile())
        service = DraftService(db)
        baseline = await service.prepare_generation_baseline()

        view = await service.create_profile_draft(
            draft_id="d1",
            generation_baseline=baseline,
            conversation_id="c1",
            run_id=None,
            proposed=_proposed_profile(),
        )

        # 草稿只写草稿行：正式档案与统一业务版本原样（01 1.4）
        assert await ProfileRepo(db).read() == baseline
        assert view.draft.status == "pending"
        assert view.draft.revision == INITIAL_REVISION
        assert view.draft.base_business_version == baseline.context_version == 1
        # 基线快照原样落库并读回；拟议内容即草稿内容
        assert view.base_profile == _seeded_profile()
        assert view.proposed_profile == _proposed_profile()
        assert await _draft_count(db) == 1


async def test_saved_draft_keeps_the_read_baseline_not_the_save_time_version(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c1")
        service = DraftService(db)
        baseline = await service.prepare_generation_baseline()
        assert baseline.context_version == 0
        assert baseline.profile is None  # 未建档

        # 生成期间发生一次正式提交：统一业务版本 V → V+1
        await _simulate_formal_commit(db, _seeded_profile())
        assert (await ProfileRepo(db).read()).context_version == 1

        view = await service.create_profile_draft(
            draft_id="d1",
            generation_baseline=baseline,
            conversation_id="c1",
            run_id=None,
            proposed=_proposed_profile(),
        )

        # 保存时间不能替代读取基线：仍绑定读取时的 V=0 与未建档基线（01 1.3）
        assert view.draft.base_business_version == 0
        assert view.base_profile is None
        reloaded = await service.get_draft("d1")
        assert reloaded is not None
        assert reloaded.draft.base_business_version == 0
        assert reloaded.base_profile is None


async def test_draft_content_baseline_source_and_revision_survive_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.db"
    async with open_database(path) as db:
        runs = RunRepo(db)
        await runs.create_conversation("c1")
        await runs.create_run_with_user_message("c1", "r1", "cri-1", "帮我建档")
        service = DraftService(db)
        created = await service.create_profile_draft(
            draft_id="d1",
            generation_baseline=await service.prepare_generation_baseline(),
            conversation_id="c1",
            run_id="r1",
            proposed=_proposed_profile(),
        )
        assert created.draft.run_id == "r1"

    async with open_database(path) as db:
        view = await DraftService(db).get_draft("d1")
        # 内容、基线、来源、revision、状态与 Diff 全部一致
        assert view == created
        assert view is not None
        assert (view.draft.conversation_id, view.draft.run_id) == ("c1", "r1")
        assert view.draft.revision == INITIAL_REVISION
        assert view.draft.base_business_version == 0


async def test_querying_unknown_identity_creates_nothing_and_lists_stay_empty(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service = DraftService(db)
        # 未知身份明确返回 None／空，不创建新草稿
        assert await service.get_draft("d-missing") is None
        assert await service.list_drafts("c-missing") == ()
        assert await _draft_count(db) == 0


# ---------- 来源关联不混淆 ----------


async def test_source_associations_do_not_mix_across_conversations_and_runs(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        runs = RunRepo(db)
        await runs.create_conversation("c1")
        await runs.create_conversation("c2")
        await runs.create_run_with_user_message("c1", "r1", "cri-1", "请求一")
        service = DraftService(db)
        baseline = await service.prepare_generation_baseline()

        d1 = await service.create_profile_draft(
            draft_id="d1",
            generation_baseline=baseline,
            conversation_id="c1",
            run_id="r1",
            proposed=_proposed_profile(),
        )
        await service.create_profile_draft(
            draft_id="d2",
            generation_baseline=baseline,
            conversation_id="c1",
            run_id=None,
            proposed=_proposed_profile(),
        )
        d3 = await service.create_profile_draft(
            draft_id="d3",
            generation_baseline=baseline,
            conversation_id="c2",
            run_id=None,
            proposed=_proposed_profile(),
        )

        first = await service.list_drafts("c1")
        assert [view.draft.id for view in first] == ["d1", "d2"]
        # 同会话内各草稿来源独立保留：有 Run 的记 Run，无 Run 的不伪造
        assert [view.draft.run_id for view in first] == ["r1", None]
        assert {view.draft.conversation_id for view in first} == {"c1"}
        second = await service.list_drafts("c2")
        assert [view.draft.id for view in second] == ["d3"]
        assert {view.draft.conversation_id for view in second} == {"c2"}
        assert d1.draft.conversation_id != d3.draft.conversation_id

        # Run 属于别的会话、会话不存在、Run 不存在：全部拒绝且不落库
        with pytest.raises(UnknownDraftSource, match="r1"):
            await service.create_profile_draft(
                draft_id="d4",
                generation_baseline=baseline,
                conversation_id="c2",
                run_id="r1",
                proposed=_proposed_profile(),
            )
        with pytest.raises(UnknownDraftSource, match="c-missing"):
            await service.create_profile_draft(
                draft_id="d5",
                generation_baseline=baseline,
                conversation_id="c-missing",
                run_id=None,
                proposed=_proposed_profile(),
            )
        with pytest.raises(UnknownDraftSource, match="r-missing"):
            await service.create_profile_draft(
                draft_id="d6",
                generation_baseline=baseline,
                conversation_id="c1",
                run_id="r-missing",
                proposed=_proposed_profile(),
            )
        assert await _draft_count(db) == 3


# ---------- 结构化 Diff：未建档、字段变更、限制增删、三态不折叠 ----------


async def test_diff_expresses_unbuilt_baseline_without_fabricating_defaults(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c1")
        service = DraftService(db)
        view = await service.create_profile_draft(
            draft_id="d1",
            generation_baseline=await service.prepare_generation_baseline(),
            conversation_id="c1",
            run_id=None,
            proposed=_proposed_profile(),
        )

        # 未建档基线：base_profile 是 None，不是显式全未知档案
        assert view.base_profile is None
        # 九个事实字段都有字段对，顺序固定
        assert [entry.field for entry in view.diff] == list(FACT_FIELDS)
        goal = _entry(view.diff, "training_goal")
        assert goal.before.is_unknown
        assert goal.after == Fact.known("增肌")
        assert goal.changed is True
        # 明确无（denied）被原样表达，不是空数组默认值
        equipment = _entry(view.diff, "available_equipment")
        assert equipment.before.is_unknown and equipment.after.is_denied
        assert equipment.changed is True
        restrictions = _entry(view.diff, "action_restrictions")
        assert restrictions.after.value == (
            ActionRestriction(scope="movement_pattern", target="深蹲"),
        )
        # 拟议中仍未收集的字段保持 unknown：不算变更、不补造
        for name in ("body_state", "red_flags"):
            entry = _entry(view.diff, name)
            assert entry.before.is_unknown and entry.after.is_unknown
            assert entry.changed is False


async def test_unbuilt_baseline_stays_distinguishable_from_explicit_empty_profile(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c1")
        service = DraftService(db)
        view = await service.create_profile_draft(
            draft_id="d-empty",
            generation_baseline=ProfileSnapshot(
                profile=Profile.empty(), context_version=0
            ),
            conversation_id="c1",
            run_id=None,
            proposed=_proposed_profile(),
        )
        # 显式全未知基线不是「未建档」：顶层语义由 base_profile 区分
        assert view.base_profile == Profile.empty()
        assert view.base_profile is not None
        # 字段对相同（逐字段都是 unknown → 拟议），区别只在基线载体
        assert view.diff == profile_diff(None, _proposed_profile())


async def test_diff_expresses_field_changes_and_restriction_add_remove(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c1")
        formal = _formal_with_restriction()
        await _simulate_formal_commit(db, formal)
        service = DraftService(db)
        view = await service.create_profile_draft(
            draft_id="d1",
            generation_baseline=await service.prepare_generation_baseline(),
            conversation_id="c1",
            run_id=None,
            proposed=_proposed_profile(),
        )

        assert view.base_profile == formal

        goal = _entry(view.diff, "training_goal")
        assert (goal.before, goal.after) == (Fact.known("力量"), Fact.known("增肌"))
        weight = _entry(view.diff, "body_weight_kg")
        assert (weight.before.value, weight.after.value) == (72.5, 73.0)

        # 限制增删：删一条具体动作限制、加一条模式限制，before/after 集合原样表达
        restrictions = _entry(view.diff, "action_restrictions")
        assert restrictions.before.value == (
            ActionRestriction(scope="specific_action", target=SEEDED_EXERCISE_ID),
        )
        assert restrictions.after.value == (
            ActionRestriction(scope="movement_pattern", target="深蹲"),
        )
        assert restrictions.changed is True

        # 已收集器械 → 明确无器械：状态变化被表达，不折叠成空数组
        equipment = _entry(view.diff, "available_equipment")
        assert equipment.before.value == ("哑铃",)
        assert equipment.after.is_denied

        changed = [entry.field for entry in view.diff if entry.changed]
        assert set(changed) == {
            "training_goal",
            "training_experience",
            "weekly_frequency",
            "session_duration_minutes",
            "available_equipment",
            "action_restrictions",
            "body_weight_kg",
        }
        # 未涉及字段不变且前后一致
        for name in ("body_state", "red_flags"):
            entry = _entry(view.diff, name)
            assert entry.changed is False
            assert entry.before == entry.after


async def test_diff_keeps_unknown_denied_and_known_empty_distinct(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c1")
        formal = Profile(
            red_flags=Fact.denied(),
            body_state=Fact.known(("肩部偶有不适",)),
        )
        await _simulate_formal_commit(db, formal)
        service = DraftService(db)
        proposed = Profile(
            available_equipment=Fact.denied(),
            red_flags=Fact.known(()),
            body_state=Fact.known(()),
        )
        view = await service.create_profile_draft(
            draft_id="d1",
            generation_baseline=await service.prepare_generation_baseline(),
            conversation_id="c1",
            run_id=None,
            proposed=proposed,
        )

        # 未收集 → 明确无：三态原样保留
        equipment = _entry(view.diff, "available_equipment")
        assert equipment.before.is_unknown and equipment.after.is_denied
        assert equipment.changed is True
        # denied（明确无）与显式空集合 known(()) 不是同一语义
        red_flags = _entry(view.diff, "red_flags")
        assert red_flags.before.is_denied and red_flags.after == Fact.known(())
        assert red_flags.changed is True
        # known 值变化按值比较
        body_state = _entry(view.diff, "body_state")
        assert body_state.before.value == ("肩部偶有不适",)
        assert body_state.after == Fact.known(())
        assert body_state.changed is True


# ---------- 非法内容不落库；输入对象不被修改 ----------


async def test_invalid_proposed_content_is_rejected_before_any_write(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c1")
        service = DraftService(db)
        baseline = await service.prepare_generation_baseline()

        invalid_proposals = (
            (
                "类型非法",
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
        for reason, proposed, expected_error in invalid_proposals:
            with pytest.raises(expected_error):
                await service.create_profile_draft(
                    draft_id="d-bad",
                    generation_baseline=baseline,
                    conversation_id="c1",
                    run_id=None,
                    proposed=proposed,
                )
            assert await _draft_count(db) == 0, reason  # 校验失败不落库
        # 同一身份在失败后仍可正常创建（无残留半状态）
        view = await service.create_profile_draft(
            draft_id="d-bad",
            generation_baseline=baseline,
            conversation_id="c1",
            run_id=None,
            proposed=_proposed_profile(),
        )
        assert view.draft.id == "d-bad"
        assert await _draft_count(db) == 1


async def test_creation_does_not_mutate_caller_passed_objects(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c1")
        service = DraftService(db)
        baseline = await service.prepare_generation_baseline()
        baseline_before = dataclasses.replace(baseline)
        proposed = _proposed_profile()
        proposed_before = dataclasses.replace(proposed)

        view = await service.create_profile_draft(
            draft_id="d1",
            generation_baseline=baseline,
            conversation_id="c1",
            run_id=None,
            proposed=proposed,
        )

        # 传入的基线快照与拟议档案不被原地修改
        assert baseline == baseline_before
        assert proposed == proposed_before
        assert view.base_profile == baseline.profile
        assert view.proposed_profile == proposed
