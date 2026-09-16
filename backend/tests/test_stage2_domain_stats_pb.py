"""Stage 2 Subtask 03 §B–§D（后端部分）：有效工作组与三类 PB 的确定性现算。

依据：``refactor-log/stage2.md`` §6／§11.2、``refactor-log/stage2-subTasks/03-stats-and-personal-bests.md``、
``LANGGRAPH_REFACTOR_PLAN.md`` §6.2、``Fit-Agent-LangGraph-重构讨论总结.md`` §7.1。

覆盖：热身／辅助／不完整组不刷新 PB、三类 PB 各自的规则（重量不含次数、外加重量按同重量分组次数、
纯自重单组最大次数不累加、计时最长秒数）、哑铃沿用单手记录值不乘 2、纯自重引体与负重引体不混算、
PB 来源训练／组序号／日期与并列来源排序、修改与删除后立即重算、PB 不落表（查询前后 Schema 不变、
无 ``personal_bests`` 表与有效工作组 View）、输出只有三类 PB（无容量 PB 与估算 1RM）。

不完整组（缺次数／缺重量／缺时长／时长为 0）领域层写不进去——必填与互斥字段由唯一规则拦下——
故这几条用直接 SQL 写入，其余用例一律经 ``WorkoutRecordsService`` 走真实写入路径。
测试只使用 pytest ``tmp_path`` 下的独立临时库。
"""

from collections.abc import Sequence
from dataclasses import fields
from datetime import date
from pathlib import Path
from typing import Any

from domain.records.schema import SetType, WorkoutSetInput
from domain.records.service import WorkoutRecordsService
from domain.stats.schema import PERSONAL_BEST_TYPES, PersonalBest, PersonalBestType
from domain.stats.service import StatsService
from storage.db import Database

DAY = date(2026, 6, 3)

SQUAT = "barbell-back-squat"  # 杠铃背蹲：外加重量（杠铃总重）
SQUAT_CONVENTION = "barbell_includes_bar_total"
DUMBBELL_BENCH = "dumbbell-bench-press"  # 哑铃平板卧推：单手重量口径
DUMBBELL_CONVENTION = "dumbbell_per_hand"
PULL_UP = "pull-up"  # 纯自重引体
WEIGHTED_PULL_UP = "weighted-pull-up"  # 独立负重引体：只记外加重量
EXTERNAL_CONVENTION = "external_added_weight"
PLANK = "plank"  # 平板支撑：计时
FRONT_LEVER = "front-lever"  # 前水平：计时


async def _migrated(path: Path) -> Database:
    db = Database(path)
    await db.open()
    await db.migrate()
    return db


async def _pbs(db: Database) -> tuple[PersonalBest, ...]:
    return await StatsService(db).list_personal_bests()


def _find(
    pbs: Sequence[PersonalBest],
    exercise_id: str,
    pb_type: PersonalBestType,
    weight_kg: float | None = None,
) -> PersonalBest:
    """按（动作、PB 类型、适用重量）取唯一一条结果；不存在或重复即测试失败。"""
    matches = [
        pb
        for pb in pbs
        if pb.exercise_id == exercise_id
        and pb.pb_type == pb_type
        and pb.weight_kg == weight_kg
    ]
    assert len(matches) == 1, matches
    return matches[0]


async def _rows(db: Database, sql: str, params: tuple[Any, ...] = ()) -> list[dict]:
    async def op(conn: Any) -> list[dict]:
        async with conn.execute(sql, params) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    return await db.under_lock(op)


async def _schema(db: Database) -> dict[str, set[str]]:
    """库内对象清单（按 sqlite_master 类型分组）：用于核对 PB 现算不落表、不建 View。"""
    schema: dict[str, set[str]] = {}
    for row in await _rows(db, "SELECT type, name FROM sqlite_master"):
        schema.setdefault(str(row["type"]), set()).add(str(row["name"]))
    return schema


async def _raw_session(db: Database, session_id: int, performed_on: date) -> None:
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO workout_sessions (id, performed_on) VALUES (?, ?)",
            (session_id, performed_on.isoformat()),
        )


async def _raw_set(
    db: Database,
    session_id: int,
    exercise_id: str,
    set_no: int,
    *,
    set_type: str = "work",
    load_convention: str | None = None,
    weight_kg: float | None = None,
    reps: int | None = None,
    duration_seconds: int | None = None,
) -> None:
    """绕过领域写入一个训练组：只用于造出领域层拒绝的不完整行。"""
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO workout_sets (workout_session_id, exercise_id, set_no, set_type,"
            " load_convention, weight_kg, reps, duration_seconds)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                exercise_id,
                set_no,
                set_type,
                load_convention,
                weight_kg,
                reps,
                duration_seconds,
            ),
        )


def _squat(
    set_no: int, weight_kg: float, reps: int, set_type: SetType = "work"
) -> WorkoutSetInput:
    return WorkoutSetInput(
        exercise_id=SQUAT,
        set_no=set_no,
        reps=reps,
        set_type=set_type,
        load_convention=SQUAT_CONVENTION,
        weight_kg=weight_kg,
    )


def _dumbbell_bench(
    set_no: int, weight_kg: float, reps: int, set_type: SetType = "work"
) -> WorkoutSetInput:
    return WorkoutSetInput(
        exercise_id=DUMBBELL_BENCH,
        set_no=set_no,
        reps=reps,
        set_type=set_type,
        load_convention=DUMBBELL_CONVENTION,
        weight_kg=weight_kg,
    )


def _weighted_pull_up(
    set_no: int, weight_kg: float, reps: int, set_type: SetType = "work"
) -> WorkoutSetInput:
    return WorkoutSetInput(
        exercise_id=WEIGHTED_PULL_UP,
        set_no=set_no,
        reps=reps,
        set_type=set_type,
        load_convention=EXTERNAL_CONVENTION,
        weight_kg=weight_kg,
    )


def _pull_up(set_no: int, reps: int, set_type: SetType = "work") -> WorkoutSetInput:
    return WorkoutSetInput(
        exercise_id=PULL_UP, set_no=set_no, reps=reps, set_type=set_type
    )


def _timed(
    exercise_id: str, set_no: int, duration_seconds: int, set_type: SetType = "work"
) -> WorkoutSetInput:
    return WorkoutSetInput(
        exercise_id=exercise_id,
        set_no=set_no,
        reps=None,
        set_type=set_type,
        duration_seconds=duration_seconds,
    )


# ---------- §B 有效工作组 ----------


async def test_personal_bests_only_count_valid_work_sets(tmp_path: Path) -> None:
    """有效工作组才是 PB 来源：热身组与辅助组即使更重也不入选，次数不参与重量 PB 数值。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        records = WorkoutRecordsService(db)
        session = await records.create(
            DAY,
            (
                _squat(1, 80.0, 20),  # 轻但次数多：不改变重量 PB
                _squat(2, 200.0, 1, "warmup"),  # 热身组不入选
                _squat(3, 150.0, 3, "assisted"),  # 辅助（借力）组不入选
                _squat(4, 120.0, 5),  # 当前最大工作重量
            ),
        )
        pbs = await _pbs(db)
        assert [(pb.pb_type, pb.value, pb.weight_kg, pb.set_no) for pb in pbs] == [
            ("weight_pb", 120.0, 120.0, 4),
            # 外加重量动作的次数按重量分别取：80kg 组 20 次、120kg 组 5 次。
            ("reps_pb", 20, 80.0, 1),
            ("reps_pb", 5, 120.0, 4),
        ]
        weight_pb = pbs[0]
        assert weight_pb.exercise_id == SQUAT
        assert weight_pb.exercise_name == "杠铃背蹲"
        assert weight_pb.load_convention == SQUAT_CONVENTION
        assert weight_pb.workout_session_id == session.id
        assert weight_pb.performed_on == DAY
    finally:
        await db.close()


async def test_incomplete_sets_do_not_refresh_pb(tmp_path: Path) -> None:
    """不完整组不入选：缺次数、缺重量、缺时长、时长为 0 都不刷新 PB（用直接 SQL 造行）。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        records = WorkoutRecordsService(db)
        valid = await records.create(DAY, (_pull_up(1, 7),))

        await _raw_session(db, 99, DAY)
        await _raw_set(db, 99, PULL_UP, 1, reps=None)  # 纯自重缺次数
        await _raw_set(db, 99, WEIGHTED_PULL_UP, 1, reps=5)  # 外加重量缺重量（与口径同时为空）
        await _raw_set(db, 99, PLANK, 1)  # 计时缺时长
        await _raw_set(db, 99, PLANK, 2, duration_seconds=0)  # 计时时长为 0

        pbs = await _pbs(db)
        assert [(pb.exercise_id, pb.pb_type, pb.value) for pb in pbs] == [
            (PULL_UP, "reps_pb", 7)
        ]
        assert pbs[0].workout_session_id == valid.id
    finally:
        await db.close()


# ---------- §C 三类 PB ----------


async def test_reps_pb_groups_external_added_weight_by_weight(tmp_path: Path) -> None:
    """外加重量动作的次数 PB 按相同重量分组：每个重量各一条，多组次数不累加。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        records = WorkoutRecordsService(db)
        await records.create(date(2026, 6, 1), (_weighted_pull_up(1, 10.0, 6),))
        await records.create(date(2026, 6, 5), (_weighted_pull_up(1, 10.0, 8),))
        await records.create(date(2026, 6, 7), (_weighted_pull_up(1, 15.0, 4),))

        pbs = await _pbs(db)
        assert [(pb.pb_type, pb.value, pb.weight_kg) for pb in pbs] == [
            # 重量 PB 只比外加重量（15kg 组 4 次不改变它）。
            ("weight_pb", 15.0, 15.0),
            # 10kg 的两组（6 次 + 8 次）取单组最大 8，不累加成 14。
            ("reps_pb", 8, 10.0),
            ("reps_pb", 4, 15.0),
        ]
        assert {pb.exercise_id for pb in pbs} == {WEIGHTED_PULL_UP}
        assert {pb.load_convention for pb in pbs} == {EXTERNAL_CONVENTION}
        assert _find(pbs, WEIGHTED_PULL_UP, "reps_pb", 10.0).performed_on == date(2026, 6, 5)
        assert _find(pbs, WEIGHTED_PULL_UP, "weight_pb", 15.0).set_no == 1
    finally:
        await db.close()


async def test_bodyweight_reps_pb_is_single_set_max_and_separate_from_weighted_pull_up(
    tmp_path: Path,
) -> None:
    """纯自重引体按单组最大次数计（多组不累加），且与负重引体是两个动作、PB 不混算。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        records = WorkoutRecordsService(db)
        await records.create(DAY, (_pull_up(1, 8), _pull_up(2, 12)))
        await records.create(DAY, (_weighted_pull_up(1, 10.0, 5),))

        pbs = await _pbs(db)
        assert {(pb.exercise_id, pb.pb_type) for pb in pbs} == {
            (PULL_UP, "reps_pb"),
            (WEIGHTED_PULL_UP, "weight_pb"),
            (WEIGHTED_PULL_UP, "reps_pb"),
        }
        bodyweight = _find(pbs, PULL_UP, "reps_pb")
        assert bodyweight.value == 12  # 8 + 12 不累加，取单组最大
        assert bodyweight.set_no == 2
        assert bodyweight.weight_kg is None  # 纯自重没有重量，也不虚构 0kg
        assert bodyweight.load_convention is None
        weighted = _find(pbs, WEIGHTED_PULL_UP, "weight_pb", 10.0)
        assert weighted.value == 10.0  # 负重引体只比外加重量，不含体重
        assert weighted.load_convention == EXTERNAL_CONVENTION
    finally:
        await db.close()


async def test_dumbbell_pb_uses_recorded_per_hand_weight(tmp_path: Path) -> None:
    """哑铃沿用记录的单手重量，不乘 2：记录 20kg/手 时重量 PB 就是 20kg。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        records = WorkoutRecordsService(db)
        await records.create(DAY, (_dumbbell_bench(1, 20.0, 8),))

        pbs = await _pbs(db)
        weight_pb = _find(pbs, DUMBBELL_BENCH, "weight_pb", 20.0)
        assert weight_pb.value == 20.0
        assert weight_pb.load_convention == DUMBBELL_CONVENTION
        assert _find(pbs, DUMBBELL_BENCH, "reps_pb", 20.0).value == 8
        assert 40.0 not in {pb.value for pb in pbs}
    finally:
        await db.close()


async def test_duration_pb_is_the_longest_single_set_of_timed_actions(
    tmp_path: Path,
) -> None:
    """计时动作取最长单组秒数；计时动作不产生重量 PB 或次数 PB。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        records = WorkoutRecordsService(db)
        await records.create(date(2026, 6, 1), (_timed(PLANK, 1, 45), _timed(PLANK, 2, 90)))
        await records.create(date(2026, 6, 2), (_timed(FRONT_LEVER, 1, 30),))

        pbs = await _pbs(db)
        assert [
            (pb.exercise_id, pb.pb_type, pb.value, pb.weight_kg, pb.load_convention)
            for pb in pbs
        ] == [
            (FRONT_LEVER, "duration_pb", 30, None, None),
            (PLANK, "duration_pb", 90, None, None),
        ]
        plank_pb = _find(pbs, PLANK, "duration_pb")
        assert (plank_pb.set_no, plank_pb.performed_on) == (2, date(2026, 6, 1))
        assert _find(pbs, FRONT_LEVER, "duration_pb").performed_on == date(2026, 6, 2)
    finally:
        await db.close()


# ---------- §D 来源稳定性与重算 ----------


async def test_tied_values_prefer_the_earlier_training_date(tmp_path: Path) -> None:
    """并列值取最早来源：日期更早的训练才是 PB 来源，日期用它的 ``performed_on``。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        records = WorkoutRecordsService(db)
        later = await records.create(date(2026, 6, 5), (_squat(1, 100.0, 5),))
        earlier = await records.create(date(2026, 6, 1), (_squat(1, 100.0, 5),))
        assert earlier.id != later.id

        weight_pb = _find(await _pbs(db), SQUAT, "weight_pb", 100.0)
        assert weight_pb.performed_on == date(2026, 6, 1)
        assert weight_pb.workout_session_id == earlier.id
    finally:
        await db.close()


async def test_tied_values_prefer_the_earlier_session_on_the_same_date(
    tmp_path: Path,
) -> None:
    """同一天并列时取训练身份更小者（先写入的那次训练）。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        records = WorkoutRecordsService(db)
        first = await records.create(DAY, (_weighted_pull_up(1, 10.0, 8),))
        second = await records.create(DAY, (_weighted_pull_up(1, 10.0, 8),))

        reps_pb = _find(await _pbs(db), WEIGHTED_PULL_UP, "reps_pb", 10.0)
        assert first.id < second.id
        assert reps_pb.workout_session_id == first.id
        assert reps_pb.performed_on == DAY
    finally:
        await db.close()


async def test_tied_values_prefer_the_lower_set_number(tmp_path: Path) -> None:
    """同一次训练内并列时取组序号更小者。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        records = WorkoutRecordsService(db)
        session = await records.create(DAY, (_pull_up(1, 10), _pull_up(2, 10)))

        reps_pb = _find(await _pbs(db), PULL_UP, "reps_pb")
        assert reps_pb.workout_session_id == session.id
        assert reps_pb.set_no == 1
    finally:
        await db.close()


async def test_update_and_delete_recompute_personal_bests(tmp_path: Path) -> None:
    """修改或删除训练记录后再次查询即最新结果：PB 不落表、无缓存。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        records = WorkoutRecordsService(db)
        session = await records.create(DAY, (_squat(1, 100.0, 5),))
        assert _find(await _pbs(db), SQUAT, "weight_pb", 100.0).value == 100.0

        await records.update(session.id, date(2026, 6, 9), (_squat(1, 70.0, 3),))
        assert [
            (pb.pb_type, pb.value, pb.weight_kg, pb.performed_on)
            for pb in await _pbs(db)
        ] == [
            ("weight_pb", 70.0, 70.0, date(2026, 6, 9)),
            ("reps_pb", 3, 70.0, date(2026, 6, 9)),
        ]

        await records.delete(session.id)
        assert await _pbs(db) == ()
    finally:
        await db.close()


# ---------- §A 只读边界 ----------


async def test_personal_best_query_creates_no_result_table_or_view(tmp_path: Path) -> None:
    """PB 现算不落表：查询前后 Schema 不变，库内既无个人最好成绩表也无有效工作组 View。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        records = WorkoutRecordsService(db)
        await records.create(DAY, (_squat(1, 100.0, 5),))

        before = await _schema(db)
        assert await _pbs(db) != ()
        assert await _schema(db) == before
        assert "personal_bests" not in before.get("table", set())
        assert before.get("view", set()) == set()
    finally:
        await db.close()


def test_personal_best_output_has_only_three_types_and_source_facts() -> None:
    """输出只有三类 PB 与来源事实：没有容量 PB、估算 1RM 或统计结果字段。"""
    assert PERSONAL_BEST_TYPES == ("weight_pb", "reps_pb", "duration_pb")
    assert {field.name for field in fields(PersonalBest)} == {
        "exercise_id",
        "exercise_name",
        "pb_type",
        "value",
        "load_convention",
        "weight_kg",
        "workout_session_id",
        "set_no",
        "performed_on",
    }
