import base64
import json
from uuid import uuid4

from app.ai.messages import ToolResultMessage
from app.interfaces.http import app
from test.check_http import (
    create_session,
    events,
    final_text,
    stored_messages,
    validate_events,
    wait_idle,
)
from test.regression_support import (
    Server,
    client,
    install_test_model_config,
    patch_default_database,
    temporary_root,
)

EVIDENCE = temporary_root("plan-attachment-acceptance")
PLAN = """# 我的训练计划

周一 推：
- 卧推 4 组 8 次
- 上斜哑铃卧推 3 组 10 次

周二 拉：
- 引体向上 4 组 6 次
- 坐姿划船 3 组 12 次

周三 休息
"""


def run_turn(http, session: str, request: str, attachments: list | None = None):
    payload = {"session_id": session, "operation_id": str(uuid4()), "request": request}
    if attachments is not None:
        payload["attachments"] = attachments
    with http.stream("POST", "/api/agent/run", json=payload) as response:
        assert response.status_code == 200, response.text
        result = list(events(response))
    wait_idle()
    return result


def calls_named(starts: dict, names: set[str]) -> list[str]:
    return [call for call, data in starts.items() if data["name"] in names]


def check() -> None:
    install_test_model_config()
    patch_default_database("plan-attachment-acceptance")
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    attachment_id = str(uuid4())
    body = PLAN.encode("utf-8")
    upload = {
        "kind": "upload",
        "attachment_id": attachment_id,
        "file_name": "plan.md",
        "data_base64": base64.b64encode(body).decode("ascii"),
    }
    evidence: dict = {"attachment_id": attachment_id}
    with Server(app) as server:
        with client(server.base_url) as http:
            session = str(uuid4())
            create_session(http, session, "计划录入")
            path = f"sessions/{session}/attachments/{attachment_id}.md"

            first = run_turn(
                http, session, "这是我现有的训练计划，请整理后录入进来。", [upload]
            )
            starts, results = validate_events(first)
            assert first[-1]["event"] == "done", first[-1]
            read_calls = calls_named(starts, {"read"})
            assert read_calls, list(starts.values())
            assert any(starts[call]["arguments"].get("path") == path for call in read_calls)
            assert any("卧推" in results[call] for call in read_calls), results
            prepare_calls = calls_named(
                starts, {"prepare_plan_import", "prepare_plan_adjustment"}
            )
            if not prepare_calls:
                (EVIDENCE / "turn1.json").write_text(
                    json.dumps(first, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            assert prepare_calls, (list(starts.values()), final_text(first))
            proposal = json.loads(results[prepare_calls[0]])
            assert proposal["proposal_id"]
            assert proposal["preparation_kind"] in {"import", "adjustment"}
            prepared = [
                message
                for message in stored_messages(session)
                if isinstance(message, ToolResultMessage)
                and message.tool_name in {"prepare_plan_import", "prepare_plan_adjustment"}
            ]
            assert prepared and not prepared[-1].is_error

            fetched = http.get(f"/api/sessions/{session}/attachments/{attachment_id}")
            assert fetched.status_code == 200, fetched.text
            assert fetched.json()["text"].encode("utf-8") == body
            evidence["turn1"] = {
                "prepare": starts[prepare_calls[0]]["name"],
                "read_path": starts[read_calls[0]]["arguments"].get("path"),
                "proposal_id": proposal["proposal_id"],
            }

            # 点击该快照的确认按钮：独立确认 HTTP 保存，后端从已绑定快照取得业务事实。
            display_entry = next(
                event["data"]["entry_id"]
                for event in first
                if event["event"] == "tool_result"
                and event["data"]["tool_call_id"] == prepare_calls[0]
            )
            operation_id = str(uuid4())
            confirmed = http.post(
                f"/api/sessions/{session}/confirmations",
                json={
                    "operation_id": operation_id,
                    "business_kind": "plan",
                    "proposal_id": proposal["proposal_id"],
                    "display_entry_id": display_entry,
                },
            )
            assert confirmed.status_code == 200, confirmed.text
            operation = confirmed.json()
            assert operation["status"] == "saved", operation
            saved = operation["result"]
            assert saved["proposal_id"] == proposal["proposal_id"]
            assert saved["id"] and saved["saved_at"] and saved["created_at"]
            evidence["turn2"] = saved

            # 确认结果通过专用 GET 恢复；确认节点不接受模型重新生成。
            fetched = http.get(f"/api/sessions/{session}/confirmations/{operation_id}")
            assert fetched.status_code == 200 and fetched.json()["status"] == "saved"
            regenerated = http.post(
                "/api/agent/regenerate",
                json={
                    "session_id": session,
                    "operation_id": str(uuid4()),
                    "target_entry_id": operation["confirmation_entry_id"],
                },
            )
            assert regenerated.status_code == 409, regenerated.text
            assert regenerated.json()["detail"]["code"] == "invalid_target_entry"

            # 同键同请求重复确认返回原固定结果，不新增计划版本。
            replay = http.post(
                f"/api/sessions/{session}/confirmations",
                json={
                    "operation_id": operation_id,
                    "business_kind": "plan",
                    "proposal_id": proposal["proposal_id"],
                    "display_entry_id": display_entry,
                },
            )
            assert replay.status_code == 200, replay.text
            assert replay.json()["result"] == saved, replay.text
            evidence["turn3"] = replay.json()["result"]

            plans = http.get("/api/plans").json()
            assert len(plans) == 1 and plans[0]["id"] == saved["id"], plans
            evidence["plans"] = [record["id"] for record in plans]

            history = http.get(f"/api/sessions/{session}/history").json()
            assert set(history) == {
                "session", "entries", "runs", "steering", "confirmations", "pending_proposals",
            }, set(history)
            assert [item["operation_id"] for item in history["confirmations"]] == [operation_id]
            assert history["pending_proposals"] == []

            # 模型只准备/展示：普通确认文字不构成保存授权，模型注册表也不提供保存工具。
            second = run_turn(http, session, "确认，保存这个计划。")
            starts2, _ = validate_events(second)
            assert not calls_named(starts2, {"save_plan"}), list(starts2.values())

    (EVIDENCE / "acceptance.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "PASS: 真实模型单文件录入：读取附件真实路径 → 录入准备 → 持久化展示绑定 → "
        "独立确认 HTTP 保存 → 专用 GET 与同键幂等固定结果"
    )
    print(json.dumps(evidence, ensure_ascii=False))


if __name__ == "__main__":
    check()
