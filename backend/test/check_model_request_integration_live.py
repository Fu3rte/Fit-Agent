import json
from pathlib import Path
from uuid import uuid4

from app.interfaces.http import app
from app.model_config import load_model_config
from test.check_http import create_session, events, wait_idle
from test.regression_support import Server, client, patch_default_database

EVIDENCE = Path(__file__).resolve().parents[2] / "tmp" / "design" / "model-request-budget-live.json"


def check() -> None:
    config = load_model_config()
    patch_default_database("model-request-integration-live")
    session_id = str(uuid4())
    request = "请调用 calculate_date，days_offset=0 查询今天的日期，然后用一句话回复查询到的日期。"
    with Server(app) as server, client(server.base_url) as http:
        create_session(http, session_id, "模型请求预算集成")
        with http.stream(
            "POST",
            "/api/agent/run",
            json={
                "session_id": session_id,
                "operation_id": str(uuid4()),
                "request": request,
                "attachments": [],
            },
        ) as response:
            received = list(events(response))
            run_id = response.headers["X-Run-ID"]
            request_entry_id = response.headers["X-Request-Entry-ID"]
        wait_idle()
        assert received[-1]["event"] == "done", received[-1]
        assert received[-1]["data"]["stop_reason"] == "stop"
        date_calls = {
            event["data"]["tool_call_id"]
            for event in received
            if event["event"] == "tool_start" and event["data"]["name"] == "calculate_date"
        }
        completed_calls = {
            event["data"]["tool_call_id"]
            for event in received
            if event["event"] == "tool_result" and not event["data"]["is_error"]
        }
        assert date_calls and date_calls <= completed_calls
        history_response = http.get(f"/api/sessions/{session_id}/history")
        assert history_response.status_code == 200
        history = history_response.json()
        run = next(item for item in history["runs"] if item["run_id"] == run_id)
        assert run["status"] == "completed" and run["error_code"] is None
        assert http.get(f"/api/sessions/{session_id}/history").json() == history
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(
        json.dumps(
            {
                "model": config.model,
                "session_id": session_id,
                "run_id": run_id,
                "request_entry_id": request_entry_id,
                "request": request,
                "events": received,
                "history": history,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print("PASS: 真实模型、真实 HTTP/SSE、只读工具续接及持久化历史一致")


if __name__ == "__main__":
    check()
