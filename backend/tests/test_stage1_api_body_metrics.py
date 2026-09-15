"""Stage 1 子任务 04 §9：身体指标表单 API（/api/body-metrics）。

对照 04 清单：新增／修改／删除、体重必填、体脂可选（未记录保持 null，不补 0）、数值越界被拒
（422）、不存在资源 404、缺字段与未知字段被拒（400）。

测试用 ``TestClient`` 走真实回环 ``base_url``（回环守卫要求），只用 pytest ``tmp_path`` 下的
独立临时库；不 import ``tests.support``（它依赖已移除的 pydantic_ai）。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app

MEASURED_ON = "2026-06-03"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(tmp_path / "data", frontend_dist=tmp_path / "dist")
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client


def test_create_read_update_delete_roundtrip(client: TestClient) -> None:
    created = client.post(
        "/api/body-metrics",
        json={"measured_on": MEASURED_ON, "weight_kg": 70.5},
    )
    assert created.status_code == 200
    metric = created.json()["metric"]
    metric_id = metric["id"]
    # 体脂未记录：保持 null，不补 0。
    assert metric == {
        "id": metric_id,
        "measured_on": MEASURED_ON,
        "weight_kg": 70.5,
        "body_fat_pct": None,
    }

    listed = client.get("/api/body-metrics")
    assert listed.status_code == 200
    assert listed.json() == {"metrics": [metric]}

    updated = client.put(
        f"/api/body-metrics/{metric_id}",
        json={"measured_on": MEASURED_ON, "weight_kg": 70.2, "body_fat_pct": 18.5},
    )
    assert updated.status_code == 200
    assert updated.json()["metric"] == {
        "id": metric_id,
        "measured_on": MEASURED_ON,
        "weight_kg": 70.2,
        "body_fat_pct": 18.5,
    }

    # 把体脂改回「未记录」（显式 null）同样保持空值。
    cleared = client.put(
        f"/api/body-metrics/{metric_id}",
        json={"measured_on": MEASURED_ON, "weight_kg": 70.2, "body_fat_pct": None},
    )
    assert cleared.status_code == 200
    assert cleared.json()["metric"]["body_fat_pct"] is None

    assert client.delete(f"/api/body-metrics/{metric_id}").status_code == 204
    assert client.get("/api/body-metrics").json() == {"metrics": []}


def test_out_of_range_values_are_rejected(client: TestClient) -> None:
    cases: list[tuple[dict[str, object], int]] = [
        # 体重越界（20–400kg）。
        ({"measured_on": MEASURED_ON, "weight_kg": 700.0}, 422),
        ({"measured_on": MEASURED_ON, "weight_kg": 10.0}, 422),
        # 体脂越界（0–100%）。
        ({"measured_on": MEASURED_ON, "weight_kg": 70.0, "body_fat_pct": 101.0}, 422),
        # 缺体重（必填）。
        ({"measured_on": MEASURED_ON}, 400),
        # 非法类型。
        ({"measured_on": MEASURED_ON, "weight_kg": "七十"}, 400),
        # 未知字段。
        ({"measured_on": MEASURED_ON, "weight_kg": 70.0, "height_cm": 180}, 400),
    ]

    for payload, expected in cases:
        response = client.post("/api/body-metrics", json=payload)
        assert response.status_code == expected, (payload, response.text)
        assert response.json()["error_code"] == "invalid_request"
        assert response.json()["http_status"] == expected

    assert client.get("/api/body-metrics").json() == {"metrics": []}


def test_missing_resource_is_404(client: TestClient) -> None:
    assert client.put(
        "/api/body-metrics/999",
        json={"measured_on": MEASURED_ON, "weight_kg": 70.0},
    ).status_code == 404
    assert client.delete("/api/body-metrics/999").status_code == 404
