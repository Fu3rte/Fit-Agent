"""Stage 5：§2.1 激活事务与 §2.2 幂等矩阵的代码前断言与激活／拒绝事务的领域行为。

依据：``refactor-log/stage5.md`` §2.1／§2.2／§2.4／§4.2／§4.3／§5.1／§7；
``LANGGRAPH_REFACTOR_PLAN.md`` §9.3／§5.6；``Fit-Agent-LangGraph-重构讨论总结.md`` §7.3／§9。

上半部分只冻结契约：激活事务的七步顺序、日期新鲜度、日程取消口径、全或无回滚与四态
幂等矩阵都在这里定死，生产或文档任一漂移即失败。已合入源码侧的交叉断言只读 Stage 5 明令不得改动的
事实：计划四态词表（``domain/plans/schema.py::PLAN_STATUSES``）、``plans``／``plan_sessions`` 的既有
时间与日程字段、确认状态与终止原因的既有词汇表（``graph/state.py``）。

下半部分用 ``tmp_path`` 下的真实迁移库驱动 ``domain.plans.service.PlanActivationService``
与 ``domain.plans.rules.validate_plan_adjustment``：首次激活（归档旧 active、取消未到期日程、建立新
日程）、无旧 active、日期不新鲜、再校验失败、事务中途失败回滚、重复确认、archived／rejected 冲突、
调整来源计划不匹配、取消与新鲜度的业务日边界、画像缺少频率，以及生成规则与调整渐进规则的分工。
业务事实只用直接 SQL 预置（计划版本行、日程、训练与组），写路径全部经被测服务。

全部断言不调模型、不需要任何 ``MODEL_*`` 环境变量（契约断言也不读库）。
"""

import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import fields
from datetime import date
from pathlib import Path
from typing import get_args

import pytest

from domain.actions.schema import Exercise, RecordType
from domain.plans.rules import (
    ProgressionDecision,
    resolve_progression,
    validate_plan_adjustment,
    validate_plan_draft,
)
from domain.plans.schema import (
    PLAN_STATUSES,
    DeterministicResult,
    EvaluationResult,
    Plan,
    PlanDraft,
    PlanSession,
    RubricResult,
    RubricVerdict,
)
from domain.plans.service import (
    PlanActivationConflict,
    PlanActivationService,
    PlanDraftStale,
    PlanNotFound,
    PlanPersistenceService,
    PlanRevalidationFailed,
)
from domain.profile.schema import Fact, Profile
from domain.profile.service import ProfileService
from domain.stats.schema import ValidWorkSet
from graph.state import ConfirmationStatus, TerminationReason
from storage.db import Database
from tests.conftest import Stage5Plan

#: §2.1 激活事务的七步：顺序固定，任一步失败整体回滚。
ACTIVATION_STEPS = (
    "1. 读取 `plan_id`，要求仍为 `draft`；",
    "2. 解析 `PlanDraft`，若 `starts_on < business_day` 则拒绝；",
    "3. 再跑确定性校验；调整 draft 还要求 `source_plan_id == 当前 active.id` 并按 active 关联训练事实校验 progression；",
    "4. 当前 active → `archived`，写 `archived_at`；",
    "5. 取消旧 active 中 `scheduled_on >= business_day` 的 `plan_sessions`；",
    "6. draft → `active`，写 `confirmed_at`，按 `training_days` 创建新 sessions；",
    "7. 提交。",
)

#: §2.1 收尾：全或无、模型不在事务内、日期或再校验失败保持 draft 可确认。
ACTIVATION_ROLLBACK_RULE = (
    "任一步失败整体回滚。模型调用不进入事务。日期新鲜度或再校验失败时 draft 保持可确认，原 active 不变。"
)

#: §2.2 幂等矩阵整表（状态 token，首列原文，confirm，reject）；状态集合即计划四态。
IDEMPOTENCY_MATRIX = (
    ("draft", "`draft`", "执行激活事务", "`draft → archived`，写 `archived_at`"),
    ("active", "同一 id 已 `active`", "返回既有结果", "冲突，不写"),
    ("archived", "`archived`", "拒绝重新激活", "返回既有结果"),
    ("rejected", "`rejected`", "拒绝重新激活", "冲突"),
)

#: §2.2 收尾：``rejected`` 只表示 Evaluator 二次阻断失败，用户拒绝永不写该状态。
ACTIVATION_REJECTION_RULE = "`rejected` 仅表示 Evaluator 二次阻断失败；用户拒绝永不写 `rejected`。"

#: 激活事务的编号步骤行（用于「步骤集合未被增删或改写」的整段相等断言）。
_NUMBERED_STEP = re.compile(r"\d+\. ")


def test_activation_transaction_steps_are_frozen_and_ordered(stage5_plan: Stage5Plan) -> None:
    """§2.1 七步顺序冻结：先确认仍为 draft、再日期新鲜度、再确定性再校验、换代、取消日程、建新日程、提交。"""
    section = stage5_plan.section("2.1 激活事务")

    steps = tuple(line for line in section.splitlines() if _NUMBERED_STEP.match(line))
    assert steps == ACTIVATION_STEPS


def test_activation_rejects_a_stale_starts_on_and_keeps_the_draft(stage5_plan: Stage5Plan) -> None:
    """§2.1：``starts_on < business_day`` 即拒绝激活；日期新鲜度失败不归档、不改状态、draft 仍可确认。"""
    section = stage5_plan.section("2.1 激活事务")

    assert ACTIVATION_STEPS[1] in section
    assert "starts_on" in PlanDraft.model_fields
    assert "日期新鲜度或再校验失败时 draft 保持可确认，原 active 不变。" in section


def test_activation_cancels_sessions_scheduled_on_or_after_the_business_day(
    stage5_plan: Stage5Plan,
) -> None:
    """§2.1：只取消 ``scheduled_on >= business_day`` 的旧日程，历史日程保留。"""
    section = stage5_plan.section("2.1 激活事务")

    assert ACTIVATION_STEPS[4] in section
    assert tuple(field.name for field in fields(PlanSession)) == (
        "id",
        "plan_id",
        "scheduled_on",
        "cancelled_at",
    )


def test_activation_rolls_back_completely_and_keeps_the_draft_confirmable(
    stage5_plan: Stage5Plan,
) -> None:
    """§2.1：任一步失败整体回滚，模型不在事务内；只写既有 ``confirmed_at``／``archived_at``。

    两个部分唯一索引作为最后防线已由 ``tests/test_stage4_migration.py`` 与
    ``tests/test_stage1_data_base.py`` 断言（本用例只看日志明文与计划字段集合）。
    """
    section = stage5_plan.section("2.1 激活事务")

    assert ACTIVATION_ROLLBACK_RULE in section
    assert {"confirmed_at", "archived_at"} <= {field.name for field in fields(Plan)}


def test_idempotency_matrix_is_frozen_for_the_four_plan_states(stage5_plan: Stage5Plan) -> None:
    """§2.2 幂等矩阵整表冻结：draft 执行、同一 active 幂等成功、archived 重新激活被拒、rejected 被拒。

    行键恰好是计划四态（§2.2：Stage 5 不新增计划状态）。
    """
    assert PLAN_STATUSES == tuple(state for state, *_ in IDEMPOTENCY_MATRIX)
    assert stage5_plan.table("2.2 confirm / reject 幂等", "confirm") == tuple(
        row[1:] for row in IDEMPOTENCY_MATRIX
    )


def test_user_rejection_never_writes_the_rejected_plan_status(stage5_plan: Stage5Plan) -> None:
    """§2.2：用户拒绝走 ``archive_draft``（确认状态才是 ``rejected``），计划状态永不写 ``rejected``。"""
    section = stage5_plan.section("2.2 confirm / reject 幂等")

    assert ACTIVATION_REJECTION_RULE in section
    assert "archive_draft" in get_args(TerminationReason)
    assert "rejected" not in get_args(TerminationReason)
    assert set(get_args(ConfirmationStatus)) == {"pending", "confirmed", "rejected"}


# ==========================================================================================
# 激活／拒绝事务的领域行为（真实临时库，无模型、无 Graph、无 HTTP）
# ==========================================================================================

#: 本次注入的业务日与固定业务记录时间：服务不读系统时钟，日期与时间都由调用方注入（§2.1）。
BUSINESS_DAY = date(2026, 6, 1)
CREATED_AT = "2026-05-01T09:00:00+08:00"
OLD_CONFIRMED_AT = "2026-05-01T10:00:00+08:00"
CONFIRMED_AT = "2026-06-01T09:00:00+08:00"
ARCHIVED_AT = "2026-06-01T09:05:00+08:00"
#: 目录里的外加负重动作（最小加重 2.5kg）与纯自重动作；``NEW_ACTION`` 不在 current active 里。
WEIGHTED = "barbell-back-squat"
NEW_ACTION = "barbell-bench-press"
BODYWEIGHT = "pull-up"
SQUAT_INCREMENT_KG = 2.5
#: current active 的固定训练日（同一动作两天，目标处方取第一处）。
ACTIVE_DAYS = (date(2026, 5, 1), date(2026, 5, 4))
#: 激活成功时只允许变化的两个字段之外的原 active 字段：其余逐字段不变（§2.1 原 active 不变）。
_OLD_ACTIVE_KEPT_FIELDS = (
    "id",
    "version",
    "source_plan_id",
    "structured_content",
    "evaluator_result",
    "created_at",
    "confirmed_at",
)


# ---------- 固定计划载荷与领域对象（无 IO） ----------


def _known_load(
    weight_kg: float, *, session: int = 1, set_no: int = 3
) -> dict[str, object]:
    """一个具体负荷：渐进只比重量，来源字段写固定值（Schema 要求与重量同现）。"""
    return {
        "status": "known",
        "weight_kg": weight_kg,
        "source_workout_session_id": session,
        "source_set_no": set_no,
    }


def _needs_calibration() -> dict[str, object]:
    return {"status": "needs_calibration"}


def _weighted(
    load: dict[str, object],
    *,
    exercise_id: str = WEIGHTED,
    sets: int = 3,
    reps_min: int = 8,
    reps_max: int = 12,
) -> dict[str, object]:
    """外加负重次数处方的一个动作。"""
    return {
        "exercise_id": exercise_id,
        "sets": sets,
        "prescription": {
            "type": "weighted_reps",
            "reps_min": reps_min,
            "reps_max": reps_max,
            "progression_note": None,
            "load": load,
        },
    }


def _bodyweight(exercise_id: str = BODYWEIGHT) -> dict[str, object]:
    """纯自重次数处方的一个动作（不携带负荷，无需有效工作组历史）。"""
    return {
        "exercise_id": exercise_id,
        "sets": 3,
        "prescription": {
            "type": "bodyweight_reps",
            "reps_min": 8,
            "reps_max": 12,
            "progression_note": None,
        },
    }


def _content(
    *days: tuple[date, Sequence[dict[str, object]]], starts_on: date = BUSINESS_DAY
) -> dict[str, object]:
    """一个结构合法的计划内容：训练日数量恰等于每周训练次数（``PlanDraft`` 的 Schema 要求）。"""
    return {
        "goal": "增肌",
        "starts_on": starts_on.isoformat(),
        "explanation": "固定案例：按当前 active 与关联训练调整",
        "weekly_frequency": len(days),
        "training_days": [
            {"scheduled_on": day.isoformat(), "exercises": list(exercises)}
            for day, exercises in days
        ],
    }


def _active_squat_content(weight_kg: float = 50.0) -> dict[str, object]:
    """current active 的固定内容：两个训练日同一 squat 处方，目标负荷 50kg、3 组 8–12 次。"""
    return _content(
        *((day, (_weighted(_known_load(weight_kg)),)) for day in ACTIVE_DAYS),
        starts_on=ACTIVE_DAYS[0],
    )


def _active_squat_draft(weight_kg: float = 50.0) -> PlanDraft:
    """``current active`` 的目标处方（规则单测用）。"""
    return PlanDraft.model_validate(_active_squat_content(weight_kg))


def _squat_candidate(
    weight_kg: float, *, extra: dict[str, object] | None = None
) -> PlanDraft:
    """一个 squat（外加负重）候选草案；``extra`` 用于再附一个动作（新动作回退口径）。"""
    exercises: list[dict[str, object]] = [_weighted(_known_load(weight_kg, session=2))]
    if extra is not None:
        exercises.append(extra)
    return PlanDraft.model_validate(_content((date(2026, 6, 2), tuple(exercises))))


def _calibration_candidate() -> PlanDraft:
    """决策为待校准时候选不得给出具体重量（只带待校准负荷）。"""
    return PlanDraft.model_validate(
        _content((date(2026, 6, 2), (_weighted(_needs_calibration()),)))
    )


def _work_set(
    *,
    session: int,
    set_no: int = 1,
    weight_kg: float = 50.0,
    reps: int = 12,
    performed_on: date = ACTIVE_DAYS[0],
    exercise_id: str = WEIGHTED,
) -> ValidWorkSet:
    """一个有效工作组（纯领域对象；有效组过滤口径本身由 ``domain.stats.repo`` 的唯一 SQL 决定）。"""
    return ValidWorkSet(
        exercise_id=exercise_id,
        exercise_name="测试动作",
        record_type="reps_weight",
        load_convention="barbell_includes_bar_total",
        weight_kg=weight_kg,
        reps=reps,
        duration_seconds=None,
        workout_session_id=session,
        set_no=set_no,
        performed_on=performed_on,
    )


def _exercise(
    exercise_id: str, record_type: RecordType, *, increment_kg: float | None = 2.5
) -> Exercise:
    """一个目录动作：负重口径与加重单位只属于外加负重类型。"""
    weighted = record_type == "reps_weight"
    return Exercise(
        id=exercise_id,
        standard_name_zh=f"测试动作 {exercise_id}",
        equipment_variant="barbell" if weighted else "bodyweight",
        record_type=record_type,
        load_convention="barbell_includes_bar_total" if weighted else None,
        min_load_increment_kg=increment_kg if weighted else None,
        recommendable=True,
        modes=("深蹲",),
        source_ref="tests/test_stage5_activation.py",
        attribution="测试固定事实",
    )


CATALOG: dict[str, Exercise] = {
    exercise.id: exercise
    for exercise in (
        _exercise(WEIGHTED, "reps_weight"),
        _exercise(NEW_ACTION, "reps_weight"),
        _exercise(BODYWEIGHT, "reps_bodyweight"),
    )
}


def _passed_evaluation() -> EvaluationResult:
    """通过路径的 Evaluator 结果（确定性层与两个硬门槛同通过，Schema 要求 ``passed`` 恰为合取）。"""
    return EvaluationResult(
        passed=True,
        deterministic=DeterministicResult(passed=True, failures=()),
        rubric=RubricResult(
            goal_alignment=RubricVerdict(passed=True, reason="目标匹配"),
            schedule_reasonableness=RubricVerdict(passed=True, reason="安排合理"),
            explanation_quality=RubricVerdict(passed=True, reason="解释质量"),
        ),
        blocking_failures=(),
        warnings=(),
        revision_count=0,
    )


# ---------- 临时业务库与直接 SQL 预置 ----------


async def _migrated(path: Path) -> Database:
    db = Database(path)
    await db.open()
    await db.migrate()
    return db


async def _write_profile(db: Database, *, weekly_frequency: int | None = 1) -> None:
    """画像（未建档即不调用本函数）：``weekly_frequency`` 为 None 时写明确「未填写」三态。"""
    await ProfileService(db).update(
        Profile(
            training_goal=Fact.known("增肌"),
            weekly_frequency=(
                Fact.unknown()
                if weekly_frequency is None
                else Fact.known(weekly_frequency)
            ),
            available_equipment=Fact.known(("barbell", "bodyweight")),
            explicit_preferences=Fact.denied(),
            current_level=Fact.known("中级"),
            known_injuries=Fact.denied(),
            forbidden_exercise_ids=Fact.denied(),
        )
    )


async def _insert_plan(
    db: Database,
    *,
    version: int,
    status: str,
    content: dict[str, object],
    source_plan_id: int | None = None,
    created_at: str = CREATED_AT,
    confirmed_at: str | None = None,
    archived_at: str | None = None,
) -> int:
    """直接 SQL 预置一个计划版本行（正式写入口是被测服务），返回计划身份。"""
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "INSERT INTO plans (version, status, source_plan_id, structured_content,"
            " evaluator_result, created_at, confirmed_at, archived_at)"
            " VALUES (?, ?, ?, ?, NULL, ?, ?, ?)",
            (
                version,
                status,
                source_plan_id,
                json.dumps(content, ensure_ascii=False),
                created_at,
                confirmed_at,
                archived_at,
            ),
        )
        try:
            return int(cursor.lastrowid or 0)
        finally:
            await cursor.close()


async def _insert_session(db: Database, *, plan_id: int, scheduled_on: date) -> int:
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "INSERT INTO plan_sessions (plan_id, scheduled_on, cancelled_at)"
            " VALUES (?, ?, NULL)",
            (plan_id, scheduled_on.isoformat()),
        )
        try:
            return int(cursor.lastrowid or 0)
        finally:
            await cursor.close()


async def _insert_workout(
    db: Database, *, performed_on: date, plan_session_id: int | None = None
) -> int:
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "INSERT INTO workout_sessions (performed_on, plan_session_id) VALUES (?, ?)",
            (performed_on.isoformat(), plan_session_id),
        )
        try:
            return int(cursor.lastrowid or 0)
        finally:
            await cursor.close()


async def _insert_weighted_work_set(
    db: Database,
    *,
    workout_session_id: int,
    set_no: int,
    weight_kg: float,
    reps: int,
) -> None:
    """一条外加负重有效工作组（``set_type='work'``、口径与重量同现）。"""
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO workout_sets (workout_session_id, exercise_id, set_no, set_type,"
            " load_convention, weight_kg, reps) VALUES (?, ?, ?, 'work',"
            " 'barbell_includes_bar_total', ?, ?)",
            (workout_session_id, WEIGHTED, set_no, weight_kg, reps),
        )


# ---------- 写路径断言用的只读快照 ----------


async def _plan_row(db: Database, plan_id: int) -> dict[str, object]:
    """计划行的全列快照（含两个状态时间字段与内容）：失败路径必须逐字段不变。"""

    async def op(conn):
        async with conn.execute(
            "SELECT id, version, status, source_plan_id, structured_content,"
            " evaluator_result, created_at, confirmed_at, archived_at"
            " FROM plans WHERE id = ?",
            (plan_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return {} if row is None else dict(row)

    return await db.under_lock(op)


async def _sessions(db: Database, plan_id: int) -> list[tuple[str, str | None]]:
    """某计划的日程（按应训练日排序）：``(scheduled_on, cancelled_at)``。"""

    async def op(conn):
        async with conn.execute(
            "SELECT scheduled_on, cancelled_at FROM plan_sessions"
            " WHERE plan_id = ? ORDER BY scheduled_on, id",
            (plan_id,),
        ) as cursor:
            return [(str(row[0]), row[1]) for row in await cursor.fetchall()]

    return await db.under_lock(op)


async def _plan_status_counts(db: Database) -> dict[str, int]:
    async def op(conn):
        async with conn.execute(
            "SELECT status, COUNT(*) FROM plans GROUP BY status"
        ) as cursor:
            return {str(row[0]): int(row[1]) for row in await cursor.fetchall()}

    return await db.under_lock(op)


def _activated_keys(row: dict[str, object]) -> dict[str, object]:
    """激活成功时只允许 ``status``／``archived_at`` 变化的那些字段。"""
    return {key: row[key] for key in _OLD_ACTIVE_KEPT_FIELDS}


async def _adjustment_fixture(db: Database, *, load_kg: float) -> tuple[int, int]:
    """预置「active 目标 50kg ＋ 两次达到次数上限的关联训练」与一个调整 draft，返回两个身份。

    §2.4 的渐进决策在这是加重：两次关联训练都在目标负荷上完整做到次数上限，下一档是 50+2.5=52.5kg。
    """
    await _write_profile(db, weekly_frequency=len(ACTIVE_DAYS))
    active_id = await _insert_plan(
        db,
        version=1,
        status="active",
        content=_active_squat_content(),
        confirmed_at=OLD_CONFIRMED_AT,
    )
    for performed_on in ACTIVE_DAYS:
        session_id = await _insert_session(db, plan_id=active_id, scheduled_on=performed_on)
        workout_id = await _insert_workout(
            db, performed_on=performed_on, plan_session_id=session_id
        )
        for set_no in (1, 2, 3):
            await _insert_weighted_work_set(
                db, workout_session_id=workout_id, set_no=set_no, weight_kg=50.0, reps=12
            )
    draft_id = await _insert_plan(
        db,
        version=2,
        status="draft",
        source_plan_id=active_id,
        content=_content(
            *(
                (day, (_weighted(_known_load(load_kg)),))
                for day in (date(2026, 6, 2), date(2026, 6, 5))
            )
        ),
    )
    return active_id, draft_id


# ---------- §2.1 激活事务 ----------


async def test_activation_archives_the_old_active_and_creates_the_new_sessions(
    tmp_path: Path,
) -> None:
    """首次激活：draft→active＋``confirmed_at``；旧 active→archived＋``archived_at``；旧计划
    ``scheduled_on >= 业务日`` 的日程取消、历史日程保留；新计划按 ``training_days`` 各建一条日程。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        await _write_profile(db)
        old_id = await _insert_plan(
            db,
            version=1,
            status="active",
            content=_content(
                (date(2026, 5, 2), (_bodyweight(),)), starts_on=ACTIVE_DAYS[0]
            ),
            confirmed_at=OLD_CONFIRMED_AT,
        )
        await _insert_session(db, plan_id=old_id, scheduled_on=date(2026, 5, 30))
        await _insert_session(db, plan_id=old_id, scheduled_on=date(2026, 6, 2))
        draft_id = await _insert_plan(
            db,
            version=2,
            status="draft",
            content=_content((date(2026, 6, 2), (_bodyweight(),))),
        )
        before_old = await _plan_row(db, old_id)

        activated = await PlanActivationService(db).activate(
            draft_id,
            business_day=BUSINESS_DAY,
            confirmed_at=CONFIRMED_AT,
            archived_at=ARCHIVED_AT,
        )

        assert activated.id == draft_id
        assert activated.version == 2
        assert activated.status == "active"
        assert activated.source_plan_id is None
        assert activated.confirmed_at == CONFIRMED_AT
        assert activated.archived_at is None
        after_old = await _plan_row(db, old_id)
        assert after_old["status"] == "archived"
        assert after_old["archived_at"] == ARCHIVED_AT
        assert _activated_keys(after_old) == _activated_keys(before_old)
        assert await _plan_status_counts(db) == {"active": 1, "archived": 1}
        assert await _sessions(db, old_id) == [
            ("2026-05-30", None),
            ("2026-06-02", ARCHIVED_AT),
        ]
        assert await _sessions(db, draft_id) == [("2026-06-02", None)]
    finally:
        await db.close()


async def test_activation_without_an_old_active_only_activates_the_new_plan(
    tmp_path: Path,
) -> None:
    """没有旧 active：只把新计划置为 active 并建日程，不产生 archived 行、不取消任何日程。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        await _write_profile(db)
        draft_id = await _insert_plan(
            db,
            version=1,
            status="draft",
            content=_content((date(2026, 6, 2), (_bodyweight(),))),
        )

        activated = await PlanActivationService(db).activate(
            draft_id,
            business_day=BUSINESS_DAY,
            confirmed_at=CONFIRMED_AT,
            archived_at=ARCHIVED_AT,
        )

        assert activated.status == "active"
        assert activated.confirmed_at == CONFIRMED_AT
        assert await _plan_status_counts(db) == {"active": 1}
        assert await _sessions(db, draft_id) == [("2026-06-02", None)]
    finally:
        await db.close()


async def test_a_stale_starts_on_is_rejected_and_the_draft_stays_confirmable(
    tmp_path: Path,
) -> None:
    """``starts_on < 本次注入业务日``：拒绝激活；不归档、不改 draft 状态、不写日程（draft 仍可确认）。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        await _write_profile(db)
        old_id = await _insert_plan(
            db,
            version=1,
            status="active",
            content=_active_squat_content(),
            confirmed_at=OLD_CONFIRMED_AT,
        )
        await _insert_session(db, plan_id=old_id, scheduled_on=date(2026, 6, 2))
        draft_id = await _insert_plan(
            db,
            version=2,
            status="draft",
            content=_content(
                (date(2026, 6, 1), (_bodyweight(),)), starts_on=date(2026, 5, 31)
            ),
        )
        before_draft = await _plan_row(db, draft_id)
        before_old = await _plan_row(db, old_id)

        with pytest.raises(PlanDraftStale):
            await PlanActivationService(db).activate(
                draft_id,
                business_day=BUSINESS_DAY,
                confirmed_at=CONFIRMED_AT,
                archived_at=ARCHIVED_AT,
            )

        assert await _plan_row(db, draft_id) == before_draft
        assert await _plan_row(db, old_id) == before_old
        assert await _sessions(db, draft_id) == []
        assert await _sessions(db, old_id) == [("2026-06-02", None)]
    finally:
        await db.close()


async def test_cancellation_and_freshness_boundaries_are_the_injected_business_day(
    tmp_path: Path,
) -> None:
    """业务日边界：``scheduled_on == 业务日`` 取消、``< 业务日`` 保留；``starts_on == 业务日`` 可激活。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        await _write_profile(db)
        old_id = await _insert_plan(
            db,
            version=1,
            status="active",
            content=_active_squat_content(),
            confirmed_at=OLD_CONFIRMED_AT,
        )
        for scheduled_on in (date(2026, 5, 31), BUSINESS_DAY, date(2026, 6, 2)):
            await _insert_session(db, plan_id=old_id, scheduled_on=scheduled_on)
        draft_id = await _insert_plan(
            db,
            version=2,
            status="draft",
            content=_content((date(2026, 6, 3), (_bodyweight(),))),
        )

        await PlanActivationService(db).activate(
            draft_id,
            business_day=BUSINESS_DAY,
            confirmed_at=CONFIRMED_AT,
            archived_at=ARCHIVED_AT,
        )

        assert await _sessions(db, old_id) == [
            ("2026-05-31", None),
            ("2026-06-01", ARCHIVED_AT),
            ("2026-06-02", ARCHIVED_AT),
        ]
        assert await _sessions(db, draft_id) == [("2026-06-03", None)]
    finally:
        await db.close()


async def test_failed_revalidation_keeps_the_draft_and_the_original_active(
    tmp_path: Path,
) -> None:
    """再校验失败（生成 draft 的负荷不等于最近一次有效工作组）：不归档、不改状态、不写日程。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        await _write_profile(db)
        old_id = await _insert_plan(
            db,
            version=1,
            status="active",
            content=_active_squat_content(),
            confirmed_at=OLD_CONFIRMED_AT,
        )
        await _insert_session(db, plan_id=old_id, scheduled_on=date(2026, 6, 2))
        workout_id = await _insert_workout(db, performed_on=date(2026, 5, 20))
        await _insert_weighted_work_set(
            db, workout_session_id=workout_id, set_no=1, weight_kg=50.0, reps=8
        )
        draft_id = await _insert_plan(
            db,
            version=2,
            status="draft",
            content=_content((date(2026, 6, 2), (_weighted(_known_load(60.0)),))),
        )
        before_draft = await _plan_row(db, draft_id)
        before_old = await _plan_row(db, old_id)

        with pytest.raises(PlanRevalidationFailed) as excinfo:
            await PlanActivationService(db).activate(
                draft_id,
                business_day=BUSINESS_DAY,
                confirmed_at=CONFIRMED_AT,
                archived_at=ARCHIVED_AT,
            )

        assert [failure.code for failure in excinfo.value.failures] == [
            "load_source_mismatch"
        ]
        assert await _plan_row(db, draft_id) == before_draft
        assert await _plan_row(db, old_id) == before_old
        assert await _sessions(db, draft_id) == []
        assert await _sessions(db, old_id) == [("2026-06-02", None)]
    finally:
        await db.close()


async def test_activation_rejects_an_adjustment_load_that_is_not_the_progression_decision(
    tmp_path: Path,
) -> None:
    """调整 draft 的负荷必须等于渐进决策（此处为加重到 52.5kg）：等于旧目标负荷即再校验失败。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        active_id, draft_id = await _adjustment_fixture(db, load_kg=50.0)
        before_draft = await _plan_row(db, draft_id)
        before_active = await _plan_row(db, active_id)

        with pytest.raises(PlanRevalidationFailed) as excinfo:
            await PlanActivationService(db).activate(
                draft_id,
                business_day=BUSINESS_DAY,
                confirmed_at=CONFIRMED_AT,
                archived_at=ARCHIVED_AT,
            )

        assert [failure.code for failure in excinfo.value.failures] == [
            "load_source_mismatch",
            "load_source_mismatch",
        ]
        assert await _plan_row(db, draft_id) == before_draft
        assert await _plan_row(db, active_id) == before_active
        assert await _sessions(db, draft_id) == []
        assert await _sessions(db, active_id) == [
            ("2026-05-01", None),
            ("2026-05-04", None),
        ]
    finally:
        await db.close()


async def test_activation_accepts_the_progression_increase_load_for_an_adjustment_draft(
    tmp_path: Path,
) -> None:
    """决策为加重时，按目标＋一档最小加重（50→52.5kg）可激活；``source_plan_id`` 与旧 active 关系保留。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        active_id, draft_id = await _adjustment_fixture(
            db, load_kg=50.0 + SQUAT_INCREMENT_KG
        )
        before_active = await _plan_row(db, active_id)

        activated = await PlanActivationService(db).activate(
            draft_id,
            business_day=BUSINESS_DAY,
            confirmed_at=CONFIRMED_AT,
            archived_at=ARCHIVED_AT,
        )

        assert activated.source_plan_id == active_id
        assert activated.status == "active"
        after_active = await _plan_row(db, active_id)
        assert after_active["status"] == "archived"
        assert after_active["archived_at"] == ARCHIVED_AT
        assert _activated_keys(after_active) == _activated_keys(before_active)
        assert await _sessions(db, active_id) == [
            ("2026-05-01", None),
            ("2026-05-04", None),
        ]
        assert await _sessions(db, draft_id) == [
            ("2026-06-02", None),
            ("2026-06-05", None),
        ]
    finally:
        await db.close()


async def test_a_failure_inside_the_transaction_rolls_back_every_write(
    tmp_path: Path,
) -> None:
    """事务中途失败整体回滚：旧 active 不被归档、其日程不被取消、draft 不激活、日程不新增。

    失败由真实写入触发：draft 已有一条同日日程（固定事实），建立新计划日程时命中
    ``UNIQUE(plan_id, scheduled_on)``——此时归档与取消已经执行，仍须整体回滚。
    """
    db = await _migrated(tmp_path / "x.db")
    try:
        await _write_profile(db)
        old_id = await _insert_plan(
            db,
            version=1,
            status="active",
            content=_active_squat_content(),
            confirmed_at=OLD_CONFIRMED_AT,
        )
        await _insert_session(db, plan_id=old_id, scheduled_on=date(2026, 6, 2))
        draft_id = await _insert_plan(
            db,
            version=2,
            status="draft",
            content=_content((date(2026, 6, 2), (_bodyweight(),))),
        )
        await _insert_session(db, plan_id=draft_id, scheduled_on=date(2026, 6, 2))
        before_draft = await _plan_row(db, draft_id)
        before_old = await _plan_row(db, old_id)

        with pytest.raises(sqlite3.IntegrityError):
            await PlanActivationService(db).activate(
                draft_id,
                business_day=BUSINESS_DAY,
                confirmed_at=CONFIRMED_AT,
                archived_at=ARCHIVED_AT,
            )

        assert await _plan_row(db, draft_id) == before_draft
        assert await _plan_row(db, old_id) == before_old
        assert await _sessions(db, old_id) == [("2026-06-02", None)]
        assert await _sessions(db, draft_id) == [("2026-06-02", None)]
        assert await _plan_status_counts(db) == {"active": 1, "draft": 1}
    finally:
        await db.close()


async def test_repeated_activation_of_the_same_plan_is_idempotent(tmp_path: Path) -> None:
    """重复确认同一已 active 的计划：幂等返回当前行，不再写任何行、不重复建日程。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        await _write_profile(db)
        old_id = await _insert_plan(
            db,
            version=1,
            status="active",
            content=_active_squat_content(),
            confirmed_at=OLD_CONFIRMED_AT,
        )
        await _insert_session(db, plan_id=old_id, scheduled_on=date(2026, 6, 2))
        draft_id = await _insert_plan(
            db,
            version=2,
            status="draft",
            content=_content((date(2026, 6, 2), (_bodyweight(),))),
        )
        service = PlanActivationService(db)
        first = await service.activate(
            draft_id,
            business_day=BUSINESS_DAY,
            confirmed_at=CONFIRMED_AT,
            archived_at=ARCHIVED_AT,
        )
        before_repeat = await _plan_row(db, draft_id)
        before_sessions = await _sessions(db, draft_id)

        second = await service.activate(
            draft_id,
            business_day=BUSINESS_DAY,
            confirmed_at="2026-06-02T09:00:00+08:00",
            archived_at="2026-06-02T09:00:00+08:00",
        )

        assert second == first
        assert second.confirmed_at == CONFIRMED_AT
        assert await _plan_row(db, draft_id) == before_repeat
        assert await _sessions(db, draft_id) == before_sessions
        assert await _plan_status_counts(db) == {"active": 1, "archived": 1}
    finally:
        await db.close()


async def test_archived_and_rejected_plans_refuse_activation_and_report_conflicts(
    tmp_path: Path,
) -> None:
    """§2.2 矩阵：archived 拒绝重新激活、reject 幂等返回当前行；rejected 两种动作都是明确冲突；
    不存在的 id 是明确错误（不猜目标、不写任何行）。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        archived_id = await _insert_plan(
            db,
            version=1,
            status="archived",
            content=_active_squat_content(),
            confirmed_at=OLD_CONFIRMED_AT,
            archived_at=OLD_CONFIRMED_AT,
        )
        rejected_id = await _insert_plan(
            db, version=2, status="rejected", content=_active_squat_content()
        )
        before = {
            archived_id: await _plan_row(db, archived_id),
            rejected_id: await _plan_row(db, rejected_id),
        }
        service = PlanActivationService(db)

        for plan_id in (archived_id, rejected_id):
            with pytest.raises(PlanActivationConflict):
                await service.activate(
                    plan_id,
                    business_day=BUSINESS_DAY,
                    confirmed_at=CONFIRMED_AT,
                    archived_at=ARCHIVED_AT,
                )
        with pytest.raises(PlanActivationConflict):
            await service.reject(rejected_id, archived_at=ARCHIVED_AT)

        kept = await service.reject(archived_id, archived_at=ARCHIVED_AT)

        assert kept.id == archived_id
        assert kept.status == "archived"
        assert kept.archived_at == OLD_CONFIRMED_AT  # 不重复写：原归档时间不变
        assert await _plan_row(db, archived_id) == before[archived_id]
        assert await _plan_row(db, rejected_id) == before[rejected_id]
        assert await _plan_status_counts(db) == {"archived": 1, "rejected": 1}
        with pytest.raises(PlanNotFound):
            await service.activate(
                999,
                business_day=BUSINESS_DAY,
                confirmed_at=CONFIRMED_AT,
                archived_at=ARCHIVED_AT,
            )
        with pytest.raises(PlanNotFound):
            await service.reject(999, archived_at=ARCHIVED_AT)
    finally:
        await db.close()


async def test_an_adjustment_draft_whose_source_is_not_the_current_active_is_rejected(
    tmp_path: Path,
) -> None:
    """调整 draft 的 ``source_plan_id`` 必须等于当前 active.id：指向别的计划同样冲突。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        await _write_profile(db)
        active_id = await _insert_plan(
            db,
            version=1,
            status="active",
            content=_active_squat_content(),
            confirmed_at=OLD_CONFIRMED_AT,
        )
        other_id = await _insert_plan(
            db,
            version=2,
            status="archived",
            content=_active_squat_content(),
            confirmed_at=OLD_CONFIRMED_AT,
            archived_at=OLD_CONFIRMED_AT,
        )
        draft_id = await _insert_plan(
            db,
            version=3,
            status="draft",
            source_plan_id=other_id,
            content=_content((date(2026, 6, 2), (_weighted(_known_load(50.0)),))),
        )
        before_draft = await _plan_row(db, draft_id)
        before_active = await _plan_row(db, active_id)

        with pytest.raises(PlanActivationConflict):
            await PlanActivationService(db).activate(
                draft_id,
                business_day=BUSINESS_DAY,
                confirmed_at=CONFIRMED_AT,
                archived_at=ARCHIVED_AT,
            )

        assert await _plan_row(db, draft_id) == before_draft
        assert await _plan_row(db, active_id) == before_active
    finally:
        await db.close()

    no_active = await _migrated(tmp_path / "no-active.db")
    try:
        await _write_profile(no_active)
        archived_id = await _insert_plan(
            no_active,
            version=1,
            status="archived",
            content=_active_squat_content(),
            confirmed_at=OLD_CONFIRMED_AT,
            archived_at=OLD_CONFIRMED_AT,
        )
        draft_id = await _insert_plan(
            no_active,
            version=2,
            status="draft",
            source_plan_id=archived_id,
            content=_content((date(2026, 6, 2), (_weighted(_known_load(50.0)),))),
        )
        before_draft = await _plan_row(no_active, draft_id)

        with pytest.raises(PlanActivationConflict):
            await PlanActivationService(no_active).activate(
                draft_id,
                business_day=BUSINESS_DAY,
                confirmed_at=CONFIRMED_AT,
                archived_at=ARCHIVED_AT,
            )

        assert await _plan_row(no_active, draft_id) == before_draft
    finally:
        await no_active.close()


async def test_activation_requires_a_known_profile_weekly_frequency(tmp_path: Path) -> None:
    """画像缺少每周训练次数时不能再校验（不猜频率）：拒绝激活且 draft 保持可确认。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        await _write_profile(db, weekly_frequency=None)
        old_id = await _insert_plan(
            db,
            version=1,
            status="active",
            content=_active_squat_content(),
            confirmed_at=OLD_CONFIRMED_AT,
        )
        draft_id = await _insert_plan(
            db,
            version=2,
            status="draft",
            content=_content((date(2026, 6, 2), (_bodyweight(),))),
        )
        before_draft = await _plan_row(db, draft_id)
        before_old = await _plan_row(db, old_id)

        with pytest.raises(PlanRevalidationFailed) as excinfo:
            await PlanActivationService(db).activate(
                draft_id,
                business_day=BUSINESS_DAY,
                confirmed_at=CONFIRMED_AT,
                archived_at=ARCHIVED_AT,
            )

        assert "每周训练次数" in str(excinfo.value)
        assert excinfo.value.failures == ()
        assert await _plan_row(db, draft_id) == before_draft
        assert await _plan_row(db, old_id) == before_old
        assert await _plan_status_counts(db) == {"active": 1, "draft": 1}
    finally:
        await db.close()


# ---------- §2.2 reject／archive_draft ----------


async def test_refusal_archives_the_draft_and_never_touches_the_original_active(
    tmp_path: Path,
) -> None:
    """用户拒绝只写 ``draft→archived``＋``archived_at``：原 active 逐字段不变、永不写 ``rejected``。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        await _write_profile(db)
        old_id = await _insert_plan(
            db,
            version=1,
            status="active",
            content=_active_squat_content(),
            confirmed_at=OLD_CONFIRMED_AT,
        )
        await _insert_session(db, plan_id=old_id, scheduled_on=date(2026, 6, 2))
        draft_id = await _insert_plan(
            db,
            version=2,
            status="draft",
            content=_content((date(2026, 6, 2), (_bodyweight(),))),
        )
        before_old = await _plan_row(db, old_id)

        archived = await PlanActivationService(db).reject(
            draft_id, archived_at=ARCHIVED_AT
        )

        assert archived.id == draft_id
        assert archived.status == "archived"
        assert archived.archived_at == ARCHIVED_AT
        assert archived.confirmed_at is None
        assert archived.source_plan_id is None
        assert await _plan_row(db, old_id) == before_old
        assert await _plan_status_counts(db) == {"active": 1, "archived": 1}
        assert await _sessions(db, draft_id) == []
        assert await _sessions(db, old_id) == [("2026-06-02", None)]
    finally:
        await db.close()


async def test_repeated_refusal_is_idempotent_and_never_writes_the_rejected_status(
    tmp_path: Path,
) -> None:
    """重复拒绝同一 archived 计划：返回当前行、不重复写；整库始终没有 ``rejected`` 状态。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        await _write_profile(db)
        await _insert_plan(
            db,
            version=1,
            status="active",
            content=_active_squat_content(),
            confirmed_at=OLD_CONFIRMED_AT,
        )
        draft_id = await _insert_plan(
            db,
            version=2,
            status="draft",
            content=_content((date(2026, 6, 2), (_bodyweight(),))),
        )
        service = PlanActivationService(db)
        first = await service.reject(draft_id, archived_at=ARCHIVED_AT)
        before_repeat = await _plan_row(db, draft_id)

        second = await service.reject(
            draft_id, archived_at="2026-06-02T09:00:00+08:00"
        )

        assert second == first
        assert second.archived_at == ARCHIVED_AT
        assert await _plan_row(db, draft_id) == before_repeat
        assert await _plan_status_counts(db) == {"active": 1, "archived": 1}
    finally:
        await db.close()


async def test_persisting_an_adjustment_draft_records_its_source_plan_id(
    tmp_path: Path,
) -> None:
    """§2.4：写入调整 draft 时落下来源计划 id；生成 draft 仍为 NULL（Stage 4 口径不变）。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        active_id = await _insert_plan(
            db,
            version=1,
            status="active",
            content=_active_squat_content(),
            confirmed_at=OLD_CONFIRMED_AT,
        )
        service = PlanPersistenceService(db)

        adjusted = await service.persist_plan_result(
            _squat_candidate(52.5),
            _passed_evaluation(),
            existing_draft_id=None,
            created_at=CREATED_AT,
            source_plan_id=active_id,
        )
        assert adjusted.source_plan_id == active_id
        assert adjusted.status == "draft"
        assert await _sessions(db, active_id) == []

        generated = await service.persist_plan_result(
            _squat_candidate(50.0),
            _passed_evaluation(),
            existing_draft_id=adjusted.id,
            created_at=CREATED_AT,
        )
        assert generated.id == adjusted.id
        assert generated.source_plan_id == active_id  # 条件替换不重写来源
    finally:
        await db.close()


# ---------- §2.4 调整计划的负荷校验与生成规则的分工 ----------


#: 两次关联日程训练都在目标负荷 50kg 上做到次数上限（决策：加重一档）。
_INCREASE_HISTORY = tuple(
    _work_set(session=session, set_no=set_no)
    for session in (1, 2)
    for set_no in (1, 2, 3)
)


def test_generate_and_adjustment_load_rules_are_distinct() -> None:
    """生成规则要求等于最近一次有效工作组；调整规则要求等于渐进决策——互不替代，两个方向都验。"""
    active = _active_squat_draft()
    linked = (1, 2)
    decision = resolve_progression(
        _INCREASE_HISTORY,
        linked_workout_session_ids=linked,
        target_sets=3,
        reps_min=8,
        reps_max=12,
        target_load_kg=50.0,
        increment_kg=SQUAT_INCREMENT_KG,
    )
    assert decision == ProgressionDecision("increase", 52.5)

    increased = _squat_candidate(52.5)
    assert (
        validate_plan_adjustment(
            increased,
            active_draft=active,
            linked_workout_session_ids=linked,
            exercises=CATALOG,
            profile_weekly_frequency=1,
            work_sets=_INCREASE_HISTORY,
        )
        == ()
    )
    assert [
        failure.code
        for failure in validate_plan_draft(
            increased,
            exercises=CATALOG,
            profile_weekly_frequency=1,
            work_sets=_INCREASE_HISTORY,
        )
    ] == ["load_source_mismatch"]

    kept = _squat_candidate(50.0)
    assert (
        validate_plan_draft(
            kept,
            exercises=CATALOG,
            profile_weekly_frequency=1,
            work_sets=_INCREASE_HISTORY,
        )
        == ()
    )
    assert [
        failure.code
        for failure in validate_plan_adjustment(
            kept,
            active_draft=active,
            linked_workout_session_ids=linked,
            exercises=CATALOG,
            profile_weekly_frequency=1,
            work_sets=_INCREASE_HISTORY,
        )
    ] == ["load_source_mismatch"]


def test_adjustment_follows_keep_regress_and_needs_calibration_decisions() -> None:
    """四类决策各自生效：不足两次关联训练保持目标负荷、连续两次未达标签回退、无可回退即待校准。"""
    active = _active_squat_draft()

    # keep：只有一次关联训练。
    single = tuple(_work_set(session=1, set_no=set_no) for set_no in (1, 2, 3))
    assert resolve_progression(
        single,
        linked_workout_session_ids=(1,),
        target_sets=3,
        reps_min=8,
        reps_max=12,
        target_load_kg=50.0,
        increment_kg=SQUAT_INCREMENT_KG,
    ) == ProgressionDecision("keep", 50.0)
    assert (
        validate_plan_adjustment(
            _squat_candidate(50.0),
            active_draft=active,
            linked_workout_session_ids=(1,),
            exercises=CATALOG,
            profile_weekly_frequency=1,
            work_sets=single,
        )
        == ()
    )
    assert [
        failure.code
        for failure in validate_plan_adjustment(
            _squat_candidate(52.5),
            active_draft=active,
            linked_workout_session_ids=(1,),
            exercises=CATALOG,
            profile_weekly_frequency=1,
            work_sets=single,
        )
    ] == ["load_source_mismatch"]

    # regress：最近两次都未达标，回退到最近一次完整完成的负荷 45kg。
    completed = tuple(
        _work_set(
            session=1, set_no=set_no, weight_kg=45.0, reps=10, performed_on=ACTIVE_DAYS[0]
        )
        for set_no in (1, 2, 3)
    )
    failed = (
        _work_set(session=2, set_no=1, weight_kg=50.0, reps=5, performed_on=ACTIVE_DAYS[1]),
        _work_set(session=3, set_no=1, weight_kg=50.0, reps=5, performed_on=date(2026, 5, 8)),
    )
    regress_history = completed + failed
    assert resolve_progression(
        regress_history,
        linked_workout_session_ids=(1, 2, 3),
        target_sets=3,
        reps_min=8,
        reps_max=12,
        target_load_kg=50.0,
        increment_kg=SQUAT_INCREMENT_KG,
    ) == ProgressionDecision("regress", 45.0)
    assert (
        validate_plan_adjustment(
            _squat_candidate(45.0),
            active_draft=active,
            linked_workout_session_ids=(1, 2, 3),
            exercises=CATALOG,
            profile_weekly_frequency=1,
            work_sets=regress_history,
        )
        == ()
    )
    assert [
        failure.code
        for failure in validate_plan_adjustment(
            _squat_candidate(50.0),
            active_draft=active,
            linked_workout_session_ids=(1, 2, 3),
            exercises=CATALOG,
            profile_weekly_frequency=1,
            work_sets=regress_history,
        )
    ] == ["load_source_mismatch"]

    # needs_calibration：连续两次未达标且没有可回退的完整完成负荷。
    calibration_history = failed
    assert resolve_progression(
        calibration_history,
        linked_workout_session_ids=(2, 3),
        target_sets=3,
        reps_min=8,
        reps_max=12,
        target_load_kg=50.0,
        increment_kg=SQUAT_INCREMENT_KG,
    ) == ProgressionDecision("needs_calibration", None)
    assert [
        failure.code
        for failure in validate_plan_adjustment(
            _squat_candidate(50.0),
            active_draft=active,
            linked_workout_session_ids=(2, 3),
            exercises=CATALOG,
            profile_weekly_frequency=1,
            work_sets=calibration_history,
        )
    ] == ["load_source_mismatch"]
    assert (
        validate_plan_adjustment(
            _calibration_candidate(),
            active_draft=active,
            linked_workout_session_ids=(2, 3),
            exercises=CATALOG,
            profile_weekly_frequency=1,
            work_sets=calibration_history,
        )
        == ()
    )


def test_adjustment_falls_back_to_the_starting_load_rule_and_keeps_structural_checks() -> None:
    """active 没有该动作的目标处方时没有渐进决策可判，按起始负荷口径校验来源；结构与目录检查同生成规则。"""
    active = _active_squat_draft()
    new_action_history = _work_set(
        session=7, set_no=2, weight_kg=40.0, reps=8, exercise_id=NEW_ACTION
    )
    history = _INCREASE_HISTORY + (new_action_history,)

    matching = _squat_candidate(
        50.0 + SQUAT_INCREMENT_KG,
        extra=_weighted(
            _known_load(40.0, session=7, set_no=2), exercise_id=NEW_ACTION
        ),
    )
    assert (
        validate_plan_adjustment(
            matching,
            active_draft=active,
            linked_workout_session_ids=(1, 2),
            exercises=CATALOG,
            profile_weekly_frequency=1,
            work_sets=history,
        )
        == ()
    )

    unknown = PlanDraft.model_validate(
        _content((date(2026, 6, 2), (_weighted(_known_load(50.0), exercise_id="no-such-action"),)))
    )
    assert "unknown_exercise" in [
        failure.code
        for failure in validate_plan_adjustment(
            unknown,
            active_draft=active,
            linked_workout_session_ids=(1, 2),
            exercises=CATALOG,
            profile_weekly_frequency=1,
            work_sets=_INCREASE_HISTORY,
        )
    ]
    assert [
        failure.code
        for failure in validate_plan_adjustment(
            _squat_candidate(50.0 + SQUAT_INCREMENT_KG),
            active_draft=active,
            linked_workout_session_ids=(1, 2),
            exercises=CATALOG,
            profile_weekly_frequency=2,
            work_sets=_INCREASE_HISTORY,
        )
    ] == ["weekly_frequency_mismatch"]
