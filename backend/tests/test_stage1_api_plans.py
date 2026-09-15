"""Stage 1 子任务 04 §9：计划只读 API（/api/plans、/api/plans/active、/api/plans/{id}/sessions）。

对照 04 清单：全部版本升序、无 active 时发 null、按身份读取、不存在 404、日程列表（含已取消行），
以及 ``/api/plans/active`` 不被 ``/api/plans/{plan_id}`` 路径参数吞掉。本阶段没有计划写入口，
计划与日程用直接 SQL 写入（创建／激活留到阶段 5）。

测试用 ``TestClient`` 走真实回环 ``base_url``（回环守卫要求），只用 pytest ``tmp_path`` 下的
独立临时库；不 import ``tests.support``（它依赖已移除的 pydantic_ai）。
"""

from collections.abc import Iterator
from datetime import date
from functools import partial
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from storage.db import Database

CREATED_AT = "2026-06-01T08:00:00+08:00"
PLAN_CONTENT = '{"plan_workouts": []}'
EVALUATOR_RESULT = '{"passed": true}'


async def _insert_plan(
    db: Database,
    *,
    version: int,
    status: str,
    structured_content: str = PLAN_CONTENT,
    evaluator_result: str | None = None,
) -> int:
    """直接 SQL 写入一个计划版本行，返回计划身份。"""
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "INSERT INTO plans (version, status, structured_content, evaluator_result,"
            " created_at, confirmed_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                version,
                status,
                structured_content,
                evaluator_result,
                CREATED_AT,
                CREATED_AT if status == "active" else None,
            ),
        )
        try:
            return int(cursor.lastrowid or 0)
        finally:
            await cursor.close()


async def _insert_plan_session(
    db: Database, plan_id: int, scheduled_on: date, cancelled_at: str | None = None
) -> int:
    """直接 SQL 写入一条计划日程行，返回日程身份。"""
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "INSERT INTO plan_sessions (plan_id, scheduled_on, cancelled_at)"
            " VALUES (?, ?, ?)",
            (plan_id, scheduled_on.isoformat(), cancelled_at),
        )
        try:
            return int(cursor.lastrowid or 0)
        finally:
            await cursor.close()


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(tmp_path / "data", frontend_dist=tmp_path / "dist")
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client


def test_no_active_plan_is_null(client: TestClient) -> None:
    assert client.get("/api/plans/active").json() == {"plan": None}
    assert client.get("/api/plans").json() == {"plans": []}


def test_read_plan_by_id_and_list_versions_in_order(client: TestClient) -> None:
    db = client.app.state.db
    draft_id = client.portal.call(
        partial(
            _insert_plan,
            version=1,
            status="draft",
            evaluator_result=EVALUATOR_RESULT,
        ),
        db,
    )
    active_id = client.portal.call(
        partial(_insert_plan, version=2, status="active"), db
    )

    read = client.get(f"/api/plans/{draft_id}")
    assert read.status_code == 200
    assert read.json()["plan"] == {
        "id": draft_id,
        "version": 1,
        "status": "draft",
        "source_plan_id": None,
        "structured_content": {"plan_workouts": []},
        "evaluator_result": {"passed": True},
        "created_at": CREATED_AT,
        "confirmed_at": None,
        "archived_at": None,
    }

    listed = client.get("/api/plans")
    assert listed.status_code == 200
    assert [plan["id"] for plan in listed.json()["plans"]] == [draft_id, active_id]
    assert [plan["version"] for plan in listed.json()["plans"]] == [1, 2]


def test_active_plan_route_is_not_swallowed_by_path_parameter(
    client: TestClient,
) -> None:
    active_id = client.portal.call(
        partial(
            _insert_plan,
            version=1,
            status="active",
            evaluator_result=EVALUATOR_RESULT,
        ),
        client.app.state.db,
    )

    response = client.get("/api/plans/active")
    assert response.status_code == 200
    assert response.json()["plan"]["id"] == active_id
    assert response.json()["plan"]["status"] == "active"
    assert response.json()["plan"]["confirmed_at"] == CREATED_AT


def test_plan_sessions_list_includes_cancelled_rows(client: TestClient) -> None:
    db = client.app.state.db
    plan_id = client.portal.call(
        partial(_insert_plan, version=1, status="draft"), db
    )
    first = client.portal.call(
        partial(_insert_plan_session, scheduled_on=date(2026, 6, 8)), db, plan_id
    )
    second = client.portal.call(
        partial(
            _insert_plan_session,
            scheduled_on=date(2026, 6, 10),
            cancelled_at=CREATED_AT,
        ),
        db,
        plan_id,
    )

    response = client.get(f"/api/plans/{plan_id}/sessions")
    assert response.status_code == 200
    assert response.json() == {
        "sessions": [
            {
                "id": first,
                "plan_id": plan_id,
                "scheduled_on": "2026-06-08",
                "cancelled_at": None,
            },
            {
                "id": second,
                "plan_id": plan_id,
                "scheduled_on": "2026-06-10",
                "cancelled_at": CREATED_AT,
            },
        ]
    }


def test_missing_plan_is_404(client: TestClient) -> None:
    assert client.get("/api/plans/999").status_code == 404
    assert client.get("/api/plans/999/sessions").status_code == 404
