import asyncio
import json
from pathlib import Path
from queue import Empty
from tempfile import TemporaryDirectory
from threading import Event
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.agent.events import AgentEvent
from app.agent.tool import (
    AgentTool,
    AgentToolResult,
    FinalizedToolCall,
    run_tool_batch,
    run_tool_call,
)
from app.agent.tools.files import create_file_tools
from app.ai.messages import (
    TextContent,
    ToolCall,
    ToolResultMessage,
    text_projection,
)
from app.interfaces.http import (
    CredentialDetectedError,
    CredentialFilter,
    RunState,
    encode,
    public_message,
    public_tool_execution_end,
    public_tool_execution_update,
)
from test.regression_support import TEST_SESSION, session_workspace


class ProbeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str
    value: int = Field(default=1, ge=1)


def internal_event(finalized: FinalizedToolCall) -> dict:
    # 复刻 agent_loop.emit_tool_finalized 的内部字段形状。
    message = finalized.message
    return {
        "type": "tool_execution_end",
        "tool_call_id": message.tool_call_id,
        "name": message.tool_name,
        "content": text_projection(message.content),
        "is_error": message.is_error,
        "duration_ms": finalized.duration_ms,
        "postprocess_ms": finalized.postprocess_ms,
    }


def drain(state: RunState) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    while True:
        try:
            events.append(state.events.get_nowait())
        except Empty:
            return events


def check_projection() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        workspace, prefix = session_workspace(root)
        (workspace / "note.txt").write_text("hello world", encoding="utf-8")
        tools = create_file_tools(TEST_SESSION, tmp_root=root)
        read = tools["read"]
        message = asyncio.run(
            run_tool_call(
                ToolCall(
                    type="toolCall",
                    id="tc-read",
                    name="read",
                    arguments={"path": f"{prefix}/note.txt"},
                ),
                tools={"read": read},
                declared={"read": read.definition()},
            )
        )
        assert message.is_error is False
        public = public_tool_execution_end(internal_event(FinalizedToolCall(0, message)))
        assert set(public) == {"tool_call_id", "tool_name", "content", "is_error"}, public
        assert public["tool_call_id"] == "tc-read" and public["tool_name"] == "read"
        assert public["content"] == text_projection(message.content)
        assert "hello world" in public["content"] and public["is_error"] is False

        # 完成事件的 content 与 tool_result 使用同一文本投影。
        committed = public_message(message)
        assert committed["role"] == "toolResult"
        assert committed["content"] == public["content"]

        failure = ToolResultMessage(
            role="toolResult",
            tool_call_id="tc-fail",
            tool_name="read",
            content=[TextContent(type="text", text="工具执行失败：没有此类文件")],
            is_error=True,
            timestamp=0,
        )
        failed = public_tool_execution_end(internal_event(FinalizedToolCall(0, failure)))
        assert failed["is_error"] is True and failed["content"] == "工具执行失败：没有此类文件"


def check_publish_and_encode() -> None:
    state = RunState(uuid4())
    state.publish(
        "tool_execution_end",
        {
            "tool_call_id": "tc-publish",
            "tool_name": "read",
            "content": "结果",
            "is_error": False,
        },
    )
    event = state.events.get_nowait()
    assert event.event == "tool_execution_end"
    assert event.data == {
        "run_id": str(state.run_id),
        "tool_call_id": "tc-publish",
        "tool_name": "read",
        "content": "结果",
        "is_error": False,
    }, event.data

    encoded = encode(event)
    assert encoded.startswith("event: tool_execution_end\ndata: "), encoded
    payload = json.loads(encoded.split("data: ", 1)[1])
    assert payload["run_id"] == str(state.run_id)
    assert "entry_id" not in payload and "parent_id" not in payload


def check_execution_update_projection() -> None:
    data = public_tool_execution_update(
        "tc-update", "read", [TextContent(type="text", text="进度快照")], False
    )
    assert data == {
        "tool_call_id": "tc-update",
        "tool_name": "read",
        "content": "进度快照",
        "is_error": False,
    }, data
    assert "entry_id" not in data and "parent_id" not in data

    state = RunState(uuid4())
    state.publish("tool_execution_update", data)
    event = state.events.get_nowait()
    assert event.event == "tool_execution_update"
    assert event.data["run_id"] == str(state.run_id)
    assert event.data["tool_call_id"] == "tc-update"
    assert encode(event).startswith("event: tool_execution_update\ndata: ")

    guard = CredentialFilter(("SECRET-TOKEN",))
    leaked = public_tool_execution_update(
        "tc-update",
        "read",
        [TextContent(type="text", text="含 SECRET-TOKEN 的进度")],
        False,
    )
    assert guard.contains(leaked) is True
    assert guard.contains(data) is False


def check_credential_guard() -> None:
    guard = CredentialFilter(("SECRET-TOKEN",))
    leaked = public_tool_execution_end(
        {
            "tool_call_id": "tc",
            "name": "read",
            "content": "包含 SECRET-TOKEN 的结果",
            "is_error": False,
        }
    )
    assert guard.contains(leaked) is True
    assert guard.contains({**leaked, "tool_name": "SECRET-TOKEN"}) is True
    clean = public_tool_execution_end(
        {"tool_call_id": "tc", "name": "read", "content": "普通结果", "is_error": False}
    )
    assert guard.contains(clean) is False


def check_completion_order() -> None:
    fast_finalized = Event()
    state = RunState(uuid4())

    def execute(tool_call_id, params: ProbeArguments, signal, on_update):
        if params.name == "slow":
            assert fast_finalized.wait(timeout=5), "fast 未先定稿"
        return AgentToolResult([TextContent(type="text", text=params.name)])

    tool = AgentTool("probe", "probe", ProbeArguments, execute, execution_mode="parallel")

    async def on_finalized(finalized: FinalizedToolCall):
        state.publish("tool_execution_end", public_tool_execution_end(internal_event(finalized)))
        if finalized.message.tool_call_id == "fast":
            fast_finalized.set()

    calls = [
        ToolCall(type="toolCall", id="slow", name="probe", arguments={"name": "slow"}),
        ToolCall(type="toolCall", id="fast", name="probe", arguments={"name": "fast"}),
        ToolCall(type="toolCall", id="missing", name="absented", arguments={"name": "x"}),
    ]
    result = asyncio.run(
        run_tool_batch(
            calls,
            tools={"probe": tool},
            declared={"probe": tool.definition()},
            execution_mode="parallel",
            on_tool_finalized=on_finalized,
        )
    )
    events = drain(state)
    published = [event.data["tool_call_id"] for event in events]
    # 准备阶段失败（absented 工具不存在）也在结果定稿时发布；并行完成按实际完成顺序。
    assert sorted(published) == ["fast", "missing", "slow"], published
    assert published.index("fast") < published.index("slow")
    assert all(event.event == "tool_execution_end" for event in events)
    # 结果数组仍按原始调用顺序，且完成事件对应真实文本投影。
    assert [message.tool_call_id for message in result.messages] == ["slow", "fast", "missing"]
    bodies = {event.data["tool_call_id"]: event.data["content"] for event in events}
    assert bodies["slow"] == "slow" and bodies["fast"] == "fast"
    assert bodies["missing"] == "工具不存在：absented"


def check_credential_stops_publication() -> None:
    guard = CredentialFilter(("SECRET-TOKEN",))
    executed: list[str] = []
    published: list[str] = []

    def execute(tool_call_id, params: ProbeArguments, signal, on_update):
        executed.append(tool_call_id)
        return AgentToolResult([TextContent(type="text", text="SECRET-TOKEN")])

    tool = AgentTool("leak", "leak", ProbeArguments, execute, execution_mode="sequential")
    calls = [
        ToolCall(type="toolCall", id="c-leak", name="leak", arguments={"name": "a"}),
        ToolCall(type="toolCall", id="c-after", name="leak", arguments={"name": "b"}),
    ]

    async def on_finalized(finalized: FinalizedToolCall):
        data = public_tool_execution_end(internal_event(finalized))
        if guard.contains(data):
            raise CredentialDetectedError()
        published.append(data["tool_call_id"])

    result = asyncio.run(
        run_tool_batch(
            calls,
            tools={"leak": tool},
            declared={"leak": tool.definition()},
            execution_mode="sequential",
            on_tool_finalized=on_finalized,
        )
    )
    assert isinstance(result.failure, CredentialDetectedError)
    assert published == []
    assert result.messages == []
    assert executed == ["c-leak"], executed


def check_http_validator() -> None:
    from test.check_http import validate_events

    run_id = str(uuid4())
    user_node = str(uuid4())
    assistant_id = str(uuid4())
    tool_node = str(uuid4())
    final_id = str(uuid4())
    calling = [
        {"content_index": 0, "type": "text", "text": "读取文件。"},
        {"content_index": 1, "type": "tool_call", "tool_call_id": "tc-v", "name": "read", "arguments": {"path": "a.txt"}},
    ]
    finishing = [{"content_index": 0, "type": "text", "text": "完成。"}]
    result = [
        {"event": "message_start", "data": {"run_id": run_id, "message_id": assistant_id, "content": calling}},
        {"event": "message_end", "data": {"run_id": run_id, "message_id": assistant_id, "content": calling, "stop_reason": "toolUse", "entry_id": assistant_id, "parent_id": user_node}},
        {"event": "tool_start", "data": {"run_id": run_id, "tool_call_id": "tc-v", "name": "read", "arguments": {"path": "a.txt"}}},
        {"event": "tool_execution_update", "data": {"run_id": run_id, "tool_call_id": "tc-v", "tool_name": "read", "content": "读取中", "is_error": False}},
        {"event": "tool_execution_end", "data": {"run_id": run_id, "tool_call_id": "tc-v", "tool_name": "read", "content": "文件内容", "is_error": False}},
        {"event": "tool_result", "data": {"run_id": run_id, "tool_call_id": "tc-v", "content": "文件内容", "is_error": False, "entry_id": tool_node, "parent_id": assistant_id}},
        {"event": "message_start", "data": {"run_id": run_id, "message_id": final_id, "content": finishing}},
        {"event": "message_end", "data": {"run_id": run_id, "message_id": final_id, "content": finishing, "stop_reason": "stop", "entry_id": final_id, "parent_id": tool_node}},
        {"event": "done", "data": {"run_id": run_id, "status": "completed", "stop_reason": "stop"}},
    ]
    starts, results = validate_events(result)
    assert starts.keys() == results.keys() == {"tc-v"}
    without_final = [item for item in result if item["event"] != "tool_execution_end"]
    try:
        validate_events(without_final)
    except AssertionError:
        pass
    else:
        raise AssertionError("缺少完成事件的 tool_result 应被拒绝")


def check() -> None:
    check_projection()
    check_publish_and_encode()
    check_execution_update_projection()
    check_credential_guard()
    check_completion_order()
    check_credential_stops_publication()
    check_http_validator()
    print(
        "PASS: 完成事件公开字段与文本投影、run_id 注入、无 entry_id/parent_id、"
        "完成顺序（含准备阶段失败）、凭据检查后才公开并停止后续调度、"
        "既有 SSE 验证器接受完成事件并要求保存确认前先定稿"
    )


if __name__ == "__main__":
    check()
