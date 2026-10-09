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

            second = run_turn(http, session, "确认，保存这个计划。")
            starts2, results2 = validate_events(second)
            save_calls = calls_named(starts2, {"save_plan"})
            assert save_calls, list(starts2.values())
            saved = json.loads(results2[save_calls[0]])
            assert saved["proposal_id"] == proposal["proposal_id"]
            assert saved["id"] and saved["saved_at"] and saved["created_at"]
            evidence["turn2"] = saved

            third = run_turn(
                http, session, "请再次调用 save_plan 保存刚才那个已确认的计划。"
            )
            starts3, results3 = validate_events(third)
            save_calls3 = calls_named(starts3, {"save_plan"})
            assert save_calls3, list(starts3.values())
            again = json.loads(results3[save_calls3[0]])
            assert again == saved, (again, saved)
            evidence["turn3"] = again

            plans = http.get("/api/plans").json()
            assert any(record["id"] == saved["id"] for record in plans), plans
            evidence["plans"] = [record["id"] for record in plans]

    (EVIDENCE / "acceptance.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "PASS: 真实模型单文件录入：读取附件真实路径 → 录入准备 → 持久化展示绑定 → "
        "后续确认 save_plan → 固定幂等结果"
    )
    print(json.dumps(evidence, ensure_ascii=False))


if __name__ == "__main__":
    check()
