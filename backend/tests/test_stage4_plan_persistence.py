"""Stage 4 子任务 03：draft/rejected 最小持久化与原 active 保护。

依据：``refactor-log/stage4.md`` §3.8／§3.9／§3.10／§5.2／§6 Subtask 03／§8.6／§9；
``Fit-Agent-LangGraph-重构讨论总结.md`` §3.3／§3.4／§9。覆盖矩阵见 ``refactor-log/stage4.md``
§8.6（另测，持久化侧）／§9.2／§5.5。

本文件只测业务库侧的持久化服务（``domain.plans.service.PlanPersistenceService``）：不接 Graph、
不调模型、不确认、不激活。计划数据只用直接 SQL 预置（active／draft），其余经被测服务写入。
每个写路径在操作前后断言原 active 的 id／状态／版本／内容／确认时间不变且行数不变（§9.2）。
"""

import ast
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from domain.plans.repo import PlanRepo
from domain.plans.schema import (
    BodyweightRepsPrescription,
    DeterministicResult,
    EvaluationResult,
    PlanDraft,
    PlannedExercise,
    RubricResult,
    RubricVerdict,
    RuleFailure,
    TrainingDay,
    evaluation_result_from_json,
    plan_draft_from_json,
    plan_draft_to_json,
)
from domain.plans.service import (
    PlanDraftConflict,
    PlanPersistenceService,
    PlanReadService,
)
from storage.db import Database

BACKEND_ROOT = Path(__file__).resolve().parents[1]

#: Stage 4 领域模块不得依赖的框架（stage4.md §5.2；``domain/__init__`` 的同一约束）。
FORBIDDEN_DOMAIN_IMPORTS = (
    "fastapi",
    "langgraph",
    "langchain",
    "langchain_openai",
    "openai",
    "pydantic_ai",
)

STARTS_ON = date(2026, 6, 1)
CREATED_AT = "2026-06-01T09:00:00+08:00"
ACTIVE_CONTENT = {
    "goal": "维持",
    "starts_on": "2026-05-01",
    "explanation": "当前正式启用的计划",
    "weekly_frequency": 1,
    "training_days": [
        {
            "scheduled_on": "2026-05-01",
            "exercises": [
                {
                    "exercise_id": "pull-up",
                    "sets": 3,
                    "prescription": {
                        "type": "bodyweight_reps",
                        "reps_min": 8,
                        "reps_max": 12,
                        "progression_note": None,
                    },
                }
            ],
        }
    ],
}


# ---------- 固定事实构造（纯领域对象，不读库、不调模型） ----------


def _draft(*, goal: str = "增肌", explanation: str = "每周一练") -> PlanDraft:
    """一个结构合法的单训练日计划草案（训练日数量恰等于每周训练次数 1）。"""
    day = TrainingDay(
        scheduled_on=STARTS_ON,
        exercises=(
            PlannedExercise(
                exercise_id="pull-up",
                sets=3,
                prescription=BodyweightRepsPrescription(
                    type="bodyweight_reps", reps_min=8, reps_max=12
                ),
            ),
        ),
    )
    return PlanDraft(
        goal=goal,
        starts_on=STARTS_ON,
        explanation=explanation,
        weekly_frequency=1,
        training_days=(day,),
    )


def _evaluation(
    *, passed: bool, revision_count: int = 0, warnings: tuple[str, ...] = ()
) -> EvaluationResult:
    """确定性层与目标匹配同通过/同失败，保证 ``passed`` 恰为两个硬门槛的合取。"""
    return EvaluationResult(
        passed=passed,
        deterministic=DeterministicResult(
            passed=passed,
            failures=()
            if passed
            else (RuleFailure(code="unknown_exercise", message="动作不在目录内"),),
        ),
        rubric=RubricResult(
            goal_alignment=RubricVerdict(passed=passed, reason="目标匹配"),
            schedule_reasonableness=RubricVerdict(passed=True, reason="安排合理"),
            explanation_quality=RubricVerdict(passed=True, reason="解释质量"),
        ),
        blocking_failures=() if passed else ("确定性校验未通过",),
        warnings=warnings,
        revision_count=revision_count,
    )


# ---------- 测试库装配（直接 SQL 只用于预置事实） ----------


async def _migrated(path: Path) -> Database:
    db = Database(path)
    await db.open()
    await db.migrate()
    return db


async def _insert_plan(
    db: Database,
    *,
    plan_id: int,
    version: int,
    status: str,
    content: object = None,
    evaluator_result: object | None = None,
    created_at: str = CREATED_AT,
    confirmed_at: str | None = None,
    archived_at: str | None = None,
) -> None:
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO plans (id, version, status, source_plan_id, structured_content,"
            " evaluator_result, created_at, confirmed_at, archived_at)"
            " VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?)",
            (
                plan_id,
                version,
                status,
                json.dumps(ACTIVE_CONTENT if content is None else content),
                None if evaluator_result is None else json.dumps(evaluator_result),
                created_at,
                confirmed_at,
                archived_at,
            ),
        )


async def _active_rows(db: Database) -> list[dict[str, object]]:
    async def op(conn):
        async with conn.execute(
            "SELECT id, status, version, structured_content, confirmed_at FROM plans"
            " WHERE status = 'active'"
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    return await db.under_lock(op)


async def _count(db: Database, status: str) -> int:
    async def op(conn):
        async with conn.execute(
            "SELECT COUNT(*) FROM plans WHERE status = ?", (status,)
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0])

    return await db.under_lock(op)


async def _assert_active_unchanged(db: Database, before: list[dict[str, object]]) -> None:
    """§9.2：原 active 的 id／状态／版本／内容／确认时间与行数逐字段不变。"""
    after = await _active_rows(db)
    assert after == before
    assert len(after) <= 1


# ---------- §8.6 首次生成与二次失败 ----------


async def test_passing_first_generation_writes_draft_with_next_version(
    tmp_path: Path,
) -> None:
    """无原 draft + 通过：插入一条 draft，版本取 max(version)+1，保存统一计划与 Evaluator JSON。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        await _insert_plan(db, plan_id=1, version=1, status="active")
        before = await _active_rows(db)
        service = PlanPersistenceService(db)
        assert await service.get_unique_draft() is None

        draft = _draft(goal="增肌")
        evaluation = _evaluation(passed=True)
        written = await service.persist_plan_result(
            draft,
            evaluation,
            existing_draft_id=None,
            created_at=CREATED_AT,
        )

        assert written.status == "draft"
        assert written.version == 2
        assert written.source_plan_id is None
        assert written.confirmed_at is None and written.archived_at is None
        assert written.structured_content == json.loads(plan_draft_to_json(draft))
        assert plan_draft_from_json(json.dumps(written.structured_content)) == draft
        assert written.evaluator_result == json.loads(
            json.dumps(evaluation.model_dump(mode="json"))
        )
        assert evaluation_result_from_json(json.dumps(written.evaluator_result)) == evaluation
        assert await _count(db, "draft") == 1
        await _assert_active_unchanged(db, before)
    finally:
        await db.close()


async def test_second_blocking_failure_without_draft_writes_rejected_with_null_timestamps(
    tmp_path: Path,
) -> None:
    """无原 draft + 二次阻断失败：写一条 rejected，两个时间字段保持 NULL，不产生 draft。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        await _insert_plan(db, plan_id=1, version=1, status="active")
        before = await _active_rows(db)
        service = PlanPersistenceService(db)

        draft = _draft(explanation="二次失败候选")
        evaluation = _evaluation(passed=False, revision_count=1)
        written = await service.persist_plan_result(
            draft,
            evaluation,
            existing_draft_id=None,
            created_at=CREATED_AT,
        )

        assert written.status == "rejected"
        assert written.version == 2
        assert written.confirmed_at is None
        assert written.archived_at is None
        assert written.structured_content == json.loads(plan_draft_to_json(draft))
        assert written.evaluator_result is not None
        assert plan_draft_from_json(json.dumps(written.structured_content)) == draft
        assert evaluation_result_from_json(json.dumps(written.evaluator_result)) == evaluation
        assert await _count(db, "draft") == 0
        assert await _count(db, "rejected") == 1
        await _assert_active_unchanged(db, before)
    finally:
        await db.close()


async def test_rejected_row_is_hidden_from_draft_and_active_reads(tmp_path: Path) -> None:
    """rejected 进版本列表与按 ID 查询，但不进 draft 列表、active 查询（日历 active 计划同源）。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        await _insert_plan(db, plan_id=1, version=1, status="active")
        before = await _active_rows(db)
        service = PlanPersistenceService(db)
        written = await service.persist_plan_result(
            _draft(),
            _evaluation(passed=False),
            existing_draft_id=None,
            created_at=CREATED_AT,
        )

        reader = PlanReadService(db)
        assert await reader.list_drafts() == ()
        active = await reader.get_active()
        assert active is not None and active.id == 1 and active.status == "active"
        assert [plan.version for plan in await reader.list_versions()] == [1, 2]
        assert [plan.status for plan in await reader.list_versions()] == [
            "active",
            "rejected",
        ]
        fetched = await reader.get_by_id(written.id)
        assert fetched is not None and fetched.status == "rejected"
        assert await reader.list_sessions(written.id) == ()
        await _assert_active_unchanged(db, before)
    finally:
        await db.close()


# ---------- §8.6 已有 draft：普通请求与显式重新生成 ----------


async def test_existing_draft_normal_request_returns_it_without_new_rows(
    tmp_path: Path,
) -> None:
    """已有 draft 的普通请求：只读返回该 draft，不新增 draft/rejected（无模型调用）。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        await _insert_plan(db, plan_id=1, version=1, status="active")
        await _insert_plan(
            db, plan_id=2, version=2, status="draft", content=ACTIVE_CONTENT
        )
        before = await _active_rows(db)
        service = PlanPersistenceService(db)

        existing = await service.get_unique_draft()
        assert existing is not None and existing.id == 2 and existing.version == 2
        assert await _count(db, "draft") == 1
        assert await _count(db, "rejected") == 0
        assert len(await PlanReadService(db).list_versions()) == 2
        await _assert_active_unchanged(db, before)
    finally:
        await db.close()


async def test_explicit_regeneration_replaces_same_id_and_version(tmp_path: Path) -> None:
    """显式重新生成通过：同一 draft 的 id/version 不变，只替换内容与 Evaluator 结果。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        await _insert_plan(db, plan_id=1, version=1, status="active")
        await _insert_plan(
            db, plan_id=2, version=2, status="draft", content=ACTIVE_CONTENT
        )
        before = await _active_rows(db)
        service = PlanPersistenceService(db)

        regenerated = _draft(explanation="重新生成的候选")
        evaluation = _evaluation(passed=True, revision_count=1)
        written = await service.persist_plan_result(
            regenerated,
            evaluation,
            existing_draft_id=2,
            created_at=CREATED_AT,
        )

        assert written.id == 2
        assert written.version == 2
        assert written.status == "draft"
        assert written.structured_content == json.loads(plan_draft_to_json(regenerated))
        assert written.structured_content != ACTIVE_CONTENT
        assert evaluation_result_from_json(json.dumps(written.evaluator_result)) == evaluation
        assert await _count(db, "draft") == 1
        assert await _count(db, "rejected") == 0
        await _assert_active_unchanged(db, before)
    finally:
        await db.close()


async def test_regeneration_blocking_failure_keeps_original_draft_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """显式重新生成二次失败：原 draft 内容/版本/Evaluator 结果不变，不写第二 draft 或 rejected。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        await _insert_plan(db, plan_id=1, version=1, status="active")
        original_evaluation = json.loads(
            json.dumps(_evaluation(passed=True, revision_count=0).model_dump(mode="json"))
        )
        await _insert_plan(
            db,
            plan_id=2,
            version=2,
            status="draft",
            content=ACTIVE_CONTENT,
            evaluator_result=original_evaluation,
        )
        before = await _active_rows(db)
        service = PlanPersistenceService(db)

        kept = await service.persist_plan_result(
            _draft(explanation="失败的重新生成候选"),
            _evaluation(passed=False, revision_count=1),
            existing_draft_id=2,
            created_at=CREATED_AT,
        )

        assert kept.id == 2 and kept.version == 2 and kept.status == "draft"
        assert kept.structured_content == ACTIVE_CONTENT
        assert kept.evaluator_result == original_evaluation
        assert await _count(db, "draft") == 1
        assert await _count(db, "rejected") == 0
        assert len(await PlanReadService(db).list_versions()) == 2
        await _assert_active_unchanged(db, before)
    finally:
        await db.close()


# ---------- §9 冲突、回滚与原 active 保护 ----------


async def test_replace_miss_reports_explicit_conflict(tmp_path: Path) -> None:
    """条件更新未命中（目标不再存在或不再是 draft）：明确冲突，不覆盖、不新增任何行。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        await _insert_plan(db, plan_id=1, version=1, status="active")
        before = await _active_rows(db)
        service = PlanPersistenceService(db)

        with pytest.raises(PlanDraftConflict):
            await service.persist_plan_result(
                _draft(),
                _evaluation(passed=True),
                existing_draft_id=999,
                created_at=CREATED_AT,
            )
        with pytest.raises(PlanDraftConflict):
            # 目标行存在但不是 draft（已启用）：条件 UPDATE 命中 0 行，不得改回 draft。
            await service.persist_plan_result(
                _draft(),
                _evaluation(passed=True),
                existing_draft_id=1,
                created_at=CREATED_AT,
            )

        assert len(await PlanReadService(db).list_versions()) == 1
        assert (await PlanRepo(db).read_by_id(1)).status == "active"
        await _assert_active_unchanged(db, before)
    finally:
        await db.close()


async def test_failed_write_rolls_back_completely(tmp_path: Path) -> None:
    """写入失败（single-draft 唯一索引兜底）整段回滚：不新增行、原 draft 与 active 逐字段不变。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        await _insert_plan(db, plan_id=1, version=1, status="active")
        await _insert_plan(
            db, plan_id=2, version=2, status="draft", content=ACTIVE_CONTENT
        )
        before_active = await _active_rows(db)
        before_draft = await PlanRepo(db).read_by_id(2)
        before_versions = len(await PlanReadService(db).list_versions())
        service = PlanPersistenceService(db)

        with pytest.raises(sqlite3.IntegrityError):
            # State 认为无 draft，但库里已有一条：插入被唯一索引拒绝，事务整体回滚。
            await service.persist_plan_result(
                _draft(),
                _evaluation(passed=True),
                existing_draft_id=None,
                created_at=CREATED_AT,
            )

        assert len(await PlanReadService(db).list_versions()) == before_versions
        assert await PlanRepo(db).read_by_id(2) == before_draft
        assert await _count(db, "draft") == 1
        assert await _count(db, "rejected") == 0
        await _assert_active_unchanged(db, before_active)
    finally:
        await db.close()


def test_persistence_service_depends_on_no_model_or_graph_sdk() -> None:
    """持久化服务不导入模型/Graph SDK：模型调用只能在业务事务之外（stage4.md §5.2、§9.3）。"""
    source = (BACKEND_ROOT / "domain" / "plans" / "service.py").read_text(encoding="utf-8")
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert not roots.intersection(FORBIDDEN_DOMAIN_IMPORTS)
