"""Stage 1 子任务 04 §9：动作目录 API（GET /api/exercises）。

对照 04 清单：表单的「动作选择」需要目录全量，且字段齐全（稳定身份、中文标准名、器械变式、
记录口径、负重口径、最小加重单位、是否可推荐、动作模式、来源与许可）。动作种子取
``storage/migrations/001_initial.sql`` 的真实 ``exercise_id``。

测试用 ``TestClient`` 走真实回环 ``base_url``（回环守卫要求），只用 pytest ``tmp_path`` 下的
独立临时库；不 import ``tests.support``（它依赖已移除的 pydantic_ai）。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app

FIELDS = (
    "id",
    "standard_name_zh",
    "equipment_variant",
    "record_type",
    "load_convention",
    "min_load_increment_kg",
    "recommendable",
    "modes",
    "source_ref",
    "attribution",
)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(tmp_path / "data", frontend_dist=tmp_path / "dist")
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client


def test_exercises_returns_seed_catalog_with_full_fields(
    client: TestClient,
) -> None:
    response = client.get("/api/exercises")
    assert response.status_code == 200
    exercises = response.json()["exercises"]
    assert exercises
    by_id = {exercise["id"]: exercise for exercise in exercises}

    for exercise in exercises:
        assert set(exercise) == set(FIELDS)
        assert exercise["record_type"] in {"reps_weight", "reps_bodyweight", "time"}
        assert exercise["modes"]
        assert exercise["source_ref"]
        assert exercise["attribution"]
        assert isinstance(exercise["recommendable"], bool)

    # 外加负重型种子：负重口径与最小加重单位齐备。
    assert by_id["barbell-back-squat"] == {
        "id": "barbell-back-squat",
        "standard_name_zh": "杠铃背蹲",
        "equipment_variant": "barbell",
        "record_type": "reps_weight",
        "load_convention": "barbell_includes_bar_total",
        "min_load_increment_kg": 2.5,
        "recommendable": True,
        "modes": ["深蹲"],
        "source_ref": "exercises-dataset:0043",
        "attribution": "© Gym visual — https://gymvisual.com/",
    }

    # 自重种子：不得虚构 0kg 或口径。
    assert by_id["pull-up"]["load_convention"] is None
    assert by_id["pull-up"]["min_load_increment_kg"] is None
    assert by_id["pull-up"]["record_type"] == "reps_bodyweight"
