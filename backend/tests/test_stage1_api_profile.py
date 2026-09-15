"""Stage 1 子任务 04 §9：画像表单 API（GET／PUT /api/profile）。

对照 04 清单：七字段三态事实整份覆盖、未建档发 ``profile: null``、未填写（unknown）与明确为空
（denied）可分、``known`` 空列表由领域拒绝（422，不在 API 层改写成 denied）、未知字段与缺字段
被拒（400）、无 ``context_version``。

测试用 ``TestClient`` 走真实回环 ``base_url``（回环守卫要求），只用 pytest ``tmp_path`` 下的
独立临时库；不 import ``tests.support``（它依赖已移除的 pydantic_ai）。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app

#: 七字段逐字拼写（与 04 响应字段一致）。
FIELDS = (
    "training_goal",
    "weekly_frequency",
    "available_equipment",
    "explicit_preferences",
    "current_level",
    "known_injuries",
    "forbidden_exercise_ids",
)


def _unknown_fields() -> dict[str, dict[str, object]]:
    """七字段全未填写基线：未填写用 unknown 表达，不冒充「明确为空」。"""
    return {name: {"state": "unknown", "value": None} for name in FIELDS}


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(tmp_path / "data", frontend_dist=tmp_path / "dist")
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client


def test_get_profile_before_onboarding_is_null(client: TestClient) -> None:
    response = client.get("/api/profile")
    assert response.status_code == 200
    assert response.json() == {"profile": None}


def test_put_then_get_returns_same_seven_facts(client: TestClient) -> None:
    body = _unknown_fields()
    body["training_goal"] = {"state": "known", "value": "增肌"}
    body["weekly_frequency"] = {"state": "known", "value": 4}
    body["available_equipment"] = {"state": "denied", "value": None}
    body["forbidden_exercise_ids"] = {
        "state": "known",
        "value": ["barbell-deadlift"],
    }

    put = client.put("/api/profile", json=body)
    assert put.status_code == 200
    profile = put.json()["profile"]
    assert set(profile) == set(FIELDS)
    assert profile["training_goal"] == {"state": "known", "value": "增肌"}
    assert profile["available_equipment"] == {"state": "denied", "value": None}
    assert profile["forbidden_exercise_ids"] == {
        "state": "known",
        "value": ["barbell-deadlift"],
    }

    read = client.get("/api/profile")
    assert read.status_code == 200
    assert read.json()["profile"] == profile


def test_known_empty_list_rejected_by_domain(client: TestClient) -> None:
    body = _unknown_fields()
    body["available_equipment"] = {"state": "known", "value": []}

    response = client.put("/api/profile", json=body)
    assert response.status_code == 422
    assert response.json()["error_code"] == "invalid_request"

    # 拒绝后不落盘半份画像：仍是未建档。
    assert client.get("/api/profile").json() == {"profile": None}


def test_forbidden_exercise_id_must_exist_in_catalog(client: TestClient) -> None:
    body = _unknown_fields()
    body["forbidden_exercise_ids"] = {"state": "known", "value": ["not-an-exercise"]}

    response = client.put("/api/profile", json=body)
    assert response.status_code == 422
    assert response.json()["error_code"] == "invalid_request"


def test_unknown_field_rejected(client: TestClient) -> None:
    body = _unknown_fields()
    body["context_version"] = 3

    response = client.put("/api/profile", json=body)
    assert response.status_code == 400
    assert response.json()["error_code"] == "invalid_request"


def test_missing_field_rejected(client: TestClient) -> None:
    body = _unknown_fields()
    del body["known_injuries"]

    response = client.put("/api/profile", json=body)
    assert response.status_code == 400
    assert response.json()["error_code"] == "invalid_request"


def test_fact_carrying_value_outside_known_rejected(client: TestClient) -> None:
    body = _unknown_fields()
    body["current_level"] = {"state": "denied", "value": "中级"}

    response = client.put("/api/profile", json=body)
    assert response.status_code == 400
    assert response.json()["error_code"] == "invalid_request"
