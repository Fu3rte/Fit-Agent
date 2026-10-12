import asyncio
import json
import sqlite3
from uuid import uuid4

from app.interfaces.http import app
from test.check_http import events, validate_events, wait_idle
from test.regression_support import (
    Server,
    client,
    install_test_model_config,
    patch_default_database,
    temporary_root,
)

ROOT = temporary_root("workout-agent-http") / uuid4().hex
ROOT.mkdir()
WIRE = []


def successful(outputs, name):
    return [json.loads(item["content"]) for item in outputs
            if item["name"] == name and not item["is_error"]]


def turn(http, session, request=None, *, path="/api/agent/run", target=None):
    body = {"session_id": session, "operation_id": str(uuid4())}
    if request is not None:
        body["request"] = request
    if target is not None:
        body["target_entry_id"] = target
    with http.stream("POST", path, json=body) as response:
        assert response.status_code == 200
        request_id = response.headers["X-Request-Entry-ID"]
        result = list(events(response))
    wait_idle()
    validate_events(result)
    assert result[-1]["event"] == "done", result[-1]
    names = {e["data"]["tool_call_id"]: e["data"] for e in result if e["event"] == "tool_start"}
    endings = {e["data"]["tool_call_id"]: e["data"] for e in result if e["event"] == "tool_execution_end"}
    outputs = []
    for event in result:
        if event["event"] == "tool_execution_end":
            assert "entry_id" not in event["data"] and "parent_id" not in event["data"]
        if event["event"] == "tool_result":
            data = event["data"]
            assert data["entry_id"] and data["parent_id"]
            ending = endings[data["tool_call_id"]]
            assert ending["content"] == data["content"] and ending["is_error"] == data["is_error"]
            outputs.append({**data, "name": names[data["tool_call_id"]]["name"],
                            "arguments": names[data["tool_call_id"]]["arguments"]})
    WIRE.append({"session": session, "request_entry_id": request_id, "path": path, "events": result})
    (ROOT / "wire.json").write_text(json.dumps(WIRE, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS turn:", path, [o["name"] for o in outputs], flush=True)
    return request_id, outputs


def session(http):
    identity = str(uuid4())
    assert http.post("/api/sessions", json={"session_id": identity, "title": "实际训练后端模型验证"}).status_code == 201
    return identity


def listing(http):
    response = http.get("/api/workouts")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    return response.json()


def history(http, identity):
    response = http.get(f"/api/sessions/{identity}/history")
    assert response.status_code == 200
    return response.json()


def proposal(http, identity):
    prepared = [entry for entry in history(http, identity)["entries"]
                if entry["message"]["role"] == "toolResult"
                and entry["message"]["tool_name"] == "prepare_workout"
                and not entry["message"]["is_error"]]
    assert prepared
    last = prepared[-1]
    return {**json.loads(last["message"]["content"]), "display_entry_id": last["entry_id"]}


def pending(http, identity, business_kind="workout"):
    items = [item for item in history(http, identity)["pending_proposals"]
             if item["business_kind"] == business_kind]
    assert items, f"当前路径缺少待确认的 {business_kind} 快照"
    return items[-1]


def confirm(http, identity, target, business_kind="workout"):
    # 合法保存只走独立确认 HTTP：后端从已绑定快照取得业务事实，不调用模型。
    return http.post(f"/api/sessions/{identity}/confirmations", json={
        "operation_id": str(uuid4()), "business_kind": business_kind,
        "proposal_id": target["proposal_id"], "display_entry_id": target["display_entry_id"]})


def main():
    install_test_model_config()
    database = patch_default_database("workout-agent-http")
    with Server(app) as server, client(server.base_url) as http:
        first = session(http)
        _, outputs = turn(http, first, "昨天实际完成俯卧撑两组，每组10次，重量和时长未知，无感受备注。目录身份未核实用null。请用 calculate_date 换算具体日期，查询当天记录，整理完整训练快照给我核对，等待后续确认。")
        assert successful(outputs, "prepare_workout") and successful(outputs, "list_workouts")
        computed = [item for item in outputs if item["name"] == "calculate_date"]
        assert computed and not computed[0]["is_error"], outputs
        assert computed[0]["arguments"] == {"days_offset": -1}, computed[0]
        relative = json.loads(computed[0]["content"])["date"]
        assert all(item["arguments"].get("date_from") == relative
                   and item["arguments"].get("date_to") == relative
                   for item in outputs if item["name"] == "list_workouts"), outputs
        a = proposal(http, first)
        assert a["performed_on"] == relative
        assert a["base_workout_id"] is None and a["base_workout_version"] is None
        assert len(a["payload"]["exercises"][0]["sets"]) == 2
        assert listing(http)["total"] == 0
        with sqlite3.connect(database) as db:
            binding = db.execute("SELECT display_entry_id FROM workout_snapshots WHERE proposal_id=?", (a["proposal_id"],)).fetchone()
            assert binding[0] == a["display_entry_id"]
        # 按钮确认：独立确认 HTTP 保存训练记录，不调用模型、不创建 Agent 运行。
        saved_operation = confirm(http, first, pending(http, first))
        assert saved_operation.status_code == 200, saved_operation.text
        saved_body = saved_operation.json()
        assert saved_body["status"] == "saved" and saved_body["proposal_id"] == a["proposal_id"]
        initial = saved_body["result"]
        confirmation = saved_body["confirmation_entry_id"]
        first_operation = saved_body["operation_id"]
        assert listing(http)["items"][0]["version"] == 1
        # 确认结果通过专用 GET 恢复；确认节点不接受模型重新生成。
        fetched = http.get(f"/api/sessions/{first}/confirmations/{first_operation}")
        assert fetched.status_code == 200 and fetched.json()["result"] == initial
        regenerated = http.post("/api/agent/regenerate", json={
            "session_id": first, "operation_id": str(uuid4()), "target_entry_id": confirmation})
        assert regenerated.status_code == 409, regenerated.text
        assert regenerated.json()["detail"]["code"] == "invalid_target_entry"
        assert listing(http)["items"][0]["version"] == 1
        other, stale = session(http), session(http)
        turn(http, other, f"{a['performed_on']}实际训练的俯卧撑改为两组，每组12次，其余原内容保留。先查询当天完整记录，准备完整更新快照等我确认。")
        b = proposal(http, other)
        assert b["base_workout_id"] == initial["id"] and b["base_workout_version"] == 1
        turn(http, stale, f"{a['performed_on']}实际训练感受改为良好，其余原内容保留。查询当天记录并准备完整更新等我确认。")
        old = proposal(http, stale)
        assert old["base_workout_version"] == 1
        updated_target = pending(http, other)
        assert updated_target["proposal_id"] == b["proposal_id"]
        updated_operation = confirm(http, other, updated_target)
        assert updated_operation.status_code == 200, updated_operation.text
        updated_body = updated_operation.json()
        assert updated_body["status"] == "saved"
        updated = [updated_body["result"]]
        assert len(updated) == 1 and updated[0]["id"] == initial["id"] and updated[0]["version"] == 2
        # 过期快照以旧依据提交：按钮确认返回版本冲突，零写入。
        stale_target = pending(http, stale)
        assert stale_target["proposal_id"] == old["proposal_id"]
        conflict = confirm(http, stale, stale_target)
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["detail"]["code"] == "workout_version_conflict"
        assert listing(http)["items"][0]["version"] == 2
        with sqlite3.connect(database) as db:
            assert db.execute("SELECT status FROM workout_snapshots WHERE proposal_id=?", (old["proposal_id"],)).fetchone()[0] == "conflicted"
        _, outputs = turn(http, stale, "明确修改意图：只把notes改为良好，其余采用当前最新已保存记录的动作和组次。请重新读取最新记录，整理并展示新快照等待我再次确认。")
        assert successful(outputs, "prepare_workout")
        queried_versions = [item for page in successful(outputs, "list_workouts")
                            for item in page["items"]]
        assert any(item["id"] == initial["id"] and item["version"] == 2 for item in queried_versions)
        assert not successful(outputs, "update_workout")
        assert not successful(outputs, "save_workout")
        branch = history(http, stale)
        active = branch["session"]["active_leaf_id"]
        entries = {entry["entry_id"]: entry for entry in branch["entries"]}
        active_ids = set()
        while active is not None:
            active_ids.add(active)
            active = entries[active]["parent_id"]
        reads = [o for o in outputs if o["name"] == "list_workouts" and not o["is_error"]]
        assert any(o["entry_id"] in active_ids for o in reads)
        refreshed = proposal(http, stale)
        assert refreshed["proposal_id"] != old["proposal_id"] and refreshed["base_workout_version"] == 2
        assert refreshed["base_workout_id"] == initial["id"]
        assert refreshed["payload"]["exercises"] == updated[0]["content"]["exercises"]
        assert refreshed["payload"]["notes"] == "良好"
        assert listing(http)["items"][0]["version"] == 2
        final_target = pending(http, stale)
        assert final_target["proposal_id"] == refreshed["proposal_id"]
        final_operation = confirm(http, stale, final_target)
        assert final_operation.status_code == 200, final_operation.text
        assert final_operation.json()["status"] == "saved"
        assert listing(http)["items"][0]["version"] == 3
        # 真实画像流程与训练确认绑定同处HTTP投影。
        turn(http, first, "我的目标是增肌，每周三次，环境健身房，其余画像未知，禁用动作为空。请查询当前画像并准备完整画像等我确认。")
        profile_operation = confirm(http, first, pending(http, first, "profile"), "profile")
        assert profile_operation.status_code == 200, profile_operation.text
        assert profile_operation.json()["status"] == "saved"
        assert http.get("/api/profile").json()["version"] == 1
        fetched = http.get(f"/api/sessions/{first}/confirmations/{first_operation}")
        assert fetched.status_code == 200 and fetched.json()["result"] == initial
        regenerated = http.post("/api/agent/regenerate", json={
            "session_id": first, "operation_id": str(uuid4()), "target_entry_id": confirmation})
        assert regenerated.status_code == 409
        assert regenerated.json()["detail"]["code"] == "invalid_target_entry"
        assert listing(http)["items"][0]["version"] == 3
        _, outputs = turn(http, first,
            f"仅核对原操作：调用get_workout_save_status查询proposal_id={initial['proposal_id']}，"
            f"再用get_workout查询workout_id={initial['id']}的最新记录；不要准备或保存。")
        queried = successful(outputs, "get_workout_save_status")
        current = successful(outputs, "get_workout")
        assert queried and queried[0]["status"] == "saved" and queried[0]["result"] == initial
        assert current and current[0]["version"] == 3
        assert not successful(outputs, "prepare_workout")
        edited, _ = turn(http, first, f"请查询{a['performed_on']}最新训练记录，只把notes改为已恢复，所有动作和组次保持当前最新已保存内容（两组每组12次），整理完整快照等待我后续核对确认。", path="/api/agent/edit", target=confirmation)
        assert edited != confirmation and listing(http)["items"][0]["version"] == 3
        replacement = proposal(http, first)
        assert replacement["base_workout_version"] == 3
        latest = listing(http)
        before = history(http, stale)
        assert http.delete(f"/api/sessions/{first}").status_code == 200
        assert listing(http) == latest
        with sqlite3.connect(database) as db:
            assert db.execute("SELECT COUNT(*) FROM workout_snapshots WHERE session_id=?", (first,)).fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM workout_save_records WHERE proposal_id=?", (initial["proposal_id"],)).fetchone()[0] == 1
            messages = db.execute("SELECT messages FROM session_entries").fetchall()
            assert all("business_context" not in (message.get("sections") or {}) for (raw,) in messages for message in json.loads(raw))
        record = {"database": str(database), "initial": initial, "latest": latest,
                  "relative_date": a["performed_on"], "turn_count": len(WIRE), "replacement": replacement}
    with Server(app) as server, client(server.base_url) as http:
        assert listing(http) == latest and history(http, stale) == before
        async def audit():
            db = app.state.session_service._repository._database
            async with db.transaction_scope():
                return [row[0] for row in await (await db.connection.execute("PRAGMA integrity_check")).fetchall()]
        assert asyncio.run_coroutine_threadsafe(audit(), app.state.loop).result() == ["ok"]
    (ROOT / "evidence.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS: real model Agent/HTTP/SSE; relative-date calculate_date; snapshot display; button confirmation via dedicated HTTP; cross-session update/conflict; fixed result via GET; profile binding; edit/delete/restart:", ROOT)


if __name__ == "__main__":
    main()
