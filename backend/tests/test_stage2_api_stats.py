"""Stage 2 Subtask 04 §C：Stats API（三类 PB、趋势、月历）与路由注册顺序。

依据：``refactor-log/stage2.md`` §9／§11.4、``LANGGRAPH_REFACTOR_PLAN.md`` §4。

覆盖：三个端点的对象包裹响应与 snake_case 契约、业务日期经 ``dependency_overrides[current_business_date]``
注入（不取系统时钟）、``no_data`` 状态经 API 可读、月历跨日／额外训练事实、非法与缺失月份返回统一 4xx
错误形状、三个路由不被静态前端兜底吞掉、删除训练后 PB／趋势／月历立即一致。

测试用 ``TestClient`` 走真实回环 ``base_url``（回环守卫要求），且 ``frontend_dist`` 指向真实带
``index.html`` 的临时产物：静态兜底在前时这些端点会返回 HTML，断言因此能真正守住注册顺序。
计划与日程用直接 SQL 写入（正式计划入口留 Stage 5），训练与身体指标走真实表单 API。
"""

from collections.abc import Iterator, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import current_business_date
from storage.db import Database

BUSINESS_DAY = date(2026, 6, 30)
CREATED_AT = "2026-06-01T08:00:00+08:00"
INDEX_HTML = '<!doctype html><html><body><div id="root"></div></body></html>'

SQUAT = "barbell-back-squat"
SQUAT_CONVENTION = "barbell_includes_bar_total"
PULL_UP = "pull-up"
WEIGHTED_PULL_UP = "weighted-pull-up"
EXTERNAL_ADDED_WEIGHT = "external_added_weight"
PLANK = "plank"


async def _seed_active_plan(db: Database, days: Sequence[date]) -> list[int]:
    """直接 SQL 写入一个 active 计划与其日程，返回日程身份（正式计划写入口留 Stage 5）。"""
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "INSERT INTO plans (version, status, structured_content, created_at,"
            " confirmed_at) VALUES (1, 'active', '{}', ?, ?)",
            (CREATED_AT, CREATED_AT),
        )
        try:
            plan_id = int(cursor.lastrowid or 0)
        finally:
            await cursor.close()
        session_ids: list[int] = []
        for day in days:
            cursor = await conn.execute(
                "INSERT INTO plan_sessions (plan_id, scheduled_on) VALUES (?, ?)",
                (plan_id, day.isoformat()),
            )
            try:
                session_ids.append(int(cursor.lastrowid or 0))
            finally:
                await cursor.close()
    return session_ids


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    app = create_app(tmp_path / "data", frontend_dist=dist)
    app.dependency_overrides[current_business_date] = lambda: BUSINESS_DAY
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client


def _record_body(
    performed_on: date, sets: list[dict[str, object]], plan_session_id: int | None = None
) -> dict[str, object]:
    return {
        "performed_on": performed_on.isoformat(),
        "plan_session_id": plan_session_id,
        "sets": sets,
    }


def _squat_sets(weight_kg: float, reps: int = 5) -> list[dict[str, object]]:
    return [
        {
            "exercise_id": SQUAT,
            "set_type": "work",
            "reps": reps,
            "load_convention": SQUAT_CONVENTION,
            "weight_kg": weight_kg,
        }
    ]


def _post_metric(
    client: TestClient, measured_on: date, weight_kg: float, body_fat_pct: float | None
) -> None:
    response = client.post(
        "/api/body-metrics",
        json={
            "measured_on": measured_on.isoformat(),
            "weight_kg": weight_kg,
            "body_fat_pct": body_fat_pct,
        },
    )
    assert response.status_code == 200


# ---------- 三类 PB ----------


def test_personal_bests_endpoint_returns_pb_types_with_sources(
    client: TestClient,
) -> None:
    """``GET /api/stats/personal-bests``：对象包裹 + snake_case 契约 + 来源训练／组序号／日期。"""
    assert client.get("/api/stats/personal-bests").json() == {"personal_bests": []}

    created = client.post(
        "/api/records",
        json=_record_body(
            BUSINESS_DAY,
            _squat_sets(100.0)
            + [{"exercise_id": PULL_UP, "set_type": "work", "reps": 9}],
        ),
    )
    assert created.status_code == 200
    record_id = created.json()["record"]["id"]

    body = client.get("/api/stats/personal-bests").json()
    # 外加重量动作只出重量 PB，纯自重引体出次数 PB。
    assert [pb["pb_type"] for pb in body["personal_bests"]] == [
        "weight_pb",
        "reps_pb",
    ]
    squat_pb = body["personal_bests"][0]
    assert squat_pb == {
        "exercise_id": SQUAT,
        "exercise_name": "杠铃背蹲",
        "pb_type": "weight_pb",
        "value": 100.0,
        "load_convention": SQUAT_CONVENTION,
        "weight_kg": 100.0,
        "workout_session_id": record_id,
        "set_no": 1,
        "performed_on": BUSINESS_DAY.isoformat(),
    }
    assert body["personal_bests"][1]["exercise_id"] == PULL_UP


def test_three_record_types_flow_from_the_form_api_into_three_pb_types(
    client: TestClient,
) -> None:
    """Gate B：外加重量／纯自重／计时三种记录经表单 API 写入后，Stats API 给出三类 PB 且互不混算。

    跨层链路：``POST /api/records`` → 领域写入 → 共享有效工作组 SQL → Stats API。纯自重引体与
    负重引体是两个动作，各自的次数／重量分别成 PB，不互相顶替；外加重量动作不出次数 PB。
    """
    weighted_reps = client.post(
        "/api/records",
        json=_record_body(
            date(2026, 6, 2),
            _squat_sets(100.0)
            + [{"exercise_id": PULL_UP, "set_type": "work", "reps": 9}],
        ),
    )
    assert weighted_reps.status_code == 200, weighted_reps.text
    timed = client.post(
        "/api/records",
        json=_record_body(
            date(2026, 6, 10),
            [
                {
                    "exercise_id": WEIGHTED_PULL_UP,
                    "set_type": "work",
                    "load_convention": EXTERNAL_ADDED_WEIGHT,
                    "weight_kg": 20.0,
                    "reps": 6,
                },
                {"exercise_id": PLANK, "set_type": "work", "duration_seconds": 120},
            ],
        ),
    )
    assert timed.status_code == 200, timed.text
    first_id = weighted_reps.json()["record"]["id"]
    second_id = timed.json()["record"]["id"]

    bests = client.get("/api/stats/personal-bests").json()["personal_bests"]
    assert [
        (
            pb["exercise_id"],
            pb["pb_type"],
            pb["value"],
            pb["weight_kg"],
            pb["load_convention"],
            pb["workout_session_id"],
            pb["set_no"],
            pb["performed_on"],
        )
        for pb in bests
    ] == [
        (SQUAT, "weight_pb", 100.0, 100.0, SQUAT_CONVENTION, first_id, 1, "2026-06-02"),
        (PLANK, "duration_pb", 120, None, None, second_id, 1, "2026-06-10"),
        (PULL_UP, "reps_pb", 9, None, None, first_id, 1, "2026-06-02"),
        (
            WEIGHTED_PULL_UP,
            "weight_pb",
            20.0,
            20.0,
            EXTERNAL_ADDED_WEIGHT,
            second_id,
            1,
            "2026-06-10",
        ),
    ]

    strength = client.get("/api/stats/trends").json()["trends"]["strength"]
    assert [
        (
            trend["exercise_id"],
            trend["pb_type"],
            trend["weight_kg"],
            [(point["performed_on"], point["value"]) for point in trend["points"]],
        )
        for trend in strength
    ] == [
        (SQUAT, "weight_pb", None, [("2026-06-02", 100.0)]),
        (PLANK, "duration_pb", None, [("2026-06-10", 120)]),
        (PULL_UP, "reps_pb", None, [("2026-06-02", 9)]),
        (WEIGHTED_PULL_UP, "weight_pb", None, [("2026-06-10", 20.0)]),
    ]


def test_pb_and_trends_recompute_after_edit_through_the_form_api(
    client: TestClient,
) -> None:
    """Gate B／C：整条覆盖训练记录或身体指标后，PB、力量趋势与趋势摘要立即按新事实重算。"""
    first_metric = client.post(
        "/api/body-metrics",
        json={
            "measured_on": "2026-06-01",
            "weight_kg": 81.0,
            "body_fat_pct": 21.0,
        },
    )
    latest_metric = client.post(
        "/api/body-metrics",
        json={"measured_on": "2026-06-20", "weight_kg": 80.5},
    )
    first = client.post(
        "/api/records", json=_record_body(date(2026, 6, 2), _squat_sets(100.0))
    )
    heavier_on_later_date = client.post(
        "/api/records",
        json=_record_body(date(2026, 6, 25), _squat_sets(100.0, reps=3)),
    )
    assert first.status_code == 200, first.text
    assert heavier_on_later_date.status_code == 200, heavier_on_later_date.text
    first_id = first.json()["record"]["id"]
    later_id = heavier_on_later_date.json()["record"]["id"]

    def strengths() -> list[tuple[str, str, float | None, list[tuple[str, object]]]]:
        return [
            (
                trend["exercise_id"],
                trend["pb_type"],
                trend["weight_kg"],
                [(point["performed_on"], point["value"]) for point in trend["points"]],
            )
            for trend in client.get("/api/stats/trends").json()["trends"]["strength"]
        ]

    assert [
        (pb["pb_type"], pb["value"], pb["workout_session_id"])
        for pb in client.get("/api/stats/personal-bests").json()["personal_bests"]
    ] == [("weight_pb", 100.0, first_id)]
    assert strengths() == [
        (SQUAT, "weight_pb", None, [("2026-06-02", 100.0), ("2026-06-25", 100.0)]),
    ]
    summary = client.get("/api/stats/trends").json()["trends"]["trend_summary"]
    assert summary["weight_change"]["change"] == -0.5

    # 整条覆盖最早的训练：它已不是最大重量，PB 回落到 6/25 的 100kg×3，来源与日期随之更新。
    edited = client.put(
        f"/api/records/{first_id}",
        json=_record_body(date(2026, 6, 2), _squat_sets(90.0, reps=4)),
    )
    assert edited.status_code == 200, edited.text
    assert [
        (
            pb["pb_type"],
            pb["value"],
            pb["weight_kg"],
            pb["workout_session_id"],
            pb["performed_on"],
        )
        for pb in client.get("/api/stats/personal-bests").json()["personal_bests"]
    ] == [
        ("weight_pb", 100.0, 100.0, later_id, "2026-06-25"),
    ]
    assert strengths() == [
        (SQUAT, "weight_pb", None, [("2026-06-02", 90.0), ("2026-06-25", 100.0)]),
    ]

    # 覆盖最近一条体重记录：趋势摘要按新的事实重算，不沿用旧变化值。
    assert latest_metric.status_code == 200, latest_metric.text
    patched = client.put(
        f"/api/body-metrics/{latest_metric.json()['metric']['id']}",
        json={"measured_on": "2026-06-20", "weight_kg": 79.0},
    )
    assert patched.status_code == 200, patched.text
    assert client.put(
        f"/api/body-metrics/{first_metric.json()['metric']['id']}",
        json={"measured_on": "2026-06-01", "weight_kg": 81.0, "body_fat_pct": 20.0},
    ).status_code == 200
    after = client.get("/api/stats/trends").json()["trends"]
    assert after["trend_summary"]["weight_change"] == {
        "status": "ok",
        "current": 79.0,
        "current_on": "2026-06-20",
        "previous": 81.0,
        "previous_on": "2026-06-01",
        "change": -2.0,
    }
    assert after["body_fat"] == [{"measured_on": "2026-06-01", "value": 20.0}]


def test_personal_bests_and_trends_recompute_after_record_deletion(
    client: TestClient,
) -> None:
    """删除训练后 PB、趋势与月历立即一致：都从当前有效记录现算，不落表、不缓存。"""
    session_ids = client.portal.call(
        _seed_active_plan, client.app.state.db, [BUSINESS_DAY]
    )
    created = client.post(
        "/api/records",
        json=_record_body(BUSINESS_DAY, _squat_sets(100.0), session_ids[0]),
    )
    record_id = created.json()["record"]["id"]
    assert client.get("/api/stats/personal-bests").json()["personal_bests"] != []
    calendar = client.get("/api/stats/calendar?month=2026-06").json()["calendar"]
    assert calendar["days"][0]["plan_sessions"][0]["status"] == "complete"

    assert client.delete(f"/api/records/{record_id}").status_code == 204

    assert client.get("/api/stats/personal-bests").json() == {"personal_bests": []}
    trends = client.get("/api/stats/trends").json()["trends"]
    assert trends["strength"] == []
    assert trends["trend_summary"]["days_since_last_workout"] == {
        "status": "no_data",
        "days": None,
        "last_performed_on": None,
    }
    after = client.get("/api/stats/calendar?month=2026-06").json()["calendar"]
    assert after["days"][0]["plan_sessions"][0]["status"] == "incomplete"
    assert after["days"][0]["workouts"] == []


# ---------- 趋势 ----------


def test_trends_endpoint_uses_the_injected_business_date(client: TestClient) -> None:
    """``GET /api/stats/trends``：窗口按注入的业务日期算，原始点、力量系列与摘要都来自后端。"""
    empty = client.get("/api/stats/trends").json()["trends"]
    assert empty["window_days"] == 30
    assert (empty["from"], empty["to"]) == ("2026-06-01", "2026-06-30")
    assert empty["weight"] == []
    assert empty["trend_summary"]["weight_change"]["status"] == "no_data"
    assert empty["trend_summary"]["body_fat_change"]["status"] == "no_data"

    _post_metric(client, date(2026, 6, 1), 81.0, 21.0)
    _post_metric(client, date(2026, 6, 20), 80.5, None)
    client.post("/api/records", json=_record_body(date(2026, 6, 2), _squat_sets(90.0)))
    client.post("/api/records", json=_record_body(date(2026, 6, 25), _squat_sets(110.0)))

    trends = client.get("/api/stats/trends").json()["trends"]
    assert set(trends) == {
        "window_days",
        "from",
        "to",
        "weight",
        "body_fat",
        "strength",
        "trend_summary",
    }
    assert trends["weight"] == [
        {"measured_on": "2026-06-01", "value": 81.0},
        {"measured_on": "2026-06-20", "value": 80.5},
    ]
    # 6/20 未记录体脂：不进体脂曲线，也不补 0。
    assert trends["body_fat"] == [{"measured_on": "2026-06-01", "value": 21.0}]
    # 外加重量动作只出一条「累计最大重量」系列：不按同重量再生成次数曲线。
    assert trends["strength"] == [
        {
            "exercise_id": SQUAT,
            "exercise_name": "杠铃背蹲",
            "pb_type": "weight_pb",
            "load_convention": SQUAT_CONVENTION,
            "weight_kg": None,
            "points": [
                {"performed_on": "2026-06-02", "value": 90.0},
                {"performed_on": "2026-06-25", "value": 110.0},
            ],
        },
    ]
    assert trends["trend_summary"]["weight_change"] == {
        "status": "ok",
        "current": 80.5,
        "current_on": "2026-06-20",
        "previous": 81.0,
        "previous_on": "2026-06-01",
        "change": -0.5,
    }
    assert trends["trend_summary"]["body_fat_change"]["status"] == "insufficient_data"
    assert trends["trend_summary"]["days_since_last_workout"] == {
        "status": "ok",
        "days": 5,
        "last_performed_on": "2026-06-25",
    }


# ---------- 月历 ----------


def test_calendar_endpoint_returns_month_facts(client: TestClient) -> None:
    """``GET /api/stats/calendar?month=YYYY-MM``：计划状态、跨日训练与额外训练都可读出。"""
    session_ids = client.portal.call(
        _seed_active_plan, client.app.state.db, [date(2026, 6, 10), date(2026, 6, 13)]
    )
    created = client.post(
        "/api/records",
        json=_record_body(date(2026, 6, 11), _squat_sets(100.0), session_ids[0]),
    )
    linked_id = created.json()["record"]["id"]
    # 额外训练：不关联任何日程，仍显示为训练日。
    extra = client.post(
        "/api/records",
        json=_record_body(date(2026, 6, 12), _squat_sets(60.0)),
    )
    extra_id = extra.json()["record"]["id"]

    body = client.get("/api/stats/calendar?month=2026-06").json()
    calendar = body["calendar"]
    assert set(calendar) == {"month", "from", "to", "days"}
    assert (calendar["month"], calendar["from"], calendar["to"]) == (
        "2026-06",
        "2026-06-01",
        "2026-06-30",
    )
    assert [day["date"] for day in calendar["days"]] == [
        "2026-06-10",
        "2026-06-11",
        "2026-06-12",
        "2026-06-13",
    ]
    # 6/10 的日程由 6/11 的训练完成：计划事实在计划日，训练事实在发生日。
    assert calendar["days"][0]["plan_sessions"] == [
        {
            "id": session_ids[0],
            "scheduled_on": "2026-06-10",
            "status": "complete",
            "workout_session_id": linked_id,
            "actual_performed_on": "2026-06-11",
        }
    ]
    assert calendar["days"][0]["workouts"] == []
    assert calendar["days"][1]["plan_sessions"] == []
    assert calendar["days"][1]["workouts"] == [
        {"id": linked_id, "performed_on": "2026-06-11", "plan_session_id": session_ids[0]}
    ]
    assert calendar["days"][2]["plan_sessions"] == []
    assert calendar["days"][2]["workouts"] == [
        {"id": extra_id, "performed_on": "2026-06-12", "plan_session_id": None}
    ]
    # 6/13 的日程没有任何训练：未完成，且当天没有训练事实。
    assert calendar["days"][3]["plan_sessions"] == [
        {
            "id": session_ids[1],
            "scheduled_on": "2026-06-13",
            "status": "incomplete",
            "workout_session_id": None,
            "actual_performed_on": None,
        }
    ]
    assert calendar["days"][3]["workouts"] == []


@pytest.mark.parametrize(
    "query",
    ["month=2026-6", "month=2026-13", "month=2026-00", "month=abc", "month=0000-01", ""],
)
def test_calendar_rejects_invalid_month(client: TestClient, query: str) -> None:
    """非法或缺失月份返回统一 4xx 错误形状：不猜月份、不静默回退当月。"""
    response = client.get(f"/api/stats/calendar?{query}")
    assert response.status_code == 400
    body: dict[str, Any] = response.json()
    assert body["http_status"] == 400
    assert body["error_code"] == "invalid_request"
    assert isinstance(body["message"], str) and body["message"]


# ---------- 注册顺序 ----------


def test_stats_routes_are_not_swallowed_by_the_frontend_fallback(
    client: TestClient,
) -> None:
    """静态兜底在前时这些端点会返回 index.html：因此必须能拿到 JSON 与被包裹的响应体。"""
    responses = {
        "/api/stats/personal-bests": "personal_bests",
        "/api/stats/trends": "trends",
        "/api/stats/calendar?month=2026-06": "calendar",
    }
    for path, wrapper in responses.items():
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert set(response.json()) == {wrapper}
        assert response.text != INDEX_HTML
    # 未实现的统计路径仍是 404 JSON，不会被兜底改写成 SPA 页面。
    missing = client.get("/api/stats/no-such-endpoint")
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("application/json")
