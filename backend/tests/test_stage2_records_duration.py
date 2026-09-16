"""Stage 2 Subtask 02 §B／§C（后端部分）：三种记录口径的训练组 CRUD 与字段校验。

依据：``refactor-log/stage2.md`` §4／§11.1、``refactor-log/stage2-subTasks/02-migration-and-records.md``
§B、``LANGGRAPH_REFACTOR_PLAN.md`` §5.4／§6.1、``Fit-Agent-LangGraph-重构讨论总结.md`` §3.1／§9。

覆盖：外加重量／纯自重／计时三类动作的必填与互斥字段（唯一规则实现）、``duration_seconds``
为 0／负数／非整数时被拒、无业务上限（超大整数仍可写入并被读回）、计时组的新增／读取／整条覆盖
修改／删除、外加重量口径 ``external_added_weight`` 与负重引体 5kg 最小加重、以及 pull-up 与
weighted-pull-up 是两个身份（不共享口径与加重单位）。

domain 测试只使用 pytest ``tmp_path`` 下的独立临时库；API 测试用 ``TestClient`` 走真实回环
``base_url``（回环守卫要求），业务日期由 ``dependency_overrides`` 固定。
"""

import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import current_business_date
from domain.actions.rules import RecordLoadMismatch
from domain.records.rules import InvalidRecordFact
from domain.records.schema import WorkoutSetInput
from domain.records.service import WorkoutRecordsService
from storage.db import Database

DAY = date(2026, 6, 3)

#: 计时动作（plank：平板支撑，time）与独立负重引体（external_added_weight）。
PLANK = "plank"
WEIGHTED_PULL_UP = "weighted-pull-up"
WEIGHTED_CONVENTION = "external_added_weight"
#: 纯自重动作，与负重引体是两个身份。
PULL_UP = "pull-up"


async def _migrated(path: Path) -> Database:
    db = Database(path)
    await db.open()
    await db.migrate()
    return db


def _plank(set_no: int, duration: int | None = 45) -> WorkoutSetInput:
    return WorkoutSetInput(
        exercise_id=PLANK,
        set_no=set_no,
        reps=None,
        set_type="work",
        duration_seconds=duration,
    )


def _weighted(
    set_no: int, *, reps: int | None = 5, weight_kg: float | None = 10.0
) -> WorkoutSetInput:
    return WorkoutSetInput(
        exercise_id=WEIGHTED_PULL_UP,
        set_no=set_no,
        reps=reps,
        set_type="work",
        load_convention=WEIGHTED_CONVENTION,
        weight_kg=weight_kg,
    )


def _pull_up(set_no: int, *, reps: int | None = 8) -> WorkoutSetInput:
    return WorkoutSetInput(
        exercise_id=PULL_UP, set_no=set_no, reps=reps, set_type="work"
    )


# ---------- domain/service：三种记录口径的 CRUD ----------


async def test_timed_set_round_trip_create_read_update_delete(tmp_path: Path) -> None:
    """计时组：新增写入秒数、读取保留、整条覆盖修改、删除后不再读到。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        service = WorkoutRecordsService(db)
        created = await service.create(
            DAY, (_weighted(1, reps=5, weight_kg=10.0), _plank(1, 45))
        )
        assert [
            (fact.exercise_id, fact.reps, fact.weight_kg, fact.duration_seconds)
            for fact in created.sets
        ] == [
            # 组按动作身份、组序号排序（repo 的既有读顺序）
            (PLANK, None, None, 45),
            (WEIGHTED_PULL_UP, 5, 10.0, None),
        ]

        read = await service.get(created.id)
        assert read is not None
        assert read.sets[0].duration_seconds == 45
        assert read.sets[0].reps is None
        assert read.sets[1].load_convention == WEIGHTED_CONVENTION

        updated = await service.update(created.id, DAY, (_plank(1, 120),))
        assert [
            (fact.exercise_id, fact.reps, fact.duration_seconds) for fact in updated.sets
        ] == [(PLANK, None, 120)]

        await service.delete(created.id)
        assert await service.get(created.id) is None
        assert await service.list_all() == ()
    finally:
        await db.close()


async def test_duration_seconds_range_and_type(tmp_path: Path) -> None:
    """时长范围只由 Domain 唯一规则实施：0／负数／非整数被拒，超大整数无上限照写。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        service = WorkoutRecordsService(db)
        for value in (0, -1, 45.5, "45", True, float("nan")):
            with pytest.raises(InvalidRecordFact):
                await service.create(DAY, (_plank(1, value),))  # type: ignore[arg-type]
        assert await service.list_all() == ()

        created = await service.create(DAY, (_plank(1, 10**12),))
        assert created.sets[0].duration_seconds == 10**12
        assert await service.get(created.id) is not None
    finally:
        await db.close()


async def test_record_type_fields_are_required_and_mutually_exclusive(
    tmp_path: Path,
) -> None:
    """三种记录口径的必填／互斥字段：计时只要时长，外加重量与纯自重只要次数。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        service = WorkoutRecordsService(db)
        cases: list[WorkoutSetInput] = [
            # 计时动作：缺时长、带次数都不合法。
            _plank(1, None),
            WorkoutSetInput(
                exercise_id=PLANK,
                set_no=1,
                reps=10,
                set_type="work",
                duration_seconds=45,
            ),
            # 外加重量动作：缺次数不合法；带时长不合法。
            _weighted(1, reps=None),
            WorkoutSetInput(
                exercise_id=WEIGHTED_PULL_UP,
                set_no=1,
                reps=5,
                set_type="work",
                load_convention=WEIGHTED_CONVENTION,
                weight_kg=10.0,
                duration_seconds=30,
            ),
            # 纯自重动作：缺次数不合法；带时长不合法。
            _pull_up(1, reps=None),
            WorkoutSetInput(
                exercise_id=PULL_UP,
                set_no=1,
                reps=8,
                set_type="work",
                duration_seconds=30,
            ),
        ]
        for fact in cases:
            with pytest.raises(InvalidRecordFact):
                await service.create(DAY, (fact,))
        assert await service.list_all() == ()

        # 三种动作的合法形态：同一训练里可以并存，字段各自只出现一次。
        created = await service.create(
            DAY, (_weighted(1), _pull_up(1), _plank(1))
        )
        assert [
            (fact.exercise_id, fact.reps, fact.weight_kg, fact.duration_seconds)
            for fact in created.sets
        ] == [
            (PLANK, None, None, 45),
            (PULL_UP, 8, None, None),
            (WEIGHTED_PULL_UP, 5, 10.0, None),
        ]
    finally:
        await db.close()


async def test_weighted_pull_up_uses_external_added_weight_convention(
    tmp_path: Path,
) -> None:
    """独立负重引体：口径必须是 external_added_weight，与纯自重引体不共用身份。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        service = WorkoutRecordsService(db)
        # 缺口径（且无重量）与口径不符一律拒绝。
        for fact in (
            WorkoutSetInput(
                exercise_id=WEIGHTED_PULL_UP,
                set_no=1,
                reps=5,
                set_type="work",
            ),
            WorkoutSetInput(
                exercise_id=WEIGHTED_PULL_UP,
                set_no=1,
                reps=5,
                set_type="work",
                load_convention="dumbbell_per_hand",
                weight_kg=10.0,
            ),
            # 纯自重引体不得携带外加重量口径。
            WorkoutSetInput(
                exercise_id=PULL_UP,
                set_no=1,
                reps=5,
                set_type="work",
                load_convention=WEIGHTED_CONVENTION,
                weight_kg=10.0,
            ),
        ):
            with pytest.raises(RecordLoadMismatch):
                await service.create(DAY, (fact,))

        # 只给重量不给口径：连事实本身都不完整（同现同隐），在口径复验前即被拒。
        with pytest.raises(InvalidRecordFact):
            await service.create(
                DAY,
                (
                    WorkoutSetInput(
                        exercise_id=WEIGHTED_PULL_UP,
                        set_no=1,
                        reps=5,
                        set_type="work",
                        weight_kg=10.0,
                    ),
                ),
            )

        created = await service.create(DAY, (_weighted(1, reps=5, weight_kg=10.0),))
        assert created.sets[0].load_convention == WEIGHTED_CONVENTION
        assert created.sets[0].weight_kg == 10.0
        # 外加重量只记录负重片重量（不包含体重）：0kg 也是合法写法（引体自重组）。
        zero = await service.create(DAY, (_weighted(1, reps=8, weight_kg=0.0),))
        assert zero.sets[0].weight_kg == 0.0
    finally:
        await db.close()


# ---------- API：计时组与三类字段的传输行为 ----------


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(tmp_path / "data", frontend_dist=tmp_path / "dist")
    app.dependency_overrides[current_business_date] = lambda: DAY
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client


def _body(sets: list[dict[str, object]]) -> dict[str, object]:
    return {"performed_on": DAY.isoformat(), "sets": sets}


def _plank_payload(duration: object) -> dict[str, object]:
    return _body(
        [{"exercise_id": PLANK, "set_type": "work", "duration_seconds": duration}]
    )


def test_api_timed_set_round_trip(client: TestClient) -> None:
    """计时组经 API 新增／读取／整条覆盖／删除：传输对象含 duration_seconds，reps 为 null。"""
    created = client.post(
        "/api/records",
        json=_body(
            [
                {
                    "exercise_id": WEIGHTED_PULL_UP,
                    "set_type": "work",
                    "load_convention": WEIGHTED_CONVENTION,
                    "weight_kg": 10.0,
                    "reps": 5,
                },
                {"exercise_id": PLANK, "set_type": "work", "duration_seconds": 90},
            ]
        ),
    )
    assert created.status_code == 200, created.text
    record = created.json()["record"]
    record_id = record["id"]
    assert record["sets"] == [
        # 组按动作身份、组序号排序（record_dto 沿用 repo 读顺序）
        {
            "exercise_id": PLANK,
            "set_no": 1,
            "set_type": "work",
            "load_convention": None,
            "weight_kg": None,
            "reps": None,
            "duration_seconds": 90,
        },
        {
            "exercise_id": WEIGHTED_PULL_UP,
            "set_no": 1,
            "set_type": "work",
            "load_convention": WEIGHTED_CONVENTION,
            "weight_kg": 10.0,
            "reps": 5,
            "duration_seconds": None,
        },
    ]

    read = client.get(f"/api/records/{record_id}")
    assert read.status_code == 200
    assert read.json() == {"record": record}

    updated = client.put(
        f"/api/records/{record_id}",
        json=_plank_payload(120),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["record"]["sets"] == [
        {
            "exercise_id": PLANK,
            "set_no": 1,
            "set_type": "work",
            "load_convention": None,
            "weight_kg": None,
            "reps": None,
            "duration_seconds": 120,
        }
    ]

    assert client.delete(f"/api/records/{record_id}").status_code == 204
    assert client.get(f"/api/records/{record_id}").status_code == 404


def test_api_duration_rejects_zero_negative_and_non_integer(
    client: TestClient,
) -> None:
    """时长 0／负数按领域规则拒绝（422）；非整数在 JSON 形状层拒绝（400）。"""
    for payload, expected in (
        (_plank_payload(0), 422),
        (_plank_payload(-5), 422),
        (_plank_payload(90.5), 400),
        (_plank_payload("90.5"), 400),
        (_plank_payload(None), 422),
    ):
        response = client.post("/api/records", json=payload)
        assert response.status_code == expected, (payload, response.text)
        assert response.json()["error_code"] == "invalid_request"
    assert client.get("/api/records").json() == {"records": []}


def test_api_duration_has_no_business_maximum(client: TestClient) -> None:
    """已拍口径：时长不设业务上限，超大整数仍可写入并被读回。"""
    huge = 10**15
    created = client.post("/api/records", json=_plank_payload(huge))
    assert created.status_code == 200, created.text
    assert created.json()["record"]["sets"][0]["duration_seconds"] == huge

    stored = client.get("/api/records").json()["records"]
    assert stored[0]["sets"][0]["duration_seconds"] == huge


def test_api_rejects_fields_not_applicable_to_record_type(
    client: TestClient,
) -> None:
    """三类动作的字段互斥：计时不得给次数，外加重量与纯自重不得给时长。"""
    cases: list[tuple[dict[str, object], int]] = [
        # 计时动作带次数。
        (
            _body(
                [
                    {
                        "exercise_id": PLANK,
                        "set_type": "work",
                        "reps": 10,
                        "duration_seconds": 90,
                    }
                ]
            ),
            422,
        ),
        # 纯自重动作带时长。
        (
            _body(
                [
                    {
                        "exercise_id": PULL_UP,
                        "set_type": "work",
                        "reps": 8,
                        "duration_seconds": 30,
                    }
                ]
            ),
            422,
        ),
        # 纯自重动作缺次数（reps 自 002 起在形状上可空，必填由领域按记录口径判定）。
        (
            _body([{"exercise_id": PULL_UP, "set_type": "work"}]),
            422,
        ),
        # 负重引体缺次数。
        (
            _body(
                [
                    {
                        "exercise_id": WEIGHTED_PULL_UP,
                        "set_type": "work",
                        "load_convention": WEIGHTED_CONVENTION,
                        "weight_kg": 10.0,
                    }
                ]
            ),
            422,
        ),
        # 负重引体缺口径。
        (
            _body(
                [
                    {
                        "exercise_id": WEIGHTED_PULL_UP,
                        "set_type": "work",
                        "weight_kg": 10.0,
                        "reps": 5,
                    }
                ]
            ),
            422,
        ),
        # 纯自重动作带外加重量口径。
        (
            _body(
                [
                    {
                        "exercise_id": PULL_UP,
                        "set_type": "work",
                        "load_convention": WEIGHTED_CONVENTION,
                        "weight_kg": 10.0,
                        "reps": 5,
                    }
                ]
            ),
            422,
        ),
    ]
    for payload, expected in cases:
        response = client.post("/api/records", json=payload)
        assert response.status_code == expected, (payload, response.text)
    assert client.get("/api/records").json() == {"records": []}


def test_api_weighted_pull_up_records_external_added_weight(
    client: TestClient,
) -> None:
    """负重引体经 API 写入外加重量：口径与目录一致，最小加重 5kg 来自目录而非传输层。"""
    created = client.post(
        "/api/records",
        json=_body(
            [
                {
                    "exercise_id": WEIGHTED_PULL_UP,
                    "set_type": "work",
                    "load_convention": WEIGHTED_CONVENTION,
                    "weight_kg": 15.0,
                    "reps": 5,
                }
            ]
        ),
    )
    assert created.status_code == 200, created.text
    fact = created.json()["record"]["sets"][0]
    assert fact["load_convention"] == WEIGHTED_CONVENTION
    assert fact["weight_kg"] == 15.0

    catalog = {
        exercise["id"]: exercise for exercise in client.get("/api/exercises").json()["exercises"]
    }
    assert catalog[WEIGHTED_PULL_UP]["load_convention"] == WEIGHTED_CONVENTION
    assert catalog[WEIGHTED_PULL_UP]["min_load_increment_kg"] == 5.0
    assert catalog[WEIGHTED_PULL_UP]["record_type"] == "reps_weight"
    # 纯自重引体仍是独立身份：口径与加重单位为空。
    assert catalog[PULL_UP]["record_type"] == "reps_bodyweight"
    assert catalog[PULL_UP]["load_convention"] is None
    assert catalog[PLANK]["record_type"] == "time"


def test_api_time_set_requires_persisted_duration_after_upgrade(
    client: TestClient,
) -> None:
    """计时组写入库内后 reps 列为 NULL、duration_seconds 列有值（列口径与传输一致）。"""
    created = client.post("/api/records", json=_plank_payload(75))
    assert created.status_code == 200, created.text
    record_id = created.json()["record"]["id"]
    # 直接核对传输层之外没有任何补 0：reps 保持 null、时长保留 75。
    assert created.json()["record"]["sets"][0]["reps"] is None
    assert created.json()["record"]["sets"][0]["duration_seconds"] == 75
    assert client.get(f"/api/records/{record_id}").json()["record"]["sets"][0] == {
        "exercise_id": PLANK,
        "set_no": 1,
        "set_type": "work",
        "load_convention": None,
        "weight_kg": None,
        "reps": None,
        "duration_seconds": 75,
    }


async def test_raw_sql_rejects_time_set_with_invalid_reps(tmp_path: Path) -> None:
    """库内兜底仍拒绝越界次数（计时组用 NULL，不写 0 占位）。"""
    db = await _migrated(tmp_path / "x.db")

    async def run() -> None:
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO workout_sessions (id, performed_on) VALUES (1, '2026-06-01')"
            )
            await conn.execute(
                "INSERT INTO workout_sets (workout_session_id, exercise_id, set_no,"
                " set_type, reps, duration_seconds)"
                " VALUES (1, ?, 1, 'work', NULL, 45)",
                (PLANK,),
            )

    async def run_invalid() -> None:
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO workout_sets (workout_session_id, exercise_id, set_no,"
                " set_type, reps, duration_seconds)"
                " VALUES (1, ?, 2, 'work', 0, 45)",
                (PLANK,),
            )

    try:
        await run()
        with pytest.raises(sqlite3.IntegrityError):
            await run_invalid()
    finally:
        await db.close()
