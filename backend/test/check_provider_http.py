import asyncio
import json
from uuid import UUID, uuid4

from app import model_config
from app.ai.messages import SystemMessage
from app.application.session.service import SessionService
from app.domain.session.models import SendCommand, SendRequest
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from app.interfaces import http as interface
from app.interfaces.http import app
from test.check_http import create_session
from test.regression_support import (
    Server,
    client,
    patch_default_database,
    temporary_root,
)

ROOT = temporary_root("provider-http") / uuid4().hex
ROOT.mkdir(parents=True)

EMPTY = {"api": None, "base_url": None, "model": None, "provider": None, "api_key": None}
KEY = "provider-http-key-" + uuid4().hex
SYSTEM_MESSAGE = SystemMessage(
    role="system", content="系统提示词，仅后端可见。", tools_added=[], timestamp=100
)


def valid_body(**overrides) -> dict:
    payload = {
        "api": "openai-completions",
        "base_url": "http://localhost:11434/v1",
        "model": "vendor/model-7b",
        "api_key": KEY,
    }
    payload.update(overrides)
    return payload


def assert_no_store(response) -> None:
    assert response.headers["cache-control"] == "no-store", response.headers


def assert_field_error(response) -> None:
    assert response.status_code == 422, response.text
    assert_no_store(response)
    assert response.json()["detail"]["code"] == "invalid_request"


def seed_guard_session(path, session_id: str, key_text: str) -> None:
    # 预置一条含凭据的用户消息，绕过受理期检查以复现在途 Key 已落库的场景。
    async def seed() -> None:
        database = await open_database(path)
        service = SessionService(SqliteSessionRepository(database))
        await service.create_session(session_id, "在途凭据")
        await service.accept_send(
            SendCommand(
                operation_id=str(uuid4()),
                session_id=session_id,
                request=SendRequest(text=f"请记录 {key_text}"),
            ),
            system_message=SYSTEM_MESSAGE,
        )
        await database.close()

    asyncio.run(seed())


def check() -> None:
    original_root = model_config.DATA_ROOT
    model_config.DATA_ROOT = ROOT / "config"
    database = patch_default_database("provider-http")
    guard_session = str(uuid4())
    guard_key = "inflight-key-" + uuid4().hex
    seed_guard_session(database, guard_session, guard_key)
    try:
        with Server(app) as server, client(server.base_url) as http:
            # 1. 未配置读取：五个字段均为 null。
            current = http.get("/api/provider")
            assert_no_store(current)
            assert current.json() == EMPTY

            # 10. 未配置时配置入口与业务数据查询可用。
            assert http.get("/api/profile").json() == {"version": None, "content": None}
            assert http.get("/api/plans/current").json() == {"id": None, "content": None}
            assert http.get("/api/plans").json() == []
            workouts = http.get("/api/workouts")
            assert workouts.status_code == 200
            assert_no_store(workouts)
            session = str(uuid4())
            create_session(http, session)
            history = http.get(f"/api/sessions/{session}/history")
            assert history.status_code == 200
            assert_no_store(history)

            # 11. 无配置时新运行被拒绝：不创建运行、不提交用户消息。
            operation = str(uuid4())
            blocked = http.post(
                "/api/agent/run",
                json={"session_id": session, "operation_id": operation, "request": "你好"},
            )
            assert blocked.status_code == 409, blocked.text
            assert_no_store(blocked)
            assert blocked.json()["detail"]["code"] == "model_not_configured"
            assert set(blocked.json()["detail"]) == {"code", "message"}
            accepted = http.get(f"/api/sessions/{session}/operations/{operation}")
            assert accepted.status_code == 200 and accepted.json()["accepted"] is False
            entries = http.get(f"/api/sessions/{session}/history").json()["entries"]
            assert not any(item["message"]["role"] == "user" for item in entries)

            # 4. 参数错误：缺字段、额外字段、非法类型、非法协议/URL、空白模型与空 Key。
            invalid_bodies = [
                {key: value for key, value in valid_body().items() if key != "api"},
                {**valid_body(), "extra": True},
                valid_body(api=123),
                valid_body(base_url=123),
                valid_body(model=123),
                valid_body(api_key=123),
                valid_body(provider=123),
                valid_body(provider=None),
                valid_body(api="openai-compatible"),
                valid_body(base_url="ftp://localhost/v1"),
                valid_body(base_url="not-a-url"),
                valid_body(model="   "),
                valid_body(api_key="   "),
            ]
            for body in invalid_bodies:
                assert_field_error(http.put("/api/provider", json=body))
                assert_field_error(http.post("/api/provider/test", json=body))
            # 校验失败不修改已保存配置。
            assert http.get("/api/provider").json() == EMPTY
            assert KEY not in json.dumps(http.get("/api/provider").json())

            # 9. 既有本地访问保护。
            assert http.get("/api/provider", headers={"Host": "invalid.example"}).status_code == 403
            assert http.get("/api/provider", headers={"Origin": "http://invalid.example"}).status_code == 403

            # 2/3. 完整保存与 provider 生效规则。
            saved = http.put("/api/provider", json=valid_body(provider="  stepfun  "))
            assert_no_store(saved)
            assert saved.status_code == 200, saved.text
            assert saved.json() == {
                "api": "openai-completions",
                "base_url": "http://localhost:11434/v1",
                "model": "vendor/model-7b",
                "provider": "stepfun",
                "api_key": KEY,
            }
            assert http.get("/api/provider").json() == saved.json()
            defaulted = http.put("/api/provider", json=valid_body())
            assert defaulted.json()["provider"] == "openai-completions"
            blank = http.put("/api/provider", json=valid_body(provider="   "))
            assert blank.json()["provider"] == "openai-completions"
            # 再次读取稳定。
            assert http.get("/api/provider").json() == blank.json()

            # 通过真实会话服务受理运行，单独验证配置清除后的 Steering 边界。
            notifications = []

            async def accept_running():
                outcome = await app.state.session_service.accept_send(
                    SendCommand(
                        operation_id=str(uuid4()),
                        session_id=session,
                        request=SendRequest(text="运行中的请求"),
                    ),
                    system_message=SYSTEM_MESSAGE,
                )
                app.state.steering.open(session, outcome.run.id, notifications.append)
                return outcome.run.id

            running_id = asyncio.run_coroutine_threadsafe(
                accept_running(), app.state.loop
            ).result(timeout=10)

            cleared = http.delete("/api/provider")
            assert_no_store(cleared)
            assert cleared.json() == EMPTY
            assert http.delete("/api/provider").json() == cleared.json()
            assert http.get("/api/provider").json() == cleared.json()

            steering = http.post(
                f"/api/agent/runs/{running_id}/steering",
                json={
                    "session_id": session,
                    "operation_id": str(uuid4()),
                    "message": "运行中追加",
                },
            )
            assert steering.status_code == 200, steering.text
            assert steering.json()["status"] == "accepted"
            queued = http.get(
                f"/api/sessions/{session}/runs/{running_id}/steering"
            )
            assert queued.status_code == 200, queued.text
            assert queued.json()["steering"][0]["steering_id"] == steering.json()["steering_id"]
            assert queued.json()["steering"][0]["text"] == "运行中追加"
            asyncio.run_coroutine_threadsafe(
                app.state.steering.finish(session, running_id, "cancelled"),
                app.state.loop,
            ).result(timeout=10)
            assert notifications[-1]["status"] == "discarded"

            assert http.get("/api/profile").status_code == 200
            cleared_operation = str(uuid4())
            cleared_blocked = http.post(
                "/api/agent/run",
                json={"session_id": session, "operation_id": cleared_operation, "request": "继续"},
            )
            assert cleared_blocked.status_code == 409
            assert cleared_blocked.json()["detail"]["code"] == "model_not_configured"

            # 2. 清除配置后，在途运行的 Key 仍参与检查：历史不得返回该 Key。
            guard_state = interface.RunState(UUID(guard_session))
            guard_state.secrets = (guard_key,)
            marker = uuid4()
            with interface.runs_lock:
                interface.runs[marker] = guard_state
            try:
                assert interface.active_secrets(guard_session) == (guard_key,)
                assert interface.active_secrets(str(uuid4())) == ()
                leaked = http.get(f"/api/sessions/{guard_session}/history")
                assert leaked.status_code == 422, leaked.text
                assert leaked.json()["detail"]["code"] == "credential_detected"
                assert guard_key not in leaked.text
            finally:
                with interface.runs_lock:
                    interface.runs.pop(marker, None)
            assert interface.active_secrets(guard_session) == ()

            # 13. 保存期间状态读取始终是某一次保存的完整组合。
            http.put("/api/provider", json=valid_body(base_url="http://one.example/v1", api_key="key-a"))
            snapshot = model_config.load_model_config()
            http.put("/api/provider", json=valid_body(base_url="http://two.example/v1", api_key="key-b"))
            assert (snapshot.base_url, snapshot.api_key) == ("http://one.example/v1", "key-a")
            assert http.get("/api/provider").json() == {
                "api": "openai-completions",
                "base_url": "http://two.example/v1",
                "model": "vendor/model-7b",
                "provider": "openai-completions",
                "api_key": "key-b",
            }
            evidence = {
                "saved": saved.json(),
                "cleared": cleared.json(),
                "guard_key_redacted": True,
            }
    finally:
        model_config.DATA_ROOT = original_root
    (ROOT / "provider-http.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("模型配置 HTTP 读取/保存/清除/校验/未配置拦截/no-store/本地保护/在途凭据保护检查通过")


if __name__ == "__main__":
    check()
