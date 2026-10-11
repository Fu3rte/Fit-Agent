from uuid import uuid4

from app import model_config
from app.application.business.catalog import Catalog
from app.infrastructure.persistence.sqlite import database as database_module
from app.interfaces.http import app
from test.regression_support import Server, client, temporary_root


def test_exercise_equipment_http(monkeypatch):
    directory = temporary_root("exercise-equipment-http") / uuid4().hex
    directory.mkdir()
    monkeypatch.setattr(database_module, "default_database_path", lambda: directory / "test.db")
    monkeypatch.setattr(model_config, "DATA_ROOT", directory)
    expected = {exercise.id: exercise.equipment for exercise in Catalog.load().all()}
    assert "body weight" in expected.values()
    with Server(app) as server, client(server.base_url) as http:
        response = http.get("/api/exercises/equipment")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == expected
        for suffix in ("?unknown=1", "?page=1&page=2"):
            response = http.get("/api/exercises/equipment" + suffix)
            assert response.status_code == 422
            assert response.json()["detail"]["code"] == "invalid_request"
        for headers in ({"Host": "invalid.example"}, {"Origin": "http://invalid.example"}):
            assert http.get("/api/exercises/equipment", headers=headers).status_code == 403
    with Server(app) as server, client(server.base_url) as http:
        assert http.get("/api/exercises/equipment").json() == expected
