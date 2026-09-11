"""Stage 3 S3-04：计划草稿创建、查询与结构化 Diff。

验收对照（stage3.md §5 S3-04）：

- 生成输入从档案／限制／版本同一快照准备；建草稿前后正式计划／档案／业务版本不变。
- 基线绑定读取时刻：读版本 V → 另一草稿提交推进到 V+1 → 本草稿仍绑定 V 与读取时快照，
  重开后内容一致。
- 来源不混淆：会话隔离、Run 归属校验、不存在的来源拒绝且不落库；计划草稿不按档案形状
  静默解码。
- 替换场景能表达旧日程取消清单预览：只含旧版「未取消、未存储锁定、按业务日期尚未到期
  锁定」的日程，仅拟议，``scheduled_sessions`` 与 ``plan_versions`` 不被修改。

边界（stage3.md §5 S3-04）：不实现纠错与丢弃（S3-05）、确认事务与日程原子切换（S3-06）；
本文件只经内部应用层、``tmp_path`` 下的临时文件库，不触碰真实用户数据目录。测试里模拟
「已确认计划」的原始 SQL 插入是 S3-06 之前的替身，不是生产写入旁路。
"""

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.confirm import ConfirmService
from app.draft_repo import InvalidDraftRow
from app.drafts import DraftKindMismatch, DraftService, UnknownDraftSource
from app.plan_drafts import PlanDraftService
from domain.actions.repo import ExerciseRepo
from domain.plan.rules import InvalidPlanPayload, project_sessions
from domain.plan.schema import (
    CalendarCycle,
    DisplaySnapshot,
    IntRange,
    InvalidPlanRow,
    PlanExerciseItem,
    PlanPayload,
    PlanProposal,
    PlanWorkout,
    Progression,
    ProposedSessionCancellation,
    RepsPrescription,
    RestCycleSlot,
    WorkoutCycleSlot,
    payload_to_json,
    proposal_from_json,
    proposal_to_json,
)
from domain.plan.service import PlanGenerationReady, generate_ppl_plan
from domain.profile.repo import ProfileRepo
from domain.profile.rules import InvalidProfilePatch, UnknownExerciseReference
from domain.profile.schema import (
    ActionRestriction,
    Fact,
    Profile,
    ProfilePatch,
    ProfileSnapshot,
    profile_to_json,
)
from domain.profile.service import ProfileService
from storage.db import Database
from storage.run_repo import RunRepo
from tests.support import open_database

STARTS_ON = date(2026, 9, 14)
REVIEW_ON = date(2026, 10, 12)
ANCHOR_DATE = date(2026, 9, 14)
BUSINESS_DATE = date(2026, 9, 20)
FULL_EQUIPMENT = ("杠铃", "哑铃", "绳索", "引体架")
SEEDED_EXERCISE_ID = "barbell-back-squat"


def _profile(**overrides: object) -> Profile:
    """完整档案（八项均已明确回答）；用例只在需要时覆盖指定事实。"""
    facts: dict[str, object] = {
        "training_goal": Fact.known("增肌"),
        "training_experience": Fact.known("1 年"),
        "weekly_frequency": Fact.known(3),
        "session_duration_minutes": Fact.known(60),
        "available_equipment": Fact.known(FULL_EQUIPMENT),
        "action_restrictions": Fact.denied(),
        "body_conditions": Fact.denied(),
        "body_weight_kg": Fact.known(70.0),
    }
    facts.update(overrides)
    return Profile(**facts)  # type: ignore[arg-type]


async def _commit_profile(db: Database, profile: Profile) -> None:
    """模拟一次正式档案提交（S2-05 之前/之外的测试替身）：写档案并推进版本到 V+1。

    与确认事务同形态（档案写入与 ``context_version +1`` 同成同败）；仅用于准备测试库，
    不是生产旁路。
    """
    async with db.transaction() as conn:
        await ProfileService(db).write_profile_in_transaction(conn, profile)
        await conn.execute(
            "UPDATE user_profile SET context_version = context_version + 1 WHERE id = 1"
        )


async def _seed_confirmed_plan(
    db: Database,
    *,
    conversation_id: str,
    plan_id: str,
    payload,
    sessions: tuple[tuple[str, str, str | None, str | None], ...],
    version: int = 1,
) -> None:
    """模拟一次计划确认（S3-06 替身）：追加 plan_versions、写日程、推进业务版本。

    ``sessions`` 每项为 ``(id, scheduled_on, locked_at, cancelled_at)``。来源草稿行与计划
    版本同事务插入，满足 ``plan_versions.source_draft_id`` 外键；不做领域复查（那是 S3-06
    的职责），只准备「已存在正式计划」的读取场景。
    """
    source_draft_id = f"{plan_id}-source-draft"
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO business_drafts (id, kind, conversation_id, run_id,"
            " base_profile_json, proposed_profile_json, base_business_version,"
            " revision, status, committed_revision, committed_business_version,"
            " created_at, updated_at, proposed_plan_json,"
            " proposed_profile_patch_json)"
            " VALUES (?, 'plan', ?, NULL, NULL, ?, 0, 1, 'committed', 1, 1,"
            " ?, ?, ?, NULL)",
            (
                source_draft_id,
                conversation_id,
                profile_to_json(_profile()),
                "2026-09-11T00:00:00+00:00",
                "2026-09-11T00:00:00+00:00",
                payload_to_json(payload),
            ),
        )
        await conn.execute(
            "INSERT INTO plan_versions (id, version, source_plan_version_id,"
            " starts_on, review_on, mode, payload_json, source_draft_id,"
            " confirmed_at) VALUES (?, ?, NULL, ?, ?, 'regular', ?, ?,"
            " '2026-09-11T00:00:00+00:00')",
            (
                plan_id,
                version,
                STARTS_ON.isoformat(),
                REVIEW_ON.isoformat(),
                payload_to_json(payload),
                source_draft_id,
            ),
        )
        for session_id, scheduled_on, locked_at, cancelled_at in sessions:
            await conn.execute(
                "INSERT INTO scheduled_sessions (id, plan_version_id,"
                " plan_workout_key, scheduled_on, cancelled_at, locked_at)"
                " VALUES (?, ?, 'push', ?, ?, ?)",
                (session_id, plan_id, scheduled_on, cancelled_at, locked_at),
            )
        await conn.execute(
            "UPDATE user_profile SET context_version = context_version + 1 WHERE id = 1"
        )


async def _payload(db: Database, profile: Profile, **overrides: object):
    """从推荐候选生成一个已通过 D9 自检的合法 payload（复用 S3-03 生成）。"""
    candidates = await ExerciseRepo(db).list_recommendable()
    kwargs: dict[str, object] = {
        "starts_on": STARTS_ON,
        "review_on": REVIEW_ON,
        "anchor_date": ANCHOR_DATE,
    }
    kwargs.update(overrides)
    result = generate_ppl_plan(profile, candidates, **kwargs)  # type: ignore[arg-type]
    assert isinstance(result, PlanGenerationReady), result
    return result.payload


_SESSIONS_SELECT = "SELECT * FROM scheduled_sessions ORDER BY id"
_PLANS_SELECT = "SELECT * FROM plan_versions ORDER BY id"


async def _draft_count(db: Database) -> int:
    async def op(conn):
        async with conn.execute("SELECT COUNT(*) FROM business_drafts") as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return int(row[0])

    return await db.under_lock(op)


async def _plan_version_rows(db: Database) -> tuple[tuple[object, ...], ...]:
    """正式计划版本行（字面量 SQL，不拼接表名）。"""

    async def op(conn):
        async with conn.execute("SELECT * FROM plan_versions ORDER BY id") as cursor:
            rows = await cursor.fetchall()
        return tuple(tuple(row) for row in rows)

    return await db.under_lock(op)


async def _session_rows(db: Database) -> tuple[tuple[object, ...], ...]:
    """应训练名额行（字面量 SQL，不拼接表名）。"""

    async def op(conn):
        async with conn.execute(
            "SELECT * FROM scheduled_sessions ORDER BY id"
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(tuple(row) for row in rows)

    return await db.under_lock(op)


def _entry(diff, field: str):
    return next(item for item in diff if item.field == field)


# ---------- 同一快照准备；建草稿不改正式事实 ----------


async def test_prepare_reads_profile_candidates_and_current_plan_in_one_snapshot(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c1")
        await _commit_profile(db, _profile())
        payload = await _payload(db, _profile())
        await _seed_confirmed_plan(
            db,
            conversation_id="c1",
            plan_id="pv-1",
            payload=payload,
            sessions=(("ss-1", "2026-09-21", None, None),),
        )

        preparation = await PlanDraftService(db).prepare_generation_input()

        # 档案（含限制）与业务版本同一快照
        assert preparation.snapshot == ProfileSnapshot(
            profile=_profile(), context_version=2
        )
        # 推荐候选：S3-02 系统迁移后恰为已核对 24 项
        assert len(preparation.candidates) == 24
        # 当前正式计划＋同一快照读到的全部日程（含锁定／取消状态）
        assert preparation.current_plan is not None
        assert preparation.current_plan.id == "pv-1"
        assert [session.id for session in preparation.current_sessions] == ["ss-1"]


async def test_creating_plan_draft_leaves_formal_facts_and_version_untouched(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c1")
        await _commit_profile(db, _profile())
        service = PlanDraftService(db)
        preparation = await service.prepare_generation_input()
        payload = await _payload(db, _profile())
        profile_before = await ProfileRepo(db).read()
        plans_before = await _plan_version_rows(db)
        drafts_before = await _draft_count(db)

        view = await service.create_plan_draft(
            draft_id="d-plan",
            preparation=preparation,
            conversation_id="c1",
            run_id=None,
            payload=payload,
            starts_on=STARTS_ON,
            review_on=REVIEW_ON,
            business_date=BUSINESS_DATE,
        )

        # 正式档案／业务版本／计划版本／日程原样；只多一条草稿行
        assert await ProfileRepo(db).read() == profile_before
        assert await _plan_version_rows(db) == plans_before
        assert await _session_rows(db) == ()
        assert await _draft_count(db) == drafts_before + 1
        # 草稿形态：Pending、revision 1、绑定读取时刻的业务版本、来源为会话
        assert view.draft.kind == "plan"
        assert view.draft.status == "pending"
        assert view.draft.revision == 1
        assert view.draft.base_business_version == preparation.snapshot.context_version
        assert view.draft.conversation_id == "c1"
        assert view.draft.run_id is None
        # 拟议内容＝生成结果；拟议日程由 payload＋区间投影，不是客户端传入
        assert view.proposal.payload == payload
        assert view.proposal.starts_on == STARTS_ON
        assert view.proposal.review_on == REVIEW_ON
        assert view.schedules == project_sessions(
            payload, starts_on=STARTS_ON, review_on=REVIEW_ON
        )
        # 首个计划：无替换基线、无取消预览、字段 Diff 的 before 为 None
        assert view.proposal.source_plan_version_id is None
        assert view.proposal.cancellations == ()
        assert _entry(view.plan_diff, "starts_on").before is None
        assert _entry(view.plan_diff, "starts_on").after == STARTS_ON
        assert _entry(view.plan_diff, "workouts").before is None
        assert _entry(view.plan_diff, "workouts").changed is True
        assert _entry(view.plan_diff, "cancellations").after == ()


async def test_generation_baseline_binding_survives_another_commit(
    tmp_path: Path,
) -> None:
    """读版本 V → 另一草稿提交 → 本草稿仍绑定 V 与读取时快照；重开一致。"""
    path = tmp_path / "app.db"
    formal_before = _profile()
    async with open_database(path) as db:
        runs = RunRepo(db)
        await runs.create_conversation("c1")
        await _commit_profile(db, formal_before)
        service = PlanDraftService(db)

        # 读取基线：V=1，档案为 formal_before
        preparation = await service.prepare_generation_input()
        assert preparation.snapshot.context_version == 1

        # 另一份档案草稿在读取之后提交：业务版本推进到 V+1、正式档案被改写
        drafts = DraftService(db)
        other = await drafts.create_profile_draft(
            draft_id="d-other",
            generation_baseline=await drafts.prepare_generation_baseline(),
            conversation_id="c1",
            run_id=None,
            proposed=_profile(training_goal=Fact.known("力量")),
        )
        await ConfirmService(db).confirm_profile_draft(
            draft_id=other.draft.id, seen_revision=1
        )
        assert (await ProfileRepo(db).read()).context_version == 2

        view = await service.create_plan_draft(
            draft_id="d-plan",
            preparation=preparation,  # 读取时刻的快照，不重读
            conversation_id="c1",
            run_id=None,
            payload=await _payload(db, formal_before),
            starts_on=STARTS_ON,
            review_on=REVIEW_ON,
            business_date=BUSINESS_DATE,
        )

        # 保存时间不能替代读取基线：仍绑定 V=1 与读取时的档案快照
        assert view.draft.base_business_version == 1
        assert view.base_profile == formal_before
        # 拟议条件按读取快照计算，不受后续正式提交影响
        assert view.proposed_profile == formal_before

    async with open_database(path) as db:
        reopened = await PlanDraftService(db).get_plan_draft("d-plan")
        assert reopened is not None
        # 重开后内容、基线、来源、revision 与 Diff 全部一致
        assert reopened == view
        assert reopened.draft.base_business_version == 1
        assert reopened.proposal.payload == view.proposal.payload


# ---------- 替换预览：只拟议，不动正式事实 ----------


async def test_replacement_preview_lists_only_future_unlocked_sessions_proposal_only(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c1")
        await _commit_profile(db, _profile())
        baseline_payload = await _payload(db, _profile())
        await _seed_confirmed_plan(
            db,
            conversation_id="c1",
            plan_id="pv-1",
            payload=baseline_payload,
            sessions=(
                # 已到期（业务日期 09-20 >= 09-14）：按日期规则已锁定，不取消
                ("ss-past", "2026-09-14", None, None),
                # 未来未锁定：提案取消
                ("ss-future", "2026-09-21", None, None),
                # 未来但已存储锁定（已确认完成／漏练）：不动
                ("ss-stored-locked", "2026-09-22", "2026-09-10T00:00:00+00:00", None),
                # 已取消：不重复取消
                ("ss-cancelled", "2026-09-23", None, "2026-09-10T00:00:00+00:00"),
                # 未来未锁定：提案取消
                ("ss-future-2", "2026-09-24", None, None),
            ),
        )
        service = PlanDraftService(db)
        preparation = await service.prepare_generation_input()
        sessions_before = await _session_rows(db)
        plans_before = await _plan_version_rows(db)

        view = await service.create_plan_draft(
            draft_id="d-replace",
            preparation=preparation,
            conversation_id="c1",
            run_id=None,
            payload=await _payload(db, _profile()),
            starts_on=STARTS_ON,
            review_on=REVIEW_ON,
            business_date=BUSINESS_DATE,
        )

        # 取消预览：恰为未来未锁定日程，按应训练日排序；含正式身份与计划版本
        assert view.proposal.source_plan_version_id == "pv-1"
        assert view.proposal.cancellations == (
            ProposedSessionCancellation(
                scheduled_session_id="ss-future",
                plan_version_id="pv-1",
                plan_workout_key="push",
                scheduled_on=date(2026, 9, 21),
            ),
            ProposedSessionCancellation(
                scheduled_session_id="ss-future-2",
                plan_version_id="pv-1",
                plan_workout_key="push",
                scheduled_on=date(2026, 9, 24),
            ),
        )
        # 结构化 Diff：基线为绑定的来源版本，取消清单是字段对的一部分
        assert _entry(view.plan_diff, "starts_on").before == STARTS_ON
        assert _entry(view.plan_diff, "review_on").before == REVIEW_ON
        assert _entry(view.plan_diff, "mode").before == "regular"
        cancellations = _entry(view.plan_diff, "cancellations")
        assert cancellations.before == ()
        assert cancellations.after == view.proposal.cancellations
        assert cancellations.changed is True
        # 仅拟议：正式日程与计划版本逐行未变（cancelled_at 仍为 NULL）
        assert await _session_rows(db) == sessions_before
        assert await _plan_version_rows(db) == plans_before

    async with open_database(tmp_path / "app.db") as db:
        reopened = await PlanDraftService(db).get_plan_draft("d-replace")
        assert reopened is not None and reopened == view
        # 重开后正式日程仍未取消
        assert await _session_rows(db) == sessions_before


async def test_replacement_source_stays_bound_after_another_plan_commit(
    tmp_path: Path,
) -> None:
    """绑定的来源版本是生成读取时刻那一版，后续新版本提交不改写本草稿的替换基线。"""
    path = tmp_path / "app.db"
    async with open_database(path) as db:
        await RunRepo(db).create_conversation("c1")
        await _commit_profile(db, _profile())
        baseline_payload = await _payload(db, _profile())
        await _seed_confirmed_plan(
            db,
            conversation_id="c1",
            plan_id="pv-1",
            payload=baseline_payload,
            sessions=(("ss-future", "2026-09-21", None, None),),
        )
        service = PlanDraftService(db)
        preparation = await service.prepare_generation_input()
        # 读取之后又提交了一个新计划版本（替换来源应仍绑 pv-1）
        await _seed_confirmed_plan(
            db,
            conversation_id="c1",
            plan_id="pv-2",
            payload=baseline_payload,
            sessions=(("ss-2", "2026-09-25", None, None),),
            version=2,
        )

        view = await service.create_plan_draft(
            draft_id="d-replace",
            preparation=preparation,
            conversation_id="c1",
            run_id=None,
            payload=await _payload(db, _profile()),
            starts_on=STARTS_ON,
            review_on=REVIEW_ON,
            business_date=BUSINESS_DATE,
        )

        assert view.draft.base_business_version == 2
        assert view.proposal.source_plan_version_id == "pv-1"
        assert [item.scheduled_session_id for item in view.proposal.cancellations] == [
            "ss-future"
        ]
        assert _entry(view.plan_diff, "starts_on").before == STARTS_ON


# ---------- 可选档案补丁：拟议条件与档案 Diff ----------


async def test_optional_profile_patch_is_proposed_only_and_diffed(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c1")
        formal = _profile()
        await _commit_profile(db, formal)
        service = PlanDraftService(db)
        preparation = await service.prepare_generation_input()
        # 与计划兼容的拟议条件：器械是原条件超集（计划动作仍全部可用）；限制加在
        # **不在计划内**的动作上（leg-press-45 在已拍 24 项内、但不是 PPL 模板动作）。
        patch = ProfilePatch(
            facts={"available_equipment": Fact.known(FULL_EQUIPMENT + ("弹力带",))},
            add_restrictions=(
                ActionRestriction(scope="specific_action", target="leg-press-45"),
            ),
        )

        view = await service.create_plan_draft(
            draft_id="d-combo",
            preparation=preparation,
            conversation_id="c1",
            run_id=None,
            payload=await _payload(db, formal),
            starts_on=STARTS_ON,
            review_on=REVIEW_ON,
            business_date=BUSINESS_DATE,
            patch=patch,
        )

        # 补丁保存在草稿内；拟议条件是应用补丁后的档案；正式档案不变
        assert view.proposed_profile_patch == patch
        assert view.proposed_profile.available_equipment == Fact.known(
            FULL_EQUIPMENT + ("弹力带",)
        )
        assert view.proposed_profile.restrictions == (
            ActionRestriction(scope="specific_action", target="leg-press-45"),
        )
        assert await ProfileRepo(db).read() == ProfileSnapshot(
            profile=formal, context_version=1
        )
        # 档案字段 Diff 存在且表达限制增删与器械变化；未涉及字段不变
        assert view.profile_diff is not None
        changed = [entry.field for entry in view.profile_diff if entry.changed]
        assert set(changed) == {"available_equipment", "action_restrictions"}
        assert _entry(view.profile_diff, "available_equipment").after == Fact.known(
            FULL_EQUIPMENT + ("弹力带",)
        )
        # 限制集合：正式为空（denied），拟议为一条具体动作限制
        assert _entry(view.profile_diff, "action_restrictions").after.value == (
            ActionRestriction(scope="specific_action", target="leg-press-45"),
        )

        # 无补丁时不伪造档案 Diff
        plain = await service.create_plan_draft(
            draft_id="d-plain",
            preparation=preparation,
            conversation_id="c1",
            run_id=None,
            payload=await _payload(db, formal),
            starts_on=STARTS_ON,
            review_on=REVIEW_ON,
            business_date=BUSINESS_DATE,
        )
        assert plain.proposed_profile_patch is None
        assert plain.profile_diff is None
        assert plain.proposed_profile == formal

    async with open_database(tmp_path / "app.db") as db:
        reopened = await PlanDraftService(db).get_plan_draft("d-combo")
        assert reopened is not None and reopened.proposed_profile_patch == patch


async def test_patch_incompatible_equipment_or_restriction_is_rejected(
    tmp_path: Path,
) -> None:
    """受限组合必须按**补丁后的拟议条件**复查计划（01 1.5）：不兼容就不落库。

    回归：调用方按旧条件生成的计划（含 SEEDED_EXERCISE_ID）配上收窄器械／新增限制的
    补丁时，必须被拒绝，不得把「已受拟议限制的动作」写进草稿。
    """
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c1")
        formal = _profile()
        await _commit_profile(db, formal)
        service = PlanDraftService(db)
        preparation = await service.prepare_generation_input()
        payload = await _payload(db, formal)
        # 前提：计划确实包含被判突的杠铃动作（否则本用例没在验证冲突）
        assert any(
            item.exercise_id == SEEDED_EXERCISE_ID
            for workout in payload.plan_workouts
            for item in workout.exercises
        )
        count = await _draft_count(db)

        incompatible_patches = (
            (
                "器械收窄到哑铃（杠铃动作不可用）",
                ProfilePatch(facts={"available_equipment": Fact.known(("哑铃",))}),
                "不可用",
            ),
            (
                "新增限制覆盖计划内动作",
                ProfilePatch(
                    add_restrictions=(
                        ActionRestriction(
                            scope="specific_action", target=SEEDED_EXERCISE_ID
                        ),
                    )
                ),
                "命中限制",
            ),
        )
        for reason, patch, expected_message in incompatible_patches:
            with pytest.raises(InvalidPlanPayload, match=expected_message):
                await service.create_plan_draft(
                    draft_id="d-incompatible",
                    preparation=preparation,
                    conversation_id="c1",
                    run_id=None,
                    payload=payload,
                    starts_on=STARTS_ON,
                    review_on=REVIEW_ON,
                    business_date=BUSINESS_DATE,
                    patch=patch,
                )
            assert await _draft_count(db) == count, reason
        # 正式档案与业务版本不变
        assert await ProfileRepo(db).read() == ProfileSnapshot(
            profile=formal, context_version=1
        )


async def test_plan_diff_covers_prescription_and_cycle_changes(
    tmp_path: Path,
) -> None:
    """同一动作身份下的处方／循环变化必须在结构化 Diff 中可见（不压成 key 摘要）。"""
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c1")
        await _commit_profile(db, _profile())
        payload = await _payload(db, _profile())
        await _seed_confirmed_plan(
            db,
            conversation_id="c1",
            plan_id="pv-1",
            payload=payload,
            sessions=(("ss-1", "2026-09-21", None, None),),
        )
        service = PlanDraftService(db)
        preparation = await service.prepare_generation_input()

        # 只改处方（组数）+ 循环相位：动作身份与 workout_key 完全不变
        first_workout = payload.plan_workouts[0]
        first_item = first_workout.exercises[0]
        changed_item = replace(
            first_item,
            prescription=replace(first_item.prescription, work_sets=5),
        )
        changed_payload = replace(
            payload,
            plan_workouts=(
                replace(
                    first_workout,
                    exercises=(changed_item,) + first_workout.exercises[1:],
                ),
            )
            + payload.plan_workouts[1:],
            calendar_cycle=replace(
                payload.calendar_cycle, anchor_date=ANCHOR_DATE + timedelta(days=1)
            ),
        )

        view = await service.create_plan_draft(
            draft_id="d-prescription",
            preparation=preparation,
            conversation_id="c1",
            run_id=None,
            payload=changed_payload,
            starts_on=STARTS_ON,
            review_on=REVIEW_ON,
            business_date=BUSINESS_DATE,
        )

        workouts = _entry(view.plan_diff, "workouts")
        cycle = _entry(view.plan_diff, "calendar_cycle")
        assert workouts.changed is True
        assert cycle.changed is True
        # before／after 保留完整处方语义（不是只有 key 与动作身份）
        assert workouts.before[0].exercises[0].prescription.work_sets == 4
        assert workouts.after[0].exercises[0].prescription.work_sets == 5
        assert workouts.before[0].exercises[0].exercise_id == first_item.exercise_id
        assert cycle.before.anchor_date == ANCHOR_DATE
        assert cycle.after.anchor_date == ANCHOR_DATE + timedelta(days=1)


async def test_plan_diff_surfaces_template_key_only_change(tmp_path: Path) -> None:
    """仅 template_key 变化也必须可见（否则模板来源变更在 Diff 中被吞掉）。"""
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c1")
        await _commit_profile(db, _profile())
        payload = await _payload(db, _profile())
        await _seed_confirmed_plan(
            db,
            conversation_id="c1",
            plan_id="pv-1",
            payload=payload,
            sessions=(("ss-1", "2026-09-21", None, None),),
        )
        service = PlanDraftService(db)
        preparation = await service.prepare_generation_input()

        view = await service.create_plan_draft(
            draft_id="d-template",
            preparation=preparation,
            conversation_id="c1",
            run_id=None,
            payload=replace(payload, template_key="ppl-return"),
            starts_on=STARTS_ON,
            review_on=REVIEW_ON,
            business_date=BUSINESS_DATE,
        )

        template = _entry(view.plan_diff, "template_key")
        assert (template.before, template.after) == ("ppl", "ppl-return")
        assert template.changed is True
        # 仅模板变化：其余字段未见变化
        assert _entry(view.plan_diff, "workouts").changed is False
        assert _entry(view.plan_diff, "calendar_cycle").changed is False


# ---------- kind 分派：Stage 2 草稿入口不碰计划草稿（S3-05/S3-06 前） ----------


async def test_profile_draft_entry_points_reject_plan_drafts(tmp_path: Path) -> None:
    """回归：档案草稿入口不得改／提交／丢弃计划草稿（否则可把补丁当档案提交）。

    验证纠错、丢弃、确认三条路径都在任何写入之前报错；草稿保持 Pending、revision 与
    拟议内容不变；正式档案、业务版本、计划版本、日程逐行不变。
    """
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c1")
        formal = _profile()
        await _commit_profile(db, formal)
        service = PlanDraftService(db)
        preparation = await service.prepare_generation_input()
        created = await service.create_plan_draft(
            draft_id="d-plan",
            preparation=preparation,
            conversation_id="c1",
            run_id=None,
            payload=await _payload(db, formal),
            starts_on=STARTS_ON,
            review_on=REVIEW_ON,
            business_date=BUSINESS_DATE,
            patch=ProfilePatch(facts={"body_weight_kg": Fact.known(71.0)}),
        )
        profile_before = await ProfileRepo(db).read()
        plans_before = await _plan_version_rows(db)
        sessions_before = await _session_rows(db)

        drafts = DraftService(db)
        with pytest.raises(DraftKindMismatch, match="plan"):
            await drafts.revise_profile_draft(
                draft_id="d-plan",
                seen_revision=1,
                proposed=_profile(training_goal=Fact.known("力量")),
            )
        with pytest.raises(DraftKindMismatch, match="plan"):
            await drafts.discard_draft(draft_id="d-plan")
        with pytest.raises(DraftKindMismatch, match="plan"):
            await ConfirmService(db).confirm_profile_draft(
                draft_id="d-plan", seen_revision=1
            )

        # 计划草稿仍 Pending、revision 与拟议内容原样（没有被纠错／丢弃／提交）
        reloaded = await service.get_plan_draft("d-plan")
        assert reloaded is not None
        assert reloaded == created
        assert reloaded.draft.status == "pending"
        assert reloaded.draft.revision == 1
        # 正式事实与版本不变：没有半套提交，也没有 context_version +1
        assert await ProfileRepo(db).read() == profile_before
        assert await _plan_version_rows(db) == plans_before
        assert await _session_rows(db) == sessions_before
        # 档案草稿入口也不按档案形状解码计划草稿（查询路径明确报错）
        with pytest.raises(DraftKindMismatch, match="plan"):
            await drafts.get_draft("d-plan")
        assert await drafts.list_drafts("c1") == ()


# ---------- 身份／会话作用域与来源校验 ----------


async def test_queries_are_scoped_by_identity_and_session_and_sources_do_not_mix(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        runs = RunRepo(db)
        await runs.create_conversation("c1")
        await runs.create_conversation("c2")
        await runs.create_run_with_user_message("c1", "r1", "cri-1", "帮我安排计划")
        await _commit_profile(db, _profile())
        service = PlanDraftService(db)
        preparation = await service.prepare_generation_input()
        payload = await _payload(db, _profile())

        d1 = await service.create_plan_draft(
            draft_id="d1",
            preparation=preparation,
            conversation_id="c1",
            run_id="r1",
            payload=payload,
            starts_on=STARTS_ON,
            review_on=REVIEW_ON,
            business_date=BUSINESS_DATE,
        )
        await service.create_plan_draft(
            draft_id="d2",
            preparation=preparation,
            conversation_id="c1",
            run_id=None,
            payload=payload,
            starts_on=STARTS_ON,
            review_on=REVIEW_ON,
            business_date=BUSINESS_DATE,
        )
        d3 = await service.create_plan_draft(
            draft_id="d3",
            preparation=preparation,
            conversation_id="c2",
            run_id=None,
            payload=payload,
            starts_on=STARTS_ON,
            review_on=REVIEW_ON,
            business_date=BUSINESS_DATE,
        )

        first = await service.list_plan_drafts("c1")
        assert [view.draft.id for view in first] == ["d1", "d2"]
        assert [view.draft.run_id for view in first] == ["r1", None]
        second = await service.list_plan_drafts("c2")
        assert [view.draft.id for view in second] == ["d3"]
        assert d1.draft.conversation_id != d3.draft.conversation_id
        # 未知身份／会话：明确空，不创建新草稿
        assert await service.get_plan_draft("d-missing") is None
        assert await service.list_plan_drafts("c-missing") == ()

        # 档案草稿不按计划形状静默解码（查询计划草稿入口拒绝别的 kind）
        drafts = DraftService(db)
        profile_draft = await drafts.create_profile_draft(
            draft_id="d-profile",
            generation_baseline=await drafts.prepare_generation_baseline(),
            conversation_id="c1",
            run_id=None,
            proposed=_profile(training_goal=Fact.known("力量")),
        )
        assert profile_draft.draft.kind == "profile_update"
        with pytest.raises(InvalidDraftRow, match="profile_update"):
            await service.get_plan_draft("d-profile")
        # 会话列表只含计划草稿，不把档案草稿混进来
        assert [view.draft.id for view in await service.list_plan_drafts("c1")] == [
            "d1",
            "d2",
        ]

        # 来源不成立：会话不存在、Run 属于别的会话、Run 不存在——全部拒绝且不落库
        count = await _draft_count(db)
        for conversation_id, run_id, marker in (
            ("c-missing", None, "c-missing"),
            ("c2", "r1", "r1"),  # r1 属于 c1，用它建 c2 的草稿必须被拒
            ("c1", "r-missing", "r-missing"),
        ):
            with pytest.raises(UnknownDraftSource, match=marker):
                await service.create_plan_draft(
                    draft_id="d-bad-source",
                    preparation=preparation,
                    conversation_id=conversation_id,
                    run_id=run_id,
                    payload=payload,
                    starts_on=STARTS_ON,
                    review_on=REVIEW_ON,
                    business_date=BUSINESS_DATE,
                )
        assert await _draft_count(db) == count


# ---------- 非法拟议内容不落库 ----------


async def test_invalid_payload_patch_or_scope_is_rejected_before_any_write(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c1")
        await _commit_profile(db, _profile())
        service = PlanDraftService(db)
        preparation = await service.prepare_generation_input()
        payload = await _payload(db, _profile())
        count = await _draft_count(db)

        # 目录引用：改成不可推荐／不存在的动作身份（候选边界不由调用方信任）
        non_candidate = replace(
            payload.plan_workouts[0].exercises[0], exercise_id="no-such-action"
        )
        broken_payload = replace(
            payload,
            plan_workouts=(
                replace(
                    payload.plan_workouts[0],
                    exercises=(non_candidate,) + payload.plan_workouts[0].exercises[1:],
                ),
            )
            + payload.plan_workouts[1:],
        )
        invalid_cases: tuple[tuple[str, dict, type[Exception]], ...] = (
            (
                "目录外动作身份",
                {"payload": broken_payload},
                InvalidPlanPayload,
            ),
            (
                "生效范围倒置",
                {"payload": payload, "starts_on": REVIEW_ON, "review_on": STARTS_ON},
                InvalidPlanPayload,
            ),
            (
                "范围内没有训练日（投影为空）",
                {
                    "payload": payload,
                    "starts_on": date(2026, 9, 15),
                    "review_on": date(2026, 9, 16),
                },
                InvalidPlanPayload,
            ),
            (
                "模式不在已拍集合",
                {"payload": payload, "mode": "unknown"},
                InvalidPlanPayload,
            ),
            (
                "业务日期不是日期",
                {"payload": payload, "business_date": "2026-09-20"},
                InvalidPlanPayload,
            ),
            (
                "补丁引用目录外动作",
                {
                    "payload": payload,
                    "patch": ProfilePatch(
                        add_restrictions=(
                            ActionRestriction(
                                scope="specific_action", target="no-such-action"
                            ),
                        )
                    ),
                },
                UnknownExerciseReference,
            ),
            (
                "补丁把事实改为未知",
                {
                    "payload": payload,
                    "patch": ProfilePatch(facts={"body_weight_kg": Fact.unknown()}),
                },
                InvalidProfilePatch,
            ),
        )
        for reason, overrides, expected_error in invalid_cases:
            kwargs: dict = {
                "draft_id": "d-bad",
                "preparation": preparation,
                "conversation_id": "c1",
                "run_id": None,
                "payload": payload,
                "starts_on": STARTS_ON,
                "review_on": REVIEW_ON,
                "business_date": BUSINESS_DATE,
            }
            kwargs.update(overrides)
            with pytest.raises(expected_error):
                await service.create_plan_draft(**kwargs)
            assert await _draft_count(db) == count, reason
        # 失败后同一身份仍可正常创建（无残留半状态）
        view = await service.create_plan_draft(
            draft_id="d-bad",
            preparation=preparation,
            conversation_id="c1",
            run_id=None,
            payload=payload,
            starts_on=STARTS_ON,
            review_on=REVIEW_ON,
            business_date=BUSINESS_DATE,
        )
        assert view.draft.id == "d-bad"
        assert await _draft_count(db) == count + 1


async def test_creation_does_not_mutate_caller_passed_objects(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c1")
        await _commit_profile(db, _profile())
        service = PlanDraftService(db)
        preparation = await service.prepare_generation_input()
        payload = await _payload(db, _profile())
        payload_before = replace(payload)
        patch = ProfilePatch(facts={"body_weight_kg": Fact.known(71.0)})
        patch_before = ProfilePatch(
            facts=dict(patch.facts),
            add_restrictions=patch.add_restrictions,
            remove_restrictions=patch.remove_restrictions,
        )

        view = await service.create_plan_draft(
            draft_id="d1",
            preparation=preparation,
            conversation_id="c1",
            run_id=None,
            payload=payload,
            starts_on=STARTS_ON,
            review_on=REVIEW_ON,
            business_date=BUSINESS_DATE,
            patch=patch,
        )

        assert payload == payload_before
        assert patch == patch_before
        assert view.proposal.payload == payload_before


# ---------- 提议信封编解码 ----------


def test_proposal_codec_round_trips_and_rejects_corruption() -> None:
    proposal = PlanProposal(
        starts_on=STARTS_ON,
        review_on=REVIEW_ON,
        payload=_payload_stub(),
        source_plan_version_id="pv-1",
        cancellations=(
            ProposedSessionCancellation(
                scheduled_session_id="ss-1",
                plan_version_id="pv-1",
                plan_workout_key="push",
                scheduled_on=date(2026, 9, 21),
            ),
        ),
    )
    assert proposal_from_json(proposal_to_json(proposal)) == proposal

    for corrupt in (
        "not json",
        "[]",
        '{"schema_version": 2, "starts_on": "2026-09-14", "review_on": "2026-10-12",'
        ' "mode": "regular", "payload": {}}',
        '{"schema_version": 1, "starts_on": "2026-09-14", "review_on": "2026-10-12",'
        ' "mode": "unknown", "payload": {}}',
        '{"schema_version": 1, "starts_on": "2026-09-14", "review_on": "2026-10-12",'
        ' "mode": "regular", "payload": {}, "unexpected": 1}',
    ):
        with pytest.raises(InvalidPlanRow):
            proposal_from_json(corrupt)


def _payload_stub() -> PlanPayload:
    """最小合法 payload（借领域单测的构造口径；不读库、不生成正式事实）。"""
    return PlanPayload(
        plan_workouts=(
            PlanWorkout(
                workout_key="push",
                name="推日",
                estimated_minutes=20,
                exercises=(
                    PlanExerciseItem(
                        item_key="push-01",
                        exercise_id="parallel-bar-dip",
                        display_snapshot=DisplaySnapshot(
                            name="双杠臂屈伸",
                            equipment_variant="bodyweight",
                            load_convention=None,
                        ),
                        record_type="bodyweight_reps",
                        prescription=RepsPrescription(
                            work_sets=3, reps_range=IntRange(8, 12)
                        ),
                        load=None,
                        progression=Progression(
                            method="repetition_progression", rule="先加次数"
                        ),
                    ),
                ),
            ),
        ),
        calendar_cycle=CalendarCycle(
            anchor_date=ANCHOR_DATE,
            slots=(WorkoutCycleSlot("push"), RestCycleSlot()),
        ),
    )
