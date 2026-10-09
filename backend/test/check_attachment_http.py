import asyncio
import base64
import json
import stat
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event
from uuid import uuid4

import httpx
import pytest

from app.application.session.service import SessionService
from app.application.session.steering import SteeringCoordinator
from app.domain.session.attachments import attachment_storage_ref
from app.domain.session.models import (
    EditCommand,
    EditRequest,
    RegenerateCommand,
    RegenerateRequest,
)
from app.infrastructure.persistence.sqlite import database as database_module
from app.interfaces import http
from app.model_config import load_model_config
from test.check_attachment_session import SYSTEM, reference, send_command, uid, upload
from test.regression_support import Server, client

ROOT = Path(__file__).resolve().parents[2] / "tmp/backend-plan-import/checks"
PUBLIC_ATTACHMENT = {"attachment_id", "file_name", "size_bytes", "created_at"}


def assert_error(response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()) == {"detail"}
    assert response.json()["detail"]["code"] == code
    assert set(response.json()["detail"]) in ({"code", "message"}, {"code", "message", "reason"})


def invoke(coro):
    return asyncio.run_coroutine_threadsafe(coro, http.app.state.loop).result(10)


def listener_checks(root: Path, connection) -> None:
    service = http.app.state.session_service
    coordinator = http.app.state.steering
    session, other = uid(), uid()
    for identity in (session, other):
        assert connection.post("/api/sessions", json={"session_id": identity, "title": "计划"}).status_code == 201
    file = upload("# 训练\r\n深蹲\ufeff".encode("utf-8"), "计划.MD")
    command = send_command(session, "", [file])
    accepted = invoke(service.accept_send(command, system_message=SYSTEM))
    duplicate_payload = {"session_id": session, "operation_id": command.operation_id, "request": "", "attachments": [file]}
    duplicate = connection.post("/api/agent/run", json=duplicate_payload)
    assert duplicate.status_code == 200 and set(duplicate.json()) == {"operation_id", "session_id", "run_id", "request_entry_id", "status"}
    assert duplicate.json()["run_id"] == accepted.run.id
    get_path = f"/api/sessions/{session}/attachments/{file['attachment_id']}"
    original = connection.get(get_path)
    assert original.status_code == 200 and original.headers["cache-control"] == "no-store"
    assert original.headers["content-type"].startswith("application/json")
    assert set(original.json()) == PUBLIC_ATTACHMENT | {"text"}
    assert original.json()["text"].encode("utf-8") == base64.b64decode(file["data_base64"])
    assert "storage_ref" not in original.text and str(root) not in original.text
    assert_error(connection.get(get_path + "?path=elsewhere"), 422, "invalid_request")
    assert_error(connection.get(f"/api/sessions/{other}/attachments/{file['attachment_id']}"), 403, "attachment_access_denied")
    assert_error(connection.get(f"/api/sessions/{session}/attachments/{uid()}"), 404, "attachment_not_found")
    assert_error(connection.get(f"/api/sessions/{uid()}/attachments/{file['attachment_id']}"), 404, "session_not_found")
    assert_error(connection.get(get_path.replace(file["attachment_id"], file["attachment_id"].replace("-", ""))), 422, "invalid_request")
    assert_error(connection.get(get_path, headers={"Host": "evil"}), 403, "host_forbidden")
    assert_error(connection.get(get_path, headers={"Origin": "http://evil"}), 403, "origin_forbidden")
    history_path = f"/api/sessions/{session}/history"
    history = connection.get(history_path)
    assert history.status_code == 200 and history.headers["cache-control"] == "no-store"
    user = history.json()["entries"][-1]["message"]
    assert set(user) == {"role", "text", "timestamp", "attachments"} and user["text"] == ""
    assert set(user["attachments"][0]) == PUBLIC_ATTACHMENT
    assert "accepted_attachments" not in history.text and "storage_ref" not in history.text
    assert_error(connection.get(history_path + "?unknown=1"), 422, "invalid_request")
    operation_path = f"/api/sessions/{session}/operations/{command.operation_id}"
    operation = connection.get(operation_path)
    assert operation.status_code == 200 and operation.json()["accepted"]
    assert operation.json()["run"]["run_id"] == accepted.run.id
    assert "accepted_attachments" not in operation.text and "storage_ref" not in operation.text
    assert_error(connection.get(operation_path + "?unknown=1"), 422, "invalid_request")
    invoke(service.finish_run(session, accepted.run.id, "completed"))
    edit_command = EditCommand(operation_id=uid(), session_id=session,
        request=EditRequest(target_entry_id=accepted.run.request_entry_id, text=" "))
    edited = invoke(service.accept_edit(edit_command))
    edit_payload = {"session_id": session, "operation_id": edit_command.operation_id,
                    "target_entry_id": edit_command.request.target_entry_id, "request": " "}
    assert connection.post("/api/agent/edit", json=edit_payload).json()["run_id"] == edited.run.id
    assert_error(connection.post("/api/agent/edit", json={**edit_payload, "attachments": []}), 422, "invalid_request")
    invoke(service.finish_run(session, edited.run.id, "completed"))
    regenerate_command = RegenerateCommand(operation_id=uid(), session_id=session,
        request=RegenerateRequest(target_entry_id=edited.run.request_entry_id))
    regenerated = invoke(service.accept_regenerate(regenerate_command))
    assert connection.post("/api/agent/regenerate", json={"session_id": session, "operation_id": regenerate_command.operation_id,
        "target_entry_id": edited.run.request_entry_id}).json()["run_id"] == regenerated.run.id
    invoke(open_boundary(coordinator, session, regenerated.run.id))
    steering_path = f"/api/agent/runs/{regenerated.run.id}/steering"
    steering_file = upload(b"steering plan", "steering.txt")
    steering_payload = {"session_id": session, "operation_id": uid(), "message": "", "attachments": [steering_file]}
    response = connection.post(steering_path, json=steering_payload)
    assert response.status_code == 200 and response.json()["status"] == "accepted"
    steering_id = response.json()["steering_id"]
    assert not connection.post(steering_path, json=steering_payload).json()["created"]
    steering_operation = connection.get(f"/api/sessions/{session}/operations/{steering_payload['operation_id']}").json()
    assert set(steering_operation["steering"]) == {"session_id", "run_id", "steering_id", "status", "entry_id", "reason", "created_at", "updated_at", "attachments"}
    assert steering_operation["steering"]["attachments"][0]["attachment_id"] == steering_file["attachment_id"]
    assert set(steering_operation["steering"]["attachments"][0]) == PUBLIC_ATTACHMENT
    steering_history = connection.get(history_path).json()["steering"][-1]
    assert steering_history["text"] == "" and steering_history["attachments"] == steering_operation["steering"]["attachments"]
    listing = connection.get(f"/api/sessions/{session}/runs/{regenerated.run.id}/steering")
    assert listing.status_code == 200 and listing.json()["steering"][-1]["attachments"] == steering_history["attachments"]
    assert invoke(consume_boundary(coordinator, regenerated.run.id)) is not None
    consumed = connection.get(history_path).json()
    assert consumed["entries"][-1]["message"]["text"] == ""
    assert consumed["entries"][-1]["message"]["attachments"] == consumed["steering"][-1]["attachments"]
    assert_error(connection.post(f"{steering_path}/{steering_id}/withdraw", json={"session_id": session}), 409, "steering_consumption_conflict")
    withdraw_payload = {"session_id": session, "operation_id": uid(), "message": "撤回", "attachments": [reference(file["attachment_id"])]}
    withdrawable = connection.post(steering_path, json=withdraw_payload).json()
    assert connection.post(f"{steering_path}/{withdrawable['steering_id']}/withdraw", json={"session_id": session}).json()["status"] == "withdrawn"
    assert connection.get(f"/api/sessions/{session}/operations/{withdraw_payload['operation_id']}").json()["steering"]["attachments"][0]["attachment_id"] == file["attachment_id"]
    key = load_model_config().OPENAI_API_KEY
    for item in (upload(key.encode("utf-8")), upload(name=key + ".md")):
        rejected = connection.post(steering_path, json={**steering_payload, "operation_id": uid(), "attachments": [item]})
        assert_error(rejected, 422, "credential_detected")
        assert key not in rejected.text
    for item in (upload(b"\xff"), {**upload(), "data_base64": "!"}, upload(name="plan.pdf")):
        assert_error(connection.post(steering_path, json={**steering_payload, "operation_id": uid(), "attachments": [item]}), 422, "attachment_format_invalid")
    for item in (upload(name="../plan.md"), {**upload(), "extra": True}, {"kind": "reference", "attachment_id": True}):
        assert_error(connection.post(steering_path, json={**steering_payload, "operation_id": uid(), "attachments": [item]}), 422, "invalid_request")
    for changes in ({"attachments": None}, {"message": None}, {"message": "", "attachments": []}, {"extra": 1},
                    {"session_id": session.replace("-", "")}, {"operation_id": False}, {"accepted_attachments": []}):
        assert_error(connection.post(steering_path, json={**steering_payload, **changes}), 422, "invalid_request")
    exact = upload(b"x" * 100000)
    assert connection.post(steering_path, json={**steering_payload, "operation_id": uid(), "attachments": [exact]}).status_code == 200
    assert_error(connection.post(steering_path, json={**steering_payload, "operation_id": uid(), "attachments": [upload(b"x" * 100001)]}), 413, "attachment_size_exceeded")
    assert_error(connection.post(steering_path, json={**steering_payload, "operation_id": uid(), "attachments": [reference(file["attachment_id"])] * 2}), 422, "invalid_request")
    assert_error(connection.post(steering_path, json={**steering_payload, "operation_id": uid(), "attachments": [upload(identity=file["attachment_id"])]}), 409, "attachment_conflict")
    assert_error(connection.post(steering_path, json={**steering_payload, "operation_id": uid(), "attachments": [reference(uid())]}), 404, "attachment_not_found")
    cleanup_failure_checks(root, connection, service, session, regenerated.run.id, steering_path)
    stored_metadata_damage_checks(root, connection, service, session, steering_path)
    # 合法HTTP framing的chunked流由真实listener处理。
    for path in ("/api/agent/run", "/api/agent/edit", steering_path):
        oversized = connection.post(path, content=iter([b"{" + b" " * 600000, b" " * 600000]), headers={"Content-Type": "application/json"})
        assert_error(oversized, 413, "request_size_exceeded")
        exact_invalid = connection.post(path, content=iter([b"{" + b" " * 1048575]), headers={"Content-Type": "application/json"})
        assert_error(exact_invalid, 422, "invalid_request")
    metadata = invoke(service.get_attachment_content(session, file["attachment_id"]))[0]
    original_path = root / metadata.storage_ref
    original_path.chmod(stat.S_IWRITE | stat.S_IREAD)
    original_bytes = original_path.read_bytes()
    original_path.write_bytes(b"\xff" * metadata.size_bytes)
    assert_error(connection.get(get_path), 500, "internal_error")
    assert_error(connection.post(steering_path, json={**steering_payload, "operation_id": uid(), "attachments": [reference(file["attachment_id"])]}), 500, "internal_error")
    original_path.write_bytes(b"x")
    assert_error(connection.get(get_path), 500, "internal_error")
    original_path.write_bytes(original_bytes)
    missing_path = original_path.with_suffix(".retained")
    original_path.rename(missing_path)
    assert_error(connection.get(get_path), 500, "internal_error")
    assert_error(connection.post(steering_path, json={**steering_payload, "operation_id": uid(), "attachments": [reference(file["attachment_id"])]}), 500, "internal_error")
    missing_path.rename(original_path)
    safe_bytes = b"x" * len(key.encode())
    credential_file = upload(safe_bytes, "stored-private.txt")
    assert connection.post(steering_path, json={**steering_payload, "operation_id": uid(), "attachments": [credential_file]}).status_code == 200
    protected_meta = invoke(service.get_attachment_content(session, credential_file["attachment_id"]))[0]
    protected_path = root / protected_meta.storage_ref
    protected_path.chmod(stat.S_IWRITE | stat.S_IREAD)
    try:
        protected_path.write_bytes(key.encode())
        blocked = connection.get(f"/api/sessions/{session}/attachments/{credential_file['attachment_id']}")
        assert_error(blocked, 422, "credential_detected")
        assert key not in blocked.text
    finally:
        protected_path.write_bytes(safe_bytes)
        protected_path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    invoke(coordinator.finish(session, regenerated.run.id, "completed"))
    assert connection.delete(f"/api/sessions/{session}").json() == {"session_id": session, "deleted": True}
    assert original_path.read_bytes() == original_bytes
    assert_error(connection.get(get_path), 404, "session_not_found")
    assert connection.get(f"/api/sessions/{other}/history").json()["entries"] == []


def stored_metadata_damage_checks(root, connection, service, session, steering_path):
    item = upload(b"unchanged original bytes", "damaged.txt")
    operation = uid()
    assert connection.post(steering_path, json={"session_id": session, "operation_id": operation, "message": "", "attachments": [item]}).status_code == 200
    metadata = invoke(service.get_attachment_content(session, item["attachment_id"]))[0]
    database = service._repository._database
    path = root / "http.db"
    original = path.read_bytes()
    alternate_id = ("f" if item["attachment_id"][0] != "f" else "e") + item["attachment_id"][1:]
    alternate_ref = metadata.storage_ref.replace(item["attachment_id"], alternate_id)
    mutations = [
        (b"damaged.txt", b"damaged.pdf"),
        (metadata.storage_ref.encode(), alternate_ref.encode()),
        (b"damaged.txt", b"dam/ged.txt"),
    ]

    async def query(statement):
        cursor = await database.connection.execute(statement)
        result = await cursor.fetchall()
        await cursor.close()
        return result

    for source, replacement in mutations:
        assert len(source) == len(replacement) and source in original
        try:
            path.write_bytes(original.replace(source, replacement))
            invoke(query("PRAGMA shrink_memory"))
            uri = f"/api/sessions/{session}/attachments/{item['attachment_id']}"
            assert_error(connection.get(uri), 500, "internal_error")
            assert_error(connection.get(f"/api/sessions/{session}/operations/{operation}"), 500, "internal_error")
            assert_error(connection.get(f"/api/sessions/{session}/history"), 500, "internal_error")
            assert_error(connection.post(steering_path, json={"session_id": session, "operation_id": uid(), "message": "", "attachments": [reference(item["attachment_id"])]}), 500, "internal_error")
        finally:
            path.write_bytes(original)
            invoke(query("PRAGMA shrink_memory"))
        assert [tuple(row) for row in invoke(query("PRAGMA integrity_check"))] == [("ok",)]
        assert invoke(query("PRAGMA foreign_key_check")) == []
        restored = connection.get(uri)
        assert restored.status_code == 200 and restored.json()["file_name"] == "damaged.txt"
        assert restored.json()["text"] == "unchanged original bytes"
    assert_error(connection.post(steering_path, json={"session_id": session, "operation_id": uid(), "message": "", "attachments": [upload(b"safe", "new.pdf")]}), 422, "attachment_format_invalid")


def cleanup_failure_checks(root, connection, service, session, run_id, steering_path):
    entered, release = Event(), Event()
    database = service._repository._database

    def hold_operation() -> int:
        entered.set()
        assert release.wait(5)
        return 1

    async def arm():
        await database.connection.create_function("hold_cleanup_operation", 0, hold_operation)
        await database.connection.execute("CREATE TEMP TRIGGER fail_cleanup_operation BEFORE INSERT ON session_operations BEGIN SELECT hold_cleanup_operation(); SELECT RAISE(ABORT, 'controlled acceptance failure'); END")

    invoke(arm())
    item = upload(b"owned before rollback", "cleanup.txt")
    operation = uid()
    path = root / attachment_storage_ref(session, item["attachment_id"], item["file_name"])
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(connection.post, steering_path, json={"session_id": session, "operation_id": operation, "message": "", "attachments": [item]})
        try:
            assert entered.wait(5)
            assert path.read_bytes() == b"owned before rollback"
            path.chmod(stat.S_IWRITE | stat.S_IREAD)
            path.unlink()
        finally:
            release.set()
        response = pending.result(10)
    assert_error(response, 500, "internal_error")
    invoke(database.connection.execute("DROP TRIGGER fail_cleanup_operation"))
    assert invoke(service.get_operation_outcome(session, operation)) is None
    assert invoke(service._repository.get_attachments([item["attachment_id"]])) == {}
    assert invoke(service.get_run(session, run_id)).status == "running"
    assert str(root) not in response.text and item["file_name"] not in response.text


async def open_boundary(coordinator, session, run):
    coordinator.open(session, run, lambda event: None)


async def consume_boundary(coordinator, run):
    return await coordinator.consume(run, await coordinator.take(run))


async def session_fingerprint(service) -> list:
    repository = service._repository
    tables = ("sessions", "session_entries", "session_runs", "steering_inputs", "session_operations",
              "session_operation_invalidations", "session_attachments", "session_entry_attachments", "steering_input_attachments")

    async def read(connection):
        result = []
        for table in tables:
            cursor = await connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            result.append([tuple(row) for row in await cursor.fetchall()])
            await cursor.close()
        return result

    async with repository.transaction():
        return await repository._database.read(read)


async def asgi_body_checks(root: Path) -> None:
    transport = httpx.ASGITransport(app=http.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000", headers={"Host": "localhost:8000"}) as connection:
        paths = ("/api/agent/run", "/api/agent/edit", f"/api/agent/runs/{uid()}/steering")
        service = http.app.state.session_service
        before = await session_fingerprint(service)
        before_files = sorted(str(path) for path in root.glob("tmp/sessions/*/attachments/*"))
        for path in paths:
            for size in (1048576, 1048577):
                body = b"{" + b" " * (size - 1)
                for content_length in (None, "1", str(size)):
                    headers = [(b"host", b"localhost:8000"), (b"content-type", b"application/json")]
                    if content_length is not None:
                        headers.append((b"content-length", content_length.encode()))
                    frames = [{"type": "http.request", "body": body[:600000], "more_body": True},
                              {"type": "http.request", "body": body[600000:], "more_body": size > 1048576}]
                    if size > 1048576:
                        frames.append({"type": "http.request", "body": b"ignored after rejection", "more_body": False})
                    received, sent = [], []

                    async def receive():
                        if frames:
                            frame = frames.pop(0)
                            received.append(len(frame["body"]))
                            return frame
                        return {"type": "http.disconnect"}

                    async def send(frame):
                        sent.append(frame)

                    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                        "http_version": "1.1", "method": "POST", "scheme": "http", "path": path,
                        "raw_path": path.encode(), "query_string": b"", "root_path": "", "headers": headers,
                        "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8000)}
                    await http.app(scope, receive, send)
                    response = next(item for item in sent if item["type"] == "http.response.start")
                    payload = json.loads(b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body"))
                    assert response["status"] == (413 if size > 1048576 else 422)
                    assert payload["detail"]["code"] == ("request_size_exceeded" if size > 1048576 else "invalid_request")
                    assert (b"cache-control", b"no-store") in response["headers"]
                    assert received == [600000, size - 600000]
                    assert len(frames) == (1 if size > 1048576 else 0)
            # 边界合法JSON经真实UUID/字段验证，拒绝的普通请求没有启动模型。
            payload = {"session_id": uid(), "operation_id": uid(), "attachments": [],
                       "message" if path.endswith("steering") else "request": "text"}
            if path == "/api/agent/edit":
                payload["target_entry_id"] = uid()
            encoded = json.dumps(payload).encode()
            response = await connection.post(path, content=encoded + b" " * (1048576 - len(encoded)), headers={"Content-Type": "application/json"})
            assert_error(response, 404, "run_not_found" if path.endswith("steering") else "session_not_found")
        assert await session_fingerprint(service) == before
        assert sorted(str(path) for path in root.glob("tmp/sessions/*/attachments/*")) == before_files
        assert http.runs == {} and not http.active.locked()
        try:
            (root / "non-attachment-missing").read_bytes()
        except OSError as error:
            with pytest.raises(OSError) as unchanged:
                await http.attachment_native_error(None, error)
            assert unchanged.value is error
        try:
            int("real invalid integer")
        except ValueError as error:
            with pytest.raises(ValueError) as unchanged:
                await http.attachment_metadata_error(None, error)
            assert unchanged.value is error
    (root / "body-evidence.json").write_text(json.dumps({"asgi_stream": "passed", "actual_limit": 1048576,
        "length_headers": ["missing", "underdeclared", "actual"], "early_rejection": "third frame unread",
        "listener_chunked": "passed", "model_execution": "not_run"}), encoding="utf-8")


def main() -> None:
    root = ROOT / "attachment-http" / uuid4().hex
    root.mkdir(parents=True)
    original_path = database_module.default_database_path
    database_module.default_database_path = lambda: root / "http.db"
    original_lifespan = http.app.router.lifespan_context

    @asynccontextmanager
    async def isolated(application):
        async with http.lifespan(application):
            service = SessionService(application.state.session_service._repository,
                                     application.state.business, project_root=root)
            application.state.session_service = service
            application.state.steering = SteeringCoordinator(service)
            yield

    http.app.router.lifespan_context = isolated
    try:
        with Server(http.app) as server, client(server.base_url, timeout=15) as connection:
            listener_checks(root, connection)
            invoke(asgi_body_checks(root))
    finally:
        http.app.router.lifespan_context = original_lifespan
        database_module.default_database_path = original_path
    print("PASS: real HTTP attachments/history/operation/Steering/security; listener chunked; real ASGI actual-byte 1 MiB protection; isolated SQLite")
    print(root)


if __name__ == "__main__":
    main()
