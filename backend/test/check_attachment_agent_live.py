import base64
import json
from uuid import uuid4

from app.agent.tools.business import business_tool_declarations
from app.infrastructure.persistence.sqlite import database as database_module
from app.interfaces import http as interface
from app.model_config import ModelConfigError, ModelNotConfigured, load_model_config
from test.check_http import events, final_text, validate_events, wait_idle
from test.regression_support import (
    Server,
    client,
    patch_default_database,
    temporary_root,
)

# 真实模型验收：使用应用已保存的模型配置，隔离数据库，仅验证纯附件发送链路。
# 缺少真实凭据或运行条件不足时明确报告阻塞，不宣称完成验收。
EVIDENCE = temporary_root("attachment-agent-live")
SAVING_TOOLS = {"save_plan", "save_profile_update", "save_workout", "update_workout"}
PREPARATION_TOOLS = {
    "prepare_profile_update", "prepare_workout", "prepare_plan",
    "prepare_plan_import", "prepare_plan_adjustment",
}
READ_TOOLS = {"read"}
PLAN_BODY = """# 当前训练计划
周一 推：卧推 4 组 8 次、肩推 3 组 10 次
周三 拉：引体向上 4 组 6 次、划船 3 组 12 次
周五 腿：深蹲 4 组 8 次
"""


def uid() -> str:
    return str(uuid4())


def upload(data: bytes, name: str = "plan.md") -> dict:
    return {
        "kind": "upload",
        "attachment_id": uid(),
        "file_name": name,
        "data_base64": base64.b64encode(data).decode("ascii"),
    }


def turn(http, session: str, request: str, attachments: list) -> tuple[list, dict, dict]:
    body = {"session_id": session, "operation_id": uid(), "request": request,
            "attachments": attachments}
    with http.stream("POST", "/api/agent/run", json=body) as response:
        assert response.status_code == 200, response.text
        wire = list(events(response))
    wait_idle()
    starts, results = validate_events(wire)
    assert wire[-1]["event"] == "done", wire[-1]
    assert wire[-1]["data"]["status"] == "completed", wire[-1]
    return wire, starts, results


def assert_read_only_clarification(starts: dict, clarification: str) -> None:
    unauthorized = [
        data["name"] for data in starts.values()
        if data["name"] in PREPARATION_TOOLS | SAVING_TOOLS
    ]
    assert not unauthorized, unauthorized
    assert clarification.strip(), "真实模型未返回澄清文本"
    assert any(token in clarification for token in ("？", "?", "请问", "请告诉", "希望", "打算")), clarification
    assert "确认保存" not in clarification and "回复确认" not in clarification, clarification


def check() -> dict:
    try:
        config = load_model_config()
    except (ModelNotConfigured, ModelConfigError) as error:
        print(f"BLOCKED: 缺少真实模型凭据，未执行真实模型验收：{error}")
        return {"blocked": str(error)}
    patch_default_database("attachment-agent-live")
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    # 模型可用注册表不提供四项保存工具：保存授权只来自独立确认 HTTP。
    declared = {tool.name for tool in business_tool_declarations()}
    assert not (declared & SAVING_TOOLS), sorted(declared & SAVING_TOOLS)
    plan_path_turns: list[dict] = []
    evidence: dict = {
        "api": config.api,
        "base_url": config.base_url,
        "model": config.model,
        "turns": plan_path_turns,
    }
    with Server(interface.app) as server, client(server.base_url, timeout=180) as http:
        session = uid()
        created = http.post("/api/sessions", json={"session_id": session, "title": "纯附件真实验收"})
        assert created.status_code == 201, created.text
        for index in (1, 2):
            attachment = upload(PLAN_BODY.encode("utf-8"), f"plan{index}.md")
            expected = f"sessions/{session}/attachments/{attachment['attachment_id']}.md"
            wire, starts, results = turn(http, session, "", [attachment])
            read_ids = [call for call, data in starts.items() if data["name"] in READ_TOOLS]
            assert read_ids, [data["name"] for data in starts.values()]
            read_paths = [starts[call]["arguments"].get("path") for call in read_ids]
            assert expected in read_paths, read_paths
            matched = next(call for call in read_ids if starts[call]["arguments"].get("path") == expected)
            assert "卧推" in results[matched] and "深蹲" in results[matched], results[matched]
            clarification = final_text(wire)
            assert_read_only_clarification(starts, clarification)
            plan_path_turns.append({
                "turn": index,
                "read_path": expected,
                "read_chars": len(results[matched]),
                "tool_names": [data["name"] for data in starts.values()],
                "clarification": clarification,
            })
        # 长纯附件：模型须读取附件；续读偏移与实际读取窗口一并记录。
        long_body = "".join(f"第{number:03d}行 训练明细\n" for number in range(1, 301))
        long_attachment = upload(long_body.encode("utf-8"), "long.md")
        long_expected = f"sessions/{session}/attachments/{long_attachment['attachment_id']}.md"
        wire, starts, results = turn(http, session, "", [long_attachment])
        long_reads = [
            (call, data) for call, data in starts.items()
            if data["name"] in READ_TOOLS and data["arguments"].get("path") == long_expected
        ]
        assert long_reads, [data["name"] for data in starts.values()]
        clarification = final_text(wire)
        assert_read_only_clarification(starts, clarification)
        # 必须读到文件末尾：任一读取窗口完整包含第 300 行，提前停止不通过。
        read_through_end = any("300: 第300行" in results[call] for call, _ in long_reads)
        assert read_through_end, [
            (data["arguments"], results[call][:120]) for call, data in long_reads
        ]
        plan_path_turns.append({
            "turn": "long",
            "read_path": long_expected,
            "read_calls": [
                {"offset": data["arguments"].get("offset"), "limit": data["arguments"].get("limit"),
                 "chars": len(results[call])}
                for call, data in long_reads
            ],
            "continuation_hint": any("继续读取" in results[call] for call, _ in long_reads),
            "read_through_line_300": read_through_end,
            "tool_names": [data["name"] for data in starts.values()],
            "clarification": clarification,
        })
        plans = http.get("/api/plans")
        assert plans.status_code == 200 and plans.json() == [], plans.text
        current = http.get("/api/plans/current")
        assert current.status_code == 200 and current.json() == {"id": None, "content": None}, current.text
        evidence["plans"] = plans.json()
        evidence["current_plan"] = current.json()
        evidence["database"] = str(database_module.default_database_path())
    (EVIDENCE / "evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("PASS: 真实模型对纯附件主动 read；连续发送与长附件均触发读取，读取内容正确；"
          "处理要求明确前无业务准备、保存或更新，读完询问处理意图")
    print("Evidence:", EVIDENCE)
    return evidence


if __name__ == "__main__":
    check()
