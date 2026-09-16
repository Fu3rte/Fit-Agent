"""Stage 1 子任务 03 §7：训练记录 domain/records —— 训练与组的 CRUD、逐项校验与关联日程。

对照 03 清单：用 ``workout_sessions``／``workout_sets`` 替换旧修订链模型、组类型三态
（work／warmup／assisted）、彻底移除 RIR 与辅助次数与修订草稿字段、一次训练及其组的原子新增、
训练及组的查询修改删除、逐项校验（日期／动作 ID／负重口径／重量／次数／组数／组类型）、可空
``plan_session_id``（NULL 即额外训练）、自动关联仅限「当天恰好一个未完成日程」，以及事务回滚、
关联歧义与重复完成测试。

测试只使用 pytest tmp_path 下的独立临时库，不 import tests.support（它依赖已移除的 pydantic_ai）；
计划与日程用直接 SQL 写入（本阶段没有计划写入口，计划创建／激活留到阶段 5）。
"""

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import domain.records.schema as records_schema
from domain.actions.rules import RecordLoadMismatch, UnknownExercise
from domain.records.repo import WorkoutRecordsRepo
from domain.records.rules import InvalidRecordFact
from domain.records.schema import SetType, WorkoutSetInput
from domain.records.service import (
    PlanSessionLinkAmbiguous,
    PlanSessionLinkUnavailable,
    WorkoutRecordNotFound,
    WorkoutRecordsService,
)
from storage.db import Database

#: 旧修订链模型的公开名字：整体重写后不得再出现在 records.schema 里。
REMOVED_SCHEMA_NAMES = (
    "ASSISTANCE_VALUES",
    "LOAD_UNITS",
    "RECORD_DRAFT_SCHEMA_VERSION",
    "TIME_PRECISIONS",
    "Assistance",
    "RawLoad",
    "LoadUnit",
    "LoadComparisonKey",
    "RecordDraftPayload",
    "DraftExerciseLog",
    "ExerciseLogFacts",
    "SetFacts",
    "TimePrecision",
    "RecordRevisionStatus",
    "record_draft_from_json",
    "record_draft_to_json",
    "parse_started_at",
)

#: 旧模型里承载 RIR／辅助／质量的列名：新版两张表都不许有。
# 旧模型删掉且 Stage 2 不恢复的列。`duration_seconds` 不在其中：002（Stage 2 §3）按已拍口径
# 重新新增该列（计时动作的单组秒数），与旧模型的同名旧列不是同一语义。
REMOVED_COLUMN_NAMES = {
    "rir",
    "assistance",
    "assisted_reps",
    "quality_text",
    "raw_load",
    "load_kg_key",
    "revision_id",
}

DAY = date(2026, 6, 3)


async def _migrated(path: Path) -> Database:
    db = Database(path)
    await db.open()
    await db.migrate()
    return db


async def _seed_plan_session(
    db: Database,
    *,
    scheduled_on: date,
    version: int = 1,
    cancelled_at: str | None = None,
) -> int:
    """直接 SQL 写入一个 draft 计划与一条日程，返回 plan_session 身份。"""
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "INSERT INTO plans (version, status, structured_content, created_at)"
            " VALUES (?, 'draft', '{}', ?)",
            (version, "2026-06-01T08:00:00+08:00"),
        )
        try:
            plan_id = int(cursor.lastrowid or 0)
        finally:
            await cursor.close()
        cursor = await conn.execute(
            "INSERT INTO plan_sessions (plan_id, scheduled_on, cancelled_at) VALUES (?, ?, ?)",
            (plan_id, scheduled_on.isoformat(), cancelled_at),
        )
        try:
            return int(cursor.lastrowid or 0)
        finally:
            await cursor.close()


async def _raw_counts(db: Database) -> tuple[int, int]:
    """裸表计数：(workout_sessions 行数, workout_sets 行数)。"""

    async def op(conn):
        async with conn.execute("SELECT COUNT(*) FROM workout_sessions") as cursor:
            sessions = int((await cursor.fetchone())[0])
        async with conn.execute("SELECT COUNT(*) FROM workout_sets") as cursor:
            sets = int((await cursor.fetchone())[0])
        return sessions, sets

    return await db.under_lock(op)


async def _raw_session(db: Database, session_id: int) -> dict[str, object] | None:
    async def op(conn):
        async with conn.execute(
            "SELECT id, performed_on, plan_session_id FROM workout_sessions WHERE id = ?",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None else dict(row)

    return await db.under_lock(op)


async def _raw_set_rows(db: Database, session_id: int) -> list[dict[str, object]]:
    async def op(conn):
        async with conn.execute(
            "SELECT id, exercise_id, set_no, set_type, load_convention, weight_kg, reps"
            " FROM workout_sets WHERE workout_session_id = ? ORDER BY id",
            (session_id,),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    return await db.under_lock(op)


async def _table_columns(db: Database, table: str) -> list[str]:
    async def op(conn):
        async with conn.execute(
            "SELECT name FROM pragma_table_info(?)", (table,)
        ) as cursor:
            return [str(row[0]) for row in await cursor.fetchall()]

    return await db.under_lock(op)


def _squat(
    set_no: int,
    *,
    reps: int = 5,
    weight_kg: float | None = 60.0,
    set_type: SetType = "work",
) -> WorkoutSetInput:
    """杠铃背蹲（外加负重型，目录口径 barbell_includes_bar_total）。"""
    return WorkoutSetInput(
        exercise_id="barbell-back-squat",
        set_no=set_no,
        reps=reps,
        set_type=set_type,
        load_convention="barbell_includes_bar_total",
        weight_kg=weight_kg,
    )


def _pull_up(set_no: int, *, reps: int = 8) -> WorkoutSetInput:
    """自重引体向上（reps_bodyweight：无负重口径、无重量）。"""
    return WorkoutSetInput(
        exercise_id="pull-up", set_no=set_no, reps=reps, set_type="work"
    )


# ---------- CRUD 往返 ----------


async def test_create_read_update_delete_round_trip(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        service = WorkoutRecordsService(db)
        created = await service.create(
            date(2026, 6, 1),
            (
                _squat(1, reps=8, weight_kg=60.0, set_type="warmup"),
                _squat(2, reps=5, weight_kg=80.0),
                _squat(3, reps=5, weight_kg=80.0),
                _pull_up(1, reps=10),
                _pull_up(2, reps=9),
            ),
        )
        assert created.performed_on == date(2026, 6, 1)
        assert created.plan_session_id is None
        assert [
            (fact.exercise_id, fact.set_no, fact.set_type, fact.reps, fact.weight_kg)
            for fact in created.sets
        ] == [
            ("barbell-back-squat", 1, "warmup", 8, 60.0),
            ("barbell-back-squat", 2, "work", 5, 80.0),
            ("barbell-back-squat", 3, "work", 5, 80.0),
            ("pull-up", 1, "work", 10, None),
            ("pull-up", 2, "work", 9, None),
        ]
        assert created.sets[3].load_convention is None
        assert created.sets[0].load_convention == "barbell_includes_bar_total"

        assert await service.get(created.id) == created
        assert await service.list_all() == (created,)

        updated = await service.update(
            created.id,
            date(2026, 6, 2),
            (_squat(1, reps=3, weight_kg=90.0), _pull_up(1)),
        )
        assert updated.id == created.id
        assert updated.performed_on == date(2026, 6, 2)
        assert [fact.weight_kg for fact in updated.sets] == [90.0, None]
        assert await service.get(created.id) == updated
        # 整条覆盖：旧组行被重建，不残留第 2、3 组
        assert [row["set_no"] for row in await _raw_set_rows(db, created.id)] == [1, 1]

        await service.delete(created.id)
        assert await service.get(created.id) is None
        assert await service.list_all() == ()
        assert await _raw_session(db, created.id) is None
        assert await _raw_set_rows(db, created.id) == []
        assert await _raw_counts(db) == (0, 0)
    finally:
        await db.close()


async def test_create_is_atomic_when_a_set_row_is_rejected(tmp_path: Path) -> None:
    """绕过 rules 直接经写事务写一条越界组行：训练行不得留下半条，事务整体回滚。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        repo = WorkoutRecordsRepo(db)
        bad = _squat(1, reps=999)
        with pytest.raises(sqlite3.IntegrityError):
            async with db.transaction() as conn:
                await repo.create_in_transaction(conn, date(2026, 6, 1), None, (bad,))
        assert await _raw_counts(db) == (0, 0)
        # 事务已完整回滚：连接可继续使用（计数语句本身也不在残留事务里）
        assert await WorkoutRecordsService(db).list_all() == ()
    finally:
        await db.close()


# ---------- 校验逐项拒绝 ----------


async def test_performed_on_must_be_a_date_object(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        service = WorkoutRecordsService(db)
        for value in (
            "2026-06-01",
            datetime(2026, 6, 1, tzinfo=timezone(timedelta(hours=8))),
            None,
        ):
            with pytest.raises(InvalidRecordFact):
                await service.create(value, (_squat(1),))  # type: ignore[arg-type]
        assert await service.list_all() == ()
    finally:
        await db.close()


async def test_exercise_id_must_exist_and_be_non_empty_text(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        service = WorkoutRecordsService(db)
        unknown = WorkoutSetInput(
            exercise_id="not-in-catalog",
            set_no=1,
            reps=5,
            set_type="work",
            load_convention="barbell_includes_bar_total",
            weight_kg=60.0,
        )
        with pytest.raises(UnknownExercise):
            await service.create(DAY, (unknown,))
        for value in ("", "   ", None, 7):
            blank = WorkoutSetInput(
                exercise_id=value,  # type: ignore[arg-type]
                set_no=1,
                reps=5,
                set_type="work",
                load_convention="barbell_includes_bar_total",
                weight_kg=60.0,
            )
            with pytest.raises(InvalidRecordFact):
                await service.create(DAY, (blank,))
        assert await _raw_counts(db) == (0, 0)
    finally:
        await db.close()


async def test_load_convention_must_match_catalog(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        service = WorkoutRecordsService(db)
        # 目录是 barbell_includes_bar_total，给别的口径即拒绝
        mismatch = WorkoutSetInput(
            exercise_id="barbell-back-squat",
            set_no=1,
            reps=5,
            set_type="work",
            load_convention="dumbbell_per_hand",
            weight_kg=60.0,
        )
        with pytest.raises(RecordLoadMismatch):
            await service.create(DAY, (mismatch,))
        # 外加负重型必须给出与目录一致的口径（不能整组无口径无重量）
        without_load = WorkoutSetInput(
            exercise_id="barbell-back-squat", set_no=1, reps=5, set_type="work"
        )
        with pytest.raises(RecordLoadMismatch):
            await service.create(DAY, (without_load,))
        # 有口径无重量（或反之）连事实都不完整：同现同隐
        with pytest.raises(InvalidRecordFact):
            await service.create(DAY, (_squat(1, weight_kg=None),))
        assert await _raw_counts(db) == (0, 0)
    finally:
        await db.close()


async def test_bodyweight_set_must_not_carry_load(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        service = WorkoutRecordsService(db)
        # 自重型带口径与重量：口径与目录不符
        with_load = WorkoutSetInput(
            exercise_id="pull-up",
            set_no=1,
            reps=8,
            set_type="work",
            load_convention="barbell_includes_bar_total",
            weight_kg=10.0,
        )
        with pytest.raises(RecordLoadMismatch):
            await service.create(DAY, (with_load,))
        # 自重型只带重量、无口径：重量与负重口径同现同隐被拒
        weight_only = WorkoutSetInput(
            exercise_id="pull-up", set_no=1, reps=8, set_type="work", weight_kg=10.0
        )
        with pytest.raises(InvalidRecordFact):
            await service.create(DAY, (weight_only,))
        # 无负重才是自重型唯一合法形态
        created = await service.create(DAY, (_pull_up(1),))
        assert created.sets[0].weight_kg is None
        assert created.sets[0].load_convention is None
    finally:
        await db.close()


async def test_weight_range_and_precision(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        service = WorkoutRecordsService(db)
        for weight in (-0.5, 1000.1, 62.55, float("nan"), float("inf"), "60", True, 10**400):
            with pytest.raises(InvalidRecordFact):
                await service.create(DAY, (_squat(1, weight_kg=weight),))  # type: ignore[arg-type]
        assert await _raw_counts(db) == (0, 0)
        for weight in (0.0, 62.5, 1000.0):
            created = await service.create(DAY, (_squat(1, weight_kg=weight),))
            assert created.sets[0].weight_kg == weight
    finally:
        await db.close()


async def test_reps_range_and_type(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        service = WorkoutRecordsService(db)
        for reps in (0, 101, "5", 5.0, True, None):
            with pytest.raises(InvalidRecordFact):
                await service.create(DAY, (_squat(1, reps=reps),))  # type: ignore[arg-type]
        assert await _raw_counts(db) == (0, 0)
        for reps in (1, 100):
            created = await service.create(DAY, (_squat(1, reps=reps),))
            assert created.sets[0].reps == reps
    finally:
        await db.close()


async def test_session_set_count_bounds(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        service = WorkoutRecordsService(db)
        for count in (0, 51):
            with pytest.raises(InvalidRecordFact):
                await service.create(
                    DAY, tuple(_squat(no) for no in range(1, count + 1))
                )
        assert await _raw_counts(db) == (0, 0)
        for count in (1, 50):
            created = await service.create(
                DAY, tuple(_squat(no) for no in range(1, count + 1))
            )
            assert len(created.sets) == count
    finally:
        await db.close()


async def test_set_type_must_be_one_of_three(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        service = WorkoutRecordsService(db)
        for value in ("drop", "", None, "WORK", "work "):
            bad = WorkoutSetInput(
                exercise_id="barbell-back-squat",
                set_no=1,
                reps=5,
                set_type=value,  # type: ignore[arg-type]
                load_convention="barbell_includes_bar_total",
                weight_kg=60.0,
            )
            with pytest.raises(InvalidRecordFact):
                await service.create(DAY, (bad,))
        assert await _raw_counts(db) == (0, 0)
        for value in ("work", "warmup", "assisted"):
            created = await service.create(
                DAY, (_squat(1, set_type=value),),  # type: ignore[arg-type]
            )
            assert created.sets[0].set_type == value
    finally:
        await db.close()


async def test_duplicate_set_number_within_exercise_rejected(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        service = WorkoutRecordsService(db)
        with pytest.raises(InvalidRecordFact):
            await service.create(DAY, (_squat(1), _squat(1, reps=6)))
        # 不同动作各自从 1 开始，不算重复
        created = await service.create(DAY, (_squat(1), _pull_up(1)))
        assert [fact.set_no for fact in created.sets] == [1, 1]
    finally:
        await db.close()


async def test_invalid_set_no_rejected(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        service = WorkoutRecordsService(db)
        for set_no in (0, 51, "1", 1.0, True):
            bad = WorkoutSetInput(
                exercise_id="barbell-back-squat",
                set_no=set_no,  # type: ignore[arg-type]
                reps=5,
                set_type="work",
                load_convention="barbell_includes_bar_total",
                weight_kg=60.0,
            )
            with pytest.raises(InvalidRecordFact):
                await service.create(DAY, (bad,))
        assert await _raw_counts(db) == (0, 0)
    finally:
        await db.close()


# ---------- 可空 plan_session_id 与额外训练 ----------


async def test_absent_plan_session_means_extra_training(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        service = WorkoutRecordsService(db)
        created = await service.create(DAY, (_squat(1),))
        assert created.plan_session_id is None
        raw = await _raw_session(db, created.id)
        assert raw is not None
        assert raw["plan_session_id"] is None
    finally:
        await db.close()


async def test_explicit_plan_session_is_linked(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        service = WorkoutRecordsService(db)
        planned = await _seed_plan_session(db, scheduled_on=DAY)
        created = await service.create(DAY, (_squat(1),), plan_session_id=planned)
        assert created.plan_session_id == planned
        assert await service.list_unfinished_plan_sessions(DAY) == ()
        with pytest.raises(PlanSessionLinkUnavailable):
            await service.create(DAY, (_squat(1),), plan_session_id=999)
    finally:
        await db.close()


# ---------- 自动关联仅限「当天恰好一个未完成日程」 ----------


async def test_auto_link_links_the_single_candidate(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        service = WorkoutRecordsService(db)
        planned = await _seed_plan_session(db, scheduled_on=DAY)
        assert await service.auto_plan_session_id(DAY) == planned
        created = await service.create(DAY, (_squat(1),), auto_link=True)
        assert created.plan_session_id == planned
        assert await service.auto_plan_session_id(DAY) is None
    finally:
        await db.close()


async def test_auto_link_refuses_without_candidate(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        service = WorkoutRecordsService(db)
        assert await service.auto_plan_session_id(DAY) is None
        assert await service.list_unfinished_plan_sessions(DAY) == ()
        with pytest.raises(PlanSessionLinkAmbiguous):
            await service.create(DAY, (_squat(1),), auto_link=True)
        assert await _raw_counts(db) == (0, 0)
    finally:
        await db.close()


async def test_update_with_auto_link_sees_no_unfinished_candidate(tmp_path: Path) -> None:
    """已关联日程即已完成（总结 §9），update(auto_link=True) 因此看到 0 个未完成候选并拒绝。

    编辑既有训练时调用方必须显式传 plan_session_id（04 表单接线约束）；显式传回自身已关联的
    日程不算冲突。
    """
    db = await _migrated(tmp_path / "x.db")
    try:
        service = WorkoutRecordsService(db)
        planned = await _seed_plan_session(db, scheduled_on=DAY)
        created = await service.create(DAY, (_squat(1),), plan_session_id=planned)
        with pytest.raises(PlanSessionLinkAmbiguous):
            await service.update(created.id, DAY, (_squat(1, reps=6),), auto_link=True)
        assert await _raw_counts(db) == (1, 1)
        kept = await service.update(
            created.id, DAY, (_squat(1, reps=6),), plan_session_id=planned
        )
        assert kept.plan_session_id == planned
    finally:
        await db.close()


async def test_auto_link_refuses_with_several_candidates(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        service = WorkoutRecordsService(db)
        first = await _seed_plan_session(db, scheduled_on=DAY, version=1)
        second = await _seed_plan_session(db, scheduled_on=DAY, version=2)
        candidates = await service.list_unfinished_plan_sessions(DAY)
        assert [item.id for item in candidates] == [first, second]
        assert await service.auto_plan_session_id(DAY) is None
        with pytest.raises(PlanSessionLinkAmbiguous):
            await service.create(DAY, (_squat(1),), auto_link=True)
        assert await _raw_counts(db) == (0, 0)
    finally:
        await db.close()


async def test_cancelled_plan_session_is_not_available(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        service = WorkoutRecordsService(db)
        cancelled = await _seed_plan_session(
            db, scheduled_on=DAY, cancelled_at="2026-06-02T08:00:00+08:00"
        )
        assert await service.list_unfinished_plan_sessions(DAY) == ()
        with pytest.raises(PlanSessionLinkUnavailable):
            await service.create(DAY, (_squat(1),), plan_session_id=cancelled)
        with pytest.raises(PlanSessionLinkAmbiguous):
            await service.create(DAY, (_squat(1),), auto_link=True)
        assert await _raw_counts(db) == (0, 0)
    finally:
        await db.close()


async def test_plan_session_can_be_completed_only_once(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        service = WorkoutRecordsService(db)
        planned = await _seed_plan_session(db, scheduled_on=DAY)
        created = await service.create(DAY, (_squat(1),), plan_session_id=planned)
        with pytest.raises(PlanSessionLinkUnavailable):
            await service.create(DAY, (_squat(1),), plan_session_id=planned)
        assert await _raw_counts(db) == (1, 1)
        # 库内 UNIQUE 仍成立：绕过领域层直接插第二条关联即失败
        with pytest.raises(sqlite3.IntegrityError):
            async with db.transaction() as conn:
                await conn.execute(
                    "INSERT INTO workout_sessions (performed_on, plan_session_id)"
                    " VALUES (?, ?)",
                    (DAY.isoformat(), planned),
                )
        # 同一条训练改到自己已关联的日程不算冲突
        kept = await service.update(
            created.id, DAY, (_squat(1, reps=6),), plan_session_id=planned
        )
        assert kept.plan_session_id == planned
        # 解除关联后该日程重新可选；改到已取消/不存在日程仍被拒
        unlinked = await service.update(created.id, DAY, (_squat(1),))
        assert unlinked.plan_session_id is None
        assert [item.id for item in await service.list_unfinished_plan_sessions(DAY)] == [
            planned
        ]
        with pytest.raises(PlanSessionLinkUnavailable):
            await service.update(created.id, DAY, (_squat(1),), plan_session_id=999)
        current = await service.get(created.id)
        assert current is not None
        assert current.plan_session_id is None
    finally:
        await db.close()


# ---------- 不存在身份 ----------


async def test_update_and_delete_reject_unknown_identity(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        service = WorkoutRecordsService(db)
        with pytest.raises(WorkoutRecordNotFound):
            await service.update(999, DAY, (_squat(1),))
        with pytest.raises(WorkoutRecordNotFound):
            await service.delete(999)
        assert await service.get(999) is None
    finally:
        await db.close()


# ---------- 旧模型字段不得残留 ----------


async def test_workout_tables_have_no_rir_or_assistance_columns(tmp_path: Path) -> None:
    """列清单：旧模型字段不得残留；002 追加的 duration_seconds 是唯一新增列。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        assert await _table_columns(db, "workout_sets") == [
            "id",
            "workout_session_id",
            "exercise_id",
            "set_no",
            "set_type",
            "load_convention",
            "weight_kg",
            "reps",
            "duration_seconds",
        ]
        assert await _table_columns(db, "workout_sessions") == [
            "id",
            "performed_on",
            "plan_session_id",
        ]
        for table in ("workout_sets", "workout_sessions"):
            assert REMOVED_COLUMN_NAMES.isdisjoint(set(await _table_columns(db, table)))
    finally:
        await db.close()


def test_legacy_revision_chain_types_are_removed() -> None:
    for name in REMOVED_SCHEMA_NAMES:
        assert not hasattr(records_schema, name), name
    assert records_schema.SET_TYPES == ("work", "warmup", "assisted")
