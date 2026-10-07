import json
from uuid import uuid4

from app.interfaces.http import app
from test.check_http import create_session, events, wait_idle
from test.regression_support import (
    Server,
    client,
    patch_default_database,
    temporary_root,
)

EVIDENCE = temporary_root("business-agent")

TURN_ONE = (
    "我的目标是增肌，训练环境是普通健身房，每周可以练四次。"
    "请先读取我当前的画像。"
)
TURN_TWO = (
    "请把上述个人情况整理成一份完整画像并展示给我确认。"
    "在我明确确认之前不要调用保存画像工具。"
)
TURN_THREE = "确认无误，请按你刚才展示的画像保存。"


def tool_names(result) -> list[str]:
    return [
        event["data"]["name"] for event in result if event["event"] == "tool_start"
    ]


def turn(http, session: str, prompt: str) -> list:
    with http.stream(
        "POST",
        "/api/agent/run",
        json={"session_id": session, "operation_id": str(uuid4()), "request": prompt},
    ) as response:
        result = list(events(response))
    wait_idle()
    assert result[-1]["event"] == "done", result[-1]
    return result


def check() -> None:
    patch_default_database("business-agent")
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with Server(app) as server, client(server.base_url) as http:
        session = str(uuid4())
        create_session(http, session)
        first = turn(http, session, TURN_ONE)
        second = turn(http, session, TURN_TWO)
        names = tool_names(first) + tool_names(second)
        assert "get_profile" in names, names
        assert "prepare_profile_update" in names, names

        # 未明确确认前不保存画像。
        profile = http.get("/api/profile")
        assert profile.status_code == 200, profile.text
        assert profile.json() == {"version": None, "content": None}, profile.json()

        history = http.get(f"/api/sessions/{session}/history").json()
        assert set(history) == {"session", "entries", "runs", "steering"}, set(history)
        endings = [
            (
                event["data"]["tool_name"],
                event["data"]["is_error"],
                event["data"]["content"],
            )
            for event in first + second
            if event["event"] == "tool_execution_end"
        ]
        prepared = [
            item
            for item in endings
            if item[0] == "prepare_profile_update" and item[1] is False
        ]
        assert prepared, {"tools": names, "endings": endings}
        proposal = json.loads(prepared[-1][2])
        assert set(proposal) == {
            "proposal_id",
            "profile_id",
            "base_profile_version",
            "payload",
        }, proposal
        assert proposal["profile_id"] == 1
        assert proposal["payload"]["goal"], proposal

        # 用户明确确认后由保存工具落库。
        third = turn(http, session, TURN_THREE)
        saved_names = tool_names(third)
        assert "save_profile_update" in saved_names, saved_names
        saved = http.get("/api/profile").json()
        assert saved["version"] == 1, saved
        assert saved["content"]["goal"], saved

        (EVIDENCE / "business-agent.json").write_text(
            json.dumps(
                {
                    "tools": names + saved_names,
                    "proposal": proposal,
                    "profile": saved,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(
            "真实模型 HTTP 画像流程检查通过："
            + json.dumps(
                {"tools": names + saved_names, "version": saved["version"]},
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    check()
