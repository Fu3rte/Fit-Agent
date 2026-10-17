import asyncio
from threading import Event
from uuid import uuid4

import pytest
from fastapi import FastAPI
from starlette.responses import StreamingResponse

from app import model_config
from app.agent import cache_observation, telemetry
from app.application.session import service as session_service
from app.infrastructure.persistence.sqlite import database as database_module
from app.interfaces.http import app
from test.check_http import create_session
from test.regression_support import Server, client
from test.v2_business_cases import _real_turn
from test.v2_eval_support import business_state, capture_turn


@pytest.fixture
def isolated_http(tmp_path, monkeypatch):
    monkeypatch.setattr(database_module, "default_database_path", lambda: tmp_path / "business.db")
    monkeypatch.setattr(telemetry, "trace_database_path", lambda: tmp_path / "trace.db")
    monkeypatch.setattr(model_config, "DATA_ROOT", tmp_path / "config")
    monkeypatch.setattr(session_service, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(cache_observation, "observation_directory", lambda: tmp_path / "observations")
    with Server(app) as server, client(server.base_url) as connection:
        session_id = str(uuid4())
        create_session(connection, session_id)
        yield connection, session_id


def invalid_upload():
    return [{
        "kind": "upload",
        "attachment_id": str(uuid4()),
        "file_name": "plan.md",
        "data_base64": "!",
    }]


def test_capture_turn_reads_rejected_attachment_response(isolated_http):
    connection, session_id = isolated_http
    attachments = invalid_upload()
    before = business_state()
    response = connection.post("/api/agent/run", json={
        "session_id": session_id,
        "operation_id": str(uuid4()),
        "request": "读取附件",
        "attachments": attachments,
    })
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "attachment_format_invalid"
    turn = capture_turn(
        connection, session_id, case_id="L06", round_index=0, step_index=1,
        request_text="读取附件", attachments=attachments,
    )
    assert turn.error_type == "builtins.RuntimeError"
    assert turn.error == f"运行请求失败: 422 {response.text}"
    assert turn.terminal == "error" and turn.status == "failed"
    assert turn.run_id is None and turn.request_entry_id is None
    assert turn.events[-1]["data"]["error"] == turn.error
    assert turn.before_state == turn.after_state == before
    assert connection.get(f"/api/sessions/{session_id}/history").json()["entries"] == []
    assert connection.get("/api/exercises/equipment").status_code == 200


def test_real_turn_preserves_rejected_attachment_detail(isolated_http):
    connection, session_id = isolated_http
    before = business_state()
    with pytest.raises(RuntimeError, match="前置真实回合失败：attachment terminal=error") as caught:
        _real_turn(
            connection, session_id, label="attachment", text="读取附件",
            attachments=invalid_upload(),
        )
    assert "运行请求失败: 422" in str(caught.value)
    assert "attachment_format_invalid" in str(caught.value)
    assert business_state() == before


def test_capture_turn_consumes_success_as_stream(isolated_http):
    _, session_id = isolated_http
    release_tail = Event()
    closed = Event()

    protocol = FastAPI()

    @protocol.post("/api/agent/run")
    async def stream():
        # 本地协议样本：done 后保持连接，验证客户端按事件退出并关闭响应。
        async def chunks():
            try:
                yield 'event: message_update\ndata: {"update_type":"text_delta","content":[{"text":"完成"}]}\n\n'
                yield 'event: done\ndata: {}\n\n'
                await asyncio.to_thread(release_tail.wait, 3)
            finally:
                closed.set()

        return StreamingResponse(chunks(), media_type="text/event-stream", headers={
            "X-Run-ID": str(uuid4()), "X-Request-Entry-ID": str(uuid4()),
        })

    before = business_state()
    with Server(protocol) as server, client(server.base_url, timeout=1) as connection:
        try:
            turn = capture_turn(
                connection, session_id, case_id="protocol", round_index=0, step_index=1,
                request_text="读取协议样本", attachments=[],
            )
            assert turn.status == "done" and turn.terminal == "done"
            assert turn.error is None and turn.error_type is None
            assert [event["event"] for event in turn.events] == ["message_update", "done"]
            assert turn.client_first_body_ms is not None
            assert turn.run_id and turn.request_entry_id
            assert turn.before_state == turn.after_state == before
            assert closed.wait(2)
        finally:
            release_tail.set()
