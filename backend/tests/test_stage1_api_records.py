"""Stage 1 子任务 04 §9：训练记录表单 API（/api/records 与日程候选）。

对照 04 清单：训练及工作组的 CRUD、额外训练（``plan_session_id`` 为 NULL）、显式选择未完成日程、
``auto_link`` 仅当天恰有一个候选时自动关联、零个或多个候选一律 409、不存在资源 404、非法日期／
重量／次数／组类型与缺字段被拒（400／422）、组序号由 API 按提交顺序分配。

测试用 ``TestClient`` 走真实回环 ``base_url``（回环守卫要求），业务日期用
``dependency_overrides[current_business_date]`` 固定；只用 pytest ``tmp_path`` 下的独立临时库；
计划与日程用直接 SQL 写入（本阶段没有计划写入口，计划创建／激活留到阶段 5）。
"""

from collections.abc import Iterator, Sequence
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import current_business_date
from storage.db import Database

DAY = date(2026, 6, 3)
CREATED_AT = "2026-06-01T08:00:00+08:00"

#: 外加负重型动作：负重口径取目录值，公式为「口径 + 重量」同现同隐。
BARBELL = "barbell-back-squat"
BARBELL_CONVENTION = "barbell_includes_bar_total"
#: 自重动作：不得携带负重口径与重量。
BODYWEIGHT = "pull-up"


async def _seed_plan_sessions(db: Database, days: Sequence[date]) -> list[int]:
    """直接 SQL 写入计划与日程，返回日程身份（每个日期一个独立计划）。

    计划状态用 archived：003 起 draft 是部分唯一索引（任意时刻最多一条 draft），而本 fixture 每个
    日期各建一条计划；日程关联读取只查 plan_sessions（``domain/records/repo`` 的候选 SQL 不 JOIN
    plans），与计划状态无关。
    """
    session_ids: list[int] = []
    async with db.transaction() as conn:
        for version, day in enumerate(days, start=1):
            cursor = await conn.execute(
                "INSERT INTO plans (version, status, structured_content, created_at)"
                " VALUES (?, 'archived', '{}', ?)",
                (version, CREATED_AT),
            )
            try:
                plan_id = int(cursor.lastrowid or 0)
            finally:
                await cursor.close()
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
    app = create_app(tmp_path / "data", frontend_dist=tmp_path / "dist")
    app.dependency_overrides[current_business_date] = lambda: DAY
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client


def _body(**overrides: object) -> dict[str, object]:
    """一次训练的合法请求体基线：外加负重组 + 自重组，组序号由 API 分配。"""
    body: dict[str, object] = {
        "performed_on": DAY.isoformat(),
        "sets": [
            {
                "exercise_id": BARBELL,
                "set_type": "work",
                "reps": 5,
                "load_convention": BARBELL_CONVENTION,
                "weight_kg": 60.0,
            },
            {
                "exercise_id": BARBELL,
                "set_type": "warmup",
                "reps": 8,
                "load_convention": BARBELL_CONVENTION,
                "weight_kg": 40.0,
            },
            {"exercise_id": BODYWEIGHT, "set_type": "work", "reps": 8},
        ],
    }
    body.update(overrides)
    return body


def test_create_read_update_delete_roundtrip(client: TestClient) -> None:
    created = client.post("/api/records", json=_body())
    assert created.status_code == 200
    record = created.json()["record"]
    record_id = record["id"]
    assert record["performed_on"] == DAY.isoformat()
    assert record["plan_session_id"] is None
    # 组序号按提交顺序对同一动作依次分配 1,2…；自重动作保持无口径无重量。
    assert [(s["exercise_id"], s["set_no"]) for s in record["sets"]] == [
        (BARBELL, 1),
        (BARBELL, 2),
        (BODYWEIGHT, 1),
    ]
    assert record["sets"][2]["load_convention"] is None
    assert record["sets"][2]["weight_kg"] is None

    listed = client.get("/api/records")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["records"]] == [record_id]

    read = client.get(f"/api/records/{record_id}")
    assert read.status_code == 200
    assert read.json() == {"record": record}

    updated = client.put(
        f"/api/records/{record_id}",
        json=_body(
            performed_on=date(2026, 6, 4).isoformat(),
            sets=[
                {
                    "exercise_id": BODYWEIGHT,
                    "set_type": "assisted",
                    "reps": 6,
                }
            ],
        ),
    )
    assert updated.status_code == 200
    after = updated.json()["record"]
    assert after["id"] == record_id
    assert after["performed_on"] == "2026-06-04"
    assert after["sets"] == [
        {
            "exercise_id": BODYWEIGHT,
            "set_no": 1,
            "set_type": "assisted",
            "load_convention": None,
            "weight_kg": None,
            "reps": 6,
            # 非计时组不记录秒数，保持 null 不补 0。
            "duration_seconds": None,
        }
    ]

    assert client.delete(f"/api/records/{record_id}").status_code == 204
    assert client.get(f"/api/records/{record_id}").status_code == 404
    assert client.get("/api/records").json() == {"records": []}


def test_null_plan_session_id_means_extra_workout(client: TestClient) -> None:
    session_ids = client.portal.call(_seed_plan_sessions, client.app.state.db, [DAY])

    created = client.post("/api/records", json=_body(plan_session_id=None))
    assert created.status_code == 200
    assert created.json()["record"]["plan_session_id"] is None

    # 未被占用的日程仍在候选里（额外训练不关联它）。
    candidates = client.get("/api/records/plan-session-candidates").json()
    assert [item["id"] for item in candidates["sessions"]] == session_ids


def test_explicit_plan_session_id_links_and_occupies_session(
    client: TestClient,
) -> None:
    session_ids = client.portal.call(_seed_plan_sessions, client.app.state.db, [DAY])
    session_id = session_ids[0]

    created = client.post("/api/records", json=_body(plan_session_id=session_id))
    assert created.status_code == 200
    assert created.json()["record"]["plan_session_id"] == session_id

    # 已被关联的日程不再作为候选出现（同一日程最多完成一次）。
    assert client.get("/api/records/plan-session-candidates").json() == {
        "sessions": []
    }


def test_auto_link_with_exactly_one_candidate(client: TestClient) -> None:
    session_ids = client.portal.call(_seed_plan_sessions, client.app.state.db, [DAY])

    created = client.post("/api/records", json=_body(auto_link=True))
    assert created.status_code == 200
    assert created.json()["record"]["plan_session_id"] == session_ids[0]


def test_auto_link_without_candidate_conflicts(client: TestClient) -> None:
    response = client.post("/api/records", json=_body(auto_link=True))
    assert response.status_code == 409
    assert response.json()["error_code"] == "invalid_request"
    assert client.get("/api/records").json() == {"records": []}


def test_auto_link_with_multiple_candidates_conflicts(client: TestClient) -> None:
    client.portal.call(_seed_plan_sessions, client.app.state.db, [DAY, DAY])

    response = client.post("/api/records", json=_body(auto_link=True))
    assert response.status_code == 409
    assert response.json()["error_code"] == "invalid_request"


def test_candidates_use_given_date_and_business_date(
    client: TestClient,
) -> None:
    other_day = date(2026, 6, 5)
    session_ids = client.portal.call(
        _seed_plan_sessions, client.app.state.db, [other_day]
    )

    # 省略 date 用业务自然日（固定为 DAY）→ 当天无候选。
    assert client.get("/api/records/plan-session-candidates").json() == {
        "sessions": []
    }

    explicit = client.get(
        "/api/records/plan-session-candidates",
        params={"date": other_day.isoformat()},
    )
    assert explicit.status_code == 200
    sessions = explicit.json()["sessions"]
    assert [item["id"] for item in sessions] == session_ids
    assert sessions[0]["scheduled_on"] == other_day.isoformat()
    assert isinstance(sessions[0]["plan_id"], int)


def test_missing_resource_is_404(client: TestClient) -> None:
    assert client.get("/api/records/999").status_code == 404
    assert client.put("/api/records/999", json=_body()).status_code == 404
    assert client.delete("/api/records/999").status_code == 404


def test_rejects_illegal_values(client: TestClient) -> None:
    cases: list[tuple[dict[str, object], int]] = [
        # 非法日期（形状）：ISO 日期解析失败。
        (_body(performed_on="2026-06-31"), 400),
        # 超出重量值域（领域规则）。
        (
            _body(
                sets=[
                    {
                        "exercise_id": BARBELL,
                        "set_type": "work",
                        "reps": 5,
                        "load_convention": BARBELL_CONVENTION,
                        "weight_kg": 2000.0,
                    }
                ]
            ),
            422,
        ),
        # 重量精度越界（最多一位小数）。
        (
            _body(
                sets=[
                    {
                        "exercise_id": BARBELL,
                        "set_type": "work",
                        "reps": 5,
                        "load_convention": BARBELL_CONVENTION,
                        "weight_kg": 60.55,
                    }
                ]
            ),
            422,
        ),
        # 次数为零。
        (
            _body(
                sets=[
                    {
                        "exercise_id": BODYWEIGHT,
                        "set_type": "work",
                        "reps": 0,
                    }
                ]
            ),
            422,
        ),
        # 次数超上限。
        (
            _body(
                sets=[
                    {
                        "exercise_id": BODYWEIGHT,
                        "set_type": "work",
                        "reps": 101,
                    }
                ]
            ),
            422,
        ),
        # 组类型不在 work／warmup／assisted 内。
        (
            _body(
                sets=[
                    {
                        "exercise_id": BODYWEIGHT,
                        "set_type": "max",
                        "reps": 5,
                    }
                ]
            ),
            422,
        ),
        # 负重口径与目录不符（自重动作不得携带口径）。
        (
            _body(
                sets=[
                    {
                        "exercise_id": BODYWEIGHT,
                        "set_type": "work",
                        "reps": 8,
                        "load_convention": BARBELL_CONVENTION,
                        "weight_kg": 20.0,
                    }
                ]
            ),
            422,
        ),
        # 动作不在目录内。
        (
            _body(
                sets=[
                    {"exercise_id": "not-an-exercise", "set_type": "work", "reps": 5}
                ]
            ),
            422,
        ),
        # 组数与口径不同现同隐：给了口径却没给重量。
        (
            _body(
                sets=[
                    {
                        "exercise_id": BARBELL,
                        "set_type": "work",
                        "reps": 5,
                        "load_convention": BARBELL_CONVENTION,
                    }
                ]
            ),
            422,
        ),
        # 零组（提交时必须至少一组）。
        (_body(sets=[]), 422),
        # 缺字段：没有 sets。
        ({"performed_on": DAY.isoformat()}, 400),
        # 缺字段：组里没有 reps。reps 自 002 起可为空（计时组无次数），因此不再是 JSON 形状必填，
        # 改由领域按目录动作的记录口径拒绝（本动作是自重型，必须给次数）。
        (
            _body(sets=[{"exercise_id": BODYWEIGHT, "set_type": "work"}]),
            422,
        ),
        # 未知字段。
        (_body(extra_field=1), 400),
        # 非法类型：reps 不是整数。
        (
            _body(
                sets=[
                    {"exercise_id": BODYWEIGHT, "set_type": "work", "reps": "五次"}
                ]
            ),
            400,
        ),
    ]

    for payload, expected in cases:
        response = client.post("/api/records", json=payload)
        assert response.status_code == expected, (payload, response.text)
        assert response.json()["error_code"] == "invalid_request"
        assert response.json()["http_status"] == expected

    assert client.get("/api/records").json() == {"records": []}


def test_non_json_body_is_400(client: TestClient) -> None:
    response = client.post(
        "/api/records",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["error_code"] == "invalid_request"
