"""S4-07：对话／Run HTTP 面、幂等、busy、重试、取消与查询恢复（stage4.md §6/S4-07 验收）。

覆盖：

1. 冻结传输拼写与路由表（含「没有公开建草稿入口」的非空断言）。
2. 会话创建／查询、请求提交（`{client_request_id, text}` → `{created, run}`）、Run 查询。
3. 幂等提交返回已有 Run、不重复建消息；活跃 Run 时不同请求 409 `conversation_busy`。
4. 显式取消与重复取消的错误形状；终态 Run 的 SSE 只给状态并结束。
5. 手动重试创建带 `retry_of_run_id` 的新 Run，不复活旧 Run。
6. 无 Provider 凭据时 fail-closed（Run 以已冻结的 `model_request_failed` 结束，不发请求）。
7. 服务重启把遗留 Run 标 `interrupted_by_restart`；已保存部分回答按未完成可查询。
8. 错误形状（404/400/409）与 Host／Origin 回环边界。

全程离线：模型经 ``app.state.model_factory`` 整体替换为 ``FunctionModel`` 脚本桩；无真实 Provider
请求。每例只操作 ``tmp_path`` 数据目录，HTTP 经回环 Host 直连应用。
"""

import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from api.app import create_app
from config import DATABASE_FILENAME
from runtime.run_service import RunService
from storage.run_repo import RunRepo
from tests.support import open_database
from tests.test_stage4_sse_events import _StreamingStub

BASE_URL = "http://127.0.0.1"
#: stage4.md §6 冻结的对话／Run／SSE 路由（方法与路径逐字对应）。
FROZEN_ROUTES = {
    ("/api/sessions", "POST"),
    ("/api/sessions/{session_id}", "GET"),
    ("/api/sessions/{session_id}/requests", "POST"),
    ("/api/runs/{run_id}", "GET"),
    ("/api/runs/{run_id}/retry", "POST"),
    ("/api/runs/{run_id}/cancel", "POST"),
    ("/api/runs/{run_id}/events", "GET"),
}


def _route_table(app: Any) -> set[tuple[str, str]]:
    """(路径, 方法) 全集：FastAPI 0.141 把 ``include_router`` 条目保留为 ``_IncludedRouter``，
    需按 ``original_router`` 展开，否则路由守卫会空转（本文件和 S4-01 契约测试共用同一口径）。"""
    table: set[tuple[str, str]] = set()
    stack = list(app.routes)
    while stack:
        route = stack.pop()
        included = getattr(route, "original_router", None)
        if included is not None:
            stack.extend(included.routes)
            continue
        path = getattr(route, "path", None)
        if path is None:
            continue
        for method in getattr(route, "methods", ()) or ():
            table.add((path, method))
    return table


def _factory(stub: _StreamingStub):
    async def factory():
        return stub.model()

    return factory


def _wait_http(condition, *, what: str, timeout: float = 5.0) -> None:
    """有限期限内等一个 HTTP 条件成立（无长等待）。"""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"条件未在期限内满足：{what}")
        time.sleep(0.01)


def _create_session(client: TestClient) -> str:
    response = client.post("/api/sessions", json={})
    assert response.status_code == 200, response.text
    return str(response.json()["session_id"])


def _submit(
    client: TestClient, session_id: str, *, key: str, text: str = "帮我看看训练安排"
):
    return client.post(
        f"/api/sessions/{session_id}/requests",
        json={"client_request_id": key, "text": text},
    )


def _run_status(client: TestClient, run_id: str) -> str:
    response = client.get(f"/api/runs/{run_id}")
    assert response.status_code == 200, response.text
    return str(response.json()["run"]["status"])


def _wait_terminal(client: TestClient, run_id: str) -> dict[str, Any]:
    _wait_http(
        lambda: _run_status(client, run_id) in ("completed", "failed", "cancelled"),
        what=f"Run {run_id} 进入终态",
    )
    return client.get(f"/api/runs/{run_id}").json()["run"]


# ---------- 1. 冻结路由表 ----------


def test_frozen_chat_run_routes_are_exposed_without_draft_creation(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path)
    table = _route_table(app)
    # 冻结拼写逐条存在（方法 + 路径）
    assert FROZEN_ROUTES <= table
    # 会话查询是恢复入口，草稿当前状态仍走既有草稿接口（不在此复制草稿语义）
    assert ("/api/sessions/{session_id}/drafts", "GET") in table
    # 仍然没有公开建草稿入口与直写正式事实的入口（非空断言：路由表能被真实展开）
    assert ("/healthz", "GET") in table
    assert not any(
        path == "/api/drafts" and method in ("POST", "PUT", "PATCH")
        for path, method in table
    )


# ---------- 2. 正常流：提交、查询、幂等 ----------


def test_submit_query_and_idempotent_resubmit(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    stub = _StreamingStub([["训练安排如下"]])
    with TestClient(app, base_url=BASE_URL) as client:
        app.state.model_factory = _factory(stub)
        session_id = _create_session(client)
        first = _submit(client, session_id, key="req-1")
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["created"] is True
        run = body["run"]
        # 冻结的 Run 传输字段（stage4.md §6）
        assert set(run) == {
            "run_id",
            "conversation_id",
            "status",
            "error_code",
            "retry_of_run_id",
            "created_at",
            "updated_at",
        }
        assert run["conversation_id"] == session_id
        assert run["status"] in ("pending", "running")

        # 幂等：相同 client_request_id 返回同一 Run，不重复创建消息或 Run
        again = _submit(client, session_id, key="req-1")
        assert again.status_code == 200, again.text
        assert again.json()["created"] is False
        assert again.json()["run"]["run_id"] == run["run_id"]

        final = _wait_terminal(client, run["run_id"])
        assert (final["status"], final["error_code"]) == ("completed", None)
        assert stub.attempts == 1  # 幂等重发不重跑

        session = client.get(f"/api/sessions/{session_id}").json()
        assert session["session_id"] == session_id
        assert [item["run_id"] for item in session["runs"]] == [run["run_id"]]
        transcript = [
            (item["role"], item["kind"], item["complete"], item["text"])
            for item in session["messages"]
        ]
        assert transcript == [
            ("user", "user_request", True, "帮我看看训练安排"),
            ("assistant", "answer", True, "训练安排如下"),
        ]


def test_busy_request_is_rejected_without_creating_run_or_message(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path)
    gate_holder: dict[str, Any] = {}

    def stub_with_gate() -> _StreamingStub:
        async def on_step(step: int) -> None:
            gate_holder["reached"] = True
            await gate_holder["gate"].wait()

        return _StreamingStub([["回答"]], on_step=on_step)

    with TestClient(app, base_url=BASE_URL) as client:
        import asyncio

        gate = asyncio.Event()
        gate_holder["gate"] = gate
        gated = stub_with_gate()
        app.state.model_factory = _factory(gated)
        session_id = _create_session(client)
        first = _submit(client, session_id, key="req-1").json()["run"]["run_id"]
        _wait_http(
            lambda: _run_status(client, first) == "running", what="Run 进入 running"
        )
        _wait_http(lambda: gate_holder.get("reached") is True, what="模型请求已开始")

        busy = _submit(client, session_id, key="req-2", text="另一个请求")
        assert busy.status_code == 409, busy.text
        error = busy.json()
        assert error["error_code"] == "conversation_busy"
        assert error["http_status"] == 409
        assert "已有活跃 Run" in error["message"]

        session = client.get(f"/api/sessions/{session_id}").json()
        assert [item["run_id"] for item in session["runs"]] == [first]
        assert [item["text"] for item in session["messages"]] == ["帮我看看训练安排"]

        portal = client.portal
        assert portal is not None
        portal.call(gate.set)
        assert _wait_terminal(client, first)["status"] == "completed"
        assert gated.attempts == 1


# ---------- 3. 取消与终态 SSE ----------


def test_cancel_is_explicit_and_repeat_cancel_is_rejected(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    import asyncio

    gate = asyncio.Event()
    reached = asyncio.Event()

    async def on_step(step: int) -> None:
        reached.set()
        await gate.wait()

    stub = _StreamingStub([["回答"]], on_step=on_step)
    with TestClient(app, base_url=BASE_URL) as client:
        app.state.model_factory = _factory(stub)
        session_id = _create_session(client)
        run_id = _submit(client, session_id, key="req-1").json()["run"]["run_id"]
        _wait_http(lambda: reached.is_set(), what="模型请求已开始")

        cancelled = client.post(f"/api/runs/{run_id}/cancel", json={})
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["run"]["status"] == "cancelled"

        # 终态不可再取消：统一错误形状 409 invalid_request
        repeat = client.post(f"/api/runs/{run_id}/cancel", json={})
        assert repeat.status_code == 409, repeat.text
        assert repeat.json()["error_code"] == "invalid_request"

        # 取消不写迟到成功消息；部分回答（若有）只能是未完成
        session = client.get(f"/api/sessions/{session_id}").json()
        assert [item["run_id"] for item in session["runs"]] == [run_id]
        assert all(
            not (item["kind"] == "answer" and item["complete"])
            for item in session["messages"]
        )

        # 终态 Run 的 SSE：只给状态事件并结束，不重放、无 id 字段
        events = client.get(f"/api/runs/{run_id}/events")
        assert events.status_code == 200, events.text
        assert events.headers["content-type"].startswith("text/event-stream")
        body = events.text
        assert "event: status" in body
        assert '"status": "cancelled"' in body
        assert "id:" not in body


# ---------- 4. 手动重试 ----------


def test_manual_retry_creates_new_run_with_retry_pointer(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    with TestClient(app, base_url=BASE_URL) as client:
        # 先在没有凭据的默认装配下提交：Run 以已冻结的模型失败码结束（不发起请求）
        session_id = _create_session(client)
        first = _submit(client, session_id, key="req-1").json()["run"]["run_id"]
        failed = _wait_terminal(client, first)
        assert (failed["status"], failed["error_code"]) == (
            "failed",
            "model_request_failed",
        )

        stub = _StreamingStub([["重试之后的回答"]])
        app.state.model_factory = _factory(stub)
        retried = client.post(
            f"/api/runs/{first}/retry", json={"client_request_id": "req-2"}
        )
        assert retried.status_code == 200, retried.text
        body = retried.json()
        assert body["created"] is True
        assert body["run"]["retry_of_run_id"] == first
        assert body["run"]["run_id"] != first

        final = _wait_terminal(client, body["run"]["run_id"])
        assert (final["status"], final["error_code"]) == ("completed", None)
        # 旧 Run 不被复活、状态与原因不变（时间戳也不改写）
        old = client.get(f"/api/runs/{first}").json()["run"]
        assert (old["status"], old["error_code"], old["retry_of_run_id"]) == (
            "failed",
            "model_request_failed",
            None,
        )
        assert (old["created_at"], old["updated_at"]) == (
            failed["created_at"],
            failed["updated_at"],
        )
        transcript = client.get(f"/api/sessions/{session_id}").json()["messages"]
        assert [
            (item["run_id"], item["kind"], item["complete"], item["text"])
            for item in transcript
        ] == [
            (first, "user_request", True, "帮我看看训练安排"),
            # 手动重试是**新** Run：它有自己的用户请求事实（同一请求文本）
            (body["run"]["run_id"], "user_request", True, "帮我看看训练安排"),
            (body["run"]["run_id"], "answer", True, "重试之后的回答"),
        ]


# ---------- 5. 重启恢复 ----------


def test_restart_marks_leftover_run_failed_and_keeps_partial_incomplete(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    async def seed() -> None:
        async with open_database(data_dir / DATABASE_FILENAME) as db:
            repo = RunRepo(db)
            await repo.create_conversation("c-restart")
            await RunService(repo).submit_request(
                conversation_id="c-restart",
                run_id="r-old",
                client_request_id="req-old",
                text="重启前的问题",
            )
            await repo.start_run("r-old")
            await repo.save_partial_answer("r-old", "重启前没写完的半句话")

    import asyncio

    asyncio.run(seed())

    app = create_app(data_dir)
    with TestClient(app, base_url=BASE_URL) as client:
        session = client.get("/api/sessions/c-restart").json()
        assert session["runs"][0]["status"] == "failed"
        assert session["runs"][0]["error_code"] == "interrupted_by_restart"
        assert session["messages"] == [
            {
                "seq": 1,
                "run_id": "r-old",
                "role": "user",
                "kind": "user_request",
                "text": "重启前的问题",
                "complete": True,
            },
            {
                "seq": 1,
                "run_id": "r-old",
                "role": "assistant",
                "kind": "partial",
                "text": "重启前没写完的半句话",
                "complete": False,
            },
        ]
        # 重启后不续跑：查询与 SSE 都只反映已保存状态
        events = client.get("/api/runs/r-old/events")
        assert '"status": "failed"' in events.text
        assert '"error_code": "interrupted_by_restart"' in events.text


# ---------- 6. 错误形状与回环边界 ----------


def test_error_shapes_and_loopback_boundaries(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    with TestClient(app, base_url=BASE_URL) as client:
        app.state.model_factory = _factory(_StreamingStub([["回答"]]))
        assert client.get("/api/sessions/nope").status_code == 404
        unknown_session = client.get("/api/sessions/nope").json()
        assert unknown_session["error_code"] == "invalid_request"
        assert client.get("/api/runs/nope").status_code == 404

        session_id = _create_session(client)
        # 请求体形状：缺字段／未知字段／空文本都是 400 invalid_request
        for body in (
            {"client_request_id": "k"},
            {"client_request_id": "k", "text": "x", "extra": 1},
            {"client_request_id": "k", "text": "   "},
        ):
            shape = client.post(f"/api/sessions/{session_id}/requests", json=body)
            assert shape.status_code == 400, shape.text
            assert shape.json()["error_code"] == "invalid_request"
        # 提交到不存在的会话：404（不建 Run）
        assert _submit(client, "missing", key="k").status_code == 404
        # 取消不存在的 Run：404
        assert client.post("/api/runs/missing/cancel", json={}).status_code == 404

    # Host／Origin 边界沿用既有中间件：非回环 Host 与越界 Origin 一律 403
    hostile = create_app(tmp_path)
    with TestClient(hostile, base_url="http://evil.example") as client:
        assert client.post("/api/sessions", json={}).status_code == 403
    with TestClient(hostile, base_url=BASE_URL) as client:
        forged = client.post(
            "/api/sessions", json={}, headers={"origin": "http://evil.example"}
        )
        assert forged.status_code == 403


# ---------- 7. 无凭据 fail-closed ----------


def test_missing_provider_credential_fails_run_closed(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    with TestClient(app, base_url=BASE_URL) as client:
        session_id = _create_session(client)
        run_id = _submit(client, session_id, key="req-1").json()["run"]["run_id"]
        final = _wait_terminal(client, run_id)
        # 不新增错误码、不伪造成功：以已冻结的模型失败码结束，且没有完整回答
        assert (final["status"], final["error_code"]) == (
            "failed",
            "model_request_failed",
        )
        session = client.get(f"/api/sessions/{session_id}").json()
        assert [item["kind"] for item in session["messages"]] == ["user_request"]

    data_dir = tmp_path / "data2"
    assert (data_dir / DATABASE_FILENAME).exists() is False
