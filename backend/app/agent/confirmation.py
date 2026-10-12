from time import time_ns
from uuid import uuid4

from app.agent.tool import run_tool_call
from app.ai.messages import (
    AssistantMessage,
    TextContent,
    ToolCall,
    Usage,
    text_projection,
)
from app.ai.types import check_cancelled


async def run_confirmation(
    confirmation, confirmation_entry_id, tool_name, *, bind_tools,
    save_message, emit, before_tool_call, signal, contains_credentials,
) -> str:
    # 用户点击直接执行绑定目标，整个确认运行不消耗模型 token。
    usage = Usage(input=0, output=0, cache_read=0, cache_write=0, total_tokens=0)

    async def publish_message(content, stop_reason):
        message_id = str(uuid4())
        message = AssistantMessage(
            role="assistant", content=content, api="fit-agent", provider="local",
            model="confirmation", usage=usage, stop_reason=stop_reason,
            timestamp=time_ns() // 1_000_000,
        )
        check_cancelled(signal)
        await emit({"type": "message_start", "message_id": message_id, "message": message})
        await save_message(message_id, message)
        await emit({"type": "message_end", "message_id": message_id, "message": message})
        return message_id

    tool_call = ToolCall(
        type="toolCall", id=str(uuid4()), name=tool_name,
        arguments={
            "proposal_id": confirmation.proposal_id,
            "display_entry_id": confirmation.display_entry_id,
            "confirmation_entry_id": confirmation_entry_id,
        },
    )
    source_id = await publish_message([tool_call], "toolUse")
    tools = bind_tools(source_id)
    await emit({
        "type": "tool_start", "tool_call_id": tool_call.id,
        "name": tool_call.name, "arguments": tool_call.arguments,
    })
    result = await run_tool_call(
        tool_call, tools=tools,
        declared={name: tool.definition() for name, tool in tools.items()},
        before_tool_call=before_tool_call, signal=signal,
        contains_credentials=contains_credentials,
    )
    node_id = str(uuid4())
    await save_message(node_id, result)
    await emit({
        "type": "tool_result", "message_id": node_id,
        "tool_call_id": result.tool_call_id, "content": text_projection(result.content),
        "is_error": result.is_error,
    })
    text = text_projection(result.content) if result.is_error else "已保存" + confirmation.text[4:] + "。"
    await publish_message([TextContent(type="text", text=text)], "stop")
    return "stop"
