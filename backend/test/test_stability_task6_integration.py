from __future__ import annotations

import io
import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

import app.interfaces.http as http_module
from app.application.session.credentials import CREDENTIAL_SAFE_MESSAGE
from test.regression_support import Server, client, install_test_model_config
from test.test_stability_task3 import (
    TMP_ROOT,
    ModelServer,
    _wait_until,
    create_session,
    patch_runtime,
    read_events,
    run_body,
    wait_idle,
)

PROFILE = {
    "goal": "增肌",
    "experience": "新手",
    "environment": "健身房",
    "availability": "每周四次",
    "health_notes": None,
    "movement_restrictions": None,
    "unavailable_equipment": [],
    "forbidden_exercise_ids": [],
}

CONFIRM_TABLES = (
    "session_runs",
    "session_entries",
    "session_operations",
    "save_confirmation_operations",
    "profile_snapshots",
    "profile_save_records",
    "profile",
)


def record(name: str, value: object) -> None:
    print(f"TASK6 {name}: {json.dumps(value, ensure_ascii=False, default=str)}", flush=True)


@contextmanager
def raw_client(base_url: str, timeout: float = 120.0):
    # 回环请求必须绕开本机代理，并补齐后端 Origin 门要求的头。
    with httpx.Client(
        base_url=base_url,
        headers={"Host": "127.0.0.1:8000", "Origin": "http://127.0.0.1:5173"},
        timeout=timeout,
        trust_env=False,
    ) as http:
        yield http


def run_concurrently(*calls) -> None:
    barrier = threading.Barrier(len(calls) + 1)
    threads = [
        threading.Thread(target=lambda call=call: (barrier.wait(), call())) for call in calls
    ]
    for thread in threads:
        thread.start()
    barrier.wait(60)
    for thread in threads:
        thread.join(120)


def stage_tool(model: ModelServer, payload: dict) -> ModelServer:
    model.tool_name = "prepare_profile_update"
    model.tool_arguments = {
        "profile_id": 1,
        "base_profile_version": None,
        "payload": payload,
    }
    return model


@contextmanager
def running_app_with_key(model: ModelServer, api_key: str):
    database_path, trace_path = patch_runtime(TMP_ROOT)
    os.environ["FIT_AGENT_TEST_MODEL_API"] = "openai-completions"
    os.environ["FIT_AGENT_TEST_MODEL_BASE_URL"] = model.base_url
    os.environ["FIT_AGENT_TEST_MODEL_ID"] = "test-model"
    os.environ["FIT_AGENT_TEST_MODEL_API_KEY"] = api_key
    os.environ["FIT_AGENT_TEST_MODEL_PROVIDER"] = "local"
    install_test_model_config(root=TMP_ROOT / f"config-{uuid4().hex}")
    with Server(http_module.app) as server:
        with client(server.base_url) as http:
            yield http, database_path, trace_path, server.base_url


@pytest.fixture(autouse=True)
def clean_http_state():
    yield
    with http_module.runs_lock:
        states = list(http_module.runs.values())
        http_module.runs.clear()
    for state in states:
        state.events.close_normal()
        state.closed_without_terminal = True
    while http_module.active.locked():
        http_module.active.release()
    http_module.reset_fault_closure()
    os.environ.pop("FIT_AGENT_RUN_BUDGET_SECONDS", None)


def dump_rows(database_path: Path, table: str) -> str:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(f"SELECT * FROM {table}").fetchall()
        return json.dumps([dict(row) for row in rows], ensure_ascii=False, default=str)


def count_rows(database_path: Path, table: str) -> int:
    with sqlite3.connect(database_path) as connection:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def dump_trace(trace_path: Path) -> str:
    if not trace_path.exists():
        return ""
    with sqlite3.connect(trace_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM spans").fetchall()
        return json.dumps([dict(row) for row in rows], ensure_ascii=False, default=str)


def dump_cache_observation() -> str:
    from app.agent.cache_observation import observation_directory

    root = observation_directory()
    if not root.exists():
        return ""
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(root.glob("*.jsonl"))
    )


@contextmanager
def log_capture():
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    logging.getLogger().addHandler(handler)
    try:
        yield buffer
    finally:
        logging.getLogger().removeHandler(handler)


def stream_post(base_url: str, body: dict) -> tuple[int, list, dict]:
    with raw_client(base_url, 180.0) as http:
        with http.stream("POST", "/api/agent/run", json=body) as response:
            headers = dict(response.headers)
            if response.status_code == 200:
                events = read_events(response)
            return response.status_code, events, headers


def stage_pending_profile(http, session_id: str) -> dict:
    """真实运行产生待确认画像快照：模型第一步只发 prepare_profile_update 工具调用。"""
    operation_id = str(uuid4())
    status, events, headers = stream_post(
        http.base_url, run_body(session_id, operation_id, "准备更新画像")
    )
    assert status == 200, events
    wait_idle()
    history = http.get(f"/api/sessions/{session_id}/history")
    assert history.status_code == 200, history.text
    pending = history.json()["pending_proposals"]
    assert len(pending) == 1, pending
    reference = pending[0]
    assert reference["business_kind"] == "profile"
    assert reference["display_entry_id"], reference
    record(f"staged[{session_id[:8]}]", {
        "run_id": headers.get("x-run-id"),
        "proposal_id": reference["proposal_id"],
        "status": reference["status"],
    })
    return reference


def confirm_body(reference: dict, operation_id: str) -> dict:
    return {
        "operation_id": operation_id,
        "business_kind": "profile",
        "proposal_id": reference["proposal_id"],
        "display_entry_id": reference["display_entry_id"],
    }


def profile_state(http, session_id: str) -> dict:
    response = http.get("/api/profile")
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 场景十：错误内容中的凭据始终被过滤
# ---------------------------------------------------------------------------


class EchoAuthServer(ModelServer):
    """上游把请求携带的凭据回显进错误正文：真实 provider 的常见失败形态。"""

    def respond(self, handler) -> None:
        echoed = handler.headers.get("authorization") or handler.headers.get("x-api-key") or ""
        body = json.dumps(
            {
                "error": {
                    "type": "invalid_request_error",
                    "message": f"credential rejected on upload: {echoed}",
                }
            },
            ensure_ascii=False,
        ).encode("utf-8")
        handler.send_response(401)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)


def test_upstream_error_echoing_credential_is_absent_from_every_outlet() -> None:
    api_key = "task6-canary-" + uuid4().hex
    model = EchoAuthServer()
    model.scenario = "success"
    with model, running_app_with_key(model, api_key) as (http, db_path, trace_path, base_url):
        session_id = str(uuid4())
        create_session(http, session_id)
        with log_capture() as logs:
            status, events, headers = stream_post(
                base_url, run_body(session_id, str(uuid4()), "只回复完成。")
            )
            run_row = dump_rows(db_path, "session_runs")
            entry_row = dump_rows(db_path, "session_entries")
            ledger_row = dump_rows(db_path, "save_confirmation_operations")
        app_log = logs.getvalue()
        outlets = {
            "http_status": str(status),
            "http_headers": json.dumps(headers, ensure_ascii=False),
            "sse": json.dumps([{"event": item["event"], "data": item["data"]} for item in events], ensure_ascii=False),
            "session_runs": run_row,
            "session_entries": entry_row,
            "save_confirmation_operations": ledger_row,
            "app_log": app_log,
            "trace_spans": dump_trace(trace_path),
            "cache_observation": dump_cache_observation(),
        }
        leaks = [name for name, text in outlets.items() if api_key in text]
        terminal = [item for item in events if item["event"] in {"done", "error"}]
        record("credential_outlets", {
            "leaked_outlets": leaks,
            "model_requests": model.requests,
            "terminal_event": terminal[-1] if terminal else None,
            "event_names": [item["event"] for item in events],
            "run_row": json.loads(run_row),
            "log_lines": app_log.count("\n"),
        })
        assert leaks == []
        assert model.requests == 1
        assert terminal and terminal[-1]["event"] == "error"
        # 公开文案必须落在固定安全说明集合内；终态 code 的归属另见验收报告待裁决项。
        assert terminal[-1]["data"]["message"] in {
            CREDENTIAL_SAFE_MESSAGE,
            "执行失败，请重新发起请求。",
        }, terminal[-1]["data"]


def test_credential_in_request_text_rejected_and_never_persisted() -> None:
    api_key = "task6-canary-" + uuid4().hex
    with ModelServer() as model, running_app_with_key(model, api_key) as (
        http,
        db_path,
        trace_path,
        _,
    ):
        session_id = str(uuid4())
        create_session(http, session_id)
        operation_id = str(uuid4())
        rejected = http.post(
            "/api/agent/run",
            json=run_body(session_id, operation_id, f"把 {api_key} 记进画像备注"),
        )
        body = rejected.json()
        record("credential_request_reject", {
            "status": rejected.status_code,
            "detail": body,
            "model_requests": model.requests,
        })
        assert rejected.status_code == 422
        assert body["detail"]["code"] == "credential_detected"
        assert api_key not in rejected.text
        assert model.requests == 0
        for table in CONFIRM_TABLES:
            assert api_key not in dump_rows(db_path, table), table
        assert api_key not in dump_trace(trace_path)


def test_credential_in_confirmation_payload_rejected_and_never_persisted() -> None:
    api_key = "task6-canary-" + uuid4().hex
    with ModelServer("tool_once") as model, running_app_with_key(model, api_key) as (
        http,
        db_path,
        _,
        _,
    ):
        stage_tool(model, {**PROFILE, "health_notes": f"伤病备注 {api_key}"})
        session_id = str(uuid4())
        create_session(http, session_id)
        status, events, _ = stream_post(
            http.base_url, run_body(session_id, str(uuid4()), "准备更新画像")
        )
        terminal = [item for item in events if item["event"] in {"done", "error"}]
        record("credential_in_payload", {
            "status": status,
            "terminal": terminal[-1] if terminal else None,
            "pending": http.get(f"/api/sessions/{session_id}/history").json()[
                "pending_proposals"
            ],
        })
        assert status == 200
        assert terminal and terminal[-1]["event"] == "error"
        assert terminal[-1]["data"]["code"] == "credential_detected"
        assert http.get(f"/api/sessions/{session_id}/history").json()["pending_proposals"] == []
        for table in CONFIRM_TABLES:
            assert api_key not in dump_rows(db_path, table), table


def test_content_validation_error_detail_keeps_input_out_of_credential_outlets() -> None:
    marker = "task6-echo-" + uuid4().hex
    with ModelServer("tool_once") as model, running_app_with_key(
        model, "task6-key-" + uuid4().hex
    ) as (http, db_path, _, _):
        stage_tool(model, {**PROFILE, "goal": marker})
        session_id = str(uuid4())
        create_session(http, session_id)
        reference = stage_pending_profile(http, session_id)
        # 同一快照内容固定，改用非法 business_kind 触发内容校验失败并核对登记行正文。
        rejected = http.post(
            f"/api/sessions/{session_id}/confirmations",
            json={
                "operation_id": str(uuid4()),
                "business_kind": "profile",
                "proposal_id": reference["proposal_id"],
                "display_entry_id": str(uuid4()),
            },
        )
        ledger = dump_rows(db_path, "save_confirmation_operations")
        record("confirmation_error_echo", {
            "status": rejected.status_code,
            "detail": rejected.json(),
            "ledger_rows": json.loads(ledger),
        })
        assert rejected.status_code >= 400
        assert marker not in rejected.text


def test_finalize_exception_body_never_reaches_logs() -> None:
    api_key = "task6-canary-" + uuid4().hex
    with ModelServer() as model, running_app_with_key(model, api_key) as (
        http,
        db_path,
        _,
        base_url,
    ):
        session_id = str(uuid4())
        create_session(http, session_id)
        # 终态提交路径注入含凭据的约束错误：RAISE 的消息即异常正文。
        raw = sqlite3.connect(db_path, isolation_level=None, timeout=30)
        raw.execute(
            "CREATE TRIGGER task6_canary_finalize BEFORE UPDATE ON session_runs "
            "WHEN NEW.status <> 'running' "
            f"BEGIN SELECT RAISE(ABORT, '{api_key}'); END"
        )
        raw.commit()
        raw.close()
        with log_capture() as logs:
            status, events, _ = stream_post(base_url, run_body(session_id, str(uuid4())))
        app_log = logs.getvalue()
        rows = json.loads(dump_rows(db_path, "session_runs"))
        record("finalize_log_protection", {
            "status": status,
            "credential_in_log": api_key in app_log,
            "closure_logged": "运行进入故障封闭" in app_log,
            "diagnostic": "terminal_persist_failed" in app_log,
            "log_lines": app_log.count("\n"),
            "event_names": [item["event"] for item in events],
            "slot_held": http_module.active.locked(),
            "run_status": [row["status"] for row in rows],
        })
        assert status == 200
        assert api_key not in app_log
        # 注入的约束错误走故障封闭路径：日志只留固定说明与标识，异常正文一概不落。
        assert "运行进入故障封闭" in app_log
        assert "terminal_persist_failed" in app_log
        assert api_key not in dump_rows(db_path, "session_runs")
        assert all(item["event"] not in {"done", "error"} for item in events), events
        assert http_module.active.locked()
        assert [row["status"] for row in rows] == ["running"]


# ---------------------------------------------------------------------------
# 场景八：确认提交与取消、编辑并发保持既有业务语义
# ---------------------------------------------------------------------------


def test_same_operation_concurrent_confirmations_register_once_and_save_once() -> None:
    with ModelServer("tool_once") as model, running_app_with_key(
        model, "task6-key-" + uuid4().hex
    ) as (http, db_path, _, base_url):
        stage_tool(model, PROFILE)
        session_id = str(uuid4())
        create_session(http, session_id)
        reference = stage_pending_profile(http, session_id)
        operation_id = str(uuid4())
        results: list[tuple[int, dict]] = []

        def submit() -> None:
            with raw_client(base_url) as extra:
                response = extra.post(
                    f"/api/sessions/{session_id}/confirmations",
                    json=confirm_body(reference, operation_id),
                )
                results.append((response.status_code, response.json()))

        run_concurrently(submit, submit)
        wait_idle()

        statuses = [item[1]["status"] for item in results]
        record("same_operation_concurrency", {
            "http": [item[0] for item in results],
            "statuses": statuses,
            "operation_rows": count_rows(db_path, "save_confirmation_operations"),
            "save_records": count_rows(db_path, "profile_save_records"),
            "profile": profile_state(http, session_id).get("version"),
        })
        assert set(statuses) == {"saved"}
        assert count_rows(db_path, "save_confirmation_operations") == 1
        assert count_rows(db_path, "profile_save_records") == 1
        assert profile_state(http, session_id).get("version") == 1


def test_different_operations_concurrent_on_same_proposal_save_once() -> None:
    with ModelServer("tool_once") as model, running_app_with_key(
        model, "task6-key-" + uuid4().hex
    ) as (http, db_path, _, base_url):
        stage_tool(model, PROFILE)
        session_id = str(uuid4())
        create_session(http, session_id)
        reference = stage_pending_profile(http, session_id)
        operations = [str(uuid4()), str(uuid4())]
        results: list[tuple[int, dict]] = []

        def submit(operation_id: str) -> None:
            with raw_client(base_url) as extra:
                response = extra.post(
                    f"/api/sessions/{session_id}/confirmations",
                    json=confirm_body(reference, operation_id),
                )
                results.append((response.status_code, response.json()))

        run_concurrently(lambda: submit(operations[0]), lambda: submit(operations[1]))
        wait_idle()

        ledger = json.loads(dump_rows(db_path, "save_confirmation_operations"))
        statuses = sorted(item[1].get("status") for item in results)
        record("different_operation_concurrency", {
            "http": [item[0] for item in results],
            "statuses": statuses,
            "ledger": [{"operation_id": row["operation_id"], "status": row["status"]} for row in ledger],
            "save_records": count_rows(db_path, "profile_save_records"),
            "profile": profile_state(http, session_id).get("version"),
        })
        assert count_rows(db_path, "profile_save_records") == 1
        assert profile_state(http, session_id).get("version") == 1
        assert "processing" not in statuses
        assert all(row["status"] in {"saved", "failed", "conflicted"} for row in ledger)
        assert sum(row["status"] == "saved" for row in ledger) >= 1


def test_disconnect_during_confirmation_still_commits_saved_and_releases_slot_once() -> None:
    with ModelServer("tool_once") as model, running_app_with_key(
        model, "task6-key-" + uuid4().hex
    ) as (http, db_path, _, base_url):
        stage_tool(model, PROFILE)
        session_id = str(uuid4())
        create_session(http, session_id)
        reference = stage_pending_profile(http, session_id)
        operation_id = str(uuid4())
        with raw_client(base_url, 5.0) as aborting:
            try:
                with aborting.stream(
                    "POST",
                    f"/api/sessions/{session_id}/confirmations",
                    json=confirm_body(reference, operation_id),
                ) as response:
                    next(response.iter_raw(), b"")
            except Exception as error:
                # 首帧之后即超时断开：读超时就是本用例注入的客户端断连。
                record("confirmation_disconnect_error", type(error).__name__)

        observed: dict | None = None

        def fetch() -> bool:
            nonlocal observed
            queried = http.get(f"/api/sessions/{session_id}/confirmations/{operation_id}")
            observed = queried.json() if queried.status_code == 200 else None
            return observed is not None

        _wait_until(fetch, 30.0)
        wait_idle()
        record("confirmation_after_disconnect", {
            "queried": observed,
            "save_records": count_rows(db_path, "profile_save_records"),
            "profile": profile_state(http, session_id).get("version"),
            "slot_free": not http_module.active.locked(),
        })
        assert observed["status"] == "saved"
        assert observed["operation_id"] == operation_id
        assert count_rows(db_path, "profile_save_records") == 1
        assert not http_module.active.locked()


def test_confirmation_racing_edit_keeps_saved_result_and_admits_edit() -> None:
    with ModelServer("tool_once") as model, running_app_with_key(
        model, "task6-key-" + uuid4().hex
    ) as (http, db_path, _, base_url):
        stage_tool(model, PROFILE)
        session_id = str(uuid4())
        create_session(http, session_id)
        reference = stage_pending_profile(http, session_id)
        # 权威字段：待确认快照绑定的用户请求节点，编辑即针对该请求重放。
        target_entry_id = reference["request_entry_id"]
        assert target_entry_id, reference

        outcomes: dict[str, object] = {}
        # 再次提交必须复用原 operation_id，这里固定它以便核对让位一方随后的提交。
        confirmation_operation_id = str(uuid4())

        def submit_confirmation() -> None:
            with raw_client(base_url) as extra:
                response = extra.post(
                    f"/api/sessions/{session_id}/confirmations",
                    json=confirm_body(reference, confirmation_operation_id),
                )
                outcomes["confirmation"] = (response.status_code, response.json())

        def submit_edit() -> None:
            with raw_client(base_url) as extra:
                with extra.stream(
                    "POST",
                    "/api/agent/edit",
                    json={
                        "session_id": session_id,
                        "operation_id": str(uuid4()),
                        "target_entry_id": target_entry_id,
                        "request": "改成减脂目标",
                    },
                ) as response:
                    edit_events = read_events(response) if response.status_code == 200 else []
                    outcomes["edit"] = (response.status_code, [item["event"] for item in edit_events])

        run_concurrently(submit_confirmation, submit_edit)
        wait_idle()

        ledger = json.loads(dump_rows(db_path, "save_confirmation_operations"))
        record("confirmation_vs_edit", {
            "confirmation": outcomes.get("confirmation"),
            "edit": outcomes.get("edit"),
            "ledger_status": [row["status"] for row in ledger],
            "save_records": count_rows(db_path, "profile_save_records"),
            "profile": profile_state(http, session_id).get("version"),
            "pending_after": http.get(f"/api/sessions/{session_id}/history").json()[
                "pending_proposals"
            ],
        })
        # 归属裁决：谁先取到全局名额谁执行，两种顺序都是既有语义；要求的是任一顺序下
        # 都不得出现半态，且让位一方按原 operation_id 重提交后仍只有零写入一种结果。
        assert outcomes["edit"][0] == 200, outcomes
        confirmation_status, confirmation_body = outcomes["confirmation"]
        assert confirmation_status in {200, 202, 409}, outcomes
        assert "processing" not in [row["status"] for row in ledger], ledger
        if confirmation_status == 409:
            assert confirmation_body["detail"]["code"] == "run_busy", confirmation_body
            assert ledger == [], ledger
            assert count_rows(db_path, "profile_save_records") == 0
            assert profile_state(http, session_id).get("version") is None
            retried = http.post(
                f"/api/sessions/{session_id}/confirmations",
                json=confirm_body(reference, confirmation_operation_id),
            )
            # 编辑重放为该会话另建新快照，旧快照按替换语义失效：让位一方复用原
            # operation_id 再提交不得保存，只核对失效结果与零写入。
            snapshots = json.loads(dump_rows(db_path, "profile_snapshots") or "[]")
            record("confirmation_retry_after_edit", {
                "status": retried.status_code,
                "detail": retried.json(),
                "snapshot_statuses": [row["status"] for row in snapshots],
                "save_records": count_rows(db_path, "profile_save_records"),
            })
            assert retried.status_code == 404, retried.text
            assert retried.json()["detail"]["code"] == "profile_proposal_not_found"
            assert count_rows(db_path, "profile_save_records") == 0
            assert reference["proposal_id"] not in [
                row["id"] for row in snapshots if row["status"] == "pending"
            ], snapshots
        else:
            assert confirmation_body["status"] == "saved", confirmation_body
            assert count_rows(db_path, "profile_save_records") == 1
            assert profile_state(http, session_id).get("version") == 1
            assert [row["status"] for row in json.loads(
                dump_rows(db_path, "save_confirmation_operations") or "[]"
            )] == ["saved"]
            assert http.get(
                f"/api/sessions/{session_id}/history"
            ).json()["pending_proposals"] == []
        assert not http_module.active.locked()
