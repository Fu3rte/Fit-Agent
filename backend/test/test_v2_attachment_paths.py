import base64
import os
from pathlib import Path
from uuid import uuid4

import pytest

from app import model_config
from app.agent import cache_observation, telemetry
from app.ai.messages import SystemMessage
from app.application.session import service as session_service
from app.domain.session.models import SendCommand, SendRequest
from app.infrastructure.persistence.sqlite import database as database_module
from app.interfaces import http as http_interface
from test.regression_support import Server, client, run_tool
from test.v2_attempts import claim_slot, read_record, slot_attachment_root
from test.v2_eval_support import (
    BACKEND,
    _call,
    experiment_env,
    load_cases,
    slot_file_roots,
)
from test.v2_test_paths import OUTPUT_ROOT

ROOT = OUTPUT_ROOT / "paths"
SYSTEM = SystemMessage(role="system", content="附件协议检查", tools_added=[], timestamp=100)


@pytest.fixture
def tmp_path():
    root = OUTPUT_ROOT / "pytest-r2/p" / uuid4().hex[:12] / "case"
    root.mkdir(parents=True)
    return root


def env_for(root, case, slot):
    return experiment_env("local-path-check", case, 0, slot, 0, root, {}, "2026-01-01")


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length路径回归")
@pytest.mark.parametrize("additional", [False, True], ids=["slot", "uuid-attempt"])
def test_windows_long_attachment_and_tool_paths(monkeypatch, additional):
    base = ROOT / "lp" / uuid4().hex[:8]
    base = base / ("d" * max(1, 175 - len(str(base)) - 1))
    root = base / "reproductions" / uuid4().hex if additional else base
    root.mkdir(parents=True)
    cases = {case.case_id: case for case in load_cases(BACKEND / "test/v2_cases.json")}
    environments = [env_for(root, cases[name], slot) for slot, name in enumerate(("L05", "L06"))]
    session_id = str(uuid4())
    previous_attachment = None
    original_factory = http_interface.create_file_tools
    original_root = session_service.PROJECT_ROOT
    unchanged = [env.model_dump() for env in environments]
    for env in environments:
        directory = slot_attachment_root(env)
        assert str(directory).startswith("\\\\?\\")
        assert slot_attachment_root(env.model_copy(update={"attachment_root": str(directory)})) == directory
        monkeypatch.setattr(database_module, "default_database_path", lambda env=env: Path(env.database_path))
        Path(env.database_path).parent.mkdir(parents=True, exist_ok=True)
        Path(env.trace_path).parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(telemetry, "trace_database_path", lambda env=env: Path(env.trace_path))
        monkeypatch.setattr(model_config, "DATA_ROOT", root / "config")
        monkeypatch.setattr(cache_observation, "observation_directory", lambda: root / "observations")
        with slot_file_roots(env) as configured:
            assert configured == directory == session_service.PROJECT_ROOT
            directory.mkdir(parents=True)
            with Server(http_interface.app) as server, client(server.base_url) as connection:
                service = http_interface.app.state.session_service
                _call(service.create_session(session_id, "附件协议检查"))
                inputs = [{
                    "kind": "upload", "attachment_id": str(uuid4()),
                    "file_name": item.file_name,
                    "data_base64": base64.b64encode(item.text.encode("utf-8")).decode("ascii"),
                } for item in cases[env.case_id].steps[0].attachments]
                assert inputs
                outcome = _call(service.accept_send(SendCommand(
                    session_id=session_id, operation_id=str(uuid4()),
                    request=SendRequest(text="读取附件", attachments=inputs),
                ), system_message=SYSTEM))
                tools = http_interface.create_file_tools(session_id)
                for item in inputs:
                    metadata, text = _call(service.get_attachment_content(session_id, item["attachment_id"]))
                    relative = metadata.storage_ref.removeprefix("tmp/")
                    ordinary = Path(env.attachment_root) / metadata.storage_ref
                    assert len(str(ordinary)) >= 260
                    stored = directory / metadata.storage_ref
                    assert stored.read_text(encoding="utf-8") == text
                    response = connection.get(f"/api/sessions/{session_id}/attachments/{item['attachment_id']}")
                    assert response.status_code == 200 and response.json()["text"] == text
                    assert text.splitlines()[0] in run_tool(tools["read"], {"path": relative}).content[0].text
                workspace = f"sessions/{session_id}/workspace/note.txt"
                run_tool(tools["write"], {"path": workspace, "content": env.case_id})
                assert (directory / "tmp" / workspace).read_text(encoding="utf-8") == env.case_id
                assert env.case_id in run_tool(tools["read"], {"path": workspace}).content[0].text
                if previous_attachment is not None:
                    assert run_tool(tools["read"], {"path": previous_attachment}).is_error
                other = f"sessions/{uuid4()}/attachments/{uuid4()}.md"
                denied = run_tool(tools["read"], {"path": other})
                assert denied.is_error
                assert "只允许访问当前会话" in denied.content[0].text
                previous_attachment = metadata.storage_ref.removeprefix("tmp/")
                files = service._files(())
                disposable = {**inputs[0], "attachment_id": str(uuid4())}
                prepared = files.prepare(session_id, [disposable], existing={})[0]
                created = []
                metadata = files.write(session_id, prepared, created_at=100, on_created=created.append)
                assert files.read(session_id, metadata)
                files.remove_created(created[0])
                assert not (directory / metadata.storage_ref).exists()
                _call(service.finish_run(session_id, outcome.run.id, "completed"))
            with pytest.raises(FileExistsError, match="槽位已有证据"):
                claim_slot(env)
            claim = root / "claims" / f"{env.case_id}-r0.json"
            assert read_record(claim)["status"] == "rejected_existing_artifacts"
        assert http_interface.create_file_tools is original_factory
        assert session_service.PROJECT_ROOT == original_root
    assert [env.model_dump() for env in environments] == unchanged
    for env in environments:
        workspace = slot_attachment_root(env) / "tmp" / f"sessions/{session_id}/workspace/note.txt"
        assert workspace.read_text(encoding="utf-8") == env.case_id


def test_slot_file_roots_restore_on_exception(tmp_path):
    case = load_cases(BACKEND / "test/v2_cases.json")[0]
    env = env_for(tmp_path, case, 0)
    original_factory = http_interface.create_file_tools
    original_root = session_service.PROJECT_ROOT
    with pytest.raises(RuntimeError, match="结束槽位检查"):
        with slot_file_roots(env):
            with slot_file_roots(env):
                assert session_service.PROJECT_ROOT == slot_attachment_root(env)
            raise RuntimeError("结束槽位检查")
    assert http_interface.create_file_tools is original_factory
    assert session_service.PROJECT_ROOT == original_root


def test_slot_attachment_root_rejects_outside_execution_root(tmp_path):
    case = load_cases(BACKEND / "test/v2_cases.json")[0]
    env = env_for(tmp_path / "experiment", case, 0)
    env = env.model_copy(update={"attachment_root": str(tmp_path / "outside")})
    with pytest.raises(ValueError, match="当前执行根"):
        slot_attachment_root(env)
