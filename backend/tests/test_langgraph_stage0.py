"""LangGraph 重构阶段 0/1 Gate。"""

import logging
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from api.app import create_app
from config import DATABASE_FILENAME

# Stage 1 子任务 01 的 7 张业务表；AUTOINCREMENT 会附建 sqlite_sequence。
BUSINESS_TABLES = {
    "athlete_profile",
    "exercises",
    "body_metrics",
    "workout_sessions",
    "workout_sets",
    "plans",
    "plan_sessions",
}


def test_startup_creates_new_database_with_business_schema(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    legacy_database = data_dir / "app.db"
    legacy_database.write_bytes(b"legacy database must remain untouched")

    with TestClient(create_app(data_dir), base_url="http://127.0.0.1") as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["provider_has_api_key"] is False

    path = data_dir / DATABASE_FILENAME
    assert DATABASE_FILENAME == "fit_agent_langgraph.db"
    assert legacy_database.read_bytes() == b"legacy database must remain untouched"
    assert path.is_file()
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        # 新版只建业务表；LangGraph Checkpoint 表不在业务迁移中创建。
        assert tables == BUSINESS_TABLES | {"sqlite_sequence"}
        # 002 之后的最新迁移编号（迁移由编号累加，不回退）。
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_api_key_never_appears_in_logs(
    monkeypatch, caplog, tmp_path: Path
) -> None:
    secret = "stage-zero-secret-must-not-leak"
    monkeypatch.setenv("MODEL_API_KEY", secret)

    with caplog.at_level(logging.DEBUG):
        with TestClient(
            create_app(tmp_path / "data"), base_url="http://127.0.0.1"
        ) as client:
            response = client.get("/healthz")
            assert response.json()["provider_has_api_key"] is True
            assert secret not in response.text

    assert secret not in caplog.text
