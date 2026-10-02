from pydantic import TypeAdapter

from src.ai.messages import (
    AssistantMessage,
    Message,
    SystemMessage,
    Tool,
    ToolCall,
    ToolResultMessage,
)

_message_list_adapter = TypeAdapter(list[Message])


def _project_messages(messages: list[Message]) -> list[Message]:
    validated = _message_list_adapter.validate_python(messages, strict=True)
    return _message_list_adapter.validate_python(
        [message.model_dump(exclude_unset=True) for message in validated], strict=True
    )


def validate_tool_pairs(messages: list[Message]) -> None:
    seen: set[str] = set()
    pending: dict[str, str] = {}
    for message in messages:
        if pending and message.role in {"user", "assistant"}:
            raise ValueError("工具结果未完整配对，无法开始下一轮消息")
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolCall):
                    if block.id in seen:
                        raise ValueError(f"重复工具调用 ID: {block.id}")
                    seen.add(block.id)
                    pending[block.id] = block.name
        elif isinstance(message, ToolResultMessage):
            if message.tool_call_id not in pending:
                raise ValueError(f"工具结果缺少待配对调用: {message.tool_call_id}")
            if pending[message.tool_call_id] != message.tool_name:
                raise ValueError(f"工具结果名称不匹配: {message.tool_call_id}")
            del pending[message.tool_call_id]
    if pending:
        raise ValueError(f"工具调用缺少结果: {', '.join(pending)}")


def normalize_context(messages: list[Message]) -> list[Message]:
    projected = _project_messages(messages)
    validate_tool_pairs(projected)
    return projected


def get_current_system_message(messages: list[Message]) -> SystemMessage | None:
    content: list[str] = []
    sections: dict[str, str] = {}
    tools: dict[str, Tool] = {}
    timestamp: int | float | None = None
    for message in _project_messages(messages):
        if not isinstance(message, SystemMessage):
            continue
        if timestamp is None:
            timestamp = message.timestamp
        text = (
            message.content
            if isinstance(message.content, str)
            else "\n".join(block.text for block in message.content)
        )
        if text:
            content.append(text)
        if message.sections is not None:
            for name, value in message.sections.items():
                if value is None:
                    sections.pop(name, None)
                else:
                    sections[name] = value
        if message.tools_removed is not None:
            for tool in message.tools_removed:
                tools.pop(tool.name, None)
        if message.tools_added is not None:
            for tool in message.tools_added:
                tools[tool.name] = tool
    if timestamp is None:
        return None
    state = {
        "role": "system",
        "content": "\n\n".join(content),
        "timestamp": timestamp,
    }
    if sections:
        state["sections"] = sections
    if tools:
        state["tools_added"] = list(tools.values())
    return SystemMessage.model_validate(state)


def get_current_system_prompt(messages: list[Message]) -> str:
    system = get_current_system_message(messages)
    if system is None:
        return ""
    parts = [system.content]
    if system.sections is not None:
        parts.extend(system.sections.values())
    return "\n\n".join(part for part in parts if part)


def get_current_tools(messages: list[Message]) -> list[Tool]:
    system = get_current_system_message(messages)
    if system is None or system.tools_added is None:
        return []
    return system.tools_added
