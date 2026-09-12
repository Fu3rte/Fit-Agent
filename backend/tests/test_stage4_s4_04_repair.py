"""S4-04 修补：目录主要肌群、四类处置快照、替换等价与长期调整路由的定向用例。

对照（均已拍口径）：`architecture/03` §3.1 补充（muscle 取上游 ``target``）、`architecture/04`
§4.3（四种处置、减载方案 1–3 与 50–70% 负荷带、当次安排按绑定训练日过期）、§4.5（长期调整
需用户明确选择、保留原复核节点与循环结构、不是原样续期）、§4.7（同等刺激替换：``modes`` ∩
主要肌群 ∩ 器械 ∩ 限制 ∩ 启用且可推荐，缺失值 fail-closed）。

边界：全程离线、只用 ``tmp_path`` 临时库；不调用真实 Provider、不写正式事实旁路。
"""

import json
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest
from pydantic_ai.messages import ToolReturnPart

from app.confirm import ConfirmService
from app.draft_repo import DraftRepo
from app.drafts import DraftService
from app.plan_drafts import PlanDraftService
from domain.actions.repo import ExerciseRepo
from domain.actions.schema import Exercise
from domain.plan.repo import PlanRepo
from domain.plan.rules import (
    InvalidArrangementTarget,
    InvalidPlanPayload,
    replacement_violations,
)
from domain.plan.schema import (
    IntRange,
    NeedsCalibration,
    PlanExerciseItem,
    RepsPrescription,
    VerifiedLoad,
)
from domain.profile.safety import message_red_flag_hits
from domain.profile.schema import ActionRestriction, Fact, profile_to_json
from domain.profile.service import ProfileService
from runtime.context import _FACTS_HEADER, read_business_facts
from runtime.run_service import RunService
from runtime.tools import BusinessTools, ToolIdentity
from storage.db import Database
from storage.run_repo import RunRepo
from tests.support import open_database
from tests.test_stage3_arrangement_confirm import (
    _create_arrangement,
    _profile_and_plan,
    _push_session,
)
from tests.test_stage3_plan_confirm import (
    _confirm_plan,
    _create_plan_draft,
    _formal_profile,
)
from tests.test_stage3_plan_drafts import REVIEW_ON, STARTS_ON, _payload, _profile
from tests.test_stage4_agent_wiring import (
    BUSINESS_DATE,
    CONVERSATION_ID,
    RED_FLAG_LABEL,
    _all_parts,
    _formal_profile_and_plan,
    _run_agent,
    _ScriptedModel,
)

# ---------- 目录：主要肌群来自既有数据集（上游 target 原样） ----------


async def test_seeded_primary_muscles_come_from_dataset_target(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = ExerciseRepo(db)
        squat = await repo.get_by_id("barbell-back-squat")
        split = await repo.get_by_id("bulgarian-split-squat")
        a_row = await repo.get_by_id("barbell-bent-over-row")
        # 上游 target 原样落库；深蹲族为 glutes、分腿蹲为 quads（保守不等价的来源事实）。
        assert squat is not None and squat.muscle == "glutes"
        assert split is not None and split.muscle == "quads"
        assert a_row is not None and a_row.muscle == "upper back"

        async def op(conn):
            async with conn.execute(
                "SELECT COUNT(*) FROM exercises WHERE muscle IS NOT NULL"
            ) as cursor:
                return int((await cursor.fetchone())[0])

        assert await db.under_lock(op) == 24  # 首批 24 项全部有值，无缺失


# ---------- 替换等价：已拍条件全数执行，缺失一律 fail-closed ----------


def _exercise(
    exercise_id: str, *, muscle: str | None, modes: tuple[str, ...]
) -> Exercise:
    return Exercise(
        id=exercise_id,
        standard_name_zh=exercise_id,
        equipment_variant="dumbbell",
        record_type="reps_weight",
        load_convention="dumbbell_per_hand",
        unilateral=False,
        recommendable=True,
        active=True,
        aliases=(),
        modes=modes,
        source_ref="test-fixture",
        attribution="test fixture",
        instructions_zh=None,
        muscle=muscle,
    )


def _item(*, exercise_id: str, replacement_id: str | None) -> PlanExerciseItem:
    return PlanExerciseItem(
        item_key="push-01",
        exercise_id=exercise_id,
        display_snapshot=__import__(
            "domain.plan.schema", fromlist=["DisplaySnapshot"]
        ).DisplaySnapshot(
            name="卧推", equipment_variant="barbell", load_convention=None
        ),
        record_type="external_load_reps",
        prescription=RepsPrescription(
            work_sets=4, reps_range=IntRange(min=6, max=10), target_rir=None
        ),
        load=VerifiedLoad(
            value=60.0, unit="kg", load_notation="barbell_includes_bar_total"
        ),
        progression=__import__(
            "domain.plan.schema", fromlist=["Progression"]
        ).Progression(method="double_progression", rule="每级最小增量"),
        disposition="equivalent_replace",
        replacement_exercise_id=replacement_id,
    )


def test_replacement_requires_modes_and_primary_muscle_intersection() -> None:
    original = _exercise("bench", muscle="pectorals", modes=("水平推",))
    same = _exercise("dumbbell-bench", muscle="pectorals", modes=("水平推",))
    other_muscle = _exercise("curl", muscle="biceps", modes=("水平推",))
    other_modes = _exercise("fly", muscle="pectorals", modes=("胸肌孤立",))
    catalog = {item.id: item for item in (original, same, other_muscle, other_modes)}

    assert (
        replacement_violations(
            _item(exercise_id="bench", replacement_id="dumbbell-bench"), catalog=catalog
        )
        == ()
    )
    assert "主要肌群不同" in "；".join(
        replacement_violations(
            _item(exercise_id="bench", replacement_id="curl"), catalog=catalog
        )
    )
    assert "动作模式无交集" in "；".join(
        replacement_violations(
            _item(exercise_id="bench", replacement_id="fly"), catalog=catalog
        )
    )


def test_replacement_fails_closed_when_muscle_or_catalog_is_missing() -> None:
    """缺失数据不得猜测：无肌群、无目录、身份读不到都算不等价。"""
    original = _exercise("bench", muscle="pectorals", modes=("水平推",))
    unknown_muscle = _exercise("mystery", muscle=None, modes=("水平推",))
    bench = _item(exercise_id="bench", replacement_id="mystery")
    assert "主要肌群未知" in "；".join(
        replacement_violations(
            bench, catalog={"bench": original, "mystery": unknown_muscle}
        )
    )
    assert "缺少动作目录" in "；".join(replacement_violations(bench, catalog=None))
    assert "替代动作身份在目录内读不到" in "；".join(
        replacement_violations(bench, catalog={"bench": original})
    )


def test_squat_family_is_not_equivalent_by_dataset_target() -> None:
    """已知保守后果（已接受、非缺陷）：上游 target 不同即不等价，不做医学修正。"""
    squat = _exercise("barbell-back-squat", muscle="glutes", modes=("深蹲",))
    split = _exercise("bulgarian-split-squat", muscle="quads", modes=("深蹲",))
    violations = replacement_violations(
        _item(exercise_id="barbell-back-squat", replacement_id="bulgarian-split-squat"),
        catalog={item.id: item for item in (squat, split)},
    )
    assert "主要肌群不同：glutes / quads" in "；".join(violations)


# ---------- 当次安排：按绑定训练日判过期（不套用长期调整口径） ----------


async def test_arrangement_cannot_be_confirmed_after_its_bound_training_day(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        plan = await _profile_and_plan(db)
        session = await _push_session(db, plan.id)
        await _create_arrangement(db, draft_id="arr-1", session_id=session.id)
        with pytest.raises(InvalidArrangementTarget, match="绑定的训练日已过"):
            await ConfirmService(db).confirm_arrangement_draft(
                draft_id="arr-1",
                seen_revision=1,
                business_date=session.scheduled_on + timedelta(days=1),
            )
        # 训练日当天仍可接受（锁定与接受分离的既有规则不变）。
        result = await ConfirmService(db).confirm_arrangement_draft(
            draft_id="arr-1",
            seen_revision=1,
            business_date=session.scheduled_on,
        )
        assert result.committed_revision == 1


# ---------- 长期调整路由：明确同意才有草稿，原样续期不落库 ----------


async def _pending_plan_drafts(db: Database) -> tuple[str, ...]:
    """本会话待确认的计划草稿身份（既有已提交草稿不算新落库）。"""
    drafts = await DraftRepo(db).list_for_conversation(CONVERSATION_ID)
    return tuple(
        draft.id
        for draft in drafts
        if draft.kind == "plan" and draft.status == "pending"
    )


async def test_plan_adjustment_needs_explicit_user_choice_and_real_change(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile_and_plan(db)
        tools = BusinessTools(
            db,
            ToolIdentity(
                conversation_id=CONVERSATION_ID,
                run_id="r1",
                business_date=BUSINESS_DATE,
            ),
        )
        start = (BUSINESS_DATE + timedelta(days=1)).isoformat()
        review = (BUSINESS_DATE + timedelta(days=22)).isoformat()

        # 未获用户明确要求调整长期计划：只追问，不落库。
        asked = await tools.propose_plan_draft(
            starts_on=start, review_on=review, long_term_adjustment=False
        )
        assert asked["created"] is False and asked["needs_user_input"] is True
        assert await _pending_plan_drafts(db) == ()

        # 明确同意但内容与基线完全相同（且无档案补丁）：不是调整，不落库。
        no_change = await tools.propose_plan_draft(
            starts_on=start, review_on=review, long_term_adjustment=True
        )
        assert no_change["created"] is False and no_change["needs_user_input"] is True
        assert await _pending_plan_drafts(db) == ()


# ---------- P1-1：长期调整以当前 payload 为基线（只改受影响条目） ----------


def _workout_of(workouts, workout_key: str):
    for workout in workouts:
        if workout.workout_key == workout_key:
            return workout
    raise AssertionError(f"没有训练日 {workout_key}")


def _item_of(workout, item_key: str):
    for item in workout.exercises:
        if item.item_key == item_key:
            return item
    raise AssertionError(f"训练日 {workout.workout_key} 里没有 {item_key}")


def _diff_value(diff, field: str, attribute: str):
    for entry in diff:
        if entry.field == field:
            return getattr(entry, attribute)
    raise AssertionError(f"计划 Diff 缺少字段 {field}")


async def _current_plan_payload(db: Database):
    current = (await PlanDraftService(db).prepare_generation_input()).current_plan
    assert current is not None
    return current


def _tool(db: Database, *, business_date: date = BUSINESS_DATE) -> BusinessTools:
    return BusinessTools(
        db,
        ToolIdentity(
            conversation_id=CONVERSATION_ID, run_id="r1", business_date=business_date
        ),
    )


async def _submit_tool_run(db: Database) -> None:
    """工具成功创建草稿时来源 Run 必须真实存在；测试只提交请求，不执行 Run。"""
    await RunService(RunRepo(db)).submit_request(
        conversation_id=CONVERSATION_ID,
        run_id="r1",
        client_request_id="req-r1",
        text="工具来源",
    )


async def test_long_term_revision_reuses_baseline_and_bumps_version_once(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile_and_plan(db)
        await _submit_tool_run(db)
        current = await _current_plan_payload(db)
        tools = _tool(db)
        result = await tools.propose_plan_draft(
            starts_on=(BUSINESS_DATE + timedelta(days=1)).isoformat(),
            review_on=current.review_on.isoformat(),
            long_term_adjustment=True,
            adjustments=[
                {"item_key": "pull-01", "disposition": "deload", "work_sets": 2}
            ],
        )
        assert result["created"] is True, result
        view = await PlanDraftService(db).get_plan_draft(result["draft_id"])
        assert view is not None
        assert view.profile_diff is None  # 独立调整：没有档案补丁
        before = _diff_value(view.plan_diff, "workouts", "before")
        after = _diff_value(view.plan_diff, "workouts", "after")
        assert [workout.workout_key for workout in after] == [
            workout.workout_key for workout in before
        ]
        revised_pull = _workout_of(after, "pull")
        original_pull = _workout_of(before, "pull")
        assert revised_pull != original_pull  # 训练日内部确有真实改动
        revised_item = _item_of(revised_pull, "pull-01")
        assert revised_item.prescription.work_sets == 2
        assert revised_item.disposition == "deload"
        assert _item_of(original_pull, "pull-01").prescription.work_sets == 3
        # 未受影响条目与未受影响训练日逐字保留（不是从模板重生成）。
        assert tuple(
            item for item in revised_pull.exercises if item.item_key != "pull-01"
        ) == tuple(
            item for item in original_pull.exercises if item.item_key != "pull-01"
        )
        for workout_key in ("push", "legs"):
            assert _workout_of(before, workout_key) == _workout_of(after, workout_key)

        version_before = (
            await ProfileService(db).read_formal_profile()
        ).context_version
        commit = await ConfirmService(db).confirm_plan_draft(
            draft_id=result["draft_id"], seen_revision=1, business_date=BUSINESS_DATE
        )
        version_after = (await ProfileService(db).read_formal_profile()).context_version
        assert version_after == version_before + 1  # 一次确认只推进一次
        confirmed = await PlanRepo(db).read_version(commit.plan_version_id)
        assert confirmed is not None
        assert confirmed.payload == view.proposal.payload
        assert (
            _item_of(
                _workout_of(confirmed.payload.plan_workouts, "pull"), "pull-01"
            ).prescription.work_sets
            == 2
        )


async def test_combined_draft_patch_cannot_excuse_identical_plan(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile_and_plan(db)
        await _submit_tool_run(db)
        current = await _current_plan_payload(db)
        profile = (await ProfileService(db).read_formal_profile()).profile
        assert profile is not None
        proposed = json.loads(
            profile_to_json(replace(profile, session_duration_minutes=Fact.known(90)))
        )
        tools = _tool(db)
        start = (BUSINESS_DATE + timedelta(days=1)).isoformat()
        review = current.review_on.isoformat()

        # 只有档案补丁、payload 与基线相同：不是长期调整，不落库。
        patch_only = await tools.propose_plan_draft(
            starts_on=start,
            review_on=review,
            long_term_adjustment=True,
            proposed_profile=proposed,
        )
        assert patch_only["created"] is False and patch_only["needs_user_input"] is True
        assert await _pending_plan_drafts(db) == ()

        # 受限组合：档案补丁 + 真实计划改动 → 一条草稿、两项 Diff。
        combined = await tools.propose_plan_draft(
            starts_on=start,
            review_on=review,
            long_term_adjustment=True,
            proposed_profile=proposed,
            adjustments=[
                {"item_key": "push-01", "disposition": "deload", "work_sets": 2}
            ],
        )
        assert combined["created"] is True, combined
        view = await PlanDraftService(db).get_plan_draft(combined["draft_id"])
        assert view is not None
        assert view.profile_diff and any(field.changed for field in view.profile_diff)
        assert "workouts" in {field.field for field in view.plan_diff if field.changed}

        version_before = (
            await ProfileService(db).read_formal_profile()
        ).context_version
        commit = await ConfirmService(db).confirm_plan_draft(
            draft_id=combined["draft_id"], seen_revision=1, business_date=BUSINESS_DATE
        )
        version_after = (await ProfileService(db).read_formal_profile()).context_version
        assert version_after == version_before + 1
        updated = (await ProfileService(db).read_formal_profile()).profile
        assert updated is not None and updated.session_duration_minutes.value == 90
        confirmed = await PlanRepo(db).read_version(commit.plan_version_id)
        assert confirmed is not None and confirmed.payload == view.proposal.payload
        assert (
            _item_of(
                _workout_of(confirmed.payload.plan_workouts, "push"), "push-01"
            ).prescription.work_sets
            == 2
        )


# ---------- P1-2：跨自重／外加负重替换（不等价门已删；负荷只降不猜） ----------


def test_replacement_allows_cross_record_type_when_modes_and_muscle_match() -> None:
    bodyweight = _exercise("pull-up", muscle="lats", modes=("垂直拉",))
    external = _exercise("lat-pulldown", muscle="lats", modes=("垂直拉",))
    item = replace(
        _item(exercise_id="pull-up", replacement_id="lat-pulldown"),
        record_type="bodyweight_reps",
        load=None,
    )
    assert (
        replacement_violations(
            item, catalog={"pull-up": bodyweight, "lat-pulldown": external}
        )
        == ()
    )


async def test_bodyweight_to_external_replacement_becomes_uncalibrated(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile_and_plan(db)
        await _submit_tool_run(db)
        current = await _current_plan_payload(db)
        tools = _tool(db)
        result = await tools.propose_plan_draft(
            starts_on=(BUSINESS_DATE + timedelta(days=1)).isoformat(),
            review_on=current.review_on.isoformat(),
            long_term_adjustment=True,
            adjustments=[
                {
                    "item_key": "pull-01",
                    "disposition": "equivalent_replace",
                    "replacement_exercise_id": "lat-pulldown",
                }
            ],
        )
        assert result["created"] is True, result
        view = await PlanDraftService(db).get_plan_draft(result["draft_id"])
        assert view is not None
        revised = _item_of(
            _workout_of(view.proposal.payload.plan_workouts, "pull"), "pull-01"
        )
        # 计划不再包含原动作：身份换成替代动作，口径映射到三类的外加负重型。
        assert revised.exercise_id == "lat-pulldown"
        assert revised.record_type == "external_load_reps"
        # 替代动作承载不了计划的负荷：转未校准，绝不猜重量。
        assert isinstance(revised.load, NeedsCalibration)
        assert revised.prescription.work_sets == 3

        commit = await ConfirmService(db).confirm_plan_draft(
            draft_id=result["draft_id"], seen_revision=1, business_date=BUSINESS_DATE
        )
        confirmed = await PlanRepo(db).read_version(commit.plan_version_id)
        assert confirmed is not None and confirmed.payload == view.proposal.payload


# ---------- 四种处置与减载边界：经工具走创建 + 确认 ----------


async def test_arrangement_tool_dispositions_and_deload_bounds(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        plan = await _profile_and_plan(db)
        session = await _push_session(db, plan.id)
        await _submit_tool_run(db)
        tools = _tool(db, business_date=session.scheduled_on)

        rejected = (
            (
                {"item_key": "push-01", "disposition": "deload", "work_sets": 5},
                "只能减组",
            ),
            (
                {"item_key": "push-01", "disposition": "deload", "load_value": 50.0},
                "没有已验证重量",
            ),
            (
                {
                    "item_key": "push-01",
                    "disposition": "deload",
                    "target_rir": {"min": 1, "max": 2},
                },
                "目标用力",
            ),
        )
        for adjustment, match in rejected:
            view = await tools.propose_arrangement_draft(
                scheduled_session_id=session.id,
                adjustments=[adjustment],
                adjustment_reason="边界探测",
            )
            assert view["created"] is False and view["needs_user_input"] is True
            assert match in view["reason"], view["reason"]

        created = await tools.propose_arrangement_draft(
            scheduled_session_id=session.id,
            adjustments=[
                {"item_key": "push-01", "disposition": "deload", "work_sets": 2},
                {"item_key": "push-02", "disposition": "keep"},
                {"item_key": "push-03", "disposition": "local_skip"},
                {
                    "item_key": "push-04",
                    "disposition": "equivalent_replace",
                    "replacement_exercise_id": "cable-overhead-triceps-extension",
                },
            ],
            adjustment_reason="当日状态不佳，按处置调整",
        )
        assert created["created"] is True, created
        commit = await ConfirmService(db).confirm_arrangement_draft(
            draft_id=created["draft_id"],
            seen_revision=1,
            business_date=session.scheduled_on,
        )
        revision = await PlanRepo(db).read_arrangement_revision(
            commit.arrangement_revision_id
        )
        assert revision is not None
        target_items = {item.item_key: item for item in revision.target.exercises}
        assert target_items["push-01"].disposition == "deload"
        assert target_items["push-01"].prescription.work_sets == 2
        assert target_items["push-02"].disposition == "keep"
        assert target_items["push-03"].disposition == "local_skip"
        assert target_items["push-04"].disposition == "equivalent_replace"
        assert (
            target_items["push-04"].replacement_exercise_id
            == "cable-overhead-triceps-extension"
        )


# ---------- P1-3：迟到确认不补确认日前的日程，拒绝整窗已过 ----------


async def test_confirmation_on_start_day_keeps_full_window(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile(db)
        await _create_plan_draft(db, draft_id="plan-on-time")
        result = await _confirm_plan(
            db, draft_id="plan-on-time", business_date=STARTS_ON
        )
        sessions = await PlanRepo(db).list_sessions(result.plan_version_id)
        assert sessions and sessions[0].scheduled_on == STARTS_ON


async def test_late_confirmation_projects_only_from_confirmation_day(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile(db)
        await _create_plan_draft(db, draft_id="plan-late")
        late = STARTS_ON + timedelta(days=3)
        result = await _confirm_plan(db, draft_id="plan-late", business_date=late)
        sessions = await PlanRepo(db).list_sessions(result.plan_version_id)
        assert sessions
        # 确认日前的日子不落日程（因此不进入完成率分母），确认日当天保留。
        assert all(item.scheduled_on >= late for item in sessions)
        version = await PlanRepo(db).read_version(result.plan_version_id)
        assert version is not None
        # 行字段（含 starts_on/review_on 元数据）不变，不向确认日顺延也不延长复核节点。
        assert version.starts_on == STARTS_ON
        assert version.review_on == REVIEW_ON


async def test_late_confirmation_refuses_passed_window_and_keeps_locked_history(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app-past.db") as db:
        # 创建点：整窗已过不接受草稿。
        await _formal_profile(db)
        with pytest.raises(InvalidPlanPayload, match="已全部过去"):
            await _create_plan_draft(db, draft_id="plan-past", business_date=REVIEW_ON)

    async with open_database(tmp_path / "app.db") as db:
        plan_a = await _formal_profile_and_plan(db)
        late = STARTS_ON + timedelta(days=3)
        await _create_plan_draft(db, draft_id="plan-b", business_date=STARTS_ON)
        sessions_a = await PlanRepo(db).list_sessions(plan_a)
        future = [item for item in sessions_a if item.scheduled_on > late]
        past = [item for item in sessions_a if item.scheduled_on <= late]
        assert future and past
        locked_future, cancellable = future[0], future[1:]
        # 篡改：把一条未来日程标记为存储锁定，模拟已核实的既有锁定。
        async with db.transaction() as conn:
            await conn.execute(
                "UPDATE scheduled_sessions SET locked_at = '2026-09-15T00:00:00+00:00'"
                " WHERE id = ?",
                (locked_future.id,),
            )

        # 确认点：整窗已过拒绝，且拒绝时旧日程不被取消（事务回滚）。
        with pytest.raises(InvalidPlanPayload, match="已全部过去"):
            await _confirm_plan(db, draft_id="plan-b", business_date=REVIEW_ON)
        after_refusal = {
            item.id: item for item in await PlanRepo(db).list_sessions(plan_a)
        }
        assert all(item.cancelled_at is None for item in after_refusal.values())

        commit = await _confirm_plan(db, draft_id="plan-b", business_date=late)
        after_commit = {
            item.id: item for item in await PlanRepo(db).list_sessions(plan_a)
        }
        assert after_commit[locked_future.id].cancelled_at is None  # 锁定历史不改写
        assert all(
            after_commit[item.id].cancelled_at is None for item in past
        )  # 已过去／已执行历史保留
        assert all(
            after_commit[item.id].cancelled_at is not None for item in cancellable
        )  # 未来未锁定才取消
        sessions_b = await PlanRepo(db).list_sessions(commit.plan_version_id)
        assert sessions_b and all(item.scheduled_on >= late for item in sessions_b)


# ---------- 安全投影：待确认档案草稿的红旗一并阻断 ----------


async def test_pending_profile_draft_red_flag_blocks_guidance(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile_and_plan(db)
        drafts = DraftService(db)
        baseline = await drafts.prepare_generation_baseline()
        await drafts.create_profile_draft(
            draft_id="pending-red-flag",
            generation_baseline=baseline,
            conversation_id=CONVERSATION_ID,
            run_id=None,
            proposed=_profile(body_conditions=Fact.known((RED_FLAG_LABEL,))),
        )
        guidance = await _tool(db).read_plan_guidance()
        # 未确认草稿不是正式事实，但红旗已 fail-closed 阻断可执行处方。
        assert guidance["safety"]["usable"] is False
        assert guidance["safety"]["red_flag_blocked"] is True
        assert guidance["plan"]["payload"] is None
        facts = await read_business_facts(
            db, conversation_id=CONVERSATION_ID, business_date=BUSINESS_DATE
        )
        assert facts.guidance is not None and facts.guidance.safety.is_blocked
        # 草稿未确认：正式档案与业务版本保持不变。
        snapshot = await ProfileService(db).read_formal_profile()
        assert snapshot.context_version == facts.snapshot.context_version


async def test_plan_safety_is_checked_at_creation_and_again_at_confirmation(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        # 创建点：正式档案带红旗 → 不给处方、不落库。
        await _formal_profile(
            db, _profile(body_conditions=Fact.known((RED_FLAG_LABEL,)))
        )
        blocked = await _tool(db).propose_plan_draft(
            starts_on=STARTS_ON.isoformat(), review_on=REVIEW_ON.isoformat()
        )
        assert blocked["created"] is False
        assert blocked["block"]["code"] == "red_flag"
        assert await _pending_plan_drafts(db) == ()

    async with open_database(tmp_path / "app-confirm.db") as db:
        # 确认点：篡改存储草稿（合法创建后引入命中最新限制的动作）→ 复查拒绝。
        await _formal_profile(
            db,
            _profile(
                action_restrictions=Fact.known(
                    (ActionRestriction(scope="movement_pattern", target="肘伸"),)
                )
            ),
        )
        service = PlanDraftService(db)
        preparation = await service.prepare_generation_input()
        restricted_profile = preparation.snapshot.profile
        assert restricted_profile is not None
        payload = await _payload(db, restricted_profile)
        await service.create_plan_draft(
            draft_id="plan-confirm-check",
            preparation=preparation,
            conversation_id=CONVERSATION_ID,
            run_id=None,
            payload=payload,
            starts_on=STARTS_ON,
            review_on=REVIEW_ON,
            business_date=STARTS_ON,
        )
        draft = await DraftRepo(db).get("plan-confirm-check")
        assert draft is not None and draft.proposed_plan_json is not None
        proposal = json.loads(draft.proposed_plan_json)
        pull_workout = next(
            workout
            for workout in proposal["payload"]["plan_workouts"]
            if workout["workout_key"] == "pull"
        )
        # 引体向上 → 双杠臂屈伸：同 record_type，但命中「肘伸」限制。
        pull_workout["exercises"][0]["exercise_id"] = "parallel-bar-dip"
        async with db.transaction() as conn:
            await conn.execute(
                "UPDATE business_drafts SET proposed_plan_json = ?"
                " WHERE id = 'plan-confirm-check'",
                (json.dumps(proposal, ensure_ascii=False),),
            )
        with pytest.raises(InvalidPlanPayload, match="命中限制"):
            await _confirm_plan(
                db, draft_id="plan-confirm-check", business_date=STARTS_ON
            )


# ---------- 路由正本：系统事实头部写明已拍路由 ----------


def test_facts_header_states_long_term_routing_rule() -> None:
    assert "受限组合计划草稿" in _FACTS_HEADER
    assert "当次安排不是长期调整" in _FACTS_HEADER
    assert "点击确认" in _FACTS_HEADER
    assert "待确认档案草稿" in _FACTS_HEADER


# ---------- C 层文本兜底：当前消息命中即本 Run 不给处方（B 未实现） ----------


async def test_message_red_flag_blocks_guidance_without_any_draft(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile_and_plan(db)
        message = "我这两天出现锐痛，昨晚还有点晕厥"
        hits = message_red_flag_hits(message)
        assert hits == ("晕厥", "锐痛")  # 按词表顺序

        facts = await read_business_facts(
            db,
            conversation_id=CONVERSATION_ID,
            business_date=BUSINESS_DATE,
            message_red_flags=hits,
        )
        assert facts.guidance is not None
        assert facts.guidance.safety.is_blocked is True
        assert facts.guidance.safety.red_flags.is_blocked is True
        assert any(
            "锐痛" in reason for reason in facts.guidance.safety.blocking_reasons
        )

        tools = BusinessTools(
            db,
            ToolIdentity(
                conversation_id=CONVERSATION_ID,
                run_id="r1",
                business_date=BUSINESS_DATE,
                message_red_flags=hits,
            ),
        )
        guidance = await tools.read_plan_guidance()
        assert guidance["safety"]["usable"] is False
        assert guidance["safety"]["red_flag_blocked"] is True
        assert guidance["plan"]["payload"] is None

        # 正式档案与业务版本不被消息命中改写（投影只读、不落库）。
        snapshot = await ProfileService(db).read_formal_profile()
        assert snapshot.context_version == facts.snapshot.context_version


def test_message_red_flag_negatives_do_not_match() -> None:
    for text in (
        "昨天练完只是普通肌肉酸痛，今天好多了",
        "肌肉酸痛算正常吧",
        "腿有点酸痛但没别的不舒服",
        "膝关节有异响，但无痛且无功能异常，需要停训吗",
    ):
        assert message_red_flag_hits(text) == (), text


async def test_benign_message_keeps_guidance_usable(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile_and_plan(db)
        facts = await read_business_facts(
            db,
            conversation_id=CONVERSATION_ID,
            business_date=BUSINESS_DATE,
            message_red_flags=message_red_flag_hits("昨天练完只是普通肌肉酸痛"),
        )
        assert facts.guidance is not None and facts.guidance.safety.is_blocked is False


async def test_run_level_message_red_flag_forces_guidance_unusable(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile_and_plan(db)
        model = _ScriptedModel([("read_plan_guidance", {}), None])
        await _run_agent(
            db, model.model(), run_id="r1", user_text="我这两天有锐痛，想继续练"
        )
        returns = [
            part
            for part in _all_parts(model.seen[-1])
            if isinstance(part, ToolReturnPart)
        ]
        assert returns, "run 级接线必须把 read_plan_guidance 结果回灌"
        guidance = json.loads(json.dumps(returns[0].content))
        assert guidance["safety"]["usable"] is False
        assert guidance["safety"]["red_flag_blocked"] is True


async def test_message_red_flag_stacks_with_pending_draft_projection(
    tmp_path: Path,
) -> None:
    """C 层消息命中与待确认草稿投影叠加：两者任一生效即本 Run 不给可执行处方。"""
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile_and_plan(db)
        drafts = DraftService(db)
        baseline = await drafts.prepare_generation_baseline()
        # 待确认草稿本身无红旗（明确无），本 Run 的阻断必须来自消息层。
        await drafts.create_profile_draft(
            draft_id="pending-benign",
            generation_baseline=baseline,
            conversation_id=CONVERSATION_ID,
            run_id=None,
            proposed=_profile(body_conditions=Fact.denied()),
        )
        hits = message_red_flag_hits("今天训练后膝盖明显肿胀")
        assert hits == ("明显肿胀",)
        facts = await read_business_facts(
            db,
            conversation_id=CONVERSATION_ID,
            business_date=BUSINESS_DATE,
            message_red_flags=hits,
        )
        assert facts.guidance is not None and facts.guidance.safety.is_blocked
        tools = BusinessTools(
            db,
            ToolIdentity(
                conversation_id=CONVERSATION_ID,
                run_id="r1",
                business_date=BUSINESS_DATE,
                message_red_flags=hits,
            ),
        )
        guidance = await tools.read_plan_guidance()
        assert guidance["safety"]["usable"] is False
        assert guidance["plan"]["payload"] is None


async def test_message_red_flag_blocks_first_time_plan_draft(tmp_path: Path) -> None:
    """消息命中即本 Run 不给处方：首次建档路径同样不落计划草稿（2026-09-12 拍板）。"""
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile(db)
        hits = message_red_flag_hits("膝盖明显肿胀，走路都费劲")
        assert hits == ("明显肿胀",)
        tools = BusinessTools(
            db,
            ToolIdentity(
                conversation_id=CONVERSATION_ID,
                run_id="r1",
                business_date=BUSINESS_DATE,
                message_red_flags=hits,
            ),
        )
        result = await tools.propose_plan_draft(
            starts_on=STARTS_ON.isoformat(), review_on=REVIEW_ON.isoformat()
        )
        assert result["created"] is False and result["needs_user_input"] is True
        assert result["block"]["code"] == "red_flag"
        assert result["block"]["red_flags"] == list(hits)
        assert "明显肿胀" in result["block"]["reason"]
        assert await _pending_plan_drafts(db) == ()


async def test_message_red_flag_blocks_long_term_and_combined_plan_drafts(
    tmp_path: Path,
) -> None:
    """长期修订与受限组合（档案补丁 + 计划改动）两条路径都在落库前被消息兜底拦住。"""
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile_and_plan(db)
        hits = message_red_flag_hits("我这两天出现锐痛")
        assert hits == ("锐痛",)
        tools = BusinessTools(
            db,
            ToolIdentity(
                conversation_id=CONVERSATION_ID,
                run_id="r1",
                business_date=BUSINESS_DATE,
                message_red_flags=hits,
            ),
        )
        current = await _current_plan_payload(db)
        profile = (await ProfileService(db).read_formal_profile()).profile
        assert profile is not None
        proposed = json.loads(
            profile_to_json(replace(profile, session_duration_minutes=Fact.known(90)))
        )
        start = (BUSINESS_DATE + timedelta(days=1)).isoformat()
        review = current.review_on.isoformat()
        long_term = await tools.propose_plan_draft(
            starts_on=start,
            review_on=review,
            long_term_adjustment=True,
            adjustments=[
                {"item_key": "pull-01", "disposition": "deload", "work_sets": 2}
            ],
        )
        combined = await tools.propose_plan_draft(
            starts_on=start,
            review_on=review,
            long_term_adjustment=True,
            proposed_profile=proposed,
            adjustments=[
                {"item_key": "push-01", "disposition": "deload", "work_sets": 2}
            ],
        )
        for result in (long_term, combined):
            assert result["created"] is False and result["needs_user_input"] is True
            assert result["block"]["code"] == "red_flag"
            assert result["block"]["red_flags"] == list(hits)
            assert "锐痛" in result["block"]["reason"]
        assert await _pending_plan_drafts(db) == ()


async def test_message_red_flag_blocks_arrangement_draft(tmp_path: Path) -> None:
    """当次安排同样是处方：消息命中即不落安排草稿。"""
    async with open_database(tmp_path / "app.db") as db:
        plan = await _profile_and_plan(db)
        session = await _push_session(db, plan.id)
        hits = message_red_flag_hits("落地时膝关节失稳")
        assert hits == ("关节失稳",)
        tools = BusinessTools(
            db,
            ToolIdentity(
                conversation_id=CONVERSATION_ID,
                run_id="r1",
                business_date=session.scheduled_on,
                message_red_flags=hits,
            ),
        )
        result = await tools.propose_arrangement_draft(
            scheduled_session_id=session.id,
            adjustments=[
                {"item_key": "push-01", "disposition": "deload", "work_sets": 2}
            ],
            adjustment_reason="当日状态不佳",
        )
        assert result["created"] is False and result["needs_user_input"] is True
        assert result["block"]["code"] == "red_flag"
        assert result["block"]["red_flags"] == list(hits)
        drafts = await DraftRepo(db).list_for_conversation(CONVERSATION_ID)
        assert [draft for draft in drafts if draft.kind == "arrangement"] == []


async def test_benign_message_keeps_draft_tools_creating(tmp_path: Path) -> None:
    """非命中消息不改变创建行为：计划与安排草稿都照常落库（无回归）。"""
    async with open_database(tmp_path / "app.db") as db:
        plan = await _profile_and_plan(db)
        session = await _push_session(db, plan.id)
        await _submit_tool_run(db)
        hits = message_red_flag_hits("昨天练完只是普通肌肉酸痛，今天好多了")
        assert hits == ()
        tools = BusinessTools(
            db,
            ToolIdentity(
                conversation_id=CONVERSATION_ID,
                run_id="r1",
                business_date=session.scheduled_on,
                message_red_flags=hits,
            ),
        )
        current = await _current_plan_payload(db)
        plan_result = await tools.propose_plan_draft(
            starts_on=(session.scheduled_on + timedelta(days=1)).isoformat(),
            review_on=current.review_on.isoformat(),
            long_term_adjustment=True,
            adjustments=[
                {"item_key": "pull-01", "disposition": "deload", "work_sets": 2}
            ],
        )
        assert plan_result["created"] is True, plan_result
        arrangement_result = await tools.propose_arrangement_draft(
            scheduled_session_id=session.id,
            adjustments=[
                {"item_key": "push-01", "disposition": "deload", "work_sets": 2}
            ],
            adjustment_reason="当日状态不佳",
        )
        assert arrangement_result["created"] is True, arrangement_result
        assert plan_result["draft_id"] in await _pending_plan_drafts(db)
