import pytest

from src.ai.context import (
    get_current_system_message,
    get_current_system_prompt,
    get_current_tools,
    normalize_context,
)
from src.ai.messages import (
    AssistantMessage,
    SystemMessage,
    Tool,
    ToolCall,
    ToolReference,
    ToolResultMessage,
)


def test_context_snake_case():
    first = Tool(name="first", description="first", parameters={})
    second = Tool(name="second", description="second", parameters={})
    systems = [
        SystemMessage(
            role="system", content="prompt", timestamp=1,
            tools_added=[first], sections={"rules": "rule"},
        ),
        SystemMessage(
            role="system", content="extra", timestamp=2,
            tools_removed=[ToolReference(name="first")], tools_added=[second],
        ),
    ]
    system = get_current_system_message(systems)
    assert system is not None
    assert system.tools_added == [second]
    assert "tools_added" in system.model_dump(exclude_unset=True)
    assert get_current_tools(systems) == [second]
    assert get_current_system_prompt(systems) == "prompt\n\nextra\n\nrule"
    assert get_current_tools([]) == []

    call = AssistantMessage(
        role="assistant", content=[ToolCall(
            type="toolCall", id="call_1", name="second", arguments={},
        )], api="openai-completions", provider="test", model="test",
        timestamp=3, usage=None, stop_reason="pending",
    )
    result = ToolResultMessage(
        role="toolResult", tool_call_id="call_1", tool_name="second",
        content=[], is_error=False, timestamp=4,
    )
    normalized = normalize_context([*systems, call, result])
    assert normalized[-1].tool_call_id == "call_1"
    assert normalized[-1].model_dump(exclude_unset=True)["tool_name"] == "second"
    with pytest.raises(ValueError, match="工具调用缺少结果"):
        normalize_context([call])
    with pytest.raises(ValueError, match="工具结果名称不匹配"):
        normalize_context([call, result.model_copy(update={"tool_name": "first"})])
    with pytest.raises(ValueError, match="工具结果缺少待配对调用"):
        normalize_context([result])
